"""B4 Plan §2.1/T1: ocr_engine の頁エラー三分類（`_error_class`）固定テスト。

process_pipeline の `_page_error` yield 点は全倉で 2 箇所（grep 済み、2026-07-24、
`grep -n '"_page_error": True' ocr_engine.py`）:
    ocr_engine.py:1813 例外分岐（`except Exception as page_err:` 内、~1802 起点）
    ocr_engine.py:1836 JSON 解析失敗分岐（`if not page_raw_data:` 内、~1822 起点）
（尾段の単ページ PDF/画像経路は `_page_error` を一切 yield しない——raw_data が
Vision 兜底後も無ければ無音 return するだけの既存挙動で、本 Plan の対象外。）

本テストは両方に `_error_class` が正しく付与されることを固定する:
    例外分岐: `isinstance(page_err, _GEMINI_RETRY_EXCEPTIONS)`（**子類含む**）なら
              "RETRYABLE"、それ以外の例外は "UNKNOWN"。
    JSON 解析失敗分岐: 例外を伴わない（モデル応答はあるが JSON 化できない）ため
              常に "CONTENT"。

ocr_engine は paddleocr / google.generativeai 等の重依存を import するため
venv311 で実行する:
    venv311/bin/python -m unittest test_ocr_engine_error_class -v
    venv311/bin/python -m pytest test_ocr_engine_error_class.py -v
"""
from __future__ import annotations

import http.client
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


def _valid_receipt_raw():
    """1品目の正常な領収書 JSON（毎回新規生成し相互汚染を防ぐ）。"""
    return {"documents": [{
        "doc_category": "receipt",
        "payment_method": "現金",
        "vendor": "テスト店",
        "items": [{
            "description": "商品",
            "amount": 1100,
            "tax_rate": 0.10,
            "debit_account": "備品・消耗品費",
        }],
    }]}


# 封筒/メモ判定 (_is_envelope_page) に落ちないよう構造キーワードを含める
_VALID_OCR_TEXT = "領収書 テスト店 合計1,100円 現金"


def _two_pdf_pages():
    return iter([
        {"page_num": 1, "total_pages": 2, "data": b"%PDF-p1", "filename": "r_p1.pdf"},
        {"page_num": 2, "total_pages": 2, "data": b"%PDF-p2", "filename": "r_p2.pdf"},
    ])


