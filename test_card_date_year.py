"""クレカ明細の**年**を券面から確定する（2026-08-20 の回帰対応）。

## 何が起きたか

2026-08-20 の真票回帰で、同一の券面・同一の pipeline なのに前日と結果が
割れた。前日は 62 行すべて日付が正しく、当日は 7 行が空・18 行が**年だけ
1 年ずれた**（2023/12/22 ← 正しくは 2022/12/22）。

原因は前日に入れた prompt の一文（T8b-3）。F-1（1 つの PDF に別会社の明細が
混在する）対策として「他ページの情報で補わない」を独立条項へ格上げし、
「このページに見えているものだけを使います」を足した。ところが**年の確定は
推理を要する** —— アメックスの明細行の日付欄は「12月18日」のように月日
だけで、年はページ上部の「明細書作成日 2023年1月19日」から倒推するしかない。
その推理路を塞いだ結果、Gemini は頁ごとに違う振る舞いをした:

    p1 / p4 / p5 / p6 … 指示に反して推測し、たまたま当たった
    p2               … 指示に従い null を返した        → 空欄（赤で可視化）
    p3               … 指示に反して推測し、外した      → 2023/12（無音の誤り）

**空欄より無音の誤年の方が危険**。空欄は `missing_date` が赤にするので人が
気づくが、2023/12/22 は書式として正当なのでそのまま MoneyForward へ入る。

## この修正の立場

Gemini の当次の気分に依存させない。**年は券面の錨（明細書作成日）から
プログラムが確定する。** 月日は Gemini の読み取りを信じる（そこは券面に
明瞭に印字されており、実際 1 度も外していない）。

`transit_ic` の `_ic_date` とは**規則が違う**ので共用しない:

| | 参照日 | Gemini が入れた完全な日付 |
|---|---|---|
| transit_ic | 実行日（券面に手掛かりが無い） | **尊重する**（`test_printed_year_is_not_overwritten`） |
| credit_card | 券面の明細書作成日 | **年は上書きする**（p3 の 2023/12 を直すため） |

錨（作成日）が読めないときは**何もしない**。実行日で推すと、2023 年の
過去資料を 2026 年に処理したとき 12月18日 が 2025/12/18 に化ける —— 直そうと
した誤りより悪い。

外部依存なし（gspread / paddleocr 不要）:
    python -m unittest test_card_date_year -v
"""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))

import card_entries  # noqa: E402
from ocr_test_fixtures import _cc_row  # noqa: E402

# 実物の券面（2023年1月.pdf）の錨。全 8 頁の頁頭に印字されている。
_STMT_DATE = "2023/01/19"


def _entries(*rows, statement_date=_STMT_DATE):
    return card_entries.build_entries_from_credit_card({
        "card": {"issuer": "アメリカン・エキスプレス", "card_name": "テストカード",
                 "statement_date": statement_date},
        "rows": list(rows)})


def _one(row, statement_date=_STMT_DATE):
    return _entries(row, statement_date=statement_date)[0]


class YearComesFromTheStatementDateTest(unittest.TestCase):
    """券面の明細書作成日から年を決める。実測の 4 パターンを一対一で固定する。"""

    def test_month_day_only_is_completed(self):
        """p2 の形: Gemini が date を null にし month_day だけ残した。

        前日は 2022/12/18 と出ていた行。当日は空欄になった。
        """
        e = _one(_cc_row(1, None, "ＥＴＣ 特別割引 九州", 250, month_day="12/18"))
        self.assertEqual(e["date"], "2022/12/18")
        self.assertTrue(e["_year_estimated"], "券面から補ったことを記録する")

    def test_wrong_year_is_corrected(self):
        """p3 の形: Gemini が作成日の年をそのまま 12 月に貼った。

        **これがこの修正の主目的。** 2023/12/22 は書式として正当なので、
        既存のどの検知器も赤くしない。人が気づく手段が無い。
        """
        e = _one(_cc_row(1, "2023/12/22", "アマゾン JP マーケットプレイス", 5920,
                         month_day="12/22"))
        self.assertEqual(e["date"], "2022/12/22")
        self.assertTrue(e["_year_estimated"])

    def test_wrong_year_is_corrected_even_without_month_day(self):
        """month_day が空でも直せること。

        prompt は month_day を「年が無く月日だけのときの逐語」と定義して
        いるので、Gemini が年を推測した頁では **month_day を埋めない**
        可能性がある。実害の p3 がまさにその形かもしれない。月日を date
        側からも取れないと、直したい行だけ直せずに終わる。
        """
        e = _one(_cc_row(1, "2023/12/22", "アマゾン", 5920))
        self.assertEqual(e["date"], "2022/12/22")

    def test_correct_year_is_left_alone(self):
        """p1 / p4 の形: 既に正しい行を壊さない。"""
        e = _one(_cc_row(1, "2022/12/11", "ＥＴＣ 特別割引 九州", 250,
                         month_day="12/11"))
        self.assertEqual(e["date"], "2022/12/11")
        self.assertFalse(e["_year_estimated"],
                         "変えていないなら推定フラグを立てない")

    def test_january_rows_do_not_fall_back_a_year(self):
        """作成日と同じ年の 1 月。退年規則が効きすぎないこと。"""
        e = _one(_cc_row(1, "2023/01/15", "ダイレックス 佐賀県 佐賀市", 4396,
                         month_day="1/15"))
        self.assertEqual(e["date"], "2023/01/15")

    def test_the_boundary_day_is_kept(self):
        """作成日そのもの（2023/01/19）は「未来」ではない。"""
        e = _one(_cc_row(1, None, "テスト", 100, month_day="1/19"))
        self.assertEqual(e["date"], "2023/01/19")


