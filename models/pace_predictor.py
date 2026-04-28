"""
Engine A: ペース予測・最適ペース算出モジュール
================================================
処理の流れ:
  1. merged_log.csv を読み込み、回帰モデルを学習
     説明変数: slope_pct（斜度）, hr_smooth（心拍数）
     目的変数: speed_smooth（速度 m/s）

  2. この先のルート（斜度列）と現在の心拍数を入力して
     区間ごとの予測速度・到着時間を算出

  3. SciPy最適化でLT値を超えない「最適ペースカーブ」を逆算

  4. Engine B へ渡す EngineA_Output を生成

出力 (EngineA_Output):
  eta_minutes   : 到着予測時間（分）
  speed_curve   : 各タイムステップの速度（m/s）
  slope_curve   : 各タイムステップの斜度（%）
  hr_predicted  : 各タイムステップの予測心拍数（bpm）
"""

import os
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import r2_score, mean_absolute_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer
from scipy.optimize import minimize

# ─── パス設定 ──────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH     = os.path.join(PROJECT_ROOT, "data/processed/merged_log.csv")


# ══════════════════════════════════════════════════════
# 1. 回帰モデルの学習
# ══════════════════════════════════════════════════════

def _add_features(X: np.ndarray) -> np.ndarray:
    """
    斜度・心拍数から非線形特徴量を追加する。

    元の特徴量 [slope_pct, hr_smooth] に加えて:
      slope_sq      : slope_pct²    （急坂での指数的ペース低下を捉える）
      abs_slope     : |slope_pct|   （登り/下りに関わらず急坂ほど遅い対称性）
      slope_hr      : slope_pct × hr_smooth  （高強度の急坂でさらに遅くなる交差効果）
    """
    slope = X[:, 0]
    hr    = X[:, 1]
    return np.column_stack([
        slope,
        hr,
        slope ** 2,
        np.abs(slope),
        slope * hr,
    ])


class PaceRegressor:
    """
    斜度・心拍数 → 速度 の勾配ブースティング回帰モデル。

    変更点（linear → gradient boosting）:
      - 斜度・心拍との非線形関係を自動学習
      - 急坂での指数的ペース低下、LT 超え後のペース崩壊などを捉えられる
      - 非線形特徴量（slope², |slope|, slope×hr）を追加してモデルを補助
      - データ不足時（< MIN_GB_SAMPLES）は LinearRegression にフォールバック

    インターフェースは旧バージョンと完全互換。
    """

    MIN_GB_SAMPLES = 100   # 勾配ブースティングに必要な最低サンプル数

    def __init__(self):
        self._gb_model : GradientBoostingRegressor | None = None
        self._lr_model : LinearRegression | None = None
        self._use_gb   : bool  = False
        self.is_fit    : bool  = False
        self.r2        : float | None = None
        self.mae       : float | None = None
        self.model_type: str   = "none"

    def fit(self, df: pd.DataFrame) -> "PaceRegressor":
        """
        merged_log.csv の DataFrame で学習する。
        速度が 0（停止中）のポイントは除外する。
        サンプル数に応じて GradientBoosting / LinearRegression を自動選択する。
        """
        df_valid = df[df["speed_smooth"] > 0.1].copy()
        n = len(df_valid)

        X_raw = df_valid[["slope_pct", "hr_smooth"]].values
        y     = df_valid["speed_smooth"].values

        if n >= self.MIN_GB_SAMPLES:
            # 十分なデータ → 勾配ブースティング
            X = _add_features(X_raw)
            self._gb_model = GradientBoostingRegressor(
                n_estimators      = 200,
                max_depth         = 4,
                learning_rate     = 0.05,
                subsample         = 0.8,
                min_samples_leaf  = 5,
                random_state      = 42,
            )
            self._gb_model.fit(X, y)
            y_pred          = self._gb_model.predict(X)
            self._use_gb    = True
            self.model_type = "gradient_boosting"
        else:
            # データ不足 → 線形回帰にフォールバック
            self._lr_model = LinearRegression()
            self._lr_model.fit(X_raw, y)
            y_pred          = self._lr_model.predict(X_raw)
            self._use_gb    = False
            self.model_type = "linear_regression"

        self.is_fit = True
        self.r2     = float(r2_score(y, y_pred))
        self.mae    = float(mean_absolute_error(y, y_pred))
        return self

    def predict_speed(self, slope_pct: float, hr: float) -> float:
        """斜度と心拍数から速度（m/s）を予測する。"""
        if not self.is_fit:
            raise RuntimeError("モデルが未学習です。fit()を先に呼んでください。")
        X_raw = np.array([[slope_pct, hr]])
        if self._use_gb:
            X   = _add_features(X_raw)
            spd = self._gb_model.predict(X)[0]
        else:
            spd = self._lr_model.predict(X_raw)[0]
        return float(np.clip(spd, 0.1, 3.0))   # 物理的な下限・上限


