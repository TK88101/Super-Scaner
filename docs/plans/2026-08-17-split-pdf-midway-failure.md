# Plan: `_split_pdf_pages` の中途失敗で後続頁が消える（IP-401 §11.0）

- 起案: 2026-08-17
- 前提: 直前の commit `ecabba3`（IP-401 非 dict raw_data）で 791 tests 緑
- 発見元: `docs/plans/2026-08-17-ip401-nondict-rawdata.md` §11.0
  （simcodex Round 2 の zero-yield 監査）
- 趙裁定 2026-08-17: 「先 git commit 然後 修你說的這個 既存的P1」→ **着手**

---

## 0. 事実表（コマンド出力で確認済み）

| # | 事実 | 根拠 |
|---|---|---|
| G1 | `_split_pdf_pages` は `try` が **for ループ全体**を包み、例外を握って `print` ＋ `return` する（ジェネレータが静かに終わる） | `ocr_engine.py:421-442` |
| G2 | `total_pages` は**ループ前に一度**算出される（`len(reader.pages)`） | `ocr_engine.py:426` |
| G3 | **【推測】** pypdf の `reader.pages` は遅延解決なので、特定頁の内容ストリームはその頁に触れて初めて解析される。ゆえに **k 頁目まで成功して k+1 頁目で `PdfReadError`** という壊れ方が起きうる | `writer.add_page(page)` / `writer.write(buf)` が逐頁で走ることは `ocr_engine.py:429-432` で確認済み。**ただし pypdf の遅延評価そのものは本 repo のコードだけでは証明できない**（Codex 評審の指摘）。なお本 Plan の修正は「producer が `total` より少なく産出した」全ての原因に効くので、**G3 が仮に外れていても設計は成立する** |
| G4 | 消費側 `for page_info in itertools.chain([first_page], page_gen)` からは、中途失敗と「PDF が本当に k 頁だった」が**区別できない**（どちらもジェネレータが尽きる） | `ocr_engine.py:2245` |
| G5 | 頁 k+1..total は per-page try に**一度も入らない**ので `_page_error_payload` が作られず、`seen_pages` にも載らない | `ocr_engine.py:2245-2334` |
| G6 | `total` は `first_page["total_pages"]`（＝pypdf 申告値）なので**中途失敗の影響を受けない**。カバレッジ突合は `missing = k+1..total` を正しく算出できる | `ocr_engine.py:2222`, `:2339` |
| G7 | 現行のカバレッジ突合は**警告 print のみ**（成否判定を変えない。§8-中7 の裁定で P2 繰延） | `ocr_engine.py:2339-2344` |
| G8 | `main` 側にも同種の突合が在り、そちらは**監査タブに「欠落」1 行**を書く。ただし範囲は**同じではない** —— `ocr_engine` 側は `range(start_page, total+1)`、`main` 側は常に `range(1, last_total_pages+1)`。本番 `main` は `start_page` を渡さない（`local_test.py --start-page N` 専用）ので実害は無い | `main.py:614-628`。差分は Codex 評審の指摘 |
| G11 | `failed_page_notes`（`{page_num: 具体的な説明}`）は **`_excluded_page` の監査失敗経路でしか埋まらない**。普通の `_page_error` 頁は memo を保存せず、partial_error 集計行には**頁番号しか出ない** | `main.py:500`（宣言と意図）/ `:546`（唯一の書込）/ `:522-526`（`_page_error` は memo を捨てて continue）。Codex 評審 P1 で判明 |
| G9 | 前 k 頁が成功していると `count > 0` かつ `error_pages == 0` → `count == error_pages` が偽 → **Success → 歸檔** | `main.py:635`, `:667` |
| G10 | 既存テスト `test_coverage_warning_fires_when_a_page_yields_nothing` は `_yield_page_results` に `iter([])` を注入し、**「p1 が出力に現れないこと」を積極的に assert** する | `test_ip401_regression.py:140`, `:168` |

### 現状の帰結

| | 状態 |
|---|---|
| ファイル | **歸檔される**（Success 扱い） |
| 欠落頁の仕訳 | **どこにも入らない**。自動再試行も無い |
| 留痕 | 監査タブに「欠落」1 行（`page_coverage_gap:[5,6,...]`）と控制台 print のみ |
| MF タブ | **1 行も出ない**（顧客が実際に見る帳簿側に痕跡ゼロ） |

「完全な無音」ではない（監査タブに 1 行残る）が、顧客がその行に気づいて
手動で再処理しない限り、その頁の帳簿データは**永久に入らない**。

---

## 1. 目標

1. **H1**: `_split_pdf_pages` が中途で尽きたとき、**取得できなかった頁ごとに
   占位を yield する**。頁が「循環に入らなかった」だけで消えることを無くす。
2. **H2**: その占位が MF タブ側に届き、顧客が**帳簿を見るだけで**気づける。
3. **H3**: 既存裁定（§8-中7「進入した頁が何も出さない場合は警告のみ」）を
   **覆さない**。`test_ip401_regression` は無修正で緑のまま。

## 2. 非目標

