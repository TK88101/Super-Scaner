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
        # 超過上限時刪除最舊的行（保留 header 第1行）
        all_rows = sheet.get_all_values()
        if len(all_rows) > MAX_HEARTBEAT_ROWS + 1:
            sheet.delete_rows(2, len(all_rows) - MAX_HEARTBEAT_ROWS)

    def write_logs(self, log_entries: List[Dict]):
        sheet = self._get_sheet('logs')
        if not log_entries:
            return
        rows = [[e.get('timestamp', ''), e.get('level', 'INFO'), e.get('message', '')] for e in log_entries]
        sheet.append_rows(rows, value_input_option='USER_ENTERED')
        all_rows = sheet.get_all_values()
        if len(all_rows) > MAX_LOG_ROWS + 1:
            sheet.delete_rows(2, len(all_rows) - MAX_LOG_ROWS)

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
