"""
app.py — Energy Tracker  (redesigned)
デフォルト: マップルートプランナー → 自動シミュレーション
"""

import os, sys, math, io
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
import gpxpy

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "models"))

from ke_estimator   import UserProfile, ACTIVITY_HIKING, ACTIVITY_RUNNING, ACTIVITY_CYCLING
from pk_model_scipy import SupplyEvent, AlertConfig, run_pk_model
from supply_optimizer import optimize_supply_plan
from report_generator import generate_report_pdf
from route_planner  import (search_place, get_route_ors, get_route_fallback,
                             get_route_graphhopper, parse_gpx_route,
                             make_route_map, coords_to_slope_array,
                             ACTIVITY_PROFILES, ACTIVITY_TO_SIM)
from weather        import (fetch_weather, fetch_weather_dummy,
                            calc_weather_factor, WeatherData,
                            fetch_weather_forecast)
from route_planner  import (find_nearest_point_on_route, calc_remaining_route,
                            estimate_current_pace, calc_eta_from_pace)  # Phase 1
from pace_predictor import PaceRegressor, optimize_pace, running_speed as _run_spd, tobler_speed as _tobler

# ══════════════════════════════════════════════════════
# ページ設定
# ══════════════════════════════════════════════════════
st.set_page_config(page_title="Energy Tracker", page_icon="⛰️", layout="wide")

ORS_KEY = os.environ.get("ORS_API_KEY", "")
OWM_KEY = os.environ.get("OWM_API_KEY", "")

# ══════════════════════════════════════════════════════
# ユーティリティ
# ══════════════════════════════════════════════════════
def fmt_minutes(m):
    h = int(m)//60; mm = int(m)%60
    return f"{h}時間{mm:02d}分" if h>0 else f"{mm}分"

def load_default_data():
    p = os.path.join(os.path.dirname(__file__), "data", "processed", "merged_log.csv")
    if not os.path.exists(p):
        # CSVがなければ合成データを返す
        return _make_synthetic_data()
    df = pd.read_csv(p, parse_dates=["timestamp"])
    # 列名の違いに対応（heart_rate / hr_smooth / hr_raw）
    for _col in ["heart_rate", "hr_smooth", "hr_raw"]:
        if _col in df.columns:
            df["heart_rate"] = pd.to_numeric(df[_col], errors="coerce")
            break
    else:
        df["heart_rate"] = 100.0
    return df.dropna(subset=["heart_rate"])

def _make_synthetic_data():
    """CSVがない場合の合成データ（ペース予測に使用）"""
    np.random.seed(42)
    n = 500
    slopes = np.random.uniform(-30, 30, n)
    speeds = np.clip(1.2 * np.exp(-0.04 * np.abs(slopes)), 0.1, 2.0)
    hrs    = np.clip(80 + np.abs(slopes) * 1.5 + np.random.normal(0, 5, n), 60, 180)
    return pd.DataFrame({
        "timestamp":  pd.date_range("2024-01-01", periods=n, freq="10s"),
        "slope_pct":  slopes,
        "speed_ms":   speeds,
        "heart_rate": hrs,
    })

def build_engine_a_output(df, current_hr, lt_ratio, hr_max, future_slope, profile=None):
    reg = PaceRegressor(); reg.fit(df)
    base_spd_ms = profile.base_run_speed_ms if profile else 3.0
    base_cyc_spd= profile.base_cycling_speed_ms if profile else 6.94
    act = profile.activity_type if profile else "hiking"
    if act == "running":
        spd = np.clip([_run_spd(s, base_spd_ms) for s in future_slope], 1.0, 8.0)
    elif act == "cycling":
        spd = np.full(len(future_slope), base_cyc_spd)
    else:
        spd = np.clip([_tobler(s) for s in future_slope], 0.1, 2.0)
    dist = spd * 10
    eta_min = len(future_slope)*10/60
    hr_pred = np.clip(lt_ratio*hr_max + future_slope*0.3, 60, hr_max)
    from ke_estimator import energy_watt
    ke_arr = np.array([energy_watt(float(spd[i]), float(s), profile) if profile else 8.0 for i,s in enumerate(future_slope)])
    return dict(speed_curve=spd, dist_curve=dist, slope_curve=future_slope,
                hr_predicted=hr_pred, ke_series=ke_arr,
                eta_minutes=eta_min, total_dist_m=float(dist.sum()))

FOOD_MASTER = {
    "エネルギージェル": {"kcal_per":100, "ka":2.0},
    "おにぎり (梅)":   {"kcal_per":170, "ka":0.9},
    "バナナ":          {"kcal_per": 86, "ka":0.8},
    "カロリーメイト":  {"kcal_per":100, "ka":0.6},
    "スポーツドリンク":{"kcal_per": 50, "ka":1.8},
    "羊羹":            {"kcal_per":125, "ka":0.7},
    "ゼリー飲料":      {"kcal_per": 90, "ka":1.5},
}

TERRAIN_MAP = {"舗装路 (η=1.0)":1.0, "登山道 (η=1.2)":1.2, "岩場 (η=1.5)":1.5}

# ══════════════════════════════════════════════════════
# session_state 初期化
# ══════════════════════════════════════════════════════
_SS_DEFAULTS = {
    "rp_start":    {"name":"","lat":None,"lon":None},
    "rp_goal":     {"name":"","lat":None,"lon":None},
    "rp_vias":     [],
    "rp_activity": "🏔️ 登山・ハイキング",
    "rp_route":    None,
    "rp_sr_start": [],
    "rp_sr_goal":  [],
    "rp_sr_vias":  {},
    "sim_result":  None,
    "sim_auto_done": False,
    # Phase 1: リアルタイム追跡
    "tracking_active"    : False,
    "position_history"   : [],      # [{"lat","lon","time"}, ...]
    "current_lat"        : None,
    "current_lon"        : None,
    "owm_api_key"        : "",      # OpenWeatherMap APIキー
    "planned_datetime"   : None,    # 活動予定日時
    "forecast_weather"   : None,    # 天気予報データ
}
for _k, _v in _SS_DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

_ss = st.session_state

# ══════════════════════════════════════════════════════
# タイトル
# ══════════════════════════════════════════════════════
st.title("⛰️ Energy Tracker")
st.caption("ルートを入力するだけで、エネルギー消費とベスト補給計画を自動計算")

st.divider()

# ══════════════════════════════════════════════════════
# STEP 1: 活動スタイル
# ══════════════════════════════════════════════════════
st.markdown("## STEP 1｜活動スタイルを選択")
_acts = list(ACTIVITY_PROFILES.keys())
_acols = st.columns(len(_acts))

def _select_activity(a):
    _ss["rp_activity"] = a
    _ss["rp_route"] = None
    _ss["sim_result"] = None
    _ss["sim_auto_done"] = False

for _ci, _a in enumerate(_acts):
    with _acols[_ci]:
        _sel = _ss["rp_activity"] == _a
        st.button(_a, key=f"act_{_ci}", use_container_width=True,
                  type="primary" if _sel else "secondary",
                  on_click=_select_activity, args=(_a,))

st.divider()

# ══════════════════════════════════════════════════════
# STEP 2: ルート設定
# ══════════════════════════════════════════════════════
st.markdown("## STEP 2｜ルートを設定")

# ── ルート入力モード選択 ──
_route_input_mode = st.radio(
    "ルートの入力方法",
    ["🗺️ 地名・住所で検索", "📁 GPXファイルをアップロード（最高精度）"],
    horizontal=True, key="route_input_mode",
    help="登山・山道ではYAMAP/ヤマレコのGPXアップロードが最も正確です")

# ── APIエンジン選択（検索モード時のみ）──
with st.container():
    if _route_input_mode == "🗺️ 地名・住所で検索":
        _api_engine = st.radio(
            "ルート計算エンジン",
            ["🔵 OpenRouteService (ORS)", "🟢 GraphHopper"],
            horizontal=True, key="api_engine",
            help="GraphHopperは登山道・山道のカバレッジが優れています")

st.divider()

