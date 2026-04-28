"""
完走確率予測モジュール (Monte Carlo Simulation)
================================================
現在のシミュレーション結果から「安全に完走できる確率」を 0〜100% で推定する。

手法: モンテカルロシミュレーション
  - N 回のシミュレーションを実行
  - 各試行でパラメータをランダムに摂動（実際の不確実性を模倣）
  - E(t) が危険水準 e_crit を下回らなかった試行の割合 = 完走確率

摂動対象パラメータ:
  weather_factor : ±15% (天気予報誤差)
  terrain_factor : ±10% (地形の不均一性)
  e_initial_kcal : ±150 kcal (個人差・食事内容のばらつき)
  speed_scale    : ±8%  (ペース予測誤差)

参考: Hausswirth & Mujika (2013) "Recovery for Performance in Sport"
"""

from __future__ import annotations
import numpy as np
from dataclasses import dataclass
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ke_estimator import UserProfile


# ══════════════════════════════════════════════════════
# 結果データクラス
# ══════════════════════════════════════════════════════

@dataclass
class CompletionResult:
    probability      : float         # 完走確率 0〜100 [%]
    confidence_low   : float         # 95%信頼区間 下限 [%]
    confidence_high  : float         # 95%信頼区間 上限 [%]
    n_simulations    : int           # 試行回数
    n_success        : int           # 成功回数
    min_e_mean       : float         # 成功試行での最小エネルギー平均 [kcal]
    min_e_p5         : float         # 最小エネルギーの5パーセンタイル [kcal]
    risk_level       : str           # "low" / "moderate" / "high" / "critical"
    message          : str           # ユーザー向けメッセージ


# ══════════════════════════════════════════════════════
# モンテカルロ完走確率予測器
# ══════════════════════════════════════════════════════

