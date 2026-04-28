"""
前処理モジュール: preprocess.py
================================
処理の流れ:
  1. GPXファイルを読み込み → timestamp / lat / lon / elevation を抽出
  2. 標高差と水平距離から 斜度(slope_pct) を算出
  3. タイムスタンプ間隔と水平距離から 速度(speed_ms) を算出
  4. 心拍数CSVを読み込み
  5. GPXデータと心拍数データを timestamp で結合（前方充填補間）
  6. rolling().mean() で速度・心拍数を平滑化
  7. data/processed/merged_log.csv に出力

出力列:
  timestamp, lat, lon, elevation,
  slope_pct, speed_ms, speed_smooth,
  hr_raw, hr_smooth
"""

import os
import math
import numpy as np
import pandas as pd
import gpxpy

# ─── パス設定 ──────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GPX_PATH    = "data/raw/routes/fuji_yoshida.gpx"
HR_CSV_PATH = "data/raw/heartrate/fuji_hr.csv"
OUT_PATH     = os.path.join(PROJECT_ROOT, "data/processed/merged_log.csv")

# ─── 平滑化ウィンドウ ──────────────────────────────────
SMOOTH_WINDOW = 30   # タイムポイント数（10秒×30 = 300秒 = 5分）


# ══════════════════════════════════════════════════════
# 1. GPX の読み込みと基本量の計算
# ══════════════════════════════════════════════════════

def haversine(lat1, lon1, lat2, lon2):
    """2点間の水平距離をメートルで返す（Haversine公式）。"""
    R = 6371000.0  # 地球半径 (m)
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def load_gpx(path: str) -> pd.DataFrame:
    """
    GPXファイルを読み込み、以下の列を持つDataFrameを返す。
      timestamp, lat, lon, elevation,
      dist_m (前点からの水平距離), dt_s (経過秒), slope_pct, speed_ms
    """
    with open(path, "r") as f:
        gpx = gpxpy.parse(f)

    rows = []
    for track in gpx.tracks:
        for segment in track.segments:
            for pt in segment.points:
                rows.append({
                    "timestamp": pt.time,
                    "lat":       pt.latitude,
                    "lon":       pt.longitude,
                    "elevation": pt.elevation,
                })

    if not rows:
        raise ValueError(f"GPXファイルにトラックポイントが見つかりません: {path}")

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_localize(None)
    df = df.sort_values("timestamp").reset_index(drop=True)

    # 水平距離・経過時間の計算（先頭行は 0）
    dist_m = [0.0]
    dt_s   = [0.0]
    for i in range(1, len(df)):
        d = haversine(df.loc[i-1, "lat"], df.loc[i-1, "lon"],
                      df.loc[i,   "lat"], df.loc[i,   "lon"])
        t = (df.loc[i, "timestamp"] - df.loc[i-1, "timestamp"]).total_seconds()
        dist_m.append(d)
        dt_s.append(t if t > 0 else 1.0)

    df["dist_m"] = dist_m
    df["dt_s"]   = dt_s

    # 斜度（%） = 標高差 / 水平距離 × 100
    d_elev = df["elevation"].diff().fillna(0.0)
    df["slope_pct"] = np.where(
        df["dist_m"] > 0,
        (d_elev / df["dist_m"]) * 100.0,
        0.0
    )
    df["slope_pct"] = df["slope_pct"].clip(-60.0, 60.0)   # 物理的に非現実な値をクリップ

    # 速度（m/s）
    df["speed_ms"] = np.where(
        df["dt_s"] > 0,
        df["dist_m"] / df["dt_s"],
        0.0
    )
    df["speed_ms"] = df["speed_ms"].clip(0.0, 5.0)   # 5 m/s 以上は異常値として除去

    return df[["timestamp", "lat", "lon", "elevation", "dist_m", "dt_s", "slope_pct", "speed_ms"]]


# ══════════════════════════════════════════════════════
# 2. 心拍数CSV の読み込み
# ══════════════════════════════════════════════════════

def load_heartrate(path: str) -> pd.DataFrame:
    """
    心拍数CSVを読み込む。
    必須列: timestamp, heart_rate
    """
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    df = df.rename(columns={"heart_rate": "hr_raw"})
    df["hr_raw"] = pd.to_numeric(df["hr_raw"], errors="coerce")
    return df[["timestamp", "hr_raw"]]


# ══════════════════════════════════════════════════════
# 3. GPX と心拍数の結合
# ══════════════════════════════════════════════════════

