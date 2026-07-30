# IP-401 の除外ページを headless 終態機へ適配する（IP-402）

作成日: 2026-07-30 ／ **第3版**（Codex 2輪の対抗評審を反映して範囲を縮小）
**対象ブランチ: `feature/sandevistan-headless`**（merge commit `2eb213b` の直後）

**前提事実: `HEADLESS_MODE` は未設定＝headless 経路は現在無効**（`ledger=None` で
UI 版が走る）。本番稼働中の不具合ではないので、暫定策で妥協せず正しく直せる。

**本版の範囲**: 分類の分離・MF 汚染の防止・分類 drift の表現・檔級終態の是正まで。
**新しい永続状態機（effect 記録）は導入しない**——§9 の headless 有効化前置条件へ繰延。

---

## 1. 事実（コード実読で確認済み）

### 1.1 除外ページの result 形状（main 由来）

`ocr_engine._yield_page_results` は封筒/社会保険料通知書を検出すると
`entries=[]`・`_page_error` **無し**・`_excluded_page=True` の result を yield し、
`_exclude_destination` で行き先を宣言する。

### 1.2 headless 側はそれを「占位ページ」と誤認する

`_classify_page_result_shape:783-802` は `_page_error` 無し＋entries 全件空なら
`("placeholder", None)`。`_excluded_page` 参照は **0 箇所**（実測）。帰結:

| 段 | 位置 | 挙動 |
|---|---|---|
| ① 形状分類 | `:802` | `placeholder` |
| ② 書込判定 | `:862` | `is_placeholder=True` |
| ③ **MF 区へ書込** | `:870` → `_flush_page` → `build_page_write` | `_build_unrecognized_block` が占位行1行を **MF 区へ書く** |
| ④ 頁級 kind | `:875` | `PLACEHOLDER_WRITTEN` |
| ⑤ 檔級終態 | `_aggregate_file_outcome:911` | 全頁占位 → **DEAD_LETTER** |
| ⑥ 台賬 | `:755` / `_extract_tickets:719` | **ticket_count=0** で CONFIRMED |
| ⑦ 重跑輪 | `_prior_page_kind:825` | `count==0` → `PLACEHOLDER_PRIOR` |

### 1.3 OCR コストとロックの実際の所在（第1版の誤りを訂正）

第1版は「台賬 SKIP が OCR を節約する」と誤認し案A/案B を比較していた。実読の結果:

- `_process_file_headless:977` は `for page in process_pipeline(...)` のループ内で
  頁を緩衝し、頁境界で初めて flush する。**台賬 lookup は OCR の後**——SKIP は
  OCR を一切節約しない
- OCR を節約するのは **memo**（`:536-540`「零下載零OCR」、download 前）。
  key は `(base, lease_epoch, file_id)`

### 1.4 終態ごとの memo 寿命と回報（設計を決めた事実）

`_report_headless_outcome:445-...`:

| 終態 | reporter | memo expire | 帰結 |
|---|---|---|---|
| SUCCESS | `report_posted` | `None`（永久） | 控制面に **POSTED**。同 epoch は二度と OCR に到達しない |
| DEAD_LETTER | `report_dead_letter` | `None` | 死信・人工介入 |
| **PARTIAL** | **呼ばない**（F06-How 未確定） | **`None`** | 控制面無変化・同 epoch 再 OCR なし・**epoch 変化で自然に再評価** |
| ESCALATED | 呼ばない（ファイル保持） | `cycle + 20`（**≈60s**） | 60秒ごとに**無限再 OCR**（`_ESCALATE_MEMO_TTL_CYCLES=20`・`SCAN_INTERVAL=3`） |
| FAILED(retryable) | 呼ばない | 不記 | 3秒自癒窓 |

**ESCALATED は「正常な除外」の器として不適**（60秒 TTL ＋ファイル保持は、CLAUDE.md
記録の `ENTRY_BUILDERS` 事故と同型のコスト構造）。**PARTIAL が正しい器。**

### 1.5 schema の制約と身分自証の限界

`_pending_payload` の保存字段は `page_id / page_num / status / ticket_count /
row_count / tickets / predicted_row_range / row_fingerprint / sheet_tab /
sheet_row_range / created_at / updated_at / schema_version`。`schema_version` を持つ
跨倉契約（v0.12 §5.5）。

