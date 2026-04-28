"""
ダミーデータ生成スクリプト
登山ルート（約3時間）を模したGPXファイルと心拍数CSVを生成する。

ルート概要:
  - 総時間: 180分
  - 区間1 (0-60分):  緩やかな登り (斜度 5-8%)
  - 区間2 (60-120分): 急登     (斜度 15-25%)
  - 区間3 (120-180分): 緩やかな下り (斜度 -5〜-10%)
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import gpxpy
import gpxpy.gpx

# ─── 設定 ─────────────────────────────────────────────
SEED        = 42
INTERVAL_S  = 10          # GPSサンプリング間隔（秒）
TOTAL_MIN   = 180          # 総時間（分）
START_LAT   = 35.6762
START_LON   = 138.7411
START_ELEV  = 1000.0       # 開始標高 (m)
HR_BASE     = 70           # 安静時心拍数 (bpm)
np.random.seed(SEED)

# ─── タイムスタンプ列の生成 ───────────────────────────
n_points   = int(TOTAL_MIN * 60 / INTERVAL_S)
start_time = datetime(2024, 8, 1, 6, 0, 0)
timestamps = [start_time + timedelta(seconds=i * INTERVAL_S) for i in range(n_points)]


# ─── 区間ごとの斜度プロファイルを生成 ────────────────
def make_slope_profile(n):
    """3区間の斜度（%）を生成する。"""
    n1 = n // 3        # 緩登り
    n2 = n // 3        # 急登
    n3 = n - n1 - n2   # 下り

    s1 = np.random.normal(loc=6.0,   scale=1.5, size=n1)
    s2 = np.random.normal(loc=20.0,  scale=3.0, size=n2)
    s3 = np.random.normal(loc=-7.0,  scale=1.5, size=n3)
    return np.concatenate([s1, s2, s3])


slope_pct = make_slope_profile(n_points)   # 斜度（%）


# ─── 速度プロファイル（斜度に依存）────────────────────
def slope_to_speed(slope_pct):
    """斜度(%)から歩行速度(m/s)を推定する簡易モデル。"""
    speed = 1.2 - 0.03 * np.abs(slope_pct)
    speed = np.clip(speed, 0.3, 1.5)
    speed += np.random.normal(0, 0.05, size=len(slope_pct))
    return np.clip(speed, 0.2, 1.6)


speed_ms = slope_to_speed(slope_pct)      # 速度（m/s）


# ─── 標高・緯度経度の生成 ─────────────────────────────
elev = [START_ELEV]
for s in slope_pct[1:]:
    delta_h = s / 100.0 * speed_ms[len(elev) - 1] * INTERVAL_S
    elev.append(elev[-1] + delta_h)
elev = np.array(elev)

# 簡易的に北方向に進む
lat = START_LAT + np.cumsum(speed_ms * INTERVAL_S) / 111000
lon = np.full(n_points, START_LON) + np.random.normal(0, 0.00005, n_points)


# ─── 心拍数プロファイル（斜度・時間疲労に依存）─────────
def make_hr_profile(slope_pct, n):
    """斜度と累積疲労から心拍数(bpm)を生成する。"""
    fatigue = np.linspace(0, 20, n)                 # 時間とともに基礎心拍が上昇
    hr_slope_effect = np.clip(slope_pct * 2.5, -10, 60)
    hr = HR_BASE + fatigue + hr_slope_effect
    hr += np.random.normal(0, 3, size=n)
    return np.clip(hr, 50, 190).astype(int)


heart_rate = make_hr_profile(slope_pct, n_points)


# ─── GPXファイルの書き出し ────────────────────────────
gpx = gpxpy.gpx.GPX()
track = gpxpy.gpx.GPXTrack()
gpx.tracks.append(track)
segment = gpxpy.gpx.GPXTrackSegment()
track.segments.append(segment)

for i in range(n_points):
    pt = gpxpy.gpx.GPXTrackPoint(
        latitude=lat[i],
        longitude=lon[i],
        elevation=elev[i],
        time=timestamps[i]
    )
    segment.points.append(pt)

import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
gpx_path = os.path.join(PROJECT_ROOT, "data/raw/routes/sample_hike.gpx")
with open(gpx_path, "w") as f:
    f.write(gpx.to_xml())
print(f"[GPX] 生成完了: {gpx_path}  ({n_points} points)")


# ─── 心拍数CSVの書き出し ─────────────────────────────
# 心拍のサンプリングは5秒ごとにランダムにずらしてリアルさを出す
hr_timestamps = [t + timedelta(seconds=np.random.randint(-2, 3)) for t in timestamps]

df_hr = pd.DataFrame({
    "timestamp": hr_timestamps,
    "heart_rate": heart_rate,
})
hr_path = os.path.join(PROJECT_ROOT, "data/raw/heartrate/sample_hr.csv")
df_hr.to_csv(hr_path, index=False)
print(f"[HR]  生成完了: {hr_path}  ({len(df_hr)} rows)")

print("\nダミーデータ生成完了。")
