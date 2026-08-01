# B7 第 2 步 — headless 合流＋§5.6 page_outcomes 接線 実行計画（Codex 初審反映版）

> 工単：サンデヴィスタン `docs/impl/03-batch-plan.md` B7 施工順序 2・3。
> 契約錨：`contracts/job-state-machine.md` v0.15 §5.6（頁処理結果台帳）。
> 分支：全改動のみ `feature/sandevistan-headless`。main 一行不動（第 1 步で完了済み）。
> 授権：checkpoint commit は本分支に可（07-16 趙授権）；**禁 push**；新依存なし（既存 google-cloud-firestore を使用）。

## 1. 目標

1. **T0 合流**：`git merge main` を headless 分支で実施（増分＝`d5b1576`＋`0543de5` の 2 commit のみ——`2eb213b` で `3f43acf` まで合流済みを実証済み）。衝突 3 檔（main.py / sheets_output.py / test_sheets_output.py、merge-tree dry-run 実証）を解消し、**両系統の既存テスト全緑**。
2. **T2 §5.6 reporter**：`firestore_progress.py`（新設）に `FirestorePageOutcomesReporter` を TDD で実装——headless の**頁決算点**ごとに `jobs/{job_key}/page_outcomes/{page_id}` へ 1 行書込。
3. **T3 接線**：headless の頁決算点（`_classify_and_flush_page` 直後）に `emit_page_outcome` adapter を置き、kind→§5.6 outcome 映射を一元化して発射。`HEADLESS_MODE=1` でのみ有効化、UI 版挙動零改動。

## 2. 非目標（本 session でやらない）

- **§5.7 provider 事件集合**：前提＝例外帰因の是正（ocr_engine の広域捕捉が Gemini 例外を PaddleOCR 失敗と出力し得る）——識別段の例外構造に踏み込む別サイズの仕事。→ 別批。
- **tab 名変更（§5.1-d `顧客番号_顧客名`）**：Sheets 出力面の顧客可視仕様変更で、進捗心跳と無関係。→ 別批。
- **控制面 reconciler 並集判定の簡化**：工単明記「本批外」。
- **F06-How（`POSTED_PARTIAL` 命名）**：TBD 維持、代拍しない。
- **headless での Sheets 進捗タブ有効化**：工単 B7-3 の要求は §5.6 のみ。adapter は多 reporter 対応にするが、headless 既定では Firestore outcomes のみ接ぐ（headless の可視化権威＝控制面。Sheets タブを接ぐか否かは趙の別途判断）。
- §11.4 の P0-10／P1-9 は T1 で現地確認のみ——既に IP-402 で覆われていれば記録して閉じ、**未覆いなら一行級でも本計画の変更対象に含めず趙へ報告**（範囲钉死）。
- 控制面リポジトリへの一切の変更・跨倉統合テスト（本 session の作業対象は SS 倉のみ）。

## 3. 任務清単（順序＝依存順、各項 DoD 付き）

### T0 合流と回帰
- **前置**：merge 直前の headless HEAD（現在 `09d2f26`）で golden replay を一度実行し基線を採取（T4 の比較起点。fixture・実行命令・正規化手順を証拠包に記録）。
- `git merge main`（headless 上）。衝突解消方針：
  - `main.py`：headless 側の大改造（intake_guard／posting_ledger 経路）を保持しつつ、main 側 B7-1 の `progress` 引数貫通・`page_done`/`file_finished` 発射点・`build_progress_reporters` を取り込む。**generator 逐頁流式モデル不変**。UI 経路の progress 発射は main 実装そのまま；headless 経路は T3 の adapter が担い、main の `page_done` 発射点を headless 決算点として**流用しない**（Codex 阻斷1）。
  - `sheets_output.py`：main 側は `append_entries` 戻り値化＋進捗タブ関連 11 行のみ——headless 側の頁級原子写と共存させる。
  - `test_sheets_output.py`：両側のテスト集合の和。命名衝突あれば headless 命名を優先し main 側を改名。
- **merge smoke**（Codex 低1）：UI `process_file` 一条／headless `_process_one_file` 一条／`HEADLESS_MODE` 開閉での import・初期化各一条。
- **DoD**：`venv311/bin/python -m unittest discover -p "test_*.py"` 全緑（両分支のテスト全部が母集団）＋merge smoke 緑。merge commit ＝ checkpoint。

