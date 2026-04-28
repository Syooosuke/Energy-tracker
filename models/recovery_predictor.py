"""
models/recovery_predictor.py — リカバリースコア & パフォーマンス準備度
========================================================================
Apple Health / Garmin から取得した HRV・睡眠・安静時心拍・トレーニング負荷を
組み合わせて、その日のコンディション（リカバリースコア）を 0〜100 で算出する。

スコアの構成（重み付き合算）:
  1. HRV スコア     (35%): 自律神経回復の指標
  2. 睡眠スコア     (30%): 睡眠時間・質の指標
  3. 安静時心拍スコア(20%): 疲労による心拍上昇の検出
  4. トレーニング負荷スコア(15%): TSB（ATL/CTL）から算出

パフォーマンス準備度 (readiness_score):
  recovery_score + 天候・時刻・個人ピーク時間帯 を加味した
  「今日活動するのに適しているか」の総合スコア。

データが蓄積されるほど「個人ベースライン」が精密化され、
スコアの個人適応精度が向上する。

参考:
  Kiviniemi et al. (2007) "Endurance performance can be predicted by heart rate variability"
  Plews et al. (2013) "Heart rate variability in elite triathletes"
  Kellmann et al. (2018) "Recovery and Stress Monitoring in Elite Sports"
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import numpy as np


# ══════════════════════════════════════════════════════
# データ入力構造体
# ══════════════════════════════════════════════════════

@dataclass
class DailyInput:
    """リカバリー計算への入力値（1日分）。"""
    hrv_rmssd:           Optional[float] = None   # HRV SDNN/RMSSD (ms)
    resting_hr:          Optional[float] = None   # 安静時心拍 (bpm)
    sleep_hours:         Optional[float] = None   # 睡眠時間 (h)
    sleep_quality_score: Optional[float] = None   # 睡眠品質 0-100
    spo2_pct:            Optional[float] = None   # 血中酸素 (%)
    tsb:                 Optional[float] = None   # Training Stress Balance
    atl:                 Optional[float] = None   # Acute Training Load


@dataclass
class RecoveryResult:
    """リカバリー計算の結果。"""
    recovery_score:    float          # 総合リカバリースコア 0-100
    readiness_score:   float          # パフォーマンス準備度 0-100
    status:            str            # "excellent"|"good"|"moderate"|"poor"|"rest"
    recommendation:    str            # 日本語の推奨メッセージ
    components: dict[str, float]      # 各コンポーネントのスコア
    confidence: float                 # 信頼度 0-1（利用可能データ数に依存）


# ══════════════════════════════════════════════════════
# 個人ベースライン管理
# ══════════════════════════════════════════════════════

class PersonalBaseline:
    """
    過去の DailyMetrics からユーザー個人のベースライン（平均・標準偏差）を計算する。
    データが蓄積されるほど個人差を正確に捉えられる。

    最低 7 日間のデータが推奨。3 日未満の場合は集団標準値を使用。
    """

    # 集団標準値（データ不足時のフォールバック）
    POPULATION_DEFAULTS = {
        "hrv_rmssd":    (45.0, 20.0),    # mean, std (ms)
        "resting_hr":   (65.0, 10.0),    # mean, std (bpm)
        "sleep_hours":  (7.0,  1.0),     # mean, std (h)
    }

    def __init__(self):
        self._hrv_history:        list[float] = []
        self._rhr_history:        list[float] = []
        self._sleep_history:      list[float] = []

    def update(self, hrv: Optional[float], rhr: Optional[float], sleep: Optional[float]):
        """新しいデイリーデータでベースラインを更新する（最大90日保持）。"""
        if hrv  is not None and 5 <= hrv <= 200:
            self._hrv_history.append(hrv)
        if rhr  is not None and 30 <= rhr <= 120:
            self._rhr_history.append(rhr)
        if sleep is not None and 0 < sleep <= 14:
            self._sleep_history.append(sleep)

        # 最新90日分のみ保持
        for lst in [self._hrv_history, self._rhr_history, self._sleep_history]:
            if len(lst) > 90:
                lst[:] = lst[-90:]

    def get_baseline(self, metric: str) -> tuple[float, float]:
        """(mean, std) を返す。データ不足時は集団標準値。"""
        data = {
            "hrv_rmssd":  self._hrv_history,
            "resting_hr": self._rhr_history,
            "sleep_hours":self._sleep_history,
        }.get(metric, [])

        if len(data) >= 3:
            return float(np.mean(data)), max(float(np.std(data)), 0.1)
        return self.POPULATION_DEFAULTS.get(metric, (0.0, 1.0))

    @property
    def n_days(self) -> int:
        return max(len(self._hrv_history), len(self._rhr_history), len(self._sleep_history))


# ══════════════════════════════════════════════════════
# リカバリースコア計算
# ══════════════════════════════════════════════════════

class RecoveryPredictor:
    """
    デイリーメトリクスからリカバリースコアを計算するクラス。

    使用例:
        predictor = RecoveryPredictor()

        # 過去データでベースラインを学習
        for dm in daily_metrics_list:
            predictor.update_baseline(dm.hrv_rmssd, dm.resting_hr, dm.sleep_hours)

        # 今日のスコアを計算
        result = predictor.calculate(today_input)
        print(result.recovery_score)  # 例: 82.5
        print(result.recommendation)  # 例: "コンディション良好。ハードな活動に適しています"
    """

    # スコアの重み（合計 1.0）
    WEIGHTS = {
        "hrv":    0.35,
        "sleep":  0.30,
        "rhr":    0.20,
        "load":   0.15,
    }

    def __init__(self):
        self.baseline = PersonalBaseline()

    def update_baseline(
        self,
        hrv: Optional[float],
        rhr: Optional[float],
        sleep: Optional[float],
    ):
        """過去のデータでベースラインを更新する。"""
        self.baseline.update(hrv, rhr, sleep)

    def calculate(self, inp: DailyInput) -> RecoveryResult:
        """リカバリースコアと準備度を計算する。"""
        components: dict[str, float] = {}
        available  = 0

        # ── 1. HRV スコア ─────────────────────────────
        hrv_score = _hrv_score(inp.hrv_rmssd, self.baseline)
        components["hrv"] = hrv_score
        if inp.hrv_rmssd is not None:
            available += 1

        # ── 2. 睡眠スコア ─────────────────────────────
        sleep_score = _sleep_score(inp.sleep_hours, inp.sleep_quality_score, self.baseline)
        components["sleep"] = sleep_score
        if inp.sleep_hours is not None:
            available += 1

        # ── 3. 安静時心拍スコア ───────────────────────
        rhr_score = _rhr_score(inp.resting_hr, self.baseline)
        components["rhr"] = rhr_score
        if inp.resting_hr is not None:
            available += 1

        # ── 4. トレーニング負荷スコア ─────────────────
        load_score = _load_score(inp.tsb, inp.atl)
        components["load"] = load_score
        if inp.tsb is not None:
            available += 1

        # ── SpO2 補正（高山病・低酸素リスク）─────────
        spo2_penalty = _spo2_penalty(inp.spo2_pct)

        # ── 重み付き合算 ──────────────────────────────
        total = (
            hrv_score   * self.WEIGHTS["hrv"]   +
            sleep_score * self.WEIGHTS["sleep"] +
            rhr_score   * self.WEIGHTS["rhr"]   +
            load_score  * self.WEIGHTS["load"]
        ) * (1.0 - spo2_penalty)

        recovery = float(np.clip(total, 0.0, 100.0))

        # ── 準備度スコア（recovery + 体内時計補正）────
        readiness = _calc_readiness(recovery, inp.atl)

        # ── ステータス & 推奨 ─────────────────────────
        status = _score_to_status(recovery)
        recommendation = _make_recommendation(status, inp, spo2_penalty)

        confidence = min(available / 4.0, 1.0) * min(self.baseline.n_days / 14.0, 1.0)
        confidence = max(confidence, 0.1)

        return RecoveryResult(
            recovery_score  = round(recovery, 1),
            readiness_score = round(readiness, 1),
            status          = status,
            recommendation  = recommendation,
            components      = {k: round(v, 1) for k, v in components.items()},
            confidence      = round(confidence, 2),
        )


# ── コンポーネントスコア関数 ──────────────────────────

def _hrv_score(hrv: Optional[float], baseline: PersonalBaseline) -> float:
    """HRV が個人ベースラインより高いほど高スコア。"""
    if hrv is None:
        return 50.0
    mean, std = baseline.get_baseline("hrv_rmssd")
    # Z スコアを -3〜+3 にクリップしてスコアに変換
    z = (hrv - mean) / max(std, 1.0)
    z = np.clip(z, -3.0, 3.0)
    # Z=0 で 60点, +3σ で 100点, -3σ で 20点
    return float(60.0 + z * 13.3)


def _sleep_score(
    hours: Optional[float],
    quality: Optional[float],
    baseline: PersonalBaseline,
) -> float:
    if hours is None and quality is None:
        return 50.0
    score = 50.0
    if hours is not None:
        mean, std = baseline.get_baseline("sleep_hours")
        z = np.clip((hours - mean) / max(std, 0.3), -3.0, 2.0)
        # 理想: 7〜9h、それ以下/以上はペナルティ
        ideal_penalty = max(0, 8.0 - hours) * 8 + max(0, hours - 9.0) * 4
        score = 65.0 + z * 10.0 - ideal_penalty
    if quality is not None:
        score = score * 0.5 + quality * 0.5
    return float(np.clip(score, 0.0, 100.0))


def _rhr_score(rhr: Optional[float], baseline: PersonalBaseline) -> float:
    """安静時心拍が低いほど（ベースラインより低いほど）高スコア。"""
    if rhr is None:
        return 50.0
    mean, std = baseline.get_baseline("resting_hr")
    z = np.clip((mean - rhr) / max(std, 1.0), -3.0, 3.0)  # 低いほど良いので符号反転
    return float(np.clip(60.0 + z * 13.3, 0.0, 100.0))


def _load_score(tsb: Optional[float], atl: Optional[float]) -> float:
    """TSB（コンディション指標）からスコアを計算する。"""
    if tsb is None:
        return 60.0  # データなし時は中立
    # TSB -30〜+20 を 0〜100 にマッピング
    # TSB=+15 が最適（80点）、TSB=0 が普通（60点）、TSB=-30 が最低（10点）
    if tsb >= 15:
        score = 80.0 + min((tsb - 15) / 10.0, 1.0) * 20.0  # フレッシュ
    elif tsb >= 0:
        score = 60.0 + (tsb / 15.0) * 20.0
    elif tsb >= -30:
        score = max(10.0, 60.0 + tsb * 1.67)  # 疲労
    else:
        score = 10.0
    return float(np.clip(score, 0.0, 100.0))


def _spo2_penalty(spo2: Optional[float]) -> float:
    """SpO2 が低いほどリカバリーに大きなペナルティを与える。"""
    if spo2 is None:
        return 0.0
    if spo2 >= 95.0:
        return 0.0
    elif spo2 >= 90.0:
        return 0.1 * (95.0 - spo2) / 5.0   # 最大 10%
    elif spo2 >= 85.0:
        return 0.1 + 0.2 * (90.0 - spo2) / 5.0  # 10〜30%
    else:
        return 0.3  # 30%（医療的注意レベル）


def _calc_readiness(recovery: float, atl: Optional[float]) -> float:
    """
    リカバリースコアとトレーニング負荷から準備度を計算する。
    高い疲労 (ATL) があっても体が回復していれば準備度は高い。
    """
    readiness = recovery
    if atl is not None:
        # ATL が高い（最近ハードなトレーニング）状態でも
        # リカバリーが高ければ「鍛えられている」状態なのでボーナス
        if recovery >= 70 and atl >= 30:
            readiness = min(100.0, recovery + 5.0)
    return float(np.clip(readiness, 0.0, 100.0))


# ── ステータス & 推奨メッセージ ───────────────────────

def _score_to_status(score: float) -> str:
    if score >= 80:
        return "excellent"
    elif score >= 65:
        return "good"
    elif score >= 50:
        return "moderate"
    elif score >= 35:
        return "poor"
    else:
        return "rest"


def _make_recommendation(
    status: str,
    inp: DailyInput,
    spo2_penalty: float,
) -> str:
    base = {
        "excellent": "コンディション絶好調。ハードなトレーニングや重要な山行に最適な日です。",
        "good":      "コンディション良好。通常のトレーニングを予定通り行えます。",
        "moderate":  "やや疲労が残っています。強度を抑えた活動か、軽いリカバリー運動を推奨します。",
        "poor":      "疲労が蓄積しています。積極的休養（軽いストレッチ・散歩）を優先してください。",
        "rest":      "十分な休息が必要です。今日は運動を控えて回復に専念することを強く推奨します。",
    }.get(status, "")

    extras = []
    if inp.sleep_hours is not None and inp.sleep_hours < 6.0:
        extras.append("睡眠不足が検出されています（6時間未満）。")
    if inp.spo2_pct is not None and inp.spo2_pct < 92.0:
        extras.append(f"血中酸素が低め（{inp.spo2_pct:.0f}%）。高山活動の場合は高山病に注意。")
    if inp.tsb is not None and inp.tsb < -20:
        extras.append("トレーニング負荷が高い状態です。オーバートレーニングに注意してください。")

    if extras:
        return base + " " + " ".join(extras)
    return base
