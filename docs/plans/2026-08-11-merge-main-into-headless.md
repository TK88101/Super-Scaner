# main → feature/sandevistan-headless マージ計画（2026-08-11）

`main` の worksheet 快取失効修復（IP-403）と失敗退避護欄を、串行専案の
作業分支 `feature/sandevistan-headless` へ取り込む。

- merge-base: `0543de5`
- 取り込む main 側コミット: `f7237c0` / `4134a6d` / `116216e` / `fd306fc`（4 件）
- 分支側の先行コミット: 46 件（IP-301〜IP-403 / B1〜B8）
- merge は実行済み（衝突状態で停止中）。`git merge --abort` で完全巻戻し可。

---

## 1. 目標と非目標

### 目標
1. main 側 4 コミットの**行為**を分支へ完全に取り込む（tab 失効自愈・備份競合封じ・失敗退避護欄）。
2. 分支側 46 コミットの**行為を一切退行させない**（IP-304 頁級台賬・IP-402 除外語義・§5.1-d 顧客 tab・B7/B8 回報）。
3. マージ後に「git は解けたが行為が壊れている」箇所をゼロにする。

### 非目標（本 merge では触らない）
- headless 書込点（`commit_page` / `peek_append_range` / `probe_page`）への tab 失効自愈の展開 → **§4 D3 で別立項**。
- 既存の設計裁定の再議（貸方一律「未払金」・三経路の不統一・除外ページ語義など、趙の既裁定事項）。
- リファクタリング一般（衝突解消に不要な整理は行わない）。

---

## 2. 事実表（両側が何を変えたか）

| 側 | 対象 | 変更の実体 |
|---|---|---|
| main | `sheets_output.py` | `_with_tab_recovery` / `_invalidate_tab` / `_resolve_tab` を新設。`append_entries`・`_write_unrecognized_row`・`append_audit_row` の「実測→容量確保→書込→取引No」を 1 つの復旧単位（closure）に包む。`_write_unrecognized_row` の署名から `ws` を削除。 |
| main | `main.py` | `_next_backoff_seconds` / `_record_file_failure` / `_record_file_success` / `_is_file_backed_off` / `_partition_by_backoff` / `_format_backoff_summary` 等を新設。`main()` ループで ready/backed_off へ分割、1 ファイル分の処理を try で受け止め、成否で退避記録を更新。 |
| main | `gas/*.gs` ×3 | 備份の読取→削除間の競合封じ（sheetId + lastRow 再実測、全有全無）。**衝突なし・自動合併済み**。 |
| HEAD | `sheets_output.py` | `append_entries` の Phase A を `_build_result_rows` へ抽出、占位行構築を `_build_unrecognized_block` へ抽出。headless 頁級原子書 `build_page_write` / `commit_page` / `next_txn_no` / `peek_append_range` / `probe_page` を新設。`_tab_namer` 注入（§5.1-d）。 |
| HEAD | `main.py` | `for file in files:` の本体を `_process_one_file` へ抽出（戻り値なし・早期 return 多数）。headless 分流・intake gate・memo・台賬・五態回報を実装。 |

**両者は直交する**（片方は「書込先が消えた時の自愈」、片方は「構築と書込の分離＋headless 経路」）。ただし**同一の行**を書き換えているため git が衝突を出している。

---

## 3. 衝突ブロック一覧と裁決（11 件）

### main.py（4 件）

| # | 位置 | 裁決 |
|---|---|---|
| C1 | import（`dataclass`/`Enum` vs `datetime`） | **併集**。3 つとも要る。 |
| C2 | `from sheets_output import ...`（`AUDIT_VERDICT_DRIFT` vs `JST`） | **併集**。HEAD の DRIFT（IP-402）と main の JST（退避摘要の時刻整形）は両方使う。 |
| C3 | `while True:` 冒頭（`cycle += 1` vs `now_ts`/`backed_off_total`/`earliest_next_attempt_ts`） | **併集**。 |
| C4 | ループ本体（`_process_one_file(...)` 呼出 vs main 側の旧本体全文） | **三者合成**（Codex P0 採納）。①イテレート対象は `ready_files`（HEAD ブロックは `for file in files:` なので字義どおり採ると直前で作った `ready_files` を無視し、退避中ファイルを毎輪処理する＝護欄が完全に無力化）②本体は HEAD の `_process_one_file` 呼出（main の旧本体を採ると intake gate・posting ledger・顧客 tab 分離・五態回報・provider events が丸ごと消える）③戻り値を受けて retry action を適用（§4 D4）。 |

### sheets_output.py（3 件）

| # | 位置 | 裁決 |
|---|---|---|
| C5 | `_build_result_rows` 内 `if not rows:` | **HEAD の `return None`**。ここは純構築関数（書込副作用ゼロ）で、占位行への分流は呼出側の責務。main 側の分流ロジックは `append_entries`（Phase B）に既に存在する。 |
| C6 | `_build_*` 群・headless 新 API 群 vs main の recovery closure | **両方採る合成＋変数の語義分離**（Codex P1 採納）。HEAD の新メソッド群をそのまま残し、`append_entries` の Phase B に main の `_read_ensure_and_write` closure ＋ `_with_tab_recovery` を適用する。**ただし `block.next_txn_no`（＝次番号）を `transaction_no`（＝今回書く番号）へ代入してはならない**——main の closure は `actual_txn_no != transaction_no` で復旧を判定するので、+1 された値と比べることになり**通常経路でも毎回「tab 復旧に伴い取引No を再採番」と誤表示する**。行値は偶然同じ値へ再代入されるため既存テストは通ってしまう（＝テストで検出できない語義漂移）。`built_txn_no`（今回書く番号）と `next_txn_no`（次番号）を別変数に分け、比較は前者、書込後のキャッシュ更新は実測値から行う。 |
| C7 | `_write_unrecognized_row` 本体 | **合成**。署名は main（`ws` なし・自動合併済み）。row 構築は HEAD の `_build_unrecognized_block`。書込は main の recovery closure。 |

