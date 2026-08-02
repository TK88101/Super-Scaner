# B8 実装記録 — provider 事件集合（契約 v0.17 §5.7）

> 対象＝`feature/sandevistan-headless`。**`main`（現役 UI 版）には一行も触れていない。**
> Plan 本体は控制面倉 `docs/plans/2026-08-02-next-super-scaner.md`（権威）。本書は
> **SS 倉側の実装記録**——Codex R1-P2 裁決により、差分表と辯論記録は SS 倉に残し、
> 控制面文書（工単・IP 進度・契約条文）への回写は**次の控制面 session が四步取証の
> うえ行う**（控制面 CLAUDE.md §6 の回流協議）。

## 1. 開工前の閘門三件（趙裁定 2026-08-02）

Codex R1 が P1 として「実装者が運用政策を臨場で決めることになる」と指摘した三件。
着手前に趙が拍板済み。

| # | 事項 | 裁定 |
|---|---|---|
| D3 | `alerts/{file_id}` 異因二度報警＝覆蓋 or 追加 | **単一文檔のまま `reason_stats` で原因別累計**（§2 の辯論記録） |
| 頻度（D6） | provider 事件の書込上限 | **頁級 1 件**＋**`provider`×`error_class` ごと滑動 10 分窓で 20 件封頂**（2026-08-03 改訂。初版は「job ごと・檔ごとに再配分」だった＝§4-ter） |
| 往返 | 真 Firestore 往返検証の DoD | **schema 契約テストで閉じる**。真往返は遺留清単（§6） |

## 2. D3 辯論記録（Codex 二輪・fatboyslim Phase 1）

輪回防止のため駁回理由まで残す。

| 輪 | 我方 | Codex | 判定 |
|---|---|---|---|
| R1 | 「異因追加・同因冪等」＝父文檔覆蓋＋子集合 `events/{reason}` | ①`reason` を文檔 ID にするなら受控枚挙必須 ②**書放大**：`at` の毎輪更新も課金書込。文檔数しか解決していない ③より簡＝単一文檔 `active_reasons` | ②が致命に見えた |
| — | **取証**：`intake_guard.py:167-247`＝REJECTED → `write_alert` **一度** → 隔離夾へ move → **監視夾から出るので再走査されない**。`alerted` キャッシュにより move 失敗時も `write_alert` は再実行しない | — | **双方の前提が崩れた** |
| R2 | 事実を回餵して複審要求 | **書放大論点を明示撤回**。ただし「控制面の読取面は**未建設**（契約原文『控制面後續建讀取面』）→ 単一文檔・一度読みは決定的優位」を維持 | **Codex 勝**（この点） |

**勝負配分**
- Codex 勝＝**子集合を作らない**。読取面が未建設なのに掃描/ソート/集約の負担を先回りで負わせるのは跨倉の悪手。
- 我方勝・Codex 採納＝①異因は絶対に覆い消さない（元議題の真問題）②reason は受控枚挙であって生エラーテキストではない ③原因ごとに回数と初出/最終時刻を持つ。
- **双方共同で駁回**＝Plan 原推奨の「純追加（auto_id）」。F20「文檔 ID 天然冪等」＝刷屏防止の護欄を壊すうえ、低頻度（一ファイル一生 1〜3 回）では不要。
- 記法は `provider_events` と**各行其是**（一方は append-only 事件流、他方は各ファイルの現況）。ただし各々「いつ書くか／去重鍵／覆蓋可否／保持期間」を契約に明記すること（Codex 提案・採納。控制面 session の作業）。

**実装形**（`firestore_report.write_alert`）

```
alerts/{file_id}
  kind, file_id, reason, posting_id …   ← payload そのもの＝当前快照（従来どおり）
  at, by                                 ← 従来どおり
  reason_stats: {                        ← 新設
    "no_posting_id"      : {occurrences, first_seen_at, last_seen_at},
    "posting_id_mismatch": {occurrences, first_seen_at, last_seen_at}}
```

reason code＝`payload["reason"]` →（無ければ）`payload["kind"]` → 両方無ければ
`reason_stats` を置かない。値域は受控枚挙のみ：`no_posting_id` / `job_not_found` /
`posting_id_mismatch` / `customer_metadata_missing`。生エラーテキストは入らない
（高基数の `firestore_error:{Type}` は DEFERRED であって alert を書かない経路）。
既存分の読取が落ちても alert 自体は必ず出す（履歴 < 可視性）。

