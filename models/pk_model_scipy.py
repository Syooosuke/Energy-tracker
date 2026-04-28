"""
Engine B: PKモデル (SciPy実装)
================================
1コンパートメントモデルを scipy.integrate.solve_ivp で数値積分する。

ODE:
  dE/dt = Ka * A(t) - Ke(t) * E(t)
  dA/dt = -Ka * A(t)

ポイント:
  - Ke(t) は各積分ステップで Pandolf式 + 心拍ゾーン補正から動的に計算する
  - 補給食の摂取イベントは A(t) へのインパルス加算で表現する
  - Engine A の EngineA_Output を入力として受け取る

出力:
  time_min   : 時刻列 (分)
  E_kcal     : E(t) (kcal)
  A_kcal     : A(t) (kcal)
  Ke_series  : Ke(t) (1/hr)
  alert_times: ハンガーノック警告が発生した時刻 (分)
"""

import numpy as np
from dataclasses import dataclass, field
from typing import List, Tuple

from ke_estimator import UserProfile, estimate_ke_series, energy_watt, WATT_TO_KCAL_HR


# ══════════════════════════════════════════════════════
# 補給食イベント
# ══════════════════════════════════════════════════════

@dataclass
class SupplyEvent:
    """
    補給食の摂取イベント。
    time_min: 摂取タイミング (分)
    kcal    : 摂取カロリー (kcal)
    ka      : 吸収速度定数 (1/hr) ← 食品マスタより
    """
    time_min : float
    kcal     : float
    ka       : float = 1.0   # デフォルト: GI中程度の補給食

    @staticmethod
    def from_food(time_min: float, food_name: str, food_master: dict) -> "SupplyEvent":
        """食品マスタから Ka を参照してイベントを生成する。"""
        food = food_master.get(food_name, {"kcal": 100, "ka": 1.0})
        return SupplyEvent(time_min=time_min, kcal=food["kcal"], ka=food["ka"])


# ══════════════════════════════════════════════════════
# 補給アラートロジック
# ══════════════════════════════════════════════════════

@dataclass
class AlertConfig:
    e_crit_kcal   : float = 300.0   # 危険水準 (kcal)
    t_warn_min    : float = 25.0    # 警告リードタイム (分)


def check_alert(
    t_now   : float,
    E_now   : float,
    A_now   : float,
    Ka_now  : float,
    Ke_now  : float,
    config  : AlertConfig,
) -> bool:
    """
    T_warn 分後の E(t) を簡易予測し、E_crit を下回るか判定する。
    補給なし（A=0）の条件で solve_ivp を短距離積分して予測する。
    """
    t_warn_hr = config.t_warn_min / 60.0

    from scipy.integrate import solve_ivp

    def ode_no_supply(t, y):
        E, A = y
        dE = Ka_now * A - Ke_now * E
        dA = -Ka_now * A
        return [dE, dA]

    sol = solve_ivp(
        ode_no_supply,
        t_span = (0, t_warn_hr),
        y0     = [E_now, A_now],
        method = "RK45",
        dense_output = False,
    )
    E_predicted = sol.y[0, -1]
    return E_predicted < config.e_crit_kcal


# ══════════════════════════════════════════════════════
# PKモデル本体
# ══════════════════════════════════════════════════════

