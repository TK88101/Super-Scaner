# T8 — 頁の去向裁決を生産経路へ接線し、リボ頁を監査タブへ落とす

**状態: 定稿**（Codex 評審 2 ラウンド完了・総合判定 YES）
作成: 2026-08-19 / 対象分支: `main`（HEAD = `6752ee6` ＋ 未 commit の
`test_card_reconciliation.py` +46 行）/ 母 Plan: `2026-08-12-credit-card-doctype.md`
/ 直前 Plan: `2026-08-19-t11-partial-local-test-card.md`（§13 要求台帳・§15 着手前 3 件）

---

## 0. この Plan を書く前に判った最重要事実（範囲が縮んだ）

**`page_family.py`（536 行）が T8 の判定ロジックの大半を既に実装済**である。
着手前の想定（「#5 と #6 の判定を新規に書く」）は**誤り**だった。

| 既存資産 | 状態 |
|---|---|
| `classify_page(ocr_text)` → `PageClass` | 実装済・`ocr_engine.py:2091` から**呼ばれている**（prompt 選択用） |
| `resolve_page_disposition(...)` → `Disposition` | 実装済・**生産経路から一度も呼ばれていない** |
| `exclusion_fields(disposition)` | 実装済・未使用 |
| `FAMILY_POINTS_ONLY` → 監査タブ | 実装済（#5 の去向） |
| `FAMILY_INFO_NOTICE` → 監査タブ | 実装済 |
| `FAMILY_CC_SUMMARY` → MF タブ | 実装済（裁定 8） |
| entries>0 は絶対に除外しない gate | **実装済**（優先序 2。IP-401 不変式） |
| `test_page_family.py` | 700 行超の単体テスト在り |

`ocr_engine.py:2054-2055` のコメント「`page_class` / `family_signal`（**T9 が消費**）」が
示すとおり、分類結果は `PageOcr` に載って運ばれているが**誰も消費していない**。

→ **T8 の本体は「接線」であり、判定の新規実装ではない。**

---

## 1. 目標 / 非目標

### 目標

1. `resolve_page_disposition` を生産経路（`_yield_page_results` の P-B 位置）へ
   接線し、`Disposition` を頁の去向の**唯一の決定者**にする。
2. リボ払い・分割払い頁（要求 #6）が監査タブへ落ちるようにする。
   現状は赤い「認識不能」行になる（実測）。
3. ポイント頁（要求 #5）が監査タブへ落ちることを**接線によって**達成する
   （判定は既存のまま。新規語彙は不要 —— 実測で去向が既に正しい）。
4. **`page_family` 由来の非記帳族は、entries を 1 件でも組めた頁を除外しない。**
   `resolve_page_disposition` の優先序 2 がこれを保証する。
   **表現を限定している理由**（Codex HIGH-3 を採納）: 「entries>0 は絶対に
   除外されない」と書くと嘘になる。優先序 1 の duplicate は entries より**前**に
   評価され（`page_family.py:459`）、社会保険料通知書は builder より**前**に
   短絡する（`ocr_engine.py:2272`）。T8 では `dedup_verdict=None` を渡すので
   前者は発火しないが、不変式の主語は「族シグナル」に限る。

### 非目標（今回やらない。理由付き）

1. **要求 #4（主副カード各人小計の兜底）** —— 除外ロジックと直交する。
   prompt 側は覆っており（`card_prompts.py:159`）、実害は「Gemini が rows に
   入れたら」という条件付き。§9 で趙の拍板を仰ぐ。
2. **補助科目（カード名）の頁級抽出＋全行配布** —— これも直交する別軸の改修。
   **T8c** として切る（§9）。
3. **`card_reconciliation` の接線**（T9）。P1「合計不整合」と P2「偽の一致」
   （`RiboPageZeroBaselineTest` の expectedFailure）はそちらで決着させる。
