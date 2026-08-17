# Plan: T3 — `_route_ocr_strategy` の resolver 化 ＋ `PageOcr` 契約

母 Plan: `docs/plans/2026-08-12-credit-card-doctype.md` §5 T3（前置依存タスク）
事実台帳: `docs/plans/2026-08-12-credit-card-sample-facts.md`
起案: 2026-08-17 / 対象ブランチ: `main`（実装は wip/ で checkpoint、main へは趙の拍板後）

---

## 0. なぜ T3 が T4 より先か（母 Plan の順序原則）

nimoca（交通系IC）の券面はクレカ明細と**同じ Drive フォルダに混載**される
（趙裁定 5。`FOLDER_TRANSIT_IC_ID` は永久に空）。つまり「どの prompt を使うか」は
フォルダでは決まらず**頁ごとに**決まる。現行 `process_pipeline` は
`ocr_engine.py:2063` で `prompt = PROMPTS.get(doc_type)` を**ファイル頭 1 回**だけ
解決し、全頁に同じ prompt を配っている。この構造のまま T4（prompt 2 本）を書くと、
クレカフォルダに入った nimoca 頁はクレカ用 prompt で読まれ、
`_build_entries_from_credit_card` が nimoca の JSON 構造を解釈できずに落ちる。

よって **prompt 解決を頁単位へ降ろす**のが T4 の前提になる。

---

## 1. 目標 / 非目標

### 目標

1. `_route_ocr_strategy` の戻り値を 3-tuple から `PageOcr` dataclass へ置換する。
   `raw_data` / `ocr_text` / `ocr_confidence` に加え
   `actual_doc_type` / `prompt` / `page_class` / `family_signal` を運ぶ。
2. prompt の解決点を「ファイル頭 1 回」から「頁ごとの resolver」へ降ろす。
   解決には `page_family.classify_page` → `page_family.select_prompt_doc_type` を使う。
3. Vision 兜底（3 箇所）にも**解決済みの prompt** を渡す。
4. 既存 doc_type（receipt / purchase_invoice / sales_invoice / salary_slip）の
   **観測可能な挙動を 1 ミリも変えない**。
5. 後方互換の unpack を**残さない**（静かに通る方が危険。母 Plan T3 の明文）。

### 非目標（T3 でやらないこと）

- prompt / builder の中身（T4）。本 T3 の間、新 2 型の prompt は
  `_CC_PROMPT_STUB` のまま、builder は `return []` のまま。
- `page_class` の**消費**（T9）。T3 は `PageOcr` に載せて運ぶだけで、
  `resolve_page_disposition` は呼ばない。`_yield_page_results` の去向決定
  ロジックは 1 行も触らない。
- 重複検出 `page_dedup` の配線（T9）。
- 窓分割リトライ（T5）、`line_mode`（T6）、overrides 豁免（T7）、異常検知（T8）。
- `.env` への `FOLDER_CREDIT_CARD_ID` 追加（T4 完了＋真票回帰まで**禁止**。
  builder がスタブのまま ID を配ると原票が仕訳ゼロで Processed へ歸檔される）。

---

## 2. 本 Plan で下す設計裁定

### AD-T3-1. `actual_doc_type` の作用範囲は **prompt と builder のみ**。タブは folder doc_type に従う

母 Plan §5 T3 は「`actual_doc_type` が **prompt・builder・タブ suffix を同時に決める**」
と書いているが、これは**現行アーキテクチャでは成立しない上に、既存の設計裁定と矛盾する**。

事実:

- `page_family.select_prompt_doc_type` の docstring（`page_family.py:395-396`）は
  「上書きしても doc_type そのものは差し替えない（**タブ・取引No 採番・分割 PDF の
  保存先が 1 ファイル内で割れるのを避ける**。D1 §5.3）」と明記している。
- タブ名は `sheets_output.py:178` / `:227` が `DOC_TYPE_TAB_SUFFIX.get(doc_type)` で
  決めており、その `doc_type` は `main.py:505` が `process_pipeline` に渡した
  **folder doc_type**。`process_pipeline` の yield は doc_type を**運んでいない**
  （`{"result", "page_num", "total_pages", "page_bytes"}` の 4 键のみ）。

つまり「タブも `actual_doc_type` で決める」には yield 契約と `main.process_file` の
改造が要り、しかもそれは既存裁定（D1 §5.3）を覆すことになる。

**裁定**: `actual_doc_type` は `PROMPTS` と `ENTRY_BUILDERS` の引き当てにのみ使う。
prompt と builder が**対**である（prompt が Gemini の出力 JSON 構造を決め、
builder がその構造を解析する）以上、この 2 つは必ず同源でなければならない。
タブ・取引No・分割 PDF 保存先は folder doc_type のまま＝ 1 ファイル 1 タブを維持する。
母 Plan §5 T3 の「タブ suffix」の記述は本 Plan で**訂正**する。

**帰結（T4 への申し送り）**: クレカフォルダに混載された nimoca 頁は、
`交通IC` タブではなく `カード明細` タブに出る。これは仕様である。

**第 3 の契約 `result["doc_type"]` の扱い（Codex 評審 #1 で発覚）**:
`_build_doc_result` は返す result dict に `"doc_type": doc_type` を刻んでいる
（`ocr_engine.py:1768`）。したがって `_yield_page_results` に `actual_doc_type` を
渡すと、**payload 内部の `result["doc_type"]` も actual になる**。
「prompt と builder だけ」という当初の表現は不正確だった。

