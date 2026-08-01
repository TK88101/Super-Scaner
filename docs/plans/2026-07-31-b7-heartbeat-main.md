# B7 第 1 步：頁級心跳フック＋main 側進捗可視化（main 分支）【定稿 v2】

> 工單：サンデヴィスタン `docs/impl/03-batch-plan.md` B7（趙 2026-07-31 拍板・両分支一括・upstream-first）。
> 本 Plan 範圍＝施工順序第 1 步（main 分支）のみ。第 2 步（headless へ merge）・第 3 步（Firestore page_outcomes 接線）は本 session 対象外。
> 可視化形態＝**候補A：Sheets 進捗タブ**（趙 2026-07-31 本 session 拍板）。
> 上游権威：契約 `contracts/job-state-machine.md` v0.15 §5.6（outcome 値域の錨）／ADR `docs/plans/2026-07-30-adr009-drift-repair.md`（サンデヴィスタン倉）§11.2-1（並集過渡・根因対策裁決）。
> **v2＝Codex 対抗評審 13 条の裁決反映版**（採納 9・修改採納 4・駁回 4 点は複審で対抗者受諾＝維持）。辯論記録＝§9。

## 1. 目標と非目標

### 目標
1. **頁級フック（両分支共用の底座）**：`main.process_file` の逐頁ループに「頁を処理し終えるたびに 1 打」の進度フック（reporter 協議＋§5.6 対斉の outcome 四値＋発射時固定の UTC 時刻）を敷く。headless 側は merge 後、reporter 構造期に job 上下文を注入した Firestore reporter を同じ発射点に掛ければ §5.6 書込が成立する形にする。**「共通」の意味＝main/headless 両分支で同一の発射点・同一の事件語彙を共有すること**であり、`local_test.py` 等 `process_pipeline` 直接消費者への接続は範囲外（低13 裁決）。
2. **main 側可視化**：Sheets 進捗タブ `_処理進捗`——1 檔 1 行、節流付き update-in-place（頁進捗・内訳計数・最終心跳時刻）、終局で状態確定。無人 miniPC で「今どこまで進んだか／いつ心跳が絶えたか」を人が実際に見る面（Sheets）に持続化する。
3. **main 自足**：Firestore・headless 特有機件への依存ゼロ。main 単独でテスト緑・単独で稼働。

### 非目標（本 Plan で一切触れない）
- headless への merge・Firestore `page_outcomes` reporter（第 2・3 步）
- 契約 §5.7 provider 事件／P0-10／P1-9／tab 名変更（headless 側残項）
- 記帳路徑の挙動変更：MF 28 列・取引No・PageUrlResolver・監査タブ語義・除外/占位/エラー判定は**一切不変**（`append_entries` への戻り値追加は挙動不変の情報開示のみ、§3.2）
- generator 逐頁流式メモリモデルの変更（CLAUDE.md 硬約束）
- 通知系（Chatwork は死代碼のまま、触らない）
- 監視範囲の拡張：**本機能の可観測範囲＝「頁処理進度」（`process_file` 内）**。下載・`start_new_file` 等の前置失敗は既存可視性（例外→main loop catch→檔滯留→3 秒後重試・console）に委ねる（中10 裁決）

## 2. 現地勘察事実（設計の根拠）

