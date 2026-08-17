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

---

## 13. §12.1 実施計画（2026-08-17 実施 session。3 件まとめて 1 commit）

§12.1 の①②③を実装するための**実施級**の計画。§12.1 は「何を・なぜ」まで
確定しているので、ここでは「どのコードが・どの終局語義で・どのテストで
守られるか」を確定する。§12.2 の B/C は**本 commit に含めない**（趙拍板）。
Codex 評審（§13.8）を経た確定版。

### 13.0 着手前に実測した前提

| # | 事実 | 根拠（本 session のコマンド出力） |
|---|---|---|
| H-1 | pypdf 4.3.1 の `reader.pages` は `_VirtualList`。`r.pages[2]` を先に取ってから `r.pages[0]` を取れる（ランダムアクセス可） | `venv311/bin/python` で 3 頁 PDF を生成して実測。§12.1 の主張を独立に再確認した |
| H-2 | `process_pipeline` は関数全体を `try` で包み、最外 `except Exception: print → return` する | `ocr_engine.py:2482-2484` |
| H-3 | ゆえに **producer の例外を消費側が捕まえないと、最外 except に吞まれて 0 件 yield** になる。main は `count==0` → 「解析に失敗しました」→ Failed → ファイル保持 → 3 秒後に再走査（Sheets に痕跡ゼロ） | `main.py:712-722` |
| H-4 | `_split_pdf_pages` の本番呼出点は `ocr_engine.py:2219` の 1 箇所のみ | `grep -rn "_split_pdf_pages" --include=*.py`（他は全てテストの mock 先） |
| H-5 | `main.py:505` は `process_pipeline(file_path, doc_type=doc_type)` で `start_page` を渡さない。渡すのは `local_test.py:93` のみ | 同 grep |
| H-6 | 尾段（単頁/画像経路）は `_route_ocr_strategy(file_data, mime_type, doc_type, ocr_strategy)` を**必ず**通る（逐頁側は `prefix=` 付きで呼ぶ） | `ocr_engine.py:2420` vs `:2273` |
| H-7 | 全頁エラー（`count == error_pages`）のとき main は **Sheets に 1 行も書かない**。partial_error のときだけ MF 集計行を書く | `main.py:644-674` |
| H-8 | `test_ip401_nondict_rawdata.py` は既に **889 行**（全局規約の上限 800 行を超過） | `wc -l` |

H-3 は②の設計を拘束する: **専用例外は「投げっぱなし」では機能しない**。
必ず消費側の catch と対で入れる（片方だけ入れると、現状より悪い
「Sheets に痕跡ゼロで永久滞留」になる）。

### 13.1 変更するもの（`ocr_engine.py` の 3 箇所 ＋ 文言 1 箇所）

#### C1. 専用例外クラスの新設

```python
class PdfSplitError(Exception):
    """PDF を頁単位に分割できない（**初回 yield 前**の失敗に限る）。

    契約（消費側 `process_pipeline` がこれに依存している）:
      ・この例外は `_split_pdf_pages` が **1 頁も yield していない**時点でしか
        投げてはならない。頁単位の中途失敗は握って `continue` すること。
      ・理由: 消費側は `next(page_gen, None)` だけを try で囲む。1 頁でも
        yield した後に例外が出ると `for page_info in chain(...)` の外へ抜け、
        最外 except に吞まれて**残頁も補填占位も消える**（H-2/H-3）。
    """
```

本 repo に既存の自前例外クラスは無い（生産コードで `grep "^class .*Error"`
が 0 件）。新設が最小手段である。

#### C2. `_split_pdf_pages` の改造（§12.1①②）

```python
def _split_pdf_pages(file_path):
    """PDF を 1ページずつ yield するジェネレータ（メモリ節約）。

    Raises:
        PdfSplitError: pypdf 未導入 / PDF を開けない / 頁数を数えられない /
            多頁と分かっているのに 1 頁も取り出せなかった。いずれも
            **まだ 1 頁も yield していない**時点の失敗。呼出側は尾段
            （ファイル全体を 1 回の Gemini 呼出へ送る経路）へ落とさず、
            `_page_error` を出して終えること。
    """
    if PdfReader is None or PdfWriter is None:
        raise PdfSplitError("pypdf未導入のためPDFを頁分割できません")

    try:
        reader = PdfReader(file_path)
        total_pages = len(reader.pages)
    except Exception as e:
        raise PdfSplitError(f"PDF読取失敗: {e}") from e

    if total_pages <= 1:
        return                      # 正常な単頁 PDF → 尾段へ（従来どおり）

    base_name = os.path.splitext(os.path.basename(file_path))[0]
    produced = 0
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
        produced += 1
        yield {
            "page_num": i,
            "total_pages": total_pages,
            "data": data,
            "filename": f"{base_name}_p{i}.pdf",
        }

    if produced == 0:
        # 多頁と分かっているのに 1 頁も出せなかった。ここで黙って終わると
        # 消費側の `first_page` が None になり**尾段へ落ちる** —— ②が塞ぐと
        # 決めた「ファイル全体を 1 回の Gemini 呼出へ送る」事故再現経路
        # そのもの。まだ 1 頁も yield していないので C1 の契約に反しない。
        raise PdfSplitError("全頁のPDF分割に失敗しました")
```

