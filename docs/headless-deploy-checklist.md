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
  - [ ] 同一環境で失敗を 1 件製造 → Firestore に対応する失敗 transition が出現
        （IP-301 完成後に打勾。単体レベルは `test_firestore_report.py` の DEAD_LETTER 系テストで担保；
        真庫聯調は U14（実 Firestore＋SA）就緒待ち）

## 2. `.env` キー一覧（静的 grep による現行棚卸し）

本番キーは prefix なし。`local_test.py` 系はテスト用 prefix 付きキーを別途読む（`config.py:183` 参照）。

| キー | 参照点 | headless での扱い |
|---|---|---|
| `GEMINI_API_KEY` | `ocr_engine.py:83` | 必須 |
| `SERVICE_ACCOUNT_FILE` | `main.py:28` | 必須（Drive/Sheets/Firestore 共用 SA。ファイル自体は gitignore） |
| `OUTPUT_SPREADSHEET_ID` | `config.py:9` | 必須 |
| `BACKUP_SPREADSHEET_ID` | `config.py:10` | 現行どおり（GAS `daily_backup.gs` 側で使用） |
| `FOLDER_RECEIPT_ID` 等（文書タイプ別） | `config.py:234`（`load_folder_map`） | 現行どおり必須 |
| `PROCESSED_FOLDER_ID` | `config.py:184` | 現行どおり（headless の成功路径 move 禁用は B4/IP-308 で扱う） |
| `SPLIT_PDF_FOLDER_ID` | `config.py:192` | **必須（契約 §6）**——Sheets `source_url` の永続リンク先。制御面の帰档/清掃はこの夾に触れてはならない |
| `INPUT_FOLDER_ID` | `config.py:245` | legacy 互換（単一夾時代）。新規環境では文書タイプ別キーを使う |
| `OCR_STRATEGY` / `OCR_CONFIDENCE_THRESHOLD` / `OCR_MODEL_TIER` / `OCR_MAX_SIDE` / `DOC_LOW_CONFIDENCE_THRESHOLD` | `config.py:13-32` | 任意（既定値あり） |
| `CHATWORK_API_TOKEN` / `CHATWORK_ROOM_ID` | `notifier.py:7-8` | **故意に未設定**（§1 参照） |

後続批で追加予定のキー（本批では未実装、参考）：`HEADLESS_MODE`（B4/IP-308）、隔離夾 ID（B2/IP-303）、交棒夾 `handoff_folder_id`（B2）。

## 3. Python 依存（B1 で追加）

- `google-cloud-firestore>=2.19,<2.28`（IP-301 の前提。**上限固定の理由**：2.28 以降は
  protobuf>=6.33.5 を要求し、Gemini SDK（`google-ai-generativelanguage`、protobuf<6）と衝突する。
  requirements.txt に固定済み、venv311 実測解＝2.27.0 / protobuf==5.29.5 維持）
- 環境は従来どおり **Python 3.11 の venv311**（PaddlePaddle 互換の唯一解）。

## 4. デプロイ手順への影響

- miniPC 側は従来どおり `git pull` → 再起動だが、**headless 分支の稼働開始は趙の逐次拍板事項**
  （feature ブランチは push 禁止・逐次承認制）。本チェックリストは事前準備の記録である。
- 依存追加があるため、次回デプロイ時は `venv311` で `pip install -r requirements.txt` を忘れないこと。
