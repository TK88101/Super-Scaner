"""接線の番人（Plan B-4 / B-6）。

ここが見張るのは「静かに効かなくなる」形の事故だけ。
どれもテストが無ければ**緑のまま壊れる**。

1. `main` と `local_test` が**同じ関数**を呼ぶこと。
   片方だけ直すと漂移する（CLAUDE.md が名指しする ENTRY_BUILDERS 未登録と同族）。
2. `main` の呼出が `count > 0` の内側にあること。
   外に出ると、保持されたファイルが 3 秒ごとに再スキャンされるたび
   監査タブに同じ行が増える（冪等性の破壊）。
3. 理由キーの訳語が登録されていること。
   漏れると理由列が英語のまま顧客の目に触れる。
4. `benchmark_ocr` が `_file_recon` を精度比較に混ぜないこと。
"""
import ast
import contextlib
import io
import pathlib
import unittest
import unittest.mock as mock

import card_file_recon as cfr
from sheets_output import AUDIT_VERDICT_TOTAL_MISMATCH, audit_reason_ja

_ROOT = pathlib.Path(__file__).resolve().parent
_WIRED = "report_and_record"


def _tree(name):
    return ast.parse((_ROOT / name).read_text(encoding="utf-8"))


def _calls_to(tree, attr):
    """`card_file_recon.<attr>(...)` の呼出ノードを集める。"""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == attr:
            out.append(node)
    return out


class BothConsumersUseTheSameFunctionTest(unittest.TestCase):
    """AD-3 / B-4: 2 つの消費側が同じ関数を呼ぶ。"""

    def test_main_calls_report_and_record(self):
        self.assertTrue(_calls_to(_tree("main.py"), _WIRED),
                        "main.py が検算を呼んでいない")

    def test_local_test_calls_report_and_record(self):
        self.assertTrue(_calls_to(_tree("local_test.py"), _WIRED),
                        "local_test.py が検算を呼んでいない")

    def test_the_function_exists_and_is_shared(self):
        self.assertTrue(callable(getattr(cfr, _WIRED, None)))

    def test_neither_consumer_calls_finalize_directly(self):
        """`finalize` を直接呼ぶと、ログ出力と監査タブの判断が分岐する。

        判断は `report_and_record` の 1 箇所に閉じておく。
        """
        for name in ("main.py", "local_test.py"):
            with self.subTest(name=name):
                self.assertEqual(
                    _calls_to(_tree(name), "finalize"), [],
                    "%s が finalize を直接呼んでいる（判断が二重化する）" % name)


class MainCallSiteIsInsideTheArchivedBranchTest(unittest.TestCase):
    """AD-6: 呼ぶのは `count > 0`（＝ファイルが歸檔される）経路だけ。

    全頁失敗と count==0 はファイルを保持する。そこで呼ぶと
    3 秒ごとの再スキャンで監査タブに同じ行が積み上がる。
    """

    def _count_gt_zero_ifs(self, tree):
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == "count"
                    and len(test.ops) == 1
                    and isinstance(test.ops[0], ast.Gt)):
                found.append(node)
        return found

    def test_call_is_nested_under_count_gt_zero(self):
        tree = _tree("main.py")
        branches = self._count_gt_zero_ifs(tree)
        self.assertTrue(branches, "main.py に `if count > 0:` が見当たらない")
        inside = []
        for br in branches:
            for stmt in br.body:
                inside.extend(_calls_to(stmt, _WIRED))
        self.assertTrue(
            inside,
            "検算の呼出が `count > 0` の外にある。保持されるファイルで"
            "呼ぶと監査タブに同じ行が無限に増える（AD-6）")

    def test_no_call_outside_that_branch(self):
        tree = _tree("main.py")
        total = len(_calls_to(tree, _WIRED))
        inside = sum(len(_calls_to(s, _WIRED))
                     for br in self._count_gt_zero_ifs(tree) for s in br.body)
        self.assertEqual(total, inside, "`count > 0` の外にも呼出がある")