- `_split_pdf_pages` の例外処理は**変えない**。中途失敗時に例外を上げる／
  番兵を yield する等の改造はしない（§3.1 で理由）。
  - 付随して: producer を「例外再送出」に変える設計へ将来移るなら、
    `first_page = next(page_gen, None)`（`ocr_engine.py:2220`）も try で
    囲む必要がある。**本 Plan の補填はそこには効かない**（Codex 評審の
    見落とし経路指摘。現行 producer は握って return するので今は無害）。
- §11.1b（pypdf が最初から開けないと多頁 PDF が尾段の 1 回呼出に落ちる）は
  別件。ここでは触らない。
- §8-中7 の「進入したが何も出さなかった頁」の扱いは据え置き（§11.2 のまま）。
- `sheets_output.py` は変更しない。
- `main.py` は **1 行だけ**変える（§3.2。G11 の是正。当初は「無改変」と
  書いていたが、H2 を満たすには不可欠だと Codex 評審で判明した）。

---

## 3. 設計

### 3.1 修正の位置 — 消費側（`process_pipeline`）であって producer ではない

候補は 4 つあった:

| 案 | 内容 | 却下/採用の理由 |
|---|---|---|
| (a) | `_split_pdf_pages` が例外を再送出する | 消費側の `for` 文は per-page try の**外**なので、例外は最外 except へ飛び**残り全頁が消える**。現状より悪い |
| (b) | `_split_pdf_pages` が番兵 dict を yield する | ページ dict の契約（`page_num`/`total_pages`/`data`/`filename`）に「番兵」という第 2 の意味を持ち込む。消費側の全読み手が分岐を覚える必要がある |
| (c) | `_split_pdf_pages` が残頁分の占位を yield する | producer が「何頁ぶん諦めたか」を知る必要があり、`total_pages` と現在位置の両方を持ち回る。しかも `data` が無い頁 dict という不整合な形が生まれる |
| **(d)** | **消費側で「循環に一度も入らなかった頁」を検出して占位を yield する** | **採用**。producer の契約も語義も変えない。しかも split 失敗に限らず「ジェネレータが `total` より少なく産出した」全ての原因を覆う |

(d) の要点は **「進入しなかった頁」と「進入したが何も出さなかった頁」を
区別する**ことである。この 2 つは今まで `seen_pages` 一本で混同されていた。

```python
                seen_pages = set()      # 一度でも yield した頁（既存）
                entered_pages = set()   # 逐頁ループの本体に入った頁（新規）
```

`entered_pages` の記録位置は `if idx < start_page: continue` の**後**。
`--start-page N` で意図的に飛ばした頁を「消えた」と誤報しないため
（既存のカバレッジ突合が `range(start_page, total+1)` を使っているのと同じ理由）。

ループ後:

```python
                # ① 循環に一度も入らなかった頁 = producer が尽きた
                never_entered = sorted(
                    set(range(start_page, total + 1)) - entered_pages)
                for miss in never_entered:
                    failed_pages += 1
                    yield _mark(_page_error_payload(
                        "PDF分割が中断（この頁を取得できませんでした）",
                        miss, total, None))

                # ② 既存のカバレッジ突合（①で占位を出した頁は seen_pages に
                #    載るので、ここに残るのは「進入したが何も出さなかった」頁だけ）
                missing = sorted(set(range(start_page, total + 1)) - seen_pages)
```

**②が既存裁定と両立する仕組み**: ①で占位を yield した頁は `_mark` により
`seen_pages` に入る。よって②の `missing` に残るのは
`entered_pages - seen_pages`、すなわち §8-中7 が「警告のみ」と裁定した
まさにその集合だけになる。既存テスト（G10）が注入するのは
「`_yield_page_results` が空を返す」＝**進入はした**頁なので、①には
掛からず②で従来どおり警告される。**無修正で緑**。
（Codex 評審が `_mark` / `missing` / 既存テストを実際に辿って同じ結論を確認した。）

**この設計が覆う範囲の正確な記述**（Codex 評審と複審の要求により狭めた）:

覆うのは **「`first_page` を取得した後、producer が `total` より少なく産出して
静かに尽きた」原因の全て**である。「頁が消えるケース全般」ではない。

| 覆う | 覆わない |
|---|---|
| `writer.add_page(page)` が特定頁で失敗 | `PdfReader(file_path)` 自体が失敗（→ §11.1b の別件） |
| `writer.write(buf)` が参照解決・圧縮・壊れた XObject 等で失敗 | `len(reader.pages)` が最初から失敗 |
| 逐頁 materialize 中のメモリ不足等 | `len(reader.pages) <= 1` の誤判定 |
| `enumerate(reader.pages, 1)` 反復中の頁ツリー不整合 | `first_page = next(page_gen, None)` の**前**に尽きる |
| 将来 `_split_pdf_pages` にフィルタや早期 return が足されて `total_pages` より少なく yield する | producer が壊れ dict を yield して `page_info["page_num"]` で落ちる（下記） |

**壊れ dict を塞がない理由**: `idx = page_info["page_num"]` と
`page_data = page_info["data"]` は per-page try の外に在る
（`ocr_engine.py:2246-2248`、try は `:2253`）ので、キー欠落の dict が来ると
`KeyError` が最外 except へ飛び残頁が消える。これを塞ぐには
「頁番号すら分からない状態で意味のある占位を作る」ことが要るが:

- `_page_error_payload` は `page_num` を必須に取る。反復回数で仮番号を
  代用すると、`main.PageUrlResolver.resolve(page_num, ...)` の `#page=N` と
  単頁 PDF キャッシュのキーに直結しているため、**実際の欠落頁と違う原票 URL を
  指す占位**が生まれる。「欠落している」とだけ分かる状態より、調査者を
  別の頁へ誘導する分だけ**悪い**
- `_split_pdf_pages` はモジュール内で唯一の producer であり、4 キーを
  同一箇所で組み立てている（`ocr_engine.py:433-438`）。本番に注入経路は無い

**限界として明記するに留める**（Codex も複審で「今回 P1 の必須範囲から外して
限界明記で足りる」と同意）。

### 3.2 分類は `_page_error`（ただし「保持」を意味しない）

一見すると「PDF が壊れている」は再試行しても直らないので `_unrecognized`
（歸檔）が適切に見える。しかし**この頁は歸檔されない側に倒すべきではない**:

- 前 k 頁は**既に成功して Sheets に書かれている**。ファイル全体を保持して
  再試行すると、その k 頁が**重複計上**される（`main.py:648` の
  「歸檔（重試による重複行を防ぐため）」がまさにこの理由）
- `_page_error` を立てても、**部分エラーなら歸檔される**（`error_pages > 0`
  かつ `< count` → `partial_error` → 歸檔 ＋ 集計行）。保持されるのは
  **全頁失敗**のときだけ

つまり `_page_error` は「保持」を意味するのではなく「**この頁は失敗した**」を
意味し、保持するか歸檔するかはファイル全体の成否で `main` が決める。
本件は必ず「前 k 頁成功 ＋ 後続失敗」なので **partial_error → 歸檔** になる。

これが H2 を満たす: `main.py:652-663` の集計行が **MF タブ**に
「⚠ ページ処理エラー N/count頁 [p5,p6] 手動再スキャン要」として書かれる。
顧客が帳簿を見るだけで気づける。

（比較: 現状は監査タブに 1 行だけ。MF タブ側は無傷＝顧客の目に触れない。）

**ただし具体的な原因は現状では集計行に載らない**（G11。Codex 評審 P1）。
当初この Plan は「/ p5: PDF分割が中断…」まで書かれると記していたが、
**それは誤りだった** —— `failed_page_notes` は `_excluded_page` の監査失敗
経路でしか埋まらず、`_page_error` 頁の memo は `main.py:522-526` で捨てられる。

**是正（`main.py` の 1 行）**:

```python
        if result.get("_page_error"):
            error_pages += 1
            failed_page_nums.append(page_num)
            # 具体的な説明を集計行へ引き継ぐ。producer は memo に原因を書いて
            # いるのに（「PDF分割が中断」「AI応答のJSON解析失敗」「整形処理
            # エラー: XxxError」）、ここで捨てると顧客が見る集計行は頁番号だけ
            # になり、「再アップロードで直るのか、原票が壊れているのか」を
            # 判断できない。failed_page_notes の宣言時のコメント（:500）が
            # 元々意図していた用途でもある。
            if result.get("memo"):
                failed_page_notes[page_num] = result["memo"]
            _emit(OUTCOME_FAILED, "page_error")
            continue
```

これは本件だけでなく**全ての `_page_error` 頁**の診断力を上げる。既存テストは
集計行の内容を assert していないため無回帰（実測で確認済み）。

### 3.3 全頁が never_entered になる場合はあるか（本番経路では無い）

**本番経路（`start_page=1`）では無い。** `first_page is not None` が成立して
初めてこの分岐に入り、p1 は `itertools.chain([first_page], page_gen)` で必ず
循環本体に入る。よって `error_pages == count` にはならず、
「保持 → 無限再試行」には落ちない。

**`start_page > 1` では起こりうる**（Codex 評審 P1 で見出しの誇張を指摘された）。
p1 を `continue` した直後に producer が尽きると、`range(start_page, total+1)`
の全頁が `never_entered` になり、`count == error_pages` → Failed → ファイル保持
となる。これは `local_test.py --start-page N` 専用の経路であり、
**本番の `main.py` は `start_page` を渡さない**（G8）。開発ツール上で
「途中から流したが producer が尽きた」ときにファイルが保持されるのは
挙動として妥当なので、そのままにする。

---

## 4. タスク清単（TDD。各項に DoD）

### T-a: 再現テスト（RED を先に見る）

`test_ip401_nondict_rawdata.py` に新クラスを足す（同じ IP-401 不変式の話で
あり、fixture と runner を共有できる）。

1. `test_truncated_split_yields_placeholder_for_missing_pages` —
   `_split_pdf_pages` が `total_pages=3` を宣言しつつ p1 だけ産出して尽きる
   → 出力に p1 の結果 ＋ p2/p3 の `_page_error` 占位が現れる
2. `test_truncated_split_placeholder_names_the_cause` — 占位の memo が
   「PDF分割が中断」を含む（汎用の「ページ処理エラー」で埋もれさせない）
3. `test_truncated_split_does_not_warn_twice` — ①で占位を出した頁について
   カバレッジ警告が**出ない**（占位が出た＝もう無音ではない）
