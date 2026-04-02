# 📄 Super Scaner — 產品需求文檔 (PRD)

> **版本:** 2.6
> **日期:** 2026-04-02
> **狀態:** 客戶本番稼働中、GAS備份自動化追加、feature/windows-deploy → main マージ完了
>
> ### v2.6 変更点 (2026-04-02)
> - **客戶本番稼働**: mini PC にて2名の従業員が利用開始
> - **feature/windows-deploy → main マージ完了**: Deploy スクリプトの $BRANCH を "main" に変更
> - **GitHub Release v1.0.0**: Deploy 4ファイルを Release Assets として公開
> - **GAS 日次バックアップ**: `gas/daily_backup.gs` — Google サーバー上で毎日 22:00 JST 自動実行
>   - MF_Import_Data の全データ tab → MF_Backup に日付シートとして一括集約（tab名+太線で区切り）
>   - バックアップ完了後、元の tab を削除（main.py が次回自動再作成）
>   - 月次クリーンアップ: 30日超の古いバックアップを自動削除
>   - Python cron 版 (scripts/daily_backup.py) を置換（PC 電源状態に依存しない）
>
> ### v2.5 変更点 (2026-03-29)
> - **Windows本番デプロイ**: Setup.bat + AutoStart.bat 一鍵部署スクリプト（中/日/英 3言語対応）
> - **封筒ページ強制フィルタ**: _is_envelope_page() コードレベル検出（Gemini prompt依存を排除）
> - **取引先科目兜底ルール**: _VENDOR_ACCOUNT_OVERRIDE でGemini分類ドリフト防止
> - **Windows console encoding**: sys.stdout.reconfigure + PYTHONUTF8=1 環境変数
> - **VC++ Redistributable**: PaddlePaddle DLL依存、Setup自動インストール
> - **google-cloud-vision条件import**: Strategy Cでは不要、ImportError時はNone
> - **Config自動生成**: .env + service_account.json をSetupスクリプト内蔵（BOMなし出力）
> - **タスクスケジューラ自動登録**: ログイン時自動起動、クラッシュ1分後再起動
> - **Azure VM x86 実機テスト完了**: PaddlePaddle 3.0 + PaddleOCR 3.4 動作確認
>
> ### v2.4 変更点 (2026-03-26)
> - OCR主導フィールド抽出、PaddleOCR v2/v3完全互換、小計フィルター、納付書日付
> - 車両費→旅費交通費統一、ハイライト凡例、取引Noタブ独立管理
>
> ### テスト結果 (80頁現金領収書PDF)
> | 環境 | 日付充填率 | T番号充填率 | No.1-59一致率 | 処理時間 |
> |------|----------|----------|------------|---------|
> | **Mac (M-series, v3)** | **100%** | **87.2%** | **基準** | **11分** |
> | **Azure VM (D2s_v3, v3)** | **100%** | **86.6%** | **95.9%** | **1h42m** |
> | EC2 (t2.micro, v2) | 低下 | 低下 | — | 5時間+ |

---

## 1. 產品概述

### 1.1 產品定位

**Super Scaner** 是一款面向日本中小型企業（特別是會計事務所）的 **自動化會計憑證處理機器人**。它通過 Google Drive 文件監聽 + PaddleOCR（ローカル OCR）+ Gemini AI 結構化提取，將員工上傳的發票、收據等財務單據自動轉換為符合 **MoneyForward（マネーフォワード）** 導入標準的會計數據，直接寫入 Google Sheets。

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
| **語言** | Python 3.11 (PaddlePaddle 互換性) |
| **OCR 引擎** | PaddleOCR（ローカル、無料）+ Gemini Vision（フォールバック）|
| **AI 引擎** | Google Gemini 2.0 Flash（結構化提取）|
| **雲端存儲** | Google Drive API v3 |
| **數據輸出** | Google Sheets API (gspread) |
| **通知系統** | Chatwork API v2 |
| **部署方式** | Docker 容器 (AWS EC2), 資源制限付き (--cpus 0.9 --memory 768m) |

### 2.2 模塊架構

