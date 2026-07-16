# headless 運行路徑 Sheets 讀取點盤點（IP-310）

> 目的：確認並固化「Sheets 僅作記賬輸出面，不作狀態源／人機操作面」（契約 §5.5／F54-b：去重權威＝Firestore 台賬，正常路徑 Sheets 只寫不讀；唯一流程豁免＝PENDING 恢復判定讀**當日** Sheets——該路徑屬 B3/IP-304 新建，現行代碼尚不存在）。
> 方法：靜態盤點（grep＋逐點人工核對錨點）。對象＝`feature/sandevistan-headless` 分支 main.py 運行路徑。
> 作成：2026-07-16（B1 基座批）。分類三類＝保留豁免／需摘除／純輸出裝飾（工單 03-batch-plan B1）；
> 對「不讀記賬內容、僅服務寫入動作本身」的讀，本文以「保留（寫入配管）」註記歸類，屬保留豁免的擴充說明。

## 1. 讀取點清單（全部位於 sheets_output.py；main.py 不直接觸 gspread）

| # | 位置 | 所在函數 | 讀了什麼 | 用途 | 分類 |
|---|---|---|---|---|---|
| 1 | sheets_output.py:64 | `__init__` | `open_by_key` 開 spreadsheet handle | 一切寫入的前提；開失敗則該 profile 整條跳過 | 保留（寫入配管：開 handle，非數據讀） |
| 2 | sheets_output.py:75,77 | `_cleanup_default_sheet` | 預設空表「シート1」存在性＋工作表總數 | 初始化清掃（>1 表才刪，防刪成 0 表） | 保留（寫入配管：初始化守衛） |
| 3 | sheets_output.py:88 | `_get_next_txn_no` | `get_all_values()` 掃 A 列求 max 取引No | 決定新行取引No（寫入內容） | **需摘除（反例①）**：計數器狀態以 Sheets A 列為真相源，違反「狀態真相只在 Firestore」（D15）。處置＝B3/IP-304 寫賬改造時對齊（取引No 整頁重試可跳號已被契約允許，F53 註記）；**本批僅記錄不改碼** |
| 4 | sheets_output.py:109 | `_get_or_create_tab` | tab 存在性探測（WorksheetNotFound） | find-or-create 分 tab | 保留（寫入配管） |
| 5 | sheets_output.py:111 | `_get_or_create_tab` | `get_all_values()` 全表拉取 → `_tab_has_data` 旗標 | 旗標**無任何下游讀者**（僅 :68/:111/:118 三處賦值） | **需摘除（反例②，死讀）**：白付一次全表 API。摘除實施留後批，本批不改碼 |
| 6 | sheets_output.py:153 | `start_new_file` | 當前行數（>6 判斷） | 決定是否插入檔案間分割行 | 純輸出裝飾 |
| 7 | sheets_output.py:158 | `start_new_file` | 分割行後新行號 | 分割行白底＋上罫線的套用位置 | 純輸出裝飾 |
| 8 | sheets_output.py:321 | `append_entries` | 寫入前 `get_all_values()` → `pre_write_count` | 定位 append 落點＋異常高亮行號。註釋所稱「重複檢測用」**已失效**（`_detect_and_highlight_duplicates` :594 為零調用死代碼，F31） | 保留（寫入配管：定位讀）。B3/IP-304 頁級原子化重構此路徑時重審 |
| 9 | sheets_output.py:438 | `_ensure_row_capacity` | `worksheet.row_count` 屬性 | 事前擴容守衛（防自動擴容染色） | 保留（寫入配管） |
| 10 | sheets_output.py:456 | `_sanitize_trailing_once` | `worksheet.row_count` 屬性 | 尾部空行白底消毒範圍 | 純輸出裝飾 |
| 11 | sheets_output.py:554 | `_write_unrecognized_row` | 寫入前行數 | 占位行落點＋紅色高亮定位 | 保留（寫入配管）。**B4/IP-306 將整體廢止此獨立寫入路徑**（占位行併入頁級聚合） |

標色標黃等裝飾邏輯（`_apply_anomaly_highlight`、`_write_legend` 等，sheets_output.py:288 起）只寫不讀，維持「純輸出裝飾、不承載流程語義」定位（工單既定口徑）。

## 2. 非運行路徑（一行註記，不入 headless 依賴）

- `local_test.py`：操作員手動測試 runner，間接觸發上表讀取；非 headless 常駐入口。
- `scripts/daily_backup.py`：已廢止的 Python cron 備份版（被 GAS 取代），內含 Sheets 讀取但不在 main.py 路徑。
- `gas/*.gs`（daily_backup 系）：GAS 側時間觸發器＝人看的面／備份面，跑在 Google 基礎設施；`_config` tab 為其概念（daily_backup.gs:154），與 Python 運行路徑無關。
- 其餘運行路徑文件（config.py / doc_types.py / ocr_engine.py / receipt_aggregation.py / anomaly_detector.py / tag_rules.py）：gspread 零觸碰（已 grep 確認）。

## 3. 結論（DoD 判定）

**「Sheets 只寫不讀（僅 PENDING 恢復豁免）」在流程決策意義上成立、在嚴格字面意義上不成立，反例已列**：

1. **流程決策類讀（skip／去重／歸檔判斷）＝零**。唯一的行級去重函數是零調用死代碼（F31）；`is_duplicate_file` 走 Drive md5 非 Sheets（且 B4/IP-308 將停用）。
2. **真反例＝取引No 計數讀**（表 #3）：寫入內容依賴 Sheets 既存數據，屬狀態語義讀，需在 B3 寫賬改造中摘除或遷移。
3. **死讀一條**（表 #5）：無語義，待摘除。
4. 其餘讀取＝寫入配管（4 條）與純輸出裝飾（3 條），不讀記賬內容、不做流程決策；B3（頁級原子化）與 B4（占位行路徑廢止）將自然重構其中大半。
5. PENDING 恢復豁免路徑現行代碼不存在（B3/IP-304 新建時方引入，屆時本清單須增補該唯一豁免條目）。

## 4. 附帶發現（文檔偏差，本批不改）

- **SS 倉 CLAUDE.md 記載過時**：「取引No 在 `_config` tab 管理，`flush()` 回写」與代碼不符——Python 側無 `_config` tab（僅 GAS 有）；`flush()` 為 no-op（sheets_output.py:393-395）；取引No 實際＝記憶體 dict `_tab_next_txn` ＋冷啟動時掃 A 列 max+1（:84-101）。是否修訂 CLAUDE.md 待趙拍板。

## 5. 殘餘風險（記錄，不在 B1 處置）

- 取引No 計數同時活在 Sheets A 列與進程記憶體：多進程併發或人工改行可致碰撞/跳號（`flush()` 無回寫）。B3 對齊。
- 定位讀（#8/#11）與其後 append＋定點格式化非原子，依賴「單寫入者」假設；假設被打破即行號錯位。B3 頁級原子寫改造對象。
- gspread 屬性讀（`row_count`）究竟走實時 API 還是本地 `_properties` 緩存屬庫實現細節，靜態盤點未做動態驗證（對分類結論無影響，對 API 開銷估算有影響）。