B4 批の身分自証（`~/.claude/skills/learned/frozen-schema-self-certifying-records.md`）は
`ticket_count` による**二分**（>0 真データ / ==0 占位）。除外を page doc に載せると
`_prior_page_kind:825` が占位と同一視する。**本版は除外を page doc に載せない**ので
この二分を一切揺らさない。

---

## 2. ユーザー裁定（既決・再議しない）

1. **境界は SS 側に限る**。控制面の終態契約は変更しない
2. **零記帳を POSTED と偽らない**。全頁除外を SUCCESS にはしない
3. その器は **PARTIAL**（ESCALATED は §1.4 の60秒 TTL で無限再 OCR を招くため却下）
4. 外部可見・append-only の業務効果には硬冪等を与える。**本版はこれを
   「その副作用自体を headless では発生させない」ことで満たす**（§4.3）。
   effect 記録による硬冪等は §9 へ繰延（2026-07-30、Codex 低9 を採り範囲縮小）

---

## 3. 目標 / 非目標

### 目標

1. **headless 経路で除外ページが MF 区へ一切書き込まない**。
   「汚染しない」の定義を明確化: *仕訳行も占位行も提示行も書かない＝書込回数ゼロ*
   （第2版は目標1と目標4が字面上衝突していた。Codex 中7 を採り解消）
2. 全頁が除外の PDF が **DEAD_LETTER にならず、POSTED とも偽らない**（→ PARTIAL）
3. 記帳が完遂したファイルは除外頁が混在しても **SUCCESS** に到達できる
   （第2版の「除外を含めば SUCCESS 不可」は撤回。Codex 中6——最頻ケース
   「仕訳頁＋封筒頁」が永久非終端になる——を採り是正）
4. 除外判定が**永久固化しない**（測定可能な定義）: 同一 `lease_epoch` 内は再評価
   しない／控制面が新 epoch で再投したときは過去の判定に妨げられず再評価される
5. 分類 drift（過去輪と今輪で判定が違う頁）を**黙って捨てず**、帳簿の事実を
   優先しつつ矛盾を人手へ回す
6. 既存の `ticket_count` 分割器と混型不変式を**壊さない**
7. 既存 page doc の保存 schema に**字段を追加しない**

### 非目標

- 控制面の終態契約変更（裁定1）
- `posting_ledger` の witness 機構（`peek_append_range` / `commit_page` /
  `_classify_probe`）の改変。除外ページはそこを通さない
- **新しい永続状態機（effect 記録）の導入**（§9 へ繰延）
- headless での社会保険料 MF 提示行（§9 へ繰延。§4.3 参照）
- `PARTIAL` の正式回報（F06-How、B4 批からの繰越）
- UI 版（`ledger is None`）の挙動変更。IP-401 で確定済み
- 既存 page doc 経路の並発安全性（B4 批からの継承事項、§9）

---

## 4. 設計（確定版）

### 4.1 頁級 kind の分離と混型不変式

`_classify_page_result_shape` に `("excluded", destination)` を追加。判定順序は
**`_page_error` の後、valid/placeholder の前**。`_EXCLUDED_KINDS` を新設。

| 同一頁内の構成 | 判定 | 理由 |
|---|---|---|
| 除外のみ（destination 一意） | `("excluded", dest)` | 正常 |
| 除外 ＋ 有効票 | `("escalate", "mixed_excluded_and_valid")` | 分割器の無歧義性を守る既存不変式と同思想 |
| 除外 ＋ 占位 | `("escalate", "mixed_excluded_and_placeholder")` | 同上 |
| 除外が複数で destination 混在 | `("escalate", "mixed_exclude_destinations")` | 行き先が一意でない頁は書かない |
| 除外のみで `_exclude_destination` 欠落 | `("excluded", audit_tab)` | 消費側デフォルト。宣言漏れは MF を汚さない側へ倒す（`_error_class` 欠落を UNKNOWN へ倒すのと同思想） |
| 除外 ＋ `_page_error` | error 経路が先に判定（`:783` のまま） | 異常入力への保守側デフォルト |

