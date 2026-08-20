"""T8b-2: 区画の取りこぼしを Gemini の**外**から検査する純関数の単体テスト。

守りたい実害（2026-08-19 実測）: 主副カードが 1 通に合印された券面の p5 で、
Gemini が主カード分だけを報告し**副カード 11 行・146,671 円が静かに消えた**。
`rows_on_page` の自己申告も 8（＝取得数と一致）だったので `card_salvage` の
行欠け検出も沈黙し、**3 層すべてが無音**になった。

設計原則（Plan §3.2）:
    検算の基準を被検査者自身の申告から取ってはならない。
    被検査者が漏らしたものは、その申告にも現れない。

したがって検査は 3 本立てる。1 本でも欠けると沈黙する経路が残る:

| # | 発火条件 | 塞ぐ穴 |
|---|---|---|
| 1 | `markers > len(sections)` | Gemini が区画そのものを 1 つしか申告しない |
| 2 | `markers >= 2` かつ `rows[].sec` の distinct <= 1 | 区画は 2 個申告しつつ行は片側だけ |
| 3 | 検出 unknown なのに明細が在る | OCR 入力が無く検査器自身が沈黙した |

#2 は Codex HIGH-3、#3 は Codex HIGH-4 の指摘に対応する。

**警告は監査タブへ出すだけで記帳は止めない**（趙拍板 2026-08-19 / IP-401）。
偽陽性の帳簿被害はゼロで、偽陰性は無音欠落。倒す向きは決まっている。

外部依存なし（gspread / paddleocr 不要）:
    python -m unittest test_section_audit -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import page_family  # noqa: E402
from page_family import section_audit_signals  # noqa: E402

# p5 の版面（実測の構造。顧客実名・カード番号は含めない）。
# 合計行（前の区画の終わり）＋ 区画頭（次の区画の始まり）で markers = 2。
_SWITCH_TEXT = (
    "AMERICAN EXPRESS 利用代金明細書 518ペー 田中 太郎様 会员番号 ******71008 "
    "1月3日 イデミツ アポロ シェル 7,254 1月5日 ソフトバク 10,000 "
    "田中 太郎 様今月乙利用额合計 350,218 "
    "今月ご利用额 田中 花子様 会员番号 016 "
    "12月21 ソフトバク 10,000 12月26 ローレル石販 7,454"
)
# 単一カード券面（区画頭 1 つだけ）。markers = 1。
_SINGLE_TEXT = (
    "AMERICAN EXPRESS 利用代金明細書 118ペー 田中 太郎様 会员番号 ******71008 "
    "今月ご利用额 田中 太郎様 "
    "12月11日 ETC 特別割引 九州 250 12月16日 ソフトバク 10,000"
)


def _raw(sections=1, rows=1, sec=0):
    """`sections` 個の区画を申告し、`rows` 行すべてを区画 `sec` に属させた raw_data。

    既定は p5 の実測形（申告 1 区画・全行が区画 0）。
    """
    return {
        "card": {},
        "sections": [{"index": i, "label": "今月ご利用額", "subtotal": None}
                     for i in range(sections)],
        "rows": [{"line_no": i + 1, "amount": 1000, "merchant": "テスト",
                  "sec": sec} for i in range(rows)],
        "rows_on_page": rows,
    }


def _cc(ocr_text, raw):
    return section_audit_signals(page_family.DOC_TYPE_CREDIT_CARD, ocr_text, raw)


class SectionUndercountTest(unittest.TestCase):
    """#1: 券面から数えた区画数 > Gemini の申告数。"""

    def test_p5_shape_fires(self):
        """実害そのもの: 券面 2 区画・申告 1 区画。"""
        signals = _cc(_SWITCH_TEXT, _raw(sections=1, rows=8, sec=0))
        self.assertIn("section_undercount:2/1", signals)

    def test_single_card_page_is_silent(self):
        """単一カード券面で誤報しない（T8b-2 の DoD）。"""
        self.assertEqual(_cc(_SINGLE_TEXT, _raw(sections=1, rows=4)), ())

    def test_continuation_page_is_silent(self):
        """マーカーの無い続き頁。申告 0 区画でも 0 > 0 は偽。"""
        text = "AMERICAN EXPRESS 218ペー 12月18日 ETC 250 12月20日 セブン 2,246"
        self.assertEqual(_cc(text, _raw(sections=0, rows=2)), ())

    def test_declared_more_than_detected_is_silent(self):
        """申告の方が多いのは取りこぼしではない（検出器の見落とし側）。"""
        self.assertEqual(_cc(_SINGLE_TEXT, _raw(sections=3, rows=4)), ())