実測: この键の消費者は現在**ゼロ**（全リポジトリ grep で `result["doc_type"]` を
読む箇所は無い。`local_test.py:77` / `:264` の `doc_type` は自前の file_info 由来で
別物）。よって挙動は変わらない。

**裁定**: `result["doc_type"]` の意味を **「この result を組んだ builder の
doc_type」** と定義する（folder ではない）。無消費だから放置、ではなく
**意味を先に固定する**。T9 が `main.process_file` を触るとき、この键を
「フォルダ種別」と誤読して Sheets のタブ選択に使う事故を防ぐため、
T3-f のテストで「`result["doc_type"]` は actual になりうる」かつ
「タブ選択はそれを見ない」の両方を固定する。

### AD-T3-2. **返される `PageOcr` の `prompt` は必ず非空**（構築前に弾く）

現行 `_route_ocr_strategy` は PaddleOCR が例外を投げると `except` で握り潰し
`(None, "", None)` を返す（`ocr_engine.py:1889-1891`）。呼出側はこれを見て
Vision 兜底へ落ちる（`:2120-2123`）。

resolver 化後、Vision 兜底は `page_ocr.prompt` を使う。したがって
**PaddleOCR が失敗した経路でも prompt が入っていなければならない**。
入っていないと、Vision 兜底が空文字 prompt で Gemini を叩き、
「呼出は成功・中身はゴミ」という最悪の無音故障になる。

**裁定**: prompt の解決は PaddleOCR の try/except の**外**（前）で
folder doc_type から先に確定させ、OCR テキストが取れたときだけ
`classify_page` → `select_prompt_doc_type` で**上書きを試みる**。
上書きの試行自体も例外を漏らさない（`classify_page` は仕様上例外を投げないが、
`PROMPTS` 引き当ての失敗は握る）。

**不変式の正確な言い方（Codex 評審 #3 を受けて訂正）**:
当初「prompt はいかなる経路でも空にならない」と書いたが、`_route_ocr_strategy` は
`process_pipeline` の早期チェック（`:2063`）を経ずに**直接呼べる**
（現に `test_ocr_engine_receipt.py:590` がそう呼んでいる）。未知の doc_type を
直接渡されたら prompt は作れない。

正しい不変式は **「`prompt` が空の `PageOcr` インスタンスは存在しない」**。
folder doc_type が `PROMPTS` に無い場合は `PageOcr` を**構築せずに `ValueError`
を送出**する。`None` を返す設計（当初案）は不変式と矛盾するうえ、
呼出側で `None` チェックを忘れると `AttributeError` が別の場所で出て
原因が追いにくい。例外なら発生地点がそのまま原因地点になる。

`process_pipeline` は `:2063` で先に存在性を検証してから呼ぶので、
この例外は本番経路では到達しない（＝プログラミングエラー専用の番人）。

### AD-T3-4. 例外境界を**現状のまま保存**する（Codex 評審 #2 / P0）

当初案の「PaddleOCR の except が後段を巻き込まないようにする」は、
**既存の兜底経路を壊す**。現行 `_route_ocr_strategy` の `try` は
PaddleOCR だけでなく `_call_gemini_text`（`:1876`, `:1880`）/
`_call_gemini_bytes`（`:1885`）/ `_call_gemini_cross_validate`（`:1887`）
**全部**を覆っており、Gemini 側の例外も握って `raw_data=None` を返す。
呼出側はそれを見て Vision 兜底へ落ちる（`:2120-2123`）。

except を PaddleOCR だけに絞ると、Gemini のタイムアウトや JSON 例外が
`process_pipeline` の逐頁 try まで飛び、**「Vision 兜底で救えたはずの頁」が
「ページ処理エラー」の占位行に転落する**。これは全 doc_type に効く回帰。

**裁定**: 例外の**捕捉範囲は現行と同一に保つ**。resolver 化で変えるのは
「prompt をどこで決めるか」だけであり、「何を握るか」は 1 ミリも変えない。
具体的には prompt の確定を `try` の**前**に置き、`try` の中身
（PaddleOCR ＋ resolver 上書き ＋ Gemini 分岐）は今と同じ 1 つの `try` で覆う。
resolver 上書きが例外を投げても、その頁は現行と同じく Vision 兜底へ落ちる
（prompt は try の前に確定済みなので folder のものが残っている）。

**DoD 追加**: 戦略 A/B/C それぞれで `_call_gemini_*` が例外を投げるケースを
テストし、その頁が Vision 兜底へ落ちることを固定する（特性テスト。
slipknot 19 の legacy 手順）。

> **2026-08-17 実施中の訂正**: 当初この特性テストを `_route_ocr_strategy` に
> 直接当て「改造前後で無修正で緑」を DoD にしていたが、**それは不可能**だった
> —— 同関数の第 3 引数と戻り値型こそが T3 で変える対象であり、
> どう書いても改造時に修正が要る。「無修正で緑」という証明力が丸ごと消える。
>
> **訂正後**: 特性テストは **`process_pipeline` 層**に置く（この関数の
> シグネチャは T3 で変わらない）。`_route_ocr_strategy` はモックせず、
> `_ocr_with_paddleocr` と `_call_gemini_text` / `_call_gemini_cross_validate` を
> モックして Gemini 側の例外を注入し、**`_call_gemini_bytes`（Vision 兜底）が
> 呼ばれること**を assert する。これなら改造を跨いで無修正で緑を維持でき、
> しかも「本当に守りたいもの（頁が兜底で救われる）」を直接測っている。

