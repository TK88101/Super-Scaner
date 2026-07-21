# golden_replay 順序盲の修復 — 実施計画（定稿）

- **作成日**：2026-07-21
- **前提**：`docs/2026-07-21-b3-golden-regression-evidence.md`（T7 証拠パッケージ）
- **HEAD**：`9d69f92`（`feature/sandevistan-headless`）
- **状態**：**定稿**（Codex 対抗評審 10 条＋反駁複審 1 輪を反映。辯論記録は §8）
- **発見経緯**：dedup 設計の多角評決（16 agent）で、証拠パッケージ §6 に記載漏れの
  第 5 の限界として指摘された。**趙は「コードは一行も変えない、この検証ツールだけ直す」と裁定**（2026-07-21）。

## 0. 何が壊れているか

`golden_replay.normalize`（`golden_replay.py:348-384`）は捕獲した書式適用を
**集合へ畳み込むため、適用順序と多重度を完全に失う**。

```python
seen.setdefault(tab, set()).add((ref, severity, rgb))   # :370  順序消失
reset_ranges.append(ref)                                 # :367  白は別枠へ隔離
return {..., "reset_ranges": sorted(set(reset_ranges)),   # :382  順序・重複消失
        "highlights": sorted(entries)}                    # :373-376
```

実際の Google Sheets の意味論は**後勝ち**である。同一セルへ
`format_cell_range(赤)` → `format_cell_range(白)` の順で適用すれば、最終的にそのセルは**白**になる。
現行 normalize は白を `reset_ranges` へ、赤を `highlights` へ**別々に**入れるだけなので、
この上書きを一切表現しない。

### 帰結（実害）

「白リセットを高亮の後へ動かす」変更を入れると、**顧客シート上では赤が消える**のに
DIFF-A は **byte 完全一致で緑**になる。異常票を示す赤が消えても検知できない。

白リセットは 5 箇所に散在する（`:300`／`:521`／`:643`／`:763`／`:845`）。
三つの書込尾部はいずれも「append → 白リセット → 高亮」の順序に依存しており、
この順序を壊す改変は現実的にあり得る。**現行の検証網はそれを検出できない。**

既存 435 本にも順序を見る assert は無い（`test_sheets_output.py:418` の
`_sanitize_trailing_once` 単体のみ）。

## 1. 目標 / 非目標

### 目標
1. `normalize` が**明示 format op のセル単位の最終非白態**を表現し、
   上書きによる高亮消失を検出できるようにする。
2. その検出力を**三つの否定対照で実証**する（順序を反転させたら赤くなることを実行ログで示す）。
3. 新口径で DIFF-A / DIFF-B を再実行し、結論が維持されることを確認する。

### 非目標（やらないこと）
- **生産コードの変更は一切しない。** `sheets_output.py` / `main.py` / `posting_ledger.py` /
  `intake_guard.py` / `ocr_engine.py` / `local_test.py` の diff はゼロを維持する（前批の受入基準 7 を継承）。
- 双写（重複）の解消。**趙が「先不改」と裁定済み**（2026-07-21）。本 Plan の射程外。
- harness 影実装の根絶（`process_file` への `pipeline` 引数追加）。生産コードに触るため別批。
- **append の書式継承の建模**（§2 D5 参照。Codex #3 採択）。
- 証拠パッケージ §6 の他の限界（同源定数への不感、真様本の異常分岐欠如）の解消。

## 2. 設計

### D1. 何を新しい正解とするか

**順序そのものを記録するのではなく、順序を適用した結果＝最終態を記録する。**

順序をそのまま JSON へ入れると DIFF-B が**偽赤**になる。UI 経路は票ごとに、頁級経路は
頁ごとに書くため、`format_cell_range` の呼出順序は**元から違う**（実測済み：
UI の reset_ranges は `['A6:AB6','A7:AB1000','A7:AB7','A8:AB8']`、頁級は
`['A6:AB7','A8:AB1000','A8:AB8']`）。順序の違いは実害の無い差異であり、
両者が到達する**最終態**が同じであることこそが等価性の正しい定義である。

したがって normalize は捕獲列を**順に再生**し、セル → 最終背景色 の写像を作る。

### D2. 規模の制御

