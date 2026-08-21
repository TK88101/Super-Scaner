# T11 真票 E2E の機械判定 ＋ T9 の再定義

- 作成: 2026-08-21 / **v2（Codex 第 1 ラウンド 16 件を裁決して改訂）**
- 母 Plan: `docs/plans/2026-08-12-credit-card-doctype.md`（§5 T9/T11、§6 A1〜A11）
- 前提コミット: `origin/main = 7af43fe`（作業樹 clean・全量 1453 tests 緑・expectedFailure 2）
- 本 Plan の位置づけ: 母 Plan の **T11 を実施**し、**T9 を趙の裁定に沿って再定義**する

---

## 0. 本 Plan を書くに至った事実（全て実測。推測は「推測」と明記する）

| # | 事実 | 出典 |
|---|---|---|
| F1 | A11（新規モジュール覆盖率 ≥ 80%）は**本 session で初めて測定し 96% で合格** | `coverage run --source=card_file_recon -m unittest discover` → `141 stmts / 6 miss / 96%` |
| F2 | A10（既存テスト全緑）は達成済み | `Ran 1453 tests` / `OK (expected failures=2)` |
| F3 | **A1〜A9 は一度も機械判定していない**。前 session の B-5 は 3 つの数字（合計不一致 0 行・verdict 分布・Gemini 呼出回数）しか見ていない | 前 session の引継ぎ |
| F4 | **A8 の「異常マーク」は製品コードに存在しない**。`_year_estimated` は `card_entries.py:609/651/655` で計算されるが**消費者はテストだけ**。`anomaly_detector` の 13 種の `type`（`account_review` `high_amount` `tax_review` `undetermined_account` `missing_date` `partial_date` `missing_vendor` `missing_invoice` `invalid_t_number` `total_mismatch` `outlier_exempt_row` `low_confidence`）に年推定相当は無く、`partial_date` は `^\d{4}/\d{2}$` にしか発火しない（推定年は完全形 `YYYY/MM/DD` を生む） | `grep -rn "_year_estimated" *.py` / `anomaly_detector.py:115` |
| F5 | 録音 slot には `usage` が入っている | `fixtures/full/0000/response.json` → `{'total_token_count': 5052, 'prompt_token_count': 3746, 'candidates_token_count': 254}`、他に `finish_reason` `raises` |
| F6 | `test_images/credit_card/` は**空**。真票 63 件は `test_images/processed/` にある。原票 8 本は `~/Downloads/クレジットカード訓練樣本/` に健在 | `ls` 実測 |
| F7 | `local_test.main()` は**真の `SheetsOutputWriter` を構築して本番スプレッドシートに書く** | `local_test.py:508` |
| F8 | `append_entries` の引数に**頁番号は無い** | `sheets_output.py:480` |
| F9 | MF は 28 列。H列 = `MF_HEADERS[7] = 借方インボイス`、P列 = `[15] = 貸方インボイス`、U列 = `[20] = タグ` | `sheets_output.py:14` |
| F10 | 監査タブの判定値は `除外 / 分岐 / 欠落 / 合計不一致` の 4 種 | `sheets_output.py:33-36` |
| **F11** | `local_test.py:41` は `from ocr_engine import process_pipeline`。**`ocr_engine.process_pipeline` を包んでも `local_test` の呼出は捕捉できない** | 実測（Codex 第 1R #10 を核実） |
| **F12** | `sheets_writer` の呼出のうち **264 / 281 / 308 / 343 行は `for page` 循環（144行）の外**（インデント 8〜12 に対し循環本体は 12 以上＋分岐）。「直前に yield した頁が当該頁」という推論は**この 4 箇所で成立しない** | `awk` によるインデント実測（Codex 第 1R #9 を核実） |
| **F13** | 監査タブの理由列は `audit_reason_ja(reason)` で**日本語に訳してから書かれる**（`sheets_output.py:951`）。機械可読キー（`duplicate_page:3` 等）は引数側にしか無い | 実測（Codex 第 1R を核実） |
| **F14** | `local_test.py:368` は `shutil.move(file_path, dest)`。**E2E は入力を消費する**。ただし `TEST_DIR = "./test_images"`（52行）と `PROCESSED_DIR`（53行）は**モジュール級の定数**であり、`scan_local_files`(81-82) と退避先(364/367) は実行時にこの名前を引く。**両方を実行時に差し替えれば、製品コード 0 行で入出力を一時ディレクトリへ逃がせる**（`gemini_record` の monkey patch と同型）。`PROCESSED_DIR` は import 時に `TEST_DIR` から算出済みなので、**片方だけ差し替えると原票が repo 内へ退避される** | 実測（Codex 第 2R #13 を核実） |
| **F15** | 全頁が 1 つも yield されない場合、`last_total_pages` が 0 のままなので `set(range(start_page, 0+1)) - seen` が空集合になり、**「欠落」監査行すら書かれない**。ファイル全体の無音消失は現行の留痕機構をすり抜ける | `local_test.py:302-303` 実測（Codex 第 1R #1 を核実） |

---

## 1. 目標

1. **A1〜A9 を機械判定に落とす**。目視合格を一切使わない。判定はスクリプトが緑/赤を返す。
2. **T11 DoD の後半**（ETC 100 行頁の出力トークン実測と `GEMINI_MAX_OUTPUT_TOKENS=32768` に対する余裕）を数値で示す。
3. **T9 を趙の 2026-08-21 裁定に沿って再定義**し、過期 docstring を是正する。
4. 判定が赤になった項目は**原因を特定して分類する**（実装の欠陥／判定器の欠陥／基準の過期／モデルの揺れ）。**製品の修正は本 Plan の範囲外**（趙の拍板事項として上申する）。

## 2. 非目標

