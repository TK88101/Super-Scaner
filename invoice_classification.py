# invoice_classification.py
# クレカ明細 / 交通系IC の 税区分（G列）と 控除判定（H列）。純関数のみ。
#
# 相反する 2 つの実害の間に立つモジュールである。
#
# ① 過大控除。F-14（国税庁 質疑応答事例）のとおり、カード会社が作成した請求明細書は
#    消費税法第30条第9項の請求書等に**該当しない**。MF は H列の空欄を「適格」と
#    解釈する（F-12）ので、判定が付かない行が黙って空欄に落ちる経路を作ると、
#    システムが控除不可の取引を機械的に控除してしまう。
# ② 事務所の実帳との乖離。MF 本番の実測（Plan 附録 C）では、貸方＝未払金 133 件中
#    111 件が「適格」相当で、かつ画面上の H列は**空欄**だった。1 万円超の行は
#    公共料金や店舗購入で、適格請求書は別途保存されているとみられる。
#    つまりこの運用では**カード明細は記帳のトリガであって控除の証憑ではない**。
#
# 趙裁定 9（Plan AD-8）はこう決着させた:
#   H列は空欄（事務所慣例の踏襲）＋ `amount >= 10000` の行に U列タグで注意喚起。
#
# したがって本モジュールの形は **判定はする／帳簿には税法主張を書かない**:
#   - `classify()` は全域関数として判定木を回し、根拠と留保を持った Verdict を返す
#   - `render_invoice_column()` は現行運用では**常に空文字**を返す
#   - 根拠（`basis_*` / `caveats`）は**監査集計にのみ**出し、摘要（S列）や
#     仕訳メモ（T列）には載せない。摘要に「少額特例」と書けば、それは帳簿上の
#     税法主張になる。少額特例には「基準期間の課税売上高 1 億円以下等」の
#     事業者要件があり、プログラムには判定不能である。
#
# 判定木を残すのは、TBD が動いたとき（MF の解釈が変わる／事務所方針が変わる）に
# 触る場所を 1 箇所に閉じ込めるため。空欄運用は「判定できないから空欄」ではなく
# 「判定したうえで慣例に従って空欄」であり、この差はコードの形に出しておく。
#
# 依存は doc_types / receipt_aggregation / card_reconciliation の 3 本のみ
# （いずれも純モジュール。gspread / paddleocr / google api 非依存なので
# venv 無しで単体テストできる）。card_reconciliation は記帳軸の kind 定数を
# **値の複製ではなく import** で使うため。

import functools
import re
import unicodedata
from datetime import date, datetime
from typing import Callable, NamedTuple, Optional

from card_reconciliation import KIND_CARRY_OVER, KIND_CHARGE, KIND_CREDIT_ADJUST
from doc_types import DocType
from receipt_aggregation import determine_tax_types


class DeductionClass:
    """H列の意味コード。MF の文字列とは分離する（切替点を 1 つにするため）。"""

    QUALIFIED = "qualified"              # 特例により明細のみで 100% 控除可
    NOT_DEDUCTIBLE = "not_deductible"    # F-14 原則: 控除不可
    OUT_OF_SCOPE = "out_of_scope"        # そもそも課税仕入れでない
    # 経過措置は enum に**予約するのみ**。判定木からは到達しない。
    # 「免税事業者等からの課税仕入れ」に対する制度であって「適格請求書を保存
    # できない」場合の救済ではなく、相手方が免税事業者かは明細から判定不能。
    # 加えて 2026/9/30 で 80%→50% に切り替わるため、自動適用すると
    # ある日を境に全件の控除率が静かに変わる。
    TRANSITIONAL_80 = "transitional_80"
    TRANSITIONAL_50 = "transitional_50"
    ALL = (QUALIFIED, NOT_DEDUCTIBLE, OUT_OF_SCOPE,
           TRANSITIONAL_80, TRANSITIONAL_50)


