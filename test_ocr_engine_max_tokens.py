"""ocr_engine の MAX_TOKENS 截断対策（[B''])単体テスト。

ocr_engine は paddleocr / google.generativeai / pdf2image 等の重依存を
import するため venv311 で実行する:
    venv311/bin/python -m unittest test_ocr_engine_max_tokens -v
    venv311/bin/python -m pytest test_ocr_engine_max_tokens.py -v
"""
import contextlib
import io
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import ocr_engine
# 偽 response の組み立ては `ocr_test_helpers` の共有版を使う。T5 で新しい
# テストが同じ形を必要としたとき、SDK 0.8.5 の `.text` / `parts` 契約の
# モデルがテスト資産に 2 つ並ぶことになった —— 片方だけ直せば「どちらが
# 本物の契約か」が判別不能になるので、T5 の実装が着地した時点で畳んだ。
# （実装中は本ファイルが**無修正で緑**であること自体が受入基準だった。）
from ocr_test_helpers import gemini_response as _make_response


class IsMaxTokensTruncatedTest(unittest.TestCase):
    def test_finish_reason_int_2_is_truncated(self):
        # Arrange
        response = _make_response(finish_reason=2)
        # Act / Assert
        self.assertTrue(ocr_engine._is_max_tokens_truncated(response))

    def test_finish_reason_enum_strings_are_truncated(self):
        for fr in ("MAX_TOKENS", "FinishReason.MAX_TOKENS"):
            with self.subTest(finish_reason=fr):
                response = _make_response(finish_reason=fr)
                self.assertTrue(ocr_engine._is_max_tokens_truncated(response))

    def test_finish_reason_stop_is_not_truncated(self):
        response = _make_response(finish_reason=1)
        self.assertFalse(ocr_engine._is_max_tokens_truncated(response))

    def test_empty_candidates_is_not_truncated(self):
        response = SimpleNamespace(candidates=[])
        self.assertFalse(ocr_engine._is_max_tokens_truncated(response))


class GenerationConfigTest(unittest.TestCase):
    def test_default_config_uses_max_output_tokens_constant(self):
        # 唯一の字面量アサート（仕様ドキュメント化）
        self.assertEqual(ocr_engine.GEMINI_MAX_OUTPUT_TOKENS, 32768)
        self.assertEqual(
            ocr_engine.GEMINI_GENERATION_CONFIG["max_output_tokens"],
            ocr_engine.GEMINI_MAX_OUTPUT_TOKENS,
        )

    def test_generate_content_receives_default_generation_config(self):
        # Arrange
        fake = mock.MagicMock()
        fake.generate_content.return_value = _make_response(text="{}")
        # Act
        with mock.patch.object(ocr_engine, "model", fake):
            ocr_engine._generate_content_with_retry(["x"])
        # Assert
        _, kwargs = fake.generate_content.call_args
        self.assertEqual(
            kwargs["generation_config"]["max_output_tokens"],
            ocr_engine.GEMINI_MAX_OUTPUT_TOKENS,
        )


class ParseGeminiResponseTest(unittest.TestCase):
    def test_valid_json_returns_parsed_dict(self):
        response = _make_response(text=json.dumps({"a": 1}), finish_reason=1)
        self.assertEqual(ocr_engine._parse_gemini_response(response), {"a": 1})

    def test_text_value_error_returns_none_without_raising(self):
        # zero-parts: text が ValueError を送出しても None で返る
        response = _make_response(raises=True, finish_reason=2)
        # 解析失敗時の警告ログをテスト出力に漏らさない（他の用例と同様に捕捉）
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(ocr_engine._parse_gemini_response(response))

    def test_garbage_text_returns_none_and_warns_finish_reason(self):
        response = _make_response(text="これはJSONではない", finish_reason=2)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = ocr_engine._parse_gemini_response(response)
        self.assertIsNone(result)
        self.assertIn("finish_reason", buf.getvalue())

    def test_truncated_warning_includes_thinking_token_estimate(self):
        usage = SimpleNamespace(
            total_token_count=9000,
            prompt_token_count=700,
            candidates_token_count=500,
        )
        response = _make_response(raises=True, finish_reason=2, usage=usage)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ocr_engine._parse_gemini_response(response)
        self.assertIn("思考≈7800", buf.getvalue())


class CallSiteWiringTest(unittest.TestCase):
    """三処の去重置換（_parse_gemini_response）の接線を守る。"""

    def _invoke_all_variants(self, fake_response):
        fake = mock.MagicMock()
        fake.generate_content.return_value = fake_response
        results = {}
        with mock.patch.object(ocr_engine, "model", fake):
            results["text"] = ocr_engine._call_gemini_text("ocr", "prompt")
            results["bytes"] = ocr_engine._call_gemini_bytes(
                b"data", "image/jpeg", "prompt"
            )
            results["cross_validate"] = ocr_engine._call_gemini_cross_validate(
                "ocr", b"data", "image/jpeg", "prompt"
            )
        return results

    def test_all_call_gemini_variants_return_parsed_dict_on_valid_json(self):
        results = self._invoke_all_variants(
            _make_response(text=json.dumps({"ok": True}), finish_reason=1)
        )
        for variant, parsed in results.items():
            with self.subTest(variant=variant):
                self.assertEqual(parsed, {"ok": True})

    def test_all_call_gemini_variants_return_none_on_zero_parts(self):
        # 截断で .text が ValueError → 三者とも None、fallback 連鎖は崩れない
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            results = self._invoke_all_variants(
                _make_response(raises=True, finish_reason=2)
            )
        for variant, parsed in results.items():
            with self.subTest(variant=variant):
                self.assertIsNone(parsed)


if __name__ == "__main__":
    unittest.main()
