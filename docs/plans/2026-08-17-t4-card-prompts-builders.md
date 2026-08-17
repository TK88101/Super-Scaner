# Plan: T4 — Gemini プロンプト 2 本 ＋ entry builder 2 本

- 起案: 2026-08-17
- 母 Plan: `docs/plans/2026-08-12-credit-card-doctype.md`（AD-* / T1〜T11 / 受入基準）
- 事実基盤: `docs/plans/2026-08-12-credit-card-sample-facts.md`（F-1〜F-14）
- 前置: T1 / T2 / T3 完了（`cb050d8` / `a915f53` / `31d24bb`）。703 tests 緑
- 対象ブランチ: `main`
- 状態: **Codex 評審前の起案**

---

## 0. 前提の確認（この Plan が立つ足場）

| 事実 | 出所 | T4 への含意 |
|---|---|---|
| `.env` に `FOLDER_CREDIT_CARD_ID` は**未追加** | 趙の申し送り | 新 doc_type は**本番から到達不能**。T4 のコードが main に入っても事故は起きない |
| T7（`_apply_ocr_overrides` 豁免）**未了** | 母 Plan T7 | `_yield_page_results` の 1 行目で doc 級 `date` / `invoice_num` が上書きされる。T4 はこれを**前提として設計する**（H列は builder が明示的に空にする。AD-T4-5） |
| T6（`line_mode` ゲート）**未了** | 母 Plan T6 | builder が立てる行級フィールド（`date` / `debit_vendor` / `memo`）は T6 まで**読まれない**。T4 はそれを承知で出力する |
| `PageOcr.actual_doc_type` が prompt と builder を頁ごとに決める | T3（AD-T3-1） | 混載フォルダの nimoca 頁は `transit_ic` の prompt/builder へ流れる。T4 はその両端を埋める作業 |
| **タブは folder doc_type に従う**（`actual_doc_type` はタブを決めない） | T3（AD-T3-1）。`main.py:569` は folder の `doc_type` を `append_entries` へ渡す | 混載フォルダの nimoca 頁は **「_カード明細」タブへ書かれる**（「_交通IC」ではない）。1 ファイル 1 タブを保つための既裁定であり、T4 はこれを変えない |

### `.env` 解禁条件は **T4 ＋ T6 ＋ T7**（趙裁定 2026-08-17）

従来の申し送りは「**T4 ＋ T7** 完了で解禁」だった。Codex 評審（附録 A #5）の指摘を
検証した結果 **T6 も必須**と判明し、趙が裁定した（「①同意」）。
根拠は 4 点、いずれも T6 が無いと「仕訳は出るが帳簿が誤っている」状態になる:

1. 取引No が 1 頁 = 1 番号のまま（ETC 100 行が全部同じ番号）
2. B列 取引日が doc 級固定（行ごとの利用日にならない）
3. AD-5 の credit_adjust 行の借方 `未払金` が `未確定勘定` に潰される（訂正 2）
4. 外貨占位行（AD-T4-8）が無音で落ちる

**この裁定は解禁を早める話ではなく遅らせる話である**（T4 完了 ≠ 解禁可）。
`FOLDER_TRANSIT_IC_ID` に値を置かないのは従来どおり（趙裁定 5・混載）。

---

## 1. 目標 / 非目標

### 目標

1. `credit_card` / `transit_ic` の Gemini プロンプトを実装し、スタブ（`_CC_PROMPT_STUB`）を置き換える。
2. 逐行記帳の entry builder 2 本を実装し、`return []` のスタブを置き換える。
3. 記帳集合と検算集合の分離（AD-5）を **builder の出力形状として**確定する。
4. プロンプトが吐く `raw_data` が、既存の純関数 4 モジュール
   （`page_dedup` / `card_reconciliation` / `invoice_classification` / `page_family`）に
   **無損で食わせられる**ことを機械で保証する。

### 非目標（T4 でやらないこと）

| 項目 | 理由 | 担当タスク |
|---|---|---|
| `sheets_output.append_entries` の行級化（A/B/F/T 列） | 本番の記帳経路。明示ゲートと golden snapshot が要る | T6 |
| 出力切断の窓分割リトライ | 独立した機構 | T5 |
| 異常検知の行級化・U列タグの実書き込み | `detect_anomalies` の doc 級構造に手を入れる | T8 |
| `main.process_file` への接線（元帳結算・重複ゲート） | 書込前/書込後の分離が要る | T9 |
| `_apply_ocr_overrides` の豁免 | 独立した 1 行の変更 | T7 |
| 実サンプルでの E2E | Gemini 実呼出が要る | T11 |
| `.env` への folder ID 追加 | **T4＋T7 完了まで禁止**（趙の申し送り） | 運用 |

---

## 2. 実測で判明した母 Plan の訂正事項（5 件）

コードを読んで、母 Plan の記述と実装が食い違う点が 5 件見つかった。
**どれも T4 の設計を変える**ので先に列挙する。

### 訂正 1. `_determine_credit_sub_account` は**死コード**（呼出点ゼロ）

母 Plan AD-11 は

> `sheets_output._determine_credit_sub_account`（711 行）は既に doc_type で分岐している
> （receipt→社長名、purchase_invoice→取引先名）。`CREDIT_CARD → カード名` を足すのは
> 同じ設計の延長

と書いているが、**この関数は定義されているだけで一度も呼ばれていない**
（`_determine_debit_sub_account` も同じ）。`append_entries` の row 構築は
D列（借方補助科目）・L列（貸方補助科目）とも `""` のハードコードである
（`sheets_output.py:267` / `:275`）。

```
$ grep -n "_determine_credit_sub_account\|_determine_debit_sub_account" sheets_output.py
685:    def _determine_debit_sub_account(debit_account, entry, invoice_num):
711:    def _determine_credit_sub_account(doc_type, entry, vendor_name):
```

**T4 への含意**: builder が `credit_sub_account: カード名` を出しても**現状は捨てられる**。
AD-11 の「貸方補助＝カード名」は T6 が L列を結線して初めて成立する。
T4 は値を出す（形は正しくしておく）が、**効くのは T6 から**と明記する。
→ **T6 の DoD に「L列に `entry["credit_sub_account"]` を結線する」を追加要求**（§10）。

### 訂正 2. `未払金` は `CREDIT_ONLY_ACCOUNTS` に在る（credit_adjust 行が壊れる）

AD-5 の裁定は **借方 `未払金` ／ 貸方 `雑収入`** だが、
`config.CREDIT_ONLY_ACCOUNTS = {"未払金", "買掛金", ...}` に `未払金` が含まれ、
`append_entries` は

```python
if debit_account in CREDIT_ONLY_ACCOUNTS:
    debit_account = UNKNOWN_ACCOUNT      # → "未確定勘定"
```

で借方を潰す。ポイント充当行は**必ず**「未確定勘定」に化け、
さらに `detect_anomalies` の `undetermined_account` で赤タグが立つ。

この置換は「Gemini が借方に貸方科目を入れる誤学習」への対策であり、
**逐行記帳ではその前提が崩れる**（未払金の借方計上が正当な業務である）。

**T4 への含意**: builder は正しく `未払金` を出す。潰されない仕組みは T6 側。
T4 は `test_card_entries` に**現状を固定する契約テスト**を置き、
T6 が豁免を入れるまで壊れたままであることを可視化する（§10）。

### 訂正 3. `raw_data` の schema は `page_dedup` が既に固定している

`page_dedup.extract_page_identity` / `build_content_digest` は raw_data を
**具体的なキー名で**読む（T2 で実装済み・テスト済み）:

```python
raw_data["card"]["member_no"] / ["statement_page"] / ["issuer"] / ["period"]
raw_data["rows"][i]["date"] / ["amount"]
raw_data["total_amount"]
```

母 Plan AD-4 / AD-10 は `sections[]` / `sec` / `jpy_amount` を語るが、
**`rows` / `amount` という既存キーとの関係を書いていない**。
プロンプトを `jpy_amount` で書くと `build_content_digest` が全行を読み飛ばし
（`_coerce_int(None) is None → continue`）、`digest` が空になり
`is_eligible()` が False → **重複判定が全件 fail-open で無効化される**。
アメックス p1≡p3 が二重記帳され、受入基準 A4 が落ちる。しかも**無症状**
（`safe_fingerprint` は例外を外へ出さない設計なので警告も出ない）。

**T4 への含意**: AD-T4-2 で schema を確定する。

### 訂正 4. `UnwiredItemsTest` の番人には盲点がある

`test_credit_card_config.UnwiredItemsTest` は
`page_family` / `card_reconciliation` / `invoice_classification` / `page_dedup`
の 4 ファイルにしか `from config import X` を探しに行かない。
T4 は `CREDIT_ADJUST_CREDIT_ACCOUNT` / `CC_TAX_TYPE_RENDERING` を
**新モジュール `card_entries.py` で**読むので、**番人は緑のまま**になる。