producer の現行フローでは除外は単独 yield＋即 return なので混型は起きない。
`_yield_page_results` の変更で崩れうる不変式として固定する（既存 `:788` と同じ扱い）。

### 4.2 ledger は必ず参照する。ただし page doc は作らない

Codex 高8「載せないことと参照しないことを混同するな」＋厳重1（第2輪）を反映。

```
shape == "excluded":
    decision = ledger.check_page(page_id)         # 必ず見る
    if decision is ESCALATE:
        return ("ESCALATE", "ledger_witness_ambiguous")
    if decision is SKIP:                          # 過去輪に page doc がある = 分類 drift
        prior = _prior_page_kind(ledger, page_id)
        record_drift_to_audit(prior)              # 監査タブへ drift 行
        if prior == "POSTED_PRIOR":
            return ("POSTED_PRIOR", None)         # 帳簿の事実を優先（下記）
        return ("EXCLUDED", None)                 # PLACEHOLDER_PRIOR → 今輪判定を優先
    return ("EXCLUDED", None)                     # WRITE = 未記帳。page doc は作らない
```

**非対称の理由**（第2輪 厳重1 への回答）:

- `POSTED_PRIOR`（過去に**仕訳**を書いた頁が今回 excluded）→ **POSTED_PRIOR を維持**。
  MF に実在する仕訳行は分類が変わっても消えない。ここで EXCLUDED にすると
  「零記帳」と集計され、実際には記帳済みなのに終態が嘘になる
- `PLACEHOLDER_PRIOR`（過去に**赤い占位行**を書いた頁が今回 excluded）→ **EXCLUDED**。
  占位行は記帳ではなく「読めなかった」という警告であり、今輪のより正確な判定
  （excluded）で上書きしてよい。ここを `PLACEHOLDER_PRIOR` のまま返すと、
  全頁除外が DEAD_LETTER に落ちて目標2・目標4 が破れる（第2輪 厳重1 の実体）
- どちらの場合も**監査タブに drift 行を残す**。MF に残る旧行（仕訳 or 占位）と
  今輪の判定が食い違っている事実を人手が突合できるようにする。ESCALATE には
  しない——60秒 TTL の無限再 OCR を招くため（§1.4）

> **撤回（2026-07-30、実装時の simcodex Round 1 ＋ Codex 裁決）**: 第3版は
> `PLACEHOLDER_PRIOR` の drift に専用 kind `EXCLUDED_DRIFT` を与え「集約では
> `EXCLUDED` と同じ扱い、kind を分けるのは監査・テスト上の識別のため」と
> していた。**この専用 kind は撤回し、素の `EXCLUDED` を返す。**
>
> 対抗評審の論拠（採納）: 頁級 kind は `_aggregate_file_outcome` への一過性の
> 入力であって履歴の記録ではない。drift の事実は監査行（`分類変化` ＋
> `drift:<prior>-><current>`）と既存の page doc が持続的に持っており、集約層に
> 100% 同義の別名を増やすと「網羅的に kind を分岐する後続コードは
> `EXCLUDED_DRIFT` が `EXCLUDED` の別名だと憶えていなければならない」という
> 負債だけが残る。`POSTED_PRIOR` を維持する非対称はむしろこの結論を支持する
> ——`POSTED_PRIOR` は**檔級終態を実際に変える**（全 POSTED_PRIOR は SUCCESS、
> 全除外は PARTIAL）が、drift 側は何も変えない。§9 の effect 記録で分岐が要る
> なら、その判別は `_handle_excluded_page` 内の ledger 状態から直接取れる
> （集約層まで運ぶ必要がない）。
>
> よって `_EXCLUDED_KINDS = frozenset({"EXCLUDED"})`。監査行の
> `drift:<prior>-><current>` 表現と T2 の該当 DoD（文字列の断言）は**そのまま維持**。

### 4.3 headless では除外ページの MF 副作用を発生させない

除外ページは `_flush_page` を通さない（`post_page` / `build_page_write` /
`commit_page` を呼ばない）。留痕は**監査タブのみ**。

**社会保険料通知書（`_exclude_destination == mf_tab`）も、headless では MF 提示行を
書かない。** 理由:

- MF 提示行は外部可見・append-only の業務効果であり、崩潰・並発に対する硬冪等が
  必要（Codex 厳重2/高3）。それには新しい永続状態機（effect 記録）が要る
- その状態機自体に PENDING 恢復の未解決問題があり（第2輪 厳重2・高3）、
  `HEADLESS_MODE` 未有効の現時点で導入するのは順序が逆（Codex 低9）
- **副作用を発生させなければ、その冪等問題は存在しない。** 裁定4 は
  この保守的な方向で満たす

窓口期の代償: headless 有効化後・§9 完了前は、社会保険料通知書の顧客向け提示が
MF タブに出ない（監査タブのみ）。**headless 有効化の前置条件**として §9 に明記する。
UI 版（現行本番）は IP-401 のまま MF 提示行を書く——挙動不変。

### 4.4 監査書込失敗時の語義（headless と UI で意図的に分ける）

`_record_excluded_page`（IP-401、UI 版向け）は監査書込失敗時に MF 赤行へ退避する。
headless で流用すると目標1 が障害時に破れる（Codex 厳重3）。

| 経路 | 監査書込失敗時 |
|---|---|
| UI 版（`ledger is None`） | **現状維持**。MF 赤行へ退避（控制面が無く他に可視化先がない） |
| headless | **MF へ退避しない**。当該頁を `("ESCALATE", "audit_write_failed")` とし、ファイル保持・未回報で控制面へ委ねる |

headless では ESCALATE の60秒再試行が**望ましい**——監査書込失敗は一時障害の
可能性が高く、再試行で自癒する余地がある（正常な除外を ESCALATE にするのとは
状況が違う）。

### 4.5 檔級終態

```
RETRYABLE 頁あり → FAILED(retryable)
UNKNOWN 頁あり   → FAILED
（以降、`_EXCLUDED_KINDS`（＝`{EXCLUDED}` 一種のみ）を母数から除いた kinds で判定）
残りが空         → PARTIAL       ← 零記帳。POSTED と偽らない（裁定2）
残り全部が占位   → DEAD_LETTER
残りに占位あり   → PARTIAL
それ以外         → SUCCESS       ← POSTED 頁があれば除外混在でも SUCCESS（目標3）
```

「仕訳頁＋封筒頁」が SUCCESS になるのは誠実である——封筒は本来記帳すべきでない
頁であり、それを除いて記帳が完遂しているなら POSTED は事実。POSTED と言えないのは
**零記帳**（全頁除外）だけ。

---

## 5. タスク一覧（DoD 付き）

### T1. 頁級 kind の分離

- 実装: `_classify_page_result_shape` に `("excluded", destination)`、`_EXCLUDED_KINDS` 新設
- **DoD:** §4.1 の表の5行それぞれに単体テスト（混型3種が ESCALATE になること含む）
- **DoD:** `_page_error` と `_excluded_page` が同一 result に立つ異常入力で error 優先

### T2. ledger 参照つき除外分岐と drift 表現

- 実装: §4.2
- **DoD:** 除外頁で `post_page` / `build_page_write` / `commit_page` が**呼ばれない**
- **DoD:** 除外頁でも `check_page` は**呼ばれる**
- **DoD:** `POSTED_PRIOR` → kind は `POSTED_PRIOR` のまま、かつ監査タブに drift 行1本
- **DoD:**（**訂正 2026-07-30**、§4.2 の撤回に伴う）`PLACEHOLDER_PRIOR` →
  kind は素の `EXCLUDED`（専用 kind `EXCLUDED_DRIFT` は撤回）、かつ監査タブに
  drift 行1本。drift の識別は監査行が担う
- **DoD:**（第3輪 #6）drift 行の内容を断言する。「1行あること」だけでは突合材料に
  ならない。既存の監査7列（`日時/ファイル名/ページ/判定/理由/OCR文字数/原票URL`）の
  範囲内で、**過去輪の分類と今輪の分類の両方**が読み取れること——`判定` に
  `分類変化`、`理由` に `drift:<prior_kind>-><current_kind>`（例
  `drift:PLACEHOLDER_PRIOR->EXCLUDED`）を入れ、その文字列をテストで固定する
