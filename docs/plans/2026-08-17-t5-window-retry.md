# Plan: T5 出力切断の窓分割リトライ ＋ 行欠け検出

- 起案: 2026-08-17
- 母 Plan: `docs/plans/2026-08-12-credit-card-doctype.md` §5 T5（`:496-505`）
- 直前の状態: `0d791be`（split-pdf §12.1）で 808 tests 緑
- 趙裁定 2026-08-17: 赤系マークの落点は **MF タブの金額 0 提示行 ＋ 監査タブ 1 行**
  （明細行は標色しない）

母 Plan の T5 は「プロンプトに `rows_on_page` を出力させ、取得行数と突合する。
不足または `finish_reason == MAX_TOKENS` なら `line_no` レンジを指定した窓分割で
再取得し、`line_no` をキーにマージする。窓数上限に達しても不足なら赤系マークを
立てる」。本 Plan はそれを実装可能な粒度へ落とす。

---

## 0. 事実表（すべて本 session のコマンド出力・4 面並行調査で確認済み）

### 0.1 実測値

| # | 事実 | 根拠 |
|---|---|---|
| F1 | `gemini-2.5-flash` の出力硬上限は **65,536**（入力 1,048,576） | `genai.list_models()` を venv311 で実行 |
| F2 | 現行 `GEMINI_MAX_OUTPUT_TOKENS = 32768` は硬上限の半分 | `ocr_engine.py:135` |
| F3 | 32768 の根拠コメントは「thinking 動的上限 24,576 ＋ JSON 本文 <2k」。**逐行記帳ではこの前提が崩れる** | `ocr_engine.py:133` |
| F4 | ETC 明細の出力トークン実測（同一 tokenizer で等価テキストを `count_tokens`）: 40 行 = **5,483** / 60 行 = **8,123** / 100 行 = **13,406** / 150 行 = **20,083** / 300 行 = **40,183** | `model.count_tokens()` を実行 |
| F5 | ゆえに **100 行頁は thinking が上限まで使うと 24,576 + 13,406 = 37,982 > 32,768 で必ず切断**。65,536 なら余裕 27,554 | F1/F3/F4 の算術 |
| F6 | **300 行頁は 65,536 でも 64,759（98.8%）でほぼ天井** —— 上限引き上げだけでは救えない | 同上 |
| F7 | `CC_WINDOW_SIZE = 40` は **上限を上げなくても安全**（5,483 + 24,576 = 30,059 < 32,768） | 同上。既定値 40 に実測の裏付けが付いた |
| F8 | SDK `google-generativeai==0.8.5` は **thinking budget を制御できない**（`GenerationConfig` の 10 フィールドに無し、`types` にも該当型無し） | `dir()` と `__dataclass_fields__` を実行 |

**F8 の帰結**: 「thinking 予算を削って本文に回す」案は**現行依存では不可能**。
実現には SDK を `google-genai` へ載せ替える必要があり、全 Gemini 呼出に波及する
依存変更（全局 §1 で要申請）。本 Plan は採らない。

### 0.2 現行コードの事実