### AD-T3-3. `classify_page` は**全 doc_type で呼ぶ**（既存経路含む）

`select_prompt_doc_type` の第 1 分岐（`page_family.py:402-403`）は
folder が新 2 型**でない**とき `family_mismatch:ic_history` シグナルを返す。
これは「客が nimoca を領収書フォルダへ投げた」の検出であり、既存 doc_type でも
シグナルとして拾う価値がある。

コストは純正規表現 1 パス。同じ頁で走る PaddleOCR（数百 ms〜秒）と
Gemini（秒〜十数秒）に比べれば無視できる。

**裁定**: 全 doc_type で `classify_page` を呼ぶ。ただし T3 では
`family_signal` を `PageOcr` に載せるだけで**何もしない**（消費は T9）。
既存 doc_type は `select_prompt_doc_type` が folder doc_type をそのまま返すので、
prompt は不変＝挙動不変。

---

## 3. `PageOcr` の契約

```python
@dataclass(frozen=True)
class PageOcr:
    """1 頁の OCR ＋ prompt 解決の結果。

    _route_ocr_strategy の唯一の戻り値型。後方互換の tuple unpack は無い。
    """
    raw_data: object                 # Gemini の生 JSON（失敗時 None）
    ocr_text: str                    # PaddleOCR テキスト（失敗時 ""）
    ocr_confidence: float | None     # PaddleOCR 経由のときだけ数値
    actual_doc_type: str             # prompt / builder の引き当てキー
    prompt: str                      # 解決済み prompt（**常に非空**）
    page_class: object               # page_family.PageClass（T9 が消費）
    family_signal: str | None        # 監査シグナル（T9 が消費）
```

`frozen=True` にするのは、呼出側が「あとから prompt を差し替える」ような
書き方を構造的に封じるため（AD-T3-2 の不変式を型で守る）。

---

## 4. 改造対象の全リスト（実測）

### 4.1 実装側（`ocr_engine.py`）

| # | 位置 | 現状 | 改造後 |
|---|---|---|---|
| 1 | `:1854-1891` | `_route_ocr_strategy(data, mime, **prompt**, strategy, prefix) -> tuple[3]` | 第 3 引数を `doc_type` へ。戻り値 `PageOcr` |
| 2 | `:2063-2066` | `prompt = PROMPTS.get(doc_type)`／無ければ return | **存在性検証のみ**に降格（prompt は下へ流さない） |
| 3 | `:2116-2118` | PDF 逐頁の呼出（3-tuple unpack） | `page_ocr = _route_ocr_strategy(..., doc_type, ...)` |
| 4 | `:2122` | Vision 兜底 `_call_gemini_bytes(..., prompt)` | `page_ocr.prompt` |
| 5 | `:2164-2166` | `_yield_page_results(**doc_type**, page_raw_data, ocr_text, ocr_conf, ...)` | `page_ocr.actual_doc_type` ＋ `page_ocr.ocr_text` / `.ocr_confidence` |
| 6 | `:2215` | 尾段の呼出（3-tuple unpack） | `PageOcr` へ |
| 7 | `:2221` | 尾段 Vision 兜底 `_call_gemini(file_path, prompt)` | `page_ocr.prompt` |
| 8 | `:2231` | 尾段 `_yield_page_results(**doc_type**, raw_data, ...)` | `page_ocr.actual_doc_type` |

`_yield_page_results` 自体のシグネチャは**変えない**（doc_type を受け取る形のまま。
呼出側が渡す値が folder doc_type から actual_doc_type に変わるだけ）。
これで T9 が触る面と T3 が触る面が重ならない。

### 4.2 テスト側（5 ファイル 6 箇所 — 母 Plan の「7 箇所」は実測 6 箇所）

| ファイル:行 | 形態 | 改造 |
|---|---|---|
| `test_ocr_engine_receipt.py:584` | 直接呼出。`len(out) == 3` を契約として固定 | **契約テストを `PageOcr` へ書き換え**（アリティ検査は廃止し、フィールド名を固定） |
| `test_ocr_engine_receipt_pipeline.py:76` | `side_effect=[(raw, text, conf), ...]` | 3-tuple → `PageOcr` 変換ヘルパ経由 |
| `test_ip401_regression.py:66` | 同上 | 同上 |
| `test_ocr_engine_invoice_pipeline.py:102` | 同上 | 同上 |
| `test_ocr_engine_invoice.py:95` | 同上 | 同上 |
| `test_ocr_engine_invoice.py:165` | `return_value=(raw, "", 0.9)` | `PageOcr` へ |

**テストデータは書き換えない**。各ファイルのヘルパ関数内で
`(raw, text, conf)` → `PageOcr` へ変換する 1 関数を噛ませ、
個々のテストケースが持つ 3-tuple のフィクスチャはそのまま使う。
理由: フィクスチャを機械的に書き換えると差分が巨大になり、
「テストの意図が変わっていないこと」のレビューが不可能になる。

