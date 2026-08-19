# 部分 T11 — `local_test.py` をクレカ/交通系IC 対応にする

- **日付**: 2026-08-19
- **分岐**: `main`（着手時 HEAD = `d9af49f`、全量 1004 tests 緑・実測）
- **母 Plan**: `docs/plans/2026-08-12-credit-card-doctype.md` §T11（`:637-639`）
- **趙裁定 2026-08-19（路線 B）**: T8 より先に真票を 1 枚流す。T8 の
  `detect_deduction_risks` は新機能であり、実際の出力を見ずに設計すると
  顧客が使わないものを作る危険がある。
- **モード**: `/fatboyslim` normal（単一ファイル小改。loop 不要）

---

## 1. 目標 / 非目標

### 目標

`local_test.py` に真票（クレジットカード利用明細書）を 1 枚流せる状態を作る。
生産スプレッドシート `MF_Import_Data` の `LocalTest_カード明細` タブに
28 列が出るところまで。

### 非目標（今回やらないこと。趙既裁定）

| 項目 | 裁定 |
|---|---|
| タブ名に `_` 接頭辞を付ける | **却下**（趙 08-19）。22:00 JST の GAS 削除は手動タブ削除で回避する |
| `OUTPUT_SPREADSHEET_ID` を別シートへ向ける | **却下**（趙 08-19） |
| T8 `detect_deduction_risks` の実装 | 真票結果を見てから決める |
| T9 `main.process_file` 接線 | 別タスク。local_test は `process_pipeline` 直接消費なので重複頁短絡・元帳結算は**本タスクでは検証不能** |
| `main.py` / `ocr_engine.py` / `sheets_output.py` の改造 | 一切しない |

---

## 2. 前提事実（実測済。再調査不要）

| # | 事実 | 証拠 |
|---|---|---|
| F1 | `FOLDER_TYPE_MAP` は `local_test.py` にしか無い（定義 1・参照 3） | `grep -rn FOLDER_TYPE_MAP --include=*.py` |
| F2 | 既存テストで `FOLDER_TYPE_MAP` の内容を縛っているものは無い | 同上 |
| F3 | `ensure_dirs()` は `for folder_name in FOLDER_TYPE_MAP` で回る（`local_test.py:52`） → map に足せば目録は自動生成される。手動作成は不要 | `local_test.py:49-53` |
| F4 | `DocType.ALL` は 6 種。`FOLDER_TYPE_MAP` は 4 種しか無い（`credit_card` / `transit_ic` が欠落） | `doc_types.py:13-14`, `local_test.py:40-45` |
| F5 | `local_test.process_local_file` は `_audit_signal` を**読んでいない**。`main.process_file:596` は読んで監査タブへ「分岐」行を書く | 両ファイル読解 |
| F6 | `_audit_signal` を発するのは (a) 封筒シグナル＋entries 有効（RECEIPT 経路）、(b) **line_mode の行欠け・救済痕跡**（`ocr_engine.py:2367/2375/2388`） | `ocr_engine.py:2344-2390` |
| F7 | `test_pipeline_consumers.py` の不変式は `_excluded_page` のみ。`_audit_signal` は縛っていない | `test_pipeline_consumers.py:88-104` |
| F8 | 真票は `~/Downloads/クレジットカード訓練樣本` に 8 ファイル 32 頁。黄金値は F-4 の 3 例: 17,295 / 933,680 / 15,503 | `docs/plans/2026-08-12-credit-card-sample-facts.md` |
| F9 | `card_salvage.page_marks()` は **shortage None かつ reason 非 None** のとき「**監査タブのみ**」と規定する。つまり「救済は経たが行数は充足」の頁は **MF 側に一切痕跡が出ない** | `card_salvage.py:259-280`（docstring `:263-265` が権威） |
| F10 | `main.process_file` は頁カバレッジ突合をして欠落を監査タブへ書く。`local_test.process_local_file` には `seen_page_nums` / `last_total_pages` に相当するものが**無い** | `main.py:617-628` vs `local_test.py` 全文 |
| F11 | `sheets_output.flush()` は `pass`（no-op）。取引No の書き戻しは無い | `sheets_output.py:765-767` |

**F5 + F6 + F7 + F9 の含意**（Codex 評審 #1・#2 を受けて修正）:

`_audit_signal` を読まない欠口は `credit_card` 追加で**生まれる**のではなく、
RECEIPT 経路（`envelope_signal_with_entries`、`ocr_engine.py:2321`）で
**既に存在していた**。本改動がするのは、この既存欠口を
**line_mode 真票テストで高リスク化させる**ことである。

危険度が跳ね上がる理由は F9 にある。line_mode には
「**MF 側に痕跡ゼロ・監査タブにだけ痕跡**」という状態が構造的に存在する
（`salvaged:N/M`）。`local_test.py` はその唯一の落点を読まないので、
**Gemini 応答が截断され救済で復元された頁が「綺麗に成功した頁」と
見分けられない**。真票を見て T8〜T10 を決めるという本タスクの目的に対し、
これは「通ったが結果が信用できない」を生む。§3 の T11a-5 で扱う。

---

## 3. 任務一覧（各項に DoD）

### T11a-1: 登録漏れを禁じる番人テストを書く（RED）

新規 `test_local_test_folder_map.py`。

- `local_test.FOLDER_TYPE_MAP` が `DocType.ALL` を**全件**覆うことを検査する
- **AST 解析で読む。`import local_test` はしない** —— `local_test` は
  module 直下で `load_dotenv()` と `from ocr_engine import process_pipeline`
  を実行するため、import すると paddleocr / google.generativeai を
  引き込む。`test_pipeline_consumers.py` が AST 方式を採っているのと同じ理由
- 否定対照を必ず持つ: 故意に 1 件欠けたソース片で解析器が**赤くなる**ことを
  実測する。「一覧を空にしただけで緑になる番人」を作らない
  （T6 で踏んだ `UnwiredItemsTest` の substring 事故と同型を避ける）