## 3. T1 差分表（§5.7 要求 × SS 現状）

| §5.7 要求 | 着手前の現状 | 本 session の対応 |
|---|---|---|
| `provider_events/{event_id}`（トップレベル集合） | 無（`jobs`/`alerts`/`postings`/`page_outcomes` のみ） | `provider_events.py` 新設 |
| `provider` 字段 | provider の次元自体が無い | 値域 `{gemini_classify, gemini_ocr, paddleocr}` を凍結 |
| `error_class`（§5.4 三分類） | 頁級 `_classify_page_error`（RETRYABLE/UNKNOWN/CONTENT）と `firestore_report` の NON_RETRYABLE が別々に存在。**provider に帰属していない** | provider 単位で帰因（`classify_provider_error`） |
| `occurred_at`（UTC）／`job_id`（任意） | `_utcnow()` あり／job_key＝`base` あり | そのまま採用 |
| 書く側＝SS 識別段の provider 失敗 | 失敗は `print` のみ。控制面へ出る通道が無い | headless 経路に結線 |
| **前提＝例外帰因の是正** | **bug 実在**：`_route_ocr_strategy` の単一 try が PaddleOCR 呼出と 3 つの Gemini 呼出を一括で包み、except が一律「PaddleOCR失敗」 | try を provider ごとに分割 |
| `event_id` 採番規則 | 契約 §9-1（U7 校准）未定 | 暫定＝`{job_id}:p{page}:{provider}:{error_class}`（内容確定的＝重跑で増殖しない）。遺留 |
| 保持期間 | 同上未定 | 控制面側の課題。SS は書くだけ |

## 4. Plan と契約の食い違い（**契約が勝つ**・鉄則 3）

Plan §4 T3 の DoD は「伝送層／レート制限／応答異常が**それぞれ別の** `error_class` で
記録される」と書いているが、これは契約下で実現不能：§5.4 の値域は三分類のみで、しかも
「429 → RETRYABLE」と明記されているため、**伝送層とレート制限は必然的に同一
`error_class`** になる。区別のための `status_code` 追加は §5.7 字段表の拡張＝跨倉の
契約改訂であり、本 session の範囲外。

→ T3 の DoD を「三類の失敗がそれぞれ**正しく帰因され** §5.4 へ写像される」へ読み替えた。
**断路器は RETRYABLE しか数えないので機能上の損失はゼロ。** 控制面 session へ申し送る。

## 4-bis. simcodex 3 輪の辯論裁決（2026-08-02）

`/simplify` の 4 agent（reuse／simplification／efficiency／altitude）＋ `codex review` を 3 輪。

**採納して直したもの**

| # | 指摘 | 命中者 | 何が問題だったか |
|---|---|---|---|
| A | `provider_events` が例外分類器（`classify_provider_error`＋名前表）を自前で持っていた | reuse＋altitude | `ocr_engine._classify_page_error` との二重定義。漂移すると同じ例外が頁級台帳では RETRYABLE、断路器へは UNKNOWN になり**無実の provider が熔断される**。分類の権威を `ocr_engine` 一本に戻し、`provider_events` からは削除（`NoClassifierHereTest` が再導入を禁じる番人）。副産物として `ocr_engine → provider_events` の依存辺そのものが消えた |
| B | `_emit_provider_event` の `except Exception` が `record` の投げる `ValueError`/`TypeError` を吞んでいた | reuse＋altitude | **脱敏の否定対照が唯一の生産経路でだけ無効化**されていた（テストは `record` を直接叩くので緑のまま）。値域違反・脱敏違反は再送出へ。番人＝`RedactionGuardSurvivesWiringTest`（本物の writer を通す 1 件を含む） |
| C | Firestore 書込に `timeout` 無し・警告の重複抑止無し | reuse | 吊るされた Firestore が OCR を止め得た／20 頁障害で同文 20 行を無人 mini PC に吐いた。`firestore_progress._try_set` と同流儀へ |
| D | `_written` set と `_counts` dict が同じ事実の二重管理 | simplification | 「冪等な再記録は上限を食わない」不変式が**両方触らないのを憶えている**ことでしか保てなかった。単一 `_seen: cap_key → set[event_id]` へ。不変式が構造になった |
| E | `gemini_attempted` の死初期化＋導出可能 | simplification＋efficiency | 読み手が 4 つの return を辿らされる |
| F | 読取失敗時に `occurrences=1` を書き、ディスク上の本物の累計を潰していた | codex R2＋altitude | **嘘をつくカウンタはカウンタが無いより悪い**。`_read_reason_stats` が「正常に読めて空」（`{}`）と「読めなかった」（`None`）を区別し、後者は `merge=True` で累計を据置き＋`reason_stats_state="stale_due_to_read_failure"` の降級標記。否定対照で番人性を実証（実装を旧挙動へ退行させると赤くなることを確認） |
| G | Vision 兜底が `None` を返した頁が事象化されていなかった | codex R3 | 主経路の応答異常は記録されるのに兜底だけ沈黙＝**同じ障害が経路次第で見えたり見えなかったりする**非対称。逐頁・尾段の両方で塞いだ |

