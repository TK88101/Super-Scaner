# B4 收尾語義批 実行計画（IP-305 → IP-306 機械半 → IP-308）

> 工単：`~/Documents/サンデヴィスタン/docs/impl/03-batch-plan.md` B4 節。
> 上游権威：`03-super-scaner.md`（IP 定義）＋ `contracts/job-state-machine.md` **v0.12** §3.2。
> 生成：2026-07-24（fatboyslim Phase 1）。狀態：**v3 定稿**（Codex 初審 13 條裁決＋複審 1 輪＝駁回 2 條成立・新 HIGH 1 條採納，附録 B）。
> 基線：`feature/sandevistan-headless` @ `73c1f95`，全量 452 テスト緑。

## 0. 拍板記録（本批の設計裁決，不再重議）

**趙裁決（2026-07-24，本 session）**：
1. 失敗頁占位行（警告行）＝**每失敗頁一行、入頁級台賬防重複**（工単原文口徑）。
2. 部分失敗檔＝**不謊報 POSTED**、留控制面/人工兜底；F06-How 落筆後一次替換。
3. **自癒優先原則（最高指導）**：「人工是最後底線，只負責最少工作。暫時類問題必須自動重試自癒；不要一遇到問題就留給人工。」

**Codex 諮詢/評審軌跡**：前置設計諮詢（附録 A）→ Plan 初審 13 條→逐條裁決（附録 B）→ 本 v2 反映。

**沿用既裁（不重議）**：F06 方向=a 分批入賬（失敗頁走未処理式動線）；F06-How TBD **禁代拍**；F07 重入上限＝控制面計數 SS 不管；F09=a 禁成功路徑 move；F26 物理語義；F53/F54＝續跑靠頁級去重（**不靠管線跳頁**——重投輪對已 CONFIRMED 頁重跑 OCR 是既定口徑，費用由 memo＋F07 封頂）；B3 台賬/三態/夾具復用；**謂詞三件套簽名凍結**（`derive_page_id`/`check_page`/`post_page`，basic-design/01——本批只允許**新增**只讀 accessor，不改三者）。

## 1. 目標／非目標

**目標**（＝工単 B4 三 IP 的 DoD 全綠）：
- IP-305：斷點續跑全程冪等——5 頁件寫 3 票崩潰→重投→恰 5 票無重複；舊 epoch 回報被拒。
- IP-306 機械半：內容類失敗頁占位行併入頁級原子寫、受 `{base}:p{n}` 台賬去重保護；headless 對 `append_entries` 聚合占位行路徑廢止；重跑兩遍→占位行恰 1 行；終態回報接口留 TBD 豁免記錄。
- IP-308：`HEADLESS_MODE=1` 零 Drive move 出站（隔離動作唯一例外）；SUCCESS→回報 POSTED（攜 `lease_epoch`）；`is_duplicate_file` headless 停用；`PageUrlResolver`/`SPLIT_PDF_FOLDER_ID` **一字不碰**。
- 趙指令落地：暫時類失敗自動重試自癒；全內容類失敗→DEAD_LETTER 回報（契約 §3.2 SS 行），堵死無限 3 秒重掃洞。

**非目標**（一行不寫）：F06-How 落筆／`POSTED_PARTIAL` 正式回報（只留接縫）；`HANDOFF_FOLDER_ID` folder_map 接線；真庫聯調（U14）；高亮統一（趙待決①）；列字母 bug（趙裁定另 session）；B5；UI 版任何行為改動；`sheets_output.py` 寫入層零改動；posting schema 零改動。

## 2. 設計定案

### 2.1 失敗頁三分類（T1）【初審 #5/#6 反映】

`ocr_engine` 的 `_page_error` yield（全倉掃描所有 yield 點）附加結構鍵 `_error_class`：