これは「全緑は壊していないことの証明ではない」の再演である。

**T4 への含意**: 結線と同時に `UNWIRED` から 2 項目を外し、
探索対象ファイルに `card_entries.py` を足す（§6 T4-g）。

### 訂正 5. `_build_description` は非 receipt で doc 級 vendor を前置する

```python
def _build_description(doc_type, vendor_name, item_description):
    if doc_type == DocType.RECEIPT:
        return f"{vendor_name} {item_description}".strip()
    return f"{vendor_name} - {item_description}"
```

逐行記帳では「取引先」は**行ごとの加盟店**であって doc 級ではない。
doc 級 `vendor` を空にすると S列が `" - 電車 西鉄福岡〜薬院"` になり、
カード会社名を入れると全行に無関係な社名が前置される。

**T4 への含意**: AD-T4-4 で `_build_description` に分岐を 1 本足す。

### 訂正 6. 事実台帳 §4 決定 5 の図の「Tab 分流」は T3 で訂正済み

事実台帳 §4「決定 5 が既存契約を壊さないことの確認」の図は

```
実際の doc_type = credit_card | transit_ic  → DOC_TYPE_TAB_SUFFIX で Tab 分流
```

と書いているが、**T3 の AD-T3-1 でこれは訂正された**（`actual_doc_type` の作用範囲は
prompt と builder のみ。タブは folder doc_type に従う。1 ファイルの頁が 2 つのタブへ
散ると取引No の採番と原票リンクが割れるため）。

**T4 への含意**: 混載フォルダから来た nimoca 頁は「_カード明細」タブに書かれる。
これは仕様であって欠陥ではない。受入基準にタブ分流の検証は**置かない**
（置くと既裁定と矛盾するテストになる）。

---

## 3. 本 Plan で下す設計裁定

### AD-T4-1. 新モジュール 2 本を作り、`ocr_engine.py` には積まない

| ファイル | 責務 | 想定行数 |
|---|---|---|
| `card_prompts.py` | Gemini プロンプト 2 本（文字列のみ） | 180 |
| `card_entries.py` | builder 2 本 ＋ 摘要組立 ＋ DTO 変換 ＋ 非記帳サマリ | 400 |

`ocr_engine.py` は 2368 行で CLAUDE.md の 800 行上限を大きく超えており、
母 Plan §4 が「これ以上積まない」と明記している。
`ocr_engine` 側は `PROMPTS[...] = card_prompts.X` と
`ENTRY_BUILDERS[...] = card_entries.Y` の**登録だけ**を持つ。

両モジュールは **venv 非依存**（gspread / paddleocr / google api を引かない）に保ち、
`test_dependency_weight._MUST_STAY_LIGHT` に登録する。
import できるのは `doc_types` / `config` / `receipt_aggregation` /
`invoice_classification` / `card_reconciliation` だけ。

### AD-T4-2. `raw_data` schema は `page_dedup` の既存契約を正本とする

**トップレベル**（プロンプト出力の最外殻）:

```jsonc
{
  "card": {                       // page_dedup.extract_page_identity が読む
    "issuer": "アメリカン・エキスプレス",
    "member_no": "****-******-26003",   // 券面の逐語（マスク記号もそのまま）
    "statement_page": "1/6",            // 券面の「1/6 ページ」の逐語
    "period": "2026/05",                // 請求月。取れなければ ""
    "statement_no": "",                 // 明細書番号（あれば）
    "statement_date": "2026/05/20",     // 明細書作成日
    "card_name": "アメリカン・エキスプレス・ビジネスカード",  // 貸方補助科目に使う
    "issuer_t_number": "T8700150009366",// **カード会社自身**の登録番号
    "account_hint": "ガソリン代として"   // 文書全体への手書き指示（F-10）
  },
  "sections": [                   // AD-4。区画と小計
    {"index": 0, "label": "お支払い金額内容", "subtotal": -35808},
    {"index": 1, "label": "今月ご利用額",     "subtotal": 933680}
  ],
  "printed_totals": [             // card_reconciliation.PrintedTotal の素
    {"label": "今回ご請求金額", "amount": 17295, "count": 12,
     "page": 1, "is_handwritten": false}
  ],
  "rows_on_page": 12,             // T5 が使う。この頁に印字された明細行数
  "total_amount": 17295,          // page_dedup.build_content_digest が読む
  "rows": [ /* 下記 */ ]
}
```

**`rows[]`**（1 明細 = 1 要素）:

```jsonc
{
  "line_no": 1,                   // 頁内通番。T5 の窓分割マージキー
  "date": "2026/04/10",           // page_dedup が読む。年が券面に無ければ null
  "month_day": "4/10",            // 券面の逐語（年の推定に使う。nimoca 用）
  "merchant": "福北高速 ＥＴＣ後納分",
  "note": "ETC NO:XX-XXXX 入：野芥西 出：野芥西",  // 副行（F-8）を連結
  "amount": 630,                  // **円貨・税込**。AD-10 の jpy_amount はこれ
  "foreign_amount": null,         // F-9. 現地通貨額
  "currency": "",                 // "USD" / "AUD"
  "fx_rate": null,                // 160.384
  "kind": "expense",              // AD-5 の記帳軸。Python が矯正する
  "sec": 1,                       // 所属区画 index（AD-4）
  "category": "",                 // nimoca 種別列「電車」「バス」「入金」（F-7）
  "place_from": "", "place_to": "",  // nimoca 施設1 / 施設2
  "debit_account": "旅費交通費",   // Gemini の科目推定（任意）
  "account_hint": "HP代"          // 行級手書き（F-10 の UC p2）
}
```

**要点**:
- `rows[].amount` は**円貨額**。AD-10 の `jpy_amount` に別名を与えない
  （訂正 3。別名にすると `build_content_digest` が沈黙して重複判定が死ぬ）。
- 円貨が読めず外貨だけの行は `amount: null`。builder が占位行に変える（AD-T4-8）。
- `merchant_t_number` は**出力させない**。F-11/F-14 のとおりカード明細に
  加盟店の登録番号は存在しない。フィールドを作れば Gemini が
  カード会社の T番号を埋めてくる（`invoice_classification._valid_merchant_t_number`
  が弾くとはいえ、存在しない概念を尋ねない方が幻覚が減る）。

**検証**: 固化 fixture を `page_dedup.safe_fingerprint` / `build_content_digest` /
`card_entries.card_ident_from_raw` に食わせ、**全て非空**であることをテストで固定する。

### AD-T4-3. 記帳軸 `kind` は Python が矯正する（Gemini の申告を最終的に信用しない）

AD-4 と同じ方針の行版。矯正の優先序（上から評価、最初に当たったものが確定）:

| # | 条件 | 矯正後 | 根拠 |
|---|---|---|---|
| 1 | `doc_type == TRANSIT_IC` かつ `category` が「入金」 | `charge` | F-7 |
| 2 | `merchant` / `note` が**繰越ラベル**に一致（`前回分口座振替` `お支払い金額` 等） | `carry_over` | AD-4 の保険の行版 |
| 3 | `amount < 0` かつ **ポイント充当ラベル**に一致（`ポイント` `キャッシュバック` `充当` 等） | `credit_adjust` | 裁定 10 |
| 4 | `amount < 0` でラベル不明 | `unknown` ＋ **占位 entry**（記帳しない） | 下記 |
| 5 | 申告が `KIND_ALL` に無い | `unknown` | 過少記帳より過剰記帳に倒す（A3 の集合定義） |
| 6 | それ以外 | 申告どおり | — |

**母 Plan T4 の DoD 文言を訂正する**。母 Plan は

> builder の強制ガード: **負数なのに `expense` と申告された行は `credit_adjust` に矯正**

と書いているが、これは AD-5 の表が負数を 2 種（ポイント充当・前月清算）しか
挙げていないことに引きずられた記述である。趙裁定 10 は
「**『クレカ相殺』は ポイント充当 のみに適用**」であり、返品・取消・調整値引き等の
**第 3 の負数**が来たとき、それを `credit_adjust`（借方 未払金 ／ 貸方 **雑収入**）に
倒すと**収益が無症状で過大になる**（Codex 評審 #2。事実台帳に実例は無いが、
無いことは来ないことを意味しない）。

よって**ラベルで積極的に同定できた負数だけ** `credit_adjust` にし、
それ以外の負数は **`amount: 0` の占位 entry**（`_placeholder_reason:
"unclassified_negative"`、摘要に逐語）にして人へ回す。**行は消さない**（AD-T4-8 と同型）。

**検算側は別軸**（AD-5）。`detail_lines_from_raw` は**符号を保ったまま**
`DetailLine` を作るので、未分類負数があっても F-4 の検算（15,503）は狂わない。
記帳集合と検算集合が食い違うこと自体が、監査タブに出る赤信号になる。

