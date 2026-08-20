"""T8b-4: 要求 #4（各人小計・総額行）の現状を回帰として固定する。

**兜底は入れない**（Plan T8b-4）。実測で「合計行が rows に混入する」事象は
発生しておらず、発生していない事象に先行実装しない。ここが固定するのは
現状の 2 つの事実で、将来どちらかが崩れたら赤くなることだけを保証する。

1. `card_entries` に合計行の兜底は**無い**。混入すれば普通の明細として記帳
   される（`_is_subtotal_line` は領収書経路専用で card 経路は通らない）。
2. それでも**無音にはならない**。券面の合計行は左端の日付欄が空で
   （2026-08-19 の版面実見）、真の明細行は必ず M月D日 を持つ。日付空は
   `missing_date`（severity=high ＝ 赤系）として既存の検知器が拾う。

2 が在るから 1 を急いで塞がなくてよい、という判断の記録である。逆に言えば
**2 が壊れたらこの判断ごと無効**になるので、両方を 1 つのファイルで縛る。

外部依存なし（gspread / paddleocr 不要）:
    python -m unittest test_card_total_row -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import card_entries  # noqa: E402
from anomaly_detector import detect_anomalies  # noqa: E402
from ocr_test_fixtures import _cc_row  # noqa: E402

# 2026-08-19 の版面実見。「<氏名> 様 今月ご利用額合計」で、**左端の日付欄が空**。
# 350,218（主）＋ 162,615（副）＝ 512,833（券面の総額）で厳密一致する。
#
# 行の既定形は `ocr_test_fixtures._cc_row` が正本。手で dict を書くと
# prompt に键が増えたとき**この標本だけ古いまま緑であり続ける**
# （`ocr_test_fixtures` の docstring が警告する漂移）。
_TOTAL_ROW = _cc_row(9, None, "田中 太郎 様 今月ご利用額合計", 350218)
_DETAIL_ROW = _cc_row(1, "2023/01/03", "イデミツ アポロ シェル", 7254,
                      month_day="1/3")


def _entries(*rows):
    return card_entries.build_entries_from_credit_card(
        {"card": {"issuer": "アメリカン・エキスプレス", "card_name": "テストカード"},
         "rows": list(rows)})


def _flags(entry):
    """生産経路と同じ形で検知器を呼ぶ（`sheets_output` の逐行記帳分岐）。

    line_mode の parent は**行級**（`{"date", "vendor"}`）。doc 級のまま
    渡すと全行に赤が付くので生産側がそう組んでおり、ここを doc 級で書くと
    テストだけが通る死んだ標本になる。
    """
    parent = {"date": entry.get("date", ""),
              "vendor": entry.get("debit_vendor", "")}
    return {f["type"]: f for f in detect_anomalies(entry, parent)}


class NoProgramSideFallbackTest(unittest.TestCase):
    """1: 合計行の兜底は無い。**それが現状であることを明示的に固定する**。

    このテストが赤くなったら「誰かが兜底を入れた」という意味で、それ自体は
    悪くない。ただし Plan T8b-4 の判断（先行実装しない）を覆す変更なので、
    テストを直す前に判断の方を更新すること。
    """

    def test_a_leaked_total_row_is_booked_like_any_other_row(self):
        entries = _entries(_TOTAL_ROW)
        self.assertEqual(len(entries), 1,
                         "card 経路に合計行フィルタは存在しない")
        self.assertEqual(entries[0]["description"],
                         "田中 太郎 様 今月ご利用額合計")

    def test_the_receipt_side_filter_does_not_reach_here(self):
        """`_is_subtotal_line` は `ocr_engine` の領収書経路専用。

        名前が似ているので「もう守られている」と誤読しやすい。card 経路の
        builder は `card_entries` に在り、あちらを 1 バイトも参照しない。
        """
        self.assertFalse(hasattr(card_entries, "_is_subtotal_line"))


class ALeakedTotalRowIsNotSilentTest(unittest.TestCase):
    """2: 兜底が無くても無音欠落にはならない（IP-401 の観点）。

    合計行が紛れ込む害は「金額が二重に乗る」ことだが、それが**気づかれずに**
    帳簿へ入るかどうかで深刻度が変わる。券面の構造上、合計行には日付が無い。
    """

    def test_missing_date_flags_the_leaked_row_in_red(self):
        flags = _flags(_entries(_TOTAL_ROW)[0])
        self.assertIn("missing_date", flags, "紛れ込んだ合計行が無印で通った")
        self.assertEqual(flags["missing_date"]["severity"], "high",
                         "赤系でなければ 300 行の明細に埋もれる")

    def test_a_real_detail_row_is_not_flagged(self):
        """真の明細行は必ず日付を持つ。誤報ゼロでなければ 2 は成立しない。"""
        self.assertNotIn("missing_date", _flags(_entries(_DETAIL_ROW)[0]))

    def test_the_pair_is_distinguishable(self):
        """同じ頁に両方在っても、赤が付くのは合計行だけ。"""
        detail, total = _entries(_DETAIL_ROW, _TOTAL_ROW)
        self.assertNotIn("missing_date", _flags(detail))
        self.assertIn("missing_date", _flags(total))


if __name__ == "__main__":
    unittest.main()
