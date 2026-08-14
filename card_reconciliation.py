# card_reconciliation.py
# クレカ／交通系IC の跨頁合計検算の元帳。1 ファイル分の寿命。
#
# 逐行記帳では「明細を 1 行取りこぼした」ことに誰も気づけない。券面の印字合計は
# そのための唯一の外部検査器である。ただし **検査器であって正解ではない**:
# 不一致でも記帳は止めず、赤系マークで可視化する（趙裁定 3。Failed にすると
# ファイルが保持され 3 秒ごとに再処理される）。
#
# 本モジュールの中心的な契約は 2 つ。
#
# 1. **検算集合と記帳集合は別軸**（AD-5）。事実台帳が 3 例で反証している:
#      負数だが検算に入る      … ENEOS ポイントキャッシュバック −3,000（F-4）
#      負数で検算にも入らない  … 前回分口座振替 −35,808（F-5、別区画）
#      正数だが記帳しない      … nimoca 入金 現金 5,000（F-7）
#    符号からどちらも導出できない。よって `kind`（記帳軸）と `section`（検算軸）を
#    独立に持ち、どちらも Python の決定表で決める（Gemini の自己申告に委ねない）。
#
# 2. **累計値しか保持しない**。明細行・description の集合・page_bytes・OCR テキストは
#    一切持たない。ETC 300 行でも nimoca 120 行でも元帳のサイズは変わらない
#    （CLAUDE.md「Generator Pipeline（内存是硬约束）」）。
#
# 重複頁の**判定**は本モジュールの管轄外。page_dedup の二重署名 AND 規則が
# 確定させたものを caller が `is_duplicate=True` で宣言し、元帳は「足さない」
# だけを行う（AD-1: 重複検出は D3 に一本化）。
#
# 外部依存なし（gspread / paddleocr / google api 非依存）。

import re
import unicodedata
from typing import NamedTuple, Optional

from doc_types import DocType

# ── 記帳軸: kind（Plan AD-5 の語彙。Gemini プロンプトの出力値でもある）───────
KIND_EXPENSE = "expense"              # 通常利用
KIND_FEE = "fee"                      # 年会費・手数料
KIND_CREDIT_ADJUST = "credit_adjust"  # ポイント充当（借方 未払金 ／ 貸方 雑収入）
KIND_CARRY_OVER = "carry_over"        # 前回分口座振替（記帳しない。銀行明細側で起票）
KIND_CHARGE = "charge"                # nimoca 入金（記帳しない。乗車を記帳するため）
KIND_UNKNOWN = "unknown"              # 申告外。記帳漏れより過剰記帳に倒す
KIND_ALL = (KIND_EXPENSE, KIND_FEE, KIND_CREDIT_ADJUST, KIND_CARRY_OVER,
            KIND_CHARGE, KIND_UNKNOWN)

# ── 検算軸: section ────────────────────────────────────────────────
SECTION_CURRENT_USAGE = "current_usage"      # 今月ご利用額 / ご利用明細
SECTION_PAYMENT_SUMMARY = "payment_summary"  # お支払い金額内容 / 今回ご請求内容
SECTION_POINT = "point"                      # ポイント明細（円貨の勘定ではない）
SECTION_UNKNOWN = "unknown"

# ── 判定語彙（人が読む日本語）───────────────────────────────────────
# 「検算不可」と「検算不能」を分けるのは必須。混ぜると nimoca が上がるたび
# （通勤費なので毎月来る）に警告が出て狼少年になるか、逆に本物の OCR 失敗が
# 「nimoca と同じやつ」として無視される。
VERDICT_MATCH = "一致"
VERDICT_MISMATCH = "不一致"
VERDICT_MISMATCH_GAP = "不一致(頁欠の可能性)"
VERDICT_DUPLICATE_PAGE = "重複頁"
VERDICT_COUNT_OK = "件数一致"
VERDICT_COUNT_NG = "件数不一致"
VERDICT_NOT_APPLICABLE = "検算不可"   # 文書に合計が構造的に存在しない（nimoca）
VERDICT_UNVERIFIABLE = "検算不能"     # 取れるはずが取れなかった

SEVERITY_HIGH = "high"      # 遡及標色の対象（tag_rules の「赤系」）
SEVERITY_MEDIUM = "medium"  # 元帳行のみ。遡及標色しない