| # | 事実 | 根拠 |
|---|---|---|
| G1 | `_get_finish_reason` / `_is_max_tokens_truncated` は**既に在る**が、**ログ文字列を作るためだけ**に使われている | `ocr_engine.py:226-242`、呼出点は `:360` と `:362` の 2 箇所のみ（どちらも `_parse_gemini_response` 内） |
| G2 | `_parse_gemini_response` の戻り値は原因を問わず **`None` 一種類**。呼出側は「MAX_TOKENS 截断」「非 JSON 応答」「候補ゼロ」を**区別できない** | `ocr_engine.py:351-368` |
| G3 | `extract_json` は截断 JSON に対し**部分パースを一切しない**。7 種の截断形態すべてで `None` | `ocr_engine.py:176-209` を実測（値の途中／内側 `}` 直後／配列トップ／閉じ括弧ゼロ／フェンス截断／parts 空） |
| G4 | **截断すると `rows_on_page` すら取れない**（JSON 全体が `None` になるため） | G3 の帰結。**本 Plan の設計を決定づける** |
| G5 | 截断後の現行挙動: Vision 兜底が**同一予算で同一頁を再送** → また截断 → `_page_error_payload("AI応答のJSON解析失敗")` | `ocr_engine.py:2363-2369`, `:2379-2388` |
| G6 | その占位は一時的ネットワーク障害と**字面まで同一**。全頁失敗ならファイル保持 → 3 秒後に**同じ予算で再試行** → 無限ループ（毎周 Gemini 2 回） | `main.py` の終態判定 ＋ G5 |
| G7 | `_generate_content_with_retry(contents)` に **generation_config の口が無い**。呼出単位で予算を変える手段はグローバル dict 書換のみ | `ocr_engine.py:114-130` |
| G8 | 再試行の引き金は**例外だけ**。`finish_reason == MAX_TOKENS` は HTTP 200 の正常応答なので再試行対象外 | `ocr_engine.py:98-130` |
| G9 | `line_no` は **prompt schema に既にある**（両 doc_type 共通、「頁内の通番 1 から」） | `card_prompts.py:71` |
| G10 | `rows_on_page` も**既にある**。CC は「取得できた数ではなく**券面に見えている数**」と明記 | `card_prompts.py:56-57`（CC）/ `:66`（IC） |
| G11 | ただし **`rows_on_page` の production 消費者はゼロ**。`card_entries.CONSUMED_TOP_KEYS` にも入っていない | `card_entries.py:73` |
| G12 | `line_no` は `card_entries.py:486` で `_line_no` として entry に載るが、**その先で誰も読んでいない** | `grep -rn "_line_no"` の非テストヒットが 1 行のみ |
| G13 | `CC_WINDOW_SIZE` / `CC_MAX_WINDOWS` / `GEMINI_MAX_OUTPUT_TOKENS_BULK` は config に在るが**読み取り点ゼロ** | `config.py:234-244`、母 Plan `:713` |
| G14 | `test_credit_card_config.UnwiredItemsTest` が「**未結線であること**」を積極的に固定している。T5 で結線した瞬間このテストが赤くなる | `test_credit_card_config.py:152-153` |
| G15 | `sheets_output` に `append_reconciliation_row` は**無い**。監査タブの口は `append_audit_row`（7 列、verdict は 除外／分岐／欠落 の 3 種） | `sheets_output.py:32-34`, `:583` |
| G16 | 100 行規模の raw_data フィクスチャは**無い**（最大 6 行 = `AMEX_A_P2_RAW`）。テキスト側には `test_page_family._rows(100, 630)` の先例が在る | `ocr_test_fixtures.py`、`test_page_family.py:247` |
| G17 | `page_dedup` / `card_reconciliation.FileReconLedger` は **production 経路から一度も呼ばれていない**（T9 待ち） | 非テスト呼出 0 件 |

### 0.3 守らねばならない既決事項（再議しない）

| 出典 | 内容 | T5 への拘束 |
|---|---|---|
| 母 Plan `:40` 裁定 3 | 明細相加 ≠ 合計 → **照常記帳 ＋ 赤系マーク**（Failed にしない） | 行が欠けてもファイルを Failed にしてはならない |
| 母 Plan `:48-49` 裁定 7 / AD-7 | 検算不一致の赤系マークは監査タブの**カード単位 1 行**。明細行は標色しない | 明細 62 行に赤タグを付けない |
| 母 Plan `:71-94` AD-0 | 頁の去向は単一の解析関数が決める。優先序 2「`entry_count > 0` → 必ず記帳」 | **行が足りないことを理由に頁を落とさない** |
| 母 Plan `:303-311` AD-10 | 外貨は `jpy_amount` のみ記帳 | 窓分割マージで `foreign_amount` を金額に昇格させない |
| 母 Plan `:149-160` AD-4 | 区画 `sec` は Gemini が出す | 窓で再取得した行も `sec` を保持しないと検算が壊れる |
| 母 Plan `:529-544` T9 DoD | AD-0 の移行完了。`_yield_page_results` に第 2 の裁決点を作らない | T5 が裁決点を増やすと T9 が達成不能になる |
| 母 Plan `:716-719` | `CC_TAX_TYPE_RENDERING` は **T6 の出力層** | T5 で触らない |
| IP-401 Plan `:156-160` | **T5 の窓分割は builder を流式に変えうる。そうなると「1 件目 yield 成功 → 2 件目で例外」→ count>0 → Success → 歸檔 で真の無音欠落**（現在の 0 件 → 保持より悪い） | **本 Plan の最重要制約。§3.2 で構造的に封じる** |
| 母 Plan `:695` | `GEMINI_MAX_OUTPUT_TOKENS_BULK` は check_models で**実測してから**入れる | F1/F4/F5 で充足済み |
| 趙裁定 2026-08-17（本 session） | 赤系マークは **MF タブ金額 0 提示行 ＋ 監査タブ 1 行** | §3.5 |

