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
from ocr_test_helpers import page_ocrs_from_tuples


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
                               side_effect=page_ocrs_from_tuples(
                                   route_side_effect, DocType.RECEIPT)), \
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

        # Assert: 失敗宣言の前に Vision 兜底を必ず試している。
        # `line_mode=False` は T5 で足した引数 —— 領収書経路が BULK 予算にも
        # 截断サルベージにも触れないことを、ここで併せて固定する。
        vision.assert_called_once_with(b"%PDF-p1", "application/pdf", mock.ANY,
                                       line_mode=False)


class PageFormattingExceptionIsolationTest(unittest.TestCase):
    """IP-401 T0: 整形段階 (_yield_page_results) の例外はそのページに閉じ込める。

    従来 `_yield_page_results` の消費は逐頁 `try` の**外**にあり、畸形 Gemini
    JSON 等で整形が例外を投げると最外層の `except` まで飛び、**PDF の残り
    全ページが無音で消えた**（元の封筒無音棄却より重大な欠落）。

    NOTE: `main` 分支の消費側 (`main.py:347`) は `_page_error` のみで分岐する。
    Plan の DoD(R4) が要求する `_error_class` は sandevistan 側の機構であり
    本分支には存在しないため、断言対象は `_page_error` + 例外型を含む memo。
    """

    def _run_with_failing_formatter(self, failing_page_num):
        """指定ページの整形だけが例外を投げるよう _yield_page_results を差替える。"""
        real = ocr_engine._yield_page_results
        calls = {"n": 0}

        def fake(doc_type, raw_data, ocr_text, ocr_conf, prefix="",
                 envelope_filter=False, page_class=None):
            calls["n"] += 1
            if calls["n"] == failing_page_num:
                # 下の for に yield があるため本関数は既に generator function。
                # よって raise は「呼び出し時」ではなく「消費時」に届く——
                # 実際の整形例外（next() の内側で発生）と同じタイミング。
                raise ValueError("畸形 JSON: items が dict ではない")
            for entry in real(doc_type, raw_data, ocr_text, ocr_conf,
                              prefix=prefix, envelope_filter=envelope_filter,
                              page_class=page_class):
                yield entry

        route = [
            (_valid_receipt_raw(), _VALID_OCR_TEXT, 0.95),
            (_valid_receipt_raw(), _VALID_OCR_TEXT, 0.95),
        ]
        with mock.patch.object(ocr_engine, "_yield_page_results", side_effect=fake):
            pages, _ = _run_receipt_pipeline(route)
        return pages

    def test_formatting_error_isolated_to_its_own_page(self):
        # Arrange / Act: p1 の整形だけが例外
        pages = self._run_with_failing_formatter(failing_page_num=1)

        # Assert: p1 は _page_error 占位行、p2 は巻き添えにならず正常 yield
        self.assertEqual(len(pages), 2)
        by_page = {p["page_num"]: p for p in pages}
        self.assertTrue(by_page[1]["result"].get("_page_error"))
        self.assertTrue(by_page[1]["result"].get("_unrecognized"))
        self.assertEqual(by_page[1]["result"]["entries"], [])
        self.assertIn("ValueError", by_page[1]["result"]["memo"])
        self.assertFalse(by_page[2]["result"].get("_page_error"))
        self.assertEqual(len(by_page[2]["result"]["entries"]), 1)

    def test_formatting_error_on_last_page_keeps_earlier_pages(self):
        # Arrange / Act: p2 の整形だけが例外（先行ページが既に yield 済みの状況）
        pages = self._run_with_failing_formatter(failing_page_num=2)

        # Assert: p1 の成果は保持され、p2 だけが占位行になる
        self.assertEqual(len(pages), 2)
        by_page = {p["page_num"]: p for p in pages}
        self.assertEqual(len(by_page[1]["result"]["entries"]), 1)
        self.assertTrue(by_page[2]["result"].get("_page_error"))

    def test_formatting_error_page_carries_page_bytes(self):
        # Arrange / Act
        pages = self._run_with_failing_formatter(failing_page_num=1)

        # Assert: 占位行から原票ページへ辿れる（他の失敗経路と同じ契約）
        by_page = {p["page_num"]: p for p in pages}
        self.assertEqual(by_page[1].get("page_bytes"), b"%PDF-p1")


def _empty_receipt_raw():
    """Gemini が有効な仕訳を組めなかったときの戻り値（documents 空）。"""
    return {"documents": []}


