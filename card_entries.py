"""クレジットカード明細 / 交通系IC 利用履歴の逐行 entry builder（T4-c/d/e）。

Plan: `docs/plans/2026-08-17-t4-card-prompts-builders.md`

責務は 3 つ:

1. **記帳側** —— `raw_data` から MF 仕訳の entry を 1 明細 1 件で組む
2. **検算側** —— 同じ `raw_data` から `card_reconciliation` の DTO を組む
3. **非記帳の可視化** —— 記帳しなかった行の件数・金額を文字列に畳む

**1 と 2 は別軸**（母 Plan AD-5）。記帳しない行（前回分口座振替）も検算には要り、
記帳する行（ポイント充当を abs して未払金を減らす）は検算では負数のまま効く。
同じフィルタで両方を処理すると F-4 の黄金値 15,503 が壊れる。

`ocr_engine` からは `ENTRY_BUILDERS` 経由で呼ばれるだけ（`ocr_engine.py` は
2368 行で 800 行上限を超過しており、これ以上積まない。母 Plan §4）。

**venv 非依存を保つこと**（gspread / paddleocr / google api を引かない）。
`test_dependency_weight` が機械で見張る —— この性質が壊れても全量テストは
緑のままになるため、人のレビューでは捕まらない。
"""
import functools
import re
import unicodedata
from datetime import date

from card_reconciliation import (
    KIND_ALL, KIND_CARRY_OVER, KIND_CHARGE, KIND_CREDIT_ADJUST, KIND_UNKNOWN,
    SECTION_CURRENT_USAGE, SECTION_PAYMENT_SUMMARY, SECTION_POINT,
    SECTION_UNKNOWN, CardIdent, DetailLine, PrintedTotal, _coerce_int,
    section_for_label,
)
from doc_types import DOC_TYPE_CONFIG, DocType
from invoice_classification import (
    DeductionInput, classify, derive_line_kind, determine_debit_tax_type,
    needs_invoice_confirmation,
)

# ── config オーバライド（config が無くても動く。母 Plan §9.5）─────────
try:
    from config import CREDIT_ADJUST_CREDIT_ACCOUNT
except ImportError:
    # AD-5: ポイント充当の貸方。『クレカ相殺』は MF 112 社中 7 社にしか無く、
    # しかも向きが目的と逆だったため採らない（母 Plan 附録 D）
    CREDIT_ADJUST_CREDIT_ACCOUNT = "雑収入"

try:
    from config import TRANSIT_IC_BOOK_CHARGE_ROWS
except ImportError:
    TRANSIT_IC_BOOK_CHARGE_ROWS = False   # TBD-2 既定: 乗車を記帳するので不要

try:
    from config import ACCOUNT_MAP as _ACCOUNT_MAP
except ImportError:
    _ACCOUNT_MAP = {}

try:
    from config import CREDIT_ONLY_ACCOUNTS as _CREDIT_ONLY_ACCOUNTS
except ImportError:
    _CREDIT_ONLY_ACCOUNTS = frozenset()


# ── プロンプトと共有する契約（`test_card_entries` が突合を見張る）──────
# **実際に読むキーだけ**を書く（`period` は `page_dedup` が読むので prompt には
# 在るが、こちらは読まない。宣言と実装の一致を AST テストが双方向で見張る）
CONSUMED_CARD_KEYS = ("issuer", "member_no", "statement_page",
                      "statement_no", "card_name", "account_hint")
CONSUMED_ROW_KEYS = ("line_no", "date", "month_day", "merchant", "note",
                     "amount", "foreign_amount", "currency", "fx_rate",
                     "kind", "sec", "category", "place_from", "place_to",
                     "debit_account", "account_hint")
CONSUMED_TOP_KEYS = ("card", "rows", "sections", "printed_totals")

# 占位（記帳できないが**行は消さない**）の理由コード。T8 が赤系タグに使う
PLACEHOLDER_NO_JPY = "no_jpy_amount"
PLACEHOLDER_UNCLASSIFIED_NEGATIVE = "unclassified_negative"
PLACEHOLDER_NO_AMOUNT = "no_amount"
PLACEHOLDER_SIGN_CONFLICT = "credit_adjust_sign_conflict"

