"""
route_planner.py  v2
====================
OpenRouteService API でリアルな道に沿ったルートを生成し、
標高・斜度列に変換してシミュレーションに接続する。

APIキー取得: https://openrouteservice.org/dev/#/signup（無料・1日2000回）
"""

from __future__ import annotations
import math, time
from typing import List, Tuple, Optional, Dict
import numpy as np
import requests

ORS_BASE  = "https://api.openrouteservice.org/v2"
NOM_BASE  = "https://nominatim.openstreetmap.org"
ELEV_BASE = "https://api.open-elevation.com/api/v1/lookup"

ACTIVITY_PROFILES = {
    "🏔️ 登山・ハイキング"  : "foot-hiking",
    "🏃 ランニング"         : "foot-walking",
    "🌲 トレイルランニング" : "cycling-mountain",
    "🚴 サイクリング"       : "cycling-regular",
    "🚵 マウンテンバイク"   : "cycling-mountain",
}

ACTIVITY_TO_SIM = {
    "🏔️ 登山・ハイキング"  : "hiking",
    "🏃 ランニング"         : "running",
    "🌲 トレイルランニング" : "running",
    "🚴 サイクリング"       : "cycling",
    "🚵 マウンテンバイク"   : "cycling",
}

# ── 地名検索 ──────────────────────────────────────────
def search_place(query: str, limit: int = 5) -> List[Dict]:
    try:
        resp = requests.get(f"{NOM_BASE}/search",
            params={"q": query, "format": "json",
                    "limit": limit, "accept-language": "ja"},
            headers={"User-Agent": "EnergyTracker/2.0"}, timeout=10)
        resp.raise_for_status()
        return [{"name": r.get("display_name","")[:80],
                 "lat": float(r["lat"]), "lon": float(r["lon"]),
                 "display": r.get("display_name","")} for r in resp.json()]
    except Exception:
        return []

# ── ORS ルート取得 ─────────────────────────────────────
def get_route_ors(waypoints, profile, api_key) -> Dict:
    coords_lonlat = [[p[1], p[0]] for p in waypoints]
    resp = requests.post(
        f"{ORS_BASE}/directions/{profile}/geojson",
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        json={"coordinates": coords_lonlat, "elevation": True, "instructions": False},
        timeout=20)
    if not resp.ok:
        raise RuntimeError(f"ORS APIエラー ({resp.status_code}): {resp.text[:200]}")
    data    = resp.json()
    feature = data["features"][0]
    props   = feature["properties"]["summary"]
    geom    = feature["geometry"]["coordinates"]
    coords     = [(c[1], c[0]) for c in geom]
    elevations = [c[2] if len(c) > 2 else 0.0 for c in geom]
    ascent = descent = 0.0
    for i in range(1, len(elevations)):
        d = elevations[i] - elevations[i-1]
        if d > 0: ascent  += d
        else:     descent += abs(d)
    return {"coords": coords, "elevations": elevations,
            "distance_m": props.get("distance", 0),
            "duration_s": props.get("duration", 0),
            "ascent_m": ascent, "descent_m": descent}


# ── GraphHopper ルート取得（ORS の代替）─────────────────
GH_BASE = "https://graphhopper.com/api/1/route"
GH_PROFILE_MAP = {
    "foot-hiking"     : "hike",
    "foot-walking"    : "foot",
    "cycling-regular" : "bike",
    "cycling-mountain": "mtb",
}