- **DoD:** ledger `ESCALATE` → `("ESCALATE", "ledger_witness_ambiguous")`
- **DoD:**（実装時に追加）同頁に複数の除外 result（destination 一致）が来た場合の
  留痕: reason は出現順で去重連結、`ocr_text_len` は最大値（最も読めた result）。
  destination 不一致は escalate だが reason は分岐に影響しない診断文字列なので、
  人手へ回すより落とさず全部残す
- **DoD:**（**訂正 2026-07-30**）`_EXCLUDED_KINDS == frozenset({"EXCLUDED"})`——
  除外 kind は 1 種のみで、drift 専用の別名を持たない

### T3. MF 副作用の遮断

- 実装: §4.3。`_exclude_destination` が `mf_tab` でも headless では MF へ書かない
- **DoD:** 除外頁（封筒・社会保険料の**両方**）で `writer.append_entries` が呼ばれない
- **DoD:** 除外頁で監査タブ書込が1回
- **DoD:** UI 版で社会保険料の MF 提示行が**従来通り書かれる**（既存テスト緑）

### T4. 監査書込失敗の語義分離

- 実装: §4.4
- **DoD:** headless で監査書込失敗時、MF への書込が0回
- **DoD:** そのファイルの終態が `ESCALATED`（SUCCESS/PARTIAL でない）
- **DoD:** UI 版の退避挙動は不変

### T5. 檔級終態

- 実装: §4.5
- **DoD:** 全頁除外 → `PARTIAL`、かつ `report_posted` / `report_dead_letter` が呼ばれない
- **DoD:** 除外1頁＋POSTED1頁 → `SUCCESS`（目標3）
- **DoD:**（**訂正 2026-07-30**）除外1頁＋占位1頁 → `DEAD_LETTER`。
  第3版は当初ここを `PARTIAL` と書いていたが、それは §4.5 のアルゴリズム
  （除外を母数から除く → 残り全部が占位 → DEAD_LETTER）と矛盾する。実装中に
  テストで検出し、Codex へ単条快速複審を出して裁決した——**§4.5 が正・T5 の
  当該行が誤記**。理由: 除外を除いた残りが `[占位]` なら単頁不可読 PDF と
  意味的に同値であり、封筒が1枚混じっただけで DEAD_LETTER が PARTIAL へ
  格下げされてはならない（除外頁は中立であって、残り頁の意味を変えない）。
  かつ零記帳なので PARTIAL の語義「一部成功」に当たらず、PARTIAL は未回報
  （F06-How 未決）＋memo 永久のため、本物の不可読頁が人手へ上がらないまま
  滞留する
- **DoD:**（上記訂正を裁決した Codex 単条複審が併せて提案した強化条項を採納）
  **除外中立性の不変式**: 非除外頁が 1 頁以上残る限り、除外頁を足しても
  引いても檔級終態は変わらない
- **DoD:** 除外1頁＋RETRYABLE1頁 → `FAILED(retryable)`（短絡順位不変）
- **DoD:** 除外を含まない既存ケースの終態が全て不変（回帰）

### T6. 分類 flip の振る舞いテスト（Codex 高4）

fake writer/ledger/reporter で以下5種を実測する:

- **DoD:** `excluded → valid`（page doc 無し → 今輪 valid）: 通常記帳される
- **DoD:** `excluded → placeholder`: 通常の占位経路（除外の残滞が無いこと）
- **DoD:** `placeholder → excluded`: kind は `EXCLUDED` ＋ 監査 drift 行、全頁なら PARTIAL
- **DoD:** `valid → excluded`: `POSTED_PRIOR` 維持 ＋ 監査 drift 行
- **DoD:** `excluded(audit) → excluded(mf_notice)`: destination が変わっても headless では
  どちらも MF 書込ゼロ（§4.3 により差が出ない）ことを固定

### T7. 消費者契約を振る舞いテストへ

- 事実: `test_pipeline_consumers.py` はファイル級 grep。`main.py` に UI 版の
  `_excluded_page` があるため `_process_file_headless` 未対応でも通る（本 merge で実証）
- 実装: 振る舞いテストを主保証にし、AST テストは「消費者0件なら赤」の空振り検出のみ残す
- **DoD:** `_process_file_headless` から除外処理を外すと振る舞いテストが赤くなる（否定対照）

