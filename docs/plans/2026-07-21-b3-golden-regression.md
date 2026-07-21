# B3 黄金様本回帰 — 定稿 Plan §4.5 の実行計画（**定稿**・辯論裁決反映済）

> 上流権威＝`~/Documents/サンデヴィスタン/docs/impl/03-b3-detail-plan.md` §4 受入基準 第5項（黄金様本回帰）。
> 対象改動＝`396dbc7`（IP-304 B3 頁級 posting_id 派生＋硬去重）。改動前基線＝`0d304f0`。
> fatboyslim Phase 1 産物（本 session、2026-07-21）。草案＝`80f0077`、本稿＝Codex 対抗評審 10 条＋複審 1 輪の裁決反映版（§8 附録）。
> 趙拍板済：**「両者都做」**（決定性 replay diff ＋ 改動後の真端到端 smoke）／真 Sheets 書込先＝**生産表の臨時 tab、跑完手工削除**。

## 0. 目標と非目標

### 目標（**証明できる範囲だけを主張する**——裁決#1で収窄）
- **G1**：B3 が **UI 版書账層**（`append_entries`）に回帰を持ち込んでいないことを、**固定 OCR result 入力下の決定性 diff** で実証する（Plan §6 R2 の実証）。
- **G2**：headless 頁級経路（`build_page_write`＋`commit_page`）が同一入力・**空タブ条件下**で UI 版と同一の 28 列 rows と高亮を産出することを実証する（Plan §6 R1 高亮偏移の実データ検証。単測 T2-1 は合成 2 票、本項は真 Gemini 出力 6 頁）。
- **G3**：改動後コードで真 Gemini＋真 Sheets の端到端が**例外なく完走する**ことを smoke 確認する（認証・容量拡張・タブ生成など単測が届かない統合層）。
- **G4**：capture / replay を再利用可能な資産として残す（B4 以降でも同じ黄金様本 diff が回せる。fixture manifest を仓へ入れる——裁決#9）。

### 主張しないこと（**証明の限界を明示**——裁決#1）
- 本批は「**OCR/Gemini 層に回帰が無い**」ことを**証明しない**。replay は OCR result を固定入力として扱うため、prompt・`config`・`doc_types`・依存版本の変化が OCR 出力を変える risk は射程外。証明できるのは「**同一 OCR result 入力下の書账層（sheets_output）の回帰**」のみ。
- G3 は smoke（完走・目視）であって等価断言ではない。真 Gemini は非決定性のため、T6 の実出力と T3 fixture の**内容一致は判定基準にしない**（裁決#1・#10）。

### 非目標（範囲釘死）
- headless 経路の**真** E2E（Firestore＋Drive＋intake_guard 実接続）。契約上 U14／IP-308（B4）待ち。
- 実装コードの改修。**プロダクションコードは一行も変えない**（`local_test.py` への `--employee` 追加も含めて禁——裁決#7）。回帰発見時は Plan を止めて趙へ報告。
- 単頁 PNG 14 枚の真端到端（quota 節約。A 段 replay には含める）。
- 精度評価（OCR がどれだけ正しく読めたか）。本批は**回帰**のみを見る。

## 1. 現状事実（コード核験済み）