**`yield` を `try` の外に置く理由**は逐頁ループの `_yield_page_results` と
同じ（`ocr_engine.py:2325` のコメント）: 消費側から `throw`/`close` された
例外を producer が誤って握り潰さないため。

**最後の `produced == 0` は Codex 評審 P1 で追加**。当初案はこの経路を
「§13.7 の残件」にしていたが、②が塞ぐと決めた出口と同種であり、
新しい通信路も要らず（同じ `PdfSplitError`）、初回 yield 前という
C1 の契約も自動的に満たすため、同 commit の責任範囲だと認めた。

#### C3. 消費側 `next()` の防護（`ocr_engine.py:2219-2221`）

**※ この節は simcodex Round 1/2 の裁決を反映した確定版**（当初案は占位を
1 件だけ出していた。経緯と理由は §13.9）。

```python
            page_gen = _split_pdf_pages(file_path)
            try:
                first_page = next(page_gen, None)
            except PdfSplitError as split_err:
                # 尾段へ落とさない: 多頁か単頁かを判定できないまま**ファイル
                # 全体**を 1 回の Gemini 呼出へ送ることになり、逐頁分岐冒頭の
                # コメントが根絶したはずの MAX_TOKENS 事故の再現経路になる。
                print(f"❌ PDF分割不可のため解析を中止: {split_err}")
                memo = f"PDF分割不可: {str(split_err)[:120]}"
                declared = split_err.total_pages or 1
                for miss in range(1, declared + 1):
                    yield _page_error_payload(memo, miss, declared, None)
                return
```

`[:120]` は既存 3 経路（`str(page_err)[:120]`）と同じ截断幅に揃える。
memo は失敗頁の説明として運用者が読むので、pypdf の例外文字列がそのまま
長々と流れるのを防ぐ。

**占位を「判明した全頁ぶん」出す理由**（§13.9 の 2 件の指摘に対応）:
`total_pages` を名乗りながら占位を 1 件しか出さないと、`main` の
カバレッジ突合が p2..pN を「欠落」と見なして監査タブへ書く。この経路は
全頁失敗＝**ファイル保持**なので 3 秒ごとに再走査され、欠落行が毎周増殖する
（CLAUDE.md「全ページ失敗 → Failed、保留ファイル（Sheets 占位行を書かない）」
に反する）。全頁ぶん出せば `seen_page_nums` が埋まって突合は沈黙し、
進捗タブの `seen/total` も「1/1」に化けない。

`PdfSplitError` は `total_pages` 属性を持つ（既定 `None`）。値が入るのは
「多頁と分かっているのに 1 頁も出せなかった」ときだけで、pypdf 未導入・
読取失敗ではそもそも頁数が不可知なので `None` のまま `or 1` で 1 に落ちる。

#### C4. 補填占位の memo 文言（§12.1①の指示）

`"PDF分割が中断（この頁を取得できませんでした）"`
→ `"PDFページ分割失敗（この頁を取得できませんでした）"`

①の後は「p3 だけ壊れ、p4 以降は正常」が起こりうるので、「中断」は事実と
食い違う。対応する print も同じ語に揃える。**この文字列は補填分岐にしか
無い**（旧 `print("⚠️ PDFページ分割失敗: ...")` は C2 で消える）ので、
テストが経路を特定する目印としての性質は保たれる。

#### C5. `main.py` は**注釈 1 行のみ**、`sheets_output.py` / `local_test.py` は無変更

③はテストだけで達成する（`main.py` の挙動は変えない）。ただし `main.py:525`
の注釈が producer の memo 文言を引用しているので、C4 の文言変更に合わせて
1 行だけ追随させた（実行文は 1 行も変えていない）。放置すると注釈が存在
しない文字列を指す。当初は「`main.py` に 1 行も触れない」と書いていたが、
それは注釈の正確さを犠牲にする条件だったので改めた（simcodex R3 の指摘）。

