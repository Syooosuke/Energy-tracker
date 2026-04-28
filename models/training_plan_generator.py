"""
models/training_plan_generator.py — 自動トレーニングプラン生成
=============================================================
目標レース（日程・距離・種別）と現在のフィットネス状態（CTL/TSB/VO2max）から
週次トレーニング計画を自動生成する。

周期化 (Periodization) の原則:
  Base Phase   (レース12週〜8週前): 有酸素ベース構築、CTL+5/週
  Build Phase  (レース8週〜3週前) : レース特異的練習（テンポ・インターバル）
  Peak Phase   (レース3週〜2週前) : 最高 CTL を維持しながら疲労を抑制
  Taper Phase  (レース2週前〜前日): ボリューム 60→40% に削減、フレッシュネス確保

参考:
  Pfitzinger & Douglas (2009) "Advanced Marathoning"
  Friel (2009) "The Triathlete's Training Bible"
  Coggan & Allen (2010) "Training and Racing with a Power Meter"
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from datetime import date, timedelta
from typing import Optional


# ══════════════════════════════════════════════════════
# 定数
# ══════════════════════════════════════════════════════

WORKOUT_REST     = "REST"
WORKOUT_EASY     = "EASY"
WORKOUT_LONG     = "LONG"
WORKOUT_TEMPO    = "TEMPO"
WORKOUT_INTERVAL = "INTERVAL"
WORKOUT_RACE     = "RACE"
WORKOUT_CROSS    = "CROSS"     # クロストレーニング（水泳・自転車など）

# アクティビティ種別ごとのデフォルトパラメータ
_ACTIVITY_CONFIG = {
    "running": {
        "base_long_min":    90,
        "peak_long_min":   180,
        "easy_min":         45,
        "tempo_min":        40,
        "interval_min":     45,
        "weekly_days":       5,
        "tss_per_hr_easy":  50,
        "tss_per_hr_tempo": 80,
        "tss_per_hr_int":  100,
    },
    "trail_running": {
        "base_long_min":   120,
        "peak_long_min":   240,
        "easy_min":         60,
        "tempo_min":        45,
        "interval_min":     50,
        "weekly_days":       5,
        "tss_per_hr_easy":  55,
        "tss_per_hr_tempo": 90,
        "tss_per_hr_int":  110,
    },
    "hiking": {
        "base_long_min":   180,
        "peak_long_min":   360,
        "easy_min":         60,
        "tempo_min":        60,
        "interval_min":     60,
        "weekly_days":       4,
        "tss_per_hr_easy":  40,
        "tss_per_hr_tempo": 65,
        "tss_per_hr_int":   80,
    },
    "cycling": {
        "base_long_min":   120,
        "peak_long_min":   240,
        "easy_min":         60,
        "tempo_min":        60,
        "interval_min":     60,
        "weekly_days":       5,
        "tss_per_hr_easy":  45,
        "tss_per_hr_tempo": 75,
        "tss_per_hr_int":   95,
    },
}

_PHASE_BASE  = "base"
_PHASE_BUILD = "build"
_PHASE_PEAK  = "peak"
_PHASE_TAPER = "taper"


# ══════════════════════════════════════════════════════
# データ構造
# ══════════════════════════════════════════════════════

@dataclass
class WorkoutDay:
    date:         date
    workout_type: str              # WORKOUT_* 定数
    duration_min: int              # 推奨時間 (分)
    target_zone:  str              # "Z1-Z2" など
    description:  str              # 詳細説明
    tss_estimate: float            # 推定トレーニングストレス
    notes:        str = ""         # 追加メモ

    def to_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        return d


@dataclass
class WeeklyPlan:
    week_number:  int              # 1始まり
    start_date:   date
    end_date:     date
    phase:        str              # base / build / peak / taper
    phase_label:  str              # 表示用ラベル
    workouts:     list[WorkoutDay]
    weekly_tss:   float            # 週合計 TSS
    focus:        str              # その週のフォーカス説明
    ctl_target:   float            # 目標 CTL

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k != "workouts"}
        d["start_date"] = self.start_date.isoformat()
        d["end_date"]   = self.end_date.isoformat()
        d["workouts"]   = [w.to_dict() for w in self.workouts]
        return d


@dataclass
class TrainingPlan:
    plan_id:           Optional[int]  # DB保存後に付与
    user_id:           int
    race_date:         date
    race_name:         str
    race_distance_km:  float
    activity_type:     str
    created_at:        str
    current_ctl:       float
    target_ctl:        float
    weeks:             list[WeeklyPlan]
    total_weeks:       int
    summary:           str

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k != "weeks"}
        d["race_date"] = self.race_date.isoformat()
        d["weeks"]     = [w.to_dict() for w in self.weeks]
        return d


# ══════════════════════════════════════════════════════
# プラン生成
# ══════════════════════════════════════════════════════

def generate_training_plan(
    user_id:          int,
    race_date:        date,
    race_name:        str,
    race_distance_km: float,
    activity_type:    str,           # "running"|"trail_running"|"hiking"|"cycling"
    current_ctl:      float,         # 現在の CTL（42日EMA TRIMP）
    current_tsb:      float,         # 現在の TSB
    current_vo2max:   Optional[float] = None,
    today:            Optional[date]  = None,
) -> TrainingPlan:
    """
    トレーニングプランを生成する。

    Parameters
    ----------
    user_id          : ユーザー ID
    race_date        : 目標レースの日付
    race_name        : レース名
    race_distance_km : レース距離 (km)
    activity_type    : アクティビティ種別
    current_ctl      : 現在の CTL（フィットネス指標）
    current_tsb      : 現在の TSB（コンディション指標）
    current_vo2max   : 現在の VO2max（省略可）
    today            : 計画開始日（省略時: 今日）
    """
    if today is None:
        today = date.today()

    days_to_race = (race_date - today).days
    if days_to_race < 7:
        return _minimal_plan(user_id, race_date, race_name, race_distance_km,
                             activity_type, current_ctl, today)

    cfg = _ACTIVITY_CONFIG.get(activity_type, _ACTIVITY_CONFIG["running"])

    # ── フェーズ境界を決定 ─────────────────────────────
    total_weeks = min(max(math.ceil(days_to_race / 7), 1), 24)
    phase_weeks = _calc_phase_weeks(total_weeks)

    # ── 目標CTLを計算 ─────────────────────────────────
    # レースまでの間に CTL を適切なレベルまで引き上げる
    target_ctl = _calc_target_ctl(
        current_ctl, race_distance_km, activity_type, total_weeks
    )

    # ── 週次プランを生成 ──────────────────────────────
    weeks: list[WeeklyPlan] = []
    ctl_current = current_ctl

    for week_num in range(1, total_weeks + 1):
        week_start = today + timedelta(weeks=week_num - 1)
        week_end   = week_start + timedelta(days=6)
        if week_end >= race_date:
            week_end = race_date

        phase      = _get_phase(week_num, total_weeks, phase_weeks)
        phase_prog = _phase_progress(week_num, total_weeks, phase_weeks)

        # 週の TSS 目標
        weekly_tss = _calc_weekly_tss(
            ctl_current, target_ctl, phase, phase_prog, cfg
        )

        # CTL更新（近似）
        ctl_current = ctl_current + (1.0 / 42.0) * (weekly_tss / 7.0 - ctl_current)

        # 日別ワークアウトを生成
        workouts = _generate_week_workouts(
            week_start, week_end, weekly_tss, phase, cfg, activity_type,
            is_race_week=(week_end >= race_date or
                          (race_date - week_end).days <= 7),
            race_date=race_date,
        )

        phase_labels = {
            _PHASE_BASE:  "ベースビルド",
            _PHASE_BUILD: "ビルドアップ",
            _PHASE_PEAK:  "ピーク",
            _PHASE_TAPER: "テーパリング",
        }
        focus = _phase_focus(phase, phase_prog, race_distance_km, activity_type)

        weeks.append(WeeklyPlan(
            week_number = week_num,
            start_date  = week_start,
            end_date    = week_end,
            phase       = phase,
            phase_label = phase_labels.get(phase, phase),
            workouts    = workouts,
            weekly_tss  = round(weekly_tss, 1),
            focus       = focus,
            ctl_target  = round(ctl_current, 1),
        ))

    summary = _generate_summary(
        total_weeks, current_ctl, target_ctl, race_distance_km,
        activity_type, phase_weeks
    )

    import datetime as _dt
    return TrainingPlan(
        plan_id           = None,
        user_id           = user_id,
        race_date         = race_date,
        race_name         = race_name,
        race_distance_km  = race_distance_km,
        activity_type     = activity_type,
        created_at        = _dt.datetime.now().isoformat(timespec="seconds"),
        current_ctl       = round(current_ctl, 1),
        target_ctl        = round(target_ctl, 1),
        weeks             = weeks,
        total_weeks       = total_weeks,
        summary           = summary,
    )


# ══════════════════════════════════════════════════════
# フェーズ計算
# ══════════════════════════════════════════════════════

def _calc_phase_weeks(total_weeks: int) -> dict[str, int]:
    """総週数からフェーズ別週数を計算する。"""
    taper  = min(2, max(1, total_weeks // 6))
    peak   = min(2, max(1, total_weeks // 8))
    build  = min(max(total_weeks // 3, 3), 8)
    base   = total_weeks - build - peak - taper
    base   = max(base, 0)
    return {
        _PHASE_BASE:  base,
        _PHASE_BUILD: build,
        _PHASE_PEAK:  peak,
        _PHASE_TAPER: taper,
    }


def _get_phase(week_num: int, total_weeks: int, phase_weeks: dict) -> str:
    base_end  = phase_weeks[_PHASE_BASE]
    build_end = base_end + phase_weeks[_PHASE_BUILD]
    peak_end  = build_end + phase_weeks[_PHASE_PEAK]

    if week_num <= base_end:
        return _PHASE_BASE
    elif week_num <= build_end:
        return _PHASE_BUILD
    elif week_num <= peak_end:
        return _PHASE_PEAK
    else:
        return _PHASE_TAPER


def _phase_progress(week_num: int, total_weeks: int, phase_weeks: dict) -> float:
    """フェーズ内の進行度 (0-1)。"""
    base_end  = phase_weeks[_PHASE_BASE]
    build_end = base_end + phase_weeks[_PHASE_BUILD]
    peak_end  = build_end + phase_weeks[_PHASE_PEAK]

    if week_num <= base_end:
        return (week_num - 1) / max(base_end, 1)
    elif week_num <= build_end:
        return (week_num - base_end - 1) / max(phase_weeks[_PHASE_BUILD], 1)
    elif week_num <= peak_end:
        return (week_num - build_end - 1) / max(phase_weeks[_PHASE_PEAK], 1)
    else:
        taper_start = peak_end
        return (week_num - taper_start - 1) / max(phase_weeks[_PHASE_TAPER], 1)


# ══════════════════════════════════════════════════════
# TSS 目標計算
# ══════════════════════════════════════════════════════

def _calc_target_ctl(
    current_ctl: float,
    distance_km: float,
    activity_type: str,
    total_weeks: int,
) -> float:
    """レースに向けた目標 CTL を計算する。"""
    # 距離に応じた推奨 CTL 範囲（経験則）
    distance_targets = {
        5:   (40, 60),
        10:  (50, 70),
        21:  (60, 80),
        42:  (75, 100),
        100: (90, 130),
    }
    target_base = 70.0
    for d, (lo, hi) in sorted(distance_targets.items()):
        if distance_km <= d:
            target_base = (lo + hi) / 2
            break
    else:
        target_base = 100.0  # 超長距離

    # 現在の CTL との差を週数で割って上限を設定（週+3 が上限）
    max_increase = total_weeks * 3.0
    target_ctl   = min(current_ctl + max_increase, target_base)
    return max(target_ctl, current_ctl)  # 現在より下げない


def _calc_weekly_tss(
    current_ctl: float,
    target_ctl:  float,
    phase:       str,
    phase_prog:  float,   # 0-1
    cfg:         dict,
) -> float:
    """週の目標 TSS を計算する。"""
    base_tss = current_ctl * 7.0   # CTL = 7日平均 TRIMP ≈ TSS/7

    multipliers = {
        _PHASE_BASE:  0.80 + 0.20 * phase_prog,    # 0.80 → 1.00
        _PHASE_BUILD: 1.00 + 0.25 * phase_prog,    # 1.00 → 1.25
        _PHASE_PEAK:  1.20 + 0.10 * phase_prog,    # 1.20 → 1.30
        _PHASE_TAPER: 0.60 - 0.20 * phase_prog,    # 0.60 → 0.40
    }
    mult = multipliers.get(phase, 1.0)

    # ショックウィーク防止: 週 TSS は前週比 +10% 以内に抑制
    weekly_tss = base_tss * mult
    return round(max(weekly_tss, 30.0), 1)


# ══════════════════════════════════════════════════════
# 日別ワークアウト生成
# ══════════════════════════════════════════════════════

_PHASE_TEMPLATES: dict[str, list[str]] = {
    # phase → [月, 火, 水, 木, 金, 土, 日]
    _PHASE_BASE:  [WORKOUT_EASY,  WORKOUT_REST,  WORKOUT_EASY,  WORKOUT_LONG,
                   WORKOUT_REST,  WORKOUT_EASY,  WORKOUT_REST],
    _PHASE_BUILD: [WORKOUT_EASY,  WORKOUT_TEMPO, WORKOUT_EASY,  WORKOUT_LONG,
                   WORKOUT_REST,  WORKOUT_INTERVAL, WORKOUT_REST],
    _PHASE_PEAK:  [WORKOUT_EASY,  WORKOUT_TEMPO, WORKOUT_REST,  WORKOUT_LONG,
                   WORKOUT_REST,  WORKOUT_TEMPO, WORKOUT_REST],
    _PHASE_TAPER: [WORKOUT_EASY,  WORKOUT_REST,  WORKOUT_EASY,  WORKOUT_EASY,
                   WORKOUT_REST,  WORKOUT_EASY,  WORKOUT_REST],
}

_WORKOUT_DETAILS: dict[str, dict] = {
    WORKOUT_REST: {
        "zone": "休養",
        "desc": "完全休養または軽いストレッチ。筋肉と神経系を回復させる重要な日です。",
        "tss_factor": 0.0,
    },
    WORKOUT_EASY: {
        "zone": "Z1-Z2",
        "desc": "会話できる楽なペースで有酸素ベースを構築。心拍は最大の60〜70%を維持。疲労感なく終われるのが目安。",
        "tss_factor": 0.8,
    },
    WORKOUT_LONG: {
        "zone": "Z2",
        "desc": "有酸素能力の最大化を目指す長距離走（LSD）。ペースより時間を重視し、終始会話できる強度を保つ。",
        "tss_factor": 1.0,
    },
    WORKOUT_TEMPO: {
        "zone": "Z3-Z4",
        "desc": "乳酸閾値（LT）ペースで持続走。会話が断片的になる程度の強度を20〜40分維持。心拍は最大の80〜88%。",
        "tss_factor": 1.5,
    },
    WORKOUT_INTERVAL: {
        "zone": "Z4-Z5",
        "desc": "高強度の繰り返しトレーニング。3〜5分のハードな走りを短いリカバリーを挟みながら繰り返す。VO2maxを高める。",
        "tss_factor": 1.8,
    },
    WORKOUT_CROSS: {
        "zone": "Z1-Z2",
        "desc": "水泳・サイクリング・ヨガなど主競技以外の有酸素運動。関節への負担を減らしながら有酸素能力を維持・向上。",
        "tss_factor": 0.7,
    },
    WORKOUT_RACE: {
        "zone": "全力",
        "desc": "目標レース当日。これまでの練習の成果を発揮する日。朝食は3時間前までに済ませ、しっかり水分補給を。",
        "tss_factor": 2.5,
    },
}


def _generate_week_workouts(
    week_start:   date,
    week_end:     date,
    weekly_tss:   float,
    phase:        str,
    cfg:          dict,
    activity_type: str,
    is_race_week: bool,
    race_date:    date,
) -> list[WorkoutDay]:
    """週の日別ワークアウトリストを生成する。"""
    template = _PHASE_TEMPLATES.get(phase, _PHASE_TEMPLATES[_PHASE_BASE])
    workouts: list[WorkoutDay] = []

    # ワークアウト別 TSS 配分比率
    type_counts  = {t: template.count(t) for t in set(template)}
    type_weights = {t: _WORKOUT_DETAILS[t]["tss_factor"] for t in type_counts}
    total_weight = sum(type_counts[t] * type_weights[t] for t in type_counts)

    day = week_start
    dow = day.weekday()  # 0=月曜
    idx = 0

    while day <= week_end and idx < 7:
        template_idx = (dow + idx) % 7
        wtype = template[template_idx]

        # レース当日は RACE に変更
        if day == race_date:
            wtype = WORKOUT_RACE

        detail = _WORKOUT_DETAILS[wtype]
        if wtype == WORKOUT_RACE:
            # RACEはテンプレート外で挿入されるため type_weights に存在しない
            tss_ratio = _WORKOUT_DETAILS[WORKOUT_RACE]["tss_factor"] / max(total_weight, 0.1)
        else:
            tss_ratio = type_weights[wtype] / max(total_weight, 0.1)
        day_tss   = round(weekly_tss * tss_ratio, 1)

        duration_min = _calc_duration(wtype, day_tss, cfg, activity_type)

        notes = ""
        if wtype == WORKOUT_LONG and activity_type in ("trail_running", "hiking"):
            notes = "コースに標高差を入れると理想的です"
        elif wtype == WORKOUT_INTERVAL:
            notes = "ウォームアップ15分 + インターバル + クールダウン15分"
        elif wtype == WORKOUT_RACE:
            notes = "目標レース当日。十分な睡眠と朝食を"

        workouts.append(WorkoutDay(
            date         = day,
            workout_type = wtype,
            duration_min = duration_min,
            target_zone  = detail["zone"],
            description  = detail["desc"],
            tss_estimate = day_tss,
            notes        = notes,
        ))

        day += timedelta(days=1)
        idx += 1

    return workouts


def _calc_duration(
    wtype: str,
    day_tss: float,
    cfg: dict,
    activity_type: str,
) -> int:
    """ワークアウトタイプと TSS から推奨時間 (分) を計算する。"""
    if wtype == WORKOUT_REST:
        return 0
    elif wtype == WORKOUT_RACE:
        return 0  # レース時間は不定

    tss_per_hr = {
        WORKOUT_EASY:     cfg["tss_per_hr_easy"],
        WORKOUT_LONG:     cfg["tss_per_hr_easy"],
        WORKOUT_TEMPO:    cfg["tss_per_hr_tempo"],
        WORKOUT_INTERVAL: cfg["tss_per_hr_int"],
        WORKOUT_CROSS:    cfg["tss_per_hr_easy"],
    }.get(wtype, cfg["tss_per_hr_easy"])

    duration = max(20, min(int(day_tss / tss_per_hr * 60), 360))

    # アクティビティ別最大時間
    if wtype == WORKOUT_LONG:
        duration = min(duration, cfg.get("peak_long_min", 240))
    elif wtype == WORKOUT_EASY:
        duration = min(duration, cfg.get("easy_min", 60) * 2)

    return duration


# ══════════════════════════════════════════════════════
# サマリーテキスト生成
# ══════════════════════════════════════════════════════

def _phase_focus(
    phase: str,
    phase_prog: float,
    distance_km: float,
    activity_type: str,
) -> str:
    """フェーズと進行度に応じたフォーカス説明を返す。"""
    focuses = {
        _PHASE_BASE:  "有酸素ベースの構築と週間ボリュームの定着",
        _PHASE_BUILD: "テンポ走・インターバルでスピードと乳酸処理能力を強化",
        _PHASE_PEAK:  "ピーク負荷到達後、疲労を抜きながらフォームを維持",
        _PHASE_TAPER: "ボリュームを減らしフレッシュネスを最大化。レース準備を整える",
    }
    return focuses.get(phase, "")


def _generate_summary(
    total_weeks: int,
    current_ctl: float,
    target_ctl: float,
    distance_km: float,
    activity_type: str,
    phase_weeks: dict,
) -> str:
    lines = [
        f"総計画期間: {total_weeks}週間",
        f"現在のCTL: {current_ctl:.1f} → 目標CTL: {target_ctl:.1f}",
        f"フェーズ構成: ベース{phase_weeks[_PHASE_BASE]}週 / "
        f"ビルド{phase_weeks[_PHASE_BUILD]}週 / "
        f"ピーク{phase_weeks[_PHASE_PEAK]}週 / "
        f"テーパー{phase_weeks[_PHASE_TAPER]}週",
    ]

    if current_ctl < 40 and distance_km >= 42:
        lines.append(
            "※ 現在の CTL がフルマラソン・ウルトラレースには低めです。"
            " ベースフェーズをしっかり積み上げることを優先してください。"
        )
    elif current_ctl >= 80:
        lines.append("現在のフィットネスは良好です。ビルドフェーズで質を高めましょう。")

    return " | ".join(lines)


# ══════════════════════════════════════════════════════
# フォールバック（レースまで1週間未満）
# ══════════════════════════════════════════════════════

def _minimal_plan(
    user_id: int,
    race_date: date,
    race_name: str,
    distance_km: float,
    activity_type: str,
    current_ctl: float,
    today: date,
) -> TrainingPlan:
    import datetime as _dt
    days = (race_date - today).days

    workouts = []
    for i in range(days):
        d = today + timedelta(days=i)
        if i < days - 2:
            wtype = WORKOUT_EASY
            detail = _WORKOUT_DETAILS[WORKOUT_EASY]
            workouts.append(WorkoutDay(
                date=d, workout_type=wtype, duration_min=30,
                target_zone=detail["zone"], description=detail["desc"],
                tss_estimate=20.0, notes="レース直前は軽めに"
            ))
        else:
            workouts.append(WorkoutDay(
                date=d, workout_type=WORKOUT_REST, duration_min=0,
                target_zone="休養", description="完全休養",
                tss_estimate=0.0, notes=""
            ))

    week = WeeklyPlan(
        week_number=1, start_date=today, end_date=race_date,
        phase=_PHASE_TAPER, phase_label="テーパー（直前）",
        workouts=workouts, weekly_tss=20.0 * max(days - 2, 0),
        focus="レース直前：軽めのランでキレを維持し、十分な睡眠を確保",
        ctl_target=current_ctl,
    )

    return TrainingPlan(
        plan_id=None, user_id=user_id, race_date=race_date,
        race_name=race_name, race_distance_km=distance_km,
        activity_type=activity_type,
        created_at=_dt.datetime.now().isoformat(timespec="seconds"),
        current_ctl=current_ctl, target_ctl=current_ctl,
        weeks=[week], total_weeks=1,
        summary="レースまで1週間未満のため最小限のテーパープランです。",
    )
