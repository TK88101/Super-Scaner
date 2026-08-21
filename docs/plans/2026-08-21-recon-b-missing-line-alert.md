# Plan: ファイル単位の突合で「行が無音で消える」のを検出する（B 案）

**状態**: Codex 評審 2 ラウンド裁決済み。**定稿（趙の承認待ち）**
**日付**: 2026-08-21
**前提**: `origin/main = aeb5c2d`、作業樹 clean、全量 1390 tests 緑 + expectedFailure 2 件
**位置づけ**: 母 Plan `2026-08-20-recon-wiring.md` の**第 2 部を置き換える**
（第 1 部＝インボイス再掲行の除外は実装・push 済み `920c75e`）

---

## 0. 趙の裁定（2026-08-21）—— スコープの外枠

本 Plan は趙の次の指示から出発する。**設計判断で迷ったらここへ戻ること。**

> 「この信用卡項目は、掃描件に何の名目が載っているかを SS が如実に抓えて
> Google Sheet に反映できればそれでよい。正しいか否かは後続で人が査験・修正する」

したがって:

- **記帳の正しさは判定しない。** 税区分・勘定科目・税法判断には一切触れない。
- 検算の唯一の存在理由は「**行や頁が無音で消えるのを防ぐ**」こと。
  人は「金額が違う」は目視で気づけるが、「本来あるはずの行が無い」には
  気づけない（CLAUDE.md の IP-401: 54 枚上げて仕訳 53 件、枚数を数えて初めて判明）。
- 出力は「このファイルは突合できない、人が見てほしい」の 1 行だけ。
  どのカードが・どの明細書が、までは言わない。

---

## 1. 実測した事実（本 Plan の土台。すべて再現手順つき）

### 事実 1: カード番号で束ねると偽の不一致が 7 件出る

`fixtures/full`（真票 8 ファイル 33 頁）に対し、実管線の中で読み取り専用に
`FileReconLedger` を空回しした（T2-0。`--replay` なので Gemini 課金ゼロ）。

結果は **一致 8 / 不一致(頁欠の可能性) 6 / 不一致 1 / 重複頁 1 / 件数不一致 1**。

不一致 7 件を原票の数字で検算すると、**金額の誤りは 1 件も無い**。全部が分組起因:

| 故障型 | 件数 | 証拠 |
|---|---|---|
| 表紙頁が独立したカードになる（明細 0 行なのに券面合計を背負う） | 2 | ENEOS p1、UC p1 |
| 1 通の明細書が複数カードに割れる | 5 | JCB: 152,154 + 5,072 = **157,226 = 券面**<br>TS CUBIC: 53,290 + 71,400 + 7,160 = **131,850 = 券面** |

母 Plan R9 が心配した「常時不一致で狼少年」が実測で現実化した。

### 事実 2: ファイル単位で足せば、分組せずに等式が成立する

同じ録音に対し、**カードで束ねず**ファイル全体で足すと:

| ファイル | 券面合計の和 | 明細和 | 判定 |
|---|---|---|---|
| ENEOS | 15,503 | 15,503 | 一致 |
| TS CUBIC | 44,490 + 97,004 + 131,850 = 273,344 | 273,344 | 一致 |
| アメックス | 17,295 + 933,680 = 950,975 | 950,975 | 一致 |
| アレコレ | 54,788 | 54,788 | 一致 |
| JCB / UC / 楽天 | （ラベル未登録で基準を取れない） | — | 検算不能 |
| nimoca | （券面に金額合計が無い。件数のみ） | — | 検算不能 |

**一致 4 / 不一致 0 / 検算不能 4。偽警報ゼロ。**

TS CUBIC は 1 つの PDF に明細書が 3 通入っているが、**3 通の券面合計を足すと
全ファイルの明細和に一致する**。つまり明細書ごとに分ける必要が無い。

### 事実 3: 「前期の清算行を外す」は既に実装されている

アメックス p5 の `前回分口座振替金額 -35,808` は `kind=carry_over` と判定され、
`_is_bookable` が False を返す（`card_reconciliation.py:560-563`）。
一方 ENEOS の `ENEOS店頭キャッシュバック -3,000` は `kind=credit_adjust` で
True を返す。**当期調整と前期清算の区別は既存コードが正しく行っている。**

判定は `card_entries.resolve_booking_kind`（`card_entries.py:302-338`）の
日本語ラベル白名単 `_CARRY_OVER_LABELS` による。

### 事実 4: raw_data は消費側に渡っていない

`_build_doc_result`（`ocr_engine.py:1902-1959`）が固定キーだけを拾って
result を組むため、`raw_data` は producer の外へ出ない。検算 DTO を作る
3 つの関数（`card_ident_from_raw` / `printed_totals_from_raw` /
`detail_lines_from_raw`）は raw_data のみを入力とするので、
**観測は producer 側でしか行えない。**