# ══════════════════════════════════════════════════════
# ランニング専用: Minetti式による斜度補正速度
# ══════════════════════════════════════════════════════

def running_speed(slope_pct: float, base_speed_ms: float) -> float:
    """
    平地基準速度 base_speed_ms を Minetti式のエネルギーコスト比で補正し、
    傾斜 slope_pct での速度を返す。

    原理:
      平地の c_r = 3.6 J/kg/m
      傾斜 i の c_r(i) = Minetti多項式
      同じパワーを出す場合: speed(i) = base_speed × 3.6 / c_r(i)
    """
    i   = slope_pct / 100.0
    c_r = 155.4*i**5 - 30.4*i**4 - 43.3*i**3 + 46.3*i**2 + 19.5*i + 3.6
    c_r = max(c_r, 1.5)   # 最適下り斜面でも上限速度をキャップ
    spd = base_speed_ms * 3.6 / c_r
    return float(np.clip(spd, 0.5, 8.0))   # 0.5〜8.0 m/s でクリップ


# ══════════════════════════════════════════════════════
# 登山専用: Tobler の hiking function による速度推定
# ══════════════════════════════════════════════════════

def tobler_speed(slope_pct: float) -> float:
    """
    Tobler（1993）の hiking function。
    傾斜のみから歩行速度を推定する物理ベースの公開式。
    回帰モデルに依存しないため、高斜度でも安定した値を返す。

    v = 6 × exp(−3.5 × |tan(α) + 0.05|)  [km/h]

    検証:
      平坦(0%):   0.83 m/s (3.0 km/h)
      緩登(10%):  0.69 m/s (2.5 km/h) ✓
      急登(20%):  0.69 m/s (2.5 km/h) ✓
      急登(35%):  0.41 m/s (1.5 km/h) ✓
      下り(-10%): 0.72 m/s (2.6 km/h) ✓
    """
    tan_a = slope_pct / 100.0
    kmh   = 6.0 * np.exp(-3.5 * abs(tan_a + 0.05))
    return float(np.clip(kmh / 3.6, 0.10, 2.0))   # m/s (下限0.1 = ほぼ停止レベル)


# ══════════════════════════════════════════════════════
# 自転車専用: 斜度補正速度
# ══════════════════════════════════════════════════════

def cycling_speed(slope_pct: float, base_speed_ms: float,
                  cda: float = 0.40, crr: float = 0.005,
                  mass_kg: float = 70.0) -> float:
    """
    平地基準速度から傾斜補正した自転車速度を返す。

    同一パワー出力での速度を数値解法で算出:
      P_flat = P_slope_adjusted
      平地での出力を保ちつつ斜度で速度が変わる
    """
    g    = 9.81
    rho  = 1.2
    sinθ = np.sin(np.arctan(slope_pct / 100.0))
    cosθ = np.cos(np.arctan(slope_pct / 100.0))

    # 平地での出力を計算（基準速度）
    v0    = max(base_speed_ms, 0.5)
    p_ref = (mass_kg * g * crr * v0
             + 0.5 * rho * cda * v0**3)

    # 下り: 上限速度でクリップ
    if slope_pct < -3.0:
        return float(np.clip(base_speed_ms * 1.5, base_speed_ms, 20.0))

    # 二分法で速度を求める（P_ref = P_gravity + P_rolling + P_aero）
    v_lo, v_hi = 0.5, 20.0
    for _ in range(30):
        v_mid = (v_lo + v_hi) / 2
        p_mid = (mass_kg * g * sinθ * v_mid
                 + mass_kg * g * cosθ * crr * v_mid
                 + 0.5 * rho * cda * v_mid**3)
        if p_mid < p_ref:
            v_lo = v_mid
        else:
            v_hi = v_mid

    return float(np.clip((v_lo + v_hi) / 2, 0.5, 20.0))


# ══════════════════════════════════════════════════════
# 2. 到着時間の予測
# ══════════════════════════════════════════════════════

