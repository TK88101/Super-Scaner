# Plan: Worksheet 快取失効による 400 無限リトライの修復

- 日付: 2026-08-10
- 分支: main（HEAD=0543de5）
- 起票: 実票検証で発覚した本番不具合
- 状態: Phase 1 ドラフト（Codex 評審前）

## 1. 事故の事実（証拠付き）

### 症状
`その他（大島文子）.pdf`（3頁: 頁1=封筒 / 頁2=セブン-イレブン ¥787 / 頁3=ナフコ ¥1,624）を
input フォルダへ投入したところ、同一ファイルが 6 輪繰り返し処理され、毎輪同じ地点で
異常終了した。ファイルは歸檔されず、3 秒間隔で永久に再試行された。

`_処理進捗` タブの記録（6 行とも同一）:

```
頁進捗=1/3  POSTED=0  EXCLUDED=1  PLACEHOLDER=0  FAILED=0  状態=異常終了（APIError）
```

`_除外ページ監査` タブ（6 行とも同一）: `ページ=1 判定=除外 理由=envelope OCR文字数=58`

### 根因（復現により確定）

**`SheetsOutputWriter._ws_cache`（sheets_output.py:97）が保持する Worksheet 参照は
一度も失効しない。対象 tab が削除された後もその参照で読み書きを続けるため、
gspread が `title` から組み立てる range が実在しない sheet を指し、Google が
`400 Unable to parse range` を返す。**

合成データによる最小復現（本番表・テスト tab 上で実施、実施後に完全清掃済み）:

```
=== [zhang-x_領収書] ws 取得済み (sheetId=2109408369) ===
    tab 削除しました。以後、古い ws 参照で書き込みを試みます
    ws.get_all_values():     APIError: [400]: Unable to parse range: 'zhang-x_領収書'
    ws.append_rows([[...]]): APIError: [400]: Unable to parse range: 'zhang-x_領収書'
```

利用者が報告した本番エラー文言と一字一句一致する。

### 無限リトライに至る三段の積み重ね

1. `_write_with_retry`（sheets_output.py:513）は `'429'` のみ退避対象。**400 は即 raise**
2. `process_file` の外殻が `ABORTED` を記録して re-raise → main loop は失敗と判定し
   **ファイルを保持**（IP-401 の正しい語義。重複記帳を防ぐため変更しない）
3. **快取はプロセス再起動でしか消えない** → 毎輪同じ死んだ参照を使い、必ず再発

結果、「数回リトライすれば直る」ではなく **`main.py` を再起動するまで永久に直らない**。
さらに `main.py` には失敗計数・退避・黒名単が一切存在せず（`grep` で
`fail_count|backoff|blacklist` ゼロヒット）、`SCAN_INTERVAL=3` のため
3 秒ごとに OCR + Gemini を焼き続ける。

### これは試験事故ではなく本番欠陥

`gas/daily_backup.gs:163` は `source.deleteSheet(sheet)`。同 `:71` が除外するのは
`_config` と `_` 始まりのタブのみ。**毎晩 22:00 JST に全従業員の記帳タブが削除される。**
`main.py` は常駐で、再起動は 02:00。

→ **22:00〜02:00 の 4 時間は本番の危険窓**。この間の投入は必ず同じ 400 を踏み、
02:00 の再起動まで無限リトライする。今回は手動削除で前倒しに踏んだだけ。

### 無罪が確定したもの（再議防止）

| 容疑 | 判定根拠 |
|---|---|
| 封筒判定 / IP-401 除外経路 | 頁1 は実際に封筒。EXCLUDED=1 は正しい挙動 |
| B7-1 心跳 / 進捗タブ | reporter の公開メソッドは全て自吞（page_progress.py:177-193） |
| `chr(ord('A')+col)` 26列バグ（:626） | AST 抽出 + 281,400 通り fuzz で col 値域は `{1,5,7,8}`、到達不能 |
| Sheets 配額 429 | 実エラーは 400。配額とは無関係 |
| tab 名の引用符 | gspread `absolute_range_name` が正しく引用（検証済み） |
| `gspread_formatting` | `a1_range_to_grid_range` で GridRange 化。文字列 range を送らない |
| `PageUrlResolver` / Drive | 例外型は `Http*`。`APIError` になり得ない |