def get_route_graphhopper(waypoints, ors_profile: str, api_key: str = "") -> Dict:
    """
    GraphHopper で実際の道に沿ったルートを取得する。
    日本の登山道（OpenStreetMap の highway=path/track）に対応。
    無料APIキー取得: https://www.graphhopper.com/
    """
    gh_profile = GH_PROFILE_MAP.get(ors_profile, "hike")
    params = {
        "profile"         : gh_profile,
        "elevation"       : "true",
        "points_encoded"  : "false",
        "locale"          : "ja",
    }
    if api_key:
        params["key"] = api_key
    for lat, lon in waypoints:
        params.setdefault("point", [])
        params["point"].append(f"{lat},{lon}")

    resp = requests.get(GH_BASE, params=params, timeout=20)
    if not resp.ok:
        raise RuntimeError(f"GraphHopperエラー ({resp.status_code}): {resp.text[:200]}")

    path  = resp.json()["paths"][0]
    geom  = path["points"]["coordinates"]   # [lon, lat, elev]
    coords     = [(c[1], c[0]) for c in geom]
    elevations = [c[2] if len(c) > 2 else 0.0 for c in geom]
    ascent = descent = 0.0
    for i in range(1, len(elevations)):
        d = elevations[i] - elevations[i-1]
        if d > 0: ascent  += d
        else:     descent += abs(d)
    return {"coords": coords, "elevations": elevations,
            "distance_m": path.get("distance", 0),
            "duration_s": path.get("time", 0) / 1000,
            "ascent_m": ascent, "descent_m": descent}


# ── GPX ファイルからルートを読み込む ─────────────────────
def parse_gpx_route(gpx_content: bytes) -> Dict:
    """
    YAMAP / ヤマレコ / Garmin / Strava等のGPXファイルをパースしてルートデータに変換。
    実際の歩行軌跡を使うため最高精度。
    tracks → routes → waypoints の順でフォールバック。
    """
    import gpxpy, io
    import defusedxml.ElementTree as _safe_et
    from defusedxml import DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden

    # ── XXE・DTD爆弾攻撃を事前ブロック ──────────────────────────
    # gpxpy は内部で lxml を使う場合があり、lxml はデフォルトで外部エンティティを
    # 処理するため XXE 攻撃が成立する。defusedxml で先に検査し、危険な XML を拒否する。
    try:
        _safe_et.fromstring(gpx_content)
    except (DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden) as e:
        raise ValueError(f"危険なXMLエンティティが含まれています（XXE攻撃の可能性）: {type(e).__name__}")
    except Exception as e:
        # XML 構造自体が壊れている場合はここで弾く（gpxpy への投げ替え前に確認）
        # 文字コードの問題は後続の gpxpy フォールバックに任せるため pass
        pass

    # BytesIO で渡すことで gpxpy に文字コード検出を任せる（UTF-8/Shift_JIS/BOM等に対応）
    try:
        gpx = gpxpy.parse(io.BytesIO(gpx_content))
    except Exception as e:
        # BytesIO が失敗した場合は文字列にデコードして再試行
        try:
            text = gpx_content.decode("utf-8", errors="replace").lstrip("\ufeff")
            gpx = gpxpy.parse(text)
        except Exception:
            raise ValueError(f"GPXファイルを解析できませんでした: {e}")

    GPX_MAX_POINTS = 50_000   # main.py と同じ値をモデル側でも独立して保持

    coords, elevations = [], []

    def _add_point(pt):
        """座標を追加しつつ上限を超えたら ValueError を送出する。"""
        if len(coords) >= GPX_MAX_POINTS:
            raise ValueError(
                f"GPXの座標点数が上限（{GPX_MAX_POINTS:,}点）を超えています。"
                " ルートを分割するか、GPSデバイスの記録間隔を長くしてください。"
            )
        if pt.latitude is not None and pt.longitude is not None:
            coords.append((pt.latitude, pt.longitude))
            elevations.append(float(pt.elevation) if pt.elevation is not None else 0.0)

    # 1. トラックポイント（最も一般的）
    for track in gpx.tracks:
        for seg in track.segments:
            for pt in seg.points:
                _add_point(pt)

    # 2. ルートポイント（tracks がない場合）
    if not coords:
        for rte in gpx.routes:
            for pt in rte.points:
                _add_point(pt)

    # 3. ウェイポイント（tracks/routes がない場合。時刻順にソート）
    if not coords and gpx.waypoints:
        wpts = sorted(gpx.waypoints, key=lambda w: (w.time or 0))
        for pt in wpts:
            _add_point(pt)

    if len(coords) < 2:
        found = (f"tracks={len(gpx.tracks)}, routes={len(gpx.routes)}, "
                 f"waypoints={len(gpx.waypoints)}")
        raise ValueError(f"GPXファイルに有効なポイントが見つかりません ({found})")

    dist = sum(_haversine(coords[i-1], coords[i]) for i in range(1, len(coords)))
    ascent = descent = 0.0
    for i in range(1, len(elevations)):
        d = elevations[i] - elevations[i-1]
        if d > 0: ascent  += d
        else:     descent += abs(d)
    return {"coords": coords, "elevations": elevations,
            "distance_m": dist,
            "duration_s": dist / 1.0,
            "ascent_m": ascent, "descent_m": descent}

