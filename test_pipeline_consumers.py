"""process_pipeline の消費者が全員 `_excluded_page` を処理していることの構造テスト。

IP-401 の実票 smoke test で発覚した事故:
`main.process_file` にだけ `_excluded_page` の分流を実装し、`local_test.py` の
逐頁ループを直し忘れた。結果、封筒ページが entries=[] かつ `_unrecognized` 無しの
ため `sheets_output` の最終防衛に落ち、**MF 区に赤い認識不能行として混入**した。
「無音欠落を直したら MF を汚した」という本末転倒で、6ラウンドの評審
（simplify 4観点 × 各ラウンド + codex review）は全て diff しか見ないため
誰も気づかなかった——実票を1枚通して初めて出た。

CLAUDE.md に記録されている ENTRY_BUILDERS 未登録事故と同族の「登録漏れ」型欠陥
であり、同じ解法を採る: **不変式をコード側に自証させる**。

不変式: `process_pipeline` を消費する本番コードは、`_excluded_page` を
必ず参照しなければならない（参照の仕方は各消費者の責務——監査タブ／MF 提示行／
集計対象外、いずれでもよいが「見ていない」は許さない）。

    venv311/bin/python -m unittest test_pipeline_consumers -v
"""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

REPO = os.path.dirname(os.path.abspath(__file__))

# テストファイル自身は対象外（fixture として生の result を組むのが仕事）
_EXEMPT_PREFIX = "test_"


def _production_py_files():
    for name in sorted(os.listdir(REPO)):
        if not name.endswith(".py"):
            continue
        if name.startswith(_EXEMPT_PREFIX):
            continue
        yield name


def _calls_process_pipeline(tree):
    """その AST が process_pipeline を呼んでいるか（定義ではなく呼出）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name == "process_pipeline":
            return True
    return False


class PipelineConsumersHandleExcludedPageTest(unittest.TestCase):
    """消費者の登録漏れを構造的に禁じる。"""

    def setUp(self):
        self.consumers = {}
        for name in _production_py_files():
            path = os.path.join(REPO, name)
            with open(path, encoding="utf-8") as f:
                source = f.read()
            try:
                tree = ast.parse(source)
            except SyntaxError:  # pragma: no cover - 壊れた .py は別問題
                continue
            if _calls_process_pipeline(tree):
                self.consumers[name] = source

    def test_consumers_are_discovered(self):
        """このテスト自身が空振りしていないことを保証する（否定対照）。

        消費者が0件ならテストは無条件 pass してしまい、不変式を守っている
        つもりで何も守らなくなる。
        """
        self.assertGreaterEqual(
            len(self.consumers), 2,
            f"process_pipeline の消費者が見つからない（検出: {list(self.consumers)}）。"
            "検出ロジックが壊れているか、呼出方法が変わった")

    def test_known_consumers_are_detected(self):
        """既知の消費者が検出対象に入っていること（検出漏れの回帰保護）。"""
        for expected in ("main.py", "local_test.py", "benchmark_ocr.py"):
            self.assertIn(expected, self.consumers,
                          f"{expected} が消費者として検出されていない")

    def test_every_consumer_references_excluded_page(self):
        """全消費者が `_excluded_page` を参照していること。

        参照の仕方は問わない（監査タブへ回す／MF 提示行を書く／集計対象外に
        する、いずれも正当）。「見ていない」ことだけを禁じる。見ていないと
        entries=[] かつ _unrecognized 無しの result が sheets_output の
        最終防衛に落ち、MF 区へ赤い認識不能行として混入する。
        """
        missing = [name for name, src in self.consumers.items()
                   if "_excluded_page" not in src]
        self.assertEqual(
            missing, [],
            f"process_pipeline を消費しているのに _excluded_page を見ていない: "
            f"{missing}。除外ページが MF 区へ漏れる（IP-401 の再発）")


if __name__ == "__main__":
    unittest.main()
