# B7 追補（S1-S3 字段対斉・§5.1-d tab 改造・末尾段 envelope_filter）実施 Plan

> 2026-08-01・分支＝`feature/sandevistan-headless`・工単＝胶水仓 `docs/impl/03-batch-plan.md` B7「収官取証」節＋`docs/plans/2026-08-01-cross-repo-read-schema-repair.md` SS 側条目（細則の権威）。
> 流程＝/fatboyslim：本 Plan → Codex 対抗評審 → 辯論裁決 → 定稿 → Sonnet worker 実施（TDD）→ /simcodex → 全量テスト＋golden 回帰。

## 0. 目標と非目標

**目標**
1. **S1**：`posting_ledger._pending_payload` の台帳キー `page_num` → `page`（契約 §5.5 字面対斉）。
2. **S2**：postings payload に `written_at` 双写（§5.3 打卡制の数据源）。防漂移三律遵守。
3. **S3**：payload ⊇ 契約 §5.5 字段表の構造テスト（防再漂移）。
4. **§5.1-d**：headless の Sheets tab 粒度を `顧客番号_顧客名`（1 社 1 tab）へ。現役 UI 版零改動。
5. **§5.1-b 裁定-5**：末尾段（単頁 PDF・画像）でも headless では `envelope_filter` 有効化。
6. 契約 v0.16 升版（胶水仓 `contracts/job-state-machine.md`）：§5.5/§5.6 補注（事前授権済）＋§9 に tab 名取得通道の TBD 登録。
7. 契約義務確認表（§6 SS 行 ①-⑤）の証拠付き回答＝収官報告に含める。

**非目標（範囲钉死）**
- §5.7 provider 事件（書込＋例外帰因是正）＝**本 session 不実施・留档**（工単「視余力」、下記 §8 遺留清単）。
- P1-12（末尾段の頁カバレッジ哨戒）＝不実施・留档（影響限定＝FAILED でファイル保持、無音消失ではない）。
- 識別段の双 AI 構造（strategy C）には触れない（契約 §6 明文）。
- 現役 UI 版（HEADLESS_MODE 未設定経路）の挙動変更ゼロ。
- 控制面（胶水仓）コードには触れない（契約 md の升版のみ）。
- commit/push は趙拍板待ち（wip/ checkpoint のみ可）。

## 1. 前提事実（現地勘察・全て file:line 実証済）