## 2. 目標と非目標

### 目標
- tab が実行中に消えても、**次の書込で自己修復**して記帳を継続する
- 修復は **1 回だけ**再試行し、それでも失敗すれば正直に例外を上げる
  （無限再構築ループを作らない）
- 修復時に **tab 単位の快取状態を漏れなく**リセットする

- **GAS 備份の読取→削除の間隙で書かれた仕訳が静かに消える競合を塞ぐ**
  （Codex CRITICAL、事実確認済み。趙 2026-08-10 裁定で本 Plan の範囲に含める）
- **未預期例外による無限リトライを構造的に止める**（趙 2026-08-10 裁定で範囲に含める）

### 非目標（本 Plan では扱わない）
- ログファイル出力の導入（別件 P2）
- IP-401 の「全頁失敗はファイル保持」語義の変更（**維持する**。護欄はファイルの
  行き先を一切変えず、再試行の間隔だけを制御する——趙 2026-08-10 裁定）
- GAS の毎晩 tab 削除という運用モデル自体の是非（今回は競合を塞ぐに留め、
  「そもそも毎日消すべきか」は別途の業務判断）
- `_tab_has_data` の死碼整理（Codex 指摘で書込専用と確認。別件 P2）

## 3. 設計

### 3.1 失効の判定

**判定の権威は tab 名ではなく sheetId（gid）**（Codex HIGH 採用）。

1. `gspread.exceptions.APIError` を捕捉し、`e.response.status_code == 400` を確認する
   （文字列 `Unable to parse range` は診断ログ用の補助情報に留め、判定の根拠にしない）
2. **快取していた worksheet の `id` を `spreadsheet.get_worksheet_by_id(old_ws.id)` で引く**
   （gspread 6.2.1 に存在することを検証済み）
   - `WorksheetNotFound` → その sheetId は実在しない = **失効確定**
   - 見つかる → 失効ではない。元例外をそのまま再送出する

`spreadsheet.worksheet(tab_name)`（名前引き）を使ってはならない理由:
tab が削除された後に人手や別プロセスが**同名の tab を作り直した**場合、名前引きは
成功してしまうが快取が持つ sheetId は既に死んでいる。名前で確認すると
「失効ではない」と誤診し、復旧できないまま例外を投げ続ける。sheetId は
immutable なので、この誤診が原理的に起きない。

同様に、文字列一致だけで判定してはならない理由: コード側のバグで本当に不正な
range を送った場合まで「tab 消失」と誤診し、tab を作り直しても直らないのに
再試行してしまう。sheetId の実在確認を挟めば、その誤診を機械的に排除できる。

### 3.2 リセット対象（全て tab 単位）

| 状態 | 場所 | 扱い |
|---|---|---|
| `_ws_cache[tab]` | :97 | 削除 |
| `_tab_next_txn[tab]` | :99 | 削除（再構築後は A 列実測で採番し直す） |
| `_tabs_sanitized` | :100 | `discard(tab)` |
| `_audit_row_count` | :101 | **`tab == AUDIT_TAB_NAME` の時のみ** 0 へ（`_get_or_create_audit_tab` が実測し直す） |

**一括で消すことが要**。`_ws_cache` だけ消すと取引No の起点が古いまま残り、
再構築後の空タブに対して誤った採番をする。

`_tab_has_data`（:98）は**対象外**。Codex 指摘のとおり :142/:149 で書き込まれる
だけで読取点が全檔に存在しない死碼であり、復旧対象に加えると死碼を増殖させる。
整理は別件 P2（§7）。

`_audit_row_count` は tab 単位の状態ではなく監査タブ専用の単一カウンタなので、
一般 tab の失効で巻き添えに 0 にしてはならない（Codex MEDIUM 採用）。
実装は `_invalidate_tab(tab_name, old_ws)` に集約し、この分岐を一箇所に閉じ込める。

### 3.3 適用範囲

worksheet を直接触る経路は「3 つの入口」より多い。Codex HIGH の指摘どおり、
入口数を覆蓋の証明に使わず、**復旧対象と best-effort を明文で線引きする**。

