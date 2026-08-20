"""`local_test.py` の `--record` / `--replay` / `--accept-drift` の配線。

Plan: `docs/plans/2026-08-20-gemini-record-replay.md` T-3。
記録再生の芯そのものは `test_gemini_record.py` / `test_gemini_record_replay.py`。

ここで守るのは**入口の作法**だけである:
  ・矛盾するフラグを Sheets へ繋ぐ**前に**弾く（繋いでから落ちると、
    タブだけ増えて何も書かれない中途半端な状態になる）
  ・フラグが無いときは `gemini_record` の context を 1 つも張らない

    venv311/bin/python -m unittest test_local_test_record_replay -v
"""
import contextlib
import tempfile
import unittest
import unittest.mock

import gemini_record
import local_test
import ocr_engine


def _args(**overrides):
    argv = []
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        for item in (value if isinstance(value, list) else [value]):
            argv += [flag, str(item)]
    return local_test.build_parser().parse_args(argv)


class FlagValidationTest(unittest.TestCase):
    """`check_record_flags`: Sheets へ繋ぐ前の早期検査。"""

    def test_no_flags_needs_nothing(self):
        self.assertEqual(local_test.check_record_flags(_args()), ())

    def test_record_and_replay_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as caught:
            local_test.check_record_flags(_args(record="a", replay="b"))
        self.assertIn("--record", str(caught.exception))

    def test_accept_drift_without_replay_is_rejected(self):
        """録音側に「差分を許す」は意味が無い。黙って無視すると、
        指定したのに効いていないことに気付けない。"""
        with self.assertRaises(SystemExit) as caught:
            local_test.check_record_flags(_args(record="a", accept_drift="ocr"))
        self.assertIn("--accept-drift", str(caught.exception))

    def test_unknown_drift_part_is_rejected_with_the_valid_list(self):
        with self.assertRaises(SystemExit) as caught:
            local_test.check_record_flags(
                _args(replay="a", accept_drift="typo"))
        message = str(caught.exception)
        self.assertIn("typo", message)
        for part in gemini_record.ALL_PARTS:
            self.assertIn(part, message)

    def test_valid_drift_parts_pass_through(self):
        self.assertEqual(
            local_test.check_record_flags(
                _args(replay="a", accept_drift=["ocr", "image"])),
            ("ocr", "image"))


class ContextWiringTest(unittest.TestCase):
    """`open_gemini_record`: context を張るのは指定されたときだけ。"""

    def test_without_flags_no_context_is_opened(self):
        original = ocr_engine._generate_content_with_retry
        with contextlib.ExitStack() as stack:
            self.assertIsNone(
                local_test.open_gemini_record(_args(), (), stack))
            self.assertIs(ocr_engine._generate_content_with_retry, original)

    def test_record_opens_a_session_and_restores_on_exit(self):
        original = ocr_engine._generate_content_with_retry
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.ExitStack() as stack:
                session = local_test.open_gemini_record(
                    _args(record=directory), (), stack)
                self.assertEqual(session.mode, "record")
                self.assertIsNot(ocr_engine._generate_content_with_retry,
                                 original)
        self.assertIs(ocr_engine._generate_content_with_retry, original)

    def test_replay_on_an_empty_directory_fails_loudly(self):
        """録音が無いのに再生を頼まれたら止まる（黙って実 Gemini に落ちない）。"""
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.ExitStack() as stack:
                with self.assertRaises(gemini_record.RecordingMissingError):
                    local_test.open_gemini_record(
                        _args(replay=directory), (), stack)


if __name__ == "__main__":
    unittest.main()


class FatalErrorTest(unittest.TestCase):
    """記録再生の例外を「このファイルは失敗」で握り潰さない（Codex 高 2）。

    Plan §5 は再生時の異常を**すべて例外で停止**と定めている。ところが
    `main()` の逐ファイルループは `except Exception` で全部を拾って
    `fail_count += 1` して先へ進む —— 不一致のまま Sheets の flush まで
    到達し、「一部のファイルが失敗しただけ」に見えてしまう。
    決定的なはずの回帰が静かに壊れる、まさにその形。
    """

    FATAL = (gemini_record.RecordError,)

    def _files(self):
        return [{"name": "dummy.pdf", "path": "x", "doc_type": "receipt"}]

    def test_replay_mismatch_stops_the_whole_run(self):
        with unittest.mock.patch.object(
                local_test, "process_local_file",
                side_effect=gemini_record.ReplayMismatchError("部位=ocr")):
            with self.assertRaises(gemini_record.ReplayMismatchError):
                local_test.process_all(self._files(), None, _args(), self.FATAL)

    def test_side_channel_missing_stops_the_whole_run(self):
        with unittest.mock.patch.object(
                local_test, "process_local_file",
                side_effect=gemini_record.SideChannelMissingError("x")):
            with self.assertRaises(gemini_record.SideChannelMissingError):
                local_test.process_all(self._files(), None, _args(), self.FATAL)

    def test_ordinary_errors_are_still_counted_not_raised(self):
        """普通の失敗（壊れた PDF など）は従来どおり数えて次へ進む。"""
        with unittest.mock.patch.object(
                local_test, "process_local_file",
                side_effect=RuntimeError("壊れた PDF")):
            success, fail = local_test.process_all(
                self._files(), None, _args(), self.FATAL)
        self.assertEqual((success, fail), (0, 1))

    def test_fatal_types_are_empty_without_a_session(self):
        self.assertEqual(local_test.record_error_types(None), ())

    def test_fatal_types_include_record_error_with_a_session(self):
        self.assertEqual(local_test.record_error_types(object()),
                         (gemini_record.RecordError,))
