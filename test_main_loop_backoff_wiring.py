"""main() ループの失敗退避護欄「接線」テスト（merge 2026-08-11・検収 4/5）。

なぜ別ファイルで固定するか:
`test_main_process_file.py` の backoff テスト群（FailureBackoffScheduleTest 以下
5 クラス）は**全て純関数級**——`_next_backoff_seconds` / `_record_file_failure` /
`_partition_by_backoff` / `_format_backoff_summary` を直接叩くだけで、`main()`
の接線を一切通らない。それらは merge で touch されないので必ず緑になり、
「接線が丸ごと失われていても全緑」という状態が起こり得る。接線こそ merge で
失われる当の部分なので、ここで別途固定する
（docs/plans/2026-08-11-merge-main-into-headless.md §11 S2・Codex P1）。

    venv311/bin/python -m unittest test_main_loop_backoff_wiring -v
"""

from __future__ import annotations

import importlib
import io
import os
import sys
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("PROCESSED_FOLDER_ID", "test_processed_folder")
os.environ.setdefault("SERVICE_ACCOUNT_FILE", "test_sa.json")
os.environ.setdefault("OUTPUT_SPREADSHEET_ID", "test_spreadsheet")
os.environ.setdefault("FOLDER_RECEIPT_ID", "test_receipt_folder")

import config
importlib.reload(config)
import main
from headless_rerun_fixture import FakeReporter, FakeWriter
from test_headless_loop_wiring import (
    _call_process_one_file, _job, _patch_file_processing,
)


class _StopLoop(BaseException):
    """main() の `while True` を任意の輪数で抜けるための番兵。

    **BaseException を継承するのが要**。Exception を継承すると main() 外殻の
    `except Exception` に捕まり、そこで `time.sleep(5)` が呼ばれて本例外が
    再び飛ぶ——永久に抜けられなくなる。
    """


_PROFILE = {
    "label": "テスト",
    "split_pdf_folder_id": "split",
    "processed_folder_id": "processed",
    "spreadsheet_id": "sheet",
}


def _drive_main(files, *, cycles=1, side_effect=None):
    """main() を `cycles` 輪だけ回し、各輪で `_process_one_file` に渡された
    file_id を輪ごとに記録して返す。

    戻り値: `[[1輪目の file_id, ...], [2輪目, ...], ...]`

    `_process_one_file` 自体は patch する——本ファイルの関心事は
    「main() が誰を呼び、戻り値をどう退避記録へ反映するか」であって、
    ファイル処理の中身ではない（そちらは test_headless_loop_wiring が担当）。
    """
    rounds = [[] for _ in range(cycles)]
    state = {"cycle": 0}

    def _fake_process_one_file(service, writer, job_reporter, file, *a, **kw):
        rounds[state["cycle"]].append(file["id"])
        if side_effect is not None:
            return side_effect(file)
        return main.RetryAction.CLEAR

    def _fake_sleep(_seconds):
        state["cycle"] += 1
        if state["cycle"] >= cycles:
            raise _StopLoop

    with ExitStack() as stack:
        stack.enter_context(patch.object(main, "get_drive_service",
                                         return_value=None))
        stack.enter_context(patch.object(main, "build_writers",
                                         return_value={"p": FakeWriter()}))
        stack.enter_context(patch.object(main, "_init_headless_reporter",
                                         return_value=None))
        stack.enter_context(patch.object(main, "build_progress_reporters",
                                         return_value={}))
        stack.enter_context(patch.object(main, "profiles", {"p": _PROFILE}))
        stack.enter_context(patch.object(main, "filter_active_folders",
                                         return_value={"in": ("receipt", "p")}))
        stack.enter_context(patch.object(main, "list_files", return_value=files))
        stack.enter_context(patch.object(main, "_process_one_file",
                                         side_effect=_fake_process_one_file))
        stack.enter_context(patch.object(main.time, "sleep",
                                         side_effect=_fake_sleep))
        with redirect_stdout(io.StringIO()):
            try:
                main.main()
            except _StopLoop:
                pass
    return rounds


def _f(file_id):
    return {"id": file_id, "name": f"{file_id}.pdf"}