# 記帳軸で分類しきれなかった行の内部哨兵。**`KIND_ALL` には入れない**
# （元帳へ渡すときは `KIND_UNKNOWN` に畳む。AD-T4-3）
_KIND_UNCLASSIFIED_NEGATIVE = "_unclassified_negative"
# Gemini が `credit_adjust` と申告したのに金額が負でない —— 負号を落とした
# 可能性がある。信じて記帳すると 借方 未払金 ／ 貸方 雑収入 で**収益が
# 無症状で過大**になるので、占位にして人へ回す（Codex 評審 R1-#2）
_KIND_SIGN_CONFLICT = "_credit_adjust_sign_conflict"
_INTERNAL_KINDS = (_KIND_UNCLASSIFIED_NEGATIVE, _KIND_SIGN_CONFLICT)


# ── 区画見出し（**合計ラベル表とは別物**）────────────────────────────
#
# `card_reconciliation.TOTAL_LABEL_SECTION` は「今月ご利用額**合計**」のような
# 合計ラベルの表で、判定は「登録ラベルが入力の部分文字列か」の向きである。
# 区画の**見出し**「今月ご利用額」は登録済みラベルより短いので、あちらでは
# 引けず SECTION_UNKNOWN に落ちる —— F-5 のアメックス カードB は
# 「お支払い金額内容」と「今月ご利用額」の 2 区画が縦に並ぶので、
# 両方が未知区画になると区画分離そのものが効かなくなる（Codex 複審 P1）。
#
# **混ぜてはいけない**: この表を合計ラベル側へ足すと、`printed_totals` で
# 「今月ご利用額」が合計ラベルとして誤命中し、区画小計を請求合計として検算する。
SECTION_HEADING = {
    "今月ご利用額": SECTION_CURRENT_USAGE,
    "今回ご利用額": SECTION_CURRENT_USAGE,
    "ご利用明細": SECTION_CURRENT_USAGE,
    "ご利用代金明細": SECTION_CURRENT_USAGE,
    "お支払い金額内容": SECTION_PAYMENT_SUMMARY,
    "お支払い金額": SECTION_PAYMENT_SUMMARY,
    "今回ご請求内容": SECTION_PAYMENT_SUMMARY,
}
_HEADING_ORDER = tuple(sorted(SECTION_HEADING, key=len, reverse=True))
_POINT_HEADING_TOKEN = "ポイント"

# 明細行のラベル（**頁の合計ラベルとは別**。同じ表にすると片方の追加が
# 他方を誤爆させる）。判定形式だけ `section_for_label` に揃える
_CARRY_OVER_LABELS = ("前回分口座振替金額", "前回分口座振替", "前月分お支払い",
                      "前回お支払い額", "口座振替")
# **負数のときだけ**参照する。ポイント語だけで行を落とさない（AD-2 の思想）
_POINT_ADJUST_LABELS = ("ポイントキャッシュバック", "キャッシュバック",
                        "ポイント充当", "ポイント利用", "ポイント値引",
                        "ポイント交換", "ポイント")
_IC_CHARGE_TOKENS = ("入金", "チャージ")

# 手書きヒントから借方科目へ解決するとき、**借方に不適な科目は除く**。
# `ACCOUNT_MAP` には `売上高` / `雑収入` / `雑損失` も在り、放置すると
# 手書きから収益科目が借方に入る（Codex 複審 P2）
DEBIT_HINT_EXCLUDED = frozenset(_CREDIT_ONLY_ACCOUNTS) | {
    "売上高", "雑収入", "雑損失"}
# **プロセス生存中は不変**として扱う（`resolve_account_hint` が結果を
# `lru_cache` するため）。テストで `_ACCOUNT_MAP` を差し替えるときは
# 必ず `resolve_account_hint.cache_clear()` を呼ぶこと —— でないと
# 同じ文字列に対して古い解決結果が静かに返る
_HINT_WORDS = tuple(sorted(
    (set(_ACCOUNT_MAP) | set(_ACCOUNT_MAP.values())), key=len, reverse=True))
# Gemini が推定した `rows[].debit_account` の白名単。`append_entries` の写像は
# `ACCOUNT_MAP.get(x, x)` で**未知キーを潰さない**ので、ここで弾かないと
# 「架空費」のような実在しない科目名がそのまま MF の勘定科目列へ書かれる
# （CLAUDE.md「科目名を臆造しない」に反する。Codex 評審 R1-#1）
KNOWN_DEBIT_ACCOUNTS = frozenset(
    name for name in (set(_ACCOUNT_MAP) | set(_ACCOUNT_MAP.values()))
    if name not in DEBIT_HINT_EXCLUDED)