### 13.2 終局語義の表（本 commit の意味論の全体）

| 状況 | producer | 消費側 | count/error | main の終局 | 原票 | Sheets |
|---|---|---|---|---|---|---|
| pypdf 未導入 | `PdfSplitError` | `_page_error` p1/1 | 1/1 | Failed | **保持** | **1 行も書かれない**(H-7) |
| `PdfReader()` / `len()` 失敗 | `PdfSplitError` | 同上 | 1/1 | Failed | **保持** | 同上 |
| 多頁だが全頁の書出に失敗 | `PdfSplitError`（C2 末尾。`total_pages=N` を載せる） | `_page_error` を **p1..pN の N 件** | N/N | Failed | **保持** | 同上 |
| 正常な単頁 PDF | 0 件 return | 尾段へ | — | 従来どおり | 従来どおり | 従来どおり |
| 中途で一部の頁だけ失敗 | その頁を skip して継続 | `never_entered` 補填占位 | n/k | partial_error | **歸檔** | MF 集計行に頁番号＋memo |

**「占位」の語の正確な意味**（Codex 評審 P2 の指摘で明確化）: `_page_error`
payload は**顧客可視の行ではない**。`main.process_file` はこれを Sheets に
書かず、`count`/`error_pages`/progress を駆動するだけである。MF 集計行が
出るのは partial_error のときだけ（H-7）。上表 1〜3 行目の終局は
「Sheets 無痕 ＋ 原票は Drive に残り 3 秒ポーリングで再試行され続ける」。

これは IP-401 の「頁が無音で消える」とは**別種**である: 原票は歸檔されず
Drive に残るので、データはまだ失われていない（記帳待ちのまま滞留する）。
消えるのは「歸檔されたのに帳簿に無い」場合であり、本表にその行は無い。

**唯一の実質的回帰**: 「pypdf では開けないが Gemini なら読めた PDF」が
成功 → 滞留に変わる。§12.1② はこれを承知のうえで「多頁か単頁か判定
できない状態で全 PDF を Gemini 全体呼出へ落とすのは A と同じ事故再現経路
であり、しかも系統的」として尾段禁止を選んだ（趙拍板＋Codex 合意）。
滞留中に **Gemini は呼ばれない**（`PdfReader` 失敗が先）ので課金は無い。

### 13.3 タスク清単（TDD。各項に DoD）

**テストの置き場所**（Codex 評審 P3 を採用）: 新規 `test_pdf_split_contract.py`
を作り、producer/消費側境界の契約テストをそこへ集める。既存
`SplitProducerContractTest` は producer 契約そのものなので**移設**する。
理由は関心の分離だけでなく、`test_ip401_nondict_rawdata.py` が既に 889 行で
全局規約の 800 行上限を超えていること（H-8）。`TruncatedSplitTest`（消費側
補填の語義）は移設せず、C4 の文言追随だけ行う。

#### T1: RED —— ①頁単位隔離（`test_pdf_split_contract.py`）

1. `test_broken_page_is_skipped_and_later_pages_survive` — 3 頁の PDF で
   p2 だけ壊れる → 産出は `[1, 3]`（**現状は `[1]`**）、`total_pages` は 3。
   **失敗の注入点を subTest で 3 種**回す（Codex 評審 P2）:
   `reader.pages[i-1]` のアクセス / `writer.add_page` / `writer.write`。
   1 点だけだと「try を write の手前で閉じる」変異が生き残る
2. `test_last_page_failure_ends_quietly_without_raising` — 既存
   `test_producer_swallows_midway_failure_and_stops_quietly` の移設・改訂。
   例外が外へ出ないことは維持し、期待産出を旧挙動の `[1]` から改める
   （**意図した挙動変更**であってテストを実装に合わせて緩めたのではない、
   と名前と docstring から読めるようにする）。壊す頁は**最終頁**にする
   —— 中間頁の生存は 1 の subTest が見ているので、こちらは 1 が見ていない
   末尾の境界へ寄せる（§13.9。当初は 1 と同一パラメータで重複していた）。
   この 1 本が「中途で `PdfSplitError` を投げる」変異（C1 契約違反）と
   「`produced == 0` を `produced < total_pages` に緩める」変異も殺す
3. `test_skipped_page_gets_a_placeholder_from_the_consumer` — 実物 producer
   ＋実物 `process_pipeline` を通し、p2 が `_page_error` 占位として現れ
   memo に「PDFページ分割失敗」を含む（①と既存 `entered_pages` 補填の
   **結合**を 1 本で押さえる。どちらかを mock すると証明できない）