同様に `is_duplicate` も `page_family.Disposition` の属性で、result dict には
載らない（載るのは `_exclude_reason` の文字列のみ）。しかも
`page_family.py:255-261` の注釈は「理由文字列の前綴で判定するな、
そのためにこの旗がある」と明記している。**重複頁の除外も producer 側でやる。**

### 事実 5: 監査タブは既に同じ形の行を書いている

`main.py:623-637` が頁カバレッジ突合の結果を
`verdict=AUDIT_VERDICT_MISSING("欠落")` で監査タブへ 1 行書いている。
**同じタブ・同じ 7 列**（`sheets_output.py:30` の `AUDIT_HEADERS`）。
B 案は verdict の値を変えるだけで、新しい機構を作る必要が無い。

### 事実 6: 曖昧ラベルは「敢えて登録しない」という既存裁定がある

`card_reconciliation.py:93-96`:

> 「今回ご請求金額」「今回のお支払金額」「ご請求金額」は発行体によって指す区画が
> 違う（曖昧）ので敢えて登録しない。

さらに `test_card_reconciliation.py:691` の
`test_ambiguous_labels_are_deliberately_unmapped` が、後人がこれらを表へ
足すことを禁じている。**この裁定を無言で覆さない。**

事実 2 の「検算不能 3 件」（JCB / UC / 楽天）はこの裁定の帰結である。
**覆さずに受け入れる。** 検算不能は報警しないので実害は「検証しない」だけ。

---

## 2. 目標 / 非目標

### 目標

1. 1 ファイルの処理が終わった時点で「券面の請求合計の和」と「明細行の和」を
   突合し、**食い違ったら監査タブへ 1 行**書く。
2. 判定できない場合（ラベル未登録・券面に合計が無い）は**何も書かない**。
3. 記帳経路を 1 バイトも変えない。検算が落ちても記帳は続く。
4. 既存の `FileReconLedger`（カード単位）には**触れない**。

### 非目標（やらないこと。逸脱防止のため明示する）

1. **カード単位・明細書単位の判定**。「どのカードが」は言わない。
2. **税区分・勘定科目の判定**。軽油引取税を含め、記帳の正しさは対象外
   （趙が 2026-08-21 に別途裁定）。
3. **PaddleOCR による合計の独立抽出**（双通道交差検証）。
   「正しさの証明」は目標でないため不要と趙が裁定。
4. **曖昧ラベルの表への追加**。事実 6 の既存裁定を維持する。
5. **遡及標色**（母 Plan AD-7 で駁回済み）。
6. **`RECON_TOLERANCE_YEN` の引き上げ**（既知の差を隠すことになる）。
7. **既存の `FileReconLedger` / `normalize_card_key` の改修**。
   影響半径が `_only()` の 45 箇所 + `CardIdentityTest` 13 メソッドに及ぶ。
   B 案はそこを通らずに目的を達成できる（事実 2）。

---

## 3. 設計裁定

### AD-1. `FileReconLedger` には触れず、ファイル単位の別モジュールを作る

**裁定**: 新規 `card_file_recon.py` を作り、既存 `card_reconciliation.py` は
1 行も変えない。

**理由**: B 案が要るのは「ファイルが突合できるか」の 1 ビットだけで、
カード単位の 17 フィールドの verdict ではない。既存元帳を改造すると
`test_card_reconciliation.py` の `_only()`（裸 `assert len(verdicts)==1`）
45 箇所が AssertionError で落ちる。エラーメッセージにテスト意図が出ないので
排査コストが高い。**目的に対して払う代償が釣り合わない。**

**捨てるもの**: カード単位の細かい報告。B 案の非目標なので損失ゼロ。

### AD-2. 突合の式と口径を凍結する

```
判定対象 = ファイル 1 件

  左辺（明細和）= Σ DetailLine.amount
                  ただし is_duplicate な頁を除き、
                  かつ _is_bookable(kind) が True の行のみ
                  （＝ carry_over を外す。charge は config 既定で外す）

  右辺（券面合計）= Σ ただ 1 つずつの PrintedTotal.amount
                    ただし section_for_label(label) == SECTION_CURRENT_USAGE
                    かつ is_handwritten でないもの
                    **同一金額は 1 回だけ数える**（去重）

  一致 ⇔ 左辺 == 右辺（許容差 0 円。RECON_TOLERANCE_YEN を流用する）
```

**去重の理由**: 同じ合計が複数頁に印刷される（ENEOS の 15,503 は p1 と p2 の
両方に、TS CUBIC の 131,850 は p5 と p9 の両方に印字される）。去重しないと
必ず 2 倍になる。

### AD-2b. 同額の合計は「頁付の連続段」で消歧する（Codex 評審 高-1 を容れて追加）

**Codex の指摘（採用）**: 素朴な金額去重は**偽の不一致だけでなく偽の一致も作る**。
1 ファイルに 2 通の明細書があり両方とも券面合計 10,000 円のとき、
2 通目の明細が丸ごと落ちると 左辺 10,000 / 右辺 10,000 で**一致してしまう**。
これは本 Plan の唯一の目的（行が無音で消えるのを防ぐ）を正面から破る。

