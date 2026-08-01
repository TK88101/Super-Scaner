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
import io
import os
import sys
import types
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
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


def _job(posting_id, lease_epoch=1, current_state="POSTING_IN_PROGRESS",
        customer_id="20220401",
        customer_label="20220401　株式会社緒方材木店"):
    """既定で customer_id/customer_label を有効な組合せにしておく
    （§5.1-d T4）——本ファイルの大半のテストは tab_owner 解決とは無関係の
    関心事（memo/回報/白名単）なので、customer_metadata_missing の
    処理スキップ短絡に巻き込まれないようにする。customer_id/label 自体を
    検証したいテストは引数で上書きする（CustomerTabOwnerResolutionTest 参照）。
    """
    return {"posting_id": posting_id, "lease_epoch": lease_epoch,
            "current_state": current_state, "customer_id": customer_id,
            "customer_label": customer_label}


def _file(file_id="f1", base="cust:hash"):
    return {"id": file_id, "name": "invoice.pdf",
            "properties": {"sandevistan_posting_id": base}}


def _call_process_one_file(writer, job_reporter, *, file=None, headless_memo=None,
                           intake_state_memo=None, cycle=1,
                           customer_meta_alerted=None):
    """main._process_one_file の呼出し足場（ProcessOneFileTest と
    IntakeStatePreGateMemoTest で共用、simcodex Round 2 #11 と同型の DRY）。"""
    main._process_one_file(
        service=None, writer=writer, job_reporter=job_reporter,
        file=file if file is not None else _file(), input_folder_id="in",
        doc_type="receipt", processed_folder_id="processed",
        split_pdf_folder_id="split", quarantine_alerted={},
        headless_memo=headless_memo if headless_memo is not None else {},
        intake_state_memo=(
            intake_state_memo if intake_state_memo is not None else {}),
        cycle=cycle,
        customer_meta_alerted=customer_meta_alerted)


# ============================================================
# DoD⑧: intake 状態白名単（表駆動）
# ============================================================

class IntakeStateWhitelistTest(unittest.TestCase):
    """`_headless_intake_gate` は job_state==POSTING_IN_PROGRESS のみ PROCESS を許す。
    それ以外（POSTED/POST_UNKNOWN/DEAD_LETTER/終態/None）は本輪スキップ。
    """

    def _gate(self, job_state):
        base = "cust:hash"
        reporter = FakeReporter({base: _job(base, current_state=job_state)})
        with patch.object(main.config, "headless_mode", return_value=True):
            return main._headless_intake_gate(
                service=None, file=_file(base=base), input_folder_id="in",
                reporter=reporter)

    def test_posting_in_progress_allows_process(self):
        should, base, epoch, state_rejected, cust_id, cust_label = self._gate(
            "POSTING_IN_PROGRESS")
        self.assertTrue(should)
        self.assertEqual(base, "cust:hash")
        self.assertEqual(epoch, 1)
        self.assertFalse(state_rejected)
        self.assertEqual(cust_id, "20220401")
        self.assertEqual(cust_label, "20220401　株式会社緒方材木店")

    def test_posted_is_skipped(self):
        should, base, epoch, state_rejected, _, _ = self._gate("POSTED")
        self.assertFalse(should)
        self.assertTrue(state_rejected)  # simcodex Round 2 #2: pre-gate memo 対象

    def test_post_unknown_is_skipped(self):
        should, base, epoch, state_rejected, _, _ = self._gate("POST_UNKNOWN")
        self.assertFalse(should)
        self.assertTrue(state_rejected)

    def test_dead_letter_is_skipped(self):
        should, base, epoch, state_rejected, _, _ = self._gate("DEAD_LETTER")
        self.assertFalse(should)
        self.assertTrue(state_rejected)

    def test_missing_job_state_is_skipped(self):
        should, base, epoch, state_rejected, _, _ = self._gate(None)
        self.assertFalse(should)
        self.assertTrue(state_rejected)

    def test_non_headless_bypasses_whitelist(self):
        with patch.object(main.config, "headless_mode", return_value=False):
            should, base, epoch, state_rejected, cust_id, cust_label = (
                main._headless_intake_gate(
                    service=None, file=_file(), input_folder_id="in",
                    reporter=None))
        self.assertTrue(should)
        self.assertIsNone(base)
        self.assertIsNone(epoch)
        self.assertFalse(state_rejected)
        self.assertIsNone(cust_id)
        self.assertIsNone(cust_label)

    def test_rejected_by_underlying_gate_short_circuits_before_state_check(self):
        # no_posting_id で REJECTED（job 未取得）→ state チェックへ到達しない
        reporter = FakeReporter({})
        with patch.object(main.config, "headless_mode", return_value=True):
            should, base, epoch, state_rejected, cust_id, cust_label = (
                main._headless_intake_gate(
                    service=None, file={"id": "f1"}, input_folder_id="in",
                    reporter=reporter))
        self.assertFalse(should)
        self.assertIsNone(epoch)
        self.assertFalse(state_rejected)  # 五分岐 REJECTED、state 白名単には未到達
        self.assertIsNone(cust_id)
        self.assertIsNone(cust_label)


