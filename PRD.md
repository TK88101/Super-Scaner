# 📄 Super Scaner — 產品需求文檔 (PRD)

> **版本:** 1.0  
> **日期:** 2026-02-23  
> **狀態:** 基於現有代碼庫整理  

---

## 1. 產品概述

### 1.1 產品定位

**Super Scaner** 是一款面向日本中小型企業（特別是會計事務所）的 **自動化會計憑證處理機器人**。它通過 Google Drive 文件監聽 + Gemini AI 視覺識別，將員工上傳的發票、收據等財務單據自動轉換為符合 **MoneyForward（マネーフォワード）** 導入標準的 CSV 會計數據。

### 1.2 產品願景

消除會計人員手動錄入憑證的繁瑣流程，實現 **「拍照/掃描 → 上傳 → 自動入賬」** 的全自動化閉環工作流，提升 10 倍以上的會計處理效率。

### 1.3 目標用戶

| 角色 | 描述 |
|------|------|
| 🧑‍💼 **一般員工** | 日常將發票/收據拍照上傳至 Google Drive |
| 🧑‍💻 **會計擔當** | 審核 CSV 報表並導入 MoneyForward |
| 🛡️ **系統管理員** | 部署和維護機器人、管理權限配置 |

---

## 2. 系統架構

### 2.1 技術棧

| 層級 | 技術選型 |
|------|---------|
| **語言** | Python 3.9+ |
| **AI 引擎** | Google Gemini 2.0 Flash（視覺 OCR） |
| **雲端存儲** | Google Drive API v3 |
| **通知系統** | Chatwork API v2 |
| **輸出格式** | MoneyForward 標準 CSV（27 列） |
| **部署方式** | Docker 容器 (AWS EC2 / GCP Compute Engine) |
| **配置管理** | `.env` 環境變量 + `service_account.json` |

### 2.2 模塊架構

```
┌─────────────────────────────────────────────────────────────┐
│                      Super Scaner                           │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌───────────┐    ┌──────────────┐    ┌──────────────┐     │
│  │  main.py   │───▶│ ocr_engine.py│───▶│csv_writer.py │     │
│  │  主控制器   │    │  AI 識別引擎  │    │  CSV 寫入器   │     │
│  └─────┬─────┘    └──────────────┘    └──────────────┘     │
│        │                                                    │
│  ┌─────┴─────┐    ┌──────────────┐    ┌──────────────┐     │
│  │ config.py  │    │ doc_types.py │    │ notifier.py  │     │
│  │  配置管理   │    │  文書類型定義  │    │  通知模組     │     │
│  └───────────┘    └──────────────┘    └──────────────┘     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
         │                                      │
         ▼                                      ▼
   ┌───────────┐                         ┌───────────┐
   │Google Drive│                         │  Chatwork  │
   │  API v3   │                         │   API v2   │
   └───────────┘                         └───────────┘
```

### 2.3 文件清單

| 文件 | 職責 | 代碼行數 |
|------|------|---------|
| `main.py` | 主控制循環、Drive 文件監聽與同步、流程編排 | ~341 |
| `ocr_engine.py` | Gemini AI 調用、Prompt 管理、仕訳（會計分錄）生成 | ~616 |
| `csv_writer.py` | MoneyForward 格式 CSV 生成、T番號校驗、追加寫入 | ~170 |
| `config.py` | 員工名單映射、文件夾映射、系統參數 | ~66 |
| `doc_types.py` | 文書類型枚舉、會計科目默認映射 | ~58 |
| `notifier.py` | Chatwork 通知發送 | ~58 |
| `Dockerfile` | Docker 容器化部署配置 | ~19 |

---

## 3. 功能需求

### 3.1 核心功能 (P0)

#### F1: Google Drive 文件監聽

