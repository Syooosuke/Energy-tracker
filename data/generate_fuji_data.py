"""
富士山 吉田ルート GPX生成スクリプト
======================================
ルート: 富士山五合目 (2305m) → 山頂 (3776m)
所要時間: 約6時間（上り）
距離: 約7.5km（水平距離）

実際の吉田ルートに基づいた区間設定:
  区間1  五合目  → 六合目  (2305m → 2390m)  緩登り
  区間2  六合目  → 七合目  (2390m → 2700m)  中斜面
  区間3  七合目  → 八合目  (2700m → 3100m)  急登
  区間4  八合目  → 本八合目 (3100m → 3400m)  急登+岩場
  区間5  本八合目→ 九合目  (3400m → 3600m)  急登（高度障害域）
  区間6  九合目  → 山頂    (3600m → 3776m)  最急登
"""

import numpy as np
import gpxpy
import gpxpy.gpx
import os
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH     = os.path.join(PROJECT_ROOT, "data/raw/routes/fuji_yoshida.gpx")
HR_OUT_PATH  = os.path.join(PROJECT_ROOT, "data/raw/heartrate/fuji_hr.csv")

np.random.seed(7)

# ─────────────────────────────────────────────────────
# 吉田ルートの実測値に基づく区間定義
# ─────────────────────────────────────────────────────
SEGMENTS = [
    # (区間名, 開始標高m, 終了標高m, 水平距離m, 所要時間min, 基準斜度%, 開始lat, 開始lon, 終了lat, 終了lon)
    ("五合目→六合目",   2305, 2390,  900,  25,  9.4, 35.3875, 138.7313, 35.3820, 138.7305),
    ("六合目→七合目",   2390, 2700, 1200,  55, 25.8, 35.3820, 138.7305, 35.3755, 138.7292),
    ("七合目→八合目",   2700, 3100, 1100,  75, 36.4, 35.3755, 138.7292, 35.3700, 138.7278),
    ("八合目→本八合目", 3100, 3400,  800,  65, 37.5, 35.3700, 138.7278, 35.3660, 138.7272),
    ("本八合目→九合目", 3400, 3600,  700,  60, 28.6, 35.3660, 138.7272, 35.3635, 138.7273),
    ("九合目→山頂",     3600, 3776,  600,  60, 29.3, 35.3635, 138.7273, 35.3622, 138.7274),
]

INTERVAL_S   = 10       # GPSサンプリング間隔（秒）
START_TIME   = datetime(2024, 7, 20, 5, 0, 0)   # 出発: 早朝5時

# ─────────────────────────────────────────────────────
# 区間ごとにトラックポイントを生成
# ─────────────────────────────────────────────────────
all_points = []
current_time = START_TIME

for seg_name, elev_start, elev_end, h_dist_m, duration_min, slope_ref, lat0, lon0, lat1, lon1 in SEGMENTS:

    n_pts = int(duration_min * 60 / INTERVAL_S)

    # 標高プロファイル（区間内は線形 + 小ノイズ）
    elev_base  = np.linspace(elev_start, elev_end, n_pts)
    elev_noise = np.random.normal(0, 2.5, n_pts)   # ±2.5m のばらつき
    elev       = elev_base + elev_noise
    elev[0]    = elev_start   # 始点・終点は正確に
    elev[-1]   = elev_end

    # 緯度経度（区間の始点→終点を線形補間 + 小ノイズ）
    lats = np.linspace(lat0, lat1, n_pts) + np.random.normal(0, 0.00003, n_pts)
    lons = np.linspace(lon0, lon1, n_pts) + np.random.normal(0, 0.00003, n_pts)
    lats[0], lats[-1] = lat0, lat1
    lons[0], lons[-1] = lon0, lon1

    for i in range(n_pts):
        pt = gpxpy.gpx.GPXTrackPoint(
            latitude  = lats[i],
            longitude = lons[i],
            elevation = elev[i],
            time      = current_time,
        )
        all_points.append(pt)
        current_time += timedelta(seconds=INTERVAL_S)

    print(f"  [{seg_name}]  {elev_start}m → {elev_end}m  "
          f"{n_pts}点  ({duration_min}分)")

total_min = (current_time - START_TIME).total_seconds() / 60

# ─────────────────────────────────────────────────────
# GPX ファイルの書き出し
# ─────────────────────────────────────────────────────
gpx     = gpxpy.gpx.GPX()
gpx.name = "富士山 吉田ルート"
gpx.description = "五合目 → 山頂  約6時間"

track   = gpxpy.gpx.GPXTrack(name="吉田ルート 上り")
gpx.tracks.append(track)
segment = gpxpy.gpx.GPXTrackSegment()
track.segments.append(segment)
segment.points.extend(all_points)

os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write(gpx.to_xml())

print(f"\n[GPX] 生成完了: {OUT_PATH}")
print(f"      総ポイント数: {len(all_points):,}")
print(f"      総時間     : {total_min:.0f} 分 ({total_min/60:.1f} 時間)")
print(f"      標高範囲   : 2305m → 3776m (+1471m)")

# ─────────────────────────────────────────────────────
# 心拍数CSVの生成（高度・斜度・時間疲労に応じたモデル）
# ─────────────────────────────────────────────────────
import pandas as pd

timestamps = [pt.time for pt in all_points]
elevations = [pt.elevation for pt in all_points]
n          = len(all_points)

# 心拍モデル:
#   - 標高が上がるにつれ同じ強度でも心拍が上昇（高度障害）
#   - 時間とともに疲労による基礎心拍上昇
#   - 斜度変化に即応

hr_base    = 75                             # 安静時心拍
fatigue    = np.linspace(0, 25, n)          # 時間疲労（最大+25bpm）
altitude_effect = np.array([               # 高度効果（3000m超で顕著）
    max(0, (e - 2500) / 100) for e in elevations
])
slope_effect = np.diff([pt.elevation for pt in all_points], prepend=elevations[0])
slope_effect = np.clip(slope_effect * 8, -10, 40)   # 斜度変化 → 心拍変動

hr = hr_base + fatigue + altitude_effect + slope_effect
hr += np.random.normal(0, 4, n)
hr = np.clip(hr, 60, 185).astype(int)

df_hr = pd.DataFrame({
    "timestamp"  : timestamps,
    "heart_rate" : hr,
})
os.makedirs(os.path.dirname(HR_OUT_PATH), exist_ok=True)
df_hr.to_csv(HR_OUT_PATH, index=False)

print(f"\n[HR]  生成完了: {HR_OUT_PATH}")
print(f"      心拍範囲   : {hr.min()} 〜 {hr.max()} bpm")
print(f"      平均心拍   : {hr.mean():.0f} bpm")
print(f"\n生成完了。")