| 事実 | 出典 |
|---|---|
| 逐頁主循環＝`main.process_file`、毎頁の終局は消費側で全部判別可能 | main.py:470-551 |
| 頁終局は 4 種：正常記帳／`_unrecognized` 占位／`_excluded_page` 留痕（成功・失敗）／`_page_error` | main.py:480-526 |
| **`append_entries` は entries>0 でも全行金額 0/None なら内部で占位行に転落**（consumer からは不可視） | sheets_output.py:298-309 |
| 契約 §5.6 outcome 値域＝`POSTED`\|`EXCLUDED`\|`PLACEHOLDER`\|`FAILED`＋`reason`＋`written_at`(UTC) | 契約 v0.15 §5.6 |
| 可視化現状＝console print のみ；IP-401 註釋自身が「控制台にしか出ない哨戒は哨戒でない」と認定 | main.py:553-557 |
| `_` 開頭タブは GAS backupAllTabs_ の削除対象外（監査タブ先例＋import 時大声検査の先例） | sheets_output.py:23-45 |
| 監査タブ機件：header 検査・自己修復・行数自前管理・429 退避 append | sheets_output.py:420-491 |
| `PageUrlResolver.__init__` は純代入・API 呼び出しゼロ（初期化失敗リスク無し） | main.py:132-141 |
| `process_file` 内の未捕捉例外は main loop の包括 catch へ伝播→檔滯留→再試（既存挙動） | main.py:850-852 |
| `local_test.py`/`benchmark_ocr.py` は `process_pipeline` 直接消費で `process_file` を通らない→フック無縁 | local_test.py:93 |
| テスト夾具 `_run_process_file(pages, writer=None)` が pipeline 差替済み→TDD 直掛可 | test_main_process_file.py:81 |
| `writer.spreadsheet` は公開属性（外部参照は現状ゼロ）→ reporter へ handle 貸与可 | sheets_output.py:89 |
| gspread `append_rows` は API 応答（`updates.updatedRange` 含む）を返す→書込行番号は応答から権威的に取得可 | gspread `values_append` 応答仕様 |

## 3. 設計

### 3.1 新モジュール `page_progress.py`（新規、200–400 行目標）

**(a) outcome 常数（契約 §5.6 と一字一句一致——headless 第 3 步でそのまま Firestore へ運ぶ）**

```python
OUTCOME_POSTED = "POSTED"            # 仕訳を MF 区へ実際に記帳した頁
OUTCOME_EXCLUDED = "EXCLUDED"        # 除外頁（監査タブ/MF 提示行に留痕済）
OUTCOME_PLACEHOLDER = "PLACEHOLDER"  # 占位行のみの頁（_unrecognized／有効金額ゼロ）
OUTCOME_FAILED = "FAILED"            # 頁エラー（頁単位では未記帳）
```

**優先序（低11 裁決）**：`_excluded_page` の頁は落地形式が MF 占位行（社保通知書の提示行）でも**恒に `EXCLUDED`**。`PLACEHOLDER` は非除外頁が占位行に終わった場合のみ。回帰テストで固定する。

**(b) machine status 常数（中9 裁決）＋日本語表示映射**

```python
STATUS_PROCESSING = "PROCESSING"                # 表示: 処理中
STATUS_COMPLETED = "COMPLETED"                  # 表示: 完了
STATUS_COMPLETED_COVERAGE_GAP = "COMPLETED_WITH_COVERAGE_GAP"  # 表示: 完了（頁欠落あり）
STATUS_PARTIAL_ERROR = "PARTIAL_ERROR"          # 表示: 部分エラー
STATUS_FAILED_RETAINED = "FAILED_RETAINED"      # 表示: 失敗（ファイル保持）
STATUS_PARSE_FAILED = "PARSE_FAILED"            # 表示: 解析失敗
STATUS_ABORTED = "ABORTED"                      # 表示: 異常終了（例外種別付記）
```

事件・状態の**機械値は英字常数**、Sheets の表示列は日本語映射。headless 第 3 步は機械値をそのまま利用。

**(c) reporter 協議（duck-typing、基底クラス強制なし——repo 風）**

| メソッド | 発射点 | 載荷 |
|---|---|---|
| `file_started(filename, uploader_name, doc_type)` | `process_file` 冒頭（resolver 生成前、中10 裁決） | total_pages は初回頁まで不明→「0/?」表示 |
| `page_done(page_num, total_pages, outcome, reason, occurred_at)` | 毎頁の終局点（§3.2 表） | `occurred_at`＝**発射時に固定した UTC**（高2/中7 裁決——節流で書込が遅延しても事件時刻は不変）；reason＝機械可読英字キー |
| `file_finished(status, error_class=None)` | `process_file` の各 return 直前＋未預期例外時（§3.2） | status＝(b) の機械値 |