- **描述：** 系統以輪詢方式（默認 3 秒間隔）持續監控一個或多個 Google Drive 文件夾中的新文件。
- **實現細節：**
  - 支持多文件夾監控，每個文件夾對應不同的文書類型 (`DocType`)
  - 向後兼容單一 `INPUT_FOLDER_ID` 模式（默認為領收書）
  - 環境變量配置：`FOLDER_RECEIPT_ID`, `FOLDER_PURCHASE_INVOICE_ID`, `FOLDER_SALES_INVOICE_ID`, `FOLDER_SALARY_SLIP_ID`
  
| 參數 | 值 | 說明 |
|------|---|------|
| 掃描間隔 | 3 秒 | `config.SCAN_INTERVAL` |
| 支持格式 | `.jpg`, `.jpeg`, `.png`, `.heic`, `.pdf` | `config.SUPPORTED_EXTENSIONS` |
| 不支持格式 | Word, Excel 等 | 自動跳過 |

#### F2: AI 視覺識別 (OCR)

- **描述：** 使用 Google Gemini 2.0 Flash 模型對上傳的圖片/PDF 進行視覺分析，提取結構化財務數據。
- **AI 模型：** `gemini-2.0-flash`
- **核心能力：**
  - 日期識別（含印章日期解析，如 `R7.9.16` → `2025/09/16`）
  - 供應商/取引先名稱提取
  - 適格請求書番號 (`T` 開頭 13 位) 識別
  - 品目明細拆分與金額提取
  - 消費稅率自動判定（8% 輕減稅率 / 10% 標準稅率）
  - 會計科目智能推定（消耗品費、旅費交通費、交際費等）
  - 支付方式判定（現金、信用卡、PayPay、ATM、振込等）

#### F3: 多文書類型支持

系統支持 **4 種文書類型**，每種擁有獨立的 AI Prompt 和會計分錄生成邏輯：

| 文書類型 | DocType 常量 | 默認借方科目 | 默認貸方科目 |
|---------|-------------|------------|------------|
| 📄 **領收書 (Receipt)** | `receipt` | 消耗品費 | 現金 |
| 📥 **支払請求書 (Purchase Invoice)** | `purchase_invoice` | 仕入高 | 買掛金 |
| 📤 **売上請求書 (Sales Invoice)** | `sales_invoice` | 売掛金 | 売上高 |
| 💰 **賃金台帳 (Salary Slip)** | `salary_slip` | 給料手当 | 普通預金 |

##### 領收書特殊功能

- **多文書檢測：** 單張圖片包含多張收據時，AI 會分別識別並返回 `documents` 數組
- **文書分類：** 自動區分普通收據 (`receipt`)、銀行振込 (`bank_transfer`)、手數料收據 (`fee_receipt`)
- **混合稅率拆分：** 自動區分 8% (食品/輕減) 和 10% (標準) 稅率
- **數學驗證 (`verify_tax_math`)：** 通過稅額反算驗證金額準確性

##### 給與明細特殊功能

- **多行分錄生成：** 自動拆分為差引支給額、社會保險料、源泉所得稅、住民稅等多行會計分錄
- 貸方科目自動分配到「預り金」子科目（社會保険料/源泉所得稅/住民稅）

#### F4: MoneyForward CSV 生成

- **描述：** 將 AI 識別結果轉換為 MoneyForward 標準導入格式的 CSV 文件。
- **T番號（適格請求書番號）校驗：** 寫入 CSV 前經 `_sanitize_invoice_num()` 驗證，確保符合 MF 導入要求（T+13位數字）。自動去除連字符、修正大小寫，排除領收書編號誤識別等異常值。
- **CSV 規格：** 27 列標準格式

| 主要欄位 | 說明 |
|---------|------|
| 取引No | 自動遞增編號 |
| 取引日 | 交易日期 |
| 借方勘定科目 / 貸方勘定科目 | 會計科目 |
| 借方稅區分 / 貸方稅區分 | 稅務分類 |
| 借方インボイス | 適格請求書番號 |
| 借方金額 / 貸方金額 | 金額（日圓） |
| 摘要 | 格式：`{供應商} - {品目} [担当: {上傳者}]` |
| 作成者 / 最終更新者 | 上傳員工姓名 |

