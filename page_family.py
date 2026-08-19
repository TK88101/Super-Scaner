# page_family.py
# クレカ／交通系IC 頁の族分類器 ＋ 頁の去向を決める唯一の裁決関数（Plan AD-0）。
#
# 本モジュールの中心的な契約は 1 つだけ:
#   **entries > 0 の頁に到達できる除外経路が存在しない。**
# これはコメントによる約束ではなく `resolve_page_disposition` の制御フローの形で
# 保証する。IP-401（2026-07-30 の本番事故）で `_is_envelope_page` が「前置拒否権」
# から「事後説明器」へ降格された経緯（CLAUDE.md）をそのまま踏襲したもので、
# 族シグナルは頁を落とせない。実在の反例は UC p2 ——「永久不滅ポイント」区画と
# 明細 12 行が同居する頁（事実台帳 F-6 / F-10）。ポイント語で否決する設計だと
# 12 件が丸ごと消える。
#
# 例外は 2 つだけで、どちらも「構造上ありえない事象」に限る:
#   1. 重複頁（AD-1。二重署名 AND 規則で page_dedup が確定させたものだけ）
#   2. 社会保険料通知書（既存。本モジュールの管轄外）
#
# 純関数のみ・副作用なし・例外を投げない。gspread / paddleocr / google api に
# 非依存なので venv 無しで単体テストできる。

import re
import unicodedata
from dataclasses import dataclass

from page_dedup import VERDICT_DUPLICATE

# ── 族 ────────────────────────────────────────────────────────────
FAMILY_CC_DETAIL = "cc_detail"        # クレカ明細行を含む頁 → 記帳
FAMILY_CC_SUMMARY = "cc_summary"      # 請求合計・合計表のみ → MF タブへ提示行（裁定 8）
FAMILY_IC_HISTORY = "ic_history"      # 交通系IC 利用履歴 → 記帳
FAMILY_POINTS_ONLY = "points_only"    # ポイント専用頁 → 監査タブ
FAMILY_INFO_NOTICE = "info_notice"    # 案内・インフォメーション頁 → 監査タブ
FAMILY_PAYMENT_METHOD_NOTICE = "payment_method_notice"  # リボ・分割の案内頁 → 監査タブ
FAMILY_UNKNOWN = "unknown"            # いずれでもない → 赤い認識不能占位行

STRENGTH_STRONG = "strong"
STRENGTH_WEAK = "weak"
STRENGTH_NONE = "none"

# ── 頁の処分（AD-0 の 3 値。これ以外の去向を作らない）──────────────
ACTION_BOOK = "book"                  # 記帳する
ACTION_EXCLUDE = "exclude"            # 記帳しない（行き先は destination が宣言）
ACTION_UNRECOGNIZED = "unrecognized"  # 赤い認識不能占位行（うるさく可視化して止める）

# ocr_engine.py:776-777 と**同一文字列**でなければならない。
# venv 非依存を保つため import せず自前で持つ。乖離すると
# main._record_excluded_page が既定の監査タブへ落ち、裁定 8 の
# 「合計表は MF タブへ提示行」が静かに失われる（test_page_family が突合する）。
EXCLUDE_DEST_AUDIT_TAB = "audit_tab"
EXCLUDE_DEST_MF_TAB = "mf_tab"

# 重複頁をどう扱うか（Plan §9.5 の CREDIT_CARD_DEDUP_MODE）。
#   exclude … 記帳しない。監査タブへ（既定）
#   mark    … 照常記帳しつつ監査に注記を残す（偽陽性が疑われるときの避難口）
#   off     … 重複判定を一切効かせない
# 偽陽性は仕訳を消すので、実運用で誤検出が出たときに**コード変更なしで**
# 段階的に弱められる逃げ道を用意しておく。
DEDUP_MODE_EXCLUDE = "exclude"
DEDUP_MODE_MARK = "mark"
DEDUP_MODE_OFF = "off"

try:
    from config import CREDIT_CARD_DEDUP_MODE as _CFG_DEDUP_MODE
except ImportError:
    _CFG_DEDUP_MODE = DEDUP_MODE_EXCLUDE

# prompt 上書きの対象。**新規 2 型のみ**。本番稼働中の既存経路は触らない。
# doc_types.DocType と同値。この 2 値のためだけに依存を増やさない
# （test_page_family が DocType との一致を突合する）。
DOC_TYPE_CREDIT_CARD = "credit_card"
DOC_TYPE_TRANSIT_IC = "transit_ic"
CC_FAMILY_DOC_TYPES = frozenset({DOC_TYPE_CREDIT_CARD, DOC_TYPE_TRANSIT_IC})