def _run_pipeline(route_side_effect, *, vision_side_effect=None, vision_return=None):
    """RECEIPT×多ページ PDF 分岐を通し、yield された結果を全件返す。

    route_side_effect: `_route_ocr_strategy` のページ順戻り値リスト
    (raw, ocr_text, conf)。raw=None のページは Vision 兜底（`_call_gemini_bytes`）
    に落ちる。vision_side_effect を渡すとその兜底呼び出しで例外を発生させ
    （例外分岐の RETRYABLE/UNKNOWN 判定を駆動）、渡さなければ vision_return を
    そのまま返す（JSON 解析失敗分岐＝CONTENT を駆動、既定 None）。
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(b"%PDF-1.4 dummy")
        path = tmp.name

    vision_kwargs = (
        {"side_effect": vision_side_effect} if vision_side_effect is not None
        else {"return_value": vision_return}
    )

    try:
        with mock.patch.object(ocr_engine, "_split_pdf_pages",
                               return_value=_two_pdf_pages()), \
             mock.patch.object(ocr_engine, "_route_ocr_strategy",
                               side_effect=route_side_effect), \
             mock.patch.object(ocr_engine, "_call_gemini_bytes", **vision_kwargs):
            with redirect_stdout(io.StringIO()):
                pages = list(ocr_engine.process_pipeline(
                    path, doc_type=DocType.RECEIPT, ocr_strategy="C"))
        return pages
    finally:
        os.unlink(path)


class _CustomRetryableSubclass(ConnectionError):
    """独自の transport 例外サブクラス（isinstance 継承判定の固定用、T1 DoD「子類含む」）。"""


_P1_FAILS_P2_OK = [
    (None, "", None),
    (_valid_receipt_raw(), _VALID_OCR_TEXT, 0.95),
]


class ExceptionBranchErrorClassTest(unittest.TestCase):
    """例外分岐（ocr_engine.py:1802-1818）の三分類固定。"""

    def test_known_retryable_exception_is_classified_retryable(self):
        pages = _run_pipeline(
            list(_P1_FAILS_P2_OK),
            vision_side_effect=http.client.RemoteDisconnected("boom"))
        by_page = {p["page_num"]: p for p in pages}
        self.assertTrue(by_page[1]["result"]["_page_error"])
        self.assertEqual(by_page[1]["result"]["_error_class"], "RETRYABLE")
        # 正常頁には _error_class を付けない（既存 result dict を汚染しない）
        self.assertNotIn("_error_class", by_page[2]["result"])

    def test_custom_subclass_of_retryable_type_is_classified_retryable(self):
        # isinstance 語義（含子類）: _GEMINI_RETRY_EXCEPTIONS 型そのものでなくても
        # isinstance() が真になるサブクラスなら RETRYABLE と分類されなければならない
        pages = _run_pipeline(
            list(_P1_FAILS_P2_OK),
            vision_side_effect=_CustomRetryableSubclass("boom"))
        by_page = {p["page_num"]: p for p in pages}
        self.assertEqual(by_page[1]["result"]["_error_class"], "RETRYABLE")

    def test_unrelated_exception_is_classified_unknown(self):
        pages = _run_pipeline(
            list(_P1_FAILS_P2_OK), vision_side_effect=ValueError("boom"))
        by_page = {p["page_num"]: p for p in pages}
        self.assertEqual(by_page[1]["result"]["_error_class"], "UNKNOWN")

    def test_all_pages_exception_all_classified(self):
        pages = _run_pipeline(
            [(None, "", None), (None, "", None)],
            vision_side_effect=TimeoutError("boom"))
        self.assertEqual(len(pages), 2)
        for p in pages:
            self.assertEqual(p["result"]["_error_class"], "RETRYABLE")


class JsonParseFailureErrorClassTest(unittest.TestCase):
    """JSON 解析失敗分岐（ocr_engine.py:1822-1841、例外を伴わない）は常に CONTENT。"""

    def test_json_parse_failure_is_classified_content(self):
        pages = _run_pipeline(list(_P1_FAILS_P2_OK), vision_return=None)
        by_page = {p["page_num"]: p for p in pages}
        self.assertTrue(by_page[1]["result"]["_page_error"])
        self.assertEqual(by_page[1]["result"]["_error_class"], "CONTENT")

    def test_all_pages_json_failure_all_classified_content(self):
        pages = _run_pipeline([(None, "", None), (None, "", None)],
                              vision_return=None)
        self.assertEqual(len(pages), 2)
        for p in pages:
            self.assertEqual(p["result"]["_error_class"], "CONTENT")


class ClassifyPageErrorUnitTest(unittest.TestCase):
    """`ocr_engine._classify_page_error`（純関数）の直接単体テスト。"""

    def test_known_retryable_types_are_classified_retryable(self):
        for exc in (
            http.client.RemoteDisconnected("x"),
            ConnectionError("x"),
            ConnectionResetError("x"),
            TimeoutError("x"),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(ocr_engine._classify_page_error(exc), "RETRYABLE")

    def test_subclass_of_retryable_type_is_classified_retryable(self):
        self.assertEqual(
            ocr_engine._classify_page_error(_CustomRetryableSubclass("x")),
            "RETRYABLE")

    def test_unrelated_exceptions_are_classified_unknown(self):
        for exc in (ValueError("x"), KeyError("x"), RuntimeError("x")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(ocr_engine._classify_page_error(exc), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
