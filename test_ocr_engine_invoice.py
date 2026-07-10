"""ocr_engine の非領収書（請求書系）多ページ PDF 分岐の単体テスト。

Session 16 更新: 旧「非領収書 PDF」分岐（全ページの OCR テキストを結合し
1 回だけ Gemini を呼ぶ）は、大型 PDF で出力が MAX_TOKENS に達し JSON が
途中切断される生産事故を起こしたため全廃した。現在は領収書分岐と同じ
「1 ページ = 1 Gemini 呼び出し」に統一されている（詳細は
test_ocr_engine_invoice_pipeline.py）。本ファイルの `_run_pipeline` は
その逐頁分岐を `_route_ocr_strategy` レベルでモックして通す形に改めた。

本ファイルが固定する不変条件（逐頁化後も維持）:

  Gemini が正常な JSON を返したが明細 0 件 → entries=[] のまま yield
    → main.process_file は count>0 / error_pages=0 と数える
    → sheets_output.append_entries は entries=[] かつ _unrecognized 無しで
       1 行も書かずに静かに return
    → count>0 なので「Success」通知 + 原票アーカイブ

という無音のデータ欠落が起きないこと。

Session 16 重複覆蓋收斂: test_ocr_engine_invoice_pipeline.py 新設に伴い、
以下は重複のため本ファイルから削除し、断言の欠落なく新檔へ引き継いだ
（詳細は各削除箇所を参照）:
  - InvoiceHappyPathTest（多頁独立 yield 場面 → 新檔
    EachPageYieldedIndependentlyTest.test_two_pages_each_yield_own_result）
  - InvoiceGeminiFailureTest.test_no_raw_data_yields_page_error_for_every_page
    （vision.call_count==2 断言は新檔
    PartialPageFailureTest.test_all_pages_fail_yields_all_page_errors へ移設）
  - InvoiceEmptyItemsTest.test_empty_items_marks_unrecognized（新檔
    EmptyEntriesMarkedUnrecognizedWithoutPageErrorTest で完全カバー済み）
本ファイルは単頁 PDF/画像経路・実物 writer 経路・横断不変式など
新檔がカバーしない固有シナリオのみを残す。

ocr_engine は paddleocr / google.generativeai / pdf2image 等の重依存を
import するため venv311 で実行する:
    venv311/bin/python -m unittest test_ocr_engine_invoice -v
    venv311/bin/python -m pytest test_ocr_engine_invoice.py -v
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
from sheets_output import SheetsOutputWriter, TAG_COL_INDEX
from tag_rules import UNRECOGNIZED_TAG
# Session 16 重複覆蓋收斂: _empty_invoice_raw は本ファイルと
# test_ocr_engine_invoice_pipeline.py の両方で個別維持されていたため漂移
# （date が "2026/07/09" と "2026-07-09" に分岐）した。単一定義に収斂し、
# 新檔から import する。
from test_ocr_engine_invoice_pipeline import _empty_invoice_raw


# 封筒/送付状/裏面メモ判定 (_is_envelope_page) に落ちないよう「請求書」構造
# キーワードを含める（短文 + 構造キーワード無しは裏面メモ扱いで無条件 skip される）
_VALID_OCR_TEXT = "請求書 テスト商事 合計10,000円 振込"


def _two_pdf_pages():
    """多ページ PDF を模した _split_pdf_pages の戻り値。

    単ページ PDF だと _split_pdf_pages が何も yield せず、本分岐ではなく
    「単ページ / 画像」の従来経路へ落ちるため必ず 2 ページ以上にする。
    """
    return iter([
        {"page_num": 1, "total_pages": 2, "data": b"%PDF-1", "filename": "a_p1.pdf"},
        {"page_num": 2, "total_pages": 2, "data": b"%PDF-2", "filename": "a_p2.pdf"},
    ])


def _run_pipeline(gemini_raw, doc_type=DocType.PURCHASE_INVOICE):
    """非領収書 PDF 分岐（逐頁化後）を通し、yield された結果を全件返す。

    Session 16 逐頁化により本分岐は領収書分岐と同じくページ単位で
    `_route_ocr_strategy` を呼ぶため、直接それをモックする。
    gemini_raw を両ページに同一適用し（None なら両ページとも一次経路失敗、
    Vision 兜底 `_call_gemini_bytes` も失敗扱い）、旧テストの「1回の
    Gemini 応答」という前提を「各ページが同じ応答を受け取った」場合に
    読み替える。
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(b"%PDF-1.4 dummy")
        path = tmp.name

    route = [(gemini_raw, _VALID_OCR_TEXT, 0.95), (gemini_raw, _VALID_OCR_TEXT, 0.95)]

    try:
        with mock.patch.object(ocr_engine, "_split_pdf_pages",
                               return_value=_two_pdf_pages()), \
             mock.patch.object(ocr_engine, "_route_ocr_strategy",
                               side_effect=route), \
             mock.patch.object(ocr_engine, "_call_gemini_bytes",
                               return_value=None) as vision:
            with redirect_stdout(io.StringIO()):
                pages = list(ocr_engine.process_pipeline(
                    path, doc_type=doc_type, ocr_strategy="C"))
        return pages, vision
    finally:
        os.unlink(path)