**復旧対象（データを失う書込）— 失効時に必ず再取得して 1 回だけやり直す**

| 経路 | 位置 | 理由 |
|---|---|---|
| `append_entries` の行 append | :352 `get_all_values` / :360 `_write_with_retry` | 仕訳そのもの |
| `_write_unrecognized_row` の占位行 | :631-690 | その頁の唯一の記録 |
| `append_audit_row` の監査行 | :488-500 | 除外頁の唯一の留痕 |
| `_get_or_create_tab` / `_get_or_create_audit_tab` | :134-152 / :428-468 | 上記全ての前提 |

**best-effort（失敗しても記帳は無傷。復旧対象にしない）**

凡例書込（:154-173）、分割線（:183-202）、背景リセット・異常標色（:370-411）、
`_ensure_row_capacity`（:546-552）、`_sanitize_trailing_once`（:564-572）。
これらは全て自前の `try/except` を持ち、失敗しても行データを壊さない。
**「全ての tab 入口が復旧する」とは主張しない**——見た目が崩れることはあるが、
帳簿の値は失われない、という線引きにする。

`start_new_file` について（**複審で Codex が勝ち、全面 best-effort へ降格**）:

当初「`_get_or_create_tab`（:180）は try の外だから例外は伝播する」と反論したが、
Codex の反例が正しい——**失効時の典型経路では `_get_or_create_tab` は
`_ws_cache` に命中して API を一切叩かず、例外も投げず、死んだ ws をそのまま返す**。
実際に 400 を踏むのは次の `:184 ws.get_all_values()` で、そこは try（:183-202）の
内側なので吞まれる。

→ `start_new_file` は**復旧の担い手にしない**。この入口は分割線描画も含め
全て best-effort とし、tab の再取得は後続の ledger append / 占位行 / 監査 append が
担う。Plan 上「この入口で tab を再取得できる」とは主張しない。

実装は `_with_tab_recovery(tab_name, fn)` に集約するが、**fn は「失効判定が
可能な単一の書込操作」に限る**（§3.4）。3 箇所へ個別に try を散らすと将来の
入口で書き忘れるため helper 自体は残すが、巨大な closure を丸ごと再実行する
使い方はしない。

**実装上の厳守事項**（複審で Codex が付した条件・採用）:
`fn` の中に post-write の副作用を入れてはならない。具体的には
`_tab_next_txn` の更新（:363）、格式化、`_sanitize_trailing_once`、
`_audit_row_count += 1`（:500）は **fn の外**で、書込が成功した後に一度だけ実行する。
closure を小さくしても、これらを内側に入れるとローカル状態が二重更新される。

### 3.4 冪等性の検討（Codex HIGH を受けて縮小）

当初案は「tab が無ければ `append_rows` は一行も書けない」から一般の再試行安全性を
導いていたが、これは過度な一般化だった。反例: (a) 同名 tab が作り直された場合、
(b) サーバ側で成功したが応答が届かなかった場合、(c) append 成功後の格式化で
初めて失敗した場合。(c) で外側の大きな closure を再実行すると**二重記帳**になる。

したがって再実行の条件を次の 2 つを**同時に**満たす場合だけに絞る:

1. 失敗した operation が**単一の書込**であること（複数副作用の closure を再実行しない）
2. `get_worksheet_by_id(old_ws.id)` が `WorksheetNotFound` を返し、
   **その sheetId が実在しない**ことが確認できたこと
   → 書込先が存在しない以上、サーバ側で部分的に成功していた可能性がない

この 2 条件を満たさない 400 は復旧を試みず、そのまま送出する。
「直せない失敗を復旧に見せかけない」ことを優先する。

### 3.5 GAS 備份の読取→削除競合（Codex CRITICAL・趙裁定で範囲内）

**事実**（`gas/daily_backup.gs` 実測）:

```
dailyBackupAndClear()
  ├ LockService.getScriptLock()      ← GAS 自身の並行しか止まらない。Python は無関係
  ├ backupAllTabs_()                 ← 全 tab を getValues()/getBackgrounds() で読取
  │                                     ★ ここから下の間、Python は書き続けている ★
  └ deleteSourceTabs_(tabNames)      ← 名前で引き直して deleteSheet
```

