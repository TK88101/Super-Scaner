#!/usr/bin/env python3
"""
daily_backup.py — 毎日 22:00 JST に実行する Google Sheets バックアップスクリプト

処理:
1. 工作 Sheet (OUTPUT_SPREADSHEET_ID) の全員工 tab を読み取り
2. 備份 Sheet (BACKUP_SPREADSHEET_ID) に日付付き tab としてコピー
3. 工作 Sheet の各 tab をクリア（ヘッダーは保持）

cron 設定 (UTC): 0 13 * * * (= JST 22:00)
EC2 ホスト上で実行（Docker コンテナ外）
"""
import os
import sys
import re
import gspread
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# .env を読み込む（secrets ディレクトリ → プロジェクトルートの順で探す）
script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(script_dir)
secrets_env = os.path.expanduser("~/super-scaner-secrets/.env")
if os.path.exists(secrets_env):
    load_dotenv(secrets_env)
else:
    load_dotenv(os.path.join(project_dir, '.env'))

# 環境変数からの設定
# EC2 実行時は secrets ディレクトリのパスを使用
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")
if not os.path.isabs(SERVICE_ACCOUNT_FILE):
    # 相対パスの場合、secrets ディレクトリを優先
    secrets_path = os.path.expanduser("~/super-scaner-secrets/service_account.json")
    if os.path.exists(secrets_path):
        SERVICE_ACCOUNT_FILE = secrets_path
    else:
        SERVICE_ACCOUNT_FILE = os.path.join(project_dir, SERVICE_ACCOUNT_FILE)

OUTPUT_SPREADSHEET_ID = os.getenv("OUTPUT_SPREADSHEET_ID", "")
BACKUP_SPREADSHEET_ID = os.getenv("BACKUP_SPREADSHEET_ID", "")

JST = timezone(timedelta(hours=9))
RETENTION_DAYS = 90  # バックアップ tab の保持日数（3ヶ月）

# tab 名のパターン: {名前}_{文書タイプ}
TAB_PATTERN = re.compile(r'^.+_(領収書|支払請求書|売上請求書|給与明細)$')
# バックアップ tab 名のパターン: 2026-03-18_池田尚也_領収書
BACKUP_TAB_PATTERN = re.compile(r'^(\d{4}-\d{2}-\d{2})_.+_(領収書|支払請求書|売上請求書|給与明細)$')


def log(msg):
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}")


def main():
    if not OUTPUT_SPREADSHEET_ID or not BACKUP_SPREADSHEET_ID:
        log("❌ OUTPUT_SPREADSHEET_ID または BACKUP_SPREADSHEET_ID が未設定")
        sys.exit(1)

    log("🔄 日次バックアップ開始")

    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    work_ss = gc.open_by_key(OUTPUT_SPREADSHEET_ID)
    backup_ss = gc.open_by_key(BACKUP_SPREADSHEET_ID)

    today = datetime.now(JST).strftime("%Y-%m-%d")
    backed_up = 0
    cleared = 0

    for ws in work_ss.worksheets():
        tab_name = ws.title

        # _config tab や非データ tab はスキップ
        if tab_name.startswith("_") or not TAB_PATTERN.match(tab_name):
            continue

        all_data = ws.get_all_values()
        if len(all_data) <= 1:
            log(f"  ⏭️ {tab_name}: データなし（スキップ）")
            continue

        # バックアップ tab 名: 2026-03-18_池田尚也_領収書
        backup_tab_name = f"{today}_{tab_name}"

        # 備份 Sheet にコピー
        try:
            backup_ws = backup_ss.add_worksheet(
                title=backup_tab_name,
                rows=len(all_data),
                cols=len(all_data[0]) if all_data else 28,
            )
            backup_ws.update(f"A1", all_data, value_input_option='USER_ENTERED')
            log(f"  ✅ バックアップ完了: {backup_tab_name} ({len(all_data)-1} 行)")
            backed_up += 1
        except Exception as e:
            log(f"  ❌ バックアップ失敗 ({tab_name}): {e}")
            continue

        # 工作 Sheet のデータをクリア（ヘッダー保持）
        try:
            header = all_data[0]
            ws.clear()
            ws.append_row(header, value_input_option='USER_ENTERED')
            log(f"  🧹 クリア完了: {tab_name}")
            cleared += 1
        except Exception as e:
            log(f"  ⚠️ クリア失敗 ({tab_name}): {e}")

    log(f"🏁 バックアップ完了: {backed_up} tab バックアップ, {cleared} tab クリア")

    # 古いバックアップ tab を削除（90日超過）
    cleanup_old_backups(backup_ss, today)


def cleanup_old_backups(backup_ss, today_str):
    """RETENTION_DAYS を超えたバックアップ tab を削除"""
    today_date = datetime.strptime(today_str, "%Y-%m-%d").date()
    deleted = 0

    for ws in backup_ss.worksheets():
        m = BACKUP_TAB_PATTERN.match(ws.title)
        if not m:
            continue

        try:
            tab_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            age_days = (today_date - tab_date).days
            if age_days > RETENTION_DAYS:
                backup_ss.del_worksheet(ws)
                log(f"  🗑️ 古いバックアップを削除: {ws.title} ({age_days}日前)")
                deleted += 1
        except ValueError:
            continue

    if deleted:
        log(f"  🧹 {deleted} 個の古いバックアップ tab を削除")


if __name__ == "__main__":
    main()
