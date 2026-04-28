"""
気象補正モジュール: weather.py
================================
OpenWeatherMap API（無料枠）から気温・湿度・風速・天気を取得し、
エネルギー消費への補正係数（weather_factor）を計算する。

補正の根拠:
  寒冷補正（体温維持コスト）:
    - 気温10℃以下で基礎代謝が上昇（Nielsen 1994）
    - 0℃: +20%, -10℃: +35%, -20℃: +50%

  高温多湿補正（発汗・冷却コスト）:
    - 30℃超 + 湿度70%超で +10〜20%
    - 体温調節に血流・発汗を要する

  風速補正（空気抵抗・体感温度）:
    - 登山: 向かい風で歩行コスト増加
    - ランニング: 向かい風で空気抵抗増加
    - Pugh（1971）: 10 m/s で +20% 程度

API: OpenWeatherMap Current Weather
  URL: https://api.openweathermap.org/data/2.5/weather
  無料プラン: 1000回/日, リアルタイム現在地データ
"""

from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Optional

import requests


# ── APIエンドポイント ────────────────────────────────
OWM_URL = "https://api.openweathermap.org/data/2.5/weather"


# ══════════════════════════════════════════════════════
# 気象データ
# ══════════════════════════════════════════════════════

@dataclass
class WeatherData:
    """OpenWeatherMap から取得した気象データ。"""
    location      : str
    temp_c        : float    # 気温 (℃)
    humidity_pct  : float    # 相対湿度 (%)
    wind_speed_ms : float    # 風速 (m/s)
    condition     : str      # 天気（"Clear", "Rain", "Snow" など）
    weather_factor: float    # エネルギー消費補正係数（1.0=基準）
    factor_detail : str      # 補正内訳の説明

    @property
    def is_rain(self) -> bool:
        return "Rain" in self.condition or "Drizzle" in self.condition

    @property
    def is_snow(self) -> bool:
        return "Snow" in self.condition

    @property
    def condition_ja(self) -> str:
        mapping = {
            "Clear"       : "☀️ 快晴",
            "Clouds"      : "☁️ 曇り",
            "Rain"        : "🌧️ 雨",
            "Drizzle"     : "🌦️ 小雨",
            "Snow"        : "❄️ 雪",
            "Thunderstorm": "⛈️ 雷雨",
            "Mist"        : "🌫️ 霧",
            "Fog"         : "🌫️ 濃霧",
        }
        for key, ja in mapping.items():
            if key in self.condition:
                return ja
        return f"🌤️ {self.condition}"


# ══════════════════════════════════════════════════════
# 補正係数の計算
# ══════════════════════════════════════════════════════

def calc_weather_factor(
    temp_c        : float,
    humidity_pct  : float,
    wind_speed_ms : float,
    condition     : str,
    activity_type : str = "hiking",
) -> tuple[float, str]:
    """
    気象条件からエネルギー消費補正係数を計算する。

    Returns
    -------
    factor : float   補正係数（1.0=基準、1.2=20%増加）
    detail : str     補正内訳の説明
    """
    details = []
    factor  = 1.0

    # ── 寒冷補正 ────────────────────────────────────────
    if temp_c < 10.0:
        # Nielsen (1994): 気温が10℃下がるごとに約10%増加（最大+50%）
        cold_bonus = min(0.05 * (10.0 - temp_c), 0.50)
        factor += cold_bonus
        details.append(f"寒冷+{cold_bonus*100:.0f}%（{temp_c:.0f}℃）")

    # ── 高温多湿補正 ─────────────────────────────────────
    elif temp_c > 28.0 and humidity_pct > 60.0:
        # 発汗・体温調節コスト
        heat_bonus = min(0.05 + 0.003 * (temp_c - 28.0) + 0.001 * (humidity_pct - 60.0), 0.25)
        factor += heat_bonus
        details.append(f"高温多湿+{heat_bonus*100:.0f}%（{temp_c:.0f}℃/{humidity_pct:.0f}%）")

    # ── 風速補正 ─────────────────────────────────────────
    if wind_speed_ms > 3.0:
        if activity_type == "running":
            # ランニング: Pugh (1971) モデル
            # 空気抵抗仕事量 ∝ wind_speed^2
            wind_bonus = min(0.01 * wind_speed_ms**1.5, 0.30)
        else:
            # 登山: 風による歩行不安定・体感温度低下
            wind_bonus = min(0.008 * wind_speed_ms**1.5, 0.25)
        factor += wind_bonus
        details.append(f"強風+{wind_bonus*100:.0f}%（{wind_speed_ms:.1f} m/s）")

    # ── 悪天候補正（雨・雪） ──────────────────────────────
    if "Rain" in condition or "Drizzle" in condition:
        rain_bonus = 0.08   # 濡れた地面・カッパの重さ・視界低下
        factor += rain_bonus
        details.append(f"雨+{rain_bonus*100:.0f}%")
    elif "Snow" in condition:
        snow_bonus = 0.15   # ラッセル・滑り止め歩行
        factor += snow_bonus
        details.append(f"積雪+{snow_bonus*100:.0f}%")

    factor = round(min(max(factor, 1.0), 2.0), 3)   # 1.0〜2.0でクリップ
    detail_str = "・".join(details) if details else "補正なし（快適条件）"

    return factor, detail_str


# ══════════════════════════════════════════════════════
# APIから気象データを取得
# ══════════════════════════════════════════════════════

