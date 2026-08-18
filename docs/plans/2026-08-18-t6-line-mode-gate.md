# T6: `sheets_output` の `line_mode` ゲート（AD-6）

**日付**: 2026-08-18
**母 Plan**: `docs/plans/2026-08-12-credit-card-doctype.md` §5 T6 / AD-6 / AD-8 / AD-11
**追加要求**: `docs/plans/2026-08-17-t4-card-prompts-builders.md` §10（5 件）
**申し送り**: `docs/plans/2026-08-17-t5-window-retry.md` §11.4（純関数の共有）
**着手時の基線**: `main` @ 3adca95、全量 895 tests 緑、`sheets_output.py` 未改造

---

## 1. 目標と非目標

### 目標

`sheets_output.append_entries` に **明示ゲート** `line_mode` を入れ、クレカ明細 /
交通系IC の「1 明細行 = 1 独立取引」を Sheets 層で表現する。既存 4 doc_type
（receipt / purchase_invoice / sales_invoice / salary_slip）の出力は **A:AB 28 列
すべてで 1 バイトも変えない**。

### 非目標（この Plan では**やらない**）

| 項目 | 担当 | 理由 |
|---|---|---|
| タブ振り分けの変更 | — | AD-T3-1。混載 nimoca が「_カード明細」タブに落ちるのは**仕様**。T4 §10-⑤ が明示的に禁止 |
| `detect_deduction_risks`（控除リスクの doc 級集約） | T8 | 新機能であって行級化ではない |
| U列への `INVOICE_CONFIRM_TAG` 付与 | T8 | T4 §10 が `_needs_invoice_confirm` を「T8 が消費する producer 側フィールド」と明記 |
| `_apply_ocr_overrides` の doc_type 豁免 | T7 | B列 取引日の汚染は T7 でしか塞がらない |
| 検算の結線・元帳 | T9 | AD-9 の書込後検算 |
| D列（借方補助科目）の結線 | — | `_determine_debit_sub_account` も死コードだが、T4 §10 が求めているのは **L列だけ**。触れば既存 doc_type の D列が変わる |
| 凡例（`_write_legend`）の文言変更 | — | 全 doc_type 共用定数。触れば既存が変わる（§9 附録 A・Codex R1④ → R2 で撤回済み） |

---

## 2. 順序の罠（最優先で守る）

T6 の DoD 第一条は「既存 doc_type の **golden row snapshot（A:AB 28 列）が完全一致**」。
**その snapshot はまだ存在しない**（`test_sheets_output.py` は 57 テストあるが整行を
固定したものは 1 つも無い。リポジトリ直下の `golden/` は 2026-07-21 の領収書
pipeline 出力で別物）。

> **鉄則**: `sheets_output.py` に 1 文字でも触る**前**に現行 28 列を捕獲する。
> 後から作れば、改造後の挙動を「正解」として記録するだけになる。

したがって **T6-0 を最初のタスクとし、それが緑になるまで実装タスクへ進まない**。

---

## 3. 設計（AD-6 の列ごと対照表の実装）

### 3.1 ゲート

```python
line_mode = bool(entries_data.get("line_mode"))
```

`line_mode` キーは `ocr_engine._build_doc_result` が **actual doc_type が
`LINE_MODE_DOC_TYPES` のときだけ**書く。既存 doc_type にはキー自体が無いので
`.get()` は `None`（falsy）。**既存経路は分岐に入らない**。

### 3.2 券種の判定キー（Codex R1① の指摘で修正。R2 で維持）

`line_mode` の中でクレカ / nimoca を区別する必要がある場面（§3.8 の
`missing_vendor` 抑制）では、**`append_entries` の引数 `doc_type` を使ってはいけない**。

| 値 | 中身 | 出処 |
|---|---|---|
| `append_entries(doc_type=...)` 引数 | **folder** doc_type | `main.py:578` が `process_file` の引数をそのまま渡す |
| `entries_data["doc_type"]` | **actual** doc_type（この頁を実際に何として解析したか） | `ocr_engine.py:1890`、語義は同 2181 の docstring |

混載フォルダでは nimoca の頁が `doc_type=credit_card` として `append_entries` に
到達する（AD-T3-1 の仕様）。引数で判定すると **抑制が効かない**。

**構造上の保証（Codex R2 の指摘で表現を限定）**: `line_mode` と `doc_type` は
**同一関数** `_build_doc_result`（`ocr_engine.py:1860` 付近）が書く。よって
**`ocr_engine` の通常生成経路では** `line_mode=True` の result は必ず `doc_type` を
持ち、「逐行記帳なのに券種不明」という半開状態は作れない。
`_blank_result`（`ocr_engine.py:2145`）は両方とも書かないので、提示行・除外頁は
旧経路（占位行）へ落ちる。

> **限定の理由**: 手書きの dict なら半開状態は作れる。「構造上不可能」ではなく
> 「producer 経路では発生しない」が正確な主張。テスト（A11）もその粒度で書く。

**副作用（Codex R2 の新指摘）**: この設計は `result["doc_type"]` に**初めて読み手を
作る**。現在「読み手は居ない」と書いてある 2 箇所が嘘になるので、同時に直す:

- `ocr_engine.py:1878` の docstring「現在この键の読み手は居らず」
- `test_ocr_engine_mixed_folder.py:210` 付近の「無消費の键」という説明

正しい表現は「**タブ選択には使わないが、出力層の doc_type 局所判定には使う**」。
挙動バグではないが、放置すると将来のレビューを誤誘導する。

### 3.3 列ごとの新旧対照（AD-6 の正本 ＋ T4 §10 の追加）

| 列 | 旧経路（`line_mode=False`） | 新経路（`line_mode=True`） | 出典 |
|---|---|---|---|
| A 取引No | 呼出ごとに 1 つ | **行ごとに連番**（`base + i`） | AD-6 |
| B 取引日 | `entries_data["date"]` | `entry["date"]`、空なら doc 級へ回帰 | AD-6 |
| F 借方取引先 | `vendor_name` | `entry["debit_vendor"]` | AD-6 |
| G 借方税区分 | `entry["debit_tax_type"]` | 同左を `CC_TAX_TYPE_RENDERING` で省略名へ | AD-11 / T4 §10-④ |
| H 借方インボイス | `_sanitize_invoice_num(...)` | `_resolve_invoice_cell(entry)` | AD-8 / T4 §10 |
| L 貸方補助科目 | `""`（直書き） | `_determine_credit_sub_account(...)` | AD-11 / T4 §10-① |
| T 仕訳メモ | doc 級 `memo` | `entry["memo"]` | AD-6 |
| 借方科目の `CREDIT_ONLY_ACCOUNTS` 置換 | 常に置換 | **`未払金` だけ豁免** | AD-5 / T4 §10-② |
| 金額 0 の行 | 無音 skip | **1 行書く** | AD-T4-8 / T4 §10-③ |
| 異常検知 | doc 級 parent | 行級 parent ＋ 抑制表 | AD-6 / §3.8 |
| 取引No キャッシュ書戻し | `+ 1` | `+ len(rows)` | AD-6（Codex P0） |

**L列の実装形態**: `_determine_credit_sub_account(doc_type, entry, vendor_name)` を
`line_mode` のときだけ呼ぶ。既存 2 分岐（receipt→社長名 / purchase_invoice→取引先名）は
**1 文字も変えない**（AD-11）。credit_card / transit_ic は「その他」分岐に落ち
`entry.get("credit_sub_account", "")` を返す。`card_entries._base_entry` が
そこへカード名を入れている（`card_entries.py:467`）ので AD-11 の
「貸方補助＝カード名」が成立する。

> 既存 doc_type で**呼ばない**理由: 呼ぶと receipt の L列が `""` から
> `（社長名未設定）` へ変わり、golden snapshot が即座に赤くなる。
> この関数が死コードなのは既存 doc_type にとっては**現状維持が正**である。

**`未払金` 豁免のキー**: 文字列を直書きせず
`DOC_TYPE_CONFIG[doc_type]["default_credit"]` から取る。credit_card / transit_ic とも
`"未払金"`（`doc_types.py:59, 68` で確認済み）なので、混載で folder doc_type が
渡っても同じ値になる。豁免は **`default_credit` と一致する 1 語だけ**で、
`CREDIT_ONLY_ACCOUNTS` の他の 8 語（現金・普通預金等）は `line_mode` でも従来どおり
`UNKNOWN_ACCOUNT` へ置換する。

### 3.4 取引No の連番と書戻し

```
base = self._get_next_txn_no(tab_name, ws)
row[0] = base + i          if line_mode else base
書戻し: base + len(rows)   if line_mode else base + 1
```

既存 doc_type は複合仕訳で `len(rows) > 1` になりうるため、書戻しを無条件に
`+ len(rows)` にしてはいけない（取引No が飛ぶ）。**必ず条件付き**。

復旧時（`_read_ensure_and_write` 内で `actual_txn_no != base` のとき）も同様に、
`line_mode` では `actual + i` で N 連番を一括再割当てする。

### 3.4.1 ファイル境界の採番（Codex R3 P0。当初 Plan の見落とし）