4. `test_entered_but_silent_page_is_not_placeholdered_after_entered_pages` —
   `_yield_page_results` が空を返す頁は、`entered_pages` 導入**後も**占位を
   作らず警告のみ（§8-中7 の裁定を固定。これが H3 の番人）。
   名前で「既存テストの写しではなく、新機構が既存裁定を侵していないことの
   検査」だと分かるようにする（Codex 評審 P2 の指摘）
5. `test_start_page_skip_is_not_reported_as_missing` — `start_page=2` で
   p1 を飛ばしても p1 の占位は作らない

**DoD**: 実装前に 1〜3 が FAIL、4・5 が PASS（4 は既存挙動の錨）。

### T-b: 実装（GREEN）

`ocr_engine.process_pipeline` の逐頁分岐に `entered_pages` と
「never_entered への占位 yield」を追加（§3.1）。

**DoD**: T-a 全緑 ／ 変更は逐頁分岐の 3 箇所以内（集合の宣言・記録・ループ後の
補填）／ `_split_pdf_pages` と `sheets_output.py` は無改変。

### T-c: `main.py` の 1 行（§3.2 / G11）と終態テスト

`_page_error` 分岐で `failed_page_notes` に memo を引き継ぐ。

1. `test_truncated_split_is_archived_with_summary_row` — 前頁成功 ＋ 欠落占位
   → `process_file` が **True**（歸檔）を返し、集計行が MF タブへ書かれ、
   その本文に欠落頁番号が入る
2. `test_truncated_split_emits_failed_outcome_per_missing_page` — 欠落頁ごとに
   `OUTCOME_FAILED` が発火する
3. `test_page_error_memo_reaches_the_summary_row` — `_page_error` 頁の memo が
   集計行に現れる（G11 の是正そのものの番人。本件以外の `_page_error`
   —— 「AI応答のJSON解析失敗」等 —— でも効くことを 1 件で示す）

**DoD**: 3 件緑 ／ 既存 `test_main_process_file` が無修正で緑
（集計行の内容を assert する既存テストが無いことを実測済み）。

### T-d: 回帰と変異検証

**DoD**:
- 全量 `venv311/bin/python -m unittest discover -p "test_*.py"` → OK
- `test_ip401_regression` が**無修正で緑**（H3。特に
  `test_coverage_warning_fires_when_a_page_yields_nothing`）
- **変異検証**: 占位 yield を削ると T-a 1〜3 と T-c が FAIL する。
  `entered_pages` の記録位置を `if idx < start_page` の前に動かすと T-a 5 が
  FAIL する（位置の意味が本当に守られているかを確かめる）

---

## 5. 受入基準（脚本判定）

```bash
cd "/Users/ibridgezhao/Documents/Super Scaner"
venv311/bin/python -m unittest discover -p "test_*.py"      # → OK, Ran >= 796
venv311/bin/python -m unittest test_ip401_regression -v     # → OK（無修正）
venv311/bin/python -m unittest test_main_process_file -v    # → OK（無修正）
```

人手判定:
- `git diff` が `ocr_engine.py` の逐頁分岐**1 箇所**と `main.py` の
  `_page_error` 分岐**1 箇所**に収まる
- `_split_pdf_pages` / `sheets_output.py` に 1 行も触れていない

---

## 6. 影響面

| 対象 | 影響 |
|---|---|
| `ocr_engine.process_pipeline` 逐頁分岐 | `entered_pages` の宣言・記録・ループ後の補填（約 12 行） |
| PDF 中途失敗時の終態 | Success 歸檔（MF 無痕）→ **partial_error 歸檔 ＋ MF 集計行** |
| 正常な PDF | **完全に無変化**（`never_entered` が空集合） |
| `_split_pdf_pages` | 無改変 |
| `main.py` | `_page_error` 分岐に 1 行（memo を `failed_page_notes` へ）。**全ての `_page_error` 頁**の集計行が具体的になる（本件以外にも効く改善） |
| `sheets_output.py` | 無改変 |
| 既存テスト | 無改変で緑（§3.1 の両立機構 ＋ 集計行の内容を assert する既存テストが無いこと） |

## 7. 風険と回退

| # | 風険 | 評価 | 緩和 |
|---|---|---|---|
| S1 | 正常 PDF で誤って占位が出る | `never_entered` は `total` と実進入数の差なので、正常時は必ず空 | T-a の無回帰錨（正常 2 頁で余分な yield が無いこと） |
| S2 | `start_page` 使用時に飛ばした頁を欠落と誤報 | 記録位置を `continue` の後にすることで構造的に回避 | T-a 5 ＋ 変異検証で位置の意味を固定 |
| S3 | 既存裁定 §8-中7 を暗黙に覆す | ①が `seen_pages` を埋めるので②に残るのは「進入したが無出力」だけ。裁定はそのまま | T-a 4 が番人 |
| S4 | 欠落頁が多いと集計行が長大になる | 既存の `failed_page_notes` と同じ形式。頁番号の羅列は既に既存挙動 | 変更しない |

**回退**: 単一 commit。`git revert` で完全に戻る。

---

## 附録 A: Codex 評審の辯論記録（2026-08-17）