| # | やらないこと | 理由 |
|---|---|---|
| N1 | `FileReconLedger`（`card_reconciliation.py`）を生産経路へ接線する | 実測で否決済（カード単位で束ねると偽の不一致 7 件）。趙 2026-08-21 裁定 |
| N2 | `test_page_disposition_wiring.py:370` の欠陥を塞ぐ | 趙 2026-08-21 裁定「本次不做、独立課題へ」 |
| N3 | A8 の欠落（F4）を**本 Plan で修正する** | 修正は `anomaly_detector` ＋ `tag_rules` という生産経路の改造。**検出して上申するところまで**が本 Plan |
| N4 | ラベル拡充による覆盖率 4/7 → 7/7（TBD-2） | 既存裁定＋番人テストに触れる。別 Plan |
| N5 | TBD-5（`_segments` が `card_file_state` の OCR 拒否権を継承していない） | 別課題 |
| N6 | P2 骨格重複（`_with_file_recon` と `_apply_page_audit_signal`） | 挙動不変のリファクタ。判定と混ぜない |
| N7 | fixtures の録り直し | prompt を変えないので不要。変えたら録り直す（`--accept-drift prompt` で誤魔化さない） |
| N8 | 製品コード（`ocr_engine.py` / `main.py` / `local_test.py` / `sheets_output.py` / `card_*.py` / `anomaly_detector.py`）の変更 | 本 Plan は**計測**。U6 の docstring 修正 2 箇所（テストファイル）だけが例外 |
| **N9** | **F15（全頁欠落が留痕されない）を製品側で修正する** | N8 と同じ。判定器は F15 を**独立に検出できる**設計にする（§3.3 A1）が、製品の穴を塞ぐのは別 Plan |

## 3. 設計

### 3.1 dump driver（`dump_e2e_rows.py`）

> **配置の変更（2026-08-21）**: 当初 `scripts/` 配下に置く予定だったが、`scripts/` には `__init__.py` が無く、**既存テストで `scripts/` 配下を import しているものは 1 本も無い**（実測）。本 repo の惯例は「根目録の `test_*.py` と根目録の同名モジュールを対にする」であり、`scripts/` に置くと sys.path 操作か `__init__.py` 追加が要る。位置に迎合して機構を増やすのは本末転倒なので、`local_test.py` / `benchmark_ocr.py` と同じ根目録に置く。

`local_test.main()` は真の Sheets に書く（F7）。判定のために本番スプレッドシートを汚すのは論外。かつ `append_entries` に頁番号が無い（F8）ので、書かれた行から頁を復元できない。よって driver を新設する。

**4 つの設計決定**（いずれも Codex 第 1R の指摘を容れた結果）:

1. **patch 対象は `local_test.process_pipeline`**（`ocr_engine.` ではない）。F11 の通り、`local_test` はモジュール取込時にローカル束縛している。契約テストで「捕捉した yield 数 > 0」を固定する。
2. **writer イベントは明示的な provenance を持つ**。「直前に yield した頁」からの推論は F12 により**受入条件に使わない**。判別は generator wrapper の状態機で行う:
   - wrapper が `yield` した直後〜次の `__next__` 呼出まで = `scope="page"`, `page=<当該頁>`
   - generator が `StopIteration` を出した後 = `scope="file"`, `page=null`
   - この規則は `local_test` の行番号に依存しない（行番号を書き写すと、製品が変わった瞬間に無音でずれる）
3. **入力頁数は PDF から独立に取得する**（`pypdf`）。F15 の通り、pipeline が 1 頁も yield しないと観測側は空集合になり、`>= 1` 系の判定は vacuous PASS する。期待頁集合を**観測に依存しない源**から取る。画像ファイルは 1 頁とする。
4. **Sheets への到達を fail-fast spy で禁じる**。`SheetsOutputWriter.__init__` / `gspread.service_account` / `gspread.authorize` を「呼ばれたら即例外」に差し替える。「構築しないこと」を確認するだけでは不十分（別経路・`flush`・モジュール初期化時の接続を捕まえられない）。
5. **入力は一時ディレクトリへステージングする**。`local_test.TEST_DIR` と `local_test.PROCESSED_DIR` を**両方**実行時に差し替え、原票を `~/Downloads/クレジットカード訓練樣本/` から temp へコピーしてから駆動する。こうすると (a) 原票が `shutil.move` で消費されない（F14）、(b) 顧客の実 PDF が repo ツリー内に置かれない、(c) 再実行が冪等になる。**製品コードは 1 行も変えない**（N8 を守ったまま Codex 第 2R #13 を容れた形）。`PROCESSED_DIR` の差し替えを忘れると原票が repo 内 `test_images/processed/` へ落ちるので、U1 の DoD でこれを固定する。

**replay**: `gemini_record` の context を張る（`fixtures/full`）。**Gemini 呼出 0 回**を出力で確認してから先へ進む。

> **罠**: 標本の正本は必ず実物（`fixtures/full` の録音と `~/Downloads/クレジットカード訓練樣本/`）。手写しの OCR 文字列は `classify_page` が `routing_family='unknown'` を返し、**実在しない経路**を駆動する（2026-08-20 と 2026-08-21 に 2 度踏んだ）。

### 3.2 出力は単一のイベントストリーム `dump/events.jsonl`

Codex 第 1R #16 を容れ、5 種類のファイルを 1 本に畳む。CSV と JSONL に同じ事実を二重化すると、両者が食い違ったときにどちらが正かを決められない。人が読む CSV は verifier が**派生物として**出す。

各行は `{"v": 1, "kind": ...}` を持つ。`v` はスキーマ版（将来の判定器が版を見て弾けるようにする）。

| kind | 主なフィールド | 用途 |
|---|---|---|
| `file_start` | `file` `doc_type` `expected_pages`（PDF から独立取得） | A1 |
| `page_yield` | `file` `page` `total_pages` `excluded` `exclude_reason` `audit_signal` `ocr_text_len` `entries_len` `year_estimated_count` | A1 A8 |
| `mf_row` | `file` `page` `scope` `row`（28 列） `raw_entries`（abs() 転正の検出用に整形前の金額を含む） | A3 A4 A6 A7 A9 |
| `audit_row` | `file` `page` `scope` `verdict` `reason_key`（引数の生キー） `reason_ja`（`audit_reason_ja` 適用後） `row`（7 列） | A1 A5 A7 |
| `recon` | `file` `verdict` `detail_sum` `statement_total` `notes` | A2 |
| `token_usage` | `slot` `file` `page` `usage` `finish_reason` `ocr_line_count` `detail_row_count` | U7 |
| `file_end` | `file` `success` `seen_pages` | A1 |

