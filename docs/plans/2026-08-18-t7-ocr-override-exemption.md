# T7: `_apply_ocr_overrides` の doc_type 豁免（AD-3）

母 Plan: `docs/plans/2026-08-12-credit-card-doctype.md` §5 T7 / AD-3（正本）
前提 Plan: `docs/plans/2026-08-18-t6-line-mode-gate.md` §3.3（列ごと対照表）

**位置づけ**: `.env` 解禁条件 `T4 ＋ T6 ＋ T7`（趙裁定 2026-08-17）の**最後の 1 つ**。
これが済めば解禁の条件が揃う（解禁そのものは本 Plan の範囲外）。

---

## 1. 目標と非目標

### 目標

1. `credit_card` / `transit_ic` の頁で、PaddleOCR 由来の**日付・T番号の doc 級上書きが
   走らない**ようにする。
2. 既存 4 doc_type（receipt / purchase_invoice / sales_invoice / salary_slip）の
   挙動を **1 バイトも変えない**。
3. 将来 doc_type を足す人に「この券面に OCR 上書きを当ててよいか」を
   **明示的に判断させる**（無音で receipt 型の上書きを継承させない）。

### 非目標（この Plan では**やらない**）

- `_extract_date_from_ocr` / `_extract_invoice_num_from_ocr` の抽出ロジック改良。
  カード券面向けの正規表現を足す誘惑があるが、**行級の日付は Gemini が読む**のが
  設計（AD-3）。抽出器を賢くする方向は AD-3 の裁決と逆行する。
- T8（`detect_deduction_risks` の新設、2 つの抑制表の統合）。
- `.env` への `FOLDER_CREDIT_CARD_ID` 追加。**趙の拍板事項**。
- `test_sheets_output.py` / `test_anomaly_detector.py` への一切の変更
  （T6 が凍結した対照群。趙裁定 2026-08-18 で docstring 1 行の修正すら見送った）。
- CLAUDE.md の「新增文书类型需同步」一覧への追記。T6 も
  `LINE_MODE_DOC_TYPES` / 2 つの抑制表を一覧へ足さず**番人テストで塞ぐ**方式を
  採ったので、それに揃える（P2 として §8 に残す）。

---

## 2. なぜ要るか（事実と証拠）

### 2.1 日付

`ocr_engine.py:590` の `_skip_keywords` に **「カード」「ポイント」** が入っている。

```python
_skip_keywords = ["終了", "有効期限", "まで", "開始", "お知らせ", "変更",
                  "ご利用ください", "ポイント", "カード", "キャンペーン", ...]
```

判定窓は前後 50 文字の対称窓。カード明細は全面が「カード」「ポイント」なので、
**ほぼ全ての日付が skip される**。skip された日付は捨てられず
`fallback_pool_p1` に溜まり、最後に

```python
if fallback_pool_p1:
    m = fallback_pool_p1[-1]      # ← 頁面で最後に読まれた日付
```

が採用される。PaddleOCR の読み順は多欄レイアウトで不定なので、
これは「頁のどこかにあった適当な日付」である。

### 2.2 T番号

`_extract_invoice_num_from_ocr` は `[TＴ][\s\-]*(\d[\s\-]*){13}` で拾う。
カード明細には ETC 番号・カード番号・会員番号など長い数字列が並ぶ。
加えて F-11 のとおり**券面の登録番号はカード会社自身のもの**であり、
F-14 によりカード明細は適格請求書に該当しない。行ごとの加盟店登録番号は
**構造上存在しない**。

### 2.3 憑空でキーを作る

カード系プロンプト（`card_prompts.py`）の出力 JSON は `card` / top / `rows` だけで、
**doc 級の `date` / `invoice_num` は存在しない**。しかし `_apply_ocr_overrides` の
else 分岐（`ocr_engine.py:1114-1130`）は

```python
raw_data["date"] = ocr_date            # または validated
raw_data["invoice_num"] = ocr_tnum
```

と**キーの有無を問わず代入する**。よってプロンプト側の改名では防げない（AD-3）。

### 2.4 T6 との継目 —— どこから漏れるか

T6 §3.3 の列対照表で追うと、現状の被害面は次のとおり。

| 列 | T6 後の経路 | doc 級汚染は届くか | 根拠 |
|---|---|---|---|
| **B 取引日** | `entry["date"] or entries_data["date"]` | **届く**（行の日付が空のとき） | `sheets_output.py:360` |
| H 借方インボイス | `_resolve_invoice_cell(entry)` | 届かない（T6 が塞いだ） | `sheets_output.py:383` |
| 異常検知 parent | `{**entries_data, "invoice_num": debit_invoice, "date": row_date, ...}` | 届かない（行級で上書き済み） | `sheets_output.py:441` |
| 認識不能行（占位） | `entries_data.get("date","") or ""` | **届く** | `sheets_output.py:901` |

**つまり T7 が塞ぐのは主に B 列と占位行の日付**である。T番号側は T6 が既に
別経路で塞いでいるが、`result["invoice_num"]` に汚染値が載ったままなのは
「消費者が今たまたま読んでいない」だけの半開状態で、AD-3 の裁決どおり
**producer 側で断つ**のが正しい。

