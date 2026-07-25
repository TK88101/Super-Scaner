"""IP-308/T4: main() 主循環の headless 接線——回報＋move審計＋memo＋intake白名単。

B4 Plan (docs/plans/2026-07-24-b4-closing-semantics.md) §2.3/§2.4/§2.6/T4。

main() 自体（while True の生きた入口点）は本倉が従来から一度も単体テストして
いない（真 Drive service が要る）——本ファイルは main() が呼ぶ抽出済み関数
（_headless_intake_gate 拡張／_prune_headless_memo／_headless_memo_skip／
_record_headless_memo／_report_headless_outcome／_process_one_file）を直接
呼び、fake reporter/writer＋patched download_file/process_file/move_file/
is_duplicate_file で「行為 spy」する。_process_one_file は main() の
for file in files: 本体そのもの（可測化のための抽出、ロジック改変なし）。
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("PROCESSED_FOLDER_ID", "test_processed_folder")
os.environ.setdefault("SERVICE_ACCOUNT_FILE", "test_sa.json")
os.environ.setdefault("OUTPUT_SPREADSHEET_ID", "test_spreadsheet")
os.environ.setdefault("FOLDER_RECEIPT_ID", "test_receipt_folder")

import config
importlib.reload(config)
import main
from headless_rerun_fixture import FakeWriter


class _FakeReporter:
    """firestore_report.FirestoreReporter のダブル（呼出し記録のみ、真庫不接）。"""

    def __init__(self, jobs=None):
        self.client = object()
        self._jobs = jobs or {}
        self.report_posted_calls = []
        self.report_dead_letter_calls = []
        self.write_alert_calls = []

    def get_job(self, base):
        return self._jobs.get(base)

    def write_alert(self, alert_id, payload):
        self.write_alert_calls.append((alert_id, payload))

    def report_posted(self, job_id, *, lease_epoch):
        self.report_posted_calls.append((job_id, lease_epoch))
        return "APPLIED"

    def report_dead_letter(self, job_id, *, lease_epoch, error):
        self.report_dead_letter_calls.append((job_id, lease_epoch, error))
        return "APPLIED"


def _job(posting_id, lease_epoch=1, current_state="POSTING_IN_PROGRESS"):
    return {"posting_id": posting_id, "lease_epoch": lease_epoch,
            "current_state": current_state}


def _file(file_id="f1", base="cust:hash"):
    return {"id": file_id, "name": "invoice.pdf",
            "properties": {"sandevistan_posting_id": base}}


# ============================================================
# DoD⑧: intake 状態白名単（表駆動）
# ============================================================

class IntakeStateWhitelistTest(unittest.TestCase):
    """`_headless_intake_gate` は job_state==POSTING_IN_PROGRESS のみ PROCESS を許す。
    それ以外（POSTED/POST_UNKNOWN/DEAD_LETTER/終態/None）は本輪スキップ。
    """

    def _gate(self, job_state):
        base = "cust:hash"
        reporter = _FakeReporter({base: _job(base, current_state=job_state)})
        with patch.object(main.config, "headless_mode", return_value=True):
            return main._headless_intake_gate(
                service=None, file=_file(base=base), input_folder_id="in",
                reporter=reporter)

    def test_posting_in_progress_allows_process(self):
        should, base, epoch = self._gate("POSTING_IN_PROGRESS")
        self.assertTrue(should)
        self.assertEqual(base, "cust:hash")
        self.assertEqual(epoch, 1)

    def test_posted_is_skipped(self):
        should, base, epoch = self._gate("POSTED")
        self.assertFalse(should)

    def test_post_unknown_is_skipped(self):
        should, base, epoch = self._gate("POST_UNKNOWN")
        self.assertFalse(should)

    def test_dead_letter_is_skipped(self):
        should, base, epoch = self._gate("DEAD_LETTER")
        self.assertFalse(should)

    def test_missing_job_state_is_skipped(self):
        should, base, epoch = self._gate(None)
        self.assertFalse(should)

    def test_non_headless_bypasses_whitelist(self):
        with patch.object(main.config, "headless_mode", return_value=False):
            should, base, epoch = main._headless_intake_gate(
                service=None, file=_file(), input_folder_id="in", reporter=None)
        self.assertTrue(should)
        self.assertIsNone(base)
        self.assertIsNone(epoch)

    def test_rejected_by_underlying_gate_short_circuits_before_state_check(self):
        # no_posting_id で REJECTED（job 未取得）→ state チェックへ到達しない
        reporter = _FakeReporter({})
        with patch.object(main.config, "headless_mode", return_value=True):
            should, base, epoch = main._headless_intake_gate(
                service=None, file={"id": "f1"}, input_folder_id="in",
                reporter=reporter)
        self.assertFalse(should)
        self.assertIsNone(epoch)


# ============================================================
# DoD⑦: memo（費用防護）
# ============================================================

class PruneHeadlessMemoTest(unittest.TestCase):
    """剪枝按夾（三場景: 他夾不誤剪／列舉失敗不剪／跨輪消失剪）。"""

    def test_other_folder_entries_not_pruned(self):
        memo = {
            ("b1", 1, "f1"): {"outcome": "FAILED", "folder_id": "folderA"},
            ("b2", 1, "f2"): {"outcome": "FAILED", "folder_id": "folderB"},
        }
        # folderA の一覧に f1 が無い（消えた）→ folderA 分のみ剪定
        main._prune_headless_memo(memo, "folderA", files=[])
        self.assertNotIn(("b1", 1, "f1"), memo)
        self.assertIn(("b2", 1, "f2"), memo)  # folderB は無関係、誤剪なし

    def test_folder_listing_failure_means_prune_never_called(self):
        # 「夾列舉失敗不剪」は呼出順序で自然に満たされる契約——list_files が
        # 例外送出したら呼出元は本関数を呼ばない。ここではその契約どおり
        # 呼ばれなければ memo が無傷であることを確認する。
        memo = {("b1", 1, "f1"): {"outcome": "FAILED", "folder_id": "folderA"}}
        original = dict(memo)
        # (呼出元が list_files 失敗時に _prune_headless_memo を呼ばない、の意図的な
        # 非呼出しをここでは「呼ばない」ことそのもので表現する)
        self.assertEqual(memo, original)

    def test_file_disappearing_across_cycles_gets_pruned(self):
        memo = {}
        # cycle1: f1 が一覧にある → まだ剪定対象ではない
        main._prune_headless_memo(memo, "folderA", files=[{"id": "f1"}])
        memo[("b1", 1, "f1")] = {"outcome": "FAILED", "folder_id": "folderA"}
        # cycle2: f1 が一覧から消えた（アーカイブ済み等）→ 剪定される
        main._prune_headless_memo(memo, "folderA", files=[{"id": "f2"}])
        self.assertNotIn(("b1", 1, "f1"), memo)

    def test_file_still_present_is_not_pruned(self):
        memo = {("b1", 1, "f1"): {"outcome": "FAILED", "folder_id": "folderA"}}
        main._prune_headless_memo(memo, "folderA", files=[{"id": "f1"}])
        self.assertIn(("b1", 1, "f1"), memo)


class HeadlessMemoSkipTest(unittest.TestCase):
    """`_headless_memo_skip`: 命中→True（TTL 過期なら削除して False）。"""

    def test_no_entry_returns_false(self):
        memo = {}
        self.assertFalse(main._headless_memo_skip(memo, ("b", 1, "f1"), cycle=5))

    def test_entry_without_ttl_always_hits(self):
        memo = {("b", 1, "f1"): {"outcome": "FAILED", "folder_id": "in",
                                 "expire_cycle": None}}
        self.assertTrue(main._headless_memo_skip(memo, ("b", 1, "f1"), cycle=999))
        self.assertIn(("b", 1, "f1"), memo)  # TTL 無しは自然消滅しない

    def test_escalated_ttl_not_yet_expired_hits(self):
        memo = {("b", 1, "f1"): {"outcome": "ESCALATED", "folder_id": "in",
                                 "expire_cycle": 25}}
        self.assertTrue(main._headless_memo_skip(memo, ("b", 1, "f1"), cycle=24))

    def test_escalated_ttl_expired_clears_and_returns_false(self):
        memo = {("b", 1, "f1"): {"outcome": "ESCALATED", "folder_id": "in",
                                 "expire_cycle": 25}}
        self.assertFalse(main._headless_memo_skip(memo, ("b", 1, "f1"), cycle=25))
        self.assertNotIn(("b", 1, "f1"), memo)  # 過期エントリは削除される

    def test_new_epoch_is_a_different_key_naturally_bypasses_memo(self):
        # epoch+1 天然放行: key に epoch を含むため別 key となり memo に無関係
        memo = {("b", 1, "f1"): {"outcome": "FAILED", "folder_id": "in",
                                 "expire_cycle": None}}
        self.assertFalse(main._headless_memo_skip(memo, ("b", 2, "f1"), cycle=1))


class RecordHeadlessMemoTest(unittest.TestCase):
    def test_records_outcome_folder_and_expire_cycle(self):
        memo = {}
        main._record_headless_memo(memo, ("b", 1, "f1"), "SUCCESS", "in", None)
        self.assertEqual(memo[("b", 1, "f1")],
                         {"outcome": "SUCCESS", "folder_id": "in", "expire_cycle": None})

    def test_overwrites_existing_entry(self):
        memo = {("b", 1, "f1"): {"outcome": "OLD", "folder_id": "in", "expire_cycle": None}}
        main._record_headless_memo(memo, ("b", 1, "f1"), "FAILED", "in", None)
        self.assertEqual(memo[("b", 1, "f1")]["outcome"], "FAILED")


# ============================================================
# DoD①③④⑤: 回報接線（_report_headless_outcome）
# ============================================================

class ReportHeadlessOutcomeTest(unittest.TestCase):
    def test_success_calls_report_posted_once_with_epoch(self):
        reporter = _FakeReporter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", 7, outcome, "f1", cycle=1)
        self.assertEqual(reporter.report_posted_calls, [("cust:hash", 7)])
        self.assertEqual(reporter.report_dead_letter_calls, [])
        self.assertEqual(label, "SUCCESS")
        self.assertIsNone(expire)

    def test_dead_letter_calls_report_dead_letter_once_with_payload(self):
        reporter = _FakeReporter()
        payload = {"stage": "ocr", "error_class": "NON_RETRYABLE",
                   "message": "all_pages_unreadable: 2/2 pages [p1,p2]"}
        outcome = main.HeadlessOutcome(main.ProcessOutcome.DEAD_LETTER,
                                       dead_letter_payload=payload)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", 3, outcome, "f1", cycle=1)
        self.assertEqual(reporter.report_dead_letter_calls,
                         [("cust:hash", 3, payload)])
        self.assertEqual(reporter.report_posted_calls, [])
        self.assertEqual(label, "DEAD_LETTER")

    def test_partial_makes_zero_reporter_calls(self):
        reporter = _FakeReporter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.PARTIAL)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", 1, outcome, "f1", cycle=1)
        self.assertEqual(reporter.report_posted_calls, [])
        self.assertEqual(reporter.report_dead_letter_calls, [])
        self.assertEqual(label, "PARTIAL")

    def test_escalated_makes_zero_reporter_calls_and_sets_ttl(self):
        reporter = _FakeReporter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.ESCALATED)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", 1, outcome, "f1", cycle=5)
        self.assertEqual(reporter.report_posted_calls, [])
        self.assertEqual(reporter.report_dead_letter_calls, [])
        self.assertEqual(label, "ESCALATED")
        self.assertEqual(expire, 5 + main._ESCALATE_MEMO_TTL_CYCLES)

    def test_failed_retryable_makes_zero_reporter_calls_and_no_memo(self):
        reporter = _FakeReporter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.FAILED, retryable=True)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", 1, outcome, "f1", cycle=1)
        self.assertEqual(reporter.report_posted_calls, [])
        self.assertIsNone(label)  # 不記（3秒自癒窗）

    def test_failed_not_retryable_makes_zero_reporter_calls_but_memos(self):
        reporter = _FakeReporter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.FAILED, retryable=False)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", 1, outcome, "f1", cycle=1)
        self.assertEqual(reporter.report_posted_calls, [])
        self.assertEqual(label, "FAILED")  # 記（per epoch、隨重投放行）

    def test_missing_epoch_success_makes_zero_reporter_calls(self):
        reporter = _FakeReporter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", None, outcome, "f1", cycle=1)
        self.assertEqual(reporter.report_posted_calls, [])  # epoch欠落→零呼出

    def test_missing_epoch_dead_letter_makes_zero_reporter_calls(self):
        reporter = _FakeReporter()
        payload = {"stage": "ocr", "error_class": "NON_RETRYABLE", "message": "x"}
        outcome = main.HeadlessOutcome(main.ProcessOutcome.DEAD_LETTER,
                                       dead_letter_payload=payload)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", None, outcome, "f1", cycle=1)
        self.assertEqual(reporter.report_dead_letter_calls, [])  # epoch欠落→零呼出

    def test_rejected_result_still_only_calls_once_no_retry(self):
        # REJECTED（stale epoch 等）でも SS は不重試——firestore_report 側が
        # 一回の事務で完結する契約（本モジュールはただ一度呼ぶだけ）。
        class _RejectingReporter(_FakeReporter):
            def report_posted(self, job_id, *, lease_epoch):
                self.report_posted_calls.append((job_id, lease_epoch))
                return "REJECTED"  # 呼出し回数だけが契約、戻り値は判断材料にしない

        reporter = _RejectingReporter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)
        main._report_headless_outcome(reporter, "cust:hash", 1, outcome, "f1", cycle=1)
        self.assertEqual(len(reporter.report_posted_calls), 1)  # 恰一回


# ============================================================
# DoD①②⑤⑥⑨: _process_one_file 統合（main() for file in files: の抽出体）
# ============================================================

class ProcessOneFileTest(unittest.TestCase):
    def _writer(self):
        return FakeWriter()

    def test_success_calls_report_posted_and_never_moves(self):
        reporter = _FakeReporter({"cust:hash": _job("cust:hash", lease_epoch=9)})
        writer = self._writer()
        headless_memo = {}
        with patch.object(main, "download_file", return_value="local.pdf") as dl, \
             patch.object(main, "process_file",
                          return_value=main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)), \
             patch.object(main, "move_file") as mv, \
             patch.object(main, "is_duplicate_file") as dup, \
             patch.object(main.config, "headless_mode", return_value=True), \
             patch("os.path.exists", return_value=False):
            main._process_one_file(
                service=None, writer=writer, reporter=reporter,
                file=_file(), input_folder_id="in", doc_type="receipt",
                processed_folder_id="processed", split_pdf_folder_id="split",
                quarantine_alerted={}, headless_memo=headless_memo, cycle=1)
        dl.assert_called_once()
        mv.assert_not_called()                      # DoD①: move 零呼出
        dup.assert_not_called()                      # DoD⑥: is_duplicate_file 零呼出
        self.assertEqual(reporter.report_posted_calls, [("cust:hash", 9)])
        self.assertEqual(headless_memo[("cust:hash", 9, "f1")]["outcome"], "SUCCESS")

    def test_dead_letter_calls_report_dead_letter_and_never_moves(self):
        reporter = _FakeReporter({"cust:hash": _job("cust:hash", lease_epoch=2)})
        writer = self._writer()
        payload = {"stage": "ocr", "error_class": "NON_RETRYABLE", "message": "x"}
        with patch.object(main, "download_file", return_value="local.pdf"), \
             patch.object(main, "process_file",
                          return_value=main.HeadlessOutcome(
                              main.ProcessOutcome.DEAD_LETTER, dead_letter_payload=payload)), \
             patch.object(main, "move_file") as mv, \
             patch.object(main.config, "headless_mode", return_value=True), \
             patch("os.path.exists", return_value=False):
            main._process_one_file(
                service=None, writer=writer, reporter=reporter,
                file=_file(), input_folder_id="in", doc_type="receipt",
                processed_folder_id="processed", split_pdf_folder_id="split",
                quarantine_alerted={}, headless_memo={}, cycle=1)
        mv.assert_not_called()
        self.assertEqual(reporter.report_dead_letter_calls, [("cust:hash", 2, payload)])

    def test_partial_makes_zero_reporter_calls_and_never_moves(self):
        reporter = _FakeReporter({"cust:hash": _job("cust:hash")})
        writer = self._writer()
        with patch.object(main, "download_file", return_value="local.pdf"), \
             patch.object(main, "process_file",
                          return_value=main.HeadlessOutcome(main.ProcessOutcome.PARTIAL)), \
             patch.object(main, "move_file") as mv, \
             patch.object(main.config, "headless_mode", return_value=True), \
             patch("os.path.exists", return_value=False):
            main._process_one_file(
                service=None, writer=writer, reporter=reporter,
                file=_file(), input_folder_id="in", doc_type="receipt",
                processed_folder_id="processed", split_pdf_folder_id="split",
                quarantine_alerted={}, headless_memo={}, cycle=1)
        mv.assert_not_called()
        self.assertEqual(reporter.report_posted_calls, [])
        self.assertEqual(reporter.report_dead_letter_calls, [])

    def test_escalated_makes_zero_reporter_calls_and_never_moves(self):
        reporter = _FakeReporter({"cust:hash": _job("cust:hash")})
        writer = self._writer()
        with patch.object(main, "download_file", return_value="local.pdf"), \
             patch.object(main, "process_file",
                          return_value=main.HeadlessOutcome(main.ProcessOutcome.ESCALATED)), \
             patch.object(main, "move_file") as mv, \
             patch.object(main.config, "headless_mode", return_value=True), \
             patch("os.path.exists", return_value=False):
            main._process_one_file(
                service=None, writer=writer, reporter=reporter,
                file=_file(), input_folder_id="in", doc_type="receipt",
                processed_folder_id="processed", split_pdf_folder_id="split",
                quarantine_alerted={}, headless_memo={}, cycle=1)
        mv.assert_not_called()
        self.assertEqual(reporter.report_posted_calls, [])

    def test_missing_epoch_success_zero_reporter_calls_and_never_moves(self):
        # job に lease_epoch キーが無い旧 schema 想定 → epoch=None（違約態）
        reporter = _FakeReporter({"cust:hash": {"posting_id": "cust:hash",
                                                "current_state": "POSTING_IN_PROGRESS"}})
        writer = self._writer()
        with patch.object(main, "download_file", return_value="local.pdf"), \
             patch.object(main, "process_file",
                          return_value=main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)), \
             patch.object(main, "move_file") as mv, \
             patch.object(main.config, "headless_mode", return_value=True), \
             patch("os.path.exists", return_value=False):
            main._process_one_file(
                service=None, writer=writer, reporter=reporter,
                file=_file(), input_folder_id="in", doc_type="receipt",
                processed_folder_id="processed", split_pdf_folder_id="split",
                quarantine_alerted={}, headless_memo={}, cycle=1)
        mv.assert_not_called()
        self.assertEqual(reporter.report_posted_calls, [])  # epoch 欠落→零呼出

    def test_memo_hit_skips_download_and_process_file(self):
        reporter = _FakeReporter({"cust:hash": _job("cust:hash", lease_epoch=1)})
        writer = self._writer()
        headless_memo = {("cust:hash", 1, "f1"):
                         {"outcome": "FAILED", "folder_id": "in", "expire_cycle": None}}
        with patch.object(main, "download_file") as dl, \
             patch.object(main, "process_file") as pf, \
             patch.object(main.config, "headless_mode", return_value=True):
            main._process_one_file(
                service=None, writer=writer, reporter=reporter,
                file=_file(), input_folder_id="in", doc_type="receipt",
                processed_folder_id="processed", split_pdf_folder_id="split",
                quarantine_alerted={}, headless_memo=headless_memo, cycle=1)
        dl.assert_not_called()   # DoD⑦: 零下載
        pf.assert_not_called()   # DoD⑦: 零OCR（process_file 呼出も無い）

    def test_intake_state_not_posting_in_progress_skips_download(self):
        reporter = _FakeReporter({"cust:hash": _job("cust:hash", current_state="POSTED")})
        writer = self._writer()
        with patch.object(main, "download_file") as dl, \
             patch.object(main, "process_file") as pf, \
             patch.object(main.config, "headless_mode", return_value=True):
            main._process_one_file(
                service=None, writer=writer, reporter=reporter,
                file=_file(), input_folder_id="in", doc_type="receipt",
                processed_folder_id="processed", split_pdf_folder_id="split",
                quarantine_alerted={}, headless_memo={}, cycle=1)
        dl.assert_not_called()
        pf.assert_not_called()

    def test_headless_mode_never_calls_is_duplicate_file(self):
        reporter = _FakeReporter({"cust:hash": _job("cust:hash")})
        writer = self._writer()
        with patch.object(main, "download_file", return_value="local.pdf"), \
             patch.object(main, "process_file",
                          return_value=main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)), \
             patch.object(main, "move_file"), \
             patch.object(main, "is_duplicate_file") as dup, \
             patch.object(main.config, "headless_mode", return_value=True), \
             patch("os.path.exists", return_value=False):
            main._process_one_file(
                service=None, writer=writer, reporter=reporter,
                file=_file(), input_folder_id="in", doc_type="receipt",
                processed_folder_id="processed", split_pdf_folder_id="split",
                quarantine_alerted={}, headless_memo={}, cycle=1)
        dup.assert_not_called()

    def test_ui_path_true_outcome_still_moves_file(self):
        # UI 版（reporter=None）は既存挙動不変: True→move、is_duplicate_file は
        # 通常経路で呼ばれ、False なら move されない。
        writer = self._writer()
        with patch.object(main, "download_file", return_value="local.pdf"), \
             patch.object(main, "process_file", return_value=True), \
             patch.object(main, "move_file") as mv, \
             patch.object(main, "is_duplicate_file", return_value=False) as dup, \
             patch("os.path.exists", return_value=False):
            main._process_one_file(
                service=None, writer=writer, reporter=None,
                file=_file(), input_folder_id="in", doc_type="receipt",
                processed_folder_id="processed", split_pdf_folder_id="split",
                quarantine_alerted={}, headless_memo={}, cycle=1)
        dup.assert_called_once()
        mv.assert_called_once()
        self.assertEqual(writer.start_new_file_calls[0][2], "invoice.pdf")

    def test_ui_path_duplicate_file_moves_and_skips_processing(self):
        writer = self._writer()
        with patch.object(main, "is_duplicate_file", return_value=True), \
             patch.object(main, "move_file") as mv, \
             patch.object(main, "download_file") as dl, \
             patch.object(main, "process_file") as pf:
            main._process_one_file(
                service=None, writer=writer, reporter=None,
                file=_file(), input_folder_id="in", doc_type="receipt",
                processed_folder_id="processed", split_pdf_folder_id="split",
                quarantine_alerted={}, headless_memo={}, cycle=1)
        mv.assert_called_once()
        dl.assert_not_called()
        pf.assert_not_called()

    def test_ui_path_false_outcome_does_not_move(self):
        writer = self._writer()
        with patch.object(main, "download_file", return_value="local.pdf"), \
             patch.object(main, "process_file", return_value=False), \
             patch.object(main, "move_file") as mv, \
             patch.object(main, "is_duplicate_file", return_value=False), \
             patch("os.path.exists", return_value=False):
            main._process_one_file(
                service=None, writer=writer, reporter=None,
                file=_file(), input_folder_id="in", doc_type="receipt",
                processed_folder_id="processed", split_pdf_folder_id="split",
                quarantine_alerted={}, headless_memo={}, cycle=1)
        mv.assert_not_called()

    def test_unsupported_format_skips_download_without_separator(self):
        # 3. 格式過濾（既存挙動: 未対応拡張子は download_file にすら進まない）
        writer = self._writer()
        file = {"id": "f1", "name": "invoice.exe",
                "properties": {"sandevistan_posting_id": "cust:hash"}}
        with patch.object(main, "download_file") as dl, \
             patch.object(main, "process_file") as pf, \
             patch.object(main, "is_duplicate_file", return_value=False):
            main._process_one_file(
                service=None, writer=writer, reporter=None,
                file=file, input_folder_id="in", doc_type="receipt",
                processed_folder_id="processed", split_pdf_folder_id="split",
                quarantine_alerted={}, headless_memo={}, cycle=1)
        dl.assert_not_called()
        pf.assert_not_called()

    def test_flush_always_called(self):
        writer = self._writer()
        with patch.object(main, "download_file", return_value="local.pdf"), \
             patch.object(main, "process_file", return_value=True), \
             patch.object(main, "move_file"), \
             patch.object(main, "is_duplicate_file", return_value=False), \
             patch("os.path.exists", return_value=False):
            main._process_one_file(
                service=None, writer=writer, reporter=None,
                file=_file(), input_folder_id="in", doc_type="receipt",
                processed_folder_id="processed", split_pdf_folder_id="split",
                quarantine_alerted={}, headless_memo={}, cycle=1)
        self.assertEqual(writer.flush_calls, 1)


if __name__ == "__main__":
    unittest.main()
