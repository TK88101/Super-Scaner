# Plan: Gemini 応答の記録再生（record-replay）

起案: 2026-08-20 / 対象ブランチ: `main`（実装は wip/ で checkpoint、main へは趙の拍板後）
先行タスク: 無し（**T8c より先に入れる** —— 趙拍板 2026-08-20）
関連: `docs/plans/2026-08-20-dedup-wiring-and-date-anchor.md`（本 Plan を生んだ回帰）
改訂: **v3（2026-08-20）** —— 実施中に見つかった Plan の欠陥 2 件を
Codex ラウンド 3 で複審し裁決（§12）。v2 は Codex ラウンド 1 の 9 件を反映（§10）、
ラウンド 2 で主キー設計を維持（§11）

---

## 0. なぜこれを T8c より先にやるか

2026-08-20 の真票回帰で、**同じ PDF・同じコード・`temperature=0` で
Gemini の出力が 3 回とも違った**ことが実測された。

| 対象 | 基線（1 回目） | 回帰（2 回目） | 診断（3 回目） |
|---|---|---|---|
| TS CUBIC p8 の 54 行の日付 | あり | **全部なし** | あり（`2026/04/14` 等の完全形） |
| `card_name`（補助科目）の充填 | 58/208 行 | **1/210 行** | — |
| p8 の券面総行数の読み取り | — | 「券面58行中54行のみ取得」 | 「券面**57**行中54行のみ取得」 |

この揺れのせいで受入判定 3（「改修前に日付が入っていた 289 行が逐字同一」）が
**外れた**。当初はコードの退行を疑い、monkey-patch による実行時観測を 3 回、
コード読解を数十分かけて、ようやく「T8d の欠陥ではなくモデルの揺れ」と切り分けた。
**毎回これをやるのは成立しない。**

`GEMINI_GENERATION_CONFIG` は既に `temperature: 0` かつ
`response_mime_type: "application/json"` である（`ocr_engine.py:145`）。
**生成パラメータによる決定化は既に打ち止め**であり、揺れを消す道は他に無い。

次タスク T8c が触るのは `card_name` —— **今回もっとも激しく揺れたフィールド**
（58/208 → 1/210）である。記録再生が無いまま T8c を入れると、
その受入判定は再び人手の帰因作業になる。だから順序は本件が先。

---

## 1. 目標 / 非目標

### 目標

1. Gemini 応答を録音し、後で再生できるようにする。再生時
   「Gemini JSON → 仕訳構築 → 出力」が**決定的**になる。
2. コード回帰（決定的）とモデル品質監視（非決定的）を**別の工程に分ける**。
3. **本番のソースを 1 行も変えない。** v1 は「`ocr_engine` に分岐を 1 箇所」だったが、
   Codex 評審を容れて **0 箇所**にした（AD-1 / AD-3）。
4. 録音資料が public repo に入らないことを、規則とテストの両方で担保する。

### 非目標（本 Plan でやらないこと）

- 受入基準そのものの再設計（不変量チェック・フィールド別判定）。
  記録再生が入ってから別 Plan で扱う。本 Plan は**基盤だけ**。
- PaddleOCR の記録再生。OCR はローカルモデルで、揺れは未観測。
  必要になってから足す（YAGNI）。§7 の R-3 で扱う。
- T8c 本体（`card_name` の頁跨ぎ継承）。本 Plan の完了後。
- 既存 1291 テストの書き換え。本 Plan は**足すだけ**。

---

## 2. 本 Plan で下す設計裁定

### AD-1. 差し替え点は `ocr_engine._generate_content_with_retry`。ただし**製品コードは触らず** monkey patch で差し替える

`ocr_engine.py` の Gemini 呼出は 1 本の漏斗に収束している:

```
_call_gemini_text            ─┐
_call_gemini_bytes           ─┤
_call_gemini_cross_validate  ─┼→ _call_gemini_parts(417)
_call_gemini(474) ─→ _call_gemini_bytes(445) ─┘
                                  → _generate_content_with_retry(116)
                                      → model.generate_content(129)
```

業務側の呼出点は 6 箇所（`2159` / `2164` / `2170` / `2173` の A/B/C 経路、
`2771` の頁級 Vision 兜底、`2928` の尾段）。**尾段だけは `_call_gemini` という
wrapper を挟む**が、そこも `_call_gemini_bytes` → `_call_gemini_parts` へ落ちる。
本番コード内で `model.generate_content` を呼ぶのは
`_generate_content_with_retry` **ただ 1 箇所**である（Codex がリポジトリ全体の
grep で確認済み）。

**差し替える層は raw response 層**（`_generate_content_with_retry`）とする。
候補は 2 つあった:

| 層 | 録るもの | 得失 |
|---|---|---|
| `_call_gemini_parts` | パース**後**の dict | 実装が最も簡単。既存 12 テストの mock 慣例とも揃う。**ただし `_parse_gemini_response` が再生の対象外になる** |
| **`_generate_content_with_retry`** | **raw response** | `_parse_gemini_response`（**salvage を含む**）まで再生対象に入る。response の直列化が要る |

**裁定: 後者。** 理由は salvage である。`_parse_gemini_response(salvage=True)` は
MAX_TOKENS で截断した応答から完結した明細行だけを回収する経路で、
**逐行記帳 doc_type（クレカ・交通IC）でしか走らない**。クレカは行数が多く最も
截断しやすい —— 実際 TS CUBIC p7/p8 は「券面59行中57行のみ取得」
「券面58行中54行のみ取得」を出しており、截断の疑いが濃い（**未検証**。§8 TBD-1）。

`test_card_salvage.py` は既にあるが、そこで使う截断 JSON は**人手で作った**もの。
パース後の層で録ると**本物の截断応答は永久に録れない**。
本 Plan の主目的は「実物でしか出ない形」を資産化することなので、簡単さより網羅を採る。

直列化のコストは低い。`_parse_gemini_response` が response から読むのは
4 面だけである（`ocr_engine.py:388-396`）:

- `.text`（zero-parts のとき `ValueError` を送出しうる）
- `_get_finish_reason(response)`（`:249`。**`response.candidates[0].finish_reason` を読む**）
- `_is_max_tokens_truncated(response)`（`:263`）
- `_format_token_usage(response)`（`:363`）

偽 response を組む設備は既にある —— **`ocr_test_helpers.gemini_response`**
（`test_ocr_engine_max_tokens.py` は `_make_response` という別名で import している。
v1 は所在を書き誤っていた。Codex 中 5）。**これを再利用する。**