# ── 正規化 ────────────────────────────────────────────────────────
#
# ocr_engine.py:645-654 の `_SIMPLIFIED_TO_JP` と同一内容。共有せず複製するのは
# ocr_engine が paddleocr を引き連れており、venv 無しでの単体テストを壊すため。
# 乖離検出は test_page_family.test_simplified_char_map_matches が担う。
_SIMPLIFIED_TO_JP = {
    "领": "領",
    "证": "証",
    "收": "収",
    "请": "請",
    "买": "買",
    "计": "計",
    "纳": "納",
    "录": "録",
}
_SIMPLIFIED_TRANS = str.maketrans(_SIMPLIFIED_TO_JP)

# 空白は「数字と数字の境界」だけ残し、それ以外は削る。
#   - 削る側: PaddleOCR は券面レイアウトどおりに改行を入れるので
#     「セ ンター\nポイント」で語が分断され、キーワードを取り逃す。
#   - 残す側: 全空白を消すと「薬院 260 5月2日」が「薬院2605月2日」になり、
#     金額 260 も日付 5月2日 も取れなくなる（nimoca の 3 桁金額で実害）。
_RE_SPACE_NEXT_TO_NON_DIGIT = re.compile(r"(?<=\D)\s+|\s+(?=\D)|^\s+|\s+$")
_RE_MULTI_SPACE = re.compile(r"\s+")


def _collapse_spaces(text):
    """数字境界の空白だけ 1 個残し、他は削る（NFKC 済みの文字列に適用する）。"""
    return _RE_MULTI_SPACE.sub(" ", _RE_SPACE_NEXT_TO_NON_DIGIT.sub("", text))


# ── シグナル語彙（★＝事実台帳で実文字列を確認済み）──────────────────
# 交通系IC。ブランド語は**単独では絶対に発火させない**（D1 §3.4）——
# クレカ明細の加盟店名欄に「nimoca オートチャージ」が現れうるため。
_IC_BRAND_TOKENS = ("nimoca", "ニモカ")
_IC_STRUCT_TOKENS = (
    "月日", "種別", "施設", "利用額", "カードポイント", "センターポイント", "sf残高",
    "電車", "バス", "入金",
)
_RE_IC_COUNT = re.compile(r"全\d+件")

# 合計・請求（F-3 実測）。長い語を先に置く（前方一致の食い合いを避ける）。
_TOTAL_LABEL_TOKENS = (
    "今回ご利用・ご請求金額合計", "今期のお支払い金額総合計", "今月ご利用額合計",
    "ご利用代金合計表", "お支払い金額合計", "ご請求金額合計", "今回のお支払金額",
    "今回ご請求内容", "今回ご請求金額", "お支払い小計", "お支払合計", "ご請求金額",
)
_RE_COMMA_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+")
_TOTAL_LOOKAHEAD_CHARS = 60

# ポイント（F-6 実測）
_POINTS_TITLE_TOKENS = ("ポイント・インフォメーション", "ポイントインフォメーション")
_POINTS_TOKENS = (
    "永久不滅ポイント", "ワールドプレゼント", "利用獲得ポイント",
    "このカードのポイント明細", "ポイント明細", "獲得ポイント", "失効予定",
    "marriottbonvoy", "marriott bonvoy",
    "前月残高", "前回残高", "当月獲得", "当月末", "ポイント残高",
)

# 支払方式の案内（T8 / 要求 #6。2026-08-19 にアメックス p8 の実 OCR で
# ヒットを確認した語だけを置く）。
#
# **強語彙**は単独で発火してよい。発行体固有の商品名か、他の文脈で出ない
# 長い複合語に限る。**「リボ」単独は絶対に入れない** —— 加盟店名や広告文
# （「ご利用はリボでお得」）に出うるため。社会保険料判定で「納入告知額」を
# 単独発火させない裁決（CLAUDE.md）と同じ原理で、誤爆すると無関係な頁が
# 監査タブへ沈む。
_PAYMENT_METHOD_STRONG_TOKENS = (
    "ペイフレックス", "あとリボ", "リボルビング払い", "リボルビング残高",
)
# **弱語彙**は 2 語以上の AND でのみ発火する。単独では明細頁の用語説明にも出る。
_PAYMENT_METHOD_WEAK_TOKENS = (
    "基本手数料率", "実質年率", "変更締切日", "分割払手数料", "ボーナス払い",
    "支払回数",
)