`reason_key` と `reason_ja` を**両方**持つのは F13 のため。判定は必ず `reason_key` 側で行う（訳文に依存した判定は、訳を直した瞬間に無音で壊れる）。

### 3.3 受入基準（機械判定・**判定式そのものが基準**）

| # | 判定式 | 判定漏れ対策 |
|---|---|---|
| **A1** | (a) `expected_pages`（PDF 独立取得）から作った期待頁集合 == `page_yield` の観測頁集合、(b) 各 (file,page) で `mf_row + audit_row >= 1`、(c) `audit_row` に `verdict == "欠落"` が 0 件、(d) `expected_pages > 0` が全 8 ファイルで成立 | (a) が F15（全頁欠落）を捕まえる。(d) が「入力を読めなかった」を FAIL にする |
| **A1b** | 同一 (file,page) 内で、MF 行の完全キー（取引日＋借方金額＋摘要＋貸方金額）が重複しない | Codex #2。A1 に混ぜず独立項にする（混ぜると FAIL 原因が「漏れ」か「重複」か分からなくなる） |
| **A2** | (a) 8 ファイル全てが `recon` に出現、(b) `verdict` が既知集合 `{一致, 検算不能, 合計不一致}` 内、(c) **期待一致の 4 件**（ENEOS / TS CUBIC / アメックス / アレコレ）が全て `一致` かつ `detail_sum == statement_total`、(d) **期待検算不能の 4 件**（JCB / UC / 楽天 / ニモカ）が全て `検算不能`、(e) ENEOS の `statement_total == 15503` | Codex #3。「一致のものだけ検証」を止め、**期待集合との完全一致**を要求する |
| **A3** | `file == ENEOS` の `mf_row` の借方金額（int 化）の総和 == **18,503** | Codex #4。範囲を ENEOS に明示。A2 と数字が違うのは正常（A2 は当期調整込み、A3 は記帳対象行のみ） |
| **A4** | `(アメックス, 3)` と `(アメックス, 4)` それぞれについて `mf_row` 件数 == 0 | Codex #5。指紋検査は**判定から外し診断出力に降格**（日付＋金額は同日同額の正当取引で過検出、OCR 差分で判定漏れ） |
| **A5** | `(アメックス, 3)` と `(アメックス, 4)` **それぞれ**に `audit_row` が 1 件以上あり、`verdict == "除外"` かつ `reason_key` が `duplicate_page` で始まる | Codex #6＋F13。訳文でなくキーで判定 |
| **A6** | (a) 全 `mf_row` の借方・貸方金額（int 化）に `< 0` が 0 件、(b) `raw_entries` の金額にも `< 0` が 0 件、(c) 摘要に `ENEOSポイントキャッシュバック` / `前回分口座振替金額` を含む行が 0 件 | Codex #7（第1R別番号）。(b) が abs() 転正を整形前の値で捕まえる。(c) は補助 |
| **A7** | `(アメックス, 6)` と `(アレコレ, 3)` **それぞれ**について `mf_row` 0 件 ＋ `audit_row` 1 件以上 ＋ `verdict == "除外"` | Codex #6。両頁を個別に assert |
| **A8** | **未実装のため FAIL**（趙 2026-08-21 裁定。§3.5）。判定式は「`year_estimated_count > 0` の nimoca 行が存在し、**その各行の U列タグに年推定を示す標識が入っている**」。F4 により現状は必ず FAIL する | Codex #7。「判定不能」を受入基準にしない。要求された性質を判定し、満たさないなら FAIL と言う |
| **A9** | (a) 全 `mf_row` の H列（借方インボイス）と P列（貸方インボイス）が空、(b) **各行について** `借方金額 >= 10000 ⇔ タグに INVOICE_CONFIRM_TAG を含む` が成立、(c) 対象行（`>= 10000`）が 1 件以上存在する | Codex #8。件数一致でなく逐行同値。(c) が vacuous PASS を潰す |
| A10 | 達成済（F2）。判定スクリプトでは再実行しない（全量テストは Phase 4 で回す） | — |
| A11 | 達成済（F1・96%）。判定スクリプトに coverage 実行を含めない | — |

**除外行の扱い**: 分隔行（`separator_row`）・占位行（`_unrecognized_placeholder`）は明細行ではない。判定式は `mf_row` に `is_data_row` フラグを持たせ、A6/A9 はデータ行のみを対象にする。この分類規則は verifier 側に 1 箇所だけ置く。

### 3.4 T9 の再定義（趙 2026-08-21 裁定）

母 Plan の T9 DoD は「expectedFailure 2 件を外すこと」だった。趙の裁定により以下へ改める:

| 元の条件 | 新しい扱い | 根拠 |
|---|---|---|
| `test_card_reconciliation.py:303` の装飾子を外す | **永久に「やらない」と宣告する**。装飾子は残す。docstring を是正して裁定へのポインタを書く | 前提（`FileReconLedger` の接線）が実測で否決された。カード単位で束ねると偽の不一致 7 件。ファイル単位の検算（`card_file_recon`）が同じ目的をより低い偽警報率で達成している |
| `test_page_disposition_wiring.py:370` の装飾子を外す | **T9 から分離し、独立課題として持ち越す**。本 session では着手しない | 「同一頁に監査 2 行」の語義決定を要し、`main` と `local_test` 双方の制御フロー改造を伴う。T11 を先に通す |

**T9 の新しい状態**: 部分完了。残 1 件は独立課題へ移管。

> `card_reconciliation.py`（製品コード）の diff は **0 行のまま**。docstring 修正が触るのは `test_card_reconciliation.py` のみ。

### 3.5 A8 の扱い（**趙 2026-08-21 裁定: (a) FAIL と判定する**）

F4 により、A8 が要求する「年推定に異常マーク」は製品に存在しない。取りうる道は 2 つ。