`NULL_REPORTER`＝全メソッド no-op のモジュールレベル単例。`process_file(..., progress=None)` は `progress or NULL_REPORTER` で受ける→既存呼出・テスト・他消費者は無変更で従来挙動。

**job 上下文の分層（高2 裁決）**：事件載荷は頁級情報のみ。headless の `job_key` 等は **Firestore reporter の構造期に注入**（reporter は檔単位で構造される）——main 側載荷に死欄位を作らない。

**(d) `SheetsProgressReporter`**

- コンストラクタ＝`SheetsProgressReporter(spreadsheet)`（gspread Spreadsheet handle。`main()` が `writer.spreadsheet` を貸与）
- タブ `_処理進捗`：`_` 開頭必須の import 時 RuntimeError 検査（sheets_output.py:36-45 と同方針）
- HEADERS＝`["開始時刻", "ファイル名", "担当", "文書タイプ", "頁進捗", "POSTED", "EXCLUDED", "PLACEHOLDER", "FAILED", "状態", "最終心跳(JST)"]`（11 列。時刻表示は JST、事件載荷は UTC——中7 裁決）
- タブ取得＝監査タブ先例踏襲：既存タブ header 不一致→**上書きせず reporter 自己無効化**（管線続行）；空タブ→header 自己修復；無ければ add_worksheet
- **行番号＝`append_rows` 応答の `updates.updatedRange` から権威的に解析**（高3 裁決——自前計数の競態を排除）。残余リスク（処理中に人が `_処理進捗` タブへ行挿入）は「機器専有タブ＋単一プロセス部署」前提の文書化で承受（`_audit_row_count` と同級の承受水準）
- **節流（高4 裁決）**：`page_done` は記憶体の計数・心跳時刻を更新するのみ。実際の Sheets update は①前回書込から `PROGRESS_FLUSH_INTERVAL`（20 秒）以上②初頁③`FAILED` 事件④終局（`file_finished`）——のいずれかで実行。定常負荷≦3 writes/min・記帳側と配額競合しない
- **degrade（中5 裁決）**：単発失敗は次の節流 tick で自然再試（記憶体状態は保持）。**連続 3 回失敗で当該檔の途中更新を停止**（console 警告）、ただし `file_finished` は**恒に独立の最終書込を 1 回試みる**——「一時失敗が永久の偽『処理中』を作る」ことを防ぐ
- 書込順序の原則＝**MF が先・進捗が後**（監査タブと同じ「帳簿を人質に取らせない」、main.py:528-531 の既裁思想）；429 リトライは reporter 独自予算（max 2）で記帳側の退避予算を食わない

### 3.2 `main.py` 改修（発射点＋`append_entries` 戻り値）

**(a) `sheets_output.append_entries` に戻り値追加（中8 裁決・挙動不変の情報開示のみ）**：
実際に MF 行を書いた→`"posted"`／占位行に転落（entries 空・全行金額 0/None・`_unrecognized`）→`"placeholder"`。既存呼出は戻り値無視で完全互換。consumer はこれで POSTED/PLACEHOLDER を正確に発射（「entries>0＝POSTED」の誤報を排除）。

**(b) 頁終局の発射表**：

| 頁終局 | 現行コード位置 | 発射 |
|---|---|---|
| `_page_error` | main.py:480-483 | `FAILED, reason="page_error"` |
| 除外・留痕成功 | main.py:491-509 | `EXCLUDED, reason=_exclude_reason`（落地形式不問・恒 EXCLUDED） |
| 除外・留痕失敗（既存語義＝FAILED 扱い） | main.py:495-506 | `FAILED, reason="exclude_record_failed"` |
| `append_entries` 戻り値 `"posted"` | main.py:521-526 | `POSTED, reason=""` |
| `append_entries` 戻り値 `"placeholder"` | 同上 | `PLACEHOLDER, reason="unrecognized"` |

