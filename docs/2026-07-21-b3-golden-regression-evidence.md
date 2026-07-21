# B3 黄金様本回帰 — 証拠パッケージ

- **対象**：IP-304 B3（`396dbc7` 頁級 posting_id 派生＋硬去重）
- **Plan**：[docs/plans/2026-07-21-b3-golden-regression.md](plans/2026-07-21-b3-golden-regression.md)
- **実施日**：2026-07-21
- **HEAD**：`8e45894`（branch `feature/sandevistan-headless`、未 push）

## 0. 結論

**B3 は書账層に回帰を持ち込んでいない。** 同一入力に対する前後の産出が正規化 JSON レベルで
バイト一致（DIFF-A）、頁級経路と UI 経路が等価（DIFF-B）、真端到端が例外ゼロ完走し
ハーネス予測とセル単位で一致（DIFF-C）。

ただし本結論には**証明の限界**がある。§6 を必ず併読すること。

## 1. 受入基準の判定

| # | 基準 | 判定 | 証拠 |
|---|---|---|---|
| 1 | 新規単測 全緑 | ✅ | `Ran 29 tests` `OK`（§5.1） |
| 2 | 全量テスト 全緑 | ✅ | `Ran 435 tests in 1.232s` `OK`（§5.1） |
| 3 | DIFF-A が空 | ✅ | sha256 両側 `1d7b7fdb…`（§2） |
| 4 | DIFF-B 完全一致 | ✅ | 15 件全て rows／highlights／reset_ranges 一致（§3） |
| 5 | DIFF-C 例外ゼロ＋目視 | ✅ | 6 頁 0 失敗、11 行着地（§4） |
| 6 | 新規コード カバレッジ 80%+ | ✅ | `golden_replay` 95%／`golden_capture` 89%／総計 94% |
| 7 | 生産コード diff ゼロ | ✅ | `git diff 396dbc7..HEAD -- <6 ファイル>` が空（§5.2） |

## 2. DIFF-A（UI 版書账層ゼロ回帰）

基線 `0d304f0`（B3 前）と HEAD `8e45894` で、**同一スクリプト・同一 fixture** による
`replay_ui` の正規化 JSON を比較。

```
diff base.replay.json head.replay.json   → 差分なし
sha256(base) = sha256(head) = 1d7b7fdb6f8598ccdf4b04665d5f78f3ba4b9677b36161577fef82b208256bd0
```

**空でないことの証明**（両側が何も産出せず一致した、ではない）:

```
fixtures=15  tabs={'LocalTest_領収書'}  rows=31  highlights=13  reset_ranges=35  warnings=(なし)
severity 内訳 = {high: 3, medium: 3, low: 7}
```

### 出自証明ブロック（worktree 取り違えの機械検出、D3／R2）

| | 基線側 | HEAD 側 |
|---|---|---|
| `has_build_page_write` | **False** | **True** |
| `head` | `0d304f0` | `8e45894` |
| `cwd` | `<scratchpad>/baseline-0d304f0` | `/Users/…/Super Scaner` |
| `PYTHONPATH` | `''` | `''` |
| `sheets_output.__file__` ほか 5 モジュール | 全て worktree 配下 | 全て主仓配下 |

基線側に `build_page_write` が**存在しない**ことが、正しく B3 前を検出している機械的証明。

### 否定対照（比較経路に辨別力があることの証明）

正向の緑だけでは「diff が常に緑」の可能性を排除できないため、既知の差分を注入して
赤になることを確認した。**1 回目は失敗し、それ自体が発見となった。**

| # | 注入 | 結果 | 解釈 |
|---|---|---|---|
| 1 | 基線側 `_SEVERITY_COLORS["high"]` を `(1,0.8,0.8)`→`(1,0.5,0.5)` | **diff 空のまま** | `golden_replay.severity_of` は同じ辞書を逆引きする。書込端と判読端が同源のため差分が**側内で相殺**された |
| 2 | env `DOC_LOW_CONFIDENCE_THRESHOLD=0.65→0.70`（ファイル無改変） | **diff 赤** | p2（conf 0.664）が低置信判定に入り、`A8:AB8` low 高亮が増加、U 列に「黄系」。highlights と rows の両経路を同時に貫通 |

