import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from unittest.mock import patch, MagicMock, call
from monitoring.sheets_writer import SheetsWriter

def make_mock_spreadsheet():
    """建立模擬的 gspread Spreadsheet"""
    mock_sheet = MagicMock()
    mock_sheet.get_all_values.return_value = [['header'], ['row1'], ['row2']]
    mock_spreadsheet = MagicMock()
    mock_spreadsheet.worksheet.return_value = mock_sheet
    return mock_spreadsheet, mock_sheet

def test_write_heartbeat_appends_row():
    mock_gc = MagicMock()
    mock_spreadsheet, mock_sheet = make_mock_spreadsheet()
    mock_gc.open_by_key.return_value = mock_spreadsheet

    with patch('gspread.service_account', return_value=mock_gc):
        writer = SheetsWriter('fake_key', 'fake_creds.json')
        writer.write_heartbeat(
            container_status='running',
            restart_count=0,
            cpu_pct=45.2,
            ram_pct=60.1,
            disk_pct=30.5
        )

    mock_sheet.append_row.assert_called_once()
    args = mock_sheet.append_row.call_args[0][0]
    assert args[1] == 'running'
    assert args[2] == 0

def test_write_logs_limits_to_500_rows():
    mock_gc = MagicMock()
    mock_spreadsheet, mock_sheet = make_mock_spreadsheet()
    # 模擬已有 510 行（含 header）
    mock_sheet.get_all_values.return_value = [['h']] + [['r']] * 510
    mock_gc.open_by_key.return_value = mock_spreadsheet

    with patch('gspread.service_account', return_value=mock_gc):
        writer = SheetsWriter('fake_key', 'fake_creds.json')
        writer.write_logs([{'timestamp': '2026-03-02', 'level': 'INFO', 'message': 'test'}])

    # 應該呼叫 delete_rows 清除舊資料
    mock_sheet.delete_rows.assert_called()