**実害の具体形（nimoca）**: `card_prompts.TRANSIT_IC_PROMPT` は
「年は券面に印字されていません。date は必ず null」と指示しており、
行の日付は `card_entries._ic_date` が `month_day` から復元する。
`month_day` が読めなかった行は `date=""` になり、B 列は doc 級へ回帰する。
そこに「頁面で最後に読まれた日付」が入ると、**赤タグの付く空欄**が
**無音で誤った日付**に化ける。本プロジェクトが一貫して避けてきた
「静かに間違ったデータ」そのもの。

---

## 3. 設計

### 3.1 豁免集合

`ocr_engine.py` にモジュール定数を置く。

```python
# OCR 主導の日付・T番号上書きを**当ててはいけない** doc_type（AD-3）。
_OCR_OVERRIDE_EXEMPT_DOC_TYPES = frozenset({
    DocType.CREDIT_CARD,
    DocType.TRANSIT_IC,
})
```

**`LINE_MODE_DOC_TYPES` の別名にしない**。T6 の実施後評審で同型の提案
（抑制表を `LINE_MODE_DOC_TYPES` の別名にする）が出て**駁回されている**のと
同じ理由: 「逐行記帳である」と「券面の日付/登録番号が doc 級に存在しない」は
**別の軸**である。行級記帳しないが券面に doc 級 T番号を持たない書類（例:
将来の口座振替通知）も、逐行記帳するが正しい doc 級発行日を持つ書類も、
どちらも原理的にありうる。別名で結ぶと片方が無音で崩れる。

置き場所は `ocr_engine.py`。消費者がこのモジュールだけであり、
`doc_types.py` へ出すと「producer と consumer が別モジュール」という
`LINE_MODE_DOC_TYPES` の移設理由（T6）が成立しない。

### 3.2 ゲートの位置 —— **関数の内側**

```python
def _apply_ocr_overrides(doc_type, raw_data, ocr_text, prefix=""):
    if not raw_data:
        return
    if doc_type in _OCR_OVERRIDE_EXEMPT_DOC_TYPES:
        return
    ...
```

**`not raw_data` を先に置く**（Codex R1③ を採納）。豁免ゲートを先頭に置くと、
豁免 doc_type のときだけ **truthy な非 dict** が早期 return で素通りする。
現状の非豁免経路はそこで `AttributeError` を出す（IP-401 が記録した事故そのもの）。
実経路では `_yield_page_results` の型ゲート（`ocr_engine.py:2208` 付近）が手前で
捕まえるので実害は無いが、**豁免経路だけ型契約の扱いが違う**という非対称を作る
理由が無い。順序を入れ替えるコストはゼロ。

> Codex の後半提案「truthy 非 dict を明示的に `TypeError` にする」は**駁回**。
> 現行の非 dict 挙動を変えるのは IP-401 の型ゲート設計への改修であり、T7 の範囲外。
> 順序の入れ替えだけ採る。

**呼出側ではなく関数の内側に置く理由**: IP-401 が確立した
「順序ではなく制御フローの形で不可能にする」の適用。呼出側ゲートは
将来 2 つ目の呼出点が生えたときに**書き忘れが無音で通る**。
現在の呼出点は 1 箇所（`ocr_engine.py:2229`、`_yield_page_results` 内）だが、
このファイルは過去に「逐頁ループと尾段の逐字コピー」で同型の漂移事故を
起こしており（`_yield_page_results` の docstring が記録）、
呼出側ゲートはその失敗様式に真正面から乗る。

### 3.3 署名 —— `doc_type` は**必須の第 1 引数**

```python
_apply_ocr_overrides(doc_type, raw_data, ocr_text, prefix="")
```

**既定値を付けない**。`doc_type=None` のような既定値は
「書き忘れた新しい呼出側が黙って上書き経路へ落ちる」footgun であり、
3.2 でゲートを内側へ寄せた意味を打ち消す。

第 1 引数に置くのは同ファイルの既存慣例に合わせるため
（`_yield_page_results(doc_type, raw_data, ...)` /
`_build_doc_result(doc_type, raw_data, entries)`）。

呼出側の改修は 1 行（`ocr_engine.py:2229`）。

### 3.4 どの doc_type を渡すか —— **actual**

`_yield_page_results` が受け取る `doc_type` は **`PageOcr.actual_doc_type`**
（T3 以降。混載フォルダではフォルダ宣言と異なる）。上書きの可否は
**その頁の券面がどういう書類か**で決まるので actual が正しい。
T6 §3.2 が抑制表について下したのと同じ裁決。

`_yield_page_results` はそもそも actual しか受け取らないので、
本関数の中で渡し間違えようがない。

**pipeline 側の射程**（Codex R1④）: `_yield_page_results` の呼出は実コード上
**2 箇所**（`ocr_engine.py:2577` 逐頁ループ / `ocr_engine.py:2719` 尾段）で、
どちらも `page_ocr.actual_doc_type` を渡している（実測確認済み）。
「pipeline が actual を渡し続ける」ことは既存の
`test_ocr_engine_mixed_folder.PromptAndBuilderSwitchPerPageTest.test_builder_doc_type_follows_the_prompt`
が固定しており、T7 で spy テストを重ねても同じ事実を二度見るだけなので**足さない**。
本 Plan が新たに証明するのは「`_yield_page_results` の中で
`_apply_ocr_overrides` へその doc_type が渡っている」一段だけ（T7-3 の 1）。