**DoD**: 1・2・3 が実装前に FAIL。

#### T2: RED —— ②0 件出口の区別（同ファイル）

尾段に入っていないことの assert は `_route_ocr_strategy.assert_not_called()`
を**主**とする（Codex 評審 P2）。尾段は必ずここを通る（H-6）ので、
「Gemini は呼ばれなかったが尾段には入っていた」変異まで殺せる。
`_call_gemini` / `_call_gemini_bytes` の未呼出も併せて見る（課金の観点）。

共通の assert は `_assert_tail_not_entered(out, mocks, total_pages=N)` に
まとめ、**占位が「判明した頁数ぶん」出ていること**（`page_num` が
`1..N` で `total_pages` が全件 N）も併せて見る（§13.9 の裁決）。

4. `test_missing_pypdf_does_not_enter_tail` — `PdfReader=None`＋`PdfWriter=None`
   → `_route_ocr_strategy` 未呼出 ＋ `_page_error` が 1 件（頁数不可知）
5. `test_unreadable_pdf_does_not_enter_tail` — `PdfReader()` / `len()` が
   例外 → 同上（subTest で 2 種）
6. `test_all_pages_broken_does_not_enter_tail` — 3 頁と読めるが全頁の
   書出が失敗 → 尾段へ落ちず、**占位が 3 件**（C2 末尾の `produced == 0` と
   `total_pages` の引継ぎ。Codex 評審 P1 ＋ §13.9）
7. `test_single_page_pdf_still_falls_through_to_tail` — `len(reader.pages)==1`
   → 尾段が動く（**変更しない出口**の錨。既存の単頁 PDF テスト
   （`test_ocr_engine_invoice` の `_run_single_page_pipeline` 系）は
   `_split_pdf_pages` を mock しているので、実物の `<= 1` 分岐は
   今まで無検査だった）
8. `test_split_error_placeholder_names_the_cause` — 占位 memo が
   「PDF分割不可」を含み、**原因文字列が引き継がれる**
   （`pypdf未導入` / `PDF読取失敗` / `全頁のPDF分割に失敗` の 3 種を
   subTest で。固定文言に潰す変異を殺す）

**DoD**: 4・5・6・8 が実装前に FAIL、7 が実装前に PASS（既存挙動の錨）。

#### T3: GREEN —— 実装（C1〜C4）

**DoD**: T1・T2 全緑 ／ 変更は `ocr_engine.py` の 3 箇所（例外クラス・
`_split_pdf_pages`・消費側 `next()` 周辺）＋ memo 文言 1 箇所 ／
`main.py`・`sheets_output.py`・`local_test.py` は無改変。

#### T4: 既存テストの追随（意図した変更ぶんだけ）

- `TruncatedSplitTest` の `assertIn("PDF分割が中断", ...)`（2 箇所）と
  同ファイル `:847` 付近の docstring → 「PDFページ分割失敗」へ
- `SplitProducerContractTest` を `test_pdf_split_contract.py` へ移設
- `grep -rn "PDF分割が中断" --include="*.py" .` が 0 件

**DoD**: 上記以外の既存テストは**無修正**で緑。

#### T5: ③番人テスト（`test_main_process_file.py`）

`_run_process_file` は `process_pipeline` を `with` 内で patch するので、
戻った後に `main.process_pipeline.call_args` は取れない（patch が外れて
実物に戻っている）。既存 20 箇所の呼出を壊さずに mock を取り出すため、
**任意引数 `capture=None`（dict）** を足す:

```python
def _run_process_file(pages, writer=None, progress=None,
                      resolver_side_effect=None, capture=None):
    ...
    with mock.patch.object(main, "process_pipeline",
                           return_value=iter(pages)) as pipeline, ...:
        if capture is not None:
            capture["pipeline"] = pipeline
```

テスト本体:

```python
    def test_main_does_not_pass_start_page_to_the_pipeline(self):
        capture = {}
        _run_process_file([_page(_valid_result(), 1, 1)], capture=capture)
        _, kwargs = capture["pipeline"].call_args
        self.assertNotIn("start_page", kwargs, ...)
```

失敗メッセージには「`start_page` を渡すなら `main.py:623` の
`range(1, last_total_pages + 1)` を同時に直すこと」を書く（番人が鳴った
とき、番人を消すのではなく本体を直すよう誘導する）。

**DoD**: 実装前に PASS（現状の前提が成立していることの確認）、
`main.py:505` に `start_page=1` を足す変異で FAIL。
既存 `test_main_process_file` は無修正で緑。