- **module docstring で工具契約を明文宣言する**（Codex 評審 #3 の複審で
  合意した形）。`DocType.ALL` 全覆蓋を「任意の全覆蓋願望」ではなく
  **`local_test` の契約**として書く:

  > `local_test` の契約 ＝ `DocType.ALL` 全件をローカル入力フォルダ経由で
  > 検証可能にすること。`FOLDER_TYPE_MAP` は doc_type 並行登録表の 7 枚目で
  > あり（PROMPTS / ENTRY_BUILDERS / DOC_TYPE_CONFIG / DOC_TYPE_TAB_SUFFIX /
  > ENV_FOLDER_MAP / RECON_POLICY に次ぐ）、登録漏れの代価は
  > 「その doc_type がローカル検証不能になり、しかも誰も気づかない」。

**DoD**: 現行 `local_test.py` に対して赤。欠落 2 件（`credit_card` /
`transit_ic`）を名指しする失敗メッセージが出る。

### T11a-2: `FOLDER_TYPE_MAP` に 2 件追加（GREEN）

`local_test.py:40-45`。

```python
FOLDER_TYPE_MAP = {
    "receipt": DocType.RECEIPT,
    "purchase_invoice": DocType.PURCHASE_INVOICE,
    "sales_invoice": DocType.SALES_INVOICE,
    "salary_slip": DocType.SALARY_SLIP,
    "credit_card": DocType.CREDIT_CARD,
    "transit_ic": DocType.TRANSIT_IC,
}
```

キー名は `DocType` の値そのまま（既存 4 件がその規約）。

**DoD**: T11a-1 が緑。`ensure_dirs()` が `test_images/credit_card/` と
`test_images/transit_ic/` を自動生成する（`local_test.py:52` の既存ループ経由。
`ensure_dirs` 自体は無改造）。

### T11a-3: module docstring の使い方欄を更新

`local_test.py:7-11` は 4 目録しか列挙していない。2 行足す。
`transit_ic` には**本番との差異**を 1 行註記する:

> 本番では nimoca はクレカと同じフォルダに混載し、プログラムが頁単位で
> 分流する（趙裁定 5、`doc_types.py:83-89`）。`test_images/transit_ic/` は
> ローカルで nimoca 単独を試すための入口であって、本番の構成ではない。

**DoD**: docstring に 6 目録が並び、上記註記が入っている。

### T11a-4: 全量テスト

`venv311/bin/python -m unittest discover -p "test_*.py"`

**DoD**: 1004 + 新規テスト件数 が緑。失敗 0。

### T11a-5: 真票結果の信頼性補強（**趙の拍板待ち。T11a-1〜4 とは独立**）

Codex 評審 #1 / #4 で昇格した項目。当初は「あれば良い」だったが、
F9 / F10 の実測で**「やらないと真票結果が信用できない」**に変わった。

`local_test.py` には、真票の結果を読み違えさせる**無音の穴が 2 つ**ある。
どちらも `main.py` には既に塞がれており、`local_test` にだけ無い。

| 穴 | 症状 | `main.py` の対応 |
|---|---|---|
| **穴 1**: `_audit_signal` を読まない | 截断→救済が起きた頁（`salvaged:N/M`）が**痕跡ゼロ**で「綺麗に成功した頁」に見える（F9） | `main.py:596-608` が監査タブへ verdict「分岐」1 行 |
| **穴 2**: 頁カバレッジ突合が無い | 32 頁のうち 31 頁しか出力されなくても成功扱いで `processed/` へ移動（F10） | `main.py:617-628` が監査タブへ verdict「欠落」1 行 |

選択肢:

| 案 | 内容 | 取 | 捨 | 代価 |
|---|---|---|---|---|
| **A** | 今回は触らない | 範囲を趙の裁定どおり 2 項に保つ | **真票の結果が「救済なし・頁欠落なし」であることを確認できない** | T8〜T10 の設計判断を、信頼性未確認のサンプルの上で行うことになる |
| **B（推奨）** | 穴 1・穴 2 の両方を `local_test.py` 内で塞ぐ。穴 1 ＝ `_audit_signal` → `append_audit_row`（≈10 行）、穴 2 ＝ `seen_page_nums` 突合 → 監査タブ 1 行 ＋ サマリーに「処理頁数 / 宣言頁数」を印字（≈12 行） | 真票の結果を額面どおり読める | 改動が `FOLDER_TYPE_MAP` の 2 行を超え、`process_local_file` 本体に及ぶ | 実装 ≈22 行＋テスト。全量再走 1 回。**改動は `local_test.py` 内に閉じる**（`main.py` / `ocr_engine.py` / `sheets_output.py` / 既存テストには触れない） |

**B が推奨な理由**: 本タスクの目的は「真票を見て T8〜T10 を決める」こと。
結果が信用できないならタスクの目的が達成されない。
なお B でも `test_pipeline_consumers.py` の不変式は**触らない**
（Codex 評審 #7・A4 との衝突を避けるため。新規テストで縛る）。

**趙が A を選んだ場合の運用回避策**: 真票実行時のコンソール出力を全部
ファイルへ落とし（`python local_test.py 2>&1 | tee run.log`）、
`grep -c "📄 文書 \["` で処理頁数を数え、`grep "取得漏れ\|salvage"` で
救済の痕跡を探す。手作業だが穴 1・穴 2 の両方を事後に検出できる。

---

## 4. 受入基準（機械判定できるもの）

| # | 基準 | 判定方法 |
|---|---|---|
| A1 | `FOLDER_TYPE_MAP` が `DocType.ALL` 全 6 件を覆う | `test_local_test_folder_map` 緑 |
| A2 | 番人が空振りしない（欠落を実際に検出できる） | 否定対照テスト緑 |
| A3 | 全量テストが緑 | `unittest discover` の `OK` |
| A4 | `local_test.py` 以外の生産コードが無変更 | `git diff --name-only` が `local_test.py` ＋ 新規テストのみ（T11a-5 案 B を採っても同じ。B の改動も `local_test.py` 内に閉じる） |
| A5 | 凍結対照群 `test_sheets_output.py` / `test_anomaly_detector.py` が byte 無変更 | `git diff --exit-code -- test_sheets_output.py test_anomaly_detector.py` |