### 3.5 豁免後に `result["date"]` が `None` になる件

豁免すると `raw_data` に `"date"` キーが生えないので、
`_build_doc_result` の `raw_data.get("date")` は **`None`** を返す
（現状は必ず str）。消費側を全部当たった結果、`None` で壊れる箇所は無い:

| 消費点 | 式 | `None` の扱い |
|---|---|---|
| `sheets_output.py:360` | `entry.get("date") or entries_data.get("date") or ""` | `""` |
| `sheets_output.py:901` | `entries_data.get("date", "") or ""` | `""` |
| `ocr_engine._yield_line_mode_results` | `_blank_result(date=result.get("date", ""))` | `None` を素通し → 上の 2 点で `""` |
| 異常検知 parent | line_mode は `{"date": row_date}` で上書き（`sheets_output.py:441`） | 到達しない |
| `main.process_file` | `result["date"]` を読まない（grep 0 件） | — |

とはいえ「壊れないことを今調べた」だけでは番人にならないので、
**`None` が B 列で空文字になることをテストで固定する**（§5 の T7-3）。

> 代案「豁免時に `raw_data.setdefault("date", "")` で型を揃える」は**採らない**。
> 存在しないキーを作るのは AD-3 が断とうとしている行為そのもの。
> 型の揺れは消費側 3 箇所が既に `or ""` で吸っており、そこをテストで固定すれば足りる。

---

## 4. 影響面

| ファイル | 変更 | リスク |
|---|---|---|
| `ocr_engine.py` | 定数 1 つ追加、`_apply_ocr_overrides` の署名＋先頭 2 行、呼出 1 行 | **低**。呼出点は 1 箇所、テスト直呼びは 0 件 |
| `test_ocr_engine_ocr_override_exempt.py`（新規） | 豁免・非豁免・結線・署名・台帳 | — |
| `test_sheets_output_line_mode.py` | B 列が doc 級汚染を受けないことの結線テストを 1 件追加 | **低**（凍結対象外） |
| `test_sheets_output.py` / `test_anomaly_detector.py` | **触らない** | 対照群 |

既存 4 doc_type への影響: **無い**。`_OCR_OVERRIDE_EXEMPT_DOC_TYPES` に
入っていない doc_type は `if` を素通りして従来と逐字同一の経路へ入る。

> **訂正（Codex R1①）**: 当初この Plan は「`test_sheets_output_golden.py` の
> 28 列 golden が機械で証明する」と書いていたが**過大主張**だった。golden は
> `append_entries`（consumer 側）の出力 snapshot であり、`_apply_ocr_overrides`
> を含む producer 側の経路を**一切通らない**（`test_sheets_output_golden.py`
> 冒頭の docstring と `_capture` が示すとおり、入力は組み立て済みの
> `entries_data` である）。よって「豁免が効きすぎて sales_invoice / salary_slip の
> 上書きが止まる」変異は golden では**殺せない**。
>
> 証明は **T7-1 の非豁免対照**が担う。そのため対照を `purchase_invoice` と
> `receipt` の 2 件から **`DocType.ALL － 豁免集合` の全件**へ広げる（下の T7-1）。
> golden の役割は「consumer 側の 28 列が動いていない」ことに限定される。

---

## 5. タスク清単（各項に DoD）

### T7-0. 基線の記録

`venv311/bin/python -m unittest discover -p "test_*.py"` → **992 / OK**。
`git diff --exit-code test_sheets_output.py test_anomaly_detector.py` → 0。

**DoD**: 両方とも上記のとおり。（**実施済み**: 992 OK を確認）

### T7-1. RED —— **結線層**で赤を見る（Codex R2 で順序を訂正）

> **順序の訂正（Codex R2・MEDIUM を採納）**: 当初 Plan は「単体テストを
> `_apply_ocr_overrides(doc_type, ...)` の**新**署名で先に書いて赤を見る」と
> していたが**成立しない**。現行署名は 3 引数（`raw_data, ocr_text, prefix`）
> なので、新署名で書いた瞬間に豁免ケースも非豁免対照も**まとめて `TypeError`
> で赤**になる。「1・2 が赤、3・4 は既存挙動なので緑」という DoD が測れない。
>
> よって RED は**署名に依存しない結線層**（`_yield_page_results` 経由）で取る。
> 新署名に依存する単体テストは GREEN の後（T7-3）へ回す。

新規 `test_ocr_engine_ocr_override_exempt.py` に、`_yield_page_results` 経由で:

1. `_yield_page_results(DocType.CREDIT_CARD, raw_data, ocr_text, ...)` —— raw_data は
   カード券面どおり `date` / `invoice_num` キー**無し**、ocr_text には確実に拾われる
   日付（`2026年3月31日`）と T番号（`T8010401088436`）を入れる。
   → result の `date` が falsy・`invoice_num` が `""` であること。
2. `transit_ic` で同上。
3. **非豁免の対照**: 同じ ocr_text で `DocType.PURCHASE_INVOICE` を通すと
   result の `date` が OCR 由来の値になること（ゲートが効きすぎていない証明）。

