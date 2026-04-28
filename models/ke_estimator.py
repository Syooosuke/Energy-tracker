"""
Ke推定モジュール: ke_estimator.py  (v2)
========================================
活動種別（hiking / running）によってエネルギー消費式を切り替える。

登山 (hiking)  : Pandolf式（1977）
ランニング (running): Minetti式（2002, J Appl Physiol）
"""

from __future__ import annotations
import numpy as np
from dataclasses import dataclass

WATT_TO_KCAL_HR  = 3600.0 / 4184.0
ACTIVITY_HIKING  = "hiking"
ACTIVITY_RUNNING = "running"
ACTIVITY_CYCLING = "cycling"


# ══════════════════════════════════════════════════
# ユーザープロフィール
# ══════════════════════════════════════════════════

@dataclass
class UserProfile:
    body_weight_kg    : float = 65.0
    load_weight_kg    : float = 10.0
    hr_max            : float = 180.0
    terrain_factor    : float = 1.2
    e_initial_kcal    : float = 1800.0
    activity_type     : str   = ACTIVITY_HIKING
    base_run_speed_ms : float = 3.0    # ランニング基準速度(m/s)
    weather_factor    : float = 1.0    # 気象補正係数（1.0=基準, 1.2=20%増）
    altitude_m        : float = 0.0    # 平均標高 (m)。高度補正に使用
    # ── 自転車専用パラメータ ──────────────────────────────────
    bike_cda          : float = 0.40   # 空気抵抗係数×前面積 (m²)。ロード:0.32, MTB:0.50
    bike_crr          : float = 0.005  # 転がり抵抗係数。舗装路:0.004, 砂利:0.010
    base_cycling_speed_ms: float = 6.94  # 基準速度(m/s)。6.94=25km/h

    @property
    def lt_hr(self): return self.hr_max * 0.85

    def hr_zone(self, hr):
        r = hr / self.hr_max
        return 1 if r < 0.60 else (2 if r < 0.80 else 3)

    def zone_correction(self, hr):
        return {1: 0.90, 2: 1.00, 3: 1.15}[self.hr_zone(hr)]


# ══════════════════════════════════════════════════
# 登山: Pandolf式
# ══════════════════════════════════════════════════

def pandolf_watt(speed_ms, slope_pct, profile):
    """
    Pandolf（1977）式。荷物を背負った歩行専用。
    W_dot = 1.5M + 2(M+L)(L/M)^2 + eta(M+L)(1.5V^2 + 0.35VG)

    有効範囲: 勾配 0〜25%（論文記載）。
    25%超の急斜面は25%でキャップして過大推定を防ぐ。
    """
    M, L = profile.body_weight_kg, max(profile.load_weight_kg, 0.01)
    V, eta = max(speed_ms, 0.01), profile.terrain_factor
    G = np.clip(slope_pct, -25.0, 25.0)   # 有効範囲でクリップ
    w = 1.5*M + 2.0*(M+L)*(L/M)**2 + eta*(M+L)*(1.5*V**2 + 0.35*V*G)
    if slope_pct < 0:
        w = max(w * (1.0 + 0.02*abs(slope_pct)), (1.5*M + 2.0*(M+L)*(L/M)**2) * 0.5)
    return max(w, 0.0)


# ══════════════════════════════════════════════════
# ランニング: Minetti式
# ══════════════════════════════════════════════════

def minetti_watt(speed_ms, slope_pct, profile):
    """
    Minetti et al.（2002）式。傾斜付きランニング専用。

    エネルギーコスト c_r [J/kg/m]:
      c_r(i) = 155.4i^5 - 30.4i^4 - 43.3i^3 + 46.3i^2 + 19.5i + 3.6
      i = slope / 100（小数の傾斜）

    W_dot = c_r × M × V [W]

    検証: 65kg, 3.0m/s(10.8km/h), 平坦:
      c_r = 3.6 J/kg/m → W = 702W → 604 kcal/hr
      42km (3.9hr) → 総消費 ≈ 2,350 kcal ✓
    """
    M, V = profile.body_weight_kg, max(speed_ms, 0.01)
    i    = slope_pct / 100.0
    # Minetti（2002）多項式にスケール係数1.21を適用
    # 原著の c_r(0)=3.6 J/kg/m は最小酸素消費量ベースで実測より約20%低め
    # 補正後: 65kg×42km→約2850kcal（実測値1.04kcal/kg/kmに合致）
    SCALE = 1.21
    c_r  = SCALE * (155.4*i**5 - 30.4*i**4 - 43.3*i**3 + 46.3*i**2 + 19.5*i + 3.6)
    c_r  = max(c_r, 1.8)   # 下限: 下り最適斜度でも代謝は必要
    # 荷物補正（ランニングでも荷物は消費を増やす）
    load_factor = 1.0 + 0.05 * profile.load_weight_kg / M
    return max(c_r * M * V * load_factor, 0.0)


# ══════════════════════════════════════════════════
# ディスパッチャ
# ══════════════════════════════════════════════════

def calc_altitude_factor(altitude_m: float) -> float:
    """
    標高からエネルギー消費補正係数を計算する。

    根拠: Fulco et al. (1998)
      1500m 以下: 補正なし（海面レベルと同等）
      1500m 超  : 1000m ごとに +4%
      上限: 1.5（5000m以上で頭打ち）

    例:
      2305m（富士山5合目）: ×1.032 (+3%)
      3776m（富士山山頂）:  ×1.091 (+9%)
    """
    bonus = 0.04 * max(0.0, (altitude_m - 1500.0) / 1000.0)
    return min(1.0 + bonus, 1.5)