# ── doc_type ごとの検算方針 ────────────────────────────────────────
# 登録漏れは「検算が黙って掛からない」状態を生む。CLAUDE.md が名指しする
# ENTRY_BUILDERS 未登録と同じ破壊様式なので import 時に全 DocType を検証する。
POLICY_AMOUNT_REQUIRED = "amount_required"  # 合計が無ければ OCR 失敗を疑う
POLICY_COUNT_ONLY = "count_only"            # 金額合計は構造的に無い（F-7）
POLICY_NONE = "n/a"

RECON_POLICY = {
    DocType.RECEIPT: POLICY_NONE,
    DocType.PURCHASE_INVOICE: POLICY_NONE,
    DocType.SALES_INVOICE: POLICY_NONE,
    DocType.SALARY_SLIP: POLICY_NONE,
    DocType.CREDIT_CARD: POLICY_AMOUNT_REQUIRED,
    DocType.TRANSIT_IC: POLICY_COUNT_ONLY,
}


def _validate_recon_policy():
    missing = [d for d in DocType.ALL if d not in RECON_POLICY]
    if missing:
        raise RuntimeError(
            "RECON_POLICY に未登録の doc_type: %s"
            "（登録漏れは検算が黙って掛からない状態で本番に出る）" % missing)


_validate_recon_policy()

# ── 印字ラベル → 区画（F-3 / F-5 の実測ラベルでシード）─────────────────
# 「今回ご請求金額」「今回のお支払金額」「ご請求金額」は発行体によって指す区画が
# 違う（曖昧）ので敢えて登録しない。区画が 1 つしかない文書ならフォールバックで
# current_usage として扱われ、複数区画あるなら検算不能へ倒れる（推測で選ばない）。
TOTAL_LABEL_SECTION = {
    "今月ご利用額合計": SECTION_CURRENT_USAGE,
    "今回ご利用・ご請求金額合計": SECTION_CURRENT_USAGE,
    "ご請求金額合計": SECTION_CURRENT_USAGE,
    "お支払合計": SECTION_CURRENT_USAGE,
    "お支払い小計": SECTION_CURRENT_USAGE,
    "今期のお支払い金額総合計": SECTION_CURRENT_USAGE,
    "お支払い金額合計": SECTION_PAYMENT_SUMMARY,
    "今回ご請求内容": SECTION_PAYMENT_SUMMARY,
    # ポイント区画の数値は**円貨の勘定ではない**（F-6: ENEOS p1 の
    # 「このカードのポイント明細」は 前月 3,050 / 獲得 540 / 交換 −3,000 /
    # 残高 590 を印字する）。ここを登録しないと SECTION_UNKNOWN に落ち、
    # 「印字合計が 1 つだけなら current_usage とみなす」フォールバックが
    # **ポイント残高を請求金額として検算してしまう**。
    "このカードのポイント明細": SECTION_POINT,
    "ポイント明細": SECTION_POINT,
    "永久不滅ポイント": SECTION_POINT,
    "ポイント残高": SECTION_POINT,
    "獲得ポイント": SECTION_POINT,
}
# 長いラベルから照合する（「ご請求金額合計」が「ご請求金額」に食われないため）
_LABEL_ORDER = tuple(sorted(TOTAL_LABEL_SECTION, key=len, reverse=True))

# ── 上限（無制限成長の禁止）────────────────────────────────────────
MAX_CARDS_PER_FILE = 16       # 実測最大は TS CUBIC の 3 枚（F-1）。5 倍の余裕
MAX_PRINTED_TOTALS = 8
MAX_NONBOOKABLE_KINDS = 5
MAX_DUP_PAGES = 16
MAX_NOTES = 16
OVERFLOW_KEY = "__overflow__"

# ── config オーバライド（config が無くても動く。Plan §9.5）──────────────
try:
    from config import RECON_TOLERANCE_YEN as _CFG_TOLERANCE
except ImportError:
    # 領収書の 2 円許容（receipt_aggregation.TOTAL_MISMATCH_TOLERANCE_YEN）は
    # 税抜/税込の丸め由来。カード明細は印字済み税込金額の純粋な加算であり、
    # F-4 の 3 例すべてが厳密一致しているので 0 円とする。
    _CFG_TOLERANCE = 0

