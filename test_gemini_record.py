"""`gemini_record` の芯（応答の直列化・鍵・side-channel）の単体テスト。

Plan: `docs/plans/2026-08-20-gemini-record-replay.md`（**v3** 定稿）§6 ＋ §12。
patch と再生の意味論は `test_gemini_record_replay.py` に分けてある。

**なぜこのモジュールが要るか**: 2026-08-20 の真票回帰で、同じ PDF・同じコード・
`temperature=0` で Gemini の出力が 3 回とも違った。受入判定の外れが
「コードの退行」か「モデルの揺れ」かを切り分けるのに、monkey patch による
実行時観測 3 回とコード読解を数十分要した。毎回これをやるのは成立しない。

venv311 が要る（`ocr_engine` 経由で google.generativeai を引く）:

    venv311/bin/python -m unittest test_gemini_record -v
"""
import contextlib
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import card_salvage
import gemini_record
import ocr_engine
from ocr_test_fixtures import etc_rows_truncated_text
from ocr_test_helpers import FINISH_MAX_TOKENS, FINISH_STOP, gemini_response


def _quiet(fn, *args, **kwargs):
    """`_parse_gemini_response` の警告 print をテスト出力に漏らさない。"""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def _roundtrip(response):
    """本物 → capture → JSON 往復 → build。保存経路まで含めて模す。

    JSON を通すのは、`capture_response` の戻り値がそのまま `response.json` に
    書かれるからである。JSON 化できない値（proto enum のような）が紛れても、
    メモリ上の往復だけでは緑になってしまう。
    """
    payload = json.loads(json.dumps(gemini_record.capture_response(response)))
    return gemini_record.build_response(payload)


def captured_contents(call):
    """製品の変体関数を呼び、`_call_gemini_parts` が受けた contents を返す。"""
    with mock.patch.object(ocr_engine, "_call_gemini_parts") as spy:
        call()
    # `_call_gemini_parts(contents, line_mode)` は位置引数 2 本で呼ばれる
    args, _ = spy.call_args
    return args[0]


# ---------------------------------------------------------------- T-1a〜T-1d