4. **`page_dedup` の接線**（T9）。`resolve_page_disposition` の第 1 引数
   `dedup_verdict` には今回 `None` を渡す（優先序 1 をスキップ）。
5. 既存 4 doc_type（receipt / purchase_invoice / sales_invoice / salary_slip）の
   挙動変更。接線は card 系 doc_type の経路にのみ効かせる。

---

## 2. 前提事実（2026-08-19 実測済。再調査しないこと）

参考資料: アメックス 8 頁券面（`~/Desktop/井戸会計事務所/任務3/...`）。
生データは scratchpad の `probe_p7p8_result.json` / `probe_family_result.json`。
**顧客実名を含むので repo にも本 Plan にも貼らない。**

### 2.1 Gemini の挙動（3 回実行・出力は逐字一致）

| | p7 ポイント頁 | p8 リボ頁 |
|---|---|---|
| `raw.rows` | `[]` | `[]` |
| `builder_entries` / `result["entries"]` | 0 / 0 | 0 / 0 |
| `raw.total_amount` | `None` | `0` |
| `raw.printed_totals` | `[]` | `[{今回ご請求金額: 0}]` |

**rows が空なのは Gemini 自身が組まなかったから**（builder の事後濾過ではない）。
恐れていた「リボ利用可能枠 1,500,000 を明細と誤認」は 3 回とも起きていない。
→ **P-B の gate は「entries 空のときだけ」で足りる。社保式の短絡は不要。**

**局限**: アメックス 1 券面 × 3 回。他発行体のリボ頁版面は未測。

### 2.2 `page_family` の分類結果（PaddleOCR のみ・Gemini 呼出ゼロ）

| 頁 | `routing_family` | `has_detail_rows` | `entry_count=0` の去向 | `entry_count=6` |
|---|---|---|---|---|
| p5（明細・対照） | `cc_detail` | True | `unrecognized`（正当） | `book` ✅ |
| **p7 ポイント** | `cc_detail` | False | **`exclude` / `audit_tab` / `info_notice`** ✅ | `book` ✅ |
| **p8 リボ** | `unknown` | **True** | **`unrecognized`** ❌ | `book` ✅ |

- **#5 は既に正しい。** 接線するだけで要求を満たす。
- **#6 は p8 が優先序 3（`has_detail_rows` → 絶対に除外しない）で止まっている。**

### 2.3 p8 の `has_detail_rows=True` は**二重の誤判定**である（核心）

`_looks_like_detail_rows` は `dates >= 5 かつ amounts >= 5` で発火する。
p8 の実測内訳:

| 種別 | 件数 | 実際の中身 |
|---|---|---|
| dates | 5（閾値ちょうど） | `2023年1月19日` / `2020年10月8日` / **`14.90`** / `2023年1月30日` / **`14.6`** |
| amounts | 9 | `818`(頁番号 8/8) / `2023` / `2020` / `0000009` / `365` / `366`(日数) 等 |

**真の日付は 3 件だけ。`14.90`・`14.6` は基本手数料率の小数**であり、
`_RE_DATE_TOKEN = r"(?:20\d{2}[/年.\-])?\d{1,2}[/月.\-]\d{1,2}日?"` の
`.` を区切りとして許す部分が小数点を拾っている。

→ **小数を日付から除けば p8 の dates=3 < 5 となり `has_detail_rows=False`。**
p5（真の明細頁・dates=17）には影響しない。

### 2.4 PaddleOCR の欠字実態と語彙の可用性

p8 の生テキストでは「ペイフレックス」が `ペイフレックス`/`ペイレックス`/
`ペイフレクス`/`ペイフックス`/`へイクス` の 5 形態で出る。しかし
**NFKC ＋ `_collapse_spaces` ＋ 簡体字写像の後、完全形は少なくとも 1 回出現する**ので
`in` 照合は成立する。実測ヒット:

`あとリボ` ✓ / `ペイフレックス` ✓ / `変更締切日` ✓ / `基本手数料率` ✓ / `実質年率` ✓