**裁定**: 同額の `current_usage` 合計が複数回現れたら、
**券面が自ら印字している頁付（n/N）の連続段**が同一かどうかで消歧する。

```
段の定義: 頁を順に見て、n == 1 または n <= 直前の n のとき新しい段を開始する。
          （券面 1 通が 1/N .. N/N を名乗る単位）

去重は**段の内側だけ**で行う:
  同一段の中に現れた同額 → 同じ明細書の再印字。1 回だけ数える
  段が違えば同額でも**別々に数える**（潰さない）

検算不能へ倒す条件:
  - いずれかの頁で n が読めない
  - 同一段の中で N が読めていて途中で変わる
```

**当初この Plan は「別段にまたがる同額は検算不能」と書いていたが、
それでは Codex の反例が監査タブに何も出さず、目的上検出できない**
（複審で当方の記述矛盾として指摘された。Codex 勝ち、その修正案に従う）。
正しくは「別段なら潰さず両方数える」。同額 10,000 が段 A と段 B に出たら
右辺は 20,000 になり、1 通が丸ごと欠落した左辺 10,000 と食い違って
**正しく不一致になる**。

**実測（`fixtures/full`）**:

| ファイル | 同額の合計 | 出現頁の n/N | 段 | 判定 |
|---|---|---|---|---|
| ENEOS | 15,503 × 2 | p1=1/2, p2=2/2 | 同一 | 去重 → 一致 |
| TS CUBIC | 131,850 × 2 | p5=1/5, p9=5/5 | 同一 | 去重 → 一致 |
| アメックス / アレコレ | 同額なし | — | — | 去重の出番なし |

Codex の構築した反例（2 通が偶然同額）では両者が別段に落ちるので**潰されず**、
左辺 10,000 / 右辺 20,000 で**正しく不一致になる**。

**この規則は分組ではない。** 段は「同額を潰してよいか」の判定にだけ使い、
明細行の帰属には一切使わない（AD-1 の「分組しない」は維持される）。

**採らなかった案と理由**:
- 「同額が複数出たら常に検算不能」（Codex ラウンド 1 の当初案）: 実測で ENEOS と
  TS CUBIC が両方とも検算不能へ落ち、覆盖率が 4/7 → **2/7** になる。
  最も頁数が多く最も漏れやすい TS CUBIC が検査対象から外れるのは本末転倒。
  **複審で Codex 自身もこの案を取り下げ、段内去重を支持した。**
- 「n==1 の出現回数で明細書の通数を数える」: TS CUBIC の p1/p2 が両方
  `1/1` を名乗るため 4 通と数えてしまう（実際は 3 通）。**実測で否決。**

### AD-3. 観測は producer 側、判定と書込は消費側

**裁定**: `ocr_engine._yield_page_results` で頁ごとの観測値を作り、
`process_pipeline` が yield する dict に `_file_recon` キーで載せる。
消費側（`main` / `local_test`）は EOF でそれを畳んで判定し、監査タブへ書く。

**理由**: 事実 4 のとおり `raw_data` と `is_duplicate` は producer にしか無い。
一方、監査タブへの書込と「ファイルが歸檔されるか」の判断は消費側にしか無い。

**`_file_recon` に載せるもの**（DTO ではなく**畳んだ数値だけ**）:

```python
{"detail_sum": int,          # この頁の bookable な明細和
 "totals": ((label, amount), ...),   # この頁の current_usage 印字合計
 "is_duplicate": bool}
```

raw_data そのものは載せない（メモリと、消費側が勝手に再解釈するのを防ぐ）。
頁付 `(n, N)` も併せて載せる（AD-2b が要求する）。

**1 物理頁につき `_file_recon` は最大 1 個**（Codex 評審 中-3 を容れて追記）。
`_yield_line_mode_results`（`ocr_engine.py:2548`）は明細 result の後に
行欠け提示用の `_blank_result` を**追加で yield する**ので、素朴に
「全 result に載せる」と同じ頁を二重に数える。載せるのは**先頭 result のみ**とし、
消費側も同一 `page_num` の二重 observation を拒否する。両方を番人で固定する。

**記帳に使う値は 1 バイトも変えない。** `entries` の snapshot 番人で固定する
（母 Plan T2-3 で Codex が指摘した点をそのまま引き継ぐ）。

### AD-4. 未知ラベルは請求合計に**しない**

**裁定**: `section_for_label` が `SECTION_CURRENT_USAGE` を返さないラベルは
右辺に加算しない。結果として右辺が空なら「検算不能」とし、**何も書かない**。

**理由**: 趙の指示「漏らす側へ倒す、誤報はしない」。誤って合計に足すと
偽の不一致になり、狼少年になる。足さずに検算不能へ倒れれば、
失うのは「検証しない」だけ。