| 類 | 判定（白名單制） | 語義與去向 |
|---|---|---|
| `CONTENT` | **僅** JSON 解析失敗分支（無例外、模型有應答但雙路徑無果，`ocr_engine.py:1822-1843` 型） | 票面不可讀。占位頁入台賬、人工重掃該頁 |
| `RETRYABLE` | `isinstance(page_err, _GEMINI_RETRY_EXCEPTIONS)`（**isinstance 語義含子類**，非 type 精確比對） | 暫時故障。零寫入，檔級 FAILED 不 memo→下輪 3s 自癒 |
| `UNKNOWN` | 其餘一切例外（SDK/認證/程式缺陷等） | 零寫入，檔級 FAILED＋**per-epoch memo**→僅隨控制面重投（≤3）重試，超限落人工。**不得**判 CONTENT/DEAD_LETTER（防「認證錯誤→全夾 DEAD_LETTER 風暴」） |

消費側對缺失鍵（舊 fixture）默認 `CONTENT`？——否：**默認 `UNKNOWN`**（保守不釘死；缺鍵只出現在測試舊資材，生產路徑必帶鍵）。UI 路徑忽略未知鍵，零影響。

### 2.2 占位行物理形態（T3，IP-306 機械半核心）

**占位頁定義**：該頁全部 result 均無有效 entries（含 `_page_error` CONTENT 件與「JSON 正常但 entries 空」的認識不能件——要件 F-PST「識別不能なページのみ未処理へ」同一動線）。**混型頁不變式（複審 #14）**：同頁「有效 result 與零-entry result 並存」→ 不寫入、即時 ESCALATED——CONFIRMED 頁恆為「全有效（ticket_count>0）」或「純占位（==0）」二態之一，`confirmed_ticket_count` 的身份判定才在重跑輪恆可靠（混型頁若入台賬，ticket_count>0 會在後輪自證為完整成功、謊報 POSTED）。占位頁不再 `continue` 跳過，error/空 result 照常進頁緩衝，隨該頁 `build_page_write`→`commit_page` 原子落地——零行分支（`sheets_output.py:481-485` `_build_unrecognized_block`）天然生成占位行，**`sheets_output.py` 零改動**。要點：
- memo 文言固定：`⚠ ページ処理エラー p{n}/{total} 手動再スキャン要`（CONTENT 件覆寫）；認識不能件沿用既有文言（UI 同型）。`vendor=檔名`（CONTENT 件）；`date=""`。
- `source_url`＝該頁單頁 PDF 連結（error yield 帶 `page_bytes`；失敗降級 `base_url#page=N`——既有降級語義）。
- **`ticket_count` 語義釘死＝有效入賬票數**【初審 #1 修法核心】：`_extract_tickets` 僅計 entries 非空的 result。占位頁台賬記錄＝`ticket_count=0`、`tickets=()`。由此 **CONFIRMED 記錄可自證身份**：`ticket_count>0`＝真入賬頁；`==0`＝占位頁——重投輪據此恆判失敗頁，杜絕「後輪 OCR 偶然成功→謊報 POSTED（數據實缺）」。既有字段既有含義（「台账记该页票数供対账」），零 schema 改動。
- 重跑：`check_page(p_n)`→CONFIRMED→SKIP，占位行恆恰 1 行。
- `main.py:498-520` 檔級聚合占位行塊**整段刪除**；headless 對 `writer.append_entries` 呼叫歸零（UI `_write_unrecognized_row` 不動）。
- 語義註釋釘死：①台賬 CONFIRMED＝「該頁輸出已落地」≠「OCR 成功」，**禁止**以「全頁 CONFIRMED」倒推 POSTED——檔級終態由 §2.3 頁級 outcome 合併決定；②占位頁 CONFIRMED 後自動路徑不再重試該頁（F06-a 人工動線）——註釋＋測試；③高亮 best-effort 弱保證，台賬只保證行不重複。
- `posting_ledger` **新增只讀 accessor** `confirmed_ticket_count(page_id) -> int | None`（無 CONFIRMED 記錄→None；三謂詞簽名不動）。

