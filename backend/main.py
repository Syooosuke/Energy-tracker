"""
backend/main.py — Energy Tracker FastAPI Server
起動: uvicorn backend.main:app --reload --port 8000
"""

import os, sys, io, json, logging, datetime as _dt, time, asyncio
from typing import Annotated, List, Literal, Optional
from collections import defaultdict
import numpy as np
import httpx
from dotenv import load_dotenv

# ── ロギング設定 ───────────────────────────────────────
# クライアントには汎用メッセージのみ返し、詳細はサーバーログに記録する。
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt = "%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("energy_tracker")

# プロジェクトルートの .env を読み込む（存在しなくてもエラーにしない）
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "models"))

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, Depends, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy.orm import Session
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from backend.database  import get_db, init_db, SessionLocal
from backend.models_db import User
from backend.auth      import (
    hash_password, verify_password, create_token,
    get_current_user, get_current_user_optional,
    set_auth_cookie, clear_auth_cookie,
)

from ke_estimator        import UserProfile, ACTIVITY_HIKING, ACTIVITY_RUNNING, ACTIVITY_CYCLING
from pk_model_scipy      import SupplyEvent, AlertConfig, run_pk_model
from route_planner       import (
    search_place, get_route_ors, get_route_fallback, get_route_graphhopper,
    parse_gpx_route, coords_to_slope_array, ACTIVITY_PROFILES, ACTIVITY_TO_SIM,
)
from weather             import fetch_weather_forecast, calc_weather_factor, WeatherData
from fatigue_model       import FatigueModel
from hr_zone_estimator   import HRZoneEstimator
from completion_predictor import CompletionPredictor
from anomaly_detector    import AnomalyDetector
from supply_optimizer    import optimize_supply_plan, OptimizeResult
from race_simulator      import simulate_race, RaceSimResult
from training_plan_generator import generate_training_plan
from ai_query            import AIQueryEngine, QueryContext

# ──────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────
ORS_KEY = os.environ.get("ORS_API_KEY", "")
OWM_KEY = os.environ.get("OWM_API_KEY", "")

if not ORS_KEY:
    import warnings
    warnings.warn("ORS_API_KEY が未設定です。ルート取得はフォールバック（直線近似）になります。")
if not OWM_KEY:
    import warnings
    warnings.warn("OWM_API_KEY が未設定です。天気情報はデフォルト値を使用します。")

FOOD_MASTER = {
    "エネルギージェル": {"kcal_per": 100, "ka": 2.0},
    "おにぎり (梅)":   {"kcal_per": 170, "ka": 0.9},
    "バナナ":          {"kcal_per":  86, "ka": 0.8},
    "カロリーメイト":  {"kcal_per": 100, "ka": 0.6},
    "スポーツドリンク":{"kcal_per":  50, "ka": 1.8},
    "羊羹":            {"kcal_per": 125, "ka": 0.7},
    "ゼリー飲料":      {"kcal_per":  90, "ka": 1.5},
}

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title="Energy Tracker API",
    # 本番では /docs /redoc /openapi.json を無効化（情報漏洩防止）
    docs_url    = "/docs"     if os.environ.get("DEBUG") else None,
    redoc_url   = "/redoc"   if os.environ.get("DEBUG") else None,
    openapi_url = "/openapi.json" if os.environ.get("DEBUG") else None,
)

# ── セキュリティヘッダー ───────────────────────────────
_IS_PROD = not os.environ.get("DEBUG")

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """全レスポンスにセキュリティヘッダーを付与する。"""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # クリックジャッキング防止
        response.headers["X-Frame-Options"] = "DENY"
        # MIMEスニッフィング防止
        response.headers["X-Content-Type-Options"] = "nosniff"
        # リファラー情報の最小化
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # XSS フィルタ（古いブラウザ向け）
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # HTTPS 強制（本番のみ）
        if _IS_PROD:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # CSP: フロントエンド配信も兼ねるため適切なソースを許可
        if request.url.path.startswith(("/api/", "/auth/")):
            # API エンドポイントは引き続き厳格に
            response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        else:
            # フロントエンド用: JS/CSS/フォント/API接続を許可
            # 'unsafe-inline' を script-src から除外（sw-register.js を外部ファイル化済み）
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "img-src 'self' data: blob:; "
                "connect-src 'self' https://*.openrouteservice.org https://*.openweathermap.org https://overpass-api.de; "
                "manifest-src 'self'; "
                "worker-src blob:; "
                "frame-ancestors 'none'"
            )
        # キャッシュ制御
        if request.url.path.startswith(("/auth/", "/api/")):
            # 認証情報を含むレスポンスはキャッシュしない
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/assets/"):
            # Vite がコンテンツハッシュ付きファイル名を生成するため長期キャッシュ可能
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

app.add_middleware(SecurityHeadersMiddleware)

# ── ログインブルートフォース対策（インメモリ）─────────
# {ip: {"count": int, "locked_until": float}}
_login_failures: dict = defaultdict(lambda: {"count": 0, "locked_until": 0.0})
_LOCK_THRESHOLD   = 10     # 10回失敗でロック
_LOCK_DURATION_S  = 300    # 5分間ロック

def _check_login_lock(ip: str):
    rec = _login_failures[ip]
    if time.monotonic() < rec["locked_until"]:
        remaining = int(rec["locked_until"] - time.monotonic())
        raise HTTPException(
            status_code=429,
            detail=f"ログイン試行回数が上限を超えました。{remaining}秒後に再試行してください。",
        )

def _record_login_failure(ip: str):
    rec = _login_failures[ip]
    rec["count"] += 1
    if rec["count"] >= _LOCK_THRESHOLD:
        rec["locked_until"] = time.monotonic() + _LOCK_DURATION_S
        rec["count"] = 0
        logger.warning("Login locked for IP %s", ip)

def _clear_login_failure(ip: str):
    _login_failures.pop(ip, None)

# 起動時刻（再起動検知用）
_BOOT_TIME = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

# ── ヘルスチェック ────────────────────────────────────────────────────
@app.get("/health", include_in_schema=False)
def health_check():
    """Render のヘルスチェック用エンドポイント。"""
    return {"status": "ok", "boot": _BOOT_TIME}

@app.on_event("startup")
def on_startup():
    """起動時にDBテーブルを作成し、カラム追加マイグレーションを実行する（冪等）。"""
    try:
        init_db()
        _run_column_migrations()
        logger.info("Database initialized.")
    except Exception as e:
        logger.error("Database initialization failed: %s — continuing without DB.", e)
        logger.warning("Auth/user features will be unavailable until DB is reachable.")


@app.get("/api/boot-time")
@limiter.limit("60/minute")
def get_boot_time(request: Request):
    """バックエンドの起動時刻を返す（フロントエンドの再起動検知用）。"""
    return {"boot_time": _BOOT_TIME}


def _run_column_migrations():
    """Alembic なし環境での冪等なカラム追加。SQLite/PostgreSQL 両対応。"""
    from backend.database import engine
    from sqlalchemy import text

    is_pg = engine.dialect.name == "postgresql"
    _auto = "SERIAL" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    _pk   = "GENERATED ALWAYS AS IDENTITY PRIMARY KEY" if is_pg else "PRIMARY KEY AUTOINCREMENT"
    _ts   = "TIMESTAMP" if is_pg else "DATETIME"

    sqlite_migrations = [
        "ALTER TABLE activities ADD COLUMN gpx_fingerprint VARCHAR(64)",
        "ALTER TABLE activities ADD COLUMN humidity_pct FLOAT",
        "ALTER TABLE activities ADD COLUMN wind_speed_ms FLOAT",
    ]
    pg_migrations = [
        "ALTER TABLE activities ADD COLUMN IF NOT EXISTS gpx_fingerprint VARCHAR(64)",
        "ALTER TABLE activities ADD COLUMN IF NOT EXISTS humidity_pct FLOAT",
        "ALTER TABLE activities ADD COLUMN IF NOT EXISTS wind_speed_ms FLOAT",
    ]
    alter_migrations = pg_migrations if is_pg else sqlite_migrations

    table_migrations = [
        f"CREATE TABLE IF NOT EXISTS training_plans ("
        f"  id {_auto},"
        f"  user_id INTEGER NOT NULL,"
        f"  race_name VARCHAR(200) NOT NULL,"
        f"  race_date DATE NOT NULL,"
        f"  race_distance_km FLOAT NOT NULL,"
        f"  activity_type VARCHAR(30) NOT NULL,"
        f"  current_ctl FLOAT,"
        f"  target_ctl FLOAT,"
        f"  plan_json TEXT NOT NULL,"
        f"  created_at {_ts} DEFAULT CURRENT_TIMESTAMP"
        f")",
        f"CREATE TABLE IF NOT EXISTS saved_routes ("
        f"  id {_auto},"
        f"  user_id INTEGER NOT NULL,"
        f"  name VARCHAR(100) NOT NULL,"
        f"  description TEXT,"
        f"  tags VARCHAR(500),"
        f"  route_json TEXT NOT NULL,"
        f"  distance_m FLOAT,"
        f"  elevation_gain_m FLOAT,"
        f"  activity_type VARCHAR(30),"
        f"  created_at {_ts} DEFAULT CURRENT_TIMESTAMP"
        f")",
        f"CREATE TABLE IF NOT EXISTS shared_routes ("
        f"  id {_auto},"
        f"  user_id INTEGER NOT NULL,"
        f"  token VARCHAR(64) NOT NULL UNIQUE,"
        f"  title VARCHAR(200),"
        f"  route_json TEXT NOT NULL,"
        f"  supply_plan_json TEXT,"
        f"  expires_at {_ts},"
        f"  created_at {_ts} DEFAULT CURRENT_TIMESTAMP"
        f")",
    ]

    with engine.connect() as conn:
        for sql in alter_migrations + table_migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # 既にカラム/テーブルが存在する場合は無視
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── タイルプロキシ（クロスオリジン問題を回避するため同一オリジンで配信）──
_tile_cache: dict[str, tuple[bytes, float]] = {}
_tile_http_client: httpx.AsyncClient | None = None

def _get_tile_http_client() -> httpx.AsyncClient:
    global _tile_http_client
    if _tile_http_client is None:
        _tile_http_client = httpx.AsyncClient(
            headers={"User-Agent": "EnergyTracker/1.0 (personal fitness app; https://github.com/Syooosuke/Energy-tracker)"},
            timeout=10.0,
            follow_redirects=True,
        )
    return _tile_http_client

@app.get("/api/tiles/{z}/{x}/{y}", include_in_schema=False)
async def proxy_map_tile(z: int, x: int, y: int):
    """OSM タイルをプロキシ配信。同一オリジンにすることでCSP/CORS問題を回避。"""
    import time
    from fastapi.responses import Response as RawResponse

    # ズームレベルと座標の妥当性チェック（任意URLへのプロキシ悪用を防ぐ）
    if not (0 <= z <= 20):
        raise HTTPException(status_code=400, detail="Invalid zoom level")
    max_coord = 2 ** z
    if not (0 <= x < max_coord and 0 <= y < max_coord):
        raise HTTPException(status_code=400, detail="Invalid tile coordinates")

    cache_key = f"{z}/{x}/{y}"
    now = time.monotonic()

    # キャッシュヒット（1時間有効）
    if cache_key in _tile_cache:
        data, expires = _tile_cache[cache_key]
        if now < expires:
            return RawResponse(content=data, media_type="image/png",
                               headers={"Cache-Control": "public, max-age=3600"})

    # OSM サブドメインを分散（a/b/c）
    subdomain = ("a", "b", "c")[hash(cache_key) % 3]
    url = f"https://{subdomain}.tile.openstreetmap.org/{z}/{x}/{y}.png"

    try:
        client = _get_tile_http_client()
        r = await client.get(url)
        if r.status_code != 200:
            raise HTTPException(status_code=404, detail="Tile not found")
        data = r.content
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Tile proxy error %s: %s", url, e)
        raise HTTPException(status_code=502, detail="Tile fetch failed")

    # キャッシュサイズ上限（1000タイル）を超えたら古いものを削除
    if len(_tile_cache) >= 1000:
        oldest = sorted(_tile_cache, key=lambda k: _tile_cache[k][1])[:200]
        for k in oldest:
            del _tile_cache[k]

    _tile_cache[cache_key] = (data, now + 3600)
    return RawResponse(content=data, media_type="image/png",
                       headers={"Cache-Control": "public, max-age=3600"})

# ── CORS ──────────────────────────────────────────────
# 許可オリジンは環境変数から読む。未設定時はローカル開発用にフォールバック。
# 本番デプロイ時は .env の ALLOWED_ORIGINS を実際のフロントエンドURLに変更すること。
_raw_origins = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
)
ALLOWED_ORIGINS: list[str] = [o.strip() for o in _raw_origins.split(",") if o.strip()]

# ワイルドカード "*" は全オリジンを許可してしまうため、明示的に禁止する。
if "*" in ALLOWED_ORIGINS:
    raise RuntimeError(
        "ALLOWED_ORIGINS に '*' を指定することは禁止されています。"
        " 具体的なオリジン（例: https://your-app.example.com）を指定してください。"
    )

# 内部IP正規表現は開発環境（DEBUG=1）のみ有効にする。
# 本番では明示的なオリジンリストのみ許可する。
_cors_origin_regex = (
    r"http://(localhost|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+)(:\d+)?"
    if os.environ.get("DEBUG")
    else None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins      = ALLOWED_ORIGINS,
    allow_origin_regex = _cors_origin_regex,
    allow_credentials  = True,   # HttpOnly Cookie 送受信に必要
    allow_methods      = ["GET", "POST", "DELETE"],
    allow_headers      = ["Content-Type", "Authorization"],
)

# ──────────────────────────────────────────────────────
# Pydantic models  （バリデーション付き）
# ──────────────────────────────────────────────────────

# アクティビティの許容値（フロントと一致させる）
ActivityKey = Literal[
    "🏔️ 登山・ハイキング",
    "🏃 ランニング",
    "🌲 トレイルランニング",
    "🚴 サイクリング",
    "🚵 マウンテンバイク",
]

class PlaceSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(5, ge=1, le=20)

class WayPoint(BaseModel):
    lat:  float = Field(..., ge=-90,  le=90)    # 有効な緯度
    lon:  float = Field(..., ge=-180, le=180)   # 有効な経度
    name: str   = Field("", max_length=200)

class RouteRequest(BaseModel):
    start:    WayPoint
    goal:     WayPoint
    vias:     List[WayPoint] = Field(default=[], max_length=5)   # 経由地は最大5箇所
    activity: ActivityKey    = "🏔️ 登山・ハイキング"
    engine:   Literal["ORS", "GraphHopper"] = "ORS"

class SupplyEventIn(BaseModel):
    time_min: float = Field(..., ge=0, le=4320)   # 0分〜72時間
    food:     str   = Field(..., max_length=100)
    qty:      int   = Field(1,   ge=1, le=10)

    @field_validator("food")
    @classmethod
    def food_must_be_registered(cls, v: str) -> str:
        if v not in FOOD_MASTER:
            raise ValueError(f"未登録の食品です: {v!r}。/api/foods で確認してください。")
        return v

class ProfileIn(BaseModel):
    body_weight:    float = Field(65.0,   ge=20,  le=300)   # 体重 kg
    load_weight:    float = Field(8.0,    ge=0,   le=150)   # 荷物 kg
    e_initial:      float = Field(1800.0, ge=200, le=5000)  # 初期エネルギー kcal
    hr_max:         float = Field(180.0,  ge=100, le=230)   # 最大心拍数 bpm
    terrain_factor: float = Field(1.2,    ge=0.5, le=5.0)   # 地形係数
    lt_ratio:       float = Field(0.85,   ge=0.4, le=1.0)   # LT比率
    e_crit:         float = Field(300.0,  ge=0,   le=2000)  # 危険水準 kcal
    t_warn:         float = Field(25.0,   ge=0,   le=2880)  # 警告余裕 分