**帰結**: 現時点の覆盖率は信用卡 7 ファイル中 4 件（事実 2）。
これは失敗ではなく保守的な出発点である。ラベルは実票で育てる（**TBD-2**）。

### AD-5. 検算不能・一致のときは監査タブに何も書かない

**裁定**: 書くのは「不一致」のときだけ。

**理由**: 監査タブは人が見る場所であり、「異常なし」の行で埋めると
本物の異常が埋もれる。母 Plan R9 と同じ論理。

**ただし運用者向けには常時ログを出す**（複審で Codex 勝ち。当方の
「回帰テストで分布を見る」案は本番の壊れ方を捉えられないと論証された）。

顧客が見る監査タブの方針は変えない（不一致のみ）。変えるのは**コンソール**で、
ファイル 1 件につき 1 行:

```
file_recon: target=credit_card verdict=match printed=273344 detail=273344
file_recon: target=credit_card verdict=unverifiable reason=label_unmapped
```

**理由**: `_file_recon` が載らない / 全件検算不能になる / finalize が呼ばれない、
という壊れ方は、監査タブにも回帰テストにも現れず**完全に無音**になる。
検算器自身が無音で死ぬのは、検算器が防ごうとしている事故と同じ形である。

**TBD-3 へ送らない。** B-4（本番経路）の DoD に入れる。

### AD-6. `finalize` を呼ぶのは「ファイルが歸檔される経路」だけ

**裁定**: `main.process_file` が `return True`（`main.py:711`）へ向かう経路のみ。
`count == 0`（`main.py:713-722`）と全頁失敗（`main.py:644-654`）では呼ばない。

**理由**: 保持されたファイルは 3 秒後に再スキャンされる。毎回書くと
監査タブに同じ行が無限に増える（CLAUDE.md §3 の冪等要求）。

**部分失敗（`main.py:658`）は歸檔されるので呼ぶ。** ただし失敗頁の明細は
Sheets に書かれていないので左辺が必ず不足する。→ **`error_pages > 0` なら
判定せず「検算不能」に倒す**（誤った診断名で人の時間を溶かさない）。

**実装位置を行で指定する**（Codex 評審 中-4 を容れて追記）:
全頁失敗判定（`main.py:644-654`）と部分失敗の占位行（`main.py:658-677`）の**後**、
`progress.file_finished(...)` / `return True`（`main.py:702-711`）の**直前**。
`if count > 0:`（`main.py:679`）ブロックの内側に置く。
**`count==0` と `error_pages==count` で呼ばれないことを番人テストで固定する**（必須）。

### AD-7. 検算の失敗で記帳を止めない（fail-open）

**裁定**: 判定関数も書込も `try/except` で包み、例外は print して握る。

**理由**: 検算は検査器であって記帳経路ではない。ここで落ちて客の帳簿が
書かれないのは本末転倒。既存 `FileReconLedger.observe_page`
（`card_reconciliation.py:352-370`）と同じ方針。

### AD-8. 監査タブの既存 7 列を使い、新しい verdict 語彙を 1 つだけ足す

**裁定**: `append_audit_row(filename, page_num, verdict, reason, ocr_text_len, source_url)`
をそのまま使う。

| 列 | 入れる値 |
|---|---|
| 判定 | `AUDIT_VERDICT_TOTAL_MISMATCH = "合計不一致"`（新設。既存の 除外/分岐/欠落 と並ぶ） |
| ページ | `0`（ファイル単位の行なので特定の頁ではない） |
| 理由 | 機械可読キー。`audit_reason_ja` が日本語へ訳す（`sheets_output.py:290, 926`） |
| OCR文字数 | `0`（ファイル単位なので使わない） |

**理由**: 事実 5 のとおり同じ形の行が既にある。列を増やすと
`AUDIT_HEADERS` の検証（`sheets_output.py:807-812`）に弾かれる。

**新しいキーの訳語を `audit_reason_ja` に登録すること。登録漏れは番人で塞ぐ**
（登録漏れ＝理由列が機械可読キーのまま顧客の目に触れる）。

### AD-9. ラベル規則は「先に決めて、後で標本に当てる」

**裁定**: 右辺に採用するラベルの集合は既存の `TOTAL_LABEL_SECTION` の
`SECTION_CURRENT_USAGE` 群を**そのまま流用し、1 つも足さない**。

**理由**: 起案者は §1 の実測を見た後にこの Plan を書いている。ラベルを
新しく選ぶと「結果に合わせて規則を作った」ことになり、標本が検証ではなく
訓練データに堕ちる（趙の指摘 2026-08-21）。**既存の表をそのまま使えば、
選択の自由度がゼロなので過学習しようがない。**

ラベルを足す必要が生じたら、それは本 Plan の範囲外の別作業とし、
**足す前に「その発行体でその語が何を指すか」を原票で確認する**手順を踏む。

### AD-10. `_is_bookable` の重複実装は「漂移検知の番人」で守る（Codex 評審 中-5）

