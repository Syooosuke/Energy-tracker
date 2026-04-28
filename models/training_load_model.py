"""
models/training_load_model.py — トレーニング負荷モデル (ATL/CTL/TSB)
=====================================================================
スポーツ科学の Performance Management Chart (PMC) を実装する。
蓄積したアクティビティの TRIMP から疲労・フィットネス・コンディションを定量化する。

指標の説明:
  TRIMP  (Training Impulse)
    = 活動時間(分) × avg_HR_ratio × exp係数
    個々のアクティビティの「負荷ポイント」

  ATL    (Acute Training Load) = 7日指数移動平均の TRIMP
    → 短期疲労の指標。高いほど直近が「ハード」

  CTL    (Chronic Training Load) = 42日指数移動平均の TRIMP
    → 長期フィットネスの指標。高いほど「鍛えられている」

  TSB    (Training Stress Balance) = CTL − ATL
    → コンディション指標。
      TSB > +10  : フレッシュ（試合/重要山行に最適）
      TSB  0〜10 : 適度な疲労（通常トレーニング）
      TSB < -10  : 疲労蓄積（オーバートレーニング注意）
      TSB < -30  : 高リスク（怪我・体調不良の危険域）

参考:
  Banister et al. (1975) "A systems model of training for athletic performance"
  Coggan (2003) "Training and racing with a power meter"
  Skiba (2012) "Modeling the expenditure and reconstitution of work capacity"
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd


# ══════════════════════════════════════════════════════
# TRIMP 計算
# ══════════════════════════════════════════════════════

def calc_trimp(
    duration_min: float,
    avg_hr: float,
    hr_rest: float = 60.0,
    hr_max: float  = 185.0,
    gender: str    = "male",
) -> float:
    """
    Banister TRIMP を計算する。

    Parameters
    ----------
    duration_min : 活動時間 (分)
    avg_hr       : 平均心拍数 (bpm)
    hr_rest      : 安静時心拍数 (bpm)
    hr_max       : 最大心拍数 (bpm)
    gender       : "male" | "female" (係数が異なる)

    Returns
    -------
    trimp : float
    """
    hr_ratio = (avg_hr - hr_rest) / max(hr_max - hr_rest, 1.0)
    hr_ratio = np.clip(hr_ratio, 0.0, 1.0)

    # 性別係数: 男性 1.92、女性 1.67 (Banister 原論文)
    k = 1.92 if gender == "male" else 1.67

    trimp = duration_min * hr_ratio * (0.64 * np.exp(k * hr_ratio))
    return float(trimp)


def calc_trimp_from_hr_series(
    hr_series: np.ndarray,
    elapsed_sec: np.ndarray,
    hr_rest: float = 60.0,
    hr_max: float  = 185.0,
    gender: str    = "male",
) -> float:
    """
    時系列心拍データから TRIMP を積分で計算する（より精密）。

    Parameters
    ----------
    hr_series   : 心拍数の時系列 (bpm)
    elapsed_sec : 対応する経過秒数
    """
    if len(hr_series) < 2:
        return 0.0

    k = 1.92 if gender == "male" else 1.67
    dt = np.diff(elapsed_sec) / 60.0  # 分単位

    hr_mid    = (hr_series[:-1] + hr_series[1:]) / 2
    hr_ratio  = np.clip((hr_mid - hr_rest) / max(hr_max - hr_rest, 1.0), 0.0, 1.0)
    trimp_per = dt * hr_ratio * (0.64 * np.exp(k * hr_ratio))

    return float(np.nansum(trimp_per))


# ══════════════════════════════════════════════════════
# ATL / CTL / TSB 計算
# ══════════════════════════════════════════════════════

@dataclass
class DailyLoad:
    date: date
    trimp: float      # その日の合計 TRIMP
    atl: float        # Acute Training Load
    ctl: float        # Chronic Training Load
    tsb: float        # Training Stress Balance
    status: str       # "fresh"|"optimal"|"fatigued"|"overreached"


def build_pmc(
    activity_records: list[tuple[date, float]],  # (date, trimp)
    atl_days: int = 7,
    ctl_days: int = 42,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> list[DailyLoad]:
    """
    Performance Management Chart (PMC) を計算する。

    Parameters
    ----------
    activity_records : (日付, TRIMP) のリスト（複数あれば同日は合算）
    atl_days         : ATL の半減期（日）、デフォルト7日
    ctl_days         : CTL の半減期（日）、デフォルト42日
    start_date       : 集計開始日（省略時は最初のアクティビティ日）
    end_date         : 集計終了日（省略時は今日）

    Returns
    -------
    list[DailyLoad]  日ごとの ATL/CTL/TSB
    """
    if not activity_records:
        return []

    # 日次 TRIMP 集計
    trimp_by_date: dict[date, float] = {}
    for d, t in activity_records:
        trimp_by_date[d] = trimp_by_date.get(d, 0.0) + t

    if start_date is None:
        start_date = min(trimp_by_date.keys())
    if end_date is None:
        end_date = date.today()

    # 指数平滑の減衰係数
    k_atl = 1.0 / atl_days
    k_ctl = 1.0 / ctl_days

    atl = 0.0
    ctl = 0.0
    result: list[DailyLoad] = []

    current = start_date
    while current <= end_date:
        t = trimp_by_date.get(current, 0.0)
        atl = atl + k_atl * (t - atl)
        ctl = ctl + k_ctl * (t - ctl)
        tsb = ctl - atl
        result.append(DailyLoad(
            date   = current,
            trimp  = t,
            atl    = round(atl, 2),
            ctl    = round(ctl, 2),
            tsb    = round(tsb, 2),
            status = _tsb_status(tsb),
        ))
        current += timedelta(days=1)

    return result


def get_current_load(
    activity_records: list[tuple[date, float]],
    atl_days: int = 7,
    ctl_days: int = 42,
) -> Optional[DailyLoad]:
    """最新（今日）の ATL/CTL/TSB を返す。"""
    pmc = build_pmc(activity_records, atl_days=atl_days, ctl_days=ctl_days)
    return pmc[-1] if pmc else None


def _tsb_status(tsb: float) -> str:
    if tsb >= 10:
        return "fresh"         # フレッシュ（重要活動に最適）
    elif tsb >= 0:
        return "optimal"       # 適度な負荷（通常トレーニング）
    elif tsb >= -10:
        return "fatigued"      # 軽度疲労（注意して続行可）
    elif tsb >= -30:
        return "heavy"         # 高負荷（回復優先）
    else:
        return "overreached"   # 過度疲労（危険域）


# ══════════════════════════════════════════════════════
# VO2max 推定
# ══════════════════════════════════════════════════════

def estimate_vo2max(
    speed_mps: float,
    avg_hr: float,
    hr_max: float,
    slope_pct: float = 0.0,
    altitude_m: float = 0.0,
) -> Optional[float]:
    """
    平地走行ペース・心拍数・HR max から VO2max を推定する。

    Daniels-Gilbert 方程式を応用した近似式:
      VO2 = -4.60 + 0.182258 * v + 0.000104 * v^2 (平地・mL/min/kg 換算)

    高度補正: 3000m で約 10% 低下

    Parameters
    ----------
    speed_mps  : 平均速度 (m/s)
    avg_hr     : 活動中の平均心拍数 (bpm)
    hr_max     : 最大心拍数 (bpm)
    slope_pct  : 平均斜度 (%)（登山は水平換算速度に補正）
    altitude_m : 平均標高 (m)（高度補正用）

    Returns
    -------
    vo2max : mL/kg/min, または None（推定不可の場合）
    """
    if avg_hr <= 0 or hr_max <= avg_hr or speed_mps <= 0:
        return None

    # 斜度補正: 登りは水平換算速度（Tobler 近似）
    if abs(slope_pct) > 2.0:
        correction = 1.0 / (1.0 + 0.015 * abs(slope_pct))
        speed_flat = speed_mps * correction
    else:
        speed_flat = speed_mps

    # m/min に変換
    v_min = speed_flat * 60.0

    # VO2 at current intensity (Daniels)
    vo2_current = -4.60 + 0.182258 * v_min + 0.000104 * (v_min ** 2)
    if vo2_current <= 0:
        return None

    # HR fraction → VO2 fraction（線形近似、低強度では誤差大）
    hr_fraction = avg_hr / hr_max
    if hr_fraction < 0.5:
        return None  # 低強度では推定信頼性が低い

    vo2max = vo2_current / hr_fraction

    # 高度補正（3000m 以上で空気密度低下）
    if altitude_m > 1500:
        altitude_factor = 1.0 - 0.032 * max(altitude_m - 1500, 0) / 1000.0
        vo2max /= max(altitude_factor, 0.7)

    vo2max = np.clip(vo2max, 20.0, 85.0)
    return round(float(vo2max), 1)