**見落としていた事実**: `start_new_file` は毎回 `self._tab_next_txn[tab_name] = 1`
へリセットする（`sheets_output.py:211`）。そして `main` は**ファイル 1 件ごとに必ず**
これを呼ぶ（`main.py:1098`、コメントも「PDF 間分割線 + 取引No リセット」と明記）。

したがって §3.4 の設計だけでは、カード明細ファイル A が `1..100` を書いた後、
ファイル B も `1..100` から始まる。**同一タブ内で取引No が完全に重複する**。

**裁決: `line_mode` 対象の doc_type ではリセットしない。** 既存 4 doc_type の
リセットは 1 文字も変えない。

```python
# start_new_file の末尾
if doc_type in LINE_MODE_DOC_TYPES:
    self._tab_next_txn.pop(tab_name, None)   # 次回 _get_next_txn_no が実測し直す
else:
    self._tab_next_txn[tab_name] = 1         # 既存挙動を維持
```

判定は **folder doc_type**（`start_new_file` の引数）で行う。§3.2 の
「actual を使え」とは**逆**である点に注意 —— `start_new_file` は 1 頁目を OCR する
**前**に呼ばれるので actual doc_type はまだ存在しない。かつタブは folder doc_type で
決まる（AD-T3-1）ので、採番の単位もタブと同じ folder 側で揃うのが正しい。

**採用理由（Codex の理由とは別）**: Codex は「MF の A列は複合仕訳グルーピングなので
独立取引が同一取引No を共有する帳簿事故になる」と述べたが、**MF が A列をどう
グルーピングするかは未検証**である。F-12（`docs/plans/2026-08-12-credit-card-sample-facts.md:156`）
は 27 列の一致・必須列・H列の正規値までしか記録しておらず、**A列の分組規則は
書かれていない**。母 Plan 212 行の「単一/複合仕訳の区別に使用」も出典が同じ F-12 で、
原典に該当記述が無い。

採用の根拠は仮説ではなく**語義**に置く: `line_mode` は「1 明細行 = 1 **独立**取引」
（AD-6）。独立取引の識別子がタブ内で衝突してよい理由が無い。既存 doc_type の
リセットは「1 ファイル = 1 取引」という語義の自然な帰結であり、`line_mode` には
その語義が無い。

> **附帯発見（T6 では修正しない。§11 に P1 として記録）**: 既存 4 doc_type にも
> 同じ「ファイル跨ぎの取引No 重複」は存在する（ファイル A も B も txn 1）。
> 本番稼働中の挙動であり、T6 の「既存は 1 バイトも変えない」に反するため触らない。

### 3.5 金額 0 の占位行（AD-T4-8）

現行の `if not amount or int(amount) == 0: continue` を純関数へ抽出する
（T5 §11.4 の申し送り）:

```python
def is_bookable_row(entry, line_mode=False):
    """この entry を MF の 1 行として書くか。

    line_mode では金額 0 の占位 entry も書く（card_entries._placeholder が
    作る外貨占位行・要確認行を無音で落とさないため）。ただし amount 欠損・
    None は除く —— 後段の int(amount) が落ちるか、不正な payload が
    そのまま帳簿になる（Codex R3 P1）。
    """
    if line_mode:
        return entry.get("amount") is not None
    amount = entry.get("amount")
    return bool(amount) and int(amount) != 0
```

**`line_mode` 側を `return True` にしない理由（Codex R3 P1 を採用。ただし条件は強化）**:
Codex の案は `"amount" in entry` だが、それでは `amount: None` を通してしまい
後段の `int(None)` で `TypeError` になる。`is not None` まで見る。
`card_entries._placeholder` は必ず `amount: 0` を入れる（`card_entries.py:498`）ので、
producer が正常なら全行が通る。通らない行が出たら producer 側の契約違反であり、
無音で落とすのではなく T6-4 の DoD で語義を固定する。

**逐字等価を守る（非 line_mode 側）**: 現行 `if not amount or int(amount) == 0: continue`
と逐値で突合済み。**短絡位置が同じ**ことが要点 —— どちらも `amount` が truthy の
ときだけ `int()` を評価するので、例外挙動まで一致する:

| `amount` | 現行 | 抽出後 | 一致 |
|---|---|---|---|
| `None` / `0` / `""` | skip（`int()` を評価しない） | `False`（`int()` を評価しない） | ✓ |
| `"0"` | `int("0")==0` → skip | `int("0")!=0` → `False` | ✓ |
| `0.4` | `int(0.4)==0` → skip | `int(0.4)!=0` → `False` | ✓ |
| `"abc"` | `ValueError` | `ValueError`（同じ位置で） | ✓ |

> ここに `try/except` を足してはいけない。既存 doc_type が汚いデータに当たった
> ときの挙動が変わり、しかも golden（きれいなデータ）では検出できない。

`test_main_process_file._AmountAwareWriter` の手写し述語をこの関数へ差し替える
（T5 §11.4 の宿題を解消）。

### 3.6 H列（`_resolve_invoice_cell`。AD-8）

```python
def _resolve_invoice_cell(entry):
    verdict = entry.get("_deduction")
    if verdict is None:
        return ""      # 占位行・ポイント充当行は判定を通っていない
    return invoice_classification.render_invoice_column(verdict)
```

`_deduction`（`DeductionVerdict`）は `card_entries._build` が既に entry へ載せている
（`card_entries.py:583`）。**`sheets_output` で再判定しない**（判定木は 1 箇所）。

> **結線の証明が難しい点**: `config.INVOICE_COL_RENDERING` は現在全値が空文字
> （AD-8 の事務所慣例）。よってこの関数は**常に空文字を返す**＝結線しなくても
> 結果が同じで、通常のテストでは結線を証明できない。

**二重で縛る（Codex R2 の提案を採用）**:

| 層 | 方法 | 何を証明するか |
|---|---|---|
| 統合寄り | `invoice_classification.INVOICE_COL_RENDERING` を非空へ patch | 変換表 → H列 の経路全体が繋がっている |
| 配線寄り | `invoice_classification.render_invoice_column` を mock | H列が `_sanitize_invoice_num` ではなく **resolver の戻り値**である |
| 継目 | `invoice_classification.INVOICE_COL_RENDERING == config.INVOICE_COL_RENDERING` | config → コピー が忠実 |

片方だけでは足りない。表 patch だけでは「resolver を経由せず偶然同じ値」を
排除できず、mock だけでは表からの経路を見ていない。

> **起案時の誤り（T6-6 実施中に判明・訂正済み）**: 当初は
> 「`config.INVOICE_COL_RENDERING` を patch」と書いていたが**届かない**。
> `invoice_classification.py:149` が import 時に `dict(...)` でコピーを作る
> （config が無い環境でも動くための設計。`test_dependency_weight` が
> `invoice_classification` を「重依存に触れない」側に固定している）。
> よって patch 先は `invoice_classification` 側。config → コピーの一段が
> 見えなくなるので、3 行目の継目テストを足して塞いだ。

### 3.7 G列（税区分の省略名。AD-11 / T4 §10-④）

```python
tax_type = entry.get("debit_tax_type", "")
if line_mode:
    tax_type = CC_TAX_TYPE_RENDERING.get(tax_type, tax_type)
```

出力層のみ。builder は canonical のまま出す。`対象外` は変換表に**入れない**
（`anomaly_detector.EXEMPT_TAX_TYPE` が精確等値で判定しているため。
`test_credit_card_config.TaxTypeRenderingTest` が既に見張っている）。

### 3.8 異常検知の行級化と抑制（趙裁定 2026-08-18・選択肢1）

**問題**: カード明細の raw_data には **doc 級 date / vendor / invoice_num が存在しない**
（`card_prompts.py` の card 級フィールドは `member_no` / `statement_date` /
`card_name` のみ）。よって現行 `detect_anomalies` を doc 級 parent のまま呼ぶと、
**全行に赤(B列) ＋ 橙(F列) ＋ 黄(H列)** が付く。300 行の ETC 明細なら 900 個の
タグが立ち、AD-7 が守ろうとした「赤の信号価値」が消える。

**行級 parent**:

```python
row_parent = {**entries_data,
              "date": entry.get("date") or entries_data.get("date", ""),
              "vendor": entry.get("debit_vendor", "")}
```

`doc_type` キーは `entries_data` 由来のまま引き継がれる（＝ actual doc_type）。

**抑制（趙裁定: 選択肢1）**:

| フラグ | 抑制する doc_type | 理由 |
|---|---|---|
| `missing_vendor` | `transit_ic` **のみ** | nimoca は券面に店名欄が無い（`card_prompts.py:109` が `merchant` を「空文字でよい」と定義）＝ **producer 契約上の空**であって読み落としではない。クレカは `merchant` を「利用店名・摘要の主行」として要求している（同 97）ので、空＝読み落とし＝人に見せるべき異常 |
| `missing_invoice` / `invalid_t_number` | `credit_card` ＋ `transit_ic` | AD-8 の裁定で H列は**空欄が正**。空欄を異常として扱えば全行が黄になる。カード明細に加盟店の登録番号は構造的に存在しない（F-11 / F-14） |