class WithoutAnAnchorDoNothingTest(unittest.TestCase):
    """錨が無いときに実行日で推さない。**これを破ると被害が拡大する。**

    2023 年の過去資料を 2026 年に処理する運用なので、実行日を基準にすると
    12月18日 が 2025/12/18 になる。直そうとした誤りより遠い。
    """

    def test_no_statement_date_keeps_geminis_value(self):
        e = _one(_cc_row(1, "2023/12/22", "アマゾン", 5920, month_day="12/22"),
                 statement_date="")
        self.assertEqual(e["date"], "2023/12/22", "錨が無いなら触らない")
        self.assertFalse(e["_year_estimated"])

    def test_unparsable_statement_date_keeps_geminis_value(self):
        e = _one(_cc_row(1, "2023/12/22", "アマゾン", 5920, month_day="12/22"),
                 statement_date="読めない文字列")
        self.assertEqual(e["date"], "2023/12/22")

    def test_no_month_day_anywhere_stays_empty(self):
        """月日そのものが取れていない行は空のまま（赤で可視化される）。"""
        e = _one(_cc_row(1, None, "テスト", 100, month_day=""))
        self.assertEqual(e["date"], "")

    def test_a_broken_month_day_does_not_crash(self):
        """2月30日 のような存在しない日付でも落ちない。"""
        e = _one(_cc_row(1, None, "テスト", 100, month_day="2/30"))
        self.assertEqual(e["date"], "")


class TransitIcIsUntouchedTest(unittest.TestCase):
    """nimoca の規則は変えない。**参照日も上書き方針も違う。**

    ここが割れると、券面に手掛かりが無い nimoca が実行日ではなく
    存在しない `statement_date` を見に行って全行の日付を失う。
    """

    _NIMOCA = {"card": {"issuer": "nimoca", "card_name": "nimoca"},
               "rows": [{"line_no": 1, "date": None, "month_day": "5/1",
                         "amount": 260, "category": "電車",
                         "place_from": "西鉄福岡", "place_to": "薬院"}]}

    def test_reference_date_still_drives_the_year(self):
        entries = card_entries.build_entries_from_transit_ic(
            self._NIMOCA, reference_date=date(2026, 5, 20))
        self.assertEqual(entries[0]["date"], "2026/05/01")
        self.assertTrue(entries[0]["_year_estimated"])

    def test_printed_date_is_still_respected(self):
        raw = {**self._NIMOCA,
               "rows": [{**self._NIMOCA["rows"][0], "date": "2024/05/01"}]}
        entries = card_entries.build_entries_from_transit_ic(
            raw, reference_date=date(2026, 5, 20))
        self.assertEqual(entries[0]["date"], "2024/05/01",
                         "nimoca は印字された日付を上書きしない")
        self.assertFalse(entries[0]["_year_estimated"])


class RealStatementShapeTest(unittest.TestCase):
    """2023年1月.pdf の実データで、頁ごとに割れないことを確かめる。

    前日 62 行が正しく、当日 7 行空 ＋ 18 行誤年になった。同じ入力なら
    同じ出力になる —— それがこの修正の要求そのものである。
    """

    def test_every_page_shape_resolves_consistently(self):
        rows = [
            _cc_row(1, "2022/12/11", "p1: 正しい年", 250, month_day="12/11"),
            _cc_row(2, None, "p2: date が空", 560, month_day="12/18"),
            _cc_row(3, "2023/12/22", "p3: 年が 1 年先", 5920, month_day="12/22"),
            _cc_row(4, "2023/01/01", "p4: 年跨ぎの 1 月", 1800, month_day="1/1"),
            _cc_row(5, None, "p5: 副カードの 12 月", 7454, month_day="12/26"),
        ]
        got = [e["date"] for e in _entries(*rows)]
        self.assertEqual(got, ["2022/12/11", "2022/12/18", "2022/12/22",
                               "2023/01/01", "2022/12/26"])


if __name__ == "__main__":
    unittest.main()