| 案 | 内容 | 帰結 |
|---|---|---|
| **(a) ★趙裁定** | A8 を受入基準に残し、**FAIL と判定する** | T11 は「A8 FAIL・product gap として上申」で完了。修正は別 Plan。nimoca の推定年が顧客に見えない会計リスクが台帳に残る |
| (b) | A8 を A1〜A9 の受入集合から外し、gap report へ移す | T11 は全 PASS を目指せる。ただし「推定年が無標識」というリスクが受入基準から消える |

**趙は 2026-08-21 に (a) を裁定した。** 理由: nimoca の券面には年が印字されておらず（`card_entries.py:719`）、`_nearest_past` が年を**推測して**完全形の日付を作る。この日付が推測だと顧客に見えないなら、年跨ぎの誤記帳が無音で通る。基準から外すのは、リスクを消すのではなく見えなくする。

**帰結**: A1〜A9 は本 Plan の完了時点で**全 PASS にならない**。A8 は恒久的に FAIL であり、それが product gap として台帳に残ること自体が成果である。§8 の L3（acceptance pass）を完了条件に入れないのはこのため。

## 4. タスク清単（各項に DoD）

| # | 内容 | DoD |
|---|---|---|
| **U1** | `dump_e2e_rows.py`。§3.1 の 4 決定を実装 | `test_dump_e2e_rows.py` 緑。特に **(a)** `SheetsOutputWriter.__init__` / `gspread.service_account` / `gspread.authorize` を fail-fast spy にして呼出 0 件、**(b)** patch 先が `local_test.process_pipeline` であり捕捉 yield 数 > 0、**(c)** `local_test.process_local_file` を実際に呼んでいる、**(d)** 既知の 2 頁ダミーで循環内の書込が `scope="page"`＋正しい頁、循環後の書込が `scope="file"`＋`page=null`、**(e)** `expected_pages` が pipeline の出力に依存せず PDF から取れている（0 頁 yield のダミーで期待頁が正しく出る）、**(f)** ステージングが `TEST_DIR` と `PROCESSED_DIR` の**両方**を差し替えており、実行後に repo 内 `test_images/` が不変であること |
| **U2** | `verify_e2e_acceptance.py`。§3.3 を評価し PASS/FAIL と根拠数値を出す。FAIL があれば exit 1 | `test_verify_e2e_acceptance.py` 緑。**(a)** スキーマ検証テスト（未知 `kind`・`v` 不一致・必須欄欠落を弾く）、**(b)** 各判定式に **false-negative 反例**（性質が壊れているのに PASS しないこと）と **false-positive 反例**（正常なのに FAIL しないこと）を 1 つずつ、**(c)** 合成合格 dump で A8 以外 PASS。**「当該項だけ FAIL」は要求しない**（A1/A5/A7 は同じ audit を共有するので複数赤が自然。人工的分離を強いると実データの意味を歪める） |
| **U3** | 真票 8 本を `~/Downloads/クレジットカード訓練樣本/` から**一時ディレクトリ**へコピーし（`credit_card/`、ニモカのみ `transit_ic/`）、`local_test.TEST_DIR` と `local_test.PROCESSED_DIR` をそこへ向けて `--replay fixtures/full` で dump を採取 | `dump/events.jsonl` が生成される（**出力 artifact は 1 本**）。**Gemini 呼出 0 回**が出力に出る。処理成功 8 件・失敗 0 件。**原票が消費されていない**（実行後に `~/Downloads` 側の 8 本が健在）。**`test_images/` 配下が実行前後で不変**（ステージングが効いている証拠） |
| **U4** | 判定を実行し結果を記録 | A1〜A9（＋A1b）の PASS/FAIL 表。FAIL 項は「製品の欠陥／判定器の欠陥／基準の過期／モデルの揺れ」に分類。**判定器の欠陥は修正して再判定する**（分類して終わりにしない） |
| **U5** | A8 の欠落を数値化 | nimoca の `year_estimated_count` の合計と、該当行の U列タグの実測値。**「年推定行が N 件あり、うち標識付きは 0 件」を数字で示す**（F4 の裏取り） |
| **U6** | T9 再定義を文書化 ＋ docstring 2 箇所を是正 | (a) 本 Plan §3.4 が裁定を記録、(b) `test_card_reconciliation.py:303` の docstring を「趙 2026-08-21 裁定により恒久的に不着手。理由は本 Plan §3.4」へ、(c) `test_page_disposition_wiring.py:370` の docstring を「T9 から分離、独立課題」へ。**全量テスト緑のまま**（装飾子は外さないので unexpectedSuccess にならない） |
| **U7** | ETC 100 行頁を**特定**し、その頁のトークンを実測 | (a) 録音から ETC 明細頁を `detail_row_count` で特定（100 行級の頁が実在することを示す。無ければ「fixture に存在しない」と明記して最大行数頁で代替し、**その旨を結論に書く**）、(b) 該当 slot の `candidates_token_count` と `finish_reason`（切断の有無）、(c) **余裕 = 1 − used/32768** を百分率で、(d) 全 slot の最大値を補助統計として、(e) thinking tokens と予算を共有する点（`ocr_engine.py:126`）を注記 |

**順序**: U1 ∥ U2（両方 TDD、独立）→ U3 → U4 → U5 ∥ U7。U6 は全体と独立。

## 5. 測試策略（TDD・全局 CLAUDE.md §9）

- **先に RED を出す**。U1/U2 とも失敗するテストから書く。
- **反例の二方向**: 判定器は「壊れているものを FAIL にする」だけでなく「正常なものを PASS にする」ことも証明する。片方だけだと「常に赤」または「常に緑」の判定器が通ってしまう。
- **スキーマ検証**: dump の欄名・型・版を verifier が検証する。欄名のずれを「値が無い＝条件を満たさない」と誤読して緑にしないため。
- **集成/E2E**: U3+U4 が製品の `process_local_file` を実データで駆動する E2E そのもの。
- **覆盖率**: 新規 2 スクリプトも `coverage` で **≥ 80%** を確認。
- **回帰**: 全量 `unittest discover` 緑（Phase 4 の閘門）。

