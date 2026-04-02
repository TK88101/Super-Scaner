# 🧾 Super Scaner - 自動化會計憑證處理機器人

## 📖 項目簡介

基於 Python + PaddleOCR (ローカル) + Gemini AI 的企業級自動化工具。全天候監聽 Google Drive，將員工上傳的 **發票、收據 (PDF/圖片)** 自動識別提取，結果直接寫入 **Google Sheets**，按員工和文書類型自動分 Tab，異常數據自動標色。

### 🌟 核心功能
* 📂 **多文件夾監聽：** 按文書類型分別監控（領収書/請求書/給与明細）
* 🔍 **雙引擎 OCR：** PaddleOCR (ローカル、無料) + Gemini AI（Strategy C、自動回退）
* 🧠 **OCR 主導抽出：** 日期/T番號由 PaddleOCR 正規表現提取（Gemini 依存度降低），科目分類のみ Gemini
* 📊 **Generator Pipeline：** 逐頁 OCR → 即時 Sheets 寫入 → GC → 下一頁（低メモリ動作）
* 🗺️ **科目自動映射 + 補助科目：** AI 通用名 → MF 正確科目名、適格/非適格/飲食贈答等 自動決定
* 🔗 **原票 URL 追蹤：** 每行數據關聯原始 PDF 鏈接 + 頁碼
* 💾 **每日自動備份 (GAS)：** 22:00 JST Google サーバー上で自動実行、全 tab 集約備份、30 天保留、PC 電源不要
* 🛡️ **異常檢測 + ハイライト凡例：** 日期空/取引先空/T番號空・不正/高額 → 對應單元格標色 + 凡例説明

---

## 🏗️ 系統架構

```
PDF 上傳 → Drive 文件夾 (領収書/請求書/給与明細)
         ↓ (3秒輪詢)
    PDF 逐頁分割 (generator yield)
         ↓
    PaddleOCR → テキスト抽出
         ↓
    正規表現で日付・T番號抽出 (OCR主導)
         ↓
    Gemini AI → 科目分類・金額・vendor 構造化
         ↓
    OCR日付/T番號で Gemini 結果を上書き
         ↓
    科目映射 + 補助科目自動決定 + 異常檢測
         ↓
    即時 Google Sheets 寫入 → GC → 次頁
         ↓
    原文件歸檔 + Chatwork 通知

    22:00 JST (GAS): 全tab → MF_Backup 集約備份 → 元tab削除
```

---

## 🖥️ Windows 客戶端部署（本番環境）

### 一鍵部署
1. 將 `SuperScaner Deploy/` 資料夾交給客戶
2. 雙擊 `SuperScaner_Setup.bat` → 自動安裝 Git、Python、PaddleOCR v3、所有依賴
3. 雙擊 `SuperScaner_AutoStart.bat` → 註冊開機自啟動
4. 完成。開機後自動後台運行，監控 Google Drive

### 部署文件
| 文件 | 用途 |
|------|------|
| `SuperScaner_Setup.bat` + `.ps1` | 環境安裝（一次性） |
| `SuperScaner_AutoStart.bat` + `.ps1` | 註冊開機自啟（一次性） |

### 系統需求
- Windows 10/11 x64
- 網路連線（Google Drive / Gemini API）
- 其餘由 Setup 腳本自動安裝

---

## 🛠️ 本地開發環境搭建（Mac）

### 1. 安裝依賴

> **注意:** PaddlePaddle 需要 Python 3.11，請使用 `venv311` 環境。

```bash
# macOS: poppler 為 pdf2image 必要依賴
brew install poppler

# 建立 Python 3.11 虛擬環境
python3.11 -m venv venv311
source venv311/bin/activate
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
python local_test.py                  # デフォルト Strategy C
python local_test.py --strategy A     # PaddleOCR → Gemini Text のみ
python local_test.py --strategy B     # 信頼度ゲート分岐
python local_test.py --strategy C     # デュアル入力 (推奨)

# OCR ベンチマーク
python benchmark_ocr.py               # 全 Strategy 比較テスト
```

---

## 📂 項目結構

```text
Super Scaner/
├── main.py                     # 主程序：Drive 監聽 + Generator消費 + 逐次Sheets寫入
├── ocr_engine.py               # PaddleOCR + Gemini 雙引擎, Generator Pipeline, OCR主導日付/T番号抽出
├── sheets_output.py            # Google Sheets 輸出（ハイライト凡例/補助科目自動/異常標色）
├── anomaly_detector.py         # 異常檢測模組（T番号空/不正/高額/要確認科目）
├── config.py                   # 配置管理 + 科目映射 (ACCOUNT_MAP)
├── doc_types.py                # 文書類型定義 + Tab 後綴映射
├── notifier.py                 # Chatwork 通知
├── csv_writer.py               # [廢止] 舊版 CSV 寫入器（參考保留）
├── local_test.py               # 本地測試腳本 (--strategy A/B/C)
├── benchmark_ocr.py            # OCR ベンチマーク (Strategy 比較)
│
├── gas/
│   ├── dashboard.gs            # 監控儀表板 (GAS Web App)
│   └── daily_backup.gs         # 每日備份 (GAS, 22:00 JST, Google サーバー実行)
│
├── scripts/
│   ├── deploy_ec2.sh           # AWS EC2 一鍵部署 (已棄用)
│   ├── daily_backup.py         # 每日備份 (Python cron 版, 已被 GAS 版置換)
│   └── install_daily_cron.sh   # Cron 安裝腳本 (已棄用)
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

## ☁️ AWS EC2 部署（已棄用）

> EC2 t2.micro 只能運行 PaddleOCR v2.x，OCR 精度顯著降低。已改為 Windows 本機部署方案。

```bash
# 舊版部署命令（僅供參考）
EC2_HOST=<IP> EC2_USER=ubuntu SSH_KEY="SuperScaner.pem" bash scripts/deploy_ec2.sh
```

---

## 📊 Google Sheets 輸出格式

### Tab 命名
- `池田尚也_領収書` — 池田的領収書數據
- `池田尚也_請求書` — 池田的請求書數據
- `_config` — 系統配置（取引No 管理）

### 欄位 (28列)
MF 標準 27 列 + 原票URL (第28列)

### ハイライト凡例 (A1-A3 に自動表示)
| 異常 | 標色位置 | 顏色 |
|------|---------|------|
| 日期為空 | B列 | 🔴 紅色 |
| 取引先為空 | F列 | 🟠 橙色 |
| T番號不正 | H列 | 🟠 橙色 |
| T番號為空 | H列 | 🟡 黃色 |
| 地代家賃/保険料/雑収入 (全件) | I列 | 🟡 黃色 |
| 修繕費 > 30萬 | I列 | 🟡 黃色 |
| 備品・消耗品費 > 10萬 | I列 | 🟡 黃色 |
| 租税公課 (宿泊税/軽油税) | I列 | 🟡 黃色 |

---

## ⚠️ 常見問題

**Q: Cloud Vision OCR 報 BILLING_DISABLED？**
→ v2.1 で PaddleOCR に置換済み（Cloud Vision コードはコメントアウトで保持）。GCP Billing 不要。

**Q: Sheets API 報 429 Quota exceeded？**
→ 已通過緩存和重試機制優化。大量文件（26頁+）會自動限流處理。

**Q: Service Account 報 storageQuotaExceeded？**
→ SA 無法新建 Drive 文件。Spreadsheet 和文件夾需手動預建並共享給 SA。

---

*Generated for Project Super Scaner v2.6 — 客戶本番稼働 + GAS 日次バックアップ*
