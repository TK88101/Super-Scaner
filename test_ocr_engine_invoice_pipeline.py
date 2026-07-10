"""非領収書（請求書系・給与明細）多ページ PDF 分岐の逐頁化テスト。

Session 16 決定事項: 旧「非領収書 PDF」分岐（全ページ OCR テキストを結合し
1回だけ Gemini を呼ぶ）は、大型 PDF で Gemini 応答が MAX_TOKENS で
途中切断され JSON 解析に失敗する生産事故を起こした。原因は「1冊まるごと
1レスポンス」に出力トークンを詰め込む設計そのものであり、対症療法では
再発を防げない。ユーザー決定により当該分岐は全廃し、全文書タイプを
領収書分岐と同じ「1ページ = 1 Gemini 呼び出し」に統一する。

本テストは統一後の逐頁分岐が、領収書分岐が既に備えている性質を
非領収書 doc_type（PURCHASE_INVOICE / SALARY_SLIP）でも満たすことを固定する:
  1. 各ページが独立 yield される（page_num/total_pages/page_bytes 正しい）
  2. 部分頁失敗 → 失敗頁のみ _page_error 付き占位、成功頁は正常
  3. 全頁失敗 → 全頁 _page_error（main が count==error_pages→Failed→ファイル保持）
  4. Gemini 正常応答だが builder が空 entries → _unrecognized のみ（_page_error 無し）
     （_build_doc_result 既存の「無限再試行を避け占位行でアーカイブ」語義を維持）
  5. 封筒フィルタ (_is_envelope_page) は非領収書 doc_type には適用しない
     （Session 16 追加裁決。誤爆による無音 skip → 無限リトライ事故防止のため
     RECEIPT 限定に変更。非領収書の封筒/送付状ページは _unrecognized 占位
     行で可視化されるだけで yield 自体は必ず行われる）
  6. SALARY_SLIP は vendor を employee_name から取る（_build_doc_result 既存ロジック）
  7. Vision 兜底 (_call_gemini_bytes) は失敗宣言前に必ず試される（回帰保護）

このテストは実装前（RED）では失敗する: 現行コードの非領収書 PDF 分岐は
全ページの OCR テキストを結合して 1 回だけ Gemini を呼ぶため、
_route_ocr_strategy はページ単位で呼ばれず、yield も page_num=1/total_pages=1
の単一結果にしかならない。

ocr_engine は paddleocr / google.generativeai 等の重依存を import するため
venv311 で実行する:
    venv311/bin/python -m unittest test_ocr_engine_invoice_pipeline -v
    venv311/bin/python -m pytest test_ocr_engine_invoice_pipeline.py -v
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


def _valid_invoice_raw(vendor="テスト商事", date="2026-07-09", amount=1000):
    """1明細の正常な請求書 JSON（毎回新規生成し相互汚染を防ぐ）。"""
    return {
        "date": date,
        "vendor": vendor,
        "payment_method": "振込",
        "items": [{"description": "備品", "amount": amount, "tax_rate": 0.10}],
    }


def _empty_invoice_raw():
    """明細ゼロの Gemini 応答（正常な JSON だが items が空）。

    日付は _validate_gemini_date が要求する YYYY/M/D 形式にする
    （ハイフン区切りだと _apply_ocr_overrides の検証で無効判定され ""
      に落とされる）。test_ocr_engine_invoice.py の同名 fixture と同一形式。
    """
    return {"date": "2026/07/09", "vendor": "テスト商事", "items": []}


def _valid_salary_raw(employee="山田太郎"):
    return {
        "employee_name": employee,
        "gross_salary": 300000,
        "net_salary": 250000,
    }


# 封筒/送付状判定 (_is_envelope_page) に落ちないよう構造キーワードを含める
_VALID_OCR_TEXT = "請求書 テスト商事 合計1,100円 振込"
# 封筒キーワードのみ・金額関連キーワード無し → _is_envelope_page が True を返す
_ENVELOPE_OCR_TEXT = "〒100-0001 東京都千代田区 御中"


def _two_pdf_pages():
    return iter([
        {"page_num": 1, "total_pages": 2, "data": b"%PDF-p1", "filename": "i_p1.pdf"},
        {"page_num": 2, "total_pages": 2, "data": b"%PDF-p2", "filename": "i_p2.pdf"},
    ])


def _run_invoice_pipeline(route_side_effect, doc_type=DocType.PURCHASE_INVOICE):
    """非領収書 doc_type の多ページ PDF 分岐を通し、yield された結果を全件返す。

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
                    path, doc_type=doc_type, ocr_strategy="C"))
        return pages, vision
    finally:
        os.unlink(path)