ラベル表は `card_reconciliation.TOTAL_LABEL_SECTION` を**流用せず**、
`card_entries` に行ラベル用の表を持つ（あちらは頁の合計ラベル、こちらは明細行の
摘要。同じ表にすると片方の追加が他方を誤爆させる）。ただし
`section_for_label` と同じ「長い順に部分一致」の判定形式は踏襲する。

### AD-T4-4. 摘要（S列）は builder が完成形を作り、`_build_description` に分岐を 1 本足す

```python
if doc_type in (DocType.CREDIT_CARD, DocType.TRANSIT_IC):
    return item_description        # builder が完成形を作っている
```

既存 2 分岐は 1 文字も変えない。sheets_output への改動はこの 3 行のみ。

摘要の組み立て（Python 側。Gemini に整形させない）:

| doc_type | 形 | 例 |
|---|---|---|
| credit_card（通常） | `merchant` | `福北高速 ＥＴＣ後納分` |
| credit_card（外貨） | `merchant 57.60USD @160.384` | `BYTESIM LIMITED ADMIRALTY 57.60USD @160.384` |
| credit_card（占位） | `merchant （円貨額なし: 57.60USD）` | AD-T4-8 |
| transit_ic | `category place_from〜place_to` | `電車 西鉄福岡〜薬院` |
| transit_ic（片側のみ） | `category place_from` | `バス 天神` |

`note`（ETC の入口/出口・カード番号）は**摘要に入れない**。行級 memo（T列）へ回す
——S列は MF の帳簿面で最も目に付く列で、ETC NO の羅列で埋めると読めなくなる。

### AD-T4-5. H列は builder が `debit_invoice: ""` を明示して空にする

`append_entries` は `entry.get("debit_invoice", invoice_num)` を書く。
**キーが在れば default（doc 級 invoice_num）は使われない**ので、
builder が空文字を明示すれば T6 を待たずに AD-8（H列空欄）が成立する。

これは **T7 の代替ではない**。T7（`_apply_ocr_overrides` 豁免）が塞ぐのは
B列 取引日の汚染（頁内最後の日付で全行が上書きされる）であって、
H列とは別の穴である。**両方要る**。

**「今すぐ効く」の正確な意味**（Codex 評審 #5）: 効くのは *H列という 1 列だけ* である。
`_apply_ocr_overrides` は今も全 doc_type で無条件に走り（`ocr_engine.py:2048`）、
raw_data の doc 級 `date` を書き換える。T6 が B列を行級化するまで、
その汚染された日付が全行の取引日になる。**T4 の完了は `.env` 解禁の理由にならない**
（§0 の進言を見よ）。

### AD-T4-6. `line_mode: True` は `_build_doc_result` が立てる

builder の戻り値は entries list であって result dict ではないので、
「builder が立てる」（AD-6 の文言）は `_build_doc_result` 側で実現する。

```python
LINE_MODE_DOC_TYPES = frozenset({DocType.CREDIT_CARD, DocType.TRANSIT_IC})
...
if doc_type in LINE_MODE_DOC_TYPES:
    result["line_mode"] = True
```

既存 doc_type にはキー自体を書かない（`entries_data.get("line_mode")` は
None → falsy。既存 row の内容は 1 バイトも変わらない）。

### AD-T4-7. 非記帳行のサマリは純関数が作り、doc 級 memo と `_nonbookable_summary` の両方に入れる

母 Plan T4 の DoD「nimoca `入金` 行が既定で記帳されず**件数・金額が memo に残る**」。

```python
summarize_nonbookable(raw_data, doc_type) -> str
# 例: "非記帳: 入金 3件 ¥15,000 / 前回分口座振替 1件 ¥35,808"
```

doc 級 `memo` に入れる（T6 前の T列で見える）と同時に、result dict の
`_nonbookable_summary` にも入れる（T6 で T列が行級化すると doc 級 memo は
読まれなくなるため。T9 が監査タブへ回す）。
**二重に持つのは移行の谷間で情報を落とさないため**であり、冗長ではない。

### AD-T4-8. 円貨額が取れず外貨額だけの行は `amount: 0` の占位 entry にする

AD-10 の「記帳せず赤い占位行にする」を逐行記帳の文脈へ落とす。
builder は行を**捨てず**、`amount: 0` ＋ `_placeholder_reason: "no_jpy_amount"` の
entry を出す。行が消えないことが IP-401 の思想（頁だけでなく行にも適用する）。

現行 `append_entries` は `if not amount or int(amount) == 0: continue` で
これを無音 skip するので、**T6 の DoD に「`line_mode` では amount==0 の占位 entry も
1 行書く」を追加要求する**（§10）。T4 時点では `.env` 未配で到達不能。

### AD-T4-9. nimoca の年は基準日から推定し、推定行に必ず `_year_estimated` を立てる

F-7 のとおり nimoca の券面には**年が無い**（月日のみ）。

```python
_build_entries_from_transit_ic(raw_data, reference_date=None)
# reference_date is None → date.today()
```

推定規則: 基準日と同じ年を仮置きし、**その日付が基準日より未来なら前年**に倒す
（履歴は過去の記録なので、未来日付は年跨ぎの徴候）。
例: 基準日 2026/01/15、券面「12/20」→ 2025/12/20。

推定した行には `_year_estimated: True` を立てる（受入基準 A8 の「必ず異常マーク」の
producer 側。タグの実書き込みは T8）。券面に年が印字されていれば推定しない。

`date.today()` を builder 内で呼ぶのは**引数が None のときだけ**。
テストは常に `reference_date` を注入する（時刻依存テストを作らない）。

### AD-T4-10. `CardIdent` / `PrintedTotal` / `DetailLine` への変換関数を T4 で提供する

**母 Plan T4 の範囲からの意図的な拡張**。理由:

prompt schema が十分かどうかは「T9 が無損で DTO を作れるか」でしか検証できない。
T9 で不足が判ればプロンプトを書き直すことになり、**T11 の真票回帰がやり直しになる**
（Gemini 実呼出のコストと時間が最も高い工程）。schema を決めた者が
消費側の形まで作って初めて、schema の十分性が機械で言える。

```python
card_ident_from_raw(raw_data) -> CardIdent
printed_totals_from_raw(raw_data) -> tuple[PrintedTotal, ...]
detail_lines_from_raw(raw_data, doc_type) -> tuple[DetailLine, ...]
```

T9 はこれを呼ぶだけ。**T4 では誰も呼ばない**（`main` には触らない）。
契約テストがこの 3 関数と builder が**同じ fixture から矛盾なく作れる**ことを固定する。

`statement_page` の `"1/6"` → `statement_page_n=1, statement_page_total=6` の
分解もここで行う（プロンプトに 3 つ出させると Gemini の自己矛盾を裁けない。
AD-4 と同じ理由で逐語 1 本にして Python が分解する）。

**`sec`（数値 index）→ `DetailLine.section`（文字列定数）の写像**（Codex 評審 #6 / 複審）:

```
rows[i].sec → sections[sec].label → card_entries.section_for_heading(label)
                                    → 外れたら card_reconciliation.section_for_label(label)
                                    → それでも外れたら SECTION_UNKNOWN
```

**`sec` の数値をそのまま `DetailLine.section` に入れない** —— あちらは
`current_usage` / `payment_summary` / `point` / `unknown` の文字列定数で比較される。
数値を入れると全行が未知区画に落ち、F-5 のアメックス カードB 型（区画 2 つが
縦に並ぶ）で 35,808 円ずれた偽の不一致が出る。

**`section_for_label` を直接使わない理由**（複審 P1）: あちらは**合計ラベル**の表で、
判定は「登録ラベルが入力の部分文字列か」の向きである。区画**見出し**の
`"今月ご利用額"` は登録済みの `"今月ご利用額合計"` より**短い**ので
`"今月ご利用額合計" in "今月ご利用額"` が False になり、`SECTION_UNKNOWN` に落ちる。
`"お支払い金額内容"` も同様（登録は `"お支払い金額合計"`）。**F-5 の 2 区画が両方とも
未知に落ちる** ——検算の区画分離が丸ごと効かなくなる。

よって `card_entries` に**見出し専用の表**を持つ:

| 見出し（F-5 / F-6 実測） | section |
|---|---|
| `今月ご利用額` / `ご利用明細` / `ご利用代金明細` | `current_usage` |
| `お支払い金額内容` / `今回ご請求内容` | `payment_summary` |
| `ポイント` を含む見出し | `point` |

**合計ラベル表と混ぜない**（混ぜると `printed_totals` 側で `"今月ご利用額"` が
合計ラベルとして誤命中し、区画小計を請求合計として検算してしまう）。
未命中時に `section_for_label` へ回帰するのは、券面によっては区画見出しが
そのまま合計ラベルの語（`今回ご請求内容`）になっているため。両表とも白名単なので
回帰しても誤爆しない。

加えて AD-4 の保険（`kind == carry_over` の行は `payment_summary` へ強制上書き）も
この関数が持つ。

