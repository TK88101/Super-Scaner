# Plan: IP-401 既存欠陥 — `raw_data` が dict でないとき頁が無音で消える

- 起案: 2026-08-17
- 前提: T4 完了・commit 済（`f502ef2` / `3f8b238`）。ベースライン **770 tests 緑**
- 申し送り元: `docs/plans/2026-08-17-t4-card-prompts-builders.md` §10
  「T4 の作業中に見つけた**既存の欠陥**（趙裁定 2026-08-17: 次 session で修復）」
- 対象: 全 doc_type 共通（T4 が入れた欠陥ではない）

---

## 0. 事実表（コマンド出力で確認済み）

| # | 事実 | 根拠 |
|---|---|---|
| F1 | `extract_json` は JSON **配列**を返しうる | `ocr_engine.py:203-205` の `arr_match` 分岐 |
| F2 | `json.loads` は list / str / int / float / **bool / None** も返しうる。欠陥の条件は「list」ではなく **「dict でない値」**。うち `_yield_page_results` まで到達するのは **truthy な非 dict だけ**（falsy は呼出側の `if not raw_data` に先に捕まる。F17） | `ocr_engine.py:185`（`json.loads(text)` を無条件 return）／ Codex 評審 #1・#4 |
| F3 | `_yield_page_results` の 1 行目が `_apply_ocr_overrides(raw_data, ...)` | `ocr_engine.py:2045` |
| F4 | `_apply_ocr_overrides` は非 dict truthy で `AttributeError`（`raw_data.get(...)`） | `ocr_engine.py:964` の `isinstance` 判定が False → `else` 節 `ocr_engine.py:997` |
| F5 | `_apply_ocr_overrides` の呼出点は F3 の 1 箇所のみ | `grep -n "_apply_ocr_overrides" ocr_engine.py` → 947(def) / 2045(call) |
| F6 | 単頁経路（画像・単頁 PDF）は `_yield_page_results` を**裸の for** で回し、例外は最外 `except Exception: print; return` へ飛ぶ → **0 件 yield** | `ocr_engine.py:2355-2365` |
| F7 | PDF 逐頁ループは `while True: next()` を try で包み、例外を `整形処理エラー` 占位（`_page_error=True`）に閉じ込める | `ocr_engine.py:2288-2300` / `_page_error_payload` `ocr_engine.py:2131-2144` |
| F8 | `_page_error=True` の頁は main が **Sheets に一切書かず** continue する | `main.py:522-526` |
| F9 | 全頁 `_page_error` → `count == error_pages` → **Failed → ファイル保持** | `main.py:632-645` |
| F10 | `count == 0` → `STATUS_PARSE_FAILED` → `return False` → **ファイル保持** | `main.py:704-716` |
| F11 | `return False` の帰結はファイル保持＋失敗退避（最長 1 時間間隔）で**永久に再試行**、歸檔されない | `main.py:1102-1112` / `_record_file_failure` |
| F12 | `_unrecognized=True` の頁は占位行 1 行を Sheets に書いて **歸檔**される（`APPEND_RESULT_PLACEHOLDER`） | `sheets_output.py:314-321` |
| F13 | 占位行の摘要（S列 = `row[18]`）は `_unrecognized` が立っていれば **producer の memo をそのまま通す**。立っていなければ遮断される | `sheets_output.py:757-773` |
| F14 | 占位行には原票 URL（AB列）と赤タグ（U列）が付く | `sheets_output.py:776-778` |
| F15 | 単頁経路が 0 件で終わると `last_total_pages = 0` のままなので、main のカバレッジ哨戒（`range(1, 0+1)` = 空）も**鳴らない** | `main.py:503`, `main.py:614` |
| F16 | 現行の `_yield_page_results` は全ての yield 点で「例外を投げうる処理」が最初の `next()` までに完了している（`_normalize_receipt_results` は list を返し、builder も yield 前に実行）。つまり**今は**「部分 yield 後に例外」は起きない | `ocr_engine.py:2064-2119` |
| F17 | **falsy な非 dict（`[]` / `""` / `0` / `False`）と `None` は `_yield_page_results` に到達しない**。逐頁は `if not page_raw_data` → `AI応答のJSON解析失敗` 占位（`_page_error`）、尾段は Vision 兜底後も falsy なら **`return`（0 件 yield）** | `ocr_engine.py:2251-2259` / `ocr_engine.py:2343-2350`。Codex 評審 #1 の指摘で判明 |
| F18 | `_is_social_insurance_notice` は非 dict の `raw_data` を安全に受ける（`_has_social_insurance_vendor` に `isinstance` 守衛。OCR 文本分岐は raw_data に触れない） | `ocr_engine.py:814-817` |
| F19 | partial_error の集計行は `source_url=base_url`（**ファイル級**の URL）で書かれる。「URL が無い」のではなく「頁を指せない」 | `main.py:663`。Codex 評審 #2 で訂正 |
| F20 | `send_notification` は Chatwork 用の死コード（会社が Chatwork 不採用。token 無しで送信されない）。ファイル終態の "Success/Failed" 文案は**顧客に届かない** | `CLAUDE.md`「Chatwork 已废弃」 |

### F1〜F16 から導かれる現状（推測ではなく上記の合成）

「Gemini が dict でない JSON を返した」とき:

| 経路 | 現在の終態 | 顧客から見えるもの | ファイル |
|---|---|---|---|
| 単頁（画像 / 単頁 PDF）・**truthy 非 dict** | `AttributeError` → 最外 except → 0 件 yield → `count==0` → PARSE_FAILED | **何も無い**（控制台 print のみ。無人 miniPC では誰も見ない） | 永久保持・永久再試行 |
| 単頁・**falsy 非 dict / None**（F17） | Vision 兜底も失敗 → `return` → 0 件 yield → PARSE_FAILED | **何も無い** | 永久保持・永久再試行 |
| PDF 逐頁（全頁が truthy 非 dict） | `整形処理エラー` 占位 → 全頁 error → Failed | **何も無い**（`_page_error` は Sheets に書かれない） | 永久保持・永久再試行 |
| PDF 逐頁（一部の頁が truthy 非 dict） | 該当頁は `_page_error`、他頁は成功 → partial_error | 集計行 1 行（「p3: 整形処理エラー: AttributeError」）。URL は**ファイル級**で該当頁を指せない（F19） | 歸檔 |

**共通の病理**: 「AI が dict でない JSON を返す」は入力と prompt が同じである限り
再試行で確定的に自癒する保証がない。にもかかわらずシステムは永久に再試行し、
その間 Sheets には 1 行も現れない。IP-401 の不変式
「進入した頁は必ず 1 件以上 yield する」の**目的**（顧客が枚数を数えるより先に気づく）が
達成されていない。

---

## 1. 目標

1. **G1**: `raw_data` が dict でない truthy 値のとき、単頁経路・逐頁経路のいずれでも
   頁が 1 件以上 yield する（IP-401 不変式の充足）。
2. **G2**: その頁は Sheets に占位行として現れ、摘要に**原因が読める文言**が入る
   （「AI応答形式不正」＋型名）。原票 URL と赤タグ付き。