p7 側は「ポイント」が `ポイト`/`ボイト`/`ポト` に崩れており `points_score=0`。
それでも `info_score=1` で `FAMILY_INFO_NOTICE` に落ちて去向は正しい。
**`points_score` を上げる改修は不要**（去向が同じ監査タブなので実益ゼロ）。

### 2.5 除外出口の位置（§15.1 で確定済）

`_yield_page_results` 内、`result = _build_doc_result(...)` の**後**、
`_is_line_mode` 分派の**直前**（`ocr_engine.py:2340-2343`）。
P-A（社保と同層）と P-C（`_yield_line_mode_results` 内）は §15.1 で排除済。
消費側（`main.process_file:543` の `_excluded_page` 分岐）は**無改修で足りる**（実測）。

---

## 3. 核心設計課題 —— リボ頁をどう通すか（**Codex 評審の主論点**）

優先序 3「`has_detail_rows` なら、いかなる族も除外を主張できない」は
IP-401 の保護そのものである。`_looks_like_detail_rows` の docstring も
「**除外しない方向にしか使わない**。閾値の誤りが『静かな監査タブ行』ではなく
『うるさい赤占位行』の側に倒れる」と明記している。

したがって「リボ族に否決権を与える」形の実装は**この契約を壊す**。

### 候補

| 案 | 内容 | 評価 |
|---|---|---|
| **甲** | `_RE_DATE_TOKEN` から小数を除外（`14.90` を日付と数えない）＋ リボ族を追加 | **採用案**。誤判定を消すだけで契約に触れない。p5 に影響なし |
| 乙 | リボ族を優先序 3 の**前**に挿入 | **排除**。族に否決権を与える＝IP-401 契約違反。真の明細頁が判定誤爆で呑まれうる |
| 丙 | リボ族を優先序 4 に追加するのみ | **単独では無効**。p8 は 3 で止まるので 4 に到達しない |
| 丁 | リボ強シグナル命中時だけ明細閾値を引き上げる | 条件付き閾値は「族の否決権」の変装。甲で足りるなら不要 |

**甲を採る。** ただし甲は 2 つの独立した変更から成る:

- **甲-1**: `_RE_DATE_TOKEN` の小数誤判定の修正（**それ自体が独立したバグ修正**。
  リボ頁と無関係に、手数料率や利率を印字する全ての頁で `has_detail_rows` を
  偽陽性にしている）
- **甲-2**: リボ／分割払い族の新設（甲-1 だけでは p8 は
  `_signal_family` が None を返して優先序 5 の `unrecognized` に落ちる）

---

## 4. 任務一覧（各項に DoD）

### T8-1: `_RE_DATE_TOKEN` の小数誤判定を修正（RED → GREEN）

- 変更: `page_family._RE_DATE_TOKEN` が `14.90` / `14.6` のような
  **小数を日付として拾わない**ようにする。

- **規則を明文化する**（Codex HIGH-1 を採納。実測で `1.19` と `14.90` は
  トークン単体では区別不能 —— どちらも `\d{1,2}.\d{1,2}` に合致する）:

  > **`.` 区切りは年付き `20xx.m.d` のときだけ日付とみなす。**
  > 年なしの日付は `/` `-` `年月日` の 3 形式でのみ受理する。

  根拠: 本件の参考資料 8 頁で `.` 区切りの日付は**一度も出現しない**
  （実測は全て `2023年1月19日` `1月3日` 形式）。年なし `.` 区切りを
  捨てても失うものが無く、手数料率・実質年率の小数を確実に排除できる。
  将来 `.` 区切り券面が出たら、そのとき実測を添えて緩める。