| # | 事実 | 位置 |
|---|---|---|
| F1 | `_pending_payload` が `"page_num"` キーで書く（契約 §5.5 字面＝`page`）。`"page_id"` も字段重複（契約補注方針＝文書 ID に一本化） | `posting_ledger.py:346-366` |
| F2 | 書込点は 2 つだけ：`_claim_pending`（全文 set・now 一回取得）と `_confirm`（`{**data, status, sheet_row_range, updated_at}` set・now 一回取得）。PRESENT 補記も `_confirm` 経由 | `posting_ledger.py:283-336` |
| F3 | ledger 本体は `page_num`/`page_id` を読み返さない（読むのは status/sheet_tab/predicted_row_range/row_fingerprint/ticket_count/created_at のみ） | `posting_ledger.py:209-298` |
| F4 | tab 名は `_tab_name(employee_name, doc_type)`＝`{従業員名}_{後缀}`、writer 内 4 箇所で共用（start_new_file / build_page_write / next_txn_no / append_entries） | `sheets_output.py:196-199,319,507,575,628` |
| F5 | headless の tab 決定点は `_flush_page` の `writer.next_txn_no(uploader_name, doc_type)`＋`writer.build_page_write(uploader_name, ...)` の 2 呼出。行内容の投稿者列は別系統（`result["uploader"]`＝`main.py:1199`） | `main.py:774-776` |
| F6 | headless は `start_new_file` を呼ばない（`ledger is None` ガード）。監査タブは owner 無関係（`append_audit_row`） | `main.py:595-596` |
| F7 | 交棒で SS が受けるのは Drive property `sandevistan_posting_id` のみ。job 文書（`get_job(base)`）には契約 §2 で `customer_id`（必填）はあるが**顧客名は無い**——`顧客番号_顧客名` の完全形を SS が得る通道は契約に未定義（控制面実装にも customer_name 零命中） | `intake_guard.py:22`、契約 §2、胶水仓 grep 実証 |
| F8 | intake 判定は job dict から lease_epoch/job_state を転記する先例あり（B4）——同型で customer 字段を転記できる | `intake_guard.py:106-115` |
| F9 | 末尾段は `_yield_page_results(doc_type, raw_data, ocr_text, ocr_conf)` を `envelope_filter` 無し（既定 False）で呼ぶ。逐頁ループは `envelope_filter=True` | `ocr_engine.py:2221,2150-2152` |
| F10 | 封筒判定は `_yield_page_results` 内 RECEIPT 分岐のみ・「事後説明器」（entries 有効を否決しない不変式）。除外 yield → headless は監査タブ＋page_outcomes EXCLUDED へ既接線（IP-402） | `ocr_engine.py:1927-1972`、CLAUDE.md |
| F11 | `config.headless_mode()` は呼出時点評価の関数（ocr_engine から参照可） | `config.py:273-275` |
| F12 | golden replay は自前で writer を構築し従業員名 tab を使う（`EMPLOYEE_NAME="LocalTest"`）——tab 改造を**注入式**にすれば golden 基線は無影響 | `golden_replay.py:36-40,495-499` |
| F13 | P1-9（テスト歯型再設計）は P0-10 消化時に**既済**：`append_calls==0`/`placeholder_calls==[]`/`audit_rows==頁数`＋正例・否定対照同一断言集 | `test_headless_excluded_page.py:174-179` 他、`test_pipeline_consumers.py:73-90` |
| F14 | 控制面側 R1-R3 実装済（snap.id 注入・校験分級・否定対照 8 件、639 緑）——SS が payload から `page_id` 字段を落としても控制面は壊れない | 修復 Plan §4.5 |
| F15 | 契約 §5.5 字段表＝`{ page, ticket_count, status, sheet_row_range, written_at, tickets }` | 契約 :350-355 |

## 2. タスク清単（実施順・各項 DoD 付き）

### T1（S1）：payload 正名 `page_num`→`page`＋`page_id` 字段落とし
- `posting_ledger.py:348` の `"page_num": summary.page_num` → `"page": summary.page_num`（dataclass `PagePostingSummary.page_num` 属性名は**不変**——契約が縛るのは Firestore 文書のキーのみ。内部名まで変えると差分が無駄に広がる）。
- 同 `:347` の `"page_id": page_id` を**削除**（契約補注「文書 ID＝page_id、不重複入字段」対斉。F14 により控制面安全）。`_pending_payload` の `page_id` 引数が不要になるなら署名も掃除。
- 影響テスト更新：`test_posting_ledger.py:42,61,180` ほか、`grep -n '"page_num"\|"page_id"' *.py` 全命中を逐一目視で「台帳 payload の話か、管線 dict の話か」分別してから直す（管線側 `page_num` は別概念・触らない）。
- **DoD**：RED→GREEN。台帳 doc に `page` キーが載り `page_num`/`page_id` キーが載らないことの断言テスト。既存全テスト緑。

### T2（S2）：`written_at` 双写・防漂移三律
- `_pending_payload` に `"written_at": updated_at` を追加（引数を増やさない＝`updated_at` と同一値で三律①「同一 now」を構造的に満たす）。
- `_confirm` の `new_data` に `"written_at": now` を追加（`updated_at` と同じ `now` 変数＝①、同一 `txn.set` 内＝②）。
- `created_at`/`updated_at` は保持（SS 内部監査用、控制面は無視）。
- **DoD**：三律③のテスト＝PENDING 直後・CONFIRMED 直後の両態で `written_at == updated_at` 恒成立の断言（fake_firestore 経由・両状態 doc を実測）。witness PRESENT 補記経路（`require_pending=True`）でも成立。