class EachPageYieldedIndependentlyTest(unittest.TestCase):
    """多ページ PDF は各ページが独立して yield されること（合併廃止の核心）。"""

    def test_two_pages_each_yield_own_result(self):
        # Arrange: p1, p2 でそれぞれ異なる取引先/金額
        route = [
            (_valid_invoice_raw(vendor="A商事", amount=1000), _VALID_OCR_TEXT, 0.95),
            (_valid_invoice_raw(vendor="B商事", amount=2000), _VALID_OCR_TEXT, 0.90),
        ]

        # Act
        pages, _ = _run_invoice_pipeline(route)

        # Assert: 2頁とも yield、page_num/total_pages/page_bytes が正しい
        self.assertEqual(len(pages), 2)
        by_page = {p["page_num"]: p for p in pages}
        self.assertEqual(by_page[1]["total_pages"], 2)
        self.assertEqual(by_page[1]["page_bytes"], b"%PDF-p1")
        self.assertEqual(by_page[2]["total_pages"], 2)
        self.assertEqual(by_page[2]["page_bytes"], b"%PDF-p2")

        # entries は各ページの builder 結果に一致（合併されていない証拠）
        expected1 = ocr_engine._build_entries_from_purchase_invoice(
            _valid_invoice_raw(vendor="A商事", amount=1000))
        expected2 = ocr_engine._build_entries_from_purchase_invoice(
            _valid_invoice_raw(vendor="B商事", amount=2000))
        self.assertEqual(by_page[1]["result"]["entries"], expected1)
        self.assertEqual(by_page[2]["result"]["entries"], expected2)
        self.assertEqual(by_page[1]["result"]["vendor"], "A商事")
        self.assertEqual(by_page[2]["result"]["vendor"], "B商事")


class PartialPageFailureTest(unittest.TestCase):
    """部分頁失敗は失敗頁のみ _page_error 付き占位、成功頁は正常。"""

    def test_partial_failure_yields_page_error_for_failed_page(self):
        # Arrange: p1=解析失敗(raw=None, Vision 兜底も None) / p2=正常
        route = [
            (None, "読めないテキスト", None),
            (_valid_invoice_raw(), _VALID_OCR_TEXT, 0.95),
        ]

        # Act
        pages, _ = _run_invoice_pipeline(route)

        # Assert
        self.assertEqual(len(pages), 2)
        by_page = {p["page_num"]: p for p in pages}
        self.assertTrue(by_page[1]["result"].get("_page_error"))
        self.assertTrue(by_page[1]["result"].get("_unrecognized"))
        self.assertEqual(by_page[1]["result"]["entries"], [])
        self.assertEqual(by_page[1].get("page_bytes"), b"%PDF-p1")
        self.assertFalse(by_page[2]["result"].get("_page_error"))
        self.assertEqual(len(by_page[2]["result"]["entries"]), 1)

    def test_all_pages_fail_yields_all_page_errors(self):
        # Arrange: 全ページ解析失敗
        route = [
            (None, "", None),
            (None, "", None),
        ]

        # Act
        pages, vision = _run_invoice_pipeline(route)

        # Assert: 全ページ _page_error 付き yield
        # → main は count==error_pages → Failed → ファイル保持
        self.assertEqual(len(pages), 2)
        for p in pages:
            self.assertTrue(p["result"].get("_page_error"),
                            f"page {p['page_num']} に _page_error が無い")
        # 失敗宣言前に各ページで Vision 兜底が試されている（回帰保護。
        # Session 16 重複覆蓋收斂で test_ocr_engine_invoice.py の
        # InvoiceGeminiFailureTest.test_no_raw_data_yields_page_error_for_every_page
        # から移設した断言）
        self.assertEqual(vision.call_count, 2)

    def test_vision_fallback_attempted_before_declaring_failure(self):
        # Arrange: p1 の一次経路が None → Vision 兜底が呼ばれるはず（回帰保護）
        route = [
            (None, "", None),
            (_valid_invoice_raw(), _VALID_OCR_TEXT, 0.95),
        ]

        # Act
        _, vision = _run_invoice_pipeline(route)

        # Assert: 失敗宣言の前に Vision 兜底を必ず試している
        vision.assert_called_once_with(b"%PDF-p1", "application/pdf", mock.ANY)


