# 🧾 Super Scaner - 自動化會計憑證處理機器人

## 📖 項目簡介

基於 Python + Cloud Vision OCR + Gemini AI 的企業級自動化工具。全天候監聽 Google Drive，將員工上傳的 **發票、收據 (PDF/圖片)** 自動識別提取，結果直接寫入 **Google Sheets**，按員工和文書類型自動分 Tab，異常數據自動標色。

### 🌟 核心功能
* 📂 **多文件夾監聽：** 按文書類型分別監控（領収書/請求書/給与明細）
* 🔍 **雙引擎 OCR：** Cloud Vision 文字識別 + Gemini AI 結構化提取（自動回退）
* 📊 **Google Sheets 輸出：** 按員工分 Tab，異常單元格標色（紅/橙/黃）
* 🗺️ **科目自動映射：** AI 通用名 → MoneyForward 正確科目名
* 🔗 **原票 URL 追蹤：** 每行數據關聯原始 PDF 鏈接 + 頁碼
* 💾 **每日自動備份：** 22:00 JST 備份到獨立 Spreadsheet，90 天保留
* 🛡️ **異常檢測：** 日期空/取引先空/T番號不正/高額 → 對應單元格標色

---

## 🏗️ 系統架構

```
員工上傳 PDF → Drive 文件夾 (領収書/請求書/給与明細)
                    ↓ (3秒輪詢)
              Cloud Vision OCR → 純文字
                    ↓
              Gemini AI → 結構化 JSON
                    ↓
              科目映射 + 異常檢測
                    ↓
              Google Sheets 寫入 (按員工分Tab + 異常標色)
                    ↓
              原文件歸檔 + Chatwork 通知

              22:00 JST: 工作Sheet → 備份Sheet → 清空
```

---

## 🛠️ 本地開發環境搭建

### 1. 安裝依賴
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置 `.env`
```env
# Google Drive
FOLDER_RECEIPT_ID=領収書文件夾ID
FOLDER_PURCHASE_INVOICE_ID=請求書文件夾ID
FOLDER_SALARY_SLIP_ID=給与明細文件夾ID
PROCESSED_FOLDER_ID=歸檔文件夾ID
SERVICE_ACCOUNT_FILE=service_account.json

# Google Sheets
OUTPUT_SPREADSHEET_ID=工作用SpreadsheetID
BACKUP_SPREADSHEET_ID=備份用SpreadsheetID

# AI
GEMINI_API_KEY=你的AIza開頭的Key

# 通知 (可選)
CHATWORK_API_TOKEN=你的Token
CHATWORK_ROOM_ID=房間ID
```

### 3. 準備 Google 資源
1. 建立 `MF_Import_Data` 試算表 → 共享給 SA 為編輯者
2. 建立 `MF_Backup` 試算表 → 共享給 SA 為編輯者
3. 建立 3 個輸入文件夾（領収書/請求書/給与明細）→ 共享給 SA
4. (可選) 啟用 GCP Cloud Vision API + Billing

### 4. 運行
```bash
# 生產模式 (監聽 Drive)
python main.py

# 本地測試 (不需要 Drive)
python local_test.py
```

---

## 📂 項目結構

```text
Super Scaner/
├── main.py                     # 主程序：Drive 監聽 + Sheets 寫入
├── ocr_engine.py               # Cloud Vision OCR + Gemini AI 雙引擎
├── sheets_output.py            # Google Sheets 輸出（Tab管理/分割線/異常標色）
├── anomaly_detector.py         # 異常檢測模組
├── config.py                   # 配置管理 + 科目映射 (ACCOUNT_MAP)
├── doc_types.py                # 文書類型定義 + Tab 後綴映射
├── notifier.py                 # Chatwork 通知
├── csv_writer.py               # [廢止] 舊版 CSV 寫入器（參考保留）
├── local_test.py               # 本地測試腳本
│
├── scripts/
│   ├── deploy_ec2.sh           # AWS EC2 一鍵部署
│   ├── daily_backup.py         # 每日備份腳本 (22:00 JST cron)
│   └── install_daily_cron.sh   # Cron 安裝腳本
│
├── monitoring/                 # 監控子系統
│   ├── metrics_pusher.py       # 指標推送
│   ├── cleanup.py              # 數據清理
│   └── ...
│
├── Dockerfile                  # Docker 部署
├── requirements.txt            # Python 依賴
├── PRD.md                      # 產品需求文檔
└── README.md                   # 本文件
```

---

## ☁️ AWS EC2 部署

```bash
# 一鍵部署
EC2_HOST=13.112.35.6 EC2_USER=ubuntu \
SSH_KEY="SuperScaner.pem" \
bash scripts/deploy_ec2.sh
```

部署腳本會自動：上傳 secrets → 拉取代碼 → Docker build → 啟動容器 → 安裝備份 cron

---

## 📊 Google Sheets 輸出格式

### Tab 命名
- `池田尚也_領収書` — 池田的領収書數據
- `池田尚也_請求書` — 池田的請求書數據
- `_config` — 系統配置（取引No 管理）

### 欄位 (28列)
MF 標準 27 列 + 原票URL (第28列)

### 異常標色
| 異常 | 標色位置 | 顏色 |
|------|---------|------|
| 日期為空 | B列 | 🔴 紅色 |
| 取引先為空 | F列 | 🟠 橙色 |
| T番號不正 | H列 | 🟠 橙色 |
| 金額 > 10萬 | I列 | 🟡 黃色 |

---

## ⚠️ 常見問題

**Q: Cloud Vision OCR 報 BILLING_DISABLED？**
→ GCP 項目需啟用 Billing。不啟用也能用（自動回退到 Gemini Vision），但數字精度稍低。

**Q: Sheets API 報 429 Quota exceeded？**
→ 已通過緩存和重試機制優化。大量文件（26頁+）會自動限流處理。

**Q: Service Account 報 storageQuotaExceeded？**
→ SA 無法新建 Drive 文件。Spreadsheet 和文件夾需手動預建並共享給 SA。

---

*Generated for Project Super Scaner v2.0*