class LineKind:
    """明細行の**実体**の類型（card_reconciliation の kind＝記帳軸とは別の軸）。"""

    TRAIN = "train"          # nimoca 種別列の「電車」（F-7 実測）
    BUS = "bus"              # 同「バス」
    ETC = "etc"              # 「ＥＴＣ後納分」「ＥＴＣカード売上」（F-8 実測）
    FUEL = "fuel"            # 給油
    GENERAL = "general"
    CHARGE = "charge"        # nimoca「入金 現金」（F-7）
    POINT = "point"          # ポイント充当・キャッシュバック（F-5）
    SETTLEMENT = "settlement"  # 前回分口座振替（F-5）
    ALL = (TRAIN, BUS, ETC, FUEL, GENERAL, CHARGE, POINT, SETTLEMENT)


PUBLIC_TRANSPORT_KINDS = frozenset({LineKind.TRAIN, LineKind.BUS})

# 記帳軸（AD-5 の kind）→ 実体軸（LineKind）の一意な対応。
# Gemini に 2 軸を出させると自己矛盾しうるので、非購入の 3 種だけは記帳軸から
# 導けるようにしておく（builder の取り違えで「チャージが少額特例で適格」に
# なるような事故を構造的に潰す）。
# 左辺は card_reconciliation の定数を**値の複製ではなく import** で使う ——
# 文字列を書き写すと、あちらの語彙が変わったときにこの写像が黙って
# 素通りするようになり、上のガードが機能しなくなる。
BOOKING_KIND_TO_LINE_KIND = {
    KIND_CHARGE: LineKind.CHARGE,
    KIND_CREDIT_ADJUST: LineKind.POINT,
    KIND_CARRY_OVER: LineKind.SETTLEMENT,
}

# 実体軸を券面の逐語から決めるための表（F-7 / F-8 の実測文字列）。
# **Gemini に line_kind を自己申告させない** —— AD-4 と同じ方針で、
# 逐語だけ報告させ分類は Python の決定表で決める。申告させると
# 「電車」と申告しつつ description が給油、のような自己矛盾を裁けない。
_RE_ETC = re.compile(r"etc|ｅｔｃ")
_RE_FUEL = re.compile(r"給油|ガソリン|軽油|ハイオク|レギュラー")
_RE_TRAIN = re.compile(r"電車|鉄道|地下鉄|新幹線")
_RE_BUS = re.compile(r"バス")

# F-11 実測。**いずれもカード会社自身**の登録番号であり加盟店のものではない。
# 既存の doc 級 invoice_num をそのまま merchant_t_number に流用すると、
# F-14 が明示的に否定している控除を機械的に量産する。
CARD_ISSUER_T_NUMBERS = frozenset({
    "T8700150009366",   # アメリカン・エキスプレス
    "T8010601027383",   # トヨタファイナンス（TS CUBIC / ENEOS BUSINESS / ENEOS）
})

# ── config オーバライド（config が無くても動く。Plan §9.5）──────────────
try:
    from config import INVOICE_COL_VALUES as _CFG_INVOICE_COL_VALUES
except ImportError:
    # F-12: MF「借方インボイス」列の正規値。**空欄を明示的に許可**する
    # （事務所慣例が空欄であり、空欄は MF が「適格」と解釈する）。
    _CFG_INVOICE_COL_VALUES = ("", "適格", "80％控除", "70％控除",
                               "50％控除", "30％控除", "控除なし")

try:
    from config import INVOICE_COL_RENDERING as _CFG_INVOICE_COL_RENDERING
except ImportError:
    # 現行は全て空欄（趙裁定 9 / AD-8）。TBD が動いたらここだけを変える。
    _CFG_INVOICE_COL_RENDERING = {
        DeductionClass.QUALIFIED: "",
        DeductionClass.NOT_DEDUCTIBLE: "",
        DeductionClass.OUT_OF_SCOPE: "",
    }

try:
    from config import INVOICE_CONFIRM_THRESHOLD_YEN as _CFG_CONFIRM_THRESHOLD
except ImportError:
    # 少額特例は「税込 1 万円**未満**」なので、1 万円ちょうどは対象外＝要確認。
    # 判定は `>=` で行う（`>` ではない）。
    _CFG_CONFIRM_THRESHOLD = 10000