### test_sheets_output.py（4 件）

| # | 位置 | 裁決 |
|---|---|---|
| C8 | `_make_writer` 署名 | **3 引数の併集**：`(spreadsheet=None, audit_row_count=0, tab_namer=None)`。 |
| C9 | `_make_writer` 本体 | **併集**：`_tabs_sanitized` ＋ `_audit_row_count` ＋ `tab_namer` 条件付き上書き。 |
| C10 | `_FakeAuditSpreadsheet` vs `_FakeSpreadsheet`（1054/1057） | **main の実装を採り、名前を `_FakeAuditSpreadsheet` へ戻す**（§4 D5 参照）。 |
| C11 | `_make_audit_writer` 本体 | **main の実装**（`_make_writer` へ寄せる）。ただし C10 の改名に追随。**改名追随の対象は `_make_audit_writer` と `_make_recovery_writer`（test_sheets_output.py:1239）の 2 つ**——後者は自動合併で衝突ブロックに現れないため見落としやすい（Codex 指摘）。docstring 中の言及も追随させる。 |

---

## 4. 「git は解けるが壊れる」箇所（merge が一切警告しない）

### D1 — `append_entries` の `_write_unrecognized_row` 呼出が引数不一致
HEAD 側は `self._write_unrecognized_row(ws, tab_name, entries_data, source_url)`。
main が署名から `ws` を削除しており、署名行自体は衝突ブロックの**外**で自動合併された。
放置すると `TypeError`（占位行を書く全経路が死ぬ）。
**対処**：呼出を `(tab_name, entries_data, source_url)` へ修正。

### D2 — `_write_unrecognized_row` 本体の `ws` 未定義
HEAD 側本体は `ws` を参照する（`_get_next_txn_no(tab_name, ws)` 等）が、署名から消えている。
放置すると `NameError`。
**対処**：C7 の合成で `self._resolve_tab(tab_name)` 経由へ。

### D3 — headless 書込点に tab 失効自愈が無い【本 merge の非目標・別立項】
main の修復は UI 経路の 3 書込点のみを覆う。分支が新設した headless 側は
**4 点**が未保護（Codex 指摘で `next_txn_no` を追加）。しかも 4 点は同列に扱えず、
**3 つの類**に分かれる：

| 類 | 対象 | 正しい扱い |
|---|---|---|
| ① PENDING 前の preparation | `next_txn_no`・`peek_append_range` | 自愈**可能**。ただし個別に closure を被せるだけでは不十分——両者の間で tab が消えると、旧取引Noで構築した rows と新 tab の予測範囲が混ざる。「tab 解決 → 採番 → PageWrite 構築 → 予測範囲」を**一つの再実行可能な単位**にする必要がある |
| ② PENDING 後の commit | `commit_page` | 素朴な再実行は**禁止**（下記） |
| ③ 既存 witness の probe | `probe_page` | tab を**再作成してはならない**。現行の ESCALATE 語義が正しい |

**②が危険な理由**（Codex による補強を反映）：
PENDING は `peek_append_range` の予測行範囲**と行指紋**を commit 前に永続化する。
tab が再作成されると、(a) 行番号が全て変わり、(b) **復旧時の取引No再採番によって
A 列が変わるため `compute_page_fingerprint` の値そのものが変わる**。
witness を更新せずに再書込すると、次回起動時の probe は範囲・指紋の両方で外れる。

ただし「必ず ESCALATE になる」は過大表現だった（Codex P2 採納）。実際に壊れるのは
**復旧後の append と confirm の間で落ちた場合**に限る——commit と confirm が連続成功すれば
probe は走らない。危険なのは条件付きの窓であって常時ではない。

**本 merge では直さない**。理由：
1. 正しい設計は「新しい範囲・再採番後の指紋を PENDING へ再 claim してから append する」
   ＝ witness 更新機構の新設であり、merge の作業量ではない。
2. headless は未だ生産経路ではない（`HEADLESS_MODE` 未設定＝生産は UI 版）。
3. merge のコミットに新機能を混ぜると「この merge が何をしたか」が審査不能になる。

**成果物**：欠陥を文書に立項し、残存リスクとして本 Plan §10 にも明記した上で、
**趙へ明示的に報告する**（P1・headless 生産投入前に必須）。
実行時の投入ブロッカーはコードに入れない——`HEADLESS_MODE` の可否は運用判断であり、
オーナーの拍板前に落碼しない（§12 の辯論で確定）。

### D4 — 失敗退避護欄が `_process_one_file` に届かない
main の護欄は「`for file in files:` の本体」に直接書かれていた。分支はその本体を
`_process_one_file` へ抽出済みで、同関数は**戻り値を持たず**（全経路 `return` のみ）、
かつ**失敗ではない早期 return が 4 つある**（intake 状態 memo 命中 / gate 拒否 /
outcome memo 命中 / 重複検出）。C4 で HEAD 側を採ると護欄が消える＝main の
コミット `116216e` の行為が取り込まれない。

**対処（Codex 評審を反映した確定案）**：`_process_one_file` に**retry action** を返させる。

戻り値は業務成否ではなく「退避記録に対する操作」として命名する（Codex P2 採納——
`SUCCESS/FAILURE/SKIPPED` は業務結果と再試行操作を混同する。実例：headless の
`DEAD_LETTER` は業務上は失敗だが退避操作としては CLEAR が正しい）：