### T1 §11.4 残項の現地確認（読み取りのみ）
- P0-10（全頁除外→SUCCESS 側）と P1-9（テスト 3 箇所の断言再設計）が IP-402（`09d2f26`）で既に消化済みかを `_classify_excluded_results`／`ProcessOutcome` 分岐とテストで確認。
- **DoD**：確認結果（済み／未済＋残量見積）を本檔 §8 に追記。コード変更はしない。

### T2 `FirestorePageOutcomesReporter`（新檔 `firestore_progress.py`、TDD）
- **kind→outcome 映射表（本計画の心臓部。table-driven テスト必須、Codex 阻斷1/阻斷2）**。
  権威定義＝**「該頁の最終帳務身分」であって本輪 OCR 初判ではない**：

  | headless 頁決算 kind | §5.6 outcome | reason（closed キー） |
  |---|---|---|
  | POSTED_NOW | `POSTED` | `posted_now` |
  | POSTED_PRIOR（ledger 既 CONFIRMED・票>0） | `POSTED` | `prior_confirmed`；本輪初判が除外だった drift は `classification_drift` |
  | PLACEHOLDER_WRITTEN | `PLACEHOLDER` | `placeholder_written` |
  | PLACEHOLDER_PRIOR（ledger 既 CONFIRMED・票=0） | `PLACEHOLDER` | `prior_confirmed`／drift 時 `classification_drift` |
  | CONTENT 頁（占位行書込済） | `PLACEHOLDER` | `content_unreadable` |
  | EXCLUDED（新規除外・監査タブ留痕済） | `EXCLUDED` | `excluded_envelope`／`excluded_social_insurance`（producer の `_exclude_reason` から**固定キーへ翻訳**、自由文字直通禁止） |
  | RETRYABLE／UNKNOWN エラー頁 | `FAILED` | `page_error_retryable`／`page_error_unknown` |
  | ESCALATE（witness 曖昧・混型頁等） | **書かない** | ——欠落＝「未決」の正しい信号。件級は POST_UNKNOWN／ファイル保持側に倒れ人工核へ；FAILED と書くと「処理済み・記帳失敗」と誤読される（裁決＝§9 #13） |
  | 映射表に無い未知 kind／未知 reason | 当該 kind の outcome が決められない場合は書かない＋警告日誌；outcome 確定で reason のみ未知なら `unclassified` に**降級**して書く（行の存在自体が完了判定材料のため拒写しない、§9 #5） |

- 書込仕様：
  - doc パス：`jobs/{job_key}/page_outcomes/{page_id}`、`page_id`＝`posting_ledger.derive_page_id` と同一の `{base}:p{n}` 再利用（別採番禁止）。
  - 字段：`page`（int）／`outcome`／`reason`（上表 closed キーのみ）／`written_at`（UTC、F49。語義＝**最後観察時刻**：同頁再書込で更新される、初回時刻ではない——§9 #7）。
  - 冪等：doc id 決定的 `set()` 上書。「同一頁 N 回発射→doc 恒に 1 件・outcome は最終決算値・written_at は最新」をテスト（byte 級冪等は主張しない）。
  - 失敗時 degrade（§9 #3/#9 裁決＝有限重試＋檔終局補写）：頁時の書込例外は捕捉し**頁処理を落とさない**→失敗頁を檔内バッファへ→`file_finished`（＝檔級 report_posted の**前**）に未書込頁を一括補写一輪→なお失敗は警告日誌して放行（記帳・檔級回報は阻まない）。停用範囲は**当該檔内のみ**（プロセス全域で停用しない）。Firestore client は timeout 明示。安全根拠＝過渡期の完了判定は「page_outcomes ∪ postings CONFIRMED」並集（drift-repair §11.2-1）のため POSTED 頁は postings 側で兜底；EXCLUDED 頁の行欠落は「未完了」誤判＝現状 P0-11 と同等で、悪化はしない。
- 単測は `fake_firestore.py` を使用、真庫接続なし。**fake に `_DocRef.set()`・書込回数計上・故障注入を追補**（影響面に計上、Codex 中2）。
- **DoD**（先に RED 確認）：①映射表全行の table-driven テスト（ESCALATE＝不書込含む）／②同一頁 2 回発射→doc 1 件・written_at 更新／③頁時書込例外→伝播せず・檔終局補写で回収・補写も失敗なら放行＋警告／④written_at UTC aware／⑤未知 reason→`unclassified` 降級・自由文字（檔名/客户名混入）不透過／⑥新規檔 branch coverage 80%+。

