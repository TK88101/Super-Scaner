# 📄 Super Scaner — 產品需求文檔 (PRD)

> **版本:** 2.0
> **日期:** 2026-03-19
> **狀態:** CSV→Google Sheets 重構完成，待客戶測試

---

## 1. 產品概述

### 1.1 產品定位

**Super Scaner** 是一款面向日本中小型企業（特別是會計事務所）的 **自動化會計憑證處理機器人**。它通過 Google Drive 文件監聽 + Cloud Vision OCR + Gemini AI 結構化提取，將員工上傳的發票、收據等財務單據自動轉換為符合 **MoneyForward（マネーフォワード）** 導入標準的會計數據，直接寫入 Google Sheets。

### 1.2 產品願景

消除會計人員手動錄入憑證的繁瑣流程，實現 **「拍照/掃描 → 上傳 → 自動入賬 → 異常標色審核」** 的全自動化閉環工作流。

### 1.3 目標用戶

| 角色 | 描述 |
|------|------|
| 🧑‍💼 **一般員工** | 日常將發票/收據拍照上傳至 Google Drive 對應文件夾 |
| 🧑‍💻 **會計擔當** | 在 Google Sheets 中審核數據（異常標色）→ 導出導入 MoneyForward |
| 🛡️ **系統管理員** | 部署和維護機器人、管理權限配置 |

---

## 2. 系統架構

### 2.1 技術棧

| 層級 | 技術選型 |
|------|---------|
| **語言** | Python 3.9+ |
| **OCR 引擎** | Google Cloud Vision API（文字識別）|
| **AI 引擎** | Google Gemini 2.0 Flash（結構化提取）|
| **雲端存儲** | Google Drive API v3 |
| **數據輸出** | Google Sheets API (gspread) |
| **通知系統** | Chatwork API v2 |
| **部署方式** | Docker 容器 (AWS EC2) |

### 2.2 模塊架構

```
┌─────────────────────────────────────────────────────────────┐
│                      Super Scaner v2.0                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌───────────┐    ┌──────────────┐    ┌────────────────┐   │
│  │  main.py   │───▶│ ocr_engine.py│───▶│sheets_output.py│   │
│  │  主控制器   │    │ OCR+AI 引擎  │    │ Sheets 寫入器   │   │
│  └─────┬─────┘    └──────────────┘    └────────────────┘   │
│        │                                                    │
│  ┌─────┴─────┐    ┌──────────────┐    ┌──────────────┐     │
│  │ config.py  │    │ doc_types.py │    │ notifier.py  │     │
│  │ 配置+科目   │    │ 文書類型定義  │    │  通知模組     │     │
│  │  映射管理   │    │              │    │              │     │
│  └───────────┘    └──────────────┘    └──────────────┘     │
│                                                             │
│  ┌───────────────────┐    ┌──────────────────────┐         │
│  │anomaly_detector.py│    │scripts/daily_backup.py│         │
│  │  異常検出模組       │    │  每日備份 (cron)      │         │
│  └───────────────────┘    └──────────────────────┘         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
         │              │                    │
         ▼              ▼                    ▼
   ┌───────────┐  ┌───────────┐      ┌───────────┐
   │Google Drive│  │  Google   │      │  Chatwork  │
   │  API v3   │  │  Sheets   │      │   API v2   │
   └───────────┘  └───────────┘      └───────────┘
```

### 2.3 文件清單

| 文件 | 職責 | 狀態 |
|------|------|------|
| `main.py` | 主控制循環、Drive 文件監聽、Sheets writer 初始化 | 重構 |
| `ocr_engine.py` | Cloud Vision OCR + Gemini Text 雙引擎、PDF 拆分 | 重構 |
| `sheets_output.py` | Google Sheets 寫入、Tab 管理、分割線、異常標色 | **新建** |
| `anomaly_detector.py` | 異常檢測（日期空/取引先空/T番號不正/高額）| **新建** |
| `config.py` | 員工映射、文件夾配置、科目映射表 (ACCOUNT_MAP) | 修改 |
| `doc_types.py` | 文書類型枚舉、DOC_TYPE_TAB_SUFFIX | 修改 |
| `notifier.py` | Chatwork 通知發送 | 不變 |
| `csv_writer.py` | MoneyForward CSV 生成（**已廢止**，參考保留）| 廢止 |
| `scripts/daily_backup.py` | 每日 22:00 JST 備份+清空工作 Sheet | **新建** |
| `scripts/install_daily_cron.sh` | EC2 cron 安裝腳本 | **新建** |

---

## 3. 核心數據流

```
員工上傳 PDF/圖片 → Google Drive (領収書/請求書/給与明細 文件夾)
    ↓
main.py 偵測到新文件 (3秒輪詢)
    ↓
下載 PDF → 拆分為單頁 (多頁PDF時)
    ↓
每一頁:
  ├→ Cloud Vision OCR → 純文字 (回退: Gemini Vision)
  ├→ Gemini AI (文字輸入) → 結構化 JSON
  ├→ 科目映射校驗 (ACCOUNT_MAP)
  └→ 異常檢測 → 標記可疑數據
    ↓
sheets_output 寫入 Google Sheets:
  ├→ Tab 名: "{員工名}_{文書類型}" (如: 池田尚也_領収書)
  ├→ 如已有數據 → 畫分割線
  ├→ 寫入數據行 (28列 = MF 27列 + 原票URL)
  └→ 異常單元格標色 (紅/橙/黃)
    ↓
原文件移到 Processed 文件夾 → Chatwork 通知

===== 22:00 JST (cron) =====
daily_backup.py:
  1. 讀取工作 Sheet 各 tab 數據
  2. 寫入備份 Sheet 的新 tab (命名: 日期_原tab名)
  3. 清空工作 Sheet 各 tab (保留表頭)
  4. 刪除超過 90 天的舊備份 tab
```