### AD-2. 録音資料は repo 内 `fixtures/` に置き、`.gitignore` で全数排除する（趙拍板 2026-08-20）

録音した raw response には**顧客の実票の中身がそのまま入る**
（店名・金額・日付・カード番号下 4 桁）。本 repo は PUBLIC
（`github.com/TK88101/Super-Scaner.git`）。

**先例がある。** `.gitignore` の `golden/` の項:

> `golden/` = 領収書稅區分的手工驗證輸出。2026-07-21 生成 15 個 JSON、
> 內容為客戶真實票據的店名・登録番号・日期・金額。
> **本 repo 是 PUBLIC，真實票據資料絕不進入。**
> 檔案已於 2026-08-19 刪除。規則刻意保留 —— 日後若再跑一次同樣的驗證而
> 重新生成，這條會自動擋住。

本件は `golden/` と**同じ性質の資料**である。よって同じ扱いにする。

**裁定**: `fixtures/` を `.gitignore` に追加し、`golden/` と同型のコメントを併記する。
（現状 `.gitignore` に `fixtures/` は**まだ無い**。Codex が `git check-ignore` で確認済み。）

**担保はルールではなくテストで行う。** ただし v1 の
「`git check-ignore` が効くこと」では**不十分**である（Codex 高 4）——
それは通常の追加しか防げず、`git add -f` と**既に tracked になった fixture** を
素通しする。よって番人テストは **`git ls-files 'fixtures/**'` が空**を検査する。
「規則が存在するか」ではなく「実際に追跡されている物があるか」を見る。

### AD-3. 有効化は**製品モジュールに状態を持たせない**。`gemini_record` の context manager が patch する

v1 は `ocr_engine.enable_replay(dir)` を公開し「`main.py` が呼ばないので
本番は構造的に到達不能」と書いたが、**これは過大主張だった**（Codex 重大 1）。
公開 API が本番と同じ import 空間にある限り、他の入口・将来の import 副作用・
対話実行から普通に有効化できる。`grep main.py` の番人は
「今この瞬間その文字列が無い」ことしか言えない。

**裁定**: `ocr_engine` に有効化 API もグローバル状態も**置かない**。
代わりに `gemini_record` 側が context manager を提供し、その中でだけ
`ocr_engine._generate_content_with_retry` を差し替える:

```python
with gemini_record.recording(dir):   # または replaying(dir)
    ...
```

**帰結**: `ocr_engine.py` の変更は **0 行**。本番は分岐そのものが存在しないので、
「誤って再生に入る」経路が**構造的に無い**。R-1 は規律依存から構造保証へ降格する。

この形は本 session の診断スクリプト 2 本（`diag_anchor.py` / `diag_rows.py`）で
既に実地検証済みである —— 製品コード無改変で `resolve_anchor` と
`_fill_row_dates` を観測できた。

**新たに生じる負債**: patch 対象の関数名が暗黙の契約になる。
`_generate_content_with_retry` が改名されると patch は**静かに当たらなくなる**
（例外も出ず、テストは緑のまま実 Gemini を呼びうる）。§6 T-2c の番人テストで
関数の存在と呼出関係を固定する。

### AD-4. 主キーは contents の完全ハッシュ。ただし**部位別に分けて保存**し、不一致は「どこが変わったか」を言う

> **v3 で改訂**（§12）。部位別ハッシュの取得方法が「raw contents の逆解析」から
> 「呼出 wrapper が受け取った実引数（side-channel）＋ 実送出文字列のハッシュ」へ
> 変わった。主キーの思想（fingerprint 主体・順序を主キーにしない）は不変。

**主キー**: contents（prompt 文字列 + 画像 bytes）+ generation_config の
`sha256`。同じ入力なら同じ録音が返る。

**なぜ順序を主キーにしないか**: 呼出順は**コードが変われば変わる**。
順序を主キーにすると、まさに検出したい「コードの差分」が
「録音の取り違え」として現れ、無関係な行が全部ずれる。

**Codex は `seq + call_kind` を主キーにせよと推奨したが、駁回した**（§10 の裁決 2）。
理由: prompt を書き換えた後も順序が一致すれば**旧 prompt への応答**が返り、
「prompt 改修は効果が無かった」という結論が**検証なしに**出てしまう。
今回の回帰は「揺れを退行と誤認」したが、こちらは
**検証していないものを検証したと誤認**する。後者の方が危険である。

**ただし Codex の懸念（fixture が失効しやすい）は実在するので、後半は採納する**:

1. **部位別ハッシュを保存**: `prompt` / `ocr` / `image` / `config` を別々に持つ。
   不一致時に「prompt だけ変わった」「OCR だけ変わった」と**言えるようにする**。
   **v3 で `text` と `call_kind` を追加し、取得元を side-channel へ変更**（§12 の表が正）。
2. **明示的な脱出口 `--accept-drift <部位>`**: 人が「この差分は応答に影響しない」と
   判断したときだけ、その部位の不一致を許して再生を続ける。**既定は厳格停止**。
   許した事実は実行ログと差分レポートに必ず出す。

   **禁止事項（複審で追加）**: `--accept-drift prompt` を
   **prompt 改修の効果検証に使ってはいけない**。prompt を変えた効果を見たいのに
   prompt の差分を許して旧応答を再生するのは、検証したい当のものを迂回する行為で、
   §10 裁決 2 で駁回した破綻シナリオと同じ結果になる。prompt を変えたなら録り直す。

3. **`seq` と `call_kind` は主キーにせず、診断の補助情報として持つ**（複審で確定）。
   用途は不一致時の報告 ——「7 回目の `cross_validate` で `prompt_sha256` だけ違う」
   と言えるようにするため。録音の選択そのものは fingerprint が行う。

**リスク**: PaddleOCR の出力が揺れると `ocr_sha256` が総崩れになる。
OCR の揺れは未観測だが未検証（§8 TBD-2）。T-4 で**測る**。

---

## 3. 保存する内容

Codex 提案は 8 項目（PDF ハッシュ / OCR / モデル名 / パラメータ / prompt 全文 /
生 response / パース後 JSON / 中間表 / Sheets 直前 CSV）だが、**縮める**。

下流が決定的である以上、**中間表と Sheets 直前 CSV は再計算できる**。
保存すると形式変更のたびに fixture が失効し、維持負担だけが残る。

**保存するもの**（1 呼出 = 1 ディレクトリ）:

| ファイル | 中身 | 用途 |
|---|---|---|
| `meta.json` | 呼出順序番号・**部位別ハッシュ 4 種**・全体ハッシュ・モデル名・generation_config・録音日時 | 照合と差分レポート |
| `contents.json` | prompt 文字列（画像は sha256 とバイト長のみ） | 何を送ったかの人間可読な記録 |
| `response.json` | `text` / `finish_reason` / `usage` / `raises` 旗 | **再生の実体** |

形（合成値。実票の値ではない）:

```json
meta.json      {"seq": 7, "call_kind": "cross_validate",
                "content_sha256": "a3f2…(64hex)",
                "parts": {"prompt_sha256": "11aa…", "image_sha256": "9c1d…",
                          "ocr_sha256": "77bb…", "config_sha256": "e401…"},
                "model": "gemini-2.5-flash",
                "generation_config": {"temperature": 0, "max_output_tokens": 32768},
                "recorded_at": "2026-08-20T18:42:11+09:00"}
contents.json  {"prompt": "＜prompt 全文＞",
                "images": [{"sha256": "9c1d…", "bytes": 184320}]}
response.json  {"text": "{\"rows\": [...]}", "finish_reason": 1,
                "usage": {"total_token_count": 9000, "prompt_token_count": 700,
                          "candidates_token_count": 500},
                "raises": false}
```

**`finish_reason` は `candidates[0].finish_reason` として復元する。**
`_get_finish_reason`（`ocr_engine.py:249`）がその構造を読むので、
平坦な値として持たせると**截断判定が静かに変わる**（Codex 中 7）。
T-1 の contract test で固定する。

**画像バイトそのものは保存しない。** 実票の画像は最も機微であり、
再生に必要なのは応答であって入力画像ではない。ハッシュだけ残せば
「同じ画像か」は判定できる。

---

## 4. タスク一覧（各項に DoD）

### T-1. 録音・再生の芯（`gemini_record.py` 新設）

`ocr_engine` に書かず**別モジュール**にする。`ocr_engine.py` は既に 3000 行近く、
CLAUDE.md の「200-400 行、最大 800」を大きく超えている。ここに足さない。

- `RecordedResponse` — `_parse_gemini_response` が読む 4 面を持つ最小の型。
  `.text` は `raises=True` のとき `ValueError` を送出（zero-parts の再現）。
  **`candidates[0].finish_reason` の構造を持つ。**
- `content_key(contents, generation_config)` → 全体 sha256 ＋ 部位別 4 種。
- `save(dir, seq, ...)` / `load(dir)`。
- `recording(dir)` / `replaying(dir, accept_drift=())` — context manager。

**DoD**: `venv311/bin/python -m unittest test_gemini_record -v` が緑。
`RecordedResponse` を `ocr_engine._parse_gemini_response` に食わせると
本物の response と**同じ結果**を返すことを、正常・截断・zero-parts の 3 形態で固定。

### T-2. context manager による差し替え（**製品コードは 0 行変更**）

`recording` / `replaying` の中でだけ
`ocr_engine._generate_content_with_retry` を差し替える。

- 再生 → 録音から引いて `RecordedResponse` を返す（`model.generate_content` を呼ばない）
- 録音 → 原関数を呼び、結果を保存してから返す
- context を抜けたら**必ず元に戻す**（例外時も）

**DoD**:
- `git diff` に `ocr_engine.py` が**現れない**
- context の外では原関数がそのまま使われる（`ocr_engine._generate_content_with_retry`
  が patch 前と同一オブジェクトであることを検査）
- 既存の Gemini 関連テストが全緑

### T-3. テスト入口への配線

`local_test.py` に `--record <dir>` / `--replay <dir>` / `--accept-drift <部位>` を追加。
`local_test` 側で context manager を張る。

**DoD**:
- `--record` で TS CUBIC を流すと `fixtures/` 配下に 9 頁分（＋兜底分）ができる
- 続けて `--replay` で流すと **Gemini を 1 度も呼ばずに**完走する
- 出力（仕訳行）が録音時と**逐字同一**

### T-4. OCR の決定性を測る（AD-4 のリスク検証）

同一 PDF に対し PaddleOCR を 2 回走らせ、テキストが逐字同一かを測る計測器。
**判定ではなく測定**。揺れるなら AD-4 の鍵設計を変える。

**DoD**: TS CUBIC 9 頁で 2 回分の OCR テキストを比較した結果が数字で出る。
揺れが観測されたら §8 TBD-2 を更新し、鍵設計の変更を別途裁定。

### T-5. 秘匿の担保

- `.gitignore` に `fixtures/` を追加（`golden/` と同型のコメント付き）
- 番人テスト: **`git ls-files 'fixtures/**'` が空**

**DoD**: `fixtures/real_votes/x/response.json` を作っても
`git ls-files` に現れない。`git add -f` で追跡させるとテストが**赤**になる。

### T-6. 使い方の記録

`CLAUDE.md` に record-replay の節を足す（録り方・再生の仕方・fixture が
repo に入らないこと・fixture を失ったら録り直しに 50 分と課金がかかること）。

**DoD**: 次の session が Plan を読まずに `--replay` を使える程度に書けている。

---

## 5. 再生時の異常系（黙って落とさない）

| # | 状況 | 挙動 |
|---|---|---|
| E-1 | ハッシュ一致の録音がある | それを返す |
| E-2 | **ハッシュ不一致** | **例外で停止**。seq・**どの部位が違うか**・期待/実ハッシュを出す |
| E-3 | `--accept-drift` で許された部位のみの不一致 | 続行。ただしログと差分レポートに明記 |
| E-4 | 録音が足りない（呼出回数が録音数を超えた） | **例外で停止**。何回目で尽きたかを出す |
| E-5 | 録音ディレクトリが空 / 存在しない | **例外で停止** |

**すべて例外**にするのは、再生は**テスト工程でしか動かない**からである。
本番なら「落とさず兜底」が正しいが（IP-401 の教訓）、テストで黙って
実 Gemini にフォールバックすると、決定的なはずの回帰が
**静かに非決定に戻る** —— 本 Plan の目的そのものを壊す。

---

## 6. テスト戦略（TDD。全局 CLAUDE.md §9）

先に落ちるテストを書いてから実装する。
**§5 の各行に 1 本ずつ**割り当てる（v1 は T-3a に曖昧に吸収していた。Codex 高 3）。