**(c) 檔終局の発射**：
- `file_started`＝`process_file` 冒頭（中10 裁決）
- 既存 4 return 経路：全頁失敗→`FAILED_RETAINED`／部分エラー→`PARTIAL_ERROR`／成功→`COMPLETED`（ただし missing_pages 非空なら `COMPLETED_WITH_COVERAGE_GAP`——中6 裁決、既存の頁カバレッジ突合結果を流用）／count==0→`PARSE_FAILED`
- **未預期例外（高1 裁決）**：ループ〜終局判定全体を `try/except` で包み、未捕捉例外時に `file_finished(STATUS_ABORTED, error_class=型名)` を best-effort 発射して**例外は原様 re-raise**（main loop の既存 catch・檔滯留・再試の語義は一切不変）。以後、進捗タブの残留「処理中」＝**プロセス死のみ**を意味する（区別可能になる）
- ページカバレッジ突合の**欠落頁は `page_done` を発射しない**（頁が来ていない＝心跳が無いのが正しい姿。留痕は既存監査タブ「欠落」行＋終態 `COMPLETED_WITH_COVERAGE_GAP` が担う）
- `main()` 接線：`build_writers` 後に writer ごとに `SheetsProgressReporter(writer.spreadsheet)` を生成（生成失敗は警告のみ・reporter 無しで続行）、`process_file(..., progress=reporter)` を渡す

### 3.3 headless への接続性（第 2・3 步の設計余地確認のみ・本步不実装）

- 事件語彙（outcome 四値・reason・`occurred_at` UTC）は §5.6 の行スキーマ（page/outcome/reason/written_at）へ 1:1 写像——Firestore reporter は**構造期に job 上下文（job_key 等）を注入**して同協議を実装し `jobs/{job_key}/page_outcomes/{page_id}` へ書く
- 複数 reporter 合成（Sheets＋Firestore 並走）は第 3 步で必要になった時に dispatch list 化（YAGNI——協議が揃っていれば機械的拡張）
- main の頁級 PLACEHOLDER と headless IP-306 の頁級占位聚合は語義が別物——写像は第 3 步の現地判断に委ね、本步は main 語義のみ定義

## 4. 任務清單（TDD・各項 DoD 付き）

| # | 任務 | DoD |
|---|---|---|
| T1 | `page_progress.py` 骨格：outcome/status 常数＋`NULL_REPORTER`＋タブ名 `_` 検査 | RED→GREEN：`test_page_progress.py` 新設——outcome 四値＝§5.6 一字一句一致（文字列固定比較）・status 常数存在・NULL_REPORTER 全メソッド no-op・非 `_` タブ名で RuntimeError。import 副作用ゼロ |
| T2 | `SheetsProgressReporter`（fake gspread で TDD） | 単測緑：タブ生成／header 検査（不一致→自己無効化・上書きなし）／空タブ自己修復／`updatedRange` 解析で行番号取得／**節流**（interval 内は書かない・初頁/FAILED/終局は即時）／**degrade**（連続 3 失敗で途中更新停止・file_finished は独立に 1 回試行）／常時失敗注入でも例外が漏れない／**contract-shape 断言**（update 呼出の range・値形状を fake で固定、低12） |
| T3 | `process_file` 発射点＋`append_entries` 戻り値（`test_main_process_file.py`・`test_sheets_output.py` 拡張） | 単測緑：発射表 5 種＋**「entries 有値・全行金額 0」→PLACEHOLDER**（中8）＋**除外頁の MF 落地でも EXCLUDED**（低11）／file_finished 4 経路＋**欠落→COMPLETED_WITH_COVERAGE_GAP**（中6）＋**未預期例外→ABORTED 発射後 re-raise**（高1）／**progress 未指定で既存テスト全部緑（無回帰）** |
| T4 | `main()` 接線 | 接線ヘルパー単測（writer→reporter 生成、生成失敗時は None 続行）；既存テスト全緑 |
| T5 | 収官：全量テスト＋証拠包 | `venv311/bin/python -m unittest discover -p "test_*.py"` 全緑；新規/改動コード覆蓋率 80%+；証拠包提出 |