class InvoiceEmptyItemsTest(unittest.TestCase):
    """Gemini が JSON を返したが明細 0 件のケース。

    Session 16 重複覆蓋收斂: test_empty_items_marks_unrecognized は
    test_ocr_engine_invoice_pipeline.py の
    EmptyEntriesMarkedUnrecognizedWithoutPageErrorTest で完全カバーされる
    ため削除した。全零金額・vendor traceability は本ファイル固有のため残す。
    """

    def test_all_zero_amount_items_marks_unrecognized(self):
        # Arrange: items はあるが全て amount=0 → builder が全件 continue し entries=[]
        raw = {
            "date": "2026-07-09",
            "vendor": "テスト商事",
            "items": [{"description": "値引", "amount": 0, "tax_rate": 0.10}],
        }

        # Act
        pages, _ = _run_pipeline(raw)

        # Assert
        self.assertEqual(len(pages), 2)
        for page in pages:
            self.assertTrue(page["result"].get("_unrecognized"))

    def test_unrecognized_result_carries_vendor_for_traceability(self):
        # Arrange: 明細は取れなかったが vendor は読めている
        raw = _empty_invoice_raw()

        # Act
        pages, _ = _run_pipeline(raw)

        # Assert: 占位行から原票を辿れるよう vendor / date は保持する
        self.assertEqual(pages[0]["result"]["vendor"], "テスト商事")
        self.assertEqual(pages[0]["result"]["date"], "2026/07/09")


# Session 16 重複覆蓋收斂: InvoiceHappyPathTest（多頁独立 yield 場面）は
# test_ocr_engine_invoice_pipeline.py の EachPageYieldedIndependentlyTest.
# test_two_pages_each_yield_own_result で完全カバーされるため削除した。


def _run_single_page_pipeline(gemini_raw, suffix=".jpg",
                              doc_type=DocType.PURCHASE_INVOICE):
    """単ページ PDF / 画像の「通常パス」を通し、yield された結果を全件返す。

    多ページ PDF 分岐と構造が別なので、同じ穴（明細ゼロで無標記 yield）が
    こちらにも開いていないことを独立に固定する。
    画像は mime が image/* なので PDF 分岐を素通りする。単ページ PDF は
    _split_pdf_pages が何も yield しないことで同じ通常パスへ落ちる。
    """
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(b"dummy")
        path = tmp.name

    try:
        with mock.patch.object(ocr_engine, "_split_pdf_pages",
                               return_value=iter([])), \
             mock.patch.object(ocr_engine, "_route_ocr_strategy",
                               return_value=(gemini_raw, "", 0.9)), \
             mock.patch.object(ocr_engine, "_call_gemini", return_value=None):
            with redirect_stdout(io.StringIO()):
                return list(ocr_engine.process_pipeline(
                    path, doc_type=doc_type, ocr_strategy="C"))
    finally:
        os.unlink(path)


