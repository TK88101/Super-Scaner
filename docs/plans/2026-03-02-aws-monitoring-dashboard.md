# AWS Monitoring Dashboard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 為 Super Scaner OCR 機器人建立監控 Dashboard — EC2 每分鐘推送指標到 Google Sheets，GAS Web App 顯示 4 頁籤儀表盤。

**Architecture:** 獨立的 `metrics_pusher.py` 腳本運行在 EC2 cron 中，通過現有的 `service_account.json` 將容器狀態、系統指標、日誌推送到 Google Sheets。GAS Web App 讀取 Sheets 並渲染為帶頁籤的 Dashboard。監控程序與主程序完全解耦。

**Tech Stack:** Python 3.9+ (gspread, subprocess, re), Google Sheets API v4, Google Apps Script (HTML Service), Ubuntu crontab

---

## 環境前提（手動操作，實施前完成）

1. 在 Google Drive 建立名為 `SuperScaner-Monitor` 的試算表
2. 在試算表中建立 4 個 Sheet：`heartbeat`、`processing_stats`、`logs`、`config`
3. 將試算表分享給 `service_account.json` 中的 `client_email`（設為編輯者）
4. 複製試算表 URL 中的 ID（`/d/` 與 `/edit` 之間的字串）
5. 在 `.env` 中追加：`MONITOR_SPREADSHEET_ID=<你複製的ID>`

---

## Task 1: 建立 monitoring 目錄結構

**Files:**
- Create: `monitoring/__init__.py`
- Create: `monitoring/requirements.txt`

**Step 1: 建立目錄與空的 __init__.py**

```bash
mkdir -p monitoring
touch monitoring/__init__.py
```

**Step 2: 建立 monitoring/requirements.txt**

```
gspread==6.1.2
```

（注意：`google-auth` 已在主專案的 requirements.txt 中，不需重複）

**Step 3: Commit**

```bash
git add monitoring/
git commit -m "feat: monitoring ディレクトリ作成"
```

---

## Task 2: 系統指標收集模組（含測試）

**Files:**
- Create: `monitoring/system_metrics.py`
- Create: `monitoring/tests/test_system_metrics.py`

**Step 1: 建立測試文件 `monitoring/tests/__init__.py`**

```bash
mkdir -p monitoring/tests
touch monitoring/tests/__init__.py
```

**Step 2: 撰寫失敗測試 `monitoring/tests/test_system_metrics.py`**

```python
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from monitoring.system_metrics import get_cpu_percent, get_ram_percent, get_disk_percent

def test_cpu_percent_returns_float():
    result = get_cpu_percent()
    assert isinstance(result, float)
    assert 0.0 <= result <= 100.0

def test_ram_percent_returns_float():
    result = get_ram_percent()
    assert isinstance(result, float)
    assert 0.0 <= result <= 100.0

def test_disk_percent_returns_float():
    result = get_disk_percent()
    assert isinstance(result, float)
    assert 0.0 <= result <= 100.0
```

**Step 3: 執行確認測試失敗**

```bash
cd "/Users/ibridgezhao/Documents/Super Scaner"
python -m pytest monitoring/tests/test_system_metrics.py -v
```

期望：`ModuleNotFoundError: No module named 'monitoring.system_metrics'`

**Step 4: 實現 `monitoring/system_metrics.py`**

```python
import time


def get_cpu_percent() -> float:
    """讀取 /proc/stat 計算 CPU 使用率（需兩次採樣）"""
    def read_cpu_times():
        with open('/proc/stat', 'r') as f:
            line = f.readline()
        parts = line.split()
        user, nice, system, idle, iowait = int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
        total = user + nice + system + idle + iowait
        return total - idle, total

    active1, total1 = read_cpu_times()
    time.sleep(0.2)
    active2, total2 = read_cpu_times()
    delta_total = total2 - total1
    if delta_total == 0:
        return 0.0
    return round((active2 - active1) / delta_total * 100, 1)


def get_ram_percent() -> float:
    """讀取 /proc/meminfo 計算 RAM 使用率"""
    mem = {}
    with open('/proc/meminfo', 'r') as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                mem[parts[0].rstrip(':')] = int(parts[1])
    total = mem.get('MemTotal', 1)
    available = mem.get('MemAvailable', 0)
    used = total - available
    return round(used / total * 100, 1)


def get_disk_percent(path: str = '/') -> float:
    """使用 os.statvfs 計算磁碟使用率"""
    import os
    stat = os.statvfs(path)
    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bfree * stat.f_frsize
    used = total - free
    if total == 0:
        return 0.0
    return round(used / total * 100, 1)
```