class IntakeStatePreGateMemoTest(unittest.TestCase):
    """simcodex Round 2 #2: 状態非対象スキップの pre-gate memo（TTL 付き）。

    終態 job（POSTED 等）が継続的にスキップされる際、intake gate（Firestore
    get_job 読取）自体を TTL 内は省く——無界の毎輪 get_job 連打を防ぐ。
    """

    def _reporter_with_state(self, state):
        return FakeReporter({"cust:hash": _job("cust:hash", current_state=state)})

    def test_second_cycle_within_ttl_makes_zero_get_job_calls(self):
        reporter = self._reporter_with_state("POSTED")
        writer = FakeWriter()
        intake_state_memo = {}
        with patch.object(reporter, "get_job", wraps=reporter.get_job) as spy, \
             patch.object(main.config, "headless_mode", return_value=True):
            _call_process_one_file(writer, reporter,
                                   intake_state_memo=intake_state_memo, cycle=1)
            self.assertEqual(spy.call_count, 1)
            self.assertIn(("cust:hash", "f1"), intake_state_memo)

            _call_process_one_file(writer, reporter,
                                   intake_state_memo=intake_state_memo, cycle=2)
            self.assertEqual(spy.call_count, 1)  # 二輪目は get_job 呼ばれない（DoD⑦）

    def test_ttl_expiry_triggers_requery(self):
        reporter = self._reporter_with_state("POSTED")
        writer = FakeWriter()
        intake_state_memo = {}
        with patch.object(reporter, "get_job", wraps=reporter.get_job) as spy, \
             patch.object(main.config, "headless_mode", return_value=True):
            _call_process_one_file(writer, reporter,
                                   intake_state_memo=intake_state_memo, cycle=1)
            expire_cycle = intake_state_memo[("cust:hash", "f1")]["expire_cycle"]
            self.assertEqual(expire_cycle, 1 + main._ESCALATE_MEMO_TTL_CYCLES)
            self.assertEqual(spy.call_count, 1)

            _call_process_one_file(writer, reporter,
                                   intake_state_memo=intake_state_memo,
                                   cycle=expire_cycle)
            self.assertEqual(spy.call_count, 2)  # TTL 過期→再照会

    def test_state_change_becomes_visible_only_after_ttl(self):
        # POSTED（スキップ・memo 記録）→ 控制面が重投して POSTING_IN_PROGRESS
        # へ戻すが、TTL 内は古い memo が優先されスキップされ続ける。TTL 過期後
        # に初めて新しい状態が見え、処理が進む。
        reporter = self._reporter_with_state("POSTED")
        writer = FakeWriter()
        intake_state_memo = {}
        with patch.object(main.config, "headless_mode", return_value=True):
            _call_process_one_file(writer, reporter,
                                   intake_state_memo=intake_state_memo, cycle=1)
            expire_cycle = intake_state_memo[("cust:hash", "f1")]["expire_cycle"]

            reporter._jobs["cust:hash"]["current_state"] = "POSTING_IN_PROGRESS"

            # TTL 内（cycle < expire_cycle）: 状態変化はまだ見えない
            with patch.object(main, "download_file") as dl, \
                 patch.object(main, "process_file") as pf:
                _call_process_one_file(
                    writer, reporter, intake_state_memo=intake_state_memo,
                    cycle=expire_cycle - 1)
                dl.assert_not_called()
                pf.assert_not_called()

            # TTL 過期後（cycle >= expire_cycle）: 新状態が見え、処理が進む
            with patch.object(main, "download_file",
                              return_value="local.pdf") as dl, \
                 patch.object(main, "process_file",
                              return_value=main.HeadlessOutcome(
                                  main.ProcessOutcome.SUCCESS)), \
                 patch.object(main, "move_file"), \
                 patch("os.path.exists", return_value=False):
                _call_process_one_file(
                    writer, reporter, intake_state_memo=intake_state_memo,
                    cycle=expire_cycle)
                dl.assert_called_once()


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


