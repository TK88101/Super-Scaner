"""IP-401 の原始事故シナリオを丸ごと再走させる回帰テスト（T5）。

個別の DoD は各タスクのテストが押さえている。ここが押さえるのは
「顧客が報告した現象そのもの」——54枚アップして仕訳が53件になった、を
決定論的な fixture で再現し、二度と起きないことを固定する。

実 API は呼ばない（Codex 低2: 実 Gemini E2E は回帰検証にならない）。
固定 PaddleOCR テキスト＋固定 Gemini JSON で全経路を通す。

    venv311/bin/python -m unittest test_ip401_regression -v
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import ocr_engine
from doc_types import DocType
from ocr_test_helpers import page_ocrs_from_tuples

# Plan §1.2 の実測値。この票が無音で消えた。
MAIZURU_OCR = (
    "舞鶴パーク\n☆领収证☆\nNo.05\n入庫26/07/1723：33:2\n"
    "精算26/07/1801：10:1\n現金\n200円"
)
NORMAL_OCR = "領収書 テスト店 合計1,100円 現金"
ENVELOPE_OCR = "〒100-0001 東京都千代田区 御中 親展"


def _maizuru_gemini():
    """Gemini はこの票を正しく読めていた（占位行が無かったことから逆算）。"""
    return {"documents": [{
        "doc_category": "receipt", "payment_method": "現金",
        "vendor": "舞鶴パーク", "date": "2026/07/18",
        "items": [{"description": "駐車場代", "amount": 200,
                   "tax_rate": 0.10, "debit_account": "旅費交通費"}],
    }]}


def _normal_gemini():
    return {"documents": [{
        "doc_category": "receipt", "payment_method": "現金",
        "vendor": "テスト店",
        "items": [{"description": "商品", "amount": 1100,
                   "tax_rate": 0.10, "debit_account": "備品・消耗品費"}],
    }]}


def _pdf_pages(n):
    return [{"page_num": i, "total_pages": n, "data": f"%PDF-p{i}".encode(),
             "filename": f"p{i}.pdf"} for i in range(1, n + 1)]


def _run_pipeline(pages, routes):
    """指定ページ構成で RECEIPT×多頁 PDF 経路を通す。"""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(b"%PDF-1.4 dummy")
        path = tmp.name
    try:
        with mock.patch.object(ocr_engine, "_split_pdf_pages",
                               return_value=iter(pages)), \
             mock.patch.object(ocr_engine, "_route_ocr_strategy",
                               side_effect=page_ocrs_from_tuples(
                                   routes, DocType.RECEIPT)), \
             mock.patch.object(ocr_engine, "_call_gemini_bytes",
                               return_value=None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                out = list(ocr_engine.process_pipeline(
                    path, doc_type=DocType.RECEIPT, ocr_strategy="C"))
        return out, buf.getvalue()
    finally:
        os.unlink(path)


class OriginalIncidentTest(unittest.TestCase):
    """「54枚アップしたが仕訳は53件」——欠落の再現と非再現。"""

    def test_misread_receipt_is_no_longer_dropped(self):
        # Arrange: p1 = 事故票（OCR 誤読で封筒判定に落ちていた）/ p2 = 正常
        pages = _pdf_pages(2)
        routes = [
            (_maizuru_gemini(), MAIZURU_OCR, 0.883),
            (_normal_gemini(), NORMAL_OCR, 0.95),
        ]

        # Act
        out, _ = _run_pipeline(pages, routes)

        # Assert: 2頁とも出力され、事故票の仕訳が実在する
        self.assertEqual(len(out), 2)
        by_page = {p["page_num"]: p["result"] for p in out}
        self.assertEqual(by_page[1]["vendor"], "舞鶴パーク")
        self.assertEqual(len(by_page[1]["entries"]), 1)
        self.assertEqual(by_page[1]["entries"][0]["amount"], 200)
        # 除外・エラーのいずれにも分類されていない
        self.assertFalse(by_page[1].get("_excluded_page"))
        self.assertFalse(by_page[1].get("_page_error"))

    def test_every_page_produces_at_least_one_output(self):
        """枚数と件数の乖離が起きない: 全頁が必ず1件以上を出力する。

        封筒も、認識不能も、エラーも、必ず何かを yield する。顧客が
        「アップした枚数」と「表の行数」を数えて初めて気づく欠落を無くす。
        """
        # Arrange: 事故票 / 正常 / 本物の封筒 / Gemini 応答なし
        pages = _pdf_pages(4)
        routes = [
            (_maizuru_gemini(), MAIZURU_OCR, 0.883),
            (_normal_gemini(), NORMAL_OCR, 0.95),
            ({"documents": []}, ENVELOPE_OCR, 0.9),
            (None, "", None),
        ]

        # Act
        out, _ = _run_pipeline(pages, routes)

        # Assert: 4頁すべてが出力に現れる
        self.assertEqual({p["page_num"] for p in out}, {1, 2, 3, 4})

    def test_coverage_warning_is_silent_when_nothing_is_missing(self):
        # Arrange
        pages = _pdf_pages(2)
        routes = [
            (_maizuru_gemini(), MAIZURU_OCR, 0.883),
            (_normal_gemini(), NORMAL_OCR, 0.95),
        ]

        # Act
        _, log = _run_pipeline(pages, routes)

        # Assert
        self.assertNotIn("ページカバレッジ警告", log)

    def test_coverage_warning_fires_when_a_page_yields_nothing(self):
        """§8-中7: 将来また別経路で無音欠落が生まれたときの最終哨戒。

        個別経路（封筒・整形例外）は塞いだので、ここでは _yield_page_results
        自体が何も返さない状況を注入して哨戒が鳴ることを確かめる。
        """
        # Arrange: p1 の整形が何も yield しない
        real = ocr_engine._yield_page_results
        calls = {"n": 0}

        def fake(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return iter([])
            return real(*args, **kwargs)

        pages = _pdf_pages(2)
        routes = [
            (_normal_gemini(), NORMAL_OCR, 0.95),
            (_normal_gemini(), NORMAL_OCR, 0.95),
        ]

        # Act
        with mock.patch.object(ocr_engine, "_yield_page_results",
                               side_effect=fake):
            out, log = _run_pipeline(pages, routes)

        # Assert: p1 が欠落し、警告が出る
        self.assertEqual({p["page_num"] for p in out}, {2})
        self.assertIn("ページカバレッジ警告", log)
        self.assertIn("[1]", log)


if __name__ == "__main__":
    unittest.main()
