"""
疲労蓄積モデル (Banister Impulse-Response Model)
================================================
長時間活動で蓄積する疲労を推定し、ペース予測に補正をかける。

参考: Banister et al. (1991) "Modeling Elite Athletic Performance"
     Morton et al. (1990) "Modeling Human Performance in Running"

疲労係数 f(E_consumed):
  f = 1 + α × (1 − exp(−E_consumed / τ_E))

  α   : 最大疲労ペナルティ（例: 0.25 = 最大25%の速度低下）
  τ_E : 疲労飽和定数 [kcal]（τ_E消費時点で疲労が63%飽和）

速度への適用:
  adjusted_speed = base_speed / fatigue_factor

  疲労が増すと同じパワーを出しても速度が落ちる。
  これは筋グリコーゲン枯渇による神経筋効率の低下を模倣している。

活動別デフォルト値（経験的キャリブレーション）:
  hiking  : α=0.20, τ=700  kcal （荷物あり・筋疲労蓄積が早い）
  running : α=0.25, τ=900  kcal （後半ペースダウンが顕著）
  cycling : α=0.15, τ=1200 kcal （回転運動で筋疲労が少ない）
"""

from __future__ import annotations
import numpy as np
from dataclasses import dataclass


# ══════════════════════════════════════════════════════
# パラメータ定義
# ══════════════════════════════════════════════════════

@dataclass
class FatigueParams:
    alpha    : float   # 最大疲労ペナルティ (0〜1)
    tau_kcal : float   # 疲労飽和定数 [kcal]


_DEFAULTS: dict[str, FatigueParams] = {
    "hiking":  FatigueParams(alpha=0.20, tau_kcal=700.0),
    "running": FatigueParams(alpha=0.25, tau_kcal=900.0),
    "cycling": FatigueParams(alpha=0.15, tau_kcal=1200.0),
}


# ══════════════════════════════════════════════════════
# 疲労モデル本体
# ══════════════════════════════════════════════════════

class FatigueModel:
    """
    累積エネルギー消費量から疲労係数を計算するモデル。

    使用例:
        fm = FatigueModel("running")
        factor = fm.factor(e_consumed_kcal=600)   # 1.16 など
        adj_speed = fm.apply_to_speed(3.0, 600)   # ~2.59 m/s
    """

    def __init__(self, activity_type: str = "hiking",
                 alpha: float | None = None,
                 tau_kcal: float | None = None):
        params = _DEFAULTS.get(activity_type, _DEFAULTS["hiking"])
        self.alpha    = alpha    if alpha    is not None else params.alpha
        self.tau_kcal = tau_kcal if tau_kcal is not None else params.tau_kcal
        self.activity_type = activity_type

    # ── スカラー版 ─────────────────────────────────────

    def factor(self, e_consumed_kcal: float) -> float:
        """
        疲労係数を返す（≥ 1.0）。
        速度 = 基準速度 / factor  なので、因数が大きいほど遅い。
        """
        ec = max(e_consumed_kcal, 0.0)
        return 1.0 + self.alpha * (1.0 - np.exp(-ec / self.tau_kcal))

    def apply_to_speed(self, base_speed: float, e_consumed_kcal: float) -> float:
        """疲労補正済み速度 [m/s] を返す。下限 0.10 m/s。"""
        return max(base_speed / self.factor(e_consumed_kcal), 0.10)

    # ── 配列版（numpy vectorized） ─────────────────────

    def curve(self, e_consumed_arr: np.ndarray) -> np.ndarray:
        """
        累積消費エネルギーの配列から疲労係数の時系列を返す。

        Parameters
        ----------
        e_consumed_arr : shape (n,) の配列 [kcal]
            各タイムステップでの累積消費エネルギー

        Returns
        -------
        shape (n,) の疲労係数配列（最小1.0）
        """
        ec = np.maximum(e_consumed_arr, 0.0)
        return 1.0 + self.alpha * (1.0 - np.exp(-ec / self.tau_kcal))

    def apply_to_speed_arr(self, base_speeds: np.ndarray,
                            e_consumed_arr: np.ndarray) -> np.ndarray:
        """速度配列に疲労補正を適用する。"""
        factors = self.curve(e_consumed_arr)
        return np.maximum(base_speeds / factors, 0.10)

    # ── ユーティリティ ────────────────────────────────

    @property
    def info(self) -> dict:
        """パラメータ情報を辞書で返す。"""
        return {
            "activity_type": self.activity_type,
            "alpha":         self.alpha,
            "tau_kcal":      self.tau_kcal,
            "max_penalty_pct": round(self.alpha * 100, 1),
            "half_sat_kcal": round(self.tau_kcal * np.log(2), 1),  # 疲労が50%飽和するkcal
        }

    def __repr__(self) -> str:
        return (f"FatigueModel(activity={self.activity_type!r}, "
                f"α={self.alpha}, τ={self.tau_kcal} kcal)")
