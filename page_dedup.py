# page_dedup.py
# クレジットカード明細の「同一原紙を 2 度スキャンした頁」を検出する。
#
# 設計の中心は **二重署名 AND 規則**:
#   識別キー（券面ヘッダ由来）と 内容ダイジェスト（明細本体由来）の
#   両方が一致したときにだけ duplicate と判定する。片方だけでは除外しない。
#
# なぜそこまで慎重か: 偽陽性は正当な仕訳を消す。つまり重複検出そのものが
# IP-401「頁は無音で消えない」不変式への脅威になりうる。見逃し（2 重記帳）は
# 検算とタグで気づけるが、消えた仕訳は誰にも気づかれない。
# したがって迷ったら必ず「重複ではない」側に倒す（fail-open）。
#
# 外部依存なし・副作用なし。1 ファイル（= 1 回の process_pipeline 呼出）分の寿命。

import hashlib
import re
import unicodedata

# 明細 1 行だけの頁は判定対象にしない。ETC の定額通行料のように
# 同額・同日が並ぶ世界では、1 行の一致は偶然でも起きる。
MIN_DETAIL_LINES = 2
# 会員番号として認める最小長。実測の券面はすべて 12 文字以上（区切り込み）
MIN_ACCOUNT_KEY_LEN = 12
# マスクされていない数字がこれ未満なら識別に使えない
MIN_VISIBLE_DIGITS = 4

VERDICT_UNIQUE = "unique"
VERDICT_DUPLICATE = "duplicate"          # ← 除外に至る唯一の値
VERDICT_KEY_CONFLICT = "key_conflict"
VERDICT_CONTENT_ONLY = "content_only"
VERDICT_NOT_ELIGIBLE = "not_eligible"

_RE_NON_KEY = re.compile(r"[\s\-‐-―ー_.]")
_RE_MASK = re.compile(r"[xX×＊*]")


def _normalize_for_match(text):
    """OCR テキストの表記ゆれを吸収する（NFKC + 空白除去）。"""
    if not text:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text)))


def _normalize_account(raw):
    """会員番号を鍵に使える形へ正規化する。

    区切りを除き、マスク記号（x / X / ＊ / *）を '*' に畳む。
    **下 4 桁を鍵にしない**: UC `4542-3200-1424-2***` や
    JCB `3541-0409-9687-2XXX` は末尾がマスクされており、
    下 4 桁方式だと別カードが同一視されて仕訳が消える。
    """
    if not raw:
        return ""
    s = _RE_NON_KEY.sub("", unicodedata.normalize("NFKC", str(raw)))
    return _RE_MASK.sub("*", s)


def _normalize_page_label(raw):
    """「1/6 ページ」→「1/6」。"""
    if not raw:
        return ""
    s = _normalize_for_match(raw)
    m = re.search(r"(\d{1,2})/(\d{1,2})", s)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def _normalize_period(raw):
    """請求月を YYYY-MM に寄せる。取れなければ空文字。"""
    if not raw:
        return ""
    s = _normalize_for_match(raw)
    m = re.search(r"(20\d{2})[-/年]?(\d{1,2})", s)
    return f"{m.group(1)}-{int(m.group(2)):02d}" if m else ""


def _normalize_date(raw):
    if not raw:
        return ""
    s = _normalize_for_match(raw)
    m = re.search(r"(\d{1,4})[/年.-](\d{1,2})[/月.-](\d{1,2})", s)
    return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}" if m else s


# 券面と OCR 出力に現れる負号のゆれ。**先頭 1 文字だけ**を見る
# （店名中のダッシュを符号として解釈しないため）。
#   − U+2212 … 事実台帳 F-5 の「ENEOSポイントキャッシュバック −3,000」がこれ
#   ▲ △      … 日本の会計慣習の負数表記
# card_reconciliation / invoice_classification と挙動を揃えること
# （test_card_reconciliation.LabelTableDriftTest が 3 者の一致を突合する）。
_RE_LEADING_MINUS = re.compile(r"^[-\u2010-\u2015\u2212▲△]")
_AMOUNT_JUNK = (",", "￥", "¥", "円", " ", "　")