## 6. 影響面

| 対象 | 変更 | リスク |
|---|---|---|
| `dump_e2e_rows.py`（新規） | 追加 | 生産経路から呼ばれない。`main.py` は参照しない |
| `verify_e2e_acceptance.py`（新規） | 追加 | 同上 |
| `test_dump_e2e_rows.py` / `test_verify_e2e_acceptance.py`（新規） | 追加 | `unittest discover` の対象に入る |
| `test_card_reconciliation.py` / `test_page_disposition_wiring.py` | docstring 各 1 箇所 | 挙動変化なし |
| `test_images/credit_card/` `transit_ic/` | 真票のコピー（gitignore 済） | repo に入らない |
| `dump/`（新規） | **顧客の実データを含む。`.gitignore` へ追加必須** | **repo は PUBLIC** |
| 製品コード | **変更なし** | — |

## 7. 風險と回退

| # | リスク | 対策 |
|---|---|---|
| R1 | **dump が顧客実データごと PUBLIC repo に入る** | `.gitignore` に `dump/` を追加し、**番人テストで `git ls-files 'dump/**'` が空を検証**（`fixtures/` の先例と同型。規則の有無ではなく実際の追跡を見る） |
| R2 | dump driver が真 Sheets を汚す | U1 DoD (a) の fail-fast spy |
| R3 | 頁 provenance がずれ A1/A4/A7 が誤判定 | U1 DoD (d)。行番号でなく generator の状態で判別する |
| R4 | 判定器が「常に緑」または「常に赤」になる | U2 DoD (b) の 双方向反例 |
| R5 | replay が実 Gemini を呼び課金される | U3 で「Gemini 呼出 0 回」を確認してから先へ。`PatchTargetContractTest` が改名検知を担う |
| R6 | 判定 FAIL の原因究明で本 Plan の範囲が膨張 | N3/N9 の通り**製品は直さない**。ただし**判定器の欠陥は直す**（§4 U4） |
| R7 | 原票が `shutil.move` で消費される／顧客 PDF が repo ツリーに残る | §3.1 決定 5 のステージング。`TEST_DIR` と `PROCESSED_DIR` を**両方**差し替える（片方だけだと repo 内に落ちる。F14）。U1 DoD (f) で固定 |
| R8 | ETC 100 行頁が fixture に存在しない | U7 DoD (a) で「存在しない」と結論に明記する道を用意済。無いものを在ることにしない |

**回退**: 新規スクリプト 2 本と docstring 2 箇所のみ。`git checkout` で足りる。製品コードを触らないので生産影響はゼロ。

## 8. 完了判定（三層。Codex 第 1R #15 を容れて分離）

| 層 | 内容 | 本 Plan の完了に必要か |
|---|---|---|
| L1 `measurement complete` | dump 採取と verifier 実行が成功し、A1〜A9 の PASS/FAIL 表が出ている | **必要** |
| L2 `verifier sound` | FAIL のうち**判定器の欠陥・dump スキーマの欠陥に起因するもの**が 0 件（見つけたら直して再判定） | **必要** |
| L3 `acceptance pass` | A1〜A9 が全 PASS | **不要**（A8 は F4 により必ず FAIL する） |

**本 Plan の完了 = L1 ＋ L2 ＋ 残る FAIL が全て `product gap` として分類され上申されていること。**
加えて: 全量 `unittest discover` 緑 / 新規 2 スクリプトの覆盖率 ≥ 80% / `git ls-files 'dump/**'` が空。

---

## 附録 A. Codex 第 1 ラウンドの辯論記録

16 件。**採納 11・部分採納 5（駁回成分あり）**。駁回した論点は次ラウンドで回餵する。

| # | 嚴重度 | 指摘 | 裁決 | 理由 |
|---|---|---|---|---|
| 1 | 重大 | A1 が全頁欠落を検出できない（`last_total_pages=0` で欠落行すら出ない） | **採納** | F15 として実測で裏取り。§3.1 決定 3（PDF から独立取得）＋ A1(a)(d) |
| 2 | 重大 | A1 が重複出力を検出しない | **部分採納** | 重複検出は必要だが A1 に混ぜない。**A1b として独立項を新設**。混ぜると FAIL 原因が「漏れ」か「重複」か判別できない |
| 3 | 重大 | A2 が不一致 verdict を実質無視 | **採納** | A2 を期待集合との完全一致に改めた（4 件一致・4 件検算不能） |
| 4 | 中 | A2/A3 の期待値と範囲が曖昧 | **採納** | A3 に `file == ENEOS` を明記。金額は int 化を仕様化 |
| 5 | 中 | A4 の指紋が過検出・判定漏れ | **部分採納** | 指紋を**判定から外し診断へ降格**。頁番号による直接判定の方が強い。ただし「完全キーを定義せよ」は A1b で採用 |
| 6 | 重大 | A7 が片方欠けても PASS | **採納** | A5/A7 とも (file,page) ごとに個別 assert へ |
| 7 | 重大 | A8「判定不能」を受入基準にしている | **採納** | A8 は FAIL と判定する。ただし (a)FAIL / (b)受入集合から除外 のどちらを採るかは**趙の拍板事項**として §3.5 に上申 |
| 8 | 重大 | A9 が件数一致だけ・0 行で vacuous PASS | **採納** | 逐行同値 ＋ 対象行 ≥ 1 件 |
| 9 | 重大 | 「直前 yield」が全 writer 呼出に対応しない | **採納** | F12 として実測で裏取り（264/281/308/343 が循環外）。generator 状態機による provenance へ設計変更 |
| 10 | 中 | patch 対象が壊れやすい | **採納** | F11 として実測で裏取り。`local_test.process_pipeline` を patch 対象と明記 |
| 11 | 中 | U1 DoD の「Sheets に書かない」検証が弱い | **採納** | fail-fast spy（`SheetsOutputWriter.__init__` / `gspread.service_account` / `gspread.authorize`） |
| 12 | 中 | U2 の「当該項だけ FAIL」は過度に強い | **採納** | 「当該項が FAIL すること」を必要条件とし、他項の巻き添えは許容。加えてスキーマ検証と双方向反例を追加 |
| 13 | 中 | U3 の「5 ファイル」が曖昧／move が入力を破壊 | **第 2R で全面採納（Codex 勝）** | 第 1R では「一時ディレクトリ化は N8 違反」として駁回した。第 2R で Codex が**「製品を変えずに実行時ステージングで実現できる」**という新しい論証を出し、`TEST_DIR`/`PROCESSED_DIR` がモジュール級定数である事実（F14）で裏が取れた。**駁回を撤回し採納**。§3.1 決定 5 |
| 14 | 重大 | U7 が ETC 100 行頁を特定していない | **採納** | `detail_row_count` で特定。`finish_reason` も見る。余裕は `1 − used/32768`。全 slot 最大値は補助へ |
| 15 | 中 | 「FAIL を分類すれば完了」は DoD として弱い | **部分採納** | 三層（L1/L2/L3）に分離。**判定器・dump の欠陥は完了不可**（採納）。ただし**製品の実装欠陥は `product gap` として完了可**とする —— N3 は趙が定めた本 Plan の範囲であり、「製品を直すまで完了させない」は範囲の書き換えに当たる（**この 1 点は駁回**） |
| 16 | 軽 | 計測専用計画として過度に複雑 | **部分採納** | dump を `events.jsonl` 1 本に畳む（採納）。**「A8/T9 の仕様変更を E2E から分離せよ」は駁回** —— T9 は docstring 2 行の是正で E2E と無結合。別 Plan を立てる管理コストの方が高い |

