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


def _entry(amount, account="備品・消耗品費", tax_type="課対仕入10%"):
    """借方科目つきの仕訳エントリ（異常検出を避ける一般科目）。"""
    return {"amount": amount, "debit_account": account,
            "debit_tax_type": tax_type,
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


class AppendEntriesOutlierExemptTest(unittest.TestCase):
    """規則①: 対象外行の構造異常 → I列赤（合計照合と同経路で合流）。"""

    def _run(self, entries, entries_extra=None):
        writer = _make_writer()
        ws = _FakeWorksheet()
        call_log = []

        def fake_apply_highlight(self, worksheet, row, flags):
            call_log.append(("entry", row, flags))

        def fake_format_with_retry(self, worksheet, cell_ref, fmt,
                                   max_retries=5):
            call_log.append(("doc", cell_ref, fmt))

        data = {"date": "2026/06/01", "vendor": "焼鳥の六角堂",
                "invoice_num": "", "memo": "", "entries": entries,
                "doc_category": "receipt"}
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
                writer.append_entries("従業員", DocType.RECEIPT, data,
                                      source_url="http://x")
        return call_log, ws, buf.getvalue()

    def test_rokkakudo_hallucination_highlights_amount_column(self):
        # Arrange: 六角堂（課税7,310 + 対象外90,000）。Σ=票面=97,310 で
        #   合計照合は通る（total_mismatch は出ない）が、規則①が拾う
        # Act
        call_log, ws, out = self._run(
            [_entry(7310, tax_type="課対仕入10%"),
             _entry(90000, account="備品・消耗品費", tax_type="対象外")],
            {"total_amount": 97310})
        # Assert: I列赤が1回（合計照合は不一致でないため①単独）
        doc_calls = [c for c in call_log if c[0] == "doc"]
        i_calls = [c for c in doc_calls if c[1].startswith("I")]
        self.assertEqual(len(i_calls), 1)
        self.assertEqual(i_calls[0][1], "I6:I7")
        self.assertIn("対象外", out)

    def test_normal_receipt_no_outlier_highlight(self):
        # Arrange: 正常票（課税5,000 + 対象外200）
        # Act
        call_log, ws, out = self._run(
            [_entry(5000, tax_type="課対仕入10%"),
             _entry(200, tax_type="対象外")],
            {"total_amount": 5200})
        # Assert: I列赤なし
        i_calls = [c for c in call_log
                   if c[0] == "doc" and c[1].startswith("I")]
        self.assertEqual(i_calls, [])

    def test_mismatch_and_outlier_paint_amount_column_once(self):
        # Arrange: 合計照合NG かつ 規則①命中（対象外100,000 突出, 票面が不一致）
        # Act
        call_log, ws, out = self._run(
            [_entry(7310, tax_type="課対仕入10%"),
             _entry(100000, tax_type="対象外")],
            {"total_amount": 50000})
        # Assert: 両命中でも I列赤は1回に合流（重複塗りしない）
        i_calls = [c for c in call_log
                   if c[0] == "doc" and c[1].startswith("I")]
        self.assertEqual(len(i_calls), 1)
        self.assertEqual(i_calls[0][1], "I6:I7")

    def test_bank_transfer_exempt_not_flagged(self):
        # Arrange: bank_transfer（本体が対象外・高額）は規則①の対象外。
        #   DocType.RECEIPT 経路でも doc_category!="receipt" でスキップ（codex 指摘）
        # Act
        call_log, ws, out = self._run(
            [_entry(100000, tax_type="対象外")],
            {"doc_category": "bank_transfer"})
        # Assert: I列赤なし（規則①は真の receipt 限定）
        i_calls = [c for c in call_log
                   if c[0] == "doc" and c[1].startswith("I")]
        self.assertEqual(i_calls, [])


class AppendEntriesLowConfidenceTest(unittest.TestCase):
    """規則②: 低置信整票 → 全行（A:AB）黄、I列赤の後に置く。"""

    def _run(self, entries, entries_extra=None):
        writer = _make_writer()
        ws = _FakeWorksheet()
        call_log = []

        def fake_format_with_retry(self, worksheet, cell_ref, fmt,
                                   max_retries=5):
            call_log.append(("doc", cell_ref, fmt))

        data = {"date": "2026/06/01", "vendor": "店",
                "invoice_num": "", "memo": "", "entries": entries}
        if entries_extra:
            data.update(entries_extra)

        with patch.object(SheetsOutputWriter, "_get_or_create_tab",
                          return_value=ws), \
             patch.object(SheetsOutputWriter, "_format_with_retry",
                          fake_format_with_retry):
            buf = io.StringIO()
            with redirect_stdout(buf):
                writer.append_entries("従業員", DocType.RECEIPT, data,
                                      source_url="http://x")
        return call_log, ws, buf.getvalue()

    def test_low_confidence_paints_full_row_yellow(self):
        # Arrange: ocr_confidence=0.60 < 0.85 閾値
        # Act
        call_log, ws, out = self._run(
            [_entry(5000)], {"ocr_confidence": 0.60})
        # Assert: A6:AB6 黄が1回
        ab_calls = [c for c in call_log
                    if c[0] == "doc" and c[1] == "A6:AB6"]
        self.assertEqual(len(ab_calls), 1)
        self.assertIn("低置信", out)

    def test_high_confidence_no_yellow(self):
        # Arrange: 六角堂相当の 0.91（①主力・②は抓えない裏付け）
        # Act
        call_log, ws, out = self._run(
            [_entry(5000)], {"ocr_confidence": 0.91})
        # Assert: 黄なし
        ab_calls = [c for c in call_log
                    if c[0] == "doc" and c[1].startswith("A6:AB")]
        self.assertEqual(ab_calls, [])

    def test_missing_confidence_no_yellow(self):
        # Arrange: ocr_confidence 欠損（Vision 兜底等の無信号）
        # Act
        call_log, ws, out = self._run([_entry(5000)])
        # Assert: 黄なし
        ab_calls = [c for c in call_log
                    if c[0] == "doc" and c[1].startswith("A6:AB")]
        self.assertEqual(ab_calls, [])

    def test_red_applied_after_yellow(self):
        # Arrange: 合計照合NG（赤）+ 低置信（黄）が同票に同居
        # Act
        call_log, ws, out = self._run(
            [_entry(5000)], {"total_amount": 99999, "ocr_confidence": 0.50})
        # Assert: A:AB 黄を先に塗り、I列赤を後に塗る。全行黄が I列を含むため、
        # 赤を最終色にして漏账の赤信号が黄に被覆されないようにする（codex 指摘の修正）
        i_idx = next((i for i, c in enumerate(call_log)
                      if c[0] == "doc" and c[1].startswith("I")), None)
        ab_idx = next((i for i, c in enumerate(call_log)
                       if c[0] == "doc" and c[1].startswith("A6:AB")), None)
        self.assertIsNotNone(i_idx)
        self.assertIsNotNone(ab_idx)
        self.assertGreater(i_idx, ab_idx)


if __name__ == "__main__":
    unittest.main()