**実装位置（Codex R1② の指摘で変更。R2 で形態を確認）**: `anomaly_detector` 側に置く。
`sheets_output` でフラグを濾すと、T8 が `_suppress_invoice_flags` を detector に
置いたとき二重管理になり漂移する。

**形態**: `detect_anomalies` の**シグネチャは変えない**。detector 内部で
`parent_data.get("doc_type")` を読み、新設の集合を引く純加算にする:

```python
_VENDOR_OPTIONAL_DOC_TYPES = frozenset({DocType.TRANSIT_IC})
_INVOICE_OPTIONAL_DOC_TYPES = frozenset({DocType.CREDIT_CARD, DocType.TRANSIT_IC})

vendor_optional = parent_data.get("doc_type") in _VENDOR_OPTIONAL_DOC_TYPES
if not vendor and not vendor_optional:
    flags.append(...)
```

既存 4 doc_type は表に載らないので従来どおりフラグが出る（純加算 ＝ 既存は
1 バイトも動かない）。`doc_type` キーを持たない旧経路・占位経路は `None` で不成立。
T8 が `_suppress_invoice_flags` を作るときは**ロジックの引越しではなく表の統合**で済む。

> シグネチャを変えない理由: 変えると既存 4 doc_type 全部の呼出点に触れることになり、
> リスク面が 1 ファイルから 2 ファイルへ広がる。T6 の DoD は「既存 golden 完全一致」。
> なお `detect_anomalies` の実装呼出は `sheets_output.py:306` の 1 箇所だけ
> （Codex が全数確認済み）。

---

## 4. タスク清単（各項に DoD）

### T6-0. golden row snapshot の捕獲（**最初に実施。実装より前**）

新規 `test_sheets_output_golden.py`。既存 4 doc_type それぞれに代表的 payload を
与え、`_write_with_retry` に渡る **A:AB 28 列の整行**を定数として固定する。

- 時刻列（X 作成日時 / Z 最終更新日時）は `sheets_output.datetime` を patch して凍結する
  （凍結しないと毎回値が変わり snapshot にならない）
- 代表 payload は「複数行の複合仕訳」「異常フラグが立つ行」「T番号あり/なし」を含める
  —— 1 行だけの正常系を固定しても改造の影響を捕まえられない

**DoD**: `sheets_output.py` が**未改造の状態で**全緑。かつ 4 doc_type すべてについて
28 列すべてが定数と逐欄一致していること（`assertEqual(row, EXPECTED)` の丸ごと比較。
列を抜き出しての部分比較にしない）。

### T6-1. 記帳可能行の述語を純関数へ抽出

`is_bookable_row(entry, line_mode=False)` を `sheets_output` のモジュール級に新設し、
`append_entries` をそれに差し替える。`test_main_process_file._AmountAwareWriter` の
手写しも差し替える。

**DoD**: T6-0 の golden が全緑（＝挙動不変）。895 tests 緑。
`_AmountAwareWriter` に述語の写しが 1 行も残っていないこと。

### T6-2. `line_mode` ゲートと A/B/F/T 列の行級化

**DoD**: golden 全緑。`line_mode=True` の payload で A 列が `base, base+1, ...`、
B/F/T 列が entry 由来になること。`line_mode` キーが無い payload では
すべて従来値であること（**同じテストで両方を assert する**）。

### T6-3. 取引No キャッシュと復旧時の N 連番

**DoD**:
- 同一タブへ連続 2 回 append しても取引No が重複しない（母 Plan の Codex P0。§5 A4）
- **`start_new_file` を挟んだ 2 ファイル連続処理でも重複しない**（Codex R3 P0。§5 A13）
  —— 「append を 2 回」だけのテストでは本番経路（ファイルごとに `start_new_file`）を
  通らないので、両方が要る
- 既存 doc_type では `start_new_file` が従来どおり 1 にリセットすること
- 既存 doc_type の複合仕訳（3 行 1 取引）で書戻しが `+1` のままであること
- 復旧発生時、`line_mode` では `actual + i` で N 連番が再割当てされること

### T6-4. 金額 0 の占位行を書く

**DoD**: `card_entries._placeholder` が作る形（`amount: 0`, `_placeholder_reason` 付き）の
entry が MF 行として出力されること。既存 doc_type では従来どおり skip され、
全行 0 なら `_write_unrecognized_row` に落ちること。
**`amount` 欠損 / `None` の entry は `line_mode` でも書かない**ことを固定する
（§3.5。producer の契約違反であって占位行ではない）。`line_mode` で全行が
そうなった場合は既存経路どおり `_write_unrecognized_row` に落ちること
—— 無音で消えないこと自体は IP-401 の不変式で守られる。

### T6-5. G列の省略名変換

**DoD**: `line_mode` で `課対仕入10%` → `課仕 10%`。既存 doc_type では変換されない。
`対象外` は `line_mode` でも変換されない。

### T6-6. H列 `_resolve_invoice_cell` の新設

**DoD**: `_deduction` を持つ entry で `render_invoice_column` の結果が H列に出る。
`_deduction` が無い entry では空文字。**結線の証明は §3.6 の二重テスト**
（config patch ＋ 関数 mock）の両方が緑であること。

### T6-7. L列 `credit_sub_account` の結線

**DoD**: `line_mode` で L列 = カード名。既存 4 doc_type の L列が `""` のまま
（golden で担保）。`_determine_credit_sub_account` の既存 2 分岐が無改造。
**混載条件も固定する**（Codex R3 P1）: folder `doc_type=credit_card` /
`entries_data["doc_type"]=transit_ic` のとき、タブ名は「_カード明細」のまま
（AD-T3-1）、L列は **entry の `credit_sub_account`**（nimoca 側の値）になること。
タブは folder・列値は entry 由来、という二重契約を 1 つのテストで縛る。

### T6-8. `未払金` の `CREDIT_ONLY_ACCOUNTS` 豁免

**DoD**: `line_mode` のポイント充当行（借方 未払金 / 貸方 雑収入）の借方が
`未払金` のまま出ること。`line_mode` でも `現金` 等の他の貸方専用科目は
`未確定勘定` へ置換されること。既存 doc_type は無変化。

### T6-9. 異常検知の行級化と抑制表

**DoD**:
- `line_mode` で `missing_date` / `missing_vendor` が**行の値**で判定される
- `transit_ic` で `missing_vendor` が出ない／`credit_card` で出る（**双方向**）
- `credit_card` / `transit_ic` で `missing_invoice` / `invalid_t_number` が出ない
- 既存 4 doc_type の異常検知が 1 件も変わらない（`test_anomaly_detector` 無修正で緑）
- **`result["doc_type"]` の説明を直す**（§3.2 の副作用）: `ocr_engine.py:1878` の
  docstring と `test_ocr_engine_mixed_folder.py:210` 付近の「無消費」記述

### T6-10. 番人の更新（順序 1→2→3 厳守）

1. `test_credit_card_config.UnwiredItemsTest.WATCHED` に `sheets_output.py` を足す
   → この時点ではまだ緑
2. `CC_TAX_TYPE_RENDERING` を `sheets_output` へ結線
   → **番人が赤くなる。その出力を記録する**（赤くならなければ番人が効いていない）
3. `UNWIRED` から `CC_TAX_TYPE_RENDERING` を外す → 緑

**DoD**: 2 の赤い出力が実施記録（§10）に貼ってあること。3 の後に緑。

### T6-11. 全量回帰と変異検証

**DoD**: 895 + 新規 tests が全緑。加えて**変異検証**（全緑は「壊していない」の
証明ではない。memory: `green-tests-hide-env-dependent-breakage`）:

| 変異 | 期待 |
|---|---|
| `line_mode` ゲートを常時 True にする | golden が赤 |
| 取引No 書戻しを `+1` 固定に戻す | 連続 append テストが赤 |
| `start_new_file` のリセット抑止を外す | 2 ファイル連続テスト（A13）が赤 |
| `line_mode` の述語を `return True` に戻す | `amount` 欠損テスト（A14）が赤 |
| `missing_vendor` 抑制表を空にする | nimoca テストが赤 |
| 抑制キーを引数 `doc_type` に変える | 混載 nimoca テストが赤 |
| `_resolve_invoice_cell` を `return ""` 固定にする | H列の二重テストが赤 |
| L列を `""` 直書きに戻す | カード名テストが赤 |

---

## 5. 受入基準（機械判定）