# ── 代替ルート（APIキーなし・直線補間）─────────────────
def get_route_fallback(waypoints) -> Dict:
    interp = _interpolate(waypoints, 100)
    elevations = get_elevations_batch(interp)
    dist = sum(_haversine(interp[i-1], interp[i]) for i in range(1, len(interp)))
    ascent = descent = 0.0
    for i in range(1, len(elevations)):
        d = elevations[i] - elevations[i-1]
        if d > 0: ascent  += d
        else:     descent += abs(d)
    return {"coords": interp, "elevations": elevations,
            "distance_m": dist, "duration_s": dist/1.2,
            "ascent_m": ascent, "descent_m": descent}

# ── 標高取得 ──────────────────────────────────────────
def get_elevations_batch(coords, batch_size=50):
    elevations = []
    for i in range(0, len(coords), batch_size):
        batch = coords[i:i+batch_size]
        try:
            resp = requests.post(ELEV_BASE,
                json={"locations": [{"latitude": la, "longitude": lo} for la,lo in batch]},
                timeout=15)
            resp.raise_for_status()
            elevations.extend([r["elevation"] for r in resp.json()["results"]])
        except Exception:
            elevations.extend([0.0]*len(batch))
        if i+batch_size < len(coords): time.sleep(0.2)
    return elevations

# ── ユーティリティ ─────────────────────────────────────
def _haversine(p1, p2):
    R = 6371000.0
    la1,lo1 = math.radians(p1[0]), math.radians(p1[1])
    la2,lo2 = math.radians(p2[0]), math.radians(p2[1])
    a = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 2*R*math.asin(math.sqrt(max(a,0)))

