"""T5: line_mode doc_type の出力予算と截断サルベージの結線（ocr_engine 側）。

守りたい実害は 3 つ:

1. **BULK=0 が SDK へ素通しされる** —— `config.GEMINI_MAX_OUTPUT_TOKENS_BULK`
   の 0 は「既存値を流用」の意味だが、素直に `max_output_tokens = 0` と
   書くと全応答が即截断する。最悪の回帰（毎頁が截断 → 提示行だらけ）。
2. **BULK 予算が既存 doc_type へ漏れる** —— 領収書等の thinking 挙動が
   黙って変わる。`assert_not_called` 系の検査では死なない変異。
3. **截断サルベージが既存 doc_type へ漏れる** —— 截断応答が None ではなく
   dict になり、Vision 兜底が発火しなくなる（既存の救済経路が静かに死ぬ）。

venv311 必須（ocr_engine が paddleocr / google.generativeai を引く）:
    venv311/bin/python -m unittest test_ocr_engine_line_budget -v
"""
import ast
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import card_salvage
import config
import ocr_engine
from ocr_test_fixtures import (etc_rows_text_before_rows,
                               etc_rows_truncated_text)
from ocr_test_helpers import (FINISH_MAX_TOKENS as _MAX_TOKENS,
                              FINISH_STOP as _STOP,
                              fake_gemini_model as _fake_model,
                              gemini_response, legacy_doc_types,
                              page_ocr_from_tuple,
                              sent_generation_config as _sent_config)


class LineGenerationConfigTest(unittest.TestCase):
    """§3.1: 予算の選択。0 が SDK へ届く経路を作らない。"""

    def _with_bulk(self, value):
        # `ocr_engine` は config を関数内で遅延 import するので、本物の
        # config モジュールを差し替える（sys.modules 経由で同一物を見る）。
        return mock.patch.object(config, "GEMINI_MAX_OUTPUT_TOKENS_BULK",
                                 value, create=True)

    def test_zero_means_reuse_not_a_zero_budget(self):
        with self._with_bulk(0):
            self.assertIsNone(ocr_engine._line_generation_config())

    def test_value_equal_to_default_is_also_reuse(self):
        with self._with_bulk(ocr_engine.GEMINI_MAX_OUTPUT_TOKENS):
            self.assertIsNone(ocr_engine._line_generation_config())

    def test_explicit_value_overrides_only_max_output_tokens(self):
        with self._with_bulk(65536):
            cfg = ocr_engine._line_generation_config()
        self.assertEqual(cfg["max_output_tokens"], 65536)
        for key, value in ocr_engine.GEMINI_GENERATION_CONFIG.items():
            if key != "max_output_tokens":
                with self.subTest(key=key):
                    self.assertEqual(cfg[key], value)
        # 既定 config を破壊していない（dict を共有せずコピーする）
        self.assertEqual(
            ocr_engine.GEMINI_GENERATION_CONFIG["max_output_tokens"],
            ocr_engine.GEMINI_MAX_OUTPUT_TOKENS)

    def test_missing_config_attribute_falls_back_to_default(self):
        stub = SimpleNamespace()          # 属性を 1 つも持たない config
        with mock.patch.dict(sys.modules, {"config": stub}):
            self.assertIsNone(ocr_engine._line_generation_config())

    def test_zero_never_reaches_the_sdk_even_in_line_mode(self):
        # 実害 1 の直撃検査（config が 0 のまま line_mode 呼出をした場合）
        with self._with_bulk(0):
            with _fake_model(gemini_response(text="{}")) as fake:
                ocr_engine._call_gemini_text("ocr", "prompt", line_mode=True)
        self.assertEqual(_sent_config(fake)["max_output_tokens"],
                         ocr_engine.GEMINI_MAX_OUTPUT_TOKENS,
                         "BULK=0 が予算 0 として SDK へ渡った")


