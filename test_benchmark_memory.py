"""benchmark_memory の単体テスト（B5 T7/T8）。

Plan＝docs/plans/2026-08-10-b5-benchmark-ss.md §4.3／§5 T7・T8。

T7 の歯型（評審 #5・#7）：
  - 採取対象は**プロセス木**（pdftoppm は Popen の子。親 RSS だけでは大件峰値を
    系統的に過小評価する）
  - **各プロセスの高水位を足し合わせない**（異時刻の峰値を重畳してしまう）。
    主指標は「同一時刻に生存する木の RSS 合計」の最大値
  - 採取器そのものを、既知量を確保して即終了する子プロセスで検証する

T8 の歯型（評審 #8）：OOM のとき worker は OS に殺されるので、worker 自身は
  報告を書けない。親が別プロセスとして監視し、異常終了を記録できること。
  「miniPC の欄が埋まった」では OOM していないことの証明にならない。
"""

import os
import signal
import sys
import unittest

import benchmark_memory as bm


class TreeRssTest(unittest.TestCase):

    def test_自プロセスのRSSが取れる(self):
        rss = bm.tree_rss_bytes(os.getpid())
        self.assertGreater(rss, 0)

    def test_子プロセスの分が合計に入る(self):
        # 既知量（約 64MB）を確保して待機する子を起こし、合計が増えることを見る
        code = ("import time,sys;"
                "buf=bytearray(64*1024*1024);"
                "sys.stdout.write('ready');sys.stdout.flush();"
                "time.sleep(30)")
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", code],
                                 stdout=subprocess.PIPE)
        try:
            child.stdout.read(5)  # ready を待つ＝確保完了の同期
            with_child = bm.tree_rss_bytes(os.getpid())
            self.assertGreater(with_child, 32 * 1024 * 1024)
        finally:
            child.kill()
            child.wait()

    def test_存在しないPIDはゼロを返す(self):
        # 走査中に子が消えるのは正常。例外にせず 0 扱いで続行する
        self.assertEqual(bm.tree_rss_bytes(999999), 0)


class PlatformPeakTest(unittest.TestCase):

    def test_プラットフォーム名が入る(self):
        metrics = bm.platform_peak_metrics()
        self.assertIn("platform", metrics)

    def test_macOSではru_maxrssをバイトで返す(self):
        if sys.platform != "darwin":
            self.skipTest("macOS 専用")
        metrics = bm.platform_peak_metrics()
        self.assertIn("ru_maxrss_bytes", metrics)
        # macOS の ru_maxrss は**バイト**（Linux は KB）。本 session で実測確認済
        self.assertGreater(metrics["ru_maxrss_bytes"], 1024 * 1024)

    def test_主指標と附加指標が混ざらない(self):
        # 評審 #5：Mac と Windows の高水位は意味が違うので主指標にしない
        metrics = bm.platform_peak_metrics()
        self.assertNotIn("peak_bytes", metrics)


class RunSampledTest(unittest.TestCase):

    def _py(self, code):
        return [sys.executable, "-c", code]

    def test_正常終了の終了コードを記録する(self):
        result = bm.run_sampled(self._py("pass"), interval_sec=0.05)
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.killed_by_signal)

    def test_異常終了の終了コードを記録する(self):
        result = bm.run_sampled(self._py("raise SystemExit(3)"), interval_sec=0.05)
        self.assertEqual(result.exit_code, 3)

    def test_SIGKILLされた場合は殺されたと記録する(self):
        # OOM 時の OS による強制終了を模す——親が沈黙してはいけない
        code = ("import os,signal,sys,time;"
                "sys.stdout.write('go');sys.stdout.flush();"
                "os.kill(os.getpid(), signal.SIGKILL);time.sleep(5)")
        result = bm.run_sampled(self._py(code), interval_sec=0.05)
        self.assertEqual(result.killed_by_signal, signal.SIGKILL)
        self.assertFalse(result.succeeded)

    def test_確保した既知量が峰値に現れる(self):
        code = ("import time;buf=bytearray(96*1024*1024);time.sleep(1.0)")
        result = bm.run_sampled(self._py(code), interval_sec=0.05)
        self.assertGreater(result.peak_bytes, 48 * 1024 * 1024)

    def test_採取周期と採取回数が結果に残る(self):
        # 「取りこぼしたかもしれない」を後から判断できるように
        result = bm.run_sampled(self._py("import time;time.sleep(0.5)"),
                                interval_sec=0.05)
        self.assertEqual(result.interval_sec, 0.05)
        self.assertGreater(result.sample_count, 1)

    def test_成功判定は終了コードと信号の両方を見る(self):
        ok = bm.run_sampled(self._py("pass"), interval_sec=0.05)
        self.assertTrue(ok.succeeded)
        ng = bm.run_sampled(self._py("raise SystemExit(1)"), interval_sec=0.05)
        self.assertFalse(ng.succeeded)


if __name__ == "__main__":
    unittest.main()