# ────────────────────────────────────────────────
# GPX アップロードモード（日時入力・天気予報連携）
# ────────────────────────────────────────────────
if _route_input_mode == "📁 GPXファイルをアップロード（最高精度）":
    st.markdown("**📁 GPXファイルをアップロード**")
    st.info(
        "**YAMAP・ヤマレコ・Garmin・Strava** などからエクスポートしたGPXファイルをアップロード。"
        "活動日時を入力すると、スタート地点の天気予報を自動取得して気象補正が自動適用されます。",
        icon="🏔️")

    # ── GPXファイル ──
    _gpx_file = st.file_uploader("GPXファイル (.gpx)", type=["gpx"], key="gpx_upload")

    # ── 活動予定日時 ──
    from datetime import datetime, date, time as dtime
    _col_d, _col_t = st.columns(2)
    with _col_d:
        _plan_date = st.date_input(
            "📅 活動予定日",
            value=date.today(),
            key="plan_date",
            help="出発予定日を選択してください")
    with _col_t:
        _plan_time = st.time_input(
            "⏰ 出発予定時刻",
            value=dtime(6, 0),
            key="plan_time",
            step=900,   # 15分刻み
            help="出発予定時刻を選択してください")
    _plan_dt = datetime.combine(_plan_date, _plan_time)
    # 日時が変わったら天気予報キャッシュを無効化
    _prev_dt = _ss.get("planned_datetime")
    if _prev_dt is None or _prev_dt != _plan_dt:
        _ss["planned_datetime"] = _plan_dt
        _ss["forecast_weather"] = None   # キャッシュ破棄 → 次回ボタン押下で再取得

    # ── OpenWeatherMap APIキー ──
    _ss["owm_api_key"] = OWM_KEY

    # ── GPX読み込み処理（ファイルが新規・変更時のみパース）──
    if _gpx_file is not None:
        _gpx_fid = f"{_gpx_file.name}_{_gpx_file.size}"
        if _ss.get("_gpx_file_id") != _gpx_fid:
            try:
                _gpx_route = parse_gpx_route(_gpx_file.read())
                _gpx_route["method"] = "GPX（実際の軌跡）"
                _gpx_route["waypoints"] = [_gpx_route["coords"][0], _gpx_route["coords"][-1]]
                _ss["rp_route"]      = _gpx_route
                _ss["sim_auto_done"] = False
                _ss["sim_result"]    = None
                _start_coord         = _gpx_route["coords"][0]
                _ss["rp_start"]      = {"name": "GPXスタート",
                                        "lat": _start_coord[0], "lon": _start_coord[1]}
                _ss["rp_goal"]       = {"name": "GPXゴール",
                                        "lat": _gpx_route["coords"][-1][0],
                                        "lon": _gpx_route["coords"][-1][1]}
                _ss["_gpx_file_id"]  = _gpx_fid
                dist_km = _gpx_route["distance_m"] / 1000
                st.success(
                    f"GPX読み込み完了  距離: {dist_km:.1f}km  "
                    f"獲得標高: {_gpx_route['ascent_m']:.0f}m  "
                    f"({len(_gpx_route['coords'])}点)")
            except Exception as _ge:
                st.error(f"GPX読み込みエラー: {_ge}")

    # ── 出発日時・確認表示（日時変更に即座に追従）──
    if _ss.get("rp_route") and _ss["rp_route"].get("method", "").startswith("GPX"):
        _cur_rt  = _ss["rp_route"]
        _s_lat   = _ss["rp_start"].get("lat", 0)
        _s_lon   = _ss["rp_start"].get("lon", 0)
        st.info(
            f"📅 出発日時: **{_plan_dt.strftime('%Y年%m/%d %H:%M')}**  "
            f"スタート地点: ({round(_s_lat, 4)}, {round(_s_lon, 4)})  \n"
            f"距離: {_cur_rt.get('distance_m', 0)/1000:.1f}km  "
            f"獲得標高: {_cur_rt.get('ascent_m', 0):.0f}m  \n"
            "👇 日時を確認したら「▶ シミュレーションを実行」ボタンを押すと、"
            "その日時の天気予報を取得してシミュレーションを開始します。",
            icon="🌤️")

else:
    # 地名検索モード
    # ── スタート ──
    st.markdown("**🟢 スタート地点**")
    _c1, _c2 = st.columns([5,1])
    with _c1:
        _sq = st.text_input("スタート", placeholder="例: 富士山五合目、上高地バスターミナル",
                            key="rp_q_start", label_visibility="collapsed")
    with _c2:
        if st.button("検索", key="search_start"):
            if _sq.strip():
                with st.spinner("検索中..."): _ss["rp_sr_start"] = search_place(_sq, 5)
            if not _ss["rp_sr_start"]: st.warning("見つかりませんでした")

    with st.container():
        if _ss["rp_sr_start"]:
            st.markdown("↓ 確定する地点を選択")
            for _ri, _r in enumerate(_ss["rp_sr_start"]):
                if st.button(f"📍 {_r['name'][:72]}", key=f"sel_s_{_ri}", use_container_width=True):
                    _ss["rp_start"] = {"name":_r["name"][:50],"lat":_r["lat"],"lon":_r["lon"]}
                    _ss["rp_sr_start"] = []
                    _ss["rp_route"] = None
                    _ss["sim_result"] = None
                    _ss["sim_auto_done"] = False

        if _ss["rp_start"]["lat"]:
            st.success(f"✅ {_ss['rp_start']['name']}")

    # ── 経由地 ──
    for _vi, _via in enumerate(list(_ss["rp_vias"])):
        st.markdown(f"**📍 経由地 {_vi+1}**")
        _vc1, _vc2, _vc3 = st.columns([4,1,0.6])
        with _vc1:
            _vq = st.text_input(f"経由地{_vi+1}", placeholder="地名を入力",
                                key=f"rp_q_via_{_vi}", label_visibility="collapsed")
        with _vc2:
            if st.button("検索", key=f"search_via_{_vi}"):
                if _vq.strip():
                    with st.spinner("..."): _ss["rp_sr_vias"][_vi] = search_place(_vq, 5)
        with _vc3:
            if st.button("✕", key=f"del_via_{_vi}"):
                _ss["rp_vias"].pop(_vi)
                _ss["rp_sr_vias"].pop(_vi, None)
                _ss["rp_route"] = None
                _ss["sim_result"] = None
                _ss["sim_auto_done"] = False

        with st.container():
            if _vi in _ss.get("rp_sr_vias", {}):
                for _ri, _r in enumerate(_ss["rp_sr_vias"][_vi]):
                    if st.button(f"📍 {_r['name'][:72]}", key=f"sel_v_{_vi}_{_ri}", use_container_width=True):
                        _ss["rp_vias"][_vi] = {"name":_r["name"][:50],"lat":_r["lat"],"lon":_r["lon"]}
                        _ss["rp_sr_vias"].pop(_vi, None)
                        _ss["rp_route"] = None
                        _ss["sim_result"] = None
                        _ss["sim_auto_done"] = False
            if isinstance(_via, dict) and _via.get("lat"):
                st.success(f"✅ {_via['name']}")

    if st.button("＋ 経由地を追加", key="add_via"):
        _ss["rp_vias"].append({"name":"","lat":None,"lon":None})

    # ── ゴール ──
    st.markdown("**🔴 ゴール地点**")
    _g1, _g2 = st.columns([5,1])
    with _g1:
        _gq = st.text_input("ゴール", placeholder="例: 富士山山頂、東京駅",
                            key="rp_q_goal", label_visibility="collapsed")
    with _g2:
        if st.button("検索", key="search_goal"):
            if _gq.strip():
                with st.spinner("検索中..."): _ss["rp_sr_goal"] = search_place(_gq, 5)
            if not _ss["rp_sr_goal"]: st.warning("見つかりませんでした")

    with st.container():
        if _ss["rp_sr_goal"]:
            st.markdown("↓ 確定する地点を選択")
            for _ri, _r in enumerate(_ss["rp_sr_goal"]):
                if st.button(f"📍 {_r['name'][:72]}", key=f"sel_g_{_ri}", use_container_width=True):
                    _ss["rp_goal"] = {"name":_r["name"][:50],"lat":_r["lat"],"lon":_r["lon"]}
                    _ss["rp_sr_goal"] = []
                    _ss["rp_route"] = None
                    _ss["sim_result"] = None
                    _ss["sim_auto_done"] = False

        if _ss["rp_goal"]["lat"]:
            st.success(f"✅ {_ss['rp_goal']['name']}")

st.divider()

# ══════════════════════════════════════════════════════
# STEP 3: オプション設定（折りたたみ）
# ══════════════════════════════════════════════════════
st.markdown("## STEP 3｜オプション設定（任意）")

# ── プロフィール ──
with st.expander("👤 プロフィール設定", expanded=False):
    _pc1, _pc2 = st.columns(2)
    with _pc1:
        st.number_input("体重 (kg)", 30.0, 150.0, 65.0, 0.5, key="body_weight")
        st.number_input("出発時エネルギー (kcal)", 500, 3500, 1800, 50, key="e_initial")
    with _pc2:
        st.number_input("荷物 (kg)", 0.0, 50.0, 8.0, 0.5, key="load_weight")

body_weight    = st.session_state.get("body_weight", 65.0)
e_initial      = st.session_state.get("e_initial", 1800)
load_weight    = st.session_state.get("load_weight", 8.0)
hr_max         = 180
current_hr     = 100
terrain_factor = 1.2
lt_ratio       = 0.85
e_crit         = 300.0
t_warn         = 25.0