### 2.3 頁級 outcome 模型與檔級終態矩陣（T3/T4）【初審 #1/#2/#3 反映】

**頁級**（物理頁粒度，頁邊界 flush 時定案；`page_num -> outcome`，禁用 yield 計數）：

| 頁況 | 頁 outcome |
|---|---|
| 例外類 `RETRYABLE`／`UNKNOWN` | 同名（零寫入、零台賬觸碰） |
| 占位頁（§2.2 定義）且 check_page→WRITE | `PLACEHOLDER_WRITTEN`（占位行落地） |
| 正常頁且 WRITE | `POSTED_NOW` |
| check_page→SKIP | 以台賬判：`confirmed_ticket_count>0`→`POSTED_PRIOR`；`==0`→`PLACEHOLDER_PRIOR`（恆失敗頁）；None（PENDING witness 復元後等值處理） |
| 同頁 error 與正常 result 並存／**同頁有效與零-entry result 並存（混型頁，#14）**／頁號重現 | 即時 `ESCALATED`（不變式破れ；混型頁靠重投（≤3）博全識別，仍混型則人工整頁承接） |

**檔級**（短路順序）：

| 條件 | Outcome | 回報 | move | memo |
|---|---|---|---|---|
| ESCALATE 事件 | `ESCALATED`（即時返回） | 無 | 無 | 記（**TTL≈20 輪**，非整 epoch【#7】） |
| 任一頁 `RETRYABLE` | `FAILED` | 無 | 無 | 不記（3s 自癒窗） |
| （無 RETRYABLE）任一頁 `UNKNOWN` | `FAILED` | 無 | 無 | 記（per epoch，隨重投放行） |
| `total_pages==0` | `FAILED` | 無 | 無 | 記（per epoch；Phase 2 修訂——零 yield 檔 3s 重掃無益且燒 OCR，見 §10-3） |
| 全頁 ∈ {`PLACEHOLDER_*`} | `DEAD_LETTER` | `report_dead_letter`（payload 見下） | 無 | 記 |
| 任一頁 ∈ {`PLACEHOLDER_*`}（其餘 posted） | `PARTIAL`（新增） | **無**（TBD 接縫 `_report_partial_tbd`：僅日誌＋豁免註釋） | 無 | 記 |
| 全頁 ∈ {`POSTED_NOW`,`POSTED_PRIOR`} | `SUCCESS` | `report_posted(base, lease_epoch=…)` | **無** | 記 |

- `ProcessOutcome` 擴 `PARTIAL`/`DEAD_LETTER`（UI bool 路徑不變）。
- **epoch 缺失（違約態）＝任何 outcome 一律零 reporter 呼叫**＋警告日誌＋檔案保持【#3】。
- **DEAD_LETTER payload 釘死**【#9】：`{"stage": "ocr", "error_class": "NON_RETRYABLE", "message": "all_pages_unreadable: {n_failed}/{n_total} pages [{p1,p3,…}]"}`——僅頁碼/計數等技術字段；**檔名/客戶名/金額/例外原文禁入**（日誌白名單制）；測試斷言 payload 精確相等＋敏感字段缺席。
- 回報結果處置：`APPLIED`/`ALREADY_DONE`→日誌；`REJECTED`（含 stale epoch）→模組自寫 alert，SS **不重試不 move**。
- `PARTIAL` 兜底鏈：不回報→打卡停滯→`POST_UNKNOWN`→重投（≤3）→超限人工。誠實優先於順暢。
- 附帶語義變化（明示）：含認識不能頁（entries 空）之檔在 headless 不再視為全成功（B3/UI 視為成功歸檔）——依 F-PST／F06-a／趙誠實指令；UI 不變。

### 2.4 入口授權與費用防護（T4）【初審 #4/#7/#8 反映】