読取と削除の間に Python が `append_rows` に成功すると、その仕訳は
**備份にも入らず、tab ごと削除される**。400 も例外も出ず `process_file` は
成功を返して原票を歸檔するため、**顧客は失われたことに気づけない**。

**対処（窓口の縮小・保守的）**: 削除の直前に実測して、備份時と変化していたら**削除しない**。

- `backupAllTabs_` の戻り値を `[{name, sheetId, lastRow}]` に拡張
  （備份時点の行数**と sheetId** を刻む）
- `deleteSourceTabs_` は各 tab を消す前に取り直し、
  **`sheetId` と `lastRow` の両方が備份時と一致する場合のみ** `deleteSheet` する
- 不一致なら削除を **skip** し、Logger + 既存の MailApp 経路で通知する

`sheetId` も比べる理由（複審で Codex が指摘・採用）: 備份後に元 tab が削除され
**同名の tab が作り直された**場合、`getSheetByName` は新しい方を返す。行数が
たまたま一致すると、まだ備份していない新 tab を消してしまう。

翌晩の備份がその tab を丸ごと取り直すのでデータは失われない。代償は
「その tab が 1 日消えずに残り、翌日の備份に前日分が重複して入る」こと。
**備份の重複は復旧可能、データの消失は復旧不能**なので、この非対称性に従う。

**残存リスク（消していない・複審で Codex が勝った点）**:
再実測と `deleteSheet` の間にも TOCTOU 窓口が残る。

```
1. GAS が再実測 → lastRow は備份時と一致
2. Python が append_rows に成功        ← この一瞬に入られると
3. GAS が deleteSheet                   ← その行は備份にも入らず消える
```

したがって本対策は静默遺失を**消滅させるものではなく、窓口を「全 tab の備份に
かかる数十秒」から「単一 tab の再実測〜削除の数ミリ秒」へ縮める**もの。
Plan 上「消えた」と書かない。完全な解決には writer と backup の間の相互排他
（備份窓口の間 Python を停止させる協議）か、そもそも書込中の tab を削除しない
運用モデルが要る。これは P2（§7-3）として残し、趙の判断に委ねる。

### 3.6 main.py の失敗退避護欄（趙裁定で範囲内・ファイルの行き先は変えない）

現状 `main.py` に失敗計数・退避・黒名単は皆無（`grep` で
`fail_count|backoff|blacklist` ゼロヒット）、`SCAN_INTERVAL=3`。
永続的な失敗（今回の 400、403、コードのバグ等）はどれも 3 秒ごとの
無限リトライになり、毎輪 OCR + Gemini を焼く。

**設計（IP-401 の語義を一切変えない）**:

- main loop に `{drive_file_id: {"count": n, "next_attempt_ts": t}}` を持つ
- `process_file` が False を返す/例外を投げたら `count += 1`、
  capped exponential backoff で次回可能時刻を設定
  （目安: 3s → 30s → 5min → 30min → 上限 1h）
- 走査時、`next_attempt_ts` 未到達のファイルは**その回だけ飛ばす**
  （他のファイルは通常どおり処理する）
- 成功したらそのエントリを削除
- 退避に入った時点で 1 回だけ目立つ警告を出す

**ファイルは従来どおり input に保持する**。移動も削除もしない
（IP-401 の「全頁失敗はファイル保持」語義は不変）。変えるのは**再試行の間隔だけ**。
プロセス内の辞書で持つため再起動でリセットされるが、それは許容する
（再起動は人が介入した合図であり、そこから再試行するのは妥当）。

## 4. タスク一覧（各項 DoD 付き）

### T1: 失敗する回帰テストを書く（RED）
`test_sheets_output.py` に `TabDeletedRecoveryTest` を新設。fake spreadsheet は
`get_worksheet_by_id` を持ち、失効を模す時だけ `WorksheetNotFound` を返す。

- `test_append_entries_recovers_when_sheet_id_gone`
  DoD: 修正前は APIError が漏れ、修正後は `'posted'` が返り、行が新 ws に入る。
- `test_recovery_rebinds_cache_to_new_worksheet`（旧「clears_all_caches」を訂正）
  Codex HIGH 採用: 復旧後は快取が**再充填される**ので「キーが無い」とは言えない。
  DoD: `_ws_cache[tab].id` が**新しい sheetId** を指し、`_tab_next_txn[tab]` が
  新 tab の A 列実測値になっていること。
