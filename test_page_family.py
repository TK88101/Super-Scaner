"""page_family（頁族分類器 ＋ AD-0 単一裁決関数）の単体テスト。

守りたい実害は 2 つある。

1. **仕訳の無音消失**（IP-401 の再演）。族シグナルが明細頁を否決すると、
   UC p2（永久不滅ポイント区画 ＋ 明細 12 行、F-6/F-10）のような実在頁で
   12 件が丸ごと消える。消えた仕訳は誰も気づけない。
2. **混載フォルダの取り違え**。nimoca がクレカ用 prompt で解析されると
   0 件になる（趙裁定 5: 同一フォルダ混載）。

したがって本モジュールの中心的な契約は
**「entries > 0 の頁に到達できる除外経路が存在しない」**（AD-2）であり、
それを順序ではなく制御フローの形で保証する（AD-0 の単一裁決関数）。

OCR テキストの fixture は事実台帳（2026-08-12-credit-card-sample-facts.md）に
**実測として記録された印字文字列**を材料に合成したものである。実 OCR の
生ダンプではない（台帳は逐語ダンプを保存していない）。合成であることを
明記しておく — これを「実測」と誤読すると、通ったテストが実機での動作を
保証しているかのように見えてしまう。

外部依存なし（gspread / paddleocr 不要）:
    python -m unittest test_page_family -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from doc_types import DocType  # noqa: E402
from page_dedup import (  # noqa: E402
    DedupVerdict,
    VERDICT_DUPLICATE,
    VERDICT_UNIQUE,
    VERDICT_NOT_ELIGIBLE,
)
from page_family import (  # noqa: E402
    ACTION_BOOK,
    ACTION_EXCLUDE,
    ACTION_UNRECOGNIZED,
    DEDUP_MODE_EXCLUDE,
    DEDUP_MODE_MARK,
    DEDUP_MODE_OFF,
    EXCLUDE_DEST_AUDIT_TAB,
    EXCLUDE_DEST_MF_TAB,
    FAMILY_CC_DETAIL,
    FAMILY_CC_SUMMARY,
    FAMILY_IC_HISTORY,
    FAMILY_INFO_NOTICE,
    FAMILY_POINTS_ONLY,
    FAMILY_UNKNOWN,
    STRENGTH_NONE,
    STRENGTH_STRONG,
    STRENGTH_WEAK,
    PageClass,
    classify_page,
    exclusion_fields,
    resolve_family,
    resolve_page_disposition,
    select_prompt_doc_type,
)


# ── fixture の材料 ────────────────────────────────────────────────


def _rows(n, amount=630, start=1):
    """明細行らしいテキストを n 行生成する（F-8 の主行の形）。"""
    return " ".join(
        "2026/04/%02d 加盟店%02d %s" % ((start + i) % 28 + 1, i, format(amount + i * 7, ","))
        for i in range(n)
    )


# 標本の正本は ocr_test_fixtures（複製すると正本の修正が伝播しない）。
# ocr_test_helpers ではない —— あちらは ocr_engine を引くので、
# 標本をあちらに置くとこのファイルが venv 必須になる（test_dependency_weight）。
# 局所名は従来どおりに束ね直し、以下 35 箇所の参照はそのまま使う。
from ocr_test_fixtures import (  # noqa: E402
    AMEX_HEAD as _AMEX_HEAD,
    NIMOCA_HEAD as _NIMOCA_HEAD,
    ic_rows as _ic_rows,
)


# 事実台帳 32 頁のうち、族が確定している 28 頁。
#
# 除いた 4 頁は **ENEOS p1 / JCB p1 / TS CUBIC p5(ETC 1/5) / UC p1** —— いずれも
# 各文書の先頭頁で、台帳は請求合計の所在（F-3）は記録しているが「明細行が
# あるか」を記録していないため、cc_detail と cc_summary のどちらかが確定しない。
# F-8 も ETC の行数を「4 頁（2/5〜5/5）」と限定しており 1/5 を暗に外している。
#
# 注: 族が変わっても記帳結果は変わらない（明細行があれば entries>0 で cc_detail、
# 無ければ 0 件で合計表扱い）ので、この 4 頁の不確定性に設計は依存していない。
#
# 形式: (名前, OCR テキスト, Gemini が返した entries 件数, 期待する族)
_LEDGER_PAGES = (
    ("楽天 p1 Visa ETC",
     "楽天カード ご利用明細 ****-****-****-1564 ご請求金額 12,940 " + _rows(17, 1460),
     17, FAMILY_CC_DETAIL),
    ("楽天 p2 Amex",
     "楽天カード ご請求金額 157,966 利用獲得ポイント 1,566ポイント " + _rows(23, 3200),
     23, FAMILY_CC_DETAIL),
    ("アメックス p1 カードA 1/6", _AMEX_HEAD + " " + _rows(4), 4, FAMILY_CC_DETAIL),
    ("アメックス p2 カードA 2/6",
     _AMEX_HEAD.replace("1/6", "2/6") + " 今月ご利用額合計 17,295 " + _rows(6, 3866),
     6, FAMILY_CC_DETAIL),
    ("アメックス p3 (=p1 の重複スキャン)", _AMEX_HEAD + " " + _rows(4), 4, FAMILY_CC_DETAIL),
    ("アメックス p4 (=p2 の重複スキャン)",
     _AMEX_HEAD.replace("1/6", "2/6") + " 今月ご利用額合計 17,295 " + _rows(6, 3866),
     6, FAMILY_CC_DETAIL),
    ("アメックス p5 カードB 1/3",
     "アメリカン・エキスプレス ****-******-11004 1/3 ページ お支払い金額内容 "
     "前回分口座振替金額 -35,808 今月ご利用額合計 933,680 " + _rows(4, 29640),
     4, FAMILY_CC_DETAIL),
    ("アメックス p6 ポイント頁",
     "ポイント・インフォメーション Marriott Bonvoy 獲得ポイント 1,781 前月残高 当月末",
     0, FAMILY_POINTS_ONLY),
    ("ニモカ p1", _NIMOCA_HEAD + " " + _ic_rows(20) + " 全120件 (1)～(20)", 20, FAMILY_IC_HISTORY),
    ("ニモカ p2", _NIMOCA_HEAD + " " + _ic_rows(20) + " 全120件 (21)～(40)", 20, FAMILY_IC_HISTORY),
    ("ニモカ p3", _NIMOCA_HEAD + " 入金 現金 5,000 " + _ic_rows(19) + " 全120件 (41)～(60)",
     19, FAMILY_IC_HISTORY),
    ("ニモカ p4", _NIMOCA_HEAD + " 入金 現金 5,000 " + _ic_rows(19) + " 全120件 (61)～(80)",
     19, FAMILY_IC_HISTORY),
    ("ニモカ p5", _NIMOCA_HEAD + " 入金 現金 5,000 " + _ic_rows(19) + " 全120件 (81)～(100)",
     19, FAMILY_IC_HISTORY),
    ("ニモカ p6", _NIMOCA_HEAD + " " + _ic_rows(20) + " 全120件 (101)～(120) SF残高 16,529円",
     20, FAMILY_IC_HISTORY),
    ("アレコレ p1",
     "アレコレカード ご利用代金明細書 8050-0003-4840-1805 ご請求金額 54,788 " + _rows(18, 1200),
     18, FAMILY_CC_DETAIL),
    ("アレコレ p2",
     "アレコレカード 8050-0003-4840-1805 ご請求金額 54,788 " + _rows(18, 1800),
     18, FAMILY_CC_DETAIL),
    ("アレコレ p3 案内頁",
     "インフォメーション ワールドプレゼント WEB明細のご案内 キャンペーンのお知らせ",
     0, FAMILY_INFO_NOTICE),
    ("ENEOS p2 明細",
     "ENEOS カード ご利用明細 XXXX-XXXX-XXXX-8602 2026/04/12 給油 7,380 "
     "2026/04/26 給油 11,123 2026/05/02 ENEOSポイントキャッシュバック -3,000 "
     "ご請求金額合計 15,503",
     3, FAMILY_CC_DETAIL),
    ("JCB p2",
     "JCB ご利用代金明細書 3541-0409-9687-2XXX お支払い小計 157,226 " + _rows(16, 9238),
     16, FAMILY_CC_DETAIL),
    ("TS CUBIC p1 合計表",
     "TS3 コーポレートカード ご利用代金合計表 1/1 T8010601027383 お支払合計 44,490 "
     "ポイント明細 前回残高 555 失効予定 -10",
     0, FAMILY_CC_SUMMARY),
    ("TS CUBIC p2 明細書兼請求書",
     "TS3 コーポレートカード 明細書兼請求書 1/1 6900-0512-4054-8959 "
     "お支払合計 44,490 2026/04/18 部品購入 44,490",
     1, FAMILY_CC_DETAIL),
    ("TS CUBIC p3 ENEOS BUSINESS 1/2",
     "ENEOS BUSINESS 明細書兼請求書 1/2 6900-0553-1626-9242 ご請求金額合計(A) 97,004 "
     + _rows(12, 8100),
     12, FAMILY_CC_DETAIL),
    ("TS CUBIC p4 ENEOS BUSINESS 2/2",
     "ENEOS BUSINESS 明細書兼請求書 2/2 6900-0553-1626-9242 " + _rows(10, 6400),
     10, FAMILY_CC_DETAIL),
    ("TS CUBIC p6 ETC 2/5",
     "ENEOS BUSINESS ETC 2/5 9200-0120-4450-2973 " + _rows(80, 630),
     80, FAMILY_CC_DETAIL),
    ("TS CUBIC p7 ETC 3/5",
     "ENEOS BUSINESS ETC 3/5 9200-0120-4450-2973 " + _rows(80, 640),
     80, FAMILY_CC_DETAIL),
    ("TS CUBIC p8 ETC 4/5",
     "ENEOS BUSINESS ETC 4/5 9200-0120-4450-2973 " + _rows(80, 650),
     80, FAMILY_CC_DETAIL),
    ("TS CUBIC p9 ETC 5/5",
     "ENEOS BUSINESS ETC 5/5 9200-0120-4450-2973 ご請求金額合計(A) 131,850 " + _rows(60, 660),
     60, FAMILY_CC_DETAIL),
    ("UC p2 ポイント区画＋明細12行",
     "UCカード 4542-3200-1424-2*** UC永久不滅ポイント 前月残高 9,367 当月獲得 79 当月末 9,446 "
     "合計 12口 79,050 " + _rows(12, 3300),
     12, FAMILY_CC_DETAIL),
)


def _dedup(kind=VERDICT_UNIQUE, origin_page=None):
    return DedupVerdict(kind, origin_page)


class LedgerPageFamilyTest(unittest.TestCase):
    """事実台帳で族が確定している 28 頁の族ラベルを固定する（T2 DoD）。"""

    def test_ledger_pages_resolve_to_expected_family(self):
        self.assertEqual(len(_LEDGER_PAGES), 28, "台帳で族が確定しているのは 28 頁")
        for name, text, entry_count, expected in _LEDGER_PAGES:
            with self.subTest(page=name):
                pc = classify_page(text)
                self.assertEqual(resolve_family(pc, entry_count), expected)

    def test_no_ledger_page_loses_its_entries(self):
        """entries を持つ台帳頁が 1 枚も除外されない（IP-401 の全数確認）。"""
        for name, text, entry_count, _ in _LEDGER_PAGES:
            if entry_count == 0:
                continue
            with self.subTest(page=name):
                d = resolve_page_disposition(_dedup(), entry_count, classify_page(text))
                self.assertEqual(d.action, ACTION_BOOK, "%s が記帳されない" % name)


class EntriesBeatFamilySignalTest(unittest.TestCase):
    """AD-2: 族シグナルは entries > 0 の頁を否決できない。"""

    def test_uc_p2_points_section_with_12_lines_is_not_points_only(self):
        """実在の反例（F-6 ＋ F-10）。ポイント語で否決すると 12 件が消える。"""
        text = ("UCカード UC永久不滅ポイント 前月残高 9,367 当月獲得 79 当月末 9,446 "
                "ポイント残高 9,446 " + _rows(12, 3300))
        pc = classify_page(text)

        self.assertGreaterEqual(pc.points_score, 2, "ポイント語は確かに命中している")
        self.assertEqual(resolve_family(pc, entry_count=12), FAMILY_CC_DETAIL)
        self.assertEqual(
            resolve_page_disposition(_dedup(), 12, pc).action, ACTION_BOOK)

    def test_summary_words_with_entries_still_book(self):
        """合計語が印字された明細頁（ENEOS p2 型）も記帳される。"""
        pc = classify_page("ご請求金額合計 15,503 " + _rows(3, 7380))
        self.assertEqual(resolve_page_disposition(_dedup(), 3, pc).action, ACTION_BOOK)

    def test_family_signal_with_entries_leaves_audit_signal(self):
        """否決はしないが、族シグナルが命中した事実は監査に残す（AD-2）。"""
        pc = classify_page("ポイント・インフォメーション 獲得ポイント 1,781 " + _rows(6))
        d = resolve_page_disposition(_dedup(), 6, pc)

        self.assertEqual(d.action, ACTION_BOOK)
        self.assertTrue(d.audit_signal, "族シグナルが黙って捨てられている")
        self.assertIn(FAMILY_POINTS_ONLY, d.audit_signal)

    def test_clean_detail_page_has_no_audit_noise(self):
        """シグナルが無い頁に監査ノイズを出さない（狼少年化の防止）。"""
        d = resolve_page_disposition(_dedup(), 8, classify_page(_AMEX_HEAD + " " + _rows(8)))
        self.assertIsNone(d.audit_signal)


class DetailRowVetoTest(unittest.TestCase):
    """AD-0 優先序 3: has_detail_rows は「除外に対する拒否権」としてのみ働く。"""

    def test_parse_failure_on_etc_page_becomes_loud_not_silent(self):
        """ETC 100 行頁で Gemini が壊れて 0 件になっても監査タブへ落とさない。

        この頁は末尾に「ご請求金額合計」を印字しているので、行らしさの
        拒否権が無いと cc_summary → 監査タブ → 100 行が静かに消える。
        """
        text = "ENEOS BUSINESS ETC 5/5 ご請求金額合計(A) 131,850 " + _rows(100, 630)
        pc = classify_page(text)

        self.assertTrue(pc.has_detail_rows)
        d = resolve_page_disposition(_dedup(), 0, pc)
        self.assertEqual(d.action, ACTION_UNRECOGNIZED)

    def test_ic_page_without_commas_still_has_detail_rows(self):
        """nimoca の 3 桁金額（150〜1,300 円）でも行らしさが立つこと。

        金額トークンをカンマ有りに限定すると nimoca 頁で拒否権が消える。
        """
        pc = classify_page(_NIMOCA_HEAD + " " + _ic_rows(20))
        self.assertTrue(pc.has_detail_rows)

    def test_detail_rows_do_not_force_booking_when_entries_exist(self):
        """拒否権は「除外させない」だけで、記帳の可否は entries が決める。"""
        pc = classify_page(_AMEX_HEAD + " " + _rows(20))
        self.assertEqual(resolve_page_disposition(_dedup(), 5, pc).action, ACTION_BOOK)


class ZeroEntryDestinationTest(unittest.TestCase):
    """entries==0 の頁の落とし先（趙裁定 7/8 ＋ AD-0）。"""

    def test_summary_only_page_goes_to_mf_tab(self):
        """裁定 8: 合計表のみの頁は MF タブへ金額 0 の提示行を書く。

        監査タブだけに落とすと「合計表を上げたのに何も出てこない」状態に
        なり、顧客が気づけない。
        """
        pc = classify_page("TS3 ご利用代金合計表 1/1 お支払合計 44,490")
        d = resolve_page_disposition(_dedup(), 0, pc)

        self.assertEqual(d.action, ACTION_EXCLUDE)
        self.assertEqual(d.destination, EXCLUDE_DEST_MF_TAB)
        self.assertEqual(d.family, FAMILY_CC_SUMMARY)
        self.assertIn("44490", d.reason.replace(",", ""))

    def test_points_only_page_goes_to_audit_tab(self):
        pc = classify_page("ポイント・インフォメーション Marriott Bonvoy 獲得ポイント 1,781")
        d = resolve_page_disposition(_dedup(), 0, pc)

        self.assertEqual(d.action, ACTION_EXCLUDE)
        self.assertEqual(d.destination, EXCLUDE_DEST_AUDIT_TAB)
        self.assertEqual(d.family, FAMILY_POINTS_ONLY)

    def test_info_notice_page_goes_to_audit_tab(self):
        pc = classify_page("インフォメーション WEB明細のご案内")
        d = resolve_page_disposition(_dedup(), 0, pc)

        self.assertEqual(d.action, ACTION_EXCLUDE)
        self.assertEqual(d.destination, EXCLUDE_DEST_AUDIT_TAB)

    def test_blank_page_becomes_unrecognized_not_excluded(self):
        """何も読めない頁は赤い認識不能行へ（無音で消さない）。"""
        d = resolve_page_disposition(_dedup(), 0, classify_page(""))
        self.assertEqual(d.action, ACTION_UNRECOGNIZED)


class DuplicateOutranksEverythingTest(unittest.TestCase):
    """AD-0 優先序 1: duplicate だけが entries より先に評価される（AD-1）。"""

    def test_duplicate_page_is_not_booked_even_with_entries(self):
        pc = classify_page(_AMEX_HEAD + " " + _rows(4))
        d = resolve_page_disposition(_dedup(VERDICT_DUPLICATE, origin_page=1), 4, pc)

        self.assertEqual(d.action, ACTION_EXCLUDE)
        self.assertEqual(d.destination, EXCLUDE_DEST_AUDIT_TAB)
        self.assertIn("duplicate", d.reason)
        self.assertIn("1", d.reason, "原本の頁番号が監査行に残らない")

    def test_non_duplicate_verdicts_never_exclude(self):
        """duplicate 以外の判定（key_conflict 等）で頁を落とさない。"""
        pc = classify_page(_AMEX_HEAD + " " + _rows(4))
        for kind in ("key_conflict", "content_only", VERDICT_NOT_ELIGIBLE, VERDICT_UNIQUE):
            with self.subTest(kind=kind):
                d = resolve_page_disposition(_dedup(kind), 4, pc)
                self.assertEqual(d.action, ACTION_BOOK)

    def test_none_verdict_is_treated_as_not_duplicate(self):
        """dedup を回していない経路（fail-open）でも落ちない。"""
        pc = classify_page(_AMEX_HEAD + " " + _rows(4))
        self.assertEqual(resolve_page_disposition(None, 4, pc).action, ACTION_BOOK)


class DedupModeTest(unittest.TestCase):
    """CREDIT_CARD_DEDUP_MODE（Plan §9.5）。偽陽性が出たとき段階的に弱められる。

    重複検出の偽陽性は仕訳を消す。実運用で誤検出が出たときに、コードを
    変えずに `mark`（記帳しつつ注記）や `off` へ落とせる逃げ道を用意しておく。
    """

    def _dup_page(self, mode):
        pc = classify_page(_AMEX_HEAD + " " + _rows(4))
        return resolve_page_disposition(_dedup(VERDICT_DUPLICATE, 1), 4, pc,
                                        dedup_mode=mode)

    def test_exclude_mode_is_the_default_behaviour(self):
        self.assertEqual(self._dup_page(DEDUP_MODE_EXCLUDE).action, ACTION_EXCLUDE)

    def test_mark_mode_books_the_page_and_keeps_the_evidence(self):
        d = self._dup_page(DEDUP_MODE_MARK)
        self.assertEqual(d.action, ACTION_BOOK)
        self.assertIn("duplicate_page", d.audit_signal)

    def test_off_mode_ignores_the_verdict_entirely(self):
        d = self._dup_page(DEDUP_MODE_OFF)
        self.assertEqual(d.action, ACTION_BOOK)
        self.assertIsNone(d.audit_signal)

    def test_mark_mode_keeps_both_signals_when_family_also_fires(self):
        pc = classify_page("ポイント・インフォメーション 獲得ポイント 1,781 " + _rows(6))
        d = resolve_page_disposition(_dedup(VERDICT_DUPLICATE, 1), 6, pc,
                                     dedup_mode=DEDUP_MODE_MARK)
        self.assertIn("duplicate_page", d.audit_signal)
        self.assertIn(FAMILY_POINTS_ONLY, d.audit_signal)

    def test_zero_entry_duplicate_still_excludes_under_mark_mode(self):
        """mark でも記帳する entries が無ければ、通常の族判定へ落ちる。"""
        pc = classify_page("ポイント・インフォメーション 獲得ポイント 1,781")
        d = resolve_page_disposition(_dedup(VERDICT_DUPLICATE, 1), 0, pc,
                                     dedup_mode=DEDUP_MODE_MARK)
        self.assertEqual(d.action, ACTION_EXCLUDE)


class FacetExtractionTest(unittest.TestCase):
    """T3/T4 の配線で使う facet（card_keys / sheet_no / issuer / signals）。

    配線が来るまで production の読み手が居ないので、ここで挙動を固定して
    おかないと「配線した瞬間に初めて壊れているのが判る」状態になる。
    """

    def test_card_keys_preserve_mask_characters(self):
        """末尾マスク（UC / JCB）を潰さないこと。下 4 桁を鍵にできない理由。"""
        pc = classify_page("UCカード 4542-3200-1424-2*** ご請求金額 79,050")
        self.assertTrue(pc.card_keys)
        self.assertIn("*", pc.card_keys[0])

    def test_issuer_registration_number_is_not_a_card_key(self):
        """F-11: 券面の T番号は**カード会社自身**の登録番号。

        T ＋ 13 桁なので、境界を見ないと 13 桁部分がカード番号として拾われる。
        下流が member_no に使うと、同一発行体の全カードが 1 枚に合流して
        検算がまとめて壊れる。
        """
        for t_number in ("T8700150009366", "T8010601027383"):
            with self.subTest(t_number=t_number):
                pc = classify_page("ご利用代金明細書 %s" % t_number)
                joined = "".join(pc.card_keys)
                self.assertNotIn(t_number[1:], joined)

    def test_real_card_number_next_to_a_t_number_is_still_found(self):
        """T番号を弾く条件が、本物のカード番号まで巻き込まないこと。"""
        pc = classify_page(_AMEX_HEAD)
        self.assertTrue(pc.card_keys)
        self.assertIn("26003", "".join(pc.card_keys))

    def test_card_keys_collect_multiple_numbers_without_choosing(self):
        pc = classify_page("6900-0512-4054-8959 と 6900-0553-1626-9242 の 2 枚")
        self.assertGreaterEqual(len(pc.card_keys), 2)

    def test_sheet_no_requires_an_anchor_word(self):
        """素の n/N は日付 4/10 と衝突するのでアンカー語隣接のみ採用する。"""
        self.assertEqual(classify_page(_AMEX_HEAD).sheet_no, (1, 6))
        self.assertEqual(classify_page("2026/04/10 福北高速 630").sheet_no, ())

    def test_sheet_no_is_dropped_when_ambiguous(self):
        pc = classify_page("1/6 ページ ... 2/6 ページ")
        self.assertEqual(pc.sheet_no, ())

    def test_issuer_detection_uses_t_numbers_and_brand_words(self):
        self.assertEqual(classify_page("T8700150009366 ご利用代金明細書").issuer, "amex")
        self.assertEqual(classify_page("楽天カード ご請求金額 157,966").issuer, "rakuten")
        self.assertEqual(classify_page("UC永久不滅ポイント 前月残高 9,367").issuer, "uc")

    def test_short_latin_tokens_are_not_issuer_signals_on_their_own(self):
        """`uc` / `ts3` のような 2〜3 字ラテンは OCR ノイズと衝突する。"""
        self.assertEqual(classify_page("uc ts3 の記載しかない頁").issuer, "")

    def test_signals_record_what_fired(self):
        pc = classify_page(_AMEX_HEAD + " 今月ご利用額合計 17,295 " + _rows(6))
        self.assertIn("issuer:amex", pc.signals)
        self.assertIn("declared_total", pc.signals)
        self.assertIn("detail_rows", pc.signals)


class DispositionPriorityMatrixTest(unittest.TestCase):
    """AD-0 の優先序を全組合せで固定する（T2 DoD）。"""

    def test_priority_order_over_all_combinations(self):
        pages = {
            "detail": classify_page(_AMEX_HEAD + " " + _rows(20)),      # has_detail_rows
            "summary": classify_page("お支払合計 44,490"),
            "points": classify_page("ポイント・インフォメーション 獲得ポイント 1,781"),
            "info": classify_page("インフォメーション ご案内"),
            "blank": classify_page(""),
        }
        for dup in (True, False):
            for entry_count in (0, 3):
                for name, pc in pages.items():
                    with self.subTest(dup=dup, entries=entry_count, page=name):
                        verdict = _dedup(VERDICT_DUPLICATE, 1) if dup else _dedup()
                        d = resolve_page_disposition(verdict, entry_count, pc)

                        if dup:
                            expected = ACTION_EXCLUDE
                        elif entry_count > 0:
                            expected = ACTION_BOOK
                        elif pc.has_detail_rows:
                            expected = ACTION_UNRECOGNIZED
                        elif name in ("summary", "points", "info"):
                            expected = ACTION_EXCLUDE
                        else:
                            expected = ACTION_UNRECOGNIZED
                        self.assertEqual(d.action, expected)

    def test_excluded_disposition_always_declares_destination(self):
        """producer が行き先を宣言する（main は理由から推測しない）。"""
        for text in ("お支払合計 44,490", "ポイント・インフォメーション 獲得ポイント 1,781",
                     "インフォメーション ご案内"):
            with self.subTest(text=text[:12]):
                d = resolve_page_disposition(_dedup(), 0, classify_page(text))
                self.assertEqual(d.action, ACTION_EXCLUDE)
                self.assertIn(d.destination, (EXCLUDE_DEST_AUDIT_TAB, EXCLUDE_DEST_MF_TAB))
                self.assertTrue(d.reason)


class TransitIcDetectionTest(unittest.TestCase):
    """交通系IC の判別（趙裁定 5: クレカと同一フォルダに混載）。"""

    def test_brand_plus_structure_is_strong(self):
        pc = classify_page(_NIMOCA_HEAD + " " + _ic_rows(20))
        self.assertEqual(pc.routing_family, FAMILY_IC_HISTORY)
        self.assertEqual(pc.ic_strength, STRENGTH_STRONG)

    def test_transit_ic_issuer_is_reported_without_making_it_a_credit_issuer(self):
        """発行体 facet は IC も報告するが、cc_strength には効かせない。

        効かせると nimoca 頁が「IC strong ＋ クレカ strong」で拮抗し、
        `family_ambiguous` に落ちて混載フォルダの自動分流が死ぬ。
        """
        pc = classify_page(_NIMOCA_HEAD + " " + _ic_rows(20))
        self.assertEqual(pc.issuer, "nimoca")
        self.assertEqual(pc.cc_strength, STRENGTH_NONE)

    def test_brand_alone_never_fires(self):
        """加盟店名としての `nimoca` 誤爆を潰す（クレカ明細が IC 扱いになる事故）。"""
        pc = classify_page("アメリカン・エキスプレス ご利用代金明細書 nimoca オートチャージ 3,000 "
                           + _rows(6))
        self.assertNotEqual(pc.routing_family, FAMILY_IC_HISTORY)

    def test_structure_only_is_weak_not_strong(self):
        """ブランド語を OCR が落とした場合の救済は weak 止まり。"""
        pc = classify_page("月日 種別 施設1 ～ 施設2 利用額 カードポイント センターポイント "
                           + _ic_rows(20))
        self.assertEqual(pc.routing_family, FAMILY_IC_HISTORY)
        self.assertEqual(pc.ic_strength, STRENGTH_WEAK)


class PromptSelectionTest(unittest.TestCase):
    """AD-2/§5.2: prompt 上書きは新規 2 型の相互取り違えに限る。"""

    def test_ic_page_in_credit_card_folder_switches_prompt(self):
        pc = classify_page(_NIMOCA_HEAD + " " + _ic_rows(20))
        doc_type, signal = select_prompt_doc_type(DocType.CREDIT_CARD, pc)

        self.assertEqual(doc_type, DocType.TRANSIT_IC)
        self.assertIn("family_override", signal)

    def test_existing_doc_types_are_never_overridden(self):
        """本番稼働中の経路の prompt は 1 ビットも変えない。"""
        pc = classify_page(_NIMOCA_HEAD + " " + _ic_rows(20))
        for folder_type in (DocType.RECEIPT, DocType.PURCHASE_INVOICE,
                            DocType.SALES_INVOICE, DocType.SALARY_SLIP):
            with self.subTest(folder=folder_type):
                doc_type, signal = select_prompt_doc_type(folder_type, pc)
                self.assertEqual(doc_type, folder_type)
                self.assertIsNotNone(signal, "取り違えの事実は監査に残す")

    def test_weak_signal_does_not_override(self):
        """弱いシグナルでフォルダ指定を覆さない（人間の明示的行為を信じる）。"""
        pc = classify_page("月日 種別 施設1 ～ 施設2 利用額 カードポイント センターポイント "
                           + _ic_rows(20))
        self.assertEqual(pc.ic_strength, STRENGTH_WEAK)
        doc_type, _ = select_prompt_doc_type(DocType.CREDIT_CARD, pc)
        self.assertEqual(doc_type, DocType.CREDIT_CARD)

    def test_ambiguous_page_keeps_folder_default(self):
        """IC 構造とクレカ発行体がともに strong なら上書きしない。"""
        text = (_NIMOCA_HEAD + " " + _ic_rows(20)
                + " アメリカン・エキスプレス T8700150009366 今月ご利用額合計 17,295")
        pc = classify_page(text)
        doc_type, signal = select_prompt_doc_type(DocType.CREDIT_CARD, pc)

        self.assertEqual(doc_type, DocType.CREDIT_CARD)
        self.assertEqual(signal, "family_ambiguous")

    def test_credit_card_page_in_transit_folder_switches_back(self):
        pc = classify_page(_AMEX_HEAD + " 今月ご利用額合計 17,295 " + _rows(6))
        doc_type, signal = select_prompt_doc_type(DocType.TRANSIT_IC, pc)

        self.assertEqual(doc_type, DocType.CREDIT_CARD)
        self.assertIn("family_override", signal)


class TotalityTest(unittest.TestCase):
    """classify_page は全域関数（例外を投げない・必ず PageClass を返す）。"""

    def test_never_raises_on_hostile_input(self):
        hostile = ("", None, "0" * 20000, "\x00\x01\x02", "１／２３４５",
                   "ページ ページ ページ", "全件", "-" * 5000, "🙂" * 500,
                   "1/2 3/4 5/6 7/8", "ご請求金額", "，，，，", "\n\n\n\t")
        for text in hostile:
            with self.subTest(text=repr(text)[:24]):
                pc = classify_page(text)
                self.assertIsInstance(pc, PageClass)

    def test_disposition_never_raises_on_hostile_page_class(self):
        for text in ("", None, "ご請求金額合計 999,999"):
            for entry_count in (-1, 0, 1, 10 ** 6):
                with self.subTest(text=repr(text)[:12], n=entry_count):
                    d = resolve_page_disposition(None, entry_count, classify_page(text))
                    self.assertIn(d.action, (ACTION_BOOK, ACTION_EXCLUDE, ACTION_UNRECOGNIZED))

    def test_broken_page_class_falls_back_to_unrecognized(self):
        """分類器が壊れても（error 付き）頁は無音消失しない。"""
        d = resolve_page_disposition(None, 0, PageClass(error="boom"))
        self.assertEqual(d.action, ACTION_UNRECOGNIZED)

    def test_resolve_family_accepts_none_page_class(self):
        self.assertEqual(resolve_family(None, 0), FAMILY_UNKNOWN)


class ExclusionFieldsTest(unittest.TestCase):
    """`exclusion_fields` は T9 で `_blank_result(**markers)` へ渡す出力契約。

    配線が来るまで production の呼び手が居ないので、ここで 3 キーの形を
    固定しておかないと「配線した瞬間に初めて壊れているのが判る」状態になる。
    変異テストで「空 dict を返す」「MF タブ行きを監査タブへ倒す」の両方が
    全テストを通過してしまったため、契約をテストで縛る。
    """

    def test_summary_page_declares_the_mf_tab_destination(self):
        d = resolve_page_disposition(_dedup(), 0, classify_page("お支払合計 44,490"))
        fields = exclusion_fields(d)

        self.assertTrue(fields["_excluded_page"])
        self.assertEqual(fields["_exclude_destination"], EXCLUDE_DEST_MF_TAB)
        self.assertIn("cc_summary", fields["_exclude_reason"])

    def test_points_page_declares_the_audit_tab_destination(self):
        d = resolve_page_disposition(
            _dedup(), 0, classify_page("ポイント・インフォメーション 獲得ポイント 1,781"))
        fields = exclusion_fields(d)

        self.assertEqual(fields["_exclude_destination"], EXCLUDE_DEST_AUDIT_TAB)
        self.assertEqual(set(fields),
                         {"_excluded_page", "_exclude_reason", "_exclude_destination"})

    def test_booked_and_unrecognized_pages_produce_no_markers(self):
        """除外でない頁に除外マーカーを付けない（付けると仕訳が消える）。"""
        booked = resolve_page_disposition(_dedup(), 5, classify_page(_AMEX_HEAD))
        unknown = resolve_page_disposition(_dedup(), 0, classify_page(""))

        self.assertEqual(exclusion_fields(booked), {})
        self.assertEqual(exclusion_fields(unknown), {})

    def test_destination_never_defaults_silently_to_audit(self):
        """全ての ACTION_EXCLUDE 経路が destination を明示していること。

        空文字のまま渡ると main が既定の監査タブへ落とし、裁定 8 の
        「合計表は MF タブへ」が静かに失われる。
        """
        for text in ("お支払合計 44,490", "ポイント・インフォメーション 獲得ポイント 1,781",
                     "インフォメーション ご案内"):
            with self.subTest(text=text[:12]):
                d = resolve_page_disposition(_dedup(), 0, classify_page(text))
                self.assertTrue(d.destination, "destination が空のまま")


class DuplicateEvidenceSurvivesTest(unittest.TestCase):
    """mark モードで entries が無い重複頁でも、重複の事実を落とさない。"""

    def test_zero_entry_duplicate_keeps_its_reason_under_mark_mode(self):
        pc = classify_page("ポイント・インフォメーション 獲得ポイント 1,781")
        d = resolve_page_disposition(_dedup(VERDICT_DUPLICATE, 1), 0, pc,
                                     dedup_mode=DEDUP_MODE_MARK)

        self.assertEqual(d.action, ACTION_EXCLUDE)
        self.assertIn("duplicate_page", d.reason)
        self.assertIn(FAMILY_POINTS_ONLY, d.reason)

    def test_unrecognized_duplicate_keeps_its_reason_under_mark_mode(self):
        pc = classify_page(_AMEX_HEAD + " " + _rows(20))
        d = resolve_page_disposition(_dedup(VERDICT_DUPLICATE, 3), 0, pc,
                                     dedup_mode=DEDUP_MODE_MARK)

        self.assertEqual(d.action, ACTION_UNRECOGNIZED)
        self.assertIn("duplicate_page", d.reason)


class DefensiveBranchTest(unittest.TestCase):
    """docstring が約束している防御分岐を、実際に通してコードの事実にする。"""

    def test_non_numeric_entry_count_is_treated_as_zero(self):
        """`_positive` の防御分岐。entry_count が壊れていても頁を消さない。"""
        pc = classify_page(_AMEX_HEAD + " " + _rows(20))
        for broken in (None, "たくさん", object()):
            with self.subTest(entry_count=repr(broken)[:16]):
                d = resolve_page_disposition(_dedup(), broken, pc)
                # 記帳扱いにはならないが、除外もされない（赤い占位行へ）
                self.assertEqual(d.action, ACTION_UNRECOGNIZED)

    def test_classifier_failure_is_swallowed_and_reported(self):
        """分類器が例外を投げても頁を落とさない（fail-open の except 本体）。

        既存の TotalityTest は例外を起こさない入力しか渡しておらず、
        握りつぶし分岐自体は 1 度も実行されていなかった。
        """
        class _Exploding:
            def __str__(self):
                raise RuntimeError("boom")

        pc = classify_page(_Exploding())

        self.assertIsInstance(pc, PageClass)
        self.assertIn("RuntimeError", pc.error)
        self.assertEqual(pc.routing_family, FAMILY_UNKNOWN)

    def test_a_page_whose_classifier_failed_still_yields_a_disposition(self):
        class _Exploding:
            def __str__(self):
                raise RuntimeError("boom")

        d = resolve_page_disposition(_dedup(), 0, classify_page(_Exploding()))

        self.assertEqual(d.action, ACTION_UNRECOGNIZED)
        self.assertIn("RuntimeError", d.reason)


class ContractWithOcrEngineTest(unittest.TestCase):
    """ocr_engine 側の定数と文字列が一致していること。

    page_family は venv 非依存を保つため定数を自前で持つ。乖離すると
    main._record_excluded_page が既定の監査タブへ落ち、裁定 8 の
    「MF タブへ提示行」が静かに失われる。
    """

    def test_exclude_destination_constants_match(self):
        try:
            import ocr_engine
        except Exception as e:  # noqa: BLE001 - venv 無しでは import できない
            self.skipTest("ocr_engine を import できない環境: %s" % type(e).__name__)
        self.assertEqual(EXCLUDE_DEST_AUDIT_TAB, ocr_engine.EXCLUDE_DEST_AUDIT_TAB)
        self.assertEqual(EXCLUDE_DEST_MF_TAB, ocr_engine.EXCLUDE_DEST_MF_TAB)

    def test_simplified_char_map_matches(self):
        """簡体字写像の乖離は「片方でだけキーワードが当たる」事故になる。"""
        try:
            import ocr_engine
        except Exception as e:  # noqa: BLE001
            self.skipTest("ocr_engine を import できない環境: %s" % type(e).__name__)
        import page_family
        self.assertEqual(page_family._SIMPLIFIED_TO_JP, ocr_engine._SIMPLIFIED_TO_JP)


if __name__ == "__main__":
    unittest.main()