```
┌─────────────────────────────────────────────────────────────┐
│                      Super Scaner v2.1                       │
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
| `ocr_engine.py` | PaddleOCR + Gemini Text/Vision、PDF 拆分、Strategy A/B/C | 重構 |
| `benchmark_ocr.py` | OCR ベンチマーク（Strategy A/B/C 比較テスト）| **新建** |
| `sheets_output.py` | Google Sheets 寫入、Tab 管理、分割線、異常標色 | **新建** |
| `anomaly_detector.py` | 異常檢測（日期空/取引先空/T番號不正/高額）| **新建** |
| `config.py` | 員工映射、文件夾配置、科目映射表 (ACCOUNT_MAP) | 修改 |
| `doc_types.py` | 文書類型枚舉、DOC_TYPE_TAB_SUFFIX | 修改 |
| `notifier.py` | Chatwork 通知發送 | 不變 |
| `csv_writer.py` | MoneyForward CSV 生成（**已廢止**，參考保留）| 廢止 |
| `scripts/daily_backup.py` | 每日備份（Python cron 版、已被 GAS 版置換）| 廢止 |
| `scripts/install_daily_cron.sh` | EC2 cron 安裝腳本 | 廢止 |
| `gas/daily_backup.gs` | GAS 日次バックアップ（Google サーバー実行、22:00 JST）| **新建** |

---

## 3. 核心數據流

```
員工上傳 PDF/圖片 → Google Drive (領収書/請求書/給与明細 文件夾)
    ↓
main.py 偵測到新文件 (3秒輪詢)
    ↓
下載 PDF → 拆分為單頁 (多頁PDF時)
    ↓
每一頁 (Strategy C — デフォルト):
  ├→ PaddleOCR (ローカル) → OCR テキスト
  ├→ OCR テキスト + 原始圖片 → Gemini AI (クロスバリデーション) → 結構化 JSON
  ├→ (失敗時回退: Gemini Vision 單獨識別)
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

#### F2: 雙引擎 OCR (PaddleOCR + Gemini)

- **Strategy A:** PaddleOCR → テキスト → Gemini Text 結構化提取
- **Strategy B:** PaddleOCR (信頼度ゲート) → Gemini Text / Vision 分岐
- **Strategy C (デフォルト):** PaddleOCR テキスト + 原始圖片 → Gemini デュアル入力（クロスバリデーション）
- **回退引擎:** Gemini Vision 直接識別（PaddleOCR 失敗時自動回退）
- 優勢: 完全無料（GCP Billing 不要）、ローカル実行で高速、Gemini とのクロスバリデーションで高精度
- `config.py`: `OCR_STRATEGY=C`, `OCR_CONFIDENCE_THRESHOLD=0.7`
- Cloud Vision API コードはコメントアウトで保持（クライアント確認待ち）

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
| 雑費 | 備品・消耗品費 |
| 交際費/食費/飲食費 | 接待交際費 |
| 現金 | 未払金 |

#### F5: 異常檢測 + 單元格標色

| 異常類型 | 標色位置 | 顏色 |
|---------|---------|------|
| 日期為空 | B列 (取引日) | 紅色 |
| 取引先為空 | F列 (借方取引先) | 橙色 |
| T番號不正 | H列 (借方インボイス) | 橙色 |
| 金額 > 10萬 | I列 (借方金額) | 黃色 |

#### F6: 每日備份 (GAS)

- 實現: `gas/daily_backup.gs` — Google Apps Script (Google サーバー実行)
- 時間: 22:00 JST (GAS time-driven trigger)
- 工作 Sheet 全 tab → 備份 Sheet 的一個日付シートに集約（tab名+太線で区切り）
- バックアップ完了後、元 tab を削除（main.py が次回自動再作成）
- 備份保留: 30 天自動清理 (每月1日 23:00 JST)
- PC 電源状態に依存しない（旧 Python cron 版を置換）

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
| Cloud Vision | コメントアウト済み（PaddleOCR に置換）、クライアント確認後に完全削除予定 |
| PaddleOCR | Python 3.11 必須 (PaddlePaddle 互換性)、venv311 使用、poppler 要インストール |
| Sheets API 限流 | 60 次/分鐘，已通過緩存+重試優化 |
| PDF 拆分頁上傳 | SA 配額限制暫不可用，改用原始 PDF URL + #page=N |
| ~~備選 OCR~~ | ~~Umi-OCR (PaddleOCR)~~ → **已採用 PaddleOCR 作為主引擎 (v2.1)** |

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
| **v2.1** | **PaddleOCR 統合 (Strategy C)、Cloud Vision 置換、ACCOUNT_MAP 拡張** | ✅ 完成 |
| **v2.5** | **Windows本番デプロイ、封筒フィルタ、科目兜底ルール** | ✅ 完成 |
| **v2.6** | **客戶本番稼働、GAS日次バックアップ、main マージ** | ✅ 完成 |

### v2.1 ベンチマーク結果 (294 ページ / 305 取引)

| フィールド | 精度 |
|-----------|------|
| date (取引日) | 100% |
| amount (金額) | 100% |
| tax_type (税区分) | 92.3% |
| vendor (取引先) | 93.6% |
| invoice (インボイス番号) | 91.9% |
| debit_account (借方勘定科目) | 89.3% |
| credit_account (貸方勘定科目) | 76.8% |

---

*本文檔反映 Super Scaner v2.6 客戶本番稼働狀態 (main 分支)，截至 2026-04-02。*
