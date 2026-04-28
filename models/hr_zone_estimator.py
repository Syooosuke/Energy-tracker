"""
心拍ゾーン自動推定モジュール
=============================
K-means クラスタリング（k=3）を用いて、
過去の心拍データから個人のHRゾーン境界を自動推定する。

従来の固定方式:
  Zone 1 (回復)  : hr < hr_max × 0.60
  Zone 2 (有酸素): hr_max × 0.60 ≦ hr < hr_max × 0.80
  Zone 3 (無酸素): hr_max × 0.80 ≦ hr

改善点:
  - 実際の活動ログから個人のHR分布を学習
  - HRmaxの推定誤差に依存しない
  - 活動種別・体力レベルの個人差を自動吸収

参考: Bourdon et al. (2017) "Monitoring Athlete Training Loads"
"""

from __future__ import annotations
import numpy as np
from dataclasses import dataclass, field
from typing import Optional


# ══════════════════════════════════════════════════════
# HRゾーン推定器
# ══════════════════════════════════════════════════════

class HRZoneEstimator:
    """
    K-means（k=3）で心拍ゾーン境界を自動推定するクラス。

    データ不足時は hr_max ベースのフォールバック閾値を使用する。
    データが十分な場合（≥30点）は K-means で実データから学習する。

    使用例:
        est = HRZoneEstimator()
        est.fit(df["hr_smooth"].values)

        zone = est.zone_for_hr(145.0)     # → 2
        corr = est.zone_correction(145.0)  # → 1.00
    """

    MIN_SAMPLES = 30   # 学習に必要な最低サンプル数

    def __init__(self, n_zones: int = 3):
        self.n_zones    = n_zones
        self.thresholds : list[float] = []   # ゾーン境界 [zone1_max, zone2_max, ...]
        self.centroids  : list[float] = []   # 各ゾーンの重心
        self._fitted    : bool        = False
        self._n_samples : int         = 0

    # ── 学習 ──────────────────────────────────────────

    def fit(self, hr_series: np.ndarray,
            hr_max: float = 180.0) -> "HRZoneEstimator":
        """
        心拍データから K-means でゾーン境界を推定する。

        Parameters
        ----------
        hr_series : 心拍数の配列 [bpm]
        hr_max    : ユーザーの最大心拍数（データ不足時のフォールバック用）
        """
        from sklearn.cluster import KMeans

        # 有効範囲でフィルタリング（安静時〜最大心拍の範囲）
        hr_valid = hr_series[(hr_series >= 40) & (hr_series <= 230)]
        self._n_samples = len(hr_valid)

        if self._n_samples < self.MIN_SAMPLES:
            # データ不足 → hr_max ベースのフォールバック
            self._set_fallback(hr_max)
            return self

        # K-means クラスタリング（k=n_zones）
        km = KMeans(n_clusters=self.n_zones, n_init=10, random_state=42)
        km.fit(hr_valid.reshape(-1, 1))

        # クラスタ重心を昇順ソート
        sorted_centroids = np.sort(km.cluster_centers_.flatten())
        self.centroids = sorted_centroids.tolist()

        # ゾーン境界: 隣接重心の中間点
        self.thresholds = [
            float((sorted_centroids[i] + sorted_centroids[i + 1]) / 2)
            for i in range(self.n_zones - 1)
        ]
        self._fitted = True
        return self

    def _set_fallback(self, hr_max: float) -> None:
        """データ不足時に hr_max ベースの固定閾値を設定する。"""
        self.thresholds = [hr_max * 0.60, hr_max * 0.80]
        self.centroids  = [hr_max * 0.45, hr_max * 0.70, hr_max * 0.90]
        self._fitted    = True   # フォールバック値でも利用可能状態にする

    # ── 推論 ──────────────────────────────────────────

    def zone_for_hr(self, hr: float) -> int:
        """心拍数からゾーン番号（1〜n_zones）を返す。"""
        if not self._fitted:
            return 2   # デフォルト: 有酸素ゾーン
        for zone_idx, threshold in enumerate(self.thresholds, start=1):
            if hr < threshold:
                return zone_idx
        return self.n_zones

    def zone_correction(self, hr: float) -> float:
        """
        ゾーンに応じたエネルギー消費補正係数を返す。

        Zone 1 (回復)  → 0.90（低強度のため基準より少ない）
        Zone 2 (有酸素)→ 1.00（基準）
        Zone 3 (無酸素)→ 1.15（高強度のため追加代謝コスト）
        """
        z = self.zone_for_hr(hr)
        corrections = {1: 0.90, 2: 1.00, 3: 1.15}
        return corrections.get(z, 1.00)

    def zone_correction_arr(self, hr_arr: np.ndarray) -> np.ndarray:
        """心拍数配列に対して補正係数配列を返す（高速化版）。"""
        if not self._fitted or len(self.thresholds) < 2:
            t1, t2 = 108.0, 144.0   # デフォルト（hr_max=180想定）
        else:
            t1, t2 = self.thresholds[0], self.thresholds[1]
        return np.where(hr_arr < t1, 0.90,
               np.where(hr_arr < t2, 1.00, 1.15))

    # ── 情報取得 ───────────────────────────────────────

    @property
    def zone_info(self) -> dict:
        """ゾーン情報を辞書形式で返す（API レスポンス用）。"""
        if not self._fitted:
            return {"fitted": False, "method": "not_fitted"}

        method = "kmeans" if self._n_samples >= self.MIN_SAMPLES else "fallback_hrmax"
        info: dict = {
            "fitted":     True,
            "method":     method,
            "n_samples":  self._n_samples,
            "n_zones":    self.n_zones,
            "centroids":  [round(c, 1) for c in self.centroids],
            "thresholds": [round(t, 1) for t in self.thresholds],
        }
        for i, t in enumerate(self.thresholds, start=1):
            info[f"zone{i}_max_bpm"] = round(t, 1)
        return info

    def __repr__(self) -> str:
        if not self._fitted:
            return "HRZoneEstimator(not fitted)"
        thresh = [f"{t:.0f}" for t in self.thresholds]
        return f"HRZoneEstimator(thresholds={thresh} bpm, n={self._n_samples})"