class ReceiptEnvelopePageStillSkippedTest(unittest.TestCase):
    """IP-401 T1/T4 で裁決変更: 封筒ページは「無音 skip」から
    「`_excluded_page` を立てて必ず yield」へ変わった。

    Session 16 の旧裁決は「封筒/送付状ページは yield されない」であり、
    このクラスはその挙動を固定していた。しかし本番事故（IP-401、社長夫人
    フィードバック③「54枚アップしたが仕訳は53件」）で、PaddleOCR が
    「領収証」を「领収证」と誤認識した小型サーマル領収証が
    `_is_envelope_page` に単独で棄却され、**Sheets にも占位行にも一切
    残らず無音で消えた**ことが判明した。

    無音 skip は顧客が枚数を数えるまで発見できない。よって
    「yield はする（`_excluded_page=True`）／MF 区には書かない
    （監査タブへ回す）」へ変更する。RECEIPT 限定であることは維持。
    """

    def test_envelope_page_is_yielded_with_excluded_flag(self):
        # Arrange: p1=封筒（entries を組めず、封筒キーワードのみ）/ p2=正常
        route = [
            (_empty_receipt_raw(), _ENVELOPE_OCR_TEXT, 0.9),
            (_valid_receipt_raw(), _VALID_OCR_TEXT, 0.95),
        ]

        # Act
        pages, _ = _run_receipt_pipeline(route)

        # Assert: p1 も yield される（無音欠落の廃止）。_excluded_page が立ち、
        # _page_error は立たない（失敗ではなく「正常な除外」のため——
        # 立てると main が Failed 判定 → ファイル保持 → 無限リトライになる）
        self.assertEqual(len(pages), 2)
        by_page = {p["page_num"]: p for p in pages}
        self.assertTrue(by_page[1]["result"].get("_excluded_page"))
        self.assertEqual(by_page[1]["result"].get("_exclude_reason"), "envelope")
        self.assertFalse(by_page[1]["result"].get("_page_error"))
        self.assertFalse(by_page[1]["result"].get("_unrecognized"))
        self.assertEqual(by_page[1]["result"]["entries"], [])

    def test_valid_entries_survive_envelope_keywords(self):
        """T1 中核 DoD: Gemini が有効 entries を返せば OCR が何であれ棄却されない。

        IP-401 の実事故（舞鶴パーク領収証）を最短で再現する形。
        """
        # Arrange: p1=封筒キーワードだらけの OCR だが Gemini は有効な仕訳を返した
        route = [
            (_valid_receipt_raw(), _ENVELOPE_OCR_TEXT, 0.9),
            (_valid_receipt_raw(), _VALID_OCR_TEXT, 0.95),
        ]

        # Act
        pages, _ = _run_receipt_pipeline(route)

        # Assert: 記帳は止まらない。ただし人手抽査のため監査シグナルは立つ
        self.assertEqual(len(pages), 2)
        by_page = {p["page_num"]: p for p in pages}
        self.assertEqual(len(by_page[1]["result"]["entries"]), 1)
        self.assertFalse(by_page[1]["result"].get("_excluded_page"))
        self.assertEqual(by_page[1]["result"].get("_audit_signal"),
                         "envelope_signal_with_entries")

    def test_non_envelope_empty_page_still_unrecognized(self):
        """entries 空 + 封筒判定不成立 → 従来通り赤い認識不能占位行。"""
        # Arrange: 領収書構造キーワードを持つ長文だが Gemini が entries を組めず
        route = [
            (_empty_receipt_raw(), _VALID_OCR_TEXT, 0.9),
            (_valid_receipt_raw(), _VALID_OCR_TEXT, 0.95),
        ]

        # Act
        pages, _ = _run_receipt_pipeline(route)

        # Assert
        self.assertEqual(len(pages), 2)
        by_page = {p["page_num"]: p for p in pages}
        self.assertTrue(by_page[1]["result"].get("_unrecognized"))
        self.assertFalse(by_page[1]["result"].get("_excluded_page"))

    def test_all_envelope_pdf_still_counts_as_processed(self):
        """全頁が封筒でも count>0 になり main の Failed→無限リトライに入らない。"""
        # Arrange
        route = [
            (_empty_receipt_raw(), _ENVELOPE_OCR_TEXT, 0.9),
            (_empty_receipt_raw(), _ENVELOPE_OCR_TEXT, 0.9),
        ]

        # Act
        pages, _ = _run_receipt_pipeline(route)

        # Assert: 2頁とも yield され、いずれも _page_error は立たない
        # （main は count=2 / error_pages=0 → Success → 歸檔 → 再スキャンなし）
        self.assertEqual(len(pages), 2)
        for p in pages:
            self.assertTrue(p["result"].get("_excluded_page"))
            self.assertFalse(p["result"].get("_page_error"))

    def test_envelope_filter_not_applied_to_single_page_path(self):
        """§3.5: 適用範囲は PDF 逐頁ループのみ。尾段（単頁/画像）には広げない。"""
        # Arrange: 尾段は _yield_page_results を envelope_filter なしで呼ぶ
        with mock.patch.object(ocr_engine, "_is_envelope_page") as envelope:
            with redirect_stdout(io.StringIO()):
                results = list(ocr_engine._yield_page_results(
                    DocType.RECEIPT, _empty_receipt_raw(), _ENVELOPE_OCR_TEXT, 0.9))

        # Assert: 判定自体が呼ばれず、従来通り認識不能占位行になる
        envelope.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].get("_unrecognized"))


class NonReceiptEnvelopeFilterGuardTest(unittest.TestCase):
    """Session 16 裁決の維持: 非 RECEIPT では封筒判定を一切呼ばない。

    構造キーワード（「領収」「請求書」「合計」等）は領収書向けヒューリスティック
    であり、給与明細等の短文ページに誤爆すると本来の票が除外される。
    """

    def test_envelope_check_not_called_for_salary_slip(self):
        # Arrange: 給与明細 doc_type で封筒キーワードだらけの OCR
        with mock.patch.object(ocr_engine, "_is_envelope_page") as envelope:
            with redirect_stdout(io.StringIO()):
                list(ocr_engine._yield_page_results(
                    DocType.SALARY_SLIP, {"employees": []},
                    _ENVELOPE_OCR_TEXT, 0.9, envelope_filter=True))

        # Assert
        envelope.assert_not_called()


if __name__ == "__main__":
    unittest.main()