---

## 1. 目標

1. **H1**: ETC 100 行級の頁で出力が切断されても、**窓分割で全行を取り切る**
2. **H2**: 取り切れなかったとき、**顧客が帳簿を見るだけで気づく**（MF 提示行）。
   黙って少ない行数で成功にしない
3. **H3**: 窓数上限で**無限ループしない**
4. **H4**: 既存 doc_type（receipt 等）の挙動を **1 ミリも変えない**

## 2. 非目標

- `page_dedup` / `FileReconLedger` の production 結線（T9）
- `sheets_output` の `line_mode` ゲート（T6）
- `CC_TAX_TYPE_RENDERING`（T6 の出力層）
- 異常検知のタグ粒度そのものの再設計（T8）
- SDK 載せ替えによる thinking budget 制御（F8。依存変更は別途申請）
- `_line_no` を Sheets へ出すこと（T6 の行級化の範囲）

---

## 3. 設計

### 3.1 截断は「行が少ない」ではなく「何も無い」（G4 が設計を決める）

母 Plan の文面「取得行数と `rows_on_page` を突合する」は、**截断時には成立しない**。
`extract_json` が `None` を返すので `rows_on_page` すら読めないからである（G3/G4）。

ゆえに引き金は **2 種類**に分かれる:

| 引き金 | 検出方法 | 起きる状況 |
|---|---|---|
| **T-a 截断** | `raw_data is None` **かつ** `_is_max_tokens_truncated(response)` | 100 行頁で thinking が予算を食った（F5） |
| **T-b 行欠け** | `raw_data` は有効だが `len(rows) < rows_on_page` | Gemini が自分で読み飛ばした |

T-a では総行数が不明なまま窓分割を始めることになる。これは**問題にならない**:
第 1 窓（`line_no` 1〜40）の応答は 5,483 トークン規模（F7）で截断しないので、
そこに載る `rows_on_page`（＝**券面に見えている総数**、G10）で必要窓数が確定する。

```
  截断 → 窓1(1-40) 要求 → 応答に rows_on_page=100 → 残り窓数 = ceil((100-40)/40) = 2
       → 窓2(41-80) → 窓3(81-100) → マージ
```

### 3.2 変更の位置 —— `_route_ocr_strategy` の下、`_yield_page_results` の上

窓分割は **raw_data 層で完結**させ、マージ済みの raw_data を既存の経路へ渡す。

```
_route_ocr_strategy() ──> PageOcr(raw_data=None, truncated=True)
                              │
                              ▼
                    _retry_by_windows()      ← 新設（本 Plan の中核）
                              │  Gemini を窓ごとに呼び、line_no でマージ
                              ▼
                    raw_data（全行そろった dict）
                              │
                              ▼
                    _yield_page_results()  ← **無改造**
                              │
                              ▼
                    card_entries.build_entries_from_*  ← **無改造**
```

**この位置を選ぶ理由**（IP-401 Plan `:156-160` の警告への構造的回答）:

builder（`card_entries`）は「完全な raw_data を受け取って entries を返す」という
契約のまま**一切変わらない**。窓分割の存在を知らない。よって
「1 件目 yield 成功 → 2 件目で例外 → count>0 → Success → 歸檔」という
**新しい無音欠落の様式は生まれない**。マージが失敗しても、それは
`_retry_by_windows` が返す raw_data の中身が足りないだけで、
yield の途中で壊れることはない。

代案として「builder を流式にして窓ごとに yield する」も考えられるが、
まさに上記の欠陥を生むので**採らない**。

### 3.3 `_retry_by_windows` の契約

```python
def _retry_by_windows(page_data, mime_type, doc_type, base_raw_data=None):
    """出力切断・行欠けを窓分割で埋める。**完全な raw_data を返すか、
    埋めきれなかった事実を添えて返す**（例外は投げない）。

    Returns:
        (raw_data, shortage) の 2-tuple。
        raw_data: マージ済み dict。1 行も取れなければ None。
        shortage: None（充足）または
                  {"expected": int|None, "got": int, "windows": int}
    """
```

