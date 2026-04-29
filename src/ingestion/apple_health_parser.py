"""
src/ingestion/apple_health_parser.py — Apple Health XML パーサー
=================================================================
iPhone の「ヘルスケア」アプリからエクスポートした export.xml を解析する。

エクスポート方法:
  iPhone → ヘルスケアApp → 右上アイコン → ヘルスケアデータを書き出す
  → export.zip を展開 → export.xml をアップロード

対応データ:
  - ワークアウト（ハイキング/ランニング/サイクリング等）
  - 心拍数 (bpm)
  - 心拍変動 HRV-SDNN (ms)
  - 安静時心拍
  - 血中酸素濃度 SpO2 (%)
  - 睡眠解析（就寝/起床/深睡眠）
  - VO2max (Apple Watch Series 3+)
  - 歩数・距離

依存ライブラリ: defusedxml>=0.7.1 (既存 requirements.txt に記載)

メモリ効率:
  iterparse を使い要素を処理後すぐ解放するため、巨大な export.xml でも
  DOM 全体をメモリに蓄積せず低メモリで動作する。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import defusedxml.ElementTree as ET


# ── Apple HealthKit ワークアウト種別 → 内部表現 ───────
_WORKOUT_MAP: dict[str, str] = {
    "HKWorkoutActivityTypeHiking":           "hiking",
    "HKWorkoutActivityTypeWalking":          "hiking",
    "HKWorkoutActivityTypeRunning":          "running",
    "HKWorkoutActivityTypeTrailRunning":     "trail_running",
    "HKWorkoutActivityTypeCycling":          "cycling",
    "HKWorkoutActivityTypeMountainBiking":   "cycling",
    "HKWorkoutActivityTypeCrossTraining":    "hiking",
}

# ── HKQuantityType 識別子 ─────────────────────────────
_HR_TYPE       = "HKQuantityTypeIdentifierHeartRate"
_HRV_TYPE      = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
_RESTING_HR    = "HKQuantityTypeIdentifierRestingHeartRate"
_SPO2_TYPE     = "HKQuantityTypeIdentifierOxygenSaturation"
_VO2MAX_TYPE   = "HKQuantityTypeIdentifierVO2Max"
_SLEEP_TYPE    = "HKCategoryTypeIdentifierSleepAnalysis"
_STEPS_TYPE    = "HKQuantityTypeIdentifierStepCount"

# 睡眠カテゴリ値
_DEEP_VALUES = {"HKCategoryValueSleepAnalysisAsleepDeep", "4"}
_REM_VALUES  = {"HKCategoryValueSleepAnalysisAsleepREM",  "5"}


@dataclass
class WorkoutSummary:
    """Apple Health から抽出した1ワークアウトのサマリー。"""
    source: str = "apple_health"
    activity_type: str = "hiking"
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_sec: float = 0.0
    distance_m: float = 0.0
    calories_kcal: Optional[float] = None
    avg_hr: Optional[float] = None
    max_hr: Optional[float] = None
    min_hr: Optional[float] = None
    elevation_gain_m: Optional[float] = None
    device_name: str = ""


@dataclass
class DailyHealthMetrics:
    """1日分のヘルスメトリクス（ML学習用）。"""
    date: date = field(default_factory=date.today)
    resting_hr: Optional[float] = None       # 安静時心拍 (bpm)
    hrv_rmssd: Optional[float] = None        # HRV SDNN (ms)
    spo2_pct: Optional[float] = None         # 血中酸素 (%)
    vo2max: Optional[float] = None           # VO2max (mL/kg/min)
    sleep_hours: Optional[float] = None      # 睡眠時間 (h)
    sleep_quality_score: Optional[float] = None  # 睡眠スコア 0-100
    steps: Optional[int] = None              # 歩数


@dataclass
class AppleHealthData:
    """parse_apple_health() の戻り値。"""
    workouts: list[WorkoutSummary]
    daily_metrics: list[DailyHealthMetrics]
    hr_timeseries: pd.DataFrame   # timestamp / hr / source_workout_id


def parse_apple_health(source) -> AppleHealthData:
    """
    Apple Health export.xml を解析する（iterparse によるストリーミング処理）。

    Parameters
    ----------
    source : bytes | BinaryIO
        export.xml のバイト列、またはファイルライクオブジェクト。
        ファイルオブジェクトを渡すとメモリへの全量読み込みを避けられる。

    Returns
    -------
    AppleHealthData
        workouts, daily_metrics, hr_timeseries を含む構造体
    """
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)

    workouts: list[WorkoutSummary] = []
    hr_rows: list[dict] = []
    resting_hr_by_day: dict[date, list[float]] = {}
    hrv_by_day:        dict[date, list[float]] = {}
    spo2_by_day:       dict[date, list[float]] = {}
    vo2max_by_day:     dict[date, list[float]] = {}
    steps_by_day:      dict[date, int] = {}
    sleep_by_day:      dict[date, dict] = {}

    try:
        for _event, elem in ET.iterparse(source, events=("end",)):
            tag = elem.tag
            if tag == "Workout":
                w = _process_workout_elem(elem)
                if w is not None:
                    workouts.append(w)
                elem.clear()
            elif tag == "Record":
                _process_record_elem(
                    elem, hr_rows,
                    resting_hr_by_day, hrv_by_day, spo2_by_day,
                    vo2max_by_day, steps_by_day, sleep_by_day,
                )
                elem.clear()
    except ET.ParseError as e:
        raise ValueError(f"Apple Health XMLの解析に失敗しました: {e}") from e

    hr_ts         = _build_hr_df(hr_rows)
    daily_metrics = _build_daily_metrics_from_buckets(
        resting_hr_by_day, hrv_by_day, spo2_by_day,
        vo2max_by_day, steps_by_day, sleep_by_day,
    )

    return AppleHealthData(
        workouts=workouts,
        daily_metrics=daily_metrics,
        hr_timeseries=hr_ts,
    )


# ── ワークアウト処理 ──────────────────────────────────

def _process_workout_elem(elem) -> Optional[WorkoutSummary]:
    """Workout 要素1件を WorkoutSummary に変換する。対象外なら None。"""
    atype    = elem.get("workoutActivityType", "")
    internal = _WORKOUT_MAP.get(atype)
    if internal is None:
        return None

    start = _parse_dt(elem.get("startDate"))
    end   = _parse_dt(elem.get("endDate"))
    dur   = float(elem.get("duration") or 0)
    dur_unit = elem.get("durationUnit", "min")
    if dur_unit == "min":
        dur *= 60

    w = WorkoutSummary(
        activity_type = internal,
        start_time    = start,
        end_time      = end,
        duration_sec  = dur,
        distance_m    = _extract_qty(elem, "totalDistance", "m"),
        calories_kcal = _extract_qty(elem, "totalEnergyBurned", "kcal"),
        device_name   = elem.get("sourceName", ""),
    )

    for stat in elem.iter("WorkoutStatistics"):
        stype = stat.get("type", "")
        if stype == _HR_TYPE:
            w.avg_hr = _safe_float(stat.get("average"))
            w.max_hr = _safe_float(stat.get("maximum"))
            w.min_hr = _safe_float(stat.get("minimum"))
        elif "FlightsClimbed" in stype or "ElevationAscended" in stype:
            w.elevation_gain_m = _safe_float(stat.get("sum"))

    return w


# ── Record 要素処理 ───────────────────────────────────

def _process_record_elem(
    elem,
    hr_rows: list,
    resting_hr_by_day: dict,
    hrv_by_day: dict,
    spo2_by_day: dict,
    vo2max_by_day: dict,
    steps_by_day: dict,
    sleep_by_day: dict,
) -> None:
    """Record 要素1件を各集計バケットに振り分ける。"""
    rtype = elem.get("type", "")
    val   = _safe_float(elem.get("value"))

    if rtype == _HR_TYPE:
        ts = _parse_dt(elem.get("startDate"))
        if ts and val and 40 <= val <= 230:
            hr_rows.append({"timestamp": ts, "hr": val})
        return

    if rtype == _SLEEP_TYPE:
        start = _parse_dt(elem.get("startDate"))
        end   = _parse_dt(elem.get("endDate"))
        if start and end:
            dur_h = (end - start).total_seconds() / 3600
            d     = start.date()
            sv    = elem.get("value", "")
            if d not in sleep_by_day:
                sleep_by_day[d] = {"total": 0.0, "deep": 0.0, "rem": 0.0}
            if "InBed" not in sv and sv != "0":
                sleep_by_day[d]["total"] += dur_h
            if sv in _DEEP_VALUES:
                sleep_by_day[d]["deep"] += dur_h
            if sv in _REM_VALUES:
                sleep_by_day[d]["rem"] += dur_h
        return

    if val is None:
        return
    ts = _parse_dt(elem.get("startDate"))
    if ts is None:
        return
    d = ts.date()

    if rtype == _RESTING_HR and 30 <= val <= 120:
        resting_hr_by_day.setdefault(d, []).append(val)
    elif rtype == _HRV_TYPE and 0 < val <= 200:
        hrv_by_day.setdefault(d, []).append(val)
    elif rtype == _SPO2_TYPE:
        v = val * 100 if val <= 1.0 else val
        if 70 <= v <= 100:
            spo2_by_day.setdefault(d, []).append(v)
    elif rtype == _VO2MAX_TYPE and 10 < val < 90:
        vo2max_by_day.setdefault(d, []).append(val)
    elif rtype == _STEPS_TYPE:
        steps_by_day[d] = steps_by_day.get(d, 0) + int(val)


# ── DataFrame 構築 ────────────────────────────────────

def _build_hr_df(hr_rows: list) -> pd.DataFrame:
    if not hr_rows:
        return pd.DataFrame(columns=["timestamp", "hr"])
    df = pd.DataFrame(hr_rows).sort_values("timestamp").reset_index(drop=True)
    return df


def _build_daily_metrics_from_buckets(
    resting_hr_by_day: dict,
    hrv_by_day: dict,
    spo2_by_day: dict,
    vo2max_by_day: dict,
    steps_by_day: dict,
    sleep_by_day: dict,
) -> list[DailyHealthMetrics]:
    """各バケットを結合して DailyHealthMetrics のリストを返す。"""
    all_dates = (
        set(resting_hr_by_day) | set(hrv_by_day) | set(spo2_by_day)
        | set(vo2max_by_day) | set(steps_by_day) | set(sleep_by_day)
    )
    result: list[DailyHealthMetrics] = []

    for d in sorted(all_dates):
        m = DailyHealthMetrics(date=d)
        if d in resting_hr_by_day:
            m.resting_hr = float(np.mean(resting_hr_by_day[d]))
        if d in hrv_by_day:
            m.hrv_rmssd = float(np.mean(hrv_by_day[d]))
        if d in spo2_by_day:
            m.spo2_pct = float(np.mean(spo2_by_day[d]))
        if d in vo2max_by_day:
            m.vo2max = float(np.mean(vo2max_by_day[d]))
        if d in steps_by_day:
            m.steps = steps_by_day[d]
        if d in sleep_by_day:
            v     = sleep_by_day[d]
            total = v["total"]
            if total >= 0.5:
                score_duration = min(total / 8.0, 1.0) * 50
                deep_ratio = v["deep"] / total if total > 0 else 0
                rem_ratio  = v["rem"]  / total if total > 0 else 0
                score_deep = min(deep_ratio / 0.15, 1.0) * 25
                score_rem  = min(rem_ratio  / 0.20, 1.0) * 25
                m.sleep_hours         = round(total, 2)
                m.sleep_quality_score = round(score_duration + score_deep + score_rem, 1)
        result.append(m)

    return result


# ── ユーティリティ ────────────────────────────────────

def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _extract_qty(elem, attr: str, target_unit: str) -> float:
    """Workout 属性から数値を取得し、単位を揃える。"""
    val = _safe_float(elem.get("total" + attr[0].upper() + attr[1:])
                      or elem.get(attr))
    unit = elem.get("total" + attr[0].upper() + attr[1:] + "Unit", "")
    if val is None:
        return 0.0
    if target_unit == "m" and unit in ("km", "kilometer"):
        val *= 1000
    elif target_unit == "m" and unit in ("mi", "mile"):
        val *= 1609.34
    return val


def _safe_float(v) -> Optional[float]:
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None