class SimulateRequest(BaseModel):
    route:      dict
    activity:   ActivityKey = "🏔️ 登山・ハイキング"
    profile:    ProfileIn   = Field(default_factory=ProfileIn)
    supplies:   List[SupplyEventIn] = Field(default=[], max_length=50)
    planned_dt: Optional[str]       = Field(None, max_length=50)

    @field_validator("planned_dt")
    @classmethod
    def planned_dt_must_be_iso(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            _dt.datetime.fromisoformat(v)
        except ValueError:
            raise ValueError(
                "planned_dt はISO形式の日時文字列が必要です（例: 2025-08-01T09:00）"
            )
        return v

    @model_validator(mode="after")
    def validate_route_shape(self) -> "SimulateRequest":
        coords = self.route.get("coords")
        if not isinstance(coords, list):
            raise ValueError("route.coords はリスト形式である必要があります")
        if len(coords) < 2:
            raise ValueError("route.coords には最低2点が必要です")
        if len(coords) > 50_000:
            raise ValueError(
                f"route.coords の点数（{len(coords):,}点）が上限（50,000点）を超えています"
            )
        return self

class WeatherRequest(BaseModel):
    lat:      float = Field(..., ge=-90,  le=90)
    lon:      float = Field(..., ge=-180, le=180)
    datetime: str   = Field(..., max_length=50)
    activity: str   = Field("hiking", max_length=50)

# ── 新機能用 Pydantic モデル ─────────────────────────────

class RaceSimRequest(BaseModel):
    """レースシミュレーターへのリクエスト。"""
    route_points:     list[dict]    # [{distance_m, elevation_m}]
    target_time_min:  float         = Field(..., ge=10, le=10080)  # 10分〜1週間
    activity_type:    str           = Field("hiking", max_length=30)
    body_weight:      float         = Field(65.0, ge=20, le=300)
    load_weight:      float         = Field(8.0, ge=0, le=150)
    terrain_factor:   float         = Field(1.2, ge=0.5, le=5.0)
    segment_km:       float         = Field(1.0, ge=0.2, le=5.0)
    use_personal_model: bool        = True
    target_date:      Optional[str] = Field(None, max_length=20)

class TrainingPlanRequest(BaseModel):
    """トレーニングプラン生成リクエスト。"""
    race_date:        str   = Field(..., max_length=10)   # ISO date "YYYY-MM-DD"
    race_name:        str   = Field(..., min_length=1, max_length=200)
    race_distance_km: float = Field(..., ge=1, le=5000)
    activity_type:    str   = Field("running", max_length=30)

    @field_validator("race_date")
    @classmethod
    def race_date_must_be_iso(cls, v: str) -> str:
        try:
            _dt.date.fromisoformat(v)
        except ValueError:
            raise ValueError("race_date は YYYY-MM-DD 形式の日付が必要です")
        return v

class AIQueryRequest(BaseModel):
    """AI自然言語クエリリクエスト。"""
    question: str = Field(..., min_length=1, max_length=500)

# ── 新規: ルート候補比較 ──────────────────────────────────
class RouteCandidateIn(BaseModel):
    label:    str       = Field("ルート", max_length=50)
    route:    dict                               # coords / elevations / distance_m 等
    activity: ActivityKey = "🏔️ 登山・ハイキング"
    profile:  ProfileIn   = Field(default_factory=ProfileIn)

class CompareCandidatesRequest(BaseModel):
    candidates: List[RouteCandidateIn] = Field(..., min_length=2, max_length=3)
    supplies:   List[SupplyEventIn]    = Field(default=[], max_length=50)

# ── 新規: トレーニングプランAI調整 ────────────────────────
class TrainingPlanAdjustRequest(BaseModel):
    plan_id:    int  = Field(..., ge=1)
    week_index: int  = Field(..., ge=0, le=52)
    message:    str  = Field(..., min_length=1, max_length=300)

# ── 新規: レース実績入力 ──────────────────────────────────
class RaceResultInput(BaseModel):
    activity_type:    str   = Field("running", max_length=30)
    race_date:        str   = Field(..., max_length=10)   # YYYY-MM-DD
    race_distance_km: float = Field(..., ge=1, le=500)
    actual_time_min:  float = Field(..., ge=5, le=10080)
    avg_hr:           Optional[float] = Field(None, ge=40, le=230)
    max_hr:           Optional[float] = Field(None, ge=40, le=230)
    elevation_gain_m: Optional[float] = Field(None, ge=0)
    notes:            str   = Field("", max_length=300)

    @field_validator("race_date")
    @classmethod
    def race_date_must_be_iso(cls, v: str) -> str:
        try:
            _dt.date.fromisoformat(v)
        except ValueError:
            raise ValueError("race_date は YYYY-MM-DD 形式の日付が必要です")
        return v

# ──────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────

def _load_df():
    """Pace predictor training data."""
    import pandas as pd
    p = os.path.join(os.path.dirname(__file__), "..", "data", "processed", "merged_log.csv")
    if not os.path.exists(p):
        return _make_synthetic_df()
    df = pd.read_csv(p, parse_dates=["timestamp"])
    for col in ["heart_rate", "hr_smooth", "hr_raw"]:
        if col in df.columns:
            df["heart_rate"] = pd.to_numeric(df[col], errors="coerce")
            break
    else:
        df["heart_rate"] = 100.0
    return df.dropna(subset=["heart_rate"])

def _make_synthetic_df():
    import pandas as pd
    np.random.seed(42)
    n = 500
    slopes = np.random.uniform(-30, 30, n)
    speeds = np.clip(1.2 * np.exp(-0.04 * np.abs(slopes)), 0.1, 2.0)
    hrs    = np.clip(80 + np.abs(slopes) * 1.5 + np.random.normal(0, 5, n), 60, 180)
    return pd.DataFrame({
        "timestamp":    pd.date_range("2024-01-01", periods=n, freq="10s"),
        "slope_pct":    slopes,
        "speed_ms":     speeds,
        "speed_smooth": speeds,
        "hr_smooth":    hrs,
        "heart_rate":   hrs,
    })

# PaceRegressor はサーバー起動時に1回だけ学習してキャッシュする（リクエストごとの学習を回避）
_cached_pace_regressor = None

def _get_pace_regressor():
    global _cached_pace_regressor
    if _cached_pace_regressor is None:
        from pace_predictor import PaceRegressor
        df = _load_df()
        reg = PaceRegressor()
        reg.fit(df)
        _cached_pace_regressor = reg
        logger.info("PaceRegressor fitted and cached (model_type=%s)", reg.model_type)
    return _cached_pace_regressor

def _build_engine_a(df, slope_arr, profile: UserProfile):
    from ke_estimator   import energy_watt
    from pace_predictor import running_speed as _run_spd, tobler_speed as _tobler

    reg = _get_pace_regressor()
    act = profile.activity_type
    if act == "running":
        spd = np.clip([_run_spd(s, profile.base_run_speed_ms) for s in slope_arr], 1.0, 8.0)
    elif act == "cycling":
        spd = np.full(len(slope_arr), profile.base_cycling_speed_ms)
    else:
        spd = np.clip([_tobler(s) for s in slope_arr], 0.1, 2.0)

    dist     = spd * 10
    eta_min  = len(slope_arr) * 10 / 60
    hr_pred  = np.clip(0.85 * profile.hr_max + slope_arr * 0.3, 60, profile.hr_max)
    ke_arr   = np.array([
        energy_watt(float(spd[i]), float(s), profile)
        for i, s in enumerate(slope_arr)
    ])
    return dict(
        speed_curve  = spd,
        dist_curve   = dist,
        slope_curve  = slope_arr,
        hr_predicted = hr_pred,
        ke_series    = ke_arr,
        eta_minutes  = eta_min,
        total_dist_m = float(dist.sum()),
    )

def _serialize_result(
    result        : dict,
    engine_a      : dict,
    route         : dict,
    prof          : UserProfile,
    completion    = None,    # CompletionResult | None
    anomalies     = None,    # List[AnomalySegment] | None
    hr_zone_info  : dict | None = None,
    fatigue_info  : dict | None = None,
    pace_model_type: str = "gradient_boosting",
) -> dict:
    """Convert numpy arrays to JSON-serializable Python lists."""
    time_min     = result["time_min"].tolist()
    e_kcal       = result["E_kcal"].tolist()
    dist_m       = engine_a["dist_curve"].cumsum().tolist()
    speed        = engine_a["speed_curve"].tolist()
    slope        = engine_a["slope_curve"].tolist()
    eta_min      = float(engine_a["eta_minutes"])
    fatigue_curve = result.get("fatigue_arr", np.ones(len(time_min))).tolist()

    coords   = route.get("coords", [])
    elevs    = route.get("elevations", [])
    dist_km  = route.get("distance_m", 0) / 1000

    out = {
        "eta_min":       eta_min,
        "dist_km":       dist_km,
        "ascent_m":      route.get("ascent_m", 0),
        "total_kcal":    float(prof.e_initial_kcal - min(e_kcal)),
        "final_kcal":    float(e_kcal[-1]),
        "e_initial":     float(prof.e_initial_kcal),
        "e_crit":        float(result["alert_config"].e_crit_kcal),
        "alert_times":   result["alert_times"],
        "supply_log":    result["supply_log"],
        "route_method":  route.get("method", ""),
        "pace_model":    pace_model_type,
        # time-series (downsampled to ≤500 pts for payload size)
        "time_min":      _downsample(time_min,      500),
        "e_kcal":        _downsample(e_kcal,        500),
        "dist_m":        _downsample(dist_m,        500),
        "fatigue_curve": _downsample(fatigue_curve, 500),
        # route geometry
        "coords":        coords[::max(1, len(coords)//500)],
        "elevations":    elevs[::max(1, len(elevs)//500)],
        "speed":         _downsample(speed, 500),
        "slope":         _downsample(slope, 500),
        # weather
        "weather": {
            "location":       getattr(route.get("_weather"), "location", ""),
            "temp_c":         getattr(route.get("_weather"), "temp_c", None),
            "humidity_pct":   getattr(route.get("_weather"), "humidity_pct", None),
            "wind_speed_ms":  getattr(route.get("_weather"), "wind_speed_ms", None),
            "condition":      getattr(route.get("_weather"), "condition", None),
            "weather_factor": getattr(route.get("_weather"), "weather_factor", 1.0),
        },
        # ── ML拡張フィールド ──────────────────────────────────
        # 完走確率（Monte Carlo）
        "completion": {
            "probability":     completion.probability     if completion else None,
            "confidence_low":  completion.confidence_low  if completion else None,
            "confidence_high": completion.confidence_high if completion else None,
            "risk_level":      completion.risk_level      if completion else None,
            "message":         completion.message         if completion else None,
            "n_simulations":   completion.n_simulations   if completion else None,
        },
        # 心拍ゾーン自動推定
        "hr_zones": hr_zone_info or {"fitted": False},
        # 疲労モデル情報
        "fatigue_model": fatigue_info or {"enabled": False},
        # 異常区間リスト
        "anomalies": [
            {
                "start_min":    seg.start_min,
                "end_min":      seg.end_min,
                "type":         seg.anomaly_type,
                "severity":     seg.severity,
                "description":  seg.description,
            }
            for seg in (anomalies or [])
        ],
    }
    return out

def _downsample(arr, n):
    if len(arr) <= n:
        return arr
    idx = np.linspace(0, len(arr) - 1, n, dtype=int)
    return [arr[i] for i in idx]


def _calc_difficulty_score(coords: list, elevations: list) -> dict:
    """
    高度プロフィールから難易度スコア（1〜10）を算出する。
    スコア要因: 最大斜度, 持続斜度(上位20%), 獲得標高, 距離, 標高獲得/km
    """
    if not coords or len(coords) < 2 or not elevations or len(elevations) < 2:
        return {"score": 1.0, "label": "不明", "factors": {}}

    n     = len(elevations)
    elevs = np.array(elevations, dtype=float)

    # 距離を計算 (coords は [[lat,lon], ...])
    coords_arr = np.array(coords)
    lat_r = np.radians(coords_arr[:, 0])
    lon_r = np.radians(coords_arr[:, 1])
    dlat  = np.diff(lat_r)
    dlon  = np.diff(lon_r)
    a     = np.sin(dlat / 2) ** 2 + np.cos(lat_r[:-1]) * np.cos(lat_r[1:]) * np.sin(dlon / 2) ** 2
    seg_dist_m = 6371000.0 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    total_dist_m = float(seg_dist_m.sum())
    dist_km = max(total_dist_m / 1000, 0.1)

    # 斜度配列（coords と elevations は同じ n 点を持つため np.diff で n-1 点の差分が得られる）
    elev_diffs  = np.diff(elevs)                                 # shape: (n-1,)
    safe_dists  = np.where(seg_dist_m > 0.1, seg_dist_m, 0.1)  # shape: (n-1,)
    slopes_pct  = elev_diffs / safe_dists * 100                 # shape: (n-1,) ✓
    up_slopes   = slopes_pct[slopes_pct > 0]

    max_grad      = float(np.percentile(up_slopes, 95)) if len(up_slopes) > 0 else 0.0
    sustained_grad = float(np.mean(up_slopes[up_slopes >= np.percentile(up_slopes, 80)])) \
        if len(up_slopes) > 5 else max_grad

    # 獲得標高
    diffs = np.diff(elevs)
    total_ascent = float(diffs[diffs > 0].sum())
    ascent_per_km = total_ascent / dist_km

    # 各要因を0〜1スコアに正規化して重み付き合計
    f_max_grad       = min(max_grad / 50.0,       1.0)   # 50%以上で1.0
    f_sustained_grad = min(sustained_grad / 35.0, 1.0)   # 35%以上で1.0
    f_ascent         = min(total_ascent / 3000.0, 1.0)   # 3000m以上で1.0
    f_dist           = min(dist_km / 30.0,        1.0)   # 30km以上で1.0
    f_ascent_per_km  = min(ascent_per_km / 200.0, 1.0)   # 200m/km以上で1.0

    score_raw = (
        f_max_grad       * 0.25
        + f_sustained_grad * 0.25
        + f_ascent         * 0.20
        + f_dist           * 0.15
        + f_ascent_per_km  * 0.15
    )
    score = round(max(1.0, min(10.0, score_raw * 10)), 1)

    if score <= 2.5:
        label = "初級"
    elif score <= 5.0:
        label = "中級"
    elif score <= 7.5:
        label = "上級"
    else:
        label = "超上級"

    return {
        "score": score,
        "label": label,
        "factors": {
            "max_gradient_pct":       round(max_grad, 1),
            "sustained_gradient_pct": round(sustained_grad, 1),
            "total_ascent_m":         round(total_ascent),
            "dist_km":                round(dist_km, 1),
            "ascent_per_km":          round(ascent_per_km, 1),
        },
    }


# ──────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════
# 認証エンドポイント
# ══════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    email:        EmailStr = Field(..., description="メールアドレス")
    password:     str      = Field(..., min_length=8, max_length=100)
    display_name: str      = Field("", max_length=50)

class LoginRequest(BaseModel):
    email:    EmailStr
    password: str

@app.post("/auth/register")
@limiter.limit("5/minute")
def auth_register(
    response: Response,
    request:  Request,
    req:      RegisterRequest,
    db:       Session = Depends(get_db),
):
    """新規アカウント登録。成功時に HttpOnly Cookie をセットする。"""
    ip = get_remote_address(request)
    _check_login_lock(ip)   # 登録エンドポイントも列挙攻撃から保護

    if db.query(User).filter(User.email == req.email).first():
        raise HTTPException(status_code=409, detail="このメールアドレスはすでに登録されています")
    user = User(
        email         = req.email,
        display_name  = req.display_name or req.email.split("@")[0],
        password_hash = hash_password(req.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_token(user.id, user.email)
    set_auth_cookie(response, token)   # HttpOnly Cookie にセット
    logger.info("New user registered: id=%d", user.id)  # メールアドレスをログに残さない
    return {
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "display_name": user.display_name},
    }

@app.post("/auth/login")
@limiter.limit("10/minute")
def auth_login(
    response: Response,
    request:  Request,
    req:      LoginRequest,
    db:       Session = Depends(get_db),
):
    """ログイン。成功時に HttpOnly Cookie をセットする。"""
    ip = get_remote_address(request)
    _check_login_lock(ip)   # ロック中なら 429

    user = db.query(User).filter(User.email == req.email).first()
    if not user or not verify_password(req.password, user.password_hash):
        _record_login_failure(ip)   # 失敗カウント++
        raise HTTPException(status_code=401, detail="メールアドレスまたはパスワードが正しくありません")

    _clear_login_failure(ip)        # 成功したら失敗カウントをリセット
    token = create_token(user.id, user.email)
    set_auth_cookie(response, token)   # HttpOnly Cookie にセット
    return {
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "display_name": user.display_name},
    }

@app.post("/auth/logout")
@limiter.limit("30/minute")
def auth_logout(response: Response, request: Request):
    """ログアウト。Cookie を削除する。"""
    clear_auth_cookie(response)
    return {"message": "ログアウトしました"}

@app.get("/auth/me")
def auth_me(current_user: User = Depends(get_current_user)):
    """現在のログインユーザー情報を返す。"""
    return {
        "id":           current_user.id,
        "email":        current_user.email,
        "display_name": current_user.display_name,
        "created_at":   current_user.created_at.isoformat() if current_user.created_at else None,
    }

# ══════════════════════════════════════════════════════════════════════
# 通常エンドポイント
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/foods")
def get_foods():
    return {k: {"kcal_per": v["kcal_per"], "ka": v["ka"]} for k, v in FOOD_MASTER.items()}

@app.get("/api/activities")
def get_activities():
    return list(ACTIVITY_PROFILES.keys())

# ── 個人HRログのパス（ユーザーごとに分離）──────────────────────────────
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

def _personal_hr_path(user_id: int) -> str:
    return os.path.join(_DATA_DIR, f"personal_hr_log_{user_id}.csv")

@app.post("/api/upload-hr")
@limiter.limit("10/minute")
async def upload_hr_log(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """
    個人の心拍数ログ（CSV）をアップロードする。
    アップロードされたデータは次回シミュレーション時に
    HRゾーン自動推定（K-means）の学習データとして使用される。

    受け付けるCSV形式:
      - カラム名に hr_smooth / heart_rate / hr_raw のいずれかを含むこと
      - 最低30行以上のデータが必要（K-meansの最低サンプル数）

    対応フォーマット例:
      Garmin CSV, YAMAP CSV, Strava HR export, 手動記録CSVなど
    """
    import pandas as pd

    # ファイル名チェック
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="CSVファイル（.csv）のみ受け付けます")

    # サイズ制限（2MB）
    HR_MAX_BYTES = 2 * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    CHUNK = 64 * 1024
    while True:
        chunk = await file.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > HR_MAX_BYTES:
            raise HTTPException(status_code=413, detail="ファイルサイズが上限（2MB）を超えています")
        chunks.append(chunk)
    data = b"".join(chunks)

    # CSVとしてパース
    try:
        import io
        df = pd.read_csv(io.BytesIO(data))
    except Exception:
        raise HTTPException(status_code=400, detail="CSVファイルの解析に失敗しました。形式を確認してください")

    # HRカラムの存在チェック
    hr_col = next((c for c in ["hr_smooth", "heart_rate", "hr_raw"] if c in df.columns), None)
    if hr_col is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "心拍数カラムが見つかりません。"
                " hr_smooth / heart_rate / hr_raw のいずれかのカラム名が必要です"
            ),
        )

    # 有効データ数チェック
    hr_valid = df[hr_col].dropna()
    hr_valid = hr_valid[(hr_valid >= 40) & (hr_valid <= 230)]
    if len(hr_valid) < HRZoneEstimator.MIN_SAMPLES:
        raise HTTPException(
            status_code=400,
            detail=f"有効な心拍データが不足しています（{len(hr_valid)}件 < 必要最低{HRZoneEstimator.MIN_SAMPLES}件）",
        )

    # 保存（既存があれば上書き、ユーザーごとに分離）
    hr_path = _personal_hr_path(current_user.id)
    os.makedirs(os.path.dirname(hr_path), exist_ok=True)
    df.to_csv(hr_path, index=False)
    logger.info("Personal HR log saved: user=%d, %d rows, col=%r", current_user.id, len(df), hr_col)

    # K-means でゾーンを即時推定して結果を返す
    est = HRZoneEstimator()
    est.fit(hr_valid.values)
    return {
        "message":    "個人HRログを保存しました。次回シミュレーションから自動でゾーン境界に反映されます",
        "n_rows":     len(df),
        "n_valid_hr": int(len(hr_valid)),
        "hr_col":     hr_col,
        "hr_zones":   est.zone_info,
    }

@app.get("/api/hr-zones")
@limiter.limit("20/minute")
def get_hr_zones(
    request: Request,
    hr_max: float = 180.0,
    current_user: User = Depends(get_current_user),
):
    """
    現在の心拍ゾーン設定を返す。
    個人HRログがあればK-meansの結果、なければhr_maxベースの標準値。
    """
    import pandas as pd
    est = HRZoneEstimator()
    hr_path = _personal_hr_path(current_user.id)
    if os.path.exists(hr_path):
        try:
            df     = pd.read_csv(hr_path)
            hr_col = next((c for c in ["hr_smooth", "heart_rate", "hr_raw"] if c in df.columns), None)
            if hr_col:
                est.fit(df[hr_col].dropna().values, hr_max=hr_max)
        except Exception:
            est._set_fallback(hr_max)
    else:
        est._set_fallback(hr_max)
    return {"has_personal_data": os.path.exists(hr_path), **est.zone_info}

@app.delete("/api/hr-zones")
@limiter.limit("10/minute")
def delete_hr_log(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """個人HRログを削除し、標準閾値に戻す。"""
    hr_path = _personal_hr_path(current_user.id)
    if os.path.exists(hr_path):
        os.remove(hr_path)
        return {"message": "個人HRログを削除しました。標準閾値（hr_max基準）に戻ります"}
    return {"message": "個人HRログは存在しません"}

@app.post("/api/search-place")
@limiter.limit("20/minute")
def api_search_place(request: Request, req: PlaceSearchRequest):
    try:
        return search_place(req.query, req.limit)
    except Exception as e:
        logger.error("Place search error [query=%r]: %s", req.query, e, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="場所の検索に失敗しました。しばらく待ってから再試行してください。",
        )

@app.post("/api/route")
@limiter.limit("10/minute")
def api_route(request: Request, req: RouteRequest):
    wp_list = [(req.start.lat, req.start.lon)]
    for v in req.vias:
        wp_list.append((v.lat, v.lon))
    wp_list.append((req.goal.lat, req.goal.lon))

    profile_ors = ACTIVITY_PROFILES.get(req.activity, "foot-hiking")
    rt = None

    if req.engine == "GraphHopper":
        try:
            rt = get_route_graphhopper(wp_list, profile_ors)
            rt["method"] = "GraphHopper（道路・登山道沿い）"
        except Exception:
            pass

    if rt is None:
        try:
            rt = get_route_ors(wp_list, profile_ors, ORS_KEY)
            rt["method"] = "ORS（道路・登山道沿い）"
        except Exception:
            pass

    if rt is None:
        try:
            rt = get_route_graphhopper(wp_list, profile_ors)
            rt["method"] = "GraphHopper（ORSフォールバック）"
        except Exception:
            pass

    if rt is None:
        rt = get_route_fallback(wp_list)
        rt["method"] = "直線近似（APIに接続できませんでした）"

    rt["waypoints"] = wp_list
    difficulty = _calc_difficulty_score(rt["coords"], rt["elevations"])
    return {
        "coords":      rt["coords"],
        "elevations":  rt["elevations"],
        "distance_m":  rt["distance_m"],
        "ascent_m":    rt.get("ascent_m", 0),
        "descent_m":   rt.get("descent_m", 0),
        "method":      rt["method"],
        "waypoints":   rt["waypoints"],
        "difficulty":  difficulty,
    }

GPX_MAX_BYTES  = 5 * 1024 * 1024    # 5 MB：実際のGPXは通常100KB〜2MB程度
GPX_MAX_POINTS = 50_000              # 5万点：フルマラソンでも〜3万点程度

@app.post("/api/gpx")
@limiter.limit("10/minute")
async def api_gpx(request: Request, file: UploadFile = File(...)):
    # ── 1. ファイル名チェック（.gpx 拡張子のみ受け付ける）──────────────
    filename = file.filename or ""
    if not filename.lower().endswith(".gpx"):
        raise HTTPException(
            status_code=400,
            detail="GPXファイル（.gpx）のみアップロードできます",
        )

    # ── 2. サイズ制限付きストリーミング読み込み ────────────────────────
    # file.read() は全量をメモリに展開するため、チャンク単位で読み込み、
    # 上限を超えた時点で即座にエラーにしてメモリ枯渇（DoS）を防ぐ。
    CHUNK = 64 * 1024  # 64 KB ずつ読む
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > GPX_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"ファイルサイズが上限（{GPX_MAX_BYTES // 1024 // 1024} MB）を超えています。"
                    " ルートを分割してアップロードしてください。"
                ),
            )
        chunks.append(chunk)
    data = b"".join(chunks)

    # ── 3. マジックバイト確認（XMLであること）─────────────────────────
    # Content-Type は偽装できるため、ファイル先頭バイトで実際の形式を確認する。
    # GPX は XML なので必ず "<?xml" または "<gpx" で始まる（BOMを除いて）。
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")   # UTF-8 BOM と空白を除去
    if not (head.startswith(b"<?xml") or head.startswith(b"<gpx")):
        raise HTTPException(
            status_code=400,
            detail="有効なGPXファイルではありません（ファイル形式を確認してください）",
        )

    try:
        rt = parse_gpx_route(data)
    except HTTPException:
        raise
    except ValueError as e:
        # parse_gpx_route が送出する ValueError はユーザー向けの安全なメッセージのみ含む
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # 予期しない内部エラー：詳細はサーバーログに残し、クライアントには汎用メッセージ
        logger.error("GPX parse unexpected error: %s", e, exc_info=True)
        raise HTTPException(
            status_code=400,
            detail="GPXファイルを解析できませんでした。ファイルが破損していないか確認してください。",
        )
    try:
        coords_list = [list(c) for c in rt["coords"]]
        elevs_list  = [float(e) for e in rt["elevations"]]
        difficulty  = _calc_difficulty_score(coords_list, elevs_list)
        return {
            "coords":      coords_list,
            "elevations":  elevs_list,
            "distance_m":  float(rt["distance_m"]),
            "ascent_m":    float(rt.get("ascent_m", 0)),
            "descent_m":   float(rt.get("descent_m", 0)),
            "method":      "GPX（実際の軌跡）",
            "difficulty":  difficulty,
        }
    except Exception as e:
        logger.error("GPX response serialization error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="サーバー内部エラーが発生しました。")

@app.post("/api/weather")
@limiter.limit("20/minute")
def api_weather(request: Request, req: WeatherRequest):
    import datetime as dt
    try:
        planned = dt.datetime.fromisoformat(req.datetime)
    except Exception:
        planned = dt.datetime.now()
    try:
        wd = fetch_weather_forecast(OWM_KEY, req.lat, req.lon, planned, req.activity)
        return {
            "location":       wd.location,
            "temp_c":         wd.temp_c,
            "humidity_pct":   wd.humidity_pct,
            "wind_speed_ms":  wd.wind_speed_ms,
            "condition":      wd.condition,
            "weather_factor": wd.weather_factor,
            "factor_detail":  wd.factor_detail,
        }
    except Exception as e:
        logger.warning("Weather fetch failed [lat=%s, lon=%s]: %s", req.lat, req.lon, e)
        wf, _ = calc_weather_factor(15.0, 60, 2.0, "Clear")
        return {
            "location":       "取得失敗",
            "temp_c":         15.0,
            "humidity_pct":   60.0,
            "wind_speed_ms":  2.0,
            "condition":      "Clear",
            "weather_factor": wf,
            "factor_detail":  "天気情報を取得できなかったためデフォルト値を使用しています",
        }

@app.post("/api/simulate")
@limiter.limit("10/minute")
def api_simulate(
    request: Request,
    req: SimulateRequest,
    current_user: User | None = Depends(get_current_user_optional),
):
    import datetime as dt

    route   = req.route
    act_key = req.activity
    act_sim = ACTIVITY_TO_SIM.get(act_key, "hiking")
    p       = req.profile

    coords = route.get("coords", [])
    elevs  = route.get("elevations", [])
    if not coords:
        raise HTTPException(status_code=400, detail="ルートデータが空です")

    spd_h = 1.0 if act_sim == "hiking" else (3.0 if act_sim == "running" else 6.94)
    slope_arr, elev_arr, dist_arr = coords_to_slope_array(coords, elevs, speed_ms=spd_h)
    alt_m = float(np.array(elev_arr).mean()) if len(elev_arr) > 0 else 0.0

    # Weather
    weather_factor = 1.0
    weather_obj    = None
    if req.planned_dt and coords:
        try:
            planned = dt.datetime.fromisoformat(req.planned_dt)
            weather_obj = fetch_weather_forecast(
                OWM_KEY, coords[0][0], coords[0][1], planned, act_sim)
            weather_factor = weather_obj.weather_factor
        except Exception:
            pass

    prof = UserProfile(
        body_weight_kg        = p.body_weight,
        load_weight_kg        = p.load_weight,
        hr_max                = p.hr_max,
        terrain_factor        = p.terrain_factor,
        e_initial_kcal        = p.e_initial,
        activity_type         = act_sim,
        base_run_speed_ms     = 3.0,
        base_cycling_speed_ms = 6.94,
        weather_factor        = weather_factor,
        altitude_m            = alt_m,
    )

    try:
        df       = _load_df()
        engine_a = _build_engine_a(df, slope_arr, prof)

        supply_events = []
        for s in req.supplies:
            food = FOOD_MASTER.get(s.food, {"kcal_per": 100, "ka": 1.0})
            supply_events.append(SupplyEvent(
                time_min = s.time_min,
                kcal     = float(food["kcal_per"] * s.qty),
                ka       = food["ka"],
            ))
        supply_events = sorted(supply_events, key=lambda e: e.time_min)

        alert_config = AlertConfig(e_crit_kcal=p.e_crit, t_warn_min=p.t_warn)

        # ── 案1: 疲労蓄積モデル ───────────────────────────────
        fatigue_model = FatigueModel(activity_type=act_sim)

        # ── 案2: 心拍ゾーン自動推定 ──────────────────────────
        # 個人のHRログ（/api/upload-hr でアップロードされたもの）がある場合のみ
        # K-means で個人最適化する。なければ hr_max ベースの標準閾値を使う。
        hr_zone_est = HRZoneEstimator()
        personal_hr_path = _personal_hr_path(current_user.id) if current_user else None
        if personal_hr_path and os.path.exists(personal_hr_path):
            try:
                import pandas as pd
                hr_df  = pd.read_csv(personal_hr_path)
                hr_col = next((c for c in ["hr_smooth", "heart_rate", "hr_raw"]
                               if c in hr_df.columns), None)
                if hr_col and len(hr_df) >= HRZoneEstimator.MIN_SAMPLES:
                    hr_zone_est.fit(hr_df[hr_col].dropna().values, hr_max=p.hr_max)
                    logger.info("HR zone estimator: fitted on personal data (%d pts)", len(hr_df))
                else:
                    hr_zone_est._set_fallback(p.hr_max)
            except Exception as e:
                logger.warning("Personal HR log load failed: %s", e)
                hr_zone_est._set_fallback(p.hr_max)
        else:
            # 個人データなし → hr_max ベースの標準閾値（正直なフォールバック）
            hr_zone_est._set_fallback(p.hr_max)

        # ── PKモデル実行（疲労+ゾーン補正あり） ──────────────
        result = run_pk_model(
            engine_a_output   = engine_a,
            profile           = prof,
            supply_events     = supply_events,
            alert_config      = alert_config,
            fatigue_model     = fatigue_model,
            hr_zone_estimator = hr_zone_est,
        )

        # ── 案4: 完走確率予測（Monte Carlo, n=100） ──────────
        completion = None
        try:
            predictor  = CompletionPredictor(n_sim=20)
            completion = predictor.predict(
                engine_a_output = engine_a,
                profile         = prof,
                supply_events   = supply_events,
                alert_config    = alert_config,
                fatigue_model   = fatigue_model,
            )
        except Exception as e_mc:
            logger.warning("Monte Carlo completion prediction failed: %s", e_mc)

        # ── 案5: 異常区間検知 ────────────────────────────────
        anomalies = []
        try:
            detector  = AnomalyDetector()
            anomalies = detector.detect(result, engine_a, prof, alert_config)
        except Exception as e_ad:
            logger.warning("Anomaly detection failed: %s", e_ad)

        # ── レスポンス構築 ───────────────────────────────────
        route["_weather"] = weather_obj

        # 案3: ペースモデル種別を取得（キャッシュ済みモデルから読む）
        try:
            pace_model_type = _get_pace_regressor().model_type
        except Exception:
            pace_model_type = "gradient_boosting"

        out = _serialize_result(
            result          = result,
            engine_a        = engine_a,
            route           = route,
            prof            = prof,
            completion      = completion,
            anomalies       = anomalies,
            hr_zone_info    = hr_zone_est.zone_info,
            fatigue_info    = {**fatigue_model.info, "enabled": True},
            pace_model_type = pace_model_type,
        )
        if weather_obj:
            out["weather"] = {
                "location":       weather_obj.location,
                "temp_c":         weather_obj.temp_c,
                "humidity_pct":   weather_obj.humidity_pct,
                "wind_speed_ms":  weather_obj.wind_speed_ms,
                "condition":      weather_obj.condition,
                "weather_factor": weather_obj.weather_factor,
                "factor_detail":  getattr(weather_obj, "factor_detail", ""),
            }
        return out

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(
            "Simulation error [activity=%r, coords=%d pts]: %s",
            act_key, len(coords), e, exc_info=True,
        )
        detail = f"シミュレーション計算中にエラーが発生しました。入力値を確認してください。"
        if os.environ.get("DEBUG"):
            detail += f" [{type(e).__name__}: {e}]\n{traceback.format_exc()}"
        raise HTTPException(status_code=500, detail=detail)


# ──────────────────────────────────────────────────────────────────────
# 補給計画自動生成
# ──────────────────────────────────────────────────────────────────────

class OptimizeSupplyRequest(BaseModel):
    route:      dict
    activity:   ActivityKey = "🏔️ 登山・ハイキング"
    profile:    ProfileIn   = Field(default_factory=ProfileIn)
    max_supplies:     int   = Field(12, ge=1, le=30)
    min_interval_min: float = Field(20.0, ge=5.0, le=120.0)

    @model_validator(mode="after")
    def validate_route_shape(self) -> "OptimizeSupplyRequest":
        coords = self.route.get("coords")
        if not isinstance(coords, list) or len(coords) < 2:
            raise ValueError("route.coords には最低2点が必要です")
        if len(coords) > 50_000:
            raise ValueError("route.coords の点数が上限（50,000点）を超えています")
        return self


@app.post("/api/optimize-supply")
@limiter.limit("10/minute")
def api_optimize_supply(request: Request, req: OptimizeSupplyRequest):
    """
    補給計画自動生成エンドポイント。
    ルート・プロフィールを受け取り、ハンガーノックを防ぐ最適な補給計画を返す。
    """
    act_key = req.activity
    act_sim = ACTIVITY_TO_SIM.get(act_key, "hiking")
    p       = req.profile

    coords = req.route.get("coords", [])
    elevs  = req.route.get("elevations", [])

    spd_h = 1.0 if act_sim == "hiking" else (3.0 if act_sim == "running" else 6.94)
    slope_arr, elev_arr, dist_arr = coords_to_slope_array(coords, elevs, speed_ms=spd_h)
    alt_m = float(np.array(elev_arr).mean()) if len(elev_arr) > 0 else 0.0

    prof = UserProfile(
        body_weight_kg        = p.body_weight,
        load_weight_kg        = p.load_weight,
        hr_max                = p.hr_max,
        terrain_factor        = p.terrain_factor,
        e_initial_kcal        = p.e_initial,
        activity_type         = act_sim,
        base_run_speed_ms     = 3.0,
        base_cycling_speed_ms = 6.94,
        altitude_m            = alt_m,
    )
    alert_config = AlertConfig(e_crit_kcal=p.e_crit, t_warn_min=p.t_warn)

    try:
        df       = _load_df()
        engine_a = _build_engine_a(df, slope_arr, prof)

        result: OptimizeResult = optimize_supply_plan(
            engine_a_output  = engine_a,
            profile          = prof,
            food_master      = FOOD_MASTER,
            alert_config     = alert_config,
            max_supplies     = req.max_supplies,
            min_interval_min = req.min_interval_min,
        )

        # supply_plan → フロント向けに食品名付きで返す
        # food名はkcal/kaが一致するものをFOOD_MASTERから逆引き
        def _find_food_name(ev) -> str:
            for name, info in FOOD_MASTER.items():
                if abs(info["kcal_per"] - ev.kcal) < 1 and abs(info["ka"] - ev.ka) < 0.05:
                    return name
            return "エネルギージェル"

        plan = [
            {
                "time_min": round(ev.time_min, 1),
                "food":     _find_food_name(ev),
                "kcal":     ev.kcal,
                "ka":       ev.ka,
                "qty":      1,
            }
            for ev in result.supply_plan
        ]

        return {
            "is_feasible":   result.is_feasible,
            "score":         round(result.score, 1),
            "min_e_kcal":    round(result.min_e, 1),
            "total_kcal":    round(result.total_kcal, 1),
            "n_supplies":    len(plan),
            "supply_plan":   plan,
            "messages":      result.messages,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Optimize-supply error [activity=%r]: %s", act_key, e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="補給計画の生成中にエラーが発生しました。",
        )


# ══════════════════════════════════════════════════════
# 個人化 ML エンドポイント群
# ══════════════════════════════════════════════════════

import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.ingestion.fit_parser          import parse_fit
from src.ingestion.apple_health_parser import parse_apple_health
from src.ingestion.normalizer          import from_fit, from_apple_workout, from_gpx_csv
from models.personal_model_trainer     import (
    PersonalModelTrainer, load_points_from_db, store_activity_points,
)
from models.training_load_model        import (
    calc_trimp, build_pmc, get_current_load, estimate_vo2max,
)
from models.recovery_predictor         import (
    RecoveryPredictor, DailyInput,
)
from backend.models_db                 import Activity, ActivityPoint, DailyMetrics

# ファイルサイズ上限
_FIT_MAX_BYTES         = 20 * 1024 * 1024   # 20 MB
_APPLE_HEALTH_MAX_BYTES = 50 * 1024 * 1024  # 50 MB（Render free tier 512MB RAM に対する上限）


# ── Pydantic モデル（新エンドポイント用） ─────────────

class ManualActivityIn(BaseModel):
    """手動入力アクティビティ（GPX+HR アップロード済みの場合）。"""
    activity_type: Literal["hiking","running","cycling","trail_running"] = "hiking"
    hr_max:   float = Field(185.0, ge=100, le=230)
    hr_rest:  float = Field(60.0,  ge=30,  le=100)


# ══════════════════════════════════════════════════════
# Garmin FIT アップロード
# ══════════════════════════════════════════════════════

@app.post("/api/upload/garmin-fit")
@limiter.limit("10/minute")
async def upload_garmin_fit(
    request: Request,
    file: UploadFile = File(...),
    hr_max:  float = 185.0,
    hr_rest: float = 60.0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Garmin デバイスの .fit ファイルをアップロードする。

    Garmin Connect → アクティビティ → エクスポート → 元のファイル をダウンロードし
    そのまま送信できる。

    処理内容:
      1. FIT ファイルを解析（GPS, 心拍, 速度, 気温 等）
      2. Activity / ActivityPoint テーブルに保存
      3. 個人化モデルを再学習
      4. TRIMP・VO2max を計算して返す
    """
    if not (file.filename or "").lower().endswith(".fit"):
        raise HTTPException(status_code=400, detail=".fit ファイルのみ受け付けます")

    data = await _read_upload(file, _FIT_MAX_BYTES)

    try:
        summary, points_df = parse_fit(data)
    except ImportError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.warning("FIT parse error: %s", e)
        raise HTTPException(status_code=400, detail="FIT ファイルの解析に失敗しました")

    # Activity 保存
    activity = _save_activity(from_fit(summary, points_df), current_user.id, db)

    # ActivityPoint 保存
    if not points_df.empty:
        store_activity_points(activity.id, points_df, activity.duration_sec, db)

    # TRIMP 計算・保存
    trimp = None
    if activity.avg_hr and activity.duration_sec:
        trimp = calc_trimp(
            duration_min = activity.duration_sec / 60,
            avg_hr       = activity.avg_hr,
            hr_rest      = hr_rest,
            hr_max       = hr_max,
        )
        activity.trimp = trimp
        db.commit()

    # VO2max 推定
    vo2max = None
    if activity.avg_speed_mps and activity.avg_hr:
        alt = 0.0
        if not points_df.empty and "elevation" in points_df.columns:
            alt = float(points_df["elevation"].dropna().mean())
        vo2max = estimate_vo2max(
            speed_mps  = activity.avg_speed_mps,
            avg_hr     = activity.avg_hr,
            hr_max     = hr_max,
            altitude_m = alt,
        )
        if vo2max:
            activity.vo2max_estimated = vo2max
            db.commit()

    # 個人モデル再学習
    retrain_result = _retrain_personal_model(current_user.id, db)

    # DailyMetrics 更新
    _update_daily_metrics_from_activity(activity, hr_rest, hr_max, db)

    return {
        "message":      "Garmin FIT ファイルを取り込みました",
        "activity_id":  activity.id,
        "activity_type":activity.activity_type,
        "duration_min": round(activity.duration_sec / 60, 1),
        "distance_km":  round(activity.distance_m / 1000, 2),
        "elevation_gain_m": activity.elevation_gain_m,
        "avg_hr":       activity.avg_hr,
        "max_hr":       activity.max_hr,
        "trimp":        round(trimp, 1) if trimp else None,
        "vo2max_estimated": vo2max,
        "n_points":     len(points_df),
        "device":       summary.device_name,
        "model_retrained": retrain_result,
    }


# ══════════════════════════════════════════════════════
# Apple Health アップロード
# ══════════════════════════════════════════════════════

def _apple_health_background(
    content: bytes,
    fname: str,
    user_id: int,
    hr_max: float,
    hr_rest: float,
) -> None:
    """
    Apple Health XML をバックグラウンドで処理する。
    リクエストのタイムアウトを回避するため、レスポンス返却後に実行される。
    独自に DB セッションを作成し、完了後に必ずクローズする。
    """
    import zipfile as _zipfile

    db = SessionLocal()
    try:
        # ZIP → XML 展開
        if fname.endswith(".zip"):
            try:
                zf = _zipfile.ZipFile(io.BytesIO(content))
                xml_name = next((n for n in zf.namelist() if n.endswith("export.xml")), None)
                if not xml_name:
                    logger.error("Apple Health BG: ZIP 内に export.xml なし (user=%d)", user_id)
                    return
                # ZIP ボム対策: 展開後サイズが 500 MB を超える場合は拒否
                _MAX_XML_BYTES = 500 * 1024 * 1024
                if zf.getinfo(xml_name).file_size > _MAX_XML_BYTES:
                    logger.error(
                        "Apple Health BG: export.xml が大きすぎます (%d bytes, user=%d)",
                        zf.getinfo(xml_name).file_size, user_id,
                    )
                    return
                xml_source = zf.open(xml_name)
            except _zipfile.BadZipFile:
                logger.error("Apple Health BG: 不正な ZIP (user=%d)", user_id)
                return
        else:
            xml_source = io.BytesIO(content)

        health_data = parse_apple_health(xml_source)

        n_workouts = 0
        n_daily    = 0

        # ── ワークアウト保存 ──────────────────────────
        for w in health_data.workouts:
            norm     = from_apple_workout(w)
            activity = _save_activity(norm, user_id, db)
            if activity.avg_hr and activity.duration_sec:
                activity.trimp = calc_trimp(
                    duration_min = activity.duration_sec / 60,
                    avg_hr       = activity.avg_hr,
                    hr_rest      = hr_rest,
                    hr_max       = hr_max,
                )
            n_workouts += 1

        # ── デイリーメトリクス保存 ────────────────────
        for dm in health_data.daily_metrics:
            _upsert_daily_metrics(user_id, dm, db)
            n_daily += 1

        db.commit()  # 全変更を一括コミット

        # ── 個人モデル再学習 + PMC 再計算 ─────────────
        _retrain_personal_model(user_id, db)
        _rebuild_pmc_for_user(user_id, hr_rest, hr_max, db)

        logger.info(
            "Apple Health BG 完了: user=%d workouts=%d daily=%d",
            user_id, n_workouts, n_daily,
        )

    except Exception as e:
        logger.error("Apple Health BG エラー: user=%d %s", user_id, e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


@app.post("/api/upload/apple-health")
@limiter.limit("5/minute")
async def upload_apple_health(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    hr_max:  float = 185.0,
    hr_rest: float = 60.0,
    current_user: User = Depends(get_current_user),
):
    """
    Apple Health の export.xml / export.zip をアップロードする。
    ファイルを受け取り即座にレスポンスを返し、実際の処理はバックグラウンドで行う。
    """
    fname = (file.filename or "").lower()
    if not (fname.endswith(".xml") or fname.endswith(".zip")):
        raise HTTPException(status_code=400, detail="export.xml または export.zip のみ受け付けます")

    # ファイル全体を読み込んでサイズチェック
    content = await file.read()
    if len(content) > _APPLE_HEALTH_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"ファイルサイズが上限（{_APPLE_HEALTH_MAX_BYTES // 1024 // 1024}MB）を超えています。"
                   "export.zip のまま（解凍せず）アップロードするか、期間を絞ってエクスポートしてください。",
        )

    # バックグラウンドタスクとしてキュー登録（タイムアウト回避）
    background_tasks.add_task(
        _apple_health_background,
        content, fname, current_user.id, hr_max, hr_rest,
    )

    return {
        "message": "アップロードを受け付けました。処理は数分で完了します。完了後にページを更新するとデータが反映されます。",
        "status":  "processing",
    }


# ══════════════════════════════════════════════════════
# 個人モデルのステータス & 精度推移
# ══════════════════════════════════════════════════════

@app.get("/api/ml/personal-models")
@limiter.limit("20/minute")
def get_personal_models(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    個人化モデルの学習状況・精度推移を返す。

    フロントエンドでは「データが増えるほど RMSE が下がる」グラフを表示できる。
    """
    n_activities = db.query(Activity).filter(Activity.user_id == current_user.id).count()
    trainer      = PersonalModelTrainer(current_user.id)
    trend        = trainer.model_accuracy_trend()
    latest       = trainer.latest_metrics

    return {
        "n_activities":    n_activities,
        "is_model_ready":  trainer.is_ready,
        "model_type":      latest["hr_model_type"] if latest else None,
        "gb_threshold":    PersonalModelTrainer.GB_THRESHOLD,
        "accuracy_trend":  trend,
        "latest_metrics":  latest,
        "message": _model_status_message(n_activities, trainer.is_ready),
    }


# ══════════════════════════════════════════════════════
# リカバリースコア
# ══════════════════════════════════════════════════════

@app.get("/api/ml/recovery")
@limiter.limit("20/minute")
def get_recovery_score(
    request: Request,
    target_date: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    今日（または指定日）のリカバリースコアを計算して返す。

    スコア構成:
      HRV (35%) + 睡眠 (30%) + 安静時心拍 (20%) + トレーニング負荷 (15%)

    データが蓄積されるほど「個人ベースライン」が精密化される。
    """
    import datetime as dt_mod

    if target_date:
        try:
            d = dt_mod.date.fromisoformat(target_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="日付は YYYY-MM-DD 形式で指定してください")
    else:
        d = dt_mod.date.today()

    # 過去90日のメトリクスを取得してベースライン学習
    ninety_ago = d - dt_mod.timedelta(days=90)
    past_metrics = (
        db.query(DailyMetrics)
        .filter(DailyMetrics.user_id == current_user.id)
        .filter(DailyMetrics.date >= ninety_ago)
        .order_by(DailyMetrics.date)
        .all()
    )

    predictor = RecoveryPredictor()
    for m in past_metrics:
        if m.date < d:
            predictor.update_baseline(m.hrv_rmssd, m.resting_hr, m.sleep_hours)

    # 今日のメトリクス取得
    today_m = (
        db.query(DailyMetrics)
        .filter(DailyMetrics.user_id == current_user.id)
        .filter(DailyMetrics.date == d)
        .first()
    )

    if today_m is None:
        return {
            "date":    d.isoformat(),
            "message": "当日の健康データがありません。Apple Health または Garmin データをアップロードしてください。",
            "recovery_score":  None,
            "readiness_score": None,
        }

    inp = DailyInput(
        hrv_rmssd           = today_m.hrv_rmssd,
        resting_hr          = today_m.resting_hr,
        sleep_hours         = today_m.sleep_hours,
        sleep_quality_score = today_m.sleep_quality_score,
        spo2_pct            = today_m.spo2_pct,
        tsb                 = today_m.tsb,
        atl                 = today_m.atl,
    )
    result = predictor.calculate(inp)

    # 計算結果を DB に保存
    today_m.recovery_score  = result.recovery_score
    today_m.readiness_score = result.readiness_score
    db.commit()

    return {
        "date":             d.isoformat(),
        "recovery_score":   result.recovery_score,
        "readiness_score":  result.readiness_score,
        "status":           result.status,
        "recommendation":   result.recommendation,
        "components":       result.components,
        "confidence":       result.confidence,
        "n_baseline_days":  predictor.baseline.n_days,
        "raw": {
            "hrv_rmssd":    today_m.hrv_rmssd,
            "resting_hr":   today_m.resting_hr,
            "sleep_hours":  today_m.sleep_hours,
            "spo2_pct":     today_m.spo2_pct,
            "tsb":          today_m.tsb,
            "atl":          today_m.atl,
            "ctl":          today_m.ctl,
        },
    }


# ══════════════════════════════════════════════════════
# トレーニング負荷 (ATL/CTL/TSB)
# ══════════════════════════════════════════════════════

@app.get("/api/ml/training-load")
@limiter.limit("20/minute")
def get_training_load(
    request: Request,
    days: int = 90,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    過去 N 日間の ATL/CTL/TSB チャートデータを返す。

    ATL : Acute Training Load（短期疲労、7日 EMA）
    CTL : Chronic Training Load（長期フィットネス、42日 EMA）
    TSB : Training Stress Balance = CTL - ATL（コンディション）

    TSB > +10  → フレッシュ（重要活動に最適）
    TSB  0〜10 → 通常
    TSB < -10  → 疲労蓄積注意
    TSB < -30  → 過負荷（休息推奨）
    """
    import datetime as dt_mod

    days = min(max(days, 14), 365)
    start = dt_mod.date.today() - dt_mod.timedelta(days=days)

    metrics = (
        db.query(DailyMetrics)
        .filter(DailyMetrics.user_id == current_user.id)
        .filter(DailyMetrics.date >= start)
        .order_by(DailyMetrics.date)
        .all()
    )

    chart = [
        {
            "date":   m.date.isoformat(),
            "atl":    round(m.atl, 1) if m.atl else None,
            "ctl":    round(m.ctl, 1) if m.ctl else None,
            "tsb":    round(m.tsb, 1) if m.tsb else None,
            "trimp":  None,
            "status": _tsb_status_label(m.tsb),
        }
        for m in metrics
    ]

    current = get_current_load(
        [(m.date, m.atl or 0) for m in metrics if m.atl]
    )
    current_tsb = current.tsb if current else None

    return {
        "days":        days,
        "chart":       chart,
        "current_tsb": round(current_tsb, 1) if current_tsb is not None else None,
        "current_status": _tsb_status_label(current_tsb) if current_tsb is not None else None,
        "n_data_points":  len(chart),
    }


# ══════════════════════════════════════════════════════
# VO2max トレンド
# ══════════════════════════════════════════════════════

@app.get("/api/ml/vo2max")
@limiter.limit("20/minute")
def get_vo2max_trend(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    累積した活動データから推定した VO2max の時系列を返す。
    アクティビティが増えるほど推定精度が向上する。
    """
    activities = (
        db.query(Activity)
        .filter(Activity.user_id == current_user.id)
        .filter(Activity.vo2max_estimated.isnot(None))
        .order_by(Activity.start_time)
        .all()
    )

    if not activities:
        return {
            "message": "VO2max 推定にはアクティビティデータが必要です。Garmin FIT をアップロードしてください。",
            "trend": [],
            "latest_vo2max": None,
        }

    trend = [
        {
            "date":          a.start_time.date().isoformat() if a.start_time else None,
            "vo2max":        round(a.vo2max_estimated, 1),
            "activity_type": a.activity_type,
        }
        for a in activities
    ]
    latest = trend[-1]["vo2max"] if trend else None

    return {
        "trend":        trend,
        "latest_vo2max":latest,
        "n_estimates":  len(trend),
        "interpretation": _vo2max_interpretation(latest),
    }


# ══════════════════════════════════════════════════════
# 個人 HR・ペース予測（活動中リアルタイム利用を想定）
# ══════════════════════════════════════════════════════

@app.get("/api/ml/predict")
@limiter.limit("30/minute")
def predict_personal(
    request: Request,
    slope_pct:     float = 0.0,
    speed_mps:     float = 1.2,
    hr:            float = 140.0,
    elevation:     float = 1000.0,
    fatigue_index: float = 0.5,
    cum_gain_m:    float = 500.0,
    target_date:   Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    個人化モデルで心拍数・ペースを予測する。

    パラメータ:
      slope_pct     : 斜度 (%)
      speed_mps     : 現在の速度 (m/s)（HR予測用）
      hr            : 現在の心拍数 (bpm)（ペース予測用）
      elevation     : 現在の標高 (m)
      fatigue_index : 疲労度 0〜1（活動経過時間 / 総予定時間）
      cum_gain_m    : 累積獲得標高 (m)
      target_date   : 予測対象日 YYYY-MM-DD（省略時は今日）
                      Apple Health コンテキスト（HRV・安静時HR・睡眠）を当日データから取得する
    """
    import datetime as dt_mod

    trainer = PersonalModelTrainer(current_user.id)
    if not trainer.is_ready:
        return {
            "message":      "個人化モデルはまだ学習されていません。アクティビティデータをアップロードしてください。",
            "predicted_hr": None,
            "predicted_speed_mps": None,
        }

    # ── コンテキスト特徴量の取得（feature v2 モデルの場合）────
    context = None
    context_info: dict = {}
    if trainer._feature_version == 2:
        try:
            pred_date = (
                dt_mod.date.fromisoformat(target_date)
                if target_date else dt_mod.date.today()
            )
            ninety_ago = pred_date - dt_mod.timedelta(days=90)
            daily_metrics = (
                db.query(DailyMetrics)
                .filter(DailyMetrics.user_id == current_user.id)
                .filter(DailyMetrics.date >= ninety_ago)
                .order_by(DailyMetrics.date)
                .all()
            )
            if daily_metrics:
                from models.health_context_features import HealthContextBuilder
                builder = HealthContextBuilder(daily_metrics)
                context = builder.get_context_for_date(pred_date)
                context_info = {
                    "resting_hr_ratio": round(context.resting_hr_ratio, 3),
                    "hrv_zscore":       round(context.hrv_zscore, 2),
                    "sleep_score_norm": round(context.sleep_score_norm, 2),
                    "context_data_available": context.data_available,
                }
        except Exception as e:
            logger.debug("Context fetch failed: %s", e)

    predicted_hr    = trainer.predict_hr(
        slope_pct, speed_mps, elevation, fatigue_index, cum_gain_m, context=context
    )
    predicted_speed = trainer.predict_pace(
        slope_pct, hr, elevation, fatigue_index, context=context
    )

    pace_min_km = (1000.0 / predicted_speed / 60.0) if predicted_speed and predicted_speed > 0.1 else None

    return {
        "predicted_hr":          round(predicted_hr, 1) if predicted_hr else None,
        "predicted_speed_mps":   round(predicted_speed, 3) if predicted_speed else None,
        "predicted_pace_min_km": round(pace_min_km, 2) if pace_min_km else None,
        "model_type":            trainer.latest_metrics.get("hr_model_type") if trainer.latest_metrics else None,
        "feature_version":       trainer._feature_version,
        "n_activities_trained":  trainer._n_activities,
        "context":               context_info if context_info else None,
    }


# ══════════════════════════════════════════════════════
# 手動リトレーニング API
# ══════════════════════════════════════════════════════

@app.post("/api/ml/retrain")
@limiter.limit("5/minute")
def retrain_personal_model(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    個人化モデルを手動で再学習する。

    Apple Health データを新規にアップロードした後などに呼び出す。
    DailyMetrics（HRV・安静時HR・睡眠）が蓄積されていれば、
    健康コンテキスト特徴量を含む feature v2 モデルを学習する。
    """
    result = _retrain_personal_model(current_user.id, db)
    if result is None:
        return {
            "success": False,
            "message": "学習データが不足しています。アクティビティをアップロードしてください。",
        }

    # PMC も再計算（個人化 hr_rest/hr_max で TRIMP を更新）
    _rebuild_pmc_for_user(current_user.id, 60.0, 185.0, db)

    return {
        "success":  True,
        "message":  "個人化モデルの再学習が完了しました",
        "result":   result,
    }


# ══════════════════════════════════════════════════════
# 内部ヘルパー関数
# ══════════════════════════════════════════════════════

async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """アップロードファイルを読み込み、サイズチェックする。"""
    chunks, total = [], 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"ファイルサイズが上限（{max_bytes // 1024 // 1024}MB）を超えています",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _extract_xml_from_zip(data: bytes) -> bytes:
    """ZIP データから export.xml を取り出す。"""
    import zipfile, io
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            xml_name = next((n for n in names if n.endswith("export.xml")), None)
            if not xml_name:
                raise HTTPException(status_code=400, detail="ZIP 内に export.xml が見つかりません")
            return zf.read(xml_name)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="ZIP ファイルの解析に失敗しました")


def _save_activity(norm, user_id: int, db: Session) -> Activity:
    """NormalizedActivity を DB に保存して Activity ORM を返す。"""
    from models.route_fingerprint import compute_gpx_fingerprint

    # GPX フィンガープリントを生成（座標がある場合のみ）
    fingerprint = None
    coords = getattr(norm, "coords", None)
    if coords and len(coords) >= 2:
        try:
            fingerprint = compute_gpx_fingerprint(coords)
        except Exception:
            pass

    activity = Activity(
        user_id          = user_id,
        source           = norm.source,
        activity_type    = norm.activity_type,
        start_time       = norm.start_time,
        duration_sec     = norm.duration_sec,
        distance_m       = norm.distance_m,
        elevation_gain_m = norm.elevation_gain_m,
        elevation_loss_m = norm.elevation_loss_m,
        avg_hr           = norm.avg_hr,
        max_hr           = norm.max_hr,
        avg_speed_mps    = norm.avg_speed_mps,
        calories_kcal    = norm.calories_kcal,
        avg_temp_c       = norm.avg_temp_c,
        hrv_ms           = norm.hrv_ms,
        vo2max_estimated = norm.vo2max_estimated,
        device_name      = norm.device_name,
        gpx_fingerprint  = fingerprint,
    )
    db.add(activity)
    db.commit()
    db.refresh(activity)
    return activity


def _retrain_personal_model(user_id: int, db: Session) -> Optional[dict]:
    """
    全アクティビティポイントで個人化モデルを再学習する。

    DailyMetrics が存在する場合は Apple Health コンテキスト特徴量
    (resting_hr_ratio / hrv_zscore / sleep_score_norm) を追加して
    feature version 2 のモデルを学習する。
    """
    try:
        points_df, n_activities, context_builder = load_points_from_db(
            user_id, db, with_context=True
        )
        if points_df.empty or n_activities == 0:
            return None
        trainer = PersonalModelTrainer(user_id)
        log = trainer.train(points_df, n_activities, context_builder=context_builder)
        result = {
            "n_activities":    log.n_activities,
            "n_points":        log.n_points,
            "hr_mae":          log.hr_mae,
            "hr_r2":           log.hr_r2,
            "pace_mae":        log.pace_mae,
            "model_type":      log.hr_model_type,
            "feature_version": log.feature_version,
            "n_context_days":  log.n_context_days,
        }
        if log.feature_version == 2 and log.n_context_days > 0:
            result["personalization_note"] = (
                f"Apple Health データ {log.n_context_days} 件のコンテキスト特徴量で個人最適化"
            )
        return result
    except Exception as e:
        logger.warning("Personal model retrain failed: %s", e)
        return None


def _update_daily_metrics_from_activity(
    activity: Activity,
    hr_rest: float,
    hr_max: float,
    db: Session,
):
    """アクティビティの TRIMP を DailyMetrics に反映する。"""
    import datetime as dt_mod
    if not activity.start_time:
        return
    d = activity.start_time.date()
    _upsert_daily_trimp(activity.user_id, d, activity.trimp or 0.0, hr_rest, hr_max, db)


def _upsert_daily_metrics(user_id: int, dm_input, db: Session):
    """DailyMetrics を upsert する（同日があれば更新、なければ挿入）。"""
    existing = (
        db.query(DailyMetrics)
        .filter(DailyMetrics.user_id == user_id)
        .filter(DailyMetrics.date == dm_input.date)
        .first()
    )
    if existing:
        if dm_input.resting_hr          is not None: existing.resting_hr          = dm_input.resting_hr
        if dm_input.hrv_rmssd           is not None: existing.hrv_rmssd           = dm_input.hrv_rmssd
        if dm_input.spo2_pct            is not None: existing.spo2_pct            = dm_input.spo2_pct
        if dm_input.vo2max              is not None: existing.vo2max              = dm_input.vo2max
        if dm_input.sleep_hours         is not None: existing.sleep_hours         = dm_input.sleep_hours
        if dm_input.sleep_quality_score is not None: existing.sleep_quality_score = dm_input.sleep_quality_score
        if dm_input.steps               is not None: existing.steps               = dm_input.steps
    else:
        new_m = DailyMetrics(
            user_id             = user_id,
            date                = dm_input.date,
            resting_hr          = dm_input.resting_hr,
            hrv_rmssd           = dm_input.hrv_rmssd,
            spo2_pct            = dm_input.spo2_pct,
            vo2max              = dm_input.vo2max,
            sleep_hours         = dm_input.sleep_hours,
            sleep_quality_score = dm_input.sleep_quality_score,
            steps               = dm_input.steps,
        )
        db.add(new_m)
    db.commit()


def _upsert_daily_trimp(
    user_id: int, d, trimp: float,
    hr_rest: float, hr_max: float,
    db: Session,
):
    """DailyMetrics の TRIMP を更新する。"""
    existing = (
        db.query(DailyMetrics)
        .filter(DailyMetrics.user_id == user_id)
        .filter(DailyMetrics.date == d)
        .first()
    )
    if existing:
        existing.atl = (existing.atl or 0) + trimp
    else:
        db.add(DailyMetrics(user_id=user_id, date=d, atl=trimp))
    db.commit()


def _rebuild_pmc_for_user(user_id: int, hr_rest: float, hr_max: float, db: Session):
    """
    全活動から ATL/CTL/TSB を再計算して DailyMetrics を更新する。

    DailyMetrics に安静時心拍や最大心拍の履歴がある場合は、
    それらを使って TRIMP を個人化する（固定値を使わない）。
    """
    from models.health_context_features import HealthContextBuilder

    # 個人の hr_rest / hr_max を DailyMetrics から推定
    daily_metrics = (
        db.query(DailyMetrics)
        .filter(DailyMetrics.user_id == user_id)
        .order_by(DailyMetrics.date)
        .all()
    )

    personal_hr_rest = hr_rest
    personal_hr_max  = hr_max
    if daily_metrics:
        context_builder = HealthContextBuilder(daily_metrics)
        all_activities_for_max = (
            db.query(Activity)
            .filter(Activity.user_id == user_id)
            .filter(Activity.max_hr.isnot(None))
            .all()
        )
        observed_max = None
        if all_activities_for_max:
            observed_max = max(
                (a.max_hr for a in all_activities_for_max if a.max_hr), default=None
            )
        personal_hr_rest, personal_hr_max = context_builder.personal_hr_params(
            observed_max_hr=observed_max
        )
        logger.info(
            "User %d: personal hr_rest=%.1f hr_max=%.1f (from %d days)",
            user_id, personal_hr_rest, personal_hr_max, context_builder.n_days,
        )

    activities = (
        db.query(Activity)
        .filter(Activity.user_id == user_id)
        .order_by(Activity.start_time)
        .all()
    )
    if not activities:
        return

    # TRIMP を個人化パラメータで再計算
    records = []
    for a in activities:
        if not a.start_time or not a.avg_hr or not a.duration_sec:
            continue
        trimp = calc_trimp(
            duration_min = a.duration_sec / 60.0,
            avg_hr       = a.avg_hr,
            hr_rest      = personal_hr_rest,
            hr_max       = personal_hr_max,
        )
        if trimp > 0:
            a.trimp = round(trimp, 2)
            records.append((a.start_time.date(), trimp))

    if records:
        db.commit()

    pmc = build_pmc(records)
    for entry in pmc:
        existing = (
            db.query(DailyMetrics)
            .filter(DailyMetrics.user_id == user_id)
            .filter(DailyMetrics.date == entry.date)
            .first()
        )
        if existing:
            existing.atl = entry.atl
            existing.ctl = entry.ctl
            existing.tsb = entry.tsb
        else:
            db.add(DailyMetrics(
                user_id = user_id,
                date    = entry.date,
                atl     = entry.atl,
                ctl     = entry.ctl,
                tsb     = entry.tsb,
            ))
    db.commit()


# ── ラベル & メッセージ生成 ───────────────────────────

def _tsb_status_label(tsb: Optional[float]) -> Optional[str]:
    if tsb is None:
        return None
    if tsb >= 10:   return "フレッシュ"
    elif tsb >= 0:  return "通常"
    elif tsb >= -10: return "軽度疲労"
    elif tsb >= -30: return "高負荷"
    else:           return "過負荷"


def _model_status_message(n_activities: int, is_ready: bool) -> str:
    if n_activities == 0:
        return "アクティビティデータがありません。Garmin FIT か Apple Health をアップロードしてください。"
    elif n_activities < PersonalModelTrainer.GB_THRESHOLD:
        remaining = PersonalModelTrainer.GB_THRESHOLD - n_activities
        return (f"{n_activities}件のアクティビティで学習中（BayesianRidge）。"
                f"あと{remaining}件で高精度モデル（GradientBoosting）に切り替わります。")
    else:
        return (f"{n_activities}件のアクティビティで個人化モデルを学習済みです（GradientBoosting）。"
                f"データが増えるほど精度が向上します。")


def _vo2max_interpretation(vo2max: Optional[float]) -> str:
    if vo2max is None:
        return "データなし"
    if vo2max >= 55:   return "非常に高い（エリートレベル）"
    elif vo2max >= 48: return "高い（上位アスリートレベル）"
    elif vo2max >= 42: return "平均以上（活動的な一般人）"
    elif vo2max >= 35: return "平均的"
    else:              return "改善の余地あり（有酸素トレーニングを推奨）"


# ══════════════════════════════════════════════════════
# アクティビティカレンダー（GitHub草スタイル用）
# ══════════════════════════════════════════════════════

@app.get("/api/ml/activity-calendar")
@limiter.limit("30/minute")
def get_activity_calendar(
    request: Request,
    year: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    指定年のアクティビティを日次集計して返す。
    GitHub の草グラフ（コントリビューションカレンダー）スタイルで
    フロントエンドが描画するためのデータソース。

    色の強度は TRIMP（トレーニング負荷）で決まる。
    データが蓄積されるほど精密な疲労可視化が可能になる。
    """
    import datetime as dt_mod

    if year is None:
        year = dt_mod.date.today().year
    if not (2000 <= year <= 2100):
        raise HTTPException(status_code=400, detail="year は 2000〜2100 の範囲で指定してください")

    activities = (
        db.query(Activity)
        .filter(Activity.user_id == current_user.id)
        .filter(Activity.start_time >= dt_mod.datetime(year, 1, 1, tzinfo=_dt.timezone.utc))
        .filter(Activity.start_time < dt_mod.datetime(year + 1, 1, 1, tzinfo=_dt.timezone.utc))
        .order_by(Activity.start_time)
        .all()
    )

    # 日次集計
    by_date: dict[str, dict] = {}
    for a in activities:
        if not a.start_time:
            continue
        d = a.start_time.date().isoformat()
        if d not in by_date:
            by_date[d] = {
                "date":             d,
                "trimp":            0.0,
                "duration_min":     0.0,
                "n_activities":     0,
                "activity_types":   [],
                "elevation_gain_m": 0.0,
                "avg_hr":           [],
            }
        by_date[d]["trimp"]            += float(a.trimp or 0)
        by_date[d]["duration_min"]     += float(a.duration_sec or 0) / 60
        by_date[d]["n_activities"]     += 1
        if a.activity_type not in by_date[d]["activity_types"]:
            by_date[d]["activity_types"].append(a.activity_type)
        by_date[d]["elevation_gain_m"] += float(a.elevation_gain_m or 0)
        if a.avg_hr:
            by_date[d]["avg_hr"].append(a.avg_hr)

    days = []
    for entry in by_date.values():
        avg_hr_list = entry.pop("avg_hr")
        entry["avg_hr"]           = round(sum(avg_hr_list) / len(avg_hr_list), 0) if avg_hr_list else None
        entry["trimp"]            = round(entry["trimp"], 1)
        entry["duration_min"]     = round(entry["duration_min"], 0)
        entry["elevation_gain_m"] = round(entry["elevation_gain_m"], 0)
        days.append(entry)

    days.sort(key=lambda d: d["date"])

    max_trimp    = max((d["trimp"]        for d in days), default=0)
    max_duration = max((d["duration_min"] for d in days), default=0)
    total_days   = len(days)
    total_trimp  = round(sum(d["trimp"]        for d in days), 1)
    total_dur_h  = round(sum(d["duration_min"] for d in days) / 60, 1)
    total_gain   = round(sum(d["elevation_gain_m"] for d in days), 0)

    # 利用可能な年リスト（アクティビティが存在する年）
    all_years = sorted(set(
        a.start_time.year for a in
        db.query(Activity).filter(Activity.user_id == current_user.id).all()
        if a.start_time
    ), reverse=True) or [dt_mod.date.today().year]

    return {
        "year":            year,
        "days":            days,
        "max_trimp":       max_trimp,
        "max_duration":    max_duration,
        "total_days":      total_days,
        "total_trimp":     total_trimp,
        "total_duration_h":total_dur_h,
        "total_elevation_gain_m": total_gain,
        "available_years": all_years,
    }


# ══════════════════════════════════════════════════════
# ダッシュボード（今の体の状態サマリー）
# ══════════════════════════════════════════════════════

@app.get("/api/dashboard")
@limiter.limit("30/minute")
def get_dashboard_summary(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    ダッシュボード用の統合サマリーを1リクエストで返す。

    含まれる情報:
      - 今日のコンディション（リカバリースコア・TSB・VO2max・睡眠）
      - 直近7日のトレンド（HRV・安静時心拍・ATL）
      - 最近のアクティビティ一覧（直近10件）
      - 個人化モデルの状態
      - アクティビティ統計（累計・種類別）
    """
    import datetime as dt_mod

    today      = dt_mod.date.today()
    week_ago   = today - dt_mod.timedelta(days=7)
    month_ago  = today - dt_mod.timedelta(days=30)

    # ── 最新デイリーメトリクス ───────────────────────
    latest_dm = (
        db.query(DailyMetrics)
        .filter(DailyMetrics.user_id == current_user.id)
        .order_by(DailyMetrics.date.desc())
        .first()
    )

    # ── リカバリースコア計算 ─────────────────────────
    past_90 = (
        db.query(DailyMetrics)
        .filter(DailyMetrics.user_id == current_user.id)
        .filter(DailyMetrics.date >= today - dt_mod.timedelta(days=90))
        .order_by(DailyMetrics.date)
        .all()
    )
    predictor = RecoveryPredictor()
    for m in past_90:
        if m.date < today:
            predictor.update_baseline(m.hrv_rmssd, m.resting_hr, m.sleep_hours)

    recovery_result = None
    if latest_dm:
        inp = DailyInput(
            hrv_rmssd           = latest_dm.hrv_rmssd,
            resting_hr          = latest_dm.resting_hr,
            sleep_hours         = latest_dm.sleep_hours,
            sleep_quality_score = latest_dm.sleep_quality_score,
            spo2_pct            = latest_dm.spo2_pct,
            tsb                 = latest_dm.tsb,
            atl                 = latest_dm.atl,
        )
        r = predictor.calculate(inp)
        recovery_result = {
            "score":          r.recovery_score,
            "readiness":      r.readiness_score,
            "status":         r.status,
            "recommendation": r.recommendation,
            "components":     r.components,
        }

    # ── 7日トレンド ──────────────────────────────────
    weekly = (
        db.query(DailyMetrics)
        .filter(DailyMetrics.user_id == current_user.id)
        .filter(DailyMetrics.date >= week_ago)
        .order_by(DailyMetrics.date)
        .all()
    )
    trend_7d = {
        "dates":       [m.date.isoformat()                  for m in weekly],
        "hrv":         [m.hrv_rmssd                         for m in weekly],
        "resting_hr":  [m.resting_hr                        for m in weekly],
        "atl":         [m.atl                               for m in weekly],
        "recovery":    [m.recovery_score                    for m in weekly],
        "sleep":       [m.sleep_hours                       for m in weekly],
    }

    # ── 最近のアクティビティ ─────────────────────────
    recent_acts = (
        db.query(Activity)
        .filter(Activity.user_id == current_user.id)
        .order_by(Activity.start_time.desc())
        .limit(10)
        .all()
    )
    recent = [
        {
            "id":              a.id,
            "date":            a.start_time.date().isoformat() if a.start_time else None,
            "activity_type":   a.activity_type,
            "duration_min":    round((a.duration_sec or 0) / 60, 0),
            "distance_km":     round((a.distance_m or 0) / 1000, 2),
            "elevation_gain_m":round(a.elevation_gain_m or 0, 0),
            "avg_hr":          a.avg_hr,
            "trimp":           round(a.trimp, 1) if a.trimp else None,
            "source":          a.source,
        }
        for a in recent_acts
    ]

    # ── 累計統計 ─────────────────────────────────────
    all_acts = db.query(Activity).filter(Activity.user_id == current_user.id).all()
    type_counts: dict[str, int] = {}
    for a in all_acts:
        type_counts[a.activity_type] = type_counts.get(a.activity_type, 0) + 1
    stats = {
        "total_activities":     len(all_acts),
        "total_duration_h":     round(sum((a.duration_sec or 0) for a in all_acts) / 3600, 1),
        "total_distance_km":    round(sum((a.distance_m or 0) for a in all_acts) / 1000, 1),
        "total_elevation_gain_m":round(sum((a.elevation_gain_m or 0) for a in all_acts), 0),
        "by_type":              type_counts,
    }

    # ── 最新 VO2max ──────────────────────────────────
    latest_vo2 = (
        db.query(Activity)
        .filter(Activity.user_id == current_user.id)
        .filter(Activity.vo2max_estimated.isnot(None))
        .order_by(Activity.start_time.desc())
        .first()
    )

    # ── モデル状態 ───────────────────────────────────
    n_acts   = len(all_acts)
    trainer  = PersonalModelTrainer(current_user.id)
    latest_m = trainer.latest_metrics

    return {
        "today": {
            "date":           today.isoformat(),
            "recovery_score": recovery_result,
            "resting_hr":     latest_dm.resting_hr          if latest_dm else None,
            "hrv_rmssd":      latest_dm.hrv_rmssd            if latest_dm else None,
            "sleep_hours":    latest_dm.sleep_hours          if latest_dm else None,
            "sleep_quality":  latest_dm.sleep_quality_score  if latest_dm else None,
            "spo2_pct":       latest_dm.spo2_pct             if latest_dm else None,
            "atl":            latest_dm.atl                  if latest_dm else None,
            "ctl":            latest_dm.ctl                  if latest_dm else None,
            "tsb":            latest_dm.tsb                  if latest_dm else None,
            "tsb_status":     _tsb_status_label(latest_dm.tsb if latest_dm else None),
            "vo2max":         latest_vo2.vo2max_estimated     if latest_vo2 else None,
            "vo2max_interp":  _vo2max_interpretation(latest_vo2.vo2max_estimated if latest_vo2 else None),
        },
        "trend_7d":         trend_7d,
        "recent_activities":recent,
        "stats":            stats,
        "model": {
            "n_activities":  n_acts,
            "is_ready":      trainer.is_ready,
            "model_type":    latest_m.get("hr_model_type") if latest_m else None,
            "hr_mae":        latest_m.get("hr_mae")        if latest_m else None,
        },
        "n_baseline_days": predictor.baseline.n_days,
    }


# ══════════════════════════════════════════════════════
# [新機能1] 補給タイミングアラート
# ══════════════════════════════════════════════════════

class SupplyTimingRequest(BaseModel):
    sim_result:          dict   # /api/simulate の出力をそのまま受け取る
    race_start:          str    # ISO形式: "2026-08-01T09:00"
    current_elapsed_min: float  = Field(0.0, ge=0)

    @field_validator("race_start")
    @classmethod
    def race_start_must_be_iso(cls, v: str) -> str:
        try:
            _dt.datetime.fromisoformat(v)
        except ValueError:
            raise ValueError("race_start は ISO 形式が必要です（例: 2026-08-01T09:00）")
        return v


@app.post("/api/alerts/supply-timing")
@limiter.limit("30/minute")
def get_supply_timing_alerts(request: Request, req: SupplyTimingRequest):
    """
    シミュレーション結果をもとに次回補給タイミングを返す。
    current_elapsed_min を更新しながら呼び出すことでリアルタイム補給ガイドを実現できる。
    """
    try:
        race_start = _dt.datetime.fromisoformat(req.race_start)
    except Exception:
        raise HTTPException(status_code=400, detail="race_start の日時形式が無効です")

    sim = req.sim_result
    supply_plan = sim.get("supply_plan", [])
    alert_times = sim.get("alert_times", [])

    # current_elapsed_min より後の補給イベント
    remaining_supply = [
        s for s in supply_plan
        if s.get("time_min", 0) > req.current_elapsed_min
    ]
    remaining_alerts = [
        t for t in alert_times
        if t > req.current_elapsed_min
    ]

    next_supply = None
    if remaining_supply:
        ns = remaining_supply[0]
        at_min = ns["time_min"]
        in_min = at_min - req.current_elapsed_min
        at_clock = (race_start + _dt.timedelta(minutes=at_min)).strftime("%H:%M")
        next_supply = {
            "time_min":      round(at_min, 1),
            "in_min":        round(in_min, 1),
            "at_clock":      at_clock,
            "food":          ns.get("food", "エネルギージェル"),
            "kcal":          ns.get("kcal", 100),
            "reason":        f"エネルギー警告まで約 {round(in_min + 20)} 分",
        }

    # 現在のエネルギー残量推定（線形補間）
    time_arr = sim.get("time_min", [])
    e_arr    = sim.get("e_kcal", [])
    current_e = None
    if time_arr and e_arr and len(time_arr) == len(e_arr):
        t = req.current_elapsed_min
        for i in range(len(time_arr) - 1):
            if time_arr[i] <= t <= time_arr[i + 1]:
                frac = (t - time_arr[i]) / max(time_arr[i + 1] - time_arr[i], 0.01)
                current_e = e_arr[i] + frac * (e_arr[i + 1] - e_arr[i])
                break

    return {
        "current_elapsed_min":  req.current_elapsed_min,
        "next_supply":          next_supply,
        "remaining_supply_count": len(remaining_supply),
        "remaining_alerts":     remaining_alerts,
        "current_e_kcal":       round(current_e, 1) if current_e is not None else None,
        "all_supply_plan":      supply_plan,
    }


# ══════════════════════════════════════════════════════
# [新機能2] 水分補給量計算
# ══════════════════════════════════════════════════════

from models.hydration_model import estimate_hydration, HydrationInput

class HydrationRequest(BaseModel):
    temp_c:           float = Field(...,  ge=-20, le=60)
    humidity_pct:     float = Field(60.0, ge=0,   le=100)
    duration_min:     float = Field(...,  ge=10,  le=2880)
    intensity_factor: float = Field(1.0,  ge=0.5, le=1.5)
    body_weight_kg:   float = Field(65.0, ge=20,  le=300)
    activity_type:    str   = Field("hiking", pattern="^(hiking|running|cycling)$")


@app.post("/api/hydration")
@limiter.limit("30/minute")
def calc_hydration(request: Request, req: HydrationRequest):
    """
    気温・湿度・強度から必要水分量と補給タイムラインを計算する。
    """
    inp = HydrationInput(
        temp_c           = req.temp_c,
        humidity_pct     = req.humidity_pct,
        duration_min     = req.duration_min,
        intensity_factor = req.intensity_factor,
        body_weight_kg   = req.body_weight_kg,
        activity_type    = req.activity_type,
    )
    result = estimate_hydration(inp)
    return {
        "total_ml":               result.total_ml,
        "rate_ml_per_hour":       result.rate_ml_per_hour,
        "sweat_rate_ml_per_hour": result.sweat_rate_ml_per_hour,
        "electrolyte_na_mg":      result.electrolyte_na_mg,
        "warning":                result.warning,
        "schedule": [
            {
                "at_min":    s.at_min,
                "amount_ml": s.amount_ml,
                "note":      s.note,
            }
            for s in result.schedule
        ],
    }


# ══════════════════════════════════════════════════════
# [新機能4] 週次/月次レポート生成
# ══════════════════════════════════════════════════════

import sys as _sys2
_sys2.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models.report_generator_periodic import (
    generate_weekly_report_pdf, generate_monthly_report_pdf,
)


class ReportPeriodRequest(BaseModel):
    period_type: Literal["weekly", "monthly"]
    year:  int           = Field(..., ge=2020, le=2030)
    week:  Optional[int] = Field(None, ge=1, le=53)   # 週次の場合
    month: Optional[int] = Field(None, ge=1, le=12)   # 月次の場合

    @model_validator(mode="after")
    def validate_period(self) -> "ReportPeriodRequest":
        if self.period_type == "weekly" and self.week is None:
            raise ValueError("period_type=weekly の場合は week が必要です")
        if self.period_type == "monthly" and self.month is None:
            raise ValueError("period_type=monthly の場合は month が必要です")
        return self


@app.post("/api/reports/generate")
@limiter.limit("5/minute")
def generate_report(
    request:      Request,
    req:          ReportPeriodRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    指定期間の週次または月次 PDF レポートを生成してダウンロードさせる。
    """
    import datetime as dt_mod

    if req.period_type == "weekly":
        # ISO カレンダー週の月曜〜日曜
        monday    = _dt.date.fromisocalendar(req.year, req.week, 1)
        sunday    = monday + _dt.timedelta(days=6)
        start_dt  = _dt.datetime(monday.year, monday.month, monday.day, tzinfo=_dt.timezone.utc)
        end_dt    = _dt.datetime(sunday.year, sunday.month, sunday.day, 23, 59, 59, tzinfo=_dt.timezone.utc)
        label     = f"{req.year}年第{req.week}週 ({monday.strftime('%m/%d')}〜{sunday.strftime('%m/%d')})"
        filename  = f"report_weekly_{req.year}W{req.week:02d}.pdf"
    else:
        start_dt  = _dt.datetime(req.year, req.month, 1, tzinfo=_dt.timezone.utc)
        if req.month == 12:
            end_dt = _dt.datetime(req.year + 1, 1, 1, tzinfo=_dt.timezone.utc) - _dt.timedelta(seconds=1)
        else:
            end_dt = _dt.datetime(req.year, req.month + 1, 1, tzinfo=_dt.timezone.utc) - _dt.timedelta(seconds=1)
        label    = f"{req.year}年{req.month}月"
        filename = f"report_monthly_{req.year}{req.month:02d}.pdf"

    activities = (
        db.query(Activity)
        .filter(Activity.user_id == current_user.id)
        .filter(Activity.start_time >= start_dt)
        .filter(Activity.start_time <= end_dt)
        .order_by(Activity.start_time)
        .all()
    )

    # DailyMetrics は period よりやや広めに取得（ATL/CTL/TSB の文脈のため）
    dm_start = start_dt.date() - _dt.timedelta(days=7)
    daily_metrics = (
        db.query(DailyMetrics)
        .filter(DailyMetrics.user_id == current_user.id)
        .filter(DailyMetrics.date >= dm_start)
        .filter(DailyMetrics.date <= end_dt.date())
        .order_by(DailyMetrics.date)
        .all()
    )

    try:
        if req.period_type == "weekly":
            pdf_bytes = generate_weekly_report_pdf(
                activities, daily_metrics, label, current_user.display_name
            )
        else:
            pdf_bytes = generate_monthly_report_pdf(
                activities, daily_metrics, label, current_user.display_name
            )
    except Exception as e:
        logger.error("Report generation error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="レポートの生成中にエラーが発生しました")

    return Response(
        content     = pdf_bytes,
        media_type  = "application/pdf",
        headers     = {"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/reports/available-periods")
@limiter.limit("20/minute")
def get_available_periods(
    request:      Request,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """データが存在する週・月の一覧を返す（レポート生成対象として表示用）。"""
    activities = (
        db.query(Activity)
        .filter(Activity.user_id == current_user.id)
        .filter(Activity.start_time.isnot(None))
        .all()
    )
    weeks: set = set()
    months: set = set()
    for a in activities:
        if a.start_time:
            d = a.start_time.date()
            iso = d.isocalendar()
            weeks.add((iso[0], iso[1]))
            months.add((d.year, d.month))

    return {
        "weeks":  sorted(
            [{"year": y, "week": w} for y, w in weeks],
            key=lambda x: (x["year"], x["week"]),
            reverse=True,
        ),
        "months": sorted(
            [{"year": y, "month": m} for y, m in months],
            key=lambda x: (x["year"], x["month"]),
            reverse=True,
        ),
    }


# ══════════════════════════════════════════════════════
# [新機能5] 比較ダッシュボード（同一ルート重ね合わせ）
# ══════════════════════════════════════════════════════

from models.route_fingerprint import compute_gpx_fingerprint


@app.get("/api/routes/my-routes")
@limiter.limit("20/minute")
def get_my_routes(
    request:      Request,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """ユーザーのユニークなルート一覧（fingerprint でグループ化）。"""
    activities = (
        db.query(Activity)
        .filter(Activity.user_id == current_user.id)
        .filter(Activity.gpx_fingerprint.isnot(None))
        .order_by(Activity.start_time.desc())
        .all()
    )

    grouped: dict = {}
    for a in activities:
        fp = a.gpx_fingerprint
        if fp not in grouped:
            grouped[fp] = {
                "fingerprint":   fp,
                "n_activities":  0,
                "activity_type": a.activity_type,
                "last_date":     None,
                "best_duration_min": None,
                "avg_hr_list":   [],
            }
        g = grouped[fp]
        g["n_activities"] += 1
        d = a.start_time.date().isoformat() if a.start_time else None
        if d and (g["last_date"] is None or d > g["last_date"]):
            g["last_date"] = d
        dur = (a.duration_sec or 0) / 60
        if dur > 0 and (g["best_duration_min"] is None or dur < g["best_duration_min"]):
            g["best_duration_min"] = round(dur, 1)
        if a.avg_hr:
            g["avg_hr_list"].append(a.avg_hr)

    result = []
    for g in grouped.values():
        avg_hr_list = g.pop("avg_hr_list")
        g["avg_hr"] = round(sum(avg_hr_list) / len(avg_hr_list), 1) if avg_hr_list else None
        result.append(g)

    result.sort(key=lambda x: x["last_date"] or "", reverse=True)
    return {"routes": result}


@app.get("/api/routes/{fingerprint}/history")
@limiter.limit("20/minute")
def get_route_history(
    request:      Request,
    fingerprint:  str,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """同一フィンガープリントの過去アクティビティサマリー一覧。"""
    activities = (
        db.query(Activity)
        .filter(Activity.user_id == current_user.id)
        .filter(Activity.gpx_fingerprint == fingerprint)
        .order_by(Activity.start_time.desc())
        .all()
    )
    return {
        "fingerprint": fingerprint,
        "activities": [
            {
                "id":              a.id,
                "date":            a.start_time.date().isoformat() if a.start_time else None,
                "activity_type":   a.activity_type,
                "duration_min":    round((a.duration_sec or 0) / 60, 1),
                "distance_km":     round((a.distance_m or 0) / 1000, 2),
                "elevation_gain_m":round(a.elevation_gain_m or 0, 0),
                "avg_hr":          a.avg_hr,
                "max_hr":          a.max_hr,
                "trimp":           round(a.trimp, 1) if a.trimp else None,
                "source":          a.source,
            }
            for a in activities
        ],
    }


@app.get("/api/routes/{fingerprint}/compare")
@limiter.limit("10/minute")
def compare_route_performances(
    request:      Request,
    fingerprint:  str,
    ids:          str   = "",   # カンマ区切りの activity_id
    current_user: User  = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    指定アクティビティIDsの時系列データ（HR・ペース・標高）を返す。
    フロントエンドで折れ線グラフを重ね合わせ表示するためのデータソース。
    """
    try:
        id_list = [int(x.strip()) for x in ids.split(",") if x.strip()][:5]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids はカンマ区切りの整数で指定してください")

    if not id_list:
        raise HTTPException(status_code=400, detail="ids を指定してください")

    series_list = []
    for act_id in id_list:
        act = db.query(Activity).filter(
            Activity.id == act_id,
            Activity.user_id == current_user.id,
            Activity.gpx_fingerprint == fingerprint,
        ).first()
        if not act:
            continue

        points = (
            db.query(ActivityPoint)
            .filter(ActivityPoint.activity_id == act_id)
            .order_by(ActivityPoint.elapsed_sec)
            .all()
        )
        series_list.append({
            "activity_id":   act_id,
            "date":          act.start_time.date().isoformat() if act.start_time else None,
            "duration_min":  round((act.duration_sec or 0) / 60, 1),
            "points": [
                {
                    "elapsed_min": round(p.elapsed_sec / 60, 1),
                    "hr":         p.hr,
                    "speed_mps":  p.speed_mps,
                    "elevation":  p.elevation,
                    "slope_pct":  p.slope_pct,
                }
                for p in points
            ],
        })

    return {"fingerprint": fingerprint, "series": series_list}


# ══════════════════════════════════════════════════════
# [新機能6] ピア比較（匿名）
# ══════════════════════════════════════════════════════

from backend.models_db import PeerStatsCache as _PeerStatsCache


def _distance_bucket(km: float) -> str:
    if km < 10:   return "0-10km"
    elif km < 20: return "10-20km"
    elif km < 40: return "20-40km"
    elif km < 80: return "40-80km"
    else:         return "80km+"


def _gain_bucket(m: float) -> str:
    if m < 300:    return "0-300m"
    elif m < 800:  return "300-800m"
    elif m < 1500: return "800-1500m"
    elif m < 3000: return "1500-3000m"
    else:          return "3000m+"


_VALID_ACTIVITY_TYPES = {"hiking", "running", "cycling", "trail_running", "walking", "swimming"}

@app.get("/api/peers/compare")
@limiter.limit("20/minute")
def get_peer_comparison(
    request:          Request,
    activity_type:    str   = "hiking",
    distance_km:      float = Query(20.0, ge=0),
    elevation_gain_m: float = Query(500.0, ge=0),
    current_user:     User  = Depends(get_current_user),
    db:               Session = Depends(get_db),
):
    """
    同条件ユーザーの集団統計と自分の統計を比較して返す。
    ユーザー数が5人未満の場合はプライバシー保護のため返さない。
    """
    import datetime as dt_mod

    if activity_type not in _VALID_ACTIVITY_TYPES:
        raise HTTPException(status_code=422, detail=f"無効なアクティビティタイプです: {activity_type!r}")

    dist_b = _distance_bucket(distance_km)
    gain_b = _gain_bucket(elevation_gain_m)
    seg_key = f"{activity_type}_{dist_b}_{gain_b}"

    # キャッシュを確認（12時間以内なら再利用）
    cache = db.query(_PeerStatsCache).filter(
        _PeerStatsCache.segment_key == seg_key
    ).first()

    if cache is None or (
        _dt.datetime.utcnow() - cache.updated_at.replace(tzinfo=None)
        > _dt.timedelta(hours=12)
    ):
        # 全ユーザーから同条件のアクティビティを集計
        dist_lo = {"0-10km": 0, "10-20km": 10, "20-40km": 20, "40-80km": 40, "80km+": 80}.get(dist_b, 0) * 1000
        dist_hi = {"0-10km": 10, "10-20km": 20, "20-40km": 40, "40-80km": 80, "80km+": 1e9}.get(dist_b, 1e9) * 1000
        gain_lo = {"0-300m": 0, "300-800m": 300, "800-1500m": 800, "1500-3000m": 1500, "3000m+": 3000}.get(gain_b, 0)
        gain_hi = {"0-300m": 300, "300-800m": 800, "800-1500m": 1500, "1500-3000m": 3000, "3000m+": 1e9}.get(gain_b, 1e9)

        peers = (
            db.query(Activity)
            .filter(Activity.activity_type == activity_type)
            .filter(Activity.distance_m >= dist_lo)
            .filter(Activity.distance_m < dist_hi)
            .filter(Activity.elevation_gain_m >= gain_lo)
            .filter(Activity.elevation_gain_m < gain_hi)
            .all()
        )

        user_ids = set(a.user_id for a in peers)
        if len(user_ids) < 5:
            return {
                "segment":  seg_key,
                "n_peers":  len(user_ids),
                "message":  "同条件のユーザーが少なすぎます（最低5人必要）。",
                "peer_stats": None,
            }

        durations = sorted([(a.duration_sec or 0) / 60 for a in peers if a.duration_sec])
        avg_hrs   = [a.avg_hr for a in peers if a.avg_hr]
        avg_paces = [a.avg_speed_mps for a in peers if a.avg_speed_mps]

        def _percentile(data, pct):
            if not data:
                return None
            idx = int(len(data) * pct / 100)
            return round(data[min(idx, len(data)-1)], 1)

        peer_stats = {
            "p25_duration": _percentile(durations, 25),
            "p50_duration": _percentile(durations, 50),
            "p75_duration": _percentile(durations, 75),
            "avg_hr":       round(sum(avg_hrs) / len(avg_hrs), 1) if avg_hrs else None,
            "avg_pace_mps": round(sum(avg_paces) / len(avg_paces), 3) if avg_paces else None,
            "n_users":      len(user_ids),
        }

        # キャッシュ更新
        if cache:
            cache.n_users          = len(user_ids)
            cache.p25_duration     = peer_stats["p25_duration"]
            cache.p50_duration     = peer_stats["p50_duration"]
            cache.p75_duration     = peer_stats["p75_duration"]
            cache.median_hr        = peer_stats["avg_hr"]
            cache.avg_pace_mps     = peer_stats["avg_pace_mps"]
        else:
            db.add(_PeerStatsCache(
                segment_key    = seg_key,
                n_users        = len(user_ids),
                p25_duration   = peer_stats["p25_duration"],
                p50_duration   = peer_stats["p50_duration"],
                p75_duration   = peer_stats["p75_duration"],
                median_hr      = peer_stats["avg_hr"],
                avg_pace_mps   = peer_stats["avg_pace_mps"],
            ))
        db.commit()
    else:
        peer_stats = {
            "p25_duration": cache.p25_duration,
            "p50_duration": cache.p50_duration,
            "p75_duration": cache.p75_duration,
            "avg_hr":       cache.median_hr,
            "avg_pace_mps": cache.avg_pace_mps,
            "n_users":      cache.n_users,
        }

    # 自分の同条件アクティビティ
    my_acts = (
        db.query(Activity)
        .filter(Activity.user_id == current_user.id)
        .filter(Activity.activity_type == activity_type)
        .all()
    )
    my_durations = sorted([(a.duration_sec or 0) / 60 for a in my_acts if a.duration_sec])
    my_hrs       = [a.avg_hr for a in my_acts if a.avg_hr]
    my_stats = {
        "avg_duration_min": round(sum(my_durations) / len(my_durations), 1) if my_durations else None,
        "avg_hr":           round(sum(my_hrs) / len(my_hrs), 1) if my_hrs else None,
    }

    # パーセンタイル計算（自分がどの位置か）
    dur_percentile = None
    if my_stats["avg_duration_min"] and peer_stats["p50_duration"]:
        p25 = peer_stats["p25_duration"] or peer_stats["p50_duration"]
        p75 = peer_stats["p75_duration"] or peer_stats["p50_duration"]
        rng = max(p75 - p25, 1.0)
        dur_percentile = int(max(0, min(100,
            50 + (peer_stats["p50_duration"] - my_stats["avg_duration_min"]) / rng * 25
        )))

    return {
        "segment":         seg_key,
        "n_peers":         peer_stats["n_users"],
        "your_stats":      my_stats,
        "peer_stats":      peer_stats,
        "percentile":      {"duration": dur_percentile},
        "interpretation":  _peer_interpretation(dur_percentile),
    }


def _peer_interpretation(pct: Optional[int]) -> str:
    if pct is None:
        return "比較データなし"
    if pct >= 75:
        return "同条件ユーザーの上位25%の速さです"
    elif pct >= 50:
        return "同条件ユーザーの平均より速いです"
    elif pct >= 25:
        return "同条件ユーザーの平均より少し遅めです"
    else:
        return "同条件ユーザーの下位25%です。トレーニングで改善できます"


# ══════════════════════════════════════════════════════
# [新機能7] Feature importance 可視化
# ══════════════════════════════════════════════════════

@app.get("/api/ml/feature-importance")
@limiter.limit("20/minute")
def get_feature_importance(
    request:      Request,
    current_user: User    = Depends(get_current_user),
):
    """
    個人化モデルの特徴量重要度を返す。
    GBT: feature_importances_ / BayesianRidge: |coef_| 正規化値。
    「あなたのペースに最も影響する要因ランキング」として表示できる。
    """
    trainer = PersonalModelTrainer(current_user.id)
    if not trainer.is_ready:
        raise HTTPException(
            status_code=404,
            detail="個人化モデルはまだ学習されていません。アクティビティをアップロードしてください。",
        )
    result = trainer.get_feature_importances()
    if not result.get("hr") and not result.get("pace"):
        raise HTTPException(status_code=404, detail="特徴量重要度を取得できませんでした。")
    return result


# ══════════════════════════════════════════════════════
# [新機能8] 予測精度フィードバックループ
# ══════════════════════════════════════════════════════

from backend.models_db import PredictionFeedback


class FeedbackCreate(BaseModel):
    activity_id:     Optional[int] = None
    prediction_type: Literal["hr", "pace", "completion", "total_kcal", "eta_min"]
    predicted_value: float
    actual_value:    float
    predicted_at:    str   = Field(..., max_length=50)
    notes:           Optional[str] = Field(None, max_length=500)

    @field_validator("predicted_at")
    @classmethod
    def predicted_at_must_be_iso(cls, v: str) -> str:
        try:
            _dt.datetime.fromisoformat(v)
        except ValueError:
            raise ValueError("predicted_at は ISO 形式が必要です")
        return v


@app.post("/api/ml/feedback")
@limiter.limit("20/minute")
def create_feedback(
    request:      Request,
    req:          FeedbackCreate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """予測値と実績値のフィードバックを記録する。"""
    predicted_at = _dt.datetime.fromisoformat(req.predicted_at)
    error_abs = abs(req.actual_value - req.predicted_value)
    error_pct = error_abs / max(abs(req.predicted_value), 1e-6) * 100

    fb = PredictionFeedback(
        user_id          = current_user.id,
        activity_id      = req.activity_id,
        prediction_type  = req.prediction_type,
        predicted_value  = req.predicted_value,
        predicted_at     = predicted_at,
        actual_value     = req.actual_value,
        feedback_source  = "manual",
        feedback_at      = _dt.datetime.utcnow(),
        error_abs        = round(error_abs, 4),
        error_pct        = round(error_pct, 2),
        notes            = req.notes,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)

    return {
        "id":             fb.id,
        "prediction_type":fb.prediction_type,
        "predicted_value":fb.predicted_value,
        "actual_value":   fb.actual_value,
        "error_abs":      fb.error_abs,
        "error_pct":      round(fb.error_pct, 2),
        "message":        "フィードバックを記録しました。モデルの精度改善に活用されます。",
    }


@app.get("/api/ml/feedback")
@limiter.limit("20/minute")
def get_feedback_history(
    request:         Request,
    prediction_type: Optional[str] = None,
    limit:           int = 50,
    current_user:    User    = Depends(get_current_user),
    db:              Session = Depends(get_db),
):
    """フィードバック履歴一覧（prediction_type でフィルタ可能）。"""
    limit = min(limit, 200)
    q = db.query(PredictionFeedback).filter(
        PredictionFeedback.user_id == current_user.id
    )
    if prediction_type:
        q = q.filter(PredictionFeedback.prediction_type == prediction_type)
    records = q.order_by(PredictionFeedback.created_at.desc()).limit(limit).all()

    return {
        "records": [
            {
                "id":              r.id,
                "prediction_type": r.prediction_type,
                "predicted_value": r.predicted_value,
                "actual_value":    r.actual_value,
                "error_abs":       r.error_abs,
                "error_pct":       r.error_pct,
                "feedback_source": r.feedback_source,
                "date":            r.created_at.date().isoformat() if r.created_at else None,
                "notes":           r.notes,
            }
            for r in records
        ]
    }


@app.get("/api/ml/feedback/summary")
@limiter.limit("20/minute")
def get_feedback_summary(
    request:      Request,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """予測種別ごとの精度統計サマリーを返す。"""
    records = (
        db.query(PredictionFeedback)
        .filter(PredictionFeedback.user_id == current_user.id)
        .filter(PredictionFeedback.actual_value.isnot(None))
        .all()
    )

    by_type: dict = {}
    for r in records:
        k = r.prediction_type
        if k not in by_type:
            by_type[k] = {"errors": [], "pcts": [], "count": 0}
        if r.error_abs is not None:
            by_type[k]["errors"].append(r.error_abs)
        if r.error_pct is not None:
            by_type[k]["pcts"].append(r.error_pct)
        by_type[k]["count"] += 1

    summary = {}
    for k, v in by_type.items():
        errs = v["errors"]
        pcts = v["pcts"]
        summary[k] = {
            "count":   v["count"],
            "mae":     round(sum(errs) / len(errs), 3) if errs else None,
            "mape":    round(sum(pcts) / len(pcts), 2) if pcts else None,
            "min_err": round(min(errs), 3) if errs else None,
            "max_err": round(max(errs), 3) if errs else None,
        }

    return {
        "total_records": len(records),
        "by_type":       summary,
    }


@app.delete("/api/ml/feedback/{feedback_id}")
@limiter.limit("20/minute")
def delete_feedback(
    request:      Request,
    feedback_id:  int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """フィードバックを削除する。"""
    fb = db.query(PredictionFeedback).filter(
        PredictionFeedback.id == feedback_id,
        PredictionFeedback.user_id == current_user.id,
    ).first()
    if not fb:
        raise HTTPException(status_code=404, detail="フィードバックが見つかりません")
    db.delete(fb)
    db.commit()
    return {"message": "削除しました"}


# ══════════════════════════════════════════════════════════════
# Feature 1: AI自然言語クエリ
# ══════════════════════════════════════════════════════════════

@app.post("/api/ai/query")
@limiter.limit("10/minute")
def ai_query(
    request:      Request,
    body:         AIQueryRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    自然言語の質問に対して、ユーザーのトレーニングデータを元に AI が回答する。
    ANTHROPIC_API_KEY が設定されていない場合は統計ベースのフォールバック回答を返す。
    """
    from backend.models_db import Activity, DailyMetrics

    # ── 最近のアクティビティを取得 ──────────────────
    activities = (
        db.query(Activity)
        .filter(Activity.user_id == current_user.id)
        .order_by(Activity.start_time.desc())
        .limit(30)
        .all()
    )
    recent_acts = [
        {
            "date":         a.start_time.strftime("%Y-%m-%d") if a.start_time else "?",
            "type":         a.activity_type,
            "distance_km":  round((a.distance_m or 0) / 1000, 2),
            "duration_min": round((a.duration_sec or 0) / 60, 0),
            "avg_hr":       a.avg_hr,
            "trimp":        a.trimp,
        }
        for a in activities
    ]

    # ── 日次メトリクスを取得（直近 90 日）──────────
    today = _dt.date.today()
    since = today - _dt.timedelta(days=90)
    metrics = (
        db.query(DailyMetrics)
        .filter(
            DailyMetrics.user_id == current_user.id,
            DailyMetrics.date >= since,
        )
        .order_by(DailyMetrics.date)
        .all()
    )
    daily_list = [
        {
            "date":           str(m.date),
            "atl":            m.atl,
            "ctl":            m.ctl,
            "tsb":            m.tsb,
            "recovery_score": m.recovery_score,
            "resting_hr":     m.resting_hr,
            "hrv_rmssd":      m.hrv_rmssd,
            "sleep_hours":    m.sleep_hours,
        }
        for m in metrics
    ]

    # ── 統計サマリー ────────────────────────────────
    total_acts = db.query(Activity).filter(Activity.user_id == current_user.id).count()
    total_dist = db.query(Activity).filter(Activity.user_id == current_user.id).all()
    total_dist_km = sum((a.distance_m or 0) for a in total_dist) / 1000.0

    # 週平均 TRIMP（直近 4 週）
    four_weeks_ago = today - _dt.timedelta(weeks=4)
    recent_acts_4w = [
        a for a in activities
        if a.start_time and a.start_time.date() >= four_weeks_ago and a.trimp
    ]
    weekly_trimps = [a.trimp for a in recent_acts_4w]
    avg_weekly_trimp = sum(weekly_trimps) / 4.0 if weekly_trimps else 0.0

    # 最新 VO2max
    latest_vo2 = None
    for m in reversed(metrics):
        if m.vo2max:
            latest_vo2 = m.vo2max
            break

    stats = {
        "total_activities":  total_acts,
        "total_distance_km": round(total_dist_km, 1),
        "avg_weekly_trimp":  round(avg_weekly_trimp, 1),
        "vo2max_latest":     latest_vo2,
    }

    ctx = QueryContext(
        recent_activities = recent_acts,
        daily_metrics     = daily_list,
        stats             = stats,
    )

    engine = AIQueryEngine()
    result = engine.query(question=body.question, context=ctx)

    has_ai = result.provider != "fallback"
    return {
        "answer":                result.answer,
        "data_used":             result.data_used,
        "confidence":            result.confidence,
        "follow_up_suggestions": result.follow_up_suggestions,
        "has_ai":                has_ai,
        "provider":              result.provider,
    }


# ══════════════════════════════════════════════════════════════
# Feature 2: レースシミュレーター
# ══════════════════════════════════════════════════════════════

_ACTIVITY_TYPE_MAP = {
    "🏔️ 登山・ハイキング":  "hiking",
    "🏃 ランニング":         "running",
    "🌲 トレイルランニング": "trail_running",
    "🚴 サイクリング":       "cycling",
    "🚵 マウンテンバイク":   "cycling",
}

@app.post("/api/race/simulate")
@limiter.limit("10/minute")
def race_simulate(
    request:      Request,
    body:         RaceSimRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    目標タイム・コースプロファイルからレースシミュレーションを実行する。
    個人化 ML モデルが利用可能な場合は HR 予測も行う。
    """
    from ke_estimator import UserProfile, ACTIVITY_HIKING, ACTIVITY_RUNNING, ACTIVITY_CYCLING
    from personal_model_trainer import PersonalModelTrainer, load_points_from_db
    from health_context_features import DayContext

    act_type = _ACTIVITY_TYPE_MAP.get(body.activity_type, body.activity_type)

    ke_act = {
        "hiking":        ACTIVITY_HIKING,
        "trail_running": ACTIVITY_RUNNING,
        "running":       ACTIVITY_RUNNING,
        "cycling":       ACTIVITY_CYCLING,
    }.get(act_type, ACTIVITY_HIKING)

    profile = UserProfile(
        body_weight_kg   = body.body_weight,
        load_weight_kg   = body.load_weight,
        terrain_factor   = body.terrain_factor,
        activity_type    = ke_act,
    )

    # 個人化モデルの取得（オプション）
    personal_trainer = None
    context = None
    if body.use_personal_model:
        try:
            trainer = PersonalModelTrainer(user_id=current_user.id)
            if trainer.is_ready:
                personal_trainer = trainer
                # 目標日のコンテキストを取得
                if body.target_date:
                    from backend.models_db import DailyMetrics
                    from health_context_features import HealthContextBuilder
                    target_d = _dt.date.fromisoformat(body.target_date)
                    daily = (
                        db.query(DailyMetrics)
                        .filter(DailyMetrics.user_id == current_user.id)
                        .order_by(DailyMetrics.date)
                        .all()
                    )
                    if daily:
                        builder = HealthContextBuilder(daily)
                        context = builder.get_context_for_date(target_d)
        except Exception as e:
            logger.warning(f"個人化モデル読込エラー（スキップ）: {e}")

    result = simulate_race(
        route_points     = body.route_points,
        target_time_min  = body.target_time_min,
        activity_type    = act_type,
        profile          = profile,
        segment_km       = body.segment_km,
        personal_trainer = personal_trainer,
        context          = context,
    )

    return result.to_dict()


# ══════════════════════════════════════════════════════════════
# Feature 3: 自動トレーニングプラン生成
# ══════════════════════════════════════════════════════════════

@app.post("/api/training/plan")
@limiter.limit("5/minute")
def create_training_plan(
    request:      Request,
    body:         TrainingPlanRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    目標レース情報と現在のフィットネス状態からトレーニングプランを生成して保存する。
    """
    from backend.models_db import DailyMetrics, TrainingPlan as TrainingPlanDB
    import json as _json

    # 現在の CTL / TSB / VO2max を取得
    today = _dt.date.today()
    latest_metric = (
        db.query(DailyMetrics)
        .filter(DailyMetrics.user_id == current_user.id)
        .order_by(DailyMetrics.date.desc())
        .first()
    )
    current_ctl    = (latest_metric.ctl if latest_metric and latest_metric.ctl else 30.0)
    current_tsb    = (latest_metric.tsb if latest_metric and latest_metric.tsb else 0.0)
    current_vo2max = (latest_metric.vo2max if latest_metric else None)

    race_date = _dt.date.fromisoformat(body.race_date)
    act_type  = _ACTIVITY_TYPE_MAP.get(body.activity_type, body.activity_type)

    plan = generate_training_plan(
        user_id           = current_user.id,
        race_date         = race_date,
        race_name         = body.race_name,
        race_distance_km  = body.race_distance_km,
        activity_type     = act_type,
        current_ctl       = current_ctl,
        current_tsb       = current_tsb,
        current_vo2max    = current_vo2max,
        today             = today,
    )

    plan_dict = plan.to_dict()

    # DB に保存
    db_plan = TrainingPlanDB(
        user_id          = current_user.id,
        race_name        = body.race_name,
        race_date        = race_date,
        race_distance_km = body.race_distance_km,
        activity_type    = act_type,
        current_ctl      = current_ctl,
        target_ctl       = plan.target_ctl,
        plan_json        = _json.dumps(plan_dict, ensure_ascii=False),
    )
    db.add(db_plan)
    db.commit()
    db.refresh(db_plan)

    plan_dict["plan_id"] = db_plan.id
    return plan_dict


@app.get("/api/training/plan")
@limiter.limit("20/minute")
def list_training_plans(
    request:      Request,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """ユーザーの全トレーニングプランを一覧表示する。"""
    from backend.models_db import TrainingPlan as TrainingPlanDB
    plans = (
        db.query(TrainingPlanDB)
        .filter(TrainingPlanDB.user_id == current_user.id)
        .order_by(TrainingPlanDB.created_at.desc())
        .all()
    )
    return [
        {
            "id":               p.id,
            "race_name":        p.race_name,
            "race_date":        str(p.race_date),
            "race_distance_km": p.race_distance_km,
            "activity_type":    p.activity_type,
            "current_ctl":      p.current_ctl,
            "target_ctl":       p.target_ctl,
            "created_at":       str(p.created_at),
        }
        for p in plans
    ]


@app.get("/api/training/plan/{plan_id}")
@limiter.limit("20/minute")
def get_training_plan(
    request:      Request,
    plan_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """特定のトレーニングプランを取得する。"""
    from backend.models_db import TrainingPlan as TrainingPlanDB
    import json as _json

    plan = db.query(TrainingPlanDB).filter(
        TrainingPlanDB.id == plan_id,
        TrainingPlanDB.user_id == current_user.id,
    ).first()
    if not plan:
        raise HTTPException(status_code=404, detail="プランが見つかりません")
    data = _json.loads(plan.plan_json)
    data["plan_id"] = plan.id
    return data


@app.delete("/api/training/plan/{plan_id}")
@limiter.limit("10/minute")
def delete_training_plan(
    request:      Request,
    plan_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """トレーニングプランを削除する。"""
    from backend.models_db import TrainingPlan as TrainingPlanDB

    plan = db.query(TrainingPlanDB).filter(
        TrainingPlanDB.id == plan_id,
        TrainingPlanDB.user_id == current_user.id,
    ).first()
    if not plan:
        raise HTTPException(status_code=404, detail="プランが見つかりません")
    db.delete(plan)
    db.commit()
    return {"message": "削除しました"}


# ══════════════════════════════════════════════════════════════
# Feature A: ルート候補比較
# ══════════════════════════════════════════════════════════════

@app.post("/api/routes/compare-candidates")
@limiter.limit("5/minute")
def compare_route_candidates(
    request: Request,
    body:    CompareCandidatesRequest,
):
    """
    2〜3本のルート候補を並べてエネルギーコスト・所要時間・難易度を比較する。
    各候補に対してエネルギーシミュレーションを実行し、サマリーを返す。
    """
    results = []
    df = _load_df()

    for cand in body.candidates:
        route    = cand.route
        p        = cand.profile
        act_key  = cand.activity
        act_sim  = ACTIVITY_TO_SIM.get(act_key, "hiking")

        coords = route.get("coords", [])
        elevs  = route.get("elevations", [])
        if not coords or len(coords) < 2:
            results.append({
                "label": cand.label,
                "error": "ルートデータが不正です",
            })
            continue

        try:
            spd_h = 1.0 if act_sim == "hiking" else (3.0 if act_sim == "running" else 6.94)
            slope_arr, elev_arr, dist_arr = coords_to_slope_array(coords, elevs, speed_ms=spd_h)

            prof = UserProfile(
                body_weight_kg        = p.body_weight,
                load_weight_kg        = p.load_weight,
                hr_max                = p.hr_max,
                terrain_factor        = p.terrain_factor,
                e_initial_kcal        = p.e_initial,
                activity_type         = act_sim,
                base_run_speed_ms     = 3.0,
                base_cycling_speed_ms = 6.94,
            )

            engine_a = _build_engine_a(df, slope_arr, prof)

            supply_events = []
            for s in body.supplies:
                food_info = FOOD_MASTER.get(s.food, {"kcal_per": 100, "ka": 1.0})
                supply_events.append(SupplyEvent(
                    time_min  = s.time_min,
                    kcal      = food_info["kcal_per"] * s.qty,
                    ka        = food_info["ka"],
                ))

            alert_cfg = AlertConfig(
                e_crit_kcal = p.e_crit,
                t_warn_min  = p.t_warn,
            )
            sim_result = run_pk_model(
                engine_a_output = engine_a,
                profile         = prof,
                supply_events   = supply_events,
                alert_config    = alert_cfg,
            )

            e_kcal = sim_result["E_kcal"].tolist()
            difficulty = _calc_difficulty_score(coords, elevs)

            results.append({
                "label":         cand.label,
                "eta_min":       round(float(engine_a["eta_minutes"]), 1),
                "dist_km":       round(route.get("distance_m", 0) / 1000, 2),
                "ascent_m":      round(route.get("ascent_m", 0)),
                "total_kcal":    round(float(prof.e_initial_kcal - min(e_kcal))),
                "final_kcal":    round(float(e_kcal[-1])),
                "alert_count":   len(sim_result["alert_times"]),
                "difficulty":    difficulty,
            })
        except Exception as e:
            logger.warning("Route candidate simulation error: %s", e)
            results.append({
                "label": cand.label,
                "error": "シミュレーションに失敗しました",
            })

    return {"results": results}


# ══════════════════════════════════════════════════════════════
# Feature B: トレーニングプランAI調整
# ══════════════════════════════════════════════════════════════

@app.post("/api/training/plan/{plan_id}/adjust")
@limiter.limit("10/minute")
def adjust_training_plan(
    request:      Request,
    plan_id:      int,
    body:         TrainingPlanAdjustRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    自然言語メッセージを元に、指定週のトレーニングプランをAIが調整する。
    変更はDBには保存されず、フロントエンドで一時的に表示する。
    """
    import json as _json
    from backend.models_db import TrainingPlan as TrainingPlanDB

    plan_db = db.query(TrainingPlanDB).filter(
        TrainingPlanDB.id == plan_id,
        TrainingPlanDB.user_id == current_user.id,
    ).first()
    if not plan_db:
        raise HTTPException(status_code=404, detail="プランが見つかりません")

    plan_data  = _json.loads(plan_db.plan_json)
    weeks      = plan_data.get("weeks", [])
    if body.week_index >= len(weeks):
        raise HTTPException(status_code=400, detail="週インデックスが範囲外です")

    target_week = weeks[body.week_index]
    workouts    = target_week.get("workouts", [])

    # AIエンジンに調整を依頼
    week_summary = (
        f"フェーズ: {target_week.get('phase_label', '')}, "
        f"週TSS: {target_week.get('weekly_tss', '')}, "
        f"今週の目標: {target_week.get('focus', '')}\n"
        f"ワークアウト一覧:\n"
        + "\n".join(
            f"  - {w.get('workout_type', '?')}: {w.get('description', '')} ({w.get('duration_min', 0)}分)"
            for w in workouts
        )
    )

    prompt = (
        f"以下はトレーニングプランの1週間分です。\n"
        f"ユーザーのリクエスト: 「{body.message}」\n\n"
        f"現在の週のプラン:\n{week_summary}\n\n"
        f"上記のリクエストに合わせて、この週のトレーニングを調整してください。\n"
        f"調整後のワークアウト一覧をJSONで返してください。フォーマットは元のワークアウトリストと同じ構造を維持してください。\n"
        f"必ずJSON配列のみを返し、前後に余計なテキストを入れないでください。\n"
        f"日本語で記述してください。"
    )

    engine = AIQueryEngine()
    from ai_query import QueryContext
    ctx = QueryContext(recent_activities=[], daily_metrics=[], stats={})

    try:
        ai_result = engine.query(question=prompt, context=ctx)
        answer    = ai_result.answer

        # JSON部分を抽出
        import re
        json_match = re.search(r"\[[\s\S]*\]", answer)
        if json_match:
            adjusted_workouts = _json.loads(json_match.group())
            return {
                "adjusted_week": {**target_week, "workouts": adjusted_workouts},
                "explanation":   f"AIがリクエスト「{body.message}」に基づいて調整しました。",
                "has_ai":        ai_result.provider != "fallback",
            }
    except Exception as e:
        logger.warning("Training plan AI adjust error: %s", e)

    # フォールバック: メッセージに基づいて負荷を軽減
    adjusted = []
    for w in workouts:
        w2 = dict(w)
        msg_lower = body.message.lower()
        if any(kw in msg_lower for kw in ["疲れ", "疲労", "きつい", "しんどい", "休み", "tired", "rest", "fatigue"]):
            if w2.get("workout_type") in ("TEMPO", "INTERVAL"):
                w2["workout_type"] = "EASY"
                w2["description"]  = "疲労を考慮してイージーペースに変更しました。" + w2.get("description", "")
            w2["duration_min"] = max(20, int(w2.get("duration_min", 40) * 0.7))
        adjusted.append(w2)

    return {
        "adjusted_week": {**target_week, "workouts": adjusted},
        "explanation":   f"疲労を考慮してトレーニング強度を調整しました（AIなしモード）。",
        "has_ai":        False,
    }


# ══════════════════════════════════════════════════════════════
# Feature C: レース実績入力 → モデル再キャリブレーション
# ══════════════════════════════════════════════════════════════

@app.post("/api/race/result")
@limiter.limit("10/minute")
def submit_race_result(
    request:      Request,
    body:         RaceResultInput,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    レース実績（距離・タイム・HR）を記録し、個人化モデルを再キャリブレーションする。
    """
    from backend.models_db import Activity

    race_dt = _dt.datetime.combine(
        _dt.date.fromisoformat(body.race_date),
        _dt.time(9, 0),
    )
    duration_sec  = body.actual_time_min * 60.0
    distance_m    = body.race_distance_km * 1000.0
    act_type      = _ACTIVITY_TYPE_MAP.get(body.activity_type, body.activity_type)

    # TRIMP 推定
    trimp = None
    if body.avg_hr:
        hr_rest  = 60.0
        hr_max   = body.max_hr or 185.0
        trimp    = body.actual_time_min * (body.avg_hr - hr_rest) / (hr_max - hr_rest) * \
                   np.exp(1.92 * (body.avg_hr - hr_rest) / (hr_max - hr_rest))

    activity = Activity(
        user_id          = current_user.id,
        source           = "manual",
        activity_type    = act_type,
        start_time       = race_dt,
        duration_sec     = duration_sec,
        distance_m       = distance_m,
        elevation_gain_m = body.elevation_gain_m,
        avg_hr           = body.avg_hr,
        max_hr           = body.max_hr,
        avg_speed_mps    = distance_m / duration_sec if duration_sec > 0 else None,
        trimp            = trimp,
        device_name      = "manual_race_result",
    )
    db.add(activity)
    db.commit()
    db.refresh(activity)

    # DailyMetrics に TRIMP を反映
    if trimp:
        _update_daily_metrics_from_activity(activity, 60.0, body.max_hr or 185.0, db)

    # 個人化モデルを再学習
    retrain_result = _retrain_personal_model(current_user.id, db)

    avg_speed_mps = distance_m / duration_sec if duration_sec > 0 else 0
    pace_min_per_km = (1000 / avg_speed_mps / 60) if avg_speed_mps > 0 else None

    return {
        "saved":          True,
        "activity_id":    activity.id,
        "pace_min_per_km": round(pace_min_per_km, 2) if pace_min_per_km else None,
        "trimp":          round(trimp, 1) if trimp else None,
        "retrain_result": retrain_result,
        "message":        "レース結果を保存し、個人化モデルを更新しました。",
    }


# ══════════════════════════════════════════════════════════════════════
# ★ 新機能群
# ══════════════════════════════════════════════════════════════════════

import secrets as _secrets
import httpx as _httpx
import re as _re

# ── インメモリキャッシュ（週次コーチング / POI）──────────
_weekly_coaching_cache: dict = {}   # {f"{uid}_{year_w}": result}
_poi_cache: dict             = {}   # {cache_key: (timestamp, list)}
_POI_CACHE_TTL    = 3600            # 1時間
_CACHE_MAX_WEEKLY = 500             # ユーザー×週のエントリ上限
_CACHE_MAX_POI    = 200             # POIエントリ上限

def _trim_cache(cache: dict, max_size: int) -> None:
    """キャッシュが上限を超えたら古いエントリを削除する（O(n) だが稀にしか発生しない）。"""
    if len(cache) > max_size:
        excess = len(cache) - max_size
        for key in list(cache.keys())[:excess]:
            cache.pop(key, None)

# ══════════════════════════════════════════════════════════════════════
# 機能1: 怪我リスク予測
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/ml/injury-risk")
@limiter.limit("20/minute")
def get_injury_risk(
    request:      Request,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """ATL/CTL/TSB と HRV 傾向からオーバートレーニング確率を算出する。"""
    from backend.models_db import DailyMetrics as DM
    rows = (
        db.query(DM)
        .filter(DM.user_id == current_user.id)
        .order_by(DM.date.desc())
        .limit(28)
        .all()
    )
    if not rows:
        return {"risk_pct": 0, "risk_level": "不明", "advice": "データが不足しています。アクティビティをアップロードしてください。", "factors": {}}

    tsb_vals  = [r.tsb  for r in rows if r.tsb  is not None]
    atl_vals  = [r.atl  for r in rows if r.atl  is not None]
    hrv_vals  = [r.hrv_rmssd for r in rows if r.hrv_rmssd is not None]

    tsb_now   = tsb_vals[0]  if tsb_vals  else 0.0
    atl_now   = atl_vals[0]  if atl_vals  else 0.0
    atl_7ago  = atl_vals[6]  if len(atl_vals) > 6 else atl_vals[-1] if atl_vals else 0.0
    hrv_now   = float(np.mean(hrv_vals[:3]))  if len(hrv_vals) >= 3 else (hrv_vals[0] if hrv_vals else None)
    hrv_base  = float(np.mean(hrv_vals[-7:])) if len(hrv_vals) >= 7 else hrv_now

    # リスク要因スコア（各0〜1）
    tsb_risk  = min(max(-tsb_now, 0) / 40.0, 1.0)           # TSB が -40 以下で最大
    atl_change = (atl_now - atl_7ago) / max(atl_7ago, 1)
    atl_risk   = min(max(atl_change, 0) / 0.3, 1.0)          # 7日で30%超増で最大
    hrv_risk   = 0.0
    if hrv_now and hrv_base:
        drop = (hrv_base - hrv_now) / max(hrv_base, 1)
        hrv_risk = min(max(drop, 0) / 0.2, 1.0)              # 20%以上低下で最大

    risk_raw = tsb_risk * 0.40 + atl_risk * 0.35 + hrv_risk * 0.25
    risk_pct = round(risk_raw * 100)

    if risk_pct < 25:
        level  = "低"
        advice = "コンディションは良好です。計画通りトレーニングを継続してください。"
    elif risk_pct < 55:
        level  = "中"
        advice = "やや疲労が蓄積しています。今週は強度を10〜15%落とし、睡眠を優先してください。"
    else:
        level  = "高"
        advice = "オーバートレーニングの兆候があります。2〜3日の完全休養を推奨します。"

    return {
        "risk_pct":   risk_pct,
        "risk_level": level,
        "advice":     advice,
        "factors": {
            "tsb_now":        round(tsb_now, 1),
            "atl_7d_change_pct": round(atl_change * 100, 1),
            "hrv_drop_pct":   round((hrv_base - hrv_now) / max(hrv_base, 1) * 100, 1) if hrv_now and hrv_base else None,
        },
    }


# ══════════════════════════════════════════════════════════════════════
# 機能2: 気象×パフォーマンス相関分析
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/analytics/weather-performance")
@limiter.limit("20/minute")
def get_weather_performance(
    request:      Request,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """過去アクティビティの気温・湿度とペースの相関を返す。"""
    from backend.models_db import Activity as Act
    rows = (
        db.query(Act)
        .filter(Act.user_id == current_user.id, Act.avg_speed_mps != None)
        .order_by(Act.start_time.desc())
        .limit(200)
        .all()
    )

    temp_bins  = [("<10", -99, 10), ("10-15", 10, 15), ("15-20", 15, 20), ("20-25", 20, 25), ("25+", 25, 99)]
    hum_bins   = [("<40%", 0, 40), ("40-60%", 40, 60), ("60-80%", 60, 80), ("80%+", 80, 101)]

    def _bin_stats(rows, val_fn, bins):
        result = []
        for label, lo, hi in bins:
            subset = [r for r in rows if val_fn(r) is not None and lo <= val_fn(r) < hi]
            if subset:
                spds = [r.avg_speed_mps for r in subset if r.avg_speed_mps]
                hrs  = [r.avg_hr        for r in subset if r.avg_hr]
                result.append({
                    "range":        label,
                    "avg_speed_mps": round(float(np.mean(spds)), 3) if spds else None,
                    "avg_hr":       round(float(np.mean(hrs)),  1)  if hrs  else None,
                    "n":            len(subset),
                })
            else:
                result.append({"range": label, "avg_speed_mps": None, "avg_hr": None, "n": 0})
        return result

    temp_data = _bin_stats(rows, lambda r: r.avg_temp_c,   temp_bins)
    hum_data  = _bin_stats(rows, lambda r: r.humidity_pct, hum_bins)

    # 最高パフォーマンス帯を特定
    best_temp = max((b for b in temp_data if b["avg_speed_mps"] and b["n"] >= 2),
                    key=lambda b: b["avg_speed_mps"], default=None)
    best_hum  = max((b for b in hum_data  if b["avg_speed_mps"] and b["n"] >= 2),
                    key=lambda b: b["avg_speed_mps"], default=None)

    return {
        "temp_bins":         temp_data,
        "humidity_bins":     hum_data,
        "best_temp_range":   best_temp["range"] if best_temp else None,
        "best_hum_range":    best_hum["range"]  if best_hum  else None,
        "data_points_total": len(rows),
    }


# ══════════════════════════════════════════════════════════════════════
# 機能3: 目標タイム達成確率
# ══════════════════════════════════════════════════════════════════════

class GoalTimeProbRequest(BaseModel):
    target_time_min:  float = Field(..., ge=5, le=10080)
    race_distance_km: float = Field(..., ge=0.1, le=500)
    elevation_gain_m: float = Field(0.0, ge=0)
    activity_type:    str   = Field("running", max_length=30)

@app.post("/api/ml/goal-time-probability")
@limiter.limit("20/minute")
def get_goal_time_probability(
    request:      Request,
    body:         GoalTimeProbRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """VO2max・CTL・コース難易度から目標タイム達成確率を算出する。"""
    from backend.models_db import DailyMetrics as DM
    from scipy.stats import norm

    latest = (
        db.query(DM)
        .filter(DM.user_id == current_user.id)
        .order_by(DM.date.desc())
        .first()
    )

    vo2max = (latest.vo2max if latest and latest.vo2max else 40.0)
    ctl    = (latest.ctl    if latest and latest.ctl    else 30.0)

    # 簡易ペース推定: vo2max と CTL から基準ペースを計算
    # VO2max 50 = 4:00/km 相当 の線形近似
    base_pace_mps = vo2max / 50.0 * (1000 / 240)   # m/s
    # 獲得標高によるペース補正（100m/km あたり 8% 低下）
    ascent_per_km = body.elevation_gain_m / max(body.race_distance_km, 0.1)
    grade_factor  = 1 / (1 + ascent_per_km / 100 * 0.08)
    adj_pace_mps  = base_pace_mps * grade_factor
    # CTL による準備度補正（CTL 80 で 100%、20 で 70%）
    readiness     = 0.70 + 0.30 * min(ctl / 80.0, 1.0)
    est_time_min  = (body.race_distance_km * 1000 / adj_pace_mps / 60) / readiness

    # 目標タイムとの差を σ 換算（変動係数 8% 想定）
    sigma         = est_time_min * 0.08
    z_score       = (est_time_min - body.target_time_min) / max(sigma, 1)
    probability   = round(float(norm.cdf(z_score)) * 100, 1)

    needed_ctl = None
    if probability < 50:
        # 50% 達成に必要な CTL を逆算（簡易）
        needed_ctl = round(ctl * (body.target_time_min / max(est_time_min, 1)) ** 2 * 80)

    return {
        "probability_pct":   probability,
        "estimated_time_min": round(est_time_min, 1),
        "target_time_min":   body.target_time_min,
        "vo2max_used":        round(vo2max, 1),
        "ctl_used":           round(ctl, 1),
        "needed_ctl_for_50pct": needed_ctl,
        "advice": (
            "現在のフィットネスで十分達成可能です。" if probability >= 70 else
            "達成可能ですが、もう少しトレーニングを積むと安心です。" if probability >= 50 else
            f"現状では厳しい目標です。CTLを{needed_ctl}程度まで上げることを目指しましょう。"
        ),
    }


# ══════════════════════════════════════════════════════════════════════
# 機能4: 時間帯別パフォーマンス分析
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/analytics/time-of-day")
@limiter.limit("20/minute")
def get_time_of_day_performance(
    request:      Request,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """アクティビティの開始時刻帯ごとにパフォーマンスを集計する。"""
    from backend.models_db import Activity as Act
    rows = (
        db.query(Act)
        .filter(Act.user_id == current_user.id, Act.start_time != None, Act.avg_speed_mps != None)
        .order_by(Act.start_time.desc())
        .limit(300)
        .all()
    )

    SLOTS = [
        ("深夜",  0,  5, "🌙"),
        ("早朝",  5,  9, "🌅"),
        ("午前",  9, 12, "☀️"),
        ("昼",   12, 15, "🌤️"),
        ("午後", 15, 18, "🌇"),
        ("夕方", 18, 21, "🌆"),
        ("夜",   21, 24, "🌃"),
    ]

    result = []
    all_speeds = [r.avg_speed_mps for r in rows if r.avg_speed_mps]
    global_mean = float(np.mean(all_speeds)) if all_speeds else 1.0

    for label, h_lo, h_hi, icon in SLOTS:
        subset = [r for r in rows if h_lo <= r.start_time.hour < h_hi]
        if subset:
            spds = [r.avg_speed_mps for r in subset if r.avg_speed_mps]
            hrs  = [r.avg_hr        for r in subset if r.avg_hr]
            avg_spd = float(np.mean(spds)) if spds else None
            result.append({
                "label":          label,
                "icon":           icon,
                "avg_speed_mps":  round(avg_spd, 3) if avg_spd else None,
                "avg_hr":         round(float(np.mean(hrs)), 1) if hrs else None,
                "n":              len(subset),
                "relative_pct":   round((avg_spd / global_mean - 1) * 100, 1) if avg_spd else 0,
            })
        else:
            result.append({"label": label, "icon": icon, "avg_speed_mps": None, "avg_hr": None, "n": 0, "relative_pct": 0})

    best = max((s for s in result if s["n"] >= 2), key=lambda s: s["avg_speed_mps"] or 0, default=None)
    return {"slots": result, "best_slot": best["label"] if best else None, "data_points_total": len(rows)}


# ══════════════════════════════════════════════════════════════════════
# 機能5: ルートライブラリ（お気に入り保存・タグ管理）
# ══════════════════════════════════════════════════════════════════════

class SaveRouteRequest(BaseModel):
    name:             str   = Field(..., min_length=1, max_length=100)
    description:      str   = Field("", max_length=500)
    tags:             str   = Field("", max_length=500)   # カンマ区切り
    route:            dict                                 # GPX 解析結果
    activity_type:    str   = Field("🏔️ 登山・ハイキング", max_length=30)

@app.post("/api/routes/library")
@limiter.limit("30/minute")
def save_route_to_library(
    request:      Request,
    body:         SaveRouteRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """ルートをライブラリに保存する。"""
    from backend.models_db import SavedRoute
    sr = SavedRoute(
        user_id          = current_user.id,
        name             = body.name,
        description      = body.description,
        tags             = body.tags,
        route_json       = json.dumps(body.route, ensure_ascii=False),
        distance_m       = body.route.get("distance_m"),
        elevation_gain_m = body.route.get("ascent_m"),
        activity_type    = body.activity_type,
    )
    db.add(sr); db.commit(); db.refresh(sr)
    return {"id": sr.id, "name": sr.name, "message": "ライブラリに保存しました"}

@app.get("/api/routes/library")
@limiter.limit("30/minute")
def list_route_library(
    request:      Request,
    tag:          str     = Query(None, max_length=100),
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """ライブラリ一覧を返す（タグフィルタ対応）。"""
    from backend.models_db import SavedRoute
    q = db.query(SavedRoute).filter(SavedRoute.user_id == current_user.id)
    if tag:
        q = q.filter(SavedRoute.tags.contains(tag))
    rows = q.order_by(SavedRoute.created_at.desc()).all()
    return {"routes": [
        {
            "id":             r.id,
            "name":           r.name,
            "description":    r.description,
            "tags":           [t.strip() for t in (r.tags or "").split(",") if t.strip()],
            "distance_km":    round(r.distance_m / 1000, 2) if r.distance_m else None,
            "elevation_gain_m": r.elevation_gain_m,
            "activity_type":  r.activity_type,
            "created_at":     r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]}

@app.get("/api/routes/library/{route_id}")
@limiter.limit("30/minute")
def get_route_from_library(
    request:      Request,
    route_id:     int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """ライブラリのルートデータ（座標含む）を返す。"""
    from backend.models_db import SavedRoute
    r = db.query(SavedRoute).filter(SavedRoute.id == route_id, SavedRoute.user_id == current_user.id).first()
    if not r:
        raise HTTPException(status_code=404, detail="ルートが見つかりません")
    return {"id": r.id, "name": r.name, "tags": [t.strip() for t in (r.tags or "").split(",") if t.strip()],
            "route": json.loads(r.route_json), "activity_type": r.activity_type}

@app.delete("/api/routes/library/{route_id}")
@limiter.limit("30/minute")
def delete_route_from_library(
    request:      Request,
    route_id:     int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    from backend.models_db import SavedRoute
    r = db.query(SavedRoute).filter(SavedRoute.id == route_id, SavedRoute.user_id == current_user.id).first()
    if not r:
        raise HTTPException(status_code=404, detail="ルートが見つかりません")
    db.delete(r); db.commit()
    return {"message": "削除しました"}


# ══════════════════════════════════════════════════════════════════════
# 機能6: ルート上のPOI表示（Overpass API）
# ══════════════════════════════════════════════════════════════════════

class POIRequest(BaseModel):
    north: float = Field(..., ge=-90,  le=90)
    south: float = Field(..., ge=-90,  le=90)
    east:  float = Field(..., ge=-180, le=180)
    west:  float = Field(..., ge=-180, le=180)
    categories: List[str] = Field(default=["mountain_hut", "water_source", "toilet"])

@app.post("/api/route/poi")
@limiter.limit("10/minute")
async def get_route_poi(request: Request, body: POIRequest):
    """Overpass API から山小屋・水場・トイレなどのPOIを取得する。"""
    # カテゴリ → Overpass クエリ条件
    _CAT_MAP = {
        "mountain_hut": '(node["tourism"="alpine_hut"]; node["tourism"="wilderness_hut"]; way["tourism"="alpine_hut"];)',
        "water_source": '(node["amenity"="drinking_water"]; node["natural"="spring"]; node["amenity"="water_point"];)',
        "toilet":       '(node["amenity"="toilets"];)',
        "shelter":      '(node["amenity"="shelter"]; node["tourism"="camp_site"];)',
    }
    valid_cats = [c for c in body.categories if c in _CAT_MAP]
    if not valid_cats:
        return {"pois": []}

    cache_key = f"{round(body.north,2)}_{round(body.south,2)}_{round(body.east,2)}_{round(body.west,2)}_{'_'.join(sorted(valid_cats))}"
    now = time.monotonic()
    if cache_key in _poi_cache:
        ts, cached = _poi_cache[cache_key]
        if now - ts < _POI_CACHE_TTL:
            return {"pois": cached}

    bbox = f"{body.south},{body.west},{body.north},{body.east}"
    union = "".join(_CAT_MAP[c] for c in valid_cats)
    query = f"[out:json][timeout:15][bbox:{bbox}];({union});out center 100;"

    pois = []
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            resp = await client.post("https://overpass-api.de/api/interpreter", data={"data": query})
            resp.raise_for_status()
            elements = resp.json().get("elements", [])
        for el in elements:
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            if not lat or not lon:
                continue
            tags = el.get("tags", {})
            pois.append({
                "lat":  lat, "lon": lon,
                "name": tags.get("name") or tags.get("name:ja") or "無名",
                "type": (
                    "mountain_hut" if tags.get("tourism") in ("alpine_hut", "wilderness_hut") else
                    "water_source" if tags.get("amenity") in ("drinking_water", "water_point") or tags.get("natural") == "spring" else
                    "toilet"       if tags.get("amenity") == "toilets" else "shelter"
                ),
            })
        _poi_cache[cache_key] = (now, pois)
        _trim_cache(_poi_cache, _CACHE_MAX_POI)
    except Exception as e:
        logger.warning("Overpass API error: %s", e)

    return {"pois": pois}


# ══════════════════════════════════════════════════════════════════════
# 機能7: ルート共有URL
# ══════════════════════════════════════════════════════════════════════

class ShareRouteRequest(BaseModel):
    title:        str  = Field("共有ルート", max_length=200)
    route:        dict
    supply_plan:  Optional[dict] = None

@app.post("/api/share/route")
@limiter.limit("5/minute")
def create_shared_route(
    request:      Request,
    body:         ShareRouteRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """共有トークンを生成してDBに保存する（有効期限30日）。"""
    from backend.models_db import SharedRoute
    import datetime as dt2
    token     = _secrets.token_urlsafe(32)
    expires   = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=30)
    sr = SharedRoute(
        user_id          = current_user.id,
        token            = token,
        title            = body.title,
        route_json       = json.dumps(body.route, ensure_ascii=False),
        supply_plan_json = json.dumps(body.supply_plan, ensure_ascii=False) if body.supply_plan else None,
        expires_at       = expires,
    )
    db.add(sr); db.commit()
    return {"token": token, "expires_at": expires.isoformat()}

@app.get("/api/share/{token}")
@limiter.limit("30/minute")
def get_shared_route(request: Request, token: str, db: Session = Depends(get_db)):
    """認証不要でトークンからルートを取得する。"""
    from backend.models_db import SharedRoute
    # トークン長チェック（64文字以内のURLセーフ文字列）
    if not token or len(token) > 64 or not _re.match(r'^[A-Za-z0-9_\-]+$', token):
        raise HTTPException(status_code=400, detail="無効なトークンです")
    sr = db.query(SharedRoute).filter(SharedRoute.token == token).first()
    if not sr:
        raise HTTPException(status_code=404, detail="共有ルートが見つかりません")
    if sr.expires_at and _dt.datetime.now(_dt.timezone.utc) > sr.expires_at:
        raise HTTPException(status_code=410, detail="このリンクは有効期限切れです")
    return {
        "title":       sr.title,
        "route":       json.loads(sr.route_json),
        "supply_plan": json.loads(sr.supply_plan_json) if sr.supply_plan_json else None,
        "created_at":  sr.created_at.isoformat() if sr.created_at else None,
    }


# ══════════════════════════════════════════════════════════════════════
# 機能8: レース戦略レポート自動生成
# ══════════════════════════════════════════════════════════════════════

class RaceStrategyRequest(BaseModel):
    race_distance_km: float = Field(..., ge=0.1, le=500)
    elevation_gain_m: float = Field(0.0, ge=0)
    target_time_min:  float = Field(..., ge=5, le=10080)
    race_date:        str   = Field("", max_length=10)
    course_notes:     str   = Field("", max_length=300)
    race_name:        str   = Field("", max_length=100)
    activity_type:    str   = Field("running", max_length=30)

@app.post("/api/ai/race-strategy")
@limiter.limit("5/minute")
def generate_race_strategy(
    request:      Request,
    body:         RaceStrategyRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """コース・体調・天気からAIがレース戦略レポートを生成する。"""
    from backend.models_db import DailyMetrics as DM
    latest = db.query(DM).filter(DM.user_id == current_user.id).order_by(DM.date.desc()).first()
    tsb = round(latest.tsb, 1)  if latest and latest.tsb  else 0
    ctl = round(latest.ctl, 1)  if latest and latest.ctl  else 0
    hrv = round(latest.hrv_rmssd, 1) if latest and latest.hrv_rmssd else None

    diff = _calc_difficulty_score([], [])  # 座標なし
    km_h = (body.race_distance_km / body.target_time_min * 60)
    target_pace = f"{int(body.target_time_min // body.race_distance_km)}:{int((body.target_time_min / body.race_distance_km % 1)*60):02d}/km"

    prompt = (
        f"あなたはプロのランニングコーチです。以下の情報を元に、日本語でレース戦略レポートを作成してください。\n\n"
        f"【レース情報】距離:{body.race_distance_km}km / 獲得標高:{body.elevation_gain_m}m / 目標タイム:{body.target_time_min}分 ({target_pace}) / レース日:{body.race_date}\n"
        f"【コースメモ】{body.course_notes or 'なし'}\n"
        f"【現在の体調】CTL:{ctl} / TSB:{tsb} / HRV:{hrv}ms\n\n"
        f"以下の形式で回答してください:\n"
        f"1. ペース戦略（前半・中盤・後半の配分）\n"
        f"2. 補給計画（タイミングと内容）\n"
        f"3. 精神的な注意点\n"
        f"4. 当日の準備チェックリスト（3〜5項目）\n"
        f"各セクションを簡潔に200字以内でまとめてください。"
    )

    engine = AIQueryEngine()
    ctx    = QueryContext(recent_activities=[], daily_metrics=[], stats={"ctl": ctl, "tsb": tsb})
    try:
        result  = engine.query(question=prompt, context=ctx)
        text    = result.answer
        has_ai  = result.provider != "fallback"
    except Exception:
        has_ai  = False
        text    = None

    if not has_ai or not text:
        cond = "良好" if tsb > -10 else "要注意"
        text = (
            f"【ペース戦略】最初の{round(body.race_distance_km * 0.3, 1)}kmは目標ペースより5%遅く入り、"
            f"中盤でペースアップ、残り{round(body.race_distance_km * 0.2, 1)}kmでラストスパート。\n"
            f"【補給計画】30〜40分ごとに100〜150kcal補給。エイドを逃さないこと。\n"
            f"【体調コメント】現在のコンディション:{cond}（TSB={tsb}）。"
            + ("積極的なレースを狙えます。" if tsb > -10 else "疲労が残っています。入りを慎重に。")
        )

    return {"strategy_text": text, "has_ai": has_ai, "tsb_used": tsb, "ctl_used": ctl}


# ══════════════════════════════════════════════════════════════════════
# 機能9: 音声入力テキストのアクティビティ解析
# ══════════════════════════════════════════════════════════════════════

class ParseActivityTextRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)

@app.post("/api/ai/parse-activity-text")
@limiter.limit("20/minute")
def parse_activity_text(request: Request, body: ParseActivityTextRequest):
    """自然言語テキストからアクティビティデータを抽出する（AIまたは正規表現）。"""
    prompt = (
        f"以下の日本語テキストからアクティビティ情報を抽出し、JSONのみを返してください。\n"
        f"テキスト: \"{body.text}\"\n"
        f"出力形式: {{\"distance_km\": number|null, \"duration_min\": number|null, "
        f"\"avg_hr\": number|null, \"activity_type\": \"running\"|\"hiking\"|\"cycling\"|null, "
        f"\"notes\": string}}\n"
        f"不明な項目は null にしてください。JSONのみ返してください。"
    )
    parsed = None
    has_ai = False
    try:
        engine = AIQueryEngine()
        ctx    = QueryContext(recent_activities=[], daily_metrics=[], stats={})
        res    = engine.query(question=prompt, context=ctx)
        if res.provider != "fallback":
            raw = res.answer.strip()
            m   = _re.search(r'\{.*\}', raw, _re.DOTALL)
            if m:
                parsed = json.loads(m.group())
                has_ai = True
    except Exception:
        pass

    if not parsed:
        # 正規表現フォールバック
        txt = body.text
        def _num(pattern):
            m = _re.search(pattern, txt)
            return float(m.group(1)) if m else None
        dist  = _num(r'(\d+(?:\.\d+)?)\s*(?:km|キロ)')
        dur   = _num(r'(\d+(?:\.\d+)?)\s*(?:分|min)')
        hr    = _num(r'(?:心拍|HR)\s*[:：]?\s*(\d+)')
        hours = _num(r'(\d+(?:\.\d+)?)\s*(?:時間|hour|h)')
        if hours and not dur:
            dur = hours * 60
        act_type = (
            "running" if _re.search(r'ラン|走|run', txt, _re.IGNORECASE) else
            "hiking"  if _re.search(r'ハイク|登山|hiking', txt, _re.IGNORECASE) else
            "cycling" if _re.search(r'サイクル|自転車|cycling', txt, _re.IGNORECASE) else None
        )
        parsed = {"distance_km": dist, "duration_min": dur, "avg_hr": hr, "activity_type": act_type, "notes": txt}

    return {"parsed": parsed, "has_ai": has_ai}


# ══════════════════════════════════════════════════════════════════════
# 機能10: 週次AIコーチングメッセージ
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/ai/weekly-coaching")
@limiter.limit("10/minute")
def get_weekly_coaching(
    request:      Request,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """今週のトレーニング振り返りと来週の処方をAIが生成する（週ごとにキャッシュ）。"""
    import datetime as dt2
    now    = _dt.datetime.now(_dt.timezone.utc)
    year_w = f"{now.isocalendar()[0]}_{now.isocalendar()[1]}"
    cache_key = f"{current_user.id}_{year_w}"

    if cache_key in _weekly_coaching_cache:
        return _weekly_coaching_cache[cache_key]

    from backend.models_db import Activity as Act, DailyMetrics as DM
    week_ago = now - _dt.timedelta(days=7)
    acts = db.query(Act).filter(Act.user_id == current_user.id, Act.start_time >= week_ago).all()
    latest_dm = db.query(DM).filter(DM.user_id == current_user.id).order_by(DM.date.desc()).first()

    n_acts   = len(acts)
    total_km = sum((a.distance_m or 0) for a in acts) / 1000
    total_h  = sum((a.duration_sec or 0) for a in acts) / 3600
    avg_hr   = float(np.mean([a.avg_hr for a in acts if a.avg_hr])) if any(a.avg_hr for a in acts) else None

    ctl  = round(latest_dm.ctl, 1)  if latest_dm and latest_dm.ctl  else 0
    tsb  = round(latest_dm.tsb, 1)  if latest_dm and latest_dm.tsb  else 0
    hrv  = round(latest_dm.hrv_rmssd, 1) if latest_dm and latest_dm.hrv_rmssd else None

    prompt = (
        f"あなたはプロのスポーツコーチです。以下の1週間のトレーニングデータを分析し、"
        f"日本語で振り返りメッセージと来週の処方を200字以内で書いてください。\n\n"
        f"【今週の実績】アクティビティ数:{n_acts}回 / 総距離:{total_km:.1f}km / 総時間:{total_h:.1f}h / 平均心拍:{round(avg_hr) if avg_hr else 'N/A'}bpm\n"
        f"【体調指標】CTL:{ctl} / TSB:{tsb} / HRV:{hrv}ms\n"
        f"親しみやすい口調で、具体的なアドバイスを含めてください。"
    )

    message  = None
    has_ai   = False
    try:
        engine = AIQueryEngine()
        ctx    = QueryContext(recent_activities=[], daily_metrics=[], stats={"ctl": ctl, "tsb": tsb})
        res    = engine.query(question=prompt, context=ctx)
        if res.provider != "fallback":
            message = res.answer
            has_ai  = True
    except Exception:
        pass

    if not message:
        cond = "良好" if tsb > -10 else ("やや疲労" if tsb > -25 else "要回復")
        message = (
            f"今週は{n_acts}回のトレーニング、合計{total_km:.1f}kmお疲れさまでした！"
            f"現在のコンディションは「{cond}」（TSB={tsb}）です。"
            + ("来週も同じペースで継続しましょう。" if tsb > -10 else "来週は強度を少し落として回復を優先してください。")
        )

    result = {
        "message":     message,
        "has_ai":      has_ai,
        "week":        year_w,
        "summary":     {"n_activities": n_acts, "total_km": round(total_km, 1), "total_hours": round(total_h, 1)},
        "metrics":     {"ctl": ctl, "tsb": tsb, "hrv": hrv},
    }
    _weekly_coaching_cache[cache_key] = result
    _trim_cache(_weekly_coaching_cache, _CACHE_MAX_WEEKLY)
    return result

# ── フロントエンド静的ファイル配信（本番用）────────────────────────────
_DIST_DIR = os.path.join(os.path.dirname(__file__), "..", "dashboard", "dist")
if os.path.isdir(_DIST_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST_DIR, "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str):
        """API以外のすべてのパスをReactのindex.htmlにフォールバック。"""
        # パストラバーサル対策: realpath で正規化し、dist ディレクトリ外へのアクセスを拒否
        dist_real = os.path.realpath(_DIST_DIR)
        file_path = os.path.realpath(os.path.join(_DIST_DIR, full_path))
        if file_path.startswith(dist_real + os.sep) or file_path == dist_real:
            if os.path.isfile(file_path):
                return FileResponse(file_path)
        # index.html は必ずサーバーから最新版を取得させる（ブラウザキャッシュ防止）
        return FileResponse(
            os.path.join(_DIST_DIR, "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