**Codex の指摘（部分採用）**: `_is_bookable` は `FileReconLedger` の private method
（`card_reconciliation.py:560`）。新モジュールが同じ判定を独自実装すると将来 drift する。
Codex は公開純関数への抽出を提案した。

**裁定**: 抽出は**しない**。`card_reconciliation.py` の diff 0 は本 Plan の
中核的な約束（AD-1）で、それを崩すと `_only()` 45 箇所の risk を再び抱え込む。

**代わりに漂移検知の番人を置く**: `KIND_ALL` の全値について
`card_file_recon` 側の判定と `FileReconLedger._is_bookable` の判定が
一致することを断言するテストを書く。これは本 repo の既存様式
（`test_card_reconciliation.py:672-735` の `LabelTableDriftTest` が
`TOTAL_LABEL_SECTION` と `page_family._TOTAL_LABEL_TOKENS` の差集合を見張る形）
と同型で、新しい発明ではない。

**代償**: 判定ロジックが 2 箇所に存在する。番人が咬むので静かな drift は起きないが、
将来 `KIND_*` が増えたときは 2 箇所を直す必要がある（番人が赤くなって教える）。

**複審で追加された必須条件**: `KIND_CHARGE` の扱いは `_book_charge_rows`
（config `TRANSIT_IC_BOOK_CHARGE_ROWS`）に依存する。番人は既定値だけでなく
**`True` / `False` 両方の ledger インスタンス**で比較すること。
片側だけ見ると設定を変えた瞬間に静かに drift する。

複審の裁定は**維持**（公開純関数への抽出はしない）。

---

## 4. タスク一覧（各項に DoD）

### B-1. `card_file_recon.py` の新設（純関数のみ）

ファイル単位の突合を行う純関数群。Sheets も Drive も Gemini も触らない。

```python
PageObservation = NamedTuple("PageObservation",
                             [("detail_sum", int),
                              ("totals", tuple),      # ((label, amount), ...)
                              ("is_duplicate", bool)])

FileVerdict = NamedTuple("FileVerdict",
                         [("verdict", str),           # 一致 / 不一致 / 検算不能
                          ("printed_total", "Optional[int]"),
                          ("detail_sum", int),
                          ("diff", "Optional[int]"),
                          ("reason_key", str)])       # 監査タブの理由列へ

def observe_page(raw_data, doc_type, is_duplicate) -> PageObservation
def finalize(observations, doc_type, had_page_errors=False) -> FileVerdict
```

**DoD**:
- `observe_page` は `card_entries` の既存 3 関数だけを使い、独自の解釈を持たない
- `finalize` は AD-2 の式を逐字実装する。**同額の去重は AD-2b の段判定を通す**
  （素朴な金額集合の去重は禁止。Codex 評審 高-1/高-2）
- `PageObservation` に頁付 `(n, N)` を持たせ、段の判定に使う
- **段判定の異常系を明文化してテストする**（複審の指摘）:
  ① `n` が不読の頁があれば検算不能
  ② 同一段内で `N` が読めていて途中で変わるなら検算不能
  ③ `n` が後退して新段になった場合、同額は別段として数える。
     ただし `n` の誤読で偽不一致になりうるので、
     **「同額の再印字らしき頁で n だけが不整合」の合成テスト**を必ず置き、
     偽不一致が出るなら検算不能へ倒す条件を追加する
- `doc_type` の `RECON_POLICY` が `count_only` / `n/a` なら常に「検算不能」を返す
  （nimoca は券面に金額合計が無い。件数比較は本 Plan の対象外）
- `had_page_errors=True` なら判定せず「検算不能」（AD-6）
- 右辺が空なら「検算不能」（AD-4）
- 例外を外へ出さない（AD-7）
- **単体テストが先に赤くなることを確認してから実装する**

### B-2. 番人テスト 3 本

**DoD**:
- `card_file_recon` が独自のラベル表を持たないことを AST で固定する
  （既存 `test_gemini_record_replay.ProductionIsolationTest` と同型）
- `AUDIT_VERDICT_TOTAL_MISMATCH` の訳語が `audit_reason_ja` に登録されて
  いることを固定する
- `card_reconciliation.py` と `test_card_reconciliation.py` の diff が 0 行で
  あることを CI 相当で確認（本 Plan では手動確認で可）

曖昧ラベル 3 語の禁止テストは既にある（`test_card_reconciliation.py:691`）ので
**足さない**。

### B-3. producer 側の観測（`ocr_engine`）

**DoD**:
- `_yield_page_results` が `_file_recon` キーを result に載せる
- `_excluded_page` の頁（封筒・重複・合計表）にも載せる —— 重複頁は
  `is_duplicate=True` で載せる（消費側が数えないため）
- **`entries` の snapshot 番人**: `builder(raw_data)` の戻り値が
  `_file_recon` 付与の前後で逐字同一であること
- 既存の `test_ocr_engine_*` が全緑

### B-4. 消費側の接線（`main` / `local_test`）

