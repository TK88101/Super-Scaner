# 未保護の worksheet を誰でも取得できる（headless 書込点に tab 失効自愈が無い）

> **見出しの粒度について**（simcodex R1・Altitude 観点の指摘を採納）：
> 当初これを「headless の 4 書込点に wrapper を足す」と立てていたが、それでは
> **次に headless 側へ書込点が増えた時に同じ穴が再生産される**。真因は
> 「`_get_or_create_tab` / `_resolve_tab` が誰からでも叩けて、復旧境界の外で
> 生の ws を掴めてしまう」という API 面の設計にある。
> したがって**完了条件は「4 点を塞ぐ」ではなく「未保護 ws を表現不能にする」**
> ——アクセサを `_with_tab_recovery` 専用へ格下げし（改名＋「他に呼出元が無い」
> ことを固定するテスト）、復旧境界を呼出点ではなく tab 解決そのものに巻く。

- 起票：2026-08-11（`main` → `feature/sandevistan-headless` の merge 過程で判明）
- 出所：`docs/plans/2026-08-11-merge-main-into-headless.md` §4 D3 / §10
- 既知欠陥テスト：`test_sheets_output.py` の `HeadlessWritePathKnownLimitationTest`
- 優先度：**P1（headless を生産投入する前に必須）**
- 実施は本 merge に含めない——merge のコミットに新機能を混ぜると
  「この merge が何をしたか」が審査不能になる。また正しい修復は
  witness 更新機構の新設であり、merge の作業量ではない。

---

## 現象

`main` の IP-403 修復（`f7237c0`）が入れた `_with_tab_recovery` は、
**UI 経路の 3 書込点しか覆っていない**：

| 書込点 | `_with_tab_recovery` | 経路 |
|---|---|---|
| `append_entries` | ✅ | UI |
| `_write_unrecognized_row` | ✅ | UI |
| `append_audit_row` | ✅ | 共通 |
| `next_txn_no` | ❌ | headless |
| `peek_append_range` | ❌ | headless |
| `commit_page` | ❌ | headless |
| `probe_page` | ❌（**正しい**） | headless |

headless の 4 点は main 側に存在しなかった（分支が IP-304 で新設した）ため、
merge しても自動的には保護されない。GAS が毎晩 22:00 に tab を削除する運用は
両経路に等しく効くので、headless を有効化すれば同じ 400 で落ちる。

## 4 点は同列に扱えない（3 類）

### ① PENDING 前の preparation — `next_txn_no` / `peek_append_range`
自愈**可能**。ただし個別に closure を被せるだけでは足りない。両者の間で tab が
消えると、旧取引No で構築した rows と新 tab の予測範囲が混ざる。
「tab 解決 → 採番 → PageWrite 構築 → 予測範囲」を**一つの再実行可能な単位**に
する必要がある。

現状の挙動：`next_txn_no` は読取例外を `_get_next_txn_no` が握るため **1 へ縮退**する
（`test_next_txn_no_degrades_to_one_instead_of_healing` で固定済み）。

### ② PENDING 後の commit — `commit_page`
**素朴な再実行は禁止**。`peek_append_range` が返した予測行範囲**と行指紋**は
commit 前に PENDING として永続化される。tab が再作成されると：

1. 行番号が全て変わる
2. **復旧時の取引No再採番で A 列が変わるため、`compute_page_fingerprint` の値
   そのものが変わる**

witness を更新せずに再書込すると、次回起動時の `probe_page` は範囲・指紋の
両方で外れる。

ただし「必ず ESCALATE になる」わけではない——commit と confirm が連続成功すれば
probe は走らない。**危険なのは復旧後の append と confirm の間で落ちた場合**という
条件付きの窓である。

### ③ 既存 witness の probe — `probe_page`
tab を**再作成してはならない**。現行の ESCALATE 語義が正しい
（`test_escalate_when_tab_missing` で固定済み）。

## 正しい修復の方向

witness 更新を伴わない post-PENDING 再書込を禁止した上で、
**新しい行範囲・再採番後の指紋を PENDING へ再 claim してから append する**。
先に着手すべきは①の preparation の原子的再構築。