### T3（S3）：契約 §5.5 構造テスト（**状態別 schema テスト**・Codex #6 採納で格上げ）
- 新テスト（`test_posting_ledger.py` へ追記）：契約 §5.5 字段表 `{"page", "ticket_count", "status", "sheet_row_range", "written_at", "tickets"}` をリテラルで列挙し、**PENDING／CONFIRMED の状態別に**キー存在＋型・値域・相関を断言：
  - 両態共通：`契約字段集 ⊆ doc.keys()`／`page == summary.page_num`（int）／`ticket_count == len(tickets)`／`written_at == updated_at`（UTC aware datetime）。
  - PENDING：`sheet_row_range is None`。
  - CONFIRMED：`sheet_row_range == [start, end]`・正整数・`start <= end`。
- 追加断言：`"page_num" not in doc` / `"page_id" not in doc`（旧形再発の否定対照）。コメントに契約 §5.5 v0.16 を参照。
- **DoD**：このテストを S1/S2 適用**前**のコードに当てると落ちること（歯の証明＝RED 確認をコミットログでなく実行ログで示す）。

### T4（§5.1-d）：headless tab＝`顧客番号_顧客名`（1 社 1 tab）

**位置づけ（Codex #1 修改採納）**：`customer_label` の控制面書込（TBD-8）拍板前は**§5.1-d の明示的例外状態＝顧客番号単独 tab** として運用。完全達成（`顧客番号_顧客名` 全文）は TBD-8 落地で自動到達（縮退規則がそのまま label を採用する）。発布門檻（連調・投産禁止）内のため運用被害なし。「従業員 tab 維持で feature 未有効化」案は駁回済（従業員軸は趙が明示に殺した前提＝より重い偏離）。

接続設計（定稿）：
1. **intake_guard**：`IntakeCheck`/`IntakeGateResult` に `customer_id: str | None = None`・`customer_label: str | None = None` を追加。`check_intake` の job 取得済分岐で `job.get("customer_id")`/`job.get("customer_label")` を転記（F8 の lease_epoch 先例と同型・五分岐判定自体は零改動）。
2. **label 検証（Codex #2 修改採納）**：main 側の tab_owner 解決で `customer_label` を検証——①非空 str ②`customer_id` で始まる ③区切りは全角空格＋非空の名前部 ④長さ ≤100（Sheets tab 上限）。**検証通過** → label 採用；**不通過／欠落** → `customer_id` 単独へ縮退＋警告 print（顧客分離の担保は posting_id 照合済み job 文書由来の `customer_id`——label は可読性装飾なので fail-closed 停止はしない）。文字列の推測・整形は SS 側で行わない。
3. **customer_id 欠落（契約 §2 違反＝奇形 job・Codex #3 採納）**：ダウンロード前に**冪等 alert**（`reporter.write_alert(file_id, {kind: "customer_metadata_missing", file_id, posting_id})`、文書 ID＝file_id で天然冪等）＋print 一行＋処理スキップ・ファイル保持。SS は毎輪 job を再読するため、控制面が job 文書を修復すれば次輪で自然回復（終端性）。既設の停滞検測（§5.3）は後詰めの網として残る。
4. **sheets_output**：`SheetsOutputWriter.__init__` に `tab_namer: Callable[[str, Any], str] | None = None` を注入（既定＝現行 `_tab_name`）。4 箇所の呼出を `self._tab_namer(...)` へ。**ambient な `config.headless_mode()` 参照を sheets_output に持ち込まない**（golden replay・UI 経路の無影響を構造で保証、F12）。
5. **main の writer 構築点**（`main.py:1643` 付近）：headless 時のみ `tab_namer=lambda owner, doc_type: owner`（後缀無し＝doc_type 跨ぎで 1 社 1 tab、契約 §5.1-d 字面）。
6. **owner の貫通（Codex #9 採納）**：`process_file` → `_process_file_impl` → `_process_file_headless` → `_classify_and_flush_page` → `_flush_page` の貫通引数は一貫して **`tab_owner`** と命名（UI 経路は従来どおり `uploader_name` を渡す＝零改動）。writer 公開署名（`employee_name`）は不改名＋docstring に「headless では顧客 tab キー」と注記。`result["uploader"]` は従来どおり実投稿者（行内容と tab 識別の分離、F5）。
- 監査タブ＝全社共通 `_除外ページ監査` のまま（`_` 始まり assert 不変）。
- ledger の `sheet_tab`/witness は `page_write.tab_name` 経由で自動追従（§5.1-d 連帯）。
- **並行前提（Codex #4 修改採納＝記載のみ）**：peek→append 間無他書込の witness 前提は **ADR-007（単進程単線程・逐件逐頁順次処理）** が担保する（`posting_ledger.py:169` 既明文）。従業員 tab も今日すでに複数ファイル共有であり顧客集約は並行モデルを変えない。この前提を運用注記として本 DoD に固定（锁は不実装）。
- **DoD**（工単 DoD 転記＋Codex #8 補強）：①同一顧客・別投稿者の 2 ファイル → 同一 tab へ集約 ②tab 名が `20220401　株式会社緒方材木店`（全角空格）と完全一致 ③label 欠落/不正 → `customer_id` 単独 tab＋警告 ④customer_id も欠落 → 書込ゼロ・冪等 alert・ファイル保持 ⑤UI 版：既存従業員 tab テスト無改動緑 ⑥後方互換不要（Phase-1 運用切替・工単明文）⑦複数 doc_type が同一 tab へ集約 ⑧ledger doc の `sheet_tab`＝新 tab 名・witness probe が新 tab 名で照合されることの断言。