```
RETRY_CLEAR      # 退避記録を消す（この file は決着した）
RETRY_INCREMENT  # 失敗カウント +1・次回試行時刻を後ろへ
RETRY_KEEP       # 退避記録に一切触れない（別の抑制機構が担当中）
```

**全 return 点の対応表**（6 系統。当初「4 つ」としたのは誤り・Codex 指摘）：

| # | 位置 | 状況 | action | 根拠 |
|---|---|---|---|---|
| 1 | main.py:596 | intake 状態 memo 命中 | `KEEP` | memo が既に費用を止めている。backoff を重ねると epoch 更新後の正常再開を遅らせる |
| 2 | main.py:609 | intake gate 拒否 | `KEEP` | 同上（`STATE_NOT_ALLOWED` memo が担当） |
| 3 | main.py:617 | outcome memo 命中 | `KEEP` | 同上 |
| 4 | main.py:627 | UI 重複検出（move 済） | `CLEAR` | archive 完了＝決着。以後 list_files に現れない |
| 5 | main.py:642 | 未対応拡張子 | `KEEP` | main 側の原実装も `continue` で護欄の外。行為を変えない |
| 6 | main.py:677 | customer_id 欠落（奇形 job・保持） | `KEEP`（暫定） | §12 で辯論中。headless 専有経路で main 側に無く、backoff 付与は merge ではなく新規行為追加になる |
| 7 | 正常終了・UI | `process_file` が真 | `CLEAR` | |
| 8 | 正常終了・UI | `process_file` が偽 | `INCREMENT` | 唯一 backoff が担当する経路 |
| 9 | 正常終了・headless | `HeadlessOutcome` を正常に返した（**五態すべて**） | `CLEAR` | headless は outcome ごとの抑制を memo 側で完結している（下表）。backoff を重ねない |
| 10 | 例外 | 両経路共通 | `INCREMENT` | outcome が無く memo も記録されない＝唯一の無限リトライ経路 |

**headless 五態に backoff を被せてはならない理由**（S1 の自己訂正を Codex が支持）：

| outcome | 既存の抑制 | backoff を足すと |
|---|---|---|
| `SUCCESS` / `DEAD_LETTER` | 終態回報＋memo | 不要 |
| `PARTIAL` | memo（F06-How TBD 接縫） | 不要 |
| `ESCALATED` | memo ＋ TTL 20 輪 | **二重抑制**。控制面の修復後の再判定を最大 1 時間遅らせる |
| `FAILED` + `retryable=True` | **意図的に memo 不記＝3 秒自癒窓**（B4 設計） | **3s→30s→5min→30min→1h へ変質＝既定設計の静かな転覆** |
| `FAILED` + `retryable=False` | memo（per epoch） | 二重抑制 |

**例外護欄の位置**（Codex の簡潔案を採納・S3 を解決）：
main 側は `download_file` / `start_new_file` / `process_file` だけを内側 try で包んでいたが、
本分支では **`main()` 側で `_process_one_file` 呼出**し**全体**を per-file try で包む。
これで `_report_headless_outcome`（Firestore 書込）や `move_file`、gate の例外も
同じ境界で隔離される——内側 try のままだと Firestore 障害が外殻まで逃げ、
同バッチの後続ファイルを巻き添えにする（護欄が防ごうとした当の事象）。
一時ファイル削除は `_process_one_file` 内の `finally` へ移す（例外時のリーク防止）。

### D6 — `transaction_no += 1` の消失【実施中に発覚・Plan にも Codex にも無かった】
main は `append_entries` を closure 内の再採番へ寄せた結果 `transaction_no += 1` の
1 行を**削除**した。この削除は衝突ブロックの**外**にあるため git が自動適用する。
ところが分支側の `_build_result_rows` は同じ行を「`ResultBlock.next_txn_no`＝次番号」を
作るために使っており、`build_page_write` は頁内の票ごとにこの値で採番を進める。

**症状**：1 頁に複数の票があると全票が同じ取引No になる
（`test_txn_number_advances_per_ticket` が `[7,7,7] != [7,7,8]` で落ちて発覚）。
merge も静的解析も何も言わない。**既存テストが無かったら本番まで抜けていた**。

**対処**：`_build_result_rows` の `return ResultBlock(...)` 直前に復元し、
「merge で消してはならない理由」をコード注釈に残す。
`append_entries` 側は `block.next_txn_no` を使わなくなった（実測値から
`_tab_next_txn` を更新する）ので、復元しても UI 経路には影響しない。

### D5 — `_FakeSpreadsheet` の同名クラス衝突（テストが静かに壊れる）
分支は `test_sheets_output.py:838` に `_FakeSpreadsheet`（`{tab: [[rows]]}` を受け、
`ProbePageTest` が使う）を持つ。main は 1057 に別語義の `_FakeSpreadsheet`
（`{tab: ws}` を受け、`get_worksheet_by_id` を持つ）を定義した。
Python は後定義が勝つため `ProbePageTest._writer` が main 版を掴み、
`{ws.id: ws for ws in ...}` が list に `.id` を求めて `AttributeError`。
git は何も言わない。

**対処**：main 側クラスを `_FakeAuditSpreadsheet`（分支側の元の名）へ戻し、
main が統合した `get_worksheet_by_id` と自増 id は保持する。分支側 838 の
`_FakeSpreadsheet` は名前も実装もそのまま。参照点（`_make_audit_writer`・
recovery 系 writer 工場）を改名に追随させる。

---

## 5. タスク一覧（各項に DoD）