範囲をセルへ展開すると `A7:AB1000` は 994 × 28 = 27,832 セルになる。全展開は
正規化 JSON を肥大化させ diff を読めなくする。

**方針**：白（リセット）は「消す操作」としてのみ扱い、**非白で触れられたセルだけ**を追跡する。

```
final = {}                       # (tab, col, row) -> (severity, rgb)
erased = []                      # 白が非白を消した記録（恒空であるべき）
for tab, ref, fmt in records:    # 捕獲順（records は append 順を保持済み）
    sev, rgb = severity_of(fmt)
    if sev is None:      continue            # 背景色を持たない書式は無視（現行同様）
    if sev == "white":   当該範囲∩final のセルを削除し erased へ記録
    else:                final[当該範囲の各セル] = (sev, rgb)
```

白の範囲は `final` の既存キーとの積集合しか触らないため、`A7:AB1000` でも走査は
`final` のサイズに比例する。出力は「最終的に着色されているセル」のみ＝現行 highlights と同規模。

### D3. 出力形式

```json
"tabs": {
  "LocalTest_領収書": {
    "rows": [...],
    "highlights": [ {"cell": "B10:B10", "severity": "high"}, ... ],  // 既存（後方互換）
    "final_highlights": [ {"cell": "B10", "severity": "high"}, ... ],// 新規・最終態
    "white_erased": [ ]                                              // 新規・恒空であるべき
  }
},
"reset_ranges": [...]            // 既存のまま診断用に残す
```

- `final_highlights` は**セル単位**（範囲ではない）。`B10:B10` ではなく `B10`。
  範囲表記のままだと「`A5:AB6` の一部だけが白で消された」状態を表現できない。
- **`white_erased`（旧名 `overwritten`）は「白が既存の非白高亮を消した」場合のみ記録する。**
  非白→非白の上書き（黄の全行下地 → I 列赤 等）は**正しい優先順位**であり記録しない
  （Codex #2 前半 採択）。
- **`white_erased` は恒空であるべき不変条件である**（Codex #2 後半は反駁により撤回、§8-2）。
  非空＝順序誤り／範囲誤り／`_sanitize_trailing_once` の起点 off-by-one のいずれかのバグ。
- 両フィールドとも **per-tab**（Codex #6 採択）。全 tab が空でもキーを持つ（Codex #9 採択）。
- ソートは **`(row, col)` の数値順**。A1 文字列順だと `"A10" < "A6"` になる（Codex #9 採択）。
- 既存 `highlights` / `reset_ranges` は**残す**。T7 証拠パッケージが参照しているため、
  削除すると過去の証拠と繋がらなくなる。

### D4. 範囲パーサ

- 対応形式：単セル `I7`、canonical `I7:I7`、行全体 `A6:AB6`、複数行 `A6:AB7`、
  大範囲 `A7:AB1000`、二文字列 `AA`/`AB`。
- **解析不能な ref は例外を投げる（fail fast）。静默無視は禁止**（Codex #8 採択）。
  静默無視は「高亮が消えたのに緑」という本 Plan が潰そうとしている失敗様態そのものを新設する。
- 逆順範囲（`AB6:A6` 等）も明示的に拒否する。

### D5. この設計が表現**しない**もの（Codex #3 採択）

`FakeWorksheet` は append 時の**書式継承を建模しない**。白リセットの本来の目的は
まさにその継承色の除去である。したがって本 Plan の「最終態」は正確には
**「明示 format op のみを適用した結果の最終非白態」**であり、真の Sheets 背景色ではない。

**検出できない失敗様態**：白リセット自体が**削除／失敗**し、append 継承で汚れた色が
残るケース。明示 op が無いため `final_highlights` に現れない。

この盲区は harness では塞がない。理由は、継承規則を fake へ建模すること自体が
新たな「写経」であり、写経の誤りは検出できないため。代わりに
**真 Sheets readback（T6 型の検査）を独立観測点として維持する**。
実績：2026-07-21 の T6 で空尾行 17–1000 行 × 28 列を全走査し着色 0 件を確認済み。

## 3. 任務清単（各項に DoD）

> **実行順序は DoD**：T1（RED）→ T2（GREEN）→ T3（否定対照）→ T4（再検証）→ T5（証拠更新）。