### T8. 回帰

- **DoD:** 全量緑（実装前の基線 641 件を下回らない。本版完了時点で 689 件）
- **DoD:** headless 既存（`test_process_file_headless` / `test_posting_ledger` /
  `test_headless_*` / `test_golden_*`）緑
- **DoD:** IP-401 由来（`test_ip401_regression` 等）緑

---

## 6. 影響面

| ファイル | 変更 | リスク |
|---|---|---|
| `main.py` | `_classify_page_result_shape` / `_classify_and_flush_page` / `_aggregate_file_outcome` | **高**（headless 終態機の中枢） |
| `sheets_output.py` | 監査タブへ drift 行を書く呼出しのみ（MF 区・witness に触れない） | 低 |
| `test_pipeline_consumers.py` | 振る舞いテストへ再構成 | 低 |
| `headless_rerun_fixture.py` | 夾具拡張（`excluded_page` / `blank_result` / `yield_of` / 監査行と `build_page_write` の spy） | 低 |
| `test_headless_excluded_page.py` | **新規**（T1-T6 ＋ 受容済み限界の記録） | 低 |
| `test_process_file_headless.py` | 重複 helper を夾具 import へ置換（テスト内容は不変） | 低 |
| `CLAUDE.md` | headless の除外語義が UI 版と違うことを追記（実装時に追加） | 低 |
| `posting_ledger.py` | **変更なし** | — |

本版は `posting_ledger` を無改変、新しい永続状態機ゼロ。既存 witness 機構にも触れない。

---

## 7. Codex 第1輪 辯論裁決（16件・要点）

第1版の中核的な事実誤認（「台賬 SKIP が OCR を節約する」）が複数指摘の根に共通し、
地基が崩れた結果として上部構造が同時に崩れたため**全16件を採納**した。事実誤認は
§1.3 で自ら再検証済み。

| # | 重大度 | 指摘の要点 | 反映先 |
|---|---|---|---|
| 1 | 厳重 | 案B は goal 3 を達成しない（SUCCESS→POSTED→memo 永久で**文件級**ロック） | §1.4 / §4.5（PARTIAL） |
| 2 | 厳重 | MF 提示行は必需出力、硬冪等が要る | §4.3（副作用自体を発生させない） |
| 3 | 厳重 | 監査失敗時の MF 退避が goal 1 を破る | §4.4 |
| 4 | 高 | 冪等鍵に filename は不可 | §9 へ繰延（effect 記録と同批） |
| 5 | 高 | check-then-append は並発安全でない | 同上 |
| 6 | 高 | 並発は台賬・witness の明示前提を破る | §9（B4 継承事項） |
| 7 | 高 | goal 3 は控制面の再投契約に依存 | §3-目標4（測定可能に書換） |
| 8 | 高 | **載せないことと参照しないことの混同** | §4.2 |
| 9 | 高 | all-excluded→SUCCESS は実害 | §4.5（PARTIAL） |
| 10 | 高 | 案A への攻撃は false dichotomy | §1.3（比較論を撤回） |
| 11 | 中 | R2 の枠組みが誤り | §1.3 / §1.4 |
| 12 | 中 | 混型除外の仕様が不完全 | §4.1 |
| 13 | 中 | 除外を母数から除くと偽成功になり得る | §4.5（零記帳のみ PARTIAL） |
| 14 | 中 | AST テストは脆く要件を証明しない | T7 |
| 15 | 中 | fail-open な監査追記が冪等要件を壊す | §4.3（副作用ゼロ）／§9 |
| 16 | 低 | 監査重複は無害な観測ノイズではない | §9（effect 記録と同批） |

---

## 8. Codex 第2輪 辯論裁決（9件）