---

## 5. タスク一覧（実施順・各項 DoD）

### T3-a. `PageOcr` 型と契約テスト（RED 先行）

`ocr_engine.py` に `PageOcr` を定義。`test_ocr_engine_receipt.py:584` の
`test_route_ocr_strategy_returns_three_tuple` を
`test_route_ocr_strategy_returns_page_ocr` へ書き換え、7 フィールドの存在と
`frozen` を固定する。

**DoD**: 実装前にこのテストが**赤**であること（`PageOcr` 未定義 or
`_route_ocr_strategy` が tuple を返す）を実行ログで確認。

### T3-b. prompt resolver の抽出（純関数・venv 非依存で単体テスト可能）

`ocr_engine` に `_resolve_page_prompt(folder_doc_type, ocr_text)` を新設。
戻り値 `(actual_doc_type, prompt, page_class, family_signal)`。

- `ocr_text` が空/None → `classify_page` は呼ぶ（空 PageClass が返る）が
  `select_prompt_doc_type` は folder を返すので folder の prompt になる。
- `PROMPTS` に `actual_doc_type` が無い（登録漏れ）→ **`actual_doc_type` と prompt を
  セットで folder へ戻す**（Codex 評審 #8）。片方だけ戻すと
  「prompt は credit_card・builder は transit_ic」という**異種混成**が生まれ、
  AD-T3-1 が守ろうとした「prompt と builder は必ず同源」を自分で破る。
  併せて `family_signal` に `prompt_fallback:<欠落型>` を立て、無音で縮退しない。
- folder の prompt すら無い → **`ValueError` を送出**（AD-T3-2）。`None` は返さない。

**DoD**:
- 新 `test_page_prompt_resolver.py` が緑。ケース:
  (a) folder=credit_card ＋ nimoca 強シグナル → `transit_ic` の prompt
  (b) folder=credit_card ＋ クレカ強シグナル → `credit_card` のまま
  (c) folder=credit_card ＋ 両方 strong → folder を信じる（`family_ambiguous`）
  (d) **既存 4 型すべて**（receipt / purchase_invoice / sales_invoice / salary_slip）
      ＋ nimoca 強シグナル → **prompt も actual_doc_type も folder のまま**、
      シグナルだけ `family_mismatch:ic_history`（Codex 評審 #9。
      receipt だけ確認して他 3 型を確認しないのは、`select_prompt_doc_type` の
      第 1 分岐が将来変わったときに気づけない）
  (e) `ocr_text=""` → folder の prompt、`family_signal is None`
  (f) `ocr_text=None` → 例外を投げず (e) と同じ
  (g) `PROMPTS` から `transit_ic` を一時的に抜き、folder=credit_card ＋ nimoca 強
      → **actual も prompt も credit_card へ戻る**（片側だけ戻らない）かつ
      `family_signal` に `prompt_fallback` を含む
  (h) folder が `PROMPTS` に無い未知の doc_type → `ValueError`
- **prompt が空文字/None の戻り値が存在しない**ことを property 的に確認
  （(h) の例外経路を除く全入力）

### T3-c. `_route_ocr_strategy` の resolver 化

第 3 引数を `prompt: str` → `doc_type: str` へ。本体を

```
① prompt / actual_doc_type を folder から確定（try の外。失敗なら ValueError）
② try:
     PaddleOCR
     テキストが取れたら resolver で ①を上書き試行
     strategy 分岐で Gemini
   except Exception:            ← 捕捉範囲は現行と 1 ミリも変えない（AD-T3-4）
     print して素通り
③ PageOcr を組んで返す（prompt は必ず①か上書き後の値＝非空）
```

の順に組み替える。**②の try は現行と同じく Gemini 呼出まで覆う**
（AD-T3-4。ここを絞ると Vision 兜底が効かなくなる）。

**DoD**:
- T3-a の契約テストが緑
- **特性テスト先行**（`test_route_ocr_characterization.py`、`process_pipeline` 層。
  AD-T3-4 の訂正注記を参照）: 改造の**前**に書き、戦略 A/B/C それぞれで
  `_call_gemini_text` / `_call_gemini_cross_validate` が例外を投げたとき
  `_call_gemini_bytes`（Vision 兜底）が呼ばれることを固定。
  **改造後も無修正で緑**であることが AD-T3-4 の証明になる
- PaddleOCR が例外を投げるケースで `PageOcr.prompt` が folder の prompt に
  なっていることをテストで固定（AD-T3-2 の不変式）
- PaddleOCR がテキストを返さないケース（`""`）でも同上
- resolver が例外を投げるケース（`select_prompt_doc_type` を patch）でも
  prompt が folder のもので残り、Vision 兜底に落ちること
- 戦略 A/B/C それぞれで `ocr_confidence` の有無が現行と同一
  （B の Vision 兜底のみ `None`）

### T3-d. 呼出側 8 箇所の書き換え

§4.1 の表のとおり。`:2063` は「PROMPTS に存在しない doc_type は早期 return」
という**既存の防御を維持**したまま、prompt 変数を下流へ流さない形にする。

**DoD**:
- `python -c "import ocr_engine"` が通る
- 3-tuple unpack が `ocr_engine.py` から**消滅**していること
  （`grep -n "= _route_ocr_strategy" ocr_engine.py` の全ヒットが `PageOcr` 受け）