| # | 種別 | 内容 |
|---|---|---|
| T-1a | 単体 | `RecordedResponse` が正常 JSON で本物と同結果 |
| T-1b | 単体 | 截断（MAX_TOKENS）で salvage が同じ行数を回収する |
| T-1c | 単体 | zero-parts で `.text` が `ValueError` を送出する |
| T-1d | 単体 | **`_get_finish_reason` / `_is_max_tokens_truncated` が本物と同判定**（Codex 中 7） |
| T-1e | 単体 | `content_key` が同一入力で同一・異なる入力で相異。部位別ハッシュが部位ごとに動く |
| T-2a | 単体 | context の外では原関数が生きている（patch が漏れない） |
| T-2b | 単体 | 例外が飛んでも context 離脱時に原関数へ戻る |
| T-2c | **番人** | `ocr_engine._generate_content_with_retry` が存在し、`_call_gemini_parts` から呼ばれている（**patch 対象の暗黙契約を固定**。AD-3 の負債） |
| ~~T-2d~~ | ~~番人~~ | ~~`git diff` に `ocr_engine.py` が現れない~~ **→ v3 で T-2d' に差し替え（§12 欠陥 B）。恒久テストにすると将来の正当な編集で誤発火する** |
| T-5-E1 | 単体 | E-1: 一致する録音が返る |
| T-5-E2 | 単体 | E-2: 不一致で例外。**どの部位かがメッセージに出る** |
| T-5-E3 | 単体 | E-3: `--accept-drift` 指定時のみ続行し、ログに残る |
| T-5-E4 | 単体 | E-4: 録音切れで例外 |
| T-5-E5 | 単体 | E-5: 空ディレクトリで例外 |
| T-5-NF | 単体 | **再生中に `model.generate_content` が呼ばれたら即赤**（例外を投げる mock を張る。Codex 高 3） |
| T-3a | 集成 | 録音 → 再生の往復で仕訳出力が逐字同一 |
| T-5a | **番人** | `git ls-files 'fixtures/**'` が空 |

**変異検証**（memory「全緑は壊していないの証明ではない」の教訓）:
実装後、以下を故意に壊してテストが赤くなることを確認する。

1. context 離脱時の復元を消す → T-2a / T-2b が赤
2. ハッシュ不一致を例外でなく**実 Gemini フォールバック**にする → T-5-NF が赤
   （`generate_content` は例外 mock なので、ネットワークにも課金にも触れない）
3. `.gitignore` から `fixtures/` を消し `git add -f` する → T-5a が赤
4. `_generate_content_with_retry` を改名する → T-2c が赤

---

## 7. 影響面とリスク

| # | リスク | 深刻度 | 対処 |
|---|---|---|---|
| R-1 | 本番が誤って再生経路に入り、無関係な応答で記帳する | **最高**（無音で顧客の帳簿が壊れる） | **AD-3 により構造的に消滅**。製品コードに分岐が無い |
| R-2 | 実票データが public repo に入る | **最高**（顧客情報の流出） | AD-2 ＋ T-5a（`git ls-files` 検査）。`golden/` の先例に従う |
| R-3 | OCR が揺れて鍵が総崩れ | 中 | T-4 で測る。揺れたら鍵設計を変更（§8 TBD-2） |
| R-4 | patch 対象の改名で patch が静かに当たらなくなる | 中 | **AD-3 で新たに生じた負債**。T-2c の番人テスト |
| R-5 | fixture を失うと録り直しに 50 分＋課金 | 低 | T-6 に明記。基線 xlsx と同じ運用問題であり、本 Plan で解決しない |

### 回退

製品コードを触らないので、`gemini_record.py` と新規テストを消せば完全に元に戻る。
fixture は消しても本番に影響しない。

---

## 8. TBD（未検証・本 Plan では決めない）

| # | 事項 | なぜ今決めないか |
|---|---|---|
| ~~TBD-1~~ | ~~TS CUBIC p7/p8 の「行取得漏れ」が MAX_TOKENS 截断かどうか~~ | **解消（§15）。截断ではなかった** —— 32/32 の録音が `finish_reason='1'`（STOP）、最大でも予算の 13% しか使っていない。Gemini が自分で「券面 57 行」と宣言しながら 54 行しか吐かない**取りこぼし**である |
| ~~TBD-2~~ | ~~PaddleOCR の決定性~~ | **解消（§13）**。同一プロセス 9/9・別プロセス 9/9 で逐字同一。鍵に `ocr` を残す |
| TBD-3 | 受入基準の再設計（不変量・フィールド別判定） | 記録再生が入ってから別 Plan。Codex の施策 3/4 に対応 |
| TBD-4 | 真票 E2E の実行頻度 | 運用判断＝趙。本 repo に CI は無く「release 前」に対応物が無い |

---

## 9. 受入基準（本 Plan の完了条件）

1. `venv311/bin/python -m unittest discover -p "test_*.py"` が全緑
   （既存 1291 ＋ 新規。既存が 1 件も赤くならないこと）
2. **`git diff` に `ocr_engine.py` を含む製品コードが現れない**
3. `local_test.py --record` → `--replay` の往復で、
   **仕訳出力が逐字同一**（Gemini 呼出 0 回で）
4. 変異検証 4 種すべてでテストが赤くなる
5. `git ls-files 'fixtures/**'` が空であることをテストが担保している

**真票 E2E の位置づけ**（v1 は「含めない」と書いたが Codex 低 9 を容れて整理）:
受入基準 3 の往復には**新規 fixture の採取が要り、それは実 Gemini を 1 回叩く**。
よって「fixture 採取のための手動 E2E 1 回」は**必要**。
ただしそれ以降の回帰は再生で足りるので、**恒常的な合否条件にはしない**。

---

## 10. 辯論記録（Codex 評審 ラウンド 1 — 2026-08-20）

Codex は実際にコードを読んで事実確認した上で 9 件を指摘した
（`.gitignore` に `fixtures/` が無いことを `git check-ignore` で、
`_make_response` の所在を `test_ocr_engine_max_tokens.py` の import 文で、
呼出漏斗をリポジトリ全体の grep で確認）。