# ── 補給食 ──
_n_sup = st.session_state.get("n_sup", 3)
with st.expander("🍙 補給食を設定", expanded=False):
    st.number_input("補給回数", 0, 8, 3, 1, key="n_sup")
    _n_sup = st.session_state.get("n_sup", 3)
    for _i in range(int(_n_sup)):
        _sc1, _sc2, _sc3 = st.columns([2,3,1])
        with _sc1:
            st.number_input(f"#{_i+1} 時刻(分)", 0, 600,
                            [30,90,150,210,270,330,390,450][_i], 5, key=f"sup_t_{_i}")
        with _sc2:
            st.selectbox("食品", list(FOOD_MASTER.keys()),
                         index=_i%len(FOOD_MASTER), key=f"sup_f_{_i}")
        with _sc3:
            st.number_input("個", 1, 5, 1, 1, key=f"sup_q_{_i}")

supply_events_list = []
for _i in range(int(st.session_state.get("n_sup", 3))):
    _t_val = st.session_state.get(f"sup_t_{_i}", [30,90,150,210,270,330,390,450][_i])
    _f_val = st.session_state.get(f"sup_f_{_i}", list(FOOD_MASTER.keys())[_i%len(FOOD_MASTER)])
    _q_val = st.session_state.get(f"sup_q_{_i}", 1)
    supply_events_list.append(SupplyEvent(
        time_min=float(_t_val),
        kcal=float(FOOD_MASTER[_f_val]["kcal_per"]*_q_val),
        ka=FOOD_MASTER[_f_val]["ka"]))
supply_events = sorted(supply_events_list, key=lambda e: e.time_min)

# ── 気象設定：GPX・検索モードはAPIで自動取得 ──────────────
_is_gpx_mode    = _route_input_mode == "📁 GPXファイルをアップロード（最高精度）"
_is_search_mode = _route_input_mode == "🗺️ 地名・住所で検索"

if not _is_gpx_mode and not _is_search_mode:
    # 手動入力モード（将来拡張用）
    with st.expander("🌤️ 気象・環境設定", expanded=False):
        _wc1, _wc2, _wc3, _wc4 = st.columns(4)
        with _wc1: st.number_input("気温 (℃)", -20.0, 45.0, 15.0, 0.5, key="w_temp")
        with _wc2: st.number_input("湿度 (%)", 0, 100, 60, 5, key="w_hum")
        with _wc3: st.number_input("風速 (m/s)", 0.0, 30.0, 2.0, 0.5, key="w_wind")
        with _wc4: st.checkbox("雨・悪天候", key="w_rain")
        _cond_preview = "Rain" if st.session_state.get("w_rain", False) else "Clear"
        _wf_disp, _wd_disp = calc_weather_factor(
            st.session_state.get("w_temp",15.0), st.session_state.get("w_hum",60),
            st.session_state.get("w_wind",2.0), _cond_preview)
        st.caption(f"気象補正: ×{_wf_disp:.2f}  {_wd_disp}")
    _cond_str = "Rain" if st.session_state.get("w_rain", False) else "Clear"
    _wf, _wd  = calc_weather_factor(
        st.session_state.get("w_temp",15.0), st.session_state.get("w_hum",60),
        st.session_state.get("w_wind",2.0), _cond_str)
    weather_data = WeatherData(
        location="手動入力",
        temp_c=st.session_state.get("w_temp",15.0),
        humidity_pct=st.session_state.get("w_hum",60),
        wind_speed_ms=st.session_state.get("w_wind",2.0),
        condition=_cond_str, weather_factor=_wf, factor_detail=_wd)
else:
    # GPX・検索モード：天気はAPIで自動取得（STEP3に手動欄は表示しない）
    # → 取得済みならそれを使う、なければデフォルト(1.0)
    weather_data = _ss.get("forecast_weather") or WeatherData(
        location="取得前", temp_c=15.0, humidity_pct=60.0,
        wind_speed_ms=2.0, condition="Clear", weather_factor=1.0, factor_detail="")

_ss["_weather_data_outer"] = weather_data

st.divider()

# ══════════════════════════════════════════════════════
# ルート検索 + シミュレーション実行
# ══════════════════════════════════════════════════════
_start_ok  = bool(_ss["rp_start"]["lat"])
_goal_ok   = bool(_ss["rp_goal"]["lat"])
_all_ready = _start_ok and _goal_ok

def _run_route_and_sim():
    """ルート検索とシミュレーションをまとめて実行する"""
    _wp_list = [(_ss["rp_start"]["lat"], _ss["rp_start"]["lon"])]
    for _v in _ss["rp_vias"]:
        if isinstance(_v, dict) and _v.get("lat"):
            _wp_list.append((_v["lat"], _v["lon"]))
    _wp_list.append((_ss["rp_goal"]["lat"], _ss["rp_goal"]["lon"]))

    with st.spinner("🗺️ ルートを計算中..."):
        # GPXモードならルート検索をスキップ（既にrp_routeに設定済み）
        if _ss.get("rp_route") and _ss["rp_route"].get("method","").startswith("GPX"):
            _rt = _ss["rp_route"]
        else:
            _profile_ors = ACTIVITY_PROFILES[_ss["rp_activity"]]
            _engine_choice = st.session_state.get("api_engine", "🔵 OpenRouteService (ORS)")
            _route_error_msg = ""
            _rt = None

            # 1st try: 選択エンジン
            try:
                if "GraphHopper" in _engine_choice:
                    _rt = get_route_graphhopper(_wp_list, _profile_ors)
                    _rt["method"] = "GraphHopper（道路・登山道沿い）"
                else:
                    _rt = get_route_ors(_wp_list, _profile_ors, ORS_KEY)
                    _rt["method"] = "ORS（道路・登山道沿い）"
            except Exception as _e1:
                _route_error_msg = str(_e1)

            # 2nd try: 反対のエンジンにフォールバック
            if _rt is None:
                try:
                    if "GraphHopper" in _engine_choice:
                        _rt = get_route_ors(_wp_list, _profile_ors, ORS_KEY)
                        _rt["method"] = "ORS（GraphHopperが失敗のためフォールバック）"
                    else:
                        _rt = get_route_graphhopper(_wp_list, _profile_ors)
                        _rt["method"] = "GraphHopper（ORSが失敗のためフォールバック）"
                except Exception as _e2:
                    _route_error_msg += f" / {_e2}"

            # 最終フォールバック: 直線近似（ユーザーに警告）
            if _rt is None:
                _rt = get_route_fallback(_wp_list)
                _rt["method"] = "直線近似（APIに接続できませんでした）"
                st.warning(
                    "⚠️ ルートAPIに接続できなかったため直線近似を使用。"
                    f"エラー: {_route_error_msg[:120]}。"
                    "登山ルートにはGPXアップロード推奨。")

            _rt["waypoints"] = _wp_list
            _ss["rp_route"] = _rt

    with st.spinner("⚡ シミュレーション計算中..."):
        _df = load_default_data()
        if _df is None:
            st.error("data/processed/merged_log.csv が見つかりません。")
            return
        _act_sim = ACTIVITY_TO_SIM[_ss["rp_activity"]]
        _coords  = _rt["coords"]
        _elevs   = _rt["elevations"]
        _spd_h   = 1.0 if _act_sim=="hiking" else (3.0 if _act_sim=="running" else 6.94)
        _sa, _ea, _da = coords_to_slope_array(_coords, _elevs, speed_ms=_spd_h)
        _alt_m   = float(_ea.mean())
        # weather_data を session_state または固定デフォルトから取得
        # （_run_route_and_sim は関数内のため、外側の変数に直接アクセスできない）
        _wd_inner = _ss.get("forecast_weather") or _ss.get("_weather_data_outer")
        _wf_val   = _wd_inner.weather_factor if _wd_inner else 1.0
        if _ss.get("forecast_weather"):
            _wd_inner = _ss["forecast_weather"]   # 天気予報を優先

        _prof = UserProfile(
            body_weight_kg=body_weight, load_weight_kg=load_weight,
            hr_max=float(hr_max), terrain_factor=terrain_factor,
            e_initial_kcal=float(e_initial), activity_type=_act_sim,
            base_run_speed_ms=3.0, base_cycling_speed_ms=6.94,
            weather_factor=float(_wf_val), altitude_m=_alt_m)

        _engine = build_engine_a_output(_df, current_hr, lt_ratio, hr_max, _sa, _prof)
        _result = run_pk_model(
            engine_a_output=_engine, profile=_prof,
            supply_events=supply_events,
            alert_config=AlertConfig(e_crit_kcal=e_crit, t_warn_min=t_warn))

        _ss["sim_result"]    = _result
        _ss["engine_a_out"]  = _engine
        _ss["sim_profile"]   = _prof
        _ss["sim_supply"]    = supply_events
        _ss["sim_weather"]   = _wd_inner
        _ss["sim_auto_done"] = True
        _ss["map_slope_arr"] = _sa