try:
    from config import INVOICE_CONFIRM_TAG as _CFG_CONFIRM_TAG
except ImportError:
    _CFG_CONFIRM_TAG = "1万円以上: インボイス確認"

try:
    from config import SMALL_AMOUNT_TAXPAYER_CONFIRMED as _CFG_TAXPAYER_CONFIRMED
except ImportError:
    # 少額特例の事業者要件（基準期間の課税売上高 1 億円以下等）を事務所が
    # 確認済みかどうか。**既定は未確認**（過大控除を避ける側に倒す）。
    # 事務所が確認したらここを True にするだけで R08 → R07 へ移る。
    _CFG_TAXPAYER_CONFIRMED = False

INVOICE_COL_VALUES = tuple(_CFG_INVOICE_COL_VALUES)
INVOICE_COL_RENDERING = dict(_CFG_INVOICE_COL_RENDERING)
INVOICE_CONFIRM_THRESHOLD_YEN = _CFG_CONFIRM_THRESHOLD
INVOICE_CONFIRM_TAG = _CFG_CONFIRM_TAG


# ── DTO ───────────────────────────────────────────────────────────
class LegalRef(NamedTuple):
    citation: str
    source_url: str = ""
    citation_verified: bool = False   # False は監査出力に「条番号 要確認」を付す


class DeductionInput(NamedTuple):
    doc_type: Optional[str] = None
    line_kind: Optional[str] = None
    amount_tax_incl: Optional[int] = None    # 税込。負数もそのまま渡す
    txn_date: Optional[date] = None
    is_foreign_currency: bool = False
    merchant_t_number: str = ""              # 【加盟店】のもの。カード会社のは入れない


class TaxPolicy(NamedTuple):
    small_amount_enabled: bool = True
    # 事業者要件は機械判定不能。既定値は config オーバライドから取る
    # （Plan §7 の影響面表が `TAX_POLICY` を config 追加項目に挙げている）。
    small_amount_taxpayer_confirmed: bool = _CFG_TAXPAYER_CONFIRMED
    small_amount_threshold_yen: int = 10000
    # 期間は 2023/10/1〜2029/9/30（F-14）。**開始日も見る** —— 会計事務所は
    # 往年の資料も扱うので、下限が無いと 2023/9 以前の古い明細まで
    # 「少額特例で適格」として集計されてしまう。
    small_amount_starts_on: date = date(2023, 10, 1)
    small_amount_expires_on: date = date(2029, 9, 30)
    public_transport_enabled: bool = True
    public_transport_threshold_yen: int = 30000
    etc_special_enabled: bool = True
    # 出張旅費等特例: 台帳に該当の事実がなく、かつ「会社がカードで直接決済した
    # 取引」は従業員への旅費**支給**とは別の取引形態である（推測）。
    # ルールとして実装せず、枠だけ残す。
    travel_expense_enabled: bool = False


DEFAULT_TAX_POLICY = TaxPolicy()


class DeductionVerdict(NamedTuple):
    deduction_class: str
    rule_id: str
    basis_short: str      # 監査集計の見出し。**摘要（S列）には出さない**
    basis_detail: str     # 監査集計の詳細。**仕訳メモ（T列）には出さない**
    caveats: tuple
    legal_refs: tuple = ()


class Rule(NamedTuple):
    rule_id: str
    priority: int
    predicate: Callable
    outcome: str
    basis_short: str
    basis_detail: str
    legal_refs: tuple
    caveats_fn: Callable
    enabled_key: Optional[str]   # TaxPolicy のどのフラグで切るか（None＝常時有効）


class _Norm(NamedTuple):
    """防御的に正規化した入力。判定木はこちらだけを見る。"""

    doc_type: str
    line_kind: str
    amount: Optional[int]
    txn_date: Optional[date]
    is_foreign_currency: bool
    t_number: str