3. **G3**: ファイルは歸檔される（永久再試行ループを断つ）。
4. **G4**: 既存 4 doc_type の回帰が無修正で緑（`test_ip401_regression` /
   `test_ocr_engine_envelope` / `test_ocr_engine_social_insurance` /
   `test_main_process_file`）。
5. **G5**（Codex #1 / 趙裁定 P-1 で追加）: **尾段に 0 件 yield で終わる経路を 1 本も残さない**。
   型ゲート（truthy 非 dict）だけでは F17（falsy）と整形例外が残る。尾段の 3 経路
   —— falsy `return` / 整形例外 / 部分 yield 後の例外 —— を逐頁ループと**同形**にする。

## 2. 非目標（やらないこと）

- **`extract_json` の array 分岐は変えない**。ここで `None` を返させると
  `if not raw_data` → Vision 兜底 → 兜底も配列なら単頁経路は `return`（`ocr_engine.py:2348-2350`）で
  結局 0 件 yield に戻る。**問題の位置は「消費側の型前提」であって「抽出側」ではない**。
- **配列を dict へ救済しない**（`arr[0]` を採る / `{"documents": arr}` に包む等）。
  AI の意図の推測であり、多書類頁を無音で 1 件に潰す危険がある。T11 の実呼出で
  実際の応答形状が分かってから検討する事項。
- 再試行・退避（`_record_file_failure`）の機構は 1 行も変えない。
- T5（窓分割リトライ）/ T6（`sheets_output` の `line_mode` ゲート）の範囲には触れない。
- `_page_error` / `_unrecognized` の一般的な意味論の見直しはしない（本件の 1 分類だけを移す）。

---

## 3. 設計

### 3.1 修正の位置 — `_yield_page_results` 入口の型ゲート（必須）

F5 より `_apply_ocr_overrides` の呼出点は 1 箇所しかなく、`_yield_page_results` は
単頁経路と逐頁経路の**共通の整形入口**（T0 で一本化済み）。ここに置けば両経路が
同時に守られ、二重メンテにならない。

```python
def _yield_page_results(doc_type, raw_data, ocr_text, ocr_conf, prefix="",
                        envelope_filter=False):
    # ── IP-401: raw_data の型ゲート ──
    # Gemini が JSON 配列（あるいは文字列・数値）を返すと extract_json は
    # それをそのまま返す。以降の整形は全て dict を前提にしており、
    # _apply_ocr_overrides の raw_data.get() で AttributeError になる。
    # 単頁経路（尾段）はこの例外を最外 except で握り潰して 0 件で終わり、
    # 頁が無音で消えていた（IP-401 不変式違反）。
    if not isinstance(raw_data, dict):
        print(f"{prefix}⚠️ AI応答が dict ではありません（{type(raw_data).__name__}）"
              f" → 認識不能として記録")
        yield _blank_result(_unrecognized=True,
                            memo=f"⚠ AI応答形式不正（{type(raw_data).__name__}）")
        return

    _apply_ocr_overrides(raw_data, ocr_text, prefix)
    ...
```

**なぜ `_unrecognized` で `_page_error` ではないか**（F8/F9/F11/F12 の対比）:

| | `_page_error=True` | `_unrecognized=True` |
|---|---|---|
| Sheets | **書かれない**（main が continue） | 占位行 1 行（摘要・原票 URL・赤タグ） |
| 全頁該当時のファイル | 保持 → 永久再試行 | 歸檔 |
| 適合する失敗の性質 | 一時障害（API 5xx・認証・ネットワーク） | 確定的な認識失敗（封筒・明細ゼロ・形式不正） |

「AI が dict でない JSON を返す」は後者。`_build_doc_result` の docstring
（`ocr_engine.py:1739-1742`）が既に同じ判断基準を明文化している
——「`_page_error` だと Failed → ファイル保持 → 無限ループに入る。占位行を 1 行だけ
書いて歸檔し、赤タグで人手確認を促す」。本件はその適用漏れである。

**memo を渡す理由**: F13 より `_unrecognized` が立っていれば memo は S 列に素通りする。
渡さなければ「⚠ 認識不能ページ」という汎用文言になり、封筒・明細ゼロと区別がつかない。
無人運用では Sheets の 1 行が唯一の診断材料なので、型名まで残す。

**ゲートを社会保険料通知書の判定より前に置く裁決**（Codex 評審 #5 への回答）:

Codex は「OCR 文本が社保通知に強命中していて、かつ Gemini が list を返した」場合、
現行の社保提示行ではなく「AI応答形式不正」行になると指摘した。F18 のとおり
`_is_social_insurance_notice` は非 dict でも安全に動くので、順序は選択可能である。
**それでもゲートを前に置く**:

1. **確定した事実は啓発的判定に優先する**。型は「見た」事実、社保判定は
   キーワードによる**啓発法**。IP-401 T1 が封筒判定を「前置拒否権」から
   「事後説明器」へ降格したのと同じ原理——啓発法に硬い事実を上書きさせない。
2. **帳簿リスクは同一**。どちらの経路も**仕訳を 1 件も作らない**（社保提示行も
   零仕訳）。二重計上の危険は生じない。差は顧客が読む文言だけである。
3. 現状では社保判定は**そもそも到達していない**（`_apply_ocr_overrides` が先に
   `AttributeError` を投げる）。順序を入れ替えるのは現状の復元ではなく新規の挙動追加。
4. ゲートを最前に置けば「`_apply_ocr_overrides` 以降の全コードは dict を仮定してよい」
   という不変式が 1 箇所で保証される。順序を後ろにすると、その保証が
   「社保判定の中だけ非 dict がありうる」という例外つきになる。

なお OCR 本文は占位行の判断材料として失われない（`_ocr_text_len` は
`_excluded_page` 経路専用だが、S 列の型名と原票 URL で人手確認は可能）。

### 3.2 尾段の例外境界の対称化（P-1。**趙裁定 2026-08-17「含める」＝採用**）

F6/F7 の非対称——逐頁ループは `while True: next()` を try で包むが、尾段は裸の for。

**現時点で実害は無い**（F16。全ての例外源が最初の `next()` までに完走する）。
守るのは**将来**である:

- 3.1 のゲートは「非 dict」しか塞がない。builder 内部の未知の例外（T5/T6 が
  `card_entries` に手を入れる。T4 の builder は 652 行）で尾段は依然 0 件になる。
- T5 の窓分割リトライは builder を**流式**に変えうる。そうなった瞬間、
  「1 件目 yield 成功 → 2 件目で例外」が成立し、**count>0 → Success → 歸檔**で
  真の無音データ欠落になる（現在の 0 件 → 保持よりも悪い）。

占位の形状は**新造しない**。逐頁ループが使っている `_page_error_payload` を
そのまま呼ぶ（Codex 評審 #3 の指摘。`{...占位...}` という曖昧な記述を排する）。
これで `_page_error=True` / `_unrecognized=True` / `page_bytes` の 3 契約が
1 箇所の出所から供給され、逐頁と尾段で漂移しない。単頁には分割 PDF が無いので
`page_bytes=None`（main は `page.get("page_bytes")` → None → base_url へ劣化。
既存の劣化経路であり新しい分岐ではない）。