- **例外を投げない**（`observe_page` の fail-open と同じ思想。窓分割の失敗が
  記帳経路を壊してはならない）
- **`line_no` をキーにマージ**。同一 `line_no` が複数窓から来たら**先勝ち**
  （窓は重ならない設計だが、Gemini が範囲指示を守らない場合の保険）
- **`sec` / `foreign_amount` / `currency` / `fx_rate` はそのまま持ち越す**（AD-4 / AD-10）
- **top 級フィールド**（`card` / `sections` / `printed_totals` / `total_amount` /
  `rows_on_page`）は**最初に取れた窓のものを採用**する。窓ごとに `card` が
  揺れると `page_dedup` の identity が壊れる（G17 で未結線だが、T9 で結線した
  瞬間に発火する）
- **窓数上限 `CC_MAX_WINDOWS` で必ず止まる**（H3）。上限に達しても
  `expected` に届かなければ `shortage` を返す

### 3.4 予算の呼出単位化（G7 の解消）

`_generate_content_with_retry` に省略可能引数を 1 本足す:

```python
def _generate_content_with_retry(contents, generation_config=None):
    cfg = generation_config or GEMINI_GENERATION_CONFIG
```

`line_mode` doc_type（`ocr_engine.py:1746` の `LINE_MODE_DOC_TYPES`、既に定義済み）
の初回呼出では `GEMINI_MAX_OUTPUT_TOKENS_BULK` を使う。窓分割の呼出は
既定予算のままでよい（F7 より 40 行なら 32768 で足りる）。

**既存 doc_type への影響はゼロ**: 既定は `GEMINI_GENERATION_CONFIG` のままで、
`line_mode` でない doc_type は新しい経路に一切入らない（H4）。

### 3.5 埋めきれなかったときの表現（趙裁定）

`shortage` が非 None のとき、**2 箇所**に痕跡を残す:

1. **MF タブ**: 金額 0 の提示行を 1 行。
   文言案 `⚠ 明細行の取得漏れ p{page}: {expected}行中{got}行のみ取得（手動確認要）`
   先例は AD-0 優先序 4 の `cc_summary`（母 Plan `:88-92`「MF タブへ金額 0 の提示行」）
2. **監査タブ**: `append_audit_row(verdict=AUDIT_VERDICT_LINE_SHORTAGE, ...)`。
   新 verdict 定数「行欠落」を `sheets_output.py:32-34` に 1 つ足す

**明細行（取れた 62 行）は照常記帳し、標色しない**（裁定 7 / T8 DoD）。
**ファイルは Failed にしない**（裁定 3）。頁も落とさない（AD-0）。

### 3.6 `rows_on_page` を初めて消費する（G11 の解消）

`card_entries.CONSUMED_TOP_KEYS` に `rows_on_page` を追加する必要は**無い**
—— 消費するのは builder ではなく `_retry_by_windows`（ocr_engine 側）だから。
ただし `test_card_entries.py:132,145` の AST 突合テストは
「builder が読むキー ⊆ prompt の生成 schema」を見ているので、
この追加では壊れない（T4 Plan `:735-742` で `==` から `⊆` へ訂正済み）。

### 3.7 config 3 項目の結線（G13/G14）

| キー | 既定値 | 本 Plan での扱い |
|---|---|---|
| `CC_WINDOW_SIZE` | 40 | そのまま結線（F7 で妥当性を実測確認） |
| `CC_MAX_WINDOWS` | 8 | そのまま結線（320 行まで） |
| `GEMINI_MAX_OUTPUT_TOKENS_BULK` | **0（＝既存値流用）** | **要・趙拍板**（下記） |

**`GEMINI_MAX_OUTPUT_TOKENS_BULK` を 65536 にすべきか**:
F5 より 100 行頁は 32768 では必ず切れる。65536 にすれば窓分割の発動自体が
稀になり、Gemini 呼出回数が減る（1 回 vs 3 回）。実測の裏付けはある。
**ただしこれは運維パラメータの変更**なので、読み取り足場は本 Plan で入れつつ、
**値の変更は趙の拍板を待つ**（0 のままでも窓分割が救うので機能は成立する）。

