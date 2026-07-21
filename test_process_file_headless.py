"""IP-304 T3: process_file の headless 頁級緩衝集約＋硬去重の統合テスト。

真 PostingLedger（fake Firestore 台賬）＋fake writer（メモリ Sheets）＋patched
process_pipeline で、真 Drive/Sheets/Firestore 不接。DoD①（二跑冪等・零新增行）、
DoD②（崩潰重跑で頁内半書なし）、DoD③相当（ESCALATE 三態）、UI 経路不変、base 接線。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import main
from doc_types import DocType
from posting_ledger import PostingLedger
# 夾具（T4、B4 復用）から fake 部品を import——二重定義を避け単一ソース化。
from headless_rerun_fixture import (
    FakeFirestore, FakeWriter, _FakeResolver, error_page, make_ledger,
    page as _page, run_headless,
)


def _run(writer, ledger, pages):
    return run_headless(writer, ledger, pages)


def _make_ledger(fs, writer):
    return make_ledger(fs, writer)


class HeadlessIdempotencyTest(unittest.TestCase):
    def test_dod1_second_run_all_skip_zero_new_rows(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_page(1, 3, "店A", 1000), _page(2, 3, "店B", 2000),
                 _page(3, 3, "店C", 3000)]

        out1 = _run(writer, ledger, pages)
        self.assertIs(out1, main.ProcessOutcome.SUCCESS)
        rows_after_1 = len(writer.sheets["田中_領収書"])
        appends_after_1 = writer.append_calls
        self.assertEqual(rows_after_1, 3)

        # 2 回目（同一 fake ledger/fs＝台賬 CONFIRMED 残存）→ 全頁 SKIP・零新増行
        pages2 = [_page(1, 3, "店A", 1000), _page(2, 3, "店B", 2000),
                  _page(3, 3, "店C", 3000)]
        out2 = _run(writer, ledger, pages2)
        self.assertIs(out2, main.ProcessOutcome.SUCCESS)
        self.assertEqual(len(writer.sheets["田中_領収書"]), rows_after_1)
        self.assertEqual(writer.append_calls, appends_after_1)  # append 呼出ゼロ増

    def test_dod2_crash_before_confirm_reruns_without_duplicate(self):
        # 3 頁中 2 頁書込後、3 頁目 commit で崩潰（PENDING 残置・未 landed）→ 再跑
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)

        # 崩潰注入：3 頁目の commit_page が例外（append 未実行＝witness ABSENT）
        real_commit = writer.commit_page
        state = {"n": 0}

        def crashing_commit(pw):
            state["n"] += 1
            if state["n"] == 3:      # 3 頁目
                raise RuntimeError("kill: append 直前で崩潰")
            return real_commit(pw)

        writer.commit_page = crashing_commit
        pages = [_page(1, 3, "店A", 1000), _page(2, 3, "店B", 2000),
                 _page(3, 3, "店C", 3000)]
        with self.assertRaises(RuntimeError):
            _run(writer, ledger, pages)
        # 2 頁だけ着地、3 頁目は PENDING 残置（未 landed）
        self.assertEqual(len(writer.sheets["田中_領収書"]), 2)

        # 再跑（正常 commit_page、同 fs／同 sheets 状態）→ 前 2 頁 SKIP、
        # 3 頁目のみ補齊。重複なし＝合計 3 行。
        writer.commit_page = real_commit
        pages2 = [_page(1, 3, "店A", 1000), _page(2, 3, "店B", 2000),
                  _page(3, 3, "店C", 3000)]
        out = _run(writer, ledger, pages2)
        self.assertIs(out, main.ProcessOutcome.SUCCESS)
        sheet = writer.sheets["田中_領収書"]
        self.assertEqual(len(sheet), 3)              # 半書なし・重複なし
        self.assertEqual([r[1] for r in sheet], ["店A", "店B", "店C"])

    def test_dod2_crash_after_landed_before_confirm_skips_on_rerun(self):
        # append landed 後・CONFIRMED 前 kill → 再跑で witness PRESENT → 補 CONFIRMED
        # → SKIP（重複なし）。confirm 段の崩潰を fs.runner で 1 回だけ注入する。
        fs = FakeFirestore()
        writer = FakeWriter()

        calls = {"n": 0}
        base_runner = fs.runner()

        def flaky_runner(body):
            # post_page の txn 呼出し順: claim(PENDING)→confirm。2 回目（confirm）で崩潰。
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("kill: CONFIRMED 直前で崩潰")
            return base_runner(body)

        ledger = PostingLedger(fs, "cust:hash", sheet_probe=writer.probe_page,
                               transaction_runner=flaky_runner)
        pages = [_page(1, 1, "店A", 1000)]
        with self.assertRaises(RuntimeError):
            _run(writer, ledger, pages)
        # append は landed（1 行）だが台賬は PENDING のまま
        self.assertEqual(len(writer.sheets["田中_領収書"]), 1)

        # 再跑（正常 runner、同 sheets）→ witness PRESENT → SKIP。零新増行。
        ledger2 = _make_ledger(fs, writer)
        pages2 = [_page(1, 1, "店A", 1000)]
        out = _run(writer, ledger2, pages2)
        self.assertIs(out, main.ProcessOutcome.SUCCESS)
        self.assertEqual(len(writer.sheets["田中_領収書"]), 1)  # 重複なし


class HeadlessEscalateContinuityTest(unittest.TestCase):
    def test_page_num_reappearance_escalates(self):
        # #6：処理済み頁番号 1 が非連続に再登場 → ESCALATE（拆頁前提破れ）
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_page(1, 3, "店A", 1000), _page(2, 3, "店B", 2000),
                 _page(1, 3, "店A再", 1000)]
        out = _run(writer, ledger, pages)
        self.assertIs(out, main.ProcessOutcome.ESCALATED)

    def test_check_page_escalate_stops_file(self):
        # ledger.check_page が ESCALATE（witness 不確実）→ ファイル中止（ESCALATED）
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        # 台賬に witness 欠損の PENDING を仕込む（_recover_pending → ESCALATE）
        from posting_ledger import derive_page_id
        pid = derive_page_id("cust:hash", 1)
        fs.store[("jobs", "cust:hash", "postings", pid)] = {
            "status": "PENDING", "sheet_tab": "田中_領収書",
            "predicted_row_range": None, "row_fingerprint": "",
        }
        out = _run(writer, ledger, [_page(1, 1, "店A", 1000)])
        self.assertIs(out, main.ProcessOutcome.ESCALATED)
        self.assertEqual(writer.append_calls, 0)  # 書込中止


class MultiTicketPageTest(unittest.TestCase):
    def test_multiple_tickets_one_page_single_append(self):
        # 同一 page_num の連続 yield（一頁多票）→ 1 頁として集約・commit 1 回
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_page(1, 1, "店A", 1000), _page(1, 1, "店B", 2000)]
        out = _run(writer, ledger, pages)
        self.assertIs(out, main.ProcessOutcome.SUCCESS)
        self.assertEqual(writer.append_calls, 1)          # 頁原子
        self.assertEqual(len(writer.sheets["田中_領収書"]), 2)  # 2 票


class HeadlessPartialErrorTest(unittest.TestCase):
    """codex P1: 部分ページエラーで失敗頁を静默に落とさない（占位行で可視化・UI 同等）。"""

    def test_partial_error_writes_placeholder_and_succeeds(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_page(1, 3, "店A", 1000), error_page(2, 3), _page(3, 3, "店C", 3000)]
        out = _run(writer, ledger, pages)
        self.assertIs(out, main.ProcessOutcome.SUCCESS)          # 歸檔される
        self.assertEqual(len(writer.sheets["田中_領収書"]), 2)   # 成功 2 頁のみ着地
        self.assertEqual(len(writer.placeholder_calls), 1)       # 失敗頁を占位行で可視化
        ph = writer.placeholder_calls[0]
        self.assertTrue(ph["_unrecognized"])
        self.assertIn("p2", ph["memo"])

    def test_placeholder_write_failure_retains_file(self):
        # 占位行の書込自体が失敗（transient Sheets 障害）→ SUCCESS を返さず保持（再試行）
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)

        def boom(**kwargs):
            raise RuntimeError("Sheets 429")

        writer.append_entries = boom
        pages = [_page(1, 3, "店A", 1000), error_page(2, 3), _page(3, 3, "店C", 3000)]
        out = _run(writer, ledger, pages)
        self.assertIs(out, main.ProcessOutcome.FAILED)   # 歸檔せず保持
        self.assertEqual(len(writer.sheets["田中_領収書"]), 2)  # 成功頁は台賬 CONFIRMED 済

    def test_all_pages_error_returns_failed_without_placeholder(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [error_page(1, 2), error_page(2, 2)]
        out = _run(writer, ledger, pages)
        self.assertIs(out, main.ProcessOutcome.FAILED)           # 全頁エラー → ファイル保持
        self.assertEqual(writer.placeholder_calls, [])           # 部分エラー占位行は出さない


class UiPathUnchangedTest(unittest.TestCase):
    def test_ledger_none_uses_append_entries(self):
        # ledger=None（UI 版）→ 従来の逐 result append_entries 経路（bool 返却）
        calls = []

        class _UiWriter:
            def append_entries(self, **kw):
                calls.append(kw)

        pages = [_page(1, 1, "店A", 1000)]
        with patch.object(main, "process_pipeline", return_value=iter(pages)), \
             patch.object(main, "PageUrlResolver", _FakeResolver), \
             patch.object(main, "send_notification"):
            out = main.process_file(
                service=None, sheets_writer=_UiWriter(), file_path="d.pdf",
                uploader_name="田中", chat_id=None, doc_type=DocType.RECEIPT,
                drive_file_id=None)  # base/ledger 既定 None
        self.assertIs(out, True)
        self.assertEqual(len(calls), 1)  # append_entries が呼ばれた（UI 経路）


class IntakeGateBaseWiringTest(unittest.TestCase):
    def test_gate_passes_base_on_process(self):
        # PROCESS 時 base が (should_process, base) で返る（#14 接線）
        file = {"id": "f1", "properties": {"sandevistan_posting_id": "cust:hash"}}

        class _Rep:
            def get_job(self, base):
                return {"posting_id": base}

            def write_alert(self, *a):
                pass

        with patch.object(main.config, "headless_mode", return_value=True):
            should, base = main._headless_intake_gate(
                service=None, file=file, input_folder_id="in", reporter=_Rep())
        self.assertTrue(should)
        self.assertEqual(base, "cust:hash")


if __name__ == "__main__":
    unittest.main()