- **特性：**
  - 自動從 Drive 同步最新 CSV → 追加寫入 → 回傳 Drive
  - UTF-8 BOM 編碼（`utf-8-sig`），確保 Excel 正確顯示日語
  - 取引No 自動遞增（讀取現有 CSV 最大編號 +1）
  - 向後兼容舊 `split_items` 格式

#### F5: 雲端 CSV 雙向同步

- **描述：** 每次處理前從 Drive 下載最新 CSV，處理後回傳更新版本。
- **流程：**
  1. `ensure_latest_csv_from_drive()` — 下載最新 CSV 到本地
  2. 自動補全 CSV 末尾換行符（防止行合併問題）
  3. `append_to_csv()` — 追加新行
  4. `sync_csv_to_drive()` — 上傳更新後的 CSV

#### F6: 文件歸檔

- **描述：** 處理完成的原始文件自動從 Input 文件夾移動到 Processed 文件夾。
- **異常處理：** 移動失敗時輸出警告但不中斷主流程。

### 3.2 輔助功能 (P1)

#### F7: 重複文件檢測

- **描述：** 通過 MD5 校驗碼 (`md5Checksum`) 比對 Processed 文件夾中的已處理文件，防止同一文件被重複處理。
- **限制：** 最多比對最近 200 個已處理文件。

#### F8: Chatwork 通知

- **描述：** 處理結果通過 Chatwork API 發送通知，支持 `@提及` 具體員工。
- **通知內容：**
  - 成功：文書類型、取引先、合計金額、文書數
  - 失敗：錯誤提示
- **安全措施：** Token 清洗（去除空格、非 ASCII 字符檢測）

#### F9: 員工身份識別

- **描述：** 根據 Google Drive 上傳者的 email 地址自動匹配真實姓名。
- **配置：** `config.EMPLOYEE_MAP` 字典映射
- **數據：** email → `{name, chat_id}`

---

## 4. 數據流

```
                        ┌─────────────┐
                        │  員工上傳     │
                        │ 圖片/PDF     │
                        └──────┬──────┘
                               │
                               ▼
                     ┌─────────────────┐
                     │  Google Drive    │
                     │  Input 文件夾    │
                     └────────┬────────┘
                              │ (輪詢監聽, 3秒)
                              ▼
                     ┌─────────────────┐
                     │ ① 格式過濾      │ → 非圖片/PDF → 跳過
                     │ ② 重複檢測      │ → MD5 重複 → 歸檔
                     │ ③ 上傳者識別    │
                     └────────┬────────┘
                              │
                              ▼
                     ┌─────────────────┐
                     │  ④ 下載到本地    │
                     │  temp_downloads/ │
                     └────────┬────────┘
                              │
                              ▼
                     ┌─────────────────┐
                     │  ⑤ Gemini AI    │
                     │  視覺 OCR 識別   │
                     │ (按 DocType      │
                     │  選擇 Prompt)    │
                     └────────┬────────┘
                              │
                              ▼
                     ┌─────────────────┐
                     │  ⑥ 會計分錄生成  │
                     │  Entry Builder   │
                     └────────┬────────┘
                              │
                    ┌─────────┼─────────┐
                    ▼         ▼         ▼
            ┌───────────┐ ┌──────┐ ┌────────┐
            │ CSV 寫入   │ │ 歸檔  │ │ 通知    │
            │ MF格式     │ │ 移動  │ │Chatwork│
            └─────┬─────┘ └──────┘ └────────┘
                  │
                  ▼
            ┌───────────┐
            │ Drive 同步 │
            │ CSV_Exports│
            └───────────┘
```

---

## 5. 稅務處理規則

### 5.1 消費稅區分邏輯

| 場景 | 借方稅區分 | 貸方稅區分 |
|------|----------|----------|
| 食品/飲料 (酒類除外) | `課対仕入8% (軽)` | `対象外` |
| 標準課稅品目 | `課対仕入10%` | `対象外` |
| 銀行振込本體 | `対象外` | `対象外` |
| 売上 (食品) | `対象外` | `課税売上8% (軽)` |
| 売上 (標準) | `対象外` | `課税売上10%` |
| 給與 | `対象外` | `対象外` |

