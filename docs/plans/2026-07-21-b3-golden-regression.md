# B3 黄金様本回帰 — 定稿 Plan §4.5 の実行計画

> 上流権威＝`~/Documents/サンデヴィスタン/docs/impl/03-b3-detail-plan.md` §4 受入基準 第5項（黄金様本回帰）。
> 対象改動＝`396dbc7`（IP-304 B3 頁級 posting_id 派生＋硬去重）。改動前基線＝`0d304f0`。
> fatboyslim Phase 1 産物（本 session、2026-07-21）。Plan 定稿前に一行も実装しない。
> 趙拍板済（本 session）：**「両者都做」**（決定性 replay diff ＋ 改動後の真端到端 sanity）／
> 真 Sheets 書込先＝**生産表の臨時 tab、跑完手工削除**。

## 0. 目標と非目標

### 目標
- **G1**：B3 が UI 版書账管線（`append_entries`）に回帰を持ち込んでいないことを、Gemini 抖動ゼロの**決定性 diff** で実証する（Plan §6 R2 の実証、既存単測より強い証拠）。
- **G2**：headless 頁級経路（`build_page_write`＋`commit_page`）が同一入力に対し UI 版と**同一の 28 列 rows と高亮**を産出することを実証する（Plan §6 R1 高亮偏移の実データ検証。単測 T2-1 は合成 2 票、本項は真 Gemini 出力 6 頁）。
- **G3**：改動後コードで真 Gemini＋真 Sheets の端到端が通ることを sanity 確認する（統合層の断線・認証・容量拡張など単測が届かない層）。
- **G4**：capture / replay を**再利用可能な資産**として残す（B4 以降の IP でも同じ黄金様本 diff が回せる）。

### 非目標（範囲釘死）
- headless 経路の**真** E2E（Firestore＋Drive＋intake_guard 実接続）。契約上 U14／IP-308（B4）待ち。本批は headless を **fake Sheets での replay 等価性**までで止める。
- 実装コードの改修。本批で `sheets_output.py` / `main.py` / `posting_ledger.py` 等の**プロダクションコードは一行も変えない**。回帰が見つかった場合は Plan を止めて趙へ報告（修正は別 IP／別ラウンド）。
- 単頁 PNG 14 枚の**真端到端**（quota）。A 段 replay では PNG も対象に含めるが、B 段真端到端は 6 頁 PDF のみ（quota 節約、趙の「跑完手工削」前提で書込量も抑える）。
- 精度評価（OCR がどれだけ正しく読めたか）。本批は**回帰**（改動前後の一致）のみを見る。正誤判断は §4.5 の射程外。

## 1. 現状事実（コード核験済み）

| # | 事実 | 出典 |
|---|---|---|
| F1 | `local_test.py` は UI 経路（`append_entries` を result 毎に呼ぶ）。headless 頁級経路は通らない。 | `local_test.py:126,150` |
| F2 | `local_test.py` は `OUTPUT_SPREADSHEET_ID`（＝生産表）へ直書き。tab は `employee_name="LocalTest"` 起点。 | `local_test.py:199,126` |
| F3 | `process_pipeline` の yield＝`{result, page_num, total_pages, page_bytes}`。`page_bytes` は分割 PDF アップロード専用で、書账管線は使わない。 | `main.py:387`、`local_test.py:92-94` |
| F4 | `build_page_write(employee_name, doc_type, results, source_urls, start_txn_no) -> PageWrite`（純データ・ws 非接触）。`PageWrite = {tab_name, rows, highlight_ops, txn_range}`。 | `sheets_output.py:463-492` |
| F5 | `commit_page(page_write) -> (start_row, end_row)`。`_get_or_create_tab` → `get_all_values` → `_ensure_row_capacity` → `_write_with_retry` → 白リセット → 高亮 op 適用 → `_sanitize_trailing_once`。 | `sheets_output.py:494-536` |
| F6 | `append_entries(employee_name, doc_type, entries_data, source_url)` が UI 経路の 1 result 書込。 | `sheets_output.py:579` |
| F7 | 既存 `test_sheets_output` は `SheetsOutputWriter.__new__` ＋ `_tab_next_txn`/`_tabs_sanitized` の手動注入＋ `_FakeWorksheet` で gspread 認証を回避している。同手法を replay ハーネスに流用可能。 | `test_sheets_output.py:118-134` |
| F8 | 黄金様本＝`~/Desktop/井戸会計事務所/任務3/税区分テスト/`：`領収書_税区分テスト_6パターン.pdf` ＋ 単頁 PNG 14 枚。実在核験済。 | 本 session `ls` |
| F9 | 基線 `0d304f0` には `build_page_write`/`commit_page` は存在しない（B3 で新設）。UI 経路 `append_entries` は両 commit に存在。 | `git log` ＋ §5 影響面 |