# ── 判定木（優先順位＝priority 昇順）──────────────────────────────────
#
# 順序は恣意的ではない。「どのルールがより強い根拠か」を
# 外部証憑への依存 / 適用期限の有無 / 納税者要件の有無 で並べている。
#   R01–R03 が最優先: 「控除できるか」以前に**課税仕入れですらない**
#   R05 > R07: 公共交通は期限も事業者要件も無い（2029/10/1 以降も判定が変わらない）
#   R06 > R07: ETC は証明書要件を監査に明記させる必要がある
#   R04 > R05–R07: 加盟店自身の登録番号があれば特例に頼る必要がない

def _no_caveats(norm, policy):
    return ()


def _foreign_caveats(norm, policy):
    return ("要:国内/国外取引の判定",)


def _etc_caveats(norm, policy):
    return ("要:道路会社ごとに利用証明書1件の別途保存",)


def _reduced_rate_caveat(norm):
    if norm.line_kind == LineKind.GENERAL:
        # G列は 10% 固定なので、軽減税率対象（食料品等）だと過大控除になる。
        # 1 行あたり最大 10,000 × (10/110 − 8/108) ≒ 182 円。
        return ("要:税率8%(軽減)の可能性",)
    return ()


def _small_amount_caveats(norm, policy):
    return _reduced_rate_caveat(norm)


def _small_amount_unconfirmed_caveats(norm, policy):
    return ("要:基準期間課税売上高1億円以下等の確認",) + _reduced_rate_caveat(norm)


def _is_small_amount_line(norm, policy):
    return (norm.amount is not None
            and 0 < norm.amount < policy.small_amount_threshold_yen
            and norm.txn_date is not None      # 期間つき特例を日付欠損に当てない
            and policy.small_amount_starts_on <= norm.txn_date
            <= policy.small_amount_expires_on)