結線した瞬間 `test_credit_card_config.UnwiredItemsTest` が赤くなる（G14）。
**それが正しい反応**であり、Plan と母 Plan §9.5 の実数（9/13 → 12/13）を
更新するのが正しい対応（母 Plan `:413-419` が明記）。

### 3.8 無限ループの構造的封じ込め（H3）

3 段で止める:

1. `CC_MAX_WINDOWS` の窓数上限（`for` の回数が有限）
2. 窓が **0 行を返したら打ち切る**（券面が申告より短かった場合）
3. `expected` が取れないまま第 1 窓も截断した場合は **1 回で諦める**
   （`shortage = {"expected": None, ...}`）—— 総数不明のまま窓を回し続けない

さらに、G6 の既存の無限ループ（ファイル保持 → 3 秒後に同じ予算で再試行）は
本 Plan で**改善される**: 窓分割が成功すれば記帳されて歸檔されるため。

---

## 4. タスク清単（TDD。各項に DoD）

### T5-1: 100 行フィクスチャ（RED の土台）

`ocr_test_fixtures.py` に `etc_rows_raw(n)` を追加（`AMEX_A_P1_RAW` の内包表記
パターンを伸ばす。`_cc_row(i+1, ...) for i in range(n)` ＋ `"rows_on_page": n`）。

**DoD**: stdlib のみ import（`ocr_test_fixtures.py:1-18` の設計宣言）／
`test_dependency_weight.py` が緑のまま／`etc_rows_raw(100)` が 100 行・
`rows_on_page=100` を返す。

### T5-2: 截断検出を戻り値へ（G2 の解消。RED → GREEN）

`_parse_gemini_response` の呼出側が「截断」と「非 JSON」を区別できるようにする。
既存シグネチャを壊さないため、`PageOcr` に `truncated: bool` を足す方向で
検討する（`_route_ocr_strategy` が組み立てる箇所は 1 つ）。

**DoD**: 截断応答で `truncated=True`、非 JSON 応答で `False`、正常応答で `False`。
既存の `test_ocr_engine_max_tokens.py` が**無修正で緑**。

### T5-3: `_generate_content_with_retry` の予算引数（§3.4）

**DoD**: 引数省略時は現行と完全に同一（既存テスト無修正で緑）／
明示時はその config が SDK へ渡ることをモックで固定。

### T5-4: `_retry_by_windows` 本体（§3.3。中核）

**DoD**:
- 100 行フィクスチャで、第 1 窓が `rows_on_page=100` を返せば 3 窓で全行そろう
- `line_no` の重複は先勝ちでマージされる
- `sec` / 外貨 3 フィールドが保持される
- top 級は最初の窓のものが採用される
- **例外を投げない**（Gemini が全窓で失敗しても `(None, shortage)` を返す）
- 窓数上限で必ず止まる（呼出回数を mock で数える）
- 窓が 0 行を返したら打ち切る

### T5-5: 逐頁ループへの結線（§3.2）

`process_pipeline` の逐頁分岐で、`line_mode` doc_type かつ截断/行欠けのときだけ
`_retry_by_windows` を通す。

**DoD**: 既存 doc_type は新経路に**一度も入らない**（mock で `assert_not_called`）／
`_yield_page_results` と `card_entries` は**無改造**／
`test_ip401_regression` / `test_pdf_split_contract` が無修正で緑。

### T5-6: 赤系マークの落点（§3.5。趙裁定）

MF 提示行 ＋ 監査タブ新 verdict「行欠落」。

**DoD**:
- `shortage` 有りで MF タブに金額 0 の提示行が 1 行出る
- 同時に監査タブに 1 行出る
- **明細行に赤タグが付かない**（T8 DoD の先取り検査）
- `process_file` が **True**（歸檔）を返す＝ Failed にしない（裁定 3）
- 取れた 62 行は**照常記帳される**（AD-0 優先序 2）

### T5-7: config 3 項目の結線（§3.7）

**DoD**: 各モジュールが自前既定値を持ち config を override として読む
（母 Plan `:670-681` のパターン）／`UnwiredItemsTest` の `UNWIRED` から 3 項目を
外し、結線実数を 9/13 → 12/13 に更新／母 Plan §9.5 も更新。

### T5-8: 回帰と変異検証

**DoD**: 全量緑 ＋ 下記の変異が全件赤:

