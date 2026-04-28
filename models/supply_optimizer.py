"""
補給計画逆算モジュール: supply_optimizer.py
============================================
目標タイムと食品リストを与えると、
ハンガーノックを防ぐ最適な補給計画を自動生成する。

アルゴリズム（プロアクティブ均等配置法）:
  1. 補給なしでシミュレーション → 必要補給量を計算
  2. 必要補給量 / 1回あたりの補給量 → 補給回数を決定
  3. 均等間隔で補給時刻を配置
  4. E(t)が危険水準に近い区間では間隔を詰める（貪欲補正）
  5. 最終シミュレーションで妥当性確認
"""

from __future__ import annotations
import numpy as np
from dataclasses import dataclass
from typing import List, Dict

from pk_model_scipy import SupplyEvent, AlertConfig, run_pk_model
from ke_estimator   import UserProfile


@dataclass
class OptimizeResult:
    supply_plan  : List[SupplyEvent]
    final_sim    : dict
    iterations   : int
    is_feasible  : bool
    score        : float
    min_e        : float
    total_kcal   : float
    messages     : List[str]


def optimize_supply_plan(
    engine_a_output  : dict,
    profile          : UserProfile,
    food_master      : Dict[str, dict],
    alert_config     : AlertConfig = None,
    max_supplies     : int   = 12,
    min_interval_min : float = 20.0,
    interval_s       : float = 10.0,
) -> OptimizeResult:
    """
    プロアクティブ均等配置法で補給計画を逆算する。
    """
    if alert_config is None:
        alert_config = AlertConfig()

    e_crit  = alert_config.e_crit_kcal
    t_warn  = alert_config.t_warn_min
    eta_min = engine_a_output["eta_minutes"]
    e0      = profile.e_initial_kcal
    messages: List[str] = []

    # ── Ka降順・kcal降順で食品をランク付け ──
    foods = sorted(food_master.items(), key=lambda x: (x[1]["ka"], x[1]["kcal_per"]), reverse=True)
    best_name, best_info = foods[0]          # 即効性最高
    rich_name, rich_info = max(foods, key=lambda x: x[1]["kcal_per"])  # 総カロリー最高

    messages.append("=== 補給計画逆算を開始 ===")

    # ── Step1: 補給なしでシミュレーション → 必要補給量を算出 ──
    r0 = run_pk_model(engine_a_output, profile, [], alert_config, interval_s)
    E0 = r0["E_kcal"]
    t0 = r0["time_min"]

    # 真の消費量 = Ke × E を各ステップで積分
    # （E が 0 でクリップされると E[-1] から逆算できないため直接積分する）
    from ke_estimator import energy_watt, WATT_TO_KCAL_HR
    spd = engine_a_output["speed_curve"]
    slp = engine_a_output["slope_curve"]
    hr  = engine_a_output["hr_predicted"]
    n   = len(spd)
    total_consumed = float(sum(
        energy_watt(spd[i], slp[i], profile) * WATT_TO_KCAL_HR
        * profile.zone_correction(hr[i]) * (interval_s / 3600.0)
        for i in range(n)
    ))
    safety_buffer   = e_crit + 400.0
    required_supply = max(total_consumed - (e0 - safety_buffer), 0.0)

    messages.append(f"  総消費量: {total_consumed:.0f} kcal")
    messages.append(f"  必要補給量: {required_supply:.0f} kcal")

    if required_supply <= 0:
        messages.append("✅ 補給不要（エネルギーが十分）")
        return OptimizeResult([], r0, 0, True, 100.0, float(E0.min()), 0.0, messages)

    # ── Step2: 必要回数を決定 ──
    avg_kcal = float(np.mean([v["kcal_per"] for _, v in foods]))
    n_needed = int(np.ceil(required_supply / avg_kcal))
    n_events = min(max(n_needed, 1), max_supplies)
    messages.append(f"  推奨補給回数: {n_events} 回（平均{avg_kcal:.0f} kcal/回）")

    # ── Step3: 補給可能な時間帯を計算（最初の30分と最後の30分は除外）──
    t_start = max(t_warn + 10.0, 30.0)
    t_end   = eta_min - 30.0

    if t_end <= t_start:
        t_start = t_warn
        t_end   = eta_min * 0.9

    # ── Step4: 均等間隔で配置 ──
    if n_events == 1:
        raw_times = [t_start + (t_end - t_start) / 2]
    else:
        raw_times = list(np.linspace(t_start, t_end, n_events))

    # ── Step5: E(t) が低い区間は間隔を詰める補正 ──
    # E0が safety_buffer 以下になる最初の時刻の少し前に補給を寄せる
    critical_mask = E0 < safety_buffer + 200
    if critical_mask.any():
        first_critical = float(t0[critical_mask.argmax()])
        adjusted_times = []
        for t in raw_times:
            if t > first_critical:
                # 危険域に入る前に補給を前倒し
                t = max(t - t_warn * 0.5, t_start)
            adjusted_times.append(round(t, 1))
        raw_times = adjusted_times

    # 最小間隔を確保
    final_times = [raw_times[0]]
    for t in raw_times[1:]:
        if t - final_times[-1] >= min_interval_min:
            final_times.append(t)
        else:
            final_times.append(final_times[-1] + min_interval_min)

    # ── Step6: 補給食を割り当て ──
    supply_plan: List[SupplyEvent] = []
    for i, t in enumerate(final_times):
        if t >= eta_min:
            break
        # 最初と最後は即効性重視、中間は総カロリー重視
        if i == 0 or i == len(final_times) - 1:
            name, info = best_name, best_info
        else:
            name, info = rich_name, rich_info
        supply_plan.append(SupplyEvent(
            time_min = t,
            kcal     = float(info["kcal_per"]),
            ka       = float(info["ka"]),
        ))
        messages.append(f"  {t:.0f}分: 「{name}」({info['kcal_per']} kcal, Ka={info['ka']:.1f})")

    # ── Step7: 貪欲補正（アラートが残っていれば追加挿入）──
    for _ in range(3):
        r_check = run_pk_model(engine_a_output, profile, supply_plan, alert_config, interval_s)
        if not r_check["alert_times"] or len(supply_plan) >= max_supplies:
            break
        alert_t = r_check["alert_times"][0]
        insert_t = max(alert_t - t_warn, t_start)
        # 既存の補給から min_interval 以上離れているか確認
        if all(abs(insert_t - ev.time_min) >= min_interval_min for ev in supply_plan):
            supply_plan.append(SupplyEvent(insert_t, float(best_info["kcal_per"]), float(best_info["ka"])))
            supply_plan = sorted(supply_plan, key=lambda e: e.time_min)
            messages.append(f"  補正追加 → {insert_t:.0f}分: 「{best_name}」")

    # ── 最終シミュレーション ──
    final_result = run_pk_model(engine_a_output, profile, supply_plan, alert_config, interval_s)
    is_feasible  = len(final_result["alert_times"]) == 0
    min_e        = float(final_result["E_kcal"].min())
    total_kcal   = sum(ev.kcal for ev in supply_plan)

    score = min(100.0, 50.0 + (min_e - e_crit) / max(e_crit, 1) * 50.0) if is_feasible \
            else max(0.0, min_e / max(e_crit, 1) * 50.0)

    status = "✅ 成功" if is_feasible else "⚠️ 不完全（補給を増やしてください）"
    messages.append(f"\n{status}  最小E: {min_e:.0f} kcal  総補給: {total_kcal:.0f} kcal  スコア: {score:.0f}点")

    return OptimizeResult(
        supply_plan = supply_plan,
        final_sim   = final_result,
        iterations  = len(supply_plan),
        is_feasible = is_feasible,
        score       = score,
        min_e       = min_e,
        total_kcal  = total_kcal,
        messages    = messages,
    )