RULES = (
    Rule("R01_charge", 10,
         lambda n, p: n.line_kind == LineKind.CHARGE,
         DeductionClass.OUT_OF_SCOPE,
         "チャージ（対価性なし）",
         "SF残高へのチャージは資産の増加であり課税仕入れでない（F-7）",
         (LegalRef("消費税法第2条第1項第12号・第4条第1項"),),
         _no_caveats, None),

    Rule("R02_non_purchase", 20,
         lambda n, p: n.line_kind in (LineKind.POINT, LineKind.SETTLEMENT),
         DeductionClass.OUT_OF_SCOPE,
         "ポイント充当/前月清算",
         "F-5: 検算用の集合と記帳用の集合は別物。合計には効くが費用ではない",
         (LegalRef("消費税法第4条第1項"),),
         _no_caveats, None),

    Rule("R03_foreign", 30,
         lambda n, p: n.is_foreign_currency,
         DeductionClass.OUT_OF_SCOPE,
         "外貨取引 要確認",
         "F-9: 現地通貨表記あり。国外取引＝不課税の可能性。自動断定しない",
         (LegalRef("消費税法第4条第1項"),),
         _foreign_caveats, None),

    Rule("R04_merchant_invoice", 40,
         lambda n, p: bool(n.t_number),
         DeductionClass.QUALIFIED,
         "適格請求書（加盟店登録番号あり）",
         "加盟店自身の登録番号が行に印字されている稀なケース。F-11 よりほぼ発火しない",
         (LegalRef("消費税法第30条第9項第1号",
                   "https://www.nta.go.jp/law/shitsugi/shohi/18/05.htm", True),),
         _no_caveats, None),

    Rule("R05_public_transport", 50,
         # クレカ側では適用しない。加盟店名（「JR」「西鉄バス」等）からの推定は
         # 台帳に根拠がなく、「根拠のない分岐を作らない」に反する。
         lambda n, p: (n.doc_type == DocType.TRANSIT_IC
                       and n.line_kind in PUBLIC_TRANSPORT_KINDS
                       and n.amount is not None
                       and 0 < n.amount < p.public_transport_threshold_yen),
         DeductionClass.QUALIFIED,
         "3万円未満の公共交通機関",
         "公共交通機関特例（3万円未満の鉄道・バス・船舶）。帳簿のみ保存で控除可",
         (LegalRef("消費税法施行令第70条の9第2項第1号"),
          LegalRef("消費税法施行令第49条第1項第1号イ"),
          LegalRef("国税庁 インボイスQ&A",
                   "https://www.nta.go.jp/taxes/shiraberu/zeimokubetsu/shohi/"
                   "keigenzeiritsu/pdf/qa/103.pdf", True)),
         _no_caveats, "public_transport_enabled"),

    Rule("R06_etc", 60,
         lambda n, p: n.line_kind == LineKind.ETC,
         DeductionClass.QUALIFIED,
         "ETC通行料 要利用証明書",
         "ETC緩和措置: カード利用明細書 + 道路会社ごとに任意1取引の利用証明書の併せ保存",
         (LegalRef("国税庁 インボイスQ&A（問番号 TBD）",
                   "https://www.nta.go.jp/taxes/shiraberu/zeimokubetsu/shohi/"
                   "keigenzeiritsu/pdf/qa/103.pdf"),
          LegalRef("ETC利用照会サービス 告知",
                   "https://www.etc-meisai.jp/news/230915.html", True)),
         _etc_caveats, "etc_special_enabled"),

    Rule("R07_small_amount", 70,
         lambda n, p: _is_small_amount_line(n, p) and p.small_amount_taxpayer_confirmed,
         DeductionClass.QUALIFIED,
         "少額特例",
         "少額特例（税込1万円未満・2023/10/1〜2029/9/30・基準期間課税売上高1億円以下等）",
         (LegalRef("平成28年改正法附則第53条の2"),
          LegalRef("国税庁 インボイスQ&A",
                   "https://www.nta.go.jp/taxes/shiraberu/zeimokubetsu/shohi/"
                   "keigenzeiritsu/pdf/qa/103.pdf", True)),
         _small_amount_caveats, "small_amount_enabled"),

    # 金額・期間の要件は満たすが、事業者要件（基準期間課税売上高 1 億円以下等）が
    # 未確認の行。**控除なしに倒す**（実害の非対称性: 過大控除は税務リスクだが、
    # 過小控除は人が確認して直せる）。rule_id を R07 と分けているのは、
    # 「そもそも特例圏外」と「確認さえ取れれば特例圏内」を監査集計で区別するため。
    # 事務所が事業者要件を確認したら `TaxPolicy.small_amount_taxpayer_confirmed`
    # を True にするだけで R07 側へ移る。
    Rule("R08_small_amount_unconfirmed", 75,
         _is_small_amount_line,
         DeductionClass.NOT_DEDUCTIBLE,
         "少額特例（事業者要件 未確認）",
         "金額・期間の要件は満たすが、基準期間課税売上高1億円以下等の事業者要件は"
         "プログラムに判定できない。確認が取れるまで控除なしへ倒す",
         (LegalRef("平成28年改正法附則第53条の2"),),
         _small_amount_unconfirmed_caveats, "small_amount_enabled"),
)

# F-14 原則。判定木の終端（全域関数を保証する）。
DEFAULT_VERDICT = DeductionVerdict(
    DeductionClass.NOT_DEDUCTIBLE, "R99_default", "控除なし",
    "カード会社作成の請求明細書は消費税法第30条第9項の請求書等に該当しない（F-14）",
    (),
    (LegalRef("消費税法第30条第9項",
              "https://www.nta.go.jp/law/shitsugi/shohi/18/05.htm", True),))

# 判定木から実際に出うる class。**手書きしない**（構造から導く）ことで
# 「経過措置は到達不能」がコメントの約束ではなくコードの事実になる。
REACHABLE_CLASSES = frozenset(
    {r.outcome for r in RULES} | {DEFAULT_VERDICT.deduction_class})