### 5.2 貸方科目決定邏輯

| 支付方式 | 貸方科目 |
|---------|---------|
| 現金 | 現金 |
| 信用卡 (VISA/Master/Credit) | 未払金 |
| PayPay | 未払金 |
| 振込 / ATM | 普通預金 |
| 銀行振込受取書 / 手數料 | 普通預金 |

---

## 6. 部署架構

### 6.1 部署方式

| 方式 | 推薦度 | 說明 |
|------|-------|------|
| 🐳 **Docker on AWS EC2** | ⭐⭐⭐ | 一鍵部署腳本 `scripts/deploy_ec2.sh` |
| 🐳 **Docker on GCP CE** | ⭐⭐⭐ | 推薦 e2-micro, Ubuntu 22.04 |
| 🖥️ **本地運行** | ⭐⭐ | 開發測試用 `python main.py` |

### 6.2 Docker 配置

- **基礎鏡像：** `python:3.9-slim`
- **啟動命令：** `python main.py`
- **重啟策略：** `--restart always`（24/7 在線）
- **環境變量注入：** `--env-file` 掛載 `.env`
- **機密文件：** `service_account.json` 以 read-only volume 掛載

### 6.3 機密文件管理

| 文件 | 用途 | Git 追蹤 |
|------|------|---------|
| `.env` | API Key、文件夾 ID 等環境變量 | ❌ 已忽略 |
| `service_account.json` | Google Drive 服務賬號密鑰 | ❌ 已忽略 |
| `SuperScaner.pem` | AWS SSH 連線密鑰（僅本地使用） | ❌ 已忽略 |

---

## 7. 外部依賴

### 7.1 Google Cloud 服務

| 服務 | 用途 |
|------|------|
| **Google Drive API v3** | 文件監聽、下載、移動、CSV 同步 |
| **Google AI (Gemini API)** | 視覺 OCR 識別 |
| **Service Account** | Drive API 無人值守認證 |

### 7.2 第三方服務

| 服務 | 用途 |
|------|------|
| **Chatwork API v2** | 處理結果通知 |
| **MoneyForward** | CSV 導入目標系統 (間接整合) |

### 7.3 Python 核心依賴

| 套件 | 版本 | 用途 |
|------|------|------|
| `google-generativeai` | 0.8.5 | Gemini AI SDK |
| `google-api-python-client` | 2.187.0 | Google Drive API |
| `google-auth` | 2.41.1 | 服務賬號認證 |
| `python-dotenv` | 1.2.1 | 環境變量管理 |
| `requests` | 2.32.5 | Chatwork API 通信 |
| `pydantic` | 2.12.4 | 數據驗證 |

---

## 8. 環境變量配置

| 變量名 | 必填 | 說明 |
|-------|------|------|
| `GEMINI_API_KEY` | ✅ | Google AI API 密鑰 |
| `SERVICE_ACCOUNT_FILE` | ✅ | 服務賬號 JSON 文件路徑 |
| `PROCESSED_FOLDER_ID` | ✅ | 歸檔文件夾 ID |
| `CSV_FOLDER_ID` | ✅ | CSV 輸出文件夾 ID |
| `INPUT_FOLDER_ID` | ⚡ | 舊版單文件夾模式（向後兼容） |
| `FOLDER_RECEIPT_ID` | ⚡ | 領收書文件夾 ID |
| `FOLDER_PURCHASE_INVOICE_ID` | ⚡ | 支払請求書文件夾 ID |
| `FOLDER_SALES_INVOICE_ID` | ⚡ | 売上請求書文件夾 ID |
| `FOLDER_SALARY_SLIP_ID` | ⚡ | 賃金台帳文件夾 ID |
| `CHATWORK_API_TOKEN` | ❌ | Chatwork 通知 Token |
| `CHATWORK_ROOM_ID` | ❌ | Chatwork 房間 ID |

> ⚡ = 至少配置一個文件夾 ID

---

## 9. 已知限制與約束