| 変異 | 落ちるべき検査 |
|---|---|
| 窓数上限を無視して回し続ける | T5-4 の上限テスト |
| `line_no` マージを後勝ちにする | T5-4 |
| `shortage` 非 None でも MF 提示行を出さない | T5-6 |
| 明細行に赤タグを付ける | T5-6 |
| 既存 doc_type も新経路へ通す | T5-5 |
| 截断時に `truncated` を立てない | T5-2 |
| `sec` を窓マージで落とす | T5-4 |
| 窓 0 行でも打ち切らない | T5-4 |

---

## 5. 受入基準（脚本判定）

```bash
cd "/Users/ibridgezhao/Documents/Super Scaner"
venv311/bin/python -m unittest discover -p "test_*.py"      # → OK, Ran >= 808
venv311/bin/python -m unittest test_ip401_regression -v     # → OK（無修正）
venv311/bin/python -m unittest test_pdf_split_contract -v   # → OK（無修正）
venv311/bin/python -m unittest test_card_entries -v         # → OK（無修正）
venv311/bin/python -m unittest test_card_prompts -v         # → OK（無修正）
python3 -m unittest test_card_entries test_card_prompts     # → venv 無しでも OK
```

人手判定:
- `card_entries.py` / `card_prompts.py` の**builder 契約が無改造**
- `sheets_output.py` は verdict 定数 1 個の追加のみ
- 既存 doc_type の経路に 1 行も差分が無い

---

## 6. 影響面

| 対象 | 影響 |
|---|---|
| `ocr_engine.py` | `_retry_by_windows` 新設、`_generate_content_with_retry` に引数 1 本、`PageOcr` に `truncated`、逐頁ループに結線。母 Plan `:595` が「危険度 高」と評価している箇所 |
| `card_prompts.py` | 窓範囲を指示する prompt 変種を生成する関数（既存定数は不変） |
| `card_entries.py` | **無改造**（§3.2 の設計目的） |
| `sheets_output.py` | verdict 定数 1 個 |
| `config.py` | 読み取り足場（値の変更は §3.7 の拍板待ち） |
| 既存 doc_type | **完全に無変化**（`line_mode` でないので新経路に入らない） |
| Gemini 呼出回数 | 截断時のみ増える（1 → 最大 1+8）。`GEMINI_MAX_OUTPUT_TOKENS_BULK=65536` なら発動自体が稀に |

## 7. 風険と回退

| 風険 | 対処 |
|---|---|
| 窓分割が Gemini の範囲指示無視で全行返し、毎窓が截断する | 窓応答が `CC_WINDOW_SIZE` を大きく超えたら以後の窓を打ち切り `shortage` へ倒す |
| マージ後の raw_data が `page_dedup` の指紋を変える | 現時点で未結線（G17）。T9 の結線時に「窓分割済み頁の指紋」を再検討する残件として登録 |
| `GEMINI_MAX_OUTPUT_TOKENS_BULK` を上げると thinking も膨らむ可能性 | 実測（F4/F5）は本文側のみ。T11 の E2E で実応答の `usage_metadata` を採取して検証する |
| 回退 | 単一 commit。`line_mode` でない doc_type には 1 行も影響しないので、revert の影響範囲は新 doc_type に閉じる |

## 8. §12.2 の B/C（共有 generator 化）をどうするか

split-pdf Plan §12.2 は「T5 の着手判断まで持ち越す」としていた。**本 Plan の
設計では不要**と判断する: §3.2 のとおり窓分割は `_route_ocr_strategy` と
`_yield_page_results` の**間**に入り、`process_pipeline` の paged/tail 骨格にも
`_split_pdf_pages` にも触れないため、split-pdf Plan `:635-641` が予測していた
「T5 は prompt と builder の層で骨格は触らない見込み」がそのまま成立する。

B/C は独立タスクとして残す（判断材料は「骨格改造の risk を今払うか後で払うか」
だけ、という同 Plan の記述も変わらない）。

---

## 9. 評審記録（2026-08-17。**§3〜§7 は作廃。実装前に再設計が必要**）

Codex（`codex exec`）＋ 4 視角の対抗評審を回した結果、**§3 の設計は前提から
作り直す必要がある**と判明した。本節を読まずに §3〜§7 を実装してはならない。

### 9.1 最重要 —— 当方の事実誤認（窓分割の存在理由が消える）