```python
        page_iter = _yield_page_results(page_ocr.actual_doc_type, raw_data,
                                        ocr_text, ocr_conf)
        while True:
            try:
                entry = next(page_iter)
            except StopIteration:
                break
            except Exception as fmt_err:
                print(f"❌ 整形処理エラー: "
                      f"{type(fmt_err).__name__}: {str(fmt_err)[:120]}")
                yield _page_error_payload(
                    f"整形処理エラー: {type(fmt_err).__name__}", 1, 1, None)
                break
            yield {"result": entry, "page_num": 1, "total_pages": 1}
```

コストは約 10 行。逐頁ループ（`ocr_engine.py:2288-2306`）と**同形**になる。

**終態は変わらない**（重要）: 「最初の `next()` で例外」なら 1 件 `_page_error` →
`count==1==error_pages` → Failed → ファイル保持。現状（0 件 → `count==0` →
PARSE_FAILED → ファイル保持）と**同じ終態**である。変わるのは
IP-401 不変式が満たされること・カバレッジ哨戒が機能すること
（現状は `last_total_pages=0` で `range(1,1)` が空になり鳴らない。F15）の 2 点。

「部分 yield 後に例外」（将来 builder が流式化したとき）は成功分＋占位行の両方が
出るため partial_error として可視化される —— 現状のように Success 歸檔で
無音に消えることがなくなる。

### 3.3 尾段 falsy 分岐の同形化（Codex 評審 #1 への対応）

F17 の残り半分。尾段は Vision 兜底の後も falsy なら `return` して 0 件で終わる。

```python
        if not raw_data:
            print("⚠️ AIの応答がJSONではありませんでした")
            yield _page_error_payload("AI応答のJSON解析失敗", 1, 1, None)
            return
```

逐頁ループの同じ状況（`ocr_engine.py:2251-2259`）と**逐字で同じ分類**にする。

**なぜここは `_page_error`（＝保持・再試行）で、§3.1 は `_unrecognized`（＝歸檔）なのか**:

| | 観測できる事実 | 一時障害の可能性 | 分類 |
|---|---|---|---|
| §3.1 truthy 非 dict | AI は**応答した**。JSON も**解析できた**。型だけが契約違反 | 低（同一入力・同一 prompt で構造が変わる保証は無い） | `_unrecognized` → 歸檔 |
| §3.3 falsy / None | AI から**使える応答が無い**（空・解析失敗・API 沈黙） | **高**（5xx・タイムアウト・レート制限で普通に起きる） | `_page_error` → 保持・再試行 |

この境界は本 Plan の発明ではなく、逐頁ループが既に採っている分類そのものである。
尾段だけがその分類の**外**（0 件 yield）に落ちていた。

**`if not raw_data` の判定式自体は変えない**（Codex #1 の後半への回答）。
Codex は `None`（解析失敗）と `[]`/`""`/`0`/`False`（解析成功だがスキーマ不正）を
区別せよと提案したが、**区別しても行動が 1 つも変わらない**:
どちらも「使える内容がゼロ」であり、Vision 兜底 → それでも駄目なら再試行、が唯一の
合理的対応である。分岐だけ増えて終態が同じ変更は入れない（YAGNI）。
テストでは `[]` / `""` / `0` / `False` / `None` を明示的に通し、
**この分類が意図的であることを固定する**（Codex の「至少證明決策是有意的」に応じる）。

### 3.4 影響を受ける終態の変化（回帰の可視化）

| # | シナリオ | 現在 | 改後 |
|---|---|---|---|
| 1 | 単頁・**truthy 非 dict** | 0 件 / PARSE_FAILED / 保持 | 1 件 `_unrecognized` / Success / **歸檔** ＋ 占位行 |
| 2 | 逐頁・全頁が truthy 非 dict | `_page_error`×N / Failed / 保持 | `_unrecognized`×N / Success / **歸檔** ＋ 占位行×N |
| 3 | 逐頁・一部が truthy 非 dict | 該当頁 `_page_error` → partial_error 集計行 | 該当頁が自分の占位行を持つ。**partial_error 集計行は出なくなる**（error_pages に数えないため） |
| 4 | 単頁・**falsy / None**（§3.3） | 0 件 / PARSE_FAILED / 保持 | 1 件 `_page_error` / Failed / 保持。**終態不変**。IP-401 不変式とカバレッジ哨戒が効くようになるだけ |
| 5 | 単頁・**整形例外**（§3.2） | 0 件 / PARSE_FAILED / 保持 | 1 件 `_page_error` / Failed / 保持。**終態不変** |

行 3 は「集計行が消える」という**情報の減少**に見えるが、実際は逆である——
現在の集計行は「p3: 整形処理エラー: AttributeError」という 1 行に潰れており、
URL は `base_url`（**ファイル級**）なので該当頁を直接指せない（F19）。
改後は該当頁自身が `PageUrlResolver` 経由の**頁級 URL** 付き占位行を持つ。
（Codex 評審 #2 による訂正——「URL が無い」は誇張だった。「頁を指せない」が正確。）

**変化するのは行 1〜3 だけ**で、いずれも「Sheets に何も出ない・永久再試行」から
「占位行が出る・歸檔」への移行である。行 4〜5 は不変式の充足のみで終態は動かない。

---

## 4. タスク清単（TDD。各項に DoD）

### T-a: 再現テスト（RED を先に見る）

新規 `test_ip401_nondict_rawdata.py`。実 API は呼ばない（既存 fixture 方式を踏襲）。

1. `test_single_page_nondict_yields_placeholder` — 画像経路で `raw_data=["bad"]`
   → `process_pipeline` が **1 件以上**を yield し、`result["_unrecognized"] is True`
2. `test_single_page_nondict_is_not_page_error` — 同上で `_page_error` が立たない
   （＝歸檔される側に落ちる）
3. `test_paged_pdf_nondict_yields_unrecognized_not_page_error` — 逐頁経路で
   2 頁中 1 頁が list → 2 頁とも出力され、該当頁は `_unrecognized` かつ `_page_error` 無し
4. `test_nondict_scalar_types_are_gated` — `"文字列"` / `123` / `True` でも同様
   （F2。list 決め打ちにしない）
5. `test_placeholder_memo_names_the_type` — memo に型名（`list` 等）が含まれる
6. `test_dict_rawdata_path_is_unchanged` — 正常 dict では 1 件も余分に yield しない
7. `test_social_insurance_ocr_with_nondict_rawdata_reports_format_error` —
   §3.1 の裁決（形式不正優先）を**明示的に固定する**。OCR が社保通知に強命中
   していても raw_data が list なら「AI応答形式不正」占位になる。
   意図した選択であってバグではないことを、この 1 件で後続の評審者に伝える

**DoD**: 実装前に 1〜5 が FAIL（1 は「0 件」、3 は `_page_error=True` で落ちる）。
6 は実装前も PASS（無回帰の錨）。7 は実装後の裁決固定（実装前は
`AttributeError` で FAIL）。

### T-b: 型ゲート実装（GREEN）

`ocr_engine._yield_page_results` の冒頭に §3.1 のゲートを追加。

**DoD**: T-a 全緑 ／ `ocr_engine.py` の他の行を変更しない ／
`_apply_ocr_overrides` 自体は無改変（呼出前に弾く方が防御位置として上流）。