**駁回したもの（理由を残す＝輪回防止）**

| # | 指摘 | 駁回理由 |
|---|---|---|
| 並 | `reason_stats` を transaction／`firestore.Increment` で原子化（codex が R1=P2 → R2=P1 → R3=P2 と 3 度提起） | **前提が成立しない**。SS は単一 mini PC 上の単一 `main.py` プロセス、generator で逐件逐頁の直列処理、`alerts/` の書き手は契約 §6／§3.2 により SS のみ（控制面は読側）、`write_alert` の 2 呼出点は同一の直列主循環内。この事実を回餵したところ **codex は R2 で明示的に撤回**し、「全面 `Increment` 化は本システムでは過度設計」にも同意した。R3 の再提起は新論拠を伴わない（codex プロセスに記憶が無いため）。**将来並列化するなら、その時に部署形態ごと見直す**（YAGNI） |
| 3 | `page_progress.utc_now` を再利用して `_utcnow` の 4 個目の複製を消す | `page_progress` は `gspread` を引き込む。`provider_events` は `ocr_engine` から import される側なので、依存を足すと全経路に効く。**2 行の複製 < 重依存の伝播**。同じ理由で `firestore_progress.SET_TIMEOUT_SECONDS` も共有せず自前に持つ（値が 10 秒と 15 秒にずれても「止まらない」目的は両方果たすので、A の分類表のような正誤に関わる複製ではない） |
| 1 | 事象送出を `_generate_content_with_retry`（HTTP 境界）へ寄せ、sink を `contextvars` で環境的に解決する | 筋は通っており**将来の選択肢として遺留**。ただし `event_sink` の 5 段貫通を消す代わりに generator 内での ContextVar 束縛の寿命検証が要り、`_route_ocr_strategy` の署名ごと変わる＝B8 の範囲を大きく超える。今回は見送り |
| 6/7/8 | テスト fake を `fake_firestore.py` へ統合／`headless_rerun_fixture.run_headless` へ `event_sink` を追加 | テスト専用の整理。P2 として遺留（§6） |

## 4-ter. 趙の再確認と D3／D6 の改訂（2026-08-03）

趙が「D3 は自分が選んだ形（追加）と違う」「D6 は拍板前に 20 が書かれた」と指摘し、
**両件を Codex に独立評審させ直した**（前回とは別プロセス＝前の辯論の記憶なし。
D3 は『負責人は追加を志向している』と明示したうえで中立に問い、D6 は
『先に自分で推導してから実装値を見よ』という順序で問うた）。

### D3 — Codex は現方式を支持。ただし理由を訂正し、欠陥を 1 つ発見

- **結論**：Codex は自分で `intake_guard.py` を読んで頻度を検証したうえで、
  「純追加」ではなく**現方式（単文檔＋原因別集計）を選ぶ**と回答。
