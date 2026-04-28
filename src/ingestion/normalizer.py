"""
src/ingestion/normalizer.py — 統一アクティビティスキーマへの変換
=================================================================
Garmin FIT / Apple Health / GPX+CSV など異なるデータソースを
単一の NormalizedActivity 構造体に変換する。

この統一スキーマが ML モデルの学習データとして使用される。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from src.ingestion.fit_parser import FitSummary
from src.ingestion.apple_health_parser import WorkoutSummary


@dataclass
class NormalizedActivity:
    """
    全データソース共通の活動サマリー。
    DB の Activity テーブルと 1:1 対応する。
    """
    source: str                          # "garmin" | "apple_health" | "manual"
    activity_type: str                   # "hiking" | "running" | "cycling" | "trail_running"
    start_time: Optional[datetime]
    duration_sec: float
    distance_m: float
    elevation_gain_m: float
    elevation_loss_m: float
    avg_hr: Optional[float]
    max_hr: Optional[float]
    avg_speed_mps: Optional[float]
    calories_kcal: Optional[float]
    avg_temp_c: Optional[float]          # 気温 (Garmin のみ)
    hrv_ms: Optional[float]              # セッション中 HRV (Garmin 一部機種)
    vo2max_estimated: Optional[float]    # セッションから推定 (後処理)
    trimp: Optional[float]               # Training Impulse（学習後に計算）
    device_name: str
    raw_points: Optional[pd.DataFrame]  # 時系列データ（保存前に分離する）


def from_fit(summary: FitSummary, df: pd.DataFrame) -> NormalizedActivity:
    """FitSummary + DataFrame → NormalizedActivity。"""
    return NormalizedActivity(
        source            = "garmin",
        activity_type     = summary.activity_type,
        start_time        = summary.start_time,
        duration_sec      = summary.duration_sec,
        distance_m        = summary.distance_m,
        elevation_gain_m  = summary.elevation_gain_m,
        elevation_loss_m  = summary.elevation_loss_m,
        avg_hr            = summary.avg_hr,
        max_hr            = summary.max_hr,
        avg_speed_mps     = summary.avg_speed_mps,
        calories_kcal     = summary.calories_kcal,
        avg_temp_c        = summary.avg_temp_c,
        hrv_ms            = None,
        vo2max_estimated  = summary.vo2max_garmin,
        trimp             = None,
        device_name       = summary.device_name,
        raw_points        = df if not df.empty else None,
    )


def from_apple_workout(w: WorkoutSummary) -> NormalizedActivity:
    """WorkoutSummary → NormalizedActivity。"""
    dur = w.duration_sec
    dist = w.distance_m
    avg_speed = (dist / dur) if dur > 0 and dist > 0 else None

    return NormalizedActivity(
        source            = "apple_health",
        activity_type     = w.activity_type,
        start_time        = w.start_time,
        duration_sec      = dur,
        distance_m        = dist,
        elevation_gain_m  = w.elevation_gain_m or 0.0,
        elevation_loss_m  = 0.0,
        avg_hr            = w.avg_hr,
        max_hr            = w.max_hr,
        avg_speed_mps     = avg_speed,
        calories_kcal     = w.calories_kcal,
        avg_temp_c        = None,
        hrv_ms            = None,
        vo2max_estimated  = None,
        trimp             = None,
        device_name       = w.device_name,
        raw_points        = None,  # Apple Health はワークアウト内 GPS なし
    )


def from_gpx_csv(
    gpx_df: pd.DataFrame,
    hr_df: pd.DataFrame,
    activity_type: str = "hiking",
) -> NormalizedActivity:
    """
    既存の GPX DataFrame + HR CSV DataFrame → NormalizedActivity。
    generate_fuji_data.py が生成したデータ形式に対応。
    """
    if gpx_df.empty:
        raise ValueError("GPX データが空です")

    # 時間
    ts_col = next((c for c in ["timestamp", "time"] if c in gpx_df.columns), None)
    start  = gpx_df[ts_col].min() if ts_col else None
    end    = gpx_df[ts_col].max() if ts_col else None
    dur    = (end - start).total_seconds() if (start and end) else 0.0

    # 距離（緯度経度から Haversine 近似）
    dist = _total_distance_m(gpx_df)

    # 標高
    elev_col = next((c for c in ["elevation", "ele", "alt"] if c in gpx_df.columns), None)
    gain, loss = 0.0, 0.0
    if elev_col:
        diffs = gpx_df[elev_col].diff().dropna()
        gain  = float(diffs[diffs > 0].sum())
        loss  = float(abs(diffs[diffs < 0].sum()))

    # 速度
    avg_speed = (dist / dur) if dur > 0 else None

    # 心拍
    avg_hr, max_hr = None, None
    if not hr_df.empty:
        hr_col = next((c for c in ["heart_rate", "hr_smooth", "hr_raw"] if c in hr_df.columns), None)
        if hr_col:
            valid = hr_df[hr_col].dropna()
            valid = valid[(valid >= 40) & (valid <= 230)]
            avg_hr = float(valid.mean()) if len(valid) > 0 else None
            max_hr = float(valid.max())  if len(valid) > 0 else None

    # 時系列データの統合
    merged = _merge_gpx_hr(gpx_df, hr_df, ts_col, elev_col)

    return NormalizedActivity(
        source            = "manual",
        activity_type     = activity_type,
        start_time        = start,
        duration_sec      = dur,
        distance_m        = dist,
        elevation_gain_m  = gain,
        elevation_loss_m  = loss,
        avg_hr            = avg_hr,
        max_hr            = max_hr,
        avg_speed_mps     = avg_speed,
        calories_kcal     = None,
        avg_temp_c        = None,
        hrv_ms            = None,
        vo2max_estimated  = None,
        trimp             = None,
        device_name       = "manual_upload",
        raw_points        = merged,
    )


# ── ユーティリティ ────────────────────────────────────

def _total_distance_m(df: pd.DataFrame) -> float:
    """Haversine 近似で総距離(m)を計算する。"""
    lat_col = next((c for c in ["lat", "latitude"] if c in df.columns), None)
    lon_col = next((c for c in ["lon", "longitude"] if c in df.columns), None)
    if not (lat_col and lon_col):
        return 0.0

    lats = df[lat_col].dropna().values
    lons = df[lon_col].dropna().values
    if len(lats) < 2:
        return 0.0

    R = 6371000.0
    dlat = (lats[1:] - lats[:-1]) * (3.14159 / 180)
    dlon = (lons[1:] - lons[:-1]) * (3.14159 / 180)
    a = (dlat / 2)**2 + ((dlon / 2)**2)
    d = R * 2 * (a**0.5)  # 近似（小距離では十分）
    return float(d.sum())


def _merge_gpx_hr(
    gpx_df: pd.DataFrame,
    hr_df:  pd.DataFrame,
    ts_col: Optional[str],
    elev_col: Optional[str],
) -> pd.DataFrame:
    """GPX と HR をタイムスタンプでマージして時系列 DataFrame を作成する。"""
    if ts_col is None:
        return gpx_df.copy()

    base = gpx_df.copy()
    base["timestamp"] = pd.to_datetime(base[ts_col], utc=True, errors="coerce")

    if not hr_df.empty:
        hr_ts_col = next((c for c in ["timestamp", "time"] if c in hr_df.columns), None)
        hr_val_col = next((c for c in ["heart_rate", "hr_smooth", "hr_raw"] if c in hr_df.columns), None)
        if hr_ts_col and hr_val_col:
            hr = hr_df[[hr_ts_col, hr_val_col]].copy()
            hr.columns = ["timestamp", "hr"]
            hr["timestamp"] = pd.to_datetime(hr["timestamp"], utc=True, errors="coerce")
            base = pd.merge_asof(
                base.sort_values("timestamp"),
                hr.sort_values("timestamp"),
                on="timestamp",
                tolerance=pd.Timedelta("30s"),
                direction="nearest",
            )

    # 標準カラム名に揃える
    rename = {}
    if elev_col and elev_col != "elevation":
        rename[elev_col] = "elevation"
    lat_col = next((c for c in ["lat", "latitude"] if c in base.columns), None)
    lon_col = next((c for c in ["lon", "longitude"] if c in base.columns), None)
    if lat_col and lat_col != "lat":
        rename[lat_col] = "lat"
    if lon_col and lon_col != "lon":
        rename[lon_col] = "lon"
    if rename:
        base = base.rename(columns=rename)

    return base