class PruneIntakeStateMemoTest(unittest.TestCase):
    """simcodex Round 3 P1: `_prune_headless_memo` の file_id_index=1 分岐
    （intake_state_memo の 2-tuple 鍵形 (base, file_id)、生産呼出しは main.py の
    `_prune_headless_memo(intake_state_memo, input_folder_id, files, file_id_index=1)`
    付近）が全倉未検証だった穴を埋める。PruneHeadlessMemoTest（3-tuple 鍵、
    file_id_index 既定 2）と同型の三場景を 2-tuple 鍵形で鏡像する——鍵形
    リファクタや file_id_index 書き間違いを検知できるようにする。
    """

    def test_other_folder_entries_not_pruned(self):
        memo = {
            ("b1", "f1"): {"outcome": "STATE_NOT_ALLOWED", "folder_id": "folderA"},
            ("b2", "f2"): {"outcome": "STATE_NOT_ALLOWED", "folder_id": "folderB"},
        }
        # folderA の一覧に f1 が無い（消えた）→ folderA 分のみ剪定
        main._prune_headless_memo(memo, "folderA", files=[], file_id_index=1)
        self.assertNotIn(("b1", "f1"), memo)
        self.assertIn(("b2", "f2"), memo)  # folderB は無関係、誤剪なし

    def test_folder_listing_failure_means_prune_never_called(self):
        # 「夾列舉失敗不剪」は呼出順序で自然に満たされる契約——list_files が
        # 例外送出したら呼出元は本関数を呼ばない。呼ばれなければ memo は無傷。
        memo = {("b1", "f1"): {"outcome": "STATE_NOT_ALLOWED", "folder_id": "folderA"}}
        original = dict(memo)
        self.assertEqual(memo, original)

    def test_file_disappearing_across_cycles_gets_pruned(self):
        memo = {}
        # cycle1: f1 が一覧にある → まだ剪定対象ではない
        main._prune_headless_memo(memo, "folderA", files=[{"id": "f1"}],
                                  file_id_index=1)
        memo[("b1", "f1")] = {"outcome": "STATE_NOT_ALLOWED", "folder_id": "folderA"}
        # cycle2: f1 が一覧から消えた（アーカイブ済み等）→ 剪定される
        main._prune_headless_memo(memo, "folderA", files=[{"id": "f2"}],
                                  file_id_index=1)
        self.assertNotIn(("b1", "f1"), memo)

    def test_file_still_present_is_not_pruned(self):
        memo = {("b1", "f1"): {"outcome": "STATE_NOT_ALLOWED", "folder_id": "folderA"}}
        main._prune_headless_memo(memo, "folderA", files=[{"id": "f1"}],
                                  file_id_index=1)
        self.assertIn(("b1", "f1"), memo)

    def test_wrong_default_index_would_misbehave_on_two_tuple_key(self):
        # file_id_index を渡し忘れる（既定 2）と 2-tuple 鍵で IndexError になる
        # ことを固定——将来の呼出し側リファクタで既定値のまま呼んでしまう
        # 事故（本 P1 の懸念そのもの）を早期に検知する回帰ガード。
        memo = {("b1", "f1"): {"outcome": "STATE_NOT_ALLOWED", "folder_id": "folderA"}}
        with self.assertRaises(IndexError):
            main._prune_headless_memo(memo, "folderA", files=[])


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
        reporter = FakeReporter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", 7, outcome, "f1", cycle=1)
        self.assertEqual(reporter.report_posted_calls, [("cust:hash", 7)])
        self.assertEqual(reporter.report_dead_letter_calls, [])
        self.assertEqual(label, "SUCCESS")
        self.assertIsNone(expire)

    def test_dead_letter_calls_report_dead_letter_once_with_payload(self):
        reporter = FakeReporter()
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
        reporter = FakeReporter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.PARTIAL)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", 1, outcome, "f1", cycle=1)
        self.assertEqual(reporter.report_posted_calls, [])
        self.assertEqual(reporter.report_dead_letter_calls, [])
        self.assertEqual(label, "PARTIAL")

    def test_escalated_makes_zero_reporter_calls_and_sets_ttl(self):
        reporter = FakeReporter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.ESCALATED)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", 1, outcome, "f1", cycle=5)
        self.assertEqual(reporter.report_posted_calls, [])
        self.assertEqual(reporter.report_dead_letter_calls, [])
        self.assertEqual(label, "ESCALATED")
        self.assertEqual(expire, 5 + main._ESCALATE_MEMO_TTL_CYCLES)

    def test_failed_retryable_makes_zero_reporter_calls_and_no_memo(self):
        reporter = FakeReporter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.FAILED, retryable=True)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", 1, outcome, "f1", cycle=1)
        self.assertEqual(reporter.report_posted_calls, [])
        self.assertIsNone(label)  # 不記（3秒自癒窗）

    def test_failed_not_retryable_makes_zero_reporter_calls_but_memos(self):
        reporter = FakeReporter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.FAILED, retryable=False)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", 1, outcome, "f1", cycle=1)
        self.assertEqual(reporter.report_posted_calls, [])
        self.assertEqual(label, "FAILED")  # 記（per epoch、隨重投放行）

    def test_missing_epoch_success_makes_zero_reporter_calls(self):
        reporter = FakeReporter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", None, outcome, "f1", cycle=1)
        self.assertEqual(reporter.report_posted_calls, [])  # epoch欠落→零呼出
        # simcodex Round 2 #1: 実際には報告していないので "SUCCESS" と偽記しない
        self.assertEqual(label, main._MEMO_LABEL_EPOCH_MISSING)
        self.assertNotEqual(label, "SUCCESS")

    def test_missing_epoch_dead_letter_makes_zero_reporter_calls(self):
        reporter = FakeReporter()
        payload = {"stage": "ocr", "error_class": "NON_RETRYABLE", "message": "x"}
        outcome = main.HeadlessOutcome(main.ProcessOutcome.DEAD_LETTER,
                                       dead_letter_payload=payload)
        label, expire = main._report_headless_outcome(
            reporter, "cust:hash", None, outcome, "f1", cycle=1)
        self.assertEqual(reporter.report_dead_letter_calls, [])  # epoch欠落→零呼出
        self.assertEqual(label, main._MEMO_LABEL_EPOCH_MISSING)
        self.assertNotEqual(label, "DEAD_LETTER")

    def test_rejected_result_still_only_calls_once_no_retry(self):
        # REJECTED（stale epoch 等）でも SS は不重試——firestore_report 側が
        # 一回の事務で完結する契約（本モジュールはただ一度呼ぶだけ）。
        class _RejectingReporter(FakeReporter):
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