- `test_same_name_tab_recreated_is_treated_as_loss`
  同名 tab が別 id で存在するケース。DoD: 名前引きなら誤判定するが、
  id 引きなので失効と判定して復旧する。
- `test_recovery_retries_only_once`
  DoD: 2 回目の失敗は呼出側へ伝播し、`add_worksheet` 呼出は 1 回のみ。
- `test_other_400_is_not_treated_as_tab_loss`
  `get_worksheet_by_id` が見つかるケース。DoD: 元例外が伝播、`add_worksheet` 不呼出。
- `test_append_audit_row_recovers_and_remeasures_count`
  DoD: 監査タブが復旧し `_audit_row_count` が実測し直される。
- `test_general_tab_loss_does_not_reset_audit_counter`
  DoD: 一般 tab の失効で `_audit_row_count` が 0 にされない（Codex MEDIUM 採用）。
- `test_unrecognized_row_path_recovers`
  DoD: 占位行経路（`_write_unrecognized_row`）でも復旧する。

### T2: `_invalidate_tab` + `_with_tab_recovery` を実装（GREEN）
DoD: T1 が全緑。既存テストが 1 本も赤化しない。

### T3: 復旧対象の 4 経路へ適用
DoD: §3.3 の「復旧対象」表の 4 経路で tab 失効から復旧する。
best-effort 側は従来どおり自前 try/except のまま（変更しない）。
`venv311/bin/python -m unittest discover -p "test_*.py"` 全綠。

### T4: GAS の読取→削除競合を塞ぐ（§3.5）
`gas/daily_backup.gs` の `backupAllTabs_` 戻り値を `[{name, lastRow}]` へ拡張し、
`deleteSourceTabs_` に削除前の再実測と skip を実装。
DoD: 備份後に行が増えた tab が削除されず、Logger と通知に記録される。
clasp で push し、GAS エディタ上で手動実行して確認する。

### T5: main.py の失敗退避護欄（§3.6）
DoD: 同一 file id の連続失敗で再試行間隔が伸び、上限 1h で頭打ちになる。
ファイルは input に保持されたまま（行き先不変）。他ファイルの処理は阻害されない。
`test_main_process_file.py` に退避計算の単体テストを追加。

### T6: 実票による E2E 検証
DoD: §5 の受入基準を満たす。

## 5. 受入基準（実票・スクリプト判定可）

### 5.1 正常系
`その他（大島文子）.pdf` を処理した後、`_処理進捗` タブの当該行が:

```
頁進捗=3/3  POSTED=2  EXCLUDED=1  PLACEHOLDER=0  FAILED=0  状態=完了
```

かつ:
- MF タブに仕訳 2 行（¥787 セブン-イレブン 2026/01/29 / ¥1,624 ナフコ 2026/02/18）
- `_除外ページ監査` に 1 行（`ページ=1 判定=除外 理由=envelope`）
- 入力フォルダからファイルが消え、`processed` へ歸檔されている
- 同一ファイルの再処理が起きない（`_処理進捗` に重複行が増えない）

### 5.2 tab 失効シナリオ
- 処理の途中で対象 tab を手動削除しても、次の書込で tab が再作成され記帳が継続する
- 同名 tab を別途作り直した場合でも（sheetId が変わる）復旧する

### 5.3 GAS 競合
- 備份の読取後・削除前に行が増えた tab は**削除されない**
- 変化のない tab は従来どおり削除される（退行なし）

### 5.4 護欄
- 永続的に失敗するファイルの再試行間隔が 3s → 30s → 5min → … と伸びる
- その間も他のファイルは通常どおり処理される
- 失敗ファイルは input に残る（IP-401 語義の退行なし）

## 6. 影響面

