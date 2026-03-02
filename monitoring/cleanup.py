#!/usr/bin/env python3
"""
Super Scaner Sheets 清理腳本
由 cron 每小時執行一次，刪除超出上限的舊資料
"""
import os
import sys
from datetime import datetime
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

from monitoring.sheets_writer import SheetsWriter

JST = pytz.timezone('Asia/Tokyo')


def main():
    spreadsheet_id = os.getenv('MONITOR_SPREADSHEET_ID')
    credentials_file = os.getenv('SERVICE_ACCOUNT_FILE', 'service_account.json')

    if not spreadsheet_id:
        print('[ERROR] MONITOR_SPREADSHEET_ID が設定されていません')
        sys.exit(1)

    if not os.path.isabs(credentials_file):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        credentials_file = os.path.join(base_dir, credentials_file)

    writer = SheetsWriter(spreadsheet_id, credentials_file)
    now = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{now}] cleanup 開始')
    writer.cleanup()
    print(f'[{now}] cleanup 完了')


if __name__ == '__main__':
    main()