class SectionRowsMissingTest(unittest.TestCase):
    """#2: 区画は複数申告しているのに行が片側にしか属していない（Codex HIGH-3）。

    `markers > len(sections)` だけでは塞げない抜け道。Gemini が sections を
    2 個返しつつ rows は主カードの 8 行だけ、という形は `2 > 2` が偽なので
    #1 では沈黙する。「区画が 2 つ在ると認識しているのに行は片側だけ」は
    構造的に異常である。
    """

    def test_two_sections_but_rows_all_in_one(self):
        signals = _cc(_SWITCH_TEXT, _raw(sections=2, rows=8, sec=0))
        self.assertIn("section_rows_missing:2/1", signals)
        self.assertFalse([s for s in signals if s.startswith("section_undercount")],
                         "2 > 2 は偽なので #1 は鳴らない。#2 だけが塞ぐ")

    def test_rows_spread_over_both_sections_is_silent(self):
        raw = _raw(sections=2, rows=4, sec=0)
        raw["rows"][2]["sec"] = 1
        raw["rows"][3]["sec"] = 1
        self.assertEqual(_cc(_SWITCH_TEXT, raw), ())

    def test_null_sec_counts_as_no_distribution(self):
        """`sec` が全部 null なら「区画を区別できていない」と同じ。

        prompt は「判らなければ null」と書いてあるので Gemini は堂々と
        null を返す。null を健全扱いすると #2 が丸ごと無力化する。
        """
        raw = _raw(sections=2, rows=8, sec=None)
        self.assertIn("section_rows_missing:2/0", _cc(_SWITCH_TEXT, raw))

    def test_string_sec_is_accepted(self):
        """Gemini が `"sec": "1"` と文字列で返しても区画分布として数える。

        厳格に int だけ受けると、正しく分けている頁を distinct=0 と誤読して
        警告が鳴る。帳簿被害はゼロだが監査タブの信号対雑音比が落ちる。
        """
        raw = _raw(sections=2, rows=4, sec="0")
        raw["rows"][2]["sec"] = "1"
        raw["rows"][3]["sec"] = "1"
        self.assertEqual(_cc(_SWITCH_TEXT, raw), ())

    def test_single_marker_page_never_fires(self):
        """markers < 2 なら区画分布を問わない（続き頁の全行 sec=0 は正常）。"""
        self.assertEqual(_cc(_SINGLE_TEXT, _raw(sections=1, rows=4)), ())