#### T6: 回帰と変異検証

**DoD**: 全量緑 ＋ 下表の変異が**全件**赤くなる。

| 変異 | 落ちるべきテスト |
|---|---|
| ①の `continue` → `return`（旧挙動へ回帰） | T1-1, T1-2, T1-3 |
| ①の `try` を for の外へ戻す | T1-1, T1-2, T1-3 |
| ①の try を `write` の手前で閉じる（部分隔離） | T1-1 の `write` subTest |
| 中途失敗時に `PdfSplitError` を raise する（C1 契約違反） | T1-2 |
| C1 の `raise` → 元の `print` ＋ `return`（pypdf 未導入） | T2-4, T2-8 |
| `PdfReader` 失敗の `raise` → 元の `print` ＋ `return` | T2-5, T2-8 |
| C2 末尾の `produced == 0` ブロックを削除 | T2-6 |
| `if total_pages <= 1: return` → `raise PdfSplitError` | T2-7 |
| C3 の `except PdfSplitError` ブロックごと削除 | T2-4, T2-5, T2-6（最外 except に吞まれ 0 件 yield） |
| C3 の `yield` を消して `return` だけにする | T2-4, T2-5, T2-6, T2-8 |
| C3 の memo から `str(split_err)` を落として固定文言にする | T2-8 |
| C4 の memo 文言を元へ戻す | T1-3 と追随済みの `TruncatedSplitTest` |
| `main.py:505` に `start_page=1` を追加 | T5 |

### 13.4 受入基準（脚本判定）

```bash
cd "/Users/ibridgezhao/Documents/Super Scaner"
venv311/bin/python -m unittest discover -p "test_*.py"        # → OK, Ran >= 799
venv311/bin/python -m unittest test_ip401_regression -v       # → OK（無修正）
venv311/bin/python -m unittest test_pipeline_consumers -v     # → OK（無修正）
venv311/bin/python -m unittest test_pdf_split_contract -v     # → OK（新規）
grep -rn "PDF分割が中断" --include="*.py" .                    # → 0 件
```

人手判定:
- `git diff --stat` が `ocr_engine.py` / `main.py`(注釈 1 行) /
  `test_pdf_split_contract.py`(新規) / `test_ip401_nondict_rawdata.py` /
  `test_main_process_file.py` / 本 Plan に収まる
- `main.py` は注釈 1 行のみ（C4 の文言追随。実行文は無変更）、
  `sheets_output.py` / `local_test.py` には 1 行も触れていない

### 13.5 影響面

| 対象 | 影響 |
|---|---|
| `ocr_engine._split_pdf_pages` | 例外契約が変わる（無言 return → `PdfSplitError`）。**本番呼出点は 1 箇所**（H-4）で同 commit 内に対処 |
| 壊れた頁を含む多頁 PDF | 失う頁が「i 以降の全部」→「頁 i だけ」 |
| pypdf 未導入 / 開けない PDF / 全頁書出失敗 | 全体 Gemini 呼出 → `_page_error` ＋ 保持（**Gemini 課金なし**、Sheets 無痕、原票は Drive に残る） |
| 正常な PDF（単頁・多頁とも） | **完全に無変化** |
| `local_test.py` | 無改変。`process_pipeline` 越しに同じ改善を受ける |
| `benchmark_ocr.py` | 無改変だが、**pypdf 未導入・読取失敗時は尾段ベンチが走らなくなる**（`_page_error` 1 件で終わる）。ベンチ結果の解釈が変わるので注記が要る（Codex 評審 P2） |
| メモリ | 無変化（`data = buf.getvalue()` は従来 `yield` 中に生きていた同じ 1 本の bytes） |
| 既存テスト | `TruncatedSplitTest` の文言 2 箇所 ＋ `ProcessFileTerminalStateTest` の docstring 1 箇所 ＋ `SplitProducerContractTest` の移設・期待値変更（意図した挙動変更）以外は無修正 |

### 13.6 風険と回退

| 風険 | 対処 |
|---|---|
| 破損 PDF が保持され 3 秒ポーリングで滞留し続ける（Sheets 無痕） | 課金は無い（Gemini 未呼出）。**現状も同じ終局**（尾段の Gemini が失敗すれば `_page_error` 1 件 → Failed → 保持）なので悪化ではない。可視化の改善は §13.7 へ |
| 出力順が `1,2,4,...,20,3` になりうる | §12.1 で許容裁定済み（`_page_error` は Sheets 本体へ書かれず集計行へ回る） |
| 回退 | 単一 commit なので `git revert` で全体を戻せる。部分回退（①だけ残す等）は C2/C3 が対で意味を成すため不可 |