# 案内（F-6 実測 ＋ 拡張候補）
_INFO_TOKENS = (
    "インフォメーション", "web明細", "ご案内", "キャンペーン", "お知らせ",
    "ご利用規約", "変更のお知らせ",
)

# 発行体（facet ＋ cc_strength の材料）。
# 「uc」「ts3」のような 2〜3 字ラテンを単独キーワードにしない（OCR ノイズと衝突する）。
#
# **交通系IC の事業者はここに入れない。** nimoca は `_IC_BRAND_TOKENS` が
# 独立に検出しており、この表に入れると nimoca 頁が cc_strength=strong になって
# IC シグナルと拮抗し `family_ambiguous` へ落ちる ——「混載フォルダの自動分流」
# （趙裁定 5）が丸ごと効かなくなる。表に入れたうえで除外集合で打ち消す形は
# 特例の上塗りなので採らない。
_ISSUER_TOKENS = (
    ("amex", ("アメリカン・エキスプレス", "アメリカンエキスプレス", "americanexpress",
              "american express", "t8700150009366")),
    ("toyota_finance", ("t8010601027383", "ts3コーポレート", "tscubic", "ts cubic",
                        "eneos business", "eneosbusiness")),
    ("eneos", ("eneos",)),
    ("rakuten", ("楽天カード",)),
    ("jcb", ("jcb",)),
    ("uc", ("永久不滅ポイント", "ucカード")),
    ("sumitomo", ("ワールドプレゼント",)),
)

ISSUER_TRANSIT_IC = "nimoca"

# 明細行らしさ（安全弁専用。除外を拒否する方向にしか使わない）
#
# 金額はカンマ有りに限定しない。nimoca の単価は 150〜1,300 円（F-7）で
# 大半にカンマが付かず、限定すると当該頁で拒否権が消える。
# T8-1: `.` 区切りは**年付きのときだけ**日付とみなす。
# 年なしの `.` を日付として拾うと `基本手数料率 14.90` `実質年率 14.6` が
# 日付 2 件に化け、リボ頁が閾値 5 に到達して has_detail_rows が真になる。
# するとAD-0 優先序 3（行らしさは除外を拒否する）が発動し、明細を持たない
# リボ頁が監査タブではなく赤い認識不能行へ落ちる（2026-08-19 実測）。
# `1.19`（年なし月日）と `14.90`（小数）はトークン単体では区別できないので
# 片方を捨てるしかない。参考資料 8 頁に `.` 区切り日付は一度も出現しないため
# 年なし `.` を捨てる。将来出たら実物を添えて緩めること。
_RE_DATE_TOKEN = re.compile(
    r"20\d{2}[/年.\-]\d{1,2}[/月.\-]\d{1,2}日?"   # 年付き（`.` 可）
    r"|\d{1,2}[/月\-]\d{1,2}日?"                    # 年なし（`.` は不可）
)
_RE_AMOUNT_TOKEN = re.compile(
    r"(?<![\d,.\-/])(?:\d{1,3}(?:,\d{3})+|\d{3,7})(?![\d,/\-])")
MIN_DETAIL_DATE_TOKENS = 5
MIN_DETAIL_AMOUNT_TOKENS = 5

# カード番号（F-1 実測の 3 系統。マスク位置が末尾のものが在るので下 4 桁を鍵にしない）
# 先頭の否定後読みが要（F-11）: 券面ヘッダの適格請求書登録番号
# `T8700150009366` / `T8010601027383` は T ＋ 13 桁で、これを外すと
# 13 桁部分がカード番号として拾われる。**それはカード会社自身の番号**なので、
# 下流が member_no に使うと同一発行体の全カードが 1 枚に合流する。
# 数字列の途中から始まる誤マッチも同時に潰れる。
_RE_CARD_KEY = re.compile(
    r"(?<![0-9a-zA-Z])"
    r"[0-9xX×＊*]{4}[\s\-‐]?[0-9xX×＊*]{4,6}[\s\-‐]?[0-9xX×＊*]{3,5}"
    r"(?:[\s\-‐]?[0-9xX×＊*]{3,5})?"
    r"(?![0-9])")