class DetectionUnknownTest(unittest.TestCase):
    """#3: 検査器自身が沈黙した頁を「健全」と断じない（Codex HIGH-4）。

    検査器は Gemini の外に在るが、**同じ page bytes / OCR 品質に依存する**。
    PaddleOCR が下半分を落とした・Vision 兜底で `ocr_text` が空、という場合は
    検査器も何も数えられない。そこを 0 個（＝健全）に丸めると、
    **一番危ない頁だけが静かになる**。
    """

    def test_no_ocr_text_with_detail_rows_is_unknown(self):
        self.assertIn("section_detection_unknown",
                      _cc("", _raw(sections=1, rows=8)))

    def test_no_ocr_text_without_rows_is_silent(self):
        """明細が 1 行も無い頁まで unknown で鳴らすと監査タブが埋まる。

        検査対象は「明細を組んだのに検算できない頁」だけでよい。
        """
        self.assertEqual(_cc("", _raw(sections=0, rows=0)), ())

    def test_type_broken_rows_still_fires(self):
        """`rows` の型が契約違反でも黙らない。

        断言を「必ず unknown」にはしない —— この入力は sections も無いので
        `section_undercount:2/0` という**より具体的な**信号が出るのが正しい。
        守りたいのは信号名ではなく「壊れた入力が沈黙に化けないこと」。
        """
        self.assertTrue(_cc(_SWITCH_TEXT, {"rows": "行ではない文字列"}),
                        "壊れた入力を「検査した結果 問題なし」に丸めてはいけない")

    def test_double_failure_is_the_loudest_case_not_the_quietest(self):
        """JSON 破損 ∧ OCR 空。**2 つの独立信号が同時に落ちた頁**。

        当初の実装はここだけ `()` を返していた（simplify 評審 P1 で発覚）。
        raw_data を落としてから `rows` の有無で分岐していたため、壊れた
        raw_data が空 rows に化け、「明細が無いなら鳴らさない」の枝に
        吸われていた。**一番危ない組み合わせが一番静か**という、IP-401 と
        同じ形の欠陥。
        """
        for broken in (None, [], "文字列", 42):
            with self.subTest(raw_data=type(broken).__name__):
                self.assertIn("section_detection_unknown", _cc("", broken))

    def test_unparseable_raw_data_is_unknown(self):
        """raw_data 自体が読めないときは unknown を出す（握り潰さない）。

        例外を投げないのは `page_family` の約束だが、() を返すと
        「検査した結果 問題なし」と区別がつかなくなる。
        """
        self.assertIn("section_detection_unknown", _cc(_SWITCH_TEXT, None))


class ScopeTest(unittest.TestCase):
    """検査を効かせる範囲。"""

    def test_transit_ic_has_no_sections(self):
        """nimoca の prompt に `sections` は無い（`_IC_TOP_FIELDS`）。

        検査を効かせると全頁が `section_undercount:1/0` で鳴り続ける。
        """
        self.assertEqual(
            section_audit_signals(page_family.DOC_TYPE_TRANSIT_IC,
                                  _SWITCH_TEXT, _raw(sections=0, rows=8)), ())

    def test_legacy_doc_types_are_untouched(self):
        self.assertEqual(
            section_audit_signals("receipt", _SWITCH_TEXT, _raw()), ())

    def test_every_doc_type_outside_the_table_is_silent(self):
        """`SECTION_AUDIT_DOC_TYPES` に無い doc_type は 1 件も鳴らさない。

        静的に 2 型を書くのではなく `DocType.ALL` を歩く。この repo は
        doc_type の並行表を 7 枚持ち、登録漏れで 2 度焼かれている
        （ENTRY_BUILDERS / RECON_POLICY）。将来 3 つ目の line-mode 型が
        増えたとき、**誰かがこの表を見に来る**ことを機械で強制する。

        新しい doc_type を足して赤くなったら、それは「この検査を効かせるか」
        の判断を求められているという意味で、テストを直す前に決めること。
        """
        from doc_types import DocType  # noqa: PLC0415 — 依存を局所化する
        for dt in DocType.ALL:
            if dt in page_family.SECTION_AUDIT_DOC_TYPES:
                continue
            with self.subTest(doc_type=dt):
                self.assertEqual(
                    section_audit_signals(dt, _SWITCH_TEXT, _raw(sections=0, rows=8)),
                    (), "表に無い doc_type で警告が鳴っている")


class SignalsCoexistTest(unittest.TestCase):
    """#1 と #2 は同じ頁で同時に成立しうる。片方に丸めない。"""

    def test_both_fire_together(self):
        signals = _cc(_SWITCH_TEXT, _raw(sections=1, rows=8, sec=0))
        self.assertIn("section_undercount:2/1", signals)
        self.assertIn("section_rows_missing:2/1", signals)


if __name__ == "__main__":
    unittest.main()