### T5（§5.1-b 裁定-5）：末尾段 envelope_filter 有効化（headless のみ）
- `ocr_engine.py:2221` を `_yield_page_results(doc_type, raw_data, ocr_text, ocr_conf, envelope_filter=config.headless_mode())` へ。周辺コメント（:1896-1899, :2218-2220 の「尾段は非適用」記述）を v0.15 裁定-5 に合わせ更新。
- UI 版＝`HEADLESS_MODE` 未設定 → False のまま（零改動）。
- **DoD**：`test_ocr_engine_envelope.py` へ追記——①headless・単頁・RECEIPT・entries 空・封筒テキスト → `_excluded_page` yield（監査行き）②headless・entries 有効＋封筒シグナル → `_audit_signal` で記帳継続（不変式「entries 否決禁止」維持）③非 headless・同入力 → 従来（認識不能占位）④非 RECEIPT は headless でも判定対象外。
- **統合検収（Codex #7 採納）**：末尾段を **`process_pipeline` 経由**で通す headless 統合テストを追加（単頁 PDF/画像・OCR/Gemini は既存様式の fake で遮断）——四点断言＝`audit_rows == 1`／MF への append 零（`append_calls==0`・`placeholder_calls==[]`）／`page_outcomes` に `EXCLUDED`／同入力を UI 経路に流すと占位行。

### T6：契約 v0.16 升版（胶水仓 `contracts/job-state-machine.md`・md のみ）
- **事前授権済分**（修復 Plan「S 系実施時に升版」）：§5.5 に補注——`sheet_row_range`＝`[start, end]` 数組形態／文書 ID＝`page_id`、**字段としては重複させない**／SS 実装が `page`・`written_at` 正名へ対斉した事実。§5.6 にも「文書 ID＝page_id 不重複入字段」同補注。
- **新規登録分（決定ではなく TBD 登録）**：§9 に **TBD-8**＝「§5.1-d tab 名 `顧客番号_顧客名` の SS 取得通道が契約に未定義（§2 job 文書に顧客名が無い）。暫定＝SS は job 文書の任意字段 `customer_label`（＝app 94 `顧客番号_顧客名` 全文）を検証付きで優先読取・無ければ `customer_id` 単独へ縮退——**この縮退期間は §5.1-d の明示的例外状態**であり、完全達成の移行条件＝控制面が `customer_label`（§2 字段新設）を書き始めること（趙拍板待ち）。SS 側は拍板後零改動で完全形へ移行する」。**§2 字段表は書き換えない**（代拍禁止）。
- 冒頭状態行・§10 に v0.16 条を追記（履歴無傷）。
- **DoD**：契約 diff が上記のみ（§2 表・状態機・他節に触れない）。git diff を証拠包へ。

