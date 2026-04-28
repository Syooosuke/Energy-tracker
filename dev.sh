#!/bin/bash
# 開発環境起動スクリプト
# 使い方:
#   ./dev.sh          — 通常起動
#   ./dev.sh --build  — Dockerイメージを再ビルドして起動（Dockerfile/requirements.txt変更時）

set -e
cd "$(dirname "$0")"

# バックエンドをDockerで起動
echo "▶ バックエンド起動中..."
if [[ "$1" == "--build" ]]; then
  docker compose up -d --build
else
  docker compose up -d
fi

# バックエンドが応答するまで待機
echo "▶ バックエンドの準備を待機中..."
until curl -sf http://localhost:8000/api/boot-time > /dev/null 2>&1; do
  printf "."
  sleep 1
done
echo " 完了"

# フロントエンドを起動（ここで待機完了してから起動）
echo "▶ フロントエンド起動中... http://localhost:5173"
echo "  （Ctrl+C で停止）"
cd dashboard && npm run dev