class ReasonIsTranslatedTest(unittest.TestCase):
    """訳語の登録漏れ＝理由列が機械可読キーのまま顧客に見える。"""

    def test_mismatch_reason_is_japanese(self):
        raw = "%s:273344/271000/-2344" % cfr.REASON_MISMATCH
        text = audit_reason_ja(raw)
        self.assertNotEqual(text, raw, "訳語が登録されていない")
        self.assertIn("273,344", text, "金額が読める形で入っていない")
        self.assertIn("271,000", text)

    def test_report_produces_that_exact_shape(self):
        """`report` が作るキーの形が、訳語側の期待と一致すること。"""
        v = cfr.FileVerdict(cfr.VERDICT_MISMATCH, 273344, 271000, -2344,
                            cfr.REASON_MISMATCH)
        rep = cfr.report(v, "テスト.pdf")
        self.assertIsNotNone(rep.audit_reason)
        self.assertNotEqual(audit_reason_ja(rep.audit_reason),
                            rep.audit_reason,
                            "report が作る形を audit_reason_ja が訳せていない")

    def test_verdict_label_is_a_new_distinct_value(self):
        from sheets_output import (AUDIT_VERDICT_BRANCH,
                                   AUDIT_VERDICT_EXCLUDED,
                                   AUDIT_VERDICT_MISSING)
        self.assertNotIn(AUDIT_VERDICT_TOTAL_MISMATCH,
                         (AUDIT_VERDICT_BRANCH, AUDIT_VERDICT_EXCLUDED,
                          AUDIT_VERDICT_MISSING))


class ReportOutputTest(unittest.TestCase):
    """AD-5 / 複審の争点 3: ログは常に出す。監査タブは不一致だけ。"""

    def test_match_logs_but_writes_nothing(self):
        v = cfr.FileVerdict(cfr.VERDICT_MATCH, 100, 100, 0, cfr.REASON_MATCH)
        rep = cfr.report(v, "テスト.pdf")
        self.assertIn("verdict=", rep.log_line)
        self.assertIsNone(rep.audit_reason, "一致で監査タブに書いてはいけない")

    def test_unverifiable_logs_but_writes_nothing(self):
        v = cfr.FileVerdict(cfr.VERDICT_UNVERIFIABLE, None, 0, None,
                            cfr.REASON_NO_TOTAL)
        rep = cfr.report(v, "テスト.pdf")
        self.assertIn(cfr.REASON_NO_TOTAL, rep.log_line,
                      "検算不能の理由がログに出ない＝壊れても気づけない")
        self.assertIsNone(rep.audit_reason)

    def test_mismatch_logs_and_writes(self):
        v = cfr.FileVerdict(cfr.VERDICT_MISMATCH, 100, 90, -10,
                            cfr.REASON_MISMATCH)
        rep = cfr.report(v, "テスト.pdf")
        self.assertIsNotNone(rep.audit_reason)


class RecordUsesTheAuditRowContractTest(unittest.TestCase):
    """監査タブの 7 列契約を守る。列を増やすと header 検証に弾かれる。"""

    class _Writer:
        def __init__(self):
            self.calls = []

        def append_audit_row(self, **kw):
            self.calls.append(kw)

    def _run(self, verdict_tuple):
        w = self._Writer()
        with mock.patch.object(cfr, "finalize", return_value=verdict_tuple):
            with contextlib.redirect_stdout(io.StringIO()):
                cfr.report_and_record([], "credit_card", "テスト.pdf", "",
                                      w, AUDIT_VERDICT_TOTAL_MISMATCH)
        return w

    def test_mismatch_writes_one_row_with_the_expected_columns(self):
        w = self._run(cfr.FileVerdict(cfr.VERDICT_MISMATCH, 100, 90, -10,
                                      cfr.REASON_MISMATCH))
        self.assertEqual(len(w.calls), 1)
        kw = w.calls[0]
        self.assertEqual(set(kw), {"filename", "page_num", "verdict", "reason",
                                   "ocr_text_len", "source_url"})
        self.assertEqual(kw["verdict"], AUDIT_VERDICT_TOTAL_MISMATCH)
        self.assertEqual(kw["page_num"], 0,
                         "ファイル単位の行なので特定頁ではない")

    def test_match_writes_nothing(self):
        w = self._run(cfr.FileVerdict(cfr.VERDICT_MATCH, 100, 100, 0,
                                      cfr.REASON_MATCH))
        self.assertEqual(w.calls, [])

    def test_writer_failure_does_not_propagate(self):
        """書込失敗で記帳を止めない（AD-7）。"""
        class _Boom:
            def append_audit_row(self, **kw):
                raise RuntimeError("boom")

        with mock.patch.object(
                cfr, "finalize",
                return_value=cfr.FileVerdict(cfr.VERDICT_MISMATCH, 100, 90,
                                             -10, cfr.REASON_MISMATCH)):
            with contextlib.redirect_stdout(io.StringIO()):
                cfr.report_and_record([], "credit_card", "テスト.pdf", "",
                                      _Boom(), AUDIT_VERDICT_TOTAL_MISMATCH)