**A6〜（真票実行時の確認。趙が実施）**

Codex 評審 #5 を受けて拡張。「タブが出た＝成功」で読み違えないための最低限。

| # | 確認項目 | 見る場所 |
|---|---|---|
| A6 | `LocalTest_カード明細` タブが出る | Sheets |
| A7 | 28 列の形（貸方＝未払金、補助科目＝カード名、G列の税区分表記） | Sheets |
| A8 | 黄金値 17,295 / 933,680 / 15,503 が F-4 の該当行に出る | Sheets |
| A9 | **処理頁数 ＝ 宣言頁数**（頁の無音欠落が無い） | コンソール（案 B ならサマリー行） |
| A10 | **救済痕跡が無い**（`salvaged:` / 「取得漏れ」が出ていない）。出た場合はその頁の行数を原票と突合 | 案 B なら監査タブ、案 A ならコンソール grep |
| A11 | `_unrecognized` の赤い提示行が MF 区に出ていない | Sheets |
| A12 | 頁エラー占位行（「⚠ ページ処理エラー」）が出ていない | Sheets |
| A13 | **真票は複製を投入する**（`local_test` は成功時に `test_images/processed/` へ `shutil.move` する。原票を直接置くと消費される） | 実行前の準備 |

---

## 5. テスト戦略

- **TDD**: T11a-1（RED）→ T11a-2（GREEN）。全局 CLAUDE.md §9 準拠
- **単体**: 新規 `test_local_test_folder_map.py`（AST 解析。venv 非依存）
- **集成**: 既存 `test_pipeline_consumers.py` が `local_test.py` を消費者として
  検出し続けることで担保（改動で壊れないことの確認）
- **E2E**: 真票 1 枚を趙が実行（機械判定不能。§4 A6）
- **変異検証**: T11a-1 の否定対照が変異検証を兼ねる（欠落 1 件で赤くなる）

---

## 6. 影響面

| 対象 | 影響 |
|---|---|
| 本番（miniPC） | **無し**。miniPC は未 pull、かつ `local_test.py` は `main.py` から一切参照されない |
| 生産スプレッドシート `MF_Import_Data` | 真票実行時に `LocalTest_カード明細` タブへ書く。**業務タブには触れない**が、同名の `LocalTest_*` タブが既に在れば**追記**であって新規作成ではない（Codex 評審 #6 前半・採納）。実行前に既存の `LocalTest_*` タブを消すか開始行を控えること。趙が実行後に手動削除する（22:00 JST の GAS バックアップ前）。`flush()` は no-op なので取引No の書き戻しは無い（F11） |
| Drive の入力フォルダ | **無し**。`local_test.py` はローカル `test_images/` しか読まない |
| Gemini API | 真票 1 枚分の呼出コスト（32 頁の 1 ファイル分） |
| 既存 4 doc_type | **無し**。map への追加はキー 2 件のみ |

---

## 7. リスクと回退

| # | リスク | 緩和 | 回退 |
|---|---|---|---|
| R1 | 新テストが `import local_test` して paddleocr を引き込む | AST 解析で読む（T11a-1 の DoD に明記） | — |
| R2 | 真票実行で `LocalTest_カード明細` が 22:00 を跨ぎ `MF_Backup` に永久残存 | 趙が 22:00 前に手動削除（既裁定） | `MF_Backup` から該当行を手動削除 |
| R3 | 救済（`salvaged:*`）・頁欠落が**無音**で通り、真票結果を額面どおり読んでしまう | §3 T11a-5。案 A なら §3 末尾の `tee` ＋ grep 運用で事後検出 | T11a-5 案 B を後追いで実施 |
| R4 | `transit_ic` 目録の存在を「本番も独立フォルダ」と誤読される | T11a-3 の docstring 註記 | — |
| R5 | 既存の `LocalTest_カード明細` タブへ追記され、前回分と混ざる | 実行前に該当タブを削除（§6） | 行を手動削除 |
| R6 | 真票の原本が `processed/` へ移動して消費される | 複製を投入する（A13） | `processed/` から戻す |

**回退手順**（Codex 評審 #7・採納。無条件の `checkout` は書かない）:

1. `git status --short` と `git diff --stat -- local_test.py` で、他人／他 session の
   未コミット改動が混ざっていないことを**先に確認する**
2. 混ざっていなければ `git checkout -- local_test.py`
3. `rm test_local_test_folder_map.py docs/plans/2026-08-19-t11-partial-local-test-card.md`

commit していないため git 履歴には何も残らない。

---

## 8. 辯論記録（Codex 評審・2026-08-19）

`codex-cli 0.147.0`。ラウンド 1 で 7 件、ラウンド 2 で反論 2 件を複審。