| # | 事実 | 出典 |
|---|---|---|
| F1 | `local_test.py` は UI 経路（`append_entries` を result 毎に呼ぶ）。headless 頁級経路は通らない。 | `local_test.py:126,150` |
| F2 | `local_test.py` は `OUTPUT_SPREADSHEET_ID`（＝生産表）へ直書き。tab は `employee_name="LocalTest"` **固定ハードコード**。 | `local_test.py:123,127,199` |
| F3 | `process_pipeline` の yield＝`{result, page_num, total_pages, page_bytes}`。`page_bytes` は分割 PDF アップロード専用で書账管線は使わない。 | `main.py:387`、`local_test.py:92-94` |
| F4 | `build_page_write(...) -> PageWrite`（純データ・ws 非接触）。`PageWrite = {tab_name, rows, highlight_ops, txn_range}`。 | `sheets_output.py:463-492` |
| F5 | `commit_page(page_write) -> (start_row, end_row)`。`_get_or_create_tab` → `get_all_values` → `_ensure_row_capacity` → `_write_with_retry` → 白リセット → 高亮 op → `_sanitize_trailing_once`。 | `sheets_output.py:494-536` |
| F6 | 既存 `test_sheets_output` は `SheetsOutputWriter.__new__` ＋ `_tab_next_txn`/`_tabs_sanitized` 注入＋`_FakeWorksheet` で gspread 認証を回避。replay ハーネスに流用可。 | `test_sheets_output.py:118-134` |
| **F7** | **rows に現在時刻が入る**：`_build_result_rows` と `_build_unrecognized_block` が `datetime.now(JST).strftime("%Y/%m/%d %H:%M")` を書く。→ **凍結しない限りバイト等価 diff は原理上不可能**（裁決#2）。 | `sheets_output.py:358,443` |
| **F8** | **UI 側の高亮出口は 4 箇所**：(a) 白リセット `format_cell_range`（:643）、(b) 低置信全行黄 `_format_with_retry`（:655）、(c) per-entry `_apply_anomaly_highlight`→`format_cell_range`（:667→:808）、(d) doc 赤 I 列 `_format_with_retry`（:679）。頁級側は `highlight_ops`→`_format_with_retry`（:526）に集約。 | 同上 |
| **F9** | **cell ref の形状が違う**：`_op_cell_ref` は単列でも `I7:I7`、`_apply_anomaly_highlight` は `I7`。全行はどちらも `A7:AB7`。→ 正規化しないと偽紅（裁決#4）。 | `sheets_output.py:99-106,808-823` |
| **F10** | **`_page_error` は wrapper 層で処理**：`local_test.process_local_file` は該頁を **skip**（Sheets へ書かない）、部分失敗時のみ**末尾に集約占位行 1 行**を追加。`append_entries` に直接食わせる経路は存在しない（裁決#3）。 | `local_test.py:96-102,146-162` |
| **F11** | `start_new_file` は既存データ有りなら separator 行を足し、取引No を 1 にリセット。→ 既存タブ条件では rows が増える（裁決#5）。 | `sheets_output.py:284-312` |
| F12 | `_ensure_row_capacity` は `worksheet.row_count`/`add_rows`、`_sanitize_trailing_once` は `row_count` を要求。欠けても例外は握り潰されるため**静かに未検証になる**（裁決#8）。 | `sheets_output.py:728-767` |
| F13 | 黄金様本＝`~/Desktop/井戸会計事務所/任務3/税区分テスト/`：6 頁 PDF ＋ 単頁 PNG 14 枚。実在核験済。 | 本 session `ls` |
| F14 | 基線 `0d304f0` に `build_page_write`/`commit_page` は無い（B3 新設）。`append_entries` は両 commit に存在。 | `git log` |

## 2. アーキテクチャ決定

### D1. capture / replay の二段分離（Gemini 抖動の遮断）
- **capture（1 回だけ真 Gemini）**：`process_pipeline` を回し、各 yield の `result`（`page_bytes` 除去）＋ manifest を JSON へ落とす。
- **replay（何度でも決定性）**：fixture を入力に、書账管線だけを fake worksheet 上で駆動し、正規化 JSON を出す。Gemini も Sheets も叩かない。

> **前提と限界**（裁決#1）：fixture は「その時の Gemini 出力」であり、コードの産物ではない。よって replay diff が緑でも**証明できるのは書账層の回帰不在のみ**。T0 は「fixture が基線でも同じはず」という前提の**成立可能性**を確認するが、成立しなくても Plan は破綻しない——その場合は主張を「HEAD の書账層に対する二経路等価（DIFF-B）」へ更に収窄するだけ。

### D2. 三本の diff（何を証明するか）

