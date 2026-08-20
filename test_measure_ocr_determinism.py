"""`scripts/measure_ocr_determinism.py` の出力に実票の中身を混ぜない。

Codex 実装評審（2026-08-20）重大 1。**この計測器は顧客の実票 PDF に
かけるもの**なので、OCR テキストの断片を既定で標準出力に出すと、
店名・カード末尾・金額がターミナルの履歴やリダイレクト先に残る。

既存コードの慣例もそうなっている —— `ocr_engine` は
`📝 PaddleOCR完了 (13文字, 置信度: 0.880)` と**長さと置信度しか出さない**。
本文を出していたのはこの計測器だけだった。

`scripts/` はパッケージではないので importlib で直接読む。

    venv311/bin/python -m unittest test_measure_ocr_determinism -v
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "scripts", "measure_ocr_determinism.py")

_spec = importlib.util.spec_from_file_location("_measure_ocr", _SCRIPT)
measure_ocr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(measure_ocr)

# 実票を模した合成テキスト（本物ではない）。これが出力に現れたら漏洩。
SECRET_A = "セブンイレブン千代田店 レギュラー 3,240円 ****1234"
SECRET_B = "セブンイレブン千代田店 レギュラー 3,248円 ****1234"


class ReportSecrecyTest(unittest.TestCase):

    def _report(self, texts, show_text=False):
        return "\n".join(measure_ocr.format_page_report(
            3, texts, [0.88] * len(texts), show_text=show_text))

    def test_identical_pages_leak_nothing(self):
        report = self._report([SECRET_A, SECRET_A])
        self.assertNotIn("セブン", report)
        self.assertNotIn("1234", report)
        self.assertIn("同一", report)

    def test_differing_pages_leak_nothing_by_default(self):
        """相違があるときこそ本文を出したくなるが、既定では出さない。"""
        report = self._report([SECRET_A, SECRET_B])
        self.assertNotIn("セブン", report)
        self.assertNotIn("3,240", report)
        self.assertNotIn("3,248", report)
        # 相違があった事実と、どこで違うかは判ること
        self.assertIn("相違", report)
        self.assertIn("idx=", report)

    def test_show_text_opts_into_the_snippet(self):
        """明示指定したときは、相違箇所の前後（idx±20 文字）が出る。"""
        report = self._report([SECRET_A, SECRET_B], show_text=True)
        self.assertIn("3,240", report)
        self.assertIn("3,248", report)

    def test_hash_distinguishes_pages_without_revealing_them(self):
        same = self._report([SECRET_A, SECRET_A])
        differ = self._report([SECRET_A, SECRET_B])
        self.assertIn("sha=", same)
        self.assertIn("sha=", differ)
        self.assertNotEqual(same, differ)


class FirstDifferenceTest(unittest.TestCase):

    def test_identical_returns_none(self):
        self.assertIsNone(measure_ocr.first_difference("abc", "abc"))

    def test_reports_the_index_of_the_first_differing_character(self):
        index, _left, _right = measure_ocr.first_difference("abcd", "abXd")
        self.assertEqual(index, 2)

    def test_a_pure_truncation_is_reported_at_the_shorter_length(self):
        index, _left, _right = measure_ocr.first_difference("abcd", "ab")
        self.assertEqual(index, 2)


if __name__ == "__main__":
    unittest.main()