## 附録 B. Codex 第 2 ラウンドの辯論記録（駁回 5 件の複審）

第 1R で駁回成分を含んだ 5 件を回餵した。**判定は Codex 自身に 3 択（ACCEPT_REBUTTAL / MAINTAIN / REVISE）で出させ、
MAINTAIN には新しい論証を要求した**（同じ論証の繰り返しを勝ちと認めないため）。

| # | Codex の判定 | 内容 | 最終裁決 |
|---|---|---|---|
| 2 | ACCEPT_REBUTTAL | 重複は A1 の頁漏れと独立した性質。A1b として分離する方が失敗原因を切り分けられる | **我方維持**。A1b を独立項として存置 |
| 5 | ACCEPT_REBUTTAL | 頁番号を直接キーにできるなら指紋より強い。指紋を診断へ降格する判断は妥当 | **我方維持**。指紋は判定に使わない |
| **13** | **REVISE** | N8 は製品コード変更を禁じるが、**実行時に一時ディレクトリへステージングすれば製品変更なしで実現できる** | **Codex 勝・採納**。§3.1 決定 5 を新設。F14 で実現可能性を裏取り |
| 15 | ACCEPT_REBUTTAL | 本 Plan の対象は計測と判定。製品コード変更を完了条件に足すのは範囲の書き換え | **我方維持**。L1+L2+product gap 分類で完了 |
| 16 | ACCEPT_REBUTTAL | T9 の変更は docstring 2 箇所に限定され製品経路と結合しない。同梱は範囲逸脱でない | **我方維持**。本 Plan に同梱 |

**この 2 ラウンドの収支**: 第 1R 16 件のうち採納 11・部分採納 5。第 2R で駁回 5 件を複審し、
**4 件は Codex が指摘を取り下げ（我方維持）、1 件（#13）は新論証により Codex 勝で全面採納**。
全採納でも全駁回でもない —— 勝敗が両方向に出ているので、辯論として機能したと判断する。


---

## 9. 実施結果（2026-08-21）

### 9.1 タスクの終態

| # | 状態 | 証拠 |
|---|---|---|
| U1 `dump_e2e_rows.py` | **完了** | 33 tests 緑 / 覆盖率 **98%** |
| U2 `verify_e2e_acceptance.py` | **完了** | 51 tests 緑 / 覆盖率 **92%** |
| U3 真票の採集 | **完了** | 8 ファイル 32 頁・`mode=replay`（実 Gemini 呼出 0）・`dump/events.jsonl` 553 イベント |
| U4 機械判定 | **完了** | **8/10 PASS**。残 2 件は製品未実装（§9.3） |
| U5 A8 の数値化 | **完了** | 推定年 **113 行**（ニモカ全 6 頁）・標識付き **0 行** |
| U6 T9 の再定義 | **完了** | docstring 2 箇所是正・全量緑のまま |
| U7 ETC トークン実測 | **完了** | §9.5 |
| 全量回帰 | **緑** | `Ran 1537 tests` / `OK (expected failures=2)` |
| `dump/` の秘匿 | **確認** | `.gitignore` 追加 ＋ 番人テスト 2 本（`git ls-files` が空を実測） |

### 9.2 A1〜A9 の判定（機械判定・目視ゼロ）

```
A1   PASS  全 8 ファイルの全頁が 1 件以上出力
A1b  PASS  同一頁の二重書き込みなし
A2   PASS  verdict 内訳 {'一致': 3, '検算不能': 4, '検算不可': 1}
A3   PASS  ENEOS 記帳合計 18503
A4   PASS  重複頁は MF に 0 行
A5   PASS  重複頁 2 件とも監査タブに留痕
A6   PASS  負数 0 件・転正の痕跡なし
A7   PASS  ポイント専用頁 2 件とも監査タブのみ
A8   FAIL  ← product gap（§9.3）
A9   FAIL  ← product gap（§9.3）
```

### 9.3 分類: 製品の欠陥（product gap）2 件

**どちらも「関数は在る・テストも在る・製品の誰も呼んでいない」という同じ形。**
実行は成功し、出力もそれらしく見えるので、機械判定を通さない限り見つからない。

