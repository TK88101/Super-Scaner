# AWS 監控 Dashboard 設計文檔

> **版本:** 1.0
> **日期:** 2026-03-02
> **狀態:** 已批准

---

## 1. 目標

為 Super Scaner OCR 機器人（運行於 AWS EC2 13.112.35.6 的 Docker 容器 `scan-bot`）建立一個可視化監控 Dashboard，讓管理員隨時掌握服務運行狀況。

---

## 2. 架構方案

採用 **方案 A：獨立推送腳本 + Google Sheets + GAS Web App**。

### 數據流

```
EC2 cron (每1分鐘)
  └─ metrics_pusher.py
       ├─ docker inspect scan-bot  → 容器狀態、重啟次數
       ├─ docker logs scan-bot     → 最新日誌（tail 100）
       ├─ /proc/stat               → CPU 使用率
       ├─ /proc/meminfo            → RAM 使用率
       └─ df /                     → 磁碟使用率
            │
            ▼ 每分鐘推送
       Google Sheets（資料庫）
            │
            ▼ GAS 讀取
       GAS Web App（瀏覽器 Dashboard）
```

### 設計原則

- **監控與被監控解耦**：`metrics_pusher.py` 是獨立進程，即使 `scan-bot` 崩潰也能偵測並回報
- **零侵入**：不修改任何現有代碼（`main.py`、`ocr_engine.py` 等）
- **複用現有認證**：使用項目已有的 `service_account.json` 連接 Google API
- **推送頻率**：每 1 分鐘，最大延遲 60 秒

---

## 3. Google Sheets 結構

試算表名稱：`SuperScaner-Monitor`（需手動建立並授權 service account）

### Sheet1 - `heartbeat`

每分鐘追加一行，保留最近 1440 行（24 小時）。

| 欄位 | 類型 | 說明 |
|------|------|------|
| `timestamp` | datetime | 推送時間（JST） |
| `container_status` | string | `running` / `exited` / `not_found` |
| `restart_count` | int | Docker 重啟次數 |
| `cpu_pct` | float | CPU 使用率 (%) |
| `ram_pct` | float | RAM 使用率 (%) |
| `disk_pct` | float | 磁碟使用率 (%) |

### Sheet2 - `processing_stats`

每分鐘更新當日統計（upsert 當日行）。

| 欄位 | 類型 | 說明 |
|------|------|------|
| `date` | date | 統計日期 |
| `success_count` | int | 成功 OCR 處理數 |
| `fail_count` | int | 失敗數 |
| `total_amount_jpy` | int | 當日處理總金額（日圓） |

### Sheet3 - `logs`

滾動保留最新 500 行日誌。

| 欄位 | 類型 | 說明 |
|------|------|------|
| `timestamp` | datetime | 日誌時間 |
| `level` | string | `INFO` / `ERROR` / `WARNING` |
| `message` | string | 日誌內容 |

### Sheet4 - `config`

單行靜態配置，手動填寫。

| 欄位 | 說明 |
|------|------|
| `ec2_host` | `13.112.35.6` |
| `deploy_time` | 最後部署時間 |
| `version` | 應用版本 |

---

## 4. metrics_pusher.py 規格

### 依賴

```
gspread>=6.0.0
google-auth>=2.0.0
```

（`google-auth` 項目已有，只需新增 `gspread`）

### 執行流程

```python
1. 讀取 service_account.json 認證
2. 連接 Google Sheets（SpreadsheetID 從環境變量讀取）
3. 執行 `docker inspect scan-bot` → 解析 Status、RestartCount
4. 讀取 /proc/stat 兩次（間隔 0.1s）計算 CPU%
5. 讀取 /proc/meminfo 計算 RAM%
6. 執行 `df /` 計算磁碟%
7. 執行 `docker logs --tail 100 scan-bot` → 解析日誌行
8. 解析日誌中的成功/失敗/金額記錄 → 更新 Sheet2
9. 追加日誌到 Sheet3（去重、限制 500 行）
10. 追加心跳行到 Sheet1（限制 1440 行）
```

### 環境變量（新增到 .env）

```env
MONITOR_SPREADSHEET_ID=你的_Google_Sheets_ID
```

---

## 5. GAS Dashboard 規格

### 部署方式

Google Apps Script → 部署為 Web App（Anyone with link 可訪問）

### 4 個頁籤

#### 頁籤1：狀態（Status）

- 大字顯示容器狀態：**● 在線**（綠）/ **✕ 離線**（紅）
- 最後心跳時間（例：「2 分鐘前」）
- Docker 重啟次數（若 > 0 顯示警告）
- 自動判斷「失聯」：若最後心跳 > 5 分鐘，顯示警告

#### 頁籤2：統計（Stats）

- 今日成功/失敗 OCR 數量卡片
- 今日處理總金額（日圓）
- 最近 7 天處理量趨勢圖（HTML Canvas 折線圖）

#### 頁籤3：日誌（Logs）

- 最新 50 條日誌
- ERROR 行以紅色高亮
- WARNING 行以橙色高亮
- 支持關鍵字篩選

#### 頁籤4：系統（System）

- CPU / RAM / 磁碟使用率
- 以進度條方式顯示（> 80% 顯示紅色警告）
- 顯示最近 60 分鐘 CPU 使用率趨勢

### 刷新機制

- 頁面載入時立即讀取
- 每 60 秒自動刷新數據（不重載頁面）

---

## 6. 新增文件清單

```
Super Scaner/
├── monitoring/
│   ├── metrics_pusher.py     # 指標推送腳本（主體）
│   ├── requirements.txt      # gspread 依賴
│   └── install_cron.sh       # EC2 一鍵安裝 cron job
└── gas/
    └── dashboard.gs          # GAS Dashboard 完整代碼
```

---

## 7. 部署步驟概覽

1. **Google Sheets**：手動建立 `SuperScaner-Monitor` 試算表，建立 4 個 Sheet，授權 service account
2. **EC2**：上傳 `monitoring/` 目錄，執行 `install_cron.sh` 設定 cron
3. **GAS**：在 Google Apps Script 建立新專案，貼入 `dashboard.gs`，部署為 Web App
4. **驗證**：等待 1 分鐘後查看 Sheets 是否有數據，打開 GAS Web App 確認顯示正常

---

## 8. 不改動的現有文件

- `main.py`、`ocr_engine.py`、`csv_writer.py`、`config.py`、`doc_types.py`、`notifier.py`
- `Dockerfile`、`scripts/deploy_ec2.sh`
- `.env`（只追加新的環境變量）

---

*本文檔已獲用戶批准，可進入實施規劃階段。*