| # | Codex の指摘 | 裁決 | 理由 |
|---|---|---|---|
| 1 | **High** T11a-5 案 A は誤導結果を生む。`salvaged:*` は監査タブのみで MF に痕跡が出ない | **採納** | `card_salvage.py:263-265` の docstring が明文で規定。当方の「MF 提示行だけ見ればよい」は**誤り**。T11a-5 を「あれば良い」から「やらないと結果が信用できない」へ昇格し、推奨を A から B へ変更 |
| 2 | **Medium** 「本改動が有効化する新しい欠口」は言い過ぎ。RECEIPT 経路でも既に `_audit_signal` は出ていた | **採納** | `ocr_engine.py:2321-2322` で確認。§2 の表現を「既存欠口が line_mode 真票テストで高リスク化する」へ修正 |
| 3 | **Medium** `FOLDER_TYPE_MAP == DocType.ALL` の番人は過度設計。`local_test` は開発工具であって生産経路ではない | **修改採納**（invariant は維持） | Codex 自身が示した第 2 案「工具契約として明文宣言する」を採る。根拠＝本専案は doc_type 並行登録表を既に 6 枚持ち、各表とも漏登録で事故を起こして構造自証へ移行済み。`FOLDER_TYPE_MAP` は 7 枚目。**複審で Codex は原意見を撤回**し、契約明文化を条件に invariant を支持 |
| 4 | **High** `main.py` の頁カバレッジ突合が `local_test` に無い。31/32 頁でも成功に見える | **採納** | `main.py:617-628` と `local_test.py` 全文で確認。T11a-5 に「穴 2」として統合し、案 B に含めた |
| 5 | **Medium** 真票の受入条件が不足（頁数・行数・救済痕跡・部分エラー・原票消費） | **採納** | §4 の A6〜A13 へ拡張。文書のみの改動で代価ゼロ |
| 6 | **Medium** 「既存タブには触れない」は不成立。既存タブへ追記される。加えて `flush()` が取引No を書き戻す | **部分採納** | 前半＝採納（§6 修正）。**後半＝駁回**：`sheets_output.py:765-767` の `flush()` は `pass` で回写ゼロ。**複審で Codex 撤回** |
| 7 | **Low** `git checkout -- local_test.py` は他人の未コミット改動を消す | **採納** | §7 の回退手順を「先に `git status` で確認」する 3 段へ変更 |

### 勝敗判定（fatboyslim Phase 1 第 3 步）

- 駁回・修改した 2 件（#3 / #6 後半）をラウンド 2 で Codex へ回餵
- **Codex は両方とも撤回**（「反論 1：成立。撤回」「反論 2：成立。撤回」）
- 判据「対抗者が重提しなければ我方勝」により**当方の主張を維持**
- ただし #3 について Codex が付けた条件（契約境界を docstring に明記）は
  有益なので**取り込む**（T11a-1 に反映済み）

### 全採納でも全駁回でもないことの確認

7 件中：採納 5・部分採納 1・修改採納 1。駁回は #6 後半のみで、
実測（`sheets_output.py:765-767`）に基づく。

---

## 9. 実施記録（2026-08-19）

### 完了したもの

| 任務 | 状態 | 証拠 |
|---|---|---|
| T11a-1 番人テスト（RED） | 完了 | 初版で `['credit_card', 'transit_ic']` を名指しして赤 |
| T11a-2 map へ 2 件追加（GREEN） | 完了 | `local_test.py:53-60`。既存 4 件の規約どおり key ＝ doc_type 値 |
| T11a-3 docstring 更新 | 完了 | 6 目録 ＋ nimoca 混載の本番差異 ＋ 原票が `processed/` へ移動する註記 |
| T11a-4 全量テスト | 完了 | **1004 → 1028 tests 緑**（+24） |
| T11a-5 信頼性補強 | **未着手・趙の拍板待ち** | §3 T11a-5 |

### 受入基準の結果

| # | 基準 | 結果 |
|---|---|---|
| A1 | `FOLDER_TYPE_MAP` が `DocType.ALL` 全 6 件を覆う | ✅ 24 tests 緑 |
| A2 | 番人が空振りしない | ✅ **変異 12 種を 12 殺**（下記） |
| A3 | 全量テスト緑 | ✅ `Ran 1028 tests ... OK` |
| A4 | `local_test.py` 以外の生産コード無変更 | ✅ `1 file changed, 18 insertions(+)` |
| A5 | 凍結対照群 byte 無変更 | ✅ `git diff --exit-code` 通過 |
| — | `ensure_dirs()` が実際に目録を作る | ✅ 実行して `test_images/credit_card/` `transit_ic/` を確認 |
| — | 脱 venv（系統 python3 3.9.6）でも番人が動く | ✅ 24 tests OK |

### 変異検証（12 種・全殺）

`_binds_name` の def/class 無視 ／ alias 無視 ／ except as 無視 ／
註釈のみ AnnAssign の扱い ／ `ast.Load` 限定の撤廃 ／ 入れ子スコープ刈り込みの
撤廃 ／ ローカル shadow 除外の撤廃 ／ 直下外束縛の不拒否 ／ 直下限定の撤廃 ／
`missing_doc_types` が常に `[]` ／ `ast.MatchAs` 直書き（3.9 破壊）／
右辺 walrus の不走査。

---

## 10. `/simcodex` 評審記録（4 ラウンド）

各ラウンド ＝ simplify 並行パネル（reuse / simplification / efficiency /
altitude）＋ `codex exec`。verify ＝ 全量 `unittest discover`。

| R | codex | パネル | 主な処置 |
|---|---|---|---|
| 1 | P1×2 P2×4 | reuse 0・efficiency 0（negligible）・simplification 1・altitude 1 | 解析を **module 直下限定**へ ／ `_folder_name_of` 分離 ／ `DocType.ALL` の型検査 ／ `**unpacking` 拒否 ／ 変異テストを `missing_doc_types` 経由へ ／ `setUpClass` |
| 2 | P1×2 P2×1（前輪 5 件を「成立」と確認） | reuse 0・efficiency 0・simplification 2・altitude 3 | consumer 検出を**直下関数限定 ＋ ローカル shadow 除外**へ ／ module 級制御構文内の再束縛を拒否 ／ subTest 表へ集約 ／ **docstring の虚偽記述を削除**（下記） |
| 3 | P1×1 P2×1（前輪 3 件「成立」） | simplification 2・altitude 0（高度は妥当と再確認）・reuse 1 | `_binds_name` で `def`/`class`/`import as`/`except as`/`match` を網羅 ／ 計数の引き算を直接判定へ書換 |
| 4（確認） | P1×1 P2×1・**残 P0 無** | — | **3.9 互換の破壊を修復** ／ 右辺 walrus の走査 |

### ラウンド 2 で自分の虚偽記述を撤回した件