@contextmanager
def _patch_file_processing(*, process_return=True, download_return="local.pdf",
                           duplicate_return=False, headless=True, path_exists=False):
    """ProcessOneFileTest 共用の patch 足場（simcodex Round 1 #11、12 メソッド
    近同 5 行複製の集約）。download_file/process_file/move_file/
    is_duplicate_file/os.path.exists を一括 patch し、Mock を
    `types.SimpleNamespace` で返す（各テストは戻り値の属性で呼出しを検証）。
    headless=True なら config.headless_mode も True に固定する
    （UI 経路テストは headless=False で main.config 自体を触らない）。
    """
    with ExitStack() as stack:
        dl = stack.enter_context(
            patch.object(main, "download_file", return_value=download_return))
        pf = stack.enter_context(
            patch.object(main, "process_file", return_value=process_return))
        mv = stack.enter_context(patch.object(main, "move_file"))
        dup = stack.enter_context(
            patch.object(main, "is_duplicate_file", return_value=duplicate_return))
        stack.enter_context(patch("os.path.exists", return_value=path_exists))
        if headless:
            stack.enter_context(
                patch.object(main.config, "headless_mode", return_value=True))
        yield types.SimpleNamespace(
            download_file=dl, process_file=pf, move_file=mv, is_duplicate_file=dup)