### T-c: main 側の終態テスト

`test_main_process_file.py` へ追加（既存クラス構成に合わせる）。

1. `test_nondict_single_page_is_archived_not_retained` — `process_file` が **True** を返す
   （＝歸檔）。現在は False（保持）
2. `test_nondict_page_writes_placeholder_row` — `append_entries` が呼ばれ、
   `entries_data["_unrecognized"]` が True、memo に「AI応答形式不正」を含む
3. `test_nondict_page_emits_placeholder_outcome` — 進捗が `OUTCOME_PLACEHOLDER`

**DoD**: 3 件緑 ／ 既存 `test_count_zero_emits_parse_failed` 等が無修正で緑。

### T-d: 尾段の例外境界（P-1。**趙裁定「含める」で確定**）

`ocr_engine.py:2355-2361` の裸 for を §3.2 の `while True: next()` へ。

1. `test_tail_formatting_exception_does_not_swallow_the_page` —
   `_yield_page_results` を「例外を投げる fake」に差し替え、単頁経路が
   **1 件以上**を yield し、`_page_error=True` である
2. `test_tail_partial_yield_then_exception_is_visible` —
   「1 件 yield 後に例外」の fake で、成功分＋占位行の**両方**が出る
3. `test_tail_placeholder_shape_matches_paged_loop` — 尾段の占位 result が
   逐頁ループの占位 result と**同一の键集合**（`_page_error` / `_unrecognized`）
   を持つ。片方だけ直す漂移を機械的に禁じる
4. `test_tail_first_next_exception_is_failed_retained`（main 側）—
   `process_file` が **False**（ファイル保持）を返す。§3.4 行 5 の「終態不変」を固定
5. `test_tail_partial_yield_exception_is_partial_error`（main 側）—
   成功 1 件＋占位 1 件 → `partial_error` 経路（歸檔＋集計行）に入る。
   将来 builder が流式化したとき無音欠落にならないことの錨

**DoD**: 5 件緑 ／ 3 が逐頁と尾段の対称性を機械判定していること。

### T-f: 尾段 falsy 分岐の同形化（Codex #1）

`ocr_engine.py:2348-2350` の `return` を §3.3 の占位 yield へ。

1. `test_tail_falsy_rawdata_yields_page_error` — Vision 兜底後も `None` なら
   1 件 `_page_error` を yield（現状は 0 件）
2. `test_tail_falsy_variants_all_yield` — `[]` / `""` / `0` / `False` / `None` の
   5 種すべてで 1 件以上（F2/F17。**分類が意図的であることの証明**）
3. `test_tail_falsy_is_retained_not_archived`（main 側）— `process_file` が False
   （§3.4 行 4 の「終態不変」を固定）

**DoD**: 3 件緑 ／ 逐頁ループの `AI応答のJSON解析失敗` と同じ memo 文言。

### T-e: 回帰と変異検証

**DoD**:
- `venv311/bin/python -m unittest discover -p "test_*.py"` → OK、テスト数 ≥ 770 ＋ 新規
- **変異検証（3 箇所すべて）**: 実装を 1 つずつ元へ戻して、対応するテストが
  実際に FAIL することを**実測**する。3 箇所とも独立に確認する:
  | 戻す実装 | FAIL すべきテスト |
  |---|---|
  | T-b の型ゲート | T-a 1〜5・7 |
  | T-d の `while/next` | T-d 1〜5 |
  | T-f の falsy 占位 | T-f 1〜3 |
  （`green-tests-hide-env-dependent-breakage` の教訓——「緑」は「壊していない」の
  証明ではない。番人が本当に見張っているかを個別に確かめる。3 つまとめて戻すと
  「1 つのテストが 3 つの実装のどれかに反応しているだけ」を見逃す）
- ~~venv 無し環境でも新規テストが走る~~ —— **この DoD は取り下げる**（実測により
  成立しないと判明）。新規テストは `ocr_engine.process_pipeline` と
  `main.process_file` を実際に駆動するので `paddleocr` / `gspread` を要求し、
  venv 無しでは import 段階で落ちる。これは既存の 17 モジュール
  （`test_ip401_regression` / `test_main_process_file` / `test_ocr_engine_*` /
  `test_sheets_output` 等）と**同じ性質**であり、CLAUDE.md も
  「`sheets_output` / `ocr_engine` に関わるテストは venv311 で走らせる」と
  既に明記している。純関数モジュール（`card_prompts` / `card_entries` 等）
  だから venv 無しで走った T4 とは対象が違う。
  実測: 変更前 448 tests / 17 errors → 変更後 449 / 18（増分は新規ファイル
  1 モジュール分の `_FailedTest` のみ。既存の緑を 1 件も壊していない）

---

## 5. 受入基準（脚本判定）

```bash
cd "/Users/ibridgezhao/Documents/Super Scaner"
venv311/bin/python -m unittest discover -p "test_*.py"   # → OK, Ran >= 776
venv311/bin/python -m unittest test_ip401_nondict_rawdata -v   # → OK
venv311/bin/python -m unittest test_ip401_regression -v        # → OK（無修正）
venv311/bin/python -m unittest test_main_process_file -v       # → OK
```

追加の人手判定（脚本化不可）:
- `git diff` が `ocr_engine.py` の **3 箇所**（型ゲート／尾段 while-next／尾段 falsy 占位）に
  収まっていること。`main.py` / `sheets_output.py` / `card_*.py` に 1 行も触れていないこと。

---

## 6. テスト戦略

- **単元**: 型ゲートの分岐（dict / list / str / int / bool）
- **集成**: `process_pipeline` を単頁・逐頁の両経路で通す。既存の fixture 方式を
  再利用し**新しい mock 方式を作らない**:
  - 逐頁: `test_ip401_regression._run_pipeline` と同形
    （`_split_pdf_pages` ＋ `_route_ocr_strategy` ＋ `_call_gemini_bytes` を patch）
  - 尾段: `test_ocr_engine_invoice._run_single_page_pipeline` と同形
    （`_split_pdf_pages` を空 iter、`_route_ocr_strategy`、`_call_gemini` を patch。
    `.jpg` の一時ファイルで mime を image に落とす）
  - 変換は `ocr_test_helpers.page_ocr_from_tuple` / `page_ocrs_from_tuples` を使う
- **E2E 相当**: `main.process_file` の終態（戻り値・Sheets 呼出・progress）を
  `test_main_process_file.py` の既存クラス構成に合わせて追加
- 実 Gemini は呼ばない（`test_ip401_regression` の Codex 低2 裁決を踏襲）
- **カバレッジ**: 新規 3 箇所はいずれも分岐 1 本なので、上記で 100% 到達する

## 7. 影響面

