"""T5: 行欠けの痕跡を producer 側でどう出すか（`_yield_page_results`）。

趙裁定 2026-08-17 の落点は **MF タブの金額 0 提示行 ＋ 監査タブ 1 行**で、
**明細行は標色しない**（AD-7）。ここで固定するのはその形そのもの:

- 明細 result は 1 バイトも変わらない（赤タグの種を混ぜない）
- 提示行は明細の**後**（先に出すと取引No が「注記 → 明細」の順になる）
- 行が 1 行も取れなかった頁は占位行 1 本に統合する（赤い占位が 2 本並ばない）
- 既存 doc_type はこの経路に一度も入らない

venv311 必須:
    venv311/bin/python -m unittest test_ocr_engine_line_shortage -v
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import card_salvage
import config
import ocr_engine
from doc_types import DocType
from ocr_test_fixtures import (etc_rows_raw, etc_rows_truncated_text,
                               temp_pdf_path)
from ocr_test_helpers import (FINISH_MAX_TOKENS as _MAX_TOKENS,
                              FINISH_STOP as _STOP, fake_gemini_model,
                              gemini_response, legacy_doc_types, pdf_pages,
                              sent_generation_config)

_OCR_TEXT = "ＥＮＥＯＳ ＢＵＳＩＮＥＳＳ ＥＴＣカード ご利用明細"


def _yield_page(doc_type, raw_data, ocr_text=_OCR_TEXT):
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        results = list(ocr_engine._yield_page_results(
            doc_type, raw_data, ocr_text, None))
    return results, buf.getvalue()


def _short_cc_page(got=62, total=100, salvaged=True):
    """券面 total 行のうち got 行しか取れなかった頁の raw_data。"""
    raw = etc_rows_raw(total)
    raw["rows"] = raw["rows"][:got]
    if salvaged:
        raw[card_salvage.SALVAGED_KEY] = True
    return raw


class ShortageWithEntriesTest(unittest.TestCase):
    """行は取れたが足りない ―― 明細はそのまま、提示行を後ろに 1 本。"""

    def setUp(self):
        self.results, self.log = _yield_page(DocType.CREDIT_CARD,
                                             _short_cc_page())

    def test_detail_then_notice_in_that_order(self):
        self.assertEqual(len(self.results), 2)
        detail, notice = self.results
        self.assertTrue(detail["entries"], "明細 result が先頭でない")
        self.assertFalse(notice["entries"])
        self.assertTrue(notice["_unrecognized"])

    def test_detail_result_is_untouched(self):
        """明細 result に痕跡を混ぜない（AD-7: 明細 62 行を赤くしない）。

        `doc_red` / `red_flags` へ shortage を流す実装だと `tag_rules` が
        全行を赤系にする —— 検算・行欠けの赤は「カード単位 1 行」であって
        明細行ではない、という裁定の正面違反になる。
        """
        untouched, _ = _yield_page(DocType.CREDIT_CARD, etc_rows_raw(62))
        detail = self.results[0]
        self.assertEqual(set(detail) - set(untouched[0]), set(),
                         "明細 result に新しいキーが足された")
        self.assertEqual(len(detail["entries"]), len(untouched[0]["entries"]))
        self.assertNotIn("_audit_signal", detail)

    def test_notice_carries_the_customer_facing_memo(self):
        notice = self.results[1]
        self.assertIn("100行中62行", notice["memo"])
        self.assertIn("原票を確認", notice["memo"])
        # 提示行の vendor/date は明細と揃える（帳簿上で紐付くように）
        self.assertEqual(notice["vendor"], self.results[0]["vendor"])
        self.assertEqual(notice["date"], self.results[0]["date"])

    def test_notice_carries_the_machine_readable_audit_reason(self):
        notice = self.results[1]
        self.assertEqual(notice["_audit_signal"], "line_shortage:62/100")
        self.assertEqual(notice["_ocr_text_len"], len(_OCR_TEXT))

    def test_shortage_is_logged(self):
        self.assertIn("100行中62行", self.log)


class ShortageWithoutEntriesTest(unittest.TestCase):
    """1 行も取れなかった頁 ―― 占位行 1 本に統合する。"""

    def test_single_placeholder_carries_both_marks(self):
        results, _ = _yield_page(DocType.CREDIT_CARD, _short_cc_page(got=0))
        self.assertEqual(len(results), 1, "赤い占位行が 2 本並んでいる")
        only = results[0]
        self.assertTrue(only["_unrecognized"])
        self.assertEqual(only["entries"], [])
        self.assertIn("100行中0行", only["memo"])
        self.assertEqual(only["_audit_signal"], "line_shortage:0/100")

    def test_unknown_total_still_produces_a_placeholder(self):
        # 券面総数すら読めないまま截断した頁（「分からない＝問題なし」に倒さない）
        results, _ = _yield_page(
            DocType.CREDIT_CARD,
            {"rows": [], card_salvage.SALVAGED_KEY: True})
        self.assertEqual(len(results), 1)
        self.assertIn("総数不明", results[0]["memo"])
        self.assertEqual(results[0]["_audit_signal"], "line_shortage:0/?")


class SalvagedButSatisfiedTest(unittest.TestCase):
    """救えたが行数は足りている ―― 帳簿は汚さず監査タブにだけ残す。"""

    def test_audit_only_signal_on_the_detail_result(self):
        raw = etc_rows_raw(4)
        raw[card_salvage.SALVAGED_KEY] = True
        results, _ = _yield_page(DocType.CREDIT_CARD, raw)
        self.assertEqual(len(results), 1, "帳簿へ提示行が出てしまっている")
        self.assertEqual(results[0]["_audit_signal"], "salvaged:4/4")
        self.assertEqual(len(results[0]["entries"]), 4)
        self.assertFalse(results[0]["_unrecognized"])


class NoShortageTest(unittest.TestCase):
    """健全な頁は現行と 1 バイトも変わらない。"""

    def test_healthy_page_yields_one_untouched_result(self):
        results, _ = _yield_page(DocType.CREDIT_CARD, etc_rows_raw(6))
        self.assertEqual(len(results), 1)
        self.assertNotIn("_audit_signal", results[0])
        self.assertEqual(len(results[0]["entries"]), 6)

    def test_row_skip_without_salvage_is_still_caught(self):
        # T-b: 有効 JSON なのに Gemini が行を読み飛ばした頁
        results, _ = _yield_page(DocType.CREDIT_CARD,
                                 _short_cc_page(got=97, salvaged=False))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[1]["_audit_signal"], "line_shortage:97/100")


class LegacyDocTypeGateTest(unittest.TestCase):
    """H4: 既存 doc_type はこの経路に入らない（rows_on_page があっても）。"""

    def test_legacy_doc_types_never_get_a_shortage_notice(self):
        for doc_type in legacy_doc_types():
            with self.subTest(doc_type=doc_type):
                # 行欠けに「見える」raw_data を渡しても反応しないこと
                raw = {"date": "2026/05/01", "vendor": "テスト",
                       "rows": [], "rows_on_page": 100,
                       card_salvage.SALVAGED_KEY: True,
                       "documents": [], "items": []}
                results, _ = _yield_page(doc_type, raw)
                for result in results:
                    self.assertNotIn("_audit_signal", result)
                    self.assertNotIn("取得漏れ", result.get("memo", ""))

    def test_the_gate_would_catch_a_leak(self):
        """番人が噛むこと（誰にも提示行を出さない変異を緑にしない）。"""
        for doc_type in sorted(ocr_engine.LINE_MODE_DOC_TYPES):
            with self.subTest(doc_type=doc_type):
                results, _ = _yield_page(doc_type, _short_cc_page(got=0))
                self.assertEqual(results[-1]["_audit_signal"],
                                 "line_shortage:0/100")


class TruncatedPageEndToEndTest(unittest.TestCase):
    """截断した実頁が `process_pipeline` を通り抜けるまでを 1 本で見る。

    層ごとの単体テスト（parse 層のサルベージ／`_yield_page_results` の 2 payload）
    は互いに緑でも、**その間の結線**が抜けていれば本番では何も起きない。
    ここだけは Gemini 応答テキストから yield される payload までを通しで確かめる。
    """

    def _run(self, response_text, finish_reason=_MAX_TOKENS):
        response = gemini_response(text=response_text,
                                   finish_reason=finish_reason)
        with temp_pdf_path() as path:
            with mock.patch.object(ocr_engine, "_split_pdf_pages",
                                   return_value=iter(pdf_pages(1))), \
                 mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                                   return_value=(_OCR_TEXT, 0.95)), \
                 fake_gemini_model(response) as fake_model:
                with contextlib.redirect_stdout(io.StringIO()) as buf:
                    pages = list(ocr_engine.process_pipeline(
                        path, doc_type=DocType.CREDIT_CARD))
        return pages, fake_model, buf.getvalue()

    def test_truncated_100_row_page_books_62_rows_and_flags_the_gap(self):
        pages, fake_model, _ = self._run(etc_rows_truncated_text(62))

        # ① Gemini は 1 回だけ（Vision 兜底が発火していない ＝ 空焼きしない）
        self.assertEqual(fake_model.generate_content.call_count, 1)
        # ② 予算は BULK（config の実値。結線が切れていればここで落ちる）
        self.assertEqual(
            sent_generation_config(fake_model)["max_output_tokens"],
            config.GEMINI_MAX_OUTPUT_TOKENS_BULK)
        # ③ 明細 62 行が記帳され、提示行が 1 本後ろに付く
        self.assertEqual(len(pages), 2)
        detail, notice = (p["result"] for p in pages)
        self.assertEqual(len(detail["entries"]), 62)
        self.assertNotIn("_page_error", detail)
        self.assertTrue(notice["_unrecognized"])
        self.assertIn("100行中62行", notice["memo"])
        self.assertEqual(notice["_audit_signal"], "line_shortage:62/100")
        # ④ 両方とも同じ物理頁として出ている（原票リンクが付く）
        self.assertEqual([p["page_num"] for p in pages], [1, 1])

    def test_unsalvageable_truncation_is_a_placeholder_not_a_retry_loop(self):
        # 1 行も救えない截断。ここで `_page_error` に落ちるとファイルが保持され、
        # 3 秒ごとに同じ頁を焼き直す無限ループに戻る（G6 の再発）。
        pages, fake_model, _ = self._run('{"card": {"iss')
        self.assertEqual(fake_model.generate_content.call_count, 1)
        self.assertEqual(len(pages), 1)
        result = pages[0]["result"]
        self.assertNotIn("_page_error", result)
        self.assertTrue(result["_unrecognized"])
        self.assertIn("総数不明", result["memo"])

    def test_non_truncated_garbage_still_falls_back_to_vision(self):
        # 截断でない解析失敗は従来どおり兜底へ（救済経路を殺していないこと）
        _, fake_model, _ = self._run("これはJSONではない", finish_reason=_STOP)
        self.assertEqual(fake_model.generate_content.call_count, 2)


if __name__ == "__main__":
    unittest.main()