### T3 headless 接線（emit_page_outcome adapter、Codex 更簡方案採用）
- **接線点の明記**（Codex 高1）：
  - `_process_one_file()` が `base`（posting_id）と job_key を確定した後、`FirestorePageOutcomesReporter(client, job_key, base)` を構築。client は既存 `firestore_report` の client 取得経路を再利用（既存名 `reporter` は **`job_reporter`**（檔級状態回報）へ改名し、頁級 outcomes と混同させない）。
  - headless 頁処理側へ `page_outcomes` 引数を貫通（対象関数と新簽名は実装時に本檔 §8 へ列記——`process_file` headless 経路に `progress` 引数は現存しないため新設）。
  - 発射点＝`_classify_and_flush_page` の決算確定直後**恰好一回**。adapter `emit_page_outcome(kind, reason, page_num)` が映射→全登録 reporter へ fan-out。fan-out は**子 reporter の例外を吞まない**（各 reporter 自身が best-effort 責務を持つ。二重吞みで実装バグを遮蔽しない、Codex 中4）。
- `HEADLESS_MODE=0`（UI 版）：構築経路に一切入らない——UI 版挙動零改動を既存 `test_main_process_file.py` の無改動通過で証明。
- **DoD**（Codex 高3 反映）：fake firestore 統合テストで
  ①3 頁件→page_outcomes 3 行・各頁 outcome 一致／②除外頁→`EXCLUDED`＋固定キー reason／③エラー頁→`FAILED`／④重跑（POSTED_PRIOR/PLACEHOLDER_PRIOR）→outcome は帳務身分・行は物理頁ごと恒に最多 1 件／⑤分類 drift（前輪 posted・今輪除外初判）→`POSTED`＋`classification_drift`／⑥ESCALATE 頁→行なし＋件級 ESCALATE 挙動不変／⑦非連続頁号・空 generator→誤発射なし／⑧UI 経路テスト無改動緑。

### T4 黄金様本回帰（§0.4 閘門 3）
- 基線＝T0 前置で採取した merge 前 replay 産物。比較範囲＝**Sheets 産物のみ**（page_outcomes は golden の対象外——fake Firestore の snapshot 検証は T2/T3 単測が担う、Codex 中5）。fixture・命令・正規化を基線採取時と同一に固定。
- **DoD**：Sheets 産物 diff 零（または差分全件に説明が付き趙報告に載る）。

## 4. 検収基準（脚本化判定優先）
1. `venv311/bin/python -m unittest discover -p "test_*.py"` 全緑（T0 後・T3 後の 2 回）。
2. 新規檔（firestore_progress.py）branch coverage 80%+（計測値を証拠包に）。
3. golden replay：Sheets 産物 diff 零回帰（基線＝merge 前 HEAD）。
4. `/simcodex`（既定 3 輪）全緑＋辯論裁決記録。
5. Sheets 28 列不変・posting_id 不入 Sheets（T2/T3 は Sheets 書込に一切触れない設計。既存 28 列断言テストが無改動緑であることを回帰の歯とする）。

## 5. テスト戦略
- TDD：各 DoD 項目を先に会失敗テストで書き RED 確認→実装 GREEN。unittest 風、venv311。
- 単元＝T2（fake_firestore・table-driven）／統合＝T3（`test_headless_loop_wiring.py` 既存様式に追記）／E2E 相当＝T4 golden replay＋T0 merge smoke。
- Firestore は fake のみ、真庫接続なし（工単 §0.2）。

## 6. 影響面
- 触る檔：`main.py`（merge＋headless 接線）／`sheets_output.py`・`test_sheets_output.py`（merge のみ）／新規 `firestore_progress.py`＋`test_firestore_progress.py`／`test_headless_loop_wiring.py`（追記）／`fake_firestore.py`（`set()`・故障注入の追補）＋`test_headless_rerun_fixture.py` 等 fake 利用側の無回帰確認。
- 触らない：`page_progress.py`（main 底座、合流後も無改造）／`PageUrlResolver`・`SPLIT_PDF_FOLDER_ID` 経路（全局禁手）／`posting_ledger.py`（derive_page_id を import 利用のみ）／`firestore_report.py`（檔級回報現行のまま。呼出側の変数改名のみ）。