### 13.7 本 commit 後に残るもの

- **全頁エラー時の可視化**（Codex 評審 P2 由来）: `count == error_pages` の
  とき main は Sheets に 1 行も書かず、通知文も
  「API障害または認証エラーの可能性」の固定文言で `failed_page_notes` を
  使わない。PDF分割不可の原因は控制台 print にしか出ない。`main.py` の
  変更を伴うので本 commit（§12.1 の 3 件）の範囲外。P2 として登録
- §12.2 の B（§11.1 共有 generator 化）と C（§11.2 空出力の構造的兜底）
- §12.3 の 2 件（`_pdf_pages` の重複コピー、壊れ dict 経路）

### 13.8 Codex 評審の辯論記録（2026-08-17。§13 に対して）

Codex 指摘 9 件。**全面採用 4 件 / 部分採用 4 件 / 駁回 1 件**。
（当初「8 件・採用 6/部分 1/駁回 1」と書いていたが、P 番号を数え直すと
P1-1, P1-2, P2-1〜P2-4, P3-1〜P3-3 の 9 件。simcodex R3 の指摘で訂正。）

**全面採用（4 件）**

| # | 指摘 | 反映先 |
|---|---|---|
| P1-1 | 多頁 PDF の全頁書出失敗が尾段へ落ちる。②が塞ぐと決めた出口と同種であり残件化は誤り | C2 末尾の `produced == 0` ＋ T2-6。なお「①が新しく作る経路」との理由付けは不正確（旧実装でも p1 が壊れれば同じ経路に落ちた）。①は**この経路を狭めた**が塞いではいない。結論は変わらないので採用 |
| P2-2 | 頁単位隔離テストが `add_page` 失敗に偏り、`write` を try 外へ出す変異が残る | T1-1 を 3 注入点の subTest 化 ＋ 変異表に 1 行 |
| P2-3 | Gemini 未呼出だけでは「尾段に入っていない」を証明できない | `_route_ocr_strategy.assert_not_called()` を主 assert に。H-6 で経路を確認済み |
| P3-3 | memo の原因引継ぎを潰す変異が表に無い | 変異表に「C3 memo から `str(split_err)` を落とす」＋ T2-8 を 3 原因の subTest 化 |

**部分採用（4 件）**

| # | 採用した部分 | 採らなかった部分と理由 |
|---|---|---|
| P1-2 | C1 の docstring に「loop 内で raise してはならない」を契約として明記。変異表に「中途で `PdfSplitError`」を追加 | 専用テストの新設は不要。T1-2（`list(_split_pdf_pages(...))` が例外を出さない）が同じ変異を殺す。テストを増やすより既存の 1 本が何を守っているかを明記する方が良い |
| P2-1 | 「占位」の語が顧客可視の行と誤読される点を §13.2 に明記（H-7 を事実表へ追加） | 「main の全頁エラー通知に `failed_page_notes` を反映するテスト」は `main.py` の変更を伴い §12.1 の 3 件の範囲外。§13.7 に P2 として登録 |
| P2-4 | §13.5 の `benchmark_ocr.py` 行を「尾段ベンチが走らなくなる」へ具体化 | benchmark 側のテスト新設は駁回。`benchmark_ocr.py` は開発ツールで、pypdf 未導入は `requirements.txt` に固定されている以上の常態ではない |
| P3-2 | 新規 `test_pdf_split_contract.py` を作り `SplitProducerContractTest` を移設 | `TruncatedSplitTest` は移設しない（消費側補填の語義であり producer 契約ではない。移設すると C4 の文言追随以外の差分が増え、レビューで「意図した変更」と「巻き添え」の区別が付かなくなる） |

**駁回（1 件）**

- **P3-1（T5 の `capture` 引数は過度。番人テストだけ単独 patch せよ）**
  `_run_process_file` は `mock.patch.object(...)` を `as` で受けずに使うので、
  with を抜けた後は mock への参照が残らず `call_args` を読む手段が無い
  （**複審での訂正**: 参照さえ保持すれば with の外でも読める。当初の理由文
  「到達不能」は不正確だった）。`capture` はその参照を呼出側へ渡す最小手段
  である。代替案はいずれも
  `send_notification` / `PageUrlResolver` / `redirect_stdout` の三重 patch を
  **もう 1 部複製する**ことを意味し、この setup こそが漂移の温床
  （`ocr_test_helpers.pdf_pages` の docstring が同じ理由で共有化を選んでいる。
  simcodex R1 でも `_run_paged_pdf_truncated` の重複を解消したばかり）。
  3 行の任意引数 1 つと、20 行の setup 複製を比べて前者を採る。