- **DoD**:
  - 先に失敗するテストを書く（`14.90` を含む文字列で日付が 0 件になること、
    `2023.1.19` は 1 件になること）
  - p8 の実文字列で `dates == 3` かつ `has_detail_rows == False` になる
  - p5 の実文字列で `has_detail_rows == True` が維持される（回帰なし）
  - **境界テスト**: `14.90` `14.6` `10.8%` → 日付 0 件 ／
    `2023.1.19` `2023.01.19` → 1 件 ／ `1/19` `1-19` `1月19日` → 1 件 ／
    **`1.19` → 0 件**（年なし `.` は捨てる、という上の規則の明示）
  - `test_page_family` 全数緑

### T8-2: リボ／分割払い族の新設（RED → GREEN）

- 変更: `page_family` に `FAMILY_INSTALLMENT`（仮称）と語彙を追加し、
  `_signal_family` が優先序 4 で拾えるようにする。去向は `EXCLUDE_DEST_AUDIT_TAB`。
- 語彙候補（**§2.4 で実測ヒット済のものだけ**）:
  `あとリボ` / `ペイフレックス` / `リボルビング払い` / `基本手数料率` /
  `あとリボ変更締切日` / `分割払い手数料`
- **単独発火を禁じる語**: `リボ` 単独（加盟店名や広告文に出うる）、
  `手数料`（あらゆる券面に出る）。社保の「納入告知額」を単独で発火させない
  裁決（CLAUDE.md）と同じ原理。**2 語以上の AND か、長い複合語のみ**。
- **DoD**:
  - p8 の実文字列 → `_signal_family` が新族を返す
  - p8 の実文字列 ＋ `entry_count=0` → `action=exclude`, `destination=audit_tab`
  - **p8 の実文字列 ＋ `entry_count=6` → `action=book`**（IP-401 gate の維持）
  - **かつ `audit_signal == "family_signal_with_entries:payment_method_notice"`**
    —— 新族を `resolve_page_disposition` 優先序 2 の audit 対象タプル
    （現在 `(FAMILY_POINTS_ONLY, FAMILY_INFO_NOTICE)`）に**必ず加える**。
    加え忘れると「リボ頁で entries が組まれた」という最も知りたい事象が
    無印で通る。記帳は止めない（Codex ラウンド 2 の最小案を採納）
  - 明細頁（p5）・ポイント頁（p7）の族が変わらない
  - 「リボ」1 語だけの文字列では発火しないことをテストで固定

### T8-3: `resolve_page_disposition` を P-B へ接線（本体）

- 変更: `ocr_engine._yield_page_results` の
  `result = _build_doc_result(doc_type, raw_data, builder(raw_data))` の直後、
  `_is_line_mode` 分派の直前に裁決を挿入する。
  - `entry_count` は `len(result.get("entries") or [])`
  - `page_class` は `PageOcr.page_class`（**引数として渡す必要がある** —— 現在
    `_yield_page_results` は `page_class` を受け取っていない。尾段（単頁 PDF・
    画像）からの呼出も同様に渡す）
  - `dedup_verdict` は `None`（T9 で接続）
  - `ACTION_EXCLUDE` なら `_blank_result(**exclusion_fields(disposition))` を
    yield して return
  - `ACTION_UNRECOGNIZED` は現行の `_unrecognized` 経路を維持

- **`_audit_signal` の合成規則**（Codex HIGH-2 を採納。**これは実在の衝突**）:
  `_with_audit_signal` は `"_audit_signal": reason` で**無条件に上書き**する
  （`ocr_engine.py`）。族シグナルを P-B で載せると、その後
  `_yield_line_mode_results` が `card_salvage` の reason で載せ直した瞬間に
  **族シグナルが消える**。逆順なら行欠けシグナルが消える。
  → **`ACTION_BOOK` の `audit_signal` は P-B で載せない。**
  `Disposition.audit_signal` を戻り値として持ち回り、`_yield_line_mode_results`
  を通った**後**の最終 result に `;` 連結で合成する。
  合成規則は 1 関数（`_merge_audit_signals`）に閉じ、両方が在るときは
  `"<family_signal>;<shortage_reason>"` の順で残す。