### AD-T4-11. `account_hint` は**既知科目に解決できたときだけ**科目に使う

趙裁定 6（手書き注記は `account_hint` として科目判定に使う。OCR 信頼度が低いときは
黄系マーク）。手書きの OCR は本質的に低信頼なので、**使った事実そのものを**
マークの根拠にする（頁級の `ocr_confidence` は builder からは見えない）。

**ただし逐語をそのまま科目にしてはいけない**（Codex 評審 #3）。
`append_entries` の写像は `ACCOUNT_MAP.get(x, x)` であり、**未知キーは
`未確定勘定` にならず素通りする**（空文字のときだけ既定へ落ちる。`sheets_output.py:249`）。
F-10 の `HP代` / `さくら備品` は `ACCOUNT_MAP` に無いので、そのまま MF の
借方勘定科目列へ書かれる —— CLAUDE.md の「科目名を臆造しない」に正面から反する。

**解決規則**（`card_entries._resolve_account_hint`）:

1. 逐語に含まれる `ACCOUNT_MAP` のキー/値を**全て**拾う（長い順に走査）
2. 拾った語を `ACCOUNT_MAP` で正式名へ写像し、**distinct な正式名の集合**を作る
3. 集合の要素が **1 つだけ** → その科目に解決。
   例: `ガソリン代として` → {旅費交通費} → 解決。
   例: `ガソリン代・駐車場代` → 語は 2 つだが正式名は {旅費交通費} の 1 つ → **解決**
   （複審 P2: 一致**語数**ではなく **canonical 科目数**で判定する）
4. 集合の要素が **2 つ以上** → 解決しない。
   例: `通信費と車輌交通費として`（F-10 アメックス p1）→ {通信費, 旅費交通費} →
   どちらを当てるかは人にしか決められない
5. 集合が空 → 解決しない。例: `HP代` / `さくら備品`

**借方に不適な科目は解決対象から除く**（複審 P2）。`ACCOUNT_MAP` には
`売上高` / `雑収入` / `雑損失` も含まれており、手書きから収益科目が借方に
入り得る。除外集合は

```python
DEBIT_HINT_EXCLUDED = set(config.CREDIT_ONLY_ACCOUNTS) | {"売上高", "雑収入", "雑損失"}
```

除外に当たった逐語は「解決しない」扱い（`_account_hint_unresolved` に残す）。
なお AD-5 の credit_adjust 行の借方 `未払金` は builder が直接指定するので
この経路を通らない（除外しても影響しない）。

借方科目の優先序:

1. 解決できた行級 `rows[].account_hint`（UC p2 型。F-10）
2. 解決できた文書級 `card.account_hint`（「ガソリン代として」型）
3. `rows[].debit_account`（Gemini の推定）
4. `DOC_TYPE_CONFIG[doc_type]["default_debit"]`

**マーカーは 2 種に分ける**:

| 状況 | マーカー | T8 での扱い |
|---|---|---|
| 解決できて科目に使った | `_account_hint_used: True` | 黄系（手書き由来なので人の確認を促す） |
| 手書きが在るのに解決できなかった | `_account_hint_unresolved: "<逐語>"` | 黄系 ＋ 逐語を memo に残す。科目は既定/推定のまま |

解決できなかった逐語は**必ず行級 memo（T列）へ入れる**。捨てると
「顧客が手書きで指示したのに無視された」になる（母 Plan 附録 B の P1 指摘）。

---

## 4. builder の出力契約（entry の全フィールド）

```python
{
  # ── 今すぐ効く（既存 append_entries が読む）──────────────
  "debit_account":   "旅費交通費",        # ACCOUNT_MAP 前の名
  "debit_tax_type":  "課対仕入10%",       # canonical。省略名変換は出力層(T6)
  "credit_account":  "未払金",
  "credit_tax_type": "対象外",
  "amount":          630,                 # 正の整数（credit_adjust は abs 済み）
  "description":     "福北高速 ＥＴＣ後納分",   # 完成形の摘要（AD-T4-4）
  "debit_invoice":   "",                  # H列空欄（AD-T4-5）。**今すぐ効く**

  # ── T6 が読む（line_mode ゲート）─────────────────────
  "date":            "2026/04/10",        # B列
  "debit_vendor":    "福北高速",           # F列
  "memo":            "ETC NO:... 入：野芥西",  # T列
  "credit_sub_account": "アメリカン・エキスプレス・ビジネスカード",  # L列（訂正 1）

  # ── T8 / T9 が読む（`_` 始まりは帳簿に出さない内部情報）──
  "_booking_kind":   "expense",           # AD-5 の記帳軸（矯正後）
  "_line_kind":      "etc",               # invoice_classification.LineKind
  "_sec":            1,                   # 区画 index
  "_deduction":      DeductionVerdict(...),  # classify() の結果
  "_needs_invoice_confirm": False,        # U列タグ（>= 1万円）
  "_year_estimated": False,               # nimoca の年推定（A8）
  "_account_hint_used": False,            # 黄系マーク（裁定 6）
  "_account_hint_unresolved": "",         # 解決できなかった手書き逐語（AD-T4-11）
  "_placeholder_reason": "",              # "no_jpy_amount" / "unclassified_negative"
  "_line_no":        1,                   # T5 の窓分割マージキー
}
```

**記帳しない kind の扱い**: `carry_over` / `charge`（`TRANSIT_IC_BOOK_CHARGE_ROWS`
が False のとき）は entry を**作らない**。件数と金額は
`summarize_nonbookable` が拾う（AD-T4-7）。

**credit_adjust 行の形**（AD-5）:

```python
{"debit_account": "未払金", "debit_tax_type": "対象外",
 "credit_account": config.CREDIT_ADJUST_CREDIT_ACCOUNT,   # "雑収入"
 "credit_tax_type": "対象外", "amount": abs(row.amount), ...}
```

税区分は両側「対象外」。`classify()` は通さない（控除の話ではない）。

---

## 5. プロンプト設計の要点

### 共通