_NONBOOKABLE_LABEL = {
    KIND_CARRY_OVER: "前回分口座振替",
    KIND_CHARGE: "チャージ入金",
}

_RE_MONTH_DAY = re.compile(r"(\d{1,2})\D+(\d{1,2})")
_RE_STATEMENT_PAGE = re.compile(r"(\d{1,3})\s*/\s*(\d{1,3})")


def _norm(text):
    """NFKC 正規化（全角 ＥＴＣ と半角 ETC を同一視する）。"""
    return unicodedata.normalize("NFKC", str(text or ""))


def _dict(value):
    return value if isinstance(value, dict) else {}


def _card(raw_data):
    return _dict(_dict(raw_data).get("card"))


def _rows(raw_data):
    """明細行の list。dict でない要素は落とす（Gemini の型ゆれ対策）。"""
    rows = _dict(raw_data).get("rows")
    if not isinstance(rows, (list, tuple)):
        return ()
    return tuple(r for r in rows if isinstance(r, dict))


def _matches(text, labels):
    """登録ラベルが入力に含まれるか（`section_for_label` と同じ判定形式）。"""
    return any(label in text for label in labels)


# ── 区画 ──────────────────────────────────────────────────────────
def section_for_heading(label):
    """区画**見出し**から区画を決める。未知は `SECTION_UNKNOWN`。"""
    text = _norm(label).strip()
    if not text:
        return SECTION_UNKNOWN
    if text in SECTION_HEADING:
        return SECTION_HEADING[text]
    for known in _HEADING_ORDER:
        if known in text:
            return SECTION_HEADING[known]
    # F-6: ポイント区画の数値は円貨の勘定ではない。検算の基準に採らせない
    if _POINT_HEADING_TOKEN in text:
        return SECTION_POINT
    return SECTION_UNKNOWN


def _sections(raw_data):
    value = _dict(raw_data).get("sections")
    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _section_of_row(row, sections, kind):
    """`sec`（数値 index）→ 区画（文字列定数）。

    数値をそのまま `DetailLine.section` に入れると全行が未知区画に落ちる
    （あちらは文字列定数で比較される）。
    """
    if kind == KIND_CARRY_OVER:
        # AD-4 の保険の行版。F-5 が「別集計」と明記した確認済み事実であり、
        # Gemini の `sec` 申告より優先する（混ぜると 933,680 が崩れる）
        return SECTION_PAYMENT_SUMMARY

    # Gemini は JSON 数値でなく "1" を返すことがある。文字列だからと
    # 未知区画に落とすと、`card_reconciliation` 側で区画分離が効かなくなる
    index = None if isinstance(row.get("sec"), bool) else _coerce_int(row.get("sec"))
    if index is None or not 0 <= index < len(sections):
        return SECTION_UNKNOWN

    # 両関数へ**同じ正規化済み文字列**を渡す。`section_for_label` は
    # NFKC をかけないので、生の逐語を両方に渡すと全角記号の券面で
    # 判定が食い違う（Reuse 評審 R1-#1）
    label = _norm(_dict(sections[index]).get("label"))
    section = section_for_heading(label)
    if section != SECTION_UNKNOWN:
        return section
    # 券面によっては区画見出しがそのまま合計ラベルの語になっている。
    # 両表とも白名単なので回帰しても誤爆しない
    return section_for_label(label)


# ── 記帳軸（AD-T4-3。Gemini の申告を Python が矯正する）────────────────
def _row_text(row):
    """分類に使う逐語をつなぐ（店名・副行・nimoca の種別と施設）。"""
    return _norm(" ".join(str(row.get(key) or "") for key in (
        "merchant", "note", "category", "place_from", "place_to")))