8 件（P1 ×3 / P2 ×5）。**6 件全面採用・1 件は限界明記へ変更・1 件は精確化**。
反駁ゼロ。

### 全面採用（6 件）

| # | severity | 指摘 | 反映先 |
|---|---|---|---|
| 1 | **P1** | §3.2 の「集計行に `p5: PDF分割が中断…` と書かれる」は**現行コード上は誤り**。`failed_page_notes` は `_excluded_page` の監査失敗経路でしか埋まらず（`main.py:546`）、普通の `_page_error` は memo を捨てる（`:522-526`） | **G11 を新設**して事実を記録。§3.2 を訂正し、`main.py` に 1 行（memo 引継ぎ）を足す方針へ変更。非目標の「`main.py` 無改変」を**撤回**。T-c 3 を番人として追加 |
| 2 | **P1** | §3.3 の見出し「全頁が never_entered になる場合は無い」は `start_page > 1` を考えると強すぎる。本文が後段で「理論上ありうる」と認めており矛盾 | 見出しを「本番経路では無い」に修正。`start_page > 1` で Failed 保持になることと、それが開発ツール専用経路であることを明記 |
| 3 | P2 | G8「main 側にも同じ突合」は不正確。`ocr_engine` は `range(start_page, total+1)`、`main` は常に `range(1, last+1)` | G8 に差分を追記 |
| 4 | P2 | G3（pypdf の遅延解決）は repo 内コードだけでは証明できない | G3 を**【推測】**と明示。さらに「G3 が外れても設計は成立する」根拠を付記 |
| 5 | P2 | T-a 4 が既存テストと重複気味 | 名前を `..._after_entered_pages` に変え、「新機構が既存裁定を侵していないことの検査」だと明示 |
| 6 | P2 | 将来 producer を「例外再送出」に変えるなら `first_page = next(...)` も try で囲む必要があり、本 Plan の補填は効かない | 非目標に明記 |

### 限界明記へ変更（1 件・複審で**当方成立**）

**#7（P1）「`idx`/`page_data` の取得が per-page try の外なので、壊れ dict で
残頁が消える」**

事実は正しいが**塞がない**。塞ぐには「頁番号が読めない状態で占位を作る」ことが
要り、反復回数で仮番号を代用すると `PageUrlResolver.resolve(page_num, ...)` を
通じて**実際の欠落頁と違う原票 URL を指す占位**が生まれる ——
「欠落している」とだけ分かる状態より、調査者を別頁へ誘導する分だけ悪い。

複審で Codex は「理由(1)は妥当。`page_num` は表示番号ではなく `#page=N` と
単頁 PDF キャッシュのキーに直結している。……今回 P1 の必須範囲から外して
限界明記で足りる」として**当方成立**を認めた。§3.1 に限界として記載。

### 精確化（1 件・複審で**当方成立**＋文言の改善提案を採用）

Codex の最終評価「『全ての頁消失を覆う』ではなく『producer が途中で静かに
尽きたため never-entered になった頁を可視化する』設計、と狭く正確に書くべき」
を受けて §3.1 に「覆う範囲の正確な記述」節を新設。複審ではさらに
**「`first_page` 取得**後**に producer が `total` より少なく産出した原因全般」**と
書くべきという指摘があり、覆う/覆わないの対照表として反映した。

**Plan 定稿**（2026-08-17）。

---

## 附録 B: 実施記録（2026-08-17）

commit: `bd9f3c2`（Plan）→ `5191eff`（TDD RED）→ `0845a17`（実装）。
**799 tests 緑**（着手時 791）。

### 実装

| 箇所 | 内容 |
|---|---|
| `ocr_engine.process_pipeline` | `entered_pages` の宣言・記録・ループ後の補填（3 箇所） |
| `main._process_file_impl` | `_page_error` 分岐で `failed_page_notes` へ memo を引き継ぐ（1 行 ＋ 理由コメント） |
| `test_ip401_nondict_rawdata.py` | `TruncatedSplitTest`（5 件）＋ `ProcessFileTerminalStateTest` に 3 件 |

`_split_pdf_pages` / `sheets_output.py` / 既存テストは**無改変**。
`test_ip401_regression` と `test_main_process_file`（57 件）は無修正で緑 ——
H3 達成。

### 変異検証 — 4 件中 1 件が**当方のコメントの誤りを暴いた**

| 変異 | FAIL したテスト |
|---|---|
| A: 補填の yield を削除 | 5 件（`TruncatedSplitTest` 3 ＋ main 側終態 2） |
| B: `entered_pages.add(idx)` を start_page スキップの**前**へ | **0 件** ← 想定外 |
| C: `main.py` の memo 引継ぎを戻す | 1 件（`test_page_error_memo_reaches_the_summary_row`） |
| D: 母集団を `range(1, total+1)` へ | 1 件（`test_start_page_skip_is_not_reported_as_missing`） |

**B が何も殺さなかった**ことで、§3.1 に書いた
「記録位置を前に置くと『飛ばしたのに欠落として占位が出る』誤報になる」が
**誤りだった**と判明した。差集合の被減数が `range(start_page, total+1)` である
以上、飛ばした頁番号はそもそも母集団に居らず、`entered_pages` に混ざっても
結果は変わらない。記録位置が効くのは**可読性であって正しさではない**。

