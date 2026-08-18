# Plan: T5 出力切断のサルベージ解析 ＋ 行欠け検出（窓分割は廃案）

- 起案: 2026-08-17 ／ 再設計: 2026-08-18（§9 の評審結論を受けて §3〜§7 を全面書換）
- 母 Plan: `docs/plans/2026-08-12-credit-card-doctype.md` §5 T5（`:496-505`）
- 直前の状態: `cc09aaa`（本 Plan 初稿 commit）時点で **808 tests 緑**（2026-08-18 実測）
- ファイル名の `window-retry` は歴史的経緯（初稿の窓分割案 → §9 評審で廃案）。改名しない。

## 裁定（このセッションで確定。再議しない）

| 日付 | 裁定 | 帰結 |
|---|---|---|
| 2026-08-17 趙 | 赤系マークの落点は **MF タブの金額 0 提示行 ＋ 監査タブ 1 行**（明細行は標色しない） | §3.5 |
| 2026-08-18 趙 | **`GEMINI_MAX_OUTPUT_TOKENS_BULK` を 65536 に上げる**（選択肢 1）。窓分割は作らない | 本 Plan 全体。T5 は「BULK 結線＋行欠け検出＋サルベージ解析」に縮小 |

窓分割を作らない根拠（§9.1）: 「300 行」はカード単位（4 頁合計）の誤認で、実物の
最悪頁は 100 行。65536 なら無分割で約 305 行/頁を容れられ（3.0 倍の余裕）、
**窓分割は実物で一度も発火しない**。発火しないコードは検証不能な死蔵経路になる。

---

## 0. 事実表

### 0.1 実測値（2026-08-17。F5/F6 は §9.1 により 2026-08-18 訂正済み）

| # | 事実 | 根拠 |
|---|---|---|
| F1 | `gemini-2.5-flash` の出力硬上限は **65,536**（入力 1,048,576） | `genai.list_models()` を venv311 で実行 |
| F2 | 現行 `GEMINI_MAX_OUTPUT_TOKENS = 32768` は硬上限の半分 | `ocr_engine.py:135` |
| F3 | 32768 の根拠コメントは「thinking 動的上限 24,576 ＋ JSON 本文 <2k」。**逐行記帳ではこの前提が崩れる** | `ocr_engine.py:133-134` |
| F4 | ETC 明細の出力トークン実測（等価テキストを `count_tokens`）: 40 行 = **5,483** / 60 行 = **8,123** / 100 行 = **13,406** / 150 行 = **20,083** / 300 行 = **40,183**（1 行 ≒ 134 tok） | `model.count_tokens()` を実行 |
| F5 | **（訂正）** 100 行頁は thinking が上限まで使う worst case で 24,576 + 13,406 = 37,982 > 32,768 となり**切断し得る**（「必ず切断」は過剰。thinking が常に上限まで使う保証は無く、`count_tokens` は等価テキストの概算）。65,536 なら worst case でも余裕 27,554 | F1/F3/F4 の算術 ＋ §9.1（Codex P2-8） |
| F6 | **（削除）** 「300 行頁は 65,536 でも天井」は誤り。300 行はカード単位（4 頁合計）で、頁単位の実測最悪は 100 行。65,536 の無分割上限は `(65,536 − 24,576) ÷ 134 ≈ 305 行/頁` | §9.1 の出典 4 点 |
| F7 | （窓分割廃案により参考値）40 行 = 5,483 + 24,576 = 30,059 < 32,768 | F4 |
| F8 | SDK `google-generativeai==0.8.5` は **thinking budget を制御できない** | `dir()` と `__dataclass_fields__` を実行 |

**F8 の帰結**: 「thinking 予算を削って本文に回す」案は現行依存では不可能。SDK 載せ替えは
依存変更（全局 §1 で要申請）であり本 Plan は採らない。

### 0.2 現行コードの事実（G 表。2026-08-17 採取、2026-08-18 に一部訂正）

| # | 事実 | 根拠 |
|---|---|---|
| G1 | `_get_finish_reason` / `_is_max_tokens_truncated` は既に在るが、ログ文字列を作るためだけに使われている | `ocr_engine.py:226-242`、呼出点は `_parse_gemini_response` 内の 2 箇所のみ |
| G2 | `_parse_gemini_response` の戻り値は原因を問わず `None` 一種類。呼出側は截断と非 JSON を区別できない | `ocr_engine.py:351-368` |
| G3 | `extract_json` は截断 JSON に対し部分パースを一切しない。7 種の截断形態すべてで `None` | `ocr_engine.py:176-209` を実測 |
| G4 | 截断すると `rows_on_page` すら取れない（JSON 全体が `None`） | G3 の帰結 |
| G5 | 截断後の現行挙動: Vision 兜底が同一予算で同一頁を再送 → また截断 → `_page_error_payload("AI応答のJSON解析失敗")` | `ocr_engine.py:2363-2368`, `:2379-2388` |
| G6 | その占位は一時的ネットワーク障害と字面まで同一。全頁失敗ならファイル保持 → 3 秒後に同じ予算で再試行 → 無限ループ（毎周 Gemini 2 回） | `main.py:644-654` ＋ G5 |
| G7 | `_generate_content_with_retry(contents)` に generation_config の口が無い | `ocr_engine.py:114-130` |
| G8 | 再試行の引き金は例外だけ。`finish_reason == MAX_TOKENS` は HTTP 200 の正常応答なので再試行対象外 | `ocr_engine.py:98-130` |
| G9 | `line_no` は prompt schema に既にある（両 doc_type 共通、「この頁の中での通番（1 から）」） | `card_prompts.py:71` |
| G10 | `rows_on_page` も既にある。CC は「取得できた数ではなく**券面に見えている数**」と明記。**IC の説明にはこの句が無い** | `card_prompts.py:56-57`（CC）/ `:66`（IC） |
| G11 | `rows_on_page` の production 消費者はゼロ（card_entries / ocr_engine / card_reconciliation / page_dedup のどこも読まない） | grep 実測（2026-08-18 再確認） |
| G12 | `line_no` は `card_entries.py:486` で `_line_no` として entry に載るが、その先で誰も読んでいない | grep 実測 |
| G13 | `CC_WINDOW_SIZE` / `CC_MAX_WINDOWS` / `GEMINI_MAX_OUTPUT_TOKENS_BULK` は config に在るが読み取り点ゼロ | `config.py:234-244`、grep 4 ヒット全部が定義とテスト |
| G14 | **（訂正）** 「結線した瞬間 `UnwiredItemsTest` が赤くなる」は誤り。番人の `WATCHED` は 6 ファイルで **`ocr_engine.py` を含まない**ため、ocr_engine 側で結線しても緑のまま＝無音。正しい手順は §3.6 | `test_credit_card_config.py:156-158`, `:167-177`（§9.4 所見 L） |
| G15 | `sheets_output` の監査タブの口は `append_audit_row`（7 列、verdict は 除外/分岐/欠落 の 3 種）。タブ名は `_` 開頭必須（GAS が毎晩 22:00 に非 `_` タブを削除） | `sheets_output.py:28-34`, `:583-584` |
| G16 | 100 行規模の raw_data フィクスチャは無い（最大 6 行 = `AMEX_A_P2_RAW`） | `ocr_test_fixtures.py:105-119` |
| G17 | `page_dedup` / `card_reconciliation.FileReconLedger` は production 経路から一度も呼ばれていない（T9 待ち） | 非テスト呼出 0 件 |

### 0.3 守らねばならない既決事項（再議しない）