def resolve_booking_kind(row, doc_type, amount, text=None):
    """記帳軸の kind を決める。返り値は `KIND_*` か内部哨兵。

    優先序は Plan AD-T4-3 の表のとおり。**`amount < 0` だけで
    `credit_adjust` にしない** —— 返品・取消などポイント以外の負数を
    「借方 未払金 ／ 貸方 雑収入」にすると収益が無症状で過大になる
    （趙裁定 10 は「クレカ相殺はポイント充当のみ」）。

    `text` は `_row_text(row)` の結果。呼出側が既に計算していれば渡す
    （同じ行を 2 回正規化すると、将来どちらかだけ手を入れたときに
    分類の入力が静かに食い違う）。
    """
    text = _row_text(row) if text is None else text

    if doc_type == DocType.TRANSIT_IC:
        if _matches(_norm(row.get("category")), _IC_CHARGE_TOKENS):
            return KIND_CHARGE

    if _matches(text, _CARRY_OVER_LABELS):
        return KIND_CARRY_OVER

    if amount is not None and amount < 0:
        if _matches(text, _POINT_ADJUST_LABELS):
            return KIND_CREDIT_ADJUST
        return _KIND_UNCLASSIFIED_NEGATIVE

    declared = row.get("kind")
    if declared == KIND_CREDIT_ADJUST:
        # ここへ来たということは金額が負でない（負なら上で確定済み）。
        # Gemini が負号を落とした可能性があり、申告どおり記帳すると
        # 借方 未払金 ／ 貸方 雑収入 で**収益が無症状で過大**になる。
        # ポイント充当は「当月利用額の減額」なので券面上は必ず負である
        return _KIND_SIGN_CONFLICT
    if declared in KIND_ALL:
        return declared
    # 申告外は記帳側へ倒す（過少記帳より過剰記帳。受入基準 A3 の集合定義）
    return KIND_UNKNOWN


def _ledger_kind(kind):
    """哨兵を元帳の語彙へ畳む。検算では符号を保ったまま集計させる。"""
    return KIND_UNKNOWN if kind in _INTERNAL_KINDS else kind


# ── T4-c: 検算 DTO への変換（prompt schema の十分性の証明）──────────
def card_ident_from_raw(raw_data):
    """`CardIdent` を組む。`"1/6"` は n/N へ分解する。

    プロンプトに n と N を別々に出させると、両者が食い違ったときに
    裁く手段が無くなる（AD-4 と同じ理由で逐語 1 本を Python が分解する）。
    """
    card = _card(raw_data)
    page_n = page_total = None
    m = _RE_STATEMENT_PAGE.search(_norm(card.get("statement_page")))
    if m:
        page_n, page_total = int(m.group(1)), int(m.group(2))

    return CardIdent(
        member_no=str(card.get("member_no") or "") or None,
        issuer=str(card.get("issuer") or "") or None,
        statement_page_n=page_n,
        statement_page_total=page_total,
        statement_no=str(card.get("statement_no") or "") or None,
    )


def printed_totals_from_raw(raw_data):
    """`PrintedTotal` の tuple。`is_handwritten` を落とさない。

    落とすと F-10 の手書き「ETC代 12,940円」が検算の基準値になり、
    検算そのものが汚染される。
    """
    value = _dict(raw_data).get("printed_totals")
    if not isinstance(value, (list, tuple)):
        return ()
    totals = []
    for item in value:
        item = _dict(item)
        if not item:
            continue
        totals.append(PrintedTotal(
            label=str(item.get("label") or ""),
            amount=_coerce_int(item.get("amount")),
            count=_coerce_int(item.get("count")),
            page=_coerce_int(item.get("page")) or 0,
            is_handwritten=bool(item.get("is_handwritten")),
        ))
    return tuple(totals)


def detail_lines_from_raw(raw_data, doc_type):
    """`DetailLine` の tuple。**符号はそのまま**（検算集合は記帳集合と別軸）。"""
    sections = _sections(raw_data)
    lines = []
    for row in _rows(raw_data):
        amount = _coerce_int(row.get("amount"))
        if amount is None:
            continue      # 金額が読めない行は検算に足せない（元帳側が note する）
        kind = resolve_booking_kind(row, doc_type, amount)
        lines.append(DetailLine(
            amount=amount,
            section=_section_of_row(row, sections, kind),
            kind=_ledger_kind(kind),
            description=str(row.get("merchant") or row.get("category") or ""),
        ))
    return tuple(lines)


