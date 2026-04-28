#!/bin/bash
# EC2 初期セットアップスクリプト
# Ubuntu 22.04 LTS 向け
# 実行: bash scripts/ec2_setup.sh your-domain.example.com

set -e

DOMAIN="${1:?Usage: $0 <domain>  例: $0 energy.example.com}"
APP_DIR="$HOME/energy-tracker"

echo "=== [1/6] システム更新 ==="
sudo apt-get update -y
sudo apt-get upgrade -y

echo "=== [2/6] Docker インストール ==="
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"
  echo "Docker をインストールしました。次回ログイン以降 sudo 不要になります。"
fi
docker --version

echo "=== [3/6] nginx + certbot インストール ==="
sudo apt-get install -y nginx certbot python3-certbot-nginx

echo "=== [4/6] nginx 設定（HTTP → HTTPS リダイレクト + リバースプロキシ）==="
sudo tee /etc/nginx/sites-available/energy-tracker > /dev/null <<NGINX
server {
    listen 80;
    server_name ${DOMAIN};
    # certbot が ACME チャレンジに使う。その後 HTTPS へリダイレクト
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}

server {
    listen 443 ssl;
    server_name ${DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;

    # Docker コンテナ（frontend nginx）へ転送
    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }

    # ファイルアップロード上限（GPX / HR ログ）
    client_max_body_size 20M;
}
NGINX

sudo ln -sf /etc/nginx/sites-available/energy-tracker /etc/nginx/sites-enabled/energy-tracker
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t

echo "=== [5/6] Let's Encrypt 証明書取得 ==="
# 事前に DNS A レコードがこのサーバーを向いている必要があります
sudo certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos \
  --register-unsafely-without-email || {
  echo ""
  echo "⚠  証明書取得に失敗しました。以下を確認してください:"
  echo "   1. DNS: ${DOMAIN} → このサーバーの IP に向いているか"
  echo "   2. ポート 80/443 がセキュリティグループで開放されているか"
  echo "   3. 取得後に sudo nginx -t && sudo systemctl reload nginx を実行してください"
  exit 1
}

sudo systemctl enable nginx
sudo systemctl reload nginx

echo "=== [6/6] アプリのクローンと初回起動 ==="
if [ ! -d "$APP_DIR" ]; then
  echo "リポジトリをクローンしてください:"
  echo "  git clone https://github.com/<your-org>/energy-tracker.git $APP_DIR"
  echo "その後:"
  echo "  cd $APP_DIR && cp .env.example .env && vi .env  # キーを設定"
  echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build"
else
  cd "$APP_DIR"
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
fi

echo ""
echo "======================================================"
echo " セットアップ完了!"
echo " https://${DOMAIN} でアクセスできます"
echo ""
echo " GitHub Secrets に以下を登録してください:"
echo "   EC2_HOST  = $(curl -s ifconfig.me 2>/dev/null || echo '<このサーバーのIP>')"
echo "   EC2_USER  = $USER"
echo "   EC2_SSH_KEY = ~/.ssh/id_rsa の内容（秘密鍵）"
echo "======================================================"
