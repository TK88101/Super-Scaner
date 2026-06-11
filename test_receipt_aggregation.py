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


def _item(amount, tax_rate, debit_account, debit_tax_type, description="品目",
          tax_included=True, tax_amount=0):
    """明細エントリ生成ヘルパー。

    tax_included: True=内税(税込表示)、False=外税(税抜表示+消費税別建て)。
    tax_amount:   外税レシートの票面消費税額（内税では集約時に無視される）。
    """
    return {
        "debit_account": debit_account,
        "debit_tax_type": debit_tax_type,
        "credit_account": "未払金",
        "credit_tax_type": "対象外",
        "amount": amount,
        "description": description,
        "tax_rate": tax_rate,
        "tax_included": tax_included,
        "tax_amount": tax_amount,
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
        self.assertEqual(by_rate["課対仕入8% (軽)"]["description"], "8%")
        self.assertEqual(by_rate["課対仕入10%"]["description"], "10%")

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
        self.assertEqual(result[0]["description"], "10%")

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
        self.assertEqual(result[0]["description"], "10%")

    def test_string_tax_rate_does_not_crash_and_groups(self):
        # Arrange: Gemini が文字列 "0.08" を返したエントリ（round(str*100) で従来クラッシュ）
        entries = [_item(150, "0.08", "接待交際費", "課対仕入8% (軽)")]

        # Act: TypeError を出さず正常に集約できる
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 0.08 として扱われ「8%」になる
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["amount"], 150)
        self.assertEqual(result[0]["description"], "8%")

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
        self.assertEqual(result[0]["description"], "10%")

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


