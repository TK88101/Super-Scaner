"""IP-304 T4: headless_rerun_fixture の DoD——夾具が崩潰重跑冪等を駆動し緑。

任意頁 kill（commit 直前＝ABSENT／CONFIRMED 直前＝PRESENT）→ 再跑で重複なし・
頁内半書なしを、頁数×kill 位置を跨いで検証する。B4（IP-305）が同夾具を import して
断点続跑シナリオを組めることの担保。
"""

from __future__ import annotations

import unittest

import main
from headless_rerun_fixture import HeadlessRerunFixture, pages


class FixtureDrivesIdempotencyTest(unittest.TestCase):
    def test_dod1_double_run_no_new_rows(self):
        fx = HeadlessRerunFixture()
        self.assertIs(fx.run(pages(3)), main.ProcessOutcome.SUCCESS)
        self.assertEqual(fx.landed_row_count(), 3)
        appends = fx.append_calls()

        self.assertIs(fx.run(pages(3)), main.ProcessOutcome.SUCCESS)  # 二跑
        self.assertEqual(fx.landed_row_count(), 3)          # 零新增行
        self.assertEqual(fx.append_calls(), appends)        # append ゼロ増

    def test_crash_at_each_commit_then_rerun_no_duplicate(self):
        # 3 頁ファイルで、commit #1/#2/#3 それぞれ崩潰させ、再跑で常に 3 行・重複なし
        for kill_page in (1, 2, 3):
            with self.subTest(kill_commit=kill_page):
                fx = HeadlessRerunFixture()
                with self.assertRaises(RuntimeError):
                    fx.run(pages(3), crash_commit_on=kill_page)
                # kill 頁の直前まで着地（append 直前 kill＝未 landed）
                self.assertEqual(fx.landed_row_count(), kill_page - 1)

                out = fx.run(pages(3))  # 再跑（正常）
                self.assertIs(out, main.ProcessOutcome.SUCCESS)
                sheet = fx.landed_rows()
                self.assertEqual(len(sheet), 3)                       # 半書なし・重複なし
                self.assertEqual([r[1] for r in sheet], ["店1", "店2", "店3"])

    def test_crash_before_confirm_then_rerun_skips_landed(self):
        # append landed 後・CONFIRMED 前 kill → 再跑で witness PRESENT → SKIP、重複なし
        fx = HeadlessRerunFixture()
        with self.assertRaises(RuntimeError):
            fx.run(pages(1), crash_confirm=True)
        self.assertEqual(fx.landed_row_count(), 1)   # landed 済み・台賬 PENDING

        out = fx.run(pages(1))                        # 再跑
        self.assertIs(out, main.ProcessOutcome.SUCCESS)
        self.assertEqual(fx.landed_row_count(), 1)   # 補 CONFIRMED・重複なし

    def test_multi_page_scales(self):
        # 6 頁（黄金様本と同頁数）でも二跑冪等
        fx = HeadlessRerunFixture()
        fx.run(pages(6))
        self.assertEqual(fx.landed_row_count(), 6)
        fx.run(pages(6))
        self.assertEqual(fx.landed_row_count(), 6)


if __name__ == "__main__":
    unittest.main()