| 項目 | 說明 |
|------|------|
| **文件格式** | 僅支持 JPG/JPEG/PNG/HEIC/PDF，不支持 Word/Excel |
| **Drive 容量** | Service Account 無法新建文件（免費版限制），需手動預建 CSV |
| **重複檢測範圍** | 僅比對 Processed 文件夾最近 200 個文件 |
| **並發處理** | 單線程順序處理，不支持並行 OCR |
| **錯誤恢復** | 處理失敗的文件留在 Input 文件夾，不自動重試 |
| **AI 準確性** | 依賴 Gemini 2.0 Flash 模型，低解析度圖片可能影響精度。T番號有校驗機制防止誤識別 |
| **幣種** | 僅支持日圓 (¥) |
| **語言** | AI Prompt 為日語，面向日本會計制度 |

---

## 10. 版本歷程

| 階段 | 功能 | 狀態 |
|------|------|------|
| **Phase 1** | 基礎設施 — DocType 枚舉、會計科目映射、文件夾映射 | ✅ 完成 |
| **Phase 2** | 核心引擎 — CSV 寫入器支持 entries 格式、取引No 遞增 | ✅ 完成 |
| **Phase 3** | OCR 路由 — process_pipeline 支持多文書類型分發 | ✅ 完成 |
| **Phase 4** | 領收書增強 — 多文書檢測、新 entries 格式、科目推定 | ✅ 完成 |
| **Phase 5** | 新文書類型 — 支払/売上請求書、賃金台帳完整支持 | ✅ 完成 |
| **Phase 6** | 系統整合 — 多文件夾監控、端到端測試通過 | ✅ 完成 |
| **Phase 7** | 生產監控 — AWS EC2 監控儀表板、Google Sheets 自動推送 | ✅ 完成 |
| **Phase 8** | 數據品質 — T番號校驗、Google API 500 重試、Prompt 優化 | ✅ 完成 |

---

## 11. 監控架構 (Phase 7)

### 11.1 監控系統架構

```
EC2 Server (cron, 每分鐘)
│
├── monitoring/metrics_pusher.py       # 主控腳本
│   ├── system_metrics.py              # CPU/RAM/Disk 採集
│   ├── docker_metrics.py             # Docker 容器狀態/日誌
│   ├── log_parser.py                 # OCR 處理統計解析
│   └── sheets_writer.py              # Google Sheets 寫入
│
└── monitoring/cleanup.py             # 每小時清理舊數據 (cron)
         │
         ▼
   Google Sheets Dashboard
   ├── Heartbeat (24h 滾動窗口, 1440 行上限)
   ├── Logs     (500 行上限)
   ├── Stats    (90 天保留)
   └── Summary
```

### 11.2 監控指標清單

| 指標 | 來源 | 更新頻率 |
|------|------|---------|
| CPU 使用率 (%) | `psutil` | 每分鐘 |
| RAM 使用率 (%) | `psutil` | 每分鐘 |
| Disk 使用率 (%) | `psutil` | 每分鐘 |
| Docker 容器狀態 | `docker inspect` | 每分鐘 |
| OCR 處理成功數 | 日誌解析 | 每分鐘 |
| OCR 處理失敗數 | 日誌解析 | 每分鐘 |

### 11.3 監控相關環境變量

| 變量名 | 必填 | 說明 |
|-------|------|------|
| `MONITOR_SPREADSHEET_ID` | ✅ | 監控 Google Sheets ID |
| `SERVICE_ACCOUNT_FILE` | ✅ | 服務賬號 JSON (與主系統共用) |

### 11.4 新增 Python 依賴

| 套件 | 版本 | 用途 |
|------|------|------|
| `pypdf` | 4.3.1 | PDF 頁數讀取、大型 PDF 拆分預處理 |
| `psutil` | — | 系統指標採集 (CPU/RAM/Disk) |
| `gspread` | — | Google Sheets API 高階封裝 |
| `pytz` | — | JST 時區轉換 |

---

*本文檔基於 Super Scaner 現有代碼庫自動生成，反映截至 2026-03-11 的系統狀態。*