- **我方の理由は却下された**：「純追加は防刷屏の護欄を壊す」は*文檔数と重複抑制*の
  意味では成立するが、**当システムの主リスクとしては成立しない**（3 秒輪詢による
  刷屏は制御フロー自体が既に消している）。正しい理由は別で、
  「この集合の役割は*各ファイル 1 件の待処置線索*であり、控制面の読取面が未建設な
  以上、消費側に事件流の走査・去重・集約を先回りで負わせるべきでない」。
  なお Codex は**これを決定的事実とは言わない**——人手監査が「毎回の拒否の時系列＋
  当時の posting_id」を要するなら append-only が正しく、読取面はそれに合わせて建てる
  べきだと明言している。要件がそう変わったら再検討する。
- **発見された欠陥（採納）**：`occurrences` の加算は**冪等でない**。
  ①`alerted` はプロセス内キャッシュなので再起動を挟むと「alert 済み・move 未了」の件が
  再び書かれる ②書込がサーバ側で成功したのに応答受領前に失敗した場合の再試行。
  つまり実体は「**書込に成功した回数**」であって業務事象数ではない。
  → **`write_count` へ改名**し、docstring に嘘をつかない定義を書いた（趙裁定＝
  「只改语义标注」）。旧字段が残る環境のために一行だけ引継ぎを入れてある。
- **見送った第三案**：単文檔内に上限付き `history` 配列（最近 20 件＋
  `history_truncated`）を持つ案。趙裁定により本輪は採らない（遺留 §6-8）。

### D6 — Codex は不一致と判定。**致命的な穴が実在した**

Codex に先に独立推導させた結果と実装の差：

| 項 | 初版実装 | Codex | 裁決 |
|---|---|---|---|
| 上限 | 20 | 5 | **20 を維持**（我方勝・下記） |
| 作用域 | `job×provider×error_class` | `provider×RETRYABLE`（job を跨ぐ） | **採納**（`provider×error_class` へ） |
| 生命周期 | **檔ごとに再配分** | 断路器と同じ滑動 10 分窓・檔を跨いで持続 | **採納** |
| 到頂時 | 静默停止 | 停止＋診断信号 | **採納** |

**採納した理由（我方に反論の余地なし）**：檔ごとに配り直すと、provider 全面障害の
最中に小さな檔が次々来た場合**各檔が 20 件ずつ書く**ので、全体の書込量に上界が無い。
封頂の目的そのものを果たしていなかった。我方が当初挙げた「前の檔の障害で使い切った
予算が次の檔を飢えさせる」という理由は、まさにその裏返しで**檔を跨いだ無防備**を
作っていた。→ `ProviderEventWriter` は**ループが 1 個持つ**形へ（`main` の
`provider_sink`）。番人＝`SinkLifetimeTest`。

**駁回した理由（上限を 5 へ絞る案）**：
①契約 §9 が断路器パラメータを「**宽松起步**、U7 benchmark 実測後に**逐步缩减**校准」と
明記しており、SS 側が先回りして最小値まで締めるのは契約の方針に反する
②§5.4 の文言は「10 分窗口内**連続** 5 件」であり、**厳密な時系列連続**だとすると
間に `NON_RETRYABLE` が挟まった時点で連鎖が切れ、5 件ちょうどでは届かない可能性がある。
Codex 自身もこの語義の確認が先だと述べている（遺留 §6-9）。

**Codex が追加で出した P2（採納）**：`occurrences` → `write_count` の改名は、旧字段を
持つ文檔の次回更新で累計を 1 へ戻してしまう。本番（`main` 分支）には
`reason_stats` 自体が存在しないので実害は無いが、`ab634c9` は push 済みで、遺留の
「真 Firestore 往返検証」を本分支で行った環境には残り得る。**一行の引継ぎを追加**
（リスクが非対称——入れない場合の代償は静かなデータ損失、入れた場合の代償は
恐らく到達しない一行）。

**駁回（3 度目の再提起）**：`reason_stats` の transaction／`Increment` 原子化。
前回 R2 で Codex 自身が「並発の前提が成立しない」と**明示的に撤回**し、
「全面 `Increment` 化は過度設計」にも同意している。新しい codex プロセスには
記憶が無いため再提起されるが、新論拠は伴っていない。§4-bis の駁回理由が引き続き有効。

## 5. 実装と検証

