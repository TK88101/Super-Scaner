"""sheets_output._build_description の単体テスト（6/11 顧客回答: 摘要「店名 税率」）。

sheets_output は gspread を import するため venv311 で実行する:
    venv311/bin/python -m unittest test_sheets_output -v
    venv311/bin/python -m pytest test_sheets_output.py -v
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from doc_types import DocType
from sheets_output import _build_description, SheetsOutputWriter


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


class _FakeWorksheet:
    """append_entries が触れる最小限の worksheet スタブ。"""

    def __init__(self):
        # legend 4 行 + header 1 行（= 既存データ5行、新規書き込みは row6 から）
        self._values = [["L1"], ["L2"], ["L3"], ["L4"], ["H"]]
        self.appended = []

    def get_all_values(self):
        return list(self._values)

    def append_rows(self, rows, value_input_option=None):
        self.appended.extend(rows)
        self._values.extend(rows)


def _make_writer():
    """gspread 認証を回避した SheetsOutputWriter を組み立てる。"""
    writer = SheetsOutputWriter.__new__(SheetsOutputWriter)
    # _get_next_txn_no（line 59）が try 外で参照する属性。欠けると AttributeError
    writer._tab_next_txn = {}
    return writer


def _entry(amount, account="備品・消耗品費"):
    """借方科目つきの仕訳エントリ（異常検出を避ける一般科目）。"""
    return {"amount": amount, "debit_account": account,
            "description": "商品", "credit_account": "未払金"}


class AppendEntriesTotalMismatchTest(unittest.TestCase):
    """[B'] doc 級照合 → 金額列（I列）赤ハイライト。"""

    def _run(self, doc_type, entries, entries_extra=None):
        """append_entries を fake ws で実行し、call log と ws を返す。"""
        writer = _make_writer()
        ws = _FakeWorksheet()
        call_log = []

        def fake_apply_highlight(self, worksheet, row, flags):
            call_log.append(("entry", row, flags))

        def fake_format_with_retry(self, worksheet, cell_ref, fmt,
                                   max_retries=5):
            call_log.append(("doc", cell_ref, fmt))

        data = {"date": "2026/06/01", "vendor": "店", "invoice_num": "",
                "memo": "", "entries": entries}
        if entries_extra:
            data.update(entries_extra)

        with patch.object(SheetsOutputWriter, "_get_or_create_tab",
                          return_value=ws), \
             patch.object(SheetsOutputWriter, "_apply_anomaly_highlight",
                          fake_apply_highlight), \
             patch.object(SheetsOutputWriter, "_format_with_retry",
                          fake_format_with_retry):
            buf = io.StringIO()
            with redirect_stdout(buf):
                writer.append_entries("従業員", doc_type, data,
                                      source_url="http://x")
        return call_log, ws, buf.getvalue()

    def test_total_mismatch_highlights_amount_column_range(self):
        # Arrange: 2 entries (3000+2870=5870)、票面6456 → 不一致
        # Act
        call_log, ws, _ = self._run(
            DocType.RECEIPT, [_entry(3000), _entry(2870)],
            {"total_amount": 6456})
        # Assert: doc 高亮が "I6:I7" に1回、かつ全 entry 高亮の後（赤壓黄）
        doc_calls = [c for c in call_log if c[0] == "doc"]
        self.assertEqual(len(doc_calls), 1)
        self.assertEqual(doc_calls[0][1], "I6:I7")
        doc_idx = call_log.index(doc_calls[0])
        entry_idxs = [i for i, c in enumerate(call_log) if c[0] == "entry"]
        if entry_idxs:
            self.assertGreater(doc_idx, max(entry_idxs))

    def test_total_match_produces_no_doc_highlight(self):
        # Arrange: Σ==total（6456）→ 照合 OK
        # Act
        call_log, ws, out = self._run(
            DocType.RECEIPT, [_entry(3586), _entry(2870)],
            {"total_amount": 6456})
        # Assert: doc 高亮なし、スキップ通知も出ない
        self.assertEqual([c for c in call_log if c[0] == "doc"], [])
        self.assertNotIn("合計照合スキップ", out)

    def test_zero_amount_entry_excluded_from_written_rows_and_range(self):
        # Arrange: 有効5870 + amount0（書き込みスキップ）、票面6456
        # Act
        call_log, ws, _ = self._run(
            DocType.RECEIPT, [_entry(5870), _entry(0)],
            {"total_amount": 6456})
        # Assert: 書き込みは1行のみ、高亮範囲は "I6:I6"（口徑=実際書き込み行）
        self.assertEqual(len(ws.appended), 1)
        doc_calls = [c for c in call_log if c[0] == "doc"]
        self.assertEqual(len(doc_calls), 1)
        self.assertEqual(doc_calls[0][1], "I6:I6")

    def test_non_receipt_doc_type_skips_check(self):
        # Arrange: PURCHASE_INVOICE は不一致 total を持っても照合しない
        # Act
        call_log, ws, out = self._run(
            DocType.PURCHASE_INVOICE, [_entry(3000)],
            {"total_amount": 999999})
        # Assert: doc 高亮なし、スキップ通知も出ない（dispatch 閘）
        self.assertEqual([c for c in call_log if c[0] == "doc"], [])
        self.assertNotIn("合計照合スキップ", out)

    def test_receipt_without_total_prints_skip_notice(self):
        # Arrange: RECEIPT で total_amount 無し、1 entry
        # Act
        call_log, ws, out = self._run(DocType.RECEIPT, [_entry(5870)])
        # Assert: doc 高亮なし、かつ可観測性のスキップ通知が出る
        self.assertEqual([c for c in call_log if c[0] == "doc"], [])
        self.assertIn("合計照合スキップ", out)


if __name__ == "__main__":
    unittest.main()