class SinglePageAndImageInvoiceTest(unittest.TestCase):
    """通常パス（画像 / 単ページ PDF）にも同じ無標記 yield の穴が無いこと。

    請求書を撮影した画像は実運用で頻出する。多ページ PDF 分岐だけ直しても
    こちらが空 entries を無標記で yield すると同じ無音のデータ欠落になる。
    """

    def test_image_with_empty_items_marks_unrecognized(self):
        # Arrange
        raw = _empty_invoice_raw()

        # Act
        pages = _run_single_page_pipeline(raw, suffix=".jpg")

        # Assert
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0]["result"].get("_unrecognized"))
        self.assertEqual(pages[0]["result"]["entries"], [])

    def test_single_page_pdf_with_empty_items_marks_unrecognized(self):
        # Arrange
        raw = _empty_invoice_raw()

        # Act
        pages = _run_single_page_pipeline(raw, suffix=".pdf")

        # Assert
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0]["result"].get("_unrecognized"))

    def test_image_with_valid_items_has_no_unrecognized(self):
        # Arrange: 回帰保護 — 明細が取れた通常ケースは従来通り
        raw = {
            "date": "2026-07-09",
            "vendor": "テスト商事",
            "payment_method": "振込",
            "items": [{"description": "備品", "amount": 1000, "tax_rate": 0.10}],
        }

        # Act
        pages = _run_single_page_pipeline(raw, suffix=".jpg")

        # Assert
        self.assertEqual(len(pages), 1)
        self.assertFalse(pages[0]["result"].get("_unrecognized"))
        self.assertEqual(len(pages[0]["result"]["entries"]), 1)

    def test_salary_slip_empty_keeps_employee_name_as_vendor(self):
        # Arrange: 給与明細は vendor を employee_name から取る。
        # _build_doc_result 抽出後もこの分岐が生きていることを固定する。
        raw = {"date": "2026-07-25", "employee_name": "山田太郎"}

        # Act
        pages = _run_single_page_pipeline(
            raw, suffix=".jpg", doc_type=DocType.SALARY_SLIP)

        # Assert
        self.assertEqual(len(pages), 1)
        result = pages[0]["result"]
        self.assertTrue(result.get("_unrecognized"))
        self.assertEqual(result["vendor"], "山田太郎")


class _FakeWorksheet:
    """_get_next_txn_no が参照する get_all_values のみを持つ最小スタブ。"""

    def get_all_values(self):
        return []


def _append_via_real_writer(result):
    """process_pipeline の result を実物の append_entries に流し、書かれた行を返す。

    ここが本バグの着弾点。sheets_output はモックせず実路径を通し、
    _unrecognized の有無で行が書かれるか否かが変わることを実証する。
    """
    written = []

    writer = SheetsOutputWriter.__new__(SheetsOutputWriter)
    writer._tab_next_txn = {}
    writer._tabs_sanitized = set()

    with mock.patch.object(SheetsOutputWriter, "_get_or_create_tab",
                           return_value=_FakeWorksheet()), \
         mock.patch.object(SheetsOutputWriter, "_ensure_row_capacity"), \
         mock.patch.object(SheetsOutputWriter, "_write_with_retry",
                           side_effect=lambda ws, rows: written.extend(rows)):
        with redirect_stdout(io.StringIO()):
            writer.append_entries("従業員", DocType.PURCHASE_INVOICE, result,
                                  source_url="http://origin/x.pdf")
    return written


