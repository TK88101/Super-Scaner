import gspread
from datetime import datetime
from typing import List, Dict
import pytz

JST = pytz.timezone('Asia/Tokyo')
MAX_LOG_ROWS = 500
MAX_HEARTBEAT_ROWS = 1440  # 24小時 × 60分鐘


class SheetsWriter:
    def __init__(self, spreadsheet_id: str, credentials_file: str):
        self.spreadsheet_id = spreadsheet_id
        gc = gspread.service_account(filename=credentials_file)
        self.spreadsheet = gc.open_by_key(spreadsheet_id)

    def _get_sheet(self, name: str):
        return self.spreadsheet.worksheet(name)

    def write_heartbeat(self, container_status: str, restart_count: int,
                        cpu_pct: float, ram_pct: float, disk_pct: float):
        sheet = self._get_sheet('heartbeat')
        now = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')
        row = [now, container_status, restart_count, cpu_pct, ram_pct, disk_pct]
        sheet.append_row(row, value_input_option='USER_ENTERED')

    def write_logs(self, log_entries: List[Dict]):
        sheet = self._get_sheet('logs')
        if not log_entries:
            return
        rows = [[e.get('timestamp', ''), e.get('level', 'INFO'), e.get('message', '')] for e in log_entries]
        sheet.append_rows(rows, value_input_option='USER_ENTERED')

    def cleanup(self):
        """清理各 Sheet 超出上限的舊資料，每小時由 cron 執行一次"""
        configs = [
            ('heartbeat', MAX_HEARTBEAT_ROWS),
            ('logs', MAX_LOG_ROWS),
            ('processing_stats', 90),  # 保留最近 90 天
        ]
        for sheet_name, max_rows in configs:
            sheet = self._get_sheet(sheet_name)
            all_rows = sheet.get_all_values()
            excess = len(all_rows) - 1 - max_rows  # 扣掉 header
            if excess > 0:
                sheet.delete_rows(2, excess)
                print(f'[cleanup] {sheet_name}: 刪除 {excess} 行')

    def update_daily_stats(self, date_str: str, success_count: int,
                           fail_count: int, total_amount_jpy: int):
        sheet = self._get_sheet('processing_stats')
        all_rows = sheet.get_all_values()
        # 找到今日行並更新（upsert）
        for i, row in enumerate(all_rows[1:], start=2):
            if row and row[0] == date_str:
                sheet.update(f'A{i}:D{i}',
                             [[date_str, success_count, fail_count, total_amount_jpy]])
                return
        # 不存在則追加
        sheet.append_row([date_str, success_count, fail_count, total_amount_jpy])