- **line shortage との競合**（Codex MEDIUM-1 を採納）:
  現行では entries=0 の card 頁は `_yield_line_mode_results` の
  `card_salvage.page_marks` を通り、行欠けが在れば監査痕跡が残る。
  P-B で早期 `return` すると、**判定が誤爆した頁でこの痕跡まで消える**。
  → **`ACTION_EXCLUDE` を返す前に `card_salvage.page_marks(raw_data)` を見る。
  `shortage` が非 None なら除外しない**（行欠けの疑いがある頁は
  「静かな監査タブ」ではなく現行どおり赤い占位行へ倒す）。
  これは `_looks_like_detail_rows` の docstring が宣言する
  「閾値の誤りは赤占位行の側へ倒す」と同じ方向であり、契約と整合する。
- **適用範囲**: card 系 doc_type のみ。既存 4 型は経路に入れない。
- **DoD**:
  - p7 / p8 が監査タブ行（`_excluded_page=True`, `destination=audit_tab`）になる
  - p5 は従来どおり記帳される
  - entries>0 の頁は `_excluded_page` が立たない（全 doc_type で）
  - 既存 4 doc_type の結果が 1 バイトも変わらない（特性テストで固定）
  - `main.process_file` は無改修で通る

### T8-4: 真票回帰（趙が実行）

- `local_test.py --only-file <アメックス複製>` で 8 頁を通し、
  Sheets 上で p7/p8 が `_除外ページ監査` タブへ、p1-p6 が MF タブへ入ることを目視。
- **DoD**: 仕訳件数が T8 前と同じ（p7/p8 は元々 entries=0 なので**変わらないはず**）。
  変わったら即座に回退。

---

## 5. 受入基準（機械判定できるもの）

1. `venv311/bin/python -m unittest discover -p "test_*.py"` が緑
   （現状 1059 tests。expectedFailure=1 は維持）
2. p8 の実 OCR 文字列で `resolve_page_disposition(None, 0, classify_page(t))` が
   `action=exclude`, `destination=audit_tab`
3. 同じ文字列で `entry_count=6` なら `action=book`
4. p5 の実 OCR 文字列で `has_detail_rows=True` が維持
5. 既存 4 doc_type の特性テストが無変更で通る

---

## 6. テスト戦略（TDD）

- **単体**: `test_page_family.py` に T8-1 / T8-2 のテストを追加（venv 非依存を維持 ——
  この性質は `test_dependency_weight.py` が見張っている）
- **接線**: `test_ocr_engine`（または新規 `test_page_disposition_wiring.py`）で
  `_yield_page_results` の出力を固定。実 OCR 文字列は fixture 化するが
  **顧客実名・カード番号はマスクして格納する**
- **回帰**: 既存 4 doc_type の特性テストを事前に確認し、接線後に再実行
- **接線の両経路テスト**（Codex MEDIUM-2 を採納）: `_yield_page_results` の
  直叩きだけでは足りない。`process_pipeline` レベルで
  **逐頁 PDF 経路（`:2618`）と尾段の単頁 PDF・画像経路（`:2760`）の両方**を通し、
  `PageOcr.page_class` が Disposition まで届くことを固定する。
  片方だけ接続された「半開状態」は、テストが無ければ誰も気づけない。
- **変異検証 5 種**（3 → 5。Codex MEDIUM-3 を採納。最大の壊れ筋 2 つが
  元の 3 種では生き残る）:
  1. リボ語彙を 1 つ削る
  2. 優先序を入れ替える（族を `has_detail_rows` より前に置く）
  3. `entry_count` gate を外す
  4. **尾段の呼出点（`:2760`）から `page_class` を落とす**（半開状態）
  5. **`_merge_audit_signals` を上書き実装に戻す**（族シグナルと行欠けの
     どちらかが黙って消える）

---