def run_pk_model(
    engine_a_output  : dict,
    profile          : UserProfile,
    supply_events    : List[SupplyEvent] = None,
    alert_config     : AlertConfig       = None,
    interval_s       : float             = 10.0,
    fatigue_model    = None,   # FatigueModel | None
    hr_zone_estimator = None,  # HRZoneEstimator | None
) -> dict:
    """
    Engine A の出力を受け取り、E(t) の時系列をシミュレーションする。

    Parameters
    ----------
    engine_a_output   : dict
        pace_predictor.py の predict_eta() / optimize_pace() の戻り値
        必須キー: speed_curve, slope_curve, hr_predicted
    profile           : UserProfile
    supply_events     : 補給食イベントのリスト
    alert_config      : ハンガーノック警告の設定
    interval_s        : タイムステップ間隔 (秒)
    fatigue_model     : FatigueModel。指定すると累積疲労をペースに反映する。
    hr_zone_estimator : HRZoneEstimator。指定するとデータ駆動のゾーン補正を使用。
    """
    if supply_events is None:
        supply_events = []
    if alert_config is None:
        alert_config = AlertConfig()

    speed_arr = engine_a_output["speed_curve"]
    slope_arr = engine_a_output["slope_curve"]
    hr_arr    = engine_a_output["hr_predicted"]
    n_steps   = len(speed_arr)
    dt_hr     = interval_s / 3600.0        # タイムステップ (hr)
    dt_min    = interval_s / 60.0          # タイムステップ (min)

    # ── 結果配列を初期化 ──
    time_min      = np.arange(n_steps) * dt_min
    E_arr         = np.zeros(n_steps)
    A_arr         = np.zeros(n_steps)
    Ke_arr        = np.zeros(n_steps)
    fatigue_arr   = np.ones(n_steps)    # 疲労係数時系列（疲労なし=1.0）

    E_arr[0]  = profile.e_initial_kcal
    A_arr[0]  = 0.0

    # 補給イベントを辞書に変換（分 → インデックス）
    supply_dict: dict[int, SupplyEvent] = {}
    for ev in supply_events:
        idx = int(ev.time_min / dt_min)
        supply_dict[idx] = ev

    alert_times: List[float] = []
    supply_log: List[dict]  = []
    Ka = 1.0

    # ── ゾーン補正配列を事前計算 ──────────────────────────
    # hr_zone_estimator がある場合はデータ駆動の閾値を使用
    if hr_zone_estimator is not None:
        zone_corr = hr_zone_estimator.zone_correction_arr(hr_arr)
    else:
        hr_ratio  = hr_arr / profile.hr_max
        zone_corr = np.where(hr_ratio < 0.60, 0.90,
                    np.where(hr_ratio < 0.80, 1.00, 1.15))

    # ── 疲労なしの場合: Ke 分子をベクトル計算（高速）──────
    use_fatigue = fatigue_model is not None
    if not use_fatigue:
        w_kcal_hr_arr = np.array([
            energy_watt(speed_arr[i], slope_arr[i], profile) * WATT_TO_KCAL_HR
            for i in range(n_steps)
        ])
        Ke_num = w_kcal_hr_arr * zone_corr

    # ── タイムステップごとに積分 ──
    for i in range(n_steps - 1):
        E = E_arr[i]
        A = A_arr[i]

        if i in supply_dict:
            ev  = supply_dict[i]
            A  += ev.kcal
            Ka  = ev.ka
            supply_log.append({
                "time_min": round(time_min[i], 1),
                "kcal":     ev.kcal,
                "ka":       ev.ka,
            })

        # ── 疲労補正（有効な場合のみ）────────────────────
        if use_fatigue:
            e_consumed     = max(profile.e_initial_kcal - E, 0.0)
            fatigue_f      = fatigue_model.factor(e_consumed)
            fatigue_arr[i] = fatigue_f
            adj_speed      = max(speed_arr[i] / fatigue_f, 0.1)
            w_kcal_hr      = energy_watt(adj_speed, slope_arr[i], profile) * WATT_TO_KCAL_HR
            Ke_i           = w_kcal_hr * zone_corr[i] / max(E, 50.0)
        else:
            Ke_i = Ke_num[i] / max(E, 50.0)

        Ke_arr[i] = Ke_i

        # RK4（ループ外で関数定義せずインライン展開して高速化）
        h   = dt_hr
        Ke  = Ke_i
        k1E = Ka * A          - Ke * E
        k1A = -Ka * A

        E2  = E + 0.5*h*k1E;  A2  = A + 0.5*h*k1A
        k2E = Ka * A2         - Ke * E2
        k2A = -Ka * A2

        E3  = E + 0.5*h*k2E;  A3  = A + 0.5*h*k2A
        k3E = Ka * A3         - Ke * E3
        k3A = -Ka * A3

        E4  = E + h*k3E;       A4  = A + h*k3A
        k4E = Ka * A4         - Ke * E4
        k4A = -Ka * A4

        E_new = E + (h / 6.0) * (k1E + 2*k2E + 2*k3E + k4E)
        A_new = A + (h / 6.0) * (k1A + 2*k2A + 2*k3A + k4A)

        E_arr[i+1] = np.clip(E_new, 0.0, profile.e_initial_kcal * 1.05)
        A_arr[i+1] = max(A_new, 0.0)

        # アラート判定（10ステップごとに実行してコストを抑える）
        if i % 10 == 0:
            alerted = check_alert(
                t_now   = time_min[i],
                E_now   = E_arr[i],
                A_now   = A_arr[i],
                Ka_now  = Ka,
                Ke_now  = Ke,
                config  = alert_config,
            )
            if alerted and (not alert_times or time_min[i] - alert_times[-1] > 10.0):
                alert_times.append(time_min[i])

    if len(Ke_arr) >= 2:
        Ke_arr[-1] = Ke_arr[-2]   # 末尾を補完
    if use_fatigue:
        fatigue_arr[-1] = fatigue_arr[-2]

    return {
        "time_min"    : time_min,
        "E_kcal"      : E_arr,
        "A_kcal"      : A_arr,
        "Ke_series"   : Ke_arr,
        "fatigue_arr" : fatigue_arr,   # 疲労係数時系列（疲労なし=1.0）
        "alert_times" : alert_times,
        "supply_log"  : supply_log,
        "profile"     : profile,
        "alert_config": alert_config,
    }


