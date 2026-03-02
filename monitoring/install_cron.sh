#!/usr/bin/env bash
# Super Scaner 監控 cron 安裝腳本
# EC2 上執行：bash monitoring/install_cron.sh
set -euo pipefail

# 此腳本在 EC2 的 /home/ubuntu/apps/super-scaner 目錄中執行
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUSHER="$APP_DIR/monitoring/metrics_pusher.py"
LOG_FILE="/home/ubuntu/metrics_pusher.log"
PYTHON_BIN="$(which python3)"

echo "=== Super Scaner Monitoring Cron Installer ==="
echo "APP_DIR: $APP_DIR"
echo "PUSHER: $PUSHER"
echo "PYTHON: $PYTHON_BIN"

# 依賴インストール
echo "[1/3] Installing monitoring dependencies..."
pip3 install -r "$APP_DIR/monitoring/requirements.txt" --quiet

# cron エントリ生成
CRON_ENTRY="* * * * * $PYTHON_BIN $PUSHER >> $LOG_FILE 2>&1"

echo "[2/3] Adding cron job..."
# 既存のエントリを削除してから追加（冪等性確保）
(crontab -l 2>/dev/null | grep -v "metrics_pusher.py"; echo "$CRON_ENTRY") | crontab -

echo "[3/3] Verifying cron..."
crontab -l | grep "metrics_pusher"

echo ""
echo "=== インストール完了 ==="
echo "ログ確認: tail -f $LOG_FILE"
echo "cron確認: crontab -l"