## 7. 影響面

| 対象 | 影響 |
|---|---|
| `page_family.py` | 語彙表 ＋ 正規表現 ＋ `_signal_family` |
| `ocr_engine.py` | `_yield_page_results` の署名（`page_class` 追加）と分派 |
| `main.py` | **無改修**（`_excluded_page` 分岐は doc_type を見ない） |
| `local_test.py` | **無改修**（部分 T11 で main と揃え済み） |
| 既存 4 doc_type | 経路に入れないので影響なし（特性テストで固定） |
| 顧客が見るもの | p7/p8 が赤い「認識不能」行 → 監査タブ 1 行に変わる。**仕訳件数は不変** |

---

## 8. リスクと回退

| リスク | 影響 | 対策 |
|---|---|---|
| リボ語彙の誤爆で真の明細頁が呑まれる | 会計データ欠落（最悪） | 優先序 2（entries>0 は絶対 book）が構造的に防ぐ。加えて 2 語 AND を強制 |
| `_RE_DATE_TOKEN` の修正が真の日付を落とす | 明細頁が `has_detail_rows=False` になり除外側へ倒れうる | p5 実文字列の回帰テストで固定。`2023.1.19` 形式のテストを明示 |
| 尾段（単頁 PDF・画像）に `page_class` を渡し忘れる | その経路だけ裁決が効かない半開状態 | **実施中に方針変更（2026-08-19）**: 当初は「必須引数にして構文エラーにする」としたが、`_yield_page_results` を位置引数で呼ぶ既存テストが **9 ファイル**あり、全面改修は本題と無関係な差分を生む。**`page_class=None` の任意引数 ＋ AST 番人テスト**（生産の 2 呼出点が両方とも渡していることを構文木で検査）に変更。防漏効果は同等で、既存テストを 1 行も触らない。手法は `test_local_test_folder_map.py` の先例に倣う |
| 他発行体のリボ頁が別版面 | 判定が効かず従来どおり赤行 | 実害は「現状維持」。段階的に語彙を足す |
| **他発行体のリボ頁で Gemini が偽 entry を組む** | T8 は**止めない**（優先序 2 により必ず記帳される） | **欠陥ではなく IP-401 契約の代価**（族に頁を落とす権限を与えない設計そのもの）。**T8 では検知しない。検知は T9 の `card_reconciliation` が担う** —— 偽 entry があれば明細合計が印字合計を超えて検算不一致になる。T8 で新機構は作らない。⚠️ `anomaly_detector` の高額タグは**科目依存で兜底として不完全**（汎用の高額ルールは無く `修繕費>30万`・`備品・消耗品費>10万` の 2 本だけ。2026-08-19 実測）。ただし credit_card の `default_debit` は **`備品・消耗品費`** なので、既定科目に落ちた偽 entry は黄系が付く —— 効くのは Gemini が別の正当科目を出さなかった場合に限る |
| 支払方式族の頁で entries が組まれたとき痕跡が残らない | 偽 entry が無印で記帳される | **`FAMILY_PAYMENT_METHOD_NOTICE` を優先序 2 の `audit_signal` 対象に含める**（T8-2 の DoD）。記帳は止めず監査タブに「分岐」1 行 |
| 族シグナルと行欠けシグナルの一方が消える | 監査タブから痕跡が黙って消える | `_merge_audit_signals` に合成を閉じ、変異 5 で殺せることを確認する |

**回退**: 全て純追加なので `git revert` 1 発。生産は miniPC の手動 pull なので
pull しない限り影響ゼロ。

---

## 9. 趙の拍板が要る事項

| # | 論点 | 私の推奨 |
|---|---|---|
| 1 | 要求 #4（主副カード各人小計の兜底）を T8 に入れるか | **入れない → T8b として切る**（趙裁定 2026-08-19）。除外ロジックと直交 |
| 2 | 補助科目の頁級抽出＋全行配布を T8 に入れるか | **入れない → T8c として切る**（趙裁定 2026-08-19）。別軸の改修 |