class ProcessOneFileTest(unittest.TestCase):
    def _writer(self):
        return FakeWriter()

    def _call(self, writer, reporter, *, file=None, headless_memo=None,
             intake_state_memo=None, cycle=1):
        _call_process_one_file(
            writer, reporter, file=file, headless_memo=headless_memo,
            intake_state_memo=intake_state_memo, cycle=cycle)

    def test_success_calls_report_posted_and_never_moves(self):
        reporter = FakeReporter({"cust:hash": _job("cust:hash", lease_epoch=9)})
        writer = self._writer()
        headless_memo = {}
        with _patch_file_processing(
                process_return=main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)) as p:
            self._call(writer, reporter, headless_memo=headless_memo)
        p.download_file.assert_called_once()
        p.move_file.assert_not_called()               # DoD①: move 零呼出
        p.is_duplicate_file.assert_not_called()        # DoD⑥: is_duplicate_file 零呼出
        self.assertEqual(reporter.report_posted_calls, [("cust:hash", 9)])
        self.assertEqual(headless_memo[("cust:hash", 9, "f1")]["outcome"], "SUCCESS")

    def test_dead_letter_calls_report_dead_letter_and_never_moves(self):
        reporter = FakeReporter({"cust:hash": _job("cust:hash", lease_epoch=2)})
        writer = self._writer()
        payload = {"stage": "ocr", "error_class": "NON_RETRYABLE", "message": "x"}
        outcome = main.HeadlessOutcome(
            main.ProcessOutcome.DEAD_LETTER, dead_letter_payload=payload)
        with _patch_file_processing(process_return=outcome) as p:
            self._call(writer, reporter)
        p.move_file.assert_not_called()
        self.assertEqual(reporter.report_dead_letter_calls, [("cust:hash", 2, payload)])

    def test_partial_makes_zero_reporter_calls_and_never_moves(self):
        reporter = FakeReporter({"cust:hash": _job("cust:hash")})
        writer = self._writer()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.PARTIAL)
        with _patch_file_processing(process_return=outcome) as p:
            self._call(writer, reporter)
        p.move_file.assert_not_called()
        self.assertEqual(reporter.report_posted_calls, [])
        self.assertEqual(reporter.report_dead_letter_calls, [])

    def test_escalated_makes_zero_reporter_calls_and_never_moves(self):
        reporter = FakeReporter({"cust:hash": _job("cust:hash")})
        writer = self._writer()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.ESCALATED)
        with _patch_file_processing(process_return=outcome) as p:
            self._call(writer, reporter)
        p.move_file.assert_not_called()
        self.assertEqual(reporter.report_posted_calls, [])

    def test_missing_epoch_success_zero_reporter_calls_and_never_moves(self):
        # job に lease_epoch キーが無い旧 schema 想定 → epoch=None（違約態）。
        # customer_id は本テストの関心事ではないため単独形で満たしておく
        # （§5.1-d T4 の customer_metadata_missing 短絡に巻き込まれない）。
        reporter = FakeReporter({"cust:hash": {"posting_id": "cust:hash",
                                                "current_state": "POSTING_IN_PROGRESS",
                                                "customer_id": "20220401"}})
        writer = self._writer()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)
        headless_memo = {}
        with _patch_file_processing(process_return=outcome) as p:
            self._call(writer, reporter, headless_memo=headless_memo)
        p.move_file.assert_not_called()
        self.assertEqual(reporter.report_posted_calls, [])  # epoch 欠落→零呼出
        # simcodex Round 2 #1: memo に "SUCCESS" と偽記していないことを確認
        self.assertEqual(headless_memo[("cust:hash", None, "f1")]["outcome"],
                         main._MEMO_LABEL_EPOCH_MISSING)

    def test_memo_hit_skips_download_and_process_file(self):
        reporter = FakeReporter({"cust:hash": _job("cust:hash", lease_epoch=1)})
        writer = self._writer()
        headless_memo = {("cust:hash", 1, "f1"):
                         {"outcome": "FAILED", "folder_id": "in", "expire_cycle": None}}
        with _patch_file_processing() as p:
            self._call(writer, reporter, headless_memo=headless_memo)
        p.download_file.assert_not_called()   # DoD⑦: 零下載
        p.process_file.assert_not_called()    # DoD⑦: 零OCR（process_file 呼出も無い）

    def test_intake_state_not_posting_in_progress_skips_download(self):
        reporter = FakeReporter({"cust:hash": _job("cust:hash", current_state="POSTED")})
        writer = self._writer()
        with _patch_file_processing() as p:
            self._call(writer, reporter)
        p.download_file.assert_not_called()
        p.process_file.assert_not_called()

    def test_headless_mode_never_calls_is_duplicate_file(self):
        reporter = FakeReporter({"cust:hash": _job("cust:hash")})
        writer = self._writer()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)
        with _patch_file_processing(process_return=outcome) as p:
            self._call(writer, reporter)
        p.is_duplicate_file.assert_not_called()

    def test_ui_path_true_outcome_still_moves_file(self):
        # UI 版（reporter=None）は既存挙動不変: True→move、is_duplicate_file は
        # 通常経路で呼ばれ、False なら move されない。
        writer = self._writer()
        with _patch_file_processing(process_return=True, headless=False) as p:
            self._call(writer, None)
        p.is_duplicate_file.assert_called_once()
        p.move_file.assert_called_once()
        self.assertEqual(writer.start_new_file_calls[0][2], "invoice.pdf")

    def test_ui_path_duplicate_file_moves_and_skips_processing(self):
        writer = self._writer()
        with _patch_file_processing(duplicate_return=True, headless=False) as p:
            self._call(writer, None)
        p.move_file.assert_called_once()
        p.download_file.assert_not_called()
        p.process_file.assert_not_called()

    def test_ui_path_false_outcome_does_not_move(self):
        writer = self._writer()
        with _patch_file_processing(process_return=False, headless=False) as p:
            self._call(writer, None)
        p.move_file.assert_not_called()

    def test_unsupported_format_skips_download_without_separator(self):
        # 3. 格式過濾（既存挙動: 未対応拡張子は download_file にすら進まない）
        writer = self._writer()
        file = {"id": "f1", "name": "invoice.exe",
                "properties": {"sandevistan_posting_id": "cust:hash"}}
        with _patch_file_processing(headless=False) as p:
            self._call(writer, None, file=file)
        p.download_file.assert_not_called()
        p.process_file.assert_not_called()

    def test_flush_always_called(self):
        writer = self._writer()
        with _patch_file_processing(process_return=True, headless=False):
            self._call(writer, None)
        self.assertEqual(writer.flush_calls, 1)

    def test_local_temp_file_removed_when_it_exists(self):
        writer = self._writer()
        with _patch_file_processing(process_return=True, headless=False,
                                    path_exists=True), \
             patch("os.remove") as rm:
            self._call(writer, None)
        rm.assert_called_once_with("local.pdf")