| # | 基準 | 判定方法 |
|---|---|---|
| A1 | 既存 4 doc_type の 28 列が完全一致 | `test_sheets_output_golden`（逐欄 assertEqual） |
| A2 | 既存 `test_sheets_output.py` が byte-level 無変更、かつ単体緑 | `git diff --exit-code -- test_sheets_output.py`（終了コードで判定。`--stat` の出力目視にしない） |
| A3 | `line_mode` で A列が行ごと連番 | 新テスト |
| A4 | 同一タブへ連続 2 回 append で取引No 重複なし | 新テスト（母 Plan の Codex P0） |
| A5 | 復旧時に N 連番が再割当て | 新テスト（既存 `TabDeletedRecoveryTest` の形を踏襲） |
| A6 | 金額 0 の占位行が出力される | 新テスト |
| A7 | H列の結線が二重に証明される | 変換表 patch ＋ 関数 mock ＋ config コピーの継目（§3.6 の訂正を参照） |
| A8 | `transit_ic` は橙なし / `credit_card` は橙あり | 双方向テスト |
| A9 | 税区分が `line_mode` でのみ省略名 | 双方向テスト |
| A10 | 番人が `sheets_output.py` を視野に入れた | `UnwiredItemsTest` の WATCHED |
| A11 | **producer 経路では** `line_mode=True` の result が必ず `doc_type` を持つ | `ocr_engine` 側の不変式テスト（手書き dict は対象外） |
| A12 | 混載 nimoca（folder=credit_card / actual=transit_ic）で抑制が効き、L列が entry 由来 | 新テスト |
| A13 | **`start_new_file` を挟んだ 2 ファイル連続処理**で取引No 重複なし（`line_mode` のみ）。既存 doc_type は 1 にリセットされたまま | 新テスト（Codex R3 P0） |
| A14 | `line_mode` でも `amount` 欠損 / `None` の entry は書かれない | 新テスト |
| A15 | 全量緑 | `python -m unittest discover -p "test_*.py"` |

---

## 6. テスト戦略（TDD）

1. **T6-0 は例外的に「現状を固定する」テスト**（特徴づけテスト）。RED から始まらない
   —— 現状が正なので最初から緑。これが緑にならなければ前提が崩れている
2. 以降の各タスクは **RED → GREEN**。先に新経路のテストを書き、落ちることを確認してから実装
3. 各タスク完了時に golden を再実行（既存が動いていないことの継続確認）
4. 最後に変異検証（§4 T6-11 の表）

**テスト配置**:

| ファイル | 内容 |
|---|---|
| `test_sheets_output_golden.py`（新規） | 既存 4 doc_type の 28 列固定 |
| `test_sheets_output_line_mode.py`（新規） | `line_mode` の全挙動（A12 の混載を含む） |
| `test_sheets_output.py`（**無修正**） | 既存。A2 の受入基準 |
| `test_anomaly_detector.py`（追加のみ） | 抑制表の双方向 |
| `test_main_process_file.py`（修正） | `_AmountAwareWriter` の述語差し替え |
| `test_credit_card_config.py`（修正） | 番人の WATCHED / UNWIRED |
| `test_ocr_engine_mixed_folder.py`（説明修正） | `doc_type` 键に読み手ができた事実 |

---

## 7. 影響面

| ファイル | 変更 | リスク |
|---|---|---|
| `sheets_output.py` | `line_mode` 分岐、純関数 2 本新設、L/G/H 列結線、**`start_new_file` の採番リセットを doc_type で分岐**（§3.4.1） | **高**（本番の帳簿出力そのもの） |
| `anomaly_detector.py` | 抑制表 2 つ、`doc_type` 参照（純加算） | 中（既存 4 doc_type が共用） |
| `ocr_engine.py` | **docstring のみ**（`doc_type` 键の読み手ができた） | 低（挙動不変） |
| `test_main_process_file.py` | 述語の差し替え | 低 |
| `test_credit_card_config.py` | 番人の一覧 | 低 |
| `test_ocr_engine_mixed_folder.py` | 説明文の修正 | 低 |
| `card_entries.py` / `main.py` / `config.py` | **変更しない** | — |

**本番への影響**: `.env` で新 doc_type のフォルダが未設定のため、T6 単体では
本番の挙動は変わらない（新 doc_type の頁が到達しない）。既存 doc_type への
影響が無いことは golden で担保する。

**依存の重さ**: `sheets_output`（重・gspread）が `invoice_classification`（軽）を
import するのは **重→軽の単方向**なので `test_dependency_weight` に違反しない。
`anomaly_detector` が `doc_types` を import するのも同様（`card_entries` が
既に同じことをしている）。

---

## 8. リスクと回退

| # | リスク | 対策 | 回退 |
|---|---|---|---|
| R1 | golden を後から作り、改造後を「正解」として固定してしまう | T6-0 を最初に実施（§2 の鉄則） | — |
| R2 | 取引No の書戻しを無条件 `+len(rows)` にして既存の複合仕訳が飛ぶ | 条件付きにする（§3.4）＋ golden | 条件を戻す |
| R3 | L列結線を全 doc_type に広げ receipt の L列が変わる | `line_mode` のときだけ呼ぶ（§3.3）＋ golden | 呼出を戻す |
| R4 | 券種判定に folder doc_type を使い混載 nimoca で抑制漏れ | `entries_data["doc_type"]` を使う（§3.2）＋ A12 | — |
| R5 | 抑制表が広すぎてクレカの読み落としが無標記になる | `missing_vendor` は transit_ic のみ（趙裁定）＋ A8 の双方向 | 表から外す |
| R6 | `_resolve_invoice_cell` が結線されていないのに緑 | 二重テスト（§3.6） | — |
| R7 | 異常検知の `doc_type` 参照が既存に副作用 | 純加算＋`test_anomaly_detector` 無修正で緑 | 表を空にすれば旧挙動 |
| R8 | 「読み手は居ない」というコメントが嘘のまま残り将来のレビューを誤誘導 | T6-9 の DoD に含める | — |
| R9 | **ファイル境界で `start_new_file` が採番を 1 に戻し、別ファイルの独立取引が同一取引No を持つ** | `line_mode` doc_type だけリセット抑止（§3.4.1）＋ A13 | 抑止を外せば旧挙動 |
| R10 | `line_mode` の記帳可能述語が広すぎ、`amount` 欠損 entry で `int()` が落ちて頁ごと死ぬ | `is not None` を要求（§3.5）＋ A14 | — |

**全体の回退**: 本 Plan の実装変更は `sheets_output.py` / `anomaly_detector.py` の
2 ファイルに閉じており、`git revert` 1 回で戻せる。本番稼働中の経路
（既存 4 doc_type）は golden が守る。

---

## 9. 附録 A: Codex 評審の辯論記録（2026-08-18）

### Round 1 — 4 点の指摘

| # | 指摘 | 裁定 | 理由 |
|---|---|---|---|
| ① | 判定キーに `append_entries` の引数 `doc_type` を使うと混載 nimoca で抑制漏れ | **採用** | 事実確認済み。`main.py:578` は folder doc_type を渡し、actual は `entries_data["doc_type"]`。当方の当初案の欠陥 |
| ② | 抑制は `sheets_output` ではなく `anomaly_detector` に置くべき（T8 と二重管理になる） | **修正のうえ採用** | 方向は正しい。ただしシグネチャ変更ではなく**純加算**（detector 内で `parent_data.get("doc_type")` を引く）にした。シグネチャを変えると既存 4 doc_type の呼出点に触れ、リスク面が広がるため |
| ③ | 第4案（nimoca の F列に `"nimoca"` を入れる）は選択肢1より劣る | **同意**（Codex 自身の却下） | F列は役務提供者を書く欄。nimoca は媒体であって運行主体ではない |
| ④ | テストで双方向固定 ＋ **凡例にも説明を足す** | **部分採用** | テスト双方向は採用。凡例修正は却下 —— `_write_legend` は全 doc_type 共用で、触ると既存 doc_type の新規タブ表示が変わる。しかも既存タブは書き換わらないので新旧不一致になり、改善ではなく劣化 |
| 妥協案 | 内部メタに「取引先空欄は交通IC仕様」を残す | **却下** | `card_entries.py:477` の `_line_kind` が既に表現済み。冗長 |

### Round 2 — 却下 2 件を回して複審

**Codex が撤回した（当方の勝ち）**:

- 凡例修正 —— 「理由は成立しています。`_write_legend` は新規タブ作成時だけの共用出力
  なので、触ると既存 doc_type の新規タブ表示だけが変わり、既存タブとは不一致になる」
- 内部メタ追加 —— 「`_line_kind` があり、交通 IC 行であることは既に表現されている。
  新フィールドは冗長」

**Codex が維持した（対抗者の勝ち。採用済み）**:

- `result["doc_type"]` を使うべき —— §3.2 に反映

**Round 2 の新指摘（3 件とも採用）**:

| # | 指摘 | 反映先 |
|---|---|---|
| a | 「半開状態は構造上作れない」は言い過ぎ。手書き dict なら作れる。主張は「ocr_engine の通常生成経路では作れない」に限定すべき | §3.2 の表現と A11 |
| b | この変更で `result["doc_type"]` に初めて読み手ができる。`ocr_engine.py:1878` の「読み手は居らず」と `test_ocr_engine_mixed_folder.py:210` の「無消費の键」が嘘になる | §3.2 の副作用、T6-9 の DoD、R8 |
| c | H列の結線は config patch（統合寄り）だけでなく `render_invoice_column` の mock（配線寄り）も併せると堅い | §3.6 の二重テスト、A7 |

**Codex による事実確認**:

- `detect_anomalies` の実装呼出は `sheets_output.py:306` の **1 箇所だけ**
- `test_anomaly_detector` / `test_invoice_classification` 101 件 OK、
  `test_sheets_output` 57 件 OK（着手前の基線）