## 7. リスクと後退
- **R1 merge 衝突の解消誤り**：解消後に両系統テスト全緑＋merge smoke＋golden replay を歯にする。後退＝merge commit 前は `git merge --abort` のみ；commit 後は**明示 revert commit**（`reset --hard` は使わない——未追跡 golden/ 資産と他ローカル変更の誤傷防止、Codex 低2）。
- **R2 二重採番**：derive_page_id 再利用を設計で钉死（§3 T2）。
- **R3 page_outcomes 書込失敗の増幅**：有限重試＋檔終局補写＋当該檔限定停用（§3 T2）。残余（補写も失敗）は並集判定過渡で POSTED 頁兜底、EXCLUDED 頁は現状同等の未完了誤判に留まる——恒久解は控制面 reconciler 補完（工単 B7-3 控制面側・本批外）。
- **R4 UI 版挙動汚染**：HEADLESS_MODE 分岐外に新構築を置かない＋既存 UI テスト無改動緑。
- **R5 進捗タブと page_outcomes の意味錯綜**：Sheets 進捗タブ＝人向け可視化（main 由来・headless 既定不接続）、page_outcomes＝控制面の完了判定権威。役割を本檔とコード docstring に明記。
- **R6 映射誤り＝完了判定汚染**：kind→outcome 表を単一真相源（モジュール定数）にし、table-driven テストで全行釘死；表に無い kind は書かず警告（静默 POSTED 化の禁止）。

## 8. T1 現地確認結果（2026-08-01 実施）
- **P0-10（全頁除外→SUCCESS 側）＝同 session 追加実施で消化済み（2026-08-01・commit `a886634`）**——趙が語義を口頭再拍板（「一条も記帳すべきでないファイルは直接算過」）したため範囲に追加。`_aggregate_file_outcome` 一行変更＋断言 8 箇所追随、全 761 テスト緑、codex 追認。以下は当初の未済実証（経緯記録として保持）：実証：`main.py` `_derive_headless_outcome` 系で全頁除外→`ProcessOutcome.PARTIAL`（merge 後 1066-1067 行）、PARTIAL は意図的沈黙・未回報（同 485-488 行、F06-How TBD 接縫）。契約 v0.15 §5.1-b 趙裁定 2 は「全頁除外を SUCCESS 側へ（一行判定変更）」——**本批範囲外につきコード不変更、趙へ報告**。改修時は同時に report_posted 側の挙動（SUCCESS 回報になる）と関連テスト（test_headless_excluded_page の全頁除外系）を追随させる必要あり＝実質「一行＋テスト数箇所」。
- **P1-9（断言の歯 3 箇所再設計）＝実質消化済み**。`test_process_file_headless.py:224/606`・`test_headless_excluded_page.py:177 ほか`に `placeholder_calls==[]`／`append_calls` 零系の断言が既在（IP-402 で整備）。P0-10 実施時に全頁除外系の期待値変更として一部書き換えが発生する点のみ残る。

## 9. Codex 対抗評審と辯論裁決記録（初審 2026-08-01・13 条）

| # | 嚴重度 | 指摘要旨 | 裁決 | 理由 |
|---|---|---|---|---|
| 1 | 阻斷 | headless 頁終局は `_classify_and_flush_page` 決算点であり main の `page_done` 流用は漏報/誤報 | **採納** | 実碼準拠の指摘。kind→outcome 映射表＋決算点発射に全面改稿（§3 T2/T3） |
| 2 | 阻斷 | outcome 権威＝「頁の最終帳務身分」；PRIOR 系を本輪初判で上書きすると控制面と ledger が矛盾 | **採納** | 映射表に PRIOR→帳務事実・drift は reason で記録と明記 |
| 3 | 阻斷 | 「失敗→永久沈黙、reconciler が兜底」は未検証；終態 job の缺頁が永久化し得る | **修改採納** | 有限重試（頁時 1 回＋檔終局補写一輪）を採用し、report_posted 前に補写。**跨倉統合契約テストは駁回**——本 session の作業対象は SS 倉のみ（工単 §0）で控制面テストは書けない；過渡期並集判定（drift-repair §11.2-1）で POSTED 頁は postings 兜底、EXCLUDED 頁の残余リスクは現状 P0-11 と同等＝悪化しない、恒久解は控制面側残項 |
| 4 | 高 | reporter 混同・接線簽名不明 | **採納** | `job_reporter`／`page_outcomes` の命名分離と構築点・貫通経路を §3 T3 に明記 |
| 5 | 高 | reason closed 白名単がない | **修改採納** | 白名単採用。ただし未知 reason は**拒写でなく `unclassified` 降級**——§5.6 の完了判定は「行が揃うこと」であり、reason 検証で行を落とすと完了判定を人質に取る |
| 6 | 高 | T3 統合テストが危険経路（PRIOR/drift/ESCALATE/非連続頁/空 generator）を欠く | **採納** | T3 DoD ①〜⑧に全数計上 |
| 7 | 中 | `set()` 上書は byte 級冪等でない・written_at 語義未定 | **採納** | written_at＝最後観察時刻と定義、更新をテスト対象に |
| 8 | 中 | fake_firestore に `set()` が無く TDD 初手が転ぶ | **採納** | fake 追補を影響面に計上 |
| 9 | 中 | 一度の例外で永久停用は暫態故障を増幅 | **修改採納** | 停用は当該檔内限定＋檔終局補写。**退避階梯は駁回**——best-effort 可視化通道に多段 backoff は過度設計；client timeout 明示で長停滞は抑止 |
| 10 | 中 | Composite の二重例外吞みがバグ遮蔽 | **採納** | Codex 更簡方案（emit_page_outcome adapter＋不吞 fan-out）へ置換 |
| 11 | 中 | golden diff 零の基線・範囲が非再現 | **採納** | 基線＝merge 前 HEAD で事前採取・範囲＝Sheets 産物のみと明記 |
| 12 | 低 | merge 正しさの歯が不足 | **採納** | merge smoke 3 条を T0 に追加 |
| 13 | 低 | `reset --hard ORIG_HEAD` は危険 | **採納** | 撤去。abort（commit 前）／revert commit（後）のみ |
| 14 | — | ESCALATE の扱い（Codex は「FAILED か不写かを明定せよ」） | **裁決＝不写** | 欠落＝未決の正しい信号。FAILED と書くと「処理済・記帳失敗」と誤読され、reconciler が「行が揃った」側へ倒れて人工核信号（POST_UNKNOWN）と矛盾し得る |