| # | 深刻度 | 指摘 | 裁決 | 根拠 |
|---|---|---|---|---|
| 1 | 重大 | 「`main.py` が呼ばないので本番は到達不能」は過大主張。公開 API がある限り他経路から有効化できる | **全面採納** | 正しい。AD-3 を書き換え、製品コードに状態を置かない設計へ。結果 `ocr_engine` の改変は 1 箇所 → **0 箇所**。R-1 が規律依存から構造保証へ降格した。**本ラウンドで最も価値が高い指摘** |
| 2 | 重大 | ハッシュ鍵が過敏。主キーを `seq + call_kind` にし、不一致は差分レポートに | **部分採納**（後半採納・前半駁回） | 部位別ハッシュと差分レポートは採納。主キー変更は駁回 —— prompt 改修後も旧応答が返り「効果が無かった」と**検証なしに**結論づける経路ができる。fixture 失効の懸念は `--accept-drift` で対処。→ 複審へ回付（§11） |
| 3 | 高 | §5 の異常系が T-3a に曖昧に吸収されている。変異検証 2 は実 Gemini 依存 | **全面採納** | §6 を §5 の各行 1 本ずつへ拡張。`generate_content` は例外 mock（T-5-NF） |
| 4 | 高 | `git check-ignore` テストは `git add -f` と既 tracked を防げない | **全面採納** | `git ls-files 'fixtures/**'` が空、へ変更。「規則の有無」でなく「実際の追跡」を見る |
| 5 | 中 | `_make_response` の所在が不正確（実体は `ocr_test_helpers.gemini_response`） | **全面採納** | 事実誤り。修正 |
| 6 | 中 | 漏斗図に尾段の `_call_gemini` wrapper が無い | **全面採納** | 図を修正 |
| 7 | 中 | `finish_reason` を平坦に持つと `candidates[0]` を読む判定が静かに変わる | **全面採納** | §3 に復元要件を明記。T-1d を追加 |
| 8 | 中 | 挿入点の記述が曖昧（関数内分岐か呼出側包装か） | **採納**（1 により自動解消） | monkey patch に変えたので「製品コードに分岐を入れない」で一意になった |
| 9 | 低 | 「真票 E2E は含めない」は受入基準 2 と矛盾 | **全面採納** | §9 を「fixture 採取に手動 E2E 1 回は必要。恒常的な合否条件にはしない」へ整理 |

## 11. 複審（ラウンド 2 — 2026-08-20）

裁決 2 の駁回部分のみを Codex へ回付した（材料: `REBUTTAL_key_design.md`）。

**結果: Codex が主キー案を撤回。当方の論証が通った。**

> 結論: 反論は正しいです。`seq + call_kind` 主キー案は撤回でよく、現在の方針、
> つまり fingerprint 主体 + 部位別差分 + 明示的 `--accept-drift` が筋の良い設計です。

判定規準（fatboyslim「対抗者が再提しなければ当方の勝ち」）に照らし、
**AD-4 の主キー設計を維持**する。

ただし複審は 3 点を足しており、いずれも採納した:

| # | 内容 | 反映先 |
|---|---|---|
| a | **当方の読み違いの訂正**: Codex の原案は `seq + call_kind + stable input fingerprint` の**複合**キーであり、fingerprint も照合条件に含む意図だった。「順序だけで選ぶ」は当方の解釈であって Codex の主張ではない。破綻シナリオが倒したのは前者である | 本節に記録（**当方の誤読を残す**。後続が「Codex が悪い案を出した」と誤って読まないため） |
| b | `--accept-drift prompt` を **prompt 改修の効果検証に使ってはならない** | AD-4 の 2 に禁止事項として明記 |
| c | `seq` / `call_kind` は主キーではなく**診断の補助情報**として持つ（不一致報告用） | AD-4 に 3 として追加 |

**Plan はここで定稿**（v2）。以降 Phase 2（実施）へ進む。

---

## 12. 実施中に見つかった Plan の欠陥と複審（ラウンド 3 — 2026-08-20）→ **v3**

Phase 2（実施）の T-1 に着手し、`ocr_engine.py` の実コードを読んだ段階で
v2 の 2 箇所に齟齬が見つかった。fatboyslim の紀律（「実施中に Plan の錯を
見つけたら止まって直す。受入標準に触るものは Phase 1 へ戻して当該条を複審」）
に従い、その 2 件だけを Codex へ回付した（材料: `PLAN_CORRECTIONS.md`）。

### 欠陥 A: prompt と OCR は raw 層で既に 1 本の文字列に連結されている

**事実**（`ocr_engine.py:439` / `:455`）: `_call_gemini_text` は
`f"{prompt}\n\n--- OCRテキスト ---\n{ocr_text}"` を組んで渡し、
`_call_gemini_cross_validate` も同様に連結済みの 1 本を渡す。
差し替え点 `_generate_content_with_retry` が受け取る `contents` は
`[str]` か `[{"mime_type":…,"data":bytes}, str]` であり、
**prompt と OCR は既に不可分**。v2 §3 の `prompt_sha256` / `ocr_sha256` は
この層では自然に取れない。

**当方の当初案**: 区切り文字列で切り戻し、製品関数を通した往復テストで固定する。

**Codex 判定: 不備あり。** 反例が成立する ——
cross_validate の形は `[image, str]` なので、**区切りが変えられると
`_call_gemini_bytes` と完全に同形**になる。逆解析器から見ると
「OCR を送らない正常系」と区別が付かず、`ocr_sha256` が黙って null になる。
往復テストは回帰検知にはなるが、**構造保証にはならない**。

**裁決: 採納（設計変更）。** 反例は成立している。Codex 案を採る:

> `gemini_record` の context manager が `_call_gemini_text` /
> `_call_gemini_bytes` / `_call_gemini_cross_validate` も薄く wrap し、
> `contextvars` で `call_kind` / `prompt` / `ocr_text` を side-channel として
> `_generate_content_with_retry` の patch に渡す。
> **メタデータの正本は wrapper が受け取った実引数**とし、区切り文字列の
> 逆解析は行わない。

**帰結**:
- `ocr_engine.py` は依然 **0 行変更**（AD-3 は維持）。patch 対象が 1 → 4 に増える。
- R-4（改名で patch が静かに外れる）の対象名が 4 本になる。T-2c の番人を 4 本へ拡張。
- `_call_gemini`（`:474`）は `_call_gemini_bytes` へ委譲するだけなので**包まない**
  （包むと二重に側路が立つ）。尾段（`:2928`）も `_call_gemini_bytes` を通る。
- **側路が立っていない呼出は fail-closed**（録音時も再生時も例外で停止）。
  新しい呼出経路が `_call_gemini_parts` を直接叩くようになったら即座に露見する。
  黙って分類不能のまま録ると、使えない fixture が静かに溜まる。

### 欠陥 A への波及（Codex 質問 3）: side-channel だけでは**定型文の改変が見えない**

**当方が見落としていた点。Codex の指摘で気付いた。**