## 2. アーキテクチャ決定

### D1. capture / replay の二段分離（Gemini 抖動の遮断）
真 Gemini は非決定性のため「改動前に真跑 → 改動後に真跑 → diff」では**回帰と模型抖動が判別不能**。そこで：

- **capture（1 回だけ真 Gemini）**：`process_pipeline` を回し、各 yield の `result`（`page_bytes` を除く）を JSON fixture へ落とす。
- **replay（何度でも決定性）**：fixture を入力に、書账管線（`append_entries` / `build_page_write`+`commit_page`）だけを fake worksheet 上で駆動し、産出物（rows＋高亮 op）を正規化 JSON で出力。

replay は Gemini も Sheets も叩かないため、**同じ fixture に対し完全再現**。これが決定性 diff の基盤。

> capture は `396dbc7`（現 HEAD）で 1 回だけ実行する。fixture は「Gemini の出力」であってコードの産物ではないため、基線側で取り直す必要はない（基線と HEAD で `process_pipeline` に差分が無いことを §3 T0 で確認する。差分があれば本前提は崩れ、Plan を止めて趙へ報告）。

### D2. 三本の diff（何を証明するか）

| diff | 左 | 右 | 期待 | 証明対象 |
|---|---|---|---|---|
| **DIFF-A** | `0d304f0` UI 経路 replay | `396dbc7` UI 経路 replay | **完全一致（バイト等価）** | G1：UI 版ゼロ回帰（R2） |
| **DIFF-B** | `396dbc7` UI 経路 replay | `396dbc7` headless 頁級経路 replay | rows 完全一致／高亮 op 集合一致 | G2：頁級経路の等価性・高亮偏移なし（R1） |
| **DIFF-C** | （なし・目視） | `396dbc7` 真端到端の実 Sheets 出力 | 単票が読め、行が着地し、色が載る | G3：統合層 sanity |

DIFF-B で**不一致が出る正当なケース**を事前に釘死しておく（出たら回帰ではない、と後付けで言い訳しないため）：
- **占位行の経路差**：UI は `_write_unrecognized_row`、頁級は `_build_unrecognized_block` で rows の普通一行として集約（定稿 Plan D2'・#9）。行内容は共用ビルダで同一のはずだが、**書込単位**が違う。→ 行内容が一致すれば OK、と判定基準に明記。
- **取引No の採番順**：どちらも票毎に +1、頁内順序も同じ。ずれたら**回帰**（言い訳しない）。
- 上記以外のあらゆる不一致は**回帰として扱い、Plan を止めて趙へ報告**する。

### D3. 基線側 replay の実行方式＝`git worktree`
基線 `0d304f0` には replay スクリプトが無い。`git stash`／`checkout` で作業樹を往復させるのは事故源（未追跡ファイル・venv・fixture の巻き込み）。→ **`git worktree add` で `0d304f0` を別ディレクトリへ検出**し、replay スクリプトをそこへコピーして実行する。
- Python は仓根の `venv311/bin/python` を**絶対パスで**呼ぶ（worktree に venv は無い）。`sys.path[0]` はスクリプト位置＝worktree なので、import されるのは基線コード。これを replay スクリプト自身が `sheets_output.__file__` を出力して**自己証明**する（取り違え事故防止）。
- 検証後 `git worktree remove`。

### D4. 正規化出力フォーマット（diff の土俵）
replay の出力は以下の JSON（キー順固定・`ensure_ascii=False`・indent=2）。**浮動要素は入れない**（時刻・実行時間・オブジェクト id 等は禁）。

```
{
  "source": "<fixture 名>",
  "path": "ui" | "page",
  "code_module": "<sheets_output.__file__ の実パス>",   # 自己証明（diff 対象外・別枠出力）
  "tabs": {
    "<tab 名>": {
      "rows": [[28 列の文字列...], ...],       # append された順
      "highlights": [ {"cell": "I7", "severity": "high"}, ... ]   # 適用順
    }
  }
}
```
- `code_module` は diff の**対象外**（パスが worktree と本体で必ず違うため）。別枠に出して人が目で確認する。
- 高亮は「白リセット」を含めない（両経路とも全新規行に一律で載る背景処理であり、意味的差分にならない）。ただし**リセット範囲**は別途 `reset_ranges` として記録し、DIFF-B の参考情報にする。