**DoD**:
- 両方が `card_file_recon.finalize` を呼ぶ。**同じ関数を呼ぶことを番人が縛る**
- `main` は `return True` の経路のみ（AD-6）。`count==0` と全頁失敗では呼ばない
- 書込順序は MF タブが先、監査タブが後
- 書込失敗は print のみ（既存 `main.py:636-637` と同語義）
- `local_test` は `--replay` で動く（Sheets 書込は既存の writer 経由）
- **両経路とも verdict を 1 行 print する**（AD-5 の運用者向けログ）。
  一致・検算不能・不一致のすべてで出す（監査タブと違い、ここは常時）

### B-6. `benchmark_ocr.py` が壊れないことの確認（Codex 評審 低-7）

`benchmark_ocr.py:107` は `process_pipeline` の result をそのまま集める
**第 3 の消費者**である。未知キーは無視されるので危険は低いが、
「判っている消費者」を TBD に残す理由が無い。

**DoD**: `_file_recon` キーが精度比較の集計に混入しないことを単体テストで固定する。
設計判断ではなく確認作業なので、実装と同じ回で片づける。

### B-5. 真票回帰（`--replay fixtures/full`。課金ゼロ）

**DoD**:
- 8 ファイルを流し、監査タブへ書かれる「合計不一致」行が **0 行**であること
  （§1 事実 2 のとおり、一致 4・検算不能 4 で不一致は無い）
- 仕訳の中身が改修前と**逐字同一**であること（差は 取引No / 作成日時 のみ）
- Gemini 呼出 0 回であること
- **verdict の分布を出力する**（対象ファイル数 / 一致 / 検算不能 / 不一致）。
  機構が壊れて全件「検算不能」になったとき、監査タブは無音のままなので
  この分布だけが唯一の気づき手段になる（Codex 評審 中-6）。
  期待値は「対象 8 / 一致 4 / 検算不能 4 / 不一致 0」

---

## 5. 受入基準

1. 全量テスト（`unittest discover`）が **1390 + 新規分**で緑、
   expectedFailure は **2 件のまま**（増減とも不可）
2. 真票回帰で「合計不一致」行 0 行、仕訳の逐字同一
3. `card_reconciliation.py` の diff が **0 行**
4. `test_card_reconciliation.py` の diff が **0 行**
5. 変異検証: 意図的に 1 行削った raw_data を食わせると「不一致」が出ること
   （番人が本当に咬むことの自証）

---

## 6. テスト戦略（TDD）

1. **RED**: `test_card_file_recon.py` を先に書く。
   §1 事実 2 の 8 ファイル分の観測値を**手で書いた合成データ**として与え、
   期待 verdict を固定する（真票そのものは使わない。fixtures は PUBLIC repo に
   入れられないため、テストは合成データで自足する）
2. **GREEN**: `card_file_recon.py` を実装
3. **番人**: B-2 の 3 本
4. **統合**: `main` / `local_test` の接線を mock writer で検証
5. **E2E**: `--replay fixtures/full`（B-5）

---

## 7. TBD（本 Plan では決めない。実装前に趙へ諮る）

- ~~**TBD-1**: 同額の合計をどう扱うか~~ → **AD-2b で決着**（Codex 評審 高-1/高-2）。
  TBD のまま実装に進むのは blocker だという Codex の指摘を容れ、
  段判定という具体解を置いた。**未決事項ではなくなった。**
- **TBD-2**: JCB / UC / 楽天 のラベルを表へ足すか。足せば覆盖率が
  4/7 → 7/7 になるが、事実 6 の既存裁定に触れる。**別 Plan とする。**
- ~~**TBD-3**: 「検算が働いた」ことの可視化~~ → **AD-5 の運用者向けログで決着**
  （複審で Codex 勝ち）。進捗タブへの常時表示は依然として本 Plan の範囲外だが、
  コンソールログで「無音で死ぬ」形は塞いだ。
- **TBD-5**（simplify 評審 2026-08-21 で新規発見）: `_segments` は Gemini が
  申告した `statement_page` をそのまま信じる。一方 `card_file_state`
  （`:118-132` の `_ocr_vetoes_page_label`）は同じ字段に対し
  **OCR による拒否権**を持っている —— OCR テキスト側に頁付の候補が在って
  Gemini の申告と矛盾するなら「読めない」に倒す、という非対称の防線である。
  こちらはその防線を継承していない。AD-2b の存在理由が「Gemini の誤読で
  偽の一致が出るのを防ぐ」ことなので、**防げる誤読を防いでいない箇所が
  残っている**。本標本 8 ファイルでは露呈していないが、
  「OCR が頁付の矛盾を読める」∩「その頁がちょうど段境界」の組合せで効く。
  継承するには `card_file_state` 側が拒否後のラベルを外へ出す必要があり、
  本 Plan の範囲外。**既知の欠口として記録する。**