cross_validate の末尾定型文（「上記のOCRテキストは参考情報です…」）を書き換えると、
**実際に Gemini へ送る文字列は変わる**のに、wrapper が受け取る `prompt` 引数は
変わらない。side-channel だけを正本にすると、この改変が鍵に現れず、
**旧応答をそのまま再生し続ける**。これは AD-4 が prompt 主キーを駁回した
理由（「検証していないものを検証したと誤認する」）と**同じ破綻**である。

**裁決: 採納。** 部位に `text` を追加する ——
`_generate_content_with_retry` が実際に受け取った文字列部品の sha256。

### 改訂後の部位（v3）

| 部位 | 出所 | 何を捕まえるか |
|---|---|---|
| `text` | **実送出文字列**（raw contents の str 部品） | 連結の定型文・区切りを含む、送った物そのもの |
| `prompt` | wrapper の `prompt` 実引数 | prompt 本体の改修 |
| `ocr` | wrapper の `ocr_text` 実引数（無ければ `None`） | PaddleOCR の揺れ（R-3） |
| `image` | blob の bytes | 別ファイル・別頁 |
| `config` | 正規化済み generation_config | 予算・temperature の変更、`line_mode` の差 |
| `call_kind` | どの wrapper が呼ばれたか | text / bytes / cross_validate の取り違え |

`generation_config=None` は `ocr_engine.GEMINI_GENERATION_CONFIG` に**正規化**して
から鍵に入れる。素通しにすると、モジュール既定を変えても fixture が失効せず、
**違う設定の応答を再生し続ける**。

### `--accept-drift` と `text` の関係（v3 で確定）

`ocr` が動けば `text` も必ず動く（OCR は text に埋め込まれているため）。
そのまま集合比較すると `--accept-drift ocr` が `text` の不一致で必ず止まる。
規則を明示する:

1. 差分集合を `{prompt, ocr, image, config, call_kind}` で取る。
2. **その集合が空でなく、かつ許可集合に含まれる**なら続行（`text` の差は
   「説明が付いた」ものとして許す）。
3. **集合が空なのに `text` だけ違う**なら —— 説明の付かない送出文字列の変化
   （定型文・区切りの改変）—— **常に停止**。`--accept-drift ocr` では許されない。
   明示的に `--accept-drift text` を書いたときだけ許す。

Codex の指摘どおり `call_kind` の不一致も停止理由に含める。
`--accept-drift ocr` は「OCR だけ動いて call_kind と prompt は一致」の場合に限る
（規則 2 が自動的にそれを保証する）。

### 欠陥 B: 「`git diff` に `ocr_engine.py` が現れない」を恒久テストにすると誤発火する

v2 §6 は T-2d を**番人**（恒久テスト）に分類していたが、これは
**将来 `ocr_engine.py` を正当な理由で編集した瞬間に赤くなる**。
「二度と変えるな」は不変量として成立しない。作業樹の状態に依存するので、
未 commit の変更がある間ずっと赤い。

**Codex 判定: 妥当**（当方の指摘が正しい）。ただし代案あり ——
文字列検査だけだとコメントや docstring の説明でも赤くなるので AST で見よ。

**裁決: 採納、ただし Codex 案より厳しくする。**
純粋な import 文の AST 検出だけでは
`importlib.import_module("gemini_record")` を漏らす（Codex 自身が
`__import__` の必要性に触れている）。よって:

> **T-2d'（番人）**: `ocr_engine.py` と `main.py` の AST を走査し、
> (a) `gemini_record` の `Import` / `ImportFrom`、
> (b) **docstring 以外**の位置に現れる文字列定数 `"gemini_record"`
> のどちらも無いことを検査する。
> コメントは AST に載らないので**説明を書く自由は残る**。

v2 §9 の受入基準 2（`git diff` に製品コードが現れない）は**そのまま残す** ——
実施完了時に人が `git diff --stat` で 1 回確認する。1 回の検証と恒久の番人は別物。

### §6 テスト表への追加（v3）

| # | 種別 | 内容 |
|---|---|---|
| T-2c' | **番人** | patch 対象 4 本（`_generate_content_with_retry` / `_call_gemini_text` / `_call_gemini_bytes` / `_call_gemini_cross_validate`）が存在し、漏斗が実際にそこを通る |
| T-2d' | **番人** | AST 検査。`ocr_engine.py` / `main.py` が `gemini_record` を import も文字列参照もしない |
| T-1f | 単体 | side-channel が立っていない呼出は録音時も再生時も**例外**（fail-closed） |
| T-1g | 単体 | **定型文だけを変えると `text` が動き、`prompt` は動かない**（Codex 質問 3 の破綻シナリオの回帰） |
| T-5-E6 | 単体 | `--accept-drift ocr` は `text` の差を許すが、**説明の付かない `text` 差**は許さない |
| T-5-E7 | 単体 | `call_kind` の不一致で停止する |

**変異検証に 2 種追加**:

5. side-channel の `prompt` を `text` と同じ値にする（＝定型文の変化を吸収させる）
   → T-1g が赤
6. wrapper の wrap を 1 本外す → T-2c' と T-1f が赤

**Plan はここで v3 として定稿。** 以降 Phase 2 を続行する。

---

## 13. 実装評審（Codex ラウンド 4 — 2026-08-20）と T-4 の測定結果

Phase 2 の実装（T-1〜T-6）完了後、fatboyslim Phase 3 の閘門
「関連する単体テスト全緑」を満たした状態で Codex に実装を回付した。
**新規ファイルは git 上 untracked なので `git diff` には現れない** ——
`codex review` に任せず、ファイルパスを明示して読ませた。

### 裁決表

