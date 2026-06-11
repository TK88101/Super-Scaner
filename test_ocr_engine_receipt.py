"""ocr_engine の領収書まわり単体テスト（6/11 顧客回答: 1枚=1借方科目）。

ocr_engine は paddleocr / google.generativeai / pdf2image 等の重依存を
import するため venv311 で実行する:
    venv311/bin/python -m unittest test_ocr_engine_receipt -v
    venv311/bin/python -m pytest test_ocr_engine_receipt.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import ocr_engine


def _doc(doc_category="receipt", vendor="テスト店", items=None,
         tax_summary=None, payment_method="現金"):
    """単一書類 doc 生成ヘルパー。"""
    doc = {
        "doc_category": doc_category,
        "payment_method": payment_method,
        "vendor": vendor,
        "items": items if items is not None else [],
    }
    if tax_summary is not None:
        return {**doc, "tax_summary": tax_summary}
    return doc


def _receipt_item(description, amount, tax_rate, debit_account):
    """品目エントリ生成ヘルパー。"""
    return {
        "description": description,
        "amount": amount,
        "tax_rate": tax_rate,
        "debit_account": debit_account,
    }


class BuildEntriesForSingleDocReceiptTest(unittest.TestCase):
    """普通領収書は1枚=1借方科目（整票の用途で決定。6/11 顧客回答）。"""

    def test_receipt_mixed_rates_unified_to_max_amount_account(self):
        # Arrange: 8%/10% 混在 + 科目バラバラ。金額最大は工具1,200（備品・消耗品費）
        doc = _doc(items=[
            _receipt_item("弁当", 500, 0.08, "接待交際費"),
            _receipt_item("エコバッグ", 30, 0.08, "備品・消耗品費"),
            _receipt_item("工具", 1200, 0.10, "備品・消耗品費"),
        ])

        # Act
        result = ocr_engine._build_entries_for_single_doc(doc)

        # Assert: 税率別2行とも金額最大品目の科目で統一される
        self.assertEqual(len(result), 2)
        self.assertTrue(
            all(r["debit_account"] == "備品・消耗品費" for r in result))

    def test_tax_summary_with_empty_items_applies_golf_vendor_override(self):
        # Arrange: items 空 + 票面内訳あり + ゴルフ場 vendor
        doc = _doc(
            vendor="○○カントリークラブ",
            items=[],
            tax_summary=[{"tax_rate": 0.10, "tax_included": True,
                          "base_amount": 13440, "tax_amount": 0}],
        )

        # Act: items 空でも UnboundLocalError を出さない（vendor hoist 回帰）
        result = ocr_engine._build_entries_for_single_doc(doc)

        # Assert: 全行が接待交際費（代表科目にも vendor override が効く）
        self.assertEqual(len(result), 1)
        self.assertTrue(
            all(r["debit_account"] == "接待交際費" for r in result))

    def test_bank_transfer_keeps_per_item_accounts(self):
        # Arrange: 振込本体(未払金, 税率0) + 手数料(支払手数料, 10%)
        doc = _doc(
            doc_category="bank_transfer",
            vendor="福岡銀行",
            payment_method="振込",
            items=[
                _receipt_item("振込本体", 10000, 0, "未払金"),
                _receipt_item("振込手数料", 330, 0.10, "支払手数料"),
            ],
        )

        # Act
        result = ocr_engine._build_entries_for_single_doc(doc)

        # Assert: 整票一科目は receipt 限定。bank_transfer は科目を各自保持
        self.assertEqual(len(result), 2)
        self.assertEqual({r["debit_account"] for r in result},
                         {"未払金", "支払手数料"})


class OverrideAccountByVendorTest(unittest.TestCase):
    """ゴルフ関連キーワードの境界（casefold 一致 + 厳格先勝ち）。"""

    def test_golf_mixed_case_overrides_to_entertainment(self):
        # Arrange / Act: 大小文字混在の Golf も casefold で命中する
        result = ocr_engine._override_account_by_vendor("○○Golf倶楽部", "消耗品費")

        # Assert: ゴルフ場 → 接待交際費（6/11 顧客回答）
        self.assertEqual(result, "接待交際費")

    def test_golf5_exclusion_wins_first_and_is_not_reversed(self):
        # Arrange / Act: ゴルフ用品店。gemini 科目が除外項と同じでも反転されない
        result = ocr_engine._override_account_by_vendor(
            "GOLF5 福岡店", "備品・消耗品費")

        # Assert: 先勝ちの除外項（GOLF5）が維持され GOLF で反転しない
        self.assertEqual(result, "備品・消耗品費")

    def test_fullwidth_golf5_exclusion_via_nfkc(self):
        # Arrange / Act: OCR が全角数字で返した「ゴルフ５」も NFKC で除外項に命中する
        result = ocr_engine._override_account_by_vendor(
            "ゴルフ５ 博多店", "消耗品費")

        # Assert: GOLF5 除外項（備品・消耗品費）が適用され、ゴルフ場扱いにならない
        self.assertEqual(result, "備品・消耗品費")

    def test_bank_transfer_vendor_keyword_does_not_rewrite_fixed_accounts(self):
        # Arrange: ゴルフ場宛の銀行振込。借方は固定科目（未払金/支払手数料）
        doc = {
            "doc_category": "bank_transfer",
            "payment_method": "振込",
            "vendor": "○○カントリークラブ",
            "items": [
                {"description": "振込本体", "amount": 50000,
                 "tax_rate": 0, "debit_account": "未払金"},
                {"description": "振込手数料", "amount": 330,
                 "tax_rate": 0.10, "debit_account": "支払手数料"},
            ],
        }

        # Act
        result = ocr_engine._build_entries_for_single_doc(doc)

        # Assert: vendor がゴルフ語彙を含んでも固定科目は上書きされない
        accounts = sorted(e["debit_account"] for e in result)
        self.assertEqual(accounts, ["支払手数料", "未払金"])

    def test_spaced_golf5_exclusion_ignores_whitespace(self):
        # Arrange / Act / Assert: OCR が語中に空白を挟んだ「GOLF 5」「ゴルフ ５」も
        # 空白無視照合で除外項に命中し、汎用 GOLF 規則に反転されない
        self.assertEqual(
            ocr_engine._override_account_by_vendor("GOLF 5 マークイズ店", "消耗品費"),
            "備品・消耗品費")
        self.assertEqual(
            ocr_engine._override_account_by_vendor("ゴルフ ５", "消耗品費"),
            "備品・消耗品費")

    def test_golf_retail_chains_stay_supplies(self):
        # Arrange / Act / Assert: ゴルフ用品連鎖店は物販 → 備品・消耗品費のまま
        # （汎用ゴルフ/GOLF 規則に反転されない）
        for vendor in ("ゴルフパートナー 福岡店", "GOLF Partner", "つるやゴルフ"):
            with self.subTest(vendor=vendor):
                self.assertEqual(
                    ocr_engine._override_account_by_vendor(vendor, "消耗品費"),
                    "備品・消耗品費")

    def test_omega_vendors_not_hit_by_mega_rule(self):
        # Arrange / Act / Assert: MEGA 規則が omega/オメガ 系店名を誤爆しない
        self.assertEqual(
            ocr_engine._override_account_by_vendor("オメガ時計店", "消耗品費"),
            "消耗品費")
        self.assertEqual(
            ocr_engine._override_account_by_vendor("omega sports", "消耗品費"),
            "消耗品費")

    def test_kantsuri_club_overrides_to_entertainment(self):
        # Arrange / Act: カンツリー表記のゴルフ場
        result = ocr_engine._override_account_by_vendor(
            "○○カンツリー倶楽部", "消耗品費")

        # Assert: 接待交際費に上書きされる
        self.assertEqual(result, "接待交際費")


if __name__ == "__main__":
    unittest.main()