- Vision 兜底 3 箇所すべてが `page_ocr.prompt` を渡していること

### T3-e. テスト側 6 箇所の追随

§4.2 の表のとおり。各ファイルに 3-tuple → `PageOcr` 変換ヘルパを置く。

**DoD**: 5 ファイルすべて緑。**テストケース本体の期待値を 1 つも書き換えていない**
こと（差分レビューで確認。書き換えが必要になったら、それは挙動が変わった証拠なので
停止して原因を調べる）。

### T3-f. 混載フォルダの結線テスト（母 Plan T3 の DoD 本体）

`test_ocr_engine_mixed_folder.py`（新規）:
クレカフォルダ（`doc_type=CREDIT_CARD`）の 2 頁 PDF で、
p1 がクレカ明細・p2 が nimoca 履歴の OCR テキストを返すようにモックし、

- p1 は `PROMPTS[CREDIT_CARD]` で Gemini が呼ばれる
- p2 は `PROMPTS[TRANSIT_IC]` で Gemini が呼ばれる
- `_yield_page_results` へ渡る doc_type が頁ごとに異なる

**タブ不分裂の検証は `main.process_file` 層で行う**（Codex 評審 #5 で訂正）。
当初案は「`process_pipeline` の yield が doc_type を運ばないこと」を固定しようと
していたが、**タブはそこで決まっていない**。実際の決定点は
`sheets_output.start_new_file`（`:175-178`）と `append_entries`（`:207,227`）で、
どちらも `main.py:569-571` が渡す **folder doc_type** を使う。
yield の形を見張っても、将来 `main` 側が `result["doc_type"]` を読んで
タブを決め始めたら検出できない——**守りたい不変式の外側を見張っていた**。

**改訂 DoD**:
- 上記 3 点が緑（prompt はスタブが同一文字列なので、`PROMPTS` を一時的に
  別文字列へ patch して識別する）
- `main.process_file` を fake writer（`start_new_file` / `append_entries` の
  `doc_type` 引数を記録するだけのスタブ）で通し、混載 2 頁に対して
  **記録された doc_type が全件 folder doc_type と一致**すること
- 同テストで `result["doc_type"]` が頁ごとに actual になっている
  （＝内部では割れているが、書込先は割れていない）ことを併せて assert。
  AD-T3-1 の「第 3 の契約」を、意味と結果の両面から固定する

### T3-g. 既存経路の不変性確認

**DoD**: `venv311/bin/python -m unittest discover -p "test_*.py"` 全緑。
改造前後で `git stash` を使い、既存テストの**件数と名前が一致**することを確認
（テストを削って緑にしていないことの証明）。

---

## 6. 受入基準（機械判定）

| # | 基準 | 判定方法 |
|---|---|---|
| B1 | 全既存テストが緑、件数が改造前と同じ | `unittest discover` の `Ran N tests` を改造前後で比較 |
| B2 | `_route_ocr_strategy` の戻り値が `PageOcr` | 契約テスト（フィールド名 7 個 ＋ `frozen`） |
| B3 | 3-tuple unpack が実装・テストの双方から消滅 | `grep -n "_route_ocr_strategy(" ocr_engine.py test_*.py` の**全ヒットを目視**し、戻り値を tuple として扱う箇所が 0 であること ＋ B2 の契約テスト（Codex 評審 #7。`grep -c "= _route_ocr_strategy"` は書式ゆれ・入れ子代入・直接呼出を取りこぼす） |
| B4 | Vision 兜底 3 箇所が解決済み prompt を使う | 混載テストで `_call_gemini_bytes` の第 3 引数を assert |
| B5 | PaddleOCR 失敗時も prompt が非空 | T3-c の不変式テスト |
| B6 | **既存 4 型すべて**で prompt が folder のものから変わらない | resolver テスト (d) |
| B7 | 混載フォルダで nimoca 頁が transit_ic の prompt へ流れる | T3-f |
| B8 | 混載でもタブが割れない | T3-f の `main.process_file` ＋ fake writer |
| B9 | Gemini 例外時の Vision 兜底が現行と同一（AD-T3-4） | `test_route_ocr_characterization.py`（`process_pipeline` 層）が改造前後とも**無修正で**緑 |
| B10 | 新規/改造モジュールのカバレッジ ≥ 80% | `coverage run --source=ocr_engine -m unittest ...` |

---

## 7. テスト戦略

- **TDD**: T3-a（契約テスト RED）→ T3-b（resolver テスト RED → GREEN）→
  T3-c/d（実装）→ T3-e（既存追随）→ T3-f（結線 RED → GREEN）。
- **単体**: `_resolve_page_prompt` は `page_family` にしか依存しないが、
  `ocr_engine` の import が paddleocr を引くため `test_page_prompt_resolver.py` は
  venv311 必須と冒頭 docstring に明記する。
- **集成**: T3-f が `process_pipeline` を通す集成テスト。
- **E2E**: T3 では**やらない**。真票 E2E は T11 の担当（prompt/builder が
  スタブのままでは E2E に意味が無い）。母 Plan の順序どおり。

---

## 8. 影響面