### T1 失敗するテストを先に書く（RED）
`test_golden_replay.py` に `FinalStateTest` を追加：

**上書き語義**
1. `test_white_after_highlight_erases_it` — 同一セルへ high → white の順 →
   `final_highlights` 空・`white_erased` 1 件。
2. `test_highlight_after_white_survives` — 逆順なら残る。
3. `test_partial_range_overwrite` — `A5:AB5` を high で塗った後 `B5` だけ白 →
   B5 だけが消える。
4. `test_non_white_overwrite_is_not_erasure` — 黄(全行) → 赤(I列) の順で
   `white_erased` が**空のまま**（正しい優先順位を誤検出しないこと。Codex #2 前半）。

**D1 の中核主張（Codex #7 採択）**
5. `test_different_operation_history_same_final_state_is_equal` —
   UI 風 records（white `A6:AB6` → low `A6:AB6` → high `I6`）と
   頁級風 records（white `A6:AB7` → low `A6:AB6` → high `I6`）で
   **`final_highlights` が一致**すること。`white_erased` の一致は要求しない。

**範囲パーサ（Codex #8 採択）**
6. `test_range_parser_forms` — 単セル `I7`／canonical `I7:I7`／単列範囲／複数行／
   `Z`・`AA`・`AB` 境界／`A5:AB6` が 56 セルへ展開されること。
7. `test_range_parser_rejects_malformed_refs` — 解析不能 ref と逆順範囲で例外。

- **DoD**：7 本とも**実行して RED を確認**し、RED の実行ログを残す。

### T2 実装（GREEN）
- `golden_replay.py` に範囲パーサと最終態計算を追加。`normalize` の戻り値へ 2 フィールド追加。
- 既存フィールド（`rows` / `highlights` / `reset_ranges` / `warnings`）は**一切変更しない**。
- **DoD**：T1 の 7 本が GREEN。既存 `test_golden_replay` 20 本＋`test_golden_capture` 9 本が
  全て GREEN のまま。新規コードのカバレッジ 80% 以上。

### T3 否定対照（検出力の実証）
本 Plan の存在理由そのものを実証する。**T2 の GREEN だけでは「網が効く」証明にならない。**

**T3-a 単元級（副作用ゼロ、Codex #5 採択）**
records 列を直接構成し、**同一多重集合・異なる順序**の 2 本を作って以下を assert する：
- 旧口径（`highlights` / `reset_ranges`）が**完全一致**（＝旧口径は盲）
- 新口径（`final_highlights` / `white_erased`）に**差分が出る**（＝新口径は検出する）

これにより「盲点そのもの」を、生産コードを一切触らずに証明する。

**T3-b 集成級（worktree 隔離、Codex #10 採択）**
`git worktree` で HEAD の複製を作り、**その中でのみ** `sheets_output.py` を改変する。
**主工作樹には一切触れない**（R3 を構造的に消す）。三つの注入を行う：

| # | 注入箇所 | 内容 |
|---|---|---|
| 1 | `append_entries :642-646` | 白リセットを高亮適用の後ろへ移動 |
| 2 | `commit_page :519-524` | 同上（頁級経路。Codex #4 採択） |
| 3 | `_sanitize_trailing_once :762` | 起点を `last_data_row + 1` → `last_data_row` へ（off-by-one。CLAUDE.md 記載の歴史的バグと同型） |

注入 3 は `_write_unrecognized_row` の代わりに選んだ。理由は、真 fixture に
`_unrecognized` が無く合成 fixture が要るのに対し、off-by-one は**実在した色伝染バグと同型**で
より現実的な失敗様態だから（Codex #4 を採択したうえで対象を差替え）。

- **DoD**：
  - 三注入すべてで新口径に差分が出る（`white_erased` が非空になる）。
  - 同じ三注入で旧口径は差分ゼロ。
  - **worktree を削除して終了**。`git status --short` が空であることをコマンド出力で示す。

### T4 新口径での DIFF-A / DIFF-B 再実行
- **DIFF-A（基線 `0d304f0` vs HEAD）**：判定＝`rows` / `final_highlights` / `white_erased` /
  既存フィールド の**全一致**。同一経路の前後比較であり順序差は本来存在しないため、
  `white_erased` も判定に含める（Codex #1 を修正採択）。
