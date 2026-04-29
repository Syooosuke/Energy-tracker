"""
database.py — SQLAlchemy DB セットアップ
=========================================
- DATABASE_URL 環境変数が設定されていれば PostgreSQL（Supabase等）を使用
- なければローカルの SQLite にフォールバック
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# DATABASE_URL が設定されていれば PostgreSQL、なければ SQLite
_database_url = os.environ.get("DATABASE_URL")

if _database_url:
    # Supabase等のPostgreSQL接続
    # postgres:// → postgresql:// に正規化（SQLAlchemy 2.x対応）
    if _database_url.startswith("postgres://"):
        _database_url = _database_url.replace("postgres://", "postgresql://", 1)
    DB_URL = _database_url
    engine = create_engine(
        DB_URL,
        echo=False,
        pool_pre_ping=True,   # アイドル後の接続断（Supabase 5分タイムアウト等）でクラッシュしない
        pool_recycle=300,     # 5分ごとに接続を更新
    )
    DB_PATH = None
    _DB_DIR = None
else:
    # ローカル開発用 SQLite
    _DB_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
    DB_PATH = os.path.join(_DB_DIR, "energy_tracker.db")
    DB_URL  = f"sqlite:///{DB_PATH}"
    engine  = create_engine(
        DB_URL,
        connect_args={"check_same_thread": False},
        echo=False,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI の Depends で使う DB セッションジェネレータ。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """起動時にテーブルを作成する（存在しない場合のみ）。"""
    if _DB_DIR:
        os.makedirs(_DB_DIR, exist_ok=True)
    from backend import models_db  # noqa: F401
    Base.metadata.create_all(bind=engine)
    # SQLite のみ: パーミッション制限
    if DB_PATH and os.path.exists(DB_PATH):
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError:
            pass