### D5. 真端到端（B 段）の副作用制御
- 書込先＝生産 `OUTPUT_SPREADSHEET_ID` の tab `LocalTest_<領収書後綴>`（`local_test.py` が `employee_name="LocalTest"` 固定のため、**既存の従業員 tab は一切汚さない**）。
- 実行前に当該 tab の存否を確認し、**実行後に趙が手工削除**する（削除は趙の手。自動削除はしない＝生産表への破壊操作を code に持たせない）。
- `test_images/receipt/` へ 6 頁 PDF を**コピー**（原本は Desktop に残す）。`local_test.py` は成功時 `processed/` へ `shutil.move` するため、原本を置かない。
- Gemini quota 消費＝6 頁 × 1 回。

## 3. タスク清単（最小可検証単元・各 DoD 付き）

### T0 前提検証（実装前・5 分）
- `git diff 0d304f0 396dbc7 --stat` で改動ファイル一覧を取り、**`ocr_engine.py` に差分が無い**ことを確認（D1 の「fixture は基線でも同じ」前提）。
- 差分があった場合 → 本 Plan の D1 が崩れる。**停止して趙へ報告**（capture を基線側でも取る等の再設計が要る）。
- **DoD**：差分一覧を証拠として貼付、`ocr_engine.py` 不在を確認。

### T1 replay ハーネス `golden_replay.py`（新規・TDD）
書账管線を fake worksheet 上で駆動し、D4 の正規化 JSON を出す。**基線でも動く**ことが必須要件（＝`build_page_write` 等の新 API は `path="page"` の時だけ触る）。

- 実装：
  - `FakeWorksheet`：`get_all_values` / `append_rows` / `title`。`format_cell_range`・`_format_with_retry` は monkeypatch で捕獲（既存 `test_sheets_output` の call_log 手法を踏襲）。
  - `make_offline_writer()`：`SheetsOutputWriter.__new__` ＋ 属性注入（F7）。`_get_or_create_tab` を fake 返却へ差替。
  - `replay_ui(fixture, doc_type)` / `replay_page(fixture, doc_type)`。
  - `normalize(capture) -> dict`（D4 のフォーマット、キー順固定）。
- TDD（先 RED、`test_golden_replay.py`）：
  1. `normalize` が同一入力に対し**バイト等価な JSON** を返す（決定性の釘死）。
  2. `FakeWorksheet.append_rows` が複数回呼ばれた時、`rows` が呼出順に連結される。
  3. 高亮捕獲が `{cell, severity}` へ正規化される（`_severity_color` の RGB へ落とさない＝色定義変更に脆くしない）。
  4. `replay_page` が単一 `append_rows` 呼出に集約する（頁原子の再確認）。
  5. `replay_ui` は `build_page_write` を**呼ばない**（基線互換の担保。存在しない API を触らない）。
- **DoD**：上記 5 本緑（venv311）。カバレッジ 80%+（全局 §9）。

### T2 capture スクリプト `golden_capture.py`（新規・TDD）
`process_pipeline` を回して fixture JSON を落とす。

- 実装：`capture(path, doc_type, strategy) -> list[dict]`（`page_bytes` を除去、`page_num`/`total_pages`/`result` のみ）。`--out` で保存先指定。
- TDD（`test_golden_capture.py`、`process_pipeline` は monkeypatch）：
  1. `page_bytes` が fixture に**含まれない**（サイズ膨張と非決定性の排除）。
  2. yield 順（`page_num` 昇順・同頁複数票の連続）が保存順として保たれる。
  3. `_page_error` 頁も記録される（replay 側で UI と同じくスキップ扱いされることを T1 で担保）。
- **DoD**：上記緑。真 Gemini は**呼ばない**（monkeypatch）。

### T3 fixture 取得（真 Gemini・1 回・quota 消費）
- `golden_capture.py` で 6 頁 PDF ＋ 単頁 PNG 14 枚を capture（strategy＝既定 C）。
- 保存先＝`golden/`（**gitignore 追加**。合成票で脱敏済とはいえ、顧客ディレクトリ由来の生データを既定で仓へ入れない）。
- **DoD**：fixture が 6 頁ぶん（＋PNG 14 件）存在し、各 result に `entries` か `_unrecognized` か `_page_error` のいずれかが入っている。頁数不一致なら停止して報告。

### T4 DIFF-A（UI 版ゼロ回帰）
- `git worktree add` で `0d304f0` を検出（D3）。`golden_replay.py`＋`golden_capture.py`＋fixture をコピー。
- 両側で `replay_ui` を実行 → 正規化 JSON を `diff`。
- **DoD**：**完全一致（diff 空）**。1 バイトでも違えば回帰候補として停止・趙へ報告。`code_module` 出力で基線/HEAD の取り違えが無いことを証拠に含める。

### T5 DIFF-B（頁級経路の等価性・高亮偏移）
- HEAD 側で `replay_ui` と `replay_page` を実行し比較。
- 判定：
  - `rows`：**完全一致**（占位行を含む。書込単位の差は正規化後の行列には現れない）。
  - `highlights`：セル参照と severity の**集合として一致**（適用順は塗り順仕様に従うため順序差は許容、ただし差が出たら理由を明記）。