実施紀律：先に会失敗するテストを書き RED 確認→実装 GREEN→リファクタ。checkpoint は `wip/` 分支のみ（main への正式 commit は趙拍板待ち）。

## 5. 驗收標準（可判定）

1. **進捗記録の正確性**：fake pipeline で 5 頁件（正常 3・除外 1・頁エラー 1）→ fake 進捗タブに「頁進捗 5/5・POSTED=3・EXCLUDED=1・FAILED=1・状態=部分エラー・心跳更新済」
2. **欠落の可視化**：3 頁件で p2 が一度も yield されない → 頁進捗 2/3＋状態＝完了（頁欠落あり）
3. **誤報排除**：entries 有値だが全行金額 0 の頁 → PLACEHOLDER（POSTED でない）
4. **異常終了の区別**：`append_entries` が未預期例外 → ABORTED が best-effort 記録され例外は伝播（檔滯留の既存語義不変）
5. **無回帰**：progress 未指定の全既存テストが一切の変更なしで緑
6. **best-effort 鉄則**：進捗書込を常時失敗させても `process_file` の戻り値・`append_entries`/`append_audit_row` 呼び出し列が完全不変
7. **契約整合**：outcome 四値が契約 §5.6 値域と一字一句一致（テストで文字列比較固定）
8. **節流**：20 秒 interval 内の連続 page_done が 1 回の Sheets 書込に合流（fake の呼出計数で判定）
9. **覆蓋率**：新規/改動コード 80%+

## 6. テスト戦略

- **単体**：T1/T2/T3（unittest 風・venv311。fake gspread＝`test_sheets_output.py` の既存 fake 流儀踏襲＋update/updatedRange 対応）
- **集成**：T3＝fake pipeline×fake writer×reporter の通し（`_run_process_file` 拡張）；T4＝接線ヘルパー
- **覆蓋率量測（低12 裁決）**：`venv311/bin/python -m coverage run --include="page_progress.py,main.py,sheets_output.py" -m unittest discover -p "test_*.py"` → `coverage report` で新規/改動行 80%+ を確認
- **E2E**：**豁免申請**——reporter の実 Sheets 経路は `main()`（実 Drive＋実 Sheets）でのみ発火し、`local_test.py` は `process_pipeline` 直接消費でフック無縁（local_test.py:93）。豁免代替＝部署チェックリストに「実機で 1 件流し `_処理進捗` タブ生成・行更新・終態を目視」を追記。黄金様本回帰は headless 分支の B3 資産（main に harness 無し）＋本步は記帳路徑零変更のため対象外——**merge 後の第 2 步で必跑**
- 既存テストは**一切改変しない**（改変が必要になったら設計が後方互換でない証拠→停手して Plan に戻る）

## 7. 影響面

| 対象 | 変更 |
|---|---|
| `main.py` | `process_file` 可選引数＋発射点＋未預期例外の ABORTED 包装＋`main()` 接線（既存分岐・例外伝播語義不変） |
| `sheets_output.py` | **`append_entries` に戻り値追加のみ**（`"posted"`/`"placeholder"`、±2 行、挙動不変——中8 裁決で「不動」から変更） |
| `page_progress.py` | 新規 |
| `test_page_progress.py` | 新規 |
| `test_main_process_file.py`／`test_sheets_output.py` | テスト追加（既存ケース不改変） |
| `.env`／デプロイ設定／GAS | **変更なし**（タブ名は定数；`_` 開頭で backup/削除対象外） |
| 生産反映 | 既存運用どおり main を git pull＋再起動のみ |

## 8. 風險と回退