| # | 深刻度 | 指摘 | 裁決 | 根拠 |
|---|---|---|---|---|
| 1 | 重大 | `measure_ocr_determinism.py` が OCR 本文の断片を標準出力に出す。実票にかける計測器なので店名・金額・カード末尾が端末履歴に残る | **採納**（深刻度は「高」と見る。提交経路ではないため） | 指摘は正しいが**理由が弱い**。決め手は「既存コードの慣例」—— `ocr_engine` は `📝 PaddleOCR完了 (13文字, 置信度: 0.880)` と長さと置信度しか出さない。本文を出していたのは本計測器だけで、破っていたのは当方 |
| 2 | 高 | `local_test.main()` の `except Exception` が `ReplayMismatchError` を握り潰し、「このファイルが失敗」として先へ進み Sheets flush まで到達する | **全面採納** | §5「すべて例外で停止」が静かに骨抜きになる。本 repo が最も嫌う無音失敗の形。`process_all(files, writer, args, fatal)` を切り出し、`except fatal: raise` を `except Exception` の前に置いた |
| 3 | 高 | `recording()` が `exist_ok=True` で既存ディレクトリを再利用し、前回の方が長かったときに古い slot が残る | **全面採納** | 9 回録った上に 5 回録ると `0005`〜`0008` が残り、再生時の録音数も fingerprint 候補も実行と食い違う。既定で拒否し、`overwrite=True`（CLI は `--record-overwrite`）でのみ消して録り直す |
| 4 | 高 | 再生が `recordings[index]` を先に照合せず全録音の fingerprint bucket から返す。順序が入れ替わっても無警告で通る | **部分採納**（前半駁回・後半採納） | 順序を主キーにしない裁定は AD-4 で 3 ラウンド裁決済み —— コードの差分が「録音の取り違え」として現れ、無関係な行まで全部ずれる。よって**順序非依存の照合は既定のまま維持**。ただし「無警告」は実在の欠信号なので、Codex 自身が挙げた代案のうち「必ず drift として記録」を採り、`session.reorders` とログに出す。**「明示オプション化」は駁回**（既定を脆くする） |
| 5 | 中 | 再生 context の終了時に未消費の録音を検査していない | **全面採納** | 9 件録って 5 件しか使われない = **4 頁が処理されていない**。IP-401（54 枚上げて 53 件、枚数を数えるまで気付けなかった）と同族の無音欠落。`ReplayIncompleteError` を新設。**本体が例外で抜けた場合と、実行中に既に記録再生の例外を投げた場合は検査しない**（原因を上書きしない・二重に叱らない） |
| 6 | 低 | 番人テストが `PATCH_TARGETS` を自分で複製しており、実装側から 1 本抜けても自分の複製を見続けて緑のまま | **全面採納** | 番人が守ると宣言したものを守っていない。`gemini_record.PATCH_TARGETS` を直接読み、さらに `{retry} ∪ _VARIANT_WRAPPERS.keys()` と一致することを検査する `PatchTargetRegistryTest` を追加 |

### 自己点検で見つけた欠陥（Codex は挙げなかった）

修復 4（順序相違の記録）と 5（未消費検査）を入れた後で自分で読み直して見つけた。

**差分許可の経路が、既に消費した録音を二度使う。**
`resolve` は fingerprint 一致で `recordings[1]` を消費した後、次の呼出が
漂移すると `recordings[index=1]` を**もう一度**返す。`unused` からの削除は
「まだ入っていれば」という条件付きなので、黙って素通りする。

症状は「1 件の録音が 2 回答える ＋ 別の録音が未消費で残る」。
未消費検査（E-6）が context を抜けるときに叫ぶので最終的には露見するが、
**それでは遅い** —— その間の仕訳は間違った応答から組まれている。

**修復**: 消費点を `take()` 1 箇所に集約し、`consumed` 集合で管理する。
差分の比較相手が消費済みなら、残っている中で最も早いものへ回す。
E-4 の判定も `index >= len(recordings)` から `len(consumed) >= len(recordings)`
へ変えた（順序が入れ替わると index は当てにならない）。
回帰は `DriftReuseTest`、変異 M13 で赤くなることを確認済み。

### 変異検証（第 2 次）

修復ごとに壊して赤くなることを確認した。**12 種すべて発火**（第 1 次 6 種 ＋ 本次 6 種）:

| # | 壊し方 | 赤くなる番人 |
|---|---|---|
| M7 | 未消費録音の検査を消す | `ReplayCompletenessTest` |
| M8 | 録音先の既存チェックを消す | `RecordingDirectoryTest` |
| M9 | `except fatal: raise` を消す | `FatalErrorTest` |
| M10 | 本文を常に出す | `ReportSecrecyTest` |
| M11 | 順序相違の記録を消す | `ReplayOrderTest` |
| M12 | `PATCH_TARGETS` から 1 本落とす | `PatchTargetRegistryTest` |
| M13 | 消費済み録音の回避を外す | `DriftReuseTest` |

### T-4 の測定結果 → **TBD-2 解消**

TS CUBIC（9 頁）に PaddleOCR をかけた:

| 測り方 | 結果 |
|---|---|
| 同一プロセス内で 2 回 | **9/9 頁が逐字同一**。置信度も小数 6 位まで一致 |
| **別プロセスで 2 回**（各 1 回・sha256 を比較） | **9/9 頁が同一 sha256**。`diff` で差分ゼロ |

跨プロセスまで測ったのは、それが**実際に効く性質**だからである ——
fixture は今日のプロセスで録り、明日の別プロセスで再生する。
同一プロセス内の決定性だけでは、その状況を保証しない
（Gemini の揺れも呼出ごと・プロセスごとに出た）。

→ `ocr` を鍵の部位に含めてよい。**TBD-2 解消**。R-3（OCR の揺れで鍵が総崩れ）は
現時点では顕在化していない。ただし測ったのは 1 ファイル 9 頁・計 4 回であり、
「絶対に揺れない」の証明ではない。揺れた場合の逃げ道は `--accept-drift ocr`
として既に在る。

計測器: `scripts/measure_ocr_determinism.py`（既定では OCR 本文を出さない）。

## 14. 実票 E2E（受入基準 3）— 2026-08-20 実施・**合格**

趙拍板は「単ファイル小標本」（全 8 ファイルではなく 1 ファイル）。
対象は UC カード明細 2 頁。

### 手順と結果

| # | 実行 | 結果 |
|---|---|---|
| 1 | `local_test.py --only-file UC --record fixtures/uc` | **Gemini 呼出 2 回**。p1 は仕訳 0 件（認識不能行）、p2 は 12 件。Sheets へ 14 行 |
| 2 | `local_test.py --only-file UC --replay fixtures/uc` | **Gemini 呼出 2 回（すべて録音から）**。不一致なし・順序相違なし・未消費なし。Sheets へ 14 行 |
| 3 | 2 実行が書いた 14 行を列単位で突合 | **仕訳の中身が違う行 = 0** |

突合の内訳（`compare_blocks.py`）:

- 完全一致した行: 1
- **実行依存列のみ違う行: 13** —— `取引No`（当該タブの max+1 を数え直す進行番号）
  `作成日時` `最終更新日時`（時刻）。**Gemini 応答由来ではない**ので比較から
  外した。外したことを明示して数えている（黙って無視すると「27 列一致」を
  「全部一致」と読み違える）