class EmptyEntriesMarkedUnrecognizedWithoutPageErrorTest(unittest.TestCase):
    """Gemini は正常応答だが builder が空 entries → _unrecognized のみ。

    _page_error を立てると count==error_pages で Failed 判定 → ファイル保持 →
    明細を持たない書類（送付状・パンフレット等）が毎回再試行される無限
    ループに入るため、_build_doc_result 既存の「占位行1件でアーカイブ」
    語義を逐頁化後も維持することを固定する。
    """

    def test_empty_items_page_marks_unrecognized_not_page_error(self):
        # Arrange: p1=明細ゼロの正常JSON応答 / p2=正常
        # OCR テキストは「請求書」構造キーワードを含め、_is_envelope_page の
        # 裏面メモ判定（短文かつ構造キーワード無し）で誤って skip されないようにする。
        route = [
            (_empty_invoice_raw(), "請求書 テスト商事 明細なし", 0.9),
            (_valid_invoice_raw(), _VALID_OCR_TEXT, 0.95),
        ]

        # Act
        pages, _ = _run_invoice_pipeline(route)

        # Assert
        self.assertEqual(len(pages), 2)
        by_page = {p["page_num"]: p for p in pages}
        self.assertTrue(by_page[1]["result"].get("_unrecognized"))
        self.assertFalse(by_page[1]["result"].get("_page_error"))
        self.assertEqual(by_page[1]["result"]["entries"], [])
        self.assertEqual(by_page[1]["result"]["vendor"], "テスト商事")


class EnvelopePageNotFilteredForNonReceiptTest(unittest.TestCase):
    """Session 16 決定: 封筒フィルタ (_is_envelope_page) は非領収書 doc_type
    には適用しない。

    _is_envelope_page の構造キーワード判定（「領収」「請求書」「合計」等）は
    領収書向けに調整されたヒューリスティックであり、給与明細等の非領収書の
    短文ページに誤爆すると skip（yield されない）→ count==0 → Failed →
    無限リトライ、という生産事故を起こす。よって封筒フィルタは
    doc_type == DocType.RECEIPT の場合のみ効かせる（受け側の裁決）。

    非領収書の実際の封筒/送付状ページ（Gemini が items=[] を返す）は、
    従来通り _build_doc_result の _unrecognized 占位行で可視化・アーカイブ
    される（skip して無音欠落させるのではなく、見える形で兜止めする）。
    """

    def test_envelope_like_page_is_yielded_not_skipped(self):
        # Arrange: p1=封筒キーワードのみ・金額関連キーワード無し・Gemini応答は
        # items=[]（実際の封筒/送付状ページを模す）/ p2=正常な請求書ページ
        route = [
            (_empty_invoice_raw(), _ENVELOPE_OCR_TEXT, 0.9),
            (_valid_invoice_raw(), _VALID_OCR_TEXT, 0.95),
        ]

        # Act
        pages, _ = _run_invoice_pipeline(route)

        # Assert: 封筒フィルタで skip されず、両ページとも yield される
        self.assertEqual(len(pages), 2)
        by_page = {p["page_num"]: p for p in pages}

        # p1: entries 空 → _unrecognized のみ（_page_error は付かない。
        # Gemini 応答自体は正常な JSON だったため）
        self.assertFalse(by_page[1]["result"].get("_page_error"))
        self.assertTrue(by_page[1]["result"].get("_unrecognized"))
        self.assertEqual(by_page[1]["result"]["entries"], [])

        # p2: 正常な明細行が生成される
        self.assertFalse(by_page[2]["result"].get("_unrecognized"))
        self.assertEqual(len(by_page[2]["result"]["entries"]), 1)