### Round 3 — Plan 全体への評審（5 件）

| # | 重大度 | 指摘 | 裁定 |
|---|---|---|---|
| a | P0 | §3.4 が `start_new_file` の取引No リセットを見落としている。`main.py:1098` はファイルごとに必ず呼ぶので、ファイル A が 1..100 を書いた後 B も 1..100 になる | **結論は採用・理由は却下**。§3.4.1 を新設。ただし Codex の理由（「MF の A列は複合仕訳グルーピングなので帳簿事故」）は**未検証の仮説**——F-12 の原典に A列の分組規則は書かれていない。採用根拠は AD-6 の語義（1 明細行 = 1 **独立**取引）に置き換えた |
| b | P1 | A2 の判定 `git diff --stat` が空、は運用依存で弱い。`git diff --exit-code` にすべき | **採用**。終了コード判定の方が機械判定として正しい |
| c | P1 | L列テストが混載（folder=credit_card / actual=transit_ic）を縛っていない | **採用**。T6-7 DoD と A12 に追加 |
| d | P1 | `is_bookable_row(line_mode=True) return True` は広すぎる。`amount` 欠損・`None` が通り後段の `int()` で落ちる | **採用・条件を強化**。Codex 案の `"amount" in entry` では `amount: None` を通すので `entry.get("amount") is not None` にした |
| e | P2 | リスク表に `start_new_file` 由来のリスクが無い | **採用**。R9 として追加（d の分は R10） |

**Codex が「問題なし」と明示した項目**: §4 のタスク順序（T6-0 を最初に置く点を含む）、
§3.3 の 3 原典との突合、§3.5 の非 line_mode 逐字等価、過度設計の有無。

---

## 10. 附録 B: T6 の範囲外に見つけた既存の欠陥

### P1: 既存 4 doc_type にもファイル跨ぎの取引No 重複がある

`start_new_file` が毎ファイル `_tab_next_txn[tab_name] = 1` に戻すため、同一タブ内で
ファイル A の取引No とファイル B の取引No が重複する（どちらも 1 から始まる）。
`line_mode` については §3.4.1 で塞ぐが、**既存 4 doc_type は塞がない**
（T6 の「既存は 1 バイトも変えない」に反するため）。

**未検証の点**: これが実害かどうかは **MF が A列をどう解釈するか**に依存する。
F-12（`docs/plans/2026-08-12-credit-card-sample-facts.md:156`）は 27 列の一致・
必須列・H列の正規値までしか記録しておらず、**A列の分組規則は原典に無い**。
母 Plan 212 行の「単一/複合仕訳の区別に使用（F-12）」も同じ出典を指しているが、
原典に該当記述が見当たらない。

- 全表を A列 でグルーピングする仕様なら → 既存 doc_type は本番で誤った複合仕訳を
  作っている可能性がある（本番稼働中なので優先度は高い）
- 連続ブロック単位、または表示専用なら → 実害なし

**趙への申し送り**: MF の A列仕様を実測で確定させる価値がある。確定するまでは
推測で既存挙動を変えない。

---

### P2: `anomaly_detector` が「脱 venv」の番人に登録されていない

`test_dependency_weight._MUST_STAY_LIGHT` に `anomaly_detector` も
`test_anomaly_detector_line_mode` も載っていない。両者とも**今日は** venv 無しで
走る（実測済み）が、契約としては固定されていない。T6-9 で
`from doc_types import DocType` を足したので、`doc_types` が将来重くなると
無症状で venv 必須へ劣化する。

**T6 でやらない理由**: 番人の視野を広げるのは T6 の依頼範囲外。
登録は 2 行の追加で済む。趙が拍板すれば別タスクで入れる。

### P2: 番人 `UnwiredItemsTest` の `UNWIRED` が空になった

T6-10 で最後の 1 項が結線され一覧が空になった。検出能力は別の 2 本
（`test_the_watchdog_can_actually_see_a_wired_item` /
`test_the_watchdog_sees_a_parenthesized_import`）が測り続けるので死んではいないが、
**「まだ読み手が無い config 項目」を次に足す人が一覧へ戻すのを忘れると
番人は永久に空回りする**。config へ未結線項目を足す手順に組み込むこと。

---

## 11. 実施記録

### T6-0. golden row snapshot の捕獲（完了 2026-08-18）

`test_sheets_output_golden.py` を新設（11 テスト）。**`sheets_output.py` を
1 文字も触っていない状態で**捕獲した（§2 の鉄則を遵守）。

**捕獲した 8 行の内訳**（`append_entries` の主要分岐を覆う）:

| ケース | 覆う分岐 |
|---|---|
| receipt 行1 | `ACCOUNT_MAP` 写像（消耗品費→備品・消耗品費）／有効T番号／J列に税額あり／**U列が空＝無標色** |
| receipt 行2 | 要確認科目（地代家賃）→ `account_review` → U列「黄系」 |
| purchase_invoice 行1 | 「店名 - 内容」摘要形式／doc 級T番号の継承 |
| purchase_invoice 行2 | **`CREDIT_ONLY_ACCOUNTS` 置換**（未払金→未確定勘定）→ U列「橙系」 |
| sales_invoice | 貸方側に税区分／T番号なし → 黄系 |
| salary_slip 行2 | **`credit_sub_account` が entry に在るのに L列は空**（死コードの証拠） |
| unrecognized_full | 占位行（完全認識不能）→ U列「赤系」／**Y・AA列の作成者が空** |
| unrecognized_partial | 占位行（金額0で skip → 部分認識ラベル） |

**実測で判明した事実**（Plan 起草時には知らなかったもの）:

1. `credit_sub_account` の producer は **salary_slip（4 箇所）と card_entries（1 箇所）
   だけ**。receipt / purchase_invoice / sales_invoice は出していない。
   よって `_determine_credit_sub_account` を全 doc_type へ結線すると
   **3 つの既存 doc_type すべてで L列が変わる**（receipt →「（社長名未設定）」、
   purchase_invoice → 取引先名、salary_slip →「社会保険料」等）。
   §3.3 の「`line_mode` のときだけ呼ぶ」判断が実測で裏付けられた。
2. 占位行は Y列 作成者 / AA列 最終更新者 が**空**（通常行は uploader 名が入る）。
   `_write_unrecognized_row` が uploader を書かないため。

**変異検証（golden が本当に噛むか）**: 「全緑は壊していないことの証明ではない」
（memory: `green-tests-hide-env-dependent-breakage`）ので、2 つの変異を注入して
確認した。いずれも注入後に `git diff` が空になるまで復元済み。

| 注入した変異 | 新 golden（11） | 既存 `test_sheets_output`（57） |
|---|---|---|
| L列を全 doc_type で結線（T6-7 で最もやりがちな誤り） | **6 失敗** | **全緑** |
| A列を行ごと連番に（`line_mode` ゲートが常時 True の状態） | **4 失敗** | **全緑** |

**この表が T6-0 の存在理由そのもの**: 既存 57 テストは、本番の帳簿出力が変わる
2 種類の改造を**どちらも検出できなかった**。部分列の断言しか持たないため
（整行比較は `grep -n "assertEqual(row," test_sheets_output.py` で 0 件）。

**DoD 判定**: 全て達成。
- `sheets_output.py` 未改造の状態で 11 テスト緑 ✓
- 4 doc_type すべて 28 列を丸ごと `assertEqual`（部分比較にしていない）✓
- 全量 **906 tests 緑**（895 基線 + 新規 11）✓
- `git diff --stat sheets_output.py` が空 ✓

### T6-1. 記帳可能行の述語を純関数へ抽出（完了 2026-08-18）

`sheets_output.is_bookable_row(entry, line_mode=False)` を新設。
`append_entries` と `test_main_process_file._AmountAwareWriter` の両方が使う。
新規テストは `test_sheets_output_line_mode.py`（7 テスト）。

- 非 line_mode は現行式と逐字等価（短絡位置まで一致。`"abc"` で `ValueError`）
- line_mode は `entry.get("amount") is not None`（Codex R3 P1 の案を強化）
- `_AmountAwareWriter` の写しは削除。`grep "int(amount) == 0" test_main_process_file.py` が 0 件

**変異検証**: line_mode 側を `return True` に戻すと新テスト 2 件が赤 ✓
**DoD 判定**: 全量 **913 tests 緑**、golden 全緑（挙動不変の証明）✓

### T6-2. `line_mode` ゲートと A/B/F/T 列の行級化（完了 2026-08-18）

`line_mode = bool(entries_data.get("line_mode"))` を `append_entries` に追加。
A（`base + i`）/ B（`entry["date"]`、空なら doc 級へ回帰）/ F（`entry["debit_vendor"]`）/
T（`entry["memo"]`）を条件分岐。

**両方向を固定した**（キー無し・`False` 明示・`True` の 3 通り）。片側だけだと
「ゲートが常時 True」の変異を検出できない。doc 級 vendor / memo が行へ漏れない
ことも別テストで固定（漏れると全行にカード会社名が付く）。

**DoD 判定**: 全量 **921 tests 緑**、golden 全緑 ✓