class CompletionPredictor:
    """
    モンテカルロシミュレーションで完走確率を推定するクラス。

    使用例:
        predictor = CompletionPredictor(n_sim=100)
        result = predictor.predict(engine_a, profile, alert_config)
        print(f"完走確率: {result.probability:.1f}%")
    """

    # 摂動の標準偏差（変動係数: 1σ = この割合だけパラメータが変動）
    _WEATHER_CV   = 0.08   # 天気予報誤差: ±8%
    _TERRAIN_CV   = 0.06   # 地形ファクタ誤差: ±6%
    _EINIT_STD    = 120.0  # 初期エネルギー誤差: ±120 kcal
    _SPEED_CV     = 0.06   # ペース誤差: ±6%

    def __init__(self, n_sim: int = 100, random_seed: int = 42):
        self.n_sim       = n_sim
        self.random_seed = random_seed

    def predict(
        self,
        engine_a_output : dict,
        profile         : "UserProfile",
        supply_events   : list | None = None,
        alert_config    = None,
        fatigue_model   = None,
        interval_s      : float = 10.0,
    ) -> CompletionResult:
        """
        モンテカルロシミュレーションで完走確率を推定する。

        Parameters
        ----------
        engine_a_output : Engine A の出力（speed_curve, slope_curve, hr_predicted）
        profile         : ユーザープロフィール
        supply_events   : 補給計画（Noneの場合は補給なしで評価）
        alert_config    : アラート設定（e_crit の参照に使用）
        fatigue_model   : FatigueModel（指定すると疲労を考慮したシミュレーション）
        interval_s      : タイムステップ間隔 [秒]
        """
        from pk_model_scipy import run_pk_model, AlertConfig

        if alert_config is None:
            alert_config = AlertConfig()
        if supply_events is None:
            supply_events = []

        e_crit = alert_config.e_crit_kcal
        rng    = np.random.default_rng(self.random_seed)

        speed_base  = engine_a_output["speed_curve"].copy()
        slope_arr   = engine_a_output["slope_curve"]
        hr_arr      = engine_a_output["hr_predicted"]

        min_e_list: list[float] = []
        success_count = 0

        for _ in range(self.n_sim):
            # ── パラメータ摂動 ──────────────────────────
            wf_perturb  = max(0.5, rng.normal(1.0, self._WEATHER_CV))
            tf_perturb  = max(0.5, rng.normal(1.0, self._TERRAIN_CV))
            ei_perturb  = profile.e_initial_kcal + rng.normal(0, self._EINIT_STD)
            ei_perturb  = float(np.clip(ei_perturb, 500, 5000))
            spd_scale   = max(0.5, rng.normal(1.0, self._SPEED_CV))

            # ── 摂動済みプロフィール生成 ──────────────────
            from copy import copy
            p_sim = copy(profile)
            p_sim.weather_factor  = float(np.clip(
                profile.weather_factor * wf_perturb, 0.5, 3.0))
            p_sim.terrain_factor  = float(np.clip(
                profile.terrain_factor * tf_perturb, 0.5, 5.0))
            p_sim.e_initial_kcal  = ei_perturb

            # ── 速度スケール適用 ───────────────────────
            speed_perturbed = np.clip(speed_base * spd_scale, 0.1, 20.0)

            ea_sim = dict(engine_a_output)
            ea_sim["speed_curve"] = speed_perturbed

            # ── シミュレーション実行 ───────────────────
            try:
                sim = run_pk_model(
                    engine_a_output = ea_sim,
                    profile         = p_sim,
                    supply_events   = supply_events,
                    alert_config    = alert_config,
                    interval_s      = interval_s,
                    fatigue_model   = fatigue_model,
                )
                min_e = float(sim["E_kcal"].min())
                min_e_list.append(min_e)
                if min_e > e_crit:
                    success_count += 1
            except Exception:
                # シミュレーション失敗 = 失敗カウント
                min_e_list.append(0.0)

        n_success = success_count
        n_total   = len(min_e_list)

        # ── 確率計算 ──────────────────────────────────
        prob = 100.0 * n_success / n_total if n_total > 0 else 0.0

        # Wilson スコア法で 95% 信頼区間を計算
        ci_low, ci_high = self._wilson_ci(n_success, n_total)

        # ── 最小エネルギーの統計 ──────────────────────
        min_e_arr = np.array(min_e_list)
        min_e_mean = float(min_e_arr.mean())
        min_e_p5   = float(np.percentile(min_e_arr, 5))

        # ── リスクレベル判定 ───────────────────────────
        risk_level, message = self._assess_risk(prob, min_e_p5, e_crit)

        return CompletionResult(
            probability     = round(prob, 1),
            confidence_low  = round(ci_low * 100, 1),
            confidence_high = round(ci_high * 100, 1),
            n_simulations   = n_total,
            n_success       = n_success,
            min_e_mean      = round(min_e_mean, 1),
            min_e_p5        = round(min_e_p5, 1),
            risk_level      = risk_level,
            message         = message,
        )

    # ── 内部ヘルパー ──────────────────────────────────

    @staticmethod
    def _wilson_ci(n_success: int, n_total: int,
                   confidence: float = 0.95) -> tuple[float, float]:
        """Wilson スコア法による二項分布の信頼区間。"""
        from scipy import stats
        if n_total == 0:
            return 0.0, 1.0
        z   = stats.norm.ppf((1 + confidence) / 2)
        p   = n_success / n_total
        denom = 1 + z**2 / n_total
        center = (p + z**2 / (2 * n_total)) / denom
        margin = z * np.sqrt(p * (1 - p) / n_total + z**2 / (4 * n_total**2)) / denom
        return float(np.clip(center - margin, 0, 1)), float(np.clip(center + margin, 0, 1))

    @staticmethod
    def _assess_risk(prob: float, min_e_p5: float, e_crit: float
                     ) -> tuple[str, str]:
        """完走確率からリスクレベルとメッセージを生成する。"""
        margin = min_e_p5 - e_crit  # 安全余裕 [kcal]

        if prob >= 90:
            return "low", f"安全に完走できる見込みが高いです（余裕エネルギー約 {max(margin,0):.0f} kcal）"
        elif prob >= 70:
            return "moderate", f"概ね完走できますが、補給計画に余裕を持たせることを推奨します"
        elif prob >= 50:
            return "high", f"完走リスクが高まっています。補給食を増やすか、ペースを落としてください"
        else:
            return "critical", f"このままでは途中でエネルギー切れになる可能性が高いです。補給計画の見直しが必要です"