**DoD**: 1・2 が**赤**（現状は OCR 値が載る）、3 が**緑**（既存挙動）。
赤の理由が `TypeError` ではなく「OCR 値が載っている」ことを出力で確認する
—— これが「テストが正しい理由で赤い」ことの確認であり、Codex R2 の指摘の核心。

### T7-2. GREEN —— 定数とゲートと署名

§3.1〜§3.3 を実装。呼出側 1 行（`ocr_engine.py:2229`）を修正。

**DoD**: T7-1 の 3 件が緑。全量 992 が緑のまま。

### T7-3. 単体層と全件対照（GREEN 後）

新署名 `_apply_ocr_overrides(doc_type, raw_data, ocr_text, prefix="")` を直接叩く:

1. 豁免 2 件（`credit_card` / `transit_ic`）で **`date` / `invoice_num` キーが
   生えない**こと。結線層（T7-1）は「result に載らない」ことしか見ておらず、
   「raw_data を汚さない」ことは単体でしか測れない。
2. **非豁免の全件対照**（Codex R1① で拡張）: `DocType.ALL － 豁免集合` の
   **全 doc_type** について `subTest` で回し、従来どおり `date` と `invoice_num` が
   OCR 値で上書きされること。列挙式に 2 件だけ書くと、豁免集合へ既存 doc_type が
   混入する変異（M3）を receipt / purchase_invoice 以外では殺せない。
   `receipt` は `documents` 形式なので raw_data の形を分岐させる。

**DoD**: 2 件とも緑。`DocType.ALL` に doc_type が増えたら 2 が自動で追随する
（列挙式にしない）。

### T7-4. 統合層 —— 「無音の誤り」から「可視の空欄」への転換を固定

`test_sheets_output_line_mode.py` に 1 件追加。豁免後の result 形状
（`date=None`）を `append_entries` に食わせ、行の日付が空の entry について
**B 列が `""`** であり、かつ **`missing_date` の異常フラグが立つ**こと。

> **後半が本質**（Codex R1② を採納）。T7 の価値は「空欄になる」ことではなく
> 「**人手確認へ回る**」ことである。豁免前は doc 級の汚染日付が B 列に入り、
> `anomaly_detector` の `missing_date`（`parent_data["date"]` の truthy 判定。
> `anomaly_detector.py:106` 付近）が**立たない** —— 誤った日付が赤タグ無しで
> 帳簿に載る。豁免後は空欄になり赤（severity=high, col=1）が立って人が見る。
> B 列 `""` だけを断言すると、この「無音 → 可視」の転換を測っていない。

**DoD**: 緑。`missing_date` の断言を外すと M1（ゲート削除）が統合層で殺せなく
なることを確認する。

### T7-5. 番人 —— 台帳と署名

1. **台帳テスト**: `DocType.ALL` の全 doc_type について
   「OCR 上書きを豁免するか」を明記させる `LEDGER` を持ち、
   `set(LEDGER) == set(DocType.ALL)` と
   `(doc_type in _OCR_OVERRIDE_EXEMPT_DOC_TYPES) == LEDGER[doc_type]["exempt"]`
   を検査。値は裸の bool ではなく **`{"exempt": bool}`**（Codex R1⑤）——
   台帳を読む人が True/False をどちら向きに読むかで迷わないようにする。
   各行に**なぜそう判断したか**を 1 行コメントで書かせる。
   `SuppressionLedgerTest`（`test_anomaly_detector_line_mode.py:141`）の写し。
   新しい doc_type を足す人は、ここへ判断を書くまで緑にならない。
2. **署名テスト**: `inspect.signature(_apply_ocr_overrides)` の `doc_type` が
   第 1 引数で `default is inspect.Parameter.empty` であること。
   §3.3 の footgun 防止を機械で固定する。

**DoD**: 2 件緑。`LEDGER` から 1 件消すと赤、`doc_type` に既定値を付けると赤。

### T7-6. 変異検証

最低 5 変異を注入し、**全て殺されること**を確認する:

| # | 変異 | 殺すはずのテスト |
|---|---|---|
| M1 | 豁免ゲートを削除 | T7-1（1・2）／T7-3（1）／T7-4 |
| M2 | 豁免集合から `TRANSIT_IC` を外す | T7-1（2）／T7-3（1）／T7-5（台帳） |
| M3 | 豁免集合に `RECEIPT` を足す | T7-3（2 の全件対照）／T7-5（台帳） |
| M4 | `doc_type` に既定値 `None` を付ける | T7-5（署名） |
| M5 | 呼出側で actual ではなく定数 `DocType.RECEIPT` を渡す | T7-1（1・2） |

**DoD**: 5 変異とも赤になり、戻すと緑。

> **実施時に 1 件追加**: 実施後評審 P1-1 を受けて **M6（`_extract_invoice_num_from_ocr`
> を常に `None` にする＝前提の劣化）** を足した。最終形は 6 変異。記録は §11。

### T7-7. 全量回帰と対照群の証明

**DoD**:
- `venv311/bin/python -m unittest discover -p "test_*.py"` → OK（992 ＋ 新規）
- `git diff --exit-code test_sheets_output.py test_anomaly_detector.py` → 差分 0
- `test_sheets_output_golden.py` 単体で緑