| 対象 | 影響 |
|---|---|
| `ocr_engine._yield_page_results` | 冒頭に型ゲート 8 行 |
| `ocr_engine.process_pipeline` 尾段 | 裸 for → while/next（約 10 行）＋ falsy 分岐に占位 yield 1 行 |
| 単頁経路の終態（truthy 非 dict） | PARSE_FAILED（保持）→ Success（歸檔）＋ 占位行 |
| 単頁経路の終態（falsy・整形例外） | **不変**（保持・再試行）。IP-401 不変式とカバレッジ哨戒が効くようになる |
| 逐頁経路の終態（truthy 非 dict） | `_page_error`（Sheets 無痕）→ `_unrecognized`（占位行） |
| `main.py` | **無改変**（`_unrecognized` の既存経路に乗るだけ） |
| `sheets_output.py` | **無改変**（`_write_unrecognized_row` の既存経路） |
| 既存 4 doc_type | 正常 dict では 1 行も挙動が変わらない |
| クレカ / nimoca | 同じゲートを通る（.env 未配なので到達不能だが、T9 接線後に効く） |

## 8. 風険と回退

| # | 風険 | 評価 | 緩和 |
|---|---|---|---|
| R1 | 一時的な形式不正だった場合、再試行で救えたはずの票が歸檔される | 現状は**永久に再試行して永久に救えない**（F11）。かつ Sheets に何も出ない。歸檔＋占位行の方が厳密に情報量が多い | 占位行に原票 URL・赤タグ・型名。顧客は再アップロード可能 |
| R2 | 逐頁一部該当時に partial_error 集計行が出なくなる | 該当頁自身が原票 URL 付き占位行を持つので情報は増える（§3.3） | テストで新旧両方の形を固定 |
| R3 | `_unrecognized` 占位行が取引No を消費する | 既存の全 `_unrecognized` 経路と同じ。本件だけの新問題ではない | 非目標として明記 |
| R4 | P-1 を入れると尾段の diff が増える | 逐頁と同形にするだけ。テストで両者の対称性を固定（T-d 3） | **趙裁定 2026-08-17「含める」で解決** |
| R5 | 全頁 truthy 非 dict のファイルが「Success / STATUS_COMPLETED」で終わる（Codex #6） | 文案上の希釈は起きるが、**その文案は誰にも届かない** —— `send_notification` は Chatwork 用の死コード（F20）。顧客が実際に見るのは Sheets の赤タグ占位行だけであり、そこには型名も原票 URL も載る | `main.py` は変更しない。真の警示は頁毎の `OUTCOME_PLACEHOLDER` と Sheets 赤タグに依存する、と本 Plan に明記（Codex の要求どおり） |
| R6 | 型ゲートが社保通知判定より前に立つ（Codex #5） | 両経路とも**零仕訳**なので帳簿リスクは同一。差は文言のみ | §3.1 の裁決 4 点。T-a 7 でこの選択をテストに固定 |

**回退**: 単一 commit。`git revert` で完全に戻る。`main.py` / `sheets_output.py` を
触らないので、回退時に他機能を巻き込まない。

---

## 9. 趙の裁定（2026-08-17）

**P-1（§3.2 尾段の例外境界を範囲に含めるか）→「含める」＝採用。**
これにより §1 に G5 を追加し、T-d を条件付きから確定タスクへ昇格した。
Codex も独立に「採用すべき」と判定（評審 #3）。

**終態語義（truthy 非 dict を歸檔にするか保持にするか）→ 趙「Codex に訊け」。**
Codex の回答: **`_unrecognized`（歸檔＋占位行）を支持**。根拠として
`main.py:522-526`（`_page_error` は Sheets に書かれない）と
`sheets_output.py:310-321`（`_unrecognized` は占位行を書いて歸檔）を自ら確認した上で
「§3.1 の `_unrecognized` 判斷是合理的」と明記。起案者の判断と一致。
→ **§3.1 のまま確定**。ただし Codex #1 の指摘により、falsy 側は
`_page_error`（保持）のままに**据え置く**（§3.3 の分類表）——
「AI が応答しなかった」と「AI の応答が型違反」は再試行可能性が違う。

---

## 附録 A: Codex 評審の辯論記録（2026-08-17）

6 件（P1 ×3 / P2 ×3）。**4 件採用・2 件部分採用・0 件反駁**。

### 採用（4 件）

| # | severity | 指摘 | 反映先 |
|---|---|---|---|
| 2 | P1 | 「partial_error 集計行は原票 URL を持たない」は不正確。`main.py:663` が `source_url=base_url` を渡している | F19 新設 ／ §3.4 を「URL が無い」→「**頁を指せない**」へ訂正。**誇張を削った** |
| 3 | P1 | P-1 の占位を `{...占位...}` と曖昧に書くな。`_page_error_payload` を再利用し `_page_error` / `_unrecognized` を明示せよ。main 側の終態もテストせよ | §3.2 を書き直し ／ T-d 3・4・5 を追加 |
| 4 | P2 | F2 が不完全。`json.loads` は bool / None も返す | F2 訂正 ／ T-f 2 で 5 種を通す |
| 6 | P2 | 改後は全頁 non-dict でもファイル終態が Success になる。Plan の風険に明記せよ | R5 新設。あわせて **F20**（`send_notification` は Chatwork 死コードで誰にも届かない）を事実表に追加し、希釈の実害がゼロであることを示した |

### 部分採用（2 件）

**#1（P1）「falsy 非 dict がゲートに到達しない」**

前半は**完全に正しく、起案者の見落とし**だった。F17 として事実表に追加し、
§3.3（尾段 falsy 分岐）と T-f を新設した。ここは Codex の勝ち。

後半の「`None` と `[]`/`0`/`False`/`""` を区別できるよう呼出側の判定を変えよ」は
**採らない**。区別しても**行動が 1 つも変わらない**からである——どちらも
「使える内容がゼロ」であり、Vision 兜底 → 駄目なら再試行、が唯一の対応。
分岐だけ増えて終態が同じ変更は入れない。ただし Codex の「至少證明決策是有意的」
という要求には応じ、T-f 2 で 5 種すべてを明示的に通すテストを置く
（＝この分類が惰性ではなく選択であることを機械可読な形で残す）。

**#5（P2）「型ゲートが社保通知判定より前に立つ」**

事実指摘は正しい（F18 で `_is_social_insurance_notice` の非 dict 安全性も確認）。
ただし Codex が挙げた 2 案のうち「順序を入れ替える」は採らず、
「**形式不正を優先すると明記する**」方を選んだ。理由 4 点は §3.1 に記載。
要点は (a) 確定した事実は啓発法に優先する（IP-401 T1 と同じ原理）、
(b) 両経路とも零仕訳で帳簿リスクは同一、(c) 現状では社保判定はそもそも到達して
いないので入れ替えは復元ではなく新規挙動、(d) ゲートを最前に置くことで
「以降のコードは dict を仮定してよい」という不変式が 1 箇所で保証される。
T-a 7 でこの選択をテストに固定し、後続の評審者が同じ論点を蒸し返さないようにする。

### 反駁（0 件）

今回は事実誤認による指摘が無かった。#1 前半は起案者の見落としであり、
Plan の範囲が 1 タスク（T-f）増えた。

### 複審 1 往復（部分採用 2 件を回し戻した結果）

fatboyslim の規律に従い、採らなかった部分（#1 後半・#5）を Codex へ差し戻した。
**判定は 2 件とも「起案者側が成立」で、Codex は再提起しなかった**（＝維持）。