| 出典 | 内容 | T5 への拘束 |
|---|---|---|
| 母 Plan `:40` 裁定 3 | 明細相加 ≠ 合計 → 照常記帳＋赤系マーク（Failed にしない） | 行が欠けてもファイルを Failed にしない |
| 母 Plan `:48-49` 裁定 7 / AD-7 | 赤系マークは監査タブのカード単位 1 行。明細行は標色しない | 明細行に赤タグを付けない |
| 母 Plan `:71-94` AD-0 | 頁の去向は単一の解析関数が決める。優先序 2「`entry_count > 0` → 必ず記帳」 | 行不足を理由に頁を落とさない。**shortage 提示行は「去向」ではなく同一頁への追加注記であり、Disposition 軸には乗らない**（§3.5） |
| 母 Plan `:303-311` AD-10 | 外貨は円貨 `amount` のみ記帳（`jpy_amount` というキーは存在しない） | サルベージが行 dict を改変しない（丸ごと保全） |
| 母 Plan `:149-160` AD-4 | 区画 `sec` は Gemini が出す | サルベージは単一応答内の切出しなので `sections`/`sec` の名前空間は一貫（窓分割で壊れた点は構造ごと消滅） |
| 母 Plan `:529-544` T9 DoD | `_yield_page_results` に第 2 の裁決点を作らない | 提示行 yield は裁決ではなく注記（§3.5 に明記） |
| 母 Plan `:716-719` | `CC_TAX_TYPE_RENDERING` は T6 の出力層 | T5 で触らない |
| IP-401 Plan `:156-160` | builder を流式にすると「部分 yield 後の例外 → Success → 歸檔」の真の無音欠落 | builder は list 返しのまま**無改造**（§3.3）。実測: `_build` は list を return（`card_entries.py:501-585`）、全か無か |
| 母 Plan `:695` | BULK は実測してから入れる | F1/F4/F5 実測済み ＋ 趙拍板 2026-08-18 |
| IP-401 Plan `:211-217` | 「AI は応答した／確定的な認識失敗」は `_unrecognized` → 歸檔（`_page_error` は一時障害用） | 截断は決定的性質 → 全滅時は `_unrecognized` へ（§3.3） |
| 母 Plan §4 | `ocr_engine.py`（現 2571 行）にこれ以上積まない | サルベージ本体は新モジュール `card_salvage.py` へ（§3.2） |

### 0.4 2026-08-18 核験事実（V 表。3 並行調査 agent ＋ 本体読解で採取）

| # | 事実 | 根拠 |
|---|---|---|
| V1 | prompt の JSON schema は **`rows` が最後**（CC: card → sections → printed_totals → rows_on_page → total_amount → rows ／ IC は sections 無し同順）。テンプレートは `_render` ＋ `%` 埋込で、`rows_on_page` の説明変更は CC/IC 各 1 箇所 | `card_prompts.py:156-166`, `:181-184`, `:205-215` |
| V2 | `_write_unrecognized_row` は `_unrecognized` が立った payload の `memo` を**そのまま S 列（摘要, row[18]）へ出し**、タグは自動で `UNRECOGNIZED_TAG`＝「赤系」（U 列, row[20]） | `sheets_output.py:757-778`, `tag_rules.py:24-25` |
| V3 | `append_entries` は entries 空（または全行金額 0/None）で `_write_unrecognized_row` → `APPEND_RESULT_PLACEHOLDER` を返す。main はそれを `OUTCOME_PLACEHOLDER` として数え、`_page_error` ではないので Failed に寄与しない → **歸檔** | `sheets_output.py:310-321`, `main.py:587-590`, `:640-654`, `:1111-1112` |
| V4 | 金額 0 の entry は `append_entries` 内で行単位に無音 skip される（`if not amount or int(amount) == 0: continue`）。ゆえに「金額 0 の提示行」を明細経路で書くことは**不可能**——提示行は `_unrecognized` payload 経路でしか書けない | `sheets_output.py:249-252` |
| V5 | `_audit_signal` 機構は完備: producer が result に `"_audit_signal": <reason 文字列>` を載せると、main が記帳成功後に監査タブへ verdict「分岐」で 1 行書く。失敗は警告 print のみ（帳簿を人質に取らない）。既存 reason は `"envelope_signal_with_entries"` | `ocr_engine.py:2183-2187`, `main.py:596-609` |
| V6 | 同一頁から複数 result を yield するのは既存能力（RECEIPT の封筒分岐が実施済み）。main は yield 1 件＝1 迭代で独立処理。ただし `count` は頁数でなく yield 数を数える（表示の分母が 1 増える。Failed 判定は `error_pages == count` なので非エラー result の追加は無害） | `ocr_engine.py:2183-2189`, `main.py:505-511`, `:644` |
| V7 | 取引No は **append_entries 呼出 1 回につき 1 つ**（行ごとではない）。`_write_unrecognized_row` も 1 つ消費。提示行は明細の次番号になる | `sheets_output.py:244`, `:270-271`, `:399-403`, `:762`, `:805` |
| V8 | `test_ocr_engine_max_tokens.py` の呼出は**全部位置引数**で、assert は戻り値のみ。3 変体＋`_generate_content_with_retry` に**既定値付き末尾引数を足しても全緑**。ただし `GenerationConfigTest` が (a) `GEMINI_MAX_OUTPUT_TOKENS == 32768` の字面と (b) 既定呼出の `generation_config` を固定——既定挙動は 1 バイトも変えられない | `test_ocr_engine_max_tokens.py:73-94`, `:130-164` |
| V9 | `UnwiredItemsTest` の検査は WATCHED 6 ファイルの**文字列 grep**（`from config import X` / `config.X`）。`PLAN_SECTION_9_5` 13 項目表は `:29-43`。`test_bulk_token_limit_is_zero_until_measured` が BULK==0 を固定（`:185-187`） | `test_credit_card_config.py` |
| V10 | `_MUST_STAY_LIGHT`（12 項目）に `ocr_test_fixtures` と `test_credit_card_config` は**入っていない**。番人は関数内 import を意図的に見ない（規約で縛る） | `test_dependency_weight.py:35-50`, `:89-91` |
| V11 | builder は list 返し・全か無か（途中例外で部分結果は返らない）。`CONSUMED_TOP_KEYS = ("card", "rows", "sections", "printed_totals")`、`rows_on_page` は不使用。AST 突合は `card_entries.py` だけを走査するので ocr_engine 側の消費は視界外＝緑のまま | `card_entries.py:72`, `:501-585`, `test_card_entries.py:132-227` |
| V12 | `_blank_result(date, vendor, **markers)` は markers で `memo` を上書きできる（既存使用例: `_blank_result(_unrecognized=True, memo="⚠ AI応答形式不正…")`） | `ocr_engine.py:2051-2064`, `:2118-2123` |
| V13 | 逐頁ループと尾段は**同じ** `_route_ocr_strategy` を通る（`:2356` / `:2507`）。Vision 兜底は逐頁 `:2363-2368`・尾段 `:2515-2517` | `ocr_engine.py` 実読 |
| V14 | 現在の全量テストは **Ran 808 / OK** | venv311 で 2026-08-18 実行 |

---

## 1. 目標

1. **H1**: ETC 100 行級の頁で出力が切断されない（BULK=65536 の結線。worst case でも 3 倍余裕）
2. **H2**: それでも切断・行欠けが起きたとき、**行を 1 行でも多く救い**（サルベージ）、
   取り切れなかった事実を**顧客が帳簿を見るだけで気づく**形で残す（MF 金額 0 提示行＋監査タブ 1 行）。
   黙って少ない行数で成功にしない
3. **H3**: 截断で無限ループしない（截断は決定的失敗 → `_unrecognized` で歸檔。`_page_error` 保持→3 秒再試行の環を断つ）
4. **H4**: 既存 doc_type（receipt 等）の挙動を **1 ミリも変えない**

## 2. 非目標

