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


class KeiyuzeiSozeikokaSplitTest(unittest.TestCase):
    """軽油税（軽油引取税）は整票一科目の例外として租税公課に分離（6/12 顧客サンプル）。"""

    def test_tax_summary_keiyuzei_label_split(self):
        # Arrange: EneJet 外税票（Sheet 行186-187）。内訳優先路径 + label
        doc = _doc(vendor="朝日石油", items=[
            _receipt_item("軽油", 5806, 0.10, "旅費交通費"),
            _receipt_item("軽油税", 708, 0, "旅費交通費"),
        ], tax_summary=[
            {"tax_rate": 0.10, "tax_included": False,
             "base_amount": 5806, "tax_amount": 581},
            {"tax_rate": 0, "tax_included": True,
             "base_amount": 708, "tax_amount": 0, "label": "軽油税"},
        ])

        # Act
        result = ocr_engine._build_entries_for_single_doc(doc)

        # Assert: 10%行=旅費交通費6,387（外税→税込）、対象外行=租税公課708
        self.assertEqual(
            [(r["debit_account"], r["amount"]) for r in result],
            [("旅費交通費", 6387), ("租税公課", 708)],
        )

    def test_tax_summary_keiyuzei_amount_fallback_without_label(self):
        # Arrange: Gemini が label を漏らしても品目側の軽油税金額一致で分離（保険）
        doc = _doc(vendor="増田石油", items=[
            _receipt_item("軽油", 4507, 0.10, "旅費交通費"),
            _receipt_item("軽油税", 493, 0, "旅費交通費"),
        ], tax_summary=[
            {"tax_rate": 0.10, "tax_included": True,
             "base_amount": 4507, "tax_amount": 410},
            {"tax_rate": 0, "tax_included": True,
             "base_amount": 493, "tax_amount": 0},
        ])

        # Act
        result = ocr_engine._build_entries_for_single_doc(doc)

        # Assert
        self.assertEqual(
            [(r["debit_account"], r["amount"]) for r in result],
            [("旅費交通費", 4507), ("租税公課", 493)],
        )

    def test_fallback_keiyuzei_item_split_from_unified_account(self):
        # Arrange: 内訳なし（fallback 逐品目路径）。増田石油 内税票
        doc = _doc(vendor="増田石油", items=[
            _receipt_item("軽油", 4507, 0.10, "旅費交通費"),
            _receipt_item("軽油税", 493, 0, "旅費交通費"),
        ])

        # Act
        result = ocr_engine._build_entries_for_single_doc(doc)

        # Assert: 整票統一後も軽油税行だけ租税公課
        self.assertEqual(
            [(r["debit_account"], r["amount"]) for r in result],
            [("旅費交通費", 4507), ("租税公課", 493)],
        )

    def test_fallback_golf_tax_item_stays_unified(self):
        # Arrange: ゴルフ場利用税は分離対象外。Gemini が誤って租税公課を
        # 付けても整票一科目（接待交際費）に統一される（P5 回帰）
        doc = _doc(vendor="秋月カントリークラブ", items=[
            _receipt_item("プレーフィ", 6000, 0.10, "接待交際費"),
            _receipt_item("ゴルフ場利用税", 200, 0, "租税公課"),
        ])

        # Act
        result = ocr_engine._build_entries_for_single_doc(doc)

        # Assert
        self.assertTrue(
            all(r["debit_account"] == "接待交際費" for r in result))

    def test_taxable_fuel_line_mentioning_keiyuzei_not_split(self):
        # Arrange: Gemini が分解せず課税の燃料行に軽油税注記を残した場合。
        # 例外は rate=0 行限定（10%行を租税公課に流出させない）
        doc = _doc(vendor="テストSS", items=[
            _receipt_item("軽油 (内軽油税 @15.0 ¥493)", 5000, 0.10, "旅費交通費"),
        ])

        # Act
        result = ocr_engine._build_entries_for_single_doc(doc)

        # Assert
        self.assertEqual(
            [(r["debit_account"], r["amount"]) for r in result],
            [("旅費交通費", 5000)],
        )

    def test_fallback_mixed_taxfree_items_keep_separate_accounts(self):
        # Arrange: 軽油税と他の非課税品目が同票に混在しても1行に潰さない
        doc = _doc(vendor="テストSS", items=[
            _receipt_item("軽油", 4507, 0.10, "旅費交通費"),
            _receipt_item("軽油税", 493, 0, "旅費交通費"),
            _receipt_item("収入印紙", 200, 0, "旅費交通費"),
        ])

        # Act
        result = ocr_engine._build_entries_for_single_doc(doc)

        # Assert: 対象外行が科目別に2行（租税公課493 / 整票科目200）
        self.assertEqual(
            [(r["debit_account"], r["amount"]) for r in result],
            [("旅費交通費", 4507), ("租税公課", 493), ("旅費交通費", 200)],
        )

    def test_keiyuzei_not_selected_as_repr_account(self):
        # Arrange: 軽油税品目が金額最大でも整票代表科目に選ばれない
        doc = _doc(vendor="テストSS", items=[
            _receipt_item("軽油税", 900, 0, "租税公課"),
            _receipt_item("洗車", 800, 0.10, "備品・消耗品費"),
        ])

        # Act
        result = ocr_engine._build_entries_for_single_doc(doc)

        # Assert: 10%行は軽油税以外の最大品目（洗車）の科目、対象外行は租税公課
        self.assertEqual(
            sorted((r["debit_account"], r["amount"]) for r in result),
            [("備品・消耗品費", 800), ("租税公課", 900)],
        )


if __name__ == "__main__":
    unittest.main()