---

## 4. 功能需求

### 4.1 核心功能 (P0)

#### F1: Google Drive 多文件夾監聽

- 支持按文書類型分文件夾監控
- 環境變量: `FOLDER_RECEIPT_ID`, `FOLDER_PURCHASE_INVOICE_ID`, `FOLDER_SALARY_SLIP_ID`
- 向後兼容單一 `INPUT_FOLDER_ID` 模式

#### F2: 雙引擎 OCR (Cloud Vision + Gemini)

- **主引擎:** Cloud Vision API → 純文字 OCR → Gemini Text 結構化提取
- **回退引擎:** Gemini Vision 直接識別（Cloud Vision 不可用時自動回退）
- 優勢: OCR 和結構化提取分離，數字精度更高

#### F3: Google Sheets 輸出

- 替代舊的 CSV 文件方式，直接寫入 Google Sheets
- 按員工+文書類型自動分 Tab
- 同一員工再次上傳 → 分割線 → 追加新數據
- 第 28 列: 原票URL（原始 PDF 的 Drive 鏈接 + #page=N 頁碼定位）

#### F4: 科目映射 (ACCOUNT_MAP)

AI 輸出的通用科目名自動轉換為 MF 正確名稱:

| AI 輸出 | MF 正確名稱 |
|---------|-----------|
| 消耗品費 | 備品・消耗品費 |
| 雑費 | 市場調查費 (待確認) |
| 現金 | 未払金 |

#### F5: 異常檢測 + 單元格標色

| 異常類型 | 標色位置 | 顏色 |
|---------|---------|------|
| 日期為空 | B列 (取引日) | 紅色 |
| 取引先為空 | F列 (借方取引先) | 橙色 |
| T番號不正 | H列 (借方インボイス) | 橙色 |
| 金額 > 10萬 | I列 (借方金額) | 黃色 |

#### F6: 每日備份

- 時間: 22:00 JST (cron)
- 工作 Sheet → 備份 Sheet (日期+tab名)
- 清空工作 Sheet (保留表頭)
- 備份保留: 90 天自動清理

### 4.2 輔助功能 (P1)

- F7: MD5 重複文件檢測
- F8: Chatwork 通知
- F9: 員工身份識別 (Google email → 姓名)

---

## 5. 環境變量配置

| 變量名 | 必填 | 說明 |
|-------|------|------|
| `GEMINI_API_KEY` | ✅ | Google AI API 密鑰 |
| `SERVICE_ACCOUNT_FILE` | ✅ | 服務賬號 JSON 文件路徑 |
| `PROCESSED_FOLDER_ID` | ✅ | 歸檔文件夾 ID |
| `OUTPUT_SPREADSHEET_ID` | ✅ | 工作用 Google Spreadsheet ID |
| `BACKUP_SPREADSHEET_ID` | ✅ | 備份用 Google Spreadsheet ID |
| `FOLDER_RECEIPT_ID` | ⚡ | 領収書文件夾 ID |
| `FOLDER_PURCHASE_INVOICE_ID` | ⚡ | 請求書文件夾 ID |
| `FOLDER_SALARY_SLIP_ID` | ⚡ | 給与明細文件夾 ID |
| `INPUT_FOLDER_ID` | ⚡ | 舊版單文件夾模式（向後兼容）|
| `SPLIT_PDF_FOLDER_ID` | ❌ | 拆分頁上傳 (SA配額限制暫不可用) |
| `CHATWORK_API_TOKEN` | ❌ | Chatwork 通知 Token |
| `CHATWORK_ROOM_ID` | ❌ | Chatwork 房間 ID |

> ⚡ = 至少配置一個文件夾 ID

---

## 6. 已知限制

| 項目 | 說明 |
|------|------|
| SA 存儲配額 | Service Account 無法新建 Drive 文件，Spreadsheet 需手動預建 |
| Cloud Vision | 需要 GCP Billing（每月 1000 次免費），不啟用則回退到 Gemini Vision |
| Sheets API 限流 | 60 次/分鐘，已通過緩存+重試優化 |
| PDF 拆分頁上傳 | SA 配額限制暫不可用，改用原始 PDF URL + #page=N |
| 備選 OCR | 如不啟用 GCP Billing，可考慮 Umi-OCR (PaddleOCR) 離線方案 |

---

## 7. 版本歷程

| 版本 | 功能 | 狀態 |
|------|------|------|
| v1.0 Phase 1-6 | 基礎架構、多文書類型、CSV 輸出 | ✅ 完成 |
| v1.0 Phase 7 | 生產監控儀表板 | ✅ 完成 |
| v1.0 Phase 8 | T番號校驗、API 重試 | ✅ 完成 |
| **v2.0 Phase 1** | **sheets_output.py + anomaly_detector.py + 科目映射** | ✅ 完成 |
| **v2.0 Phase 2** | **Cloud Vision OCR + Gemini Text 雙引擎** | ✅ 完成 |
| **v2.0 Phase 3** | **PDF 拆分 + 原票URL 追蹤** | ✅ 完成 |
| **v2.0 Phase 4** | **main.py CSV→Sheets 完全替換** | ✅ 完成 |
| **v2.0 Phase 5** | **每日備份 + 部署更新** | ✅ 完成 |

---

*本文檔反映 Super Scaner v2.0 重構狀態 (feature/restructure-sheets-ocr 分支)，截至 2026-03-19。*