---

## 6. 受入基準（機械判定）

```bash
venv311/bin/python -m unittest discover -p "test_*.py"          # OK
venv311/bin/python -m unittest test_sheets_output_golden -v     # OK
git diff --exit-code test_sheets_output.py test_anomaly_detector.py   # exit 0
```

加えて、母 Plan §5 T7 の DoD を逐語で満たすこと:
**「新 doc_type で日付・T番号が上書きされない。既存 doc_type は不変。」**

---

## 7. テスト戦略

TDD（RED → GREEN → 変異）。層は 4 つ:

| 層 | 何を証明するか | ファイル |
|---|---|---|
| 結線（**RED はここ**） | 呼出側が doc_type を `_apply_ocr_overrides` へ渡している | `test_ocr_engine_ocr_override_exempt.py`（`_yield_page_results` 経由） |
| 単体 | 豁免集合の doc_type で raw_data が汚れない／非豁免 **全件**で従来どおり上書きされる | 同上 |
| 統合 | 豁免後の `date=None` が B 列 `""` ＋ `missing_date` 赤タグになる | `test_sheets_output_line_mode.py` |
| 番人 | doc_type 追加時に判断を強制／署名の footgun 封じ | `test_ocr_engine_ocr_override_exempt.py` |

E2E（実票）は本番 Drive が要るので範囲外。`.env` 解禁後の真票回帰で担保する
（母 Plan §9 の運用手順）。

---

## 8. リスクと回退

| リスク | 影響 | 対処 |
|---|---|---|
| 豁免が効きすぎて既存 4 doc_type の日付上書きが止まる | 客の帳簿の日付が Gemini 幻覚に戻る（**重大**） | golden 28 列＋非豁免対照テスト（T7-1 の 3・4）で機械判定 |
| `result["date"]` が `None` になり未知の消費者で落ちる | 頁が無音で消える | 消費点を全 grep 済（§3.5）＋ T7-3 の 3 で固定 |
| 将来 doc_type を足す人が豁免判断を忘れる | 新券種が receipt 型の上書きを継承 | T7-4 の台帳テストが赤で止める |
| 呼出点が増えて doc_type を渡し忘れる | — | 必須引数なので `TypeError` で即死（無音にならない） |

**回退**: 変更は `ocr_engine.py` の 3 箇所のみ。`git revert` 1 発で戻る。
本番影響なし（`.env` に folder ID が無く新 doc_type は到達不能）。

**P2（本 Plan では手を付けない）**: CLAUDE.md の「新增文书类型需同步」一覧に
`_OCR_OVERRIDE_EXEMPT_DOC_TYPES` を足すか否か。T6 の先例（番人テストで代替）に
揃えて見送るが、一覧に載る 6 表と番人だけの表が混在する状態は将来の混乱源。

---

## 9. 附録 A: Codex 評審の辯論記録（2026-08-18）

評審者: `codex exec`（codex-cli 0.147.0）。**実ソースを読ませた**上での対抗評審。

### Round 1 —— 6 件の指摘

| # | 厳重度 | 指摘 | 裁決 |
|---|---|---|---|
| ① | HIGH | `test_sheets_output_golden.py` が `_apply_ocr_overrides` の不変性を証明する、は**過大主張**。golden は `append_entries` の consumer 側 snapshot で producer 経路を通らない | **採納** |
| ② | HIGH | 統合テストは B 列 `""` だけでなく **`missing_date` 赤タグが立つ**ことも固定すべき | **採納** |
| ③ | MEDIUM | 豁免ゲートを `not raw_data` より前に置くと、豁免 doc_type だけ truthy 非 dict を素通しする | **前半採納・後半駁回** |
| ④ | MEDIUM | 「actual を渡している」証明の射程が曖昧。pipeline 側の保証は別 | **修改採納** |
| ⑤ | LOW | 台帳の値を `{"exempt": bool}` にして読み違いを減らせ | **採納** |
| ⑥ | LOW | 変異 5 件は重い。2 件に絞れ | **駁回** |

**①の詳細**: 事実確認したところ指摘どおり。`test_sheets_output_golden._capture` は
組み立て済みの `entries_data` を `append_entries` に食わせるだけで、
`_apply_ocr_overrides` は 1 行も走らない。「豁免が効きすぎて sales_invoice /
salary_slip の上書きが止まる」変異を golden では殺せない。
→ §4 の主張を訂正し、非豁免対照を `DocType.ALL － 豁免集合` の**全件**へ拡張（T7-3）。

**②の詳細**: 採納。T7 の価値は「空欄になる」ではなく「**人手確認へ回る**」。
豁免前は汚染日付が入って `missing_date` が**立たない**（無音の誤り）。
豁免後は空欄＋赤タグ（可視）。この転換こそが測るべき差分。→ T7-4。

**③の裁決**: 前半（順序の入れ替え）は採納 —— コストゼロで非対称が消える。
後半（明示 `TypeError`）は**駁回**: 現行の非 dict 挙動は IP-401 の型ゲートが
手前で捕まえる前提の設計で、そこへ手を入れるのは T7 の範囲外。