初版の docstring に「`main()` は 0 件時にフォルダ一覧を印字するので
完全な無音ではない」と書いた。altitude パネルが実コードで反証:
`scan_local_files` が数えるのは**全登録フォルダの合計**で、`receipt/` に
旧ファイルが 1 つでも在ればその分岐は走らない。**未登録フォルダは実行時に
完全な無音**である。当該記述を削除し、逆の事実（この番人が唯一の信号）を
明記した。高度の判断（import 時 `RuntimeError` にしない）は
重大性の非対称で独立に立つため変更なし —— ラウンド 3 の altitude パネルが
独立に再検証して同じ結論。

### 駁回した指摘

| 指摘 | 出所 | 駁回理由 |
|---|---|---|
| `flush()` が取引No を書き戻す | codex R1 | `sheets_output.py:765-767` は `pass`。**codex が複審で撤回** |
| 番人が過度設計（`DocType.ALL` 全覆蓋は厳しすぎ） | codex R1 | 工具契約として明文化する形で維持。**codex が複審で撤回** |
| import 時 `RuntimeError` にすべき | altitude R1 | 生産経路（無限リトライで Gemini を焼く）との重大性差 ／ 趙が**今から使う工具**への行為変更。R3 で独立再検証し維持 |
| stdlib `symtable` で手書き scope 走査を置換 | reuse R3 | 指摘自体は正しいが、第 2 の呼出点（module 級再束縛）は `symtable` の `is_assigned()` が真偽値のため置換不能。採ると 1 つの概念に 2 機構が並立し `ast.unparse` の往復も増える。純損 |

### 自分で見つけた不備（変異検証が暴いた）

ラウンド 3 で codex P2 に応えて `_count_bindings` に「註釈のみ AnnAssign を
除外」を入れたが、変異検証で **M2 が生存**。調べると (1) module 直下は
`_assigns_name` が先に `continue` するので到達不能な死码、(2) PEP 526 では
**関数スコープの裸註釈は本当にローカル束縛を作る**ので除外は意味が逆。
除外を撤回し `_top_level_bindings` 側へ一本化した。
**テストが緑でも変異が生きているなら、その修復は効いていない。**

---

## 11. T11a-5 実施記録（趙拍板 2026-08-19「この穴は必ず塞ぐ。main と同じにすること」）

案 B を採用。実施中に**当初報告した 2 穴以外にさらに 2 件の乖離**が出た
（Codex 評審＋死変数の発見）。「main と同じにする」という裁定の射程に
入るため併せて塞いだ。

### 塞いだ乖離 4 件

| # | 乖離 | 塞ぐ前に起きること | main 側の実装 |
|---|---|---|---|
| 1 | `_audit_signal` を読まない | 截断→救済された頁（`salvaged:N/M`）が痕跡ゼロ。`card_salvage.page_marks` の規定で MF 側には出ない | `main.py:596-608` |
| 2 | 頁カバレッジ突合が無い | 32 頁中 31 頁でも成功扱いで歸檔 | `main.py:617-635` |
| 3 | 除外ページの書込失敗が裸 | 監査タブ失敗 → 例外が `process_local_file` ごと落とし、留痕も残りの頁の処理も消える | `main.py:355-417` |
| 4 | `excluded_pages` が死変数 | 数えるだけで誰も読まない。「何頁が封筒として除外されたか」が結果から読めない | `main.py:679-681` |

乖離 3 は行き先で扱いが違う。**MF タブ行き**（社保通知書）はその行が頁の
唯一の出力なので、書けなければ頁を失敗として数え再試行に載せる。
**監査タブ行き**（封筒）は失敗したら MF の認識不能行へ退避して必ず可視化する。

### main と意図的に違う 1 点

カバレッジの起点。main は `range(1, ...)`、`local_test` は
`range(start_page, ...)`。`--start-page N` は `local_test` にしか無く
（`docs/plans/2026-08-17-split-pdf-midway-failure.md` の G8）、1 起点を
写経すると毎回 p1..p(N-1) が偽の「欠落」になる。
`test_start_page_shifts_the_coverage_window` と
`test_start_page_still_detects_a_gap_inside_the_window` の 2 本で
「窓はずらすが窓の中は見逃さない」を固定した。

### 副産物

`_unrecognized_placeholder()` を新設（`main._build_unrecognized_placeholder`
と同形）。同じ dict リテラルが 3 箇所（除外の MF 提示行・監査失敗の退避行・
部分ページエラーの占位行）に散っていたため。

### 検証

- 新規 `test_local_test_page_audit.py`：**24 tests**
- 全量：1040 → **1052 tests 緑**
- **変異 17 種を 17 殺**（1 種は初回生存 → 死変数だったと判明 → 乖離 4 を塞いで殺した）
- `local_test.py` 以外の生産コードは無変更。凍結対照群も byte 無変更

### 多エージェント評審（30 agents・4 次元 × 対抗検証）

4 次元（parity / 無音失敗 / テスト品質 / 高度）で findings を出し、
1 件ずつ独立の反証エージェントに掛けた（「refute せよ、迷ったら refuted」）。

**26 件中 23 件が反証で落ち、3 件が生き残った。** 生き残った 3 件は
**すべて「コードは正しいがテストが変異を殺せない」**類で、反証側が
実際に変異を注入して生存を実証している:

| # | 生存変異 | 何が起きるか |
|---|---|---|
| R1 | `range(start_page + 1, ...)` | **窓の下端の欠落を見逃す**。1 頁目が消えた 32 頁の明細を「完全な 32 頁」として読む —— IP-401 そのものの形 |
| R2 | `page_num=1` ハードコード | 分岐行が常に p1 を指す。頁 19 が救済されても「p1 を見ろ」と言う。頁番号は「どこを読み直すか」を伝える唯一の欄 |
| R3 | `last_total_pages` を `continue` の下へ | **全頁が封筒の PDF で末尾が消えると突合ごとスキップ**され無音欠落に戻る |

3 件とも fixture の穴だった —— 欠落 fixture が全部「窓の内側」に穴を開け、
分岐 fixture が全部「1 頁ファイルの 1 頁目」で、カバレッジ fixture が
全部「正常頁で始まる」。テストを 3 本追加・1 本の fixture を多頁化して
3 変異とも殺した（再実測済み）。