def cycling_watt(speed_ms, slope_pct, profile):
    """
    Martin et al. (1998) Cycling Power Model。
    自転車走行時の体が消費する総エネルギー [W] を返す。

    機械的出力:
      P_gravity  = m·g·sin(θ)·v     重力（登り）
      P_rolling  = m·g·cos(θ)·Crr·v 転がり抵抗
      P_aero     = 0.5·ρ·CdA·v³     空気抵抗

    体への総エネルギー = 機械的出力 / 効率(0.25) + 安静時代謝(80W)

    検証: 70kg, 25km/h, 平坦 → 497W = 428 kcal/hr → 100km で 1710 kcal
    """
    m    = profile.body_weight_kg + max(profile.load_weight_kg, 0.0)
    g    = 9.81
    v    = max(speed_ms, 0.01)
    rho  = 1.2    # 空気密度 (kg/m³)
    cda  = getattr(profile, "bike_cda",  0.40)
    crr  = getattr(profile, "bike_crr",  0.005)

    sinθ = np.sin(np.arctan(slope_pct / 100.0))
    cosθ = np.cos(np.arctan(slope_pct / 100.0))

    p_mech = (m * g * sinθ * v
            + m * g * cosθ * crr * v
            + 0.5 * rho * cda * v**3)

    p_mech = max(p_mech, 0.0)   # 下りの回生は考慮しない（コースティング）

    basal  = 80.0    # 安静時代謝 (W)
    return p_mech / 0.25 + basal


def energy_watt(speed_ms, slope_pct, profile):
    """
    活動種別に応じて式を切り替えて W_dot [W] を返す。
    weather_factor（気象）と altitude_factor（高度）を乗算する。
    """
    if profile.activity_type == ACTIVITY_RUNNING:
        base_w = minetti_watt(speed_ms, slope_pct, profile)
    elif profile.activity_type == ACTIVITY_CYCLING:
        base_w = cycling_watt(speed_ms, slope_pct, profile)
    else:
        base_w = pandolf_watt(speed_ms, slope_pct, profile)
    wf = getattr(profile, "weather_factor", 1.0)
    af = calc_altitude_factor(getattr(profile, "altitude_m", 0.0))
    return base_w * wf * af


# ══════════════════════════════════════════════════
# Ke 時系列計算
# ══════════════════════════════════════════════════

def estimate_ke_series(speed_arr, slope_arr, hr_arr, e_arr, profile):
    """Ke(t) = W_dot_kcal_hr(t) × zone_correction(t) / E(t)  [1/hr]"""
    ke = np.zeros(len(speed_arr))
    for i in range(len(speed_arr)):
        w_kcal_hr = energy_watt(speed_arr[i], slope_arr[i], profile) * WATT_TO_KCAL_HR
        ke[i]     = (w_kcal_hr * profile.zone_correction(hr_arr[i])) / max(e_arr[i], 50.0)
    return ke


# ══════════════════════════════════════════════════
# 動作確認
# ══════════════════════════════════════════════════

def run_ke_check():
    def table(cases, profile):
        print(f"  体重:{profile.body_weight_kg}kg 荷物:{profile.load_weight_kg}kg "
              f"E(0):{profile.e_initial_kcal}kcal")
        print(f"  {'条件':<18}{'速度':>6}{'斜度':>6}{'心拍':>5}"
              f"{'W(W)':>8}{'消費(kcal/hr)':>14}{'Ke':>10}")
        print("  " + "-"*67)
        for label, v, g, hr in cases:
            w  = energy_watt(v, g, profile)
            kh = w * WATT_TO_KCAL_HR * profile.zone_correction(hr)
            ke = kh / max(profile.e_initial_kcal, 50.0)
            print(f"  {label:<18}{v:>6.1f}{g:>+6.1f}{hr:>5}{w:>8.0f}{kh:>14.1f}{ke:>10.4f}")

    print("=== 登山モード (Pandolf式) ===")
    hike = UserProfile(65, 10, 180, 1.2, 1800, ACTIVITY_HIKING)
    table([("平坦・ゆっくり", 0.8,  0.0,  75),
           ("緩登り・中強度", 1.0,  8.0, 110),
           ("急登・高強度",   0.6, 20.0, 145),
           ("下り・低強度",   1.2, -8.0,  85)], hike)

    print("\n=== ランニングモード (Minetti式) ===")
    run = UserProfile(65, 0, 180, 1.0, 2500, ACTIVITY_RUNNING)
    table([("ゆっくりジョグ", 2.5,  0.0, 130),
           ("マラソンペース", 3.0,  0.0, 155),
           ("サブ3ペース",    3.5,  0.0, 165),
           ("上り坂5%",       2.5,  5.0, 165),
           ("下り坂-5%",      3.5, -5.0, 140)], run)

    print("\n=== フルマラソン消費カロリー検証 ===")
    V = 3.0; dist = 42.195; time_hr = dist / (V * 3.6)
    w = minetti_watt(V, 0.0, run)
    total = w * WATT_TO_KCAL_HR * time_hr
    print(f"  {V}m/s ({3600/V/60:.2f}分/km)  {time_hr:.2f}hr")
    print(f"  Minetti式: {total:.0f} kcal")
    print(f"  体重×距離×1.04: {65*dist*1.04:.0f} kcal  ← 実測値の目安")


if __name__ == "__main__":
    run_ke_check()