class GenerateContentBudgetArgumentTest(unittest.TestCase):
    """§3.1: `_generate_content_with_retry` の省略可能引数。"""

    def test_omitted_argument_keeps_the_default_config_object(self):
        with _fake_model(gemini_response(text="{}")) as fake:
            ocr_engine._generate_content_with_retry(["x"])
        self.assertIs(_sent_config(fake), ocr_engine.GEMINI_GENERATION_CONFIG)

    def test_explicit_config_is_passed_through(self):
        custom = {"temperature": 0, "max_output_tokens": 65536}
        with _fake_model(gemini_response(text="{}")) as fake:
            ocr_engine._generate_content_with_retry(["x"],
                                                    generation_config=custom)
        self.assertIs(_sent_config(fake), custom)


class ParseGeminiResponseSalvageTest(unittest.TestCase):
    """§3.3: サルベージは parse 層で完結する（PageOcr も戻り値契約も不変）。"""

    def _parse(self, response, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            parsed = ocr_engine._parse_gemini_response(response, **kwargs)
        return parsed, buf.getvalue()

    def test_truncated_response_is_salvaged_when_enabled(self):
        response = gemini_response(text=etc_rows_truncated_text(62),
                                   finish_reason=_MAX_TOKENS)
        parsed, log = self._parse(response, salvage=True)
        self.assertEqual(len(parsed["rows"]), 62)
        self.assertEqual(parsed["rows_on_page"], 100)
        self.assertTrue(parsed[card_salvage.SALVAGED_KEY])
        self.assertIn("62", log, "回収行数がログに出ていない")

    def test_unsalvageable_truncation_still_returns_a_truthy_dict(self):
        # §3.3: これが falsy だと Vision 兜底が発火し、同じ截断を焼き直す
        response = gemini_response(raises=True, finish_reason=_MAX_TOKENS)
        parsed, _ = self._parse(response, salvage=True)
        self.assertEqual(parsed, {"rows": [], card_salvage.SALVAGED_KEY: True})
        self.assertTrue(parsed)

    def test_rows_key_is_normalized_even_when_truncated_before_rows(self):
        response = gemini_response(text=etc_rows_text_before_rows(),
                                   finish_reason=_MAX_TOKENS)
        parsed, _ = self._parse(response, salvage=True)
        self.assertEqual(parsed["rows"], [])
        self.assertEqual(parsed["rows_on_page"], 100)

    def test_schema_invalid_rows_are_normalized_not_crashed_on(self):
        # Gemini が `"rows": null` を**完結した値として**出した截断応答。
        # 素朴な setdefault だと null が残り len() で TypeError → 救えたはずの
        # 行欠け payload が消えて兜底 → _page_error → 再試行の環に戻る。
        for label, rows in (("null", "null"), ("dict", '{"a": 1}'),
                            ("string", '"x"')):
            with self.subTest(rows=label):
                text = ('{"rows": %s, "rows_on_page": 100, "card": {"issuer": "X"'
                        % rows)
                parsed, _ = self._parse(
                    gemini_response(text=text, finish_reason=_MAX_TOKENS),
                    salvage=True)
                self.assertEqual(parsed["rows"], [])
                self.assertEqual(parsed["rows_on_page"], 100)
                self.assertTrue(parsed[card_salvage.SALVAGED_KEY])

    def test_non_truncated_garbage_is_not_salvaged(self):
        # finish_reason=STOP は「AI が JSON を返さなかった」。截断ではない
        response = gemini_response(text="これはJSONではない", finish_reason=_STOP)
        parsed, log = self._parse(response, salvage=True)
        self.assertIsNone(parsed)
        self.assertIn("finish_reason", log)

    def test_salvage_disabled_keeps_the_legacy_none(self):
        response = gemini_response(text=etc_rows_truncated_text(62),
                                   finish_reason=_MAX_TOKENS)
        parsed, _ = self._parse(response)
        self.assertIsNone(parsed)

    def test_valid_json_is_untouched_in_both_modes(self):
        for salvage in (False, True):
            with self.subTest(salvage=salvage):
                response = gemini_response(text=json.dumps({"a": 1}),
                                           finish_reason=_STOP)
                parsed, _ = self._parse(response, salvage=salvage)
                self.assertEqual(parsed, {"a": 1})


class CallVariantLineModeTest(unittest.TestCase):
    """§3.3: line_mode は予算選択と salvage 許可を同時に決める単一の旗。"""

    def _invoke(self, variant, response, **kwargs):
        variants = {
            "text": lambda: ocr_engine._call_gemini_text(
                "ocr", "prompt", **kwargs),
            "bytes": lambda: ocr_engine._call_gemini_bytes(
                b"data", "image/jpeg", "prompt", **kwargs),
            "cross_validate": lambda: ocr_engine._call_gemini_cross_validate(
                "ocr", b"data", "image/jpeg", "prompt", **kwargs),
        }
        with _fake_model(response) as fake:
            with contextlib.redirect_stdout(io.StringIO()):
                parsed = variants[variant]()
        return parsed, fake

    VARIANT_NAMES = ("text", "bytes", "cross_validate")

    def test_line_mode_applies_both_budget_and_salvage(self):
        with mock.patch.object(config, "GEMINI_MAX_OUTPUT_TOKENS_BULK",
                               65536, create=True):
            for variant in self.VARIANT_NAMES:
                with self.subTest(variant=variant):
                    parsed, fake = self._invoke(
                        variant,
                        gemini_response(text=etc_rows_truncated_text(62),
                                        finish_reason=_MAX_TOKENS),
                        line_mode=True)
                    self.assertEqual(_sent_config(fake)["max_output_tokens"],
                                     65536)
                    self.assertEqual(len(parsed["rows"]), 62)

    def test_default_is_legacy_behaviour_in_every_variant(self):
        with mock.patch.object(config, "GEMINI_MAX_OUTPUT_TOKENS_BULK",
                               65536, create=True):
            for variant in self.VARIANT_NAMES:
                with self.subTest(variant=variant):
                    parsed, fake = self._invoke(
                        variant,
                        gemini_response(text=etc_rows_truncated_text(62),
                                        finish_reason=_MAX_TOKENS))
                    self.assertIs(_sent_config(fake),
                                  ocr_engine.GEMINI_GENERATION_CONFIG)
                    self.assertIsNone(parsed)


class SingleGeminiEntryPointTest(unittest.TestCase):
    """「予算選択 ＋ salvage 許可」の対が 1 箇所にしか無いこと。

    変体ごとに手で対を守らせると、4 つ目の呼出変体（将来の Files API 経路等）が
    片方だけ書いて緑になる。しかも変体を列挙する形のテストは新変体を検査対象に
    すら入れないので、**呼出点の集合そのもの**を AST で固定する。
    CLAUDE.md の ENTRY_BUILDERS 未登録事故と同型の欠陥を構造で封じるため。
    """

    SINGLE_CALLER = "_call_gemini_parts"

    def _functions_calling(self, target):
        tree = ast.parse(pathlib.Path(ocr_engine.__file__).read_text(
            encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == target):
                    found.add(node.name)
        return found

    def test_only_one_function_dispatches_to_gemini(self):
        for target in ("_generate_content_with_retry", "_parse_gemini_response"):
            with self.subTest(target=target):
                self.assertEqual(
                    self._functions_calling(target), {self.SINGLE_CALLER},
                    "%s の呼出が %s の外へ増えた。line_mode の「予算と salvage は"
                    "不可分」がそこで破れる" % (target, self.SINGLE_CALLER))

    def test_the_guard_can_actually_see_a_caller(self):
        # 番人自身が噛むこと（走査が空振りしていれば上の assert は無意味）
        self.assertIn("_call_gemini_text",
                      self._functions_calling("_call_gemini_parts"))


class LineModeGateTest(unittest.TestCase):
    """H4: 既存 doc_type は新経路に一度も入らない（**截断標本で**検査する）。

    トリガ条件を満たさない標本で `assert_not_called` を回しても、
    ゲート条件を削る変異が素通りする。截断させたうえで「入らない」ことを
    見るのが牙。doc_type は手書きせず `DocType.ALL` から導出する
    （新 doc_type が増えたとき自動で検査対象に入る）。
    """

    def _route(self, doc_type):
        """PaddleOCR 成功 → Gemini が截断応答、という頁を 1 枚流す。"""
        response = gemini_response(text=etc_rows_truncated_text(62),
                                   finish_reason=_MAX_TOKENS)
        with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                               return_value=("テキスト", 0.95)):
            with _fake_model(response) as fake:
                with contextlib.redirect_stdout(io.StringIO()):
                    page = ocr_engine._route_ocr_strategy(
                        b"data", "application/pdf", doc_type, "C")
        return page, fake

    def test_legacy_doc_types_never_salvage_and_never_get_bulk_budget(self):
        with mock.patch.object(config, "GEMINI_MAX_OUTPUT_TOKENS_BULK",
                               65536, create=True):
            for doc_type in legacy_doc_types():
                with self.subTest(doc_type=doc_type):
                    page, fake = self._route(doc_type)
                    self.assertIsNone(page.raw_data,
                                      "%s で截断応答が救われた" % doc_type)
                    for _, kwargs in fake.generate_content.call_args_list:
                        self.assertIs(kwargs["generation_config"],
                                      ocr_engine.GEMINI_GENERATION_CONFIG,
                                      "%s の呼出に BULK 予算が漏れた" % doc_type)

    def test_the_gate_would_catch_a_leak(self):
        """番人が噛むこと（誰にもサルベージしない変異を緑にしない）。"""
        with mock.patch.object(config, "GEMINI_MAX_OUTPUT_TOKENS_BULK",
                               65536, create=True):
            for doc_type in sorted(ocr_engine.LINE_MODE_DOC_TYPES):
                with self.subTest(doc_type=doc_type):
                    page, fake = self._route(doc_type)
                    self.assertEqual(len(page.raw_data["rows"]), 62)
                    self.assertEqual(_sent_config(fake)["max_output_tokens"],
                                     65536)


class TailPathLineModeTest(unittest.TestCase):
    """尾段（単頁 PDF・画像）の Vision 兜底も逐頁ループと同じ扱いであること。

    ここを落とすと、単頁のクレカ PDF・画像だけが小さい予算で兜底され、
    截断 → サルベージ無し → `_page_error` → ファイル保持 → 3 秒ごとの
    永久再試行に入る。IP-401 が潰した「逐頁と尾段の非対称」の再発であり、
    訓練サンプルは全て 2〜9 頁なので**実票の E2E では露見しない**。
    """

    def _run_tail(self, doc_type):
        """PaddleOCR も Gemini も実らず、尾段の Vision 兜底へ落ちる画像を 1 枚。"""
        page = page_ocr_from_tuple((None, "", None), doc_type)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(b"\xff\xd8\xff")
            path = tmp.name
        try:
            with mock.patch.object(ocr_engine, "_route_ocr_strategy",
                                   return_value=page), \
                 mock.patch.object(ocr_engine, "_call_gemini",
                                   return_value=None) as vision:
                with contextlib.redirect_stdout(io.StringIO()):
                    list(ocr_engine.process_pipeline(path, doc_type=doc_type))
        finally:
            os.unlink(path)
        return vision

    def test_line_mode_doc_types_keep_the_flag_in_the_tail(self):
        for doc_type in sorted(ocr_engine.LINE_MODE_DOC_TYPES):
            with self.subTest(doc_type=doc_type):
                vision = self._run_tail(doc_type)
                vision.assert_called_once()
                self.assertTrue(vision.call_args.kwargs.get("line_mode"),
                                "尾段の兜底が line_mode を落としている")

    def test_legacy_doc_types_stay_on_the_default_budget_in_the_tail(self):
        for doc_type in legacy_doc_types():
            with self.subTest(doc_type=doc_type):
                vision = self._run_tail(doc_type)
                vision.assert_called_once()
                self.assertFalse(vision.call_args.kwargs.get("line_mode", False))


if __name__ == "__main__":
    unittest.main()