**反証で落ちた 23 件のうち、運用上有用だったもの**: 「local_test の監査行が
生産の監査タブに混ざり、開発実行と区別できない」という指摘は反証された ——
**原票URL 欄が確定的な判別子**になる。生産経路（`main.py:483-485`）は必ず
Drive URL を入れ、`local_test` は全書込で `""` を渡す。テスト行の掃除は
URL 欄が空の行で絞り込めばよい。

### 変異検証が暴いたこと

`excluded_pages` を「記録成功の前に数える」変異が**生き残った**。調べると
`local_test` ではこの変数が一度も読まれておらず、増やしても減らしても
外部に差が出ない死変数だった。main は同じ値を使って除外内訳を印字している。
**テストで殺せない変異は、たいてい壊れているのがテストではなく本体である。**

---

## 12. 真票テストの結果（2026-08-19 実施・趙）

**投入**: `アメックスカード_6枚_20260723-150955.pdf のコピー.pdf`（6 頁、1.58MB、複製）
**コマンド**: `venv311/bin/python local_test.py --only-file アメックス`

### 今回塞いだ 4 穴は**実データで誤報ゼロ**

| 穴 | 結果 |
|---|---|
| 頁カバレッジ突合 | 警告なし → 6/6 頁が出力された。**誤報なし** |
| `_audit_signal` | 「分岐」行なし → 截断救済は発生していない |
| 除外ページの失敗語義 | 除外ページ自体が無かった（未発火） |
| 除外内訳の印字 | 除外 0 のため未発火。**誤報なし** |

頁別行数 `4+6+4+6+4+0 = 24`。Sheets は仕訳 24 行 ＋ 認識不能 1 行 ＝ 25 行。
**一行の過不足も無い。**

### 判明した 3 件

1. **p1・p2 と p3・p4 が同一内容**（金額・日付・店名が完全一致）。
   **趙確認: スキャンの失誤による重複**。`page_dedup` の重複頁短絡が
   まさにこれ。**T9 の設計は変更不要**。
   ただし 2 回の読取結果は文字レベルで違う（補助科目の有無、
   `ETC NO` / `ETC NO :` / `ETC NO:` の 3 通り）。
   **指紋を Gemini の文字出力に基づけてはいけない**——金額基準（`rows[].amount`）
   という既存設計が正しいことの実証。
2. **補助科目（カード名）の読取が不安定**。同一頁の中で前半 4 行には
   カード名が入り、後半には入らない。カード名は**券面級**の情報なのに
   行級で読ませている。T8 で頁級に一度抽出して全行へ配る。
3. **p6 が「認識不能」の赤行になった**。実物は
   「ご利用代金明細書 2/3 ページ ポイント・インフォメーション」——
   獲得ポイント 1,781、振込先口座、外貨換算の説明、用語説明のみ。
   **仕訳対象が構造的に無い**。Gemini の判断は正しく、**去向だけが誤り**。

---

## 13. T8 の要求台帳（趙 2026-08-19 提示・従業員要望）

| # | 要求 | 現状 | T8 でやること |
|---|---|---|---|
| 1 | 明細書の**総額**で「各明細の合計＝総額」を検算 | `RECON_POLICY` の `amount_required` が既存 | T9 で接線すれば発火。T8 は不要 |
| 2 | **前回分は無視** | **実装済・二層**。`card_prompts.py:185`（kind=`carry_over`）＋ `card_entries.py:115-116` の `_CARRY_OVER_LABELS` によるプログラム側兜底 | **追加規則は不要**。前回分は「記帳しないが検算には要る」（`card_entries.py:11-12`）——券面総額は前回分を含むので、検算から外すと永久に合わない |
| 3 | 各明細名目を記録 | line_mode で実装済 | 真票で検証済み。不要 |
| 4 | 主副カード合印時、**A の合計・B の合計を記録に入れない** | `card_prompts.py:159`「合計行・小計行を rows に入れないでください」は在る。**各人小計まで覆えているかは版面を見ないと不明** | 参考資料で版面確認 → 必要なら prompt とプログラム側兜底を追加 |
| 5 | **ポイント頁は記録しない**。ボーナス・ポイントと識別して破棄。「認識不能」にしない | 行級のポイント除外は在る（`card_prompts.py:161-162`、`card_entries.py:111` `_POINT_HEADING_TOKEN`）。**頁全体がポイント情報の場合の規則が無い** | **判定＋接線の 2 件**（下記） |
| 6 | **リボ払い・分割払いの頁も記録しない** | prompt にもプログラム側にも**無い**。真の欠口 | 判定を追加。去向は #5 と同じ |
| 7 | 重複頁は**一份だけ記録** | `page_dedup` が既存 | T9 で接線。設計変更不要（上記の実証あり） |
| 8 | 高額に色を付ける | `anomaly_detector` ＋ `tag_rules` | 真票で検証済み（30 万・58.7 万に黄系）。不要 |

### #5 / #6 の去向（趙裁定 2026-08-19）

**MF 区には書かない。`_除外ページ監査` タブへ 1 行だけ残す。**

「完全に何も残さない」ではない理由: CLAUDE.md の IP-401 不変式
「逐頁ループに入った頁は必ず 1 件以上 yield する」。2026-07-30 の事故
（54 枚アップロードで仕訳 53 件、枚数を数えるまで気づけなかった）の再発防止。
監査タブは取引No を消費せず MF インポートにも入らないため顧客影響はゼロ、
かつ将来この判定が誤爆したとき（真の明細頁をポイント頁と誤判）に
**唯一発見できる場所**になる。

既存機構をそのまま使う: `_excluded_page=True` /
`_exclude_destination=EXCLUDE_DEST_AUDIT_TAB`（封筒と同じ経路）。
タブ名 `_除外ページ監査` は `sheets_output.py:29`、`_` 始まりは
`:37-45` の起動時検査が守っている（GAS の 22:00 削除対策）。

### T8 着手前に必ず片付ける 3 件