### T7：収官閘門（fatboyslim Phase 3/4）
- 改動関連テスト緑 → `/simcodex`（既定輪数）→ 辯論裁決 → 全量テスト：`venv311/bin/python -m unittest discover -p "test_*.py"`。
- **golden 回帰（Codex #8 修改採納＝証拠請求の限定）**：実施**前**に現 HEAD で replay 産物を採取（基線）→ 実施後同一 fixture・同一命令で再実行 → **diff 零**。golden が証明するのは **UI 経路（既定 tab_namer）の無影響のみ**——T4（headless tab 改造）の成功証拠は T4 DoD ①②⑦⑧の専用テストが担う。headless 用 golden fixture 族の新設はしない（単体＋統合で覆えるものへの二重投資）。台帳キー変更（T1/T2）は Sheets 産物外なので golden diff 零が期待値。非零なら全件説明を趙報告へ。
- 証拠包：テスト出力摘要／`git diff HEAD --stat`（両倉）／二段評審辯論摘要／遺留 P2 清単。

## 3. 検収基準（脚本化判定優先）
1. 全量 unittest 緑（現基線 761 件＋新規、退行ゼロ）。
2. T3 構造テストが S1/S2 前コードで RED だった実行ログ。
3. golden replay diff 零。
4. `grep -rn '"page_num"' posting_ledger.py` 零命中／`grep -n 'written_at' posting_ledger.py` が claim/confirm 両書込点に存在。
5. UI 経路テスト（従業員 tab・start_new_file 系）無改動緑。
6. /simcodex 全緑＋辯論記録。

## 4. テスト戦略
- TDD（先 RED 後 GREEN）。単元＝posting_ledger（fake_firestore）・sheets_output（fake worksheet）・ocr_engine（`_yield_page_results` 直叩き＋`config.headless_mode` を patch）。
- 統合＝`test_headless_loop_wiring.py` 様式で intake→tab_owner→writer の貫通を 1 本。
- E2E 相当＝golden replay（Sheets 産物）＋既存 `test_process_file_headless`/`test_pipeline_consumers` の無退行。
- 真 Firestore・真 Sheets には接続しない（従来方針）。

## 5. 影響面
- SS 仓：`posting_ledger.py` / `intake_guard.py` / `main.py` / `sheets_output.py` / `ocr_engine.py` ＋対応テスト。
- 胶水仓：`contracts/job-state-machine.md` のみ（md、コード零）。
- 発布門檻（修復 Plan）：S1/S2 落地＋四步取証まで両倉連調・投産禁止——本 session 終了時点でも**連調はまだ禁止**（四步取証は趙の跨倉 session）。
- 生産（main 分支）：無影響（全改動 headless 分支）。

## 6. リスクと回退
- **R1 tab 注入の UI 汚染**：注入既定値＝現行関数・headless 分岐外に新構築なし＋UI テスト無改動緑を歯に。回退＝writer 構築点の 1 行。
- **R2 payload 正名の取りこぼし**（`page_num` の同名別概念が管線に多数）：grep 全命中の逐一分別を worker 指示に明記。誤変更は T3 構造テスト＋既存管線テストで検出。
- **R3 末尾段 envelope の誤発火**（UI へ漏れる）：`config.headless_mode()` ガード＋非 headless 否定対照テスト。
- **R4 契約の代拍**：§2 表不触・TBD 登録のみで運用（§9 は open question の正規置場）。
- 回退はいずれもファイル単位 revert で完結（スキーマ移行なし・生産未投）。