- **窓分割リトライ**（§9.1 により廃案。`CC_WINDOW_SIZE` / `CC_MAX_WINDOWS` は削除する——§3.6）
- `page_dedup` / `FileReconLedger` の production 結線（T9）
- `sheets_output` の `line_mode` ゲート・行級 A/B/F/T/H 列（T6）
- `CC_TAX_TYPE_RENDERING`（T6）
- 異常検知のタグ粒度の再設計（T8）
- SDK 載せ替えによる thinking budget 制御（F8）
- `_line_no` を Sheets へ出すこと（T6）
- 取引No の行級化（T6。**テストで「N 行 → N 取引No」を書いてはならない**——現行は呼出単位採番、V7）

---

## 3. 設計

### 3.1 予算の結線（H1 の主対策）

`_generate_content_with_retry` に省略可能引数を 1 本足す（G7 の解消）:

```python
def _generate_content_with_retry(contents, generation_config=None):
    cfg = generation_config or GEMINI_GENERATION_CONFIG
```

予算の選択は新ヘルパ 1 つ（ocr_engine 内。数行）:

```python
def _line_generation_config():
    """line_mode doc_type 用の generation_config。BULK=0/未設定なら None（既定を流用）。"""
    import config
    bulk = getattr(config, "GEMINI_MAX_OUTPUT_TOKENS_BULK", 0)
    if not bulk or bulk == GEMINI_MAX_OUTPUT_TOKENS:
        return None
    return {**GEMINI_GENERATION_CONFIG, "max_output_tokens": bulk}
```

**構造で守る 2 点**:
- BULK=0（流用の意味）が SDK に `max_output_tokens=0` として渡ると**全応答が即截断**する
  最悪の回帰になる（§9.4 所見 M）。`not bulk → None → 既定` の形にして 0 が SDK へ
  届く経路を作らない。変異検証で固定（§4 T5-9 #8）
- 既定引数の既定挙動は 1 バイトも変えない（V8 の `GenerationConfigTest` が番人）

### 3.2 サルベージ解析（新モジュール `card_salvage.py`）

**根拠（§9.3）**: prompt schema は `rows` が最後（V1）。MAX_TOKENS で切れた応答テキスト
にも、`card` / `sections` / `printed_totals` / `rows_on_page` / `total_amount` と
**完結した行オブジェクト N 個**が残っている。`extract_json` の all-or-nothing（G3）が
全部捨てているだけ。截断時だけ「完結した部分までを救う」解析を足せば、
**Gemini を 1 度も追加で叩かずに** N 行＋総数が取れる。

新モジュール `card_salvage.py`（**stdlib のみ import**。母 Plan §4 の「ocr_engine に
積まない」裁定に従う。venv 無しで単体テスト可能＝母 Plan A11 のカバレッジ対象に入る）:

```python
SALVAGED_KEY = "_salvaged_truncation"   # ocr_engine が salvage 経由の dict に立てる内部旗

def salvage_truncated_json(text):
    """截断 JSON から「完結した部分」だけを回収する。

    Returns: dict（回収できた top 級フィールド＋完結した rows 要素のみ）| None。
    例外は投げない（fail-open。observe_page と同じ思想）。
    """

class LineShortage(NamedTuple):
    expected: int | None   # 券面の明細行総数（rows_on_page。取れなければ None）
    got: int               # len(raw_data["rows"])
    salvaged: bool         # 截断サルベージを経たか

def detect_shortage(raw_data):
    """行欠けの判定。shortage 無しなら None。"""

def shortage_memo(shortage): ...        # MF 提示行の S 列文言
def shortage_audit_reason(shortage): ...  # 監査タブ reason（機械可読 "line_shortage:62/100"）

def salvaged_audit_reason(raw_data):
    """salvaged だが充足（shortage 無し）のときだけ "salvaged:{got}/{expected}" を返す。
    それ以外は None。§3.4 の「監査タブのみ」分岐の唯一の出所（Codex R2 #3）。"""
```

**`got` の数え方（Codex R2 #2）**: `got` は **builder が実際に見る行数と同じ定義**
——`rows` が list でなければ 0、list 内の **dict 要素のみ**を数える。サルベージ
出力は構造上 list-of-dict を保証するが、T-b 経路（Gemini が有効 JSON で `rows` に
dict/str/null を返した場合）に `len()` 直読みだと破損要素を行数と誤認する。

**サルベージの完了判定規則（会計数値の破損防止。最重要）**:
メンバー／配列要素を「完結」と認めるのは、**元テキスト上でその直後に `,` `}` `]`
のいずれかが続く場合のみ**。理由: `"amount": 630` が `"amount": 63` の位置で切れた
テキストは、そのまま閉じ括弧を補うと**有効な JSON かつ誤った金額**になる。
数値・文字列の途中で切れた要素は丸ごと捨てる（行を 1 行失う方が、金額が 1 桁
欠ける より遥かに安全）。変異検証で固定（§4 T5-9 #4/#15）。

その他の契約:
- fenced code block（```json）内でも動く（`extract_json` と同じ前処理）
- 行 dict は**丸ごと保全**（キーの白名単を作らない。§9 評審 2-B P3-a——白名単は
  T6/T8 で行キーが増えたとき無音で落とす）
- `rows` に一度も到達していない截断では、回収できた top 級だけの dict
  （`rows` 無し）を返してよい——呼出側で `rows` を `[]` に正規化する
- 純関数・副作用なし・遅延 import なし（V10 の番人の死角を規約で縛る）

### 3.3 起動条件と経路（どこで発火し、失敗したらどうなるか）

サルベージは **`_parse_gemini_response` の層**でのみ発火する。response オブジェクトと
生テキストを両方握っているのはここだけであり（G1/G2、6-A）、ここなら
`PageOcr` にも 3 変体の戻り値契約にも触れずに済む（§9.4 所見 P の解決形）:

```python
def _parse_gemini_response(response, salvage=False):
    ...
    parsed = extract_json(text)
    if parsed is None and salvage and _is_max_tokens_truncated(response):
        recovered = card_salvage.salvage_truncated_json(text)   # None もあり得る
        recovered = recovered if isinstance(recovered, dict) else {}
        recovered.setdefault("rows", [])
        recovered[card_salvage.SALVAGED_KEY] = True
        print(...)   # サルベージ結果のログ（回収行数）
        return recovered
    ...