| T | 内容 | DoD |
|---|---|---|
| T1 | C1–C3 の併集解消（main.py import・ループ冒頭） | `main.py` に衝突マーカー無し |
| T2 | C5–C7 の合成（sheets_output.py） | `sheets_output.py` に衝突マーカー無し・`_with_tab_recovery` が `append_entries` / `_write_unrecognized_row` / `append_audit_row` の 3 点で使われている |
| T3 | D1・D2 の修正（呼出署名と `ws` 解決） | `_write_unrecognized_row` の呼出が全て 3 引数・本体に未定義 `ws` 参照が無い |
| T4 | C8–C11＋D5（テスト工場と Fake クラスの合成・`_FakeAuditSpreadsheet` への改名、`_make_audit_writer`・`_make_recovery_writer` の追随） | `test_sheets_output.py` 単体が全緑 |
| T5 | C4＋D4（retry action の導入・`for file in ready_files:`・per-file try を `main()` 側へ・一時ファイル削除を `finally` へ） | 検収 4・5 のテストが全緑、`test_main_process_file.py` と `test_process_file_headless.py` が全緑 |
| T5.5 | C6 の偽再採番テスト（TDD：先に落ちるテストを書く） | 通常経路で「取引No を再採番」ログが出ないことを assert するテストが RED → GREEN |
| T6 | D3 の欠陥立項（実行時ブロッカーは入れない）＋**既知欠陥テスト**（§12【2】Codex 勝ち） | 立項文書が存在・本 Plan §10 に残存リスク記載・趙への報告文に記載・検収 8 の `known_limitation` テストが通る |
| T7 | 全量テスト＋検収 1・7 の機械判定 | `unittest discover` 全緑、`git ls-files -u`・`rg` marker・`git diff --check`・`git diff main -- gas/` が全て空 |

---

## 6. 検収基準

1. **衝突の完全解消**（Codex P2 採納・`*.py` の `<<<`/`>>>` だけでは不足）。次の 3 つが全て空：
   - `git ls-files -u`（index に未解消エントリが残っていない）
   - `rg '^(<<<<<<<|=======|>>>>>>>)'`（`=======` と非 Python ファイルも対象に含める）
   - `git diff --check`
2. `venv311/bin/python -m unittest discover -p "test_*.py"` 全緑（マージ前の両側の全テストを含む）。
3. main 側の新規テスト `test_main_process_file.py` が**改変なしで**通る。
   **ただしこれは護欄が生きている証明にならない**（§11 S2・Codex P1）——同ファイルの
   backoff テスト 5 クラスは全て純関数級で、`main()` の接線を一切通らない。
   接線こそマージで失われる当の部分なので、**4 が必須**。
4. **`main()` 接線テスト（新規・本 merge の中核的検収）**。1 輪だけ実行できる loop helper を
   最小限抽出し、次を検証する：
   - `ready_files` のみが `_process_one_file` へ渡る（退避中ファイルが呼ばれない）
   - ファイル A が例外を投げても同バッチのファイル B は処理される
   - `INCREMENT` された file が次輪で `_partition_by_backoff` に濾される
5. **表駆動テスト**（Codex P1 採納。全量緑でも未検証のまま残る箇所）：
   - headless `FAILED` の 2 態（retryable True/False）が共に `CLEAR`
   - `ESCALATED` が `CLEAR`（memo TTL との二重抑制が起きない）
   - reporter（Firestore）例外が per-file try に隔離される
   - 6 系統の早期 return それぞれの retry action
   - **通常経路（tab 復旧なし）で「取引No を再採番」ログが出ない**（C6 の偽再採番検出）
6. 分支側の headless テスト群（`test_process_file_headless.py` 等）が**改変なしで**通る。
   ※ T5 で `_process_one_file` の戻り値を追加するため、同関数を直接呼ぶテストのみ
   assert 追加は許容。既存 assert の削除・緩和は不可。
7. **`git diff --exit-code fd306fc -- gas/` が空**（GAS 3 本が main と逐字同一＝merge が GAS を
   壊していない必要十分な証明。§12【1】の辯論で静的構文検査の代替として確定）。
   可動参照の `main` ではなく**固定コミット `fd306fc`** で照合する——後日 main が進んでも
   検収証拠が再現できるようにする（Codex 復審の改善提案を採納）。
8. **既知欠陥テスト（D3 の固定）**が存在し通る。§12【2】で Codex 勝ちにより採納。
   新しい動作を足さず、「本 merge が意図的に残した欠陥」の事実を固定する：
   - `commit_page`：失効 worksheet で 400 が回復されず外へ出る
   - `peek_append_range`：同上
   - `next_txn_no`：読取失敗後に 1 へ縮退する（＝再採番→fingerprint 変化の実行可能な証拠）
   - `probe_page`：既存 `test_escalate_when_tab_missing` が既に固定済み（追加不要）
   テスト名に `known_limitation` を含め、D3 立項先を docstring に明記し、
   **D3 修正時に反転または削除する契約**とする。先例＝`test_headless_excluded_page.py`
   の `KnownLimitationTest`。
9. D3 が文書として立項され、趙へ報告されている。

---

## 7. テスト戦略

- **回帰の主軸は既存テスト**。本 merge は新機能を足さない（T5 の戻り値導入を除く）ため、
  両側の既存テストが無改変で通ることが最強の証拠。
- **T5 のみ TDD**：戻り値と退避記録の対応は新しい契約なので、先に失敗するテストを書く
  （SKIPPED が退避記録に触れないこと、headless ESCALATE が FAILURE になること、
  DEAD_LETTER が SUCCESS 扱いになること）。
- E2E は本 merge では走らせない（実 Drive / 実 Sheets を要し、生産表を触るため）。
  真票回帰は趙の出社時に別途（既存の IP-403 未検証分と合流させる）。

---

## 8. 影響面