_RE_SHEET_NO = re.compile(r"(\d{1,2})/(\d{1,2})\s*(?:ページ|頁|page|p\.)")
MAX_CARD_KEYS = 4
MAX_DECLARED_TOTALS = 6


@dataclass(frozen=True)
class PageClass:
    """1 頁の分類結果。**採否ではなく事実だけ**を運ぶ。

    呼び出し側に `continue` を書ける形の bool を渡さないのが要点
    （IP-401: 前置拒否権を構造的に持たせない）。
    """

    # routing_family は ic_strength / cc_strength から導かれる「この頁は大体
    # どちらか」の 1 語。prompt の選択は ic_strength / cc_strength を直接見る
    # （strong 同士の拮抗を表現する必要があるため）ので、routing_family の
    # 唯一の読み手は resolve_family。
    routing_family: str = FAMILY_UNKNOWN
    ic_strength: str = STRENGTH_NONE
    cc_strength: str = STRENGTH_NONE
    signals: frozenset = frozenset()
    issuer: str = ""
    card_keys: tuple = ()
    sheet_no: tuple = ()
    declared_totals: tuple = ()
    points_score: int = 0
    info_score: int = 0
    payment_method_score: int = 0
    has_detail_rows: bool = False          # 「除外を拒否する」ためだけの値
    error: str = ""


@dataclass(frozen=True)
class Disposition:
    """頁の去向。**この型を作れるのは resolve_page_disposition だけ**（AD-0）。"""

    action: str
    family: str = FAMILY_UNKNOWN
    reason: str = ""
    destination: str = ""
    audit_signal: object = None            # 記帳しつつ監査に残す注記（None = 無し）


# ── 分類（Gemini 呼出前。頁は落とせない）────────────────────────────
def classify_page(ocr_text):
    """OCR テキストから頁の族シグナルを抽出する。**絶対に例外を投げない。**"""
    try:
        return _classify(ocr_text)
    except Exception as e:  # noqa: BLE001 - 分類器ごときで頁を落とさない
        return PageClass(error="%s: %s" % (type(e).__name__, str(e)[:80]))


def _classify(ocr_text):
    # NFKC 正規化は 1 回だけ。カード番号・頁番号の抽出はマスク記号と桁の並びを
    # 保つ必要があるので「空白を畳む前」のこの文字列を使い、キーワード照合は
    # さらに空白を処理した `t` を使う。
    nfkc = unicodedata.normalize("NFKC", str(ocr_text or ""))
    t = _collapse_spaces(nfkc.translate(_SIMPLIFIED_TRANS).lower())
    if not t:
        # Vision 兜底頁（OCR テキスト無し）。シグナル無しとして扱う。
        return PageClass()

    signals = set()

    ic_brand = any(tok in t for tok in _IC_BRAND_TOKENS)
    ic_struct = sum(1 for tok in _IC_STRUCT_TOKENS if tok in t)
    if _RE_IC_COUNT.search(t):
        ic_struct += 1
    if ic_brand and ic_struct >= 2:
        ic_strength = STRENGTH_STRONG
    elif ic_struct >= 4:
        # ブランド語を OCR が落とした場合の救済。フォルダ指定は覆さない。
        ic_strength = STRENGTH_WEAK
    else:
        ic_strength = STRENGTH_NONE
    if ic_strength != STRENGTH_NONE:
        signals.add("ic:%s" % ic_strength)

    issuer = _detect_issuer(t)
    totals = _extract_declared_totals(t)
    if issuer:
        cc_strength = STRENGTH_STRONG
    elif totals:
        cc_strength = STRENGTH_WEAK
    else:
        cc_strength = STRENGTH_NONE
    # facet としての発行体は交通系IC も報告する（card_reconciliation のキーに使う）。
    # ただし cc_strength には効かせない ——「クレカ発行体か」と
    # 「どの事業者の券か」は別の問いである。
    if not issuer and ic_brand:
        issuer = ISSUER_TRANSIT_IC
    if issuer:
        signals.add("issuer:%s" % issuer)
    if totals:
        signals.add("declared_total")

    points_title = any(tok in t for tok in _POINTS_TITLE_TOKENS)
    points_score = 2 if points_title else sum(1 for tok in _POINTS_TOKENS if tok in t)
    if points_score:
        signals.add("points:%d" % points_score)
    info_score = sum(1 for tok in _INFO_TOKENS if tok in t)
    if info_score:
        signals.add("info:%d" % info_score)
    # 強語彙は単独で 2 点（＝発火）。弱語彙は 1 点ずつで 2 語 AND を要求する。
    #
    # revolving / installment の下位タグは**置かない**。Codex LOW-1 は
    # 「将来の監査分析で区別したくなる」として分離を勧め一度は入れたが、
    # simplify 評審が (1) 誰も読んでいない (2) 現行の強語彙 4 つは全て
    # 「リボ」か「ペイフレックス」を含むので installment 側の枝が到達不能、
    # の 2 点を実証した。去向は 1 つで足り、必要になってから足す。
    pm_strong = any(tok in t for tok in _PAYMENT_METHOD_STRONG_TOKENS)
    payment_method_score = 2 if pm_strong else sum(
        1 for tok in _PAYMENT_METHOD_WEAK_TOKENS if tok in t)
    if payment_method_score:
        signals.add("payment_method:%d" % payment_method_score)

    has_detail_rows = _looks_like_detail_rows(t)
    if has_detail_rows:
        signals.add("detail_rows")

    # routing_family は 2 つの strength から導く粗い値。記帳の可否は決めない。
    if ic_strength != STRENGTH_NONE:
        routing_family = FAMILY_IC_HISTORY
    elif cc_strength != STRENGTH_NONE:
        routing_family = FAMILY_CC_DETAIL
    else:
        routing_family = FAMILY_UNKNOWN

    return PageClass(
        routing_family=routing_family,
        ic_strength=ic_strength,
        cc_strength=cc_strength,
        signals=frozenset(signals),
        issuer=issuer,
        card_keys=_extract_card_keys(nfkc),
        sheet_no=_extract_sheet_no(nfkc),
        declared_totals=totals,
        points_score=points_score,
        info_score=info_score,
        payment_method_score=payment_method_score,
        has_detail_rows=has_detail_rows,
    )