# ============================================================
# §5.1-d T4-2/T4-3: headless tab_owner 解決（DoD①③④⑤）
# ============================================================

class ValidateCustomerLabelTest(unittest.TestCase):
    """_validate_customer_label の 4 条件（純関数、§5.1-d T4-2）。"""

    def test_valid_label_passes(self):
        self.assertTrue(main._validate_customer_label(
            "20220401", "20220401　株式会社緒方材木店"))

    def test_none_label_fails(self):
        self.assertFalse(main._validate_customer_label("20220401", None))

    def test_empty_string_label_fails(self):
        self.assertFalse(main._validate_customer_label("20220401", ""))

    def test_non_str_label_fails(self):
        self.assertFalse(main._validate_customer_label("20220401", 12345))

    def test_wrong_prefix_fails(self):
        self.assertFalse(main._validate_customer_label(
            "20220401", "99999999　別会社"))

    def test_missing_separator_fails(self):
        # customer_id で始まるが区切り（全角空格）が無い
        self.assertFalse(main._validate_customer_label(
            "20220401", "20220401株式会社緒方材木店"))

    def test_half_width_space_separator_fails(self):
        # 区切りは全角空格限定——半角空格は不通過
        self.assertFalse(main._validate_customer_label(
            "20220401", "20220401 株式会社緒方材木店"))

    def test_empty_name_part_after_separator_fails(self):
        self.assertFalse(main._validate_customer_label("20220401", "20220401　"))

    def test_too_long_label_fails(self):
        label = "20220401　" + "株" * 100
        self.assertGreater(len(label), 100)
        self.assertFalse(main._validate_customer_label("20220401", label))

    def test_exactly_100_chars_passes(self):
        prefix = "20220401　"
        label = prefix + "株" * (100 - len(prefix))
        self.assertEqual(len(label), 100)
        self.assertTrue(main._validate_customer_label("20220401", label))


class ResolveTabOwnerTest(unittest.TestCase):
    """_resolve_tab_owner（純関数、§5.1-d T4-2）。"""

    def test_customer_id_none_returns_none(self):
        self.assertIsNone(main._resolve_tab_owner(None, "anything"))

    def test_valid_label_is_adopted(self):
        self.assertEqual(
            main._resolve_tab_owner("20220401", "20220401　株式会社緒方材木店"),
            "20220401　株式会社緒方材木店")

    def test_invalid_label_falls_back_to_customer_id(self):
        self.assertEqual(
            main._resolve_tab_owner("20220401", "不正なラベル"), "20220401")

    def test_missing_label_falls_back_to_customer_id(self):
        self.assertEqual(main._resolve_tab_owner("20220401", None), "20220401")