| diff | 左 | 右 | 期待 | 証明対象 |
|---|---|---|---|---|
| **DIFF-A** | `0d304f0` UI 経路 replay | `396dbc7` UI 経路 replay | **完全一致**（時刻凍結後） | G1：UI 版書账層ゼロ回帰 |
| **DIFF-B** | `396dbc7` UI 経路 replay | `396dbc7` 頁級経路 replay | rows 完全一致／高亮 canonical 集合一致 | G2：頁級等価・高亮偏移なし |
| **DIFF-C** | （なし・目視） | `396dbc7` 真端到端 | **例外ゼロ完走**＋目視 | G3：統合層 smoke |

**DIFF-B の適用条件（裁決#5 で収窄）**：**空タブ（fresh tab）条件に限定**する。`start_new_file` の separator 挿入・取引No リセット（F11）は既存タブ時のみ発生し、頁級経路には対応物が無い。既存タブ条件の等価は**主張しない**（本批の射程外・§7 TBD）。

**wrapper 語義の再現（裁決#3）**：replay は `sheets_output` を裸で叩くのではなく、`local_test.process_local_file` の制御フローを再現する：
- `_page_error` 頁 → **書账層に渡さない**（skip、`error_pages` 計上）。
- 部分失敗（`0 < error_pages < count`）→ **末尾に集約占位行 1 件**を `append_entries` 相当で追加（UI 経路）。頁級経路も同一の集約占位行を最終頁の後に置く（比較の土俵を揃える）。
- 全頁失敗 → 書込ゼロ（Failed 相当）。

**DIFF-B で不一致が出た場合の扱い**：正当な不一致は**上記 fresh tab 条件下では存在しない**——占位行は `_build_unrecognized_block` 共用（定稿 Plan D2'・#9）、取引No は両経路とも票毎 +1。よって**あらゆる不一致を回帰として扱い、停止して趙へ報告**する（後付けの言い訳を封じる）。

### D3. 基線側 replay の実行方式＝`git worktree`（裁決#6 で証明強化）
`git stash`／`checkout` の往復は事故源。**`git worktree add` で `0d304f0` を別ディレクトリへ検出**し、replay/capture スクリプトと fixture をコピーして実行。
- 実行は `cwd=<worktree>`、**`PYTHONPATH` を空にする**、Python は仓根 `venv311/bin/python` の絶対パス。
- replay スクリプトが**出自証明ブロック**を出力する（diff 対象外・別枠）：
  - `git rev-parse HEAD`（worktree 側）
  - `sheets_output` / `config` / `doc_types` / `anomaly_detector` / `tag_rules` の `__file__`
  - `hasattr(SheetsOutputWriter, "build_page_write")` の真偽（基線側は **False** でなければ worktree 誤り＝即停止）
- 検証後 `git worktree remove`。

### D4. 正規化出力フォーマット（diff の土俵）
```
{
  "source": "<fixture 名>",
  "path": "ui" | "page",
  "tabs": {
    "<tab 名>": {
      "rows": [[28 列の文字列...], ...],
      "highlights": [ {"cell": "I7:I7", "severity": "high"}, ... ]
    }
  },
  "reset_ranges": ["A6:AB11", ...],
  "warnings": [...]
}
```
出自証明ブロック（D3）は**別ファイル**へ出す（diff 対象外）。

正規化規則（裁決#2・#4・#8）：
1. **時刻の凍結**：replay 実行中 `sheets_output.datetime` を固定時刻（合成定数 `2026/01/01 00:00 JST`）へ patch する。加えて保険として、normalize 時に「作成日時／最終更新日時」列を `<FROZEN>` へ置換する（二重防御）。
2. **高亮の捕獲層＝format 出口の統一**（裁決#4、複審成立）：モジュール級 `format_cell_range` **のみ**を patch し `(cell_ref, backgroundColor)` を記録。`_apply_anomaly_highlight` の語義層は patch **しない**（(a)(b)(d) を取りこぼすため）。
   > **実施中の訂正（Phase 2、笔误直改）**：草案は `_format_with_retry` も併せて patch すると書いていたが、`_format_with_retry` は内部で同じモジュール級 `format_cell_range` を呼ぶ（`sheets_output.py:718`）。両方を patch すると二重記録または記録漏れになる。**モジュール級 1 箇所の patch で F8 の 4 出口＋頁級経路すべてを捕獲できる**。判定基準への影響なし。