class RecordedResponseContractTest(unittest.TestCase):
    """T-1a〜T-1d: 再生した response が本物と**同じ判定**を受けること。

    `_parse_gemini_response` が response から読むのは 4 面だけ
    （`.text` / `_get_finish_reason` / `_is_max_tokens_truncated` /
    `_format_token_usage`）。その 4 面すべてで本物と一致することを固定する。
    """

    def test_t1a_normal_json_parses_identically(self):
        # Arrange
        real = gemini_response(text=json.dumps({"a": 1, "rows": [{"x": 2}]}),
                               finish_reason=FINISH_STOP)
        # Act
        replayed = _roundtrip(real)
        # Assert
        self.assertEqual(_quiet(ocr_engine._parse_gemini_response, replayed),
                         _quiet(ocr_engine._parse_gemini_response, real))
        self.assertEqual(_quiet(ocr_engine._parse_gemini_response, replayed),
                         {"a": 1, "rows": [{"x": 2}]})

    def test_t1b_truncated_salvage_recovers_same_rows(self):
        """截断応答の salvage まで再生対象に入る（AD-1 の裁定理由そのもの）。"""
        # Arrange: 100 行のうち 37 行目直後で切れた本物の形
        real = gemini_response(text=etc_rows_truncated_text(37, total=100),
                               finish_reason=FINISH_MAX_TOKENS)
        # Act
        replayed = _roundtrip(real)
        got_real = _quiet(ocr_engine._parse_gemini_response, real, salvage=True)
        got_replayed = _quiet(ocr_engine._parse_gemini_response, replayed,
                              salvage=True)
        # Assert
        self.assertEqual(got_replayed, got_real)
        # salvage が実際に走ったことを確かめる（0 行なら素通りでも緑になる）
        self.assertTrue(got_replayed[card_salvage.SALVAGED_KEY])
        self.assertEqual(len(got_replayed["rows"]), 37)

    def test_t1c_zero_parts_text_raises_value_error(self):
        # Arrange
        real = gemini_response(raises=True, finish_reason=FINISH_MAX_TOKENS)
        # Act
        replayed = _roundtrip(real)
        # Assert: `.text` の送出まで再現する
        with self.assertRaises(ValueError):
            _ = replayed.text
        self.assertIsNone(_quiet(ocr_engine._parse_gemini_response, replayed))
        self.assertEqual(_quiet(ocr_engine._parse_gemini_response, replayed),
                         _quiet(ocr_engine._parse_gemini_response, real))

    def test_t1d_finish_reason_and_truncation_judged_identically(self):
        """`candidates[0].finish_reason` の構造を保つ（Codex 中 7）。

        平坦な値で持たせると `_get_finish_reason` が空を返し、截断判定が
        **静かに**変わる。
        """
        for finish in (FINISH_STOP, FINISH_MAX_TOKENS, "MAX_TOKENS",
                       "FinishReason.MAX_TOKENS", 0):
            with self.subTest(finish=finish):
                real = gemini_response(text="{}", finish_reason=finish)
                replayed = _roundtrip(real)
                self.assertEqual(ocr_engine._get_finish_reason(replayed),
                                 ocr_engine._get_finish_reason(real))
                self.assertEqual(ocr_engine._is_max_tokens_truncated(replayed),
                                 ocr_engine._is_max_tokens_truncated(real))

    def test_t1d_token_usage_formats_identically(self):
        usage = SimpleNamespace(total_token_count=9000,
                                prompt_token_count=700,
                                candidates_token_count=500)
        for u in (usage, None):
            with self.subTest(has_usage=u is not None):
                real = gemini_response(text="{}", usage=u)
                replayed = _roundtrip(real)
                self.assertEqual(ocr_engine._format_token_usage(replayed),
                                 ocr_engine._format_token_usage(real))
        # 中身が本当に効いていること（両方 '?' で一致しても緑になるので）
        with_usage = _roundtrip(gemini_response(text="{}", usage=usage))
        self.assertIn("思考≈7800", ocr_engine._format_token_usage(with_usage))


# ------------------------------------------------------------ T-1e（鍵）