- `sheets_output.py`：UI 経路の 3 書込点の**内部構造**が変わる（行為は不変の想定）。
  headless 経路（`commit_page` 系）は本 merge で**一切変更しない**。
- `main.py`：`_process_one_file` の署名に戻り値が加わる。呼出元は `main()` の 1 箇所と
  テスト。headless 分流ロジックそのものは変更しない。
- `gas/*.gs`：自動合併済み。**GAS 側は別途 clasp/手動デプロイが要る**（本 merge では
  デプロイしない。IP-403 の未配置分と同じ扱い）。
- 生産（Windows ミニ PC）：本分支は生産分支ではない（生産は `main`）。
  本 merge は生産に**即時の影響を与えない**。

---

## 9. リスクと巻戻し

| リスク | 対処 |
|---|---|
| 合成ミスで UI 経路の記帳が壊れる | 検収 2・3 で担保。加えて `git diff main -- sheets_output.py` を目視し、main 由来の recovery ロジックが 3 点とも残っていることを確認 |
| headless 経路を無自覚に退行させる | 検収 4。headless テストの無改変通過を必須にする |
| D3 を忘れて headless を生産投入する | T6 で立項。本 Plan と欠陥文書の両方に残す |
| 途中で収拾がつかなくなる | `git merge --abort` で merge 前へ完全復帰（作業樹はクリーンだった） |

---

## 10. 残存リスク（意図的に残すもの）

- **D3**：headless 書込点（4 点）は tab 失効に対して無防備なまま。headless 生産投入前に
  必須の P1。検収 8 の `known_limitation` テストで事実を固定済み。
- **customer_id 欠落経路（main.py:677）の無界リトライ**：§12【3】で KEEP を維持した結果、
  奇形 job が 1 件でも常駐すると毎輪 Firestore `get_job` が走り続ける。
  加えて **3 秒ごとのバナー・欠落ログによる console churn**（無人機で console は唯一の
  観測面なので、これ自体が観測性の劣化）。ダウンロード/OCR/Gemini/Sheets には到達しない。
  複数の奇形 job が常駐する運用になれば優先度を引き上げる。
  正しい対処は backoff ではなく `customer_meta_alerted` を TTL 付き再照会 memo へ拡張する
  こと（抑制機構を memo 側へ寄せる既存設計と層位が揃う）。
- **GAS 未デプロイ**：`gas/*.gs` の競合封じはコード上のみ。実際の保護は手動デプロイ後。
- **真票回帰未実施**：本 merge の検証は単体テストのみ。

---

## 11. 自己評審で見つけた本 Plan の欠陥（Codex 提出後に自分で発見）

### S1 — §4 D4 の五態名と語義が誤り【P0・Plan 修正必須】
実物は `ProcessOutcome`（main.py:797）＝
`SUCCESS / FAILED / ESCALATED / PARTIAL / DEAD_LETTER`。Plan が書いた
`ESCALATE` は存在しない。より重大なのは語義の誤読で、**headless には既に
outcome ごとに精密設計された再試行抑制が存在する**（`_report_headless_outcome`）：

| outcome | 既存の抑制 |
|---|---|
| `SUCCESS` / `DEAD_LETTER` | 終態回報。memo 記録 |
| `PARTIAL` | memo 記録（F06-How TBD 接縫） |
| `ESCALATED` | memo ＋ TTL（`cycle + _ESCALATE_MEMO_TTL_CYCLES`＝20 輪） |
| `FAILED` + `retryable=True` | **意図的に memo 不記＝「3 秒自癒窓」**（B4 の設計） |
| `FAILED` + `retryable=False` | memo 記録（per epoch。控制面の重投でのみ再試行） |

ここへ backoff を一律に被せると、`FAILED`+retryable の**3 秒自癒窓が
3s→30s→5min→30min→1h へ変質する**——B4 の既定設計を静かに覆すことになる。
`ESCALATED` も memo TTL と backoff の二重抑制になる。

**修正案**：backoff 護欄の適用範囲を
**「UI 経路（`job_reporter is None`）の失敗」＋「両経路共通の例外」**に限定する。
headless の正常な五態は既存の memo 機構に委ね、backoff は触らない。
例外だけは headless でも必要——例外時は outcome が無く memo も記録されないため、
唯一の無限リトライ経路が残る。

### S2 — §6 検収基準 3 が護欄の接線を証明できない【P1・検収基準の追加が必須】
main 側の `test_main_process_file.py` の backoff テスト（`FailureBackoffScheduleTest`
以下 5 クラス）は**全て純関数級**——`_next_backoff_seconds` / `_record_file_failure` /
`_partition_by_backoff` / `_format_backoff_summary` / `_should_print_backoff_summary`
を直接叩くだけで、`main()` ループの接線を一切通らない。
これらの純関数はマージで touch されないので**必ず通る**。つまり
「無改変で通った」は護欄が生きている証拠に**ならない**。
接線こそがマージで失われる当の部分である。

**修正案**：T5 に「`main()` の接線テスト（新規）」を必須項目として追加する。
最低限、`_process_one_file` が FAILURE を返した時に `file_retry_state` へ
記録が入り、次輪で `_partition_by_backoff` に濾されることを検証する。

### S3 — 例外時の `_report_headless_outcome` の扱いが未定義【P2・要裁決】
main 側の try は `download_file` / `start_new_file` / `process_file` のみを包み、
`move_file` は try の外（`if success:` 分岐）にある。headless で対応するのは
`_report_headless_outcome`（Firestore 書込）。これを try の内外どちらに置くか
Plan が規定していない。外に置くと Firestore 障害時に例外が `main()` の外殻 try へ
逃げ、同バッチの残りファイルを巻き添えにする（護欄が防ごうとした当の事象）。