3. **severity の復元**：記録した色を `_severity_color` の逆写像で severity 名へ戻す。両経路が同一の色テーブル（`_SEVERITY_COLORS`）を使うため、色定義が変わっても両側同時に変わり diff は成立し続ける。白（1,1,1）は severity ではなく **`reset_ranges`** へ分離（意味的高亮ではない）。
4. **cell ref の canonicalize**：`I7` と `I7:I7` を同一表現へ正規化（単セルは `X{n}:X{n}` 形へ寄せる）。
5. **順序**：`rows` は append 順（意味あり、順序比較）。`highlights` は**集合比較**（塗り順は仕様上の重ね順であり、両経路で順序が違っても最終色は同じ）。ただし差分が出た場合は順序も報告に載せる。
6. **浮動要素の禁止**：実行時刻・所要時間・オブジェクト id・絶対パスを出力に入れない。

### D5. 真端到端（T6）の副作用制御（裁決#7）
- 書込先＝生産 `OUTPUT_SPREADSHEET_ID` の tab `LocalTest_<領収書後綴>`（`local_test.py` ハードコード、F2）。既存従業員 tab は不変。
- **硬前置条件**：実行前に当該 tab の**存否を確認**。既に存在する場合は**実行せず停止して趙へ報告**（先に削除してもらう）。自動削除・自動リネームはしない（生産表への破壊操作を code に持たせない、かつ `local_test.py` を改造しない）。
- 実行後の tab 削除は**趙の手**。報告書冒頭に削除依頼を明記。
- `test_images/receipt/` へ 6 頁 PDF を**コピー**（原本は Desktop に残す。`local_test.py` は成功時 `processed/` へ move するため）。
- Gemini quota＝6 頁 × 1 回。

## 3. タスク清単（最小可検証単元・各 DoD 付き）

> **実行順序は DoD**（裁決#10・R4）：T0 → T1 → T2（単測緑）→ T3（真 Gemini）→ T4 → T5 → T6 → T7。
> replay 側の不備が判明してから fixture を取り直す空振りを防ぐため、**T1/T2 が緑になるまで T3 を実行しない**。

### T0 前提検証（実装前・10 分）
- `git diff 0d304f0 396dbc7 --stat` で改動ファイル一覧を取得。
- **OCR 出力に影響しうる全モジュール**に差分が無いことを確認（裁決#1 で拡大）：`ocr_engine.py` / `doc_types.py` / `config.py` / `receipt_aggregation.py` / `requirements.txt`。
- 差分があった場合 → **Plan は止めない**が、DIFF-A の主張を更に弱め、当該差分が OCR 出力を変えうるかを個別評価して報告に明記する（D1 の限界節に従う）。
- **DoD**：差分一覧を証拠として貼付、上記 5 ファイルの差分有無を明記。

### T1 replay ハーネス `golden_replay.py`（新規・TDD）
書账管線を fake worksheet 上で駆動し、D4 の正規化 JSON を出す。**基線でも動く**ことが必須（新 API は `path="page"` の時だけ触る）。

- 実装：
  - `FakeWorksheet`：`title` / `get_all_values` / `append_rows` / **`append_row`** / **`row_count`** / **`add_rows`**（裁決#8。欠けると容量拡張・尾行清掃が握り潰されて静かに未検証になる、F12）。容量・清掃の呼出は `warnings` へ記録。
  - `make_offline_writer()`：`SheetsOutputWriter.__new__` ＋ 属性注入（F6）、`_get_or_create_tab` を fake 返却へ差替。
  - `freeze_time()`：`sheets_output.datetime` を固定（D4-1）。
  - `capture_formats()`：`format_cell_range` と `_format_with_retry` を patch（D4-2）。
  - `replay_ui(fixture, doc_type)` / `replay_page(fixture, doc_type)`：**`local_test.process_local_file` の wrapper 語義を再現**（D2）。
  - `normalize(...) -> dict`／`origin_report() -> dict`（D3 の出自証明）。
