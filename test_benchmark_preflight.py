"""benchmark_preflight の単体テスト（B5 T3/T9）。

Plan＝docs/plans/2026-08-10-b5-benchmark-ss.md §5 T3・T9。

T3 の歯型（評審 #3 由来）：`ledger` を渡すだけでは headless にならない。
  消費側は切り替わるが producer 側（ocr_engine 末尾段の envelope_filter）は
  `config.headless_mode()` を読むため、`HEADLESS_MODE` 未設定のまま走らせると
  「headless 消費者 ＋ UI 生産者」の混合経路を測ってしまう。だから走る前に
  落とす。

T9 の歯型（評審 #14 由来）：pypdf 不在時、`_split_pdf_pages` は警告 1 行で
  空を返し、逐頁分岐が return せず末尾段の全ファイル読込へ落ちる
  （＝内存爆発経路）。benchmark でそこへ突入させない——走る前に拒否する。
"""

import os
import unittest
from unittest import mock

import benchmark_preflight as pf


class HeadlessModeAssertionTest(unittest.TestCase):

    @mock.patch.dict(os.environ, {}, clear=False)
    def test_HEADLESS_MODE未設定なら落とす(self):
        os.environ.pop("HEADLESS_MODE", None)
        with self.assertRaises(pf.PreflightError) as ctx:
            pf.assert_headless_mode()
        self.assertIn("HEADLESS_MODE", str(ctx.exception))

    @mock.patch.dict(os.environ, {"HEADLESS_MODE": "0"}, clear=False)
    def test_HEADLESS_MODEがゼロなら落とす(self):
        with self.assertRaises(pf.PreflightError):
            pf.assert_headless_mode()

    @mock.patch.dict(os.environ, {"HEADLESS_MODE": "1"}, clear=False)
    def test_HEADLESS_MODEが一なら通る(self):
        pf.assert_headless_mode()  # 例外が出ないこと

    @mock.patch.dict(os.environ, {"HEADLESS_MODE": "1"}, clear=False)
    def test_config側の判定と一致していることを確かめる(self):
        # 環境変数を直読みするのではなく config を通す（呼出時点評価の一元化）
        import config
        self.assertTrue(config.headless_mode())
        pf.assert_headless_mode()


class SinglePageAssertionTest(unittest.TestCase):
    """契約 headless の入力は 1 頁／1 切片（契約 v0.19 §65・§304）。"""

    def test_複数頁PDFは拒否する(self):
        with mock.patch.object(pf, "_count_pdf_pages", return_value=478):
            with self.assertRaises(pf.PreflightError) as ctx:
                pf.assert_single_page("dummy.pdf")
        self.assertIn("478", str(ctx.exception))

    def test_単頁PDFは通る(self):
        with mock.patch.object(pf, "_count_pdf_pages", return_value=1):
            pf.assert_single_page("dummy.pdf")

    def test_画像は一頁として扱う(self):
        # 画像は PDF ではないので頁数計算に入らず、常に 1 単位
        pf.assert_single_page("dummy.png")
        pf.assert_single_page("dummy.jpg")

    def test_頁数が読めない時は拒否する(self):
        # 「読めなかったので通す」は測定母数を汚す。落とす方を選ぶ
        with mock.patch.object(pf, "_count_pdf_pages", return_value=None):
            with self.assertRaises(pf.PreflightError):
                pf.assert_single_page("broken.pdf")


class PypdfAvailabilityTest(unittest.TestCase):

    def test_pypdf不在なら落とす(self):
        with mock.patch.object(pf, "_pypdf_available", return_value=False):
            with self.assertRaises(pf.PreflightError) as ctx:
                pf.assert_pypdf_available()
        # 危険経路の説明が入っていること（なぜ拒否するかが伝わる）
        self.assertIn("pypdf", str(ctx.exception))

    def test_pypdf在れば通る(self):
        with mock.patch.object(pf, "_pypdf_available", return_value=True):
            pf.assert_pypdf_available()

    def test_実環境ではpypdfが入っている(self):
        # venv311 の実状を固定（入っていなければ benchmark は走らせない）
        self.assertTrue(pf._pypdf_available())


class RunPreflightTest(unittest.TestCase):
    """一括実行＋メタ情報（生データに残して後から検証できるように）。"""

    @mock.patch.dict(os.environ, {"HEADLESS_MODE": "1"}, clear=False)
    def test_全て満たせばメタを返す(self):
        with mock.patch.object(pf, "_count_pdf_pages", return_value=1), \
             mock.patch.object(pf, "_pypdf_available", return_value=True):
            meta = pf.run_preflight("dummy.pdf")
        self.assertTrue(meta["headless_mode"])
        self.assertTrue(meta["pypdf_available"])
        self.assertEqual(meta["page_count"], 1)

    @mock.patch.dict(os.environ, {}, clear=False)
    def test_一つでも欠ければ例外(self):
        os.environ.pop("HEADLESS_MODE", None)
        with mock.patch.object(pf, "_count_pdf_pages", return_value=1), \
             mock.patch.object(pf, "_pypdf_available", return_value=True):
            with self.assertRaises(pf.PreflightError):
                pf.run_preflight("dummy.pdf")


class HeadlessEnvScopeTest(unittest.TestCase):
    """実行中だけ HEADLESS_MODE を立て、抜けたら元に戻す（評審 #3）。"""

    def test_文脈を抜けると元の値に戻る(self):
        os.environ.pop("HEADLESS_MODE", None)
        with pf.headless_env():
            self.assertEqual(os.environ.get("HEADLESS_MODE"), "1")
        self.assertIsNone(os.environ.get("HEADLESS_MODE"))

    def test_元の値がある場合はその値に戻る(self):
        os.environ["HEADLESS_MODE"] = "0"
        try:
            with pf.headless_env():
                self.assertEqual(os.environ.get("HEADLESS_MODE"), "1")
            self.assertEqual(os.environ.get("HEADLESS_MODE"), "0")
        finally:
            os.environ.pop("HEADLESS_MODE", None)

    def test_例外が出ても復元する(self):
        os.environ.pop("HEADLESS_MODE", None)
        with self.assertRaises(RuntimeError):
            with pf.headless_env():
                raise RuntimeError("boom")
        self.assertIsNone(os.environ.get("HEADLESS_MODE"))


if __name__ == "__main__":
    unittest.main()