class SalarySlipVendorFromEmployeeNameTest(unittest.TestCase):
    """SALARY_SLIP は vendor を employee_name から取る（逐頁化後も維持）。"""

    def test_salary_slip_vendor_is_employee_name(self):
        # Arrange
        route = [
            (_valid_salary_raw(employee="山田太郎"), _VALID_OCR_TEXT, 0.9),
            (_valid_salary_raw(employee="鈴木花子"), _VALID_OCR_TEXT, 0.9),
        ]

        # Act
        pages, _ = _run_invoice_pipeline(route, doc_type=DocType.SALARY_SLIP)

        # Assert
        self.assertEqual(len(pages), 2)
        by_page = {p["page_num"]: p for p in pages}
        self.assertEqual(by_page[1]["result"]["vendor"], "山田太郎")
        self.assertEqual(by_page[2]["result"]["vendor"], "鈴木花子")
        self.assertFalse(by_page[1]["result"].get("_unrecognized"))


class InvoicePaymentDueDateNotOverridingIssueDateTest(unittest.TestCase):
    """Session 16 回帰: 請求書の「支払期限」日付が発行日を誤って上書きしない。

    統一逐頁化により多頁請求書の各ページが本経路（_apply_ocr_overrides）を
    新規に通るようになった。多欄レイアウトの OCR 読み順は不定で、
    「支払期限」が「発行日」より先に読まれるケースがある。
    _extract_date_from_ocr の _skip_keywords に支払期日系の語彙が無いと、
    支払期限の日付を正規表現が最初に採用し、Gemini が正しく抽出した発行日を
    上書きしてしまう（仕訳日付が支払期日にすり替わる）。
    """

    def test_payment_due_date_appearing_before_issue_date_does_not_override(self):
        # Arrange: p1 は「支払期限」が「発行日」より先に出現する OCR テキスト
        # （多欄請求書で読み順が入れ替わるケースを模す）。
        # Gemini は正しい発行日 2026/03/01 を返す。
        ocr_text_p1 = "支払期限 2026年3月31日 請求書 発行日 2026年3月1日 合計1,100円"
        route = [
            (_valid_invoice_raw(date="2026/03/01"), ocr_text_p1, 0.9),
            (_valid_invoice_raw(), _VALID_OCR_TEXT, 0.95),
        ]

        # Act
        pages, _ = _run_invoice_pipeline(route)

        # Assert: 支払期限の 2026/03/31 に上書きされず、発行日 2026/03/01 のまま
        by_page = {p["page_num"]: p for p in pages}
        self.assertEqual(by_page[1]["result"]["date"], "2026/03/01")