class ContentKeyTest(unittest.TestCase):
    """T-1e: 主キー（fingerprint）と部位別ハッシュ。

    部位の出所は v3（§12）で変わった —— `prompt` / `ocr` / `call_kind` は
    呼出 wrapper が受け取った**実引数**（side-channel）から、
    `text` は**実際に送出した文字列**から取る。
    """

    CTX = gemini_record.CallContext("cross_validate", "PROMPT", "OCR")

    def _contents(self, text="PROMPT\n\n--joiner--\nOCR", data=b"\xff\xd8img"):
        return [{"mime_type": "image/jpeg", "data": data}, text]

    def _key(self, *, text=None, data=b"\xff\xd8img", config=None, ctx=None):
        kwargs = {}
        if text is not None:
            kwargs["text"] = text
        return gemini_record.content_key(
            self._contents(data=data, **kwargs), config, ctx or self.CTX)

    def test_same_input_same_key(self):
        self.assertEqual(self._key().overall, self._key().overall)
        self.assertEqual(self._key().parts, self._key().parts)

    def test_prompt_change_moves_only_prompt_part(self):
        base = self._key()
        other = self._key(ctx=gemini_record.CallContext(
            "cross_validate", "PROMPT-2", "OCR"))
        self.assertNotEqual(base.overall, other.overall)
        self.assertNotEqual(base.parts["prompt"], other.parts["prompt"])
        self.assertEqual(base.parts["ocr"], other.parts["ocr"])
        self.assertEqual(base.parts["image"], other.parts["image"])

    def test_ocr_change_moves_only_ocr_part(self):
        base = self._key()
        other = self._key(ctx=gemini_record.CallContext(
            "cross_validate", "PROMPT", "OCR-2"))
        self.assertNotEqual(base.overall, other.overall)
        self.assertNotEqual(base.parts["ocr"], other.parts["ocr"])
        self.assertEqual(base.parts["prompt"], other.parts["prompt"])

    def test_call_kind_change_moves_call_kind_part(self):
        base = self._key()
        other = self._key(ctx=gemini_record.CallContext(
            "bytes", "PROMPT", "OCR"))
        self.assertNotEqual(base.overall, other.overall)
        self.assertNotEqual(base.parts["call_kind"], other.parts["call_kind"])

    def test_image_change_moves_only_image_part(self):
        base = self._key(data=b"aaa")
        other = self._key(data=b"bbb")
        self.assertNotEqual(base.overall, other.overall)
        self.assertNotEqual(base.parts["image"], other.parts["image"])
        self.assertEqual(base.parts["prompt"], other.parts["prompt"])
        self.assertEqual(base.parts["ocr"], other.parts["ocr"])
        self.assertEqual(base.parts["text"], other.parts["text"])

    def test_generation_config_change_moves_config_part(self):
        base = self._key(config={"max_output_tokens": 1})
        other = self._key(config={"max_output_tokens": 2})
        self.assertNotEqual(base.overall, other.overall)
        self.assertNotEqual(base.parts["config"], other.parts["config"])

    def test_omitted_generation_config_normalizes_to_module_default(self):
        """`None` は `GEMINI_GENERATION_CONFIG` として鍵に入る。

        素通しにすると、モジュール既定（temperature や予算）を変えても
        fixture が失効せず、**違う設定の応答を再生し続ける**。
        """
        omitted = self._key(config=None)
        explicit = self._key(config=dict(ocr_engine.GEMINI_GENERATION_CONFIG))
        self.assertEqual(omitted.parts["config"], explicit.parts["config"])

    def test_t1g_boilerplate_change_moves_text_but_not_prompt(self):
        """**v3 の中核**（§12 波及）: 定型文の改変が鍵に現れること。

        cross_validate は `prompt` 実引数のほかに「上記のOCRテキストは参考情報
        です…」等の定型文を連結して送る。side-channel だけを正本にすると、
        定型文を書き換えても `prompt` 引数は変わらないので鍵が動かず、
        **旧応答をそのまま再生し続ける** —— AD-4 が prompt 主キーを駁回した
        理由と同じ破綻（検証していないものを検証したと誤認する）。
        """
        base = self._key(text="PROMPT\n\n--joiner-A--\nOCR")
        other = self._key(text="PROMPT\n\n--joiner-B--\nOCR")
        self.assertNotEqual(base.parts["text"], other.parts["text"])
        self.assertEqual(base.parts["prompt"], other.parts["prompt"])
        self.assertEqual(base.parts["ocr"], other.parts["ocr"])
        self.assertNotEqual(base.overall, other.overall,
                            "定型文を変えても鍵が動かない = 旧応答を再生し続ける")

    def test_text_part_is_the_string_actually_sent(self):
        """`text` の出所が実送出文字列であることを製品関数で確かめる。"""
        contents = captured_contents(
            lambda: ocr_engine._call_gemini_cross_validate(
                "OCRBODY", b"img", "image/jpeg", "PROMPTBODY"))
        sent = contents[1]
        key = gemini_record.content_key(
            contents, None,
            gemini_record.CallContext("cross_validate", "PROMPTBODY",
                                      "OCRBODY"))
        self.assertEqual(key.parts["text"],
                         hashlib.sha256(sent.encode("utf-8")).hexdigest())

    def test_image_bytes_are_not_stored_only_hashed(self):
        """実票の画像は最も機微。ハッシュとバイト長だけ残す（Plan §3）。"""
        key = self._key(data=b"SECRET-IMAGE")
        blob = json.dumps(key.parts) + json.dumps(list(key.images))
        self.assertNotIn("SECRET-IMAGE", blob)
        self.assertEqual(key.images[0]["bytes"], len(b"SECRET-IMAGE"))
        self.assertEqual(key.images[0]["sha256"],
                         hashlib.sha256(b"SECRET-IMAGE").hexdigest())

    def test_missing_side_channel_is_fail_closed(self):
        """T-1f: 分類できない呼出を黙って録らない。"""
        with self.assertRaises(gemini_record.SideChannelMissingError):
            gemini_record.content_key(self._contents(), None, None)