def fetch_weather(
    api_key       : str,
    lat           : Optional[float] = None,
    lon           : Optional[float] = None,
    city_name     : Optional[str]   = None,
    activity_type : str             = "hiking",
) -> WeatherData:
    """
    OpenWeatherMap から気象データを取得して WeatherData を返す。

    Parameters
    ----------
    lat, lon   : 緯度・経度（優先）
    city_name  : 都市名（例: "Tokyo"、lat/lon がない場合に使用）
    """
    if not api_key or api_key.strip() == "":
        raise ValueError("APIキーが設定されていません")

    params: dict = {"appid": api_key, "units": "metric", "lang": "ja"}
    if lat is not None and lon is not None:
        params["lat"] = lat
        params["lon"] = lon
    elif city_name:
        params["q"] = city_name
    else:
        raise ValueError("lat/lon または city_name を指定してください")

    resp = requests.get(OWM_URL, params=params, timeout=10)

    if resp.status_code == 401:
        raise ValueError("APIキーが無効です。OpenWeatherMap で発行したキーを確認してください。")
    if resp.status_code == 404:
        raise ValueError(f"地名が見つかりません: {city_name}")
    resp.raise_for_status()

    data = resp.json()

    temp_c        = float(data["main"]["temp"])
    humidity_pct  = float(data["main"]["humidity"])
    wind_speed_ms = float(data.get("wind", {}).get("speed", 0.0))
    condition     = data["weather"][0]["main"] if data.get("weather") else "Clear"
    location      = data.get("name", city_name or f"{lat},{lon}")

    factor, detail = calc_weather_factor(
        temp_c, humidity_pct, wind_speed_ms, condition, activity_type
    )

    return WeatherData(
        location      = location,
        temp_c        = temp_c,
        humidity_pct  = humidity_pct,
        wind_speed_ms = wind_speed_ms,
        condition     = condition,
        weather_factor= factor,
        factor_detail = detail,
    )


def fetch_weather_dummy(activity_type: str = "hiking") -> WeatherData:
    """
    APIキー不要のダミーデータ（晴れ・快適条件）。
    APIキーが未設定の場合に使用する。
    """
    factor, detail = calc_weather_factor(15.0, 50.0, 1.0, "Clear", activity_type)
    return WeatherData(
        location      = "（ダミーデータ）",
        temp_c        = 15.0,
        humidity_pct  = 50.0,
        wind_speed_ms = 1.0,
        condition     = "Clear",
        weather_factor= factor,
        factor_detail = detail,
    )


# ══════════════════════════════════════════════════════
# 動作確認
# ══════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=== 気象補正係数 テスト ===\n")

    cases = [
        ("快適（15℃, 湿度50%, 無風, 晴れ）", 15.0,  50.0, 1.0,  "Clear"),
        ("寒冷（0℃, 湿度40%, 風5m, 曇り）",   0.0,  40.0, 5.0,  "Clouds"),
        ("極寒（-15℃, 湿度30%, 風10m, 雪）", -15.0, 30.0, 10.0, "Snow"),
        ("高温多湿（32℃, 湿度80%, 無風）",    32.0,  80.0, 0.5,  "Clear"),
        ("台風並み（20℃, 湿度70%, 風20m）",   20.0,  70.0, 20.0, "Rain"),
    ]

    for label, temp, hum, wind, cond in cases:
        f, detail = calc_weather_factor(temp, hum, wind, cond, "hiking")
        print(f"  {label}")
        print(f"    補正係数: {f:.3f}  ({detail})")
        print()


# ══════════════════════════════════════════════════════
# 天気予報取得（OpenWeatherMap 5日間予報）
# ══════════════════════════════════════════════════════

OWM_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"

def fetch_weather_forecast(
    api_key       : str,
    lat           : float,
    lon           : float,
    target_dt,              # datetime オブジェクト（JST）
    activity_type : str = "hiking",
) -> "WeatherData":
    """
    指定日時に最も近い天気予報を取得する。
    - 当日または過去 → Current Weather API（現在の実況）
    - 翌日以降       → 5日間予報 API（最近接スロット）
    """
    from datetime import datetime, timezone, timedelta
    JST = timezone(timedelta(hours=9))

    if not api_key or not api_key.strip():
        return fetch_weather_dummy(activity_type)

    # target_dt を JST の aware datetime に正規化
    if target_dt.tzinfo is None:
        target_dt = target_dt.replace(tzinfo=JST)

    # 常に予報APIを使用（指定時刻に最も近いスロットを選ぶ）
    # Current Weather API は「今この瞬間」しか返さないため使わない
    try:
        resp = requests.get(
            OWM_FORECAST_URL,
            params={"lat": lat, "lon": lon, "appid": api_key,
                    "units": "metric", "lang": "ja", "cnt": 40},
            timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # target_dt の UTC timestamp で最も近いスロットを選択
        target_ts = target_dt.timestamp()
        best = min(data["list"], key=lambda x: abs(x["dt"] - target_ts))

        temp_c        = best["main"]["temp"]
        humidity_pct  = best["main"]["humidity"]
        wind_speed_ms = best["wind"]["speed"]
        condition     = best["weather"][0]["main"] if best.get("weather") else "Clear"
        city_name     = data.get("city", {}).get("name", f"{lat:.3f},{lon:.3f}")
        fc_dt         = datetime.fromtimestamp(best["dt"], tz=timezone.utc).astimezone(JST)
        fc_label      = f"{fc_dt.strftime('%m/%d %H:%M')} 予報"

        wf, detail = calc_weather_factor(temp_c, humidity_pct, wind_speed_ms,
                                         condition, activity_type)
        return WeatherData(
            location      = f"{city_name} 付近（{fc_label}）",
            temp_c        = temp_c,
            humidity_pct  = humidity_pct,
            wind_speed_ms = wind_speed_ms,
            condition     = condition,
            weather_factor= wf,
            factor_detail = detail,
        )

    except Exception as e:
        dummy = fetch_weather_dummy(activity_type)
        dummy.location = f"取得失敗: {e}"
        return dummy