| ファイル | 変更 | 既存への危険度 |
|---|---|---|
| `ocr_engine.py` | `PageOcr` 追加、`_resolve_page_prompt` 追加、`_route_ocr_strategy` 署名変更、呼出 8 箇所 | **高**（本番の全 doc_type が通る経路） |
| `page_family.py` | **変更しない**（既存関数を呼ぶだけ） | なし |
| `local_test.py` | **変更しない**が `process_pipeline` の第 2 の消費者 | 低（下記） |
| テスト 5 ファイル | mock 形態の追随 | 中（意図を変えない機械的変換） |
| 新規テスト 3 ファイル | 追加のみ | なし |

**`local_test.py` について**（Codex 評審 #6）: `process_pipeline` の消費者は
本番の `main.py` だけでなく、ローカル検証の `local_test.py` もある。
後者も書込時に folder doc_type を渡している（`:122`, `:167`）ので
AD-T3-1 と整合しており、**T3 では 1 行も変えなくてよい**。
ただし T9 が payload にメタ情報を足すときに片方だけ直す事故が起きうるので、
「2 消費者ある」という事実をここに明記して申し送る。
（Codex は追加のテストも提案したが、コード変更ゼロの消費者に対して
テストを増やすのは過剰。事実の記録で足りる — 部分採納）

**変更しない**: `main.py` / `sheets_output.py` / `anomaly_detector.py` /
`doc_types.py` / `config.py` / `receipt_aggregation.py` / `tag_rules.py` / `gas/*`。

---

## 9. リスクと回退

| # | リスク | 深刻度 | 対策 |
|---|---|---|---|
| R1 | 署名変更で既存 4 doc_type の本番経路が壊れる | **P0** | B1（全テスト緑＋件数一致）。後方互換 unpack を作らないので、直し漏れは実行時 `TypeError` で必ず露見する（静かに通らない） |
| R2 | Vision 兜底に空 prompt が渡り「成功したゴミ」が記帳される | **P0** | AD-T3-2 ＋ B5。prompt を try の外で先に確定 |
| R3 | `classify_page` が既存頁で誤って強シグナルを出し prompt が化ける | P1 | `select_prompt_doc_type` の第 1 分岐が folder∉{cc,ic} を素通しする構造。B6 で固定 |
| R4 | mock 書き換えでテストの意図が変わり、緑が偽陽性になる | P1 | T3-e の DoD「期待値を 1 つも書き換えない」＋ B1 の件数一致 |
| R5 | `actual_doc_type` がタブまで動かし 1 ファイルが 2 タブに割れる | P1 | AD-T3-1 で範囲を限定。T3-f が yield 契約に doc_type が無いことを固定 |

**回退**: T3 は単一コミット（wip/ ブランチ）に収める。壊れたら `git revert` 1 発で
戻る。`page_family` を触らないので、T2 の成果物は回退の影響を受けない。

---

## 10. 附録: 母 Plan からの訂正事項

| 母 Plan の記述 | 実測 | 本 Plan の扱い |
|---|---|---|
| 「`actual_doc_type` が prompt・builder・**タブ suffix** を同時に決める」（§5 T3） | yield が doc_type を運ばず、タブは `main` 経由の folder doc_type で決まる。`select_prompt_doc_type` の docstring は「doc_type そのものは差し替えない」と明記 | **AD-T3-1 で訂正**。タブは folder に従う |
| 「5 ファイル 7 箇所のモック」（§5 T3） | 実測 5 ファイル **6 箇所**（うち 1 箇所は mock ではなく直接呼出の契約テスト） | §4.2 の実測表で置換 |
| 「Vision 兜底（`ocr_engine.py:2079`）」（§5 T3） | 行番号が漂移。現在は `:2122`（PDF 逐頁）と `:2221`（尾段）の **2 箇所** ＋ 戦略 B 内部の `:1885` で計 3 箇所 | §4.1 の実測表で置換 |

---

## 11. 後続タスクへの申し送り（T3 の作業ではないが T3 で判明したこと）

### 順序依存: **T7 は `.env` に folder ID を配る前に落ちていなければならない**

Codex 評審 #4 の初回指摘は因果関係が誤っていた（下記辯論記録参照）が、
複審で**本物の順序依存**が残った。

`_apply_ocr_overrides` は `_yield_page_results` の 1 行目で**無条件**に走り
（`ocr_engine.py:1936`）、`documents` 配列を持たない raw_data に対して
トップレベルの `date` と `invoice_num` を上書き・新設する（`:987-1003`）。
クレカ明細の raw_data はこの形になる見込みで、母 Plan AD-3 が
「新 doc_type では overrides を無効化する」と定めているのはまさにこのため。

**ゲート**: `FOLDER_CREDIT_CARD_ID` を `.env` に書く前に、T7 が完了していること。
T4 のコードだけ先に main へ入っても、ID が配られていない限り新 doc_type は
到達不能なので安全（現に `.env` の実測ヒット数は 0）。
既存の申し送り「ID 追加は T4 完了まで禁止」に、**T7 完了も条件として加える**。

### 語義の変化: `_yield_page_results` の `doc_type` 引数

T3 以降、この引数は「フォルダの種別」ではなく **「この頁を解析した builder の
種別」** を意味する。シグネチャは変えない（T9 との衝突を避けるため）が、
docstring に明記して、T9 で触る人が folder 種別と誤読しないようにする。

---

