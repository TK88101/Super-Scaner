"""截断した Gemini 応答のサルベージ解析 ＋ 行欠け検出（T5）。

**stdlib 以外を一切 import しない**。理由は 2 つ:
- 母 Plan §4「`ocr_engine.py`（2500 行超）にこれ以上積まない」
- venv 無し（`python3 -m unittest test_card_salvage`）で単体テストできること
  ——`test_dependency_weight` がこの性質を機械で見張る

## なぜサルベージが要るのか

`gemini-2.5-flash` は thinking tokens と出力予算を共有するため、逐行記帳
（クレカ／交通系IC の明細 100 行級）では応答が MAX_TOKENS で切れうる。
現行の `ocr_engine.extract_json` は all-or-nothing で、切れた応答を丸ごと
`None` にする —— 券面に見えている行数（`rows_on_page`）すら取れない。

一方 prompt の JSON schema は **`rows` が最後**（`card_prompts.py` の
テンプレート）。つまり切れた応答テキストにも `card` / `sections` /
`printed_totals` / `rows_on_page` / `total_amount` と、**完結した行
オブジェクトが N 個**残っている。それを拾えば Gemini を 1 度も追加で
叩かずに N 行 ＋ 総数が取れる。

## 絶対に守る規則 —— 会計数値を破損させない

`"amount": 630` が `"amount": 63` の位置で切れたテキストは、閉じ括弧を
補うと **有効な JSON かつ誤った金額** になる。よって値を採用するのは

- 数値: 直後に**もう 1 文字ある**とき（区切りか空白。EOF は 630 と 6300 を
  区別できないので捨てる）
- それ以外（文字列・オブジェクト・配列・null/真偽）: 自己終端しているとき
  （途中で切れていれば JSON パーサ自身が失敗するので採用しない）

行を 1 行失う方が、金額が 1 桁欠けるより遥かに安全である。
"""
import json
import re
from typing import Any, List, NamedTuple, Optional

# 行の数え方と数値の読み方は**記帳側と同じ定義でなければならない**。
# 別実装を置くと、顧客が読む「券面100行中62行のみ取得」が帳簿の実態と
# ずれる（過検出なら健全な頁に赤い提示行、過少検出なら行欠けが無音）。
# どちらも venv 非依存モジュール（`test_dependency_weight` が見張る）なので
# stdlib のみという本モジュールの性質は保たれる。
from card_entries import _rows as _builder_rows
from card_reconciliation import _coerce_int

# `ocr_engine` がサルベージ経由の raw_data に立てる内部旗。
# Gemini の応答由来のキーと衝突しないよう `_` 始まりにしてある。
SALVAGED_KEY = "_salvaged_truncation"

_DECODER = json.JSONDecoder()
_FENCE_OPEN = re.compile(r"^```[A-Za-z]*[ \t]*\r?\n?")
_WHITESPACE = " \t\r\n"

# raw_decode の失敗を表す番兵。`None` を使うと JSON の `null` と区別できない。
_FAIL = object()


class LineShortage(NamedTuple):
    """行欠けの事実。

    expected: 券面の明細行総数（`rows_on_page`）。読めなければ None
    got:      実際に取れた行数（**builder が見る行数**と同じ定義）

    「截断を経たか」は持たない —— それは `raw_data[SALVAGED_KEY]` が唯一の
    出所で、文言も監査 reason も expected/got だけで決まる。消費者の無い
    フィールドを足すと、後で「どちらが真か」を調べる手間だけが増える。
    """
    expected: Optional[int]
    got: int


# ============================================================
# サルベージ解析
# ============================================================

def salvage_truncated_json(text: Optional[str]) -> Optional[dict]:
    """截断 JSON から「完結した部分」だけを回収する。

    Returns:
        回収できた top 級フィールド ＋ 完結した配列要素だけを持つ dict。
        1 つも回収できなければ None。

    `rows` キーに到達する前に切れていれば戻り値に `rows` は**入らない**
    （空配列への正規化は呼出側の責務。T5 Plan §3.3）。

    例外は投げない（fail-open）。サルベージの失敗が記帳経路を壊しては
    ならない —— 呼出側は None を「救えなかった」として扱えばよい。
    """
    try:
        return _salvage(text)
    except Exception:               # noqa: BLE001 — fail-open（上の docstring）
        return None


