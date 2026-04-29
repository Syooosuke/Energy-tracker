"""
database.py — SQLAlchemy + SQLite セットアップ
================================================
データは /app/data/energy_tracker.db に保存される。
docker-compose の volumes: - ./data:/app/data により
コンテナ再起動後もデータは永続化される。
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# データベースファイルのパス
# 環境変数 DB_PATH が設定されていればそれを使う（Renderの永続ディスク用）
# なければデフォルトの data/ ディレクトリ
if os.environ.get("DB_PATH"):
    DB_PATH = os.environ["DB_PATH"]
    _DB_DIR = os.path.dirname(DB_PATH)
else:
    _DB_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
    DB_PATH = os.path.join(_DB_DIR, "energy_tracker.db")
DB_URL   = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False},  # SQLite + FastAPI スレッド対応
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
    os.makedirs(_DB_DIR, exist_ok=True)
    # models_db をインポートしてテーブル定義を登録してからcreate_all
    from backend import models_db  # noqa: F401
    Base.metadata.create_all(bind=engine)
    # DBファイルのパーミッションをオーナーのみ読み書き可能に制限（600）
    try:
        if os.path.exists(DB_PATH):
            os.chmod(DB_PATH, 0o600)
    except OSError:
        pass  # コンテナ環境等でパーミッション変更できない場合は無視