### T6-3. 取引No の採番（完了 2026-08-18）

3 箇所を条件分岐にした:

1. 書戻し `actual_txn_no + (len(rows) if line_mode else 1)`
2. 復旧時の再採番 `actual_txn_no + i if line_mode else actual_txn_no`
3. `start_new_file` のリセット —— `line_mode` doc_type ではキャッシュを
   `pop` して次回実測させる（§3.4.1。Codex R3 P0）

**Plan からの逸脱（§7 の影響面を更新）**: `LINE_MODE_DOC_TYPES` を
`ocr_engine` から **`doc_types` へ移した**。`start_new_file` が集合を引く必要が
あり、`sheets_output` に `ocr_engine` を import させると google.generativeai と
起動時のレジストリ検証まで引き込むため。`ocr_engine` 側は import に切り替えた
だけで値・公開名とも同一（Plan は「ocr_engine は docstring のみ」と書いていたが、
import 行 1 行の変更が加わった）。複製 + 突合テスト（`page_family` の
`EXCLUDE_DEST_*` の形）にしなかったのは、あれが「venv 非依存を保つ」ための
妥協であり、両者とも `doc_types` を既に import している本件には当てはまらないため。

**実装中に見つけたハーネスの罠**: `_write_with_retry` を patch して行を捕捉する
とき、fake ws にも反映しないと `_get_next_txn_no` が A列を実測できず、
連続 append テストが**実装ではなくハーネスの都合で**落ちる。一度これで
偽の赤を踏んだ（実装は正しかった）。

**DoD 判定**: 全量 **926 tests 緑**、golden 全緑 ✓
- 連続 2 回 append で重複なし ✓
- `start_new_file` を挟んだ 2 ファイルで重複なし（A13）✓
- 既存 doc_type は 1 にリセットされたまま ✓
- 既存の複合仕訳（3 行 1 取引）は `+1` のまま ✓
- `line_mode` で既存表の A列 max+1 から続く ✓

---

### T6-4. 金額 0 の占位行を書く（完了 2026-08-18）

**実装の変更は 0 行**。述語 `is_bookable_row` は T6-1 で `line_mode` を受ける形に
なっており、`append_entries` は T6-2 で結線済みだった。T6-4 で足したのは
**語義の固定**——テスト 11 件（`ZeroAmountPlaceholderRowTest` 7 件 /
`MissingAmountContractTest` 4 件）。

占位 entry の形は `card_entries._placeholder` を**実物のまま呼んで**作る
（`_placeholder_entry` ヘルパ）。手写しすると producer が形を変えたときに
テストだけが緑で取り残される——T5 §11.4 で解消したばかりの失敗様式を
テスト側で再生産しない。

固定した語義:

| 事象 | `line_mode` | 既存 doc_type |
|---|---|---|
| `amount: 0`（占位行） | 1 行書く。採番も消費する | skip → 占位行へ |
| `amount: None` | 書かない（A14） | 書かない |
| `amount` キー欠損 | 書かない（A14） | 書かない |
| 全行が欠損 | `_write_unrecognized_row`（赤タグ）へ | 同左 |

副次的に固定したもの: S列 摘要に理由文が出ること（金額 0 の行だけ並んでも
人が確認に回せる）／ `_placeholder_reason` の内部 ID（`no_jpy_amount` 等）が
28 列のどこにも漏れないこと／ 4 種の理由すべてが行になること
（reason で選り分ける実装を検出するため）。

**変異検証（最初から緑だったので必須）**: 実装を壊してテストが赤くなるか実測。
git を使わず原文を書き戻し、SHA256 一致で復元を証明した。

| 変異 | 結果 |
|---|---|
| `line_mode` 特例を削除（0 円を落とす旧挙動へ回帰） | 8 件失敗 → 殺した |
| `line_mode` で無条件 `True`（`amount` 欠損も通す） | 6 件失敗 → 殺した |
| ゲートを常時 `True`（既存 doc_type を巻き込む） | 13 件失敗 → 殺した |

**DoD 判定**: `test_sheets_output_line_mode` 31 件緑 / golden 11 件緑 ✓

---

### T6-5. G列の省略名変換（完了 2026-08-18）

`append_entries` 内で行を組む直前に 1 行だけ変換する（§3.7 のとおり）。

**canonical と表示を分離した**: 変換した値は `row` にだけ入れ、`mapped_entry`
（`detect_anomalies` へ渡す dict）は entry の canonical をそのまま持つ。
省略名を判定側へ流すと `anomaly_detector.EXEMPT_TAX_TYPE` 等の精確等値が
無音でずれる。この分離自体をテストで固定した
（`test_anomaly_detection_sees_the_canonical_name`）。

固定した語義: 標準/軽減の 2 値だけ変換 ／ `対象外` は不変（変換表に載せない）
／ 表に無い値は素通し ／ **O列 貸方税区分は変換しない** ／ 既存 doc_type は不変。
結線の証明は `config.CC_TAX_TYPE_RENDERING` を哨兵値へ差し替えるテスト
（`patch.dict` は同一 dict を書き換えるので、関数内 `from config import` でも届く）。

**DoD 判定**: RED 4 件 → 実装 → 緑。`line_mode` 40 件 / golden 11 件 / 既存 57 件 ✓

---

### T6-6. H列 `_resolve_invoice_cell` の新設（完了 2026-08-18）

`sheets_output` に `import invoice_classification`（module import。`from` 形式に
すると mock が刺さらない）。resolver は §3.6 の 4 行そのまま。

**セル値を 1 箇所で決めるようにした**: 従来 H列は `row` 構築と異常検知用
`actual_invoice` で**同じ式を 2 回**書いていた。`debit_invoice` 変数に一本化して
両方が同じ値を見る（既存 doc_type には逐値等価）。既存コメントが宣言していた
「異常検出（実際に書き込む値で判定）」を構造で担保する形になる。

**Plan の誤りを 1 件訂正**（§3.6 に留痕）: patch 先は `config` ではなく
`invoice_classification` 側。import 時コピーのため config を patch しても届かない。
RED 実行で判明した（テストが緑になってから気づいたのではない）。

**DoD 判定**: RED 9/10 件（緑の 1 件は「今は全値空文字」だから通っただけ ——
二重縛りが要る理由の実演）→ 実装 → 緑。全 119 件 ✓

---

### T6-7. L列 `credit_sub_account` の結線（完了 2026-08-18）

`_determine_credit_sub_account` を `line_mode` のときだけ呼ぶ。既存 2 分岐は
無改造（直接呼び出しのテストで確認）。渡す doc_type は**引数（folder）**でよい
—— credit_card / transit_ic はどちらも「その他」分岐に落ちるので混載でも同値。

**A12 を 1 つのテストで縛った**: 混載 nimoca で「タブは folder 由来
（`従業員_カード明細`）／ L列は entry 由来（`nimoca`）」という**逆向きの
二重契約**を同一テストで assert する。別々に書くと片方だけ倒れても気づけない。

ハーネスに `writer.captured_tabs` を追加（`_get_or_create_tab` の引数を積む）。
呼出**回数**は契約ではない（書込復旧の経路からも呼ばれる）ので `set()` で見る。

**DoD 判定**: RED 2 件 → 実装 → 緑。golden で既存 4 doc_type の L列が `""` のまま ✓

---

### T6-8. `未払金` の `CREDIT_ONLY_ACCOUNTS` 豁免（完了 2026-08-18）

置換条件に `and debit_account != exempt_credit_only` を足しただけ。豁免語は
`DOC_TYPE_CONFIG.get(doc_type, {}).get("default_credit", "")`、既存 doc_type では
`""` になるのでどの科目とも一致せず兜底は不変。

**直書きしないことをテストで縛った**: `DOC_TYPE_CONFIG` の `default_credit` を
`現金` へ差し替えると豁免対象が入れ替わること。`未払金` を直書きした実装だと
差し替えても `未払金` が生き残り、そこで落ちる。

**豁免は 1 語だけ**であることも固定（現金 / 普通預金 / 買掛金 / 預り金 は
`line_mode` でも `未確定勘定` へ置換）—— 兜底そのものを無効化していないこと。

**Plan からの逸脱（軽微）**: `DOC_TYPE_CONFIG` を関数内ローカル import では
なく module レベル（`sheets_output.py:8`）に置いた。同じ行が既に
`DocType` / `LINE_MODE_DOC_TYPES` を引いており、関数内 import の行は
`start_new_file` と 2 箇所あって置換先が曖昧だったため。`doc_types` は軽量
（`test_dependency_weight` の重依存に触れない側）なので起動コストの問題はない。

**DoD 判定**: RED 4 件 → 実装 → 緑。全量 **968 tests 緑** ✓

---

### T6-9. 異常検知の行級化と抑制表（完了 2026-08-18）

2 層に分けて入れた。

**検知器側（`anomaly_detector.py`）**: シグネチャは無改造。冒頭に集合 2 つを
足し、`parent_data.get("doc_type")` で引くだけの純加算。表に載らない既存 4
doc_type と `doc_type` キーを持たない旧経路（`None`）は 1 つも成立しない。

**出力側（`sheets_output.append_entries`）**: parent を行級にする。

