"""benchmark_record の単体テスト（B5 T6）。

Plan＝docs/plans/2026-08-10-b5-benchmark-ss.md §5 T6。

歯型（評審 #10・#18）：DoD は「欄が埋まった」ではなく機械判定可能な述語。
生データと報告の整合、試行数の保存、必須メタの実在を validator が検査する。
"""

import json
import os
import tempfile
import unittest

import benchmark_record as br


def _rec(path="a.pdf", outcome="POSTED_NOW", elapsed=1.0, error=None):
    return {
        "input_path": path,
        "outcome": outcome,
        "elapsed_sec": elapsed,
        "error": error,
        "page_count": 1,
        "base": "job-1",
        "sha256": "deadbeef",
    }


class JsonlRoundTripTest(unittest.TestCase):

    def test_書いて読むと同じになる(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "r.jsonl")
            records = [_rec("a.pdf"), _rec("b.pdf", outcome="EXCLUDED")]
            br.write_jsonl(p, records)
            self.assertEqual(br.read_jsonl(p), records)

    def test_一行一件で書かれる(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "r.jsonl")
            br.write_jsonl(p, [_rec("a.pdf"), _rec("b.pdf")])
            with open(p, encoding="utf-8") as f:
                lines = [ln for ln in f if ln.strip()]
            self.assertEqual(len(lines), 2)
            json.loads(lines[0])  # 各行が単独で JSON として妥当

    def test_日本語パスが壊れない(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "r.jsonl")
            br.write_jsonl(p, [_rec("領収書_井戸会計.pdf")])
            self.assertEqual(br.read_jsonl(p)[0]["input_path"], "領収書_井戸会計.pdf")


class ValidatorTest(unittest.TestCase):

    def _meta(self, **over):
        meta = {
            "git_sha": "abc1234",
            "platform": "Darwin-25.5.0-arm64",
            "headless_mode": True,
            "attempted": 2,
            "quantile_algorithm": "nearest-rank",
            "p99_min_samples": 100,
        }
        meta.update(over)
        return meta

    def test_整合していれば通る(self):
        issues = br.validate_records([_rec("a.pdf"), _rec("b.pdf")], self._meta())
        self.assertEqual(issues, [])

    def test_試行数が合わなければ落ちる(self):
        # 評審 #9：入力試行数 == 出力レコード数
        issues = br.validate_records([_rec("a.pdf")], self._meta(attempted=5))
        self.assertTrue(any("試行数" in i for i in issues))

    def test_HEADLESS_MODEが偽なら落ちる(self):
        issues = br.validate_records([_rec()], self._meta(headless_mode=False))
        self.assertTrue(any("HEADLESS_MODE" in i for i in issues))

    def test_git_shaが無ければ落ちる(self):
        meta = self._meta()
        del meta["git_sha"]
        issues = br.validate_records([_rec()], meta)
        self.assertTrue(any("git_sha" in i for i in issues))

    def test_未知の終態は落ちる(self):
        bad = _rec()
        bad["outcome"] = "MYSTERY"
        issues = br.validate_records([bad], self._meta(attempted=1))
        self.assertTrue(any("終態" in i for i in issues))

    def test_NaNや負の耗時は落ちる(self):
        bad = _rec(elapsed=float("nan"))
        issues = br.validate_records([bad], self._meta(attempted=1))
        self.assertTrue(any("elapsed_sec" in i for i in issues))

        neg = _rec(elapsed=-1.0)
        issues = br.validate_records([neg], self._meta(attempted=1))
        self.assertTrue(any("elapsed_sec" in i for i in issues))

    def test_多頁が混ざっていたら落ちる(self):
        # 契約 headless の入力は 1 頁／1 切片
        bad = _rec()
        bad["page_count"] = 3
        issues = br.validate_records([bad], self._meta(attempted=1))
        self.assertTrue(any("頁" in i for i in issues))

    def test_問題は全部集めて返す_最初の一件で止まらない(self):
        bad = _rec(elapsed=-1.0)
        bad["outcome"] = "MYSTERY"
        issues = br.validate_records([bad], self._meta(attempted=99,
                                                       headless_mode=False))
        self.assertGreaterEqual(len(issues), 3)


if __name__ == "__main__":
    unittest.main()