注意：`/proc/stat` 和 `/proc/meminfo` 僅存在於 Linux（EC2 Ubuntu）。在 Mac 本機開發時測試會失敗，這是預期行為，EC2 上測試才是目標。

**Step 5: 執行測試（在 EC2 上執行，Mac 會因 /proc 不存在而跳過）**

本機跳過此步驟，在 EC2 部署後再驗證。

**Step 6: Commit**

```bash
git add monitoring/system_metrics.py monitoring/tests/
git commit -m "feat: system metrics collector (CPU/RAM/disk)"
```

---

## Task 3: Docker 狀態收集模組（含測試）

**Files:**
- Create: `monitoring/docker_metrics.py`
- Modify: `monitoring/tests/test_docker_metrics.py`

**Step 1: 撰寫失敗測試 `monitoring/tests/test_docker_metrics.py`**

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from unittest.mock import patch, MagicMock
from monitoring.docker_metrics import get_container_status, get_container_logs

def test_get_container_status_running():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = '{"Status":"running","RestartCount":2}'
    with patch('subprocess.run', return_value=mock_result):
        status = get_container_status('scan-bot')
    assert status['status'] == 'running'
    assert status['restart_count'] == 2

def test_get_container_status_not_found():
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ''
    with patch('subprocess.run', return_value=mock_result):
        status = get_container_status('scan-bot')
    assert status['status'] == 'not_found'
    assert status['restart_count'] == 0

def test_get_container_logs_returns_list():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = 'line1\nline2\nline3'
    with patch('subprocess.run', return_value=mock_result):
        logs = get_container_logs('scan-bot', tail=100)
    assert isinstance(logs, list)
    assert len(logs) == 3
```

**Step 2: 執行確認測試失敗**

```bash
python -m pytest monitoring/tests/test_docker_metrics.py -v
```

期望：`ModuleNotFoundError`

**Step 3: 實現 `monitoring/docker_metrics.py`**

```python
import subprocess
import json
from typing import Dict, List


