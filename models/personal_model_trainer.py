"""
models/personal_model_trainer.py — 個人化インクリメンタル学習モデル
=====================================================================
アクティビティが追加されるたびに全履歴データで再学習し、
個人に特化した HR 予測・ペース予測モデルを構築する。

設計方針:
  - データが少ない段階（1〜4件）: BayesianRidge（過学習しにくい）
  - 5件以上: GradientBoostingRegressor（非線形な個人特性を捉える）
  - 全アクティビティの ActivityPoint を特徴量として使用
  - モデルは data/models/{user_id}/ に保存し、次回起動で再利用

HR 予測モデル:
  特徴量: slope_pct, speed_mps, elevation_norm, fatigue_index, cum_gain_norm
  目的変数: hr (bpm)

ペース予測モデル:
  特徴量: slope_pct, hr, elevation_norm, fatigue_index
  目的変数: speed_mps

参考: Pfitzinger & Douglas (2009) "Advanced Marathon Racing"
     Billat et al. (2003) "Training and bioenergetics characteristics in elite"
"""

from __future__ import annotations

import json
import os
import pickle
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.linear_model import BayesianRidge
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import cross_val_score

from models.health_context_features import HealthContextBuilder, DayContext


# ── パス設定 ──────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR   = os.path.join(PROJECT_ROOT, "data", "models")

# ── 特徴量セット定義 ──────────────────────────────────
# v1: アクティビティ時系列のみ（コンテキストなし）
_HR_FEATURES_V1   = ["slope_pct", "speed_mps", "elevation_norm",
                      "fatigue_index", "cum_gain_norm"]
_PACE_FEATURES_V1 = ["slope_pct", "hr", "elevation_norm", "fatigue_index"]

# v2: Apple Health コンテキスト追加（resting_hr_ratio, hrv_zscore, sleep_score_norm）
_HR_FEATURES_V2   = _HR_FEATURES_V1 + ["resting_hr_ratio", "hrv_zscore", "sleep_score_norm"]
_PACE_FEATURES_V2 = _PACE_FEATURES_V1 + ["resting_hr_ratio", "hrv_zscore"]

# v3: 気象コンテキスト追加（temp_c_norm, humidity_norm）
# 気温はHR に直接影響する（暑熱環境 → 心拍上昇）
_HR_FEATURES_V3   = _HR_FEATURES_V2 + ["temp_c_norm", "humidity_norm"]
_PACE_FEATURES_V3 = _PACE_FEATURES_V2 + ["temp_c_norm"]

# コンテキスト特徴量のデフォルト値（データなし時のフォールバック）
_CTX_DEFAULTS = {
    "resting_hr_ratio": 1.0,
    "hrv_zscore":       0.0,
    "sleep_score_norm": 0.5,
    "temp_c_norm":      0.67,   # 20℃ / 30℃ = 0.67（標準的な気温）
    "humidity_norm":    0.60,   # 60%（標準的な湿度）
}


# ══════════════════════════════════════════════════════
# モデル学習ログ
# ══════════════════════════════════════════════════════

@dataclass
class TrainingLog:
    """モデルの学習履歴エントリ。"""
    timestamp:        str
    n_activities:     int
    n_points:         int
    hr_model_type:    str
    hr_mae:           float       # 平均絶対誤差 (bpm)
    hr_r2:            float       # 決定係数
    pace_model_type:  str
    pace_mae:         float       # 平均絶対誤差 (m/s)
    pace_r2:          float
    feature_version:  int = 1     # 1=基本特徴量 / 2=健康コンテキスト追加
    n_context_days:   int = 0     # コンテキスト特徴量が有効だった日数


# ══════════════════════════════════════════════════════
# 個人化モデルトレーナー
# ══════════════════════════════════════════════════════