| ファイル | 変更内容 | リスク |
|---|---|---|
| `sheets_output.py` | `_invalidate_tab` / `_with_tab_recovery` 追加、復旧 4 経路へ適用 | 中。記帳の主経路に触れる |
| `gas/daily_backup.gs` | 削除前の再実測と skip | 中。誤ると備份が回らなくなる。GAS 上で手動実行確認が必須 |
| `gas/daily_backup_ido.gs` | 上と**完全同一**のロジック移植 | 同上。井戸会計事務所の実配置 |
| `gas/daily_backup_rental.gs` | 上と**完全同一**のロジック移植 | 同上 |
| `main.py` | 失敗退避辞書と走査時の skip | 中。走査ループに触れる |
| `test_sheets_output.py` | `TabDeletedRecoveryTest`（8 本） | 低 |
| `test_main_process_file.py` | 退避計算の単体テスト | 低 |

`ocr_engine.py` / `page_progress.py` / `doc_types.py` は**変更しない**。

## 7. P2（本 Plan では扱わない・記録のみ）

1. **ログファイル出力**: 現状 `print` のみで、窓を閉じるか 02:00 再起動で消える。
   今回の調査でも本番エラー全文の取得が難所になった。
2. **`_tab_has_data` の死碼整理**: :142/:149 で書くだけで読取点が無い（Codex 指摘）。
3. **GAS の運用モデル再考**: 「毎晩 tab を消す」設計そのものの是非。
   日付ごとの新 tab など、競合が原理的に起きない形にできるか。
4. **取引No の意味づけ**: 全 tab 一意ではなく PDF 単位のグルーピング鍵である旨を
   ドキュメントへ明記（Codex MEDIUM。README の記述と実装は既に一致している）。
5. **復旧時の取引No 非連続**（実装 agent が報告・裁定＝本次は修正しない）:
   `transaction_no` は `append_entries` の冒頭 :237 で確定するため、その後に
   復旧が走って空 tab が再作成されると、新 tab の初行が 1 以外の番号を持ちうる。
   次回呼出で `_invalidate_tab` が消した快取が実測し直されるので自己修復する。
   **修正しない理由**: これは §9 #9 で既知の「同一原票が新旧 tab に分裂する」
   リスクの一表現に過ぎず、より重大なのは**分裂によって前半頁の仕訳が
   tab ごと消えること**の方。番号の連続性だけ直しても本質は解決しない。
   本筋の解は §7-3（備份窓口の停写協議 / 書込中の tab を消さない運用）。
   なお本現象はデータの重複も欠落も生まない（番号が飛ぶだけ）。

## 8. 回退

3 つの変更は互いに独立しており、個別に revert できる。
DB/schema 変更なし、Sheets 上のデータ形式変更なし。
GAS のみ clasp で前版を push し直す必要がある。

## 9. 辯論記録（Phase 1 / Codex 対抗評審 2026-08-10）

Codex 12 条の裁決。事実確認を経てから採否を決めた。

| # | 指摘 | 裁決 | 根拠 |
|---|---|---|---|
| 1 | CRITICAL: GAS 読取→削除の競合で静默遺失 | **採納**（範囲へ昇格） | `dailyBackupAndClear` の二段構造を実測確認。`LockService` は Python を止めない。趙裁定で本 Plan の範囲に |
| 2 | HIGH: 名前引きでなく sheetId で失効判定 | **採納** | `get_worksheet_by_id` の存在を 6.2.1 で検証。同名再作成の誤判定は実在する穴 |
| 3 | HIGH: status は `e.response.status_code` から取る | **採納** | 文字列一致は診断補助へ降格 |
| 4 | HIGH: `start_new_file` は例外を吞むので helper が効かない | **事実を部分駁回・結論は採納** | `_get_or_create_tab`(:180) は try の**外**で伝播する。吞まれるのは分割線のみ。ただし「入口数を覆蓋の証明にするな」は正しく、§3.3 を復旧対象/best-effort の線引きに書き換え |
| 5 | HIGH: 入口は 3 つでは足りない | **採納** | 占位行・監査 header・凡例・格式化を棚卸しし表に明記 |
| 6 | HIGH: 冪等性の論証が過度な一般化 | **採納** | §3.4 を「単一書込 かつ sheetId 不在確認済み」の 2 条件へ縮小 |
| 7 | HIGH: `clears_all_caches` の DoD が自己矛盾 | **採納** | 復旧後は快取が再充填される。DoD を「新 sheetId を指すこと」へ訂正 |
| 8 | MEDIUM: `_tab_has_data` は死碼 | **採納** | 読取点ゼロを grep で確認。復旧対象から除外し P2 へ |
| 9 | MEDIUM: 取引No は元々 PDF ごとに 1 へリセット | **採納** | :204-205 で確認。当方の risk 記述が誤りだった。真のリスクは「同一原票が新旧 tab に分裂すること」 |
| 10 | MEDIUM: main.py 護欄を範囲外にするのは不合理 | **採納** | 「行き先を決めなくても backoff は入れられる」は正しい。趙裁定で範囲へ |
| 11 | MEDIUM: テストが 5 本では不足 | **採納** | 8 本へ拡張（同名再作成・占位行・audit counter 巻き添え等） |
| 12 | LOW: 汎用 helper は過度設計 | **修正採納** | helper は残す（第 4 の入口での書き忘れ防止）が、fn を単一書込に限定し巨大 closure の再実行はしない |

