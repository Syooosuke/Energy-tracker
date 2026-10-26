# ── dashboard/Dockerfile ───────────────────────────────────────────
# マルチステージビルド:
#   Stage 1 (builder) : Node で React アプリをビルド
#   Stage 2 (runner)  : Nginx で静的ファイルを配信
# ───────────────────────────────────────────────────────────────────

# ── Stage 1: ビルド ─────────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

# 依存パッケージを先にインストール（package*.json が変わらない限りキャッシュ再利用）
COPY package.json package-lock.json .npmrc ./
RUN npm ci --silent

# ソースをコピーしてビルド
COPY . .
RUN npm run build


# ── Stage 2: 本番配信 ───────────────────────────────────────────────
FROM nginx:1.27-alpine AS runner

# ビルド成果物を Nginx の配信ディレクトリへ
COPY --from=builder /app/dist /usr/share/nginx/html

# カスタム Nginx 設定（/api プロキシ + SPA フォールバック）
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