# スタート地点が変わったら天気予報をリセット（ボタン押下まで再取得しない）
if _ss.get("_last_start_lat") != _ss["rp_start"]["lat"]:
    _ss["forecast_weather"] = None
    _ss["_last_start_lat"] = _ss["rp_start"]["lat"]


# ── シミュレーション実行ボタン ──
if _all_ready:
    _plan_dt_now = _ss.get("planned_datetime")   # UIで選択中の日時
    _btn_label = "▶ シミュレーションを実行"

    if st.button(_btn_label, type="primary", use_container_width=True, key="run_sim_btn"):
        # ボタン押下時に天気予報を取得（出発予定日時を常に使用）
        if _ss["rp_start"]["lat"]:
            import datetime as _dt_mod
            _fc_target = _plan_dt_now if _plan_dt_now else _dt_mod.datetime.now()
            _act_type_for_wx = ACTIVITY_TO_SIM.get(_ss["rp_activity"], "hiking")
            try:
                with st.spinner("🌤️ スタート地点の天気予報を取得中..."):
                    _fc_btn = fetch_weather_forecast(
                        OWM_KEY,
                        _ss["rp_start"]["lat"], _ss["rp_start"]["lon"],
                        _fc_target, _act_type_for_wx)
                    _ss["forecast_weather"]    = _fc_btn
                    _ss["_weather_data_outer"] = _fc_btn
                    weather_data               = _fc_btn
            except Exception as _we:
                st.warning(f"天気予報取得失敗: {_we}")

        _ss["sim_auto_done"] = False
        _run_route_and_sim()
else:
    st.info("👆 スタートとゴールを確定してください")