| 項目 | 内容 |
|---|---|
| 新規 | `provider_events.py`（50 stmts, **coverage 100%**）／`test_provider_events.py`（23）／`test_provider_events_wiring.py`（14） |
| 改修 | `ocr_engine.py`（帰因 try 分割・`_emit_provider_event`・`process_pipeline` に `event_sink`/`job_id`）／`main.py`（headless 全経路の貫通と sink 構築）／`firestore_report.py`（D3 `reason_stats`）／`test_firestore_report.py`（＋9） |
| 全量 | `unittest discover` **875 tests OK**（B7 終了時 813 → ＋62） |
| 黄金様本回帰 | `test_golden_replay` ＋ `test_golden_capture` **46 tests OK・skip 0**、fixture 15 本を実消費 |
| 脱敏 | **否定対照**＝禁止字段を渡すと `TypeError`。`record` は keyword-only かつ `**kwargs` を持たないので、迂回路が構造的に存在しない |
| UI 版への波及 | **ゼロ**。`event_sink` 未注入なら事象を出さず戻り値も不変。番人テスト＝`test_ui_path_never_injects_a_sink`（`_process_file_impl` の pipeline 呼出が無改変であることを源で検査） |

**上限を件ごとに配り直す理由**（`main._process_one_file`）：`ProviderEventWriter` は
1 ファイル＝1 個で作る。件を跨いで持ち回すと、前の件の障害で使い切った 20 件の予算の
せいで次の件の事象が黙って書かれなくなる。集合はトップレベルなので、writer の寿命を
件に揃えても控制面から見える形は変わらない。

**封頂 20 が熔断を妨げないこと**：断路器の閾値は 5 件（§5.4）。20 > 5 は
`test_cap_does_not_starve_the_breaker_threshold` が番人として常時検査する。

## 6. 遺留清単

1. **真 Firestore 往返検証**（趙裁定＝本 session は schema 契約テストで閉じる）。実 Firestore の
   パス／権限／index／控制面が実際に読めるかは、次の控制面 session（読取側・凭証持ち）で。
2. **`event_id` 採番規則と保持期間の契約落筆**（§9-1 U7 校准待ち）。本 session の暫定値を
   契約へ昇格させるか別案にするかは控制面の裁定。
3. **契約条文への回写**（跨倉のため本 session では実施しない）：D3 の `reason_stats`、
   §5.7 の書込頻度上限、上記 §4 の Plan/契約食い違い、`alerts` と `provider_events`
   それぞれの「いつ書くか／去重鍵／覆蓋可否／保持期間」明記。
4. **控制面側の `customer_label` 転記実装**（B7 からの継続。本 session の対象外）。
5. **テスト fake の統合**——`test_provider_events.py` の `FakeClient` 系を共有
   `fake_firestore.py`（`set_hook` で書込失敗注入が可能）へ寄せる。`test_firestore_report.py`
   の `_ExplodingGetCollection` も同ファイルの `Exploding*Client` 慣例へ揃える。
6. **`headless_rerun_fixture.run_headless` に `event_sink` を通す**——現状は既定 None で
   通るので壊れていないが、`_process_file_headless` の署名を追う夾具が二つに分かれている。
7. **事象送出を HTTP 境界へ寄せる案**（`_generate_content_with_retry` ＋ `contextvars`）。
   §4-bis で見送った設計。`event_sink` の 5 段貫通と 4 箇所の送出点が 1 箇所になる代わりに、
   generator 内での ContextVar 寿命の検証が要る。
8. **`alerts` に上限付き `history` 配列を持たせる案**（§4-ter の第三案）。趙が本輪は
   「只改语义标注」と裁定したため見送り。人手監査が「毎回の拒否の時系列＋当時の
   posting_id」を要すると判明したら、この案（または真の append-only 事件流）へ。
9. **§5.4「連続 5 件」の語義確定**——「10 分内に 5 件の RETRYABLE」なのか
   「厳密な時系列で途切れないこと」なのか。後者なら間に `NON_RETRYABLE` が挟まると
   連鎖が切れるため、SS 側の封頂値（現 20）と error_class 別の封じ方を見直す必要がある。
   **控制面側で確認すべき事項**（D6 で上限を 5 へ絞らなかった根拠の一つ）。
10. **`occurrences` 引継ぎ一行の削除**——真 Firestore 往返検証で旧字段の不在が
    確認できたら `_merged_reason_stats` から落とす。