注入 2 により比較経路の辨別力を確認。注入 1 が示す不感領域は §6-2 に限界として明記する。

### 追加の否定対照（2026-07-21 順序盲修復時、`docs/plans/2026-07-21-golden-replay-order-blindness.md`）

順序盲（§6-5）の修復にあたり、worktree 隔離下で 3 つのバグを注入して検出力を実測した。
**主工作樹には一切触れていない。**

| # | 注入 | 旧口径 | 新口径 | 判定 |
|---|---|---|---|---|
| 3 | `append_entries:642-646` の白リセットを高亮の後ろへ | **差分ゼロ（盲）** | UI 側 `final_highlights` 13→0、`white_erased` 0→13 | 旧盲・新検出 |
| 4 | `commit_page:519-524` の白リセットを高亮の後ろへ | **差分ゼロ（盲）** | 頁級側 13→0、`white_erased` 0→13 | 旧盲・新検出 |
| 5 | `_sanitize_trailing_once:762` の起点 off-by-one | 差分あり | 両側 13→11、`white_erased` 各 2 件（`H6` の low が消えた等） | **両方検出** |

注入 5 は当初「順序盲」の一例として計画したが、実測の結果**分類が誤り**だった——
起点の変更は `reset_ranges` の文字列自体を変えるため旧口径にも差分が出る。
ただし旧口径が示すのは「範囲文字列が変わった」だけで、そこから被害を人が推論する必要がある。
新口径は「`H6` の low 高亮が白に消された」と直接指す。**検出の有無ではなく情報の質が違う。**

単元級の否定対照（生産コードに触れずに盲点を証明する）は
`test_golden_replay.OrderBlindnessProofTest` として常設テスト化した。

## 3. DIFF-B（頁級経路の等価性）

HEAD 側で `replay_ui` と `replay_page` を **fresh tab 条件**で実行し比較。

```
比較 fixture 数=15  ui側 rows=31  highlights=13
DIFF-B: 全件一致（rows / highlights / reset_ranges）
```

`reset_ranges` は Plan の判定 2 項に加えて自主的に追加した第 3 の判定軸。

### 否定対照

頁番号 `[1,2,1]` の合成 fixture を投入したところ driver は即座に赤を報告した。

```
--- reappear: rows=False highlights=False resets=False
    row[2] ui  =['3', '2026/04/01', '備品・消耗品費', …]
    row[2] page=None
    ui only  =[('LocalTest_領収書', 'H8:H8', 'low')]
    ui only  =['A8:AB8']
```

これは**設計どおりの挙動**であり回帰ではない（`main.py:462-465`：頁番号が非連続に再登場した
場合、頁級経路は ESCALATE して以降を書かない。UI／local_test 経路にこの契約は無い）。
真 fixture の頁番号は連続（PDF は 1..6）のため正向は全緑。

## 4. DIFF-C（真端到端 smoke）

- **硬前置**：実行前に `LocalTest_領収書` tab の不在を確認（表内は GAS 側の `_config` のみ）。
  `シート1` も不在のため構築子の `_cleanup_default_sheet` は何も削除していない。
- **入力**：`領収書_税区分テスト_6パターン.pdf`。
  sha256 `3ce5535f…` が **原本・複製・T3 manifest の三者で一致**（同一原票の証明）。
- **実行**：`local_test.py --only-file`（真 Gemini・Strategy C）

```
✅ PDF分割解析完了: 6件抽出 (失敗ページ: 0)
📊 処理結果サマリー   ✅ 成功: 1 件   ❌ 失敗: 0 件
```

### 目視確認項目（等価断言はしない——裁決#1・#10）