| 争点 | Codex の複審回答 |
|---|---|
| #1 後半（`None` と falsy を区別せよ） | 「找不到一個會導致『不同正確行動』的具體場景。……區分後最多改善診斷文言，不改終態或處理行動。用 T-f 固定這是刻意分類，足夠」 |
| #5（型ゲートを社保判定より後ろへ） | 「§3.1 的 4 點沒有看到事實錯誤；尤其『兩經路都零仕訳、帳簿風險同一』成立」。証拠として `_blank_result` の `entries: []`（`ocr_engine.py:1996`）、社保経路も `_blank_result`（`:2053`）、`_build_unrecognized_placeholder` も `entries: []`（`main.py:338`）、`_write_unrecognized_row` は金額・科目を書かない（`sheets_output.py:748`）を自ら確認。「差異只在提示語／進捗分類／除外統計，不在仕訳生成」 |

**Plan 定稿**（2026-08-17）。以降の実装はこの版に従う。

---

## 附録 B: 実施記録（2026-08-17）

### 実装（`ocr_engine.py` 4 箇所）

当初 Plan は 3 箇所。simcodex で **4 箇所目**が必要と判明し、その範囲が
Round 2 でさらに広がった（下記 A1 / Z2）。

1. `_yield_page_results` 冒頭の型ゲート（§3.1）
2. 尾段の `while True: next()` 例外境界（§3.2）
3. 尾段 falsy 分岐の `_page_error_payload` yield（§3.3）
4. **尾段の「ファイル読取 ＋ `_route_ocr_strategy` ＋ Vision 兜底」を
   per-page try で包む**（新規。R1 の A1 ＋ R2 の Z2）

4 について、当初は `_call_gemini` だけを対象にし、`open()` は
「逐頁も最外 try なので対称」と判断していた。**これは誤り**だった ——
逐頁側の `open()` は `_split_pdf_pages` が**自前の try** で包んで優雅に
降格させている（`ocr_engine.py:421-442`）ので、守られていないのは尾段だけ
だった。Round 2 の zero-yield 監査で指摘され、`open()` も try の内側へ移した。

### テスト

新規 `test_ip401_nondict_rawdata.py`（20 件）。`ocr_test_helpers.pdf_pages` を新設。
`main.py` / `sheets_output.py` / `card_*.py` は 1 行も触っていない。

### 変異検証の実測（T-e の DoD）

4 箇所を 1 つずつ元に戻し、対応するテストが実際に落ちることを確認した:

| 戻した実装 | FAIL したテスト |
|---|---|
| 型ゲート | 9 件（分類・memo・社保優先・main 側終態 3 件） |
| 尾段 `while/next` | 5 件（うち main 側終態 2 件） |
| 尾段 falsy 占位 | 3 件 |
| 尾段 route+fallback の try | 1 件（`test_tail_vision_fallback_exception_does_not_swallow_the_page`） |
| 尾段 `open()` を try の外へ戻す | 1 件（`test_tail_file_read_failure_does_not_swallow_the_page`） |
| 共有 `_page_error_payload` から `_unrecognized` 键を削除 | 1 件（`test_tail_placeholder_shape_matches_paged_loop`。Round 2 で強化した後） |

**変異検証が実際に欠陥を見つけた 2 件**（緑のままだったテストを強化した）:

1. `_unrecognized` だけを見ていた 2 件は、型ゲートを削除しても緑だった ——
   例外が尾段の境界に捕まり、その占位も `_unrecognized=True` を含むため。
   `assertFalse(_page_error)` を追加して初めて噛むようになった。
2. main 側の終態テスト 2 件は占位 dict を**手書き**していたため、尾段を裸 for に
   戻しても緑だった。**尾段の実出力を `process_file` へ流す**形に変えて噛ませた。

**Round 2 でさらに 1 件**（テストの牙の監査で発見）:

3. `test_tail_placeholder_shape_matches_paged_loop` は両経路の占位 dict の
   **键集合を相互比較**していたが、両者は同じ `_page_error_payload` を呼ぶので
   **その共有 helper 自体が壊れると両側が同じように壊れて集合は一致したまま**
   になる。相互比較で捕まえられるのは*非対称*な壊れ方だけで、*対称*な腐蝕は
   素通りする。契約を字面で押さえる assert を追加して噛ませた
   （変異検証: helper から `_unrecognized` 键を消すと赤になる）。

これは [[green-tests-hide-env-dependent-breakage]] の教訓の再演である ——
「全緑」は「壊していない」の証明ではない。3 件に共通するのは
**「テストが守っていると主張する対象」と「実際に守っている対象」のずれ**で、
いずれも変異検証（実装を 1 つずつ戻す）でしか露見しなかった。

### simcodex Round 1 の指摘と裁決

**codex review: 指摘ゼロ**（"The new handling paths appear consistent with the
existing page-error and unrecognized-page semantics."）

4 観点エージェント:

| # | 観点 | severity | 指摘 | 裁決 |
|---|---|---|---|---|
| A1 | altitude | **P1** | 尾段の Vision 兜底 `_call_gemini`（`ocr_engine.py:2371`）が per-page try の**外**。`_generate_content_with_retry` は `raise last_err` で例外を上げるので、再試行を使い切ると最外 except へ飛び **0 件 yield**。逐頁ループの同じ呼出は try の中に在る | **採用・修正済**（実装 4）。G5 の漏れであり範囲拡張ではない。事実確認: `_call_gemini` → `_call_gemini_bytes` → `_generate_content_with_retry:130` に try 無し |
| A2 | altitude | P2 | 逐頁と尾段は「1 頁を処理して ≥1 件 yield する」の**独立した 2 実装**。今回の対称化は共有ではなく**複写**で行われた。A1 はその複写漏れの実例であり、共有 generator へ寄せれば構造的に不可能になる | **見送り（申し送りへ）**。§11 に記載。今回の範囲（3〜4 点修正）を超え、`process_pipeline` の骨格改造になる |
| S1 | simplification | P2 | `test_single_page_nondict_is_not_page_error` が `test_single_page_nondict_yields_placeholder` の厳密な部分集合 | **採用**。削除し、理由は残す方の assert のコメントへ移した |
| S2 | simplification | P2 | `boom` / `half` スタブが 3 箇所・2 箇所に複写（既に片方だけコメントが付く漂移が発生していた） | **採用**。`_boom` / `_half` としてモジュール級へ |
| S3 | simplification | P2 | `ProcessFileTerminalStateTest` の arrange 3 箇所が同一 | **採用**。`_nondict_pages()` へ |
| S4 | simplification | P3 | 尾段の例外境界コメントが逐頁の同型コメントと 6 行重複 | **部分採用**。将来の流式化リスクという新規の点は残し、重複部分は圧縮 |
| R1 | reuse | P2 | `_pdf_pages` が `test_ip401_regression.py:54-56` と逐字同一 | **採用（片側）**。`ocr_test_helpers.pdf_pages` を新設し新ファイルはそれを使う。既存 `test_ip401_regression.py` は G4「無修正で緑」のため触らない |
| R2 | reuse | P2 | runner の重複。しかも docstring の理由（「テスト間 import は discover が絡む」）が**誤り** —— `test_ocr_engine_invoice.py:57` が既にやっている | **部分採用**。汎用化は 3 つの既存テスト改造が要るので見送り。ただし**誤った理由は書き直した**（間違った理由は理由が無いより悪い。後から読む人が「その道は塞がっている」と誤解する） |
| R3 | reuse | P2 | `_RecordingWriter` が 3 つ目の fake 実装 | **部分採用**。動的戻り値が必要な理由（1 実行内に POSTED と PLACEHOLDER が混在する）を docstring に明記。統一は範囲外 |
| E1 | efficiency | P2 | 実 `process_pipeline` を呼ぶので `gc.collect()` が 1 頁ごとに走り、テストプロセスでは ~210k tracked objects のため 20-25ms/回。本ファイルで ~450ms（全 790 件 3.2s の約 15%） | **見送り**。(a) 3.2s→2.8s は開発ループ上で無差別、(b) 既存の同族 `test_ip401_regression` が同じ設計でより悪く（63ms/件）、片方だけ直すと非対称、(c) GC は低メモリ設計の一部で、実経路を通す価値がある |
| E2 | efficiency | — | `ocr_engine.py` は clean。`page_bytes=None` は零コスト（`PageUrlResolver.resolve` が `total_pages <= 1` で早期 return するため `page_bytes` を見ない） | 事実確認として記録 |