**④の裁決**: 修改採納。実測で `_yield_page_results` の呼出は 2 箇所
（`ocr_engine.py:2577` / `2719`）、どちらも `page_ocr.actual_doc_type`。
pipeline 側の保証は既存 `test_ocr_engine_mixed_folder.PromptAndBuilderSwitchPerPageTest`
に依存する旨を §3.4 へ明記。spy テストは同じ事実の二度見なので**足さない**。

**⑥の駁回理由**: (a) 変異注入は T7 実施時の 1 回きりで CI 常設ではない。
(b) 直前タスク T6 は 8 変異＋評審で 6 変異を実測しており、同一案件で水準を下げない。
(c)「通常テストで自然に殺せる」は**仮説**であり、それを実測するのが変異検証。
特に M5 は Codex 自身の指摘④が「射程が微妙」と言った箇所で、推測で省くのは逆方向。

### Round 2 —— 駁回 2 件の複審 ＋ 新規指摘

**③後半・⑥ともに Codex が「取り下げ」**（我方の維持が通った）。

新規 1 件:

| # | 厳重度 | 指摘 | 裁決 |
|---|---|---|---|
| ⑦ | MEDIUM | T7-1 の RED DoD が**現行署名と矛盾**。新署名で単体テストを書くと豁免ケースも非豁免対照も `TypeError` でまとめて赤になり、「1・2 赤／3・4 緑」が測れない | **採納** |

**⑦の詳細**: 完全に成立する見落とし。`doc_type` を必須第 1 引数にする設計
（§3.3）と「単体テストを先に書いて赤を見る」TDD 手順が**両立しない**。
→ RED は**署名に依存しない結線層**（`_yield_page_results` 経由）で取り、
新署名に依存する単体テストは GREEN 後（T7-3）へ回すようタスク順序を組み替えた。
これにより RED が「正しい理由で赤い」（`TypeError` ではなく「OCR 値が載っている」）
ことを確認できる。

§3.2 / §3.4 / §4 / §5 T7-3 / T7-4 については **新規指摘なし**。

## 10. 実施記録（2026-08-18）

### 基線

`venv311/bin/python -m unittest discover -p "test_*.py"` → **992 / OK**。
`git diff --exit-code test_sheets_output.py test_anomaly_detector.py` → 差分 0。

### 実装（`ocr_engine.py` の 3 箇所だけ）

1. `_OCR_OVERRIDE_EXEMPT_DOC_TYPES = frozenset({CREDIT_CARD, TRANSIT_IC})` を
   `_apply_ocr_overrides` の直前に追加（理由をコメントで併記。別名化の禁止も）
2. 署名を `_apply_ocr_overrides(doc_type, raw_data, ocr_text, prefix="")` へ変更。
   ゲートは `if not raw_data: return` の**後**（§3.2）
3. 呼出側 1 行（`_yield_page_results` 内）

`sheets_output.py` / `card_entries.py` / `doc_types.py` / `main.py` は**無改造**。

### テスト（新規 13 ＋ 既存ファイルへ 2）

`test_ocr_engine_ocr_override_exempt.py`（新規・13 件）:

| クラス | 何を証明するか |
|---|---|
| `ExtractionProbeTest` | プローブ文字列が本当に抽出される（緑の理由が「抽出できなかった」でないこと） |
| `ExemptDocTypesAreNotOverriddenTest` | **結線層**。`_yield_page_results` 経由で result に OCR 値が載らない |
| `NonExemptDocTypesStillOverriddenTest` | 効きすぎていない対照（purchase_invoice） |
| `RawDataIsNotPollutedTest` | 単体層。raw_data に**キーが生えない**（豁免側は台帳全件も回す） |
| `EveryNonExemptDocTypeIsStillOverriddenTest` | 非豁免を**台帳の全件**で回す（golden では殺せない変異を殺す） |
| `OverrideLedgerTest` | doc_type 追加時に豁免の是非を明記させる番人 |
| `SignatureTest` | `doc_type` の既定値禁止を機械で固定 |

`test_sheets_output_line_mode.py`（+2 件）:
`OcrOverrideExemptionReachesTheSheetTest` —— B列 `""` ＋ `missing_date` 赤タグ。
対照として「doc 級が汚染されているとどうなるか」も同じクラスに置いた
（B列 が埋まり `missing_date` が**立たない**＝無音の誤り）。

**台帳は実装定数から導出していない**。導出すると「豁免集合に既存 doc_type を
混入させる」変異が台帳ごと一緒に動き、番人が自分の見張る対象と同じ嘘をつく。

### RED の確認（Codex R2 の指摘どおり結線層で取った）

```
AssertionError: '2026/03/31' is not false : カード明細の doc 級 date に OCR 値が載った
AssertionError: '2026/03/31' is not false : nimoca の doc 級 date に OCR 値が載った
```

`TypeError` ではなく「OCR 値が載っている」で赤い ＝ 正しい理由で赤い。
同時に非豁免対照（purchase_invoice）とプローブ 2 件は最初から緑。

### 変異検証（T7-6。5 変異とも殺された）

