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

## 12. 未処置（趙の拍板待ち・優先度順）

| P | 項目 | 詳細 |
|---|---|---|
| ~~P1~~ | ~~T11a-5 真票結果の信頼性補強~~ | **完了**（§11。趙拍板 2026-08-19） |
| ~~P1~~ | ~~`CLAUDE.md:71` の同期リスト~~ | **完了**（趙拍板「甲」2026-08-19。`local_test.FOLDER_TYPE_MAP` を追記し、7 表目の段落も追加） |
| P2 | `local_test.py` docstring の CSV 記述 | `:4` 「Gemini API のみ」・`:16` 「MF_Import_Data.csv を確認」は Sheets 出力版の現状と不一致（既存の誤り。本変更が持ち込んだものではない） |
| P2 | `missing_doc_types` 相当の述語が 3 箇所 | `ocr_engine._validate_doc_type_registries` ／ `card_reconciliation._validate_recon_policy` ／ 本テスト。各 1 行で YAGNI 圏内 |
| P2 | 番人テストの分量 | 722 行。4 ラウンドの評審で積み上がった。妥当と判断したが、趙が過大と見るなら削る候補は §10 の「駁回」欄には無く、`UNREADABLE_SOURCES` の 12 例の一部 |
