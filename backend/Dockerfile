# ── backend/Dockerfile ─────────────────────────────────────────────
# ビルドコンテキスト: プロジェクトルート（models/ を含むため）
# docker-compose から: context: . / dockerfile: backend/Dockerfile
# ───────────────────────────────────────────────────────────────────

FROM python:3.13-slim

# タイムゾーン設定（ログの時刻を日本時間に）
ENV TZ=Asia/Tokyo \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl fonts-noto-cjk fonts-ipafont-gothic fontconfig && \
    fc-cache -fv && \
    rm -rf /var/lib/apt/lists/*

# 依存パッケージを先にインストール（レイヤーキャッシュを活かす）
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    python -c "import matplotlib.font_manager as fm; fm._rebuild()" 2>/dev/null || \
    find / -name "fontlist-*.json" -delete 2>/dev/null; true

# アプリケーションコードをコピー
COPY backend/ ./backend/
COPY models/   ./models/
COPY src/      ./src/

# data ディレクトリを作成（CSV が存在しない場合でも起動できるように）
RUN mkdir -p data/processed

# 非rootユーザーで実行（セキュリティ強化）
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