# ══════════════════════════════════════════════════════
# 結果表示
# ══════════════════════════════════════════════════════
if _ss.get("sim_result") and _ss.get("rp_route"):
    _route   = _ss["rp_route"]
    result   = _ss["sim_result"]
    engine_a = _ss["engine_a_out"]
    prof     = _ss["sim_profile"]
    supply   = _ss["sim_supply"]
    w_data   = _ss["sim_weather"]

    _coords  = _route["coords"]
    _elevs   = _route["elevations"]
    _wps_ll  = _route.get("waypoints",[])
    _dist_km = _route["distance_m"]/1000

    st.divider()
    st.markdown("## 📊 シミュレーション結果")

    # KPIカード
    _E  = result["E_kcal"]
    _t  = result["time_min"]
    _al = result.get("alert_times",[])
    _k1,_k2,_k3,_k4,_k5 = st.columns(5)
    _k1.metric("総距離",     f"{_dist_km:.1f} km")
    _k2.metric("予測時間",   fmt_minutes(_t[-1]))
    _k3.metric("残エネルギー", f"{max(_E[-1],0):.0f} kcal")
    _k4.metric("総消費量",   f"{prof.e_initial_kcal - max(_E[-1],0):.0f} kcal")
    _k5.metric("獲得標高",   f"{_route['ascent_m']:.0f} m")

    if _al:
        st.error(f"⚠️ ハンガーノック警告: {len(_al)}回  [{', '.join(fmt_minutes(a) for a in _al)}]")
    else:
        st.success(f"✅ エネルギー安全（最小: {_E.min():.0f} kcal > 閾値: {e_crit:.0f} kcal）")

    # タブで結果を整理
    _tab1, _tab2, _tab3 = st.tabs(["🗺️ ルート & 標高", "📈 エネルギー推移", "🍙 補給計画"])

    with _tab1:
        # ── 標高・ペースデータを事前計算 ──
        _act_sim2 = ACTIVITY_TO_SIM[_ss["rp_activity"]]
        _spd2 = 1.0 if _act_sim2=="hiking" else (3.0 if _act_sim2=="running" else 6.94)
        _sa2, _ea2, _da2 = coords_to_slope_array(_coords, _elevs, speed_ms=_spd2)
        _dk2 = _da2 / 1000  # km
        # 各ルート座標点のペース（m/分）を補間して対応させる
        _n_coords = len(_coords)
        _n_slope  = len(_sa2)
        # coords と slope配列のインデックスを対応付け
        _coord_km = np.linspace(0, float(_dk2[-1]) if len(_dk2)>0 else 1, _n_coords)
        _pace_mmin= np.interp(_coord_km, _dk2, _spd2 * 60 * np.ones(len(_dk2)))                     if len(_dk2) > 1 else np.ones(_n_coords) * _spd2 * 60
        _slope_at_coord = np.interp(_coord_km, _dk2[1:], _sa2) if len(_dk2) > 1 else np.zeros(_n_coords)
        _elev_at_coord  = np.interp(_coord_km, _dk2, _ea2) if len(_dk2) > 1 else np.array(_elevs[:_n_coords])

        # ── インタラクティブ地図（Plotly Scattermapbox）──
        # ルートを斜度で色分けして表示
        import plotly.express as _px

        def _slope_rgba(s):
            if   s >= 30: return [180, 0,   0]
            elif s >= 20: return [220, 50,  50]
            elif s >= 10: return [230, 120, 30]
            elif s >=  3: return [240, 180, 60]
            elif s >= -3: return [140, 140, 140]
            elif s >= -10: return [80, 160, 220]
            else:          return [30, 100, 200]

        # ルート座標を間引いて描画（最大300点）
        _step_map = max(1, _n_coords // 300)
        _map_lats  = [_coords[i][0] for i in range(0, _n_coords, _step_map)]
        _map_lons  = [_coords[i][1] for i in range(0, _n_coords, _step_map)]
        _map_km    = [float(_coord_km[i]) for i in range(0, _n_coords, _step_map)]
        _map_elev  = [float(_elev_at_coord[i]) for i in range(0, _n_coords, _step_map)]
        _map_slope = [float(_slope_at_coord[i]) for i in range(0, _n_coords, _step_map)]
        _map_pace  = [float(_pace_mmin[i]) if i < len(_pace_mmin) else float(_spd2*60)
                      for i in range(0, _n_coords, _step_map)]
        _map_colors= [f"rgb({r},{g},{b})" for r,g,b in
                      [_slope_rgba(s) for s in _map_slope]]
        _map_hover = [
            f"{_map_km[i]:.2f}km<br>"
            f"標高: {_map_elev[i]:.0f}m<br>"
            f"斜度: {_map_slope[i]:+.1f}%<br>"
            f"推奨ペース: {_map_pace[i]:.0f}m/分"
            for i in range(len(_map_lats))
        ]

        import json as _json
        import streamlit.components.v1 as _stc

        # ── 地図フィギュア構築（赤丸なし・JS側で追加）──
        _fig_map = go.Figure()
        _seg = max(1, len(_map_lats) // 60)
        for _mi in range(0, len(_map_lats) - _seg, _seg):
            _fig_map.add_trace(go.Scattermapbox(
                lat=_map_lats[_mi:_mi+_seg+1],
                lon=_map_lons[_mi:_mi+_seg+1],
                mode="lines",
                line=dict(width=4, color=_map_colors[_mi]),
                hoverinfo="skip", showlegend=False))
        # ホバー検知用マーカー（ほぼ透明、customdata=km）
        _fig_map.add_trace(go.Scattermapbox(
            lat=_map_lats, lon=_map_lons,
            mode="markers",
            marker=dict(size=10, color=_map_colors, opacity=0.01),
            text=_map_hover,
            hovertemplate="%{text}<extra></extra>",
            customdata=_map_km,
            name="ルート", showlegend=False))
        if _wps_ll:
            _fig_map.add_trace(go.Scattermapbox(
                lat=[_wps_ll[0][0]], lon=[_wps_ll[0][1]],
                mode="markers+text", marker=dict(size=14, color="#27ae60"),
                text=["スタート"], textposition="top right",
                hovertemplate="スタート<extra></extra>", showlegend=False))
            _fig_map.add_trace(go.Scattermapbox(
                lat=[_wps_ll[-1][0]], lon=[_wps_ll[-1][1]],
                mode="markers+text", marker=dict(size=14, color="#e74c3c"),
                text=["ゴール"], textposition="top right",
                hovertemplate="ゴール<extra></extra>", showlegend=False))
        _clat = sum(p[0] for p in _wps_ll) / len(_wps_ll)
        _clon = sum(p[1] for p in _wps_ll) / len(_wps_ll)
        _fig_map.update_layout(
            mapbox=dict(style="open-street-map",
                        center=dict(lat=_clat, lon=_clon), zoom=11),
            height=360, margin=dict(t=0, b=0, l=0, r=0),
            showlegend=False, hoverdistance=30)

        # ── 標高プロファイル構築（赤丸なし・JS側で追加）──
        _fig_rp = go.Figure()
        _seg2 = max(1, len(_dk2) // 80)
        for _ri in range(0, len(_dk2) - _seg2, _seg2):
            _sc = _slope_rgba(float(_sa2[min(_ri, len(_sa2) - 1)]))
            _fill_c = f"rgba({_sc[0]},{_sc[1]},{_sc[2]},0.18)"
            _line_c = f"rgb({_sc[0]},{_sc[1]},{_sc[2]})"
            _fig_rp.add_trace(go.Scatter(
                x=_dk2[_ri:_ri+_seg2+1].tolist(),
                y=_ea2[_ri:_ri+_seg2+1].tolist(),
                mode="lines", line=dict(color=_line_c, width=2.5),
                fill="tozeroy", fillcolor=_fill_c,
                showlegend=False, hoverinfo="skip"))
        # ホバー検知用透明ライン（x=km, customdata=km）
        _fig_rp.add_trace(go.Scatter(
            x=_dk2.tolist(), y=_ea2.tolist(),
            mode="lines",
            line=dict(color="rgba(0,0,0,0)", width=12),
            hovertemplate="%{x:.2f}km / 標高 %{y:.0f}m<extra></extra>",
            customdata=_dk2.tolist(),
            name="詳細"))
        for _ev in supply:
            _ev_dist = _ev.time_min / 60 * _spd2 * 3.6
            _ev_elev = (float(np.interp(_ev_dist, _dk2, _ea2))
                        if _ev_dist < float(_dk2[-1]) else float(_ea2[-1]))
            _fig_rp.add_vline(x=_ev_dist, line_dash="dot",
                              line_color="#3498db", line_width=1.5, opacity=0.7)
            _fig_rp.add_annotation(x=_ev_dist, y=_ev_elev + 30,
                text="[補給]", showarrow=False,
                font=dict(size=10, color="#3498db"))
        _total_km = float(_dk2[-1])
        _dtick = (0.5 if _total_km <= 5  else
                  1.0 if _total_km <= 10 else
                  2.0 if _total_km <= 20 else
                  5.0 if _total_km <= 50 else 10.0)
        _fig_rp.update_layout(
            height=240, margin=dict(t=10, b=40, l=60, r=20),
            hovermode="x",
            hoverlabel=dict(bgcolor="white", font_size=12),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
        _fig_rp.update_xaxes(
            title_text="距離 (km)", gridcolor="#eee",
            range=[0, _total_km], dtick=_dtick,
            showspikes=True, spikemode="toaxis+marker", spikesnap="cursor",
            spikedash="solid", spikecolor="#e74c3c", spikethickness=1)
        _fig_rp.update_yaxes(
            title_text="標高 (m)", gridcolor="#eee", rangemode="nonnegative")

        # ── JS連動コンポーネントとして両グラフを描画 ──
        _map_j  = _fig_map.to_json().replace("</script>", r"<\/script>")
        _elev_j = _fig_rp.to_json().replace("</script>", r"<\/script>")
        _js_km   = _json.dumps(_dk2.tolist())
        _js_ea   = _json.dumps(_ea2.tolist())
        _js_sa   = _json.dumps([float(v) for v in _sa2])
        _js_mkm  = _json.dumps(_map_km)
        _js_mlat = _json.dumps(_map_lats)
        _js_mlon = _json.dumps(_map_lons)

        _html_component = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"'
            ' charset="utf-8"></script>'
            '<style>'
            'body{margin:0;padding:0;font-family:sans-serif;background:transparent;}'
            '#info-bar{font-size:13px;color:#333;background:#f8f9fa;'
            'padding:5px 12px;border-left:3px solid #e74c3c;'
            'min-height:24px;line-height:24px;}'
            '</style></head><body>'
            '<div id="map-div" style="width:100%;height:360px;"></div>'
            '<div id="info-bar">ルートまたはグラフにカーソルを合わせると情報が表示されます</div>'
            '<div id="elev-div" style="width:100%;height:240px;"></div>'
            '<script>(function(){'
            f'var mf={_map_j};'
            f'var ef={_elev_j};'
            f'var kmA={_js_km};'
            f'var eaA={_js_ea};'
            f'var saA={_js_sa};'
            f'var mkA={_js_mkm};'
            f'var laA={_js_mlat};'
            f'var loA={_js_mlon};'
            # 赤丸トレースをJS側で追加（白リング＋赤丸）
            'mf.data.push({'
            'type:"scattermapbox",lat:[],lon:[],mode:"markers",'
            'marker:{size:26,color:"white",opacity:0.7},'
            'hoverinfo:"skip",showlegend:false});'
            'mf.data.push({'
            'type:"scattermapbox",lat:[],lon:[],mode:"markers",'
            'marker:{size:16,color:"#e74c3c",opacity:1.0},'
            'hoverinfo:"skip",showlegend:false});'
            'var mRing=mf.data.length-2, mDot=mf.data.length-1;'
            'ef.data.push({'
            'type:"scatter",x:[],y:[],mode:"markers",'
            'marker:{size:14,color:"#e74c3c",symbol:"circle",'
            'line:{color:"white",width:2.5}},'
            'hoverinfo:"skip",showlegend:false});'
            'var eDot=ef.data.length-1;'
            'var md=document.getElementById("map-div");'
            'var ed=document.getElementById("elev-div");'
            'var ib=document.getElementById("info-bar");'
            'Plotly.newPlot(md,mf.data,mf.layout,{responsive:true,displayModeBar:false});'
            'Plotly.newPlot(ed,ef.data,ef.layout,{responsive:true,displayModeBar:true,displaylogo:false});'
            # 補間ユーティリティ
            'function interp(xs,ys,x){'
            'if(x<=xs[0])return ys[0];'
            'if(x>=xs[xs.length-1])return ys[ys.length-1];'
            'var lo=0,hi=xs.length-1;'
            'while(hi-lo>1){var m=(lo+hi)>>1;if(xs[m]<=x)lo=m;else hi=m;}'
            'var t=(x-xs[lo])/(xs[hi]-xs[lo]);return ys[lo]+t*(ys[hi]-ys[lo]);}'
            'function nearest(a,v){'
            'var idx=0,b=Math.abs(a[0]-v);'
            'for(var i=1;i<a.length;i++){var d=Math.abs(a[i]-v);if(d<b){b=d;idx=i;}}'
            'return idx;}'
            # 両グラフ更新
            'function upd(km){'
            'var elev=interp(kmA,eaA,km);'
            'var si=Math.min(nearest(kmA,km),saA.length-1);'
            'var slope=saA[si];'
            'var mi=nearest(mkA,km);'
            'var lat=laA[mi],lon=loA[mi];'
            'Plotly.restyle(ed,{x:[[km]],y:[[elev]]},[eDot]);'
            'Plotly.restyle(md,{lat:[[lat]],lon:[[lon]]},[mDot]);'
            'Plotly.restyle(md,{lat:[[lat]],lon:[[lon]]},[mRing]);'
            'var dir=slope>3?"登り":(slope<-3?"下り":"平坦");'
            'ib.innerHTML="<b>"+km.toFixed(2)+" km</b>"'
            '+"&ensp;標高 <b>"+Math.round(elev)+" m</b>"'
            '+"&ensp;斜度 <b>"+(slope>=0?"+":"")+slope.toFixed(1)+"%</b>"'
            '+"&ensp;("+dir+")";}'
            'function clr(){'
            'Plotly.restyle(ed,{x:[[]],y:[[]]},[eDot]);'
            'Plotly.restyle(md,{lat:[[]],lon:[[]]},[mDot]);'
            'Plotly.restyle(md,{lat:[[]],lon:[[]]},[mRing]);'
            'ib.innerHTML="ルートまたはグラフにカーソルを合わせると情報が表示されます";}'
            # 標高グラフのホバーイベント
            'ed.on("plotly_hover",function(d){'
            'if(!d.points||!d.points.length)return;'
            'for(var i=0;i<d.points.length;i++){'
            'var p=d.points[i];'
            'if(p.x!==undefined&&p.x!==null){upd(+p.x);return;}'
            '}});'
            'ed.on("plotly_unhover",clr);'
            # 地図のホバーイベント
            'md.on("plotly_hover",function(d){'
            'if(!d.points||!d.points.length)return;'
            'for(var i=0;i<d.points.length;i++){'
            'var p=d.points[i];'
            'if(p.customdata!==undefined&&p.customdata!==null){'
            'upd(+p.customdata);return;}}'
            '});'
            'md.on("plotly_unhover",clr);'
            '})();</script></body></html>'
        )

        st.caption("ルートマップ（色: 赤=急登 / 橙=登り / 灰=平坦 / 青=下り）"
                   "  カーソルを合わせると赤丸が連動します")
        _stc.html(_html_component, height=650, scrolling=False)
        st.caption(
            f"総距離: {_dk2[-1]:.1f}km  |  "
            f"最高: {_ea2.max():.0f}m  最低: {_ea2.min():.0f}m  |  "
            f"平均斜度: {_sa2.mean():.1f}%  獲得標高: {float(np.sum(np.diff(_ea2).clip(0))):.0f}m")

    with _tab2:
        _HR  = np.array(engine_a["hr_predicted"], dtype=float)
        _t2  = np.array(_t, dtype=float)
        _E2  = np.array(_E, dtype=float)
        _spd_c = np.array(engine_a["speed_curve"], dtype=float)
        _slp_c = np.array(engine_a["slope_curve"], dtype=float)
        n      = min(len(_t2), len(_E2), len(_HR))

        # ── グラフ① エネルギー残量の推移（行動の判断基準） ──
        st.markdown("#### ⚡ エネルギー残量")
        st.caption(
            "**見方**: 緑の線が警告ライン（赤点線）に近づいたら補給が必要。"
            "青の縦線が補給タイミング。赤い縦線はエネルギー切れ警告。")

        _fig_e = go.Figure()
        # 安全ゾーン（e_crit以上）を塗りつぶし
        _e_safe = np.maximum(_E2[:n], e_crit)
        _e_risk = np.minimum(_E2[:n], e_crit)
        _fig_e.add_trace(go.Scatter(
            x=_t2[:n].tolist(), y=_E2[:n].tolist(),
            name="エネルギー残量",
            line=dict(color="#27ae60", width=3),
            fill="tozeroy", fillcolor="rgba(39,174,96,0.15)",
            hovertemplate="<b>%{x:.0f}分</b><br>残量: %{y:.0f} kcal<extra></extra>"))
        # 警告ライン
        _fig_e.add_hline(
            y=e_crit, line_dash="dash", line_color="#e74c3c", line_width=2,
            annotation_text=f"危険ライン {e_crit:.0f}kcal",
            annotation_font_color="#e74c3c", annotation_font_size=11)
        # 補給タイミング（青縦線＋ラベル）
        for _ev in supply:
            _fig_e.add_vline(x=_ev.time_min, line_dash="solid",
                             line_color="#3498db", line_width=2, opacity=0.7)
            _fig_e.add_annotation(
                x=_ev.time_min, y=float(prof.e_initial_kcal),
                text=f"+{_ev.kcal:.0f}kcal",
                showarrow=False, yanchor="top",
                font=dict(color="#2980b9", size=10),
                bgcolor="rgba(255,255,255,0.8)", bordercolor="#3498db",
                borderwidth=1, borderpad=3)
        # ハンガーノック警告（赤縦線）
        for _at in _al:
            _fig_e.add_vline(x=_at, line_color="#e74c3c", line_width=2.5)
            _fig_e.add_annotation(
                x=_at, y=float(e_crit),
                text="HN警告", showarrow=True, ay=40,
                font=dict(color="#e74c3c", size=10))
        # ゴール縦線
        _fig_e.add_vline(x=float(_t2[-1]), line_dash="dot",
                         line_color="#27ae60", line_width=1.5,
                         annotation_text=f"ゴール {fmt_minutes(_t2[-1])}",
                         annotation_font_color="#27ae60")
        _fig_e.update_layout(
            height=320, margin=dict(t=30, b=50, l=60, r=20),
            hovermode="x unified", showlegend=False,
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
        _fig_e.update_xaxes(
            title_text="経過時間（分）", gridcolor="#eee",
            tickvals=list(range(0, int(_t2[-1])+1, max(int(_t2[-1])//8, 15))),
            ticktext=[fmt_minutes(v) for v in
                      range(0, int(_t2[-1])+1, max(int(_t2[-1])//8, 15))])
        _fig_e.update_yaxes(
            title_text="エネルギー残量 (kcal)",
            gridcolor="#eee", rangemode="nonnegative",
            ticksuffix=" kcal")
        st.plotly_chart(_fig_e, use_container_width=True,
                        key=f"energy_chart_{id(_fig_e)}")

        # アドバイスメッセージ
        _min_e = float(_E2[:n].min())
        if _min_e < e_crit:
            st.error(
                f"⚠️ **補給計画の見直しが必要**  "
                f"最低エネルギー {_min_e:.0f}kcal が警告ライン({e_crit:.0f}kcal)を下回っています。"
                f"「🍙 補給計画」タブで補給回数を増やすか、補給タイミングを早めてください。")
        else:
            _margin = _min_e - e_crit
            if _margin < 200:
                st.warning(
                    f"⚠️ **余裕が少ない**  最低残量 {_min_e:.0f}kcal（余裕 {_margin:.0f}kcal）。"
                    f"体調・天候によっては補給を増やすと安心です。")
            else:
                st.success(
                    f"✅ **計画は安全**  最低残量 {_min_e:.0f}kcal（余裕 {_margin:.0f}kcal）")


    with _tab3:
        if supply:
            st.markdown(f"**補給回数: {len(supply)}回 / 総補給量: {sum(e.kcal for e in supply):.0f} kcal**")
            _sup_rows = [["順番","タイミング","カロリー","吸収速度"]]
            for _i2, _ev in enumerate(supply):
                _spd_lbl = "速い⚡" if _ev.ka>=1.5 else ("普通🔶" if _ev.ka>=0.9 else "遅い🐢")
                _sup_rows.append([f"第{_i2+1}回", fmt_minutes(_ev.time_min),
                                   f"{_ev.kcal:.0f} kcal", _spd_lbl])
            _sup_df = pd.DataFrame(_sup_rows[1:], columns=_sup_rows[0])
            st.dataframe(_sup_df, use_container_width=True, hide_index=True)

            # 補給タイムラングラフ
            _fig_sup = go.Figure()
            _times      = [e.time_min for e in supply]
            _kcals      = [e.kcal     for e in supply]
            _total_min  = float(_t[-1])
            _colors_sup = ["#27ae60" if e.ka >= 1.5 else
                           ("#e67e22" if e.ka >= 0.9 else "#2980b9")
                           for e in supply]

            # 背景: 活動を4等分して薄いゾーンで時間帯を可視化
            _q_bg = ["rgba(240,248,255,0.6)", "rgba(240,255,240,0.6)",
                     "rgba(255,253,235,0.6)", "rgba(255,245,245,0.6)"]
            for _qi in range(4):
                _fig_sup.add_vrect(
                    x0=_total_min * _qi / 4, x1=_total_min * (_qi + 1) / 4,
                    fillcolor=_q_bg[_qi], line_width=0,
                    annotation_text=f"{_qi*25}%",
                    annotation_position="top left",
                    annotation_font=dict(size=9, color="#bbb"))

            # 累積補給量の面グラフ（下レイヤー）
            _cum_x = [0.0] + _times + [_total_min]
            _cum_y = [0.0] + list(np.cumsum(_kcals)) + [float(sum(_kcals))]
            _fig_sup.add_trace(go.Scatter(
                x=_cum_x, y=_cum_y,
                mode="lines",
                line=dict(color="rgba(41,128,185,0.45)", width=1.5, dash="dot"),
                fill="tozeroy", fillcolor="rgba(41,128,185,0.07)",
                name="累積補給量",
                hovertemplate="%{x:.0f}分  累積: %{y:.0f} kcal<extra></extra>"))

            # 補給バー（上レイヤー）
            _bar_w = max(_total_min * 0.04, 6)
            _sup_hover = [
                f"<b>{fmt_minutes(t)}</b><br>"
                f"{k:.0f} kcal<br>"
                f"吸収: {'速い' if e.ka>=1.5 else ('普通' if e.ka>=0.9 else 'ゆっくり')}"
                for t, k, e in zip(_times, _kcals, supply)
            ]
            _fig_sup.add_trace(go.Bar(
                x=_times, y=_kcals,
                marker_color=_colors_sup,
                marker_line=dict(color="white", width=1.5),
                marker_opacity=0.9,
                width=[_bar_w] * len(supply),
                text=[f"{k:.0f}" for k in _kcals],
                textposition="outside",
                textfont=dict(size=11, color="#333", family="sans-serif"),
                hovertext=_sup_hover,
                hoverinfo="text",
                name="補給量",
                showlegend=False))

            # 補給タイミングの番号ラベル
            _y_max_sup = max(_kcals) * 1.55 if _kcals else 200
            for _si, (_st, _sk) in enumerate(zip(_times, _kcals)):
                _fig_sup.add_annotation(
                    x=_st, y=_sk + _y_max_sup * 0.12,
                    text=f"<b>#{_si+1}</b>",
                    showarrow=False,
                    font=dict(size=10, color=_colors_sup[_si]))

            # 補給間隔インジケーター
            if len(_times) >= 2:
                for _ii in range(len(_times) - 1):
                    _mid = (_times[_ii] + _times[_ii + 1]) / 2
                    _gap = _times[_ii + 1] - _times[_ii]
                    _fig_sup.add_annotation(
                        x=_mid, y=_y_max_sup * 0.05,
                        text=f"{fmt_minutes(_gap)}間隔",
                        showarrow=False,
                        font=dict(size=9, color="#aaa"))

            # ゴール縦線
            _fig_sup.add_vline(
                x=_total_min, line_dash="dash", line_color="#27ae60", line_width=2,
                annotation_text=f"ゴール {fmt_minutes(_total_min)}",
                annotation_font=dict(color="#27ae60", size=11),
                annotation_position="top right")

            # 合計カロリーと回数を右上に注記
            _fig_sup.add_annotation(
                x=0.01, y=0.97, xref="paper", yref="paper",
                text=f"<b>合計 {sum(_kcals):.0f} kcal / {len(supply)}回</b>",
                showarrow=False,
                font=dict(size=12, color="#2c3e50"),
                align="left",
                bgcolor="rgba(255,255,255,0.85)",
                bordercolor="#ddd", borderwidth=1, borderpad=5)

            # 色の凡例説明
            _fig_sup.add_annotation(
                x=0.99, y=0.97, xref="paper", yref="paper",
                text="緑=速吸収  橙=普通  青=ゆっくり",
                showarrow=False,
                font=dict(size=10, color="#777"),
                align="right",
                bgcolor="rgba(255,255,255,0.75)",
                borderpad=4)

            _sup_tick_step = max(int(_total_min) // 6, 15)
            _fig_sup.update_layout(
                height=300, margin=dict(t=35, b=50, l=60, r=20),
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                showlegend=True,
                legend=dict(orientation="h", y=-0.22, x=0.5, xanchor="center",
                            font=dict(size=10), bgcolor="rgba(0,0,0,0)"),
                bargap=0.4,
                yaxis_range=[0, _y_max_sup])
            _fig_sup.update_xaxes(
                title_text="経過時間", gridcolor="#eee",
                tickvals=list(range(0, int(_total_min)+1, _sup_tick_step)),
                ticktext=[fmt_minutes(v)
                          for v in range(0, int(_total_min)+1, _sup_tick_step)])
            _fig_sup.update_yaxes(
                title_text="補給量 (kcal)", gridcolor="#eee", rangemode="nonnegative")
            st.plotly_chart(_fig_sup, use_container_width=True, key="supply_chart")
        else:
            st.info("補給食は設定されていません。STEP 3で補給食を設定できます。")

    # PDF出力（ダウンロードボタンのみ・ページ遷移なし）
    st.divider()
    _pdf_key = "pdf_bytes_v2"

    # シミュレーション結果のシグネチャ（結果が変わったらPDFを再生成）
    _result_sig = f"{_ss.get('sim_route_mode','')}_{int(_t[-1])}_{int(_E[-1])}"
    if st.session_state.get("_pdf_result_sig") != _result_sig:
        st.session_state.pop(_pdf_key, None)

    # PDFがまだなければバックグラウンド生成（ボタンクリック不要）
    if _pdf_key not in st.session_state:
        try:
            _pdf = generate_report_pdf(
                result, engine_a, prof, supply, FOOD_MASTER,
                weather_data=w_data,
                alert_config=AlertConfig(e_crit_kcal=e_crit, t_warn_min=t_warn),
                route_name=f"{_ss['rp_start'].get('name','スタート')} → {_ss['rp_goal'].get('name','ゴール')}",
                elev_km=_dk2.tolist(),
                elev_m=_ea2.tolist(),
                slope_arr=_sa2.tolist(),
            )
            st.session_state[_pdf_key] = _pdf
            st.session_state["_pdf_result_sig"] = _result_sig
        except Exception as _pe:
            st.warning(f"PDFの準備に失敗しました: {_pe}")

    # ダウンロードボタン（rerunしない）
    _pdf_data = st.session_state.get(_pdf_key)
    if _pdf_data:
        st.download_button(
            label="📄 PDFレポートをダウンロード",
            data=_pdf_data,
            file_name="energy_tracker_report.pdf",
            mime="application/pdf",
            type="secondary",
            key="dl_pdf_stable")


# ══════════════════════════════════════════════════════
# Phase 1: リアルタイムGPS追跡（登山中モード）
# ══════════════════════════════════════════════════════
if _ss.get("sim_result") and _ss.get("rp_route"):
    _rt_now = _ss["rp_route"]
    _route_coords_now = _rt_now.get("coords", [])

    st.divider()
    st.markdown("## 📡 登山中サポート — リアルタイム予測更新")
    st.caption(
        "スマートフォンのブラウザでこのページを開いて「活動を開始」を押すと、"
        "GPSで現在地を自動取得し、ETA・エネルギー残量・補給タイミングをリアルタイムで更新します。")

    # ── 活動開始/停止ボタン ──
    import streamlit.components.v1 as _comp
    from datetime import datetime as _dt
    from streamlit_autorefresh import st_autorefresh

    _is_tracking = _ss.get("tracking_active", False)

    _tc1, _tc2 = st.columns([1, 1])
    with _tc1:
        if not _is_tracking:
            if st.button("🟢 活動を開始", type="primary",
                         use_container_width=True, key="start_tracking"):
                _ss["tracking_active"]   = True
                _ss["position_history"]  = []
                _ss["tracking_start_dt"] = _dt.now()
        else:
            if st.button("🔴 活動を終了", type="secondary",
                         use_container_width=True, key="stop_tracking"):
                _ss["tracking_active"] = False
    with _tc2:
        if _is_tracking:
            _elapsed_track = (_dt.now() - _ss.get("tracking_start_dt", _dt.now())).seconds // 60
            st.markdown(f"**📡 追跡中**  経過 {_elapsed_track}分  "
                        f"（30秒ごとに自動更新）")

    # ── 自動更新タイマー（追跡中のみ30秒ごとに rerun）──
    if _is_tracking:
        st_autorefresh(interval=30_000, key="tracking_autorefresh")

    # ── GPS取得コンポーネント（URLクエリパラメータ経由でPythonへ渡す）──
    _gps_js = """
<div id="gps_panel" style="
  background:rgba(46,204,113,0.08);border:1px solid #2ecc71;
  border-radius:8px;padding:10px 14px;margin:6px 0">
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <button id="gps_btn" onclick="getAndSendGPS()" style="
      background:#2ecc71;color:white;border:none;padding:8px 18px;
      border-radius:6px;font-size:14px;cursor:pointer;font-weight:500">
      📍 現在地を取得
    </button>
    <span id="gps_status" style="font-size:13px;color:#555">
      （ボタンを押すか、追跡中は自動取得されます）
    </span>
  </div>
</div>
<script>
var _lastLat = null, _lastLon = null;

function getAndSendGPS() {
  var status = document.getElementById('gps_status');
  status.textContent = '📡 取得中...';
  navigator.geolocation.getCurrentPosition(
    function(pos) {
      var lat = pos.coords.latitude.toFixed(6);
      var lon = pos.coords.longitude.toFixed(6);
      status.textContent = '✅ ' + lat + ', ' + lon;
      // URLクエリパラメータを更新してStreamlitに伝える
      var url = new URL(window.parent.location.href);
      url.searchParams.set('gps_lat', lat);
      url.searchParams.set('gps_lon', lon);
      url.searchParams.set('gps_ts', Date.now());
      window.parent.history.replaceState(null, '', url.toString());
    },
    function(err) {
      status.textContent = '❌ GPS取得失敗: ' + err.message;
    },
    {enableHighAccuracy: true, timeout: 20000, maximumAge: 15000}
  );
}

// 追跡中は30秒ごとに自動取得
if (true) {
  getAndSendGPS();   // 初回即時取得
  setInterval(getAndSendGPS, 30000);
}
</script>
"""
    if _is_tracking:
        _comp.html(_gps_js, height=70)
    else:
        # 停止中は手動ボタンのみ（自動更新なし）
        _gps_js_manual = _gps_js.split("// 追跡中は30秒")[0] + "</script>"
        _comp.html(_gps_js_manual, height=70)

    # ── URLクエリパラメータからGPS座標を読み取る ──
    _qp = st.query_params
    _gps_lat_q = _qp.get("gps_lat")
    _gps_lon_q = _qp.get("gps_lon")

    if _gps_lat_q and _gps_lon_q:
        try:
            _cur_lat_v = float(_gps_lat_q)
            _cur_lon_v = float(_gps_lon_q)
            # 位置履歴に追加（新しい位置のみ）
            _hist = _ss.get("position_history", [])
            if not _hist or abs(_hist[-1]["lat"] - _cur_lat_v) > 0.00005                          or abs(_hist[-1]["lon"] - _cur_lon_v) > 0.00005:
                _ss["position_history"] = _hist + [{"lat": _cur_lat_v,
                                                     "lon": _cur_lon_v,
                                                     "time": _dt.now()}]
            _ss["current_lat"] = _cur_lat_v
            _ss["current_lon"] = _cur_lon_v
        except ValueError:
            _cur_lat_v = _ss.get("current_lat")
            _cur_lon_v = _ss.get("current_lon")
    else:
        _cur_lat_v = _ss.get("current_lat")
        _cur_lon_v = _ss.get("current_lon")

    # ── 計算・表示 ──
    if _cur_lat_v and _cur_lon_v and len(_route_coords_now) >= 2:
        # ① ルート上の最近接点
        _near_idx, _near_dist, _passed_dist = find_nearest_point_on_route(
            _cur_lat_v, _cur_lon_v, _route_coords_now)
        _rem_coords, _rem_dist = calc_remaining_route(_route_coords_now, _near_idx)
        _total_dist = _rt_now.get("distance_m", 1) or 1
        _progress   = min(_passed_dist / _total_dist * 100, 100)

        # ② 実測ペース
        _cur_pace  = estimate_current_pace(_ss.get("position_history", []))
        _sim_eng   = _ss.get("engine_a_out", {})
        _spd_curve = _sim_eng.get("speed_curve", np.array([1.0]))
        _plan_pace = float(np.mean(_spd_curve)) if len(_spd_curve) > 0 else 1.0

        # ③ 現在地の天気予報を取得・シミュレーションに反映
        _now_weather = _ss.get("current_weather")
        if _is_tracking:
            try:
                _now_weather = fetch_weather_forecast(
                    OWM_KEY, _cur_lat_v, _cur_lon_v, _dt.now(),
                    activity_type=ACTIVITY_TO_SIM[_ss.get("rp_activity","🏔️ 登山・ハイキング")])
                _ss["current_weather"] = _now_weather
            except Exception:
                pass   # 取得失敗時は前回の値を使い続ける

        _wf_now = _now_weather.weather_factor if _now_weather else 1.0

        # ④ 残り区間の斜度列を取得
        _prof_now = _ss.get("sim_profile")
        _df_now   = load_default_data()
        _e_now_val = None
        _rem_eta_min = calc_eta_from_pace(_rem_dist, _cur_pace, _plan_pace)

        if _prof_now and _df_now is not None and len(_rem_coords) >= 2:
            try:
                _rem_elevs = _rt_now.get("elevations", [])
                _rem_elevs_slice = _rem_elevs[_near_idx:] if _rem_elevs else []
                if len(_rem_elevs_slice) >= 2:
                    _spd_h = 1.0 if _prof_now.activity_type=="hiking" else                              (3.0 if _prof_now.activity_type=="running" else 6.94)
                    _sa_rem, _ea_rem, _da_rem = coords_to_slope_array(
                        _rem_coords, _rem_elevs_slice, speed_ms=_spd_h)

                    # 現在地点の天気・標高を反映した新しいプロファイル
                    _prof_updated = UserProfile(
                        body_weight_kg       = _prof_now.body_weight_kg,
                        load_weight_kg       = _prof_now.load_weight_kg,
                        hr_max               = _prof_now.hr_max,
                        terrain_factor       = _prof_now.terrain_factor,
                        e_initial_kcal       = _prof_now.e_initial_kcal,
                        activity_type        = _prof_now.activity_type,
                        base_run_speed_ms    = _prof_now.base_run_speed_ms,
                        base_cycling_speed_ms= _prof_now.base_cycling_speed_ms,
                        weather_factor       = float(_wf_now),
                        altitude_m           = float(np.mean(_ea_rem)))

                    # 残り区間の ke 計算でエネルギー消費を推定
                    from ke_estimator import energy_watt as _ew
                    _ke_rem = np.array([
                        _ew(float(_spd_h), float(s), _prof_updated)
                        for s in _sa_rem])
                    _dt_rem = 10.0  # 10秒ステップ
                    _e_consumed_rem = float(np.sum(_ke_rem * _dt_rem) / 4184)  # J→kcal
                    # 現在のエネルギー残量（シミュレーション値から推定）
                    _E_arr    = _ss["sim_result"]["E_kcal"]
                    _t_arr    = _ss["sim_result"]["time_min"]
                    _t_passed = (_passed_dist / _total_dist) * _t_arr[-1]
                    _e_idx    = min(int(_t_passed / max(_t_arr[-1],1) * len(_E_arr)),
                                   len(_E_arr)-1)
                    _e_current = max(float(_E_arr[_e_idx]), 0)
                    _e_at_goal = max(_e_current - _e_consumed_rem, 0)
                    _e_now_val = _e_current
            except Exception as _calc_err:
                pass

        # ⑤ 経過時間・次の補給
        _t_arr    = _ss["sim_result"]["time_min"]
        _elapsed  = (_passed_dist / _total_dist) * _t_arr[-1]
        _supply_now  = _ss.get("sim_supply", [])
        _next_supply = next((_ev for _ev in _supply_now if _ev.time_min > _elapsed), None)

        # ─── ダッシュボード ───────────────────────────
        st.markdown("### 📊 現在の状況")

        # 天気バナー
        if _now_weather and "取得失敗" not in (_now_weather.location or ""):
            _wf_pct = (_wf_now - 1) * 100
            st.caption(
                f"🌤️ 現在地の天気: {_now_weather.location}  "
                f"{_now_weather.temp_c:.1f}℃  湿度{_now_weather.humidity_pct:.0f}%  "
                f"風{_now_weather.wind_speed_ms:.1f}m/s  "
                f"気象補正 ×{_wf_now:.2f}（{_wf_pct:+.0f}%）")

        _d1, _d2, _d3, _d4 = st.columns(4)
        _d1.metric("進捗", f"{_progress:.0f}%",
                   delta=f"残 {_rem_dist/1000:.1f}km")
        _d2.metric("残り予測時間", fmt_minutes(_rem_eta_min),
                   delta=(f"実測 {_cur_pace*60:.0f}m/min" if _cur_pace > 0.1
                          else "ペース計算中"))
        _d3.metric("現在のエネルギー残量",
                   f"{_e_now_val:.0f} kcal" if _e_now_val is not None else "計算中",
                   delta=(f"ゴール時推定 {_e_at_goal:.0f}kcal"
                          if _e_now_val is not None and "_e_at_goal" in dir() else ""))
        _d4.metric("ルート誤差", f"{_near_dist:.0f}m",
                   delta="良好" if _near_dist < 100 else "ルート外",
                   delta_color="normal" if _near_dist < 100 else "inverse")

        st.progress(int(_progress))
        st.caption(f"最終更新: {_dt.now().strftime('%H:%M:%S')}  |  "
                   f"位置履歴: {len(_ss.get('position_history',[]))}点")

        # 補給アラート
        if _next_supply:
            _t2sup = _next_supply.time_min - _elapsed
            if _t2sup <= 0:
                st.error(f"🍙 **今すぐ補給！** {_next_supply.kcal:.0f}kcal（{_t2sup:.0f}分超過）")
            elif _t2sup <= 15:
                st.warning(f"⚠️ **まもなく補給** あと{_t2sup:.0f}分  {_next_supply.kcal:.0f}kcal")
            else:
                st.info(f"🍙 次の補給: {fmt_minutes(_next_supply.time_min)}  "
                        f"（あと{_t2sup:.0f}分・{_next_supply.kcal:.0f}kcal）")
        elif _supply_now:
            st.success("✅ 全補給完了")

        # 地図
        if _near_dist < 1000:
            import folium as _fl
            from streamlit_folium import st_folium as _stf
            _m_now = _fl.Map(location=[_cur_lat_v, _cur_lon_v], zoom_start=14)
            _fl.TileLayer(
                "https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png",
                attr="国土地理院", name="地理院地図").add_to(_m_now)
            if len(_route_coords_now) >= 2:
                _fl.PolyLine(_route_coords_now, color="#bdc3c7",
                             weight=3, opacity=0.6).add_to(_m_now)
            if len(_rem_coords) >= 2:
                _fl.PolyLine(_rem_coords, color="#e74c3c",
                             weight=4, opacity=0.9).add_to(_m_now)
            _fl.Marker([_cur_lat_v, _cur_lon_v], popup="現在地",
                icon=_fl.Icon(color="blue", icon="person", prefix="fa")).add_to(_m_now)
            _fl.Marker(_route_coords_now[-1],
                popup=f"ゴール（残{_rem_dist/1000:.1f}km）",
                icon=_fl.Icon(color="red", icon="flag", prefix="fa")).add_to(_m_now)
            with st.container():
                _stf(_m_now, use_container_width=True, height=360,
                     key="realtime_map", returned_objects=[])
        else:
            st.warning(f"⚠️ ルートから{_near_dist:.0f}m離れています。GPS精度を確認してください。")

    else:
        if _is_tracking:
            st.info("📡 GPS座標を取得中です... しばらくお待ちください")
        else:
            st.caption("「🟢 活動を開始」ボタンを押すと自動追跡が始まります")