```

3 変体＋`_call_gemini` は末尾に `line_mode=False` を 1 本ずつ足し、
(a) `_generate_content_with_retry(..., generation_config=_line_generation_config() if line_mode else None)`、
(b) `_parse_gemini_response(response, salvage=line_mode)` の 2 点に配線する。
**`line_mode` は予算選択と salvage 許可を同時に決める単一の旗**である。ただし
BULK=0/既定値のときは予算のみ既定へ縮退し、salvage は常に有効（§7 の「BULK を
0 に戻されても機能は退行しない」はこの縮退のこと。Codex R2 #6 で文言を正確化）。

**`.text` アクセサの扱い（Codex R2 #1 の裁決）**: SDK 0.8.5 の実装読解により、
`response.text` が ValueError を投げるのは **parts が空（または candidates 空/複数）
のときだけ**で、parts が非空なら finish_reason=MAX_TOKENS でも全 part の text を
連結して返す（`generation_types.py` の `text`/`parts` property 実読）。つまり
截断で本文が部分的に出ている場合、現行の text 取得がそのまま部分 JSON を届ける。
parts フォールバックヘルパは到達不能な死代碼になるため**作らない**（Codex 撤回済み）。

旗を立てる場所（V13 により逐頁と尾段の両方が自動的に覆われる）:
- `_route_ocr_strategy` 内の戦略 A/B/C の 3 呼出＋戦略 B の低置信 Vision
  （`ocr_engine.py:2024-2036`）: `line_mode=(actual_doc_type in LINE_MODE_DOC_TYPES)`
- 逐頁ループの Vision 兜底（`:2367`）と尾段の Vision 兜底（`:2517`）:
  `line_mode=(page_ocr.actual_doc_type in LINE_MODE_DOC_TYPES)`

**全滅時の分類（H3。§9 評審 5-E の採用）**: 截断かつサルベージが 1 行も回収でき
なくても、戻り値は `{"rows": [], SALVAGED_KEY: True}` という**真値の dict**である。
帰結:
1. `if not page_raw_data:` が偽 → **Vision 兜底は発火しない**。截断は「頁の内容が
   長い」という決定的性質であり、同一モデル・同一予算での画像再送は同じ截断に
   終わる（G5 の環）。BULK=65536 では thinking 上限 24,576 を引いても本文に
   40,960 残る——そこで切れる応答に再送の期待値は無い
2. `_yield_page_results` → builder(rows=[]) → entries 空 → `_unrecognized` 占位行
   （§3.5 の shortage 文言入り）→ `APPEND_RESULT_PLACEHOLDER` → **歸檔**（V3）。
   `_page_error` による「保持 → 3 秒後再試行 → 毎周 Gemini 2 回」の無限ループ
   （G6）は line_mode 截断について**構造的に消滅**する
3. 非 line_mode の截断は今までどおり `None` → Vision 兜底 → `_page_error`
   （**1 バイトも変えない**。H4）

### 3.4 行欠け検出（T-b）と `rows_on_page` の再定義

**現行定義の欠陥（§9.2-B）**: CC の `rows_on_page` は「券面に見えている数」だが、
`rows` は合計行・小計行・ポイント区画を**除外**する（`card_prompts.py:147-150`）。
定義が揃っていないので `len(rows) < rows_on_page` は健全な頁でも恒常発火する。

**対処**: 両 prompt の `rows_on_page` の説明文を「**rows に入れるべき明細行の数**」に
揃える。物理アンカー（券面）は保持する——「取得できた数」を数えさせると検出が
循環するため（CC の既存の括弧書きの意図）:

- CC（`card_prompts.py:56-57`）: 「この頁の券面に印字された**明細行**の総数
  （rows に入れるべき行の数。合計行・小計行・ポイント区画は数えない。
  **取得できた数ではなく券面に見えている数**）」
- IC（`card_prompts.py:66`）: 「この頁の券面に印字された**明細行**の総数
  （rows に入れるべき行の数。入金行も数える。ポイント列・手書き線は数えない。
  **取得できた数ではなく券面に見えている数**）」

変更点は各 1 箇所（V1）。キー名・既存の逐語固定文言・カンマ規約は不変なので
`test_card_prompts` は無修正で緑（agent 核験 Q11）。新規に「整合句が両 prompt に
在ること」を固定するテストを足す。

**判定規則（`card_salvage.detect_shortage`）**:

| 条件 | 判定 |
|---|---|
| `expected` が正整数 かつ `got < expected` | shortage（T-a 截断後も T-b 行飛ばしも同じ式に合流） |
| `SALVAGED_KEY` が立っている かつ `expected` 不明 | shortage（`expected=None`。「分からない＝問題なし」に**倒さない**——§9 評審 M4） |
| `SALVAGED_KEY` が立っている かつ `got >= expected` | shortage ではないが**監査タブのみ** 1 行（reason `"salvaged:{got}/{expected}"`）。Gemini が総数を過少申告した上で截断した場合の保険——MF は汚さず、内部痕跡だけ残す |
| それ以外 | None |

**誤検出のリスクと受容**: Gemini が総数を誤読すれば偽の提示行が出る（推測）。
受容できる理由: 落点は非破壊（照常記帳＋提示行 1 行＋監査 1 行。裁定 3/7）で、
Failed にも標色にもならない。過少申告側は上記の salvaged 保険で拾う。

### 3.5 痕跡の落点（趙裁定 2026-08-17 の実装。sheets_output / main は無改造）

`_yield_page_results` の非領収書分岐（`ocr_engine.py:2192-2200`）を、line_mode かつ
shortage のときだけ **2 payload** にする:

```
result = _build_doc_result(doc_type, raw_data, builder(raw_data))
shortage = card_salvage.detect_shortage(raw_data)   # line_mode のみ。他は None
if shortage が無い:
    audit = card_salvage.salvaged_audit_reason(raw_data)   # salvaged 充足の保険（§3.4）
    if audit: result に _audit_signal / _ocr_text_len を付与
    yield result                                     （それ以外は現行どおり 1 バイト不変）
if shortage かつ entries あり:  yield result（明細。無改造）
                               yield 提示行 payload              （追加注記）