---

## 12. 附録：Codex 対抗評審の辯論記録

再議防止のため、採納・駁回の両方と理由を残す。

### 第 1 輪（初審）— 主要な採納

| 指摘 | 厳重度 | 裁決 | 内容 |
|---|---|---|---|
| C4 | P0 | **採納** | 「HEAD の呼出を採る」だけでは `for file in files:` を字義どおり採ることになり、直前で作った `ready_files` を無視＝**護欄が完全に無力化**。三者合成へ改めた |
| C6 | P1 | **採納** | `block.next_txn_no`（次番号）と closure の `transaction_no`（今回書く番号）の語義差。素朴合成だと**通常経路で毎回「再採番」を誤表示**し、しかも行値は偶然正しいので既存テストで検出できない |
| D3 | P1 | **採納** | 未保護は 3 点でなく 4 点（`next_txn_no` 追加）。3 類へ分割。さらに Plan が見落としていた根本原因＝**復旧時の取引No再採番で fingerprint そのものが変わる**（行番号だけの問題ではない） |
| D3 | P2 | **採納** | 「必ず ESCALATE」は過大表現。実際は復旧後 append と confirm の間で落ちた場合に限る条件付き障害 |
| D4 | P1 | **採納** | 早期 return は 4 系統でなく **6 系統**（未対応拡張子・customer_id 欠落を欠いていた） |
| D4 | P2 | **採納** | `SUCCESS/FAILURE/SKIPPED` は業務成否と再試行操作を混同する。`CLEAR/INCREMENT/KEEP` へ改名（実例：headless `DEAD_LETTER` は業務上失敗だが操作は CLEAR） |
| D4/S3 | P1 | **採納** | per-file try は `_process_one_file` の内側ではなく **`main()` 側で呼出全体を包む**。内側だと `_report_headless_outcome` の Firestore 例外が外殻へ逃げ、同バッチの後続ファイルを巻き添えにする |
| §6.3 | P1 | **採納** | main 側 backoff テストは純関数級のみ。接線を完全に落としても全緑になる。`main()` 接線テストを必須検収へ |
| §6.1 | P2 | **採納** | marker 検査に `git ls-files -u`・`=======`・`git diff --check` を追加 |

S1（五態名と語義の誤り）・S2（検収不足）は自己評審で先に発見しており、Codex も独立に同じ結論に達した。S3（reporter 例外の扱い）は Codex の簡潔案で解決した。

### 第 2 輪（複審）— 駁回した 3 点の帰趨

**【1】GAS の Apps Script 静的構文検査 → 我方維持（Codex 同意）**
駁回理由：`git diff main -- gas/` が実測 0 行。merge が GAS を壊していない証明には
逐字同一性で必要十分であり、構文検査は「main 自体が正しいか」という別命題。
3 本間の漂移も main 側の既存設計で本 merge が導入したものではない。新ツール導入は事前承認が要る。
Codex は自ら `0543de5..HEAD -- gas/` と blob hash まで検証した上で「維持で妥当」と認めた。
**ただし改善提案を採納**：可動参照の `main` ではなく固定コミット `fd306fc` で照合する
（後日 main が進んでも検収証拠が再現可能）。

**【2】D3 の投入ブロッカー → 分割裁決**
- *実行時ブロッカーを入れない* → **我方維持**（Codex 同意）。`HEADLESS_MODE` はオーナー管理の
  運用判断であり、merge 実装者がコードで禁止するのは運用権限の先取り。
- *既知欠陥テスト* → **Codex 勝ち・採納**。再提され、論証も成立した：新しい動作を足さず
  「今回意図的に残す欠陥」の事実を固定するだけで、merge の範囲外ではなく**残存リスクの
  検収範囲**。リポジトリに先例あり（`test_headless_excluded_page.py` の `KnownLimitationTest`）。
  実装も具体的に示された（`commit_page` / `peek_append_range` が `_with_tab_recovery` を
  通らないこと、`next_txn_no` が読取失敗時に 1 へ縮退すること）。→ 検収 8 へ。

**【3】customer_id 欠落経路（main.py:677）の retry action → 我方維持（Codex 同意）**
駁回理由：headless 専有経路で main 側に存在せず、backoff 付与は merge ではなく新規行為追加。
また headless の抑制機構は全て memo 側に寄せて設計されているので、この系統だけ backoff 側へ
寄せると抑制機構が二系統に分裂する。
Codex は「維持で妥当」とし、さらに**根拠を補強**した：この経路のコメント上の既存契約は
「控制面修復の次輪に自然回復」であり、INCREMENT すると修復検知が最大 1 時間遅れて
**その契約自体を変更してしまう**。実害も限定的（Firestore `get_job` のみ。`resolve_posting_id` は
純関数で Drive properties は `list_files` の返却に既に含まれるため、この分岐専用の
`files.get` は発生しない。ダウンロード/OCR/Gemini/Sheets には到達しない）。
**採納した補足 2 点**：①既知問題に console churn も明記（§10 済）
②接線テストでこの経路が `KEEP` を返し退避状態を増減させないことを固定（検収 4 へ）。

---

**Plan 定稿**（2026-08-11）。以降、実施中に Plan の誤りを見つけた場合は
筆誤なら直接修正して痕跡を残し、検収基準に触れる誤りなら該当条を再審する。

---

## 13. 実施記録

### 実施中に判明した Plan 外の事項