| # | 重大度 | 指摘 | 裁決 |
|---|---|---|---|
| 1 | 厳重 | `PLACEHOLDER_PRIOR → 従来語義` では placeholder→excluded の drift で全頁除外が依然 DEAD_LETTER になる。目標2・4 を破る | **採納**。§4.2 で `PLACEHOLDER_PRIOR` は `EXCLUDED_DRIFT` へ。`POSTED_PRIOR` とは非対称にする理由も明記 |
| 2 | 厳重 | `audit PENDING → 再実行可` は PENDING 恢復経路で並発排他を失い、effect 記録が重複防止として機能しない | **採納（範囲外へ移動）**。effect 記録そのものを §9 へ繰延。副作用を発生させないので本版に該当経路が無い |
| 3 | 高 | `mf_notice PENDING → ESCALATE` は at-most-once であって硬冪等ではない。append 前 crash の提示行が永久に補われず、60秒再 OCR も続く | **採納（範囲外へ移動）**。同上。headless では MF 提示行を書かない（§4.3） |
| 4 | 高 | §4.2 と §4.3 は一体の状態機なのにタスクが分離、5種の分類 flip テストが無い | **採納**。T6 として5種を明示 DoD 化 |
| 5 | 高 | effect doc の collection path / payload / CAS 遷移が未定義。**sibling subcollection なら v0.12 契約変更ではない**（definitive answer） | **採納（範囲外へ移動）**。§9 の前置条件に「`jobs/{job}/effects/{effect_id}` として設計する」を明記。本版は effect を作らない |
| 6 | 中 | 「除外を含めば SUCCESS 不可」は最頻ケース（仕訳頁＋封筒頁）を永久非終端にする | **採納**。当該規則を撤回（§3-目標3、§4.5）。零記帳のみ PARTIAL |
| 7 | 中 | 目標1（MF 汚染なし）と目標4（MF 提示行を書く）が字面上衝突 | **採納**。§3-目標1 で「書込回数ゼロ」と定義し直し、headless の提示行は §9 へ繰延して衝突を解消 |
| 8 | 中 | PARTIAL は通常ケースで目標2・3を満たす（round1 #1/#7/#9 は genuinely resolved）。ただし drift 例外あり | **確認として受領**。drift 例外は本輪 #1 の対応で解消 |
| 9 | 低 | `HEADLESS_MODE` 未有効なのに新永続状態機まで一度に入れるのは順序が逆 | **採納（本版の範囲縮小の根拠）**。分類分離・MF 汚染防止・drift matrix を本版に、effect 硬冪等と PARTIAL 正式回報を §9 の有効化前置条件へ |

**ユーザー裁定（2026-07-30）**: 上記 #9 に沿って範囲を縮小する。裁定4（外部可見副作用の
硬冪等）は「headless ではその副作用を発生させない」という保守的な方向で満たす。

> **2026-07-30 追記（実装時）**: 上表 #1 の裁決で採用した `EXCLUDED_DRIFT`（drift 専用の
> 頁級 kind）は実装時に撤回された（§4.2 の撤回注記）。撤回されたのは「専用 kind」という
> **実現手段だけ**で、裁決の要点——drift を黙って捨てない／`POSTED_PRIOR` と
> `PLACEHOLDER_PRIOR` を非対称に扱う／全頁除外を DEAD_LETTER に落とさない——は
> そのまま維持されている。drift の識別は監査行（判定＝`分類変化`）が担う。
> §8.1 #6（監査行に過去輪と今輪の両分類を符号化せよ）も影響を受けない——
> `drift:<prior>-><current>` の断言は T2 の DoD として維持。

---

## 8.1 Codex 第3輪 辯論裁決（判定: **実装可**）

第3輪の判定は「**Implementable as written. No remaining blocker to implementation.**
ただし §9 の前置条件が全て終わるまで headless は有効化不可（intentionally not
enableable）」。第1輪・第2輪の阻塞指摘は再提されなかった——勝負判据（対抗者が
再提するか否か）により、本版の設計で決着とする。

