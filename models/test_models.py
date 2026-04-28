"""
自動テスト: test_models.py
===========================
実行方法:
  cd ~/energy-tracker
  python models/test_models.py

各モジュールの主要関数の出力が物理的に妥当かを確認する。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

# ── テストユーティリティ ────────────────────────────
PASS = "✅"
FAIL = "❌"
results = []

def check(label, cond, detail=""):
    status = PASS if cond else FAIL
    results.append((status, label, detail))
    print(f"  {status} {label}" + (f"  ({detail})" if detail else ""))


# ══════════════════════════════════════════════════
# 1. ke_estimator のテスト
# ══════════════════════════════════════════════════
print("\n=== ke_estimator ===")
from ke_estimator import (UserProfile, pandolf_watt, minetti_watt,
                           energy_watt, WATT_TO_KCAL_HR,
                           ACTIVITY_HIKING, ACTIVITY_RUNNING)

hike_prof = UserProfile(65, 10, 180, 1.2, 1800, ACTIVITY_HIKING)
run_prof  = UserProfile(65,  0, 180, 1.0, 2500, ACTIVITY_RUNNING, 3.0)

# Pandolf式: 平坦1.0m/sでの消費
w_flat = pandolf_watt(1.0, 0.0, hike_prof)
check("Pandolf 平坦1m/s: 100〜300W",
      100 < w_flat < 300, f"{w_flat:.0f}W")

# Pandolf式: 急登20%でより大きな消費
w_steep = pandolf_watt(0.6, 20.0, hike_prof)
check("Pandolf 急登20%: 平坦より大きい",
      w_steep > w_flat, f"{w_steep:.0f}W > {w_flat:.0f}W")

# Minetti式: 3.0m/sフラット → 実測値の目安
w_run = minetti_watt(3.0, 0.0, run_prof)
kcal_hr_run = w_run * WATT_TO_KCAL_HR
check("Minetti 3.0m/s平坦: 400〜800 kcal/hr",
      400 < kcal_hr_run < 800, f"{kcal_hr_run:.0f} kcal/hr")

# フルマラソン総消費 (3.0m/s, 42.195km)
time_hr = 42.195 / (3.0 * 3.6)
total_kcal = w_run * WATT_TO_KCAL_HR * time_hr
check("Minetti フルマラソン総消費: 2000〜3500 kcal",
      2000 < total_kcal < 3500, f"{total_kcal:.0f} kcal")

# ディスパッチャ
w_hike = energy_watt(1.0, 10.0, hike_prof)
w_run2 = energy_watt(3.0, 1.0, run_prof)
check("energy_watt: hiking → Pandolf",  w_hike == pandolf_watt(1.0, 10.0, hike_prof))
check("energy_watt: running → Minetti", w_run2 == minetti_watt(3.0, 1.0, run_prof))


# ══════════════════════════════════════════════════
# 2. pace_predictor のテスト
# ══════════════════════════════════════════════════
print("\n=== pace_predictor ===")
from pace_predictor import PaceRegressor, tobler_speed, running_speed, optimize_pace

# Tobler: 各斜度での速度
check("Tobler 平坦0%: 1.0〜1.8 m/s",  1.0 < tobler_speed(0)  < 1.8,  f"{tobler_speed(0):.3f}")
check("Tobler 急登20%: 0.4〜0.9 m/s", 0.4 < tobler_speed(20) < 0.9,  f"{tobler_speed(20):.3f}")
check("Tobler 急登60%: 下限0.1",       tobler_speed(60) >= 0.1,        f"{tobler_speed(60):.3f}")
check("Tobler 急登 < 平坦",            tobler_speed(20) < tobler_speed(0))

# running_speed: Minetti傾斜補正
v_flat  = running_speed(0.0,  3.0)
v_up    = running_speed(5.0,  3.0)
v_down  = running_speed(-5.0, 3.0)
check("running_speed 平坦: 基準速度と近い", abs(v_flat - 3.0) < 0.5, f"{v_flat:.2f}")
check("running_speed 上り < 平坦",          v_up < v_flat,           f"{v_up:.2f}")
check("running_speed 下り > 平坦",          v_down > v_flat,         f"{v_down:.2f}")

# 回帰モデル（学習データが必要）
log_path = os.path.join(os.path.dirname(__file__), "../data/processed/merged_log.csv")
if os.path.exists(log_path):
    df = pd.read_csv(log_path, parse_dates=["timestamp"])
    reg = PaceRegressor()
    reg.fit(df)
    check("PaceRegressor: R² > 0.5", reg.r2 > 0.5, f"R²={reg.r2:.3f}")
    check("PaceRegressor: MAE < 0.3 m/s", reg.mae < 0.3, f"MAE={reg.mae:.3f}")
else:
    print(f"  ⚠️ merged_log.csv が見つかりません。回帰モデルのテストをスキップ")


# ══════════════════════════════════════════════════
# 3. pk_model_scipy のテスト
# ══════════════════════════════════════════════════
print("\n=== pk_model_scipy ===")
from pk_model_scipy import SupplyEvent, AlertConfig, run_pk_model, check_alert

# ダミーの Engine A 出力
n = 360   # 60分分（10秒×360点）
dummy_ea = {
    "speed_curve"  : np.full(n, 0.8),
    "slope_curve"  : np.full(n, 10.0),
    "hr_predicted" : np.full(n, 120.0),
    "eta_minutes"  : 60.0,
}

# 補給なし → E(t) が単調減少
r_no_supply = run_pk_model(dummy_ea, hike_prof, [], AlertConfig(300, 25))
E = r_no_supply["E_kcal"]
check("補給なし: E(t) 単調減少",
      np.all(np.diff(E) <= 0.01), f"最小={E.min():.0f} kcal")
check("補給なし: 最終E < 初期E",
      E[-1] < E[0], f"{E[0]:.0f} → {E[-1]:.0f} kcal")

# 補給あり → 補給後に E(t) が回復
r_supply = run_pk_model(dummy_ea, hike_prof,
                        [SupplyEvent(10.0, 200.0, 2.0)],
                        AlertConfig(300, 25))
E_s = r_supply["E_kcal"]
idx_supply = int(10.0 * 60 / 10)
E_before = E_s[idx_supply]
E_after  = E_s[min(idx_supply + 30, n-1)]
check("補給後: E(t) が回復方向に",
      E_after > E_before - 5, f"{E_before:.0f} → {E_after:.0f} kcal")

# アラート機能
alerted = check_alert(0, 400, 0, 1.0, 2.0, AlertConfig(e_crit_kcal=300, t_warn_min=10))
not_alerted = check_alert(0, 2000, 0, 1.0, 0.01, AlertConfig(e_crit_kcal=300, t_warn_min=10))
check("アラート: E=400, Ke=2.0 → 発火する",      alerted)
check("アラート: E=2000, Ke=0.01 → 発火しない",  not not_alerted)

# supply_log が正しく記録される
check("supply_log: 補給イベントが記録される",
      len(r_supply.get("supply_log", [])) == 1)


# ══════════════════════════════════════════════════
# 結果サマリー
# ══════════════════════════════════════════════════
print("\n" + "="*50)
passed = sum(1 for r in results if r[0] == PASS)
failed = sum(1 for r in results if r[0] == FAIL)
print(f"結果: {passed} passed / {failed} failed / {len(results)} total")
if failed > 0:
    print("\n失敗したテスト:")
    for status, label, detail in results:
        if status == FAIL:
            print(f"  {FAIL} {label}  ({detail})")
    sys.exit(1)
else:
    print("全テスト通過 ✅")