def get_container_status(container_name: str) -> Dict:
    """執行 docker inspect 獲取容器狀態"""
    result = subprocess.run(
        ['docker', 'inspect', '--format',
         '{"Status":"{{.State.Status}}","RestartCount":{{.RestartCount}}}',
         container_name],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {'status': 'not_found', 'restart_count': 0}
    try:
        data = json.loads(result.stdout.strip())
        return {
            'status': data.get('Status', 'unknown'),
            'restart_count': data.get('RestartCount', 0)
        }
    except json.JSONDecodeError:
        return {'status': 'unknown', 'restart_count': 0}


def get_container_logs(container_name: str, tail: int = 100) -> List[str]:
    """抓取容器最新日誌"""
    result = subprocess.run(
        ['docker', 'logs', '--tail', str(tail), container_name],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    # docker logs 可能輸出到 stderr（正常行為）
    output = result.stdout or result.stderr
    return [line for line in output.splitlines() if line.strip()]
```

**Step 4: 執行測試確認通過**

```bash
python -m pytest monitoring/tests/test_docker_metrics.py -v
```

期望：3 個測試 PASS

**Step 5: Commit**

```bash
git add monitoring/docker_metrics.py monitoring/tests/test_docker_metrics.py
git commit -m "feat: docker container status/logs collector"
```

---

## Task 4: 日誌解析模組（含測試）

**Files:**
- Create: `monitoring/log_parser.py`
- Create: `monitoring/tests/test_log_parser.py`

**Step 1: 撰寫失敗測試**

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from monitoring.log_parser import parse_log_lines, extract_daily_stats

SAMPLE_LOGS = [
    "2026-03-02 10:00:01 INFO ✅ 処理成功: 山田商店 - 合計 ¥5,500",
    "2026-03-02 10:01:00 INFO ✅ 処理成功: ABC株式会社 - 合計 ¥12,000",
    "2026-03-02 10:02:00 ERROR ❌ 処理失敗: ファイル読み込みエラー",
    "2026-03-02 10:03:00 INFO 監視中...",
    "2026-03-02 10:04:00 WARNING ⚠️ リトライ中",
]

def test_parse_log_lines_returns_structured():
    result = parse_log_lines(SAMPLE_LOGS)
    assert len(result) == 5
    assert result[0]['level'] == 'INFO'
    assert result[2]['level'] == 'ERROR'

def test_extract_daily_stats_counts_success():
    stats = extract_daily_stats(SAMPLE_LOGS)
    assert stats['success_count'] == 2
    assert stats['fail_count'] == 1

def test_extract_daily_stats_totals_amount():
    stats = extract_daily_stats(SAMPLE_LOGS)
    assert stats['total_amount_jpy'] == 17500

def test_parse_log_lines_empty():
    result = parse_log_lines([])
    assert result == []
```

**Step 2: 執行確認失敗**

```bash
python -m pytest monitoring/tests/test_log_parser.py -v
```

**Step 3: 實現 `monitoring/log_parser.py`**

```python
import re
from typing import List, Dict


def parse_log_lines(lines: List[str]) -> List[Dict]:
    """將日誌行解析為結構化字典"""
    result = []
    # 嘗試匹配 "YYYY-MM-DD HH:MM:SS LEVEL message" 格式
    pattern = re.compile(r'(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\s+(INFO|ERROR|WARNING|DEBUG)\s+(.*)')
    for line in lines:
        m = pattern.match(line)
        if m:
            result.append({
                'timestamp': m.group(1),
                'level': m.group(2),
                'message': m.group(3)
            })
        else:
            # 無法解析時歸類為 INFO
            result.append({
                'timestamp': '',
                'level': 'INFO',
                'message': line
            })
    return result


def extract_daily_stats(lines: List[str]) -> Dict:
    """從日誌行中提取當日統計（成功數、失敗數、總金額）"""
    success_count = 0
    fail_count = 0
    total_amount = 0

    success_pattern = re.compile(r'✅.*処理成功')
    fail_pattern = re.compile(r'❌.*処理失敗')
    amount_pattern = re.compile(r'¥([\d,]+)')

    for line in lines:
        if success_pattern.search(line):
            success_count += 1
            m = amount_pattern.search(line)
            if m:
                total_amount += int(m.group(1).replace(',', ''))
        elif fail_pattern.search(line):
            fail_count += 1

    return {
        'success_count': success_count,
        'fail_count': fail_count,
        'total_amount_jpy': total_amount
    }
```

**Step 4: 執行測試確認通過**

```bash
python -m pytest monitoring/tests/test_log_parser.py -v
```

期望：4 個測試 PASS

**Step 5: Commit**

```bash
git add monitoring/log_parser.py monitoring/tests/test_log_parser.py
git commit -m "feat: log parser - extract stats and structured logs"
```

---

## Task 5: Google Sheets 寫入模組（含測試）

**Files:**
- Create: `monitoring/sheets_writer.py`
- Create: `monitoring/tests/test_sheets_writer.py`

**Step 1: 安裝 gspread（本機開發環境）**

```bash
cd "/Users/ibridgezhao/Documents/Super Scaner"
pip install gspread==6.1.2
```

**Step 2: 撰寫失敗測試（使用 Mock，不實際連線）**

```python
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
```

**Step 3: 執行確認失敗**

```bash
python -m pytest monitoring/tests/test_sheets_writer.py -v
```

**Step 4: 實現 `monitoring/sheets_writer.py`**

```python
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
```

**Step 5: 執行測試確認通過**

```bash
python -m pytest monitoring/tests/test_sheets_writer.py -v
```

期望：2 個測試 PASS

**Step 6: Commit**

```bash
git add monitoring/sheets_writer.py monitoring/tests/test_sheets_writer.py
git commit -m "feat: Google Sheets writer with row limit management"
```

---

## Task 6: 主推送腳本 metrics_pusher.py

**Files:**
- Create: `monitoring/metrics_pusher.py`

**Step 1: 建立 `monitoring/metrics_pusher.py`**

```python
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
```

**Step 2: 追加 pytz 到 monitoring/requirements.txt**

```
gspread==6.1.2
pytz==2024.1
```

**Step 3: Commit**

```bash
git add monitoring/metrics_pusher.py monitoring/requirements.txt
git commit -m "feat: metrics_pusher.py - main push orchestrator"
```

---

## Task 7: cron 安裝腳本

**Files:**
- Create: `monitoring/install_cron.sh`

**Step 1: 建立 `monitoring/install_cron.sh`**

```bash
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
```

**Step 2: 設定執行權限並 Commit**

```bash
chmod +x monitoring/install_cron.sh
git add monitoring/install_cron.sh
git commit -m "feat: cron installer script for EC2"
```

---

## Task 8: GAS Dashboard

**Files:**
- Create: `gas/dashboard.gs`

**Step 1: 建立目錄與主程式**

```bash
mkdir -p gas
```

**Step 2: 建立 `gas/dashboard.gs`（完整 GAS 程式碼）**

```javascript
// =============================================
// Super Scaner Monitoring Dashboard (GAS)
// 部署方式：Apps Script > 部署 > 新しいデプロイ > ウェブアプリ
// 實行者：自分、アクセス権：全員
// =============================================

const SPREADSHEET_ID = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID');

function doGet() {
  return HtmlService.createHtmlOutput(getDashboardHtml())
    .setTitle('Super Scaner Monitor')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function getMetrics() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);

  // heartbeat - 最新行
  const hbSheet = ss.getSheetByName('heartbeat');
  const hbData = hbSheet.getLastRow() > 1
    ? hbSheet.getRange(hbSheet.getLastRow(), 1, 1, 6).getValues()[0]
    : ['', 'unknown', 0, 0, 0, 0];

  // processing_stats - 今日
  const today = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd');
  const statsSheet = ss.getSheetByName('processing_stats');
  const statsData = statsSheet.getDataRange().getValues();
  let todayStats = [today, 0, 0, 0];
  for (let i = 1; i < statsData.length; i++) {
    if (statsData[i][0] === today) { todayStats = statsData[i]; break; }
  }

  // 最近7日の処理統計
  const last7Days = statsData.slice(-7).filter(r => r[0]);

  // logs - 最新50行
  const logsSheet = ss.getSheetByName('logs');
  const logsData = logsSheet.getLastRow() > 1
    ? logsSheet.getRange(Math.max(2, logsSheet.getLastRow() - 49), 1, 50, 3).getValues()
    : [];

  // heartbeat - 最近60分の CPU トレンド
  const recentHb = hbSheet.getLastRow() > 1
    ? hbSheet.getRange(Math.max(2, hbSheet.getLastRow() - 59), 1, 60, 6).getValues()
    : [];

  // 最後のハートビートからの経過時間
  let minutesAgo = 999;
  if (hbData[0]) {
    const lastTs = new Date(hbData[0]);
    minutesAgo = Math.floor((new Date() - lastTs) / 60000);
  }

  return JSON.stringify({
    timestamp: hbData[0] || '-',
    container_status: hbData[1] || 'unknown',
    restart_count: hbData[2] || 0,
    cpu_pct: hbData[3] || 0,
    ram_pct: hbData[4] || 0,
    disk_pct: hbData[5] || 0,
    minutes_ago: minutesAgo,
    today_success: todayStats[1] || 0,
    today_fail: todayStats[2] || 0,
    today_amount: todayStats[3] || 0,
    last7days: last7Days,
    logs: logsData.reverse(),
    cpu_trend: recentHb.map(r => ({ts: r[0], cpu: r[3]}))
  });
}

function getDashboardHtml() {
  return `<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Super Scaner Monitor</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }
  .header { background: #1e293b; padding: 16px 24px; border-bottom: 1px solid #334155; display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 18px; font-weight: 700; color: #f1f5f9; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 9999px; background: #334155; color: #94a3b8; }
  .tabs { display: flex; gap: 2px; padding: 16px 24px 0; background: #0f172a; }
  .tab { padding: 8px 20px; border-radius: 8px 8px 0 0; cursor: pointer; font-size: 13px; color: #94a3b8; background: #1e293b; border: 1px solid #334155; border-bottom: none; }
  .tab.active { background: #1e40af; color: #fff; border-color: #1e40af; }
  .panel { display: none; padding: 24px; }
  .panel.active { display: block; }
  .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 20px; margin-bottom: 16px; }
  .status-big { font-size: 48px; font-weight: 900; }
  .status-online { color: #22c55e; }
  .status-offline { color: #ef4444; }
  .status-unknown { color: #f59e0b; }
  .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
  .stat-card { background: #0f172a; border-radius: 8px; padding: 16px; text-align: center; }
  .stat-value { font-size: 28px; font-weight: 800; color: #38bdf8; }
  .stat-label { font-size: 11px; color: #64748b; margin-top: 4px; }
  .progress-bar { background: #334155; border-radius: 9999px; height: 8px; margin-top: 8px; overflow: hidden; }
  .progress-fill { height: 100%; border-radius: 9999px; transition: width 0.3s; }
  .fill-ok { background: #22c55e; }
  .fill-warn { background: #f59e0b; }
  .fill-danger { background: #ef4444; }
  .log-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .log-table th { text-align: left; padding: 6px 8px; color: #64748b; border-bottom: 1px solid #334155; }
  .log-table td { padding: 5px 8px; border-bottom: 1px solid #1e293b; font-family: monospace; }
  .log-error { color: #ef4444; }
  .log-warn { color: #f59e0b; }
  .log-info { color: #94a3b8; }
  .filter-input { background: #0f172a; border: 1px solid #334155; color: #e2e8f0; padding: 6px 12px; border-radius: 6px; font-size: 13px; width: 280px; margin-bottom: 12px; }
  .refresh-info { font-size: 11px; color: #475569; text-align: right; padding: 8px 0; }
  canvas { max-width: 100%; }
</style>
</head>
<body>
<div class="header">
  <h1>🤖 Super Scaner Monitor</h1>
  <span class="badge" id="lastUpdate">読込中...</span>
</div>
<div class="tabs">
  <div class="tab active" onclick="switchTab('status')">📊 ステータス</div>
  <div class="tab" onclick="switchTab('stats')">📈 統計</div>
  <div class="tab" onclick="switchTab('logs')">📋 ログ</div>
  <div class="tab" onclick="switchTab('system')">💻 システム</div>
</div>

<div id="panel-status" class="panel active">
  <div class="card">
    <div class="status-big" id="statusIcon">⏳</div>
    <div style="margin-top:8px;font-size:14px;color:#94a3b8" id="statusLabel">読込中...</div>
    <div style="margin-top:4px;font-size:12px;color:#64748b" id="lastHeartbeat"></div>
    <div style="margin-top:4px;font-size:12px;color:#f59e0b" id="restartWarning"></div>
  </div>
</div>

<div id="panel-stats" class="panel">
  <div class="stat-grid" id="statsGrid">
    <div class="stat-card"><div class="stat-value" id="successCount">-</div><div class="stat-label">本日 成功</div></div>
    <div class="stat-card"><div class="stat-value" id="failCount" style="color:#ef4444">-</div><div class="stat-label">本日 失敗</div></div>
    <div class="stat-card"><div class="stat-value" id="totalAmount" style="color:#a78bfa">-</div><div class="stat-label">本日 処理金額（円）</div></div>
  </div>
  <div class="card">
    <div style="font-size:13px;color:#64748b;margin-bottom:12px">過去7日間 処理件数</div>
    <canvas id="chart7days" height="80"></canvas>
  </div>
</div>

<div id="panel-logs" class="panel">
  <input class="filter-input" id="logFilter" placeholder="🔍 キーワードフィルター..." oninput="renderLogs()">
  <div class="card" style="padding:0;overflow:hidden">
    <table class="log-table">
      <thead><tr><th>時刻</th><th>レベル</th><th>メッセージ</th></tr></thead>
      <tbody id="logBody"></tbody>
    </table>
  </div>
</div>

<div id="panel-system" class="panel">
  <div class="card">
    <div style="font-size:13px;color:#64748b;margin-bottom:12px">CPU 使用率</div>
    <div id="cpuPct" style="font-size:24px;font-weight:700">-</div>
    <div class="progress-bar"><div class="progress-fill" id="cpuBar" style="width:0%"></div></div>
    <div style="margin-top:16px;font-size:13px;color:#64748b">直近60分 CPU トレンド</div>
    <canvas id="cpuTrend" height="60" style="margin-top:8px"></canvas>
  </div>
  <div class="card">
    <div style="font-size:13px;color:#64748b;margin-bottom:8px">RAM 使用率</div>
    <div id="ramPct" style="font-size:24px;font-weight:700">-</div>
    <div class="progress-bar"><div class="progress-fill" id="ramBar" style="width:0%"></div></div>
  </div>
  <div class="card">
    <div style="font-size:13px;color:#64748b;margin-bottom:8px">ディスク 使用率</div>
    <div id="diskPct" style="font-size:24px;font-weight:700">-</div>
    <div class="progress-bar"><div class="progress-fill" id="diskBar" style="width:0%"></div></div>
  </div>
</div>

<div class="refresh-info" style="padding:8px 24px">自動更新: 60秒ごと | <span id="nextRefresh"></span></div>

<script>
let metricsData = null;
let countdown = 60;

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['status','stats','logs','system'][i] === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
}

function fillBar(id, pct) {
  const el = document.getElementById(id);
  el.style.width = pct + '%';
  el.className = 'progress-fill ' + (pct >= 90 ? 'fill-danger' : pct >= 70 ? 'fill-warn' : 'fill-ok');
}

function renderMetrics(data) {
  metricsData = data;
  // Status tab
  const isOnline = data.container_status === 'running' && data.minutes_ago < 5;
  const el = document.getElementById('statusIcon');
  el.textContent = isOnline ? '● 稼働中' : (data.container_status === 'running' ? '⚠️ 応答遅延' : '✕ 停止');
  el.className = 'status-big ' + (isOnline ? 'status-online' : data.container_status === 'running' ? 'status-unknown' : 'status-offline');
  document.getElementById('statusLabel').textContent = 'コンテナ: ' + data.container_status;
  document.getElementById('lastHeartbeat').textContent = '最終ハートビート: ' + (data.minutes_ago < 999 ? data.minutes_ago + '分前' : '不明') + ' (' + data.timestamp + ')';
  document.getElementById('restartWarning').textContent = data.restart_count > 0 ? '⚠️ 再起動回数: ' + data.restart_count : '';

  // Stats tab
  document.getElementById('successCount').textContent = data.today_success;
  document.getElementById('failCount').textContent = data.today_fail;
  document.getElementById('totalAmount').textContent = '¥' + (data.today_amount || 0).toLocaleString();
  drawBarChart('chart7days', data.last7days);

  // System tab
  document.getElementById('cpuPct').textContent = data.cpu_pct + '%';
  document.getElementById('ramPct').textContent = data.ram_pct + '%';
  document.getElementById('diskPct').textContent = data.disk_pct + '%';
  fillBar('cpuBar', data.cpu_pct);
  fillBar('ramBar', data.ram_pct);
  fillBar('diskBar', data.disk_pct);
  drawLineChart('cpuTrend', data.cpu_trend.map(d => d.cpu));

  // Header
  document.getElementById('lastUpdate').textContent = '更新: ' + new Date().toLocaleTimeString('ja-JP');
  renderLogs();
}

function renderLogs() {
  if (!metricsData) return;
  const filter = document.getElementById('logFilter').value.toLowerCase();
  const rows = metricsData.logs
    .filter(r => !filter || (r[2] || '').toLowerCase().includes(filter))
    .slice(0, 50)
    .map(r => {
      const cls = r[1] === 'ERROR' ? 'log-error' : r[1] === 'WARNING' ? 'log-warn' : 'log-info';
      return '<tr class="' + cls + '"><td>' + (r[0]||'') + '</td><td>' + (r[1]||'') + '</td><td>' + escHtml(r[2]||'') + '</td></tr>';
    }).join('');
  document.getElementById('logBody').innerHTML = rows || '<tr><td colspan="3" style="text-align:center;color:#475569;padding:20px">ログなし</td></tr>';
}

function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function drawBarChart(id, data) {
  const canvas = document.getElementById(id);
  const ctx = canvas.getContext('2d');
  canvas.width = canvas.parentElement.offsetWidth - 40;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!data || data.length === 0) return;
  const max = Math.max(...data.map(d => (d[1]||0) + (d[2]||0)), 1);
  const w = canvas.width / data.length;
  data.forEach((d, i) => {
    const success = (d[1]||0) / max * canvas.height * 0.8;
    const fail = (d[2]||0) / max * canvas.height * 0.8;
    ctx.fillStyle = '#22c55e';
    ctx.fillRect(i*w+2, canvas.height - success, w-4, success);
    ctx.fillStyle = '#ef4444';
    ctx.fillRect(i*w+2, canvas.height - success - fail, w-4, fail);
  });
}

function drawLineChart(id, values) {
  const canvas = document.getElementById(id);
  const ctx = canvas.getContext('2d');
  canvas.width = canvas.parentElement.offsetWidth - 40;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!values || values.length < 2) return;
  const max = Math.max(...values, 1);
  ctx.strokeStyle = '#38bdf8';
  ctx.lineWidth = 2;
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = i / (values.length - 1) * canvas.width;
    const y = canvas.height - (v / max * canvas.height * 0.9);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function fetchData() {
  google.script.run.withSuccessHandler(json => {
    try { renderMetrics(JSON.parse(json)); } catch(e) { console.error(e); }
  }).getMetrics();
}

// 自動更新
setInterval(() => {
  countdown--;
  document.getElementById('nextRefresh').textContent = '次回更新まで ' + countdown + '秒';
  if (countdown <= 0) { countdown = 60; fetchData(); }
}, 1000);

fetchData();
</script>
</body>
</html>`;
}
```

**Step 3: Commit**

```bash
git add gas/dashboard.gs
git commit -m "feat: GAS Dashboard - 4タブ監視ウェブアプリ"
```

---

## Task 9: EC2 デプロイ & 動作確認

**Step 1: ローカルから EC2 に monitoring/ と gas/ をデプロイ**

```bash
# deploy_ec2.sh を実行（既存のデプロイフローに乗る）
EC2_HOST=13.112.35.6 EC2_USER=ubuntu \
SSH_KEY="/Users/ibridgezhao/Documents/Super Scaner/SuperScaner.pem" \
bash scripts/deploy_ec2.sh
```

**Step 2: EC2 上で監視用依存関係をインストール & cron 設定**

```bash
# EC2 に SSH 接続
ssh -i SuperScaner.pem ubuntu@13.112.35.6

# アプリディレクトリに移動
cd /home/ubuntu/apps/super-scaner

# cron インストーラ実行
bash monitoring/install_cron.sh
```

期望出力：
```
=== インストール完了 ===
ログ確認: tail -f /home/ubuntu/metrics_pusher.log
```

**Step 3: 1分間待機してログを確認**

```bash
# EC2 上で実行
tail -f /home/ubuntu/metrics_pusher.log
```

期望：`[OK] HH:MM:SS 推送完了 | running | CPU:X% RAM:X% Disk:X%`

**Step 4: Google Sheets でデータ確認**

ブラウザで `SuperScaner-Monitor` 試算表を開き、`heartbeat` Sheet に行が追加されていることを確認。

**Step 5: GAS Dashboard 設定**

1. [script.google.com](https://script.google.com) にアクセス
2. 「新しいプロジェクト」 → `dashboard.gs` の内容を貼り付け
3. 「プロジェクトの設定」→「スクリプトプロパティ」→ `SPREADSHEET_ID` を追加
4. 「デプロイ」→「新しいデプロイ」→「ウェブアプリ」→ アクセス権「全員」
5. デプロイ URL を開いて Dashboard が表示されることを確認

**Step 6: 最終動作確認チェックリスト**

- [ ] `heartbeat` Sheet に 1 分ごと新行が追加される
- [ ] GAS Dashboard の「ステータス」タブに `● 稼働中` が表示される
- [ ] 「システム」タブに CPU/RAM/Disk 数値が表示される
- [ ] `scan-bot` コンテナを停止 → 1 分後 Dashboard が `✕ 停止` に変わる
- [ ] `scan-bot` を再起動 → 1 分後 `● 稼働中` に戻る

```bash
git add .
git commit -m "docs: deployment verification complete"
```

---

## ファイル構成まとめ

```
Super Scaner/
├── monitoring/
│   ├── __init__.py
│   ├── metrics_pusher.py      # メイン推送スクリプト
│   ├── system_metrics.py      # CPU/RAM/Disk
│   ├── docker_metrics.py      # Docker 状態・ログ
│   ├── log_parser.py          # ログ解析・統計
│   ├── sheets_writer.py       # Google Sheets 書き込み
│   ├── requirements.txt       # gspread, pytz
│   ├── install_cron.sh        # EC2 cron 設定
│   └── tests/
│       ├── __init__.py
│       ├── test_system_metrics.py
│       ├── test_docker_metrics.py
│       ├── test_log_parser.py
│       └── test_sheets_writer.py
├── gas/
│   └── dashboard.gs           # GAS Dashboard
└── docs/plans/
    ├── 2026-03-02-aws-monitoring-dashboard-design.md
    └── 2026-03-02-aws-monitoring-dashboard.md
```
