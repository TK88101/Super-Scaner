"""benchmark_stats の単体テスト（B5 T1/T2）。

Plan＝docs/plans/2026-08-10-b5-benchmark-ss.md §5 T1/T2。

T1 の歯型：nearest-rank を固定し、標本量が閾値未満なら「P99 と名乗らない」。
  N < 100 では ceil(0.99*N) == N となり P99 が最大値へ退化する——これが
  「100 未満は P99 不可」の数学的根拠（評審 #8・複審認可）。

T2 の歯型：入力試行数 == 出力レコード数（除外頁を黙って母数から落とさない。
  評審 #9 の成功者バイアス対策）。
"""

import unittest

import benchmark_stats as bs


class NearestRankTest(unittest.TestCase):
    """nearest-rank の定義を歯型で固定する（算法差し替えを検出）。"""

    def test_単一標本ではP50もP99も其の値になる(self):
        self.assertEqual(bs.nearest_rank([7.0], 50), 7.0)
        self.assertEqual(bs.nearest_rank([7.0], 99), 7.0)

    def test_二標本のP50は小さい方_P99は大きい方(self):
        # ceil(0.50*2)=1 → 1 番目、ceil(0.99*2)=2 → 2 番目
        self.assertEqual(bs.nearest_rank([10.0, 20.0], 50), 10.0)
        self.assertEqual(bs.nearest_rank([10.0, 20.0], 99), 20.0)

    def test_入力順に依存しない(self):
        self.assertEqual(bs.nearest_rank([20.0, 10.0], 50), 10.0)

    def test_百標本のP99は九十九番目_最大値ではない(self):
        # これが閾値 100 の根拠：N=100 で初めて P99 が最大値と分離する
        values = [float(i) for i in range(1, 101)]  # 1..100
        self.assertEqual(bs.nearest_rank(values, 99), 99.0)
        self.assertEqual(max(values), 100.0)

    def test_九十九標本のP99は最大値へ退化する(self):
        values = [float(i) for i in range(1, 100)]  # 1..99
        self.assertEqual(bs.nearest_rank(values, 99), 99.0)
        self.assertEqual(bs.nearest_rank(values, 99), max(values))

    def test_空標本はNoneを返す(self):
        self.assertIsNone(bs.nearest_rank([], 50))


class QuantileReportTest(unittest.TestCase):
    """標本量閾値の判定（P99 を名乗ってよいか）。"""

    def test_百標本未満はP99不可フラグが立つ(self):
        values = [float(i) for i in range(1, 100)]  # 99 件
        report = bs.summarize(values)
        self.assertFalse(report.p99_reportable)
        self.assertIsNone(report.p99)
        self.assertEqual(report.sample_count, 99)
        # P50 と max は不足時でも出す
        self.assertIsNotNone(report.p50)
        self.assertEqual(report.max_value, 99.0)

    def test_百標本ちょうどでP99可になる(self):
        values = [float(i) for i in range(1, 101)]
        report = bs.summarize(values)
        self.assertTrue(report.p99_reportable)
        self.assertEqual(report.p99, 99.0)

    def test_空標本は全てNoneでP99不可(self):
        report = bs.summarize([])
        self.assertFalse(report.p99_reportable)
        self.assertIsNone(report.p50)
        self.assertIsNone(report.p99)
        self.assertIsNone(report.max_value)
        self.assertEqual(report.sample_count, 0)

    def test_閾値は明示定数として公開される(self):
        # 報告に「なぜ 100 か」を書けるよう、定数を外から読めること
        self.assertEqual(bs.P99_MIN_SAMPLES, 100)


class OutcomeVocabularyTest(unittest.TestCase):
    """層別軸は自前で決めず、系統の権威値域に従う。

    頁級の真値は main 内部の kind（7 値）で、契約 §5.6 の outcome（4 値）は
    そこからの写像（firestore_progress.KIND_OUTCOME_MAP）。benchmark が独自の
    語彙を発明すると、報告の「終態」が系統のどの概念とも対応しなくなる。
    ここで漂移を検出する。
    """

    def test_層別軸はKIND_OUTCOME_MAPの全鍵を含む(self):
        import firestore_progress as fp
        self.assertTrue(set(fp.KIND_OUTCOME_MAP.keys()).issubset(set(bs.OUTCOMES)))

    def test_ESCALATEも層別軸に含む(self):
        # firestore_progress.py:88-89——ESCALATE も record_page に渡るが
        # 「意図的に不写」の裁決で契約 outcome を持たない。だが頁は実際に
        # 処理コストを payしているので benchmark の母数からは外さない
        self.assertIn("ESCALATE", bs.OUTCOMES)

    def test_契約outcomeへの写像を持つ(self):
        import firestore_progress as fp
        for kind in fp.KIND_OUTCOME_MAP:
            expected = fp.KIND_OUTCOME_MAP[kind][0]
            self.assertEqual(bs.to_contract_outcome(kind), expected)

    def test_ESCALATEは契約outcomeを持たない(self):
        self.assertIsNone(bs.to_contract_outcome("ESCALATE"))

    def test_契約outcomeは四値に畳まれる(self):
        folded = {bs.to_contract_outcome(k) for k in bs.OUTCOMES}
        folded.discard(None)  # ESCALATE 分
        self.assertEqual(len(folded), 4)


class StratifyTest(unittest.TestCase):
    """層別集計：入力試行数 == 出力レコード数（評審 #9）。"""

    def _rec(self, outcome, elapsed):
        return {"outcome": outcome, "elapsed_sec": elapsed}

    def test_全終態が層別され総数が保存される(self):
        records = [
            self._rec("POSTED_NOW", 1.0),
            self._rec("POSTED_PRIOR", 2.0),
            self._rec("EXCLUDED", 3.0),
            self._rec("PLACEHOLDER_WRITTEN", 4.0),
            self._rec("PLACEHOLDER_PRIOR", 4.5),
            self._rec("RETRYABLE", 5.0),
            self._rec("UNKNOWN", 6.0),
        ]
        result = bs.stratify(records)

        # 入力 7 件 → 各層の件数合計も 7 件（黙って落ちない）
        total_in_strata = sum(s.sample_count for s in result.strata.values())
        self.assertEqual(total_in_strata, len(records))
        # 全体分布も別に持つ
        self.assertEqual(result.overall.sample_count, len(records))

    def test_除外頁は母数から落とされない(self):
        records = [self._rec("POSTED_NOW", 1.0), self._rec("EXCLUDED", 99.0)]
        result = bs.stratify(records)
        self.assertEqual(result.overall.sample_count, 2)
        self.assertEqual(result.overall.max_value, 99.0)
        self.assertIn("EXCLUDED", result.strata)

    def test_未知の終態は例外にする(self):
        # 静かに無視すると母数が欠ける——大声で落とす
        with self.assertRaises(ValueError):
            bs.stratify([self._rec("MYSTERY", 1.0)])

    def test_空入力でも全終態の枠が出る(self):
        result = bs.stratify([])
        self.assertEqual(result.overall.sample_count, 0)
        for outcome in bs.OUTCOMES:
            self.assertIn(outcome, result.strata)
            self.assertEqual(result.strata[outcome].sample_count, 0)


if __name__ == "__main__":
    unittest.main()