1. **`ocr_engine._yield_page_results` の分岐構造を通読し、line_mode 経路の
   除外出口をどこに挿すか決める。** 現状 `_yield_line_mode_results` には
   `_excluded_page` の出口が**一つも無い**。封筒判定は
   `envelope_filter=True`（RECEIPT の PDF 逐頁ループ、`ocr_engine.py:2620`）
   でしか有効にならず、カード経路は届かない。
   **挿す位置を誤ると「判定失敗時に真の明細頁を呑む」**ので、
   推測で設計しないこと
2. **参考資料を読む**:
   `~/Desktop/井戸会計事務所/任務3/西久保令和4年～令和6年調査資料/アメックスカード明細/2023年1月.pdf`
   → 主副カードの版面を確認し #4 の判定条件を決める。同時に #6
   （リボ・分割頁）の実際の見え方も採取する
3. **P2 テスト 2 本の追加**（趙の通知待ち）: 多頁にまたがる `_audit_signal`、
   `EXCLUDE_DEST_MF_TAB` 成功経路の memo。どちらも評審で変異生存が実証済み

---

## 14. 未処置（趙の拍板待ち・優先度順）

| P | 項目 | 詳細 |
|---|---|---|
| ~~P1~~ | ~~T11a-5 真票結果の信頼性補強~~ | **完了**（§11。趙拍板 2026-08-19） |
| ~~P1~~ | ~~`CLAUDE.md:71` の同期リスト~~ | **完了**（趙拍板「甲」2026-08-19。`local_test.FOLDER_TYPE_MAP` を追記し、7 表目の段落も追加） |
| P2 | `local_test.py` docstring の CSV 記述 | `:4` 「Gemini API のみ」・`:16` 「MF_Import_Data.csv を確認」は Sheets 出力版の現状と不一致（既存の誤り。本変更が持ち込んだものではない） |
| P2 | `missing_doc_types` 相当の述語が 3 箇所 | `ocr_engine._validate_doc_type_registries` ／ `card_reconciliation._validate_recon_policy` ／ 本テスト。各 1 行で YAGNI 圏内 |
| P2 | 番人テストの分量 | 722 行。4 ラウンドの評審で積み上がった。妥当と判断したが、趙が過大と見るなら削る候補は §10 の「駁回」欄には無く、`UNREADABLE_SOURCES` の 12 例の一部 |

---

## 15. T8 着手前の必須 3 件の結果（2026-08-19 実施）

### 15.1 件 1: `_yield_page_results` の分岐構造と、除外出口の挿入位置

**通読した実物の構造**（`ocr_engine.py:2207-2342`）:

```
_yield_page_results(doc_type, raw_data, ocr_text, ocr_conf, prefix, envelope_filter)
├ G1  raw_data 非 dict → _unrecognized、return          （型ゲート。全経路）
├     _apply_ocr_overrides(...)
├ G2  _is_social_insurance_notice → _excluded_page + MF_TAB、return
│       ※ doc_type 不問・envelope_filter 不問・**entries 不問**（業務規則）
├ B-RECEIPT （doc_type == RECEIPT）
│  ├ page_results = _normalize_receipt_results(...)
│  ├ is_envelope = envelope_filter and _is_envelope_page(...)
│  ├ page_results 空 かつ is_envelope → _excluded_page + AUDIT_TAB、return
│  ├ page_results 空          → _unrecognized、return
│  ├ is_envelope（entries 有）→ 先頭に _audit_signal、全行 yield、return
│  └ 全行 yield、return
└ B-OTHER
   ├ builder 無し → 何も yield せず return（防御的。到達しない想定）
   ├ result = _build_doc_result(doc_type, raw_data, builder(raw_data))
   │            ※ ここで `_unrecognized = not entries` が決まる（:1932）
   ├ not _is_line_mode → yield result、return
   └ yield from _yield_line_mode_results(result, raw_data, ocr_text, prefix)
```

**挿入位置の候補と、証拠による絞り込み**:

| 候補 | 位置 | 判定 | 根拠 |
|---|---|---|---|
| P-A | G2 と同層（社保の直後） | **排除** | doc_type gate が無い。「ポイント」語はポイントカード提示の領収証券面に普通に出る。RECEIPT を巻き込んで誤爆する |
| P-B | `_build_doc_result` の**後**、`_is_line_mode` 分派の直前 | **採用** | `result["entries"]` / `result["_unrecognized"]` を見られる唯一の位置。PDF 逐頁ループと尾段（単頁 PDF・画像）の**両方**を通る（尾段は `ocr_engine.py:2760` が既定 `envelope_filter=False` で同関数を呼ぶ） |
| P-C | `_yield_line_mode_results` の内部 | **排除** | 同関数の docstring が「**ここが足すのは注記であって頁の去向ではない**…AD-0 / T9 の Disposition 軸には乗らない」と明記（`ocr_engine.py:2378-2380`）。去向を入れると T9 の設計と衝突する |

**なぜ P-B なら「判定失敗時に真の明細頁を呑む」を構造的に防げるか**:
P-B は `result["entries"]` を持つ。IP-401 が封筒判定に施したのと同じ
「entries を組めていれば棄却経路は存在しない」ゲートをそのまま適用できる。
判定が誤爆しても、明細行が 1 行でも取れている頁は絶対に除外へ落ちない。

**消費側は無改修で足りる**（実測）:
- `main.process_file:543` の `_excluded_page` 分岐は **doc_type を見ない**。
  line_mode 由来の除外もそのまま監査タブへ落ちる
- `error_pages` には数えず `count` には数えるので、全頁除外でも
  `count>0 → Failed 無限リトライ` にならない（`main.py:530-565` のコメント）
- `local_test.py:160` も同形（部分 T11 で揃え済み）

**未確定（実測が要る）**: P-B のゲートを「entries 空のときだけ」にするか、
社保と同じく entries を見ずに短絡させるか。判準は
**「防ぐ誤りが〈Gemini が entries を組めない〉か〈Gemini が誤った entries を組む〉か」**。
- #5 ポイント頁 → **前者と実測済**（§12 の真票 p6 が entries=0 → 認識不能）。
  entries 空ゲートで足りる