- **intake 狀態白名單**：入口守衛透出 `current_state`，**僅 `POSTING_IN_PROGRESS` 允許處理**（契約授權的寫賬窗口；交棒順序「先 transition 後 move」保證正常件必處此態）。其餘一切（`POSTED`／`POST_UNKNOWN`／`DEAD_LETTER`／終態／缺失 None）→本輪跳過（不下載不 OCR 不打卡）＋一次性日誌。`POST_UNKNOWN` 件由控制面重投（→`POSTING_IN_PROGRESS`，F26）後自然放行——SS 不在無租約狀態下寫賬。表驅動測試逐契約狀態斷言。IP-303 注記④在此完全落地。
- **memo**：進程內 dict，鍵 `(base, lease_epoch, file_id)`，值 `{outcome, folder_id, expire_cycle?}`。命中→跳過本輪。epoch+1 天然放行；`ESCALATED` 帶 TTL（≈20 輪＝約 60s，過期重試——probe 暫時故障自癒【#7】）；**剪枝按夾**：僅當某夾本輪 `list_files` 成功且 file_id 未見時，剪該夾所屬項（夾列舉失敗不剪，防誤刪）【#8】。進程重啟清零。註釋釘死：memo 只是同進程費用緩衝，不承擔跨進程正確性。

### 2.5 lease_epoch／job 狀態透出（T2）

`IntakeCheck`/`IntakeGateResult` 尾部追加 `lease_epoch: int | None = None`、`job_state: str | None = None`（`job.get("lease_epoch")`／`job.get("current_state")`；鍵名已與契約 §2、控制面 `firestore_store.py:165`、本倉 `firestore_report.py:274` 三方比對一致）。守衛五分岐判定零改動（白名單判定在 main 消費側）。

### 2.6 IP-308 move 出口全審計（T4）【初審 #12 反映：驗收改行為 spy】

| 出口 | 現行 | headless 處置 |
|---|---|---|
| `main.py:841-846` 重複件檢測＋move | `is_duplicate_file`→move | **分支整體跳過**（`reporter is None` 時才走） |
| `main.py:890-891` SUCCESS move | move | **刪除**，代之以 `report_posted` |
| 入口守衛隔離夾 move | 隔離誤投件 | **保留**（唯一例外，F20 已決） |
| `main.py:897` UI SUCCESS move | move | 不動（`reporter is None` 路徑） |
| `PageUrlResolver` 拆頁上傳 | 上傳（非 move） | 一字不碰（全局禁手） |

驗收＝行為 spy：headless 各 outcome／duplicate／REJECTED 路徑斷言 move 零呼叫；隔離路徑恰一次。本表為人工審計清單（一次性），不做 grep/簽名式脆弱斷言。

## 3. 任務清單（TDD：每項先 RED 後 GREEN；checkpoint commit 每 IP 一個；單 worker 順序實施）

**T1 `ocr_engine` 三分類鍵**（§2.1）
- DoD：transport 例外（含**自定義子類**）→`RETRYABLE`；JSON 解析失敗→`CONTENT`；其他例外→`UNKNOWN`；全 `_page_error` yield 點覆蓋（grep 清單入測試註釋）；既有測試全綠。

**T2 `intake_guard` 透出＋`posting_ledger` accessor**（§2.5/§2.2）
- DoD：透出字段正確（有值/缺失 None）；`confirmed_ticket_count` 三態（>0／==0／None）單測；三謂詞簽名未動（代碼審視項）；既有測試全綠。