**§0.1 F6「300 行頁は 65,536 でも天井」は誤り。** 「300 行」は**カード単位**
（4 頁の合計）であって頁単位ではない:

| 出典 | 逐語 |
|---|---|
| `docs/plans/2026-08-12-credit-card-sample-facts.md:107` | `ENEOS BUSINESS ETC \| 4 頁（2/5〜5/5） \| 1 頁 60〜100 行 → 計 250〜300 行` |
| 母 Plan `:242` | 「不一致**カード**の全行を遡及標色すると ETC 300 行が…」 |
| 母 Plan `:527` | 「ETC 300 行フィクスチャで赤系タグが 300 個付かない」＝ T8 の標色範囲 |
| 母 Plan `:502-505` | T5 の DoD 自体は「ETC **100 行**フィクスチャ」しか要求していない |
| `card_prompts.py:140` | prompt は「以下は…**1 ページ分**です」。1 呼出 = 1 頁 |

**算術**（F4 の実測 13,406 tok / 100 行 ＝ 1 行 ≒ 134 tok）:
`GEMINI_MAX_OUTPUT_TOKENS_BULK = 65,536` なら無分割上限は
`(65,536 − 24,576) ÷ 134 ≈ **305 行/頁**`。実物の最悪頁（100 行）に対し **3.0 倍の余裕**。

→ **65536 に上げるだけで、実物では窓分割が一度も発火しない。**
§3〜§7 の 7 割はこの誤認の上に積まれている。

**さらに F5 の「必ず切断」も過剰**（Codex P2-8）。thinking が常に上限まで使う
保証は無く、`count_tokens` は等価テキストの概算。「worst case では切断し得る」が
正しい強度。

### 9.2 §3 の設計が壊れる箇所（窓分割を残す場合に必ず直す）

| # | 指摘 | 根拠 |
|---|---|---|
| A | **`line_no` はマージキーとして成立しない**。`card_prompts.py:71` は「この頁の中での**通番（1 から）**」＝ Gemini 採番。窓 2 に「41〜80 を返せ」と頼んでも自分の 40 行に 1〜40 を振り直す蓋然性が高く、§3.3 の先勝ちは**窓 2 の新規 40 行を全捨て**する（8 呼出を焼いて `got` は 40 のまま） | `card_prompts.py:71` |
| B | **T-b の判定式 `len(rows) < rows_on_page` は健全な頁でも恒常発火**。`rows` は合計行・小計行・ポイント区画を除く（`card_prompts.py:147-150`）が `rows_on_page` は券面の印字行総数。定義上ずれる | `card_prompts.py:56-57,147-150` |
| C | **`rows_on_page` を first-win にすると 60 行が無音で消える**。窓 prompt は「1〜40 だけ返せ」を含むので LLM は `rows_on_page: 40` と答えやすい → `expected=40, got=40` → shortage なし。T5 DoD(b) の直撃違反 | 設計上の推論（`card_prompts.py:56-57`） |
| D | **`printed_totals` / `total_amount` も first-win 不可**。券面の合計は頁**下部**にあり窓 1 は空で返す → 窓 3 が取った合計を捨てる → CC の `POLICY_AMOUNT_REQUIRED` が「合計取れず＝OCR 失敗」へ倒れる | `card_reconciliation.py:79` |
| E | **`sec` は窓ローカルな `sections` 配列の添字**。窓ごとに配列が違えば同じ `sec=0` が別区画を指し、検算が偽の赤へ | `card_entries.py:212-219`, `card_prompts.py:93` |
| F | **結線が逐頁分岐だけで尾段が漏れている**。単頁クレカ PDF・画像は `ocr_engine.py:2515` 以降を通る | Codex P1-4 |
| G | **Vision 兜底との関係が未定義**。`raw_data` falsy 直後に兜底が無条件で走る（`:2363-2368`）。窓分割と両方走ると 1 頁で最大 10 呼出＋画像 10 回再送 | `ocr_engine.py:2363-2368` |
| H | **§7 の「窓応答が CC_WINDOW_SIZE を大きく超えたら打ち切り」は原理的に到達不能**。行数を数えるには parse が要るが、超過応答は截断して `extract_json` が None（G3）。検出できるのは 41〜60 行の軽微な overshoot だけ | G3/F4 |
| I | **第 1 窓も截断した場合、G6 の無限ループが悪化**（Gemini 呼出が 2 回/周 → 3 回/周）。§3.1 の「第 1 窓は截断しない」と §3.8-3 の「截断したら諦める」は矛盾 | §3.1 vs §3.8 |