| # | 欠陥 | 実測 | 影響 |
|---|---|---|---|
| **G1**（A8） | `_year_estimated`（`card_entries.py:609/651/655`）を**製品の誰も消費しない**。`anomaly_detector` の 13 種に年推定は無く、`partial_date` は `^\d{4}/\d{2}$` にしか発火しない（推定年は完全形を生む） | ニモカ **113 行**が推定年。U列に標識 **0 行** | nimoca の券面に年は印字されていない。`_nearest_past` が推測した年が「推測である」と顧客に見えない。年跨ぎの誤記帳が無音で通る |
| **G2**（A9） | `invoice_confirm_tag()`（`invoice_classification.py:522`）の**唯一の呼出元がテスト**。1 万円以上の行に `INVOICE_CONFIRM_TAG` を書く経路が製品に無い | ENEOS 11,123 / JCB 18,200・15,780・13,750・23,530・16,495 等が無標識 | 1 万円以上はインボイス確認が要る。タグが出ないと人手確認の起点が無い |

**本 Plan では直さない**（N3。趙 2026-08-21 裁定）。修正は `anomaly_detector` ＋ `tag_rules`
という生産経路の改造なので、別 Plan で趙の拍板を経ること。

### 9.4 分類: 判定器の欠陥 3 件（**全て修正して再判定済み**）

「FAIL を分類して終わり」にすると判定器のバグが製品の欠陥に化ける。§8 の L2 はこれを塞ぐ条項。

| # | 欠陥 | 直し方 |
|---|---|---|
| V1 | driver が `card_file_recon.report_and_record` を包んでおらず **recon イベントが 0 件**。A2 が全滅していた | 戻り値の `FileVerdict` を捕まえる wrapper を追加 |
| V2 | A1b が「同一頁に同額同名の行」を重複と判定。**実測で TS CUBIC p6 に「ETC通行料金/N西日本 760 円」が 5 行あり正当**（同じ料金所を何度も通る） | 判定単位を行から **batch**（`append_entries` 1 回分）へ。同じ頁が二度書かれた場合だけ赤 |
| V3 | A3 が「借方＝未払金」の調整行を記帳合計に算入。ENEOS p2 の「店頭キャッシュバック 3,000 円」を足して 21,503 になっていた | 調整行を母集団から除外 → **18,503**（母 Plan の値と一致） |

加えて driver 側に**番人**を 1 つ入れた: `dumping_run` の後で誰かが
`local_test.process_pipeline` を差し替えると wrapper が無音で外れ、
「このファイルには行が無かった」と区別が付かなくなる。`run_one_file` が
これを検出して中断する（`WrapperDetachmentGuardTest`）。

### 9.5 分類: 基準の過期 2 件

| # | 過期した記述 | 実測 | 根拠 |
|---|---|---|---|
| **O1** | memory `credit-card-doctype-progress` の「**アレコレ＝一致**」 | **検算不能**（`file_recon_page_number`） | 録音 3 頁の `statement_page` は `"2/2"` / `"1枚目"` / （無し）。`card_file_recon._segments` は「どれか 1 頁でも n が読めなければ段を確定しない」設計なので**検算不能が正しい挙動**。memory の記録は Gemini が揺れた別の回の観測とみられる（記録再生を入れた理由そのもの）。ニモカも「検算不能」ではなく **検算不可**（`RECON_POLICY` の count_only） |
| **O2** | 母 Plan T11 の「`GEMINI_MAX_OUTPUT_TOKENS` **32768** に対する余裕」 | 逐行記帳 doc_type は **65536** を使う | `ocr_engine._line_generation_config()` が `config.GEMINI_MAX_OUTPUT_TOKENS_BULK = 65536` を適用する（クレカ・交通系IC）。T11 が書かれた後に入った変更 |

### 9.6 U7: ETC 頁の出力トークン実測

**「ETC 100 行頁」は本 fixture に存在しない。** 最多は 58 行（`fixtures/full/0009`・`0010`）。
無いものを在ることにしないため、最大行数頁で代替した旨をここに明記する。

| slot | 明細行 | prompt | 本文 | total | 本文＋thinking | 65536 に対する使用率 | 32768 なら |
|---|---|---|---|---|---|---|---|
| 0009 | 58 | 3,896 | 8,294 | 31,197 | **27,301** | **41.7%**（余裕 58.3%） | 83.3% |
| 0010 | 58 | 3,992 | 8,679 | 21,175 | 17,183 | 26.2% | 52.4% |
| 0011 | 54 | 4,214 | 8,210 | 21,823 | 17,609 | 26.9% | 53.7% |

- **切断は 1 件も無い**: 全 32 slot の `finish_reason` が `1`（STOP）、`raises` は 0 件
- **thinking が本文より重い**: slot 0009 は thinking ≒ 19,007 に対し本文 8,294（2.3 倍）
- **thinking は安定しない**: 同じ 58 行でも 19,007（0009）と 8,504（0010）で **2.2 倍の開き**。
  よって「1 行あたり N tokens」の線形外挿は**信用できない**（推測。根拠はこの 2 標本の開き）
- **もし 32768 のままなら余裕は 16.7% しか無かった**。`_BULK = 65536` への引き上げは
  実測で裏が取れた形になる

### 9.7 完了判定（§8 の三層）

| 層 | 判定 |
|---|---|
| L1 `measurement complete` | **達成**。dump 採取・verifier 実行とも成功、PASS/FAIL 表が出ている |
| L2 `verifier sound` | **達成**。判定器の欠陥 3 件（V1〜V3）は全て修正して再判定した。残る FAIL に判定器起因は無い |
| L3 `acceptance pass` | **未達（想定どおり）**。A8・A9 が product gap のため |

**本 Plan の完了条件（L1 ＋ L2 ＋ 残 FAIL の分類・上申）を満たした。**

### 9.8 持ち越し

**趙 2026-08-21 裁定: G1・G2 とも「後置。立項しない」。**
本 Plan の成果は「2 つの穴を数字で台帳に載せた」ところまでであり、
塞ぐか否かは別の判断として保留された。着手する日が来たら、まず
`verify_e2e_acceptance.py` の A8・A9 を回して現状の FAIL を再現すること
（判定式は既に「実装されたら緑になる」形で書いてある）。