# ------------------------------------------------------- side-channel（T-1f）

class SideChannelTest(unittest.TestCase):
    """側路が製品の 3 変体すべてで立つこと（v3 §12 欠陥 A の対処）。

    区切り文字列の逆解析を捨てた理由: cross_validate の contents は
    `[image, str]` なので、**区切りが変えられると `_call_gemini_bytes` と
    完全に同形**になる。逆解析器からは「OCR を送らない正常系」と区別が付かず、
    `ocr` が黙って null になる。実引数を側路で受け取れば、その曖昧さが消える。
    """

    def _observe(self, call):
        """変体を呼び、`_generate_content_with_retry` 時点の側路を捕まえる。"""
        seen = []

        def spy(contents, generation_config=None):
            seen.append(gemini_record.current_call())
            return gemini_response(text="{}")

        with mock.patch.object(ocr_engine, "_generate_content_with_retry", spy):
            with gemini_record.side_channel():
                call()
        return seen[0]

    def test_text_variant_exposes_prompt_and_ocr(self):
        got = self._observe(
            lambda: ocr_engine._call_gemini_text("OCRBODY", "PROMPTBODY"))
        self.assertEqual(
            (got.call_kind, got.prompt, got.ocr_text),
            ("text", "PROMPTBODY", "OCRBODY"))

    def test_cross_validate_variant_exposes_prompt_and_ocr(self):
        got = self._observe(
            lambda: ocr_engine._call_gemini_cross_validate(
                "OCRBODY", b"img", "image/jpeg", "PROMPTBODY"))
        self.assertEqual(
            (got.call_kind, got.prompt, got.ocr_text),
            ("cross_validate", "PROMPTBODY", "OCRBODY"))

    def test_bytes_variant_has_no_ocr(self):
        """OCR を送らない経路。`ocr_text=None` は**正常系**であって失敗ではない。"""
        got = self._observe(
            lambda: ocr_engine._call_gemini_bytes(
                b"img", "image/jpeg", "PROMPTBODY"))
        self.assertEqual(
            (got.call_kind, got.prompt, got.ocr_text),
            ("bytes", "PROMPTBODY", None))

    def test_tail_wrapper_reaches_the_bytes_variant(self):
        """尾段の `_call_gemini` は `_call_gemini_bytes` へ委譲する（包まない）。"""
        from ocr_test_fixtures import temp_pdf_path
        with temp_pdf_path() as path:
            got = self._observe(
                lambda: ocr_engine._call_gemini(path, "PROMPTBODY"))
        self.assertEqual(got.call_kind, "bytes")
        self.assertEqual(got.prompt, "PROMPTBODY")

    def test_direct_parts_call_leaves_the_side_channel_empty(self):
        """T-1f: 側路を通らない呼出は分類不能。ここが `None` を返すことが、
        再生・録音側の fail-closed の前提になる。"""
        seen = []

        def spy(contents, generation_config=None):
            seen.append(gemini_record.current_call())
            return gemini_response(text="{}")

        with mock.patch.object(ocr_engine, "_generate_content_with_retry", spy):
            with gemini_record.side_channel():
                ocr_engine._call_gemini_parts(["bare"])
        self.assertIsNone(seen[0])

    def test_side_channel_is_restored_after_the_context(self):
        original = ocr_engine._call_gemini_text
        with gemini_record.side_channel():
            self.assertIsNot(ocr_engine._call_gemini_text, original)
        self.assertIs(ocr_engine._call_gemini_text, original)

    def test_side_channel_is_restored_after_an_exception(self):
        original = ocr_engine._call_gemini_cross_validate
        with self.assertRaises(RuntimeError):
            with gemini_record.side_channel():
                raise RuntimeError("boom")
        self.assertIs(ocr_engine._call_gemini_cross_validate, original)


if __name__ == "__main__":
    unittest.main()