## 附録 A: Codex 評審の辯論記録（2026-08-17）

評審者: `codex-cli 0.147.0`。Plan 全文 ＋ 実コードを読ませ、
特に AD-T3-1 / AD-T3-2 / 再順序化の安全性 / §4.1 の網羅性を突かせた。
指摘 9 件（P0×2 / P1×4 / P2×3）。

### 採用（7 件）

| # | 指摘 | 反映先 |
|---|---|---|
| 1 | `_build_doc_result` が `result["doc_type"]` を刻むので、`actual_doc_type` を渡すと **prompt/builder 以外に第 3 の可観測契約が動く**。「prompt と builder だけ」は不正確 | AD-T3-1 に「第 3 の契約」節を追加。実測で消費者ゼロを確認したうえで**意味を先に固定**し、T3-f で両面を assert |
| 2 (P0) | 例外の捕捉範囲を PaddleOCR に絞ると、**現在は握られている Gemini 例外**（`:1876/1885/1887`）が漏れ、Vision 兜底（`:2120`）に落ちるはずの頁が「ページ処理エラー」に転落する | **AD-T3-4 を新設**。捕捉範囲を現行と同一に保つと明文化。T3-c に特性テスト（改造前に書き、改造後も無修正で緑）を DoD 追加 |
| 3 | 「prompt は決して空にならない」は `_route_ocr_strategy` を直接呼ぶ経路（`test_ocr_engine_receipt.py:590`）では成立しない。かつ T3-b の「folder prompt 無しは None を返す」と自己矛盾 | AD-T3-2 を **「prompt が空の `PageOcr` インスタンスは存在しない」** へ言い換え、folder prompt 欠落時は `ValueError`（`None` を返さない）に変更 |
| 5 | T3-f の「タブが割れない」テストが `process_pipeline` の yield 形を見張っているが、**タブはそこで決まっていない**（`sheets_output:175/207` ＋ `main:569-571`） | T3-f を `main.process_file` ＋ fake writer 方式に全面改訂。B8 として受入基準に独立 |
| 6 | `process_pipeline` の消費者は `main.py` だけでなく `local_test.py`（`:122/:167`）もある | §8 影響面に明記。**ただしテスト追加は過剰として部分採納**（コード変更ゼロの消費者） |
| 7 | B3 の `grep -c "= _route_ocr_strategy"` は書式ゆれ・入れ子代入・直接呼出を取りこぼす | B3 を「全ヒットを目視 ＋ 契約テスト」へ強化 |
| 8 | `PROMPTS[actual]` 欠落時に prompt だけ folder へ戻すと、**prompt=credit_card / builder=transit_ic** の異種混成が生まれ、AD-T3-1 を自分で破る | T3-b を「actual と prompt を**セットで**戻す」＋ `family_signal` に `prompt_fallback` を立てるよう改訂。テスト (g) を追加 |
| 9 | resolver の既存型回帰テストが receipt だけでは、`select_prompt_doc_type` の第 1 分岐が将来変わったとき気づけない | テスト (d) を既存 4 型全部へ拡大。B6 も同様に改訂 |

### 反駁 → Codex 撤回（1 件）

**#4 (P0)「`actual_doc_type` を渡すと T7 の豁免より先に `_apply_ocr_overrides` が
クレカ raw_data に走る」**

反駁の根拠 3 点:

1. `_apply_ocr_overrides` は `_yield_page_results` の**1 行目で無条件**に呼ばれる
   （`:1936`）。doc_type で分岐していない。よって folder を渡すか actual を渡すかは
   overrides が走るかどうかを**何も変えない**。因果連鎖が成立しない。
2. T3 の時点で新 doc_type は**本番到達不能**。`.env` に `FOLDER_CREDIT_CARD_ID` が
   無く（実測ヒット 0）、`config.load_folder_map` がどのフォルダも新型に写像しない。
3. 提案された修正（`_yield_page_results` に folder と actual の両方を渡す）は
   同関数のシグネチャを変える。T3 は T9 と作業面が重ならないよう
   **意図的に凍結**している。

**Codex の複審回答**: 「(a) Yes, I withdraw the P0 severity. ... My causal chain
was wrong.」「(c) No concrete T3 runtime hazard from freezing the signature.」

→ **我方の維持が勝ち**。ただし複審で以下 2 点の副産物を得たので取り込んだ:

- **(b) 本物の順序依存**: overrides の豁免（T7）は「T4 のコードが main に入る前」
  ではなく「**`.env` に folder ID が配られる前**」に必要。→ §11 に明記
- **(c) 語義の caveat**: `_yield_page_results` 内で `doc_type` の意味が
  「folder 種別」から「builder 種別」へ変わる。→ §11 に明記

### 辯論から得た教訓（Plan 段階）

Codex の 9 件のうち、**最も価値が高かったのは #2（例外境界）** ——
「resolver 化」という言葉に引きずられて `try` の形まで整理しようとしていた。
リファクタで**捕捉範囲を「ついでに」綺麗にする**のは、本番の兜底経路を
無音で殺す典型的な手口だった。

一方 #4 は、**プランの文面だけを読んで因果を組み立てた**誤りだった
（`_apply_ocr_overrides` が無条件呼出であることを確認していない）。
指摘の重大度ではなく、**その指摘が実コードのどの行で検証されたか**で
採否を決めるべきという slipknot 核心協議どおりの結果になった。