## 7. 契約義務確認（§6 SS 行・本 session 回答）
| 義務 | 状態 | 証拠 |
|---|---|---|
| ①§5.6 頁処理結果台帳 | ✅ 済（B7-2） | `firestore_progress.py`・commit `b9e09a3`・跨倉抽査「契約と完全一致」 |
| ②§5.7 provider 事件＋帰因是正 | ❌ 未・**本 session 留档** | 修復 Plan 缺口 G-a（両側未実装）・§8 参照 |
| ③§5.1-d tab 名 | 本 session T4 | — |
| ④末尾段 envelope_filter | 本 session T5（IP-401 では**未充足**——現行は逐頁ループのみ、F9 実証） | — |
| ⑤全頁除外→SUCCESS | ✅ 済（P0-10） | commit `a886634`・F13 |
| （付随）§5.5 字段対斉 S1-S3 | 本 session T1-T3 | — |
| （付随）P1-9 テスト歯型化 | ✅ 済（P0-10 と同時消化） | F13 |

## 8. 遺留清単（明確留档）
- **§5.7 provider 事件**：①例外帰因是正（`_route_ocr_strategy` 周辺の広域捕捉が Gemini 例外を PaddleOCR 失敗と誤帰因し得る）②`provider_events/{event_id}` 書込＋控制面断路器接線。両側未実装（缺口 G-a）。event_id 採番・保持期間は §9-1（U7 校准）待ち。**着手条件**＝識別段 try/except の帰因分離を先行（誤 provider 計数は断路器誤作動）。
- P1-12：末尾段の頁カバレッジ哨戒（影響限定・FAILED でファイル保持）。
- ~~TBD-8：`customer_label` の控制面書込（§2 字段新設）＝趙拍板待ち。~~ → **裁定済（2026-08-01 趙・収官対話で即日閉）**＝契約 §2 `customer_label` 正式新設（「入口で分かっている出所は header の如く job に随伴させよ」）。**残＝控制面側の転記実装のみ**（job 建立時に app 94 から書く・控制面 session）。落地まで headless tab は `customer_id` 単独形で動く（SS 側は零改動で完全形へ移行）。
- 缺口 G-b：`alerts/{file_id}` の控制面読取端ゼロ（修復 Plan 既登録・控制面側）。

## 附録 A：評審辯論記録（2026-08-01・Codex 初審 9 条 → 裁決 → 複審「9 件すべて受諾・再論証なし・新規重大指摘なし」）

| # | 嚴重度 | 指摘要旨 | 裁決 | 理由・反映先 |
|---|---|---|---|---|
| 1 | 重大 | 暫定実装（customer_id 単独 tab）を契約達成扱いにしている | **修改採納** | T4 冒頭に「§5.1-d 例外状態」明記＋TBD-8 に移行条件。代替案「従業員 tab 維持で未有効化」は駁回——従業員軸は趙が明示に殺した前提＝より重い偏離。発布門檻内で運用被害なし |
| 2 | 重大 | customer_label 無検証採用は顧客分離を壊す | **修改採納** | 検証 4 条件採用（T4-2）。fail-closed 停止は駁回——分離担保は customer_id、label は装飾。不正 label は番号単独へ縮退＝他顧客 tab へ書くリスク消滅・管線継続 |
| 3 | 高 | customer_id 欠落時の観測性・終端性不足 | **採納** | 冪等 alert `customer_metadata_missing`＋保持スキップ（T4-3）。毎輪 job 再読で控制面修復後に自然回復 |
| 4 | 高 | tab 集約が witness の並行前提を危うくする | **修改採納（記載のみ）** | ADR-007 単進程単線程が現行明文前提（posting_ledger.py:169）・従業員 tab も既に複数ファイル共有＝新規リスクでない。锁実装は過剰設計として駁回、前提の DoD 明記のみ |
| 5 | 高 | 既存 in-flight 文書の移行・互換欠如 | **駁回（Codex 受諾）** | 生産未投・Firestore＝テストデータのみ（修復 Plan §1 で既裁定「迁移成本≈0」）。跨版本 in-flight は発布門檻により発生せず、旧形 doc は控制面 DoD-1e fail-fast が設計どおり捕捉 |
| 6 | 中 | 構造テストがキー存在しか見ない | **採納** | T3 を状態別 schema テストへ格上げ（型・値域・相関） |
| 7 | 中 | T5 が単体のみで headless 実経路を未検収 | **採納** | process_pipeline 経由の統合テスト＋四点断言を T5 に追加 |
| 8 | 中 | golden diff 零は T4 の証拠にならない | **修改採納** | golden＝UI 無影響の歯に限定（T7）。T4 証拠は専用テスト（DoD ⑦⑧追加）。headless golden fixture 族新設は駁回（二重投資） |
| 9 | 低 | owner 抽象の曖昧さ | **採納（軽量形）** | 貫通引数を一貫 `tab_owner` 命名＋writer docstring 注記。writer 公開署名の改名は不採用（UI churn 回避） |