class DocTypeTableDriftTest(unittest.TestCase):
    """producer の門と検算資格の表が食い違わないこと。

    producer 側の門は `page_family.CC_FAMILY_DOC_TYPES`
    （`ocr_engine._with_file_recon`）、検算資格の権威は
    `card_reconciliation.RECON_POLICY`。**今日たまたま一致している**が、
    別モジュールで別目的に定義された 2 つの表であって、同期を保証する
    仕組みは無かった。

    片方だけに doc_type を足すと: producer が `_file_recon` を載せない →
    観測が空 → `finalize` が「検算不能」を返す → 監査タブに何も出ない。
    **例外も出ず、テストも赤くならず、ただ検算が効かない。**
    CLAUDE.md が名指しする ENTRY_BUILDERS 未登録と同じ破壊様式なので、
    同じように番人で塞ぐ（Altitude 評審 2026-08-21）。
    """

    def test_producer_gate_matches_recon_policy(self):
        import card_reconciliation as cr
        import page_family
        eligible = {dt for dt, policy in cr.RECON_POLICY.items()
                    if policy != cr.POLICY_NONE}
        self.assertEqual(
            set(page_family.CC_FAMILY_DOC_TYPES), eligible,
            "producer の門と RECON_POLICY がズレている。"
            "片方だけに足すと、その doc_type の検算が無音で効かなくなる")


class NotApplicableIsSilentTest(unittest.TestCase):
    """検算対象外の doc_type はコンソールにも出さない。

    本番は無人運転で、コンソールが唯一の人手監視経路である
    （CLAUDE.md「靠人工查看控制台/表格产出确认」）。領収書 1 通ごとに
    零情報の行を出すと、IP-401 型の事故信号がその中に流れて消える。
    """

    def test_not_applicable_produces_no_log_line(self):
        v = cfr.FileVerdict(cfr.VERDICT_NOT_APPLICABLE, None, 0, None,
                            cfr.REASON_POLICY)
        self.assertEqual(cfr.report(v, "領収書.pdf").log_line, "")

    def test_the_other_three_verdicts_still_log(self):
        """黙らせるのは対象外だけ。検算不能まで黙ると壊れても気づけない。"""
        for verdict, reason in ((cfr.VERDICT_MATCH, cfr.REASON_MATCH),
                                (cfr.VERDICT_UNVERIFIABLE,
                                 cfr.REASON_NO_TOTAL),
                                (cfr.VERDICT_MISMATCH, cfr.REASON_MISMATCH)):
            with self.subTest(verdict=verdict):
                v = cfr.FileVerdict(verdict, 100, 100, 0, reason)
                self.assertTrue(cfr.report(v, "テスト.pdf").log_line)


class BenchmarkIsUnaffectedTest(unittest.TestCase):
    """B-6: `benchmark_ocr` は process_pipeline の第 3 の消費者。"""

    def test_benchmark_does_not_read_file_recon(self):
        src = (_ROOT / "benchmark_ocr.py").read_text(encoding="utf-8")
        self.assertNotIn("_file_recon", src,
                         "benchmark が検算の键を読むと精度比較に混ざる")

    def test_benchmark_does_not_import_the_recon_module(self):
        tree = _tree("benchmark_ocr.py")
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertNotIn("card_file_recon", imported)


if __name__ == "__main__":
    unittest.main()