| # | 事項 | 優先度 | 状態 |
|---|---|---|---|
| G1 | 年推定に異常マークを付ける（`anomaly_detector` ＋ `tag_rules`） | P1 | **後置**（趙 2026-08-21）— 113 行が無標識。年跨ぎの誤記帳が無音 |
| G2 | 1 万円以上の行に `INVOICE_CONFIRM_TAG` を書く経路を作る | P1 | **後置**（趙 2026-08-21）— 人手確認の起点が出ない |
| — | `test_page_disposition_wiring.py:370`（T9 から分離した独立課題） | P2 |
| — | memory `credit-card-doctype-progress` の「アレコレ＝一致」を訂正 | P2 |
| — | TBD-2（ラベル拡充 4/7 → 7/7）・TBD-5（OCR 拒否権の継承） | P2 |

---

## 附録 C. 実施後評審の辯論記録（/simcodex rounds=1）

Codex 1 本 ＋ simplify の 4-agent panel（reuse / simplification / efficiency / altitude）。
**P0 は 0 件。** 重複を除いた P1 は 9 件で、8 件採納・1 件駁回。

| 出処 | 指摘 | 裁決 | 理由 |
|---|---|---|---|
| Codex | patch の設置が `try/finally` の外。途中で失敗すると復元されず他テストへ漏れる | **採納** | 設置と解除を同じ try に入れた。`PatchLifecycleTest` で例外経路の復元を固定 |
| Codex | `_ACTIVE` が単一 global。入れ子で内側の後始末が外側を消す | **採納** | 入れ子を明示的に拒否（静かに混ざるより止める）。5 本のテストで固定 |
| reuse | `_WriterCapture.build()` が `test_sheets_output._make_writer` を**4 本目の手抄ファクトリ**として再実装している | **採納** | あちらの docstring が「3 本の手抄が非同期化した反省」を記録し、`test_sheets_output_golden.py:36` は既に同じ理由で借りている。借りる側へ変更 |
| reuse | `_FakeWorksheet` を手書きせず既存を継承すべき（`test_sheets_output_line_mode.py:53` に先例） | **採納** | 継承へ変更。legend 行の逐字コピーが消えた |
| reuse | `VERDICT_*` の硬編碼に番人が無い。`card_reconciliation` は gspread 非依存なので import できる | **部分採納** | **import はしない**（判定器は dump だけで走れる独立性を保つ。読むのは events.jsonl であって製品の import 鎖ではない）。代わりに `test_verdict_words_match_the_reconciliation_module` を新設し、産地で語を変えたら赤くなるようにした。**独立性を選んだ対価がこの番人テスト** |
| simplify | `EXPECT_UNVERIFIABLE` が死定数。allowlist に見えて `_a2` は暗黙 else で判定していた | **採納** | 起動時に 3 分割の網羅性・排他性を検査（違反なら RuntimeError）。`_a2` も明示分岐へ。9 本目を足して分割を直し忘れると起動時に落ちる |
| simplify | `detail_row_count` を誰も読まない。`row_brace_count` と同じ量を 2 通りで計算 | **採納** | `row_brace_count` に一本化 |
| simplify | `_resolve_tab` の patch は冗長（実物は純粋な振り分けで spreadsheet を触らない） | **採納** | 削除。注釈も実態に合わせた |
| simplify | `_audit_tab_name` は誰も読まない死代入 | **採納** | 削除 |
| simplify | `COL_DATE` / `AUDIT_COL_VERDICT` は判定に使われず、番人テストのためだけに在る | **採納** | 削除。番人テストの該当行も同時に削除 |
| altitude | 2 つのファイルが同じラベルへ落ちても検出しない。`_View` が静かに上書きし、混ざった結果が偶然 PASS しうる | **採納** | `label_collisions` を検出し A2 を FAIL に。`LabelCollisionTest` 2 本で固定 |
| altitude | `append_entries` の wrap は `_batch` のためだけ。`_write_with_retry` へ下ろせる | **駁回** | 指摘者自身が caveat を挙げている ——`_with_tab_recovery` の retry 時、現行は 1 batch のまま、下ろすと 2 batch に割れる。A1b が見たいのは「同じ頁を二度**書いた**」であって「書き込み API が二度呼ばれた」ではない。fake では再現しない差だが、語義が曖昧になる側へは動かさない |
| efficiency | P0/P1 なし。`collect_token_usage` が `gemini_record` の読んだ JSON を再読するが数十 ms、かつ修正には保護対象の `gemini_record` API 変更が要る | **採納（＝何もしない）** | 指摘者の結論に同意 |

### 採納した P2（1 件）

| 指摘 | 対応 |
|---|---|
| Codex ＋ simplify: ファイル境界の分隔行は `ws.append_row` で書かれ `_write_with_retry` を通らないので**捕まらない**。既存テストは全て単一ファイルでこの経路を一度も通っていない | docstring を「全ての行」→「データ行」に訂正（明細ではないので判定の母集団にも入らないのが正しい）。`MultipleFilesShareOneTabTest` 3 本を追加。真の dump は 7 ファイルが 1 タブを共有するので、この経路は本番で必ず通る |

### 持ち越した P2

- `_PageCursor.in_loop` は `page is not None` から導出可能（冗長状態）
- `_noop` 4 本は `format_cell_range` のモジュール級無効化と重複（**二重の防波堤として意図的に残す**旨を注釈済み）
- `_a4`/`_a7`、`_a5`/`_a7` に共通の反復イディオム
- `_a8` が「観測対象が無い」と「在るが誤っている」を同じ FAIL に畳んでいる（メッセージは区別される）
- `label_of` の部分一致は 9 本目の標本で衝突しうる（衝突検出は A2 に入れたので無音では通らない）

### 評審後の再検証

| 項目 | 結果 |
|---|---|
| 全量テスト | `Ran 1552 tests` / `OK (expected failures=2)` |
| 覆盖率 | `dump_e2e_rows` **98%** / `verify_e2e_acceptance` **92%** |
| 真票 E2E 再走 | 553 イベント・**8/10 PASS**（評審前と逐字一致。判定は動いていない） |