if shortage かつ entries 空:    result に memo / _audit_signal を併合して yield（1 payload のまま）
```

（audit-only 分岐を疑似コードに明記——§3.4 の表と本節の齟齬を Codex R2 #3 が指摘、採用）

提示行 payload は `_blank_result(date=result の date, vendor=result の vendor,
_unrecognized=True, memo=shortage_memo(...), _audit_signal=shortage_audit_reason(...),
_ocr_text_len=len(ocr_text or ""))`（V12 の既存機構そのまま）。

**この設計が既存機構だけで趙裁定を満たす根拠**:
- MF タブ金額 0 提示行＋赤系: `_unrecognized` payload → `_write_unrecognized_row` が
  memo を S 列へ、タグを自動で「赤系」に（V2）。金額 0 skip（V4）に阻まれる
  明細経路は**使わない**（§9 評審 所見 A の採用）
- 監査タブ 1 行: `_audit_signal` → main が verdict「分岐」・reason 逐語で 1 行（V5）。
  新 verdict 定数は**作らない**（§9.4——既存「欠落」と 1 字違いの「行欠落」は混同の元。
  reason 列は機械可読キーの既存規約なので `"line_shortage:62/100"` で足りる）
- 明細 62 行は照常記帳・無標色: 明細 result は 1 バイトも触らない。shortage を
  `red_flags`/`doc_red` に**流してはならない**（流すと `tag_rules.py:58-61` で全行赤
  ＝裁定 7 違反。変異検証 §4 T5-9 #11 で固定）
- Failed にしない: 提示行は PLACEHOLDER であって `_page_error` ではない（V3）
- `_excluded_page` は**使わない**（§9 評審 1-A）: あれは「記帳しない」チャネルで、
  entries 付きの頁に立てると main が `continue` して 62 行が丸ごと消える
  （`main.py:543-566`）——AD-0 優先序 2 の正面違反

**AD-0 / T9 との整合（1-B の採用）**: 提示行は頁の**去向**ではない。去向（記帳）は
明細 result が担い、提示行はその**後ろに追加される注記行**である。T9 で
`resolve_page_disposition` へ通す対象外。順序は必ず「明細 → 提示行」
（逆にすると取引No が 注記→明細 の順になる。§9 評審 4 P3-b）。

**副作用（許容して仕様として固定）**: 提示行 payload の分だけ main の `count` が
1 増え、取引No を 1 つ消費し、**進捗集計の outcome も yield 単位で増える**——
1 物理頁が `POSTED=1 ＋ PLACEHOLDER=1` になり、頁数と outcome 合計は一致しない
（`page_progress` の page_num は set で重複排除、outcome は yield 単位。Codex R2 #4）。
RECEIPT の複数 result と同型の既存挙動であり、Failed 判定（`error_pages == count`）
には影響しない。この可観測な挙動は T5-7 の process_file 級テストで**仕様として固定**する。

### 3.6 config の結線と後片付け

| キー | 現状 | 本 Plan での扱い |
|---|---|---|
| `GEMINI_MAX_OUTPUT_TOKENS_BULK` | 0（読み取り点ゼロ） | **65536 に変更**（趙拍板 2026-08-18）＋ ocr_engine が結線（§3.1）。コメントの「実測してから」条件は F1/F4/F5＋拍板で充足済みと書き換える |
| `CC_WINDOW_SIZE` | 40（読み取り点ゼロ） | **削除**（窓分割廃案。発火しない設定は誤解の温床） |
| `CC_MAX_WINDOWS` | 8（読み取り点ゼロ） | **削除**（同上） |

削除の影響面（全て本 Plan 内で更新する）: `config.py:234-244` のブロック、
`test_credit_card_config.py` の `UNWIRED`（`:152-153`）と `PLAN_SECTION_9_5` 表
（`:29-43`）と `test_bulk_token_limit_is_zero_until_measured`（`:185-187`。
「65536＝実測済み・拍板済み」を固定する形へ書換）、母 Plan §9.5 の実数。
production 読取点はゼロ（V9 grep）なので削除でコードは壊れない。

**追跡の保全（Codex R2 #5 採用）**: `PLAN_SECTION_9_5` は単にリストから消さず、
「現存 config keys」と「**廃止済み historical keys**（CC_WINDOW_SIZE /
CC_MAX_WINDOWS、廃止理由＝窓分割廃案 §9.1）」に分割する。さらに再追加を検知する
番人 `assertFalse(hasattr(config, "CC_WINDOW_SIZE"))` / 同 `CC_MAX_WINDOWS` を置く
——リストから消すだけだと将来の無断復活を誰も検知できない。

**結線の手順（G14 訂正版。順序が本体）**:
1. **先に** `WATCHED` へ `"ocr_engine.py"` を追加し、結線前に緑を確認（ベースライン）
2. ocr_engine に BULK 読取を書く → `UnwiredItemsTest` が**赤になるのを目視で記録**
3. `UNWIRED` から BULK を外し（窓 2 項は削除で消える）、緑に戻す

この 1→2→3 を踏まないと「遷移を観測せずに結論だけ書き換える」ことになる（所見 L）。

**番人の拡充（V10 の解消）**: `_MUST_STAY_LIGHT` へ `"card_salvage"` /
`"test_card_salvage"` / `"ocr_test_fixtures"` / `"test_credit_card_config"` を追加。
`test_credit_card_config.py` には **ocr_engine を import しない**（値効果の検証は
venv 側の新テストファイルへ。所見 N）。

### 3.7 既存 doc_type 無変化の構造保証（H4）

- 全ての新引数は既定値付き末尾追加（`line_mode=False` / `generation_config=None` /
  `salvage=False`）。既定経路のバイト列は不変（V8 が機械で保証）
- `LINE_MODE_DOC_TYPES` 判定の外に新コードは 1 行も無い
- ゲートテストは「**トリガ条件を満たした標本**（截断応答）で入らないこと」を
  `DocType.ALL − LINE_MODE_DOC_TYPES` の全型に対して検査し（固定リスト手書き禁止。
  §9 評審 所見 E/G）、加えて陽性 assert（截断時の memo が
  「AI応答のJSON解析失敗」のまま・既定 generation_config のまま）で
  「番人が空振りしていないこと」も固定する（所見 F/Q）

---

## 4. タスク清単（TDD。各項に DoD。T5-8 のみ独立 commit）

### T5-1: 100 行フィクスチャ

`ocr_test_fixtures.py` に `etc_rows_raw(n, section_at=50, fx_at=(7, 63))` を追加。
区画境界（`section_at` で sec 0→1、sections 2 区画）と外貨行（`fx_at` の行は
foreign_amount/currency/fx_rate 持ち）を**必ず**織り込む——全行同質だと
「サルベージが sec/外貨フィールドを落とす」変異が生き残る（§9 評審 所見 J）。
`rows_on_page=n`、printed_totals は count=n。純データ関数（引数→dict、副作用・
遅延 import なし）と docstring に明記（所見 H）。

**DoD**: stdlib のみ／`etc_rows_raw(100)` が rows 100・rows_on_page 100・
2 区画・外貨 2 行を返す／`test_dependency_weight` 緑（`_MUST_STAY_LIGHT` 追加後）。

### T5-2: `card_salvage.salvage_truncated_json`（RED → GREEN）

**DoD**（テストは `test_card_salvage.py`、python3 単体で走ること）:
- G3 の 7 截断形態（値の途中／内側 `}` 直後／配列トップ／閉じ括弧ゼロ／フェンス
  截断／parts 空 等）すべてで、完結分が回収される or None（例外は 1 つも出ない）
- `etc_rows_raw(100)` の JSON テキストを行 62 の直後で切る → rows 62 行＋
  rows_on_page=100＋sections/printed_totals/total_amount が回収される
- **数値の途中で切ったテキストから、切れた値が回収されない**（`"amount": 630` を
  `63` で切る → その行は捨てられ、630 とも 63 とも記録されない）
- 行の途中（フィールド欠け）で切る → その行は捨てられる
- `rows` に未到達の截断 → top 級のみの dict
- sec / foreign_amount / currency / fx_rate / amount が逐字で保全される（白名単無し）

### T5-3: `detect_shortage` / `shortage_memo` / `shortage_audit_reason`

**DoD**（同上 venv 非依存）:
- `LineShortage` の**中身まで**等値 assert（bool 判定だと `{}` でも緑になる——§9
  評審 M1 の形。`assertEqual(shortage, LineShortage(expected=100, got=62, salvaged=True))`）
- `got` は常に `len(raw_data["rows"])` と一致（複数シナリオの subTest。M2）
- expected 不明＋salvaged → shortage（`expected=None`。M4）
- salvaged かつ got>=expected → `detect_shortage` は None だが
  `salvaged_audit_reason` が `"salvaged:{got}/{expected}"` を返す（§3.4 の保険）
- 非 salvaged かつ expected 無し → None
- **`got` の正規化（Codex R2 #2）**: `rows` が dict / str / null / 数値のとき got=0、
  list 内の非 dict 要素は数えない（builder の見え方と一致）——subTest で全型を回す
- memo 文言: 「⚠ 明細行の取得漏れ: 券面{expected}行中{got}行のみ取得（原票を確認
  してください）」／expected 不明時「⚠ 明細行の取得漏れ: AI応答が途中で切断
  （{got}行のみ取得・総数不明。原票を確認してください）」

### T5-4: prompt の `rows_on_page` 再定義（§3.4）

**DoD**: CC/IC 各 1 箇所の説明文変更のみ／既存 `test_card_prompts` 無修正で緑／
新テスト: 両 prompt に「rows に入れるべき行の数」の整合句と「取得できた数ではなく」
の物理アンカー句が在ることを固定。

### T5-5: 予算の口（§3.1）

**DoD**: `_generate_content_with_retry` 引数省略時は現行と完全同一
（`test_ocr_engine_max_tokens` **無修正で緑**）／明示時はその config が SDK へ渡る／
`_line_generation_config()` は BULK=0 で None・BULK=65536 で
`max_output_tokens=65536` の dict・BULK==既定値でも None／
**0 が SDK に渡る経路が存在しない**ことをテストで固定。

### T5-6: サルベージの配線（§3.3）

**DoD**:
- 3 変体＋`_call_gemini` の `line_mode=False` 既定で既存テスト無修正緑
- `line_mode=True`＋截断応答 → サルベージ dict（`SALVAGED_KEY` 付き・rows 正規化済み）
- `line_mode=True`＋截断＋回収ゼロ → `{"rows": [], SALVAGED_KEY: True}`（None ではない）
- `line_mode=False`＋截断 → None のまま（既存挙動）
- 逐頁ループ・尾段の両方で: CC 截断 → **Vision 兜底が発火しない**（mock で
  `assert_not_called`）／当該頁は `_unrecognized` 占位で**歸檔**（`_page_error` に
  ならない）——尾段は §9 評審 5-D の指摘箇所なので明示 DoD

### T5-7: `_yield_page_results` の 2 payload 結線（§3.5）

**DoD**:
- shortage＋entries あり → 明細 result（無改造・無標色）→ 提示行 payload の**順**で
  2 件 yield。提示行は `_unrecognized`＋memo（S 列文言）＋`_audit_signal`
  （`"line_shortage:62/100"`）＋`_ocr_text_len`
- shortage＋entries 空 → 1 payload に併合
- salvaged かつ充足 → 明細のみ＋`_audit_signal`（`"salvaged:…"`）を明細 result に付与
- shortage 無し → 現行と 1 バイトも変わらない
- **ゲートテスト（§3.7 の形）**: `DocType.ALL − LINE_MODE_DOC_TYPES` の全型を
  **截断標本で**回し、①サルベージ不発火 ②既定 generation_config ③截断時 memo
  「AI応答のJSON解析失敗」不変、の 3 点を共通ヘルパで assert。加えて
  「CC では発火する」逆向きの番人 1 本（機能丸殺し変異を殺す）
- **process_file 級の仕様固定テスト（Codex R2 #4 前半採用）**: shortage 頁 1 枚の
  ファイルで「1 物理頁 → `POSTED=1` ＋ `PLACEHOLDER=1`・頁カバレッジ警告なし・
  `process_file` は True（歸檔）」を固定
- `test_ip401_regression` / `test_pdf_split_contract` / `test_card_entries` /
  `test_card_prompts`（T5-4 の追加分以外）無修正で緑／`card_entries.py`・
  `sheets_output.py`・`main.py` は**無改造**

### T5-8: config 結線と後片付け（§3.6。**単独 commit**）

**DoD**: §3.6 の手順 1→2→3 を順に実施し、2 の赤を記録に残す／BULK=65536・
窓 2 項削除・UNWIRED/PLAN_SECTION_9_5/BULK テスト更新（歴史 keys 分割込み）／
`assertFalse(hasattr(config, "CC_WINDOW_SIZE"))`・同 `CC_MAX_WINDOWS` の再追加番人／
`_MUST_STAY_LIGHT` 4 項追加／母 Plan §9.5 の実数更新／
`test_credit_card_config` に ocr_engine を import しない。

### T5-9: 回帰と変異検証

**DoD**: 全量緑（Ran ≥ 808 ＋ 新規分）＋ 下記の変異が**全件赤**:

| # | 変異 | 落ちるべき検査 |
|---|---|---|
| 1 | 充足判定を `got >= 1` に緩める | T5-3 の等値テスト |
| 2 | `got` を rows 以外（サルベージ内部カウンタ等）から数える | T5-3 の不変式 |
| 3 | expected 不明を「充足」に倒す | T5-3 |
| 4 | 数値途中カットを有効値として回収する | T5-2 |
| 5 | サルベージ全滅で None を返す（Vision/_page_error 復活） | T5-6 の分類テスト |
| 6 | salvage を全 doc_type に開く | T5-7 ゲート① |
| 7 | BULK 予算を全 doc_type に適用 | T5-7 ゲート② |
| 8 | BULK=0 を SDK へ素通しする | T5-5 |
| 9 | shortage 非 None でも提示行を出さない | T5-7 |
| 10 | 提示行を明細の**前**に出す | T5-7 の順序テスト |
| 11 | shortage を doc_red / red_flags へ流す（明細 62 行が赤化） | T5-7 の無標色 assert |
| 12 | 既存 doc_type の截断時 memo を別文言に変える | T5-7 ゲート③ |
| 13 | `_audit_signal` を立てない | T5-7 |
| 14 | 誰に対してもサルベージしない（機能丸殺し） | T5-7 の逆向き番人 |
| 15 | 「`,`/`}`/`]` 後続」の完了判定を外す | T5-2 |
| 16 | `rows` が dict/str/null のとき `len()` 直読みで got を数える | T5-3 の正規化テスト |
| 17 | salvaged 充足で監査行を出さない | T5-7 の audit-only テスト |

---

## 5. 受入基準（脚本判定）

```bash
cd "/Users/ibridgezhao/Documents/Super Scaner"
venv311/bin/python -m unittest discover -p "test_*.py"      # → OK, Ran >= 808+新規
venv311/bin/python -m unittest test_ip401_regression -v     # → OK（無修正）
venv311/bin/python -m unittest test_pdf_split_contract -v   # → OK（無修正）
venv311/bin/python -m unittest test_card_entries -v         # → OK（無修正）
venv311/bin/python -m unittest test_ocr_engine_max_tokens -v # → OK（無修正）
python3 -m unittest test_card_salvage test_card_entries test_card_prompts \
    test_credit_card_config test_dependency_weight          # → venv 無しでも OK