def merge_gpx_hr(df_gpx: pd.DataFrame, df_hr: pd.DataFrame) -> pd.DataFrame:
    """
    GPXデータと心拍数データを timestamp で結合する。
    心拍はGPXより疎または密な場合があるため、
    merge_asof（最近傍タイムスタンプで結合）を使用する。
    許容タイムラグ: 30秒以内
    """
    df_hr_sorted = df_hr.sort_values("timestamp").reset_index(drop=True)
    df_gpx_sorted = df_gpx.sort_values("timestamp").reset_index(drop=True)

    df_merged = pd.merge_asof(
        df_gpx_sorted,
        df_hr_sorted,
        on="timestamp",
        tolerance=pd.Timedelta("30s"),
        direction="nearest"
    )

    # 30秒以内に対応する心拍が見つからなかった場合は補間
    missing = df_merged["hr_raw"].isna().sum()
    if missing > 0:
        df_merged["hr_raw"] = df_merged["hr_raw"].interpolate(method="linear")
        print(f"  [警告] 心拍数: {missing}点を線形補間しました。")

    return df_merged


# ══════════════════════════════════════════════════════
# 4. 移動平均による平滑化
# ══════════════════════════════════════════════════════

def smooth(df: pd.DataFrame, window: int) -> pd.DataFrame:
    """
    speed_ms と hr_raw に rolling 移動平均を適用し、
    speed_smooth / hr_smooth 列を追加する。
    """
    df["speed_smooth"] = (
        df["speed_ms"]
        .rolling(window=window, center=True, min_periods=1)
        .mean()
    )
    df["hr_smooth"] = (
        df["hr_raw"]
        .rolling(window=window, center=True, min_periods=1)
        .mean()
    )
    return df


# ══════════════════════════════════════════════════════
# 5. 品質チェック
# ══════════════════════════════════════════════════════

def quality_check(df: pd.DataFrame) -> None:
    """DataFrameの基本統計と欠損状況を表示する。"""
    print("\n─── 品質チェック ───────────────────────────────────")
    print(f"  行数           : {len(df):,}")
    print(f"  期間           : {df['timestamp'].min()} → {df['timestamp'].max()}")
    total_sec = (df["timestamp"].max() - df["timestamp"].min()).total_seconds()
    print(f"  総時間         : {total_sec/60:.1f} 分")
    print(f"  欠損値\n{df.isnull().sum().to_string()}")
    print("\n  基本統計:")
    cols = ["slope_pct", "speed_ms", "speed_smooth", "hr_raw", "hr_smooth", "elevation"]
    print(df[cols].describe().round(2).to_string())
    print("───────────────────────────────────────────────────\n")


# ══════════════════════════════════════════════════════
# メイン処理
# ══════════════════════════════════════════════════════

def run_preprocessing(
    gpx_path: str = GPX_PATH,
    hr_path:  str = HR_CSV_PATH,
    out_path: str = OUT_PATH,
    smooth_window: int = SMOOTH_WINDOW,
) -> pd.DataFrame:

    print("=== Step 1: 前処理パイプライン開始 ===\n")

    # 1. GPX 読み込み
    print(f"[1/5] GPX読み込み中: {gpx_path}")
    df_gpx = load_gpx(gpx_path)
    print(f"      → {len(df_gpx):,} points")

    # 2. 心拍数CSV 読み込み
    print(f"[2/5] 心拍数CSV読み込み中: {hr_path}")
    df_hr = load_heartrate(hr_path)
    print(f"      → {len(df_hr):,} rows")

    # 3. マージ
    print("[3/5] タイムスタンプでマージ中 (tolerance=30s) ...")
    df = merge_gpx_hr(df_gpx, df_hr)
    print(f"      → マージ後: {len(df):,} rows")

    # 4. 平滑化
    print(f"[4/5] 移動平均で平滑化中 (window={smooth_window}) ...")
    df = smooth(df, smooth_window)

    # 5. 出力列を整理して保存
    print(f"[5/5] CSVに保存中: {out_path}")
    out_cols = [
        "timestamp", "lat", "lon", "elevation",
        "dist_m", "dt_s", "slope_pct",
        "speed_ms", "speed_smooth",
        "hr_raw", "hr_smooth"
    ]
    df = df[out_cols]
    df.to_csv(out_path, index=False)
    print(f"      → 保存完了")

    # 品質チェック
    quality_check(df)

    print("=== 前処理完了 ===")
    return df


if __name__ == "__main__":
    run_preprocessing()