def _detect_issuer(t):
    for name, tokens in _ISSUER_TOKENS:
        if any(tok in t for tok in tokens):
            return name
    return ""


def _extract_declared_totals(t):
    """合計ラベルの直後にある金額を全部拾う（選別しない）。

    アメックスは区画ごとに小計を持つ（F-5）ので 1 頁に複数あってよい。
    どれを検算に使うかは card_reconciliation の責務であり、ここでは決めない。
    """
    found = []
    for label in _TOTAL_LABEL_TOKENS:
        start = 0
        while True:
            i = t.find(label, start)
            if i < 0:
                break
            window = t[i + len(label):i + len(label) + _TOTAL_LOOKAHEAD_CHARS]
            m = _RE_COMMA_AMOUNT.search(window)
            if m:
                value = int(m.group(0).replace(",", ""))
                if value not in found:
                    found.append(value)
            start = i + len(label)
            if len(found) >= MAX_DECLARED_TOTALS:
                return tuple(found)
    return tuple(found)


def _looks_like_detail_rows(t):
    """行らしさ。閾値に強い根拠は無く best-effort（`_is_envelope_page` と同様）。

    だからこそ**除外しない方向にしか使わない**。閾値の誤りが
    「静かな監査タブ行」ではなく「うるさい赤占位行」の側に倒れる。
    """
    dates = len(_RE_DATE_TOKEN.findall(t))
    if dates < MIN_DETAIL_DATE_TOKENS:
        return False
    return len(_RE_AMOUNT_TOKEN.findall(t)) >= MIN_DETAIL_AMOUNT_TOKENS


def _extract_card_keys(nfkc_text):
    """カード番号候補（facet）。マスク記号と桁の並びを保つため、空白を畳む前の
    NFKC 済みテキストから拾う。

    1 頁に複数の番号が出る文書があるので tuple で返し、単一選択しない。
    """
    if not nfkc_text:
        return ()
    keys = []
    for m in _RE_CARD_KEY.finditer(nfkc_text):
        s = m.group(0).strip()
        if sum(c.isdigit() for c in s) < 4:
            continue
        if s not in keys:
            keys.append(s)
        if len(keys) >= MAX_CARD_KEYS:
            break
    return tuple(keys)