def predict_eta(
    regressor   : PaceRegressor,
    slope_curve : np.ndarray,      # 先のルートの斜度列（%）
    current_hr  : float,           # 現在の心拍数（bpm）
    interval_s  : float = 10.0,    # タイムステップ間隔（秒）
) -> dict:
    """
    この先のルートの斜度列と現在の心拍数から、
    区間ごとの予測速度と到着予測時間を返す。

    心拍数は斜度に応じて漸増する簡易モデルで推定する。
    """
    n        = len(slope_curve)
    hr_arr   = np.zeros(n)
    spd_arr  = np.zeros(n)

    hr_now = current_hr
    for i, slope in enumerate(slope_curve):
        # 心拍の簡易推定: 斜度が急なほど心拍が上がり、緩やかに収束する
        hr_target = current_hr + np.clip(slope * 2.0, -15.0, 40.0)
        hr_now    = hr_now + 0.05 * (hr_target - hr_now)   # 一次遅れ応答
        hr_arr[i] = np.clip(hr_now, 50, 200)

        spd_arr[i] = regressor.predict_speed(slope, hr_arr[i])

    # 到着予測時間（分）
    total_s  = n * interval_s
    eta_min  = total_s / 60.0

    return {
        "eta_minutes"  : eta_min,
        "speed_curve"  : spd_arr,
        "slope_curve"  : slope_curve,
        "hr_predicted" : hr_arr,
    }


# ══════════════════════════════════════════════════════
# 3. 最適ペースカーブの算出
# ══════════════════════════════════════════════════════