**完了条件**（見出しの粒度に対応）：4 点に wrapper が付いたことではなく、
**未保護の ws を取得する経路がコード上に存在しないこと**。具体的には
`_get_or_create_tab` / `_resolve_tab` を `_with_tab_recovery` 専用へ格下げし、
「他に呼出元が無い」ことをテストで固定する。

**着手時の注意 2 点**（本 merge の実施後評審で判明）：

1. **`_append_rows_with_recovery` をそのまま `commit_page` から呼んではならない**。
   `PageWrite.rows` は複数票・複数取引No を含み得るが、同 helper は再採番時に
   **全行の `row[0]` を同一番号へ置換**する。加えて PENDING 記録**後**に rows を
   書き換えると、既に保存した fingerprint と不一致になる。戻り値も単一の
   `actual_txn_no` で、更新後の `txn_range` や witness を表現できない。
   再利用できるのはさらに下位の「実測 → 容量確保 → append」部分のみ。
2. `_resolve_tab` を headless へ展開する際、**`tab_owner` に監査タブの予約名
   （`_除外ページ監査`）を許さない入力検証**を足すこと。現状 UI の tab 名は
   `{従業員}_{文書suffix}` なので衝突しないが、headless の tab キーは顧客由来で
   外部入力に近い。

## 併せて立項する周辺欠陥（同じく merge では直さない）

いずれも `main` 側の既存挙動で、merge が導入したものではない。

### L1【P1・headless 投入前に D3 と併せて解消】`_get_next_txn_no` の握り潰しが取引No を汚染する
`sheets_output.py:298` の bare `except Exception: return 1` が、一過性の読取失敗
（429 / 5xx）を「取引No = 1」へ黙って変換する。`_invalidate_tab` は
「400 かつ sheetId 不在」の時しか走らないので、キャッシュはプロセス寿命ぶん汚れたまま。

**症状**：健全な tab に取引No=1 の重複行が入り、復旧していないのに
「🔧 tab 復旧に伴い取引No を再採番」が出る。

**方向**：復旧単位専用の厳格採番（raise させて `_with_tab_recovery` に
400 / 非400 を裁かせる）を用意し、fallback-to-1 は冷起動時だけに閉じる。
正確性を決める判断を「自吞するアクセサ」の上に建てない。

### L2【低】同名 tab の再作成を recovery が検知できない
gspread は title で解決するため、削除後に**同名 tab が作り直されている**と
`get_all_values` / `append_rows` が成功してしまい、例外分岐（`old_ws.id` の検証）に
到達しない。`_tab_next_txn` と `_tabs_sanitized` が旧 tab 基準のまま残る。

発火には「GAS 削除後・Python 書込前に外部が同名 tab を作る」という窄い窓が要る
（Python 自身が作る場合は `_ws_cache` も同時に更新される）。
修すには毎書込前の sheetId 検証が要り、API コストと引き換えになる。

**出所**：codex review（本 merge の実施後評審 Round 1）。

## 影響範囲と緊迫性

- 生産（Windows ミニ PC）は `HEADLESS_MODE` 未設定＝UI 版で稼働中のため、
  **現時点の生産影響はゼロ**。
- headless を有効化した時点で顕在化する。したがって
  **投入判断の前提条件**として扱う。
- 実行時ブロッカーはコードに入れていない——`HEADLESS_MODE` の可否は運用判断で
  あり、実装者が先取りして封じるものではない（Plan §12【2】で辯論の上確定）。
  代わりに既知欠陥テストで事実を固定し、本文書で投入前必須として明示する。

## 併せて記録する既知問題（別件・同 merge 由来）

`main.py` の customer_id 欠落経路（奇形 job）は retry action が `KEEP` のため、
奇形 job が 1 件でも常駐すると毎輪 Firestore `get_job` が走り続け、
**3 秒ごとのバナー・欠落ログで console churn** が起きる。無人機で console は
唯一の観測面なので、これ自体が観測性の劣化になる。
ダウンロード / OCR / Gemini / Sheets には到達しない。

正しい対処は backoff ではなく `customer_meta_alerted` を TTL 付き再照会 memo へ
拡張すること（headless の抑制機構は全て memo 側に寄せる既存設計と層位を揃える）。
複数の奇形 job が常駐する運用になれば優先度を引き上げる。