def _coerce_int(v):
    """数値化できなければ None（例外にしない）。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        s = unicodedata.normalize("NFKC", v).strip()
        for junk in _AMOUNT_JUNK:
            s = s.replace(junk, "")
        s = _RE_LEADING_MINUS.sub("-", s, count=1)
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
    return None


class PageIdentity:
    """券面ヘッダ由来の識別情報。これ単独では重複判定に使わない。"""

    __slots__ = ("issuer", "account_key", "page_label", "period")

    def __init__(self, issuer="", account_key="", page_label="", period=""):
        self.issuer = issuer
        self.account_key = account_key
        self.page_label = page_label
        self.period = period

    def is_complete(self):
        return (len(self.account_key) >= MIN_ACCOUNT_KEY_LEN
                and sum(c.isdigit() for c in self.account_key) >= MIN_VISIBLE_DIGITS
                and bool(self.page_label))

    def match_key(self):
        # period は入れない。「同じ頁番号が別の月に来る」のは正常なので、
        # period は一致判定ではなく拒否権（period_vetoes）として使う。
        return (self.issuer, self.account_key, self.page_label)

    def __repr__(self):
        return f"PI({self.issuer}/{self.account_key}/{self.page_label}/{self.period})"


def period_vetoes(a, b):
    """両頁に請求月があり、それが食い違う → 別の明細書。重複ではない。"""
    return bool(a.period) and bool(b.period) and a.period != b.period


def _visible_digits_in(haystack, account_key):
    """会員番号の可視数字が OCR 本文にも出ているか（Gemini の裏取り）。

    **haystack は `account_key` と同じ規則で正規化する。** `account_key` は
    `_normalize_account` が区切りを落とした形なのに、haystack 側に区切りが
    残っていると、下 4 桁が区切りを跨ぐ券面で照合が必ず失敗する。

    実測（2026-08-20 の実施後評審）: UC `4542-3200-1424-2***` の下 4 桁は
    `4242` だが、券面には `1424-2` と印字されているので区切りを残した
    haystack には現れない。結果 `account_key=""` → `is_complete()` False →
    **その券面では重複判定が永久に不成立**になっていた（JCB
    `3541-0409-9687-2XXX` も同じ）。どちらも `_normalize_account` の
    docstring が「実測の券面」として名指ししているものである。
    """
    digits = "".join(c for c in account_key if c.isdigit())
    if len(digits) < MIN_VISIBLE_DIGITS:
        return False
    tail = digits[-MIN_VISIBLE_DIGITS:]
    return tail in _RE_NON_KEY.sub("", haystack)


def extract_page_identity(ocr_text, raw_data):
    """Gemini の構造化フィールド × OCR 生テキストの二重確認で identity を作る。

    片方にしか無い要素は「欠損」に倒す。欠損した頁は重複判定の資格を失い、
    そのまま記帳される（＝安全側）。
    """
    o = _normalize_for_match(ocr_text)
    card = (raw_data or {}).get("card") or {}

    acc = _normalize_account(card.get("member_no"))
    if not (acc and _visible_digits_in(o, acc)):
        acc = ""

    page = _normalize_page_label(card.get("statement_page"))
    if page and page.replace("/", "") not in o.replace("/", ""):
        page = ""

    return PageIdentity(
        issuer=str(card.get("issuer") or "").strip().upper(),
        account_key=acc,
        page_label=page,
        period=_normalize_period(card.get("period")),
    )


def build_content_digest(raw_data):
    """(digest, line_count, positive_total) を返す。

    採るのは **日付と金額と頁合計だけ**。店名・摘要・OCR 生テキスト・画像は
    入れない —— 手書き注記（F-10: p1 にはあり p3 には無い）や Gemini の
    文字列ゆれを構造的に締め出すため。並び順は券面のまま保つ
    （ETC の同額行が並ぶとき、順序が識別の助けになる）。
    """
    rows = (raw_data or {}).get("rows") or []
    pairs = []
    positive_total = 0
    for ln in rows:
        if not isinstance(ln, dict):
            continue
        amt = _coerce_int(ln.get("amount"))
        if amt is None:
            continue
        pairs.append((_normalize_date(ln.get("date")), amt))
        if amt > 0:
            positive_total += amt
    if not pairs:
        return "", 0, 0
    payload = repr((pairs, _coerce_int((raw_data or {}).get("total_amount")) or 0))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), len(pairs), positive_total


class PageFingerprint:
    """1 頁分の要約。**明細行そのものは持たない**（逐頁メモリモデル厳守）。"""

    __slots__ = ("identity", "digest", "line_count", "positive_total", "ocr_text_len")

    def __init__(self, identity, digest, line_count, positive_total, ocr_text_len):
        self.identity = identity
        self.digest = digest
        self.line_count = line_count
        self.positive_total = positive_total
        self.ocr_text_len = ocr_text_len

    def is_eligible(self):
        """重複判定の資格。満たさない頁は常に記帳される。"""
        return (self.line_count >= MIN_DETAIL_LINES
                and self.positive_total > 0
                and bool(self.digest)
                and self.identity.is_complete())

    def __repr__(self):
        return (f"FP({self.identity!r},{self.digest[:8]},"
                f"n={self.line_count},sum={self.positive_total})")


class DedupVerdict:
    __slots__ = ("kind", "origin_page", "detail")

    def __init__(self, kind, origin_page=None, detail=""):
        self.kind = kind
        self.origin_page = origin_page
        self.detail = detail

    def __repr__(self):
        return f"DV({self.kind},orig={self.origin_page})"


def safe_fingerprint(ocr_text, raw_data):
    """例外を絶対に外へ出さない。失敗＝None＝「重複ではない」に倒す。"""
    try:
        identity = extract_page_identity(ocr_text, raw_data)
        digest, n, pos = build_content_digest(raw_data)
        return PageFingerprint(identity, digest, n, pos, len(ocr_text or ""))
    except Exception as e:  # noqa: BLE001 - 指紋ごときで頁を落とさない
        print(f"⚠️ 指紋生成に失敗（重複判定をスキップ）: {type(e).__name__}: {e}")
        return None


def _detail(prev_fp, fp):
    """監査タブ「理由」列に載せる構造化文字列（列は増やさない）。

    書式は **`キー:引数` を `;` で連ねる**（T8d）。監査タブの理由列は
    `sheets_output.audit_reason_ja` が `;` で分割し `:` でキーと引数に
    割って日本語へ訳す規約なので、`key=…` の形だと訳されず機械語のまま
    顧客の目に残る（趙が 2026-08-20 に指摘した状態への逆戻り）。

    区切りは `;`（要素）/ `:`（キーと引数）/ `@`（引数の中）の 3 種。
    `issuer` は Gemini がカード会社名を**逐語で**返す欄なので、そこに
    区切り文字が混じると分割が壊れ、**その行だけ機械語のまま顧客の目に
    出る**（帳簿は無傷だが可読性が落ちる）。埋める値から区切りを落とす。
    """
    more = ";dup_more_text" if fp.ocr_text_len > prev_fp.ocr_text_len * 1.05 else ""
    i = prev_fp.identity
    return "dup_key:%s/%s/%s;dup_amount:%d%s" % (
        _safe_for_reason(i.issuer), _safe_for_reason(i.account_key),
        _safe_for_reason(i.page_label), fp.positive_total, more)


_RE_REASON_DELIM = re.compile(r"[;:@]")


def _safe_for_reason(text):
    """理由列の区切り文字を落とす（値がキーの境界を壊さないように）。"""
    return _RE_REASON_DELIM.sub("", str(text or ""))


class PageDedupIndex:
    """1 ファイル分の重複索引。保持するのは頁あたり数百バイトの要約のみ。"""

    def __init__(self):
        self._by_key = {}      # match_key -> (page_num, fp)
        self._by_digest = {}   # digest    -> (page_num, fp)

    def classify(self, fp, page_num):
        """★ 不変式: DUPLICATE を返す経路は「キー一致 かつ digest 一致」の一箇所のみ。
        _by_digest ヒットは CONTENT_ONLY までしか到達しない。
        """
        if fp is None or not fp.is_eligible():
            return DedupVerdict(VERDICT_NOT_ELIGIBLE)

        hit = self._by_key.get(fp.identity.match_key())
        if hit:
            prev_page, prev_fp = hit
            if period_vetoes(prev_fp.identity, fp.identity):
                return DedupVerdict(VERDICT_UNIQUE)          # 別月の同じ頁番号
            if prev_fp.digest == fp.digest:
                return DedupVerdict(VERDICT_DUPLICATE, prev_page, _detail(prev_fp, fp))
            return DedupVerdict(VERDICT_KEY_CONFLICT, prev_page, _detail(prev_fp, fp))

        hit_d = self._by_digest.get(fp.digest)
        if hit_d:
            return DedupVerdict(VERDICT_CONTENT_ONLY, hit_d[0], _detail(hit_d[1], fp))

        return DedupVerdict(VERDICT_UNIQUE)

    def remember(self, fp, page_num):
        """**実際に記帳できた頁だけ**を登録する。

        is_eligible() が positive_total > 0 を要求しているのが要点。
        sheets_output.py:244 は amount==0 の行を書かずに skip するため、
        金額ゼロの頁を「記帳済み」として登録すると、後続の真の重複頁が
        除外され、その金額は **0 回** 記帳される。

        先着優先（setdefault）: origin は常に最初の頁を指す。
        """
        if fp is None or not fp.is_eligible():
            return
        self._by_key.setdefault(fp.identity.match_key(), (page_num, fp))
        self._by_digest.setdefault(fp.digest, (page_num, fp))


class NullDedupIndex:
    """重複検出を無効化したときの既定実装（何も除外しない）。"""

    def classify(self, fp, page_num):
        return DedupVerdict(VERDICT_NOT_ELIGIBLE)

    def remember(self, fp, page_num):
        pass
