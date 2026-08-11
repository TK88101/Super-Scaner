"""benchmark_e2e の単体テスト（B5 T4/T5＋頁切出）。

Plan＝docs/plans/2026-08-10-b5-benchmark-ss.md §4.1／§5 T4・T5。

T4 の歯型（評審 #11）：自作の InMemoryLedger で代替せず、**真の PostingLedger**
  に fake Firestore transport を挿す。自作代替は本物の局所計算と状態順序を
  飛ばしてしまい、測っているものが生産と別物になる。

T5 の歯型（評審 #20）：計時は注入した単調時計で決定的に検証する。実 sleep で
  「許容誤差」を見る方式は CI スケジューラ次第で揺れるうえ、ストップウォッチが
  動くことしか示さない。

頁切出：契約 headless の入力は 1 頁／1 切片。切出は**計時区間の外**で行う
  （評審 #2——切出まで計ると UI 多頁形状の数字が混ざる）。
"""

import os
import tempfile
import unittest
from unittest import mock

import benchmark_e2e as be


class SplitToSinglePagesTest(unittest.TestCase):
    """多頁 PDF → 単頁ファイル群（計時区間外の前処理）。"""

    def _make_pdf(self, path, pages):
        from pypdf import PdfWriter
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=200, height=200)
        with open(path, "wb") as f:
            writer.write(f)

    def test_多頁PDFは頁数分のファイルになる(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "three.pdf")
            self._make_pdf(src, 3)
            out = os.path.join(tmp, "out")
            produced = be.split_to_single_pages([src], out)
            self.assertEqual(len(produced), 3)
            for p in produced:
                self.assertTrue(os.path.exists(p))

    def test_切出したファイルは各一頁である(self):
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "five.pdf")
            self._make_pdf(src, 5)
            out = os.path.join(tmp, "out")
            for p in be.split_to_single_pages([src], out):
                self.assertEqual(len(PdfReader(p).pages), 1)

    def test_画像はそのまま一単位として通る(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "a.png")
            with open(src, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")
            out = os.path.join(tmp, "out")
            produced = be.split_to_single_pages([src], out)
            self.assertEqual(len(produced), 1)

    def test_出力名に元ファイルと頁番号が入る(self):
        # 後から「どの頁がどのファイル由来か」を辿れること（報告の追跡性）
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.pdf")
            self._make_pdf(src, 2)
            out = os.path.join(tmp, "out")
            produced = be.split_to_single_pages([src], out)
            names = [os.path.basename(p) for p in produced]
            self.assertTrue(any("src" in n and "1" in n for n in names))
            self.assertTrue(any("src" in n and "2" in n for n in names))


class FakeBackedLedgerTest(unittest.TestCase):
    """T4：真 PostingLedger ＋ fake transport（自作代替を使わない）。"""

    def _writer(self):
        writer = mock.Mock()
        writer.probe_page = mock.Mock(return_value=None)
        return writer

    def test_返るのは本物のPostingLedgerである(self):
        from posting_ledger import PostingLedger
        ledger, page_outcomes, fs = be.build_fake_backed_ledger(self._writer(), "job-x")
        self.assertIsInstance(ledger, PostingLedger)

    def test_page_outcomesも本物のreporterである(self):
        from firestore_progress import FirestorePageOutcomesReporter
        ledger, page_outcomes, fs = be.build_fake_backed_ledger(self._writer(), "job-x")
        self.assertIsInstance(page_outcomes, FirestorePageOutcomesReporter)

    def test_transportはfake_firestoreである(self):
        from fake_firestore import FakeFirestore
        ledger, page_outcomes, fs = be.build_fake_backed_ledger(self._writer(), "job-x")
        self.assertIsInstance(fs, FakeFirestore)


class InjectedClockTest(unittest.TestCase):
    """T5：注入時計で決定的に計る（実 sleep を使わない）。"""

    def test_経過時間は注入時計の差分になる(self):
        ticks = iter([100.0, 103.5])

        with mock.patch.object(be, "_invoke_process_file",
                               return_value=("POSTED_NOW", None)):
            record = be.measure_one(
                "dummy.pdf", writer=mock.Mock(), doc_type="receipt",
                base="job-1", clock=lambda: next(ticks),
                preflight=lambda p: {"page_count": 1})

        self.assertAlmostEqual(record["elapsed_sec"], 3.5)

    def test_終態が記録に載る(self):
        ticks = iter([0.0, 1.0])
        with mock.patch.object(be, "_invoke_process_file",
                               return_value=("EXCLUDED", None)):
            record = be.measure_one(
                "dummy.pdf", writer=mock.Mock(), doc_type="receipt",
                base="job-1", clock=lambda: next(ticks),
                preflight=lambda p: {"page_count": 1})
        self.assertEqual(record["outcome"], "EXCLUDED")

    def test_例外が出ても記録は必ず一件出る(self):
        # 評審 #9：試行が黙って母数から消えないこと
        ticks = iter([0.0, 2.0])
        with mock.patch.object(be, "_invoke_process_file",
                               side_effect=RuntimeError("boom")):
            record = be.measure_one(
                "dummy.pdf", writer=mock.Mock(), doc_type="receipt",
                base="job-1", clock=lambda: next(ticks),
                preflight=lambda p: {"page_count": 1})
        self.assertEqual(record["outcome"], "UNKNOWN")
        self.assertIn("boom", record["error"])
        self.assertAlmostEqual(record["elapsed_sec"], 2.0)

    def test_前提違反は記録を作らず例外を上げる(self):
        # preflight は「測ってよいか」の門。破れたら測定自体を止める
        import benchmark_preflight as pf

        def failing_preflight(path):
            raise pf.PreflightError("HEADLESS_MODE が立っていない")

        with self.assertRaises(pf.PreflightError):
            be.measure_one("dummy.pdf", writer=mock.Mock(), doc_type="receipt",
                           base="job-1", clock=lambda: 0.0,
                           preflight=failing_preflight)

    def test_出力の終態は層別軸の値域に収まる(self):
        import benchmark_stats as bs
        ticks = iter([0.0, 1.0])
        with mock.patch.object(be, "_invoke_process_file",
                               return_value=("POSTED_NOW", None)):
            record = be.measure_one(
                "dummy.pdf", writer=mock.Mock(), doc_type="receipt",
                base="job-1", clock=lambda: next(ticks),
                preflight=lambda p: {"page_count": 1})
        self.assertIn(record["outcome"], bs.OUTCOMES)


if __name__ == "__main__":
    unittest.main()
