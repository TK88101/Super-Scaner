"""receipt_aggregation の単体テスト（5/25 仕様変更: 税率別合計出力）。

外部依存なしの純粋ロジックのため、標準ライブラリ unittest で実行可能:
    python3 -m unittest test_receipt_aggregation -v
    python3 -m pytest test_receipt_aggregation.py -v   # pytest があれば
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import receipt_aggregation as agg
from receipt_aggregation import aggregate_entries_by_tax_rate, coerce_tax_rate


def _item(amount, tax_rate, debit_account, debit_tax_type, description="品目"):
    """明細エントリ生成ヘルパー。"""
    return {
        "debit_account": debit_account,
        "debit_tax_type": debit_tax_type,
        "credit_account": "未払金",
        "credit_tax_type": "対象外",
        "amount": amount,
        "description": description,
        "tax_rate": tax_rate,
    }


class AggregateEntriesByTaxRateTest(unittest.TestCase):
    def test_mixed_tax_rates_produce_two_lines(self):
        # Arrange: 8% 2品目 + 10% 2品目（便利店混在票）
        entries = [
            _item(150, 0.08, "接待交際費", "課対仕入8% (軽)"),
            _item(130, 0.08, "接待交際費", "課対仕入8% (軽)"),
            _item(500, 0.10, "備品・消耗品費", "課対仕入10%"),
            _item(220, 0.10, "備品・消耗品費", "課対仕入10%"),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 税率別に2行、合計(総和)行は出さない
        self.assertEqual(len(result), 2)
        by_rate = {r["debit_tax_type"]: r for r in result}
        self.assertEqual(by_rate["課対仕入8% (軽)"]["amount"], 280)
        self.assertEqual(by_rate["課対仕入10%"]["amount"], 720)
        # 摘要は税率ラベルのみ（店名は sheets_output 側で前置されるため二重防止）
        self.assertEqual(by_rate["課対仕入8% (軽)"]["description"], "8%対象")
        self.assertEqual(by_rate["課対仕入10%"]["description"], "10%対象")

    def test_single_tax_rate_produces_one_line(self):
        # Arrange: 10% のみ（単一税率）
        entries = [
            _item(500, 0.10, "旅費交通費", "課対仕入10%"),
            _item(300, 0.10, "旅費交通費", "課対仕入10%"),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 合計1行のみ
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["amount"], 800)
        self.assertEqual(result[0]["description"], "10%対象")

    def test_same_rate_different_accounts_uses_max_amount(self):
        # Arrange: 同一8%グループで科目が異なる（弁当500 vs 購物袋30）
        entries = [
            _item(500, 0.08, "接待交際費", "課対仕入8% (軽)", "弁当"),
            _item(30, 0.08, "備品・消耗品費", "課対仕入8% (軽)", "エコバッグ"),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 既定戦略(max_amount)で金額最大の科目を採用
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["amount"], 530)
        self.assertEqual(result[0]["debit_account"], "接待交際費")

    def test_bank_transfer_zero_rate_kept_separate(self):
        # Arrange: 振込本体(税率0) + 手数料(10%)
        entries = [
            _item(10000, 0, "未払金", "対象外", "振込本体"),
            _item(330, 0.10, "支払手数料", "課対仕入10%", "振込手数料"),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 対象外と10%は別行、対象外は摘要も「対象外」
        self.assertEqual(len(result), 2)
        by_tax = {r["debit_tax_type"]: r for r in result}
        self.assertEqual(by_tax["対象外"]["amount"], 10000)
        self.assertEqual(by_tax["対象外"]["description"], "対象外")
        self.assertEqual(by_tax["課対仕入10%"]["amount"], 330)

    def test_null_tax_rate_merges_into_standard_rate(self):
        # Arrange: Gemini が tax_rate: null を返したエントリ（10%扱い）＋通常10%
        # _build_entries_for_single_doc 相当で debit_tax_type は既に課対仕入10%
        null_item = _item(200, None, "備品・消耗品費", "課対仕入10%")
        entries = [
            null_item,
            _item(300, 0.10, "備品・消耗品費", "課対仕入10%"),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: null は10%に寄せられ、1行に合算され摘要も整合する
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["amount"], 500)
        self.assertEqual(result[0]["debit_tax_type"], "課対仕入10%")
        self.assertEqual(result[0]["description"], "10%対象")

    def test_string_tax_rate_does_not_crash_and_groups(self):
        # Arrange: Gemini が文字列 "0.08" を返したエントリ（round(str*100) で従来クラッシュ）
        entries = [_item(150, "0.08", "接待交際費", "課対仕入8% (軽)")]

        # Act: TypeError を出さず正常に集約できる
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 0.08 として扱われ「8%対象」になる
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["amount"], 150)
        self.assertEqual(result[0]["description"], "8%対象")

    def test_zero_rate_not_coerced_to_default(self):
        # Arrange: 税率0(対象外)は既定税率に変えてはならない（or 演算子バグ回帰防止）
        entries = [_item(10000, 0, "未払金", "対象外")]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 対象外のまま、摘要も対象外
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["description"], "対象外")
        self.assertEqual(result[0]["debit_tax_type"], "対象外")

    def test_empty_entries_returns_empty(self):
        # Arrange / Act / Assert
        self.assertEqual(aggregate_entries_by_tax_rate([]), [])

    def test_zero_amount_group_skipped(self):
        # Arrange: 金額0のみのグループは出力しない
        entries = [_item(0, 0.10, "備品・消耗品費", "課対仕入10%")]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert
        self.assertEqual(result, [])

    def test_description_is_tax_label_only(self):
        # Arrange: 摘要は税率ラベルのみ（店名は sheets_output が前置するため含めない）
        entries = [_item(100, 0.10, "備品・消耗品費", "課対仕入10%")]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert
        self.assertEqual(result[0]["description"], "10%対象")

    def test_fixed_strategy_uses_fixed_account(self):
        # Arrange: 戦略を fixed に切り替え（テスト後に必ず復元）
        original = agg.AGGREGATED_DEBIT_ACCOUNT_STRATEGY
        agg.AGGREGATED_DEBIT_ACCOUNT_STRATEGY = "fixed"
        try:
            entries = [
                _item(500, 0.10, "接待交際費", "課対仕入10%"),
                _item(300, 0.10, "旅費交通費", "課対仕入10%"),
            ]

            # Act
            result = aggregate_entries_by_tax_rate(entries)

            # Assert: 固定科目になる
            self.assertEqual(
                result[0]["debit_account"], agg.AGGREGATED_DEBIT_ACCOUNT_FIXED
            )
        finally:
            agg.AGGREGATED_DEBIT_ACCOUNT_STRATEGY = original


class CoerceTaxRateTest(unittest.TestCase):
    def test_float_passthrough(self):
        self.assertEqual(coerce_tax_rate(0.08), 0.08)

    def test_int_to_float(self):
        self.assertEqual(coerce_tax_rate(0), 0.0)

    def test_string_number_parsed(self):
        self.assertEqual(coerce_tax_rate("0.10"), 0.10)

    def test_none_defaults_to_standard(self):
        self.assertEqual(coerce_tax_rate(None), agg.DEFAULT_TAX_RATE)

    def test_invalid_string_defaults_to_standard(self):
        self.assertEqual(coerce_tax_rate("abc"), agg.DEFAULT_TAX_RATE)

    def test_bool_defaults_to_standard(self):
        # bool は int のサブクラスだが税率ではないので既定に寄せる
        self.assertEqual(coerce_tax_rate(True), agg.DEFAULT_TAX_RATE)


if __name__ == "__main__":
    unittest.main()