def _salvage(text: Optional[str]) -> Optional[dict]:
    if not isinstance(text, str):
        return None
    body = _strip_fence(text.strip())
    # トップレベルが JSON オブジェクトでないものは扱わない。先頭の `{` を
    # テキスト中から探しに行くと、配列や散文に埋もれた**入れ子の**
    # オブジェクトを応答全体と取り違える（schema 違反の応答を「救った」
    # ことにしてしまう）。
    if not body.startswith("{"):
        return None
    try:
        whole = json.loads(body)
    except ValueError:
        pass
    else:
        return whole if isinstance(whole, dict) else None
    return _salvage_object(body) or None


def _strip_fence(text: str) -> str:
    """```json フェンスを剥がす（閉じフェンスが無い截断形態も扱う）。"""
    opened = _FENCE_OPEN.match(text)
    if not opened:
        return text
    body = text[opened.end():]
    closed = body.rfind("```")
    return (body[:closed] if closed != -1 else body).strip()


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in _WHITESPACE:
        i += 1
    return i


def _decode_at(text: str, i: int):
    """text[i] から 1 つの JSON 値を読む。失敗時は (_FAIL, i)。"""
    try:
        return _DECODER.raw_decode(text, i)
    except ValueError:
        return _FAIL, i


def _is_terminated(text: str, value: Any, end: int) -> bool:
    """値が途中で切れていないか（モジュール docstring の「絶対に守る規則」）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # 文字列・オブジェクト・配列・null・真偽は自己終端している。
        # 途中で切れていれば raw_decode が既に失敗しているので、ここには来ない。
        return True
    # 数値だけは EOF で曖昧（630 と 6300 が区別できない）。**区切り記号が
    # 実際に続いていること**まで確かめる。「次の 1 文字がある」だけで足りる
    # ようにも見えるが、それだと末尾が空白で切れた応答（`"amount": 63\n`）で
    # 63 を採ってしまう。今は `_salvage` の strip がその形を潰しているものの、
    # 会計値の安全を遠くの 1 行に預けない —— ここで自足させる。
    nxt = _skip_ws(text, end)
    return nxt < len(text) and text[nxt] in ",}]"


def _salvage_object(text: str) -> dict:
    """先頭が `{` のテキストから、**完結したメンバーだけ**を回収する。"""
    out = {}
    i = _skip_ws(text, 1)                       # `{` の次から
    while i < len(text):
        char = text[i]
        if char == "}":
            break
        if char == ",":
            i = _skip_ws(text, i + 1)
            continue
        if char != '"':
            break                               # 壊れた並び。ここで打ち切る
        key, i = _decode_at(text, i)
        if key is _FAIL or not isinstance(key, str):
            break                               # キーが途中で切れた
        i = _skip_ws(text, i)
        if i >= len(text) or text[i] != ":":
            break
        i = _skip_ws(text, i + 1)
        if i >= len(text):
            break
        value, end = _decode_at(text, i)
        if value is _FAIL:
            # 値そのものが途中で切れている。配列なら**要素単位**で救える
            # （これが 62/100 行を拾う本体）。オブジェクトには入らない
            # —— 部分的な card / section を作ると識別子や小計が化ける。
            if text[i] == "[":
                out[key] = _salvage_array(text, i)
            break
        if not _is_terminated(text, value, end):
            break                               # 値が途中で切れた → 捨てる
        out[key] = value
        i = _skip_ws(text, end)
        if i >= len(text) or text[i] not in ",}":
            break                               # 続きは読めない（値は確定済み）
    return out


def _salvage_array(text: str, i: int) -> List[Any]:
    """`[` から始まる截断配列から、完結した要素だけを回収する。"""
    items = []                                  # type: List[Any]
    i = _skip_ws(text, i + 1)
    while i < len(text):
        char = text[i]
        if char == "]":
            break
        if char == ",":
            i = _skip_ws(text, i + 1)
            continue
        value, end = _decode_at(text, i)
        if value is _FAIL or not _is_terminated(text, value, end):
            break
        items.append(value)
        i = _skip_ws(text, end)
        if i >= len(text) or text[i] not in ",]":
            break
    return items


# ============================================================
# 行欠け検出
# ============================================================

def _visible_rows(raw_data: dict) -> List[dict]:
    """**builder が実際に見る行**。数え方は builder へ委譲する。

    `len(raw_data["rows"])` を直に読んではいけない —— `rows` が dict のとき
    キー数を行数と誤認する（Gemini が schema を外した応答を返すと起きる）。
    自前でフィルタを書くのも駄目で、記帳側と定義が割れた瞬間に
    「3 行記帳したのに券面3行中0行のみ取得」のような偽の警告が出る。
    """
    return list(_builder_rows(raw_data))


def _expected_rows(raw_data: dict) -> Optional[int]:
    """`rows_on_page`（券面に印字された明細行数）。解釈できなければ None。

    数値の読み方は検算側（`printed_totals[].count` を読む経路）と同じ
    `_coerce_int` に委ねる。同じ raw_data の中で「券面が申告した件数」が
    2 通りの規則で読まれると、T7 の検算結線で必ず食い違う。
    0 以下は「申告なし」として扱う（これは salvage 固有の足切り）。
    """
    count = _coerce_int(raw_data.get("rows_on_page"))
    return count if count and count > 0 else None


def detect_shortage(raw_data: Any) -> Optional[LineShortage]:
    """行欠けの判定。shortage が無ければ None。

    截断（サルベージ経由）と行飛ばし（有効 JSON なのに行が少ない）は
    同じ式に合流する —— 顧客から見れば「明細が足りない」の一事である。

    総数が読めないまま截断した場合も **shortage として扱う**。
    「分からない＝問題なし」に倒すと、100 行の頁が 0 行で成功になる。
    """
    if not isinstance(raw_data, dict):
        return None
    got = len(_visible_rows(raw_data))
    expected = _expected_rows(raw_data)
    if expected is not None and got < expected:
        return LineShortage(expected=expected, got=got)
    if raw_data.get(SALVAGED_KEY) and expected is None:
        return LineShortage(expected=None, got=got)
    return None


def page_marks(raw_data: Any):
    """この頁に残すべき痕跡 `(shortage, audit_reason)`。**優先規則の唯一の所在**。

    - shortage 非 None → MF の提示行 ＋ 監査タブ 1 行（reason は shortage 由来）
    - shortage None かつ reason 非 None → **監査タブのみ**。救済は経たが行数は
      充足している場合の保険で、Gemini が総数を過少申告したうえで截断した
      ときに効く。帳簿（MF タブ）は汚さず内部の痕跡だけ残す
    - 両方 None → 痕跡なし（健全な頁）

    2 つの規則を呼出側の分岐と関数内の guard に分けて書くと、片方だけ
    書き換えたとき「提示行と salvaged 監査行が両方出る」ようなズレが、
    どちらの単体テストでも死なない形で入り込む。だから 1 箇所に閉じる。
    """
    shortage = detect_shortage(raw_data)
    if shortage is not None:
        return shortage, shortage_audit_reason(shortage)
    if not isinstance(raw_data, dict) or not raw_data.get(SALVAGED_KEY):
        return None, None
    expected = _expected_rows(raw_data)
    return None, "salvaged:%d/%s" % (
        len(_visible_rows(raw_data)),
        expected if expected is not None else "?")


def shortage_memo(shortage: LineShortage) -> str:
    """MF タブの提示行（S 列＝摘要）の文言。顧客がこれだけ見て気づけること。"""
    if shortage.expected is None:
        return ("⚠ 明細行の取得漏れ: AI応答が途中で切断"
                "（%d行のみ取得・総数不明。原票を確認してください）" % shortage.got)
    return ("⚠ 明細行の取得漏れ: 券面%d行中%d行のみ取得（原票を確認してください）"
            % (shortage.expected, shortage.got))


def shortage_audit_reason(shortage: LineShortage) -> str:
    """監査タブの理由列。既存規約どおり**機械可読キー**で書く。"""
    return "line_shortage:%d/%s" % (
        shortage.got,
        shortage.expected if shortage.expected is not None else "?")