**T3 `main` 頁級 outcome 模型＋占位行併入頁級寫**（§2.2/2.3；IP-306 機械半本體）
- DoD（`test_process_file_headless` 新增群）：①CONTENT 單頁失敗→占位行 1 行隨頁原子寫、台賬 `ticket_count=0`、單頁 URL；②重跑→SKIP、占位行恆 1 行；③RETRYABLE/UNKNOWN 頁→零寫入、FAILED；④全占位頁→DEAD_LETTER＋payload 精確斷言＋敏感字段缺席斷言；⑤混合優先級表驅動（RETRYABLE>UNKNOWN>占位>成功；ESCALATE 最高）；⑥**一頁多票＋另頁失敗→按物理頁計數**（3 票頁＋1 敗頁＝1/2 非 1/4）【#2】；⑦`_extract_tickets` 僅計有效票；⑧**前輪占位 CONFIRMED、後輪同頁 OCR 成功→恆 PARTIAL 不得 POSTED**【#1b】；⑨**前輪成功 CONFIRMED、後輪同頁 RETRYABLE→不翻轉檔級語義（仍 SUCCESS）**【#1c】；⑩聚合塊已刪→headless 零 `append_entries`（行為 spy）；⑪**混型頁（有效＋零-entry 同頁）→零寫入、即時 ESCALATED＋跨重跑測試**【#14】。占位行物理形態（memo/vendor/URL/紅標列位）用**真 `SheetsOutputWriter.build_page_write` 純構造**斷言，不經 FakeWriter【#11】。

**T4 `main` 循環接線：回報＋move 審計＋memo＋intake 白名單**（§2.3/2.4/2.6；IP-308 本體）
- DoD（fake 三件套接線測試）：①SUCCESS→`report_posted` 恰一次攜正確 epoch＋move 零呼叫（spy）；②REJECTED（stale epoch）→不 move 不重試（IP-305 DoD②）；③DEAD_LETTER→`report_dead_letter` 恰一次；④PARTIAL→零回報＋TBD 接縫日誌；⑤epoch 缺失→SUCCESS/DEAD_LETTER 均零 reporter 呼叫【#3】；⑥headless 下 `is_duplicate_file` 零呼叫；⑦memo：同 epoch 第二輪零下載零 OCR（OCR 呼叫記錄斷言）、epoch+1 放行、ESCALATED TTL 過期重試、多夾剪枝三場景（他夾不誤剪／列舉失敗不剪／跨輪消失剪）【#7/#8/#10】；⑧intake 狀態表驅動（`POSTING_IN_PROGRESS`→PROCESS；`POSTED`/`POST_UNKNOWN`/`DEAD_LETTER`/None→跳過）【#4】；⑨UI 路徑行為不變（既有測試綠）。

**T5 IP-305 斷點續跑測試群**（復用 `HeadlessRerunFixture`＋`fake_firestore`，禁新造夾具；夾具允許適配：error 頁注入、per-頁 OCR 呼叫記錄、reporter fake）
- DoD：①5 頁寫 3 票 kill→重投→恰 5 票無重複（工単 DoD①）；②舊 epoch 回報被拒→檔案保持、不重試寫賬；③暫時類自癒鏈：p3 RETRYABLE→FAILED→下輪 p3 成功→SUCCESS→POSTED（成功頁 Sheets 零重寫）；④content 部分失敗重跑兩遍→占位行恰 1 行（IP-306 DoD）。

**T6 回歸閘門**
- DoD：黃金樣本回歸 `golden_replay` 全 diff 綠（觸寫賬管線→必跑；預期 DIFF-A 空）；全量 unittest discover 綠（≥452＋新增）；新增/改動代碼覆蓋率 80%+。

**T7 文檔落檔**
- DoD：`docs/headless-deploy-checklist.md` 更新（IP-308 已實裝＋memo/白名單行為）；`docs/headless-sheets-read-audit.md` 第 11 條更新；F06-How TBD 豁免記錄（代碼註釋＋本 Plan §2.3）；認識不能頁語義變化記入匯報；`03-super-scaner.md` 進度回寫草案（是否 commit 請示趙）。

任務硬序：T1→T2→T3→T4→T5→T6→T7（單 worker；T1/T2 無依賴但同 worker 順做）。

## 4. 驗收標準（可腳本化）

```bash
venv311/bin/python -m unittest discover -p "test_*.py"                                    # 全量
venv311/bin/python -m unittest test_process_file_headless test_intake_guard test_headless_rerun_fixture -v
venv311/bin/python golden_replay.py                                                        # 黃金樣本回歸
```
加 §3 各 T DoD 斷言全綠＝完成線（工単 §0.4 閘門另行走完）。