### 13.9 simcodex の辯論記録（2026-08-17。実装後）

Round 1（simplify 4 観点 ＋ `codex review --uncommitted`）と Round 2 を回した。
**Round 2 の codex は指摘ゼロ**（自ら実物を走らせて `[(1,3),(3,3)]` を確認）。

#### Round 1（採用 5 / 見送り 0）

| 観点 | 指摘 | 裁決 |
|---|---|---|
| reuse | なし（複用点 5 箇所は正しく既存物を使っている） | — |
| efficiency | なし。pypdf 4.3.1 の `_VirtualList` は初回アクセスで `_flatten()` し以後 O(1) なので、**ランダムアクセスは順次走査と同コスト**（H-1 をソースで裏取り）。旧コードが 2 回呼んでいた `len(reader.pages)` が 1 回に減る副次的改善も確認 | 事実として記録 |
| simplification | ①失効した行番号参照 ②T1-2 が T1-1 の subTest と同一パラメータ ③`capture` の docstring が patch の数を誤記 ④`PdfSplitError` の契約が 2 箇所に重複 | ①③④採用。②は**削除ではなく差別化**（最終頁を壊す形に変え、`produced == 0` 変異も殺せるようにした）——削ると simcodex R1 で置いた producer 契約の番人が消える |
| altitude | `PdfSplitError` が `total_pages` を捨てている。20 頁の PDF が全滅しても進捗タブ（`page_progress.py:311` の `seen/total`）に「1/1」と出て単頁失敗と区別が付かない | 採用。例外に `total_pages` 属性を追加 |

#### Round 1 の codex review（P2 1 件・採用）

> 占位を 1 件だけ出して `total_pages` に N を名乗ると、`main` のカバレッジ
> 突合が p2..pN を欠落と見なして監査タブへ書く。この経路は全頁失敗＝
> ファイル保持なので、**再試行のたびに監査行が増殖**する。

**これは上の altitude 採用が生んだ回帰**（当方が連鎖を読み切れていなかった）。
codex の 2 案（(a) 判明した全頁ぶん占位を出す / (b) この終局だけ突合を抑止）
のうち **(a) を採用**。(b) は `main` に特例を足すことになり、しかも
`main` は本 commit の範囲外。(a) は既存の `never_entered` 補填と同じ形なので
機構が 1 つ増えない。事実確認: 進捗タブは檔ごとに 1 行を `update` する方式
（`page_progress.py:206` で append、`:345` で update）＋節流なので、
占位が N 件でも Sheets 書込は増えない。

#### Round 2（採用 3 / 却下 1・実験で決着）

| 観点 | 指摘 | 裁決 |
|---|---|---|
| reuse | 新設した `AllPagesErrorLeavesNoSheetRowsTest` は既存 2 テストと重複 | **採用**（下記の実験で確認）。クラスを削り、純増の assert（`audit_calls == []`）を既存 `test_page_error_still_counted_and_skipped` へ畳んだ |
| efficiency | なし。500 頁が全滅しても Sheets 書込は約 3 回（節流）で、全頁失敗分岐は `failed_page_nums` を文字列化しない（するのは partial_error 分岐） | 事実として記録 |
| simplification | ①`:2213-2217` の行番号が漂移して無関係な箇所を指す（2 箇所）②消費側 except の注釈が `PdfSplitError` の docstring と同じ理由を 3 度目に展開 | 両方採用。行番号は話文参照へ、理由は指針 1 行へ圧縮 |
| altitude | Plan §13.1/§13.3 が**却下された旧案（占位 1 件）のまま**で、実装と食い違う | 採用。本節と §13.1/§13.3 を更新 |

**reuse と altitude が割れた点を実験で決着させた**: altitude は新テストを
「層間契約テストとして正しい層」と評価し、reuse は「既存 2 本の焼き直し」と
評価した。`main.py` のカバレッジ突合から `- seen_page_nums` を落とす変異
（M17）を注入したところ、**既存 9 本が落ちた**（新テストは 10 本目）。
すなわち新テストは唯一の番人ではなく、削っても検出力は落ちない。
一方 M16（占位を 1 件に潰す）を殺すのは producer 側の
`test_all_pages_broken_does_not_enter_tail` だけであり、そちらは残す。

#### 変異検証（最終形。**17 件すべて赤**）

