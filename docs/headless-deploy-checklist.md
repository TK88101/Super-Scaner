# Super Scaner headless デプロイチェックリスト（feature/sandevistan-headless）

> 対象：`feature/sandevistan-headless` ブランチのデプロイ環境（サンデヴィスタン統合、Phase-1）。
> UI 版（main ブランチ）のデプロイには適用しない。
> 根拠：サンデヴィスタン `docs/impl/03-super-scaner.md` IP-309 ＋ `contracts/job-state-machine.md` §6（v0.12）。
> 執行器義務の全体像は `docs/sandevistan-integration.md`（只読対接説明）を参照。本書はその部署面の checklist。
> 作成：2026-07-16（B1 基座批）。

## 1. Chatwork は故意に未設定（IP-309 / F45 / D16）

**headless 環境の `.env` には `CHATWORK_API_TOKEN` / `CHATWORK_ROOM_ID` を設定しない。**

- これは**遺漏ではなく設計判断**である。後任者は「キーが足りない」と誤解して補回しないこと。
  - 会社判断で Chatwork は廃止済み（SS リポジトリ CLAUDE.md 参照）。
  - headless 運用での失敗上報は **IP-301 の Firestore `transition` 回報（＋ `alerts/{file_id}` 直書き）が唯一の経路**（D16/F45）。Chatwork を復活させても制御面はそれを見ない。
- コード側の `send_notification` 呼び出し（`main.py:362/410/419`）は**削除しない**（F45）。
  `notifier.py:20-22` が token 未設定時に静默スキップするため、キー不在＝零コスト無害。
- 検収基準（IP-309 験収線索）：
  - [ ] headless 全流程走行中、`api.chatwork.com` への出站リクエストが零件（ログ/抓包で確認）
  - [x] 失敗製造 → Firestore に失敗 transition 出現・**単体レベル**（2026-07-16 担保：
        `test_firestore_report.py` の DEAD_LETTER 系テストが fake Firestore 上で
        POSTING_IN_PROGRESS→DEAD_LETTER の transition と last_error 落檔を検証、
        モジュール全テスト綠・simcodex 3 輪通過）
  - [ ] 同上・**main.py 接線後の統合テスト**（接線は B2 以降の後続批）
  - [ ] 同上・**真庫聯調**（実 Firestore＋SA が就緒する U14 待ち。B1 時点では実施不能）

## 2. `.env` キー一覧（静的 grep による現行棚卸し）

本番キーは prefix なし。`local_test.py` 系はテスト用 prefix 付きキーを別途読む（`config.py:183` 参照）。

| キー | 参照点 | headless での扱い |
|---|---|---|
| `GEMINI_API_KEY` | `ocr_engine.py:83` | 必須 |
| `SERVICE_ACCOUNT_FILE` | `main.py:28` | 必須（Drive/Sheets/Firestore 共用 SA。ファイル自体は gitignore） |
| `OUTPUT_SPREADSHEET_ID` | `config.py:9` | 必須 |
| `BACKUP_SPREADSHEET_ID` | `config.py:10` | 現行どおり（GAS `daily_backup.gs` 側で使用） |
| `FOLDER_RECEIPT_ID` 等（文書タイプ別） | `config.py:234`（`load_folder_map`） | 現行どおり必須 |
| `PROCESSED_FOLDER_ID` | `config.py:184` | UI 版のみ使用。**B4/IP-308 実装済（2026-07-25）**：`HEADLESS_MODE=1` では成功路径 move 全廃（重複件検出 `is_duplicate_file` も停用）——SS は書賬＋`report_posted`（`lease_epoch` 携行）のみ、歸檔搬運は控制面。隔離夾 move（入口守衛）が唯一の例外。終態件の再掃防止＝進程内 memo（epoch 感知・TTL 付、跨進程正確性は台賬側）＋intake 状態白名単（`current_state=="POSTING_IN_PROGRESS"` のみ処理） |
| `SPLIT_PDF_FOLDER_ID` | `config.py:192` | **必須（契約 §6）**——Sheets `source_url` の永続リンク先。制御面の帰档/清掃はこの夾に触れてはならない |
| `INPUT_FOLDER_ID` | `config.py:245` | legacy 互換（単一夾時代）。新規環境では文書タイプ別キーを使う |
| `OCR_STRATEGY` / `OCR_CONFIDENCE_THRESHOLD` / `OCR_MODEL_TIER` / `OCR_MAX_SIDE` / `DOC_LOW_CONFIDENCE_THRESHOLD` | `config.py:13-32` | 任意（既定値あり） |
| `CHATWORK_API_TOKEN` / `CHATWORK_ROOM_ID` | `notifier.py:7-8` | **故意に未設定**（§1 参照） |
| `HEADLESS_MODE` | `config.py`（IP-303 追加） | **必須（"1" で有効化）**。当初 B4/IP-308 を予定していたが、監視夾入口守衛（IP-303）が UI 版と headless 版の挙動を分岐する唯一のスイッチであるため **B2 に前倒し**（新行為は本フラグで隔離、UI 版は未設定のまま影響ゼロ）。値解釈：`"1"` → True、`""`／`"0"`（含む他の非 `"1"` 値）→ False |
| `QUARANTINE_FOLDER_ID` | `config.py`（IP-303 追加） | **headless 必配**。`HEADLESS_MODE=1` かつ本キー未設定は `main()` 起動時に即 `exit(1)`（`config.py` 同様の「落として気付かせる」方針）。無 posting_id / job 不一致件の隔離先 |
| `HANDOFF_FOLDER_ID` | `config.py`（IP-303 追加） | **B2 時点は鍵の予約のみ・未接線**。契約上の交棒夾 `handoff_folder_id`（控制面がここへ file を投入）に対応する SS 側の鍵だが、`folder_map` への接線（監視対象化）は後続批。実体夾は趙が実建後に接続 |
| `FIRESTORE_PROJECT_ID` | `config.py` / `firestore_report.build_reporter_from_env()`（IP-303 追加） | 鍵先行、**U14（真 Firestore 環境）待ち**。未設定なら SDK の既定解決（ADC 等）に委ねる |

## 3. Python 依存（B1 で追加）

- `google-cloud-firestore>=2.19,<2.28`（IP-301 の前提。**上限固定の理由**：2.28 以降は
  protobuf>=6.33.5 を要求し、Gemini SDK（`google-ai-generativelanguage`、protobuf<6）と衝突する。
  requirements.txt に固定済み、venv311 実測解＝2.27.0 / protobuf==5.29.5 維持）
- 環境は従来どおり **Python 3.11 の venv311**（PaddlePaddle 互換の唯一解）。

## 4. デプロイ手順への影響

- miniPC 側は従来どおり `git pull` → 再起動だが、**headless 分支の稼働開始は趙の逐次拍板事項**
  （feature ブランチは push 禁止・逐次承認制）。本チェックリストは事前準備の記録である。
- 依存追加があるため、次回デプロイ時は `venv311` で `pip install -r requirements.txt` を忘れないこと。