### simcodex Round 2 の指摘と裁決

**codex review**（残存 zero-yield 経路の探索を指示）:

| # | severity | 指摘 | 裁決 |
|---|---|---|---|
| C1 | P1 | `_yield_page_results` が例外なしで 0 件返すとき、両経路とも占位を出さない。具体経路として `ENTRY_BUILDERS` 未登録を提示 | **見送り**。具体経路は**到達不能**（実測: 3 表の键集合が完全一致 ＋ import 時 `_validate_doc_type_registries` が起動不能にする）。一般論は既存裁定 §8-中7「警告のみ・P2 繰延」であり、既存テストが 0 件 yield を**積極的に assert** している（`test_ip401_regression.py:168`）ので、覆すと G4 に反する。**複審で Codex は「当方成立・反論なし」と認め、具体経路を撤回**。→ §11.2 |
| C2 | P2 | 「例外なしの空出力」を pin するテストが無い | 行動を変えない以上テストも足さない。現行挙動は既存テストが pin 済み |

**zero-yield 監査エージェント**（`process_pipeline` の残存 0 件経路を網羅探索）:

| # | severity | 指摘 | 裁決 |
|---|---|---|---|
| Z1 | P1(既存) | `_split_pdf_pages` の**中途失敗**で後続頁が消え、しかもファイルは歸檔される | **本件の範囲外・趙の拍板事項**。機序を実測確認のうえ §11.0 へ |
| Z2 | P1 | 尾段の `open()` が try の外。逐頁側は `_split_pdf_pages` が自前 try で守っている（＝非対称）。無人 Windows ミニ PC ではウイルス対策の一時ロックで現実に起きる | **採用・修正済**（実装 4 に統合）。G5 の字面の漏れ |
| Z3 | P2(既存) | pypdf が最初から開けないと多頁 PDF 全体が尾段の 1 回呼出に落ち、MAX_TOKENS 事故が再現しうる | **範囲外**。§11.1b へ |
| — | — | `ENTRY_BUILDERS` 未登録 / `GeneratorExit` / `start_page` skip は到達不能または設計どおりと確認 | 事実確認として記録 |

**テストの牙の監査エージェント**（20 件が本当に噛むかの変異観点監査）:

| # | 指摘 | 裁決 |
|---|---|---|
| T1 | `test_tail_placeholder_shape_matches_paged_loop` は共有 helper の**対称な**腐蝕に盲 | **採用・修正済**（上記「Round 2 でさらに 1 件」） |
| T2 | `test_tail_falsy_variants_all_yield` の 5 値のうち 3 つは mutation-kill 上冗長（型で分岐する箇所が無いため） | **保留**。この test の目的は kill-power ではなく「この分類が惰性でなく選択であること」の固定（Codex R1 #1 後半への回答）。監査者自身も「defect ではない」と明記 |
| — | 残り 17 件は「主張どおりの変異を実際に殺す」と確認 | 記録 |

### simcodex Round 3（最終確認）

Round 2 で 2 箇所（尾段 `open()` の try 内移動 / test 10 の契約 assert 追加）を
変更したため、**その変更自体が新しい問題を作っていないか**だけを検査。

Codex: **P0/P1/P2 いずれも無し**。4 点を逐一確認 ——
(1) `file_data` / `page_ocr` / `ocr_conf` に新たな未束縛使用は無い、
(2) 以前伝播していた例外を新たに隠してはいない（元々最外 except が飲んでいた
ものが明示的な page-error yield に変わっただけ）、
(3) `except` 内の `yield` に `.close()` が来ても `GeneratorExit` は
`except Exception` に捕まらない、
(4) `page_iter` の break 経路に新しいリークは無い（整形例外時点で
`_yield_page_results` は既に終了している）。

これは起案者側の自己点検と一致した。**early exit 条件成立**
（simplify 側の新規 P0/P1 ゼロ ＋ codex ゼロ ＋ verify 全緑）。

> 注: Round 3 の 1 回目は **0 バイトで exit 0** という異常終了だった。
> 「指摘なし」と読み替えず、再実行して結果を得ている（`codex` の沈黙を
> 合格と解釈しない —— fatboyslim の「降級＋明示、静默させない」に従う）。

---

## 附録 C: 最終検証（2026-08-17）

```
venv311/bin/python -m unittest discover -p "test_*.py"
→ Ran 791 tests ... OK      （ベースライン 770 / 新規 21）
```

**変更行のカバレッジ**: `ocr_engine.py` 全体は 74%（既存水準・本件で低下せず）。
未到達行 `2142-2143` / `2203-2204` / `2251` / `2268-2275` / `2437-2439` は
すべて**本件で触っていない既存領域**（順に: 到達不能な `ENTRY_BUILDERS` 未登録
分岐、逐頁ループ既存部、`--start-page`（`local_test.py` 専用）、逐頁ループ既存部、
最外 except）。**本件の 4 箇所は 100% 到達**している。

**変更ファイル**: `ocr_engine.py`（+104/-15）/ `ocr_test_helpers.py`（+15）/
`test_ip401_nondict_rawdata.py`（新規）/ 本 Plan。
`main.py` / `sheets_output.py` / `card_*.py` / 既存テストは**1 行も変更なし**。

---

## 11. 後続タスクへの申し送り（本件で見つけたが範囲外としたもの）

**逐頁ループと尾段を 1 つの generator へ寄せる**（simcodex R1・A2。**P2**）

`process_pipeline` は「1 頁を処理して必ず 1 件以上 yield する」という同じ仕事を
2 箇所で独立に実装している。両者の差は本質的にはループ簿記（`_mark` /
`seen_pages` / `failed_pages` の有無）と N=1 か N=多いかだけで、これは
**呼出側の関心事**であって頁処理本体が 2 実装ある理由にはならない。