## 5. 測試策略

unittest 風格；Firestore 全 fake；單元（T1/T2）＋集成（T3/T4/T5 fake 三件套＋夾具）＋黃金樣本決定論重放（E2E 等價物）；覆蓋率新增/改動 80%+。

## 6. 影響面

| 檔 | 改動 | 風險級 |
|---|---|---|
| `main.py` | 頁級 outcome 模型、占位行併入、回報接線、move 刪除、memo、intake 白名單 | 高（headless 專屬；UI 零觸） |
| `ocr_engine.py` | 僅 `_page_error` yield 附加 `_error_class` | 低 |
| `intake_guard.py` | dataclass 尾部追加兩字段 | 低 |
| `posting_ledger.py` | 新增只讀 `confirmed_ticket_count`（三謂詞不動） | 低 |
| `headless_rerun_fixture.py` | 夾具適配 | 測試域 |
| `sheets_output.py` | **零改動** | — |
| UI 行為 | **零改動**（HEADLESS_MODE 未設定＝不激活） | — |

## 7. 風險與回退

- **R1 誤分類**：CONTENT 白名單化後「程式缺陷被釘死」面已除；殘餘＝UNKNOWN 重投 3 次的費用（有界）。
- **R2 回報接線缺陷**→job 卡 `POSTING_IN_PROGRESS`：打卡制→reconciliation 兜底；REJECTED/ALREADY_DONE/缺 epoch 分支測試釘死。
- **R3 memo 誤吞**：epoch 鍵＋按夾剪枝＋TTL＋重啟清零；測試覆蓋。
- **R4 新舊交棒件並存**：所有權屬控制面契約域（F26）；SS 台賬保證不重複入賬；記匯報觀察項。
- **R5 認識不能頁語義變化**（§2.3 附帶）：headless 含此類頁之檔不再 POSTED——記匯報、趙可否決回退（單點：占位頁定義收窄回 `_page_error` 件）。
- **回退**：逐 IP checkpoint，`git revert` 粒度；最壞回 `73c1f95`。

## 8. 附録 A：Codex 前置設計諮詢裁決（2026-07-24，Plan 前）

問題 1（占位行形態）：Codex 站隊 A 附條件，修正 4 條全採納（§2.2 註釋三條＋單頁 URL）。問題 2（部分失敗終態）：站隊 A 修正版，B 案（謊報 POSTED）明確否決；修正全採納：epoch 感知 memo、memo 蓋 ESCALATED/POSTED、move 全審計、優先級釘死、REJECTED 不 move 不重試、memo＝費用緩衝非正確性、不入凍結 schema 不發明 Drive property。另新舊件並存風險→R4。趙自癒指令→三分類與 RETRYABLE 快速自癒窗。

## 9. 附録 B：Plan 初審辯論裁決（Codex 13 條，2026-07-24）