- ~~**TBD-4**: `benchmark_ocr.py`~~ → **B-6 として実装タスクへ格上げ**
  （Codex 評審 低-7）。判っている消費者を先送りする理由が無い。

---

## 8. 影響面とリスク

### 触るファイル

| ファイル | 変更 |
|---|---|
| `card_file_recon.py` | **新規** |
| `test_card_file_recon.py` | **新規** |
| `ocr_engine.py` | `_yield_page_results` に `_file_recon` を載せる（数行） |
| `main.py` | EOF での判定と書込（1 箇所） |
| `local_test.py` | 同上（1 箇所） |
| `sheets_output.py` | `AUDIT_VERDICT_TOTAL_MISMATCH` 定数と訳語の追加 |
| `test_benchmark_ocr_*.py` | **新規**（B-6。`_file_recon` が精度比較に混入しない） |

### 触らないファイル（明示）

`card_reconciliation.py` / `test_card_reconciliation.py` / `card_entries.py` /
`doc_types.py` / `tag_rules.py` / `anomaly_detector.py` / `invoice_classification.py`

### リスク

| # | リスク | 対処 |
|---|---|---|
| R-1 | `_file_recon` の付与が `entries` を汚す | snapshot 番人（B-3） |
| R-2 | 部分失敗ファイルで偽の不一致 | AD-6 で検算不能へ倒す |
| R-3 | 重複頁を二重に数える | producer 側で `is_duplicate` を載せる（AD-3） |
| R-4 | 監査タブが赤で埋まる | 実測で不一致 0 件（§1 事実 2）。B-5 で再確認 |
| R-5 | 理由列が機械可読キーのまま顧客に見える | 訳語の番人（B-2） |
| R-6 | 検算の例外が記帳を止める | fail-open（AD-7） |
| R-7 | メモリ増（頁ごとの観測を EOF まで保持） | 頁あたり int 1 個 + ラベル数個。33 頁で数 KB。generator 制約に影響しない |

### 回退

`main` / `local_test` の呼出 1 行ずつをコメントアウトすれば検算は完全に止まり、
記帳は改修前と逐字同一に戻る。`card_file_recon.py` は他から参照されない。

---

## 9. 辯論記録

（Codex 評審後に追記）

### ラウンド 1（Codex / 2026-08-21）— 裁決

| # | 重大度 | 指摘 | 裁決 | 反映先 |
|---|---|---|---|---|
| 1 | 高 | 素朴な金額去重は**偽の一致**を作る。2 通が偶然同額で 1 通が丸ごと落ちると検出できない | **採用**。ただし Codex の当初案（同額は常に検算不能）は実測で覆盖率 4/7→2/7 になるため採らず、頁付の段判定で消歧する対案を置いた | AD-2b（新設） |
| 2 | 高 | TBD-1 は未決事項ではなく実装 blocker。B-1 の DoD と自己矛盾 | **採用**。TBD から外し AD-2b で決着させた | B-1 DoD / TBD-1 |
| 3 | 中 | 1 物理頁が複数 result を yield するので二重観測しうる（`_yield_line_mode_results`） | **採用**。先頭 result のみに載せ、消費側も同一 page_num を拒否。両方番人化 | AD-3 |
| 4 | 中 | `finalize` の実装位置を行で指定すべき | **採用**。`count>0` ブロック内・`return True` 直前に固定し、番人テストを必須化 | AD-6 |
| 5 | 中 | `_is_bookable` の重複実装は drift する。公開純関数へ抽出せよ | **部分採用**。抽出はしない（`card_reconciliation.py` diff 0 は AD-1 の中核。崩すと `_only()` 45 箇所の risk を再び抱える）。代わりに `KIND_ALL` 全値の判定一致を見る漂移番人を置く。既存 `LabelTableDriftTest` と同型 | AD-10（新設） |
| 6 | 中 | 不一致のときしか書かないので、機構が壊れると完全に無音 | **部分採用**。顧客の監査タブは不一致のみ（AD-5 維持）。B-5 の DoD に verdict 分布の出力を追加。進捗タブへの常時表示は TBD-3 のまま | B-5 DoD |
| 7 | 低 | `benchmark_ocr.py` を TBD に残す理由が無い | **採用**。TBD-4 を B-6 として実装タスクへ格上げ | B-6（新設） |

**駁回ゼロ・全採用ゼロではない**（#5・#6 は実装方法を変えた部分採用）。
#1 は指摘の内容を全面的に認めた上で、対案が実測で優ることを示して置き換えた。

### ラウンド 2（複審 / 2026-08-21）— 裁決

前ラウンドで**部分採用・対案提示**した 3 点だけを争点として複審に付した。