def _extract_sheet_no(nfkc_text):
    """`n/N`（facet）。**アンカー語に隣接する候補のみ**採用する。

    素の `(\\d{1,2})/(\\d{1,2})` は日付 `4/10` と衝突する。候補が 0 個または
    2 個以上なら空タプルを返す（曖昧なら持たない）。
    """
    if not nfkc_text:
        return ()
    hits = _RE_SHEET_NO.findall(nfkc_text.lower())
    if len(hits) != 1:
        return ()
    return (int(hits[0][0]), int(hits[0][1]))


# ── ① Gemini 呼出前: prompt の選択 ────────────────────────────────
def select_prompt_doc_type(folder_doc_type, page_class):
    """(prompt に使う doc_type, 監査シグナル or None) を返す。

    上書きは**新規 2 型の相互取り違えに限る**。receipt 等の既存本番経路は
    シグナルを記録するだけで prompt を変えない（本番を触らない）。
    上書きしても doc_type そのものは差し替えない（タブ・取引No 採番・
    分割 PDF の保存先が 1 ファイル内で割れるのを避ける。D1 §5.3）。
    """
    pc = page_class if isinstance(page_class, PageClass) else PageClass()
    ic_strong = pc.ic_strength == STRENGTH_STRONG
    cc_strong = pc.cc_strength == STRENGTH_STRONG

    if folder_doc_type not in CC_FAMILY_DOC_TYPES:
        return folder_doc_type, ("family_mismatch:ic_history" if ic_strong else None)

    if ic_strong and cc_strong:
        # 曖昧なときはフォルダ（＝人間の明示的行為）を信じるのが最小驚き。
        return folder_doc_type, "family_ambiguous"
    if ic_strong and folder_doc_type == DOC_TYPE_CREDIT_CARD:
        return DOC_TYPE_TRANSIT_IC, "family_override:ic_history@credit_card"
    if cc_strong and folder_doc_type == DOC_TYPE_TRANSIT_IC:
        return DOC_TYPE_CREDIT_CARD, "family_override:credit_card@transit_ic"
    return folder_doc_type, None


# ── ② Gemini 呼出後: 族ラベルと処分 ──────────────────────────────
def _signal_family(pc):
    """シグナルだけから見た非記帳族（該当なしは None）。

    `has_detail_rows` を見ない生の族。entries があっても監査に残す注記の
    材料として使うため、拒否権とは分離してある。
    """
    # 支払方式族を declared_totals より**前**に置く。リボ頁が
    # 「今回ご請求金額 1,234」のようにカンマ付き合計を印字していると
    # cc_summary（→ MF タブ）に取られ、趙裁定「#5/#6 は監査タブ」が
    # 静かに破れるため。商品名は極めて特異なシグナルで、汎用の合計
    # ラベルに譲る理由が無い。
    if pc.payment_method_score >= 2:
        return FAMILY_PAYMENT_METHOD_NOTICE
    if pc.declared_totals:
        return FAMILY_CC_SUMMARY
    if pc.points_score >= 2:
        return FAMILY_POINTS_ONLY
    if pc.info_score >= 1:
        return FAMILY_INFO_NOTICE
    return None


def resolve_family(page_class, entry_count):
    """報告用の族ラベル。AD-0 の優先序そのもの（duplicate は呼出側で先に処理）。"""
    pc = page_class if isinstance(page_class, PageClass) else PageClass()
    if pc.routing_family == FAMILY_IC_HISTORY:
        return FAMILY_IC_HISTORY
    if _positive(entry_count):
        return FAMILY_CC_DETAIL
    if pc.has_detail_rows:
        # 行があるのに 0 件＝解析失敗。除外させない（ETC 100 行頁の無音欠落防止）。
        return FAMILY_UNKNOWN
    return _signal_family(pc) or FAMILY_UNKNOWN