### 9.3 窓分割より安い代替（評審が提示）

`card_prompts.py:155-166` の schema 順序は `card → 上位フィールド（`rows_on_page` を含む）
→ `rows` が**最後**。よって MAX_TOKENS で切れた応答テキストにも
`"rows_on_page": 100` と**完結した行オブジェクトが N 個**残っている。
`extract_json` の all-or-nothing（G3）が全部捨てているだけ。

→ **截断時だけ「最後の完結した `}` までを救う」サルベージ解析**を足せば、
Gemini を 1 度も追加で叩かずに N 行 ＋ `expected` が取れる。
コストは約 20 行のパーサ vs 最大 8 回の追加呼出。

**失うもの**: Gemini が schema の鍵順序を守る保証は無い（推測）。ただし窓分割も
「Gemini が範囲指示を守る」という同種の仮定に立っているので、仮定の強さは同等。

### 9.4 その他（窓分割の有無に関わらず有効）

- **T5-2（`PageOcr.truncated`）は落とせる**: `truncated` の真偽で取るべき行動が
  変わらない（`raw_data is None` なら原因を問わず「別条件で取り直す」）。
  可視性も `_parse_gemini_response:364-367` が既に `finish_reason=` を印字しており
  1 ミリも失われない
- **`shortage` を bare dict にするのは repo の慣習に反する**。既存の「結果＋診断」は
  `str | None`（`card_reconciliation._resolve_printed_count:651-657` 他 4 例）か
  NamedTuple（`CardVerdict`）。dict は `.get()` の綴り誤りが静かに None になり、
  `PageOcr` を 3-tuple から dataclass へ移した理由と正面衝突する
- **監査タブの新 verdict「行欠落」は不要**（過度設計 ＋ 既存の「欠落」と 1 文字違いで
  混同を招く）。既存の `_audit_signal` 機構（`ocr_engine.py:2185` → `main.py:596-609`）が
  まさに「記帳は通したうえで監査タブに 1 行」を実装済みで、`reason` 列は機械可読
  キーと定義されている（`sheets_output.py:31`）ので `"line_shortage:62/100"` が
  そのまま入る。**`sheets_output.py` が無改造になる**
- **T5-7（config 結線）は他と独立**。`UnwiredItemsTest` を赤くする破壊的変更なので
  単独 commit が切り戻しやすい
- **T9 結線時に頁級（T5）とカード級（`FileReconLedger` の `VERDICT_COUNT_NG`）で
  赤系マークが二重に立つ**。どちらを残すか T9 で決める残件
- **部分取得頁は重複判定の資格を持たせない**（`shortage` 非 None の頁は
  `safe_fingerprint` を呼ばない）を T9 への申し送りに。現行 §7 の「指紋が変わる」
  だけでは弱い —— 実際の帰結は「同一原紙の 2 度スキャンで digest が一致せず
  **両方記帳＝二重計上**」

### 9.5 次 session への申し送り（この順で）

1. **趙へ 1 問だけ先に確認する**: `GEMINI_MAX_OUTPUT_TOKENS_BULK` を **65536 に
   上げるか**。上げるなら §9.1 より窓分割は実物で不要になり、T5 は
   「BULK 結線 ＋ 行欠け検出 ＋ サルベージ解析」に縮む（実装量 7 割減）。
   上げないなら 100 行頁が毎回切れるので窓分割は必須。
   **現 Plan は両方に賭けて両方を作る形になっており、それが誤り**
2. 未読の評審 2 本を読む: `decree-conflicts`（既決事項との衝突検査）と
   `test-teeth`（変異検証の穴）。本 session では時間切れで未読。
   全文は `~/.claude/projects/-Users-ibridgezhao-Documents-Super-Scaner/
   21250a1c-cbb1-4714-add4-f80a40998637/subagents/workflows/wf_e7f3a3ca-d6c/journal.jsonl`
   の `{"type":"result"}` 行（フィールド名は `key` と `result`）
3. 上の結論を踏まえて §3〜§7 を書き直し、再度 Codex 評審にかける
4. **§0.1 F5/F6 を訂正する**（F6 は削除、F5 は「worst case では切断し得る」へ）
