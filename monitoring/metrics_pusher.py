#!/usr/bin/env python3
"""
Super Scaner 監控指標推送腳本
由 cron 每分鐘執行一次，將 EC2 狀態推送到 Google Sheets
"""
import os
import sys
from datetime import datetime
import pytz

# 確保可以 import 同目錄的模組
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

from monitoring.system_metrics import get_cpu_percent, get_ram_percent, get_disk_percent
from monitoring.docker_metrics import get_container_status, get_container_logs
from monitoring.log_parser import parse_log_lines, extract_daily_stats
from monitoring.sheets_writer import SheetsWriter

CONTAINER_NAME = 'scan-bot'
JST = pytz.timezone('Asia/Tokyo')


def main():
    spreadsheet_id = os.getenv('MONITOR_SPREADSHEET_ID')
    credentials_file = os.getenv('SERVICE_ACCOUNT_FILE', 'service_account.json')

    if not spreadsheet_id:
        print('[ERROR] MONITOR_SPREADSHEET_ID が設定されていません')
        sys.exit(1)

    # 認証ファイルの絶対パス解決
    if not os.path.isabs(credentials_file):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        credentials_file = os.path.join(base_dir, credentials_file)

    writer = SheetsWriter(spreadsheet_id, credentials_file)

    # 1. システム指標取得
    cpu = get_cpu_percent()
    ram = get_ram_percent()
    disk = get_disk_percent()

    # 2. Docker 状態取得
    container = get_container_status(CONTAINER_NAME)

    # 3. ログ取得・解析
    raw_logs = get_container_logs(CONTAINER_NAME, tail=200)
    parsed_logs = parse_log_lines(raw_logs)

    # 4. 当日統計抽出
    today = datetime.now(JST).strftime('%Y-%m-%d')
    stats = extract_daily_stats(raw_logs)

    # 5. Sheets 書き込み
    writer.write_heartbeat(
        container_status=container['status'],
        restart_count=container['restart_count'],
        cpu_pct=cpu,
        ram_pct=ram,
        disk_pct=disk
    )
    writer.write_logs(parsed_logs[-50:])  # 最新50行のみ
    writer.update_daily_stats(
        date_str=today,
        success_count=stats['success_count'],
        fail_count=stats['fail_count'],
        total_amount_jpy=stats['total_amount_jpy']
    )

    print(f'[OK] {datetime.now(JST).strftime("%H:%M:%S")} 推送完了 '
          f'| {container["status"]} | CPU:{cpu}% RAM:{ram}% Disk:{disk}%')


if __name__ == '__main__':
    main()