- **DIFF-B（HEAD の UI vs 頁級）**：判定＝`rows` と `final_highlights` の**一致**。
  加えて `white_erased` は**両側それぞれが空**であることを assert する
  （「両側が等しい」ではなく「両側とも不変条件を満たす」。Codex 複審の提案を採択、§8-2）。
- **赤が出た場合**：
  - DIFF-A が赤 → 旧口径が見落としていた真の回帰の可能性。**本 Plan では直さない**。
    差分の具体セル・severity を事実として趙へ報告し、B3 の扱いを別途裁決（前批 R5 踏襲）。
  - DIFF-B の `final_highlights` が赤 → D1 の前提が誤り。**設計から見直す**。
- **DoD**：両 DIFF の結果と、新旧口径の比較表を記録。

### T5 証拠パッケージの更新
- `docs/2026-07-21-b3-golden-regression-evidence.md` §6 に **限界 5「順序盲」**を追記し、
  本 Plan で解消済みである旨と解消日、および **D5 の残存盲区（append 書式継承）**を明記。
- §2 の否定対照の表に T3 の三注入を追加。
- **DoD**：証拠パッケージを読んだだけで「いつ何が盲点で、いつどう塞がれ、何が残ったか」が辿れる。

## 4. 受入基準（脚本化判定優先）

1. `venv311/bin/python -m unittest test_golden_replay test_golden_capture -v` 全緑。
2. `venv311/bin/python -m unittest discover -p "test_*.py"` 全緑（現行 435 本＋新規 7 本）。
3. **T3-a で「旧口径 緑・新口径 赤」の非対称**が実行ログで示される。
4. **T3-b の三注入すべてで `white_erased` が非空**になる。
5. 新口径の DIFF-A が空（赤なら停止・報告）。
6. 新口径の DIFF-B が `final_highlights` 一致、かつ両側の `white_erased` が空。
7. 新規コード カバレッジ 80% 以上。
8. **生産コードの diff がゼロ**。T3-b は worktree 内でのみ改変し、終了後
   `git status --short` が空であることをコマンド出力で証明する。

## 5. 影響面

- **改修**：`golden_replay.py`（`normalize` へフィールド追加＋範囲パーサ追加）、
  `test_golden_replay.py`（テスト 7 本追加）、
  `docs/2026-07-21-b3-golden-regression-evidence.md`（§2・§6 更新）。
- **新規**：本 Plan。
- **不動**：生産コード全て。Drive／Firestore／生産 Spreadsheet／Gemini quota。
  **本 Plan は外部副作用を一切持たない**（真 Gemini 呼出なし、Sheets 書込なし）。
- **趙の作業**：無し（生産機 miniPC への反映も不要——生産コードが変わらないため）。

## 6. リスクと回退

- **R1 D1 の前提が誤り（順序差が最終態に現れる）**：→ T1-5 が RED のまま通らない、
  または T4 の DIFF-B が赤。その時点で設計へ差し戻す。
- **R2 新口径が DIFF-A を赤にする（真の回帰発見）**：→ **本 Plan では直さない**。事実を趙へ報告。
- **R3 mutation を戻し忘れる**：→ **T3-b を worktree に隔離することで構造的に消滅**（Codex #10）。
  主工作樹には最初から触れない。
- **R4 範囲パーサのバグ**：→ T1-6/T1-7 で形式と境界を固定。fail fast により静默誤りを排除。
  加えて既存 15 fixture での DIFF-A が実データによる回帰ガードになる。
- **R5 過去の証拠との断絶**：→ 既存フィールドを削除せず残すことで、T7 証拠パッケージの
  記述（rows=31 / highlights=13 / reset_ranges=35）が引き続き検証可能。
- **回退**：`golden_replay.py` と `test_golden_replay.py` の改修を revert すれば元に戻る。
  生産コードに触れないため、生産機への影響は原理的にゼロ。

## 7. 本 Plan が主張しないこと

- **append 書式継承に由来する汚れ色は検出できない**（§2 D5）。白リセット自体の削除／失敗は
  真 Sheets readback でしか捕まらない。