**全採納ではない点**: #4 は事実認識を訂正のうえ結論のみ採用。#12 は「helper 廃止」を
採らず範囲縮小で対応。#1 は事実を全面採納しつつ「快取修正は単独でも価値がある
（無限リトライと API 焼却を止める）」として、GAS 修正が快取修正を阻塞する構成は採らない
——3 つを独立に revert 可能な形で並行実装する（§8）。

### 9.1 複審ラウンド（駁回した 3 条を Codex へ差し戻した結果）

勝負判据＝対抗者が再提するか。再提せず＝当方の維持、再提し論証が成立＝Codex 採用。

| 条 | 結果 | 内容 |
|---|---|---|
| A（#4 start_new_file） | **Codex 勝・全面採用** | 当方の「`_get_or_create_tab` は try 外だから伝播する」は、**失効時の典型経路では快取命中で API を叩かず例外も出ない**という反例で崩れた。:184 `get_all_values()` が吞まれる側にある。→ この入口を復旧の担い手から外し、全面 best-effort へ降格（§3.3 反映済み） |
| B（#12 helper） | **当方維持・条件付き** | Codex は範囲縮小案に同意し再提せず。ただし「post-write の副作用（`_tab_next_txn` 更新・格式化・`_sanitize_trailing_once`・`_audit_row_count += 1`）を fn に入れるな」という条件を付した。妥当なので §3.3 の厳守事項として採用 |
| C（#1 GAS） | **折半** | 「快取修正は単独でも価値がある／独立 revert 可」は Codex が明示同意（当方維持）。一方「lastRow 再実測で静默遺失が消える」は TOCTOU の反例で崩れた。→ §3.5 の表現を「消滅」から「窓口の縮小」へ訂正し、残存窓口を明記。加えて sheetId 比較を追加（同名再作成対策） |

**Plan 定稿**: 2026-08-10。以降の実装は本稿に従う。

### 9.5 最終状態（2026-08-10 実施完了）

全量テスト **438 tests OK**（着手時 406 → 本批で +32 本）。未 commit。

実装した 5 ブロック:
1. `sheets_output.py` — tab 失効の自己修復（sheetId 判定・1 回だけ再試行）
2. `gas/*.gs` 3 本 — 備份の読取→削除競合対策（全有全無ゲート）
3. `main.py` — 失敗退避護欄（3s→30s→5min→30min→1h、ファイルの行き先は不変）
4. `main.py` — 退避の可観測性（ready/backed_off 分割＋60 秒節流の要約行）
5. `sheets_output.py` — 取引No と容量確保を復旧範囲へ（趙 2026-08-10 裁定）

### 9.4 第二次 codex review と追加裁決（2026-08-10）