| 風險 | 対策 |
|---|---|
| 進捗書込が記帳側の Sheets 配額を圧迫 | 節流（20 秒 interval・定常≦3 writes/min）＋reporter 独自の軽量リトライ（max 2）で記帳側退避予算と分離（高4 裁決） |
| 既存 `_処理進捗` 同名タブとの衝突 | header 検査・不一致は上書きせず reporter 自己無効化（監査タブ先例） |
| reporter 例外が管線へ漏れて記帳を壊す | 全公開メソッド内部 try/except；驗收 6 で常時失敗注入を単測固定 |
| 行番号ズレ（人工の行挿入） | `updatedRange` 権威取得で自算競態を排除；残余は機器専有タブ＋単一プロセス前提を文書化して承受（高3 裁決） |
| 一時的書込失敗が偽「処理中」を残す | 節流 tick での自然再試＋連続 3 失敗まで degrade しない＋file_finished 独立最終書込（中5 裁決） |
| 進捗タブ行の無限蓄積 | 1 檔 1 行（重試は新行＝重試史）。日次数十行規模、当面問題なし；掃除は将来の運用課題として P2 記録 |
| 崩潰時の「処理中」行残留 | ABORTED 発射導入後、残留「処理中」＝**プロセス死のみ**（区別可能・B7 の狙いどおり）。次回重試で新行 |

回退＝`main()` の reporter 生成 1 箇所を外せば完全に現状復帰（フックは no-op に落ちる）。

## 9. 附錄：Codex 對抗評審・辯論記録（2026-07-31・codex-cli 0.145.0）

R1＝Codex 初審 13 条（高4・中6・低3）→ 主 session 逐条裁決 → R2＝駁回 4 点を Codex へ回餵・複審 → **4 点全て対抗者受諾（再提なし）＝駁回維持**。全採納でも全駁回でもない（採納 9・修改採納 4・部分駁回 4 点）。

| # | 嚴重度 | 指摘要旨 | 裁決 | 反映先 |
|---|---|---|---|---|
| 1 | 高 | 未預期例外で file_finished 不発射→偽「処理中」 | **採納（修改形）**：ABORTED 発射＋re-raise、既存例外語義不変 | §3.2(c)・驗收4 |
| 2 | 高 | 協議に job_key／written_at 欠落、「reporter 差替だけ」不成立 | **部分採納**：`occurred_at` UTC を載荷に追加。job_key は駁回——main に job 概念無し、headless reporter 構造期注入が正しい分層（**R2 で Codex 受諾**） | §3.1(c)・§3.3 |
| 3 | 高 | append 後の行番号自算は競態 | **部分採納**：`updatedRange` 権威解析に変更。run_id＋update 前検証読は駁回——毎頁 +1 read は高4 の配額主張と矛盾（**R2 で Codex 受諾**） | §3.1(d)・§8 |
| 4 | 高 | 毎頁同期書込は配額楽観 | **採納**：節流（20s interval・初頁/FAILED/終局即時） | §3.1(d)・驗收8 |
| 5 | 中 | 初回失敗即 degrade は偽警報製造 | **採納（修改形）**：連続 3 失敗まで再試・file_finished 独立最終書込 | §3.1(d) |
| 6 | 中 | 頁欠落なのに終態「完了」の矛盾 | **採納**：`COMPLETED_WITH_COVERAGE_GAP` 新設＋驗收案例 | §3.1(b)・驗收2 |
| 7 | 中 | JST 文字列と UTC written_at の不整合 | **採納（修改形）**：載荷 UTC 発射時固定・表示列 JST。DST 論拠のみ駁回（JST に DST 無し、**R2 で Codex 受諾**） | §3.1(c)(d) |
| 8 | 中 | entries>0 でも全行金額 0→占位転落を POSTED 誤報 | **採納**：`append_entries` 戻り値（"posted"/"placeholder"）で consumer が正確発射（sheets_output.py:298-309 で事実確認済） | §3.2(a)・驗收3 |
| 9 | 中 | 表示文案を資料値に使うな | **採納**：machine status 常数＋日本語映射 | §3.1(b) |
| 10 | 中 | file_started が遅い／main() へ前移せよ | **部分採納**：`process_file` 冒頭へ移動＋監視範囲を「頁処理進度」と明記。main() 前移は駁回——resolver init 純代入（事実確認済）・前置失敗は既存可視性あり・生命週期跨越の耦合増（**R2 で Codex 受諾**） | §1 非目標・§3.2(c) |
| 11 | 低 | EXCLUDED/PLACEHOLDER 優先序が曖昧 | **採納**：優先序明記＋回帰テスト | §3.1(a)・T3 |
| 12 | 低 | 覆蓋率命令未定・fake の update shape 未検証 | **採納**：coverage 命令明記＋contract-shape 断言 | §6・T2 |
| 13 | 低 | 「共通底座」過度宣稱（local_test 未接続） | **採納**：表述を「main.process_file の頁級フック（両分支共用）」へ収斂 | §1 |