- 仕訳の中身が違う行: **0**

### 「録音由来である」ことの決定的証明

「録音と同じ結果になった」だけでは足りない —— 実 Gemini をもう一度呼んで
**たまたま**同じ答えが返っただけ、という説明が残る。そこで録音の中の金額を
書き換えて追随するかを見た（Sheets を汚さない FakeWriter、Gemini 呼出 0 回）:

| 録音の `text` 内の金額 | 出力された仕訳 |
|---|---|
| `42680`（原本） | `備品・消耗品費 ¥42680` |
| **`99999`（改竄）** | **`備品・消耗品費 ¥99999`** |

追随した。よって再生の出力は**録音から来ている**。

### 副次的に判ったこと

- `_split_pdf_pages` が出す単頁 PDF のバイト列は**実行間で同一**
  （そうでなければ `image` 部位のハッシュが食い違って再生が止まっていた）。
  pypdf の分割が決定的であることの実測証拠。
- 本ファイルは `finish_reason='1'`（STOP）で截断していない。
  TBD-1（TS CUBIC p7/p8 の行取得漏れが MAX_TOKENS 截断か）は**まだ未検証**
  —— それを判じるには TS CUBIC を録る必要がある。

### 受入基準（§9）の最終状態

| # | 内容 | 状態 |
|---|---|---|
| 1 | 全量テスト緑（既存が 1 件も赤くならない） | **合格** 1371 tests OK / expectedFailure 2（既存 1291 ＋ 新規 80） |
| 2 | `git diff` に製品コードが現れない | **合格** `.gitignore` / `CLAUDE.md` / `local_test.py` のみ。`ocr_engine.py` と `main.py` は出ない |
| 3 | 録音 → 再生の往復で仕訳出力が逐字同一 | **合格**（本節） |
| 4 | 変異検証すべてでテストが赤くなる | **合格** 13 種（当初 4 種 → 実装評審と自己点検で 13 種へ拡張） |
| 5 | `git ls-files 'fixtures/**'` が空をテストが担保 | **合格** 実際に録った後も空を確認済み |

**Plan 完了。** 残タスクは無し。次は T8c（補助科目の頁跨ぎ継承）。


---

## 15. 全量真票回帰（2026-08-20 実施）— **合格**

趙の指示で、UC 1 ファイルの E2E（§14）に加えて**真票 8 ファイル全量**を
2 巡した。1 巡目は実 Gemini を呼んで録音、2 巡目は録音のみで再生。
タブは**削除していない**（追記して増分ブロックを突合する方式にし、
破壊的操作を避けた）。

### 実行

| 巡 | コマンド | 所要 | Gemini | 結果 |
|---|---|---|---|---|
| 1 | `local_test.py --record fixtures/full` | 27 分 | **32 回** | 8 ファイル全成功・失敗 0・33 頁 |
| 2 | `local_test.py --replay fixtures/full` | **11 分** | **0 回**（32 回すべて録音から） | 同上 |

2 巡目が 16 分短いのは、Gemini の往復がまるごと消えたためである
（PaddleOCR は 2 巡目も走る。§1 非目標のとおり OCR は録っていない）。

### 突合（`取引No` / `作成日時` / `最終更新日時` は実行ごとに必ず変わるので除外）

| タブ | 1 巡目が書いた行 | 2 巡目が書いた行 | **仕訳の中身が違う行** |
|---|---|---|---|
| `LocalTest_カード明細` | 354 | 354 | **0** |
| `LocalTest_交通IC` | 114 | 114 | **0** |

異常はゼロ —— `SideChannelMissingError` / `ReplayMismatchError` /
`ReplayExhaustedError` / `ReplayIncompleteError` いずれも発生せず、
順序相違も差分許可も 0 件。

**`SideChannelMissingError` が 1 度も出なかったことには意味がある。**
これは「3 変体の wrapper を通らない呼出」を検出する探針であり、
32 回すべてが側路に捕捉された = §12 で決めた覆いが実物で完全だったことになる
（推測ではなく実測）。なお 32 回すべて `cross_validate` で、
Vision 兜底も尾段も発火しなかった。

### TBD-1 の答え —— **截断ではなかった**

| 検査 | 結果 |
|---|---|
| 32 件の `finish_reason` | **全件 `'1'`（STOP）。MAX_TOKENS は 0 件** |
| 最大応答の産出トークン | 8,658 / 予算 65,536（**13%**） |
| salvage の発火 | **0 回**（ログに `🩹` 無し） |

TS CUBIC p8 の「券面57行中54行のみ取得」は**予算不足ではない**。
Gemini が正常終了しながら、自分で宣言した行数を吐き切らない**取りこぼし**である。
よって `max_output_tokens` を上げても直らない（既に実使用の 8 倍ある）。

**AD-1 への影響**: AD-1 が raw response 層を選んだ理由づけの一部
（「クレカは截断しやすい」）は、少なくとも今回の標本では**成立しなかった**。
ただし AD-1 自身が「截断でなくても成立する」と留保していたとおり、
裁定は変えない —— zero-parts（`.text` の `ValueError`）も
`_parse_gemini_response` の分岐であり、パース後の層では再生できない。

### 副産物: Gemini の揺れの 4 度目の観測

**同じコード（`origin/main = 7d97397`、製品コード無改変）・同じ真票**で:

| 実行 | 「日付が空・金額あり」の行数 |
|---|---|
| 本 session 前半の回帰（§0 の 2 回目） | **57** |
| 本節の 1 巡目（録音） | **4** |
| 本節の 2 巡目（再生） | **4**（1 巡目と逐字一致） |

57 → 4 はコードの変更ではなくモデルの揺れである。
そして**再生した 2 巡目は 1 巡目と完全に一致した** ——
記録再生を入れる理由そのものが、この 3 数字に表れている。

### 既存機能の回帰確認（本 Plan の対象外だが同時に見た）

- **T8d 重複頁除外**: アメックス p3/p4 が監査タブに日本語 2 行で除外されている
  （「1 ページ目と同じ内容のため記帳していません／照合キー…」）
- **F 列取引先の是正**: 取引先欄は店名（`ETC通行料金/N西日本` 等）であり
  商品名ではない

### 資産

`fixtures/full/`（32 slot・1.0 MB）を残した。T8c の回帰はこれを再生するだけでよく、
**Gemini 課金は要らない**。`git ls-files fixtures` が空であることを確認済み。