- #6 リボ頁 → **未実測**。§15.2 の p8 は円金額と日付を持つので、
  Gemini が偽の明細を組む可能性を排除できない。推測で決めない

### 15.2 件 2: 参考資料の版面（`アメックスカード明細/2023年1月.pdf`・全 8 頁）

`pdftoppm -r 110` で全 8 頁を採取して実見した。**主副カード合印の実物**。

| 頁 | 内容 |
|---|---|
| 1 | 券面ヘッダ ＋ お支払い金額内容（前回分口座振替 −635,375）＋ `今月ご利用額 西久保 智宏 様`（主カード区画の見出し）＋ 明細 4 行 |
| 2-4 | 主カードの明細続き（p2 の下部に用語説明の長文） |
| 5 | 主カード明細の続き → **`西久保　智宏　様　今月ご利用額合計　350,218`** → **`今月ご利用額　西久保　絵理　様`（副カード区画の見出し・会員番号 71016）** → 副カード明細 |
| 6 | 副カード明細 4 行 → **`西久保　絵理　様　今月ご利用額合計　162,615`** → **`今回ご利用・ご請求金額合計　512,833`** |
| 7 | **ポイント・インフォメーション**（#5 の実物） |
| 8 | **ペイフレックス登録・利用・請求状況一覧**（#6 の実物） |

検算: 350,218 ＋ 162,615 ＝ 512,833。**券面の 3 数値は厳密一致**。

#### #4（各人小計を記録に入れない）の判定条件

- 券面の逐語は **`<氏名> 様　今月ご利用額合計`**。ラベル自体に「合計」を含む
- **構造特徴: 小計行・総額行は左端の日付欄が空**。真の明細行は必ず `M月D日` を持つ
- 区画の切替は **`今月ご利用額 <氏名> 様` ＋ 会員番号**の 2 行（p1 と p5 に出現）
- prompt 側は `card_prompts.py:159-160`「合計行・小計行を rows に入れないでください」
  が**文面上は覆っている**（このラベルは字義どおり合計行）
- **プログラム側の兜底は無い**（実測）。`ocr_engine._is_subtotal_line` は
  領収書経路専用（`:1502` の `_build_entries_for_single_doc` からのみ）。
  `card_entries.resolve_booking_kind` に小計行の分岐は無く、
  `_CARRY_OVER_LABELS` にも「今月ご利用額合計」は当たらない。
  Gemini が rows に入れてしまえば **350,218 がそのまま記帳される**

#### #6（リボ・分割頁）の実際の見え方（p8）

- 見出し: `ペイフレックス登録・利用・請求状況一覧（金額はすべて円）`
- 表 1「ペイフレックス登録状況」: 登録プラン名 / **登録日 2020年10月8日** /
  **リボルビング払い利用可能枠 1,500,000** / 基本手数料率 14.90 /
  **あとリボ変更締切日 2023年1月30日**
- 表 2「今回ペイフレックス請求金額明細」: 5 項目すべて **0**
- 下半分は用語説明の長文

**危険点**: この頁は**円金額と日付の両方を持つ**。
「登録日 2020年10月8日 ／ 1,500,000」を明細 1 件として組まれると、
存在しない 150 万円の仕訳が入る。§15.1 の未確定はここに由来する。

#### #5 の実際の見え方（p7）

- 見出し: `ポイント・インフォメーション`
- `今回の獲得ポイント 18,980` / `今回のボーナス・ポイント 0` / `今回の調整ポイント 0`
- `【獲得ポイント】カードの種類 / 会員番号 / ポイント数` / `◆獲得ポイント計 18,980`
- **日付行なし・円貨なし**（18,980 はポイント数）。構造的に仕訳対象ゼロ

### 15.3 件 3: P2 テスト 2 本（実施済・変異検証済）

`test_local_test_page_audit.py` に 3 メソッドを追加（全量 1055 → **1058 tests 緑**）。

| 追加 | 殺す変異 | 実測 |
|---|---|---|
| `AuditSignalIsRecordedTest.test_every_signalled_page_gets_its_own_branch_row` | 分岐行を先頭 1 本に限定（`if audit_signal and not _branch_done:`） | 変異 A で当該テストのみ FAIL |
| `ExcludedPageToMfTabCarriesItsReasonTest.test_the_mf_placeholder_carries_the_producer_memo` | `r.get("memo", "")` → `""`（顧客に文言が届かない） | 変異 B で当該テストのみ FAIL |
| `ExcludedPageToMfTabCarriesItsReasonTest.test_the_mf_destination_writes_no_audit_row` | MF 行きでも監査タブへ二重書き | 変異 C で当該テストのみ FAIL |

既存 `test_an_audit_signal_produces_a_branch_row` は 1 頁にしかシグナルを
載せないため「先頭 1 本だけ」変異が生存していた。MF 行きは**失敗経路**
しか固定されておらず、成功経路の memo が空に潰れても誰も気づかなかった。

### 15.4 この 3 件で見つかった **T8 スコープ外**の所見

| P | 所見 | 詳細 |
|---|---|---|
| P1 | **主副カードで検算が「合計不整合」に落ちる**（T9） | `card_reconciliation.TOTAL_LABEL_SECTION`（`:97-105`）は「今月ご利用額合計」と「今回ご利用・ご請求金額合計」を**どちらも `SECTION_CURRENT_USAGE`** に登録している。2023年1月.pdf は同一区画に 350,218 / 162,615 / 512,833 の 3 値が並ぶ。`_choose_section_and_total`（`:681-688`）は `len(current) > 1` で **`"合計不整合"`** を返すので、健全な券面で偽の不整合が出る。頁ヘッダの `member_no` は全頁 71008（基本会員）なので、カード別バケツにも分かれない。**T9 で接線する前に決着が要る** |
| P2 | #4 のプログラム側兜底が無い | 上記 §15.2。prompt 頼み。T8 で入れるかは Plan で決める |