# ── 判定 ──────────────────────────────────────────────────────────
def classify(inp, policy=None):
    """**全域関数**。必ず Verdict を返す（None も例外もなし）。

    IP-401 の思想を行レベルに適用したもの: 判定が付かない行が黙って
    H列空欄（＝MF が「適格」と解釈, F-12）へ落ちる経路を作らない。
    """
    p = policy or DEFAULT_TAX_POLICY
    norm = _normalize(inp)
    for rule in _active_rules(p):
        try:
            hit = rule.predicate(norm, p)
        except Exception:  # noqa: BLE001 - 1 つのルールの事故で全域性を失わない
            hit = False
        if hit:
            return DeductionVerdict(rule.outcome, rule.rule_id, rule.basis_short,
                                    rule.basis_detail, rule.caveats_fn(norm, p),
                                    rule.legal_refs)
    return DEFAULT_VERDICT


@functools.lru_cache(maxsize=32)
def _active_rules(policy):
    """有効なルールを priority 昇順で返す。

    `classify` は**明細 1 行ごと**に呼ばれる（ETC 1 ファイルで 300 回）。
    `TaxPolicy` は全フィールドが hashable な NamedTuple で、実運用では
    同じインスタンスが使い回されるので、キャッシュが素直に効く。
    並べ替えはしない —— `RULES` は宣言順が既に priority 昇順であり、
    それは import 時の `_validate_rule_order` が保証する（実行時に毎回
    ソートし直すのは、必ず入力と同じ順序を返すだけの純粋な浪費）。
    """
    return tuple(r for r in RULES
                 if r.enabled_key is None or getattr(policy, r.enabled_key, False))


def _normalize(inp):
    line_kind = getattr(inp, "line_kind", None)
    if line_kind not in LineKind.ALL:
        line_kind = LineKind.GENERAL
    return _Norm(
        doc_type=getattr(inp, "doc_type", None) or "",
        line_kind=line_kind,
        amount=_coerce_amount(getattr(inp, "amount_tax_incl", None)),
        txn_date=_coerce_date(getattr(inp, "txn_date", None)),
        is_foreign_currency=bool(getattr(inp, "is_foreign_currency", False)),
        t_number=_valid_merchant_t_number(getattr(inp, "merchant_t_number", "")),
    )


# 券面と OCR 出力に現れる負号のゆれ。**先頭 1 文字だけ**を見る。
#   − U+2212 … 事実台帳 F-5 の「ENEOSポイントキャッシュバック −3,000」がこれ
#   ▲ △      … 日本の会計慣習の負数表記
# page_dedup / card_reconciliation と挙動を揃えること
# （test_card_reconciliation.LabelTableDriftTest が 3 者の一致を突合する）。
_RE_LEADING_MINUS = re.compile(r"^[-\u2010-\u2015\u2212▲△]")
_AMOUNT_JUNK = (",", "￥", "¥", "円", " ", "　")
_RE_INTEGER = re.compile(r"[+-]?\d+")


def _coerce_amount(value):
    if isinstance(value, bool) or value is None:
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
        # `int(s)` は `1_000` のような Python の数値リテラル記法まで受け入れ、
        # page_dedup / card_reconciliation と解釈が割れる。厳しい側で揃える。
        if _RE_INTEGER.fullmatch(s):
            return int(s)
    return None


def _coerce_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    return None


def _valid_merchant_t_number(value):
    """有効な**加盟店**登録番号なら正規化して返す。そうでなければ空文字。

    カード会社自身の番号は明示的に弾く（F-11）。ここを緩めると、
    券面ヘッダの T番号が全行に付いて全件が「適格」になる。
    """
    s = str(value or "").strip().upper().replace("-", "").replace(" ", "")
    if len(s) != 14 or not s.startswith("T") or not s[1:].isdigit():
        return ""
    if s in CARD_ISSUER_T_NUMBERS:
        return ""
    return s


# ── 出力（帳簿側）────────────────────────────────────────────────
def render_invoice_column(verdict_or_class):
    """H列の値。現行運用では**常に空文字**（趙裁定 9 / AD-8）。

    空欄にするのはあくまで事務所慣例の踏襲であって、税法判断の自動化ではない。
    TBD が動いたら `config.INVOICE_COL_RENDERING` だけを書き換える。
    """
    cls = getattr(verdict_or_class, "deduction_class", verdict_or_class)
    return INVOICE_COL_RENDERING.get(cls, "")


