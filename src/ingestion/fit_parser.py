"""
src/ingestion/fit_parser.py — Garmin FIT ファイルパーサー
==========================================================
Garmin デバイス（Forerunner, Fenix, Edge 等）が生成する .fit ファイルを
解析し、正規化された DataFrame と活動サマリーを返す。

対応データ:
  - GPS軌跡（lat/lon/標高）
  - 心拍数 (bpm)
  - 速度 (m/s)
  - 気温 (℃)
  - 気圧高度 (m)
  - ケイデンス (rpm) ※ランニング/サイクリング
  - パワー (W) ※サイクリング/ランニングパワーメーター

依存ライブラリ: fitparse>=1.2.0
  pip install fitparse
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

try:
    from fitparse import FitFile
    _FITPARSE_AVAILABLE = True
except ImportError:
    _FITPARSE_AVAILABLE = False


# ══════════════════════════════════════════════════════
# 活動種別マッピング (FIT sport → 内部表現)
# ══════════════════════════════════════════════════════

_SPORT_MAP: dict[str, str] = {
    "hiking":       "hiking",
    "walking":      "hiking",
    "trail_running":"trail_running",
    "running":      "running",
    "cycling":      "cycling",
    "mountain_biking": "cycling",
    "generic":      "hiking",
}


@dataclass
class FitSummary:
    """FIT ファイルから抽出した活動サマリー。"""
    source: str = "garmin"
    activity_type: str = "hiking"
    start_time: Optional[datetime] = None
    duration_sec: float = 0.0
    distance_m: float = 0.0
    elevation_gain_m: float = 0.0
    elevation_loss_m: float = 0.0
    avg_hr: Optional[float] = None
    max_hr: Optional[float] = None
    avg_speed_mps: Optional[float] = None
    avg_cadence: Optional[float] = None
    avg_power_w: Optional[float] = None
    avg_temp_c: Optional[float] = None
    calories_kcal: Optional[float] = None
    device_name: str = ""
    # Garmin 独自指標
    training_load: Optional[float] = None
    vo2max_garmin: Optional[float] = None


def parse_fit(data: bytes) -> tuple[FitSummary, pd.DataFrame]:
    """
    FIT バイト列を解析してサマリーと時系列 DataFrame を返す。

    Parameters
    ----------
    data : bytes
        .fit ファイルのバイト列（ファイル読み込み or HTTP upload）

    Returns
    -------
    summary : FitSummary
        活動全体のサマリー情報
    df : pd.DataFrame
        1秒〜10秒間隔の時系列データ。カラム:
          timestamp, elapsed_sec, lat, lon, elevation,
          hr, speed_mps, distance_cum_m, slope_pct,
          cadence, power_w, temp_c
    """
    if not _FITPARSE_AVAILABLE:
        raise ImportError(
            "fitparse がインストールされていません。"
            " `pip install fitparse` を実行してください。"
        )

    fit = FitFile(io.BytesIO(data))
    summary = FitSummary()
    rows: list[dict] = []

    # ── session メッセージ（活動サマリー）────────────────
    for msg in fit.get_messages("session"):
        fields = {d.name: d.value for d in msg}
        sport = str(fields.get("sport", "generic")).lower()
        summary.activity_type = _SPORT_MAP.get(sport, "hiking")
        summary.start_time    = _to_utc(fields.get("start_time"))
        summary.duration_sec  = float(fields.get("total_elapsed_time") or 0)
        summary.distance_m    = float(fields.get("total_distance") or 0)
        summary.avg_hr        = _safe_float(fields.get("avg_heart_rate"))
        summary.max_hr        = _safe_float(fields.get("max_heart_rate"))
        summary.avg_speed_mps = _safe_float(fields.get("avg_speed"))
        summary.avg_cadence   = _safe_float(fields.get("avg_cadence"))
        summary.avg_power_w   = _safe_float(fields.get("avg_power"))
        summary.avg_temp_c    = _safe_float(fields.get("avg_temperature"))
        summary.calories_kcal = _safe_float(fields.get("total_calories"))

    # ── device_info メッセージ ─────────────────────────
    for msg in fit.get_messages("device_info"):
        fields = {d.name: d.value for d in msg}
        if fields.get("manufacturer") and fields.get("product_name"):
            summary.device_name = f"{fields['manufacturer']} {fields['product_name']}"
            break

    # ── record メッセージ（時系列）───────────────────────
    for msg in fit.get_messages("record"):
        fields = {d.name: d.value for d in msg}
        ts  = _to_utc(fields.get("timestamp"))
        lat = _semicircles_to_deg(fields.get("position_lat"))
        lon = _semicircles_to_deg(fields.get("position_long"))
        row = {
            "timestamp":     ts,
            "lat":           lat,
            "lon":           lon,
            "elevation":     _safe_float(fields.get("altitude") or fields.get("enhanced_altitude")),
            "hr":            _safe_float(fields.get("heart_rate")),
            "speed_mps":     _safe_float(fields.get("speed") or fields.get("enhanced_speed")),
            "distance_cum_m":_safe_float(fields.get("distance")),
            "cadence":       _safe_float(fields.get("cadence")),
            "power_w":       _safe_float(fields.get("power")),
            "temp_c":        _safe_float(fields.get("temperature")),
        }
        rows.append(row)

    if not rows:
        return summary, pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.sort_values("timestamp").reset_index(drop=True)

    # elapsed_sec
    if summary.start_time and df["timestamp"].notna().any():
        t0 = df["timestamp"].dropna().iloc[0]
        df["elapsed_sec"] = (df["timestamp"] - t0).dt.total_seconds()
    else:
        df["elapsed_sec"] = np.arange(len(df)) * 10.0

    # 斜度計算（標高差 / 水平距離）
    df["slope_pct"] = _calc_slope(df["elevation"], df["distance_cum_m"])

    # 獲得/喪失標高
    elev = df["elevation"].dropna().values
    if len(elev) > 1:
        diffs = np.diff(elev)
        summary.elevation_gain_m = float(diffs[diffs > 0].sum())
        summary.elevation_loss_m = float(abs(diffs[diffs < 0].sum()))

    return summary, df


# ── ユーティリティ ─────────────────────────────────────

def _safe_float(v) -> Optional[float]:
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _to_utc(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v
    return None


def _semicircles_to_deg(v) -> Optional[float]:
    """Garmin の緯度経度は semicircles 単位（÷ 2^31 × 180）。"""
    if v is None:
        return None
    try:
        return float(v) * (180.0 / 2**31)
    except (TypeError, ValueError):
        return None


def _calc_slope(elev: pd.Series, dist: pd.Series) -> pd.Series:
    """標高差と累積距離から斜度(%)を計算する。"""
    elev_diff = elev.diff().fillna(0)
    dist_diff = dist.diff().fillna(1).replace(0, 1)
    slope = (elev_diff / dist_diff) * 100
    return slope.clip(-60, 60)
