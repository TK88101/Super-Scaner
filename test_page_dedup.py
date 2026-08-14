"""page_dedup（重複ページ検出）の単体テスト。

守りたい実害: F-2 の実測どおり、同一原紙が 2 度スキャンされた PDF が実在する
（アメックスサンプル p1≡p3, p2≡p4）。これを検出しないと同じ金額を 2 回記帳し、
顧客の会計が壊れる。

同時に、**偽陽性は仕訳を消す**ので重複検出そのものが IP-401 の
「頁は無音で消えない」不変式への脅威になる。したがって本モジュールの
中心的な契約は「二重署名 AND 規則」——識別キーと内容ダイジェストの
**両方**が一致したときにだけ duplicate と判定する。

外部依存なし（gspread / paddleocr 不要）なので venv 無しでも動く:
    python -m unittest test_page_dedup -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from page_dedup import (  # noqa: E402
    PageDedupIndex,
    NullDedupIndex,
    safe_fingerprint,
    VERDICT_UNIQUE,
    VERDICT_DUPLICATE,
    VERDICT_KEY_CONFLICT,
    VERDICT_CONTENT_ONLY,
    VERDICT_NOT_ELIGIBLE,
)


def _raw(member_no="****-******-26003", page="1/6", period="2026-05",
         rows=None, total=17295, issuer="AMEX"):
    """テスト用の raw_data を組み立てる（D4 プロンプト出力の形）。"""
    if rows is None:
        rows = [
            {"date": "2026/04/10", "amount": 630},
            {"date": "2026/04/10", "amount": 630},
            {"date": "2026/04/21", "amount": 630},
            {"date": "2026/04/21", "amount": 630},
        ]
    return {
        "card": {"member_no": member_no, "issuer": issuer,
                 "statement_page": page, "period": period},
        "rows": rows,
        "total_amount": total,
    }


# OCR テキストは identity の裏取りに使われるので、券面の値を含めておく
_OCR = "ご利用代金明細書 ****-******-26003 1/6 ページ 2026年5月21日 17,295"


class TwoSignatureRuleTest(unittest.TestCase):
    """二重署名 AND 規則: 片方の署名だけでは絶対に duplicate にしない。"""

    def test_identical_page_scanned_twice_is_duplicate(self):
        # Arrange: F-2 のアメックス p1≡p3 相当（識別キーも内容も完全一致）
        idx = PageDedupIndex()
        fp1 = safe_fingerprint(_OCR, _raw())
        idx.remember(fp1, 1)

        # Act
        verdict = idx.classify(safe_fingerprint(_OCR, _raw()), 3)

        # Assert
        self.assertEqual(verdict.kind, VERDICT_DUPLICATE)
        self.assertEqual(verdict.origin_page, 1)

    def test_same_key_different_content_is_not_duplicate(self):
        """同じ会員番号・同じ頁番号でも中身が違えば重複ではない。

        頁番号の OCR 誤読で偽の一致が起きうる。ここで duplicate にすると
        別頁の仕訳が消える。可視化はするが記帳は止めない。
        """
        idx = PageDedupIndex()
        idx.remember(safe_fingerprint(_OCR, _raw()), 1)

        other_rows = [{"date": "2026/04/24", "amount": 5799},
                      {"date": "2026/04/24", "amount": 3866},
                      {"date": "2026/04/28", "amount": 3220}]
        verdict = idx.classify(
            safe_fingerprint(_OCR, _raw(rows=other_rows, total=12885)), 2)

        self.assertEqual(verdict.kind, VERDICT_KEY_CONFLICT)
        self.assertNotEqual(verdict.kind, VERDICT_DUPLICATE)

    def test_same_content_different_card_is_not_duplicate(self):
        """内容が一致しても別カードなら重複ではない。

        ETC の定額通行料など、別カードで同額・同日の明細が並ぶことは
        現実にある（F-8: 630 円が繰り返し現れる）。
        """
        idx = PageDedupIndex()
        idx.remember(safe_fingerprint(_OCR, _raw()), 1)

        ocr_b = "ご利用代金明細書 ****-******-11004 1/3 ページ"
        verdict = idx.classify(
            safe_fingerprint(ocr_b, _raw(member_no="****-******-11004",
                                         page="1/3")), 5)

        self.assertEqual(verdict.kind, VERDICT_CONTENT_ONLY)
        self.assertNotEqual(verdict.kind, VERDICT_DUPLICATE)


class FalsePositiveGuardTest(unittest.TestCase):
    """偽陽性を構造的に防ぐ（偽陽性 = 正当な仕訳の消失）。"""

    def test_handwriting_difference_does_not_break_duplicate_detection(self):
        """手書き注記の有無は指紋に影響しない。

        F-2 の実測: p1 には赤字「通信費と車輌交通費として」があり p3 には無い。
        内容ダイジェストは日付と金額だけから作るので、この差は無視される。
        """
        idx = PageDedupIndex()
        idx.remember(safe_fingerprint(_OCR + " 通信費と車輌交通費として", _raw()), 1)

        verdict = idx.classify(safe_fingerprint(_OCR, _raw()), 3)

        self.assertEqual(verdict.kind, VERDICT_DUPLICATE)

    def test_different_period_vetoes_duplicate(self):
        """同一カードの同一頁番号でも、請求月が違えば別の明細書。

        毎月同じ「1/6 ページ」が来るので、これを重複にすると
        翌月分が丸ごと消える。
        """
        idx = PageDedupIndex()
        idx.remember(safe_fingerprint(_OCR, _raw(period="2026-05")), 1)

        verdict = idx.classify(safe_fingerprint(_OCR, _raw(period="2026-06")), 9)

        self.assertEqual(verdict.kind, VERDICT_UNIQUE)

    def test_single_line_page_is_not_eligible(self):
        """明細 1 行だけの頁は判定対象にしない（偶然の一致が起きやすい）。"""
        idx = PageDedupIndex()
        one = [{"date": "2026/05/03", "amount": 44490}]
        fp = safe_fingerprint(_OCR, _raw(rows=one, total=44490))
        idx.remember(fp, 1)

        verdict = idx.classify(safe_fingerprint(_OCR, _raw(rows=one, total=44490)), 2)

        self.assertEqual(verdict.kind, VERDICT_NOT_ELIGIBLE)

    def test_missing_identity_is_not_eligible(self):
        """会員番号が読めなかった頁は重複判定の資格を失う（＝記帳される）。"""
        idx = PageDedupIndex()
        blind = _raw(member_no="")
        idx.remember(safe_fingerprint("明細書", blind), 1)

        verdict = idx.classify(safe_fingerprint("明細書", blind), 2)

        self.assertEqual(verdict.kind, VERDICT_NOT_ELIGIBLE)


class FailOpenTest(unittest.TestCase):
    """指紋生成に失敗しても記帳を止めない（fail-open）。"""

    def test_broken_raw_data_returns_none_without_raising(self):
        for broken in (None, {}, {"rows": None}, {"rows": [{"amount": "abc"}]}):
            with self.subTest(broken=broken):
                fp = safe_fingerprint("", broken)
                self.assertTrue(fp is None or not fp.is_eligible())

    def test_none_fingerprint_never_becomes_duplicate(self):
        idx = PageDedupIndex()
        idx.remember(safe_fingerprint(_OCR, _raw()), 1)

        self.assertEqual(idx.classify(None, 2).kind, VERDICT_NOT_ELIGIBLE)


class RegistrationGuardTest(unittest.TestCase):
    """記帳できなかった頁を「記帳済み」として登録しない。

    Codex/D3 検証の P0: sheets_output.py:244 は amount==0 の行を書かずに
    skip する。producer 側が「entries はあった」と誤認して登録すると、
    後続の真の重複頁が除外され、金額が **0 回** 記帳される。
    """

    def test_zero_amount_page_is_not_registered(self):
        idx = PageDedupIndex()
        zero_rows = [{"date": "2026/04/10", "amount": 0},
                     {"date": "2026/04/11", "amount": 0}]
        idx.remember(safe_fingerprint(_OCR, _raw(rows=zero_rows, total=0)), 1)

        # 同じ内容の頁が来ても、1 頁目は登録されていないので除外されない
        verdict = idx.classify(
            safe_fingerprint(_OCR, _raw(rows=zero_rows, total=0)), 2)

        self.assertNotEqual(verdict.kind, VERDICT_DUPLICATE)

    def test_first_occurrence_wins_as_origin(self):
        """先着優先。origin は常に最初の頁を指す。"""
        idx = PageDedupIndex()
        idx.remember(safe_fingerprint(_OCR, _raw()), 1)
        idx.remember(safe_fingerprint(_OCR, _raw()), 3)

        self.assertEqual(idx.classify(safe_fingerprint(_OCR, _raw()), 7).origin_page, 1)


class NullIndexTest(unittest.TestCase):
    """dedup 無効時の既定実装は何も除外しない。"""

    def test_null_index_never_reports_duplicate(self):
        idx = NullDedupIndex()
        fp = safe_fingerprint(_OCR, _raw())
        idx.remember(fp, 1)

        self.assertEqual(idx.classify(fp, 3).kind, VERDICT_NOT_ELIGIBLE)


class MemoryFootprintTest(unittest.TestCase):
    """逐頁ストリーミングのメモリモデルを壊さない。"""

    def test_index_does_not_retain_page_rows(self):
        """索引が保持するのは要約値のみ。明細行そのものを持たない。"""
        idx = PageDedupIndex()
        big_rows = [{"date": "2026/04/%02d" % (i % 28 + 1), "amount": 500 + i}
                    for i in range(300)]
        idx.remember(safe_fingerprint(_OCR, _raw(rows=big_rows)), 1)

        blob = repr(idx.__dict__)
        self.assertNotIn("2026/04/15", blob)
        self.assertLess(len(blob), 2000)


if __name__ == "__main__":
    unittest.main()