class UnrecognizedReachesSheetRowTest(unittest.TestCase):
    """明細ゼロでも Sheets に 1 行残ること（無音のデータ欠落の再発防止）。"""

    def test_pipeline_result_writes_red_tagged_placeholder_row(self):
        # Arrange: pipeline を実際に通して result を得る（手組みしない）
        raw = _empty_invoice_raw()
        pages, _ = _run_pipeline(raw)

        # Act: その result を実物の append_entries へ
        written = _append_via_real_writer(pages[0]["result"])

        # Assert: 占位行が 1 行、赤タグ付きで原票 URL まで辿れる
        self.assertEqual(len(written), 1)
        row = written[0]
        self.assertEqual(row[TAG_COL_INDEX], UNRECOGNIZED_TAG)
        self.assertEqual(row[5], "テスト商事")
        self.assertEqual(row[27], "http://origin/x.pdf")

    def test_without_unrecognized_flag_placeholder_is_written(self):
        """防御ガード導入済み: 無標記の空 entries でも占位行が書かれる。

        旧 characterization テスト（無標記なら 1 行も書かれない = データ
        欠落の着弾点）の反転版。append_entries 側が最終防衛として占位行を
        書くため、producer がフラグを立て忘れても無音欠落しない。
        producer 側の保証（YieldInvariantTest）と二重防御になる。
        """
        # Arrange: フラグ立て忘れの result 形（_unrecognized 無し・明細ゼロ）
        buggy = {"doc_type": DocType.PURCHASE_INVOICE, "date": "2026-07-09",
                 "vendor": "テスト商事", "invoice_num": "", "memo": "",
                 "entries": []}

        # Act
        written = _append_via_real_writer(buggy)

        # Assert: 赤タグ付き占位行が 1 行書かれ、人手確認へ可視化される
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0][TAG_COL_INDEX], UNRECOGNIZED_TAG)
        self.assertEqual(written[0][5], "テスト商事")


# Session 16 重複覆蓋收斂: InvoiceGeminiFailureTest.
# test_no_raw_data_yields_page_error_for_every_page は削除した。
# vision.call_count==2 の断言は test_ocr_engine_invoice_pipeline.py の
# PartialPageFailureTest.test_all_pages_fail_yields_all_page_errors へ
# 移設済み（断言を失わずに引き継ぐ）。


class YieldInvariantTest(unittest.TestCase):
    """不変条件: entries が空の yield は必ず標記を持つ。

    形状ごとの個別テストは「誰も列挙し忘れなかった形状」しか守れない。
    実際、最初の修正は多ページ PDF 分岐だけを直し、画像 / 単ページ PDF の
    通常パスに同じ穴を残していた。ここは形状 × 文書タイプを横断して
    「空 entries に _unrecognized も _page_error も無い yield は存在しない」
    ことを直接主張する。新しい経路を足した人が標記を忘れたら落ちる。
    """

    SHAPES = ("multipage_pdf", "image", "single_page_pdf")
    DOC_TYPES = (DocType.PURCHASE_INVOICE, DocType.SALES_INVOICE,
                 DocType.SALARY_SLIP)

    @staticmethod
    def _yield_pages(shape, doc_type, raw):
        if shape == "multipage_pdf":
            pages, _ = _run_pipeline(raw, doc_type=doc_type)
            return pages
        suffix = ".jpg" if shape == "image" else ".pdf"
        return _run_single_page_pipeline(raw, suffix=suffix, doc_type=doc_type)

    def test_empty_entries_never_yielded_without_a_marker(self):
        for shape in self.SHAPES:
            for doc_type in self.DOC_TYPES:
                with self.subTest(shape=shape, doc_type=doc_type):
                    pages = self._yield_pages(
                        shape, doc_type, _empty_invoice_raw())
                    for page in pages:
                        result = page["result"]
                        if result.get("entries"):
                            continue
                        marked = (result.get("_unrecognized")
                                  or result.get("_page_error"))
                        self.assertTrue(
                            marked,
                            f"{shape}/{doc_type}: entries=[] なのに標記が無い。"
                            "sheets_output が 1 行も書かず Success 判定 →"
                            "原票が明細ゼロでアーカイブされる（無音のデータ欠落）",
                        )


if __name__ == "__main__":
    unittest.main()