コードのコメントと test の docstring を訂正し、当該 test が実際に守っている
のは「母集団が `start_page` 起点であること」だと書き直したうえで、
**変異 D でそれが実際に落ちることを確認**した（訂正した主張を再度変異で
裏取りする）。

これは前 commit（IP-401 非 dict）で 3 件見つかった「テストが守っていると
主張する対象と実際に守っている対象のずれ」と同族だが、今回ずれていたのは
テストではなく**実装コメントの理由付け**だった。変異検証は
「テストの牙」だけでなく「**自分の因果説明が正しいか**」も検査する。

---

## 附録 C: simcodex Round 1（2026-08-17。commit `2189514`）

codex review: **指摘ゼロ**。4 観点エージェントの発見を以下のとおり裁決した。
コードの挙動は変えていない（799 tests 緑のまま）。

### 採用（テストの牙 6 件 ＋ 重複 3 件 ＋ 前提の番人 1 件）

**テストの牙**: 監査エージェントが 6 件の**生存変異**を特定。いずれも
「主張どおりの変異を殺せていなかった」もので、是正後に 7 変異を実際に
注入して全件が落ちることを確認した:

| 変異 | 是正前 | 是正後に落ちる |
|---|---|---|
| `idx < start_page` → `<=` | 生存 | 7 件 |
| 補填範囲 `total+1` → `total+2`（幻の頁） | 生存 | 6 件 |
| 補填の頁番号を +1 ずらす | 生存 | 4 件 |
| `failed_page_notes[page_num]` → 固定キー `[1]` | 生存 | 1 件 |
| カバレッジ警告ブロックごと削除 | 生存 | 1 件 |
| `if missing:` → `if True:` | 生存 | 1 件 |
| **producer を `raise` に変える**（新規） | 検査なし | 1 件 |

とくに 1 行目と 4 行目は質が悪かった ——
`idx <= start_page` では p2 自身がスキップされて占位に化け**実データが
失われる**のに、頁番号集合は `{2}` のままなので素通りしていた。
固定キー変異では memo 本文は集計行に出るが**帰属する頁が誤る**、
すなわち G11 が是正しようとした失敗そのものが再現するのに緑だった。

**producer の契約に番人を置いた**（本修正の前提が無検査だった）:
`_split_pdf_pages` 本体を駆動するテストは suite 全体に 1 つも無く、常に
mock 先だった。つまり「中途失敗しても例外を外へ出さず静かに尽きる」という
補填ロジックの**前提そのもの**が無検査で、誰かが `except Exception: return`
を `raise` に変えると補填には一度も到達しないのに mock ベースのテストは
全部緑のままになる。`SplitProducerContractTest` で実物を走らせて固定した。

**重複の解消**: `_run_paged_pdf_truncated` を廃して `_run_paged_pdf` に統合 /
`ocr_test_helpers.pdf_pages` に `total_pages` 引数を追加（この helper の
docstring 自身が「コピーが増えると片方に足し忘れる」と警告しており、
前 commit はまさにそれを犯していた）/ `missing` を
`sorted(entered_pages - seen_pages)` へ簡約（補填後は等価。等価性を証明する
ための 3 行コメントが不要になり、式が意味を直接表すようになった）。

### 見送り（設計層。§11 の申し送りへ）

altitude 監査の 4 件は本件の範囲（可視化）を超える設計判断なので見送った。
**うち 1 件は「選択肢表に最良案が載っていなかった」という当方の落ち度**で、
下記 §11.3 に記録する。

---

## 11. 後続への申し送り（simcodex Round 1 の altitude 監査より）

### 11.3 【最優先・趙の拍板】producer 側の頁単位隔離 —— **選択肢表に載っていなかった最良案**

§3.1 の選択肢表は (a) 再送出 / (b) 番兵 / (c) 残頁占位 / (d) 消費側補填 の
4 案を比べて (d) を採った。**しかし第 5 案が存在し、それが表に無かった**:

> **`_split_pdf_pages` の `try` を for ループの「外」から「中」へ移す**
> （頁単位で catch → log → `continue`。壊れた頁だけ産出せず、
> ジェネレータは終了しない）

この案は (a)(b)(c) に挙げた欠点を**1 つも持たない**:

- 再送出しないので後続頁を殺さない（(a) の欠点なし）
- 番兵 dict を作らないので頁 dict の契約に第 2 の意味を持ち込まない（(b) なし）
- `data` を欠いた不整合な dict を作らない（(c) なし）

しかも本 commit で入れた `entered_pages` 機構と**併用できる**（実際に飛ばした
頁だけが `never_entered` に落ちるので補填は正しく働く）。

**(d) 単独を選んだことの実際の代価**（これが本質）:

`_split_pdf_pages` の `try` が for ループ全体を包んでいる以上、
**PDF のどこか 1 頁が壊れると、そこから最後までの全頁が失われる**。
20 頁のスキャンで 3 頁目が壊れれば、3〜20 の **18 頁**が占位行になり
人手の再スキャン対象になる。頁単位隔離なら失うのは 3 頁目だけである。

本 commit が達成したのは「**失われたことが見えるようになった**」であって
「**失われる頁が減った**」ではない。表に第 5 案が無かったため、
その差が趙の前に選択肢として提示されないまま (d) に決まった。

