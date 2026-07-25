"""IP-305 斷點續跑（B4 Plan §T5）——工単 DoD の受入テスト群。

`HeadlessRerunFixture`＋`fake_firestore` を復用（新造夾具禁）。夾具適配は
Plan が明示許容する範囲のみ（headless_rerun_fixture.py の error_page
error_class 引数／HeadlessRerunFixture.reporter フック）。

工単 DoD：
    ①5 頁件、3 票（頁）書込後崩潰→重投→恰 5 票無重複。
    ②舊 epoch で report_posted が REJECTED（stale_lease_epoch）→ ファイルは
      保持（move 全廃で自明）・SS は書込を再試行しない（頁級台賬が SKIP を
      保証、新 epoch での再回報のみ許可）。
    ③暫時類自癒鏈：p3 RETRYABLE→FAILED(retryable)→下輪 p3 成功→SUCCESS→
      report_posted。成功頁（p1/p2）は Sheets 零重寫。
    ④content 部分失敗を二回重跑→占位行は恆に 1 行のまま（IP-306 DoD 再検証）。
"""

from __future__ import annotations

import unittest

import main
from headless_rerun_fixture import (
    FakeReporter, HeadlessRerunFixture, error_page, page, pages,
)


class FivePageCrashResumeTest(unittest.TestCase):
    """DoD①: 5 頁件、3 頁書込後崩潰→重投→恰 5 票無重複。"""

    def test_five_pages_crash_after_three_then_resume_exactly_five_no_duplicate(self):
        fx = HeadlessRerunFixture()
        with self.assertRaises(RuntimeError):
            fx.run(pages(5), crash_commit_on=4)  # 3 頁着地後、4 頁目 commit で崩潰
        self.assertEqual(fx.landed_row_count(), 3)

        out = fx.run(pages(5))  # 重投（再跑）
        self.assertIs(out.outcome, main.ProcessOutcome.SUCCESS)
        rows = fx.landed_rows()
        self.assertEqual(len(rows), 5)                       # 恰 5 票
        self.assertEqual([r[1] for r in rows],
                         ["店1", "店2", "店3", "店4", "店5"])  # 無重複・順序保持

    def test_five_pages_no_crash_baseline_matches_five(self):
        # 崩潰無しでも 5 票が揃うことのベースライン確認（①比較対象）
        fx = HeadlessRerunFixture()
        out = fx.run(pages(5))
        self.assertIs(out.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(fx.landed_row_count(), 5)


class StaleEpochRejectedTest(unittest.TestCase):
    """DoD②: 舊 epoch で report_posted が REJECTED → 檔案保持・不重試寫賬。"""

    def test_stale_epoch_report_rejected_does_not_retrigger_sheet_write(self):
        base = "cust:hash"
        # 控制面が epoch を 1→2 へ進めた後（reconciliation 等）、SS がまだ古い
        # epoch=1 で report_posted を呼ぶ状況を模す。
        reporter = FakeReporter({base: {"lease_epoch": 2}})
        fx = HeadlessRerunFixture(base=base, reporter=reporter)

        out1 = fx.run(pages(3), lease_epoch=1, cycle=1)
        self.assertIs(out1.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(reporter.report_posted_calls, [(base, 1)])  # 恰一回
        rows_after_1 = fx.landed_row_count()
        self.assertEqual(rows_after_1, 3)

        # ファイルは move されない（IP-308 の move 全廃で自明・本テストでは
        # 「次輪も同じファイルが再スキャンされ得る」ことをそのまま模擬する）。
        # 次輪、control-plane が epoch=2 へ更新済みの job を再提示——頁級台賬は
        # 既に 3 頁とも CONFIRMED のため SKIP のみ、Sheets への書込は起きない。
        out2 = fx.run(pages(3), lease_epoch=2, cycle=2)
        self.assertIs(out2.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(fx.landed_row_count(), rows_after_1)   # 零新增行（不重試寫賬）
        self.assertEqual(reporter.report_posted_calls,
                         [(base, 1), (base, 2)])                # 新 epoch での再回報は許可

    def test_stale_epoch_rejection_does_not_raise_or_retry_internally(self):
        # _report_headless_outcome は REJECTED でも例外を出さず、呼出し回数は
        # 恰一回（契約：SS は不重試）。
        base = "cust:hash"
        reporter = FakeReporter({base: {"lease_epoch": 99}})
        fx = HeadlessRerunFixture(base=base, reporter=reporter)
        fx.run(pages(1), lease_epoch=1, cycle=1)
        self.assertEqual(len(reporter.report_posted_calls), 1)


class RetryableSelfHealTest(unittest.TestCase):
    """DoD③: 暫時類自癒鏈——p3 RETRYABLE→FAILED→下輪 p3 成功→SUCCESS→POSTED。
    成功頁（p1/p2）は Sheets 零重寫。
    """

    def test_retryable_page_then_success_on_rerun_posts_without_rewriting_success_pages(self):
        base = "cust:hash"
        reporter = FakeReporter({base: {"lease_epoch": 1}})
        fx = HeadlessRerunFixture(base=base, reporter=reporter)

        pages1 = [page(1, 3, "店1", 1000), page(2, 3, "店2", 2000),
                 error_page(3, 3, error_class="RETRYABLE")]
        out1 = fx.run(pages1, lease_epoch=1, cycle=1)
        self.assertIs(out1.outcome, main.ProcessOutcome.FAILED)
        self.assertTrue(out1.retryable)
        self.assertEqual(reporter.report_posted_calls, [])   # RETRYABLE は回報せず
        rows_after_1 = fx.landed_row_count()
        self.assertEqual(rows_after_1, 2)                     # p1/p2 は既に着地

        # 下輪: p3 が今度は成功（一時故障が自癒）
        pages2 = pages(3)  # p1/p2/p3 とも正常
        out2 = fx.run(pages2, lease_epoch=1, cycle=2)
        self.assertIs(out2.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(reporter.report_posted_calls, [(base, 1)])  # 恰一回 POSTED

        rows = fx.landed_rows()
        self.assertEqual(len(rows), 3)                        # p3 のみ追加（2→3）
        # 成功頁（p1/p2）は零重寫: 行内容が最初の書込のまま変わらない
        self.assertEqual(rows[0][1], "店1")
        self.assertEqual(rows[1][1], "店2")
        # run1: p1/p2 それぞれ 1 commit（2回）。run2: p1/p2 は SKIP（0回）、
        # p3 のみ新規 WRITE（1回）。合計 3 回——「零重寫」は commit 回数の
        # 不変ではなく p1/p2 の内容が変わらないことで検証する（下記）。
        self.assertEqual(fx.writer.append_calls, 3)


class ContentPartialDoubleRerunPlaceholderTest(unittest.TestCase):
    """DoD④（IP-306 DoD 再検証、IP-305 夾具視点）: content 部分失敗を二回重跑
    しても占位行は恆に 1 行のまま。
    """

    def test_content_placeholder_stays_single_row_across_two_reruns(self):
        fx = HeadlessRerunFixture()
        pages_ = [page(1, 2, "店A", 1000), error_page(2, 2, error_class="CONTENT")]

        out1 = fx.run(pages_)
        self.assertIs(out1.outcome, main.ProcessOutcome.PARTIAL)
        self.assertEqual(fx.landed_row_count(), 2)   # 正常1行+占位1行

        # 一回目の重跑
        out2 = fx.run(pages_)
        self.assertIs(out2.outcome, main.ProcessOutcome.PARTIAL)
        self.assertEqual(fx.landed_row_count(), 2)   # 増えない

        # 二回目の重跑（工単 DoD の「二回重跑」を字義通り検証）
        out3 = fx.run(pages_)
        self.assertIs(out3.outcome, main.ProcessOutcome.PARTIAL)
        self.assertEqual(fx.landed_row_count(), 2)   # 占位行は恆に1行のまま


if __name__ == "__main__":
    unittest.main()