if __name__ == "__main__":
    import sys, os, pandas as pd
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pace_predictor import PaceRegressor, optimize_pace, tobler_speed
    from ke_estimator   import ACTIVITY_HIKING

    PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    df   = pd.read_csv(os.path.join(PROJ, "data/processed/merged_log.csv"), parse_dates=["timestamp"])
    reg  = PaceRegressor(); reg.fit(df)
    np.random.seed(42)

    segs  = [(20.0, 2.5, 900), (35.0, 3.0, 1080), (25.0, 2.5, 360)]
    slope = np.clip(np.concatenate([np.random.normal(s[0],s[1],s[2]) for s in segs]), -60, 60)
    prof  = UserProfile(65, 10, 180, 1.2, 1800, ACTIVITY_HIKING)
    tob   = np.array([tobler_speed(s) for s in slope])
    dist  = np.clip(tob*10, 0.5, 20)
    ea    = optimize_pace(reg, slope, 110, np.sum(dist/np.clip(tob,0.1,2.0))/60*1.1, 180, 0.85, profile=prof)

    food_master = {
        "エネルギージェル" : {"kcal_per": 100, "ka": 2.0},
        "おにぎり (梅)"    : {"kcal_per": 170, "ka": 0.9},
        "バナナ"           : {"kcal_per":  86, "ka": 0.8},
        "カロリーメイト"   : {"kcal_per": 100, "ka": 0.6},
        "スポーツドリンク" : {"kcal_per":  50, "ka": 1.8},
    }

    r = optimize_supply_plan(ea, prof, food_master, AlertConfig(300, 25), max_supplies=12)
    print("\n".join(r.messages))
    print(f"\n推奨補給計画 ({len(r.supply_plan)}件):")
    for ev in r.supply_plan:
        h, m = int(ev.time_min)//60, int(ev.time_min)%60
        print(f"  {h}時間{m:02d}分: {ev.kcal:.0f} kcal  Ka={ev.ka:.1f}")
    print(f"\n実現可能: {r.is_feasible}  スコア: {r.score:.0f}点")