**関連する第 2 の代価**（altitude 監査 Finding 4）: `except Exception` は
永久的原因（本当に壊れた頁 —— 再試行しても無駄）と一時的原因
（逐頁 materialize 中のメモリ不足等 —— `CLAUDE.md` が miniPC の硬い制約と
して挙げているもの）を区別できない。現行設計は両者を同じく歸檔扱いにする
ので、一時的原因なら単純な再試行で全頁救えたはずの場合でも、人手の
再スキャンが必要になる。頁単位隔離ならこの差も縮む。

**未検証の前提**（G3 と同じ確度で明示する）: 頁単位隔離が本当に後続頁を
救えるかは、pypdf の `reader.pages` が「頁 i の失敗後に頁 i+1 を独立して
取り出せる」かどうかに依る。**pypdf の実装は未確認**であり、着手時に
実物で確かめる必要がある（壊れた PDF を用意するか、`PdfWriter` を
モックして頁単位失敗を注入する。後者は本 commit の
`SplitProducerContractTest` が既に雛形を持っている）。

**確定した事実**（altitude 監査が実測）: 「producer が再送出し、消費側が
manual `next()` を try で包む」という別案は**成立しない**。ジェネレータの
フレームから例外が逃げた時点で CPython はそのジェネレータを永久に
枯渇させるので、次の `next()` は即 `StopIteration` を返し、
`_split_pdf_pages` 内部の for が `i+1` から再開することはない。
これは呼び出し方の問題ではなくジェネレータの性質なので、消費側を
どう書き換えても「再送出しつつ残頁を保つ」は達成できない。
（Codex 評審が「未検討の別案」として挙げていたもの。**不可能と確定**。）

### 11.4 カバレッジ差分の計算が 3 箇所（P2）

`ocr_engine` の `never_entered` と `missing`、`main.py:623` の
`missing_pages` の 3 つ。本 commit 後、paged 分岐では `missing` と
`missing_pages` が**同じ集合**を計算するようになった（補填占位が
`seen_pages` にも `main` の `seen_page_nums` にも流れるため）。

矛盾はしていない。`main` 側は (a) 尾段（`entered_pages` の概念が無い）の
唯一のカバレッジ検査であり、(b) 唯一 signal を**永続化**する（`ocr_engine`
側は print のみで、無人 miniPC では print は無音に等しい）ので、今は
どちらも必要である。

ただし両者が同じ集合を計算していることは、`main` が `start_page` を
渡さないという**強制されていない前提**だけで支えられている。将来
バッチ再開機能などで `start_page` が `main` まで通ると、`main.py:623` の
`range(1, ...)` が `start_page` 未満の全頁を「欠落」と誤報する。
§11.1 の共有 generator 化と併せて 1 箇所へ寄せるのが筋。

### 11.5 §11.1（共有 generator 化）への影響

altitude 監査の判定: **難易度は変わらない**。`entered_pages` の簿記は
「producer が頁 i を渡してきたか」という**呼出側の関心事**であり、正しく
呼出側に置かれている（尾段は `_split_pdf_pages` を呼ばないので対応する
失敗様式を持たず、この非対称は構造的である）。共有 generator を作るときは
この簿記を**呼出側に残す**こと —— 共有単位へ押し込むと尾段側に掛ける先が
無くなって誤る。paged 分岐の呼出側専用状態が約 20 行増えたので、
切り分けの対象が 1 つ増えたことだけ意識すればよい。

---

## 12. 次 session の作業指示（2026-08-17 趙拍板 ＋ Codex 合意）

趙が「頁単位隔離を採る」と拍板し、懸案 4 件について Codex と合意形成した
結果を、**次 session がこれだけ読めば着手できる**形にまとめる。

### 12.1 立刻やる 3 件（すべて `_split_pdf_pages` 周辺。まとめて 1 commit）

3 件とも同じ関数を触るので、**分けずに一度に**やるのが最も安い。

#### ① 頁単位隔離（趙拍板済み）

`try` を for ループの外から中へ移し、`enumerate(reader.pages, 1)` を
`range` ＋ 索引アクセスへ変える。壊れた頁だけ `continue` で飛ばし、
ジェネレータは終了させない。

```python
    for i in range(1, total_pages + 1):
        try:
            page = reader.pages[i - 1]      # _VirtualList はランダムアクセス可
            writer = PdfWriter()
            writer.add_page(page)
            buf = io.BytesIO()
            writer.write(buf)
            data = buf.getvalue()
        except Exception as e:
            print(f"⚠️ p{i} のPDF分割に失敗（この頁を飛ばして継続）: {e}")
            continue
        yield {...}
```

**実測済みの前提**（G3 の「推測」を格上げした）: pypdf 4.3.1 の
`reader.pages` は `_VirtualList` で**ランダムアクセス可能**。p3 を先に
取ってから p1 を取っても動く（venv311 で実測）。ゆえに頁 i の失敗は
頁 i+1 の取得を妨げない。

**効果**: 20 頁の 3 頁目が壊れたとき、失うのが 3〜20 の 18 頁から
**3 頁目だけ**になる。既存の `entered_pages` 補填がその 1 頁に占位を出す。
**producer → 消費側の新しい通信路は要らない**（Codex 同意）。