class CustomerTabOwnerResolutionTest(unittest.TestCase):
    """_process_one_file が process_file へ渡す tab_owner kwarg を検証する。

    process_file 自体は _patch_file_processing で mock 化されている——実際の
    Sheets tab 集約（同一 tab_owner 値が同一タブへ落ちる保証）は
    test_sheets_output.TabNamerInjectionTest が担う。ここは main 側の
    customer_id/customer_label → tab_owner 解決ロジックのみを検収する。
    """

    def _file_with_uploader(self, email, file_id="f1", base="cust:hash"):
        return {"id": file_id, "name": "invoice.pdf",
                "properties": {"sandevistan_posting_id": base},
                "lastModifyingUser": {"emailAddress": email,
                                      "displayName": email}}

    def test_resolved_tab_owner_matches_full_customer_label(self):
        # ②tab 名が customer_label（全角空格区切り）と完全一致
        reporter = FakeReporter({"cust:hash": _job(
            "cust:hash", customer_id="20220401",
            customer_label="20220401　株式会社緒方材木店")})
        writer = FakeWriter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)
        with _patch_file_processing(process_return=outcome) as p:
            _call_process_one_file(writer, reporter)
        self.assertEqual(p.process_file.call_args.kwargs.get("tab_owner"),
                         "20220401　株式会社緒方材木店")

    def test_different_uploaders_same_customer_resolve_to_same_tab_owner(self):
        # ①同一顧客・別投稿者の 2 ファイル → 同一 tab_owner へ集約
        reporter = FakeReporter({"cust:hash": _job(
            "cust:hash", customer_id="20220401",
            customer_label="20220401　株式会社緒方材木店")})
        writer = FakeWriter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)
        file_a = self._file_with_uploader("a@example.com", file_id="fA")
        file_b = self._file_with_uploader("b@example.com", file_id="fB")

        with _patch_file_processing(process_return=outcome) as p:
            _call_process_one_file(writer, reporter, file=file_a)
            tab_owner_a = p.process_file.call_args.kwargs.get("tab_owner")
            _call_process_one_file(writer, reporter, file=file_b)
            tab_owner_b = p.process_file.call_args.kwargs.get("tab_owner")

        self.assertEqual(tab_owner_a, tab_owner_b)
        self.assertEqual(tab_owner_a, "20220401　株式会社緒方材木店")

    def test_invalid_label_falls_back_to_customer_id_with_warning(self):
        # ③label 不正（customer_id で始まらない）→ customer_id 単独＋警告
        reporter = FakeReporter({"cust:hash": _job(
            "cust:hash", customer_id="20220401",
            customer_label="別の顧客のラベル")})
        writer = FakeWriter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)
        with _patch_file_processing(process_return=outcome) as p:
            buf = io.StringIO()
            with redirect_stdout(buf):
                _call_process_one_file(writer, reporter)
        self.assertEqual(p.process_file.call_args.kwargs.get("tab_owner"), "20220401")
        self.assertIn("customer_label 不正", buf.getvalue())

    def test_missing_label_falls_back_to_customer_id_with_warning(self):
        # ③label 欠落（None）→ customer_id 単独＋警告（不通過／欠落は同じ扱い）
        reporter = FakeReporter({"cust:hash": _job(
            "cust:hash", customer_id="20220401", customer_label=None)})
        writer = FakeWriter()
        outcome = main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)
        with _patch_file_processing(process_return=outcome) as p:
            buf = io.StringIO()
            with redirect_stdout(buf):
                _call_process_one_file(writer, reporter)
        self.assertEqual(p.process_file.call_args.kwargs.get("tab_owner"), "20220401")
        self.assertIn("customer_label 不正", buf.getvalue())

    def test_missing_customer_id_writes_zero_and_alerts_and_keeps_file(self):
        # ④customer_id も欠落 → ダウンロード前に処理スキップ・冪等 alert・
        # ファイル保持（move も process_file も一切呼ばれない）
        reporter = FakeReporter({"cust:hash": _job(
            "cust:hash", customer_id=None, customer_label=None)})
        writer = FakeWriter()
        with _patch_file_processing() as p:
            _call_process_one_file(writer, reporter)
        p.download_file.assert_not_called()
        p.process_file.assert_not_called()
        p.move_file.assert_not_called()
        self.assertEqual(len(reporter.write_alert_calls), 1)
        alert_id, payload = reporter.write_alert_calls[0]
        self.assertEqual(alert_id, "f1")
        self.assertEqual(payload["kind"], "customer_metadata_missing")
        self.assertEqual(payload["file_id"], "f1")
        self.assertEqual(payload["posting_id"], "cust:hash")

    def test_ui_path_tab_owner_is_none(self):
        # ⑤UI 版: tab_owner は常に None（UI 経路＝ledger None は tab_owner を
        # 一切使わない。既存従業員 tab 挙動は無改動、simcodex R1 で
        # フォールバック自体を生産経路から撤去済）
        writer = FakeWriter()
        with _patch_file_processing(process_return=True, headless=False) as p:
            _call_process_one_file(writer, None)
        self.assertIsNone(p.process_file.call_args.kwargs.get("tab_owner"))

    def test_non_string_customer_id_treated_as_missing_not_crash(self):
        # codex R1-P2: 数値 customer_id（奇形 job）は TypeError で輪詢サイクルを
        # 落とさず、欠落と同じ alert-and-skip 経路へ封じ込める。
        reporter = FakeReporter({"cust:hash": _job(
            "cust:hash", customer_id=12345,
            customer_label="20220401　株式会社緒方材木店")})
        writer = FakeWriter()
        with _patch_file_processing() as p:
            _call_process_one_file(writer, reporter)
        p.process_file.assert_not_called()
        self.assertEqual(len(reporter.write_alert_calls), 1)
        self.assertEqual(reporter.write_alert_calls[0][1]["kind"],
                         "customer_metadata_missing")

    def test_missing_customer_id_alert_throttled_by_memo_and_rearmed(self):
        # simcodex R1（効率）: 3 秒輪詢で同一 file に毎輪 write_alert を打たない。
        # ①memo 付きで 2 輪回す → alert は 1 回だけ ②job が修復されたら
        # memo が解除され、再発時に改めて alert が飛ぶ（再武装）。
        job_missing = _job("cust:hash", customer_id=None, customer_label=None)
        job_fixed = _job("cust:hash", customer_id="20220401",
                         customer_label="20220401　株式会社緒方材木店")
        reporter = FakeReporter({"cust:hash": job_missing})
        writer = FakeWriter()
        memo = {}
        outcome = main.HeadlessOutcome(main.ProcessOutcome.SUCCESS)

        with _patch_file_processing(process_return=outcome):
            _call_process_one_file(writer, reporter, cycle=1,
                                   customer_meta_alerted=memo)
            _call_process_one_file(writer, reporter, cycle=2,
                                   customer_meta_alerted=memo)
        self.assertEqual(len(reporter.write_alert_calls), 1)  # 節流
        self.assertIn("f1", memo)

        # 控制面が job を修復 → 処理再開＋memo 解除（再武装）
        reporter._jobs["cust:hash"] = job_fixed
        with _patch_file_processing(process_return=outcome):
            _call_process_one_file(writer, reporter, cycle=3,
                                   customer_meta_alerted=memo)
        self.assertNotIn("f1", memo)

        # 再発 → 改めて alert（計 2 回目）
        reporter._jobs["cust:hash"] = job_missing
        with _patch_file_processing(process_return=outcome):
            _call_process_one_file(writer, reporter, cycle=4,
                                   customer_meta_alerted=memo)
        self.assertEqual(len(reporter.write_alert_calls), 2)