class RealSamplePatternTest(unittest.TestCase):
    """顧客フィードバック表（6/10 版）の6パターン実票サンプルに基づく検証。

    Google Sheet「自動仕訳ツール 税区分」の期待出力と完全一致させる。
    金額は実票（紅枠スクリーンショット）から検証済み。
    """

    def test_p1_gooday_8_and_10_uchizei(self):
        # Arrange: GooDay 甘木店（8%+10% 内税）。割引は8%対象額に純額化済み
        entries = [
            _item(124, 0.08, "備品・消耗品費", "課対仕入8% (軽)", "紅茶花伝(割引後)"),
            _item(940, 0.10, "備品・消耗品費", "課対仕入10%", "ジョイントコーク"),
            _item(591, 0.10, "備品・消耗品費", "課対仕入10%", "モール外角"),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 8%行=124, 10%行=1531（出現順を保持）
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["description"], "8%")
        self.assertEqual(result[0]["amount"], 124)
        self.assertEqual(result[1]["description"], "10%")
        self.assertEqual(result[1]["amount"], 1531)

    def test_p2_tonkatsu_10_only_uchizei(self):
        # Arrange: とんかつきく富（10%のみ 内税、ランチ2点）
        entries = [
            _item(1200, 0.10, "接待交際費", "課対仕入10%", "ヒレ＆メンチカツランチ"),
            _item(1200, 0.10, "接待交際費", "課対仕入10%", "ヒレカツランチ"),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 1行のみ、税込合計2400
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["amount"], 2400)
        self.assertEqual(result[0]["debit_tax_type"], "課対仕入10%")

    def test_p3_chidori_8_sotozei_with_printed_tax(self):
        # Arrange: 千鳥饅頭（8%のみ 外税）。票面: 対象額1,620 + 8%外税額130
        entries = [
            _item(1620, 0.08, "接待交際費", "課対仕入8% (軽)", "千鳥饅頭詰合せ",
                  tax_included=False, tax_amount=130),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 借方金額は税込 1,750（=1,620+130）。税抜のまま出さない
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["amount"], 1750)
        self.assertEqual(result[0]["debit_tax_type"], "課対仕入8% (軽)")

    def test_p3_sotozei_without_printed_tax_falls_back_to_rounding(self):
        # Arrange: 外税だが票面税額を抽出できなかった場合（tax_amount=0）
        entries = [
            _item(1620, 0.08, "接待交際費", "課対仕入8% (軽)", "千鳥饅頭詰合せ",
                  tax_included=False, tax_amount=0),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 1,620×8%=129.6 → 四捨五入130 を加算して税込1,750
        self.assertEqual(result[0]["amount"], 1750)

    def test_p4_golf_8_10_and_taxfree_uchizei(self):
        # Arrange: ゴルフ利用明細（8%+10%+非課税 内税）。
        # 10%: プレーフィ13,490−割引2,000+担々麺1,650+おにぎり300=13,440
        # 8%: 和菓子270+キリンレモン380=650 / 非課税: 利用税500
        entries = [
            _item(13490, 0.10, "接待交際費", "課対仕入10%", "プレーフィ"),
            _item(-2000, 0.10, "接待交際費", "課対仕入10%", "グリーンメンテナンス割"),
            _item(1650, 0.10, "接待交際費", "課対仕入10%", "担々麺"),
            _item(300, 0.10, "接待交際費", "課対仕入10%", "おにぎり"),
            _item(270, 0.08, "接待交際費", "課対仕入8% (軽)", "和菓子"),
            _item(380, 0.08, "接待交際費", "課対仕入8% (軽)", "キリンレモン"),
            _item(500, 0, "接待交際費", "対象外", "ゴルフ場利用税"),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 10%/8%/対象外 の3行（出現順）、貸借合計14,590と整合
        self.assertEqual(len(result), 3)
        self.assertEqual(
            [(r["description"], r["amount"]) for r in result],
            [("10%", 13440), ("8%", 650), ("対象外", 500)],
        )
        self.assertEqual(sum(r["amount"] for r in result), 14590)

    def test_p5_akizuki_10_and_taxfree_uchizei(self):
        # Arrange: 秋月CC（10%+非課税 内税）。プレーフィ6,700−値引700 / 利用税200
        entries = [
            _item(6700, 0.10, "接待交際費", "課対仕入10%", "プレーフィ"),
            _item(-700, 0.10, "接待交際費", "課対仕入10%", "特別値引"),
            _item(200, 0, "接待交際費", "対象外", "ゴルフ場利用税"),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 10%行=6,000 と 対象外行=200、合計6,200
        self.assertEqual(len(result), 2)
        self.assertEqual(
            [(r["description"], r["amount"]) for r in result],
            [("10%", 6000), ("対象外", 200)],
        )

    def test_p6_trial_uchizei_sotozei_mixed_not_merged(self):
        # Arrange: TRIAL（内税と外税が混在）。同じ10%でも内税/外税は別行。
        # 外税グループ: 596+180+356=1,132 + 外税113 = 1,245 / 内税: レジ袋5
        entries = [
            _item(596, 0.10, "備品・消耗品費", "課対仕入10%", "領収証小切手判",
                  tax_included=False, tax_amount=113),
            _item(180, 0.10, "備品・消耗品費", "課対仕入10%", "ウェットフローリング(値引後)",
                  tax_included=False),
            _item(356, 0.10, "備品・消耗品費", "課対仕入10%", "トイレクリーナー",
                  tax_included=False),
            _item(5, 0.10, "備品・消耗品費", "課対仕入10%", "レジ袋"),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 10%外税1,245 と 10%内税5 の2行（合算して1,250にしない）
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["amount"], 1245)
        self.assertEqual(result[1]["amount"], 5)
        self.assertTrue(all(r["debit_tax_type"] == "課対仕入10%" for r in result))

    def test_p6_sotozei_fallback_rounding_when_no_tax_amount(self):
        # Arrange: 外税グループの票面税額が無い場合、1,132×10%=113.2→113
        entries = [
            _item(596, 0.10, "備品・消耗品費", "課対仕入10%", tax_included=False),
            _item(180, 0.10, "備品・消耗品費", "課対仕入10%", tax_included=False),
            _item(356, 0.10, "備品・消耗品費", "課対仕入10%", tax_included=False),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert
        self.assertEqual(result[0]["amount"], 1245)

    def test_negative_sotozei_group_passes_through(self):
        # Arrange: 純返品の外税グループ（負額）。無断で行を落とさず赤字行として残す
        entries = [
            _item(-500, 0.10, "備品・消耗品費", "課対仕入10%", "返品",
                  tax_included=False, tax_amount=-50),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: -500-50=-550 の赤字行（人手確認に委ねる）
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["amount"], -550)

    def test_zero_rate_normalized_to_single_taxfree_row(self):
        # Arrange: rate=0 で Gemini が tax_included を true/false 混在で返しても
        # 対象外行は1行に集約される（プロンプト契約違反への防御）
        entries = [
            _item(300, 0, "接待交際費", "対象外", "宿泊税", tax_included=True),
            _item(200, 0, "接待交際費", "対象外", "入湯税", tax_included=False),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 2行に割れず1行500
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["amount"], 500)
        self.assertEqual(result[0]["description"], "対象外")

    def test_uchizei_ignores_tax_amount(self):
        # Arrange: 内税では amount が既に税込のため tax_amount を加算してはならない
        entries = [
            _item(2400, 0.10, "接待交際費", "課対仕入10%", tax_included=True,
                  tax_amount=218),
        ]

        # Act
        result = aggregate_entries_by_tax_rate(entries)

        # Assert: 2,400 のまま（2,618 にしない）
        self.assertEqual(result[0]["amount"], 2400)


class BuildRowsFromTaxSummaryTest(unittest.TestCase):
    """票面の税率別内訳から直接行を起こす（内訳優先）。6サンプルで検証。"""

    def _rows(self, summary, account="備品・消耗品費", doc_category="receipt"):
        return agg.build_rows_from_tax_summary(summary, account, doc_category)

    def test_p1_gooday_8_and_10_uchizei(self):
        # 票面: 8%内税対象額124 / 10%内税対象額1531
        rows = self._rows([
            {"tax_rate": 0.08, "tax_included": True, "base_amount": 124, "tax_amount": 9},
            {"tax_rate": 0.10, "tax_included": True, "base_amount": 1531, "tax_amount": 139},
        ])
        self.assertEqual([(r["description"], r["amount"]) for r in rows],
                         [("8%", 124), ("10%", 1531)])
        self.assertEqual(rows[0]["debit_tax_type"], "課対仕入8% (軽)")
        self.assertEqual(rows[1]["debit_tax_type"], "課対仕入10%")

    def test_p3_chidori_8_sotozei_to_taxincluded(self):
        # 外税: 対象額1620 + 外税額130 → 税込1750
        rows = self._rows([
            {"tax_rate": 0.08, "tax_included": False, "base_amount": 1620, "tax_amount": 130},
        ], account="接待交際費")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 1750)
        self.assertEqual(rows[0]["debit_account"], "接待交際費")

    def test_p3_sotozei_fallback_rounding(self):
        # 外税で票面税額が無い → 1620×8%=129.6→130、税込1750
        rows = self._rows([
            {"tax_rate": 0.08, "tax_included": False, "base_amount": 1620, "tax_amount": 0},
        ])
        self.assertEqual(rows[0]["amount"], 1750)

    def test_p4_golf_three_rates_incl_taxfree(self):
        # 10%13440 / 8%650 / 非課税500、合計14590
        rows = self._rows([
            {"tax_rate": 0.10, "tax_included": True, "base_amount": 13440, "tax_amount": 1221},
            {"tax_rate": 0.08, "tax_included": True, "base_amount": 650, "tax_amount": 48},
            {"tax_rate": 0, "tax_included": True, "base_amount": 500, "tax_amount": 0},
        ], account="接待交際費")
        self.assertEqual([(r["description"], r["amount"]) for r in rows],
                         [("10%", 13440), ("8%", 650), ("対象外", 500)])
        # 非課税行も票全体の代表科目（接待交際費）を共有する
        self.assertTrue(all(r["debit_account"] == "接待交際費" for r in rows))
        self.assertEqual(rows[2]["debit_tax_type"], "対象外")
        self.assertEqual(sum(r["amount"] for r in rows), 14590)

    def test_p6_trial_uchizei_sotozei_mixed_not_merged(self):
        # 同じ10%でも外税組(税込1245)と内税組(5)は別行
        rows = self._rows([
            {"tax_rate": 0.10, "tax_included": False, "base_amount": 1132, "tax_amount": 113},
            {"tax_rate": 0.10, "tax_included": True, "base_amount": 5, "tax_amount": 0},
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["amount"], 1245)
        self.assertEqual(rows[1]["amount"], 5)
        self.assertTrue(all(r["debit_tax_type"] == "課対仕入10%" for r in rows))

    def test_zero_amount_division_skipped(self):
        rows = self._rows([
            {"tax_rate": 0.10, "tax_included": True, "base_amount": 0, "tax_amount": 0},
            {"tax_rate": 0.08, "tax_included": True, "base_amount": 100, "tax_amount": 7},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 100)

    def test_fullwidth_base_amount_normalized(self):
        # Gemini が全角/カンマ文字列で返しても正しく解釈
        rows = self._rows([
            {"tax_rate": 0.10, "tax_included": True, "base_amount": "１，２００", "tax_amount": 0},
        ])
        self.assertEqual(rows[0]["amount"], 1200)


class DetermineTaxTypesTest(unittest.TestCase):
    def test_standard_rate(self):
        self.assertEqual(agg.determine_tax_types("receipt", 0.10),
                         ("課対仕入10%", "対象外"))

    def test_reduced_rate(self):
        self.assertEqual(agg.determine_tax_types("receipt", 0.08),
                         ("課対仕入8% (軽)", "対象外"))

    def test_zero_rate_is_taxfree(self):
        self.assertEqual(agg.determine_tax_types("receipt", 0),
                         ("対象外", "対象外"))

    def test_bank_transfer_is_taxfree(self):
        self.assertEqual(agg.determine_tax_types("bank_transfer", 0.10),
                         ("対象外", "対象外"))


class SelectAggregatedDebitAccountTest(unittest.TestCase):
    def test_empty_returns_fixed(self):
        # 空グループ（内訳優先で品目が全て小計だった場合）は固定科目に退避
        self.assertEqual(agg.select_aggregated_debit_account([]),
                         agg.AGGREGATED_DEBIT_ACCOUNT_FIXED)


class CoerceTaxIncludedTest(unittest.TestCase):
    def test_none_defaults_to_included(self):
        # 日本のレシートは税込表示が大半のため既定は内税
        self.assertTrue(agg.coerce_tax_included(None))

    def test_bool_passthrough(self):
        self.assertTrue(agg.coerce_tax_included(True))
        self.assertFalse(agg.coerce_tax_included(False))

    def test_string_false_parsed(self):
        self.assertFalse(agg.coerce_tax_included("false"))

    def test_invalid_value_defaults_to_included(self):
        self.assertTrue(agg.coerce_tax_included("???"))


class CoerceTaxAmountTest(unittest.TestCase):
    def test_none_is_zero(self):
        self.assertEqual(agg.coerce_tax_amount(None), 0)

    def test_string_number_parsed(self):
        self.assertEqual(agg.coerce_tax_amount("130"), 130)

    def test_comma_and_currency_stripped(self):
        self.assertEqual(agg.coerce_tax_amount("1,300"), 1300)
        self.assertEqual(agg.coerce_tax_amount("¥130"), 130)
        self.assertEqual(agg.coerce_tax_amount("130円"), 130)

    def test_fullwidth_string_normalized(self):
        # Gemini が全角で返しても票面税額を 0 に落とさない（NFKC 正規化）
        self.assertEqual(agg.coerce_tax_amount("￥１８"), 18)
        self.assertEqual(agg.coerce_tax_amount("1，300"), 1300)

    def test_invalid_is_zero(self):
        self.assertEqual(agg.coerce_tax_amount("abc"), 0)


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