| # | 事項 | 対処 |
|---|---|---|
| I1 | **D6**（`transaction_no += 1` の消失）。Plan にも Codex 初審にも無かった。既存テスト `test_txn_number_advances_per_ticket` が `[7,7,7] != [7,7,8]` で落ちて発覚 | §4 D6 として追記。復元＋「消してはならない理由」をコード注釈へ |
| I2 | `TabNamerInjectionTest.test_append_entries_uses_injected_tab_namer` の `assert_called_once_with` が成立しなくなった。main 側 `append_entries` は冒頭で `_get_or_create_tab` を呼び、`_with_tab_recovery` 内の `_resolve_tab` でもう一度解決する＝**main 由来の固有挙動**（2 回目は `_ws_cache` 命中で追加 API 無し） | 回数ではなく**全呼出しの引数が注入名であること**を検証へ変更。名前の検証強度は不変で、復旧経路が別名で引く退行も捕まえられるぶん強化 |
| I3 | 検収 1 の `git ls-files -u` は `git add` 後でないと空にならない | 作業樹レベル（marker ゼロ・`git diff --check` 空・全量緑）は達成済み。index の unmerged 解消＝`git add` は**提出フローの一部**であり、趙の拍板を待つ |

### 実施後の自証（mutation 検証）

検収 4/5 のテストが**本当に退行を捕まえるか**を、意図的な改悪で確認した
（緑になるテストは、緑であること自体には意味が無い）：

| mutation | 期待 | 結果 |
|---|---|---|
| `for file in ready_files:` → `for file in files:`（C4 の見落としを再現） | 接線テストが落ちる | ✅ 2 件 FAIL |
| per-file try を外す（例外が走査を止める） | 隔離テストが落ちる | ✅ 2 件 FAIL |
| 復旧判定の比較対象を `+1`（＝`next_txn_no` 相当の誤合成） | 偽再採番テストが落ちる | ✅ 2 件 FAIL |

### 検収結果

| 検収 | 結果 |
|---|---|
| 1 marker ゼロ / `git diff --check` 空 | ✅（`git ls-files -u` は I3 のとおり `git add` 待ち） |
| 2 全量 `unittest discover` | ✅ **996 tests OK**（merge 前 977 → 新規 19） |
| 3 `test_main_process_file.py` 無改変で通過 | ✅ |
| 4 `main()` 接線テスト（新規 `test_main_loop_backoff_wiring.py`） | ✅ 14 tests・mutation 検証済み |
| 5 表駆動（五態 CLEAR・早期 return・例外隔離・偽再採番） | ✅ |
| 6 headless テスト群 無改変で通過 | ✅（`_call_process_one_file` の戻り値透過のみ追加。assert の削除・緩和なし） |
| 7 `git diff --exit-code fd306fc -- gas/` | ✅ 空 |
| 8 既知欠陥テスト（D3 固定） | ✅ `HeadlessWritePathKnownLimitationTest` 4 件 |
| 9 D3 立項 | ✅ `docs/2026-08-11-defect-headless-tab-invalidation.md` |

---

## 14. 実施後評審（/simcodex）の記録

### Round 1 — simplify 4 観点（並行）＋ codex review

**採納して修正した**（複数観点が独立に指摘＝去重後の強信号を優先）：

| 指摘 | 観点 | 対処 |
|---|---|---|
| `append_entries` と `_write_unrecognized_row` の `_read_ensure_and_write` closure が行単位で同一（`rows` vs `[row]` の差のみ） | **Reuse P1 ＋ Simplification P1**（2 観点独立） | `_append_rows_with_recovery(tab_name, rows, built_txn_no)` へ抽出。**注釈が自ら心配していた「片方だけ直すと必ず漂移する」が、規律から構造保証へ変わった**——実際 main 側の codex review も `append_entries` だけを指摘し、占位行経路は後から手で揃えられていた |
| `ws = self._get_or_create_tab(tab_name)`（:664）が死んだ局所変数。取号にしか使われず 70 行後に上書きされる。tab 解決の入口が 2 系統に割れる | **Reuse P2 ＋ Simplification P2 ＋ Efficiency P2 ＋ Altitude P1**（4 観点） | `self._resolve_tab(tab_name)` へ統一し削除 |
| `RenumberLogOnNormalPathTest` が module 直下の `_run_append_entries` を手写きで複製 | Reuse P1 | 既存ハーネスを再利用（戻り値 `[2]` が stdout） |

**Efficiency が検証して「問題なし」と確認した点**（合成の健全性の証拠）：
- API 呼出数は merge 前から**増えていない**。`_get_or_create_tab` / `_get_next_txn_no` の
  2 回目はいずれも `_ws_cache` / `_tab_next_txn` のヒットで、`get_all_values` は 1 回のまま。
- closure の `rows` 捕捉は**リークではない**。`_with_tab_recovery` は fn を保持しないので
  フレームと同時に死ぬ（merge 前の `rows` と同一寿命）。
- per-file `try` は Python 3.11 の zero-cost exceptions によりコストゼロ。

**Altitude が「層位は正しい」と裁定した点**：
- `RetryAction` の「決める／適用する」分離は適切。「backoff がイベントを購読する」案は
  **より浅い**（結合が暗黙化し、10 個の出口で担当機構を追えなくなる）。
- memo と backoff の二系統併存は**正当**。識別子が違う（`(base, lease_epoch, file_id)` ＝
  控制面の job 同一性 vs `file_id` ＝ Drive の物体同一性）ので統合すると epoch 更新での
  自然回復が壊れる。
- per-file try を `main()` 側へ置いた境界は正しい。

### Round 1 で defer した指摘（本 merge の範囲外・独立立項が要るもの）

いずれも**既存コード由来**で、merge が導入したものではない。混ぜると merge の審査可能性が落ちる。