# ══════════════════════════════════════════════════════
# メイン処理（Engine A と統合した動作確認）
# ══════════════════════════════════════════════════════

def run_engine_b(log_path: str = None) -> dict:
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from pace_predictor import PaceRegressor, predict_eta
    import pandas as pd

    if log_path is None:
        log_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data/processed/merged_log.csv"
        )

    print("=== Step 3: Engine B (PKモデル) 開始 ===\n")

    # ── Engine A からペース予測を取得 ──
    print("[1/4] Engine A: ペース予測...")
    df         = pd.read_csv(log_path, parse_dates=["timestamp"])
    regressor  = PaceRegressor().fit(df)
    np.random.seed(42)
    # 急登区間を想定した60分分のルート
    future_slope = np.concatenate([
        np.random.normal(15.0, 2.0, 180),   # 前半30分: 急登
        np.random.normal( 5.0, 1.5, 180),   # 後半30分: 緩登り
    ])
    engine_a_out = predict_eta(regressor, future_slope, current_hr=105.0)
    print(f"      到着予測時間: {engine_a_out['eta_minutes']:.1f} 分\n")

    # ── ユーザープロフィール設定 ──
    print("[2/4] ユーザープロフィール設定...")
    profile = UserProfile(
        body_weight_kg = 65.0,
        load_weight_kg = 10.0,
        hr_max         = 180.0,
        terrain_factor = 1.2,
        e_initial_kcal = 1800.0,
    )
    print(f"      体重: {profile.body_weight_kg}kg  荷物: {profile.load_weight_kg}kg  "
          f"E(0): {profile.e_initial_kcal}kcal\n")

    # ── 補給食イベントの設定 ──
    print("[3/4] PKモデル実行中...")
    supply_events = [
        SupplyEvent(time_min=20.0, kcal=100.0, ka=2.0),   # 20分: エネルギージェル
        SupplyEvent(time_min=45.0, kcal=150.0, ka=0.9),   # 45分: おにぎり
    ]

    result = run_pk_model(
        engine_a_output = engine_a_out,
        profile         = profile,
        supply_events   = supply_events,
        alert_config    = AlertConfig(e_crit_kcal=300.0, t_warn_min=25.0),
    )

    # ── 結果サマリ ──
    print(f"\n[4/4] 結果サマリ:")
    E = result["E_kcal"]
    t = result["time_min"]
    print(f"      E(0)           : {E[0]:.1f} kcal")
    print(f"      E(終了時)      : {E[-1]:.1f} kcal")
    print(f"      最小E(t)       : {E.min():.1f} kcal  (t={t[E.argmin()]:.1f}分)")
    print(f"      総消費量       : {E[0] - E[-1]:.1f} kcal")
    print(f"      平均Ke         : {result['Ke_series'].mean():.4f} 1/hr")

    if result["alert_times"]:
        print(f"      ⚠ アラート発生 : {result['alert_times']} 分")
    else:
        print(f"      ✓ アラートなし（E(t) > E_crit を維持）")

    print("\n=== Engine B 完了 ===")
    return result


if __name__ == "__main__":
    run_engine_b()