def determine_debit_tax_type(verdict_or_class):
    """G列（借方税区分）。H列とは独立の軸（D5 §1）。

    カード明細に載った ETC 通行料は、適格請求書が無くても**課税仕入れである
    こと自体は変わらない**。変わるのは請求書等の保存要件であり、それが H列。

    語彙は receipt_aggregation から取る（新しい文字列リテラルを書き起こさない）。
    anomaly_detector.EXEMPT_TAX_TYPE が精確等値で判定しているため、
    表記が 1 文字ズレると既存の規則が静かに壊れる。
    軽減税率 8% は判定しない —— カード明細に税率の印字があるという事実は台帳にない。
    """
    cls = getattr(verdict_or_class, "deduction_class", verdict_or_class)
    rate = 0 if cls == DeductionClass.OUT_OF_SCOPE else 0.10
    debit_tax_type, _credit = determine_tax_types("credit_card", rate)
    return debit_tax_type


def needs_invoice_confirmation(amount_tax_incl, threshold=None):
    """U列タグを付けるか。**`>=` で判定する**（`>` ではない）。

    少額特例の要件が「税込 1 万円**未満**」なので、1 万円ちょうどは
    特例の対象外＝適格請求書の有無を人が確認すべき行になる。
    """
    amount = _coerce_amount(amount_tax_incl)
    if amount is None:
        return False
    limit = INVOICE_CONFIRM_THRESHOLD_YEN if threshold is None else threshold
    return amount >= limit


def invoice_confirm_tag():
    """タグ文言。「少額特例により適格」とは**書かない**。

    少額特例には事業者要件（基準期間の課税売上高 1 億円以下等）があり、
    プログラムには判定できない。タグは注意喚起であって判定の保証ではない。
    """
    return INVOICE_CONFIRM_TAG


# ── 出力（監査側）────────────────────────────────────────────────
def build_audit_note(verdict):
    """監査タブ用の根拠文字列。**帳簿（摘要・仕訳メモ）には載せない。**"""
    if not isinstance(verdict, DeductionVerdict):
        return ""
    parts = ["%s: %s" % (verdict.rule_id, verdict.basis_detail)]
    parts.extend(verdict.caveats)
    unverified = [ref.citation for ref in verdict.legal_refs
                  if not ref.citation_verified]
    if unverified:
        parts.append("（条番号 要確認: %s）" % "・".join(unverified))
    return "／".join(parts)


def summarize(rows):
    """判定コード単位に畳む（行単位の監査行は 287 行になって読めない）。

    rows: [(DeductionVerdict, amount_tax_incl), ...]
    """
    result = {}
    for row in rows or ():
        try:
            verdict, amount = row[0], row[1]
        except (TypeError, IndexError, KeyError):
            continue
        if not isinstance(verdict, DeductionVerdict):
            continue
        bucket = result.setdefault(verdict.rule_id, {
            "count": 0, "sum_amount": 0, "deduction_class": verdict.deduction_class,
            "basis_short": verdict.basis_short, "caveats": set(),
        })
        bucket["count"] += 1
        bucket["sum_amount"] += _coerce_amount(amount) or 0
        bucket["caveats"].update(verdict.caveats)
    return result


# ── 記帳軸 ↔ 実体軸のブリッジ ─────────────────────────────────────
def line_kind_from_booking_kind(booking_kind, declared_line_kind):
    """記帳軸の kind（AD-5）から実体軸の LineKind を決める。

    非購入の 3 種は記帳軸が一意に決める（Gemini の 2 軸申告が食い違っても、
    チャージ行が「少額特例で適格」になる経路を作らない）。
    それ以外は申告された LineKind を尊重し、不正値なら GENERAL へ倒す。
    """
    mapped = BOOKING_KIND_TO_LINE_KIND.get(booking_kind)
    if mapped:
        return mapped
    if declared_line_kind in LineKind.ALL:
        return declared_line_kind
    return LineKind.GENERAL