```python
row_level = ({"date": row_date, "vendor": row_vendor} if line_mode else {})
actual_parent = {**entries_data, "invoice_num": debit_invoice, **row_level}
```

`doc_type` キーは `entries_data` 由来のまま引き継ぐ（＝ actual doc_type）。
**M5 の変異検証がこれを守っている** —— `"doc_type": doc_type`（folder 側）を
足すと混載テストが即座に落ちる。

**テストの置き場所**: 検知器側の単体テストは新ファイル
`test_anomaly_detector_line_mode.py`（12 件・venv 非依存）へ置いた。
`test_anomaly_detector.py` を**無修正のまま**にして「既存 4 doc_type の異常検知が
1 件も変わらない」ことをその無改造＋全緑で示すため（DoD の文言そのもの）。
出力層の行級 parent は `test_sheets_output_line_mode.RowLevelAnomalyTest`（8 件）。
そこでは `detect_anomalies` を**実物のまま呼びつつ**引数と戻り値を積む spy を
使う —— 戻り値を mock で差し替えると「行級 parent を渡している」ことしか
測れず、抑制表が実際に効いているかが見えない。

**旧記述の訂正 2 件**（§3.2 の副作用）: `result["doc_type"]` を「現在この键の
読み手は居らず」「無消費の键」と書いていた `ocr_engine._build_doc_result` の
docstring と `test_ocr_engine_mixed_folder` のコメントを、消費者ができた事実に
合わせて書き換えた。

**DoD 判定**: 全量 **988 tests 緑** ／ `test_anomaly_detector.py` 無修正 ✓

---

### T6-10. 番人の更新（完了 2026-08-18）

**手順どおりに進めたら番人が緑のままだった** —— そこで番人自体の欠陥が出た。

`UnwiredItemsTest` は `"from config import X"` と `"config.X"` の**部分文字列**を
探していた。T6-5 の結線は

```python
from config import (ACCOUNT_MAP, UNKNOWN_ACCOUNT,
                    CREDIT_SUB_ACCOUNT_RECEIPT, CREDIT_ONLY_ACCOUNTS,
                    DOC_LOW_CONFIDENCE_THRESHOLD,
                    CC_TAX_TYPE_RENDERING)
```

という**括弧付き複数行 import**（名前が増えたときの最も普通の書き方）なので、
どちらの部分文字列も含まない。番人は結線を見落として緑を出した。
**番人がすり抜ける瞬間は、番人が最も必要な瞬間である。**

判定を `ast` へ差し替えた（`ImportFrom(module="config")` の alias ＋
`config.X` の `Attribute`）。すり抜けの再発は
`test_the_watchdog_sees_a_parenthesized_import` が塞ぐ。

**赤い遷移（DoD の要求物。実際の出力）**:

```
AssertionError: Lists differ: ['CC_TAX_TYPE_RENDERING'] != []
First list contains 1 additional elements.
First extra element 0:
'CC_TAX_TYPE_RENDERING'
- ['CC_TAX_TYPE_RENDERING']
+ [] : 結線された項目がある。この一覧と Plan §9.5 の「読み取り点が未実装」
注記を更新すること: ['CC_TAX_TYPE_RENDERING']
```

そのうえで `UNWIRED` を空にして緑。**空でも番人は死んでいない** ——
`test_the_watchdog_can_actually_see_a_wired_item`（結線済み 2 項が視野に在ること）
と括弧テストが検出能力そのものを測り続ける。

**Plan との差異**: 手順 1 は「この時点ではまだ緑」の想定だったが、T6-5 で既に
結線済みだったので手順 1 で赤になるはずだった。実際は番人の欠陥で緑。
得られた証拠は当初計画より**強い**（番人が噛まない条件そのものを 1 件発見した）。

**DoD 判定**: 赤い出力を上に貼付 ✓ ／ 修正後 `test_credit_card_config` 全緑 ✓
／ `test_dependency_weight` 緑（`ast` は stdlib。番人は import せず read_text のまま）✓

---

### T6-11. 全量回帰と変異検証（完了 2026-08-18）

**全量 989 tests 緑**（着手時 926 → +63）。`test_sheets_output.py` は
`git diff --exit-code` で byte-level 無変更を確認（A2）。

**変異検証 8/8 全殺**。git を使わず原文を書き戻し、SHA256 一致で復元を証明。

| # | 変異 | 失敗 | 代表的に落ちたテスト |
|---|---|---|---|
| M1 | ゲートを常時 `True` | 27 | golden 全般 ／ `AppendEntriesReturnValueTest` |
| M2 | 取引No 書戻しを `+1` 固定 | 1 | `test_two_consecutive_appends_do_not_reuse_numbers` |
| M3 | `start_new_file` のリセット抑止を外す | 2 | `test_start_new_file_does_not_reset_numbering_in_line_mode` |
| M4 | 述語を `return True` に戻す | 6 | `MissingAmountContractTest` 全件 |
| M5 | 抑制キーを引数 `doc_type` に変える | 1 | `test_mixed_folder_suppression_follows_the_actual_doc_type` |
| M6 | `_resolve_invoice_cell` を `""` 固定 | 5 | `InvoiceColumnResolverTest` の二重縛り |
| M7 | L列を `""` 直書きに戻す | 2 | `CreditSubAccountTest` |
| M8 | `missing_vendor` 抑制表を空にする | 3 | `VendorSuppressionTest` ＋ 混載 |

M2 と M5 は**それぞれ 1 件でしか死なない**。この 2 本を消すと変異が生き残る
（＝護欄が 1 枚しかない箇所）。将来テストを整理する人はここを削らないこと。

---

### 進捗スナップショット（2026-08-18 更新）

**T6-0 〜 T6-11 の 12 タスク すべて完了。全量 989 tests 緑。未 commit。**

| ファイル | 状態 |
|---|---|
| `sheets_output.py` | 変更。ゲート / A・B・F・G・H・L・T 列 / 採番 / 行級 parent / 未払金豁免 |
| `anomaly_detector.py` | 変更。抑制表 2 つ（純加算） |
| `doc_types.py` | 変更。`LINE_MODE_DOC_TYPES` を新設 |
| `ocr_engine.py` | 変更。import 1 行 ＋ docstring の訂正 |
| `test_credit_card_config.py` | 変更。番人を AST 判定へ ／ `WATCHED` に `sheets_output.py` ／ `UNWIRED` 空 |
| `test_main_process_file.py` | 変更。手写し述語を共有関数へ |
| `test_ocr_engine_mixed_folder.py` | 変更。コメントの訂正のみ |
| `test_sheets_output_golden.py` | **新規**（11 件）。既存 4 doc_type の 28 列護欄 |
| `test_sheets_output_line_mode.py` | **新規**（60 件）。line_mode の全挙動 |
| `test_anomaly_detector_line_mode.py` | **新規**（12 件）。抑制表 |
| `docs/plans/2026-08-18-t6-line-mode-gate.md` | **新規**（本ファイル） |
| `test_sheets_output.py` / `test_anomaly_detector.py` | **無修正**（A2 / T6-9 DoD の証拠） |

**次の一手**: fatboyslim Phase 3（`/simcodex`）→ 辯論裁決 → 全量再走 →
趙の拍板を待って commit。**`.env` 解禁は T7 が終わるまで不可**（趙裁定 08-17）。

**`test_sheets_output_line_mode.py` に在る道具**（再実装しないこと）:

- 列 index 定数 `COL_*` ／ `_FrozenDatetime` ／ `_FakeWorksheet(existing_txn_nos=())`
- `_capture(doc_type, entries_data, writer=None, ws=None)`
  → `(戻り値, 書かれた行, writer, ws)`。`writer.captured_tabs` にタブ名が積まれる
- `_start_new_file(writer, ws, doc_type, filename)` ／ `_make_writer()`
- `_card_entry(amount, date, merchant, memo, **extra)` — `_base_entry` と同じキー形
- `_card_payload(entries, doc_type=CREDIT_CARD)` — doc 級 date=None / vendor="" 
- `_placeholder_entry(...)` — `card_entries._placeholder` を実物のまま呼ぶ
- `_verdict(deduction_class)` — 実型 `DeductionVerdict` を組む
- `_anomaly_calls(doc_type, payload)` — `detect_anomalies` を実物で通しつつ
  引数 parent と戻り値の type 集合を積む spy

**踏んだ罠 3 つ**:

1. `_write_with_retry` を patch して行を捕捉するとき、fake ws にも反映しないと
   `_get_next_txn_no` が A列を実測できず**実装ではなくハーネスの都合で**落ちる
2. `patch.dict(config.INVOICE_COL_RENDERING, ...)` は届かない。
   `invoice_classification` が import 時に `dict(...)` でコピーを作る
3. `anomaly_detector._is_valid_t_number` は**形式だけ**（T + 13 桁）。
   チェックディジット検算は `sheets_output._sanitize_invoice_num` 側にある

---

## 12. 実施後評審の記録（`/simcodex` 2 ラウンド。2026-08-18）

### Round 1 — 4 レンズ ＋ codex

