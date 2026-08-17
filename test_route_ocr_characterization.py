"""T3 の特性テスト（characterization test）: Gemini 例外時の Vision 兜底。

このファイルは **T3 の改造前に書かれ、改造後も 1 行も変えずに緑であること**
自体が成果物である（Plan `docs/plans/2026-08-17-t3-page-ocr-resolver.md` の
AD-T3-4 / B9）。

守っている不変式:

    _route_ocr_strategy の内側で Gemini 呼出が例外を投げても、そのページは
    「ページ処理エラー」の占位行に転落せず、Gemini Vision の兜底で救われる。

現行実装ではこれは `_route_ocr_strategy` の try が PaddleOCR だけでなく
`_call_gemini_text` / `_call_gemini_bytes` / `_call_gemini_cross_validate`
まで覆っていることで成立している（ocr_engine.py:1871-1890）。T3 の
「resolver 化」で except の範囲を PaddleOCR だけに絞ると、この兜底が
静かに死ぬ —— 全 doc_type に効く回帰であり、しかも本番では
「認識不能占位行が増えた」としか見えず原因に辿り着けない。

**なぜ process_pipeline 層に当てるか**: T3 は `_route_ocr_strategy` の
第 3 引数（prompt → doc_type）と戻り値型（3-tuple → PageOcr）を変える。
同関数に直接当てたテストは改造時に必ず書き換えが要り、「無修正で緑」という
証明力が消える。`process_pipeline` のシグネチャは T3 で変わらないので、
ここに当てれば改造を跨いで同じテストが使える。しかも測っているのが
「頁が兜底で救われるか」という守りたいもの本体になる。

**このファイルは意図的に足場を共有していない**: 他のテストが使う
`ocr_test_fixtures.temp_pdf_path()` へ寄せれば tempfile の定型を 2 箇所
減らせるが、それはやらない。このファイルの価値は中身が T3 の前後で
1 行も変わっていないことそのものにあり、整理のためであっても手を入れると
「無修正で緑だった」という証明が弱くなる。重複 2 箇所はその対価として
受け入れる（レビュー指摘済み・2026-08-17 に維持を裁定）。

（この段落を含め、T3 期間に本ファイルへ加えたのは**説明文だけ**である。
import・ヘルパ・モック構成・アサーションは 1 行も変えていない ——
git 履歴で本ファイルに差分があるのを見た人が、証明が崩れたのかどうかを
diff を読まずに判断できるよう明記しておく。）

venv311 必須（ocr_engine が paddleocr / google.generativeai を引くため）:
    venv311/bin/python -m unittest test_route_ocr_characterization -v
"""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import ocr_engine
from doc_types import DocType


_VALID_RECEIPT_JSON = {
    "documents": [{
        "date": "2026/08/17",
        "vendor": "テスト商店",
        "invoice_num": "T1234567890123",
        "total": 1100,
        "account": "消耗品費",
    }]
}


def _one_pdf_page():
    """1 ページ PDF の _split_pdf_pages 相当（逐頁分岐へ入れるため）。"""
    return iter([{"page_num": 1, "total_pages": 1, "data": b"%PDF-page1"}])