§13.3 T6 の 13 件に、simcodex の裁決で 4 件を追加した:

| 変異 | 落ちたテスト |
|---|---|
| M14 `produced == 0` → `produced < total_pages` | `test_last_page_failure_ends_quietly_without_raising` 他 2 |
| M15 `declared = split_err.total_pages or 1` → `= 1` | `test_all_pages_broken_does_not_enter_tail` |
| M16 占位を 1 件だけにする | 同上 |
| M17 `main` の突合から `- seen_page_nums` を落とす | `test_page_error_still_counted_and_skipped` 他 9 |

#### 本 commit の範囲外として記録した既存問題

ファイル保持中は 3 秒ごとの再試行が毎回 `progress.file_started` を呼ぶため、
進捗タブに 1 行ずつ追加され続ける（B7 心跳機構の既存挙動で、頁数とは無関係。
1 頁失敗でも 500 頁失敗でも同じ速度で増える）。本 commit が作った経路では
ないが、`PdfSplitError` の終局が「保持」である以上この滞留に晒される。
§13.7 の残件に併せて記録する。

#### Round 3（採用 3 / 駁回 2。codex は 2 ラウンド連続で指摘ゼロ）

| 観点 | 指摘 | 裁決 |
|---|---|---|
| altitude | ①§13.2 の語義表だけ旧案（占位 1 件）のまま ②C5/§13.4 が「`main.py` 無変更」と書いているが実際は注釈 1 行を変えた ③**C3 が `start_page` を無視するのは本 commit が新たに開いた経路なのに、Plan にも Codex 記録にも無く、テストも 0 件** | 3 件とも採用。③には `test_split_error_placeholders_ignore_start_page` を追加し、変異 M18（C3 の範囲を `range(start_page, ...)` にする）で牙を確認した |
| reuse | 孤児なし・重複造轮なし（実測 90 tests 緑）。C3 と `never_entered` の 2 つの占位ループが形状として重複 | 前者は事実として記録。後者は Round 2 で裁決済み（合併しない）＋指摘者自身が「非必須」としているので駁回 |
| simplification | コードの指摘は 0。文書の不整合 6 件（§13.8 の件数が本文・小見出し・表の行数で三者不一致 ／ §13.5 が `ProcessFileTerminalStateTest` の docstring 追随を落としている ／ §13.4 の `git diff --stat` 列挙に `main.py` が無い ／ `page_progress.py:345` の引用 ／ `test_ocr_engine_invoice.py:158` が mock 行でなく docstring 行を指す ／ テストファイル 2 本が 800 行上限を超えたまま） | 5 件採用、1 件駁回（`page_progress.py:345` は `self._ws.update(` の行そのもので**引用は正しい**。指摘者は `_update_row` の定義/呼出行と取り違えている） |
| efficiency | 新テストが実ファイルを 13 回作るが、実際に中身を読むのは 1 テストだけ。12 回は文字列パスで足りる | **駁回**。節約できるのはマイクロ秒（指摘者自身の評価）で、代わりに「どのテストの path は実在しなければならないか」を都度判断する前提を持ち込む。将来どれかが実 `open()` に触れた瞬間に不可解な失敗になる。全量 808 tests が 3 秒で回る現状ではトレードが合わない |

**③が本ラウンドの収穫**: 実装時に「start_page は使わない」と注釈だけ書いて
決めていた。逐頁ループ側の補填は `range(start_page, total+1)` を使うので
**非対称**であり、その非対称に根拠（`start_page > declared` だと 0 件 yield に
なって IP-401 の不変式を破る）があることも、テストで固定されていることも、
どこにも無かった。simcodex R1 が「前提そのものが無検査」を見つけたのと
同型の欠落である。

**変異検証の最終形は 18 件**（§13.9 の 17 件 ＋ M18）。全件が赤になることを
確認済み。

#### Round 3 で顕在化した、本 commit の範囲外の残件

- **テストファイルの行数**（simcodex R3）: `test_main_process_file.py` は
  本 commit 前から 804 行で上限超過しており、③の番人と `capture` で
  **850 行**になった（純増 46）。`test_ip401_nondict_rawdata.py` は
  `SplitProducerContractTest` を移設して 889 → **828 行**まで下げたが、
  まだ上限内ではない。H-8 は「800 行超過」を移設の根拠の 1 つに挙げたので、
  同じ論理はこの 2 本にも及ぶ。分割は本 commit の範囲（`_split_pdf_pages`
  周辺）を超えるため、§12.2 の B（共有 generator 化）でテスト側を
  触るときに併せて片付けるのが自然