- TDD（先 RED、`test_golden_replay.py`）：
  1. `normalize` が同一入力に対し**バイト等価な JSON** を返す（決定性の釘死。時刻凍結が効いていることの証明を兼ねる）。
  2. 時刻凍結を**外した**場合に作成日時列が変わることを示し、凍結ありで変わらないことを対で検証（凍結機構そのものの回帰ガード）。
  3. 高亮捕獲が 4 出口すべてを拾う（白リセットは `reset_ranges`、他は `highlights`）。
  4. cell ref canonicalize：`I7` と `I7:I7` が同一表現になる。
  5. `_page_error` 頁が書账層へ渡らない／部分失敗時に集約占位行が末尾に 1 件付く（wrapper 語義、F10）。
  6. `replay_page` が単一 `append_rows` 呼出に集約する（頁原子の再確認）。
  7. `replay_ui` は `build_page_write` を**呼ばない**（基線互換の担保）。
  8. `origin_report` が基線条件（`build_page_write` 不在）を検出できる。
- **DoD**：上記 8 本緑（venv311）。新規コード カバレッジ 80%+。

### T2 capture スクリプト `golden_capture.py`（新規・TDD）
- 実装：`capture(path, doc_type, strategy) -> (fixture, manifest)`。fixture＝`page_num`/`total_pages`/`result`（`page_bytes` 除去）。**manifest**（裁決#9）＝`{source_file, source_sha256, commit, strategy, gemini_model, captured_at, page_count}`。`--out` 指定。**責務は狭く保つ**：`process_pipeline` 呼出・`page_bytes` 除去・出力のみ。
- TDD（`test_golden_capture.py`、`process_pipeline` は monkeypatch。**真 Gemini は呼ばない**）：
  1. `page_bytes` が fixture に含まれない。
  2. yield 順（`page_num` 昇順・同頁複数票の連続）が保存順として保たれる。
  3. `_page_error` 頁も fixture に記録される（skip 判断は replay 側の責務）。
  4. manifest に必須キーが揃い、`source_sha256` が実ファイルから算出される。
- **DoD**：上記緑。カバレッジ 80%+。

### T3 fixture 取得（真 Gemini・1 回・quota 消費）
- 6 頁 PDF ＋ 単頁 PNG 14 枚を capture（strategy＝既定 C）。
- 保存先＝`golden/`（**`.gitignore` に追加**、生データは仓へ入れない）。**manifest のみ `golden_manifest/` として仓へ追跡**（裁決#9、G4 の再現性担保）。
- **DoD**：fixture が 6 頁ぶん（＋PNG 14 件）存在、各 result が `entries`／`_unrecognized`／`_page_error` のいずれかを持つ。頁数不一致なら停止・報告。

### T4 DIFF-A（UI 版書账層ゼロ回帰）
- `git worktree add` で `0d304f0` 検出（D3）。スクリプト＋fixture をコピー。`cwd=worktree`、`PYTHONPATH` 空。
- 両側 `replay_ui` → 正規化 JSON を `diff`。**出自証明ブロックを先に確認**（基線側 `build_page_write` 不在＝True でなければ即停止）。
- **DoD**：**完全一致（diff 空）**。1 バイトでも違えば回帰候補として停止・趙へ報告（本 Plan では直さない）。

### T5 DIFF-B（頁級経路の等価性・高亮偏移）
- HEAD 側で `replay_ui` と `replay_page` を **fresh tab 条件**で実行し比較（D2）。
- 判定：`rows` 完全一致／`highlights` canonical 集合一致。
- **DoD**：完全合致。不一致はすべて回帰として停止・報告。