def _run_pipeline_with_gemini_error(strategy, failing_call):
    """PaddleOCR は成功・指定の Gemini 呼出だけが例外を投げる状況を作る。

    Returns:
        (yielded_pages, vision_mock) — vision_mock は _call_gemini_bytes。
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(b"%PDF-1.4 dummy")
        path = tmp.name

    patches = {
        "_ocr_with_paddleocr": mock.patch.object(
            ocr_engine, "_ocr_with_paddleocr",
            return_value=("領収書 テスト商店 1,100円", 0.95)),
        "_split_pdf_pages": mock.patch.object(
            ocr_engine, "_split_pdf_pages", return_value=_one_pdf_page()),
        # Vision 兜底は成功させる。これが呼ばれること自体が検証対象。
        "_call_gemini_bytes": mock.patch.object(
            ocr_engine, "_call_gemini_bytes",
            return_value=_VALID_RECEIPT_JSON),
        "_call_gemini_text": mock.patch.object(
            ocr_engine, "_call_gemini_text",
            return_value=_VALID_RECEIPT_JSON),
        "_call_gemini_cross_validate": mock.patch.object(
            ocr_engine, "_call_gemini_cross_validate",
            return_value=_VALID_RECEIPT_JSON),
    }

    try:
        started = {name: p.start() for name, p in patches.items()}
        # 指定された呼出だけを爆発させる
        started[failing_call].side_effect = RuntimeError(
            "boom: simulated Gemini failure")
        try:
            with redirect_stdout(io.StringIO()):
                pages = list(ocr_engine.process_pipeline(
                    path, doc_type=DocType.RECEIPT, ocr_strategy=strategy))
            return pages, started["_call_gemini_bytes"]
        finally:
            for p in patches.values():
                p.stop()
    finally:
        os.unlink(path)


class GeminiExceptionFallsBackToVisionTest(unittest.TestCase):
    """Gemini 側の例外が Vision 兜底に落ちる（＝頁が失われない）。"""

    def test_strategy_a_text_call_raises_falls_back_to_vision(self):
        # Arrange / Act: 戦略 A はテキスト経由。その呼出が例外を投げる
        pages, vision = _run_pipeline_with_gemini_error(
            "A", "_call_gemini_text")

        # Assert: Vision 兜底が呼ばれ、頁は正常な仕訳として出ている
        self.assertTrue(
            vision.called,
            "Gemini テキスト呼出の例外が握られず Vision 兜底に落ちなかった")
        self.assertEqual(len(pages), 1)
        self.assertNotIn("_page_error", pages[0]["result"],
                         "兜底で救えるはずの頁がページ処理エラーに転落した")

    def test_strategy_c_cross_validate_raises_falls_back_to_vision(self):
        # Arrange / Act: 戦略 C（本番既定）は交差検証。その呼出が例外を投げる
        pages, vision = _run_pipeline_with_gemini_error(
            "C", "_call_gemini_cross_validate")

        # Assert
        self.assertTrue(
            vision.called,
            "交差検証の例外が握られず Vision 兜底に落ちなかった")
        self.assertEqual(len(pages), 1)
        self.assertNotIn("_page_error", pages[0]["result"])

    def test_strategy_b_high_confidence_text_call_raises_falls_back(self):
        # Arrange / Act: 戦略 B は置信度が閾値以上ならテキスト経由。
        # モックの 0.95 は既定閾値を上回るのでテキスト経路に入る
        pages, vision = _run_pipeline_with_gemini_error(
            "B", "_call_gemini_text")

        # Assert
        self.assertTrue(vision.called)
        self.assertEqual(len(pages), 1)
        self.assertNotIn("_page_error", pages[0]["result"])

    def test_paddleocr_raises_still_falls_back_to_vision(self):
        # Arrange: PaddleOCR 自体が落ちる既存経路（回帰保護）
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(b"%PDF-1.4 dummy")
            path = tmp.name
        try:
            with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                                   side_effect=RuntimeError("paddle down")), \
                 mock.patch.object(ocr_engine, "_split_pdf_pages",
                                   return_value=_one_pdf_page()), \
                 mock.patch.object(ocr_engine, "_call_gemini_bytes",
                                   return_value=_VALID_RECEIPT_JSON) as vision:
                # Act
                with redirect_stdout(io.StringIO()):
                    pages = list(ocr_engine.process_pipeline(
                        path, doc_type=DocType.RECEIPT, ocr_strategy="C"))
        finally:
            os.unlink(path)

        # Assert
        self.assertTrue(vision.called)
        self.assertEqual(len(pages), 1)
        self.assertNotIn("_page_error", pages[0]["result"])


if __name__ == "__main__":
    unittest.main()