def derive_line_kind(doc_type, booking_kind, text="", declared_line_kind=None):
    """券面の逐語から実体軸の LineKind を決める（builder が行ごとに呼ぶ）。

    Args:
        doc_type: DocType.CREDIT_CARD / DocType.TRANSIT_IC
        booking_kind: 記帳軸の kind（AD-5。card_reconciliation の KIND_*）
        text: 券面の逐語をつないだもの（利用店名 ＋ nimoca の種別列など）
        declared_line_kind: Gemini が申告した LineKind（あれば。無くてよい）

    優先序:
      1. 非購入の 3 種は記帳軸が一意に決める（最優先。ここが崩れると
         チャージ行が少額特例で「適格」になる）
      2. 券面の逐語（F-7 の種別列 `電車`/`バス`、F-8 の `ＥＴＣ後納分`/
         `ＥＴＣカード売上`、給油）
      3. Gemini の申告（形式が正しいときのみ）
      4. GENERAL

    **TRAIN / BUS はcredit_card 側で導出しない**。クレカ明細に種別の印字が
    あるという事実は台帳になく、加盟店名（「JR」「西鉄バス」等）からの推定は
    根拠がない（D5 §2.1）。R05 側でも doc_type を見ているので二重の防御になる。

    これを Python 側に置くのは AD-4 と同じ理由 —— Gemini に分類を自己申告
    させると、逐語と分類が食い違ったときに裁く手段がなくなる。加えて
    この関数が無いと、nimoca の電車行も ETC 通行料も一律 GENERAL に落ちて
    R05/R06 が永久に発火せず、しかも「要:税率8%(軽減)の可能性」という
    通行料・給油には無意味な caveat が付く。
    """
    mapped = BOOKING_KIND_TO_LINE_KIND.get(booking_kind)
    if mapped:
        return mapped

    t = unicodedata.normalize("NFKC", str(text or "")).lower()
    if _RE_ETC.search(t):
        return LineKind.ETC
    if _RE_FUEL.search(t):
        return LineKind.FUEL
    if doc_type == DocType.TRANSIT_IC:
        if _RE_TRAIN.search(t):
            return LineKind.TRAIN
        if _RE_BUS.search(t):
            return LineKind.BUS

    if declared_line_kind in LineKind.ALL:
        return declared_line_kind
    return LineKind.GENERAL


# ── import 時の自己検証（起動時に大声で落ちる方針）──────────────────
def _validate_rule_order(rules=None):
    """`RULES` が priority 昇順で宣言されていることを保証する。

    `_active_rules` が実行時のソートを省けるのはこの不変式のおかげ。
    ルールを足すときに順序を崩したら import 時に落ちる（判定木の優先順位が
    静かに入れ替わると、ETC 行が少額特例で拾われて証明書要件の注記が
    落ちる、といった形で誤りが表に出ない）。
    """
    target = RULES if rules is None else rules
    priorities = [r.priority for r in target]
    if priorities != sorted(priorities):
        raise RuntimeError(
            "RULES は priority 昇順で宣言すること（実行時ソートを省いている）: %s"
            % priorities)


def _validate_rendering(rendering=None, allowed=None):
    """H列の写像が MF の正規値から外れていないことを確認する。

    ここが緩むと、判定は正しいのに MF が値を解釈できず（あるいは空欄扱いで
    「適格」と解釈して）過大控除になる。ocr_engine._validate_doc_type_registries と
    同じ「起動時に落とす」方針。
    """
    mapping = INVOICE_COL_RENDERING if rendering is None else rendering
    values = INVOICE_COL_VALUES if allowed is None else allowed

    missing = [c for c in sorted(REACHABLE_CLASSES) if c not in mapping]
    bad = [v for v in mapping.values() if v not in values]
    if missing or bad:
        raise RuntimeError(
            "INVOICE_COL_RENDERING 不整合 — 未定義:%s / MF正規値外:%s"
            "（空欄は MF が「適格」と解釈するため、写像の穴は過大控除に化ける。F-12）"
            % (missing, bad))


_validate_rule_order()
_validate_rendering()