class PersonalModelTrainer:
    """
    ユーザーごとの HR・ペース予測モデルを管理するクラス。

    使用例:
        trainer = PersonalModelTrainer(user_id=1)
        trainer.train(points_df)            # 新アクティビティ追加後に呼ぶ
        hr = trainer.predict_hr(features)
        speed = trainer.predict_pace(features)
    """

    GB_THRESHOLD = 5   # これ以上のアクティビティ数で GBT に切り替え

    def __init__(self, user_id: int):
        self.user_id   = user_id
        self._model_dir = os.path.join(MODELS_DIR, str(user_id))
        os.makedirs(self._model_dir, exist_ok=True)

        self._hr_pipeline:    Optional[Pipeline] = None
        self._pace_pipeline:  Optional[Pipeline] = None
        self._n_activities:   int = 0
        self._training_log:   list[dict] = []
        self._feature_version: int = 1   # 保存済みモデルの特徴量バージョン

        self._load_models()

    # ── 学習 ──────────────────────────────────────────

    def train(
        self,
        points_df: pd.DataFrame,
        n_activities: int,
        context_builder: Optional[HealthContextBuilder] = None,
    ) -> TrainingLog:
        """
        全アクティビティポイントの DataFrame でモデルを再学習する。

        Parameters
        ----------
        points_df : pd.DataFrame
            全 ActivityPoint を結合した DataFrame。
            必須カラム: slope_pct, speed_mps, elevation, hr, fatigue_index,
                       cum_gain_m, elapsed_sec, activity_id
            オプション: activity_date（コンテキスト特徴量付与に使用）
        n_activities : int
            学習に使用したアクティビティ件数（モデル切り替え判定に使用）
        context_builder : HealthContextBuilder | None
            Apple Health デイリーメトリクスから構築したコンテキストビルダー。
            渡すと特徴量 v2（健康コンテキスト追加）でモデルを学習する。
        """
        self._n_activities = n_activities
        use_gbt = n_activities >= self.GB_THRESHOLD

        df = self._prepare_features(points_df, context_builder)
        if len(df) < 10:
            raise ValueError("学習データが不足しています（最低10ポイント必要）")

        # コンテキスト特徴量が有効かどうかを判定
        has_context = (
            context_builder is not None
            and "resting_hr_ratio" in df.columns
            and df["resting_hr_ratio"].notna().any()
        )
        # 気象特徴量 (v3) が有効かどうかを判定
        has_weather = (
            has_context
            and "temp_c_norm" in df.columns
            and df["temp_c_norm"].notna().any()
            and (df["temp_c_norm"] != _CTX_DEFAULTS["temp_c_norm"]).any()
        )
        if has_weather:
            feature_version = 3
            hr_features     = _HR_FEATURES_V3
            pace_features   = _PACE_FEATURES_V3
        elif has_context:
            feature_version = 2
            hr_features     = _HR_FEATURES_V2
            pace_features   = _PACE_FEATURES_V2
        else:
            feature_version = 1
            hr_features     = _HR_FEATURES_V1
            pace_features   = _PACE_FEATURES_V1
        n_context_days = int(df["resting_hr_ratio"].notna().sum()) if has_context else 0

        # ── HR 予測モデル ──────────────────────────
        hr_df = df.dropna(subset=hr_features + ["hr"])
        hr_df = hr_df[(hr_df["hr"] >= 40) & (hr_df["hr"] <= 230)]

        if len(hr_df) >= 10:
            X_hr = hr_df[hr_features].values
            y_hr = hr_df["hr"].values
            self._hr_pipeline = self._build_pipeline(use_gbt, task="hr", n_features=len(hr_features))
            self._hr_pipeline.fit(X_hr, y_hr)
            hr_mae, hr_r2 = self._evaluate(self._hr_pipeline, X_hr, y_hr)
        else:
            hr_mae, hr_r2 = float("nan"), float("nan")

        # ── ペース予測モデル ────────────────────────
        pace_df = df.dropna(subset=pace_features + ["speed_mps"])
        pace_df = pace_df[(pace_df["speed_mps"] > 0.05) & (pace_df["speed_mps"] < 10.0)]

        if len(pace_df) >= 10:
            X_pace = pace_df[pace_features].values
            y_pace = pace_df["speed_mps"].values
            self._pace_pipeline = self._build_pipeline(use_gbt, task="pace", n_features=len(pace_features))
            self._pace_pipeline.fit(X_pace, y_pace)
            pace_mae, pace_r2 = self._evaluate(self._pace_pipeline, X_pace, y_pace)
        else:
            pace_mae, pace_r2 = float("nan"), float("nan")

        # ── 保存 & ログ ─────────────────────────────
        self._feature_version = feature_version
        self._save_models()
        model_type = "GradientBoosting" if use_gbt else "BayesianRidge"
        log = TrainingLog(
            timestamp       = datetime.utcnow().isoformat(),
            n_activities    = n_activities,
            n_points        = len(df),
            hr_model_type   = model_type,
            hr_mae          = round(hr_mae, 2),
            hr_r2           = round(hr_r2, 3),
            pace_model_type = model_type,
            pace_mae        = round(pace_mae, 4),
            pace_r2         = round(pace_r2, 3),
            feature_version = feature_version,
            n_context_days  = n_context_days,
        )
        self._training_log.append(asdict(log))
        self._save_log()
        return log

    # ── 予測 ──────────────────────────────────────────

    def predict_hr(
        self,
        slope_pct: float,
        speed_mps: float,
        elevation: float,
        fatigue_index: float,
        cum_gain_m: float,
        elev_min: float = 0.0,
        elev_max: float = 4000.0,
        gain_max: float = 2000.0,
        context: Optional[DayContext] = None,
    ) -> Optional[float]:
        """
        斜度・速度・標高・疲労度から心拍数を予測する。

        Parameters
        ----------
        context : DayContext | None
            当日の健康コンテキスト。v2 モデルで予測する際に使用する。
            None の場合はデフォルト値（体調標準状態）を使用する。
        """
        if self._hr_pipeline is None:
            return None
        elev_norm     = _normalize(elevation, elev_min, elev_max)
        cum_gain_norm = _normalize(cum_gain_m, 0, gain_max)

        if self._feature_version == 3:
            ctx = context or DayContext()
            temp_c = getattr(ctx, "temp_c", 20.0) or 20.0
            humidity = getattr(ctx, "humidity_pct", 60.0) or 60.0
            temp_c_norm = float(np.clip((temp_c + 10.0) / 50.0, 0.0, 1.0))
            humidity_norm = float(np.clip(humidity / 100.0, 0.0, 1.0))
            X = np.array([[
                slope_pct, speed_mps, elev_norm, fatigue_index, cum_gain_norm,
                ctx.resting_hr_ratio, ctx.hrv_zscore, ctx.sleep_score_norm,
                temp_c_norm, humidity_norm,
            ]])
        elif self._feature_version == 2:
            ctx = context or DayContext()
            X = np.array([[
                slope_pct, speed_mps, elev_norm, fatigue_index, cum_gain_norm,
                ctx.resting_hr_ratio, ctx.hrv_zscore, ctx.sleep_score_norm,
            ]])
        else:
            X = np.array([[slope_pct, speed_mps, elev_norm, fatigue_index, cum_gain_norm]])

        try:
            return float(np.clip(self._hr_pipeline.predict(X)[0], 40, 230))
        except ValueError:
            # 保存済みモデルと特徴量数が合わない場合（再学習が必要）
            return None

    def predict_pace(
        self,
        slope_pct: float,
        hr: float,
        elevation: float,
        fatigue_index: float,
        elev_min: float = 0.0,
        elev_max: float = 4000.0,
        context: Optional[DayContext] = None,
    ) -> Optional[float]:
        """
        斜度・心拍・標高・疲労度から速度(m/s)を予測する。

        Parameters
        ----------
        context : DayContext | None
            当日の健康コンテキスト。v2 モデルで予測する際に使用する。
        """
        if self._pace_pipeline is None:
            return None
        elev_norm = _normalize(elevation, elev_min, elev_max)

        if self._feature_version == 3:
            ctx = context or DayContext()
            temp_c = getattr(ctx, "temp_c", 20.0) or 20.0
            temp_c_norm = float(np.clip((temp_c + 10.0) / 50.0, 0.0, 1.0))
            X = np.array([[
                slope_pct, hr, elev_norm, fatigue_index,
                ctx.resting_hr_ratio, ctx.hrv_zscore, temp_c_norm,
            ]])
        elif self._feature_version == 2:
            ctx = context or DayContext()
            X = np.array([[
                slope_pct, hr, elev_norm, fatigue_index,
                ctx.resting_hr_ratio, ctx.hrv_zscore,
            ]])
        else:
            X = np.array([[slope_pct, hr, elev_norm, fatigue_index]])

        try:
            return float(np.clip(self._pace_pipeline.predict(X)[0], 0.0, 10.0))
        except ValueError:
            return None

    # ── ステータス ────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """学習済みモデルが存在するか。"""
        return self._hr_pipeline is not None or self._pace_pipeline is not None

    @property
    def training_log(self) -> list[dict]:
        return self._training_log

    @property
    def latest_metrics(self) -> Optional[dict]:
        return self._training_log[-1] if self._training_log else None

    def get_feature_importances(self) -> dict:
        """
        GBT モデルの feature_importances_ を返す。
        BayesianRidge の場合は係数絶対値を正規化して返す。
        モデル未学習の場合は空の dict を返す。
        """
        result = {}

        def _extract(pipeline, features, name):
            if pipeline is None:
                return None
            model = pipeline.named_steps.get("model")
            if model is None:
                return None

            if hasattr(model, "feature_importances_"):
                importances = model.feature_importances_.tolist()
                model_type = "GradientBoosting"
            elif hasattr(model, "coef_"):
                coef = np.abs(model.coef_)
                total = coef.sum()
                importances = (coef / max(total, 1e-10)).tolist()
                model_type = "BayesianRidge"
            else:
                return None

            # feature と importances のリスト長を合わせる（安全策）
            n = min(len(features), len(importances))
            paired = sorted(
                zip(features[:n], importances[:n]),
                key=lambda x: x[1],
                reverse=True,
            )
            return {
                "features":    [p[0] for p in paired],
                "importances": [round(p[1], 4) for p in paired],
                "model_type":  model_type,
                "target":      name,
            }

        hr_features   = _HR_FEATURES_V2   if self._feature_version == 2 else _HR_FEATURES_V1
        pace_features = _PACE_FEATURES_V2 if self._feature_version == 2 else _PACE_FEATURES_V1

        hr_info   = _extract(self._hr_pipeline,   hr_features,   "hr_bpm")
        pace_info = _extract(self._pace_pipeline, pace_features, "speed_mps")

        if hr_info:
            result["hr"] = hr_info
        if pace_info:
            result["pace"] = pace_info

        result["feature_version"] = self._feature_version
        result["n_activities"]    = self._n_activities
        return result

    def model_accuracy_trend(self) -> dict:
        """
        モデル精度の推移を返す（「データが増えるほど精度向上」の可視化用）。
        """
        if not self._training_log:
            return {"hr_mae": [], "pace_mae": [], "n_activities": []}

        return {
            "n_activities": [e["n_activities"] for e in self._training_log],
            "hr_mae":       [e["hr_mae"] for e in self._training_log],
            "hr_r2":        [e["hr_r2"] for e in self._training_log],
            "pace_mae":     [e["pace_mae"] for e in self._training_log],
            "pace_r2":      [e["pace_r2"] for e in self._training_log],
            "model_type":   [e["hr_model_type"] for e in self._training_log],
        }

    # ── 内部メソッド ──────────────────────────────────

    def _prepare_features(
        self,
        df: pd.DataFrame,
        context_builder: Optional[HealthContextBuilder] = None,
    ) -> pd.DataFrame:
        """生 DataFrame に正規化特徴量を追加する。"""
        out = df.copy()

        elev_col = "elevation"
        if elev_col in out.columns:
            elev_min = out[elev_col].min()
            elev_max = out[elev_col].max()
            rng = max(elev_max - elev_min, 1.0)
            out["elevation_norm"] = (out[elev_col] - elev_min) / rng
        else:
            out["elevation_norm"] = 0.0

        if "cum_gain_m" in out.columns:
            gain_max = max(out["cum_gain_m"].max(), 1.0)
            out["cum_gain_norm"] = out["cum_gain_m"] / gain_max
        else:
            out["cum_gain_norm"] = 0.0

        if "fatigue_index" not in out.columns:
            if "elapsed_sec" in out.columns and "activity_id" in out.columns:
                out["fatigue_index"] = out.groupby("activity_id")["elapsed_sec"].transform(
                    lambda x: x / max(x.max(), 1.0)
                )
            else:
                out["fatigue_index"] = 0.5

        # ── コンテキスト特徴量の付与 ─────────────────────
        if context_builder is not None and "activity_date" in out.columns:
            # アクティビティ日ごとにコンテキストを計算してマッピング
            unique_dates = out["activity_date"].dropna().unique()
            ctx_map: dict = {}
            for d in unique_dates:
                ctx = context_builder.get_context_for_date(d)
                ctx_map[d] = {
                    "resting_hr_ratio": ctx.resting_hr_ratio,
                    "hrv_zscore":       ctx.hrv_zscore,
                    "sleep_score_norm": ctx.sleep_score_norm,
                }

            for col in ["resting_hr_ratio", "hrv_zscore", "sleep_score_norm"]:
                default = _CTX_DEFAULTS[col]
                out[col] = out["activity_date"].map(
                    lambda d, c=col: ctx_map.get(d, {}).get(c, default)
                )
        else:
            for col in ["resting_hr_ratio", "hrv_zscore", "sleep_score_norm"]:
                if col not in out.columns:
                    out[col] = _CTX_DEFAULTS[col]

        # ── 気象特徴量の付与 (v3) ─────────────────────
        # temp_c_norm: 気温を 0-1 に正規化 (0℃→0.33, 20℃→0.67, 35℃→1.0)
        if "avg_temp_c" in out.columns and out["avg_temp_c"].notna().any():
            out["temp_c_norm"] = (out["avg_temp_c"].fillna(20.0) + 10.0) / 50.0
            out["temp_c_norm"] = out["temp_c_norm"].clip(0.0, 1.0)
        else:
            out["temp_c_norm"] = _CTX_DEFAULTS["temp_c_norm"]

        if "humidity_pct" in out.columns and out["humidity_pct"].notna().any():
            out["humidity_norm"] = out["humidity_pct"].fillna(60.0) / 100.0
            out["humidity_norm"] = out["humidity_norm"].clip(0.0, 1.0)
        else:
            out["humidity_norm"] = _CTX_DEFAULTS["humidity_norm"]

        return out

    def _build_pipeline(self, use_gbt: bool, task: str, n_features: int = 5) -> Pipeline:
        if use_gbt:
            params = _GBT_PARAMS.get(task, _GBT_PARAMS["hr"])
            model = GradientBoostingRegressor(**params, random_state=42)
        else:
            model = BayesianRidge()
        return Pipeline([("scaler", StandardScaler()), ("model", model)])

    def _evaluate(self, pipeline: Pipeline, X: np.ndarray, y: np.ndarray) -> tuple[float, float]:
        """CV なしでトレーニングデータで誤差計算（データ少量時は CV 不安定）。"""
        n = len(X)
        if n >= 50:
            try:
                cv = min(5, n // 10)
                scores_mae = -cross_val_score(
                    pipeline, X, y, cv=cv, scoring="neg_mean_absolute_error"
                )
                scores_r2  = cross_val_score(pipeline, X, y, cv=cv, scoring="r2")
                return float(scores_mae.mean()), float(scores_r2.mean())
            except Exception:
                pass
        y_pred = pipeline.predict(X)
        return float(mean_absolute_error(y, y_pred)), float(r2_score(y, y_pred))

    def _save_models(self):
        if self._hr_pipeline is not None:
            _dump(self._hr_pipeline, os.path.join(self._model_dir, "hr_model.pkl"))
        if self._pace_pipeline is not None:
            _dump(self._pace_pipeline, os.path.join(self._model_dir, "pace_model.pkl"))
        meta = {
            "n_activities":    self._n_activities,
            "updated_at":      datetime.utcnow().isoformat(),
            "feature_version": self._feature_version,
        }
        with open(os.path.join(self._model_dir, "meta.json"), "w") as f:
            json.dump(meta, f)

    def _load_models(self):
        hr_path   = os.path.join(self._model_dir, "hr_model.pkl")
        pace_path = os.path.join(self._model_dir, "pace_model.pkl")
        log_path  = os.path.join(self._model_dir, "training_log.json")
        meta_path = os.path.join(self._model_dir, "meta.json")

        self._hr_pipeline   = _load(hr_path)
        self._pace_pipeline = _load(pace_path)

        if os.path.exists(log_path):
            with open(log_path) as f:
                self._training_log = json.load(f)

        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
                self._n_activities    = meta.get("n_activities", 0)
                self._feature_version = meta.get("feature_version", 1)

    def _save_log(self):
        log_path = os.path.join(self._model_dir, "training_log.json")
        with open(log_path, "w") as f:
            json.dump(self._training_log, f, indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════════════
# DB からの特徴量取得ヘルパー
# ══════════════════════════════════════════════════════

def load_points_from_db(
    user_id: int,
    db_session,
    with_context: bool = True,
) -> tuple[pd.DataFrame, int, Optional[HealthContextBuilder]]:
    """
    DB の ActivityPoint + Activity + DailyMetrics テーブルから全データを取得し、
    学習用 DataFrame を構築する。

    Parameters
    ----------
    with_context : bool
        True の場合、DailyMetrics も取得して HealthContextBuilder を構築する。

    Returns
    -------
    (points_df, n_activities, context_builder)
        context_builder は with_context=True かつデータがある場合のみ非 None。
    """
    from backend.models_db import Activity, ActivityPoint, DailyMetrics

    activities = (
        db_session.query(Activity)
        .filter(Activity.user_id == user_id)
        .all()
    )
    if not activities:
        return pd.DataFrame(), 0, None

    act_ids  = [a.id for a in activities]
    act_meta = {
        a.id: {
            "duration_sec":  a.duration_sec,
            "activity_date": a.start_time.date() if a.start_time else None,
            "avg_temp_c":    a.avg_temp_c,
            "humidity_pct":  getattr(a, "humidity_pct", None),  # v3 weather feature
        }
        for a in activities
    }

    points = (
        db_session.query(ActivityPoint)
        .filter(ActivityPoint.activity_id.in_(act_ids))
        .all()
    )
    if not points:
        return pd.DataFrame(), len(activities), None

    rows = []
    for p in points:
        meta = act_meta.get(p.activity_id, {})
        dur  = meta.get("duration_sec", 1.0) or 1.0
        rows.append({
            "activity_id":    p.activity_id,
            "activity_date":  meta.get("activity_date"),
            "elapsed_sec":    p.elapsed_sec,
            "elevation":      p.elevation,
            "slope_pct":      p.slope_pct,
            "hr":             p.hr,
            "speed_mps":      p.speed_mps,
            "distance_cum_m": p.distance_cum_m,
            "fatigue_index":  p.fatigue_index if p.fatigue_index is not None
                              else (p.elapsed_sec / dur),
            "cum_gain_m":     p.cum_gain_m,
            # v3 weather features (activity-level, constant within activity)
            "avg_temp_c":     meta.get("avg_temp_c"),
            "humidity_pct":   meta.get("humidity_pct"),
        })

    df = pd.DataFrame(rows)

    # ── HealthContextBuilder の構築 ──────────────────
    context_builder: Optional[HealthContextBuilder] = None
    if with_context:
        daily_metrics = (
            db_session.query(DailyMetrics)
            .filter(DailyMetrics.user_id == user_id)
            .order_by(DailyMetrics.date)
            .all()
        )
        if daily_metrics:
            context_builder = HealthContextBuilder(daily_metrics)
            # 全アクティビティの最大心拍を記録して hr_max 推定精度を向上
            for a in activities:
                if a.max_hr:
                    context_builder.update_observed_hr_max(a.max_hr)

    return df, len(activities), context_builder


def store_activity_points(
    activity_id: int,
    points_df: pd.DataFrame,
    duration_sec: float,
    db_session,
):
    """
    時系列 DataFrame を ActivityPoint テーブルに一括挿入する。
    10秒ごとのデータを間引いて保存（ストレージ削減）。
    """
    from backend.models_db import ActivityPoint

    if points_df is None or points_df.empty:
        return

    # 10秒間隔にダウンサンプリング（既に10秒未満なら間引く）
    df = points_df.copy()
    if "elapsed_sec" in df.columns:
        df = df[df["elapsed_sec"] % 10 < 1.0].copy()  # 10秒ごとに近い点を選ぶ
    if len(df) > 5000:
        step = len(df) // 5000 + 1
        df = df.iloc[::step]

    # 累積獲得標高を計算
    elev = df.get("elevation", pd.Series(dtype=float))
    if len(elev) > 1:
        diffs = elev.diff().fillna(0)
        cum_gain = diffs.clip(lower=0).cumsum()
    else:
        cum_gain = pd.Series([0.0] * len(df))

    for i, (_, row) in enumerate(df.iterrows()):
        elapsed = float(row.get("elapsed_sec", i * 10))
        pt = ActivityPoint(
            activity_id    = activity_id,
            elapsed_sec    = elapsed,
            lat            = _fval(row.get("lat")),
            lon            = _fval(row.get("lon")),
            elevation      = _fval(row.get("elevation")),
            slope_pct      = _fval(row.get("slope_pct")),
            hr             = _fval(row.get("hr")),
            speed_mps      = _fval(row.get("speed_mps")),
            distance_cum_m = _fval(row.get("distance_cum_m")),
            cadence        = _fval(row.get("cadence")),
            power_w        = _fval(row.get("power_w")),
            fatigue_index  = elapsed / max(duration_sec, 1.0),
            cum_gain_m     = float(cum_gain.iloc[i]) if i < len(cum_gain) else None,
        )
        db_session.add(pt)

    db_session.commit()


# ── パラメータ定数 ────────────────────────────────────

_GBT_PARAMS = {
    "hr": {
        "n_estimators": 200,
        "max_depth": 4,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "min_samples_leaf": 10,
    },
    "pace": {
        "n_estimators": 200,
        "max_depth": 4,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "min_samples_leaf": 10,
    },
}


# ── ユーティリティ ────────────────────────────────────

def _normalize(v: float, vmin: float, vmax: float) -> float:
    rng = max(vmax - vmin, 1.0)
    return (v - vmin) / rng


def _fval(v) -> Optional[float]:
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _dump(obj, path: str):
    with open(path, "wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)


def _load(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None
