"""
models/hydration_model.py — 水分補給量推定モデル
====================================================
気温・湿度・運動強度・体重・アクティビティ種別から
必要水分量と補給タイムラインを推定する。

参考文献:
  Armstrong LE (2002) "Hydration Assessment Techniques"
  Maughan RJ & Shirreffs SM (2010) "Dehydration and Rehydration in Sports"
  American College of Sports Medicine Position Stand (2007)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class HydrationInput:
    temp_c:           float  # 気温 (℃)
    humidity_pct:     float  # 湿度 (%)
    duration_min:     float  # 運動時間 (分)
    intensity_factor: float  # 運動強度係数 0.5〜1.5 (LT比率から換算)
    body_weight_kg:   float  # 体重 (kg)
    activity_type:    str    # "hiking" | "running" | "cycling"


@dataclass
class HydrationSchedulePoint:
    at_min:    float   # 運動開始からの経過時間 (分)
    amount_ml: int     # 補給量 (mL)
    note:      str     # 補給メモ


@dataclass
class HydrationResult:
    total_ml:               float   # 必要水分量合計 (mL)
    rate_ml_per_hour:       float   # 時間あたり補給量 (mL/h)
    sweat_rate_ml_per_hour: float   # 推定発汗率 (mL/h)
    electrolyte_na_mg:      float   # 推定ナトリウム必要量 (mg)
    schedule:               List[HydrationSchedulePoint] = field(default_factory=list)
    warning:                str = ""


# ── 基準発汗率テーブル (mL/h) ───────────────────────────────────────────
# 気温 15℃・湿度 50%・中等度強度の場合の基準値
_BASE_SWEAT_RATE = {
    "hiking":  500,
    "running": 900,
    "cycling": 700,
}

# 気温補正係数：10℃ごとに約20%増加 (Armstrong 2002)
def _temp_factor(temp_c: float) -> float:
    base = 15.0
    delta = (temp_c - base) / 10.0
    return max(0.5, 1.0 + delta * 0.20)

# 湿度補正係数：湿度が高いほど発汗率増加
def _humidity_factor(humidity_pct: float) -> float:
    if humidity_pct <= 40:
        return 0.90
    elif humidity_pct <= 60:
        return 1.00
    elif humidity_pct <= 80:
        return 1.12
    else:
        return 1.25

# 体重補正係数：体重 65kg 基準
def _weight_factor(weight_kg: float) -> float:
    return weight_kg / 65.0


def estimate_hydration(inp: HydrationInput) -> HydrationResult:
    """
    水分補給量を推定する。

    Parameters
    ----------
    inp : HydrationInput

    Returns
    -------
    HydrationResult
        total_ml, rate_ml_per_hour, sweat_rate_ml_per_hour,
        electrolyte_na_mg, schedule
    """
    base = _BASE_SWEAT_RATE.get(inp.activity_type, 700)

    sweat_rate = (
        base
        * _temp_factor(inp.temp_c)
        * _humidity_factor(inp.humidity_pct)
        * _weight_factor(inp.body_weight_kg)
        * inp.intensity_factor
    )

    duration_h  = inp.duration_min / 60.0
    total_sweat = sweat_rate * duration_h

    # 実際の補給量 = 発汗量の 70〜80%（完全補充は不要）
    total_intake = total_sweat * 0.75
    rate         = total_intake / duration_h if duration_h > 0 else 0.0

    # ナトリウム推定: 汗 1L あたり 500〜1000mg → 中央値 700mg/L
    electrolyte_na = (total_sweat / 1000.0) * 700.0

    # 補給スケジュール生成
    schedule = _build_schedule(inp.duration_min, total_intake, inp.activity_type)

    warning = ""
    if total_intake > 3000:
        warning = "水分必要量が多めです。電解質補給も必ず行ってください。"
    elif inp.temp_c >= 30 and inp.humidity_pct >= 70:
        warning = "高温多湿です。熱中症に注意し、こまめに補給してください。"

    return HydrationResult(
        total_ml               = round(total_intake, 0),
        rate_ml_per_hour       = round(rate, 0),
        sweat_rate_ml_per_hour = round(sweat_rate, 0),
        electrolyte_na_mg      = round(electrolyte_na, 0),
        schedule               = schedule,
        warning                = warning,
    )


def _build_schedule(duration_min: float, total_ml: float, activity_type: str) -> List[HydrationSchedulePoint]:
    """補給タイムラインを生成する。"""
    if duration_min <= 0 or total_ml <= 0:
        return []

    # 基本間隔: 20分ごと（ハイキング）、15分ごと（ランニング）
    interval = 15 if activity_type == "running" else 20

    points = []
    t = interval
    while t <= duration_min:
        # 開始・終了付近は少なめ、中盤は多め
        frac = t / duration_min
        if frac < 0.15 or frac > 0.90:
            ratio = 0.8
        else:
            ratio = 1.1

        amount = round((total_ml / (duration_min / interval)) * ratio / 50) * 50
        amount = max(100, min(400, int(amount)))

        if t == interval:
            note = "スタート直後の早期補給"
        elif t >= duration_min * 0.8:
            note = "ゴール前の補給（飲みすぎ注意）"
        elif activity_type == "running" and (duration_min - t) < 30:
            note = "ゴール間際 — 胃腸への負担に注意"
        else:
            note = f"経過 {int(t)} 分 — こまめに補給"

        points.append(HydrationSchedulePoint(at_min=t, amount_ml=amount, note=note))
        t += interval

    return points