# ── AD-T4-11: 手書きヒントの解決 ──────────────────────────────────
@functools.lru_cache(maxsize=256)
def resolve_account_hint(text):
    """手書き注記から借方科目を解決する。できなければ空文字。

    純関数なのでキャッシュする —— 文書級のヒントは 300 行の ETC 明細で
    同じ文字列が 300 回渡ってくる（`invoice_classification._active_rules` と
    同じ理由・同じ手口）。

    **逐語をそのまま科目にしてはいけない** —— `append_entries` の写像は
    `ACCOUNT_MAP.get(x, x)` で、未知キーは `未確定勘定` にならず**素通りする**。
    `HP代` がそのまま MF の勘定科目列へ書かれる（CLAUDE.md の
    「科目名を臆造しない」に反する）。

    判定は一致**語数**ではなく **canonical 科目数**で行う ——
    `ガソリン代・駐車場代` は 2 語だがどちらも旅費交通費なので実質 1 科目。
    """
    t = _norm(text).strip()
    if not t:
        return ""

    found = set()
    for word in _HINT_WORDS:
        if word and word in t:
            canonical = _ACCOUNT_MAP.get(word, word)
            if canonical not in DEBIT_HINT_EXCLUDED:
                found.add(canonical)

    return found.pop() if len(found) == 1 else ""


def _valid_debit_account(name):
    """Gemini が推定した科目名を白名単で濾す。未知なら空文字。

    `append_entries` の `ACCOUNT_MAP.get(x, x)` は**未知キーを潰さない**ので、
    ここを通さないと存在しない科目名が MF の勘定科目列へ素通りする。
    """
    candidate = _norm(name).strip()
    return candidate if candidate in KNOWN_DEBIT_ACCOUNTS else ""


def _debit_account(row, card, doc_type):
    """(科目, ヒントを使ったか, 未解決ヒント, 却下した AI 推定) を返す。

    **却下した AI 推定を捨てない**。`account_hint` が解決できなかったときは
    逐語を memo に残す（AD-T4-11）のに、Gemini の推定を白名単で弾いたときだけ
    無痕跡で消えると、人が原票と突合しても「AI はもっと精確な判断をしていた」
    ことが見えない。`ACCOUNT_MAP` に無い正当な科目（分割払手数料の
    `支払利息` 等）が在れば、この痕跡が唯一の発見手段になる。
    """
    # 却下の記録は**ヒントの解決より先に**確定させる。「Gemini が実在しない
    # 科目名を出した」という事実は、その推定が採用されたかどうかとは独立に
    # 価値がある（`ACCOUNT_MAP` の欠落を見つける唯一の手掛かりになる）。
    # ヒントが解決したときに早期 return すると、この痕跡が消える
    declared = str(row.get("debit_account") or "").strip()
    valid = _valid_debit_account(declared)
    rejected = declared if declared and not valid else ""

    unresolved = ""
    for hint in (row.get("account_hint"), card.get("account_hint")):
        hint = str(hint or "").strip()
        if not hint:
            continue
        resolved = resolve_account_hint(hint)
        if resolved:
            return resolved, True, "", rejected
        unresolved = unresolved or hint

    return (valid or DOC_TYPE_CONFIG[doc_type]["default_debit"],
            False, unresolved, rejected)


# ── 摘要（S列）。Gemini に整形させない ────────────────────────────
def _fx_suffix(row):
    """`57.60USD @160.384`。円貨と取り違えないよう通貨記号を必ず添える。"""
    foreign, currency = row.get("foreign_amount"), str(row.get("currency") or "")
    if foreign is None or not currency:
        return ""
    try:
        text = "%.2f%s" % (float(foreign), currency)
    except (TypeError, ValueError):
        return ""
    try:
        return "%s @%.3f" % (text, float(row.get("fx_rate")))
    except (TypeError, ValueError):
        return text


def _description(row, doc_type):
    if doc_type == DocType.TRANSIT_IC:
        category = str(row.get("category") or "").strip()
        stations = [s for s in (str(row.get(k) or "").strip()
                                for k in ("place_from", "place_to")) if s]
        return " ".join(x for x in (category, "〜".join(stations)) if x)

    # 副行（ETC NO・入口/出口）は**摘要に入れない**。S列は帳簿で最も目に付く
    # 列で、ETC NO の羅列で埋めると読めなくなる。行級 memo（T列）へ回す
    merchant = str(row.get("merchant") or "").strip()
    suffix = _fx_suffix(row)
    return "%s %s" % (merchant, suffix) if suffix else merchant