本件の A1（尾段の Vision 兜底が try の外）は、その複写が生む欠陥の実例である。
「同形にする」を**複写**で達成した以上、次に片方だけ手が入れば再び乖離する。
`test_tail_placeholder_shape_matches_paged_loop` は占位 dict の**键集合**しか
見ないので、この種の乖離（何が占位を発火させるか）は捕まえられない ——
実際 A1 はこのテストを素通りして見つかった。

共有 generator（例: `_process_one_page(page_data, mime_type, doc_type,
ocr_strategy, page_num, total_pages, prefix, envelope_filter)`）へ寄せれば、
この一族の欠陥が構造的に生まれなくなる。

**なぜ今やらないか**: `process_pipeline` の骨格改造であり、本件（IP-401 の
1 欠陥修復）の範囲を大きく超える。**T5 の直前に判断するのが自然**である ——
T5 は窓分割リトライで builder に手を入れるので、そのとき 2 箇所を同期させる
コストを実際に払うことになり、統合の損得がその場で見える。

なお Codex との辯論で扱ったのは「非対称のまま放置 vs 複写で対称化」（P-1）で
あり、「複写 vs 共有 generator」は**論点に上がっていなかった**（却下されたの
ではなく検討されなかった）。

### 11.0 【**趙の判断を仰ぐ**】`_split_pdf_pages` の中途失敗で後続頁が消える（P1・既存）

simcodex Round 2 の zero-yield 監査で発見。**本件が入れた欠陥ではない**が、
IP-401 の同族でありながら本 Plan の 3 経路のいずれにも当たらない。

**機序**（コード実測で確認済み）:

- `_split_pdf_pages`（`ocr_engine.py:415-442`）は `try` が **for ループ全体**を
  包んでおり、`total_pages` は**ループ前に一度**算出される（`:426`）
- pypdf の `reader.pages` は遅延解決で、特定頁の内容ストリームはその頁に
  触れて初めて解析される。**k 頁目まで成功して k+1 頁目で `PdfReadError`**
  という壊れ方が現実に起きる（スキャナ/プリンタのやや非準拠な PDF 出力）
- 例外はジェネレータ**内部**で握られ、`print` して `return` する。消費側の
  `process_pipeline` からは「PDF が k 頁しか無かった」と**区別できない**
- 頁 k+1..total は per-page try に**一度も入らない**ので `_page_error_payload`
  が作られず、`seen_pages` にも載らない

**帰結**（`main.py` で確認）:

| | 状態 |
|---|---|
| ファイル | `count > 0` かつ `error_pages == 0` → **Success → 歸檔** |
| 欠落頁の仕訳 | **どこにも入らない**。自動再試行も無い |
| 留痕 | カバレッジ哨戒は動く（`total` は pypdf 申告値なので `missing = k+1..total` を正しく算出）→ 監査タブに「欠落」1 行 |

**評価**: 監査タブに 1 行残るので「完全な無音」ではない（P0 ではない）。
だが顧客がその行に気づいて手動で再処理しない限り、その頁の帳簿データは
永久に入らない。**P1**。

**本件で直さない理由**: 修正には設計判断が要る ——
`_split_pdf_pages` が中途失敗したとき (a) 例外を上げて逐頁ループの
per-page try に拾わせるか、(b) 番兵を yield するか、(c) 残頁分の占位を
生成するか。いずれも「PDF 分割失敗 → 尾段へ fallback」という既存の
語義（下記 11.1b）と相互作用する。1 欠陥の修復の範囲を超える。

**→ 趙の拍板事項**: 次に着手するか、T5 以降へ回すか。

### 11.1b `_split_pdf_pages` が最初から開けないと多頁 PDF が 1 行に潰れる（**P2 → P1 に格上げ。2026-08-17 趙拍板 ＋ Codex 合意**）

> **状態更新（2026-08-17）**: 本項は **P1 へ格上げ**され、次 session の
> 立刻着手対象になった。あわせて **pypdf 未導入の経路も同じ扱い**（尾段禁止）
> にすることが決まっている —— そちらは偶発ではなく**系統的**に全 PDF が
> 該当するため、より悪い。作業指示は
> `docs/plans/2026-08-17-split-pdf-midway-failure.md` §12.1 ② を参照。

同じ zero-yield 監査で発見。`PdfReader()` や `len(reader.pages)` 自体が
落ちる場合（暗号化 PDF・不正な xref 等）、`_split_pdf_pages` は 1 件も
yield せずに終わり、**尾段へ落ちて多頁 PDF 全体を 1 回の Gemini 呼出**に
送る（`ocr_engine.py:2379`）。

不変式（≥1 yield）は満たすので違反ではない。しかしこれは逐頁ループが
生まれた理由そのもの（`ocr_engine.py:2213-2217` のコメント:
「大型 PDF で出力が MAX_TOKENS に達し JSON が途中切断される生産事故」）を
再現する経路である。20 頁のスキャンが 1 行に潰れうる。

11.0 と同じ箇所の設計判断なので、併せて扱うのが自然。

### 11.2 「`_yield_page_results` が例外なしで 0 件返す」への構造的兜底（P2）

simcodex Round 2 で Codex が P1 として提起。**当方の裁決は見送り**で、
Codex も複審で「当方成立・反論なし」と認めた。

**事実**: 例外を伴わない 0 件（`StopIteration` が即座に来る）に対して、
逐頁ループも尾段も占位を出さない。逐頁はカバレッジ警告だけ、尾段は無言。

**見送りの理由 3 点**:

1. これは**本件が入れた欠陥ではなく既存の設計裁定**である。IP-401 の元 Plan
   §8-中7 が「カバレッジ突合は警告のみ・成否判定は変えない（P2 繰延）」と
   明示裁決しており、`test_ip401_regression.py:140`
   `test_coverage_warning_fires_when_a_page_yields_nothing` が
   `iter([])` を注入して**「p1 が出力に現れないこと」を積極的に assert**
   している（`:168`）。裁定はテストとして機械可読な形で固定済み。
2. 提案（零 emission なら占位を yield）を入れると**その既存テストが必ず赤に
   なる**。本件の受入基準 G4 は「既存回帰が**無修正で**緑」であり、
   1 欠陥の修復の範囲で既存裁定を覆すことになる。
3. Codex が挙げた具体経路（`ENTRY_BUILDERS` 未登録）は**到達不能**。実測:
   `set(PROMPTS) - set(DocType.ALL)` / `set(PROMPTS) - set(ENTRY_BUILDERS)` /
   `set(DocType.ALL) - set(ENTRY_BUILDERS)` すべて空。
   `page_family.select_prompt_doc_type` の return は 3 種いずれも `DocType.ALL`
   内。`_validate_doc_type_registries` が import 時に 5 表を検査し、漏れれば
   **起動不能**にする（実行時兜底より早く鳴る硬い層）。Codex はこの具体経路を
   **撤回**した。

**申し送り先**: §11.1（共有 generator 化）と同時に扱うのが自然。
両者とも「1 頁 = 1 出力」を構造で保証する話であり、共有 generator の中に
「何も出さずに終わったら占位を出す」を 1 度だけ書けば両経路に効く。
そのとき §8-中7 の裁定（警告のみ）を明示的に更新し、
`test_coverage_warning_fires_when_a_page_yields_nothing` の期待値も
併せて改めること。