**複審（駁回・修改条目の回餵）**：#3 の跨倉テスト駁回／#5 の降級方式／#9 の退避駁回／#14 の不写裁決——結果は下に追記。

### 複審結果（2026-08-01 追記）
4 項全て **Codex 接受**（原意見を再提出せず＝我方裁決維持で決着）：
- #3 跨倉テスト駁回：接受（SS 倉範囲で不能；有限重試＋並集兜底で永久沈黙は回避済み）
- #5 `unclassified` 降級：接受（outcome 確定時の行完整性維持。未知 kind＝不写の境界も合理）
- #9 退避階梯駁回：接受（best-effort 通道に多段 backoff は複雑度不相称）
- #14 ESCALATE 不写：接受（未決を FAILED と誤表現しない。POST_UNKNOWN 人工核と整合）

**→ 本計画定稿（2026-08-01）。以後の変更は実施中の Plan 修正手続（fatboyslim Phase 2）による。**

## 10. 実施後評審記録（simcodex・2026-08-01）

- **Round 1 で early-exit**（規約どおり：panel 0 P0/P1・codex 0 P0/P1・verify 全緑）。
- simplify 4 視角 panel（Reuse/Simplification/Efficiency/Altitude 並行）：**P0/P1 ゼロ**、P2 計 18 件。
- codex review（`--base origin/feature/sandevistan-headless`＝本批全量）：「No actionable correctness issues」。修復後の `--uncommitted` 追認：「No actionable regressions」。
- **即修した 3 件**：①Altitude#4＝`job_reporter` 改名（Plan §9 #4 既裁事項の履行——コメント代替は bandaid だった）②Altitude#1＝`flush_pending` を try/finally 化（途中例外でも決算済み頁の行を落とさない）③Altitude#5＝未知 kind 警告の檔内去重。
- **P2 遺留清単（次批以降・拘束なし）**：
  1. `social_insurance_notice` リテラルが `ocr_engine.SOCIAL_INSURANCE_REASON` と二重定義（依存を増やさず共有するには定数の dep-free モジュール移設が要る）
  2. `_handle_excluded_page` の `reason` が監査タブ表示文字列と機械 detail キーの二役（表示形式を変えると白名単翻訳が unclassified へ縮退）
  3. kind/detail 語彙が main.py 産出側・映射表・網羅テストの 3 箇所に文字列で分散（定数昇格＋テスト導出で漂移検出可能に）
  4. UI 経路 `page_done` の reason が未消費の混在語彙（消費者が現れる前に閉語彙化 or 撤去）
  5. `_emit` 閉包の毎頁再生成／`basename` 二重計算／fake の `_make(fs)` 死引数／テスト内 import 反復等の微小整理
  6. `test_drive_functions.BuildProgressReportersTest` docstring の「None を割り当て」記述と実装（キー省略）の乖離