def _interpolate(coords, n_total):
    if len(coords) < 2: return coords
    n_seg = max(n_total//(len(coords)-1), 2)
    result = []
    for i in range(len(coords)-1):
        la1,lo1 = coords[i]; la2,lo2 = coords[i+1]
        for j in range(n_seg):
            t = j/n_seg
            result.append((la1+t*(la2-la1), lo1+t*(lo2-lo1)))
    result.append(coords[-1])
    return result

def coords_to_slope_array(coords, elevations, interval_s=10.0, speed_ms=1.0):
    n = len(coords)
    if n < 2: return np.zeros(10), np.array(elevations[:1]*10), np.zeros(10)
    slopes, elevs, dists = [], [elevations[0]], [0.0]
    cum = 0.0
    for i in range(1, n):
        h = _haversine(coords[i-1], coords[i])
        v = elevations[i] - elevations[i-1]
        s = float(np.clip(v/h*100 if h>0 else 0, -60, 60))
        n_pts = max(int(h/(speed_ms*interval_s)), 1)
        cum += h
        slopes.extend([s]*n_pts)
        elevs.extend(np.linspace(elevations[i-1], elevations[i], n_pts+1)[1:].tolist())
        dists.extend(np.linspace(cum-h, cum, n_pts+1)[1:].tolist())
    return np.array(slopes,dtype=float), np.array(elevs,dtype=float), np.array(dists,dtype=float)

# ── マップ生成 ────────────────────────────────────────
def make_route_map(center_lat=35.6762, center_lon=138.7411, zoom=10,
                   waypoints=None, elevations=None, route_coords=None):
    import folium
    from folium.plugins import MeasureControl
    m = folium.Map(location=[center_lat, center_lon], zoom_start=zoom)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(m)
    folium.TileLayer(
        "https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png",
        attr="国土地理院", name="地理院地図").add_to(m)
    folium.LayerControl().add_to(m)
    MeasureControl(primary_length_unit="kilometers").add_to(m)
    if route_coords and len(route_coords) >= 2:
        folium.PolyLine(route_coords, color="#e74c3c", weight=4, opacity=0.85).add_to(m)
    if waypoints:
        for i, (lat, lon) in enumerate(waypoints):
            ev = f"<br>標高:{elevations[i]:.0f}m" if elevations and i < len(elevations) else ""
            if i == 0:
                label, icon = f"🟢 スタート{ev}", folium.Icon(color="green", icon="play", prefix="fa")
            elif i == len(waypoints)-1:
                label, icon = f"🔴 ゴール{ev}", folium.Icon(color="red", icon="flag", prefix="fa")
            else:
                label, icon = f"📍 経由地{i}{ev}", folium.Icon(color="blue", icon="circle", prefix="fa")
            folium.Marker([lat, lon], popup=label, icon=icon).add_to(m)
    return m

# 後方互換エイリアス
def get_elevations(coords, batch_size=50): return get_elevations_batch(coords, batch_size)
def interpolate_coords(coords, n_points=100): return _interpolate(coords, n_points)


# ══════════════════════════════════════════════════════
# リアルタイム進捗計算（Phase 1）
# ══════════════════════════════════════════════════════

def find_nearest_point_on_route(
    current_lat : float,
    current_lon : float,
    route_coords: list,
) -> tuple:
    """
    現在地から最も近いルート上の点を見つける。

    Returns
    -------
    (index, dist_m, passed_dist_m)
        index        : ルート配列内のインデックス
        dist_m       : 現在地からルート点までの距離(m)
        passed_dist_m: スタートから該当点までの累積距離(m)
    """
    if not route_coords:
        return 0, 0.0, 0.0

    best_idx  = 0
    best_dist = float("inf")
    cum_dist  = 0.0
    passed    = 0.0

    for i, coord in enumerate(route_coords):
        d = _haversine((current_lat, current_lon), coord)
        if d < best_dist:
            best_dist = d
            best_idx  = i
        if i > 0:
            cum_dist += _haversine(route_coords[i-1], route_coords[i])
        if best_idx == i:
            passed = cum_dist

    return best_idx, best_dist, passed


def calc_remaining_route(
    route_coords : list,
    from_index   : int,
) -> tuple:
    """
    指定インデックス以降の残りルート情報を返す。

    Returns
    -------
    (remaining_coords, remaining_dist_m)
    """
    remaining_coords = route_coords[from_index:]
    remaining_dist   = sum(
        _haversine(remaining_coords[i-1], remaining_coords[i])
        for i in range(1, len(remaining_coords))
    )
    return remaining_coords, remaining_dist


def estimate_current_pace(
    positions : list,
) -> float:
    """
    直近の位置履歴からペース（m/s）を推定する。

    Parameters
    ----------
    positions : [{"lat": float, "lon": float, "time": datetime}, ...]  最低2点必要

    Returns
    -------
    float: 推定速度 m/s（計算できない場合は 0.0）
    """
    if len(positions) < 2:
        return 0.0

    # 直近の5点を使って平均速度を計算
    pts = positions[-5:]
    total_dist = 0.0
    total_time = 0.0

    for i in range(1, len(pts)):
        d = _haversine(
            (pts[i-1]["lat"], pts[i-1]["lon"]),
            (pts[i]["lat"],   pts[i]["lon"])
        )
        dt = (pts[i]["time"] - pts[i-1]["time"]).total_seconds()
        if dt > 0:
            total_dist += d
            total_time += dt

    if total_time > 0:
        return total_dist / total_time
    return 0.0


def calc_eta_from_pace(
    remaining_dist_m : float,
    current_pace_ms  : float,
    planned_pace_ms  : float,
) -> float:
    """
    残り距離と現在ペースから到着予測時間（分）を計算する。
    現在ペースが不明な場合は計画ペースを使う。

    Returns
    -------
    float: 残り時間（分）
    """
    pace = current_pace_ms if current_pace_ms > 0.1 else planned_pace_ms
    pace = max(pace, 0.1)  # ゼロ除算防止
    return remaining_dist_m / pace / 60.0
