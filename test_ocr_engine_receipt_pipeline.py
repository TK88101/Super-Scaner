"""領収書多ページ PDF 分岐の「JSON 解析失敗ページ無声 skip」可視化テスト。

Session 14 で発見した連帯穴: 領収書多ページ PDF 経路
(ocr_engine.process_pipeline の RECEIPT×PDF 分岐) で、あるページの
Gemini 応答が JSON として解析できない（page_raw_data=None、Vision 兜底も
失敗）とき failed_pages を数えるだけで何も yield せず continue していた。

  部分失敗（他ページは成功）→ main.process_file は error_pages=0 と数え
  Success 判定 → 原票アーカイブ → 失敗ページのデータが無音欠落。

同分岐の例外経路は既に `_page_error` 付き占位 result を yield している
(ページ処理エラー)。本テストは JSON 解析失敗も同じ扱いであることを固定する。
全ページ失敗時は count==error_pages → main が Failed 判定 → ファイル保持、
という従来語義（count==0 → Failed）と同値に保たれることも固定する。

ocr_engine は paddleocr / google.generativeai 等の重依存を import するため
venv311 で実行する:
    venv311/bin/python -m unittest test_ocr_engine_receipt_pipeline -v
    venv311/bin/python -m pytest test_ocr_engine_receipt_pipeline.py -v
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
# 封筒キーワードのみ・金額関連キーワード無し → _is_envelope_page が True を返す
_ENVELOPE_OCR_TEXT = "〒100-0001 東京都千代田区 御中"


def _two_pdf_pages():
    return iter([
        {"page_num": 1, "total_pages": 2, "data": b"%PDF-p1", "filename": "r_p1.pdf"},
        {"page_num": 2, "total_pages": 2, "data": b"%PDF-p2", "filename": "r_p2.pdf"},
    ])


def _run_receipt_pipeline(route_side_effect):
    """RECEIPT×多ページ PDF 分岐を通し、yield された結果を全件返す。

    route_side_effect: _route_ocr_strategy のページ順戻り値リスト
    (raw, ocr_text, conf)。raw=None のページは Vision 兜底も失敗させる。
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(b"%PDF-1.4 dummy")
        path = tmp.name

    try:
        with mock.patch.object(ocr_engine, "_split_pdf_pages",
                               return_value=_two_pdf_pages()), \
             mock.patch.object(ocr_engine, "_route_ocr_strategy",
                               side_effect=route_side_effect), \
             mock.patch.object(ocr_engine, "_call_gemini_bytes",
                               return_value=None) as vision:
            with redirect_stdout(io.StringIO()):
                pages = list(ocr_engine.process_pipeline(
                    path, doc_type=DocType.RECEIPT, ocr_strategy="C"))
        return pages, vision
    finally:
        os.unlink(path)


class JsonParseFailurePageVisibilityTest(unittest.TestCase):
    """JSON 解析失敗ページは _page_error 付きで必ず yield されること。"""

    def test_partial_failure_yields_page_error_for_failed_page(self):
        # Arrange: p1=解析失敗(raw=None, Vision 兜底も None) / p2=正常
        route = [
            (None, "読めないテキスト", None),
            (_valid_receipt_raw(), _VALID_OCR_TEXT, 0.95),
        ]

        # Act
        pages, _ = _run_receipt_pipeline(route)

        # Assert: 2 ページとも yield され、p1 に _page_error が立つ
        # （立たないと main が error_pages=0 → Success → p1 データ無音欠落）
        self.assertEqual(len(pages), 2)
        by_page = {p["page_num"]: p for p in pages}
        self.assertTrue(by_page[1]["result"].get("_page_error"))
        self.assertTrue(by_page[1]["result"].get("_unrecognized"))
        self.assertEqual(by_page[1]["result"]["entries"], [])
        self.assertFalse(by_page[2]["result"].get("_page_error"))
        self.assertEqual(len(by_page[2]["result"]["entries"]), 1)

    def test_all_pages_fail_yields_all_page_errors(self):
        # Arrange: 全ページ解析失敗
        route = [
            (None, "", None),
            (None, "", None),
        ]

        # Act
        pages, _ = _run_receipt_pipeline(route)

        # Assert: 全ページ _page_error 付き yield
        # → main は count==error_pages → Failed → ファイル保持（従来語義と同値）
        self.assertEqual(len(pages), 2)
        for p in pages:
            self.assertTrue(p["result"].get("_page_error"),
                            f"page {p['page_num']} に _page_error が無い")

    def test_failed_page_carries_page_bytes_for_source_link(self):
        # Arrange: p1=解析失敗
        route = [
            (None, "", None),
            (_valid_receipt_raw(), _VALID_OCR_TEXT, 0.95),
        ]

        # Act
        pages, _ = _run_receipt_pipeline(route)

        # Assert: 占位行から原票ページへ辿れるよう page_bytes を保持する
        # （main.PageUrlResolver が単ページ PDF リンク生成に使う）
        by_page = {p["page_num"]: p for p in pages}
        self.assertEqual(by_page[1].get("page_bytes"), b"%PDF-p1")

    def test_vision_fallback_attempted_before_declaring_failure(self):
        # Arrange: p1 の一次経路が None → Vision 兜底が呼ばれるはず（回帰保護）
        route = [
            (None, "", None),
            (_valid_receipt_raw(), _VALID_OCR_TEXT, 0.95),
        ]

        # Act
        _, vision = _run_receipt_pipeline(route)

        # Assert: 失敗宣言の前に Vision 兜底を必ず試している
        vision.assert_called_once_with(b"%PDF-p1", "application/pdf", mock.ANY)


class ReceiptEnvelopePageStillSkippedTest(unittest.TestCase):
    """Session 16 裁決: 封筒フィルタ (_is_envelope_page) は
    doc_type == DocType.RECEIPT 限定に変更されたが、RECEIPT 自体の既存挙動
    （封筒/送付状ページは skip=yield されない）は維持されなければならない。

    このテストは将来 RECEIPT 限定の条件を誤って剥がしてしまう回帰を防ぐ
    ガードであり、封筒フィルタの適用範囲を狭めた際に領収書側の挙動まで
    一緒に変えてしまわないようにする。
    """

    def test_envelope_page_is_not_yielded_for_receipt(self):
        # Arrange: p1=封筒（金額関連キーワード無し）/ p2=正常な領収書
        route = [
            (_valid_receipt_raw(), _ENVELOPE_OCR_TEXT, 0.9),
            (_valid_receipt_raw(), _VALID_OCR_TEXT, 0.95),
        ]

        # Act
        pages, _ = _run_receipt_pipeline(route)

        # Assert: p1 は yield されず（RECEIPT では従来通り skip）、p2 のみ残る
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["page_num"], 2)


if __name__ == "__main__":
    unittest.main()