| # | 嚴重度 | 裁決 | 處置 |
|---|---|---|---|
| 1 | HIGH | **部分採納** | (b)(c) 採納＝頁級語義合併（SKIP 頁以 `confirmed_ticket_count` 判身份，§2.2/2.3；T3⑧⑨）。(a)「CONFIRMED 頁重跑仍燒 OCR」**駁回**：工単/F53/F54 既定口徑＝續跑靠去重不靠管線跳頁（「不暴露 process_pipeline(start_page)」明文）；費用由 memo＋F07≤3 封頂 |
| 2 | HIGH | 採納 | 物理頁粒度 outcome 模型（§2.3；T3⑥） |
| 3 | HIGH | 採納 | epoch 缺失＝一律零 reporter 呼叫（§2.3；T4⑤） |
| 4 | HIGH | 採納 | intake 白名單僅 `POSTING_IN_PROGRESS`（§2.4；T4⑧） |
| 5 | HIGH | 採納（修改） | 三分類：CONTENT 白名單化＋UNKNOWN→FAILED+memo（§2.1）；未知不判 CONTENT/DEAD_LETTER |
| 6 | MED | 採納 | isinstance＋子類測試（§2.1；T1） |
| 7 | MED | 採納（修改） | ESCALATED memo 改 TTL≈20 輪（§2.4；T4⑦）；不採「probe 例外分型」——`check_page` 簽名凍結，TTL 已達同效 |
| 8 | MED | 採納 | 剪枝按夾成功列舉；memo 帶 folder_id（§2.4；T4⑦三場景） |
| 9 | MED | 採納 | DEAD_LETTER payload 釘死＋脫敏斷言（§2.3；T3④） |
| 10 | MED | 部分採納 | 兩場景全收（T3⑧⑨）；「驗 CONFIRMED 頁未送 OCR」**駁回**（同 #1a——re-OCR 為既定口徑；OCR 呼叫斷言改用於 memo/intake 跳過測試 T4⑦） |
| 11 | MED | 採納 | 占位形態用真 `build_page_write` 純構造斷言（T3 尾註） |
| 12 | LOW | 採納 | 結構性/簽名/grep 斷言→行為 spy（§2.6；T3⑩/T4①）；§2.6 表降格為人工審計清單 |
| 13 | LOW | 採納 | 串行論證瘦身（單 worker 事實不變） |

**複審結果（同日）**：
- 駁回 #1a／#10-第一彈：Codex **「接受，不重提」**＝我方勝（維持駁回）。
- 駁回 #7 半條（probe 例外分型）：Codex **「接受，不重提」**＝TTL 方案維持。
- Codex 新提 **#14 HIGH（混型頁）**：同頁有效＋零-entry result 並存時 `ticket_count>0` 無法自證完整性，重跑輪誤判 POSTED——論證成立＝Codex 勝，**採納**：混型頁納入不變式破壞→ESCALATED（§2.2/§2.3；T3⑪）。
- 其餘 v2 新設計（三分類 UNKNOWN／intake 白名單／ticket_count 身份判定／R5）：Codex 明示無新增 HIGH 異議。

**定稿宣言**：13＋1 條全部閉環（採納 12・部分採納 2・駁回成立 2），v3 定稿（2026-07-24）。有輸有贏（Codex 複審贏 1 條），裁決紀律成立。

## 10. Phase 2 実施偏離裁決（2026-07-25，worker 照実申告 8 項）

| # | 申告 | 裁決 |
|---|---|---|
| 1 | `HeadlessOutcome` return 型新設（outcome/retryable/dead_letter_payload） | 採納——memo 三態判定與 payload 傳遞的最小侵襲實現；UI bool 路徑不變 |
| 2 | `_process_one_file` 抽出（`main()` 逻辑不變分離） | 採納——B2 接線 helper（`49eddab`）同型前例，行為 spy 可測化所需 |
| 3 | `total_pages==0` FAILED 記 memo（偏離 §2.3 原表「不記」） | 採納並修表——零 yield 檔每 3s 重掃無益燒 OCR；per-epoch memo 符合費用防護意圖 |
| 4 | `_prior_page_kind` None 緣倒向 `PLACEHOLDER_PRIOR` | 採納——誠實默認（不謊報 POSTED），單測已固定 |
| 5 | DEAD_LETTER message 分母含封筒無音跳過頁 | 記錄——診斷文言級，既有封筒語義未觸，不改 |
| 6 | T6/T7 未着手（worker 範圍＝T1-T5） | 符合指令——T6 由編排者在 Phase 3/4 閘門執行、T7 收官執行 |
| 7 | Plan 文檔未 commit | 編排者補（本 commit） |
| 8 | main.py 924→1214 行（超 800 目安） | 記錄——着手前既超、Plan 錨點釘死本檔，拆分屬範圍外；列入遺留清單匯報趙 |