**memo 文言を直すこと**: 補填占位の
`PDF分割が中断（この頁を取得できませんでした）` は、
「p3 だけ壊れて p4 以降は正常」の新しい状況では「p3 以降が中断した」と
読めてしまう。→ **`PDFページ分割失敗（この頁を取得できませんでした）`**

**出力順が変わる**（Codex 指摘・許容と判断）: p3 を飛ばして p4..p20 を
yield し、補填はループ後なので出力順は `1,2,4,...,20,3` になりうる。
`main` は `_page_error` を Sheets 本体へ書かず集計行に回すので実害なし。
厳密に頁順を保つなら消費側で idx のギャップを見た時点で補填する必要が
あるが、今回はそこまで広げない。

#### ② A（旧 §11.1b）— 0 件出口 3 つを区別する【**P2 → P1 に格上げ**】

`_split_pdf_pages` には 0 件で終わる出口が 3 つあり、消費側の
`first_page = next(page_gen, None)` は**全部同じ「単頁/画像の尾段」扱い**に
する（`ocr_engine.py:2220`）。結果、**開けない多頁 PDF がファイル全体を
1 回の Gemini 呼出に送る** —— `ocr_engine.py:2211-2216` のコメントが
「大型 PDF で出力が MAX_TOKENS に達し JSON が途中切断される生産事故が
発生した」として逐頁分岐に統一した、**その事故の再現経路そのもの**。

| 出口 | 現状 | 改後 |
|---|---|---|
| `:417` pypdf 未導入 | 尾段へ | **fail-fast。尾段禁止** |
| `:423` `len(reader.pages) <= 1`（正常な単頁） | 尾段へ | **変更なし**（正しい挙動） |
| `:440` 読取失敗・破損 | 尾段へ | **専用例外 → `_page_error` 占位** |

**pypdf 未導入を尾段へ落とさないのは Codex の主張を採用したもの**。
当方は当初「従来どおり」としていたが、
「多頁か単頁かを判定できない状態で全 PDF を Gemini 全体呼出へ落とす。
A と同じ事故再現経路」との反対を受けて撤回した。しかもこちらは偶発では
なく**系統的**である（pypdf が壊れていれば全 PDF が該当し、痕跡は
print 1 行だけ）。`requirements.txt` に `pypdf==4.3.1` が在るのに
`ocr_engine.py:39-42` が `ImportError` を握って `None` に落とす構造。

**枯渇問題は起きない**: 専用例外が飛ぶのは `PdfReader()` / `len()` の
失敗時、すなわち**まだ 1 件も yield していない**時点なので、消費側の
`next()` を try で囲めばよい。ジェネレータ枯渇が制約になるのは
「yield を始めた後」だけである（＝中途失敗。そちらは①の `continue` が
処理するので、そもそも例外を投げない）。**①と②は互いの境界を
綺麗にする**（Codex が「専用例外は初回 yield 前に限定せよ」と注意した
点は、①を同時に入れることで自動的に満たされる）。

#### ③ D（§11.4）— 番人テスト 1 本

```python
_, kwargs = main.process_pipeline.call_args
self.assertNotIn("start_page", kwargs)
```

`main.py:614` の `range(1, last_total_pages + 1)` は「main は常に全体を
処理する」という**強制されていない前提**に依存している。将来バッチ再開等で
`start_page` が main まで通ると `start_page` 未満を全部「欠落」と誤報する。
前提が破れた瞬間に赤くなる番人を置く。既存 `test_main_process_file.py` の
`_run_process_file` が既に `process_pipeline` を mock しているので、
そこへ 1 本足すのが自然。

### 12.2 T5 の着手判断まで持ち越す 2 件

**B（§11.1 共有 generator 化）と C（§11.2 空出力の構造的兜底）を束ねる。**

C を単独でやるのは、**現時点で到達不能**な経路のために既存裁定（§8-中7）を
覆して既存テスト（`test_ip401_regression.py:168`）を書き換えるだけになり、
変更理由が弱い。B で共有 generator を作るとき「1 頁入口の契約」として
一度だけ書けば両経路に効く。両者とも Codex 同意。

**正直な注記**: B/C と T5 に**技術的な硬依存は無い**。T5（窓分割リトライ）は
prompt と builder の層で、`process_pipeline` の paged/tail 骨格は触らない
見込みである。「T5 のとき」としているのは、そこで pipeline を見直す機会が
あるからという**スケジュール上の判断**であって、技術的必然ではない。
B/C を独立に先行させることも可能で、その場合の判断材料は
「骨格改造の risk を今払うか、後で払うか」だけである。

### 12.3 それでも残るもの

- `test_ip401_regression._pdf_pages` は `ocr_test_helpers.pdf_pages` の
  2 つ目のコピーのまま（P3）。G4「既存回帰は無修正で緑」を守るため
  意図的に触っていない。統合するなら回帰テスト側の変更を伴うので、
  B の作業に含めるのが自然
- 壊れ dict（`page_info` のキー欠落）で残頁が消える経路（§3.1 の限界節）。
  頁番号が読めない状態で意味のある占位は作れないため、限界として明記済み
