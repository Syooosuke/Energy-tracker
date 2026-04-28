"""
models/race_simulator.py — レースシミュレーター
================================================
目標タイム・コースプロファイルから km 別スプリット予測・
エネルギー消費・補給スケジュールを生成する。

アルゴリズム:
  1. ルートを segment_km 刻みに分割
  2. 各セグメントの勾配に応じた速度を Tobler / Minetti / 疲労モデルで計算
  3. ke_estimator でエネルギー消費を積分
  4. PersonalModelTrainer が利用可能なら個人化 HR を予測
  5. 補給推奨・警告・実現可能性スコアを出力

参考:
  Minetti et al. (2002) "Energy cost of walking and running at extreme uphill
    and downhill slopes" – J. Appl. Physiol.
  Pandolf et al. (1977) "Predicting energy expenditure with loads while
    standing or walking very slowly" – J. Appl. Physiol.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

from ke_estimator import (
    UserProfile,
    pandolf_watt,
    minetti_watt,
    cycling_watt,
    ACTIVITY_HIKING,
    ACTIVITY_RUNNING,
    ACTIVITY_CYCLING,
)
from pace_predictor import running_speed, tobler_speed


# ══════════════════════════════════════════════════════
# データ構造
# ══════════════════════════════════════════════════════

@dataclass
class RaceSplit:
    km:               float   # 累積距離 (km)
    elapsed_min:      float   # 経過時間 (分)
    pace_min_per_km:  float   # ペース (分/km)
    speed_mps:        float   # 速度 (m/s)
    elevation_m:      float   # 標高 (m)
    grade_pct:        float   # 勾配 (%)
    power_w:          float   # 機械的パワー (W)
    energy_kcal_cum:  float   # 累積消費カロリー (kcal)
    hr_predicted:     Optional[float]  # 予測心拍数 (bpm)
    fatigue_index:    float   # 疲労指標 (0-1)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RaceSimResult:
    target_time_min:       float         # 目標完走時間 (分)
    predicted_time_min:    float         # モデル予測完走時間 (分)
    total_energy_kcal:     float         # 総消費カロリー (kcal)
    avg_power_w:           float         # 平均パワー (W)
    avg_pace_min_per_km:   float         # 平均ペース (分/km)
    avg_hr:                Optional[float]  # 平均予測心拍 (bpm)
    total_distance_km:     float
    total_elevation_gain_m: float
    splits:                list[RaceSplit]
    supply_recommendations: list[dict]   # [{at_km, at_min, kcal, reason}]
    warnings:              list[str]
    feasibility_pct:       float         # 目標達成可能性 (0-100)

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k != "splits"}
        d["splits"] = [s.to_dict() for s in self.splits]
        return d


# ══════════════════════════════════════════════════════
# メインシミュレーション
# ══════════════════════════════════════════════════════

def simulate_race(
    route_points: list[dict],          # [{distance_m, elevation_m}]
    target_time_min: float,
    activity_type: str,                # "hiking"|"running"|"trail_running"|"cycling"
    profile: UserProfile,
    segment_km: float = 1.0,           # スプリット間隔 (km)
    personal_trainer=None,             # PersonalModelTrainer | None
    context=None,                      # DayContext | None
) -> RaceSimResult:
    """
    レースシミュレーションを実行する。

    Parameters
    ----------
    route_points    : ルートポイントリスト [{distance_m, elevation_m}]
    target_time_min : 目標完走時間 (分)
    activity_type   : アクティビティ種別
    profile         : ユーザープロファイル
    segment_km      : スプリット間隔 (km)、デフォルト1km
    personal_trainer: 個人化HR予測モデル（省略可）
    context         : 当日の健康コンテキスト（省略可）
    """
    if not route_points or len(route_points) < 2:
        return _empty_result(target_time_min)

    # ── ルートデータ準備 ──────────────────────────────
    dists = np.array([float(p["distance_m"]) for p in route_points])
    elevs = np.array([float(p["elevation_m"]) for p in route_points])

    total_dist_m = float(dists.max())
    if total_dist_m <= 0:
        return _empty_result(target_time_min)

    # 総獲得標高
    diffs = np.diff(elevs)
    total_gain_m = float(np.sum(diffs[diffs > 0]))

    # スプリット点の距離を生成
    seg_m = segment_km * 1000.0
    split_dists = np.arange(seg_m, total_dist_m + seg_m, seg_m)
    split_dists = np.clip(split_dists, seg_m, total_dist_m)
    split_dists = np.concatenate([[0.0], split_dists])
    split_elevs = np.interp(split_dists, dists, elevs)

    # ── 目標速度 ──────────────────────────────────────
    target_speed_mps = total_dist_m / (target_time_min * 60.0)

    # ── 各セグメントを処理 ────────────────────────────
    splits: list[RaceSplit] = []
    cum_energy = 0.0
    cum_time_sec = 0.0
    powers: list[float] = []
    hrs: list[float] = []

    for i in range(1, len(split_dists)):
        seg_dist_m  = float(split_dists[i] - split_dists[i - 1])
        if seg_dist_m <= 0:
            continue

        elev_diff   = float(split_elevs[i] - split_elevs[i - 1])
        grade_pct   = (elev_diff / seg_dist_m) * 100.0
        grade_pct   = float(np.clip(grade_pct, -40.0, 40.0))
        fatigue     = float(split_dists[i] / total_dist_m)

        # ── 速度計算 ──────────────────────────────────
        speed = _calc_segment_speed(
            target_speed_mps, grade_pct, fatigue, activity_type
        )

        seg_time_sec = seg_dist_m / max(speed, 0.1)
        cum_time_sec += seg_time_sec

        # ── エネルギー計算 ────────────────────────────
        p_w = _calc_power(speed, grade_pct, profile, activity_type)
        p_w = max(p_w, 0.0)

        # 機械効率25%でワット→kcal換算 (1 kcal = 4184 J)
        seg_kcal = (p_w * seg_time_sec) / (4184.0 * 0.25)
        cum_energy += seg_kcal
        powers.append(p_w)

        # ── HR予測 ────────────────────────────────────
        hr_pred = _predict_hr(
            speed, grade_pct, split_elevs[i], elevs,
            fatigue, personal_trainer, context
        )
        if hr_pred is not None:
            hrs.append(hr_pred)

        # ペース (min/km)
        pace = (seg_time_sec / 60.0) / (seg_dist_m / 1000.0)

        splits.append(RaceSplit(
            km              = round(float(split_dists[i]) / 1000.0, 2),
            elapsed_min     = round(cum_time_sec / 60.0, 1),
            pace_min_per_km = round(pace, 2),
            speed_mps       = round(speed, 3),
            elevation_m     = round(float(split_elevs[i]), 1),
            grade_pct       = round(grade_pct, 1),
            power_w         = round(p_w, 1),
            energy_kcal_cum = round(cum_energy, 1),
            hr_predicted    = round(hr_pred, 1) if hr_pred else None,
            fatigue_index   = round(fatigue, 3),
        ))

    if not splits:
        return _empty_result(target_time_min)

    predicted_time_min = cum_time_sec / 60.0
    avg_power = float(np.mean(powers)) if powers else 0.0
    avg_pace  = (predicted_time_min / (total_dist_m / 1000.0)) if total_dist_m > 0 else 0.0
    avg_hr    = float(np.mean(hrs)) if hrs else None

    # ── 補給推奨 ─────────────────────────────────────
    supply_recs = _generate_supply_recs(splits, cum_energy, activity_type)

    # ── 実現可能性スコア ──────────────────────────────
    feasibility, warnings = _assess_feasibility(
        target_time_min, predicted_time_min, cum_energy, activity_type
    )

    return RaceSimResult(
        target_time_min        = round(target_time_min, 1),
        predicted_time_min     = round(predicted_time_min, 1),
        total_energy_kcal      = round(cum_energy, 1),
        avg_power_w            = round(avg_power, 1),
        avg_pace_min_per_km    = round(avg_pace, 2),
        avg_hr                 = round(avg_hr, 1) if avg_hr else None,
        total_distance_km      = round(total_dist_m / 1000.0, 2),
        total_elevation_gain_m = round(total_gain_m, 0),
        splits                 = splits,
        supply_recommendations = supply_recs,
        warnings               = warnings,
        feasibility_pct        = round(feasibility, 1),
    )


# ══════════════════════════════════════════════════════
# 速度計算ヘルパー
# ══════════════════════════════════════════════════════

def _calc_segment_speed(
    target_speed: float,
    grade_pct: float,
    fatigue: float,
    activity_type: str,
) -> float:
    """勾配・疲労を考慮したセグメント速度を計算する。"""
    if activity_type in ("running", "trail_running"):
        # Minetti 勾配補正（running_speed は slope_pct, base_speed_ms の順）
        try:
            speed = running_speed(grade_pct, target_speed)
        except Exception:
            # フォールバック: 勾配ペナルティ近似
            speed = target_speed * math.exp(-0.03 * abs(grade_pct))
    elif activity_type == "cycling":
        # 自転車: 登りは減速、下りは加速（最大 2.5 倍まで）
        speed = target_speed / max(1.0 + 0.025 * grade_pct, 0.3)
        speed = min(speed, target_speed * 2.5)
    else:
        # ハイキング: Tobler の公式ベース
        try:
            tobler_base = tobler_speed(grade_pct)   # m/s
            # 目標速度とのスケール比で調整
            tobler_flat = tobler_speed(0.0)
            ratio = tobler_base / max(tobler_flat, 0.01)
            speed = target_speed * ratio
        except Exception:
            speed = target_speed * math.exp(-0.015 * abs(grade_pct))

    # 疲労補正: 後半ほどペースが落ちる（二次曲線）
    fatigue_factor = 1.0 - 0.12 * (fatigue ** 2)
    speed = max(speed * fatigue_factor, 0.2)

    return float(speed)


# ══════════════════════════════════════════════════════
# エネルギー計算ヘルパー
# ══════════════════════════════════════════════════════

def _calc_power(
    speed: float,
    grade_pct: float,
    profile: UserProfile,
    activity_type: str,
) -> float:
    """アクティビティ種別に応じたパワー (W) を返す。"""
    try:
        if activity_type in ("running", "trail_running"):
            return minetti_watt(speed, grade_pct, profile)
        elif activity_type == "cycling":
            return cycling_watt(speed, grade_pct, profile)
        else:
            return pandolf_watt(speed, grade_pct, profile)
    except Exception:
        # 最低限のフォールバック
        total_mass = profile.body_weight_kg + getattr(profile, "load_weight_kg", 0)
        return total_mass * speed * 3.5


# ══════════════════════════════════════════════════════
# HR 予測ヘルパー
# ══════════════════════════════════════════════════════

def _predict_hr(
    speed: float,
    grade_pct: float,
    elevation: float,
    all_elevs: np.ndarray,
    fatigue: float,
    personal_trainer,
    context,
) -> Optional[float]:
    if personal_trainer is None:
        return None
    try:
        elev_max = float(all_elevs.max()) if len(all_elevs) > 0 else 4000.0
        cum_gain_approx = max(grade_pct * 0.1, 0.0)   # 近似値
        hr = personal_trainer.predict_hr(
            slope_pct     = grade_pct,
            speed_mps     = speed,
            elevation     = elevation,
            fatigue_index = fatigue,
            cum_gain_m    = cum_gain_approx,
            elev_min      = 0.0,
            elev_max      = max(elev_max, 1.0),
            context       = context,
        )
        if hr and 40 < hr < 230:
            return float(hr)
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════
# 補給推奨
# ══════════════════════════════════════════════════════

def _generate_supply_recs(
    splits: list[RaceSplit],
    total_kcal: float,
    activity_type: str,
) -> list[dict]:
    """
    定期的な補給推奨を生成する。
    ハイキング: 45分ごと
    ランニング: 30分ごと
    サイクリング: 30分ごと
    """
    interval_min = 45 if activity_type == "hiking" else 30
    recs: list[dict] = []
    last_rec_min = 0.0
    total_km = splits[-1].km if splits else 1.0

    for s in splits:
        if s.elapsed_min - last_rec_min >= interval_min:
            progress = s.km / max(total_km, 1.0)
            expected_burn = total_kcal * progress
            # 補給推奨量: 消費量の 35% 程度を補給
            rec_kcal = max(60.0, min(180.0, expected_burn * 0.35 / max(len(recs) + 1, 1)))
            recs.append({
                "at_km":  s.km,
                "at_min": s.elapsed_min,
                "kcal":   round(rec_kcal, 0),
                "reason": f"{s.elapsed_min:.0f}分経過 — エネルギー補充",
            })
            last_rec_min = s.elapsed_min

    return recs


# ══════════════════════════════════════════════════════
# 実現可能性評価
# ══════════════════════════════════════════════════════

def _assess_feasibility(
    target_min: float,
    predicted_min: float,
    total_kcal: float,
    activity_type: str,
) -> tuple[float, list[str]]:
    """
    目標タイムの実現可能性スコア (0-100) と警告リストを返す。
    """
    warnings = []
    ratio = predicted_min / max(target_min, 1.0)  # 1.0 = ぴったり

    # ratio < 1.0: 目標がモデル予測より速い（タイトな目標）
    # ratio > 1.0: 目標がモデル予測より遅い（余裕ある目標）
    if ratio <= 0.90:
        # 予測より 10%+ 速い目標 → かなり厳しい
        feasibility = max(15.0, 50.0 - (0.90 - ratio) * 500)
        warnings.append(
            f"目標タイムはモデル予測より {(1-ratio)*100:.0f}% 速いです。"
            " 現時点のフィットネスでは厳しい可能性があります。"
        )
    elif ratio <= 0.95:
        feasibility = 65.0
        warnings.append("目標タイムはやや厳しい設定です。ペース配分に注意してください。")
    elif ratio <= 1.05:
        # ほぼ一致
        feasibility = 90.0
    elif ratio <= 1.15:
        feasibility = 85.0
    else:
        # 目標がかなり余裕
        feasibility = min(95.0, 80.0 + (ratio - 1.15) * 50)

    if total_kcal > 3000:
        warnings.append(
            f"総消費カロリーが {total_kcal:.0f} kcal と高いため、"
            " 補給計画をしっかり立てることが重要です。"
        )
    if total_kcal > 5000:
        warnings.append("超長距離レース向けの栄養管理が必要です（胃腸トレーニングも検討）。")

    return feasibility, warnings


# ══════════════════════════════════════════════════════
# ヘルパー
# ══════════════════════════════════════════════════════

def _empty_result(target_time_min: float) -> RaceSimResult:
    return RaceSimResult(
        target_time_min=target_time_min,
        predicted_time_min=target_time_min,
        total_energy_kcal=0.0,
        avg_power_w=0.0,
        avg_pace_min_per_km=0.0,
        avg_hr=None,
        total_distance_km=0.0,
        total_elevation_gain_m=0.0,
        splits=[],
        supply_recommendations=[],
        warnings=["ルートデータが不足しています。GPXをアップロードするか地点を指定してください。"],
        feasibility_pct=50.0,
    )