- **DoD**：上記合致、または D2 で釘死した「正当な不一致」のみ。それ以外は回帰として停止・報告。

### T6 DIFF-C（真端到端 sanity）
- `396dbc7` で `local_test.py --only-file` により 6 頁 PDF を 1 本だけ実行、生産表 `LocalTest_*` tab へ書込。
- 確認項目：6 頁ぶんの行が着地／税区分 6 パターンが 28 列の該当列に入っている／高亮が載る／`_ensure_row_capacity` で例外が出ない／取引No が連番。
- fixture（T3）と実出力を突き合わせ、**capture 時と同じ内容が Sheets に載っている**ことを確認（capture→replay の忠実性の実証）。
- **DoD**：例外ゼロで完走、上記目視項目クリア、行ダンプを証拠に添付。**tab 削除は趙へ依頼**（自動削除しない）。

### T7 証拠パッケージ
- 三 diff の結果、実行コマンド、fixture 統計、全量テスト出力、`git diff HEAD --stat` を一枚にまとめて報告。

## 4. 受入基準（脚本化判定優先）
1. `venv311/bin/python -m unittest test_golden_replay test_golden_capture -v` 全緑。
2. `venv311/bin/python -m unittest discover -p "test_*.py"` 全緑（既存 406 本＋新規、回帰ゼロ）。
3. **DIFF-A が空**（UI 版ゼロ回帰）。
4. **DIFF-B が rows 完全一致・highlights 集合一致**（頁級等価）。
5. **DIFF-C が例外ゼロ完走**＋目視項目クリア。
6. 新規コード カバレッジ 80%+。
7. プロダクションコード（`sheets_output.py`/`main.py`/`posting_ledger.py`/`intake_guard.py`/`ocr_engine.py`）の diff が**ゼロ**（本批は検証のみ）。

## 5. 影響面
- **新規**：`golden_capture.py`、`golden_replay.py`、`test_golden_capture.py`、`test_golden_replay.py`、`.gitignore` に `golden/` 追加、本 Plan 文書。
- **改修**：なし（プロダクションコード不動＝受入基準 7）。
- **外部副作用**：
  - Gemini API quota：6 頁 × 2 回（T3 capture ＋ T6 端到端）＋ PNG 14 枚 × 1 回（T3 のみ）。
  - 生産 Spreadsheet：`LocalTest_*` tab に約 6〜10 行が着地（T6）。**趙が手工削除**。既存従業員 tab は不変。
  - ローカル：`test_images/receipt/` へ PDF をコピー→`processed/` へ移動（いずれも gitignore 配下）。
- **不動**：Drive、Firestore、`main.py` の headless 経路（真接続せず）、`daily_backup.gs`。

## 6. リスクと回退
- **R1 fixture の代表性不足**：一度の Gemini 出力が特定分岐（占位行・同頁多票・高額異常）を含まない可能性。→ 本批の目的は「同入力での前後一致」であり網羅ではない。網羅は単測 T2-1/T3（B3 で緑）が担う。fixture が薄い分岐は §7 に明記して単測へ委ねる。
- **R2 基線 worktree の取り違え**：worktree 側で誤って HEAD のコードを import すると DIFF-A が偽陰性（常に一致）になる。→ `code_module` の自己証明出力を**必ず**確認。加えて基線側で `build_page_write` の**不在**を assert する（存在したら worktree が誤り）。
- **R3 生産表汚染**：T6 の書込が消し忘れられる。→ tab 名を `LocalTest_*` に隔離し、報告書の冒頭に削除依頼を明記。
- **R4 quota 消費の空振り**：T3 で fixture を取った後に replay 側の不備が発覚し取り直し。→ **T1/T2 を先に完成・単測緑にしてから T3 を実行**（順序を DoD 化）。
- **R5 diff が赤（真の回帰発見）**：→ **本 Plan では直さない**。停止して事実（差分の具体行・列・高亮位置）を趙へ報告し、修正は別ラウンドで裁決。本批はプロダクションコード不動が受入基準。
- **回退**：全成果物が新規ファイルのみ。削除すれば完全に元へ戻る。生産表は tab 削除のみ。

## 7. TBD / 開工核実
- fixture を仓へ入れるか（現案＝gitignore）。将来 B4 以降で共有回帰基準にしたいなら別途 `golden/` を追跡対象化する判断が要る（趙拍板事項、本批は据え置き）。
- `LocalTest` tab の後綴実名（`DOC_TYPE_TAB_SUFFIX` の receipt 値）は T6 実行時に実測して報告へ記す。
