"""sheets_output._build_description の単体テスト（6/11 顧客回答: 摘要「店名 税率」）。

sheets_output は gspread を import するため venv311 で実行する:
    venv311/bin/python -m unittest test_sheets_output -v
    venv311/bin/python -m pytest test_sheets_output.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from sheets_output import _build_description


class BuildDescriptionTest(unittest.TestCase):
    """摘要の組み立て。領収書 tab は「店名 税率」（空白区切り・対象後綴なし）。"""

    def test_receipt_joins_vendor_and_rate_with_space(self):
        # Arrange / Act / Assert: 領収書は「店名 税率」形式（例: ファミマ 10%）
        self.assertEqual(_build_description("receipt", "ファミマ", "10%"),
                         "ファミマ 10%")

    def test_receipt_taxfree_label_kept(self):
        # Arrange / Act / Assert: 対象外ラベルはそのまま空白区切りで後置
        self.assertEqual(_build_description("receipt", "ファミマ", "対象外"),
                         "ファミマ 対象外")

    def test_receipt_empty_vendor_has_no_leading_space(self):
        # Arrange / Act / Assert: 店名が空なら前導空白を残さない
        self.assertEqual(_build_description("receipt", "", "10%"), "10%")

    def test_other_doc_type_keeps_hyphen_format(self):
        # Arrange / Act / Assert: 領収書以外は既存の「店名 - 摘要」を維持
        self.assertEqual(
            _build_description("purchase_invoice", "X社", "部品代"),
            "X社 - 部品代")


if __name__ == "__main__":
    unittest.main()