### 後続タスクの実行順（趙提示 2026-08-19。本 Plan の範囲外）

| 標籤 | 内容 | 備考 |
|---|---|---|
| **T8b** | #4 小計行の兜底（簡易版: 左端日付欄が空 ＋ ラベルに「合計」→ entry を作らない） | **T9 の検算では兜底できない**。主副カード合印は P1「合計不整合」で検算自体が沈黙するため（§15.4）。完全版（各カードの小計と総額の区別）は T9 の按カード分桶が要る |
| **T8c** | 補助科目（カード名）の頁級抽出＋全行配布 | 出力品質の改善。錯帳防止ではないので T8b の後 |
| **T9** | 検算接線（P1 ＋ P2 の expectedFailure 解除）＋ `page_dedup` 接線 | `RiboPageZeroBaselineTest` の装飾子を外すのが完了条件の 1 つ |
| 3 | 新族の名前 | **`FAMILY_PAYMENT_METHOD_NOTICE`**（支払方式の案内頁）。去向は 1 つで足りるが、`signals` には `revolving` / `installment` を**別々に**残して将来の分析に備える（Codex LOW-1 を修正採納 —— family を 2 つに割ると去向が同じなのに分岐が増える） |

---

## 10. 辯論記録（Codex 評審 ラウンド 1 — 2026-08-19）

Codex の総合判定は **NO（このままでは実装に入るな）**。先に Plan の事実主張
4 件を実際にコードを読んで検証させ、**4 件とも「正しい」**と確認された上での指摘。

| # | 深刻度 | 指摘 | 裁決 | 根拠 |
|---|---|---|---|---|
| 1 | HIGH | `.` 区切り日付の仕様が未定義（`1.19` と `14.90` は区別不能） | **採納** | Codex が実機で `_RE_DATE_TOKEN.findall` を実行し提示。§4 T8-1 に規則を明文化 |
| 2 | HIGH | `_with_audit_signal` が `_audit_signal` を無条件上書きし、族シグナルと行欠けシグナルが食い合う | **採納** | 自分で `ocr_engine` を読んで確認。実在の衝突。§4 T8-3 に `_merge_audit_signals` を追加 |
| 3 | HIGH | 「entries>0 は絶対に除外しない」は不正確（duplicate と社保が前段にある） | **採納** | `page_family.py:459` / `ocr_engine.py:2272` を確認。§1 目標 4 の主語を「族シグナル」に限定 |
| 4 | MEDIUM | P-B の早期 exclude が line shortage の監査痕跡を消す | **採納** | `_yield_line_mode_results` を読んで確認。§4 T8-3 に `card_salvage.page_marks` の事前確認を追加 |
| 5 | MEDIUM | 尾段を含む接線テストが無い | **採納** | §6 に両経路テストを追加 |
| 6 | MEDIUM | 変異 3 種では最大の壊れ筋を殺せない | **採納** | §6 を 5 種へ |
| 7 | MEDIUM | 他発行体で Gemini が偽 entry を組むと T8 は止めない。T9/reconciliation での検知方針を書け | **全面採納**（当初は後半を駁回したが**自分の検証で撤回**） | 下記 |
| 8 | LOW | `FAMILY_INSTALLMENT` は広すぎる。族を分けるか signals に残せ | **修正採納** | 族は 1 つ（去向が同じなのに分岐を増やさない）＋ signals に `revolving`/`installment` を別々に残す |
| 9 | LOW | 補助科目を次タスクとして明示せよ | **採納** | §9 に **T8c** として明記（趙 2026-08-19 の排期裁定で T8b は #4 に割当）|

### #7 後半について: **一度駁回し、自分の検証で撤回した**

当初こう駁回した:

1. これは欠陥ではなく IP-401 契約の代価である
2. **既存の兜底が効く** —— `anomaly_detector` の高額検知が黄系タグを付ける
3. 未観測の事象への先行設計は過度設計である