---

## 附録 B: 実施記録と simcodex 評審（2026-08-17）

### 結果

| | 件数 |
|---|---|
| 改造前ベースライン | 674 tests / OK |
| 実施後 | **703 tests / OK**（純増 29。既存テストの削除・改竄なし＝B1） |
| 脱 venv（`python3`）で走る純関数テスト | **222 tests / OK**（skip 2 は意図的な ocr_engine 突合） |
| T3 で新設・改造した関数のカバレッジ | Missing 行ゼロ |

受入基準 B1〜B10 は全て充足。B3 は `grep` を目視判定へ強化した結果、
`_route_ocr_strategy` の全 5 参照が単一変数受けで tuple unpack ゼロを確認。

### Round 1（4 観点エージェント ＋ codex）

codex: finding ゼロ。エージェント側:

| 観点 | 結果 |
|---|---|
| Efficiency | P0/P1 なし。実測して noise と確定（`classify_page` 最悪 315µs / `import page_family` 2.2ms / `PageOcr` は page bytes を保持せず per-page GC を妨げない） |
| Reuse | **P1**: OCR 標本が 3 箇所に複製。**P2**: fake writer の重複 |
| Altitude | **P1**: §11 が約束した docstring が未落地（呼出側コメントだけだった） |
| Simplification | P0/P1 なし。P2 が 4 件 |

**エージェントの副産物から自分のテストの欠陥が 1 件出た**: Reuse が
「`main.process_file` は `start_new_file` を呼ばない」と指摘したことで、
`for dt in writer.start_doc_types:` が**空リストを回す無齒アサーション**だった
と判明。`assertEqual(writer.start_doc_types, [])` に置換し、
「タブを開くのは `main()` のポーリングループであって `process_file` ではない」
という事実の方を固定した。

### Round 2 — **Round 1 の修正が P0 を作った**

Round 1 の「標本を 1 箇所へ寄せる」修正で、標本を `ocr_test_helpers`
（`PageOcr` を組むため `ocr_engine` を import する）に置いた。結果:

```
test_page_family → ocr_test_helpers → ocr_engine → google.generativeai
```

母 Plan §4 が定める「4 純関数モジュールは venv 無しで単体テスト可能」が壊れた。

| | `python3 -m unittest test_page_family` |
|---|---|
| Round 1 前 | 56 tests OK |
| Round 1 後 | `ModuleNotFoundError: No module named 'google'` |

**この間、全 701 テストは緑のままだった。** CI も手元も venv311 で走らせるので、
重依存が紛れ込んでも誰も気づかない。**4 観点のエージェントも codex も
一次では検出していない**（Round 2 で明示的に指示して初めて確認された）。
発見したのは「脱 venv で実際に走らせてみる」という 1 回の実行。

修正:

1. `ocr_test_fixtures.py`（**stdlib のみ**）＝ 標本と `temp_pdf_path`
2. `ocr_test_helpers.py`（`ocr_engine` に依存）＝ `PageOcr` 変換だけ
3. `test_dependency_weight.py` ＝ **番人**。4 モジュールとそのテストの
   import を AST で推移的に辿り、重依存への到達を検出して落ちる

番人は**変異検証済み**: `test_page_family` の先頭に故意に `import ocr_engine`
を入れると検出して赤になることを確認（確認後に還元）。

番人の実装で 1 度失敗している: 最初 `ast.walk` で全 import を数えたため、
`test_page_family` が**関数内で** `try: import ocr_engine / except: skip`
している既存の正しい設計を違反と誤判定した。「守りたいのは
**import 時に実行される** import が軽いこと」であって「重依存に一切触れないこと」
ではない。関数・クラス本体には降りず、トップレベルの `try`/`if`/`with` には
降りる形へ直した。

### Round 2 の残り（P2 2 件）

| 指摘 | 処置 |
|---|---|
| `test_page_family.py` の注記が旧 `ocr_test_helpers` を指したまま | 修正 |
| `test_route_ocr_characterization.py` が `temp_pdf_path()` を使わず tempfile を 2 箇所ベタ書き | **維持（駁回）**。このファイルの価値は中身が T3 の前後で変わっていないこと自体であり、整理でも触れば「無修正で緑だった」証明が弱まる。理由をファイル冒頭に明記し、T3 期間の追記が説明文のみでコードは 1 行も変えていないことも併記した（AST で 13 個のトップレベル要素が不変であることを確認） |

Round 2 で早期終了（simplify P0/P1 ゼロ・codex finding ゼロ・全量緑）。

### 教訓（次に同種の改造をする人へ）

**「全量テストが緑」は「壊していない」の証明にならない。**
テスト実行環境が 1 種類しかないとき、その環境でしか成立しない前提が
壊れても緑のままになる。ここでは「venv 無しでも走る」という設計上の性質が
それだった。エージェントを 4 体並べても、codex を 2 周回しても出てこず、
**実際に別の環境で 1 回走らせる**ことでしか出なかった。

同種の性質（別 OS で動く / 特定の依存が無くても動く / 特定の設定が既定値でも動く）
を持つコードを触るときは、**その性質を機械で見張るテストを同じコミットに入れる**。
人間もエージェントも、緑のテストスイートを前にして疑いを持ち続けることはできない。