def _memo(row, unresolved_hint, rejected_account=""):
    """行級 memo（T列）。解決できなかった判断材料は**必ず**残す。

    捨てると「客が手書きで指示したのに無視された」「AI はもっと精確に
    判断していたのに黙って既定科目へ落ちた」が人には見えなくなる。
    """
    parts = [str(row.get("note") or "").strip()]
    if unresolved_hint:
        parts.append("手書き: %s" % unresolved_hint)
    if rejected_account:
        parts.append("AI推定科目(未登録): %s" % rejected_account)
    return " ".join(p for p in parts if p)


# ── entry の組み立て ─────────────────────────────────────────────
def _base_entry(row, card, doc_type, booking_kind, line_kind, unresolved_hint,
                rejected_account=""):
    return {
        "credit_account": DOC_TYPE_CONFIG[doc_type]["default_credit"],
        "credit_tax_type": "対象外",
        # AD-11: 貸方債務の相手は加盟店ではなくカード会社。結線は T6
        # （訂正 1: `_determine_credit_sub_account` は現在**死コード**）
        "credit_sub_account": str(card.get("card_name") or "").strip(),
        # AD-8 / AD-T4-5: doc 級 invoice_num（カード会社のT番号）を継がせない。
        # `entry.get("debit_invoice", invoice_num)` はキーが在れば default を
        # 使わないので、これは T6 を待たずに**今すぐ効く**
        "debit_invoice": "",
        "date": str(row.get("date") or ""),
        "debit_vendor": str(row.get("merchant") or "").strip(),
        "memo": _memo(row, unresolved_hint, rejected_account),
        "description": _description(row, doc_type),
        "_booking_kind": booking_kind,
        "_line_kind": line_kind,
        "_sec": row.get("sec"),
        "_deduction": None,
        "_needs_invoice_confirm": False,
        "_year_estimated": False,
        "_account_hint_used": False,
        "_account_hint_unresolved": unresolved_hint,
        "_debit_account_rejected": rejected_account,
        "_placeholder_reason": "",
        "_line_no": _coerce_int(row.get("line_no")),
    }


def _placeholder(entry, reason, note):
    """記帳できない行も**消さない**（IP-401 の思想を行に適用）。

    `amount: 0` は現行 `append_entries` に無音 skip されるため、
    T6 の `line_mode` で「0 でも 1 行書く」ことが要る（Plan §10）。
    """
    return {**entry, "amount": 0, "debit_account": "", "debit_tax_type": "",
            "description": "%s（%s）" % (entry["description"], note),
            "_placeholder_reason": reason}


def _build(raw_data, doc_type, reference_date):
    card = _card(raw_data)
    entries = []

    for row in _rows(raw_data):
        amount = _coerce_int(row.get("amount"))
        text = _row_text(row)
        kind = resolve_booking_kind(row, doc_type, amount, text)

        if kind == KIND_CARRY_OVER:
            continue      # 銀行明細側で起票される。ここで記帳すると二重計上
        if kind == KIND_CHARGE and not TRANSIT_IC_BOOK_CHARGE_ROWS:
            continue      # TBD-2: 乗車を逐行記帳するのでチャージは不要

        debit, hint_used, unresolved, rejected = _debit_account(
            row, card, doc_type)
        line_kind = derive_line_kind(doc_type, kind, text)
        entry = _base_entry(row, card, doc_type, _ledger_kind(kind),
                            line_kind, unresolved, rejected)
        entry["_account_hint_used"] = hint_used

        if doc_type == DocType.TRANSIT_IC:
            entry["date"], entry["_year_estimated"] = _ic_date(
                row, reference_date)

        if kind == _KIND_UNCLASSIFIED_NEGATIVE:
            # ラベルで同定できない負数を `credit_adjust`（貸方 雑収入）に
            # 倒すと収益が無症状で過大になる（K8）。人へ回す
            entries.append(_placeholder(
                entry, PLACEHOLDER_UNCLASSIFIED_NEGATIVE,
                "要確認: 分類できない負数 %s" % row.get("amount")))
            continue

        if kind == _KIND_SIGN_CONFLICT:
            # `credit_adjust` 申告なのに金額が負でない。負号の脱落を疑う
            entries.append(_placeholder(
                entry, PLACEHOLDER_SIGN_CONFLICT,
                "要確認: ポイント充当の申告だが金額が負でない %s"
                % row.get("amount")))
            continue

        if amount is None:
            no_jpy = row.get("foreign_amount") is not None
            entries.append(_placeholder(
                entry,
                PLACEHOLDER_NO_JPY if no_jpy else PLACEHOLDER_NO_AMOUNT,
                ("円貨額なし: %s" % _fx_suffix(row)) if no_jpy
                else "金額を読み取れず"))
            continue

        if amount == 0:
            entries.append(_placeholder(entry, PLACEHOLDER_NO_AMOUNT, "金額 0"))
            continue

        if kind == KIND_CREDIT_ADJUST:
            # AD-5: カード未払金を直接減らす。税区分は控除の話ではないので
            # `classify` を通さず両側「対象外」で確定させる
            entries.append({
                **entry,
                "debit_account": "未払金",
                "debit_tax_type": "対象外",
                "credit_account": CREDIT_ADJUST_CREDIT_ACCOUNT,
                "credit_tax_type": "対象外",
                "amount": abs(amount),
            })
            continue

        verdict = classify(DeductionInput(
            doc_type=doc_type, line_kind=line_kind, amount_tax_incl=amount,
            txn_date=entry["date"] or None,
            is_foreign_currency=bool(str(row.get("currency") or "").strip()),
            # F-11 / F-14: カード明細に加盟店の登録番号は存在しない。
            # 券面の T番号はカード会社自身のもので、配ると全件が「適格」になる
            merchant_t_number="",
        ))
        entries.append({
            **entry,
            "debit_account": debit,
            "debit_tax_type": determine_debit_tax_type(verdict),
            "amount": amount,
            "_deduction": verdict,
            "_needs_invoice_confirm": needs_invoice_confirmation(amount),
        })

    return entries