class ExtractDateFromOcrSkipsPaymentDueDateTest(unittest.TestCase):
    """_extract_date_from_ocr 単体: 支払期日・振込期限系の語彙を skip すること。

    ここでの単体断言はパイプライン断言より一段細かく、境界（前後 50 文字の
    コンテキスト窓）内に支払期日系語彙があるケースを直接固定する。
    """

    def test_payment_due_date_before_issue_date_is_skipped(self):
        text = "支払期限 2026年3月31日 請求書 発行日 2026年3月1日 合計1,100円"
        self.assertEqual(ocr_engine._extract_date_from_ocr(text), "2026/03/01")

    def test_furigana_payment_due_date_is_skipped(self):
        text = "お支払期限 2026年3月31日 発行日 2026年3月1日"
        self.assertEqual(ocr_engine._extract_date_from_ocr(text), "2026/03/01")

    def test_bank_transfer_due_date_is_skipped(self):
        text = "振込期限 2026年3月31日 発行日 2026年3月1日"
        self.assertEqual(ocr_engine._extract_date_from_ocr(text), "2026/03/01")

    def test_salary_slip_payment_date_is_not_skipped(self):
        # 給与明細の「支払日」「支給日」は支払期日系語彙の部分文字列に
        # ならないため誤爆しないことを確認する（防回帰）。
        text = "支払日 2026年3月25日 給与明細"
        self.assertEqual(ocr_engine._extract_date_from_ocr(text), "2026/03/25")
        text2 = "支給日 2026年3月25日 給与明細"
        self.assertEqual(ocr_engine._extract_date_from_ocr(text2), "2026/03/25")

    def test_issue_date_before_payment_due_date_is_not_skipped(self):
        # codex review 発見: 発行日が支払期日より先に書かれる一般的な請求書
        # レイアウトでは、対称窓（前後 50 文字）だと発行日側の後方窓に
        # 「支払期限」が入ってしまい発行日まで誤スキップ→フォールバックで
        # 支払期限日付を誤採用していた。支払期日系語彙は「日付の前」窓のみで
        # 判定すべきで、発行日側は影響を受けてはならない。
        text = "発行日 2026年3月1日 支払期限 2026年3月31日"
        self.assertEqual(ocr_engine._extract_date_from_ocr(text), "2026/03/01")

    def test_issue_date_before_furigana_payment_due_date_is_not_skipped(self):
        text = "請求日：2026年3月1日　お支払期日：2026年3月31日"
        self.assertEqual(ocr_engine._extract_date_from_ocr(text), "2026/03/01")

    def test_only_payment_due_date_present_returns_none(self):
        # codex Round 3 発見: OCR テキストに支払期限系の日付しか無い場合、
        # ループ内では _due_date_prefix_keywords 判定で skip されるが、
        # 既存の fallback（matches_p1[-1] をそのまま返す）はフィルタ前の
        # 生マッチ一覧を見ているため、skip されたはずの支払期限日付を
        # そのまま返してしまっていた（修正前はここが FAIL し '2026/03/31' を返す）。
        # 支払期限しか無い＝取引日が OCR から取れないケースなので、
        # ここでは None を返し Gemini の日付をそのまま生かすべき。
        text = "支払期限 2026年3月31日"
        self.assertIsNone(ocr_engine._extract_date_from_ocr(text))

    def test_only_furigana_payment_due_date_present_returns_none(self):
        text = "お支払期限 2026年3月31日"
        self.assertIsNone(ocr_engine._extract_date_from_ocr(text))

    def test_only_bank_transfer_due_date_present_returns_none(self):
        text = "振込期限 2026年3月31日"
        self.assertIsNone(ocr_engine._extract_date_from_ocr(text))

    def test_only_symmetric_skip_keyword_date_present_keeps_existing_fallback(self):
        # 防回帰: 対称窓 _skip_keywords（例: 「有効期限」）で skip された
        # 日付しか OCR テキストに無い場合の fallback 挙動は、今回の修正で
        # 変えてはいけない（今回の裁決は due_date_prefix 系のみを fallback
        # 候補から除外する話であり、_skip_keywords 系の既存 fallback 語義は
        # receipt 生産調校済みのため一字も変えない）。
        # 改修前の実測値: '2026/03/31'（フォールバックとして採用される）。
        text = "有効期限 2026年3月31日"
        self.assertEqual(ocr_engine._extract_date_from_ocr(text), "2026/03/31")


class ExtractDateFromOcrSkipsPaymentDueDatePipelineTest(unittest.TestCase):
    """codex Round 3 発見のパイプライン結線テスト。

    OCR テキストに支払期限しか無いページで、_extract_date_from_ocr が
    誤って支払期限を返すと _apply_ocr_overrides（非 documents フォーマット
    無条件上書き）が Gemini の正しい発行日をすり替えてしまう。
    """

    def test_only_payment_due_date_ocr_text_does_not_override_gemini_issue_date(self):
        # Arrange: OCR テキストは支払期限のみ、Gemini は正しい発行日を返す
        # （_two_pdf_pages が常に2頁分返すため route も2件用意する）
        ocr_text_p1 = "支払期限 2026年3月31日"
        route = [
            (_valid_invoice_raw(date="2026/03/01"), ocr_text_p1, 0.9),
            (_valid_invoice_raw(), _VALID_OCR_TEXT, 0.95),
        ]

        # Act
        pages, _ = _run_invoice_pipeline(route)

        # Assert: 支払期限 2026/03/31 に上書きされず、発行日 2026/03/01 のまま
        by_page = {p["page_num"]: p for p in pages}
        self.assertEqual(by_page[1]["result"]["date"], "2026/03/01")


if __name__ == "__main__":
    unittest.main()