class MainLoopBackoffWiringTest(unittest.TestCase):
    """検収 4: main() ループが ready_files のみを回し、戻り値を退避へ反映する。"""

    def test_increment_puts_the_file_into_backoff_next_round(self):
        """INCREMENT された file は次輪で `_partition_by_backoff` に濾される。

        C4 を「HEAD 側の呼出だけ採る」と `for file in files:` のままになり、
        ready_files が無視されて護欄が完全に無力化する——それを捕まえる。
        """
        rounds = _drive_main(
            [_f("ok"), _f("bad")], cycles=2,
            side_effect=lambda f: (main.RetryAction.INCREMENT
                                   if f["id"] == "bad" else main.RetryAction.CLEAR))
        self.assertEqual(sorted(rounds[0]), ["bad", "ok"])
        self.assertEqual(rounds[1], ["ok"], "退避中の bad が 2 輪目でも呼ばれている")

    def test_one_broken_file_does_not_block_the_rest_of_the_batch(self):
        """A が例外を投げても同バッチの B は処理される（per-file 例外隔離）。

        例外捕捉が main() の外殻 try だけだと、A の例外で走査ごと止まり
        B に到達しない——1 個の壊れたファイルが行列全体を塞いではならない。
        """
        def _boom(f):
            if f["id"] == "a":
                raise RuntimeError("drive down")
            return main.RetryAction.CLEAR

        rounds = _drive_main([_f("a"), _f("b")], cycles=1, side_effect=_boom)
        self.assertEqual(rounds[0], ["a", "b"])

    def test_exception_is_counted_as_failure_and_backed_off(self):
        """例外は INCREMENT 扱い（Plan D4 #10）——唯一の無限リトライ経路を塞ぐ。"""
        def _boom(f):
            if f["id"] == "a":
                raise RuntimeError("drive down")
            return main.RetryAction.CLEAR

        rounds = _drive_main([_f("a"), _f("b")], cycles=2, side_effect=_boom)
        self.assertEqual(rounds[1], ["b"], "例外を投げた a が退避されていない")

    def test_keep_never_creates_a_backoff_record(self):
        """KEEP は退避記録に触れない——memo 等の別機構が抑制を担当する経路。

        ここで加算すると二重抑制になり、控制面が直した後の再開が最大 1 時間
        遅れる（Plan D4・§12【3】）。
        """
        rounds = _drive_main([_f("a")], cycles=2,
                             side_effect=lambda f: main.RetryAction.KEEP)
        self.assertEqual(rounds[1], ["a"], "KEEP なのに退避記録が作られている")

    def test_clear_keeps_the_file_processable_every_round(self):
        """CLEAR は毎輪処理され続ける（成功した file を誤って退避させない）。"""
        rounds = _drive_main([_f("a")], cycles=2)
        self.assertEqual(rounds[1], ["a"])