### 9.2 Phase 3（/simcodex 3 輪）辯論記録（2026-07-31〜08-01）

checkpoint：`9d00c86`（TDD 本体）→`026d4b7`（R1/R2 反映）→`9683395`（R3 反映）。全輪 verify＝unittest discover 全綠（最終 406 件）。

| 輪 | 審査 | findings | 裁決 |
|---|---|---|---|
| R1 | simplify 4 視角 | 去重後 7 | 採納 4（`rowcol_to_a1` 復用・5 発射点→`_emit` 集約・`APPEND_RESULT_*` 定数化・FAILED 即時 flush は檔内初回のみ〔故障批次の O(頁数) 同期書込防止〕）／駁回 2（JST 定数復用＝底座への依存鏈追加が過大・public/`_impl` の `_guard` 集約＝協議面の顕式性優先）／P2 繰延 2（タブ供給三複本統一・テスト fake 去重） |
| R1 | codex | 1 P2 | 修改採納：頁進捗分子→distinct 頁集合（codex 案の yield 事件数は一頁多票で水増しするため上方修正）＋釘死テスト 2 件 |
| R2 | simplify 4 視角 | 5 | 採納 4（テスト側定数導入＋契約値 pin テスト・`_reset_file_state` 抽出・`_process_file_impl` keyword-only 化・`_emit` 純閉包化）／駁回 1（`_seen_pages` の呼出側統一＝消費者非依存の底座設計を優先、註釈で取捨を文書化） |
| R2 | codex | 1 P2 | **駁回**：除外頁の監査タブ＋MF 退避の双重失敗で `EXCLUDED` 発射は誤報——だが「退避も失敗したらログのみ」は IP-401 既裁設計であり、返 False 化は檔級判定（保持/歸檔）を変える＝本批非目標の記帳路徑変更。複審で codex 受諾（「事件落庫可靠性と頁分類成立を混同していた」）。残欠口は P2 遺留（headless 第 3 步 ESCALATE 語義と合わせ再議） |
| R3 | simplify 4 視角 | 4 | 採納 3（`_last_flush_ts` を `_reset_file_state` へ併入〔将来の早期 return 分岐での節流窓汚染を封じる・零行為変化〕・`build_progress_reporters` 欠落式〔`build_writers` と同構〕・`_run_process_file` に `resolver_side_effect` 追加で ABORTED テストの様板重複解消）／駁回 1（テスト前導の setUp 抽出＝AAA 顕式 arrange の倉庫流儀を優先） |
| R3 | codex | 0 | **全綠**：「reporter 缺席時の挙動保持・終態発射の経路網羅・テスト通過」を明示認可 |

**P2 遺留清單**：①タブ供給 dance 三複本（監査タブ／MF タブ／進捗タブ）の共通化 ②テスト `_FakeSpreadsheet` 二複本の共有化 ③除外頁双重書込失敗の進捗細分（headless 第 3 步で再議）④進捗タブ行の長期蓄積掃除（運用課題）⑤実機 E2E＝部署時に 1 件流して `_処理進捗` タブ生成・行更新・終態を目視（§6 豁免代替）。