| 争点 | Codex の判定 | 勝敗 | 結果 |
|---|---|---|---|
| 1. AD-2b の段判定 | **変更すべき** | **Codex 勝ち** | 当方の記述が矛盾していた（「別段は検算不能」と書きながら「別段だから不一致になる」とも書いた）。前者だと反例は無音になる。**Codex の修正案「段の内側だけで去重、段が違えば別々に数える」に全面的に従う。** 異常系 3 条件も DoD へ追加 |
| 2. AD-10 の漂移番人 | **維持** | **当方勝ち** | 公開純関数への抽出はしない判断を Codex が支持。ただし `KIND_CHARGE` は `_book_charge_rows` に依存するので番人を `True`/`False` 両方で回すこと、という補足を採用 |
| 3. AD-5 の可視化 | **変更すべき** | **Codex 勝ち** | 「回帰テストで分布を見る」では本番の壊れ方（`_file_recon` 未載・全件検算不能・finalize 未呼出）を捉えられないと論証された。**監査タブの方針（不一致のみ）は維持したまま、運用者向けコンソールログを B-4 の DoD に追加。** TBD-3 から格上げ |

**争点 1 は当方の設計ミスであり、指摘されなければ「反例を検出できる」と
誤信したまま実装に入っていた。** 段判定という方向自体は複審でも支持されたが、
規則の書き方が目的を裏切っていた。

2 ラウンドを通じて Codex 指摘 10 件のうち **8 件採用・1 件維持（当方勝ち）・
1 件は対案へ差し替え**。全採用でも全駁回でもない。

---

## 10. 実施後評審（simcodex Round 1 / 2026-08-21）

5 路並行（simplify の 4 観点 ＋ codex）。**真の欠陥が 2 件出た。**

| # | 出所 | 指摘 | 裁決 |
|---|---|---|---|
| 1 | Reuse | 新テストが手写しの OCR 標本を使い、`classify_page` が `routing_family='unknown'` を返していた。**実物のカード頁は `cc_detail`** —— つまり番人テストが実在しない経路を守っていた | **採用・修正**。`ocr_test_fixtures.AMEX_HEAD` へ差し替え。`test_page_disposition_wiring.py:405` が 2026-08-20 に記録した罠に同じく嵌まっていた |
| 2 | codex | `CREDIT_CARD_DEDUP_MODE="mark"` では重複頁が `ACTION_BOOK` のまま `is_duplicate=True` になる（`page_family.py:695`）。明細は Sheets に**書かれる**のに、検算の両辺から落としていた | **採用・修正**。落とす条件を `action == ACTION_EXCLUDE` に絞った。codex は P2 としたが、検算が突合する集合と Sheets の中身が食い違う欠陥なので実質は高 |
| 3 | Altitude | `CC_FAMILY_DOC_TYPES`（producer の門）と `RECON_POLICY`（検算資格）が別モジュールの別表で、同期の保証が無い。片方だけに doc_type を足すと検算が**無音で効かなくなる** | **採用**。`DocTypeTableDriftTest` を追加 |
| 4 | Simplification | 非カード系 doc_type でも毎ファイル 1 行ログを出す。本番コンソールは**唯一の人手監視経路**で、零情報の行が事故信号を埋める | **採用**。`VERDICT_NOT_APPLICABLE` は黙らせ、他 3 態は従来どおり出す |
| 5 | Simplification | `_finalize` の 4 箇所で `FileVerdict(UNVERIFIABLE, None, X, None, r)` を重複構築 | **採用**。`_unverifiable()` に畳んだ |
| 6 | Reuse | `_segments` と `card_file_state._AnchorRun` が同種の分段器 2 本目 | **部分採用**。統合はしない（目的が違う）が交差参照コメントを入れ、**TBD-5** として欠口を記録 |
| 7 | Efficiency | `detail_lines_from_raw` と `builder` が同じ rows を 2 度走る（300 行頁で 300 回の重複判定） | **駁回**。提案者自身が「AD-3 の設計を崩す」と留保。絶対量は μs〜低 ms で、同頁の Gemini 呼出（秒）に対し無視できる |
| 8 | Efficiency | `DetailLine.section` を計算するが `card_file_recon` は読まない | **駁回**（範囲外）。`card_entries` の共有 DTO 構築子であり、専用の軽量版を足す方が高くつく |
| 9 | Simplification | `_with_file_recon` と `_apply_page_audit_signal` が「先頭だけ変換」の骨格を重複 | **P2 へ送る**。共通化は既存 `_apply_page_audit_signal` を触ることになり、あちらは「同居は実在する」という別の複雑さを抱えている。文脈が逼迫した状態で触る判断ではない |

### 検証で確認できたこと（Efficiency 評審が実測）

- `PageObservation` は int / bool / 短いタプルのみ保持。**raw_data も画像も持たない** ——
  逐頁流式の内存モデル（CLAUDE.md の硬制約）は破っていない
- 非カード系 4 型は `_with_file_recon` が即 `yield from` で抜けるので、
  追加の走査はゼロ

### 最終状態

- 全量 **1453 tests OK**（基線 1390 ＋ 新規 63）、expectedFailure **2 件**（増減なし）
- `card_reconciliation.py` / `test_card_reconciliation.py` / `benchmark_ocr.py` の diff **0 行**