| # | 指摘 | 出所 | なぜ defer か |
|---|---|---|---|
| L1 | **`_get_next_txn_no` の bare `except: return 1`**（:298）が一過性の読取失敗（429/5xx）を「取引No=1」へ黙変換し**キャッシュする**。健全な tab に重複取引No が書かれ得る＝**静かな会計データ汚染**。復旧していないのに「再採番」ログも出る | Altitude P1 → **Round 2 で codex も P1 と裁定** | 正確性に関わる真の欠陥だが main 側の既存挙動で、正しい修復には例外分類と採番契約の TDD が要る。**headless 本番投入前に D3 と併せて解消する**（codex R2 の指定）。例外を伝播する厳格採番 API と、真に空の tab だけを `1` とする処理を分離する |
| L2 | **同名 tab 再作成を recovery が検知できない**。gspread は title で解決するので、削除後に同名 tab が作られていると `get_all_values`/`append_rows` が成功してしまい、`old_ws.id` の検証に到達しない。`_tab_next_txn` が旧値のまま残る | **codex review P2** | main 側修復の固有限界。発火には「GAS 削除後・Python 書込前に外部が同名 tab を作る」窄い窓が要る。修すには毎書込前の sheetId 検証（API コスト）が要る |
| L3 | 高亮が 4+N 回の逐次 `format_cell_range`（頁あたり ~5-25 リクエスト）。多頁 PDF が自ら 429 を招く | Efficiency P1 | merge 前から存在。`batch_update` 化は独立の性能作業 |
| L4 | `len(ws.get_all_values())` が行数を数えるためだけに全表をダウンロード | Efficiency P1 | 同上。`_audit_row_count` 式のメモリ計数は崩潰重跑時の正確性と引き換えになるので設計判断が要る |
| L5 | ~~`file_retry_state` に剪定が無く「⏳ 失敗退避中 N件」を水増しする~~ → **Round 2 で欠陥不成立と判明** | Efficiency P2 ＋ Altitude P2（症状の記述が誤り） | **訂正**：`backed_off_total` は毎輪の `files` と交差した `backed_off_files` から数えている（`main.py:2186`）ので、手動削除・移動された file は母数に入らず**水増ししない**。残るのは「02:00 の再起動まで dict に小さな不要エントリが残る」だけ。共通剪定は保守改善として別途でよく、欠陥として追う必要は無い |
| L6 | memo は輪数（TTL 20 輪）、backoff は壁時計秒——**非通約な単位**。1 輪は空回りなら 3 秒だが処理中は分〜時間なので、抑制窓が無関係な檔の OCR 負荷で伸縮する | Altitude P2 | 分裂自体は正当。統一すべきは単位であって機構ではない |
| L7 | `KEEP` が「他機構が担当中」と「担当者不在だが意図的に放置」を同一値へ潰している | Altitude P2 | `KEEP(owner=...)` 化は語彙の拡張。§10 の宿題を型側へ移す価値はあるが merge の範囲外 |
| L8 | `main()` の一括 `except` が「副作用前の例外」と「finally 内で終態回報**後**に出た例外」を区別できない | Altitude P2 | 後者は memo が恒久 KEEP を返すので退避項が居座る（L5 と合成して顕在化）。エッジケース |

### Altitude による D3 立項の格上げ（採納）

> headless の 4 点が無防備なのは「wrapper を 4 つ忘れた」からではなく、
> **未保護の ws を誰でも取得できる API 面が存在する**という構造が原因。
> 立項の見出しをそこへ引き上げないと、次に headless 側へ書込点が増えた時に
> 同じ穴が再生産される。

`docs/2026-08-11-defect-headless-tab-invalidation.md` の見出しをこの粒度へ改める
（完了条件が「4 点を塞ぐ」から「未保護 ws を表現不能にする」へ変わる）。

### Round 2 — codex による リファクタ検証：**no findings**

Round 1 で入れた `_append_rows_with_recovery` の統合について、挙動同値性
（`[row]` の単行代入等値・`count + len(rows)` ≡ `count + 1`・戻り値 3-tuple の意味一致）、
`_resolve_tab` の監査タブ誤分岐の不在、初回採番と復旧内再解決の競合窓が Round 1 前と
同幅であること——いずれも確認され、**新規欠陥ゼロ**。

**Round 2 が訂正した私の記録 2 件**（重要）：

1. **L5 は欠陥不成立**（上表で訂正済み）。私が「水増しする」と書いたのは誤り。
2. **`_append_rows_with_recovery` は D3 の受け皿に「そのままは」ならない**。
   Round 1 で Reuse 観点の「D3 で commit_page を治す時の受け皿にもなる」を
   検証せずに採り入れていたが、codex R2 が却下した：
   - `PageWrite.rows` は複数票・複数取引No を含み得るのに、helper は再採番時に
     **全行の `row[0]` を同一番号へ置換**してしまう
   - PENDING 記録**後**に helper が rows を書き換えると、既に保存した fingerprint と
     不一致になる
   - 戻り値が単一の `actual_txn_no` なので、更新後の `txn_range` や witness を表現できない

   → D3 で再利用できるのは helper 全体ではなく、さらに下位の
   「実測 → 容量確保 → append」部分だけ。D3 本体は
   「解決 → 厳格採番 → PageWrite 再構築 → 予測範囲・fingerprint 再 claim」を
   一単位にする別設計が要る。

**Round 2 の追加提案（D3 立項へ反映済み）**：D3 で `_resolve_tab` を headless へ
展開する際、`tab_owner` に監査タブの予約名を許さない入力検証を足すこと。

### Early exit 判定

- Round 1 の P0/P1 → 全て修正済み
- Round 2 の codex → **no findings**
- verify → 全量 996 tests OK

3 条件を満たしたため **Round 3 は実施せず early exit**（simcodex の early-exit 規定）。