def resolve_page_disposition(dedup_verdict, entry_count, page_class, dedup_mode=None):
    """**頁の去向を決める唯一の関数**（AD-0）。

    優先序（上から評価、最初に当たったものが確定）:
      1. duplicate       → 記帳しない。監査タブのみ（AD-1）
      2. entry_count > 0 → 必ず記帳（族シグナルは audit_signal に落とすだけ）
      3. has_detail_rows → 除外させない。赤い認識不能占位行
      4. 非記帳族        → cc_summary は MF タブ（裁定 8）／他は監査タブ
      5. それ以外        → 赤い認識不能占位行

    どのモジュールもこの関数を経由せずに頁の去向を決めてはならない。
    多頭裁決を許すと、矛盾したときの勝者が実装の書き順で決まってしまう。
    """
    pc = page_class if isinstance(page_class, PageClass) else PageClass()

    # 1. 重複頁。**entries の有無より先**に効く唯一の族外ルール。
    #    page_dedup の二重署名 AND 規則（識別キー一致 かつ 内容ダイジェスト一致）を
    #    通ったものだけがここへ来る。片側署名だけの一致は duplicate にならない。
    mode = _CFG_DEDUP_MODE if dedup_mode is None else dedup_mode
    is_duplicate = (mode != DEDUP_MODE_OFF
                    and getattr(dedup_verdict, "kind", None) == VERDICT_DUPLICATE)
    dup_reason = ""
    if is_duplicate:
        origin = getattr(dedup_verdict, "origin_page", None)
        detail = getattr(dedup_verdict, "detail", "") or ""
        dup_reason = "duplicate_page:orig=p%s" % (origin if origin is not None else "?")
        if detail:
            dup_reason = "%s;%s" % (dup_reason, detail)
        if mode != DEDUP_MODE_MARK:
            return Disposition(ACTION_EXCLUDE, family=resolve_family(pc, entry_count),
                               reason=dup_reason,
                               destination=EXCLUDE_DEST_AUDIT_TAB)

    # 2. entries があれば必ず記帳する。ここに族の拒否権は存在しない。
    if _positive(entry_count):
        signal_family = _signal_family(pc)
        # cc_summary は明細末尾に合計が印字されているだけの正常な形（F-3）なので
        # 注記しない。毎頁鳴ると監査タブが狼少年になる。
        audit = None
        if signal_family in (FAMILY_POINTS_ONLY, FAMILY_INFO_NOTICE,
                             FAMILY_PAYMENT_METHOD_NOTICE):
            audit = "family_signal_with_entries:%s" % signal_family
        if dup_reason:                      # mode == mark: 記帳しつつ注記を残す
            audit = "%s;%s" % (dup_reason, audit) if audit else dup_reason
        return Disposition(ACTION_BOOK, family=resolve_family(pc, entry_count),
                           audit_signal=audit)

    # mark モードで entries が無かった頁でも、重複だった事実は落とさない。
    # 落とすと「mark にしたのに重複の痕跡が監査に残らない」状態になり、
    # 偽陽性の調査手段が消える（mode を弱める目的そのものが達成できない）。
    def _with_dup(reason):
        return "%s;%s" % (dup_reason, reason) if dup_reason else reason

    # 3. 行らしさが検出されたら、いかなる族も除外を主張できない。
    if pc.has_detail_rows:
        return Disposition(ACTION_UNRECOGNIZED, family=FAMILY_UNKNOWN,
                           reason=_with_dup("detail_rows_without_entries"))

    # 4. 非記帳族。行き先は producer が宣言する（main は理由から推測しない）。
    family = _signal_family(pc)
    if family == FAMILY_CC_SUMMARY:
        totals = ",".join(str(v) for v in pc.declared_totals[:3])
        return Disposition(ACTION_EXCLUDE, family=family,
                           reason=_with_dup("cc_summary:total=%s" % totals),
                           destination=EXCLUDE_DEST_MF_TAB)
    if family in (FAMILY_POINTS_ONLY, FAMILY_INFO_NOTICE,
                  FAMILY_PAYMENT_METHOD_NOTICE):
        return Disposition(ACTION_EXCLUDE, family=family, reason=_with_dup(family),
                           destination=EXCLUDE_DEST_AUDIT_TAB)

    # 5. 何も判らない頁は赤い認識不能占位行へ（監査タブに沈めない）。
    return Disposition(ACTION_UNRECOGNIZED, family=FAMILY_UNKNOWN,
                       reason=_with_dup(pc.error or "unclassified"))


def exclusion_fields(disposition):
    """`_blank_result` へ渡す除外フィールド（producer が行き先を宣言する形）。

    ACTION_EXCLUDE 以外では空 dict を返す。
    """
    if getattr(disposition, "action", None) != ACTION_EXCLUDE:
        return {}
    return {
        "_excluded_page": True,
        "_exclude_reason": disposition.reason,
        "_exclude_destination": disposition.destination or EXCLUDE_DEST_AUDIT_TAB,
    }


def _positive(n):
    """entry_count の防御的解釈（None / 非数値 / 負数を 0 扱いにする）。"""
    try:
        return int(n) > 0
    except (TypeError, ValueError):
        return False