勝敗実績：Codex 勝（全面/部分採納）＝#2 検証・#3・#6・#7・#8 の証拠限定・#9／我方勝（維持）＝#1 の縮退方針・#2 の非停止・#4 の锁不要・#5 全体。全採納でも全駁回でもない＝裁決機能の健全性確認。

## 附録 B：実施記録（2026-08-01・収官）

- **実装**：T1-T5＝Sonnet worker（TDD・T3 は S1/S2 前 RED 実証）；T6 契約 v0.16＝裁決層直行（diff 5 hunk・§2 表/状態機不触）。worker 偏差 3 件（tab_owner 可選形参／800 行超過檔不拆／署名連帯のテスト構築点修正）＝全て承認。
- **/simcodex**：2 輪 early-exit。
  - R1 simplify（4 視角並行）：P1×4 全修＝①customer alert の毎輪 Firestore 打を memo 節流（効率・層位同点）②tab_owner fallback を夹具層（run_headless）へ下沉、生産経路は厳格透伝③`_tab_namer` をクラス属性化（`__new__` 旁路の手動補設 3 点を撤去）④UI 側統合テストの死分岐＋内聯 `_UiWriter` 重複を共有 FakeWriter へ統一。P2×6 緩議。
  - R1 codex：P1「fallback 復活」＝**修改採納**（復活ではなく `_process_file_headless` 入口 fail-fast ValueError——None tab への静默記帳を大声拒否）；P2「非 str customer_id で TypeError」＝採納（`_resolve_tab_owner` 型縮退）。
  - R2 simplify：効率＝clean（節流実装の熱路径零追加を逐条検証）；復用 P2×2・簡化 P2×1 緩議；P3 docstring 矛盾＝即修；層位 P2「customer 型防御は intake 境界へ」＝採納（`_str_or_none`・`resolve_posting_id` 先例同型・縦深防御維持）。
  - R2 codex：P0/P1 零。P2「Sheets 禁字」＝緩議＋標疑（`/` 禁止は Excel 規則、Google Sheets では合法の公算——U14 真庫聯調で実証、受限なら検証 4 条件に白名単を足すだけ・縮退機制既設）。
- **検収**：全量 813 tests OK（基線 761＋52 新規）／golden replay diff 零（30 産物・実施前基線と逐字節一致＝UI 経路無影響の歯）／T3 RED 実証ログ＝worker 報告に記録。
- **遺留（P2・順手で収める）**：test_intake_guard 鏡像類統合／`_probe_const` 第 5 份／written_at 重複用例／test_drive_functions 注釈重複／envelope テスト fixture の跨檔重複 2 件／3.5 步驟の節流塊 helper 抽出／customer_meta_alerted の形状統一＋剪枝（毎日 02:00 再起動が寿命を封頂）／`_TAB_LABEL_MAX_LEN` の置場／Sheets 禁字実証（U14）。