```

人手判定:
- `card_entries.py` / `sheets_output.py` / `main.py` が**無改造**（`git diff --stat` で確認）
- `card_prompts.py` の差分が rows_on_page 説明文 2 箇所＋（あれば）新テスト用の定数のみ
- 既存 doc_type の経路に 1 行も差分が無い
- T5-8 の「赤の遷移」記録が残っている

## 6. 影響面

| 対象 | 影響 |
|---|---|
| `card_salvage.py` | **新設**（サルベージ＋shortage 判定＋文言。stdlib のみ、カバレッジ ≥ 80%。行数目標は設けない——品質はテストで縛る（Codex R2 #7）。実装は `json.JSONDecoder.raw_decode`＋小さな走査の併用等、保守性優先） |
| `ocr_engine.py` | `_generate_content_with_retry` 引数 1 本／`_line_generation_config` 新設／3 変体＋`_call_gemini` に `line_mode`／`_parse_gemini_response` に `salvage`／`_yield_page_results` 非領収書分岐に 2 payload 結線（合計 +40 行程度。ロジック本体は card_salvage 側） |
| `card_prompts.py` | `rows_on_page` 説明文 2 箇所のみ |
| `config.py` | BULK 65536・窓 2 項削除・コメント書換 |
| `main.py` / `sheets_output.py` / `card_entries.py` | **無改造** |
| テスト | `test_card_salvage.py` 新設／`test_ocr_engine_line_budget.py`（仮称、venv 側の配線・ゲート検査）新設／`test_credit_card_config.py`・`test_dependency_weight.py`・`test_card_prompts.py` 追補／母 Plan §9.5 更新 |
| Gemini 呼出回数 | 増えない。截断時はむしろ減る（Vision 兜底 1 回が消える。G5→§3.3） |
| 既存 doc_type | 完全に無変化（H4） |

## 7. リスクと回退

| リスク | 対処 |
|---|---|
| Gemini が `rows_on_page` を誤読し偽の提示行が出る（推測） | 落点が非破壊（照常記帳・Failed 無し・明細無標色）なので誤検出の実害は提示行 1 行。過少申告側は salvaged 保険（§3.4）で拾う |
| 65536 で thinking が膨らむ可能性 | 実測（F4/F5）は本文側のみ。T11 の E2E で実応答の `usage_metadata` を採取して検証（残件登録） |
| サルベージ経由の頁は rows が部分集合 → T9 で dedup 指紋が揺れ、同一原紙の 2 度スキャンが `VERDICT_KEY_CONFLICT`（fail-open）で両方記帳になり得る | **T9 への申し送り**: `SALVAGED_KEY` または shortage 付きの頁は `safe_fingerprint` を呼ばない（重複判定の資格を持たせない） |
| T9 結線時に頁級（本 Plan）とカード級（`FileReconLedger`）の赤系が二重に立つ | T9 で「どちらを残すか」を決める残件として登録 |
| BULK を .env 等で 0 に戻された場合 | 機能は退行しない: 32768 で截断してもサルベージ＋提示行が受け止める（§3.3） |
| 回退 | T5-8（config）と本体を分けて commit。revert の影響は line_mode doc_type に閉じる（既存 doc_type への差分ゼロのため） |

## 8. split-pdf §12.2 の B/C（共有 generator 化）

不要のまま（初稿の判断を維持、根拠はより強くなった）: サルベージは
`_parse_gemini_response` の層に入り、`process_pipeline` の paged/tail 骨格にも
`_split_pdf_pages` にも触れない。しかも V13 のとおり両経路は `_route_ocr_strategy`
を共有しているので、尾段の非対称（§9 評審 5-D）も骨格改造なしで消える。

---

## 9. 評審記録 R1（2026-08-17。初稿＝窓分割案への評審。**§3〜§7 は本書換で置換済み**）

Codex（`codex exec`）＋ 4 視角の対抗評審を回した結果、初稿 §3 の設計は前提から
作り直しになった。本節は歴史記録として保存する（旧 §3〜§7 の本文は git 履歴
`cc09aaa` にある）。

### 9.1 最重要 —— 当方の事実誤認（窓分割の存在理由が消える）

**旧 F6「300 行頁は 65,536 でも天井」は誤り。** 「300 行」は**カード単位**
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
旧 §3〜§7 の 7 割はこの誤認の上に積まれていた。

**さらに旧 F5 の「必ず切断」も過剰**（Codex P2-8）。thinking が常に上限まで使う
保証は無く、`count_tokens` は等価テキストの概算。「worst case では切断し得る」が
正しい強度。→ §0.1 F5/F6 は訂正済み。

### 9.2 窓分割設計の 9 つの硬傷（A〜I。廃案の追加根拠として保存）

| # | 指摘 |
|---|---|
| A | `line_no` はマージキーとして不成立（Gemini 採番。窓 2 が 1 から振り直すと先勝ちで全捨て） |
| B | T-b 判定式 `len(rows) < rows_on_page` は定義不整合で健全頁でも恒常発火（→ 新 §3.4 で解消） |
| C | `rows_on_page` first-win は窓ローカル件数化で 60 行無音消滅 |
| D | `printed_totals` / `total_amount` の first-win は頁下部の合計を捨てる |
| E | `sec` は窓ローカルな `sections` 添字。窓間で名前空間が違い検算が静かにずれる |
| F | 結線が逐頁分岐だけで尾段が漏れる（→ 新 §3.3 は共有層で解消） |
| G | Vision 兜底との関係未定義（1 頁最大 10 呼出） |
| H | 「窓応答が WINDOW_SIZE を大きく超えたら打ち切り」は原理的に到達不能（超過応答は截断して parse 不能） |
| I | 第 1 窓も截断した場合、G6 の無限ループが悪化（2 回/周 → 3 回/周） |

### 9.3 窓分割より安い代替（→ 新 §3.2 として採用）

schema 順序は `rows` が最後。截断応答にも `rows_on_page` と完結行 N 個が残っており、
`extract_json` の all-or-nothing が捨てているだけ。サルベージ解析なら Gemini を
1 度も追加で叩かずに済む。コスト約 20〜150 行のパーサ vs 最大 8 回の追加呼出。

### 9.4 その他（採否は §10 の表に統合）

- T5-2（`PageOcr.truncated`）は落とせる → 採用（新設計はサルベージを parse 層で完結させ、旗の伝搬自体を不要にした）
- `shortage` を bare dict にしない → 採用（`LineShortage` NamedTuple）
- 監査タブの新 verdict「行欠落」は作らない → 採用（`_audit_signal`＋機械可読 reason）
- config 結線は単独 commit → 採用（T5-8）
- T9 の二重赤系・部分取得頁の指紋除外 → §7 の申し送りに登録

### 9.5 次 session への申し送り（実施記録）

1. 趙へ BULK=65536 の一問 → **2026-08-18 拍板: 上げる（選択肢 1）** ✅
2. 未読の評審 2 本（decree-conflicts / test-teeth）を読む → **読了・採否は §10** ✅
3. §3〜§7 を書き直し、再度 Codex 評審 → **本書換。評審記録は §10 に追記**
4. §0.1 F5/F6 を訂正 → **訂正済み** ✅

---

## 10. 評審記録 R2（2026-08-18。未読だった 2 評審の採否 ＋ 再設計への反映）

### 10.1 test-teeth（変異検証の穴）の採否

| 所見 | 採否 | 反映先 |
|---|---|---|
| M1 充足判定緩和の変異が殺せない | 採用 | T5-3 DoD・T5-9 #1 |
| M2 got の二重帳簿 | 採用 | T5-3 DoD・T5-9 #2 |
| M3 builder 1 回/頁の番人 | 採用（形を変えて） | builder は list 返しで構造的に流式化不能（V11）。ゲートテストの陽性 assert に吸収 |
| M4 expected 不明→充足の変異 | 採用 | §3.4 判定規則・T5-9 #3 |
| M5/M6/M8/M9 窓固有（再採番・範囲・first-win・0 行打切） | **廃案に伴い消滅** | — |
| M7 「例外を投げない」契約 | 採用 | T5-2 DoD（salvage は例外を投げない） |
| M10 truncated 常時 True の逆変異 | 採用（形を変えて） | 非截断・非 line_mode でサルベージが走らないゲート（T5-9 #6） |
| 所見 A: 金額 0 提示行は append_entries 経由では書けない | 採用 | §3.5 の `_unrecognized` 経路（V4 で裏取り） |
| 所見 B: main.py が影響面に無い／キー名未定義 | 採用（結論は逆転） | `_audit_signal`＋`_unrecognized` の既存機構で **main 無改造**を達成（V5/V6） |
| 所見 C: mark placement テストの形 | 採用 | T5-7 DoD（明細無標色＋提示行赤の両 assert） |
| 所見 D: doc_red へ流す変異 | 採用 | T5-9 #11 |
| 所見 E/F/G: ゲートテストの牙（截断標本・予算口・DocType.ALL 導出） | 採用 | §3.7・T5-7 DoD |
| 所見 H/I: フィクスチャ純データ規約・_MUST_STAY_LIGHT 直登録 | 採用 | T5-1・§3.6 |
| 所見 J: 区画境界＋外貨行を織り込む | 採用 | T5-1 |
| 所見 K: 関数形の追認 | 採用 | T5-1 |
| 所見 L: UnwiredItemsTest は赤くならない（WATCHED に ocr_engine 無し） | 採用 | G14 訂正・§3.6 手順 1→2→3 |
| 所見 M: BULK=0 の SDK 素通し | 採用 | §3.1・T5-9 #8 |
| 所見 N: test_credit_card_config に ocr_engine を import しない | 採用 | §3.6 |
| 所見 O: 名前結線と値効果の両輪 | 採用 | T5-5（値効果は venv 側テスト） |
| 所見 P: PageOcr.truncated は既存テストと両立しない | 採用（より簡素な解で） | サルベージを parse 層で完結（§3.3）。truncated 旗の伝搬自体が不要に |
| 所見 Q: 「既存を壊す」方向の変異が無い | 採用 | T5-9 #12 |

### 10.2 decree-conflicts（既決事項との衝突）の採否

| 所見 | 採否 | 反映先 |
|---|---|---|
| 1-A `_excluded_page` は「記帳しない」チャネル（P1） | 採用 | §3.5（`_unrecognized` 追加 payload 方式） |
| 1-B 提示行は去向ではなく注記と明記 | 採用 | §0.3・§3.5 |
| 2-A `sec` の窓間名前空間混線（P1） | **廃案に伴い消滅**（サルベージは単一応答内） | §0.3 AD-4 行に注記 |
| 2-B P3-a `jpy_amount` は実在しない名／白名単禁止 | 採用 | §0.3 AD-10 訂正・§3.2（行 dict 丸ごと保全） |
| 2-B P3-b null 勝ちマージ | 窓マージ消滅により対象消滅（サルベージは値を改変しない） | §3.2 |
| 3 AD-1 衝突なし／dedup 指紋ドリフト | 採用（申し送り） | §7 |
| 4 取引No: 「N 行→N 番」をテストに書かない／提示行は後 | 採用 | §2 非目標・§3.5 順序 |
| 5-B/5-C 窓 prompt の rows_on_page/line_no 崩れ（P1） | **廃案に伴い消滅**。ただし「第 2 の突合軸」の発想は salvaged 保険（§3.4）として部分採用 | §3.4 |
| 5-D 尾段漏れ（P2） | 採用 | §3.3（共有層で構造的に解消）・T5-6 DoD |
| 5-E 全滅時は `_page_error` でなく `_unrecognized`（P2） | 採用 | §3.3・H3 |
| 6-A T5-2 の DoD 矛盾 | 採用（所見 P と同解） | §3.3 |
| 6-B ocr_engine に積まない／新モジュール切出し | 採用 | `card_salvage.py`（§3.2） |
| 6-C 窓 prompt タスク欠落 | **廃案に伴い消滅**（prompt 変更は rows_on_page 説明 2 箇所のみ＝T5-4） | — |
| 追加 P3: 監査タブの書き口二重化への予防線 | 採用 | §3.5（新 verdict を作らない） |
| 追加 P3: G11 の行番号ずれ | 採用 | §0.2 訂正 |

### 10.3 Codex 評審 R2（2026-08-18、書換後の Plan に対して。7 条＋複審 1 輪）

| # | 指摘（要旨） | 裁決 | 反映先 |
|---|---|---|---|
| 1 | P1: `.text` ValueError 時に parts の部分 JSON が失われる → `_response_text` フォールバック必須 | **駁回 → Codex 撤回**。SDK 0.8.5 実読: ValueError ⇔ parts 空 ⇔ 回収対象が存在しない。parts 非空なら MAX_TOKENS でも `.text` は連結を返す | §3.3 に裁決を明記（死代碼を作らない） |
| 2 | P2: `rows` 非 list / 非 dict 要素で `got` が誤計数 | 採用 | §3.2 の got 定義・T5-3 DoD・T5-9 #16 |
| 3 | P2: salvaged 充足の audit-only 分岐が §3.5 疑似コードから脱落 | 採用 | `salvaged_audit_reason` 新設・§3.5 疑似コード修正・T5-9 #17 |
| 4 | P2: 2 payload で進捗 outcome が頁数とずれる（前半＝テストで仕様固定／後半＝main 側処理へ設計切替） | **前半採用・後半駁回 → Codex 撤回**（RECEIPT 複数 result の既存仕様＋main 無改造の価値が上回る） | T5-7 の process_file 級テスト・§3.5 副作用の明文化 |
| 5 | P2: config 削除で過去設計との追跡が切れる／再追加を検知できない | 採用 | §3.6（PLAN_SECTION_9_5 の現存/廃止分割＋`hasattr` 否定番人）・T5-8 DoD |
| 6 | P3: 「line_mode 不可分」の文言が §7（BULK=0 でも salvage 有効）と矛盾 | 採用 | §3.3 文言修正（予算のみ縮退） |
| 7 | P3: salvage parser の 150 行目標は品質制約として逆効果 | 採用 | §6（行数目標削除、テストで縛る） |

複審の結果: 駁回 2 件（#1・#4 後半）は Codex が根拠を確認のうえ**撤回**。
再提出なし＝本 Plan 定稿。総評（Codex）: 「BULK 結線、rows_on_page 再定義、
`_unrecognized` 経路で提示行を書く判断は実コードと整合」。

---

## 11. 実施記録（2026-08-18）

### 11.1 設計からの逸脱（実装中に判明した事実で変えた点）

| 箇所 | Plan の記述 | 実際 | 理由 |
|---|---|---|---|
| `_visible_rows` / `_expected_rows` | card_salvage 内で自前実装 | `card_entries._rows` / `card_reconciliation._coerce_int` へ**委譲** | 自前実装は初日から漂移していた（tuple 受理の有無）。tuple の raw_data で「3 行記帳したのに券面3行中0行のみ取得」の偽警告が出る。両モジュールとも venv 非依存なので stdlib-only は保たれる |
| 予算＋salvage の配線 | 3 変体それぞれに書く | `_call_gemini_parts` **単一出口**へ集約 | 「不可分」を 3 箇所の手写しで守る形は、4 つ目の変体で片側だけ書ける（ENTRY_BUILDERS 未登録事故と同型）。呼出点の集合を AST 番人で固定 |
| `detect_shortage` / `salvaged_audit_reason` | 2 つの公開関数 | `page_marks(raw_data) -> (shortage, reason)` へ集約 | 優先規則が呼出側の分岐と関数内 guard の 2 箇所に符号化されていた |
| `LineShortage.salvaged` | 3 フィールド | 2 フィールド（expected / got） | production の読み手がゼロ。截断を経たかの出所は `raw_data[SALVAGED_KEY]` 一つでよい |
| `_is_terminated`（数値） | 「直後にもう 1 文字ある」 | 「区切り記号 `,`/`}`/`]` が続く」 | 前者は `_salvage` 冒頭の strip に安全を依存していた（strip を外した日に黙って崩れる）。codex R3 の指摘は誤報だったが、依存の存在自体が問題 |
| `line_mode` 判定 | 各所で集合参照 | `_is_line_mode` ＋ `PageOcr.line_mode` | 6 箇所へ散っていた |
| `test_ocr_engine_max_tokens` | 無修正 | **実装完了後に**共有 helper へ統合 | 「無修正で緑」は実装期間中の受入基準で、それは満たした。窓口が閉じた後も複製を残すと SDK 契約の二重帳簿になる |

### 11.2 検証結果

| 項目 | 結果 |
|---|---|
| 全量テスト（venv311） | **Ran 895 / OK**（起点 808 → +87） |
| 脱 venv（`python3` 単体、9 モジュール） | **Ran 350 / OK**（skipped 2＝venv 前提の突合） |
| `card_salvage.py` カバレッジ | **100%**（母 Plan A11 は ≥80%） |
| 変異検証 | **25/25 KILLED**（§4 T5-9 の 17 変異 ＋ 実施中に追加した 8） |
| `card_entries.py` / `sheets_output.py` / `main.py` | **差分ゼロ**（`git diff cc09aaa --stat` で確認） |
| Codex 評審 | R1 で P1 を 1 件検出→修正、R2/R4 は「actionable な欠陥なし」 |
| 4 視角 cleanup panel | R1 で P1 4 件・P2 多数 → 反映、R2 は P2 のみ → 反映 |

### 11.3 変異検証で見つかった穴（テストが緑のまま通していた欠陥）

1. **`"rows": null` の完結値**（codex R1・P1）: `setdefault` は既存キーを置き換えないので
   `len()` で TypeError → 救えたはずの行欠け payload ごと兜底へ落ちていた
2. **配列要素の未終端検査**: 標本の配列要素が全て dict（自己終端）なので、規則を
   消しても全テストが緑のまま通った。数値要素の標本を足して塞いだ
3. **尾段（単頁 PDF・画像）の `line_mode`**: 逐頁ループのテストだけでは死なない。
   訓練サンプルが全て 2〜9 頁なので**実票 E2E でも露見しない**位置
4. **散文に埋もれた JSON**: 「最初の `{` を探す」実装へ寄せると、截断テキストで
   行オブジェクト 1 個を応答全体と取り違える

### 11.4 残件（本 Plan の範囲外）

- T6: `sheets_output` の `line_mode` ゲート（行級 A/B/F/T/H 列、金額 0 行の出力）。
  そのとき `append_entries` の「記帳可能な行」判定を純関数へ抽出すれば、
  `test_main_process_file._AmountAwareWriter` の手写しも解消できる
- T7: 検算結線。`printed_totals[].count` と `rows_on_page` が同じ `_coerce_int` を
  通るようになったので、両者の突合は素直に書ける
- T9: `SALVAGED_KEY` または shortage 付きの頁は `safe_fingerprint` を呼ばない
  （部分取得頁に重複判定の資格を持たせない）。頁級とカード級の赤系二重立ちの裁定も
- T11: 実応答の `usage_metadata` を採取し、65536 で thinking が膨らまないことを確認
- `test_ip401_regression` / `test_ip401_nondict_rawdata` のローカル代役
  （`pdf_pages` / `_RecordingWriter`）は「無修正で緑」が受入基準のため未統合