### T6 DIFF-C（真端到端 smoke）
- **前置**：`LocalTest_*` tab の存否確認 → 存在すれば停止・報告（D5）。
- `local_test.py --only-file` で 6 頁 PDF を 1 本実行。
- 確認項目（**目視 smoke、等価断言はしない**——裁決#1・#10）：例外ゼロ完走／6 頁ぶんの行が着地／28 列に税区分が入っている／高亮が載る／取引No 連番／`_ensure_row_capacity` 由来の警告が出ていない。
- **DoD**：例外ゼロ完走＋上記目視クリア＋行ダンプを証拠添付。**tab 削除は趙へ依頼**。

### T7 証拠パッケージ
- 三 diff の結果、出自証明ブロック、実行コマンド、fixture manifest、全量テスト出力、`git diff HEAD --stat`、**証明の限界**（§0「主張しないこと」）を一枚に。

## 4. 受入基準（脚本化判定優先）
1. `venv311/bin/python -m unittest test_golden_replay test_golden_capture -v` 全緑。
2. `venv311/bin/python -m unittest discover -p "test_*.py"` 全緑（既存 406 本＋新規）。
3. **DIFF-A が空**。
4. **DIFF-B が rows 完全一致・highlights canonical 集合一致**（fresh tab 条件）。
5. **DIFF-C が例外ゼロ完走**＋目視項目クリア（等価断言は課さない）。
6. 新規コード カバレッジ 80%+（全局 §9・趙既定口径、本批不可改）。
7. プロダクションコード（`sheets_output.py`/`main.py`/`posting_ledger.py`/`intake_guard.py`/`ocr_engine.py`/**`local_test.py`**）の diff が**ゼロ**。

## 5. 影響面
- **新規**：`golden_capture.py`、`golden_replay.py`、`test_golden_capture.py`、`test_golden_replay.py`、`golden_manifest/`（追跡）、`.gitignore` に `golden/` 追加、本 Plan。
- **改修**：なし（受入基準 7）。
- **外部副作用**：Gemini quota＝6 頁×2 回＋PNG 14 枚×1 回／生産 Spreadsheet の `LocalTest_*` tab に約 6〜10 行（**趙が手工削除**）／`test_images/` 配下のコピー・移動（gitignore 配下）。
- **不動**：Drive、Firestore、headless 経路の真接続、`daily_backup.gs`。

## 6. リスクと回退
- **R1 fixture の代表性不足**：一度の Gemini 出力が特定分岐（同頁多票・高額異常）を含まない可能性。→ 本批の目的は網羅ではなく同入力での前後一致。網羅は B3 の単測が担う。fixture に現れなかった分岐は §7 に明記して単測へ委ねる。
- **R2 worktree の取り違え**：→ 出自証明ブロック（D3）を必ず確認。基線側 `build_page_write` 不在を assert、`cwd`／`PYTHONPATH` を制御、主要 transitive モジュールの `__file__` を全出力。
- **R3 生産表汚染**：→ T6 の硬前置条件（tab 存否）＋報告冒頭の削除依頼。
- **R4 quota 空振り**：→ T1/T2 単測緑を T3 の前提条件として §3 冒頭に順序 DoD 化。
- **R5 diff が赤（真の回帰発見）**：→ **本 Plan では直さない**。差分の具体行・列・高亮位置を事実として趙へ報告、修正は別ラウンドで裁決。
- **R6 ハーネス自体の誤り（自洽緑）**：fake harness にモデル誤差があれば DIFF は自洽的に緑になる（Codex #10 の核心懸念）。→ 緩和＝T1-2（時刻凍結の対検証）・T1-3（4 出口の捕獲証明）・T1-5（wrapper 語義）で**ハーネス自身に回帰ガード**を掛ける。加えて T6 の真 Sheets smoke が「ハーネス外」の独立観測点として残る。完全な排除は不可能——この限界を §7 に明記。
- **回退**：全成果物が新規ファイルのみ。削除すれば元へ戻る。生産表は tab 削除のみ。

## 7. TBD / 開工核実
- 既存タブ条件（`start_new_file` separator＋取引No リセット）での UI/頁級等価は**本批の射程外**。頁級経路に separator 対応物が無いこと自体が設計上正しいかは B4 で contract を確認する（趙／控制面判断）。
- fixture 本体を仓へ入れるか（現案＝manifest のみ追跡）。B4 以降で共有回帰基準にしたいなら別途拍板。
- ハーネス自洽緑の限界（R6）は原理的に残る。将来 `local_test.py` の実 Sheets 出力を機械読取して replay 出力と突き合わせれば閉じるが、Gemini 非決定性のため同一 run 内でしか成立しない——B4 以降の検討事項。
- `LocalTest` tab の後綴実名（`DOC_TYPE_TAB_SUFFIX` の receipt 値）は T6 実行時に実測して報告へ記す。

## 8. 附録：Codex 辯論裁決記録（Phase 1・定稿）

Codex 対抗評審 10 条。逐条裁決（部分駁回 3 条は 1 輪複審済、Codex 全て「成立・不重提」＝我方勝）。

| # | Sev | 論点 | 裁決 | 理由 |
|---|---|---|---|---|
| 1 | High | capture/replay は真 E2E 無回帰を証明できない／T0 が `ocr_engine` のみ／T6 の等価断言は非決定性で不成立 | **採納** | 事実。§0 に「主張しないこと」を新設、T0 を 5 ファイルへ拡大、T6 を smoke へ降格。 |
| 2 | High | rows に `datetime.now()` → バイト等価 diff は原理上不可能 | **採納** | `sheets_output.py:358,443` で核験。時刻凍結＋列置換の二重防御（D4-1）。 |
| 3 | High | `_page_error` の wrapper 語義（skip＋末尾集約占位）が未再現 | **採納** | `local_test.py:96-102,146-162` で核験。replay が wrapper 制御フローを再現（D2）。 |
| 4 | High | 高亮 cell ref 形状差＋UI 側に severity 語義なし | **部分採納** | canonicalize 採納（D4-4）。**駁回**「`_apply_anomaly_highlight` の語義層を patch」＝UI の 4 出口中 (a)(b)(d) を取りこぼす（F8）。統一 format 出口＋色逆写像へ改（D4-2/3）。**複審成立**。 |
| 5 | Med | rows 完全一致は fresh tab 前提でしか成立しない | **採納** | `start_new_file` の separator／取引No リセット（F11）。DIFF-B を fresh tab 条件へ収窄、既存タブ等価は §7 へ。 |
| 6 | Med | worktree の出自証明が 1 モジュールでは不足 | **採納** | `cwd`／`PYTHONPATH`／`rev-parse`／主要 transitive `__file__`／`build_page_write` 不在 assert（D3）。 |
| 7 | Med | `LocalTest_*` tab が既存なら汚染 | **部分採納** | 硬前置条件（存在すれば停止・報告）採納。**駁回**「`local_test.py` に `--employee` を追加」＝受入基準 7（production code diff ゼロ）違反かつ範囲拡大。**複審成立**。 |
| 8 | Med | FakeWorksheet が `row_count`/`add_rows`/`append_row` を欠き、容量・清掃が握り潰されて静かに未検証 | **採納** | F12 で核験。規格を T1 へ明記、警告を `warnings` へ記録。 |
| 9 | Med | G4「再利用可能資産」と fixture gitignore が矛盾（metadata 無し） | **採納** | manifest（source_sha256/commit/strategy/model/captured_at）を `golden_manifest/` として追跡。 |
| 10 | Low | 過度設計（capture CLI・coverage 80%・worktree orchestration） | **部分採納** | smoke 化・freeze time 採納（#1 と重合）。**駁回**「capture 脚本削除」＝T3 の実行体そのもので、無ければ fixture が取れず G4 も崩れる。**駁回**「coverage 80% 降標」＝全局 §9・basic-design/07 の趙既定口径、本批不可改。**複審成立**。R6 として自洽緑の限界を明記。 |