| # | 重大度 | 指摘 | 裁決 |
|---|---|---|---|
| 1 | 中 | 第1・2輪の阻塞は解決または正当に繰延済み。残る繰延リスク（audit/effect 冪等・社会保険 MF 提示行・PARTIAL 回報・既存台賬の並発）は**有効化を阻むが実装を阻まない** | **受領**。§9 の位置づけと一致 |
| 2 | 阻塞なし | `POSTED_PRIOR` / `PLACEHOLDER_PRIOR` の非対称は妥当。`POSTED_PRIOR` を含む全頁除外が SUCCESS になるのも「実際に零記帳ではない」ので正しい | **確認として受領** |
| 3 | 阻塞なし | `除外＋POSTED → SUCCESS` は通常の除外では誠実。社会保険料については §9-1/2 完了前に有効化した場合のみ不誠実になるが、§9 がそれを禁じている | **確認として受領** |
| 4 | 中 | headless の監査失敗 ESCALATE（60秒再試行）は一時障害には妥当（正常な除外と違い再試行で自癒し得る）。ただし「遠端では commit されたが応答が失われた」曖昧失敗では監査行が重複しうる | **採納（§9 へ）**。effect 冪等が前置条件である限り妥当、という条件付き成立を §10 に明記 |
| 5 | 中 | 除外頁の監査が成功した後、別頁が原因でファイルが retryable になると、次の3秒輪で同じ監査行が再追記される。`EXCLUDED_DRIFT` 行も epoch 間で反復しうる | **採納（§9 へ）**。繰延中の effect 冪等問題の別の現れ。§10 のリスク表に追記 |
| 6 | 低 | `EXCLUDED_DRIFT` 行は既存の監査列に**過去輪と今輪の両分類**を符号化しないと突合証拠として不足。T2 は値を断言すべき | **採納（本版で実施）**。T2 に DoD を追加 |

---

## 9. headless 有効化の前置条件（本版では実施しない）

`HEADLESS_MODE` を有効化する前に、別 Plan ＋ 独立した設計評審で解決すること:

1. **effect 記録による副作用の硬冪等**。`jobs/{job}/effects/{effect_id}` という
   sibling subcollection とし、既存 `jobs/{job}/postings/{page_id}` の意味・schema・
   列挙前提を変えない（Codex 第2輪 #5 の definitive answer に従えば v0.12 契約変更に
   ならない）。要定義: collection path / document ID の encoding / payload 字段 /
   `PENDING` の人手解決手順と許される遷移 / append 前 crash と append 後 crash の別テスト /
   並発再取得の CAS（`PENDING → RETRYING(attempt_id)` 等）
2. **headless での社会保険料 MF 提示行**。1 が済んで初めて安全に書ける（§4.3）
3. **`PARTIAL` の正式回報（F06-How）**。B4 批からの繰越。本版で「全頁除外」も
   同じ未回報状態に合流させたため、F06 未決のままでは全頁除外ファイルが
   watchdog→新 epoch→PARTIAL を繰り返す
4. **既存 page doc 経路の並発安全性**（`_claim_pending` の owner 未検査、
   `peek_append_range`〜`commit_page` 間の排他、読取りキャッシュの単一 lease 前提）。
   B4 批からの継承事項

---

## 10. リスクと回退

| リスク | 対応 |
|---|---|
| 終態機改変で既存 headless テストが崩れる | T8 で全緑を DoD 化。崩れたら設計を疑う（テストを緩めない） |
| 分類 drift で旧占位行が MF に残ったまま終態が変わる | 監査タブへ drift 行（判定＝`分類変化`）を必ず残す（§4.2）。人手突合の材料を作る |
| 全頁除外が PARTIAL で未回報のまま滞留 | §9-3 を有効化前置条件に明記。本版の範囲では控制台ログと監査タブに残る |
| 監査行の重複（第3輪 #4/#5）: ①「遠端 commit 済みだが応答喪失」の曖昧失敗 ②除外頁の監査が成功した後に別頁が原因でファイルが retryable になり次の3秒輪で再追記 ③`分類変化`（drift）監査行の epoch 間反復（第3版では `EXCLUDED_DRIFT` 行と呼んでいたもの。§4.2 の撤回で専用 kind は無くなったが、監査行の重複リスクは変わらない。`test_known_limitation_audit_row_duplicates_on_retry` で現状を固定済み） | **本版では受容**。いずれも繰延中の effect 冪等（§9-1）の現れであり、監査タブは可観測性設備で帳簿ではない。§9-1 完了までの既知の許容範囲として明文化する。headless 有効化前に必ず解消すること |
| 回退 | `wip/sandevistan-before-ip401` 残置。本版の実装は `2eb213b` の後なので `git revert` 可 |