# ── 年の推定（nimoca には年が印字されていない。F-7）──────────────────
def _ic_date(row, reference_date):
    """(YYYY/MM/DD, 推定したか) を返す。

    履歴は**過去の記録**なので、基準日より未来になる月日は年跨ぎの徴候
    （1 月に 12 月の履歴を処理する形）。前年に倒す。
    """
    printed = str(row.get("date") or "").strip()
    if printed:
        return printed, False

    m = _RE_MONTH_DAY.search(_norm(row.get("month_day")))
    if not m:
        return "", False

    month, day = int(m.group(1)), int(m.group(2))
    ref = reference_date or date.today()
    for year in (ref.year, ref.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue      # 2/29 が非閏年に当たった等
        if candidate <= ref:
            return candidate.strftime("%Y/%m/%d"), True
    return "", False


# ── 非記帳行の可視化（AD-T4-7）────────────────────────────────────
def summarize_nonbookable(raw_data, doc_type):
    """記帳しなかった行の件数と金額を 1 行に畳む。

    doc 級 memo（T6 前の T列）と `_nonbookable_summary`（T9 が監査タブへ）の
    両方に入れる。二重に持つのは移行の谷間で情報を落とさないため。
    """
    buckets = {}
    for row in _rows(raw_data):
        amount = _coerce_int(row.get("amount"))
        kind = resolve_booking_kind(row, doc_type, amount)
        if kind == KIND_CHARGE and TRANSIT_IC_BOOK_CHARGE_ROWS:
            continue      # 設定で記帳する方針なら非記帳ではない
        if kind not in _NONBOOKABLE_LABEL:
            continue
        count, total = buckets.get(kind, (0, 0))
        buckets[kind] = (count + 1, total + abs(amount or 0))

    if not buckets:
        return ""
    return "非記帳: " + " / ".join(
        "%s %d件 ¥%s" % (_NONBOOKABLE_LABEL[kind], count, format(total, ","))
        for kind, (count, total) in sorted(buckets.items()))


# ── `ENTRY_BUILDERS` に登録される 2 本 ───────────────────────────
def build_entries_from_credit_card(raw_data):
    """クレジットカード明細 → 逐行仕訳（1 明細 = 1 仕訳。趙裁定 1）。"""
    return _build(raw_data, DocType.CREDIT_CARD, None)


def build_entries_from_transit_ic(raw_data, reference_date=None):
    """交通系IC 利用履歴 → 逐行仕訳。

    `reference_date` は年の推定の基準（既定は実行日）。テストは必ず注入する
    ——時刻依存のテストを作らないため。
    """
    return _build(raw_data, DocType.TRANSIT_IC, reference_date)