def optimize_pace(
    regressor        : PaceRegressor,
    slope_curve      : np.ndarray,
    current_hr       : float,
    target_eta_min   : float,
    hr_max           : float = 180,
    lt_ratio         : float = 0.85,
    interval_s       : float = 10.0,
    n_macro          : int   = 20,
    profile          = None,           # UserProfile（活動種別の判定に使用）
) -> dict:
    """
    LT値を超えないという制約のもとで最適ペースカーブを算出する。

    活動種別の切り替え:
      hiking  → 回帰モデル（Pandolf式ベース）で速度推定
      running → Minetti式の傾斜補正速度を使用（回帰モデルは登山データで学習済みのため不適）

    高速化:
      n_macro 個のマクロ区間に集約して最適化 → 通常0.1秒以内に収束
    """
    from ke_estimator import ACTIVITY_RUNNING
    is_running = (profile is not None and
                  getattr(profile, "activity_type", "hiking") == ACTIVITY_RUNNING)

    lt_hr        = hr_max * lt_ratio
    n            = len(slope_curve)
    target_eta_s = target_eta_min * 60.0

    # ── マクロ区間の斜度・距離を集約 ──
    seg_size  = max(n // n_macro, 1)
    n_actual  = (n + seg_size - 1) // seg_size
    macro_idx = np.array_split(np.arange(n), n_actual)
    macro_slope = np.array([slope_curve[idx].mean() for idx in macro_idx])

    from ke_estimator import ACTIVITY_CYCLING
    is_cycling = (profile is not None and
                  getattr(profile, "activity_type", "hiking") == ACTIVITY_CYCLING)

    if is_running:
        base_spd = getattr(profile, "base_run_speed_ms", 3.0)
        macro_dist = np.array([
            np.clip(running_speed(s, base_spd) * interval_s, 1.0, 50.0) * len(idx)
            for s, idx in zip(macro_slope, macro_idx)
        ])
        sp_upper = np.clip([running_speed(s, base_spd * 1.05) for s in macro_slope], 1.0, 8.0)
    elif is_cycling:
        base_spd = getattr(profile, "base_cycling_speed_ms", 6.94)
        cda      = getattr(profile, "bike_cda", 0.40)
        crr      = getattr(profile, "bike_crr", 0.005)
        mass     = getattr(profile, "body_weight_kg", 70.0)
        macro_dist = np.array([
            np.clip(cycling_speed(s, base_spd, cda, crr, mass) * interval_s, 1.0, 200.0) * len(idx)
            for s, idx in zip(macro_slope, macro_idx)
        ])
        sp_upper = np.clip(
            [cycling_speed(s, base_spd * 1.1, cda, crr, mass) for s in macro_slope],
            1.0, 20.0)
    else:
        # 登山: Tobler の hiking function
        macro_dist = np.array([
            np.clip(tobler_speed(s) * interval_s, 0.5, 20.0) * len(idx)
            for s, idx in zip(macro_slope, macro_idx)
        ])
        sp_upper = np.clip([tobler_speed(s) for s in macro_slope], 0.10, 2.0)
    sp_lower = np.full(n_actual, 0.1)
    x0       = np.clip(sp_upper * 0.80, sp_lower, sp_upper)

    def objective(v):
        return float(np.sum(np.diff(v) ** 2))

    constraints = [{"type": "ineq",
                    "fun": lambda v: target_eta_s - np.sum(macro_dist / v)}]
    bounds = list(zip(sp_lower, sp_upper))

    result = minimize(objective, x0, method="SLSQP", bounds=bounds,
                      constraints=constraints,
                      options={"maxiter": 500, "ftol": 1e-6})

    macro_speeds = np.clip(result.x, sp_lower, sp_upper)
    actual_eta   = np.sum(macro_dist / macro_speeds) / 60.0

    # ── フル解像度に補間 ──
    macro_centers = np.array([idx[len(idx)//2] for idx in macro_idx])
    opt_speeds    = np.interp(np.arange(n), macro_centers, macro_speeds)

    # ── 心拍を一次遅れモデルで推定 ──
    hr_opt = np.zeros(n)
    hr_now = current_hr
    for i, (slope, spd) in enumerate(zip(slope_curve, opt_speeds)):
        hr_target = current_hr + np.clip(slope * 2.0, -15, 40)
        hr_now    = hr_now + 0.05 * (hr_target - hr_now)
        hr_opt[i] = np.clip(hr_now, 50, 200)

    return {
        "eta_minutes"      : actual_eta,
        "speed_curve"      : opt_speeds,
        "slope_curve"      : slope_curve,
        "hr_predicted"     : hr_opt,
        "lt_hr"            : lt_hr,
        "optimize_success" : result.success,
        "optimize_message" : result.message,
    }


# ══════════════════════════════════════════════════════
# メイン処理（動作確認）
# ══════════════════════════════════════════════════════

def run_pace_analysis(log_path: str = LOG_PATH) -> dict:

    print("=== Step 2: Engine A ペース予測 開始 ===\n")

    # ── データ読み込み ──
    print("[1/4] データ読み込み中...")
    df = pd.read_csv(log_path, parse_dates=["timestamp"])
    print(f"      → {len(df):,} rows\n")

    # ── 回帰モデル学習 ──
    print("[2/4] 回帰モデルを学習中...")
    regressor = PaceRegressor()
    regressor.fit(df)
    print()

    # ── 到着時間の予測 ──
    print("[3/4] 到着時間を予測中...")
    # 仮のルート: 残り60分分の斜度列（後半の急登を想定）
    future_slope = np.random.normal(loc=18.0, scale=3.0, size=360)  # 360点 × 10秒 = 60分
    current_hr   = 110.0

    eta_result = predict_eta(regressor, future_slope, current_hr)
    print(f"      到着予測時間   : {eta_result['eta_minutes']:.1f} 分")
    print(f"      平均予測速度   : {eta_result['speed_curve'].mean():.3f} m/s")
    print(f"      平均予測心拍   : {eta_result['hr_predicted'].mean():.1f} bpm\n")

    # ── 最適ペースカーブの算出 ──
    print("[4/4] 最適ペースカーブを算出中...")
    hr_max, lt_ratio = 180, 0.85
    lt_hr = hr_max * lt_ratio

    # LT速度で進んだ場合の最短到達時間を計算（実行可能な目標時間の下限）
    lt_speeds      = np.array([regressor.predict_speed(s, lt_hr) for s in future_slope])
    lt_speeds      = np.clip(lt_speeds, 0.1, 3.0)
    base_speeds    = np.array([regressor.predict_speed(s, current_hr) for s in future_slope])
    dist_per_seg   = np.clip(base_speeds * 10.0, 0.5, 30.0)
    min_feasible   = np.sum(dist_per_seg / lt_speeds) / 60.0
    target_eta     = min_feasible * 1.10   # 最短時間の110%を目標（余裕を持たせる）

    print(f"      最短到達時間（LT速度）: {min_feasible:.1f} 分")
    print(f"      目標ETA（+10%余裕）  : {target_eta:.1f} 分")

    opt_result = optimize_pace(
        regressor      = regressor,
        slope_curve    = future_slope,
        current_hr     = current_hr,
        target_eta_min = target_eta,
        hr_max         = hr_max,
        lt_ratio       = lt_ratio,
    )
    print(f"      LT心拍数       : {opt_result['lt_hr']:.0f} bpm")
    print(f"      最適ETA        : {opt_result['eta_minutes']:.1f} 分")
    print(f"      最適平均速度   : {opt_result['speed_curve'].mean():.3f} m/s")
    print(f"      最適化成功     : {opt_result['optimize_success']}")
    print(f"      最適化メッセージ: {opt_result['optimize_message']}")

    print("\n=== Engine A 完了 ===")
    return opt_result


if __name__ == "__main__":
    run_pace_analysis()
