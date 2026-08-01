"""process_pipeline の消費者が `_excluded_page` を処理していることの契約テスト。

IP-401 の実票 smoke test で発覚した事故:
`main.process_file` にだけ `_excluded_page` の分流を実装し、`local_test.py` の
逐頁ループを直し忘れた。結果、封筒ページが entries=[] かつ `_unrecognized` 無しの
ため `sheets_output` の最終防衛に落ち、**MF 区に赤い認識不能行として混入**した。
「無音欠落を直したら MF を汚した」という本末転倒で、6ラウンドの評審
（simplify 4観点 × 各ラウンド + codex review）は全て diff しか見ないため
誰も気づかなかった——実票を1枚通して初めて出た。

**IP-402 T7 で構成を変えた。** 初版はファイル級 grep（AST）だけで不変式を守った
つもりだったが、それは要件を証明しない: `main.py` には UI 版の `_excluded_page`
分岐があるので、`_process_file_headless` が除外を完全に無視していても grep は
緑のままだった（`feature/sandevistan-headless` の merge で実証済み）。

現在の二層構成:

  第1層（主保証・振る舞い）: 実際に headless 経路を流し、除外ページで MF 区への
    書込が 0 回であること等を実測する。**否定対照つき**——除外処理を外した
    退化実装では同じ断言が実際に落ちることを確認し、このテストに歯があることを
    証明する（緑が「守れている」を意味すると言い切れるように）。
  第2層（補助網・AST）: `local_test.py` / `benchmark_ocr.py` のような、行為
    テストを持たないローカル用消費者の登録漏れを拾う。単独では main.py の
    headless 経路を証明できない（上記の実証済みの穴）ので、あくまで補助。

    venv311/bin/python -m unittest test_pipeline_consumers -v
"""
import ast
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import main  # noqa: E402
from headless_rerun_fixture import (  # noqa: E402
    FakeFirestore, FakeWriter, excluded_page, make_ledger, run_headless,
)

REPO = os.path.dirname(os.path.abspath(__file__))

# テストファイル自身は対象外（fixture として生の result を組むのが仕事）
_EXEMPT_PREFIX = "test_"

# patch する前の実体を捕まえておく（否定対照の wrapper から呼ぶため。
# patch 後の main._classify_page_result_shape を呼ぶと無限再帰する）
_REAL_CLASSIFY_SHAPE = main._classify_page_result_shape


def _classify_blind_to_excluded(results):
    """`_excluded_page` を見ない消費者を再現する（否定対照の注入実装）。

    印だけを剥がして本物の分類器へ渡す＝「除外処理を外した headless」と
    等価な状態を作る。IP-402 以前の `_classify_page_result_shape` は
    まさにこの挙動だった（除外を占位頁と誤認して MF 区へ占位行を書く）。
    """
    stripped = [{k: v for k, v in r.items() if k != "_excluded_page"}
                for r in results]
    return _REAL_CLASSIFY_SHAPE(stripped)


def _run_all_excluded_file():
    """全頁が除外（封筒）の PDF を headless 経路で 1 本流す。"""
    fs = FakeFirestore()
    writer = FakeWriter()
    ledger = make_ledger(fs, writer)
    out = run_headless(writer, ledger,
                       [excluded_page(1, 2), excluded_page(2, 2)])
    return writer, out


def _assert_excluded_contract(case, writer, out):
    """除外ページの消費者契約（IP-401＋IP-402）。正例と否定対照で共用する。

    共用が肝——否定対照が「別の緩い断言」で落ちたのでは、本テストに歯が
    あることの証明にならない。**同一の断言集**が退化実装で落ちて初めて
    「緑＝守れている」と言える。
    """
    case.assertEqual(writer.append_calls, 0,
                     "除外ページで MF 区への頁原子書込が発生している")
    case.assertEqual(writer.placeholder_calls, [],
                     "除外ページで MF 区へ占位行（append_entries）が書かれている")
    case.assertEqual(len(writer.audit_rows), 2,
                     "除外ページの留痕（監査タブ行）が頁数分ない")
    case.assertIs(out.outcome, main.ProcessOutcome.SUCCESS,
                  "全頁除外の終態が SUCCESS でない（正常完了扱い＝契約 v0.15 "
                  "§5.1-b 裁定2／P0-10。死信化していないかも確認）")


class HeadlessExcludedPageBehaviourTest(unittest.TestCase):
    """第1層: 主保証（振る舞い）＋否定対照。"""

    def test_headless_consumer_honours_excluded_page_contract(self):
        writer, out = _run_all_excluded_file()
        _assert_excluded_contract(self, writer, out)

    def test_negative_control_blind_consumer_breaks_the_same_assertions(self):
        """除外処理を外すと、上と**同一の断言集**が実際に落ちること。

        落ちなければ、この契約テストは何も守っていない（空振り）。
        """
        with patch.object(main, "_classify_page_result_shape",
                          _classify_blind_to_excluded):
            writer, out = _run_all_excluded_file()

        with self.assertRaises(AssertionError):
            _assert_excluded_contract(self, writer, out)

    def test_negative_control_reproduces_the_original_defect_shape(self):
        """否定対照が再現するのが「たまたま別の失敗」ではなく IP-402 が直した
        欠陥そのもの（MF 汚染＋死信化）であることを名指しで固定する。"""
        with patch.object(main, "_classify_page_result_shape",
                          _classify_blind_to_excluded):
            writer, out = _run_all_excluded_file()

        self.assertEqual(writer.append_calls, 2)   # MF 区へ占位行が 2 頁分
        self.assertEqual(writer.audit_rows, [])    # 監査タブには何も残らない
        self.assertIs(out.outcome, main.ProcessOutcome.DEAD_LETTER)


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
    """第2層: 補助網（AST）。行為テストを持たない消費者の登録漏れを拾う。

    **この層だけでは要件を証明できない**（file 級 grep なので、同一ファイル内の
    別経路が参照していれば緑になる）。主保証は上の振る舞いテスト。
    """

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

        `main.py` については振る舞いテスト（上）が本命——ここが緑でも
        headless 経路が対応済みとは限らない。
        """
        missing = [name for name, src in self.consumers.items()
                   if "_excluded_page" not in src]
        self.assertEqual(
            missing, [],
            f"process_pipeline を消費しているのに _excluded_page を見ていない: "
            f"{missing}。除外ページが MF 区へ漏れる（IP-401 の再発）")


if __name__ == "__main__":
    unittest.main()