- 出力は JSON のみ。`extract_json` が ```json フェンスを剥がす既存実装に合わせる。
- **金額はカンマなしの数値**。負数は `-3000` の形（券面の `−`/`▲` を Gemini が
  そのまま返しても `_coerce_*` が吸収するが、プロンプトでは ASCII を指示する）。
- **読めない値は null**。0 で埋めさせない（0 と「読めなかった」は検算で意味が違う）。
- **合計行・小計行を `rows` に入れない**。それは `printed_totals` / `sections[].subtotal`。
- ポイント区画の数値を金額として出さない（F-6。`sections[].label` が
  ポイント系なら `kind` は問わず `sec` でぶら下げるだけ）。

### credit_card 固有

- 「1 PDF = 1 文書」ではない（F-1）。**この 1 頁に見えているカードだけ**を報告する。
- 副行（ETC NO / 入口・出口 / 区間カタカナ）は主行の `note` へ連結する（F-8）。
- 外貨は 3 フィールドに分ける（F-9 / AD-10）。円貨が無ければ `amount: null`。
- 手書き注記は `account_hint` に**逐語のまま**入れる（解釈しない。F-10）。

### transit_ic 固有

- 列は `月日 / 種別 / 施設1 ～ 施設2 / 利用額 / カードポイント / センターポイント`（F-7）。
- **年は券面に無い**。`date` は null、`month_day` に逐語を入れる。
- 種別の値は `電車` `バス` `入金` の 3 つが実測（それ以外は逐語のまま）。
- ポイント列（カードポイント / センターポイント）は `rows` に**入れない**。
- 頁下部の「全 XXX 件」は `printed_totals` に `count` だけで入れる（`amount` は null）。
  nimoca には合計金額が構造的に存在しない（F-3 / `RECON_POLICY` の `count_only`）。

---

## 6. タスク一覧（実施順・各項 DoD）

TDD。各項で **RED を確認してから** 実装する。

### T4-a. fixture の追加（`ocr_test_fixtures.py`）

事実台帳から固化 raw_data を作る。**stdlib のみ**（`test_dependency_weight` 違反を作らない）。

| fixture | 由来 | 何を固定するか |
|---|---|---|
| `AMEX_A_P1_RAW` | F-4 アメックス カードA p1（630×4） | 基本形・検算 17,295 |
| `AMEX_B_P5_RAW` | F-5 アメックス カードB（区画 2 つ・`前回分口座振替 −35,808`） | carry_over・`sec` |
| `ENEOS_P2_RAW` | F-4 ENEOS（`7,380 + 11,123 − 3,000 = 15,503`） | credit_adjust・黄金検算 |
| `JCB_FX_RAW` | F-9 の 3 行（9,238 / 3,494 / 1,578 円 ＋ USD/AUD） | 外貨は円貨額で記帳 |
| `JCB_FX_NO_JPY_RAW` | 上記から円貨を落としたもの | AD-T4-8 の占位 |
| `UC_P2_RAW` | F-10 の行級手書き（`HP代` / `さくら備品`） | account_hint 優先序 |
| `NIMOCA_P1_RAW` | F-7（電車・バス・`入金 現金 5,000`） | charge 除外・年推定 |
| `AMEX_B_RETURN_RAW` | `−1,200 返品` 相当（台帳に実例なしの合成） | 未分類負数が `credit_adjust` に落ちないこと（AD-T4-3） |

**OCR テキスト fixture も要る**（Codex 評審 #7）。`page_dedup.extract_page_identity` は
Gemini の構造化フィールドを **OCR 本文と突き合わせて**裏取りする（会員番号の
可視数字とページラベルが本文にも在ることを確認する）。raw_data だけの契約テストでは
`is_eligible()` が常に False になり、B8 が「通らないこと」を確認するだけの
無意味なテストになる。

既存の `AMEX_HEAD` / `NIMOCA_HEAD` を**複製せず再利用**して組む
（`ocr_test_fixtures.py` の冒頭 docstring が複製を明示的に禁じている ——
正本を直したとき複製側へ伝播せず、古い標本を検証し続ける事故を防ぐため）。

**DoD**: `python3 -m unittest test_dependency_weight` が緑（venv 無しで）。

### T4-b. `card_prompts.py`（プロンプト 2 本）

**DoD**:
- `PROMPTS[CREDIT_CARD]` / `[TRANSIT_IC]` が `_CC_PROMPT_STUB` でなくなる
- `REQUIRED_RAW_KEYS` / `REQUIRED_ROW_KEYS` を **`{キー名: 日本語の説明}` の
  順序付き定数**として置き、**プロンプト中の JSON 例をそこから生成する**
  （複審の改善提案）。自然文を grep するのではなく、**prompt と消費側が
  同じ 1 つの正本を見る**構造にする。
- テストは「生成された schema 断片のキー集合 == `card_entries` が読むキー集合」を見る
- `_validate_doc_type_registries()` が通る（import 時）

**この保証の届く範囲を明記しておく**: これが保証するのは
**prompt と消費側の同期**だけである。Gemini が実際にその形で返すかは
**T11（実呼出）でしか確認できない**。それでも置くのは、消費側の schema を
変えたのに prompt を直し忘れる事故（K1。無症状で重複判定が死ぬ）を塞ぐため。

### T4-c. `card_entries.py` — DTO 変換 3 関数（AD-T4-10）

**DoD**: 7 つの fixture 全てで
- `card_ident_from_raw` が非 None、`statement_page` が `"n/N"` 形式なら n/N が分解される
- `printed_totals_from_raw` が `PrintedTotal` の tuple を返す（`is_handwritten` を落とさない）
- `detail_lines_from_raw` を `FileReconLedger` に食わせた `CardVerdict.detail_sum` が
  **F-4 の 3 例で印字合計と一致**（17,295 / 933,680 / 15,503）
- `page_dedup.safe_fingerprint(ocr_text, raw_data)` が **`is_eligible()` True** を返す
  （アメックス fixture。訂正 3 の穴が塞がっていることの証明）

### T4-d. `card_entries._build_entries_from_credit_card`

**DoD**:
- 負数行が仕訳に混入しない（`amount < 0` の entry が 0 件）
- `credit_adjust` 行が **借方 未払金 / 貸方 雑収入 / abs(3000)** で 1 件出る
- `carry_over` 行の entry が**出ない**（`summarize_nonbookable` に件数・金額が残る）
- JCB の 3 行が **9,238 / 3,494 / 1,578** で記帳され、
  `57.60` / `22.60` / `14.76` が `amount` に**現れない**
- 円貨なし外貨行が `amount: 0` ＋ `_placeholder_reason: "no_jpy_amount"` で残る
- **ラベル不明の負数**（`−1,200 返品`）が `credit_adjust` に**ならず**、
  `amount: 0` ＋ `_placeholder_reason: "unclassified_negative"` の占位になる。
  貸方が `雑収入` の entry が 0 件（AD-T4-3）
- 同じ行を `detail_lines_from_raw` に通すと `DetailLine.amount` は **`-1200` のまま**
  （記帳集合と検算集合が別軸であることの証明）
- UC の行級 `account_hint`（`HP代`）は**解決できず** `debit_account` に現れない。
  `_account_hint_unresolved` に逐語が残り、memo にも入る
- `ガソリン代として` は `旅費交通費` に解決され `_account_hint_used` が立つ
- `通信費と車輌交通費として`（2 科目指示）は**解決しない**（AD-T4-11 規則 3）
- 全 entry の `debit_invoice` が `""`
- `_line_kind` が `derive_line_kind` 経由で決まる（ETC 行が `etc`、給油行が `fuel`）
- `amount >= 10000` の行だけ `_needs_invoice_confirm` が True（**`>=`** で判定）

### T4-e. `card_entries._build_entries_from_transit_ic`

**DoD**:
- `入金` 行が既定で記帳されず、件数・金額が `summarize_nonbookable` に残る
- `TRANSIT_IC_BOOK_CHARGE_ROWS=True` を注入すると記帳される（結線の証明）
- 年が券面に無い行で `reference_date=date(2026,6,15)` を注入 → `2026/05/01` になり
  `_year_estimated` が True
- 年跨ぎ: `reference_date=date(2026,1,15)` ＋ 券面「12/20」→ `2025/12/20`
- 電車行の `_line_kind` が `train`、バス行が `bus`
- 借方科目が `旅費交通費`（DOC_TYPE_CONFIG の default_debit）

### T4-f. `ocr_engine` 側の結線

- `_CC_PROMPT_STUB` とスタブ builder 2 本を削除し、`card_prompts` / `card_entries` を登録
- `LINE_MODE_DOC_TYPES` と `_build_doc_result` の分岐（AD-T4-6）
- `_build_doc_result` に `_nonbookable_summary` の受け渡しを追加（AD-T4-7）
- `sheets_output._build_description` に 1 分岐（AD-T4-4）

**DoD**: 既存 703 tests が**無修正で**緑。`test_doc_type_registries` 緑。

### T4-g. 番人の更新（訂正 4）

- `test_credit_card_config.UnwiredItemsTest.UNWIRED` から
  **`CREDIT_ADJUST_CREDIT_ACCOUNT` だけ**を外す。
  **`CC_TAX_TYPE_RENDERING` は UNWIRED に残す** —— builder は canonical の
  税区分を出し、省略名変換は T6 の出力層で行う（AD-11 / §4）。T4 で外すと
  「結線した」という虚偽の記録になる（Codex 評審 #4）
- 探索対象ファイルに `card_entries.py` / `card_prompts.py` を追加し、
  検出パターンを `from config import X` **と** `config.X` の両方にする
  （現行は前者のみ。後者の書き方で結線されると番人がすり抜ける）
- `config.py` の `CREDIT_ADJUST_CREDIT_ACCOUNT` の「※ 読み取り点が未実装」注記を削除
  （`CC_TAX_TYPE_RENDERING` の注記は残す）
- 母 Plan §9.5 の「足場の実数」を 8 → 9 項目へ更新

**DoD**:
- `CREDIT_ADJUST_CREDIT_ACCOUNT` の値を変異させると `test_card_entries` が赤くなる
  （config が本当に効いていることの確認。`try/except ImportError` は名前を
  間違えても静かに既定値へ回帰するので、結線の証明にはこれが要る）
- `UNWIRED` に `CC_TAX_TYPE_RENDERING` を残したまま `card_entries.py` を
  探索対象に加えても緑（虚偽でないことの確認）

### T4-h. 全量回帰 ＋ カバレッジ

**DoD**: `venv311/bin/python -m unittest discover -p "test_*.py"` 全緑。
`card_entries.py` / `card_prompts.py` のカバレッジ ≥ 80%。

---

## 7. 受入基準（全て機械判定）

| # | 基準 | 判定方法 |
|---|---|---|
| B1 | F-4 の 3 例で `CardVerdict.detail_sum` が印字合計と一致（17,295 / 933,680 / **15,503**） | `test_card_entries` |
| B2 | 記帳集合の和 = `expense_sum`（ENEOS は **18,503**）。検算集合とは別軸 | 同上 |
| B3 | 負数の entry が 0 件、かつ摘要に `ENEOSポイントキャッシュバック` / `前回分口座振替金額` が abs 転正で現れない | 同上（A6 の行版） |
| B4 | JCB 3 行が円貨額で記帳され、外貨数値が `amount` に現れない | 同上 |
| B5 | nimoca `入金` が記帳されず、件数・金額が memo に残る | 同上 |
| B6 | 全 entry の H列（`debit_invoice`）が `""` | 同上 |
| B7 | `amount >= 10000` の行数 == `_needs_invoice_confirm` True の行数 | 同上 |
| B8 | アメックス fixture の `PageFingerprint.is_eligible()` が True | `test_card_entries`（訂正 3） |
| B9 | 既存 703 tests が**無修正で**緑 | `unittest discover` |
| B10 | `python3 -m unittest test_card_entries test_dependency_weight` が **venv 無しで**緑 | 脱 venv 実行 |
| B11 | プロンプト文字列に `REQUIRED_RAW_KEYS` / `REQUIRED_ROW_KEYS` が全て現れる | `test_card_prompts` |
| B12 | ラベル不明の負数が `credit_adjust`（貸方 雑収入）にならず占位 entry になる | `test_card_entries`（AD-T4-3。合成 fixture） |
| B13 | 未知の `account_hint`（`HP代`）が `debit_account` に素通りしない | 同上（AD-T4-11） |
| B14 | `detail_lines_from_raw` の `section` が文字列定数（`sec` の数値ではない）。F-5 の見出し `今月ご利用額` / `お支払い金額内容` が `current_usage` / `payment_summary` に解決される | 同上（AD-T4-10 / K11） |
| B15 | `card_entries` が読むキー集合 **⊆** `card_prompts` の生成 schema | `test_card_entries` |

> **B15 の訂正（実装時）**: 当初は `==`（相等）と書いていたが、これは**誤った
> 制約**だった。prompt は `rows_on_page` / `total_amount` のように builder が
> 読まないフィールドも出させる（T5 の窓分割と `page_dedup` が消費する）。
> 相等を要求すると、正しい設計の方がテストで落ちる。正しくは**部分集合**
> （builder が prompt に無いキーを読んでいないこと）。
>
> さらに Codex 評審 R1-#5 の指摘により、**AST で実装が実際に読むキーを抽出して
> `CONSUMED_*_KEYS` 定数と突合する**テストを追加した。定数を手で書いただけでは
> 「builder が新しいキーを読み始めたのに定数を更新し忘れる」を検出できず、
> 上の部分集合テストは緑のままになる（＝何も検証していない）。

---

## 8. 影響面

| ファイル | 変更 | 既存への危険度 |
|---|---|---|
| `card_prompts.py` | 新規 | なし |
| `card_entries.py` | 新規 | なし |
| `ocr_test_fixtures.py` | fixture 7 本追加 | なし（テスト専用） |
| `ocr_engine.py` | スタブ削除 ＋ 登録 ＋ `LINE_MODE_DOC_TYPES` ＋ `_build_doc_result` 分岐 | **中**（`_build_doc_result` は請求書系・給与も通る。既存 doc_type ではキーを足さないことをテストで固定する） |
| `sheets_output.py` | `_build_description` に 1 分岐（3 行） | 低（既存 2 分岐は不変。3 doc_type の摘要形をテストで固定） |
| `test_credit_card_config.py` | 番人の一覧更新 | 低 |
| `test_dependency_weight.py` | `_MUST_STAY_LIGHT` に 2 モジュール追加 | 低 |
| `config.py` | 注記の削除のみ（値は変えない） | なし |
| 母 Plan | §9.5 の実数更新、訂正 1〜5 の反映 | なし |

**変更しない**: `main.py` / `anomaly_detector.py` / `card_reconciliation.py` /
`page_dedup.py` / `page_family.py` / `invoice_classification.py` /
`receipt_aggregation.py` / `tag_rules.py` / `gas/*`。

---

## 9. リスクと回退

| # | リスク | 深刻度 | 対策 |
|---|---|---|---|
| K1 | プロンプトのキー名が schema からずれ、`page_dedup` が沈黙して重複判定が死ぬ | **P0** | AD-T4-2 ＋ B8 ＋ B11。fixture を消費側 3 モジュールに実際に食わせる |
| K2 | `credit_adjust` の借方 `未払金` が `未確定勘定` に潰される（訂正 2） | P0 | T4 では**塞がらない**。契約テストで現状を固定し、T6 DoD へ義務を移送（§10） |
| K3 | `_build_doc_result` の分岐が既存 doc_type の result を変える | P1 | 既存 doc_type で `line_mode` キーが**存在しない**ことをテストで固定 |
| K4 | `_build_description` の分岐が receipt の摘要を変える | P1 | 既存 `test_sheets_output` 無修正緑 ＋ 3 doc_type の摘要形を明示的に固定 |
| K5 | 新モジュールが重依存を引き、venv 非依存が静かに壊れる | P1 | `_MUST_STAY_LIGHT` 登録 ＋ B10（脱 venv 実行） |
| K6 | nimoca の年推定が誤る（年跨ぎの境界） | P1 | 推定行に必ず `_year_estimated`。基準日は注入可能にしテストで境界を固定 |
| K7 | `unknown` kind の行が過剰記帳を生む | P2 | A3 の集合定義がそもそも `unknown` を記帳側に含む（母 Plan）。検算で可視化される |
| K8 | ポイント以外の負数（返品・取消）が `雑収入` に化けて収益が過大になる | **P0** | AD-T4-3 のラベル駆動 ＋ B12。ラベルで同定できた負数だけ `credit_adjust` |
| K9 | 未知の手書き科目名（`HP代`）が MF の勘定科目列へ素通りする | **P0** | AD-T4-11 の解決規則 ＋ B13。`ACCOUNT_MAP.get(x, x)` は未知キーを潰さない |
| K10 | `sec` の数値が `DetailLine.section` に入り、区画突合が全滅する | P1 | AD-T4-10 の写像 ＋ B14 |
| K11 | 区画見出しを合計ラベル表で引いて F-5 の 2 区画が両方 `SECTION_UNKNOWN` に落ちる | ~~P1~~ → **P2**（実装後の変異検証で降格。下記） | AD-T4-10 の見出し専用表 ＋ B14 |
| K12 | 手書きから `売上高` / `雑収入` が借方に入る | P1 | `DEBIT_HINT_EXCLUDED`（AD-T4-11） ＋ B13 |

### K11 の格下げ（実装後の変異検証で判明。2026-08-17）

Plan 段階では Codex 複審の指摘（P1）をそのまま採ったが、実装後に
`section_for_heading` を殺す変異を入れて回帰させたところ、**金額は 1 円も
変わらなかった**。理由は 3 層の兜底が同時に効いているため:

1. 見出しが引けなくても `section_for_label` へ回帰する ——
   「このカードのポイント明細」は**合計ラベル表にも登録済み**（F-6 の対策で T2 が入れた）
2. `carry_over` の行は `sec` の申告に関わらず `payment_summary` へ強制される
   （AD-4 の保険）ので、F-5 の 2 区画のうち危ない方は見出しに依存しない
3. それでも `SECTION_UNKNOWN` に落ちた行は `_verdict_for` が
   detail_sum に**足す**（過小警告より過剰警告に倒す設計）ので合計は変わらない

したがって K11 の現実的な帰結は「監査 note の雑音」であって会計値の誤りではない。
**見出し表は残す**（区画の語義が正しくなり、将来の券面形態にも効く）が、
severity は P2 へ落とす。

> この降格は「Codex の指摘が誤りだった」という意味ではない ——
> 指摘は `section_for_label` の判定方向について**正しかった**。
> 誤っていたのは、そこから帰結する被害の大きさについての**両者の推測**である。
> 変異を実際に入れて回帰させるまで、どちらも兜底 3 層の存在に気づいていなかった。

**回退**: `.env` に folder ID が無いため新 doc_type は到達不能。
`git revert` 1 発で戻せる（既存経路への改動は `_build_doc_result` の 1 分岐と
`_build_description` の 1 分岐のみ）。

---

## 10. 後続タスクへの申し送り（T4 で判明した義務）

**T6（`sheets_output` の `line_mode` ゲート）に追加要求する DoD**:

1. **L列に `entry["credit_sub_account"]` を結線する**（訂正 1）。
   `_determine_credit_sub_account` は現在**死コード**。AD-11 の「貸方補助＝カード名」は
   これを繋がない限り永久に成立しない。
2. **`line_mode` では借方 `未払金` を `CREDIT_ONLY_ACCOUNTS` 置換から豁免する**（訂正 2）。
   でないと AD-5 のポイント充当行が必ず「未確定勘定」＋赤タグになる。
   豁免の範囲は `line_mode` の中だけ（既存 doc_type の誤学習対策は 1 文字も変えない）。
3. **`line_mode` では `amount == 0` の占位 entry も 1 行書く**（AD-T4-8）。
   現行の `if not amount ... continue` は外貨占位行を無音で落とす。
4. 借方税区分の省略名変換（`config.CC_TAX_TYPE_RENDERING`）は**出力層で**行う。
   builder は canonical のまま出している（AD-11）。結線したら
   `UnwiredItemsTest.UNWIRED` から外し、番人の探索対象に `sheets_output.py` を足すこと
   （T4 では意図的に残してある）。
5. **タブは folder doc_type のまま**（AD-T3-1）。混載 nimoca が「_カード明細」タブに
   落ちるのは仕様。ここを変える改修を T6 に紛れ込ませないこと。

**T8（異常検知の行級化）が消費する producer 側フィールド**:
`_needs_invoice_confirm`（U列タグ）/ `_year_estimated`（A8）/
`_account_hint_used`（裁定 6 の黄系）/ `_placeholder_reason`（赤系）。

**T9（`main` 接線）が呼ぶ関数**:
`card_entries.card_ident_from_raw` / `printed_totals_from_raw` /
`detail_lines_from_raw`（AD-T4-10）と、result dict の `_nonbookable_summary`。

**T7 は依然として `.env` 解禁の必須条件**。AD-T4-5 が塞ぐのは H列だけで、
B列 取引日の汚染は T7 でしか塞がらない。

### T4 の作業中に見つけた**既存の欠陥**（趙裁定 2026-08-17: **次 session で修復**）

**`raw_data` が dict でない truthy 値のとき、単ページ経路で頁が無音で消える（P1）**

- 経路: Gemini が JSON の**配列**を返す → `extract_json`（`ocr_engine.py:196-203` の
  `arr_match` 分岐）が list を返す → `_yield_page_results` の 1 行目
  `_apply_ocr_overrides(raw_data, ...)` が `raw_data.get(...)` で `AttributeError`
- PDF 逐頁ループは `整形処理エラー` の占位行に落ちる（可視化される）が、
  **単ページ PDF・画像の尾段**（`ocr_engine.py:2362-2364`）は最外の
  `except Exception: print(...); return` で**1 件も yield せずに終わる**
- これは IP-401 の不変式「進入した頁は必ず 1 件以上 yield する」を破る。
  しかも `print` は無人運用の miniPC では誰も見ない（CLAUDE.md 既記）

**T4 の欠陥ではない**（全 doc_type に同等に存在し、T4 はこの経路を 1 行も変えていない）。
Codex も「`card_entries` 側では直せない。T4 固有ではない」と同意している。
CLAUDE.md の「不擅自扩大范围」に従い T4 では直さなかった。

**趙裁定（2026-08-17）: 次 session で修復する。** 以下は着手時の申し送り。

**最小修正案**: `_yield_page_results` の冒頭で `isinstance(raw_data, dict)` を確認し、
違えば `_blank_result(_unrecognized=True, memo="AI応答形式不正")` を yield する。

**着手時に必ず確認すること**:

1. これは **PDF 逐頁側の挙動も変える** —— 現在は「整形処理エラー」占位、
   改後は「認識不能」占位。`_page_error` が立つか立たないかで
   `main.process_file` の終態（Failed か否か）が変わる:
   - 現在: `整形処理エラー` は `_page_error=True` → 全頁失敗なら **Failed → ファイル保持 → 再試行**
   - 改後: `_unrecognized=True` は Failed にならない → **占位行を書いてアーカイブ**
   どちらが正しいかは「AI が配列を返す」が再試行で直るかによる。**再試行しても
   同じ応答なら無限ループになる**ので、改後（アーカイブ）が正しい可能性が高いが、
   `test_main_process_file` の既存期待値を読んでから決めること
2. **再現テストを先に書く**（`raw_data=["bad"]` で単ページ経路が 1 件以上 yield する）
3. 既存 4 doc_type の回帰（`test_ip401_regression` / `test_ocr_engine_envelope` /
   `test_ocr_engine_social_insurance`）が無修正で緑であること

---

## 附録 A: Codex 評審の辯論記録（2026-08-17）

9 件の指摘（P1 ×5 / P2 ×4）。**7 件採用・1 件は部分採用・1 件は反駁**。

### 採用（7 件）

| # | severity | 指摘 | 反映先 |
|---|---|---|---|
| 2 | P1 | `amount < 0` を一律 `credit_adjust` は広すぎる。返品・取消が「貸方 雑収入」になり**収益が無症状で過大**になる。裁定 10 は「ポイント充当のみ」 | **AD-T4-3 全面改訂**（ラベル駆動）/ K8 / B12。母 Plan T4 の DoD 文言も訂正 |
| 3 | P1 | `account_hint` をそのまま `debit_account` にすると、`ACCOUNT_MAP.get(x, x)` が未知キーを潰さないので `HP代` が MF の科目列へ素通りする | **AD-T4-11 全面改訂**（解決規則 4 段）/ K9 / B13 |
| 4 | P1 | `CC_TAX_TYPE_RENDERING` を T4 で `UNWIRED` から外すのは、§4「変換は T6 の出力層」と矛盾。外せば虚偽の記録になる | T4-g の処方を修正（外すのは `CREDIT_ADJUST_CREDIT_ACCOUNT` だけ） |
| 5 | P1 | 「H列は今すぐ効く」は谷間でしか成立しない。`.env` 解禁条件に T6 を加えるべき | AD-T4-5 に注記 ＋ §0（**趙が 2026-08-17 に裁定。解禁条件は T4＋T6＋T7**） |
| 6 | P2 | `sec`（数値）→ `DetailLine.section`（文字列定数）の写像が未明文 | AD-T4-10 に追記 / K10 / B14 |
| 7 | P2 | `safe_fingerprint` は OCR 本文との突合をするので、fixture に OCR text が要る | T4-a に追記（既存 `AMEX_HEAD` を再利用。複製禁止） |
| 1 の半分 | P1 | T4 Plan がタブ分流について何も書いていないのは漏れ | **訂正 6 を新設**（混載 nimoca は「_カード明細」タブ。仕様である） |

### 部分採用（1 件）

**#8「プロンプトのキー名 grep テストは脆く保証も弱い。定数化せよ」**

定数化（`REQUIRED_RAW_KEYS`）は採用。ただし「文字列 grep は最低限の補助に留める」は
**そのままでは実行不能**なので、代わりに**保証の弱さを Plan に明記する**形にした ——
プロンプトと Gemini の実出力の一致は T11（実呼出）でしか確認できず、
T4 の段階で置ける機械判定は grep しか存在しない。「弱い保証だと知って置く」のと
「強い保証だと誤解して置く」のは別物であり、後者だけが危険である。

### 反駁（1 件）

**#1 の後半「T6/T9 に『`actual_doc_type` でタブを分ける』変更を正式追加せよ」**

反駁: これは **T3 の AD-T3-1 で既に裁定済み**の事項である。タブが folder doc_type に
従うのは、1 ファイルの頁が 2 つのタブへ散ると (a) 取引No の採番が 2 系列に割れ、
(b) 原票リンクと監査タブの突合が壊れるため。母 Plan と事実台帳の図が古いのが問題で
あって、実装が誤っているのではない。よって **T6/T9 への変更追加は行わず、
訂正 6 として「これは仕様」と記録する**。受入基準にタブ分流の検証を置くと
既裁定と矛盾するテストを作ることになる。

> 注: Codex の指摘のうち「T4 Plan に書いていないのは漏れ」という部分は正当なので
> そちらは採用した（上表）。指摘を丸ごと採るか丸ごと捨てるかの二択にしない。

### 複審（2 ラウンド目。反駁と新設計を審査させた）

| 判定 | 内容 |
|---|---|
| **我方勝** | #1 の反駁を Codex が**認めた**（「反駁は成立。T6/T9 にタブ分流を追加しない判断でよい」）。AD-T3-1 を読み直した上での撤回 |
| **我方勝（条件付）** | #8 の部分採用は妥当と認めた。ただし「prompt/consumer の同期保証」なら grep より強い手段がある（定数から schema 断片を生成）と**より良い代案**を提示 → **採用**。T4-b を書き換えた |
| 同意 | AD-T4-3（負数のラベル駆動）/ §0 の `.env` 進言 |
| **Codex 勝** | **[P1] `section_for_label` は合計ラベル表であり、区画見出しには使えない**。`"今月ご利用額"` は登録済み `"今月ご利用額合計"` より短いため判定の向きが合わず、**F-5 の 2 区画が両方 `SECTION_UNKNOWN` に落ちる**。実際に `TOTAL_LABEL_SECTION` を読んで裏を取った → **採用**（見出し専用表を新設。AD-T4-10） |
| **Codex 勝** | **[P2] `account_hint` の一致判定は語数ではなく canonical 科目数で行うべき**（`ガソリン代・駐車場代` はどちらも旅費交通費なので実質 1 科目） → **採用**（AD-T4-11 規則 2-3） |
| **Codex 勝** | **[P2] `ACCOUNT_MAP` には `売上高` / `雑収入` / `雑損失` も在り、手書きから収益科目が借方に入り得る** → **採用**（`DEBIT_HINT_EXCLUDED`） |

### 事実確認（Codex がコードを読んで検証）

§2 の訂正 1〜5 は**すべて事実として正しい**ことを Codex が独立に確認した
（補助科目関数は定義のみで row は空文字固定 / `未払金` は `CREDIT_ONLY_ACCOUNTS` 内 /
`page_dedup` は `rows[].amount|date` を読む / 番人の探索対象は 4 ファイル /
`_build_description` は非 receipt で vendor 前置）。

---

## 附録 B: 実施記録と simcodex 評審（2026-08-17）

### 結果

| 項目 | 値 |
|---|---|
| 全量テスト | **770 tests 緑**（ベースライン 703 → +67） |
| 脱 venv 実行 | `python3 -m unittest test_card_entries test_card_prompts test_dependency_weight` 緑 |
| カバレッジ | `card_entries.py` **91%** / `card_prompts.py` **100%**（目標 80%） |
| 新規 | `card_prompts.py` 225 行 / `card_entries.py` 652 行 / テスト 2 本 898 行 / fixture 12 本 |
| 既存への改動 | `ocr_engine.py`（登録＋`line_mode` ゲート）/ `sheets_output.py`（3 行）/ 番人 2 本 / `config.py` 注記 |

### 実装で Plan から動いた点

1. **`card_entries.py` が 652 行になった**（Plan の想定は 400 行）。CLAUDE.md の
   「200-400 行典型」を超えるが 800 行上限内。責務は「記帳 entry 生成」と
   「検算 DTO 生成」の 2 つで、両者は `_rows` / `resolve_booking_kind` /
   `_section_of_row` という**共有の解析器**を通じて実際に結びついている
   （分離すると private 関数の公開か重複実装のどちらかになる）。
   **T6/T8/T9 が行級フィールドを足すときが分割の判断点**。
2. **B15 を `==` から `⊆` へ訂正**（§7 の注記）。
3. **K11 を P1 → P2 へ降格**（§9。変異検証の実測による）。

### simcodex Round 1（4 観点エージェント ＋ codex）

**採用 7 件**（P1 ×2 / P2 ×5）:

| # | severity | 指摘 | 修正 |
|---|---|---|---|
| 1 | **P1** | `rows[].debit_account`（Gemini の推定科目）が白名単を通らず、`ACCOUNT_MAP.get(x, x)` は未知キーを潰さないので「架空費」が MF の勘定科目列へ素通りする | `KNOWN_DEBIT_ACCOUNTS` ＋ `_valid_debit_account()`。**AD-T4-11 は手書きだけを守っていて Gemini の推定を守っていなかった** |
| 2 | **P1** | `kind == credit_adjust` を申告だけで信じている。Gemini が負号を落とすと 借方 未払金 ／ 貸方 雑収入 で**収益が過大**になる | 哨兵 `_KIND_SIGN_CONFLICT` → 占位 entry。ポイント充当は券面上必ず負である |
| 3 | P2 | `sec` が `"1"`（文字列）だと区画が未知に落ちる | `_coerce_int` で受ける |
| 4 | P2 | `section_for_heading` は NFKC するが `section_for_label` はしない。同じ逐語で判定が食い違う | `_section_of_row` で**正規化済みの文字列を両方に渡す** |
| 5 | P2 | `CONSUMED_*_KEYS` は手書き定数で、prompt と突合しても**実装が読むキーは検証していない** | AST で `row.get("...")` 等を抽出して定数と突合するテストを追加 |
| 6 | P2 | `resolve_account_hint` が文書級ヒントを行ごとに再解決（300 行なら 300 回） | `functools.lru_cache` |
| 7 | P2 | `card_prompts.ALL_TOP_KEYS` が定義だけで消費者ゼロ（半端な保証） | `CONSUMED_TOP_KEYS` を足して対称化 |

**見送り 3 件**（理由付き）:

| 指摘 | 見送りの理由 |
|---|---|
| `_build` と `summarize_nonbookable` が同じ rows を 2 回走査するので、builder を `(entries, summary)` の tuple 返しにせよ | `ENTRY_BUILDERS` の契約（全 builder が list を返す）が分岐し、`_yield_page_results` に特例が要る。**契約の統一性 > ミリ秒未満の節約**（両エージェントとも「Gemini の網羅呼出に比べ無視できる」と明記） |
| 「長い順部分一致」の走査を `card_reconciliation` の共有関数へ抽出せよ | あちらは T4 の**非目標**（§8「変更しない」）。NFKC の食い違いという実害だけを呼出側で塞いだ（採用 #4） |
| `card_entries.py` が 626 行・2 責務なので分割せよ | 共有解析器で実際に結合している。**T6/T8/T9 が足すときが判断点**（上記） |

**趙へ報告して持ち越し 1 件**（P1・T4 の範囲外）:

`raw_data` が dict でない truthy 値（Gemini が JSON 配列を返す）のとき、
単ページ経路で頁が**無音で消える**。IP-401 違反だが**既存の欠陥**であり、
全 doc_type に同等に存在し、T4 はこの経路を 1 行も変えていない。→ §10 に詳細。

### simcodex Round 2

Codex: **「問題なし」**（4 件の修正に新規欠陥なし。`_KIND_SIGN_CONFLICT` の
非対称 —— 検算側は `KIND_UNKNOWN` として正数を計上し、記帳側は占位 0 ——
も「正しい分離」と確認。持ち越し判断も妥当と同意）。

エージェント側は **P2 を 2 件**検出、いずれも採用:

| # | 指摘 | 修正 |
|---|---|---|
| 8 | **却下した AI 推定が無痕跡で消える**。`account_hint` は未解決なら memo に残るのに、Gemini の推定を白名単で弾いたときだけ痕跡が無い。`ACCOUNT_MAP` に無い正当な科目（分割払手数料の `支払利息` 等）が在っても誰も気づけない | `_debit_account_rejected` フィールド ＋ memo に「AI推定科目(未登録): X」 |
| 9 | `resolve_account_hint` の `lru_cache` はモジュール級グローバルに暗黙依存する。将来 `_ACCOUNT_MAP` を monkeypatch すると**古い値が静かに返る** | 宣言箇所に「差し替えるなら `cache_clear()` を呼べ」と明記 |

さらに **AST 突合テストの単方向性**を指摘され（`実装 ⊆ 宣言` しか見ておらず、
「宣言したが読んでいない」に沈黙する）、**双方向**に変更した。
その場で 2 件の実害が出た:

- `CONSUMED_CARD_KEYS` の `period` —— 宣言していたが**誰も読んでいなかった**
  （読むのは `page_dedup`）→ 削除
- `place_from` / `place_to` —— `row.get(k) for k in (...)` という内包表記経由で
  読んでおり、リテラル走査では**取りこぼしていた** → AST 側を内包表記対応へ拡張

### simcodex Round 3 — **Round 2 の修正が P1 を作った**

Codex が実際にコードを走らせて再現:

> `account_hint` が解決すると `_debit_account` が早期 return するため、
> 同じ行の未知 `debit_account` が評価されず、却下 AI 推定が残らない。
> 再現: `account_hint='通信費として'`, `debit_account='支払利息'` →
> `_debit_account_rejected == ''`, `memo == ''`

**採用**。「Gemini が実在しない科目名を出した」という事実の価値は、
その推定が採用されたかどうかとは**独立**である —— 却下の記録を
ヒント解決より先に確定させ、早期 return でも返すようにした。
回帰テスト `test_rejected_ai_account_is_recorded_even_when_a_hint_wins` を追加。

> **教訓**: Round 2 で「hint が勝つなら Gemini 推定は使われないから記録不要」と
> 判断したのは、**フィールドを足した動機（`ACCOUNT_MAP` の欠落発見）を
> 自分で忘れていた**ということ。新しいフィールドを足したら、
> その動機に照らして全経路を通すこと。

### simcodex Round 4

Codex: **「問題なし」**。early-exit 条件（codex 0 件 ＋ verify 全緑）を満たして終了。

### 番人の変異検証（AST 突合テストが本当に噛むか）

| 変異 | 結果 |
|---|---|
| 未宣言のキーをリテラルで読む | 噛んだ（赤） |
| 未宣言のキーを内包表記の変数で読む | 噛んだ（赤） |
| 宣言だけして読まない（逆方向） | 噛んだ（赤） |

### 変異検証（全緑は「壊していない」の証明ではない）

実装後に 14 個の変異を注入して、テストが本当に噛むかを実測した。

| 結果 | 内容 |
|---|---|
| 噛んだ（12） | H列にT番号を漏らす / 収益科目の除外を外す / 年跨ぎの前年回退を外す / canonical 数でなく語数で判定 / 未分類負数も credit_adjust に倒す / carry_over を記帳する / carry_over の区画強制を外す / 外貨行で foreign を金額に使う / 閾値を `>` にする / `statement_page` を分解しない / ポイント語表を空にする / 見出し表を殺す（見出しテスト） |
| **噛まなかった（2）** | 見出し解決を殺しても**金額は変わらない**（K11 の降格根拠。§9 に記録） |

最初に書いた変異 2 件は**変異そのものが不完全**（表の一部しか壊していなかった）で、
「テストが甘い」と誤診しかけた。**変異が効いていることを先に確かめる**必要がある。