| 項目 | 結果 |
|---|---|
| 例外ゼロ完走 | ✅ 6 頁 0 失敗 |
| 行の着地 | ✅ 11 データ行（同頁多税率で分割）＋凡例 4 行＋ヘッダ |
| 28 列・税区分 | ✅ 全行 `len=28`、`課対仕入10%`／`課対仕入8% (軽)`／`対象外` が入位 |
| 高亮 | ✅ 行10-12：B 赤(日付空)＋F 橙(取引先空)＋H 黄(T番号空)／行13-14：H 黄 |
| U 列タグ | ✅ 赤系 ×3、黄系 ×2（severity と対応） |
| 取引No 連番 | ✅ 1,1,2,3,4,4,4,5,5,6,6（頁ごとに 1 番号、同頁複数行は共有） |
| `_ensure_row_capacity` 警告 | ✅ 無し（grid 1000 行に対し 16 行使用） |
| 空尾行の色伝染 | ✅ 17–1000 行 × 28 列を全走査し着色 0 件 |

### 三観測点の交差検証（R6 緩和の要）

同一 fixture に対する **① ハーネス予測 → ② Google API 読戻し → ③ 趙がダウンロードした xlsx**
の三点がセル単位で一致した。

```
ハーネス予測 highlights: B10,B11,B12 high | F10,F11,F12 medium | H10..H14 low
API 読戻し           : 行10-12 赤B 橙F 黄H | 行13-14 黄H
xlsx（openpyxl）      : FFCCCC=B | FFE5B2=F | FFFFB2=H （同上）
U列タグ・取引No・日付  : 三者完全一致
```

DIFF-A／DIFF-B は自作 fake 上で走るため、ハーネスに建模誤差があれば緑が自洽的に成立する
（Plan R6）。**②③ はハーネス外の独立観測点**であり、fake の建模が生産の書込挙動と
一致していることを示す。前二者の緑はこれによって初めて支えられる。

なお今回の Gemini 出力は T3 の capture と逐字同一だった（p6 の `2025/07/21` も含む）ため
セル単位の対照が可能になったが、これは**偶然であり等価断言の根拠にはしない**。

## 5. 実行環境と再現

### 5.1 テスト出力

```
$ venv311/bin/python -m unittest discover -p "test_*.py"
Ran 435 tests in 1.232s
OK

$ venv311/bin/python -m coverage run --source=golden_replay,golden_capture \
      -m unittest test_golden_replay test_golden_capture
Ran 29 tests   OK

Name                Stmts   Miss  Cover
golden_capture.py      70      8    89%
golden_replay.py      209     10    95%
TOTAL                 279     18    94%
```

### 5.2 変更範囲

```
$ git diff 396dbc7..HEAD --stat
 .gitignore                                    |   6 +
 docs/plans/2026-07-21-b3-golden-regression.md | 223 +
 golden_capture.py                             | 138 +
 golden_replay.py                              | 428 +
 test_golden_capture.py                        | 153 +
 test_golden_replay.py                         | 363 +
 golden_manifest/*.manifest.json （15 本）      | 135 +
 21 files changed, 1446 insertions(+)

$ git diff 396dbc7..HEAD --stat -- sheets_output.py main.py posting_ledger.py \
      intake_guard.py ocr_engine.py local_test.py
（空）
```

**全て新規ファイル。生産コードの改修はゼロ。** 回退は新規ファイルの削除のみで足りる。

### 5.3 fixture の出所

`golden_manifest/*.manifest.json`（15 本、仓追跡）に source sha256・capture 時 commit・
`gemini_model: models/gemini-2.5-flash`・`strategy: C`・頁数を記録。
fixture 本体（`golden/`）は**客先の領収書の生 OCR 結果を含むため gitignore**。
再現は manifest の sha256 で原票を同定し `golden_capture.py` を再実行する。

### 5.4 driver スクリプト

DIFF-A／DIFF-B の driver は使い捨てのため scratchpad に置き、仓へは入れていない
（`t4_driver.py`／`t5_driver.py`）。いずれも `golden_replay` の公開関数
（`replay_ui`／`replay_page`／`origin_report`）を呼ぶだけで、独自ロジックを持たない。

## 6. 証明の限界（**必読**）