| 指摘 | 裁決 | 対応 |
|---|---|---|
| **P1** 部分削除が残す中間状態は、同日再実行時に `backupAllTabs_` の冪等処理（当日 backup sheet を消して作り直す）と噛み合って、**既に削除済み tab の備份だけが消える**。源データも備份も両方失われ復旧不能 | **採納・修正済** | 当方が導入した「部分 skip」が生んだ新規リスク。`deleteSourceTabs_` を検査フェーズと実行フェーズに分離し、**skip が 1 件でもあれば 1 つも削除しない**（全有全無）。中間状態を作らないので同日再実行しても源表は完全なまま |
| **P1-2**（simplify 由来）退避篩選が決定点の下層にあり、偽バナー刷屏＋心跳が潰れる | **codex と辯論 → 双方一致で成立** | codex が行番号を実地確認して全面同意。推奨も (a)。**codex が追加で指摘した点**: 「濾すだけで要約を出さない」案は *有失敗作業待ち* を *正常アイドル* に偽装するので不可。→ ready/backed_off 分割 + 60 秒節流の要約行 + 心跳の原語義回復 |
| **裁-1** `_ensure_row_capacity` / `_get_next_txn_no` が復旧前の古い ws を掴む | **趙裁定「現在就納入」→ 実施済** | closure 内で解決済みの新 ws に対し `_get_next_txn_no` を再呼出。正常路径は快取ヒットで **API 呼出ゼロ増**、復旧時のみ新 tab の A 列から実測し、既に構築済みの `rows[i][0]` を上書きしてから書込む。§7-5 に記録した「復旧時の取引No 非連続」はこれで解消 |

**当方が追加検出**（codex は主記帳経路しか見なかった）:
`_write_unrecognized_row`（占位行経路）も同一機構なので併せて修正。片方だけ直すと必ず漂移する。

### 9.3 実装後評審 `codex review --uncommitted`（2026-08-10）

| 指摘 | 裁決 | 対応 |
|---|---|---|
| **P1** `main.py` 護欄の try が `process_file` からしか始まらず、`download_file` と `start_new_file` の恒久失敗は依然として外殻へ抜けて走査全体を止める | **採納・修正済** | 事実確認: try は :987 開始、`download_file`(:976) と `start_new_file`(:979) はその外。しかも `start_new_file` は内部の `_get_or_create_tab`（sheets_output.py:180）が自身の try の外にあるため **APIError をそのまま投げる**——tab 消失こそ本護欄が守るべき当の事象なのに、そこだけ護欄の外に落ちていた。try の起点を download の前へ移動。`local_path` は `None` 初期化＋清掃時の None チェックを追加（download 自体が落ちた時の NameError 回避） |
| **P2** `sheets_output.py` 復旧が起きると `pre_write_count` が消えた tab の行数のまま残り、`start_row`/`end_row` がずれて異常ハイライトが無関係な行を塗る | **採納・修正済** | 「実測 → 容量確保 → 書込」を一つの復旧単位へ統合。§3.4 の「単一書込」原則には反しない——closure に格式化を含めず、復旧は「旧 sheetId が実在しない」確認時のみ発火するので前回の append が部分成功していた可能性がない |

**codex が見落とし、当方で追加検出した同源問題**:
`_write_unrecognized_row`（占位行経路）も読取と書込が別々の復旧単位になっており、
同じ行番号ずれが起きる。codex は主記帳経路しか指摘しなかったが、
**同一機構を片方だけ直すと必ず漂移する**ため併せて修正した。

修正後 `venv311/bin/python -m unittest discover -p "test_*.py"` → **422 tests OK**。

### 9.2 実装フェーズで判明した Plan の漏れ（2026-08-10）

**GAS は 3 本ある**。`gas/daily_backup.gs` / `daily_backup_ido.gs` /
`daily_backup_rental.gs` の各冒頭に「⚠ 同期必須: 本ファイルのロジックは3本で完全に同一。
差分は先頭コメントと定数のみ」と明記されているが、Plan §3.5/§6 は
`daily_backup.gs` 1 本しか挙げていなかった。実装 agent が指摘し、事実確認のうえ採用。

- 確認方法: `grep -c 'tabNames' gas/*.gs` → 基準版 0、他 2 本は各 10（＝漂移が発生していた）
- `ido` は井戸会計事務所（社長専用）の実配置向け。現時点で表は未初期化の空殻だが、
  **コードの漂移は将来の有効化時にそのまま欠陥として残る**ため同期する
- 裁定: 範囲の拡大ではなく「同一修正の完全な着地」として 3 本同期を必須とする
- 移植は函数ロジックのみ。各檔の冒頭コメントと定数（spreadsheet ID 等）の
  既存差分は保持する（`cp` による全檔上書きは禁止）
