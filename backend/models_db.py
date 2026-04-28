"""
models_db.py — SQLAlchemy ORM モデル定義
==========================================
フェーズ1: users テーブル（認証用）
フェーズ2: activities / activity_points / daily_metrics
           個人化 ML モデルの学習データを蓄積するテーブル群
"""

from sqlalchemy import (
    Column, Integer, String, DateTime, Float, Date,
    ForeignKey, Boolean, Text,
)
from sqlalchemy.sql import func
from backend.database import Base


class User(Base):
    """ユーザーアカウント。"""
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    email         = Column(String(255), unique=True, index=True, nullable=False)
    display_name  = Column(String(50),  nullable=False, default="")
    password_hash = Column(String(255), nullable=False)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


class Activity(Base):
    """
    1アクティビティ（ワークアウト）のサマリー。
    Garmin FIT / Apple Health / 手動アップロードを統一して保存する。

    このテーブルが個人化 ML の学習データソースとなる。
    データが増えるほどモデル精度が向上する。
    """
    __tablename__ = "activities"

    id               = Column(Integer, primary_key=True, index=True)
    user_id          = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    source           = Column(String(20),  nullable=False)   # "garmin"|"apple_health"|"manual"
    activity_type    = Column(String(30),  nullable=False)   # "hiking"|"running"|"cycling"
    start_time       = Column(DateTime(timezone=True), nullable=True)
    duration_sec     = Column(Float,   nullable=False, default=0.0)
    distance_m       = Column(Float,   nullable=False, default=0.0)
    elevation_gain_m = Column(Float,   nullable=True)
    elevation_loss_m = Column(Float,   nullable=True)
    avg_hr           = Column(Float,   nullable=True)   # 平均心拍 (bpm)
    max_hr           = Column(Float,   nullable=True)   # 最大心拍 (bpm)
    avg_speed_mps    = Column(Float,   nullable=True)   # 平均速度 (m/s)
    calories_kcal    = Column(Float,   nullable=True)
    avg_temp_c       = Column(Float,   nullable=True)   # 平均気温 (℃)
    hrv_ms           = Column(Float,   nullable=True)   # セッション HRV (ms)
    # ── ML 派生値（学習後に埋める） ──
    trimp            = Column(Float,   nullable=True)   # Training Impulse
    vo2max_estimated = Column(Float,   nullable=True)   # 推定 VO2max
    # ── ルートフィンガープリント（比較ダッシュボード用）──
    gpx_fingerprint  = Column(String(64), nullable=True, index=True)
    # ── 気象（アクティビティ時の実測値）──
    humidity_pct     = Column(Float,   nullable=True)   # 湿度 (%)
    wind_speed_ms    = Column(Float,   nullable=True)   # 風速 (m/s)
    # ── メタ ──
    device_name      = Column(String(100), nullable=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())


class ActivityPoint(Base):
    """
    アクティビティ内の時系列データポイント（10秒〜1分間隔）。
    HR 予測モデル / ペース予測モデルの学習特徴量として使用する。

    データが蓄積されるほどモデルの個人適応精度が向上する中核テーブル。
    """
    __tablename__ = "activity_points"

    id              = Column(Integer, primary_key=True, index=True)
    activity_id     = Column(Integer, ForeignKey("activities.id"), nullable=False, index=True)
    elapsed_sec     = Column(Float,   nullable=False)   # 活動開始からの経過秒
    # GPS
    lat             = Column(Float,   nullable=True)
    lon             = Column(Float,   nullable=True)
    elevation       = Column(Float,   nullable=True)    # 標高 (m)
    slope_pct       = Column(Float,   nullable=True)    # 斜度 (%)
    # センサー
    hr              = Column(Float,   nullable=True)    # 心拍数 (bpm)
    speed_mps       = Column(Float,   nullable=True)    # 速度 (m/s)
    distance_cum_m  = Column(Float,   nullable=True)    # 累積距離 (m)
    cadence         = Column(Float,   nullable=True)    # ケイデンス (rpm)
    power_w         = Column(Float,   nullable=True)    # パワー (W)
    # 特徴量（前処理済み）
    fatigue_index   = Column(Float,   nullable=True)    # 経過時間 / 総時間 (0〜1)
    cum_gain_m      = Column(Float,   nullable=True)    # 累積獲得標高 (m)