| レンズ | P0 | P1 | P2 |
|---|---|---|---|
| Reuse | 0 | 2 | 1 |
| Simplification | 0 | 1 | 3 |
| Efficiency | 0 | 0 | 2 |
| Altitude | 0 | 0 | 4 |
| `codex review --uncommitted` | **0** | **0** | 0 |

codex の所見: 「No actionable correctness issues were found in the changed files」。
ただし codex 側は read-only sandbox で書込可能な temp が無く `test_ocr_engine_mixed_folder`
を走らせられなかったと明記している。こちらでは走っている（全量緑）。

#### 採用 3 件

1. **テスト二重体の共有**（Reuse P1）—— `_make_writer` / `_FakeWorksheet` を
   `test_sheets_output.py` から借りる（line_mode 側は派生クラスで 2 機能だけ追加）。
   決め手は**既存コード自身の証言**: `test_sheets_output._make_writer` の docstring が
   「3 本の手抄ファクトリがそれぞれ違う部分集合しか設定せず非同期化していた反省」を
   記録している。当方はそこへ 4 本目と 5 本目を書いていた。
2. **legacy payload のヘルパ化**（Simplification P1）—— `_legacy_entry` /
   `_legacy_payload` を新設し 10 箇所の手写しを置換。
3. **抑制台帳の番人**（Reuse P1-a の駁回に対する緩和。下記）。

#### 駁回 1 件 —— `_INVOICE_OPTIONAL_DOC_TYPES` を `LINE_MODE_DOC_TYPES` の別名にする案

**駁回**。「逐行記帳である」と「券面に取引先／登録番号が無い」は**別の軸**であり、
`_VENDOR_OPTIONAL_DOC_TYPES`（transit_ic のみ）が両者の不一致を実証している。
別名で結ぶと、加盟店の T番号を持つ逐行 doc_type が将来現れたとき、その doc_type の
インボイス検査が**無音で抑制される** —— 余分な黄タグ（うるさいが目に見える）より
遥かに悪い。Altitude レンズも独立に同じ結論に達し、「aliasing would have wrongly
coupled two independent axes」と明示した。

ただし提案が指摘した危険（3 つ目の逐行 doc_type で抑制表が追随しない）は実在するので、
**`SuppressionLedgerTest`** を新設して塞いだ。`LINE_MODE_DOC_TYPES` が増減すると
台帳へ True/False を明記するまで赤になる。CLAUDE.md が `ENTRY_BUILDERS` /
`RECON_POLICY` について記録している「登録漏れが無症状で効かなくなる」失敗様式と同じ様式。
**番人が噛むことを変異で実測済み**（架空の 3 つ目を足す → 赤 → SHA256 復元一致）。

### Round 2 — 4 レンズ（角度を入れ替え）＋ Round 1 の修正を検査

Round 1 の修正が新しい問題を作っていないかを見るため、レンズを
Reuse / Simplification / **テスト有効性** / **註解の事実性** に組み替えた。

| レンズ | 結果 |
|---|---|
| Reuse | クリーン（P2 informational 1 件のみ） |
| Simplification | **P1 × 1**（採用） |
| テスト有効性 | P0/P1 なし。**4 変異を実注入**して護欄が噛むことを確認（全ファイル SHA 復元） |
| 註解の事実性 | **P1 × 2 ＋ P2 × 2**（P1 2 件と P2 1 件を採用） |

#### 採用 4 件

1. **隠れ既定値が斷言へ漏れていた**（Simplification P1）——
   `test_legacy_parent_stays_document_level` の期待値 `2026/08/01` が
   `_legacy_payload` の既定値由来で、テスト本体から見えなかった。明示した。
   これは Round 1 の折衷欄で当方が「代価」として自認していた副作用の実例。
2. **`ocr_engine.py:1809` の列一覧が不完全**（註解 P1）—— 「A/B/F/T/H 列」と
   書いてあるが T6 は **G列と L列**も切り替える。`A/B/F/G/H/L/T` へ訂正。
3. **「`_determine_credit_sub_account` は死コード」が自己矛盾**（註解 P1）——
   同じクラスの 2 つ下のテストが「呼ばれてカード名を返す」ことを証明している。
   将来「本当に死んでいるコード」を探す人が削除しかねない。「T6-7 まで死コード
   だった」「既存 doc_type 経路では引き続き呼ばれない」へ訂正。
4. **golden 側の同じ言い回し**（註解 P2）—— 同一の誤りなので併せて訂正。
   片方だけ直すと残った方がより紛らわしい。

#### 駁回 2 件

| 指摘 | 級別 | 駁回理由 |
|---|---|---|
| `_capture` が golden と line_mode で二重定義 | P2 | 両者は能力が違う（golden は単発、line_mode は跨 append の続きと `captured_tabs` を持つ）。統合は和集合版を作ることになり可読性が下がる。指摘者自身も「arguable」と留保 |
| `UNWIRED = ()` になり当該斷言が恒真 | P2 | 検出能力は別の 2 本が維持しており、Round 2 の変異検証（config を de-wire する変異）で**実際に噛むことを確認済み**。附録 B に P2 として記録済み |

### P2 の裁定（2026-08-18。趙の指示で codex に対抗検証させた）

当方の裁定「3 件とも修正不要」を codex へ回した結果、**1 件が覆った**。

| 項目 | 当方の裁定 | codex | 結果 |
|---|---|---|---|
| A. `anomaly_detector` を脱 venv 番人へ登録 | 不要 | **反対** | **codex 勝・採用** |
| B. `UNWIRED = ()` で恒真になった斷言 | 不要 | 同意 | 維持 |
| C-1. `if line_mode:` 4 分岐の統合 | 不要 | 同意 | 維持 |
| C-2. 行級値選択の純関数化 | 不要 | 同意 | 維持 |
| C-3. 関数内 import の巻き上げ | 不要 | 同意 | 維持 |

**A で負けた理由（当方の事実誤認）**: 「_MUST_STAY_LIGHT は母 Plan §4 由来の
名簿で `anomaly_detector` はその設計に含まれない＝誰も決めていない契約の新設」と
裁定したが、`test_anomaly_detector.py` の docstring が**着手前から**
「依存ゼロ（config / receipt_aggregation のみ）。系統 python3 で実行可」と
契約を宣言していた。登録は新設ではなく、散文の契約を機械執行へ引き上げる作業。
T6 で `anomaly_detector` に `from doc_types import DocType` を足した以上、
執行可能にしておく理由も増えている。

`anomaly_detector` / `test_anomaly_detector` / `test_anomaly_detector_line_mode`
の 3 つを登録した。**番人が噛むことを変異で実測**（`anomaly_detector` へ
`import gspread` を混入 → 3 件失敗・到達経路つきで報告 → SHA256 復元一致）。

**C-3 に codex が足した論拠**（当方が挙げていなかったもの）: 
`CREDIT_SUB_ACCOUNT_RECEIPT` を module scope へ上げると **config 差し替えの
観測タイミングが変わりうる**。関数内 import は毎回 config を引き直すので、
`patch.dict` が効く。巻き上げると効かなくなる経路が生まれる。

### 残る P2 は **趙裁定で「直さない」**（2026-08-18）

`test_anomaly_detector.py:3` の docstring が「依存ゼロ（config /
receipt_aggregation のみ）」と書いているが、T6 で `doc_types` が加わり
括弧内の列挙が不完全になった。

**裁定: 直さない**（趙 2026-08-18）。判断材料として以下を実測して提示した:

| 項目 | 実測値 |
|---|---|
| 改動範囲 | **3 行目 の 1 行のみ**（全 273 行・29 テストメソッド） |
| 失うもの | `git diff --exit-code -- test_anomaly_detector.py` が落ちる ＝ T6-9 DoD の「1 コマンドで示せる対照群」 |
| 代替証拠 | docstring を剝いだ AST が HEAD と一致（＝テスト論理は 1 ノードも動いていない）。実測で確認済み |
| 直さない場合の残留危害 | 「`doc_types` は入っていない」と誤読される可能性のみ。**重依存が紛れ込む危険は無い** —— `test_dependency_weight` が機械執行するようになったため |

要点: あの括弧の列挙は**もはや唯一の真実源ではない**。真実源は
`_MUST_STAY_LIGHT` の登録（P2-A で追加済み）。主張の本体（依存ゼロ・
系統 python3 で実行可）は依然として真。**綺麗な対照群を、より完全な括弧と
引き換えにしない。**

### 完了した P2

- `CLAUDE.md` の位置参照を行番号（`sheets_output.py:84-101`。**HEAD 時点で既に
  誤り**、当時の実体は 120 行）から関数名 `sheets_output._get_next_txn_no` へ変更。
  行番号は改修のたびに過期するので、番号を正しく書き直すのは治標にすぎない
- `anomaly_detector` / `test_anomaly_detector` / `test_anomaly_detector_line_mode`
  を `_MUST_STAY_LIGHT` へ登録（P2-A。codex の反証を採用）

### 最終状態

全量 **992 tests 緑**（着手時 926）。`test_sheets_output.py` / `test_anomaly_detector.py`
とも `git diff --exit-code` で byte-level 無変更。脱 venv の番人 38 件も緑。
