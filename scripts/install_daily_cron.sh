#!/usr/bin/env bash
# install_daily_cron.sh — EC2 ホスト上で daily_backup.py の cron をインストール
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SCRIPT="${SCRIPT_DIR}/daily_backup.py"
VENV_PYTHON="${VENV_PYTHON:-/usr/bin/python3}"
LOG_DIR="${HOME}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/super-scaner-backup.log"

# cron エントリ: 毎日 13:00 UTC (= 22:00 JST)
CRON_ENTRY="0 13 * * * ${VENV_PYTHON} ${BACKUP_SCRIPT} >> ${LOG_FILE} 2>&1"

# 既存の cron に重複がないか確認
EXISTING=$(crontab -l 2>/dev/null || true)
if echo "${EXISTING}" | grep -qF "daily_backup.py"; then
    echo "⚠️ daily_backup.py の cron は既にインストール済みです"
    echo "既存のエントリ:"
    echo "${EXISTING}" | grep "daily_backup.py"
    exit 0
fi

# cron に追加
(echo "${EXISTING}"; echo "${CRON_ENTRY}") | crontab -
echo "✅ cron インストール完了:"
echo "   ${CRON_ENTRY}"
echo ""
echo "ログ出力先: ${LOG_FILE}"
echo "手動テスト: ${VENV_PYTHON} ${BACKUP_SCRIPT}"