- 順序盲の解消は、証拠パッケージ §6 の**他の限界を解消しない**：
  - 真様本 20 頁に `_page_error` / `_unrecognized` が無いこと（限界 1）
  - DIFF-A が同源定数に不感であること（限界 2）
  - harness が生産コードの影実装であること（dedup 評決で判明。`drive_ui` の
    `start_new_file` 遅延呼出しは生産 `main.py:876-877` の無条件呼出しと既に乖離している）
- 本 Plan 完了後も harness は「生産の写経」のままであり、写経の誤りは検出できない。
  その解消には `process_file` への `pipeline` 引数追加（生産コードへの改修）が必要で、
  **趙の拍板を要する別批**である。

## 8. 辯論記録（後続評審者が輪回しないため）

### 8-1 Codex 対抗評審 10 条の裁決

| # | severity | 裁決 | 根拠 |
|---|---|---|---|
| 1 | High | **修正採択** | `white_erased` を DIFF-B の等価判定から外すのは正しい（中間態を跨経路比較へ持ち込むと順序偽赤が戻る）。ただし DIFF-A は同一経路の前後比較であり順序差は本来存在しないため、判定に含める |
| 2 前半 | High | **採択** | 非白→非白の上書き（黄→赤）は正しい優先順位。語義を「白が非白を消した場合のみ」へ収窄し `white_erased` へ改名 |
| 2 後半 | High | **駁回**（§8-2） | 「恒空を要求するな」は却下。Codex は複審で撤回 |
| 3 | High | **採択** | fake は append 書式継承を建模しない。「最終背景色」は誇大表現。D5 として限界を明記し、真 Sheets readback を独立観測点として残す |
| 4 | High | **採択＋対象差替** | 白リセットは 5 箇所。注入を 3 つへ増やす。ただし 3 本目は `_write_unrecognized_row`（合成 fixture が要る）ではなく `_sanitize_trailing_once` の off-by-one を選ぶ——CLAUDE.md 記載の実在した色伝染バグと同型でより現実的 |
| 5 | Med | **採択** | mutation を脚本化し「records 多重集合同一・順序相異・旧 JSON 同一・新 final 相異」を assert。T3-a として単元級で実施 |
| 6 | Med | **採択** | `white_erased` を per-tab へ。多 tab で `B10` は一意でない |
| 7 | Med | **採択** | D1 の中核主張に対応するテストが無かったのは漏れ。T1-5 として追加 |
| 8 | Med | **採択** | 解析不能 ref は fail fast。静默無視は本 Plan が潰そうとしている失敗様態そのものを新設する |
| 9 | Low | **採択** | `"A10" < "A6"` の字典序陥穽は実在。ソートを `(row, col)` 数値順へ |
| 10 | Low | **採択・#5 へ統合** | mutation を worktree で隔離。主工作樹に触れないことで R3 を構造的に消す |

### 8-2 駁回 1 件と複審結果

**争点**：Codex #2 後半「`white_erased` に恒空を要求するな。合法的に『先に塗って局部的に清める』流程がありうる」。

**当方の反駁**（3 点）：
1. 実測：三つの書込尾部（`append_entries :642-646`／`commit_page :519-524`／
   `_write_unrecognized_row :844-848`）はいずれも白リセットが高亮適用の**前**。
   `_sanitize_trailing_once :762` は `last_data_row + 1` から清めるためデータ行の高亮に触れない。
   よって正しい実装下では恒空。
2. この欄位の価値は恒空であること自体にある。非空＝バグ。予め緩和すると判定力を失い、
   人間の解読を要する診断欄位へ退化する——機械判定可能な信号を作るという本 Plan の存在理由に反する。
3. 将来そのような流程が実際に現れたなら、その流程に白名単を付けて理由を書くべきで、
   今から受入基準を緩めるべきではない。

**複審結果**：**Codex は撤回**。四箇所の行番号を自ら再確認したうえで
「現行コードに具体的な合法反例を出せない」「以前の懸念は将来流程への保守的仮定にすぎず、
機械判定信号としての価値を損なうには足りない」と認めた。

**副産物**：複審で Codex が提案した形が双方の原案より優れていたため採択した——
DIFF-B では `white_erased` を「両側が等しい」ではなく**「両側それぞれが空」**と判定する。
不変条件の検査であって履歴の比較ではないため、#1 の偽赤懸念が構造的に消える。