class RetryActionTest(unittest.TestCase):
    """検収 5: `_process_one_file` が返す RetryAction（Plan D4 の対応表）。"""

    def _headless(self, outcome, **kw):
        reporter = FakeReporter({"cust:hash": _job("cust:hash", lease_epoch=9)})
        with _patch_file_processing(process_return=outcome):
            with redirect_stdout(io.StringIO()):
                return _call_process_one_file(FakeWriter(), reporter,
                                              headless_memo={}, **kw)

    def test_all_five_headless_outcomes_are_clear(self):
        """headless が HeadlessOutcome を正常に返した以上、五態すべて CLEAR。

        backoff を重ねてはならない理由（Plan D4）:
          ESCALATED         → memo TTL 20 輪との二重抑制
          FAILED(retryable) → B4 が意図的に設けた「3 秒自癒窓」が
                              3s→30s→5min→30min→1h へ変質＝既定設計の転覆
        """
        cases = {
            "SUCCESS": main.HeadlessOutcome(main.ProcessOutcome.SUCCESS),
            "PARTIAL": main.HeadlessOutcome(main.ProcessOutcome.PARTIAL),
            "ESCALATED": main.HeadlessOutcome(main.ProcessOutcome.ESCALATED),
            "FAILED_retryable": main.HeadlessOutcome(
                main.ProcessOutcome.FAILED, retryable=True),
            "FAILED_unknown": main.HeadlessOutcome(
                main.ProcessOutcome.FAILED, retryable=False),
            "DEAD_LETTER": main.HeadlessOutcome(
                main.ProcessOutcome.DEAD_LETTER,
                dead_letter_payload={"total_pages": 1, "failed_pages": [1]}),
        }
        for label, outcome in cases.items():
            with self.subTest(outcome=label):
                self.assertIs(self._headless(outcome), main.RetryAction.CLEAR)

    def test_ui_success_clears(self):
        with _patch_file_processing(process_return=True, headless=False):
            with redirect_stdout(io.StringIO()):
                action = _call_process_one_file(FakeWriter(), None)
        self.assertIs(action, main.RetryAction.CLEAR)

    def test_ui_failure_increments(self):
        """UI 経路の失敗＝backoff が担当する唯一の正常系経路（Plan D4 #8）。"""
        with _patch_file_processing(process_return=False, headless=False):
            with redirect_stdout(io.StringIO()):
                action = _call_process_one_file(FakeWriter(), None)
        self.assertIs(action, main.RetryAction.INCREMENT)

    def test_duplicate_file_clears(self):
        """重複検出＝archive 完了＝決着（Plan D4 #4）。"""
        with _patch_file_processing(duplicate_return=True, headless=False):
            with redirect_stdout(io.StringIO()):
                action = _call_process_one_file(FakeWriter(), None)
        self.assertIs(action, main.RetryAction.CLEAR)

    def test_unsupported_extension_keeps(self):
        """未対応拡張子は KEEP——main 側の原実装も護欄の外だった（Plan D4 #5）。"""
        with _patch_file_processing(headless=False):
            with redirect_stdout(io.StringIO()):
                action = _call_process_one_file(
                    FakeWriter(), None, file={"id": "z", "name": "z.xyz"})
        self.assertIs(action, main.RetryAction.KEEP)

    def test_headless_memo_hit_keeps(self):
        """memo 命中は KEEP——memo が既に費用を止めている（Plan D4 #3）。"""
        reporter = FakeReporter({"cust:hash": _job("cust:hash", lease_epoch=9)})
        memo = {("cust:hash", 9, "f1"): {"outcome": "SUCCESS", "folder_id": "in",
                                         "expire_cycle": None}}
        with _patch_file_processing(
                process_return=main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)):
            with redirect_stdout(io.StringIO()):
                action = _call_process_one_file(FakeWriter(), reporter,
                                                headless_memo=memo, cycle=1)
        self.assertIs(action, main.RetryAction.KEEP)

    def test_customer_id_missing_keeps(self):
        """customer_id 欠落（奇形 job）は KEEP（Plan D4 #6・§12【3】で維持裁決）。

        既存契約は「控制面が job を直せば次輪で自然回復」。INCREMENT にすると
        その回復検知が最大 1 時間遅れて契約自体を変えてしまう。
        """
        reporter = FakeReporter({
            "cust:hash": _job("cust:hash", lease_epoch=9, customer_id=None)})
        with _patch_file_processing(
                process_return=main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)):
            with redirect_stdout(io.StringIO()):
                action = _call_process_one_file(FakeWriter(), reporter,
                                                headless_memo={},
                                                customer_meta_alerted={})
        self.assertIs(action, main.RetryAction.KEEP)


class ProcessOneFileCleanupTest(unittest.TestCase):
    """検収 5: 例外が抜けても一時ファイルは消える（finally 配置の固定）。"""

    def test_reporter_exception_propagates_to_the_caller(self):
        """Firestore 障害は `_process_one_file` を抜けて main() の per-file try へ。

        関数内で握り潰すと退避が働かず、無限リトライへ戻る（Plan D4/S3）。
        """
        reporter = FakeReporter({"cust:hash": _job("cust:hash", lease_epoch=9)})
        with _patch_file_processing(
                process_return=main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)):
            with patch.object(main, "_report_headless_outcome",
                              side_effect=RuntimeError("firestore down")):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(RuntimeError):
                        _call_process_one_file(FakeWriter(), reporter,
                                               headless_memo={})

    def test_temp_file_is_removed_even_when_an_exception_escapes(self):
        """per-file try を main() 側へ移したので、削除は finally でしか効かない。

        正常系にだけ削除を書くと、失敗のたびに一時ファイルが残り続ける。
        """
        reporter = FakeReporter({"cust:hash": _job("cust:hash", lease_epoch=9)})
        with _patch_file_processing(
                process_return=main.HeadlessOutcome(main.ProcessOutcome.SUCCESS),
                path_exists=True):
            with patch.object(main, "_report_headless_outcome",
                              side_effect=RuntimeError("firestore down")):
                with patch("os.remove") as rm:
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaises(RuntimeError):
                            _call_process_one_file(FakeWriter(), reporter,
                                                   headless_memo={})
        rm.assert_called_once_with("local.pdf")


if __name__ == "__main__":
    unittest.main()