**理由 2 は誤りだった。** 回餵の直前に自分で `anomaly_detector.py:32-120` を
読んで検証したところ、**汎用の高額ルールは存在しない**。あるのは
`修繕費 > 300,000`（`:67`）と `備品・消耗品費 > 100,000`（`:98`）の
**科目限定 2 本だけ**。リボ偽 entry の 1,500,000 円は、科目がこの 2 つで
なければ**何のタグも付かない**。真票で 30 万・58.7 万に黄系が出たのは、
それらの科目がたまたま該当していたからで、金額の大きさが理由ではない。
（`UNKNOWN_ACCOUNT` に落ちれば medium は付くが、それは保証ではない。）

**兜底が当てにならないと判ると、正しい答えが見える**: この風険は
**`card_reconciliation` が構造的に覆う**。Gemini が存在しない 150 万円を
entry 化すれば、明細合計が印字合計を 150 万円超過し、検算が不一致になる。
検算は T9 で接線する。つまり Codex の言う「T9/reconciliation で検知」は
**新機構ではなく既存設計の被覆関係の指摘**であり、過度設計にはあたらない。

→ **#7 を全面採納**。§8 のリスク表に「検知は T9 の検算が担う」と
「`anomaly_detector` の高額検知は当てにならない」の 2 点を明記した。

理由 1 と 3 は維持する（**新機構は作らない**。T8 で優先序 2 を迂回する
第 2 の裁決者を作らないという判断は変わらない）。

### Codex 複審（ラウンド 2）の結果

**核心の争点は我方の勝ち**: Codex は「#7 後半は『T8 で止めるな』は**あなたが正しい**」
と明言し、以後この点を再提起しなかった（fatboyslim の勝敗判据）。
ただし 3 点の修正を受け入れた:

| 指摘 | 裁決 | 内容 |
|---|---|---|
| 理由 1 の「検知方針を書く＝第 2 の裁決者」は**言い過ぎ** | **採納** | `Disposition.audit_signal` は `ACTION_BOOK` を保ったまま監査へ注記する仕組みで、裁決者ではない（`page_family.py:477`）。裁決者を増やさずに痕跡を残す道は在る |
| 理由 2 は誤り（**私も回餵前に自力で撤回済**） | 一致 | Codex は追加で `anomaly_detector` が **line_mode でも発火する**ことを `sheets_output.py:432` で確認し、6 科目の実測表を提示。汎用高額ルールが無い点は両者一致 |
| 最小案: 新族を優先序 2 の `audit_signal` 対象に含めるだけ | **採納** | T8-2 の DoD に追加。T9/reconciliation には何も新設しない |

**総合判定: YES（実装に入ってよい）** —— 理由 2 とリスク表文言の修正が前提であり、
どちらも本 Plan に反映済み。

**教訓**: 対抗者に回餵する前に、自分の論拠を自分で検証すべきだった。
理由 2 は memory の記述（「真票で 30 万・58.7 万に黄系」）から
「汎用の高額ルールがある」と**推測して書いた**もので、コードを読んでいなかった。
[[adversarial-agreement-is-not-verification]] と同根の失敗（一次資料を読まずに
主張を組み立てた）。

### 全採納でも全駁回でもないことの確認

9 件中 8 件を全面採納、1 件（#8 族名）を修正採納。当初 #7 後半を駁回したが
自分の検証で撤回した（上記）。**駁回がゼロになったのは失職の結果ではなく、
駁回理由の 1 つが自分の検証で崩れたため**——経緯を残すことで、次に
同じ推測を根拠にしないようにする。
採納したもののうち #2 と #4 は**自分でコードを読んで再確認**しており、
Codex の主張をそのまま信じた項目は無い（2026-08-19 の教訓
[[adversarial-agreement-is-not-verification]] の適用）。