try:
    from config import TRANSIT_IC_BOOK_CHARGE_ROWS as _CFG_BOOK_CHARGE
except ImportError:
    _CFG_BOOK_CHARGE = False   # TBD-2 既定: 乗車を記帳するのでチャージは記帳しない

RECON_TOLERANCE_YEN = _CFG_TOLERANCE


# ── 入力 DTO（元帳は保持しない。observe_page の引数としてのみ生きる）────────
class DetailLine(NamedTuple):
    amount: int
    section: str = SECTION_UNKNOWN
    kind: str = KIND_EXPENSE
    description: str = ""      # 非記帳内訳の分類にだけ使い、元帳は畳んで捨てる


class PrintedTotal(NamedTuple):
    label: str
    amount: Optional[int]
    count: Optional[int] = None      # UC「合計 12口」/ JCB「32口」/ nimoca「全XXX件」
    page: int = 0
    is_handwritten: bool = False     # True は基準値に採らない（F-10）


class CardIdent(NamedTuple):
    member_no: Optional[str] = None
    issuer: Optional[str] = None
    statement_page_n: Optional[int] = None
    statement_page_total: Optional[int] = None
    statement_no: Optional[str] = None


# ── 出力 DTO ──────────────────────────────────────────────────────
class CardVerdict(NamedTuple):
    card_key: str
    issuer: str
    key_confidence: str
    page_range: tuple
    section_used: str
    printed_total: Optional[int]
    detail_sum: int
    diff: Optional[int]
    printed_count: Optional[int]
    detail_count: int
    verdict: str
    severity: Optional[str]
    bookable_rows: int
    expense_sum: int                 # kind ∈ {expense, fee, unknown} の和（受入基準 A3）
    nonbookable: tuple               # ((kind, 件数, 金額), ...)
    dup_pages: tuple
    notes: tuple


def section_for_label(label):
    """印字ラベルから区画を決める。**Gemini の自己申告を最終的に信用しない。**

    未知ラベルは SECTION_UNKNOWN。呼出側が元帳に逐語で残すので、
    決定表は実データから育てられる。
    """
    if not label:
        return SECTION_UNKNOWN
    text = str(label).strip()
    if text in TOTAL_LABEL_SECTION:
        return TOTAL_LABEL_SECTION[text]
    for known in _LABEL_ORDER:
        if known in text:
            return TOTAL_LABEL_SECTION[known]
    return SECTION_UNKNOWN


def normalize_card_key(ident, current_key, expected_next_n, page_num=0):
    """(key, key_confidence) を返す。

    **信頼度の低い頁は絶対に新カードを作らない**（継続扱いにする）。
    新カードを作る誤りは「本物のカードの明細が 2 分割されて両方とも不一致」を
    生むのに対し、継続の誤りは「別カードが 1 枚に合流して不一致」を生む。
    どちらも警告は出るが、前者は**存在しないカードを台帳に増やす**ので
    人が原票と突合できなくなる。よって継続に倒す。
    """
    member_no = getattr(ident, "member_no", None)
    digits = "".join(c for c in str(member_no or "") if c.isdigit())
    issuer = _norm_issuer(getattr(ident, "issuer", None))

    if len(digits) >= 4:
        return "%s:%s" % (issuer, digits[-5:]), "member_no"

    statement_no = getattr(ident, "statement_no", None)
    if statement_no:
        return "%s:S%s" % (issuer, statement_no), "statement_no"

    n = getattr(ident, "statement_page_n", None)
    if current_key and n is not None and n == expected_next_n:
        return current_key, "page_seq"
    if current_key:
        return current_key, "positional"
    return "%s:P%s" % (issuer, page_num), "positional_weak"


# 発行体が読めなかったことを表す印。「別の発行体」とは区別する
# （区別しないとカードの合流判定が過剰にも過小にもなる）。
UNKNOWN_ISSUER = "?"