| # | 変異 | 結果 |
|---|---|---|
| M1 | 豁免ゲートを削除 | FAILED（failures=6） |
| M2 | 豁免集合から `TRANSIT_IC` を外す | FAILED（failures=4） |
| M3 | 豁免集合に `RECEIPT` を足す | FAILED（failures=3） |
| M4 | `doc_type` に既定値 `None` を付ける | FAILED（failures=1） |
| M5 | 呼出側で actual ではなく定数 `DocType.RECEIPT` を渡す | FAILED（failures=2） |

復元後 OK。

### 受入基準の実測

- 全量: **Ran 1007 tests / OK**（992 → +15）
- `test_sheets_output_golden` 単体: Ran 11 / OK
- `git diff --exit-code test_sheets_output.py test_anomaly_detector.py` → **差分 0**（対照群を保った）

## 11. 実施後評審の記録（`/simcodex` 3 ラウンド。2026-08-18）

構成: 各ラウンド 4 レンズ（agent 並行）＋ `codex review --uncommitted`。
**`codex review` は 3 ラウンドとも「指摘なし」**。実質的な指摘は全て agent 側から出た。

### Round 1 — 4 レンズ（Reuse / Simplification / Efficiency / Altitude）

| レンズ | 指摘 | 裁決 |
|---|---|---|
| Efficiency | 指摘なし。豁免の早期 return は**逆に速い**（regex 全掃 2 回を丸ごと省く） | — |
| Altitude | 高度は正しい。ゲート位置・署名・定数の置き場所いずれも既存慣例と一致 | — |
| Reuse **P1** | `_flags_for` が同ファイルの `_anomaly_calls`（`test_sheets_output_line_mode.py:958`）を作り直していた | **採納** |
| Reuse P2 ×2 | `_run_page` は同型ヘルパの 5 つ目の複製／台帳骨架が 2 つ目 | **順延**（下記） |
| Simplify **P1** | 列挙式 2 件が参数化 1 件に完全に包含され、変異殺傷に差が無い | **採納** |
| Simplify P2 | 非豁免 2 件の body が逐字コピー | **採納** |
| Simplify P2 | 反事実テストが本改修のコードを呼んでいない | **駁回** |
| Simplify P2 | 排序註釈が自認する重要度に対して長い（5 行） | **修改採納**（3 行へ） |

**Reuse P1 の修正**: spy 本体を `_spying_on_anomalies` contextmanager へ抽出し、
`_anomaly_calls` はその薄い包みにした。既存呼出側は 1 文字も変えていない。

**Simplify P1 の修正**: 列挙式 2 件を削除し、参数化 1 件が**実標本**
（`UC_P2_RAW` / `NIMOCA_P1_RAW`）を引くようにした（`_EXEMPT_RAW_SAMPLES`）。
合成形で済ませると「本物のカード raw_data がこの形をしている」という根拠が消える。

**反事実テストの駁回理由**: 指摘は「`sheets_output` / `anomaly_detector` は
本 diff で変わっていないので、このテストは T7 の変更で回帰しない」。正しいが、
役割が違う —— これは**前提の固定**である。将来 B列 の回帰経路や `missing_date`
の判定が変わると、対になっているテストが「作者が信じている意味」とは別の
ことを測り始める。本ファイルの module docstring が明記している
「両方向を必ず固定する。片方だけ書くとゲートが常時 True になる変異を
検出できない」という既定方針そのもの。

**Reuse P2 の順延理由**: `_run_page` 型ヘルパの共通化は既存 4 ファイル
（`test_ip401_nondict_rawdata` / `test_ocr_engine_line_shortage` /
`test_ocr_engine_social_insurance` / `test_ocr_engine_receipt_pipeline`）を
巻き込む。1 つだけ `ocr_test_helpers` へ移すと 3 つ目の様式が増えて悪化する。
指摘した agent 自身も「この diff の義務ではない」と述べている。**P2 として残す**。

### Round 2 — 4 レンズ（角度を入れ替え: 番人有効性 / 爆発半径 / 註釈正確性 / 慣例適合）

| レンズ | 指摘 | 裁決 |
|---|---|---|
| 註釈正確性 | **全 8 claim が実ソースと一致**（F-11/F-14 の出典、`fallback_pool[-1]`、`card_prompts` の schema、T6 の駁回記録、`AMEX_HEAD` の T番号、`missing_date` の severity/col、両呼出点の actual） | — |
| 爆発半径 | `None` は 3 つの境界点（`sheets_output.py:360` / `:901` / `row_level` マージ）で全て `or` に吸われる。`main.py` は `date`/`invoice_num` を一切読まない。`invoice_num` は `_build_doc_result` の `""` 既定値で元から無害 | — |
| 爆発半径 P1 | `_blank_result(date=result.get("date", ""))` の既定値が死んでいる（键は在るが値が None） | **試行 → 回退**（下記） |
| 慣例適合 | CLAUDE.md の 6 表一覧へ足さないのは T6 先例どおりで正しい。言語様式・命名・ファイル名・`test_dependency_weight`・`UnwiredItemsTest` いずれも適合 | — |
| 慣例適合 P2 | 台帳前の空行が 3 行（慣例は 2 行） | **採納** |
| 番人有効性 **P1-1** | T番号側の断言が**抽出器の成功に暗黙依存**。`invoice_num` は `if ocr_tnum:` に else 節が無く、抽出失敗でもキーが立たない。抽出器退化＋ゲート削除の複合変異で豁免テストが**假綠**になる | **採納** |
| 番人有効性 P1-2 | `SignatureTest` は doc_type を固定した別名ラッパーを塞げない | **修改採納**（射程を docstring へ明記） |
| 番人有効性 **P2-1** | 結線層が 2 件のハードコードで、3 つ目の豁免 doc_type に結線証明が付かない | **採納** |