if __name__ == "__main__":
    unittest.main()


class PageOutcomesLoopWiringTest(unittest.TestCase):
    """B7-2 T3: _process_one_file の page_outcomes 構築・貫通・檔終局 flush。

    Plan §3 T3——構築点＝base 確定後（reporter.client 再利用）、
    flush＝report_posted の**前**（頁時失敗の補写一輪）。
    """

    class _StubOutcomes:
        instances = []

        def __init__(self, client, base):
            self.client = client
            self.base = base
            self.events = None  # テストが注入
            type(self).instances.append(self)

        def record_page(self, *a, **k):
            pass

        def flush_pending(self):
            if self.events is not None:
                self.events.append("flush")
            return 0

    def setUp(self):
        self._StubOutcomes.instances = []

    def _run_headless_success(self, events):
        stub_cls = self._StubOutcomes

        class _EventReporter(FakeReporter):
            def report_posted(self, base, *, lease_epoch):
                events.append("report_posted")
                return super().report_posted(base, lease_epoch=lease_epoch)

        reporter = _EventReporter({"cust:hash": _job("cust:hash", lease_epoch=9)})
        writer = FakeWriter()

        def _bind_events(client, base):
            inst = stub_cls(client, base)
            inst.events = events
            return inst

        with patch("firestore_progress.FirestorePageOutcomesReporter",
                   side_effect=_bind_events), \
             _patch_file_processing(process_return=main.HeadlessOutcome(
                 main.ProcessOutcome.SUCCESS)) as p:
            _call_process_one_file(writer, reporter)
        return p, reporter

    def test_constructed_with_job_client_and_base_and_passed_to_process_file(self):
        events = []
        p, reporter = self._run_headless_success(events)
        self.assertEqual(len(self._StubOutcomes.instances), 1)
        inst = self._StubOutcomes.instances[0]
        self.assertIs(inst.client, reporter.client)   # 同一 client 再利用
        self.assertEqual(inst.base, "cust:hash")
        kwargs = p.process_file.call_args.kwargs
        self.assertIs(kwargs.get("page_outcomes"), inst)

    def test_flush_pending_runs_before_report_posted(self):
        events = []
        self._run_headless_success(events)
        self.assertEqual(events, ["flush", "report_posted"])

    def test_ui_path_gets_no_page_outcomes(self):
        writer = FakeWriter()
        with _patch_file_processing(process_return=True, headless=False) as p:
            _call_process_one_file(writer, None)
        kwargs = p.process_file.call_args.kwargs
        self.assertIsNone(kwargs.get("page_outcomes"))
        self.assertEqual(self._StubOutcomes.instances, [])