本パッケージが**主張しないこと**を明示する。緑の範囲を過大に読まないため。

1. **異常分岐の真様本カバレッジ欠如**
   真 fixture 20 頁は全て正常票であり、`_page_error` も `_unrecognized` も 1 件も無い。
   したがって wrapper 語義のうち「部分失敗の集約占位行」「全頁失敗＝書込ゼロ」は
   **合成単測のみのカバレッジ**であり、黄金様本では駆動されていない。
   （ESCALATE 分岐のみ DIFF-B の否定対照で合成入力ながら比較経路上を通過した。）

2. **DIFF-A は同源定数に不感**
   `_SEVERITY_COLORS` のように「生産が書込み、ハーネスが同じ定義を逆引きする」定数は、
   値が変わっても側内で相殺され diff に現れない（§2 の否定対照 1 が実証）。
   本件では B3 がこれらを改変していない（受入基準 7 のゼロ diff が担保）ため結論に影響しないが、
   **DIFF-A がこの種の改変を検出できるとは主張しない**。

3. **DIFF-B の射程**
   DIFF-B の緑が示すのは `build_page_write`＋`commit_page` が UI 経路と等価な産出をすること
   であって、**真の headless 呼出側が既にそう振る舞っている**ことではない。
   headless 経路の真接続は本 Plan の非目標。

4. **DIFF-C は等価断言ではない**
   目視 smoke であり、前後一致を主張していない。§4 の三点一致は補強証拠であって基準ではない。

5. **順序盲（2026-07-21 発見 → 同日修復済み）**
   当初の `normalize` は高亮を集合へ畳むため**適用順序と多重度を失っていた**。
   Google Sheets の書式は後勝ちであり、白リセットが高亮の後に走れば顧客シート上で赤が消えるが、
   旧口径ではそれが **byte 完全一致で緑**になった（上表の注入 3・4 が実証）。
   **本パッケージ初版の §6 にはこの限界が記載されていなかった。**

   **修復済み**：`final_highlights`（セル単位の最終非白態）と `white_erased`（白が消した高亮）を
   `normalize` へ追加。DIFF-A は全フィールドで、DIFF-B は `final_highlights` 一致＋
   両側 `white_erased` 空で判定する。新口径での DIFF-A / DIFF-B は再実行済みで
   **いずれも緑**（DIFF-A の sha256 両側 `65fa77c7…`）。全量 447 テスト緑。

   **残存する盲区**：`FakeWorksheet` は append の**書式継承を建模しない**。白リセットの本来の
   目的はその継承色の除去であるため、新口径が表現するのは正確には
   「**明示 format op のみを適用した最終非白態**」であって真の Sheets 背景色ではない。
   白リセット自体が削除／失敗して汚れ色が残るケースは harness では捕まらない。
   継承規則を fake へ建模することは新たな写経を生むため行わず、
   **真 Sheets readback（§4 の T6 型検査）を独立観測点として維持する**方針とした。

6. **ハーネス自洽緑の完全排除は不可能**
   fake に建模誤差があれば DIFF は自洽的に緑になりうる（R6）。緩和策は
   ハーネス自身への回帰ガード（時刻凍結の対検証・4 出口の捕獲証明・wrapper 語義・
   基線互換の AST ガード）と、§4 の外部観測点。**完全な排除には至っていない。**

## 7. 残留事項

| # | 内容 | 状態 |
|---|---|---|
| 1 | 生産表 `MF_Import_Data` の `LocalTest_領収書` tab（16 行） | **趙が手工削除**（コードは自動削除しない） |
| 2 | 日付が同批他票より 1 年古い票（TRIAL `2025/07/21`） | **推測**：票面どおりか OCR 誤読か未分別。`anomaly_detector` に「日付過旧」規則は無い。別議題 |
| 3 | `openpyxl` 3.1.5 を venv311 へ導入（趙の許可済） | `requirements.txt` へは**未記載**（分析用途であり生産 PC の配備に影響させない） |
| 4 | 本ブランチは未 push | 趙の裁決待ち |
