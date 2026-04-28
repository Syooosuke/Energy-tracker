"""
models/health_context_features.py — Apple Health コンテキスト特徴量生成
======================================================================
アクティビティ実施日の健康状態（HRV・安静時心拍・睡眠）を、
PersonalModelTrainer が利用できる標準化コンテキスト特徴量に変換する。

なぜ必要か:
  同じ斜度・速度でも体の状態によって心拍応答は大きく異なる。
  疲労時は同ペースでも HR が高く、睡眠不足時は HR ドリフトが早い。
  Apple Health のデイリーデータをコンテキストとして加えることで
  「当日の体の状態」を考慮した予測をモデルが学習できる。

提供する特徴量:
  resting_hr_ratio : 当日安静時HR / 個人90日平均 (1.0 が標準、>1.0 = 疲労)
  hrv_zscore       : 当日HRV の個人内 Z スコア (正 = 好調 / 負 = 疲労)
  sleep_score_norm : 前日の睡眠スコア / 100 (0–1)

TRIMP 個人化パラメータ:
  personal_hr_rest : 90日平均安静時心拍 (bpm)
  personal_hr_max  : 活動中の最大心拍履歴 or 経験則推定 (bpm)

参考:
  Plews et al. (2013) "Heart rate variability in elite triathletes"
  Kellmann et al. (2018) "Recovery and Stress Monitoring in Elite Sports"
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import numpy as np


@dataclass
class DayContext:
    """1日分のコンテキスト特徴量（PersonalModelTrainer に渡す）。"""
    resting_hr_ratio: float = 1.0   # 当日 RHR / 個人平均 RHR
    hrv_zscore:       float = 0.0   # (当日 HRV − 平均) / 標準偏差
    sleep_score_norm: float = 0.5   # 前日睡眠スコア / 100
    data_available:   int   = 0     # 利用できたデータ項目数 (0–3)
    # v3 気象コンテキスト（アクティビティ時の実測値）
    temp_c:           float = 20.0  # 気温 (℃) ※ 利用可能な場合は実測値
    humidity_pct:     float = 60.0  # 湿度 (%)


class HealthContextBuilder:
    """
    DailyMetrics の履歴から個人ベースラインを構築し、
    任意の日付のコンテキスト特徴量を生成するクラス。

    使用例:
        builder = HealthContextBuilder(daily_metrics_rows)
        ctx = builder.get_context_for_date(activity_date)
        hr_rest, hr_max = builder.personal_hr_params(observed_max_hr=195.0)
    """

    # 個人データ不足時のフォールバック（集団標準値）
    _DEFAULTS = {
        "resting_hr": (60.0, 8.0),   # (mean, std)
        "hrv_rmssd":  (45.0, 18.0),
    }
    _HRMAX_MIN  = 150.0
    _HRMAX_MAX  = 215.0
    _HRREST_MIN =  35.0
    _HRREST_MAX =  90.0

    def __init__(self, daily_metrics: list):
        """
        Parameters
        ----------
        daily_metrics : DailyMetrics ORM オブジェクトのリスト、または
                        date / resting_hr / hrv_rmssd / sleep_quality_score 属性を持つオブジェクト。
        """
        self._by_date: dict[date, object] = {}
        rhr_vals: list[float] = []
        hrv_vals: list[float] = []

        for dm in sorted(daily_metrics, key=lambda x: x.date):
            self._by_date[dm.date] = dm
            if dm.resting_hr is not None:
                rhr_vals.append(float(dm.resting_hr))
            if dm.hrv_rmssd is not None:
                hrv_vals.append(float(dm.hrv_rmssd))

        # 個人ベースライン（最新90日）
        rhr_window = rhr_vals[-90:]
        hrv_window = hrv_vals[-90:]

        if len(rhr_window) >= 3:
            self._rhr_mean = float(np.mean(rhr_window))
            self._rhr_std  = max(float(np.std(rhr_window)), 0.5)
        else:
            self._rhr_mean, self._rhr_std = self._DEFAULTS["resting_hr"]

        if len(hrv_window) >= 3:
            self._hrv_mean = float(np.mean(hrv_window))
            self._hrv_std  = max(float(np.std(hrv_window)), 1.0)
        else:
            self._hrv_mean, self._hrv_std = self._DEFAULTS["hrv_rmssd"]

        self._hr_max_observed: Optional[float] = None

    # ── コンテキスト特徴量生成 ────────────────────────────

    def get_context_for_date(self, target_date: date) -> DayContext:
        """指定日のコンテキスト特徴量を返す。"""
        ctx = DayContext()
        available = 0

        # 当日のメトリクスを探す（前後2日以内の最近接データを使用）
        dm = self._by_date.get(target_date)
        if dm is None:
            for delta in (1, -1, 2, -2):
                dm = self._by_date.get(target_date + timedelta(days=delta))
                if dm is not None:
                    break

        if dm is not None:
            # 安静時心拍比: >1.0 は疲労兆候
            if dm.resting_hr is not None:
                ctx.resting_hr_ratio = float(dm.resting_hr) / max(self._rhr_mean, 1.0)
                ctx.resting_hr_ratio = float(np.clip(ctx.resting_hr_ratio, 0.5, 2.0))
                available += 1

            # HRV Z スコア: 負 = 疲労、正 = 好調
            if dm.hrv_rmssd is not None:
                ctx.hrv_zscore = (float(dm.hrv_rmssd) - self._hrv_mean) / self._hrv_std
                ctx.hrv_zscore = float(np.clip(ctx.hrv_zscore, -3.0, 3.0))
                available += 1

        # 睡眠スコア: 前日の睡眠がその日のパフォーマンスに影響
        prev_dm = self._by_date.get(target_date - timedelta(days=1))
        if prev_dm is not None and prev_dm.sleep_quality_score is not None:
            ctx.sleep_score_norm = float(prev_dm.sleep_quality_score) / 100.0
            available += 1
        elif dm is not None and getattr(dm, "sleep_quality_score", None) is not None:
            ctx.sleep_score_norm = float(dm.sleep_quality_score) / 100.0
            available += 1

        ctx.data_available = available
        return ctx

    # ── TRIMP 個人化パラメータ ───────────────────────────

    def personal_hr_params(
        self,
        observed_max_hr: Optional[float] = None,
    ) -> tuple[float, float]:
        """
        (hr_rest, hr_max) を返す。TRIMP 計算の個人化に使用する。

        Parameters
        ----------
        observed_max_hr : 全アクティビティ中の最大心拍の最大値（省略可）
        """
        hr_rest = float(np.clip(self._rhr_mean, self._HRREST_MIN, self._HRREST_MAX))

        if observed_max_hr is not None and self._HRMAX_MIN <= observed_max_hr <= self._HRMAX_MAX:
            hr_max = float(observed_max_hr)
        elif self._hr_max_observed is not None:
            hr_max = float(self._hr_max_observed)
        else:
            # 個人データ不足時: Tanaka (2001) 変形近似
            #   HRmax ≈ 211 - 0.64 × RHR（加齢・安静時HR 相関を利用）
            hr_max = 211.0 - 0.64 * hr_rest

        hr_max = float(np.clip(hr_max, self._HRMAX_MIN, self._HRMAX_MAX))
        return hr_rest, hr_max

    def update_observed_hr_max(self, hr_max: float):
        """Activity.max_hr の履歴最大値を記録して hr_max 推定精度を向上させる。"""
        if self._HRMAX_MIN <= hr_max <= self._HRMAX_MAX:
            if self._hr_max_observed is None or hr_max > self._hr_max_observed:
                self._hr_max_observed = hr_max

    @property
    def n_days(self) -> int:
        """ベースライン構築に使用した日数。"""
        return len(self._by_date)

    @property
    def personal_rhr_mean(self) -> float:
        """個人平均安静時心拍 (bpm)。"""
        return self._rhr_mean

    @property
    def personal_hrv_mean(self) -> float:
        """個人平均 HRV (ms)。"""
        return self._hrv_mean