class DailyMetrics(Base):
    """
    日次の健康指標。Apple Health / Garmin から取得する。

    リカバリースコア・パフォーマンス予測・トレーニング負荷の
    ML 学習に使用する。
    """
    __tablename__ = "daily_metrics"

    id                  = Column(Integer, primary_key=True, index=True)
    user_id             = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    date                = Column(Date,    nullable=False, index=True)
    # 心臓関連
    resting_hr          = Column(Float, nullable=True)   # 安静時心拍 (bpm)
    hrv_rmssd           = Column(Float, nullable=True)   # HRV SDNN/RMSSD (ms)
    spo2_pct            = Column(Float, nullable=True)   # 血中酸素濃度 (%)
    vo2max              = Column(Float, nullable=True)   # VO2max (mL/kg/min)
    # 睡眠
    sleep_hours         = Column(Float, nullable=True)   # 総睡眠時間 (h)
    sleep_quality_score = Column(Float, nullable=True)   # 睡眠品質スコア 0-100
    # 活動量
    steps               = Column(Integer, nullable=True) # 歩数
    # トレーニング負荷（計算値）
    atl                 = Column(Float, nullable=True)   # Acute Training Load (7日EMA)
    ctl                 = Column(Float, nullable=True)   # Chronic Training Load (42日EMA)
    tsb                 = Column(Float, nullable=True)   # Training Stress Balance = CTL - ATL
    # ML 予測値
    recovery_score      = Column(Float, nullable=True)   # リカバリースコア 0-100
    readiness_score     = Column(Float, nullable=True)   # パフォーマンス準備度 0-100
    created_at          = Column(DateTime(timezone=True), server_default=func.now())


class PredictionFeedback(Base):
    """
    予測値と実績値のフィードバック記録。
    予測精度フィードバックループに使用する。
    """
    __tablename__ = "prediction_feedback"

    id                 = Column(Integer, primary_key=True, index=True)
    user_id            = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    activity_id        = Column(Integer, ForeignKey("activities.id"), nullable=True, index=True)
    prediction_type    = Column(String(30), nullable=False)
    # "hr" | "pace" | "completion" | "total_kcal" | "eta_min"
    predicted_value    = Column(Float, nullable=False)
    predicted_at       = Column(DateTime(timezone=True), nullable=False)
    actual_value       = Column(Float, nullable=True)
    feedback_source    = Column(String(20), nullable=True)  # "manual" | "garmin" | "apple_health"
    feedback_at        = Column(DateTime(timezone=True), nullable=True)
    error_abs          = Column(Float, nullable=True)   # |actual - predicted|
    error_pct          = Column(Float, nullable=True)   # error_abs / predicted * 100
    notes              = Column(Text, nullable=True)
    created_at         = Column(DateTime(timezone=True), server_default=func.now())


class TrainingPlan(Base):
    """
    自動生成されたトレーニングプラン。
    generate_training_plan() の出力を JSON で保存する。
    """
    __tablename__ = "training_plans"

    id               = Column(Integer, primary_key=True, index=True)
    user_id          = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    race_name        = Column(String(200), nullable=False)
    race_date        = Column(Date,  nullable=False)
    race_distance_km = Column(Float, nullable=False)
    activity_type    = Column(String(30), nullable=False)
    current_ctl      = Column(Float, nullable=True)
    target_ctl       = Column(Float, nullable=True)
    plan_json        = Column(Text,  nullable=False)   # TrainingPlan.to_dict() の JSON
    created_at       = Column(DateTime(timezone=True), server_default=func.now())


class SavedRoute(Base):
    """ルートライブラリ — GPXをタグ付きで保存する。"""
    __tablename__ = "saved_routes"

    id               = Column(Integer, primary_key=True, index=True)
    user_id          = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name             = Column(String(100), nullable=False)
    description      = Column(Text, nullable=True)
    tags             = Column(String(500), nullable=True)    # カンマ区切り
    route_json       = Column(Text, nullable=False)          # GPX解析結果JSON
    distance_m       = Column(Float, nullable=True)
    elevation_gain_m = Column(Float, nullable=True)
    activity_type    = Column(String(30), nullable=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())


class SharedRoute(Base):
    """共有ルートトークン — URLシェア用。"""
    __tablename__ = "shared_routes"

    id               = Column(Integer, primary_key=True, index=True)
    user_id          = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token            = Column(String(64), nullable=False, unique=True, index=True)
    title            = Column(String(200), nullable=True)
    route_json       = Column(Text, nullable=False)
    supply_plan_json = Column(Text, nullable=True)
    expires_at       = Column(DateTime(timezone=True), nullable=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())


class PeerStatsCache(Base):
    """
    同条件ユーザーの集団統計キャッシュ。
    ピア比較機能に使用する（最低5人未満は非表示）。
    """
    __tablename__ = "peer_stats_cache"

    id              = Column(Integer, primary_key=True)
    segment_key     = Column(String(100), nullable=False, unique=True, index=True)
    # 例: "hiking_20-40km_800-1500m"
    n_users         = Column(Integer, nullable=False)
    avg_pace_mps    = Column(Float, nullable=True)
    median_hr       = Column(Float, nullable=True)
    p25_duration    = Column(Float, nullable=True)
    p50_duration    = Column(Float, nullable=True)
    p75_duration    = Column(Float, nullable=True)
    avg_calories    = Column(Float, nullable=True)
    avg_elevation_gain_m = Column(Float, nullable=True)
    updated_at      = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