**P1-1 の修正（本ラウンドで最も価値のある指摘）**: `_ProbeGuardedTest` 基底を
新設し、4 つの豁免テストクラスが継承する。`setUpClass` でプローブの成立
（日付・T番号の両方が期待値どおり抽出される）を検査し、崩れていれば
クラスごと落とす。**変異 M6（`_extract_invoice_num_from_ocr` を常に `None` に
する）で実測**: 修正前は `failures=1`（プローブテストだけ）で豁免テストは
緑のまま。修正後は `failures=1, errors=4` —— 前提が崩れた 4 クラスが
まとめて落ちる。

**P1-2 を「射程の明記」に留めた理由**: 「呼出点は 1 箇所であり続けよ」型の
番人を置くと、将来の**正当な**呼出者まで誤爆する。内側ゲートの設計により
普通の新呼出者は既に安全であり、残るのは「doc_type を固定したラッパーを
故意に作る」という意図的行為だけ。それは署名検査では原理的に塞げない
（まだ存在しないコードだから）ので、評審で捕まえる方針を docstring に書いた。

**P2-1 の修正**: 結線層を `_EXEMPT_DOC_TYPES` のループへ変え、標本が無い
将来の doc_type には `_exempt_raw_for` が最小合成形を渡す。台帳が
「豁免すると書け」と強制し、結線層が「それが実際に効く」を強制する二段になった。

**爆発半径 P1 の試行と回退（重要）**: `result.get("date") or ""` へ直そうとしたら
既存の `test_ocr_engine_line_shortage.ShortageWithEntriesTest.test_notice_carries_the_customer_facing_memo`
が落ちた（`'' != None`）。T5 が「提示行の日付 ≡ 明細 result の日付」という
**同一性**を契約として固定していた。片方だけ `""` へ寄せると契約が割れる。
下流は両方 `or ""` で吸うので見える挙動は同じ。**回退し、理由をコメントに残した**。

### Round 3 — 最終確認 ＋ Round 2 差分の点検

`codex review --uncommitted` → 「No actionable correctness issues were found」。

Round 2 で新設した `_ProbeGuardedTest` 周りはまだどのレンズも見ていない
新規コードなので、定向 agent で別途点検した。機構は実測で確認された
（`_extract_date_from_ocr` を差し替えると 4 クラスが `setUpClass` で ERROR、
`wasSuccessful() == False`。ゼロテストの基底クラスは discovery を汚さない）。

| 指摘 | 裁決 |
|---|---|
| **P1**: `assertTrue(results)` が `subTest` の**外**にある。1 つ目の券種で yield 0 件だとループごと中断し、2 つ目の券種が一度も実行されない | **採納**（`subTest` 内へ移した） |
| P2: `ExtractionProbeTest` と `setUpClass` の検査が重なる | **維持**。消すなら残すのは**守衛の方**だが、独立したテストとして赤くなる価値もある（belt-and-suspenders は P1-1 の指摘に紐づいた意図的なもの） |
| P2: `_ProbeGuardedTest` は本スイート 39 ファイルで唯一の `setUpClass` 利用＝新しい語彙 | **維持**。docstring が「なぜ要るか」を書いてあり、代替（各テストで前提を手書き）は同じ穴を N 個作る |

**P1 の重み**: 指摘のとおり、この番人が探しているのは正に
「結線が複数箇所で同時に壊れる」種の事故（CLAUDE.md の `ENTRY_BUILDERS`
先例）。1 箇所で止まると 2 箇所目が見えない。断言を全て `subTest` に
入れて、1 回の実行で全券種の結果が出るようにした。

### 変異検証（最終形。6 変異とも殺された）

| # | 変異 | 結果 |
|---|---|---|
| M1 | 豁免ゲートを削除 | FAILED（failures=4） |
| M2 | 豁免集合から `TRANSIT_IC` を外す | FAILED（failures=3） |
| M3 | 豁免集合に `RECEIPT` を足す | FAILED（failures=3） |
| M4 | `doc_type` に既定値 `None` を付ける | FAILED（failures=1） |
| M5 | 呼出側で actual ではなく定数 `DocType.RECEIPT` を渡す | FAILED（failures=2） |
| M6 | `_extract_invoice_num_from_ocr` を常に `None` にする（前提の劣化） | FAILED（failures=1, errors=4） |

### 残す P2（本 Plan では手を付けない）

1. `_run_page` 型ヘルパの共通化（既存 4 ファイル ＋ 本ファイルの 5 箇所）
2. 台帳骨架（`OverrideLedgerTest` / `SuppressionLedgerTest`）の共通 assert ヘルパ化
3. CLAUDE.md の 6 表一覧と「番人テストだけの表」が混在している件（§8 に既出）
4. `ocr_engine.py` が 2775 行で 800 行上限を大幅超過（既存問題。本 diff は +45 行）