def _norm_issuer(issuer):
    """発行体名を鍵に使える形へ。**空白のみは「読めなかった」に畳む。**

    先に strip してから哨兵へ倒すのが要点。`issuer or UNKNOWN_ISSUER` を
    先に評価すると `"   "` は真なのでそのまま通り、strip 後に `""` になって
    哨兵と一致しなくなる。すると `_find_mergeable` が「別の発行体」と誤認し、
    同じカードの頁が別々の元帳へ割れて偽の不一致が出る。
    """
    normalized = str(issuer or "").strip().lower()
    return normalized or UNKNOWN_ISSUER


# 券面と OCR 出力に現れる負号のゆれ。**先頭 1 文字だけ**を見る
# （店名中のダッシュを符号として解釈しないため）。
#   − U+2212 … 事実台帳 F-5 の「ENEOSポイントキャッシュバック −3,000」がこれ
#   ▲ △      … 日本の会計慣習の負数表記
_RE_LEADING_MINUS = re.compile(r"^[-\u2010-\u2015\u2212▲△]")
_AMOUNT_JUNK = (",", "￥", "¥", "円", " ", "　")
_RE_INTEGER = re.compile(r"[+-]?\d+")


def _coerce_int(value):
    """数値化できなければ None（例外にしない）。

    負号を取りこぼすと `_absorb_lines` がその行を `unparsable_amount` として
    捨て、ポイント充当や前回分清算が検算母集合から落ちる。F-4 の
    `7,380 + 11,123 − 3,000 = 15,503` は負数行を含めて初めて一致するので、
    ここは偽の不一致に直結する。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = unicodedata.normalize("NFKC", value).strip()
        for junk in _AMOUNT_JUNK:
            s = s.replace(junk, "")
        s = _RE_LEADING_MINUS.sub("-", s, count=1)
        # `int(s)` ではなく明示的な形式検査を使う。`int()` は `1_000` のような
        # Python の数値リテラル記法まで受け入れてしまい、page_dedup 側の
        # 実装（fullmatch）と解釈が割れる —— 同じ行が指紋側では「解析不能」、
        # 検算側では 1000 になる。券面にその記法は出ないので、厳しい側で揃える。
        if _RE_INTEGER.fullmatch(s):
            return int(s)
    return None


class _CardAccum:
    """1 カード分の累計。**明細行そのものは持たない。**"""

    __slots__ = ("key", "issuer", "key_confidence", "first_page", "last_page",
                 "statement_pages_seen", "statement_total_pages",
                 "sum_by_section", "count_by_section", "printed_totals",
                 "printed_counts", "bookable_rows", "expense_sum",
                 "nonbookable_by_kind", "dup_pages", "notes")

    def __init__(self, key, issuer, key_confidence, page):
        self.key = key
        self.issuer = issuer
        self.key_confidence = key_confidence
        self.first_page = page
        self.last_page = page
        self.statement_pages_seen = set()
        self.statement_total_pages = None
        self.sum_by_section = {}
        self.count_by_section = {}
        self.printed_totals = []     # (section, label, amount, page)
        self.printed_counts = []     # (count, page)
        self.bookable_rows = 0
        self.expense_sum = 0
        self.nonbookable_by_kind = {}
        self.dup_pages = []
        self.notes = set()

    def note(self, text):
        if len(self.notes) < MAX_NOTES:
            self.notes.add(text)


class FileReconLedger:
    """1 ファイル分の跨頁検算元帳。

    結算は **EOF で一括**（早期結算という操作を設計から消す）。
    途中で必要なのは帰属だけで、帰属はキー辞書への加算なので順序も閉包も要らない。
    `n/N` は結算トリガーにしない —— F-1 のアメックス カードA は 1/6・2/6 の 2 頁
    しか PDF に無いのに F-4 でその 2 頁の相加が印字合計と一致する。`N/N` の不在を
    「未完」と解釈したらこの文書は永久に結算されない。
    """

    def __init__(self, doc_type, total_pages, start_page=1, book_charge_rows=None):
        """
        book_charge_rows: nimoca「入金（チャージ）」行を記帳対象に含めるか。
            None なら config（TBD-2 既定 False）に従う。**構築時にのみ決まる** ——
            元帳は observe_page で逐次畳み込むので、途中で方針が変わると
            前半の頁が旧方針・後半が新方針で集計され、型では防げない
            不変式違反になる。TBD-2 が「記帳する」に転んでも動くのは
            config の 1 行だけで足りる。
        """
        self.doc_type = doc_type
        self.policy = RECON_POLICY.get(doc_type, POLICY_NONE)
        self.total_pages = total_pages
        self.partial_scan = bool(start_page and start_page > 1)
        self.cards = {}
        self.current_key = None
        self.degraded = False
        self.unknown_labels = set()
        self._last_n = None
        self._book_charge_rows = (bool(_CFG_BOOK_CHARGE) if book_charge_rows is None
                                  else bool(book_charge_rows))

    def observe_page(self, page_num, ident, printed_totals, lines,
                     is_duplicate=False):
        """1 頁分を累計へ畳み込み、card_key を返す。

        **例外を絶対に外へ出さない**。検算は検査器であって記帳経路ではないので、
        ここで落ちて頁の記帳が止まるのは本末転倒（fail-open）。
        """
        if self.policy == POLICY_NONE:
            return ""
        try:
            return self._observe_page(page_num, ident, printed_totals, lines,
                                      is_duplicate)
        except Exception as e:  # noqa: BLE001 - 検算ごときで記帳を止めない
            print("⚠️ 検算元帳の畳み込みに失敗（記帳は継続）: %s: %s"
                  % (type(e).__name__, e))
            card = self.cards.get(self.current_key)
            if card is not None:
                card.note("ledger_error:p%s" % page_num)
            return self.current_key or ""

    def _observe_page(self, page_num, ident, printed_totals, lines, is_duplicate):
        key, confidence = normalize_card_key(
            ident, self.current_key, self._expected_n(), page_num)
        card = self._get_or_create(key, ident, confidence, page_num)
        # 「直前カード」＝最後に処理した実カード。信頼度で絞ってはいけない ——
        # 会員番号が 1 頁も読めない明細書（明細書番号だけ・識別情報皆無）では
        # current_key が永久に None のままになり、§2.2 のフォールバック連鎖
        # 3/4（頁を直前カードへ継がせる）が到達不能になる。結果、1 通の明細書が
        # 頁ごとに別カードへ割れ、実在しないカードが台帳に増えたうえで
        # 全カードが偽の不一致（赤系）になる。
        # 溢れバケットだけは除く（帰属推定の基準に据えると全頁を巻き込む）。
        if card.key != OVERFLOW_KEY:
            self.current_key = card.key

        n = getattr(ident, "statement_page_n", None)
        total_n = getattr(ident, "statement_page_total", None)
        if n is not None:
            self._last_n = n

        # 重複頁は金額も件数も足さない（17,295 が 34,590 になるのを防ぐ）。
        # ただし**検算が「一致」で通ってしまう**点に注意 —— MF タブには重複行が
        # 実際に 2 倍書かれているので、重複は合計不一致とは独立した赤信号として
        # 別建てで報告する。
        if is_duplicate:
            if len(card.dup_pages) < MAX_DUP_PAGES:
                card.dup_pages.append(page_num)
            card.note("duplicate_page")
            card.last_page = max(card.last_page, page_num)
            return card.key

        if n is not None:
            card.statement_pages_seen.add(n)
        if total_n:
            card.statement_total_pages = total_n

        self._absorb_printed_totals(card, printed_totals, page_num)
        self._absorb_lines(card, lines)
        card.last_page = max(card.last_page, page_num)
        return card.key

    def _expected_n(self):
        return None if self._last_n is None else self._last_n + 1

    def _get_or_create(self, key, ident, confidence, page_num):
        card = self.cards.get(key)
        if card is not None:
            return card

        upgraded = self._upgrade_weak_predecessor(key, confidence, page_num)
        if upgraded is not None:
            return upgraded

        merged = self._find_mergeable(key)
        if merged is not None:
            merged.note("key_merged")
            return merged

        if len(self.cards) >= MAX_CARDS_PER_FILE:
            # 上限超過は捨てるのではなく 1 個の可視な劣化事実にする。
            self.degraded = True
            return self._overflow_bucket(page_num)

        card = _CardAccum(key, _norm_issuer(getattr(ident, "issuer", None)),
                          confidence, page_num)
        self.cards[key] = card
        return card

    def _upgrade_weak_predecessor(self, key, confidence, page_num):
        """仮キーで始まったカードを、後続頁の確かな会員番号へ引き継ぐ。

        先頭頁の識別情報が読めないと `?:P1` のような仮キーでカードが立つ。
        その次の頁で会員番号が読めたときに素直に新カードを作ると、
        **印字合計（先頭頁）と明細（次頁）が別々の元帳へ割れて両方が
        偽の不一致になる**。頁が連続していることを条件に、仮キーの
        カードを本キーへ改名して引き継ぐ。

        頁が飛んでいるなら引き継がない —— そこは本当に別カードの
        始まりでありうるので、合流させると別カードを混ぜてしまう。
        """
        if confidence != "member_no" or not self.current_key:
            return None
        prev = self.cards.get(self.current_key)
        if prev is None or prev.key_confidence != "positional_weak":
            return None
        if prev.last_page + 1 != page_num:
            return None

        del self.cards[prev.key]
        prev.key = key
        prev.key_confidence = "member_no"
        prev.note("key_upgraded")
        self.cards[key] = prev
        return prev

    def _find_mergeable(self, key):
        """同一明細書が頁ごとに違う読み取り結果になる場合の吸収。

        合流の条件は「番号が接尾辞一致」かつ「発行体が矛盾しない」。
        **`?`（読めなかった）は矛盾に数えない** —— 発行体名や T番号は先頭頁の
        ヘッダにしか印字されない文書が多いのに対し、会員番号は各頁に出る。
        一致を厳格に要求すると 1 通の明細書が `amex:26003` と `?:26003` に割れ、
        両方が偽の不一致（赤系）になる。

        逆に**両方の発行体が読めていて食い違う**場合は合流しない。末尾 5 桁の
        偶然の一致で別カードを混ぜると、そちらは誰にも検出できない。
        """
        issuer, _, digits = key.partition(":")
        if not digits.isdigit() or len(digits) < 4:
            return None
        for other_key, card in self.cards.items():
            other_issuer, _, other_digits = other_key.partition(":")
            if not other_digits.isdigit():
                continue
            if other_issuer != issuer and UNKNOWN_ISSUER not in (issuer, other_issuer):
                continue
            if digits.endswith(other_digits) or other_digits.endswith(digits):
                return card
        return None

    def _overflow_bucket(self, page_num):
        card = self.cards.get(OVERFLOW_KEY)
        if card is None:
            card = _CardAccum(OVERFLOW_KEY, "?", "overflow", page_num)
            card.note("card_overflow")
            self.cards[OVERFLOW_KEY] = card
        return card

    def _absorb_printed_totals(self, card, printed_totals, page_num):
        for pt in printed_totals or ():
            if not hasattr(pt, "amount"):
                continue
            # 手書き数値を基準値に採ると検算の基準そのものが汚染される
            # （F-10: 楽天 p1 の手書き「ETC代 12,940円」は印字の請求金額欄に重なる）
            if getattr(pt, "is_handwritten", False):
                card.note("handwritten_total_ignored")
                continue

            label = getattr(pt, "label", None)
            section = section_for_label(label)
            if section == SECTION_UNKNOWN and label:
                self.unknown_labels.add(str(label).strip())

            amount = _coerce_int(getattr(pt, "amount", None))
            page = getattr(pt, "page", None) or page_num
            if amount is not None and len(card.printed_totals) < MAX_PRINTED_TOTALS:
                entry = (section, str(label or ""), amount, page)
                if entry not in card.printed_totals:
                    card.printed_totals.append(entry)

            count = _coerce_int(getattr(pt, "count", None))
            if count is not None and len(card.printed_counts) < MAX_PRINTED_TOTALS:
                card.printed_counts.append((count, page))

    def _absorb_lines(self, card, lines):
        for ln in lines or ():
            amount = _coerce_int(getattr(ln, "amount", None))
            if amount is None:
                card.note("unparsable_amount")
                continue
            kind = getattr(ln, "kind", KIND_EXPENSE) or KIND_EXPENSE
            section = getattr(ln, "section", SECTION_UNKNOWN) or SECTION_UNKNOWN

            if kind == KIND_CARRY_OVER:
                # F-5 が「別集計」と明記した確認済み事実。Gemini が何と言おうと
                # 当月利用の区画には入れない（入れると 933,680 が崩れる）。
                section = SECTION_PAYMENT_SUMMARY
            if kind not in KIND_ALL:
                card.note("unknown_kind:%s" % str(kind)[:16])
                kind = KIND_UNKNOWN
            elif kind == KIND_UNKNOWN:
                card.note("unknown_kind")

            card.sum_by_section[section] = card.sum_by_section.get(section, 0) + amount
            card.count_by_section[section] = card.count_by_section.get(section, 0) + 1

            if self._is_bookable(kind):
                card.bookable_rows += 1
                if kind in (KIND_EXPENSE, KIND_FEE, KIND_UNKNOWN):
                    card.expense_sum += amount
            else:
                cnt, amt = card.nonbookable_by_kind.get(kind, (0, 0))
                if kind in card.nonbookable_by_kind or \
                        len(card.nonbookable_by_kind) < MAX_NONBOOKABLE_KINDS:
                    card.nonbookable_by_kind[kind] = (cnt + 1, amt + amount)
                else:
                    o_cnt, o_amt = card.nonbookable_by_kind.get("other", (0, 0))
                    card.nonbookable_by_kind["other"] = (o_cnt + 1, o_amt + amount)

    def _is_bookable(self, kind):
        if kind == KIND_CHARGE:
            return self._book_charge_rows
        return kind != KIND_CARRY_OVER

    # ── 結算 ───────────────────────────────────────────────────
    def finalize(self, had_page_errors=False):
        """EOF で一括結算する。呼ぶのは**ファイルが歸檔される経路のみ**。

        全頁失敗（Failed 保持）で呼ぶとリトライのたびに元帳行が増殖する
        （CLAUDE.md §3 の冪等要求）。
        """
        if self.policy == POLICY_NONE:
            return ()
        forced = None
        if self.partial_scan:
            forced = "%s(部分処理)" % VERDICT_UNVERIFIABLE
        if had_page_errors:
            # 頁エラーの明細は Sheets に書かれていない（main.py の部分失敗経路）。
            # 降格しないと「合計不一致」という誤った診断名が付き、人が記帳内容を
            # 疑って時間を溶かす。
            forced = "%s(頁エラー)" % VERDICT_UNVERIFIABLE
        if self.degraded:
            forced = "%s(カード数超過)" % VERDICT_UNVERIFIABLE
        return tuple(self._verdict_for(card, forced)
                     for card in self.cards.values())

    def _verdict_for(self, card, forced):
        notes = set(card.notes)
        if card.statement_total_pages and \
                len(card.statement_pages_seen) < card.statement_total_pages:
            notes.add("頁欠 %d/%d" % (len(card.statement_pages_seen),
                                      card.statement_total_pages))

        detail_count = sum(card.count_by_section.values())
        printed_count, count_note = self._resolve_printed_count(card)
        if count_note:
            notes.add(count_note)

        def _mk(verdict, severity, section=SECTION_UNKNOWN, printed_total=None,
                detail_sum=0, diff=None):
            return CardVerdict(
                card_key=card.key, issuer=card.issuer,
                key_confidence=card.key_confidence,
                page_range=(card.first_page, card.last_page),
                section_used=section, printed_total=printed_total,
                detail_sum=detail_sum, diff=diff,
                printed_count=printed_count, detail_count=detail_count,
                verdict=verdict, severity=severity,
                bookable_rows=card.bookable_rows, expense_sum=card.expense_sum,
                nonbookable=tuple(sorted(
                    (k, c, a) for k, (c, a) in card.nonbookable_by_kind.items())),
                dup_pages=tuple(card.dup_pages),
                notes=tuple(sorted(notes)))

        if card.dup_pages:
            return _mk(VERDICT_DUPLICATE_PAGE, SEVERITY_HIGH,
                       detail_sum=card.sum_by_section.get(SECTION_CURRENT_USAGE, 0)
                       + card.sum_by_section.get(SECTION_UNKNOWN, 0))
        if forced:
            return _mk(forced, SEVERITY_MEDIUM)

        section, total, conflict = _choose_section_and_total(card.printed_totals)
        if conflict:
            return _mk("%s(%s)" % (VERDICT_UNVERIFIABLE, conflict), SEVERITY_MEDIUM)

        if total is None:
            if self.policy == POLICY_COUNT_ONLY:
                # nimoca は金額合計が構造的に無い（F-7）。件数だけが検算できる。
                # 警告色を付けないのが要点 —— 通勤費なので毎月来る。
                if printed_count is None:
                    return _mk(VERDICT_NOT_APPLICABLE, None)
                if printed_count == detail_count:
                    return _mk(VERDICT_COUNT_OK, None)
                return _mk(VERDICT_COUNT_NG, SEVERITY_MEDIUM)
            return _mk(VERDICT_UNVERIFIABLE, SEVERITY_MEDIUM)

        detail_sum = card.sum_by_section.get(section, 0)
        if section != SECTION_UNKNOWN and SECTION_UNKNOWN in card.sum_by_section:
            # 過小警告（静かな誤り）より過剰警告に倒す。
            detail_sum += card.sum_by_section[SECTION_UNKNOWN]
            notes.add("unknown_section_lines:%d"
                      % card.count_by_section.get(SECTION_UNKNOWN, 0))

        diff = detail_sum - total
        if abs(diff) <= RECON_TOLERANCE_YEN:
            return _mk(VERDICT_MATCH, None, section, total, detail_sum, diff)
        has_gap = any(n.startswith("頁欠") for n in notes)
        return _mk(VERDICT_MISMATCH_GAP if has_gap else VERDICT_MISMATCH,
                   SEVERITY_HIGH, section, total, detail_sum, diff)

    def _resolve_printed_count(self, card):
        values = {c for c, _ in card.printed_counts}
        if not values:
            return None, None
        if len(values) == 1:
            return values.pop(), None
        return max(values), "printed_count_conflict"

    def stored_object_count(self):
        """メモリ不変式テスト用: 保持オブジェクト総数（行数に比例しないこと）。"""
        total = len(self.cards) + len(self.unknown_labels)
        for card in self.cards.values():
            total += (len(card.statement_pages_seen) + len(card.sum_by_section)
                      + len(card.count_by_section) + len(card.printed_totals)
                      + len(card.printed_counts) + len(card.nonbookable_by_kind)
                      + len(card.dup_pages) + len(card.notes))
        return total


def _choose_section_and_total(printed_totals):
    """(section, total, conflict_reason) を返す。**推測で片方を選ばない。**

    1. `current_usage` の印字合計があればそれを使う
    2. 無く、印字合計が全体で 1 種類だけなら（＝区画が 1 つしかない文書）
       それを current_usage とみなす
    3. 複数あって特定できない → 検算不能（区画曖昧）
    """
    if not printed_totals:
        return SECTION_UNKNOWN, None, None

    current = {amount for section, _, amount, _ in printed_totals
               if section == SECTION_CURRENT_USAGE}
    if len(current) == 1:
        return SECTION_CURRENT_USAGE, current.pop(), None
    if len(current) > 1:
        # F-3: 大半の発行体が先頭頁と明細末尾の 2 箇所に印字する。値が食い違うのは
        # OCR 誤読の可能性が高く、どちらを基準にしても誤診になる。
        return SECTION_CURRENT_USAGE, None, "合計不整合"

    # フォールバックの母集合は **区画が判らないラベルだけ**。
    #   - ポイント区画は円貨の勘定ではない（F-6）。これを基準にすると
    #     「ポイント残高しか読めなかった頁」で残高を請求額として検算する。
    #   - payment_summary と**判っている**ものを current_usage に読み替えるのも
    #     同じ誤り。アメックス カードB は「お支払い金額合計」（前回分口座振替
    #     −35,808 を含む）と「今月ご利用額合計 933,680」が別区画で並ぶ（F-5）。
    #     前者だけ読めたときに当月利用の明細と突合すると、35,808 円ちょうど
    #     ずれた偽の不一致（赤系）が出る。
    # どちらも「基準が無い」＝検算不能へ倒すのが正しい（推測で基準を作らない）。
    unknown_totals = {amount for section, _, amount, _ in printed_totals
                      if section == SECTION_UNKNOWN}
    if len(unknown_totals) == 1:
        return SECTION_CURRENT_USAGE, unknown_totals.pop(), None
    if not unknown_totals:
        return SECTION_UNKNOWN, None, None
    return SECTION_UNKNOWN, None, "区画曖昧"
