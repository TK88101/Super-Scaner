"""sheets_output._build_description の単体テスト（6/11 顧客回答: 摘要「店名 税率」）。

sheets_output は gspread を import するため venv311 で実行する:
    venv311/bin/python -m unittest test_sheets_output -v
    venv311/bin/python -m pytest test_sheets_output.py -v
"""
import io
import os
import sys
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import gspread

from doc_types import DocType
from sheets_output import (
    APPEND_RESULT_PLACEHOLDER, APPEND_RESULT_POSTED,
    AUDIT_HEADERS, AUDIT_TAB_NAME, _build_description, SheetsOutputWriter,
)


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
    writer._tabs_sanitized = set()  # _sanitize_trailing_once が参照する
    return writer


def _entry(amount, account="備品・消耗品費", tax_type="課対仕入10%"):
    """借方科目つきの仕訳エントリ（異常検出を避ける一般科目）。"""
    return {"amount": amount, "debit_account": account,
            "debit_tax_type": tax_type,
            "description": "商品", "credit_account": "未払金"}


def _run_append_entries(doc_type, data, patch_highlight=True,
                        patch_unrecognized=False, capture_written=False):
    """append_entries を fake ws で流す共通ハーネス（各テスト類の _run が委譲）。

    call_log に ("entry", row, flags)（_apply_anomaly_highlight）と
    ("doc", cell_ref, fmt)（_format_with_retry）を時系列で記録する。
    patch_highlight=False は _apply_anomaly_highlight を実物のまま通す
    （低置信テスト等、実経路の塗り順を検証するケース用）。注意: その場合
    ("entry", ...) は記録されず、実物の塗りは fake ws 相手に握り潰される
    （append_entries 内の try/except）ため call_log からは見えない。
    capture_written=True は _write_with_retry / _ensure_row_capacity を
    差し替え、実際に書かれた行を written へ捕捉する（_write_unrecognized_row
    は実物のまま通る → 占位行の中身を検証するケース用）。

    Returns: (call_log, ws, stdout_text, placeholder_calls, written)
    """
    writer = _make_writer()
    ws = _FakeWorksheet()
    call_log = []
    placeholder_calls = []
    written = []

    def fake_apply_highlight(self, worksheet, row, flags):
        call_log.append(("entry", row, flags))

    def fake_format_with_retry(self, worksheet, cell_ref, fmt,
                               max_retries=5):
        call_log.append(("doc", cell_ref, fmt))

    def fake_unrecognized(self, worksheet, tab_name, entries_data,
                          source_url):
        placeholder_calls.append((tab_name, entries_data, source_url))

    with ExitStack() as stack:
        stack.enter_context(patch.object(
            SheetsOutputWriter, "_get_or_create_tab", return_value=ws))
        stack.enter_context(patch.object(
            SheetsOutputWriter, "_format_with_retry",
            fake_format_with_retry))
        if patch_highlight:
            stack.enter_context(patch.object(
                SheetsOutputWriter, "_apply_anomaly_highlight",
                fake_apply_highlight))
        if patch_unrecognized:
            stack.enter_context(patch.object(
                SheetsOutputWriter, "_write_unrecognized_row",
                fake_unrecognized))
        if capture_written:
            stack.enter_context(patch.object(
                SheetsOutputWriter, "_ensure_row_capacity"))
            stack.enter_context(patch.object(
                SheetsOutputWriter, "_write_with_retry",
                side_effect=lambda w, rows: written.extend(rows)))
        buf = io.StringIO()
        with redirect_stdout(buf):
            writer.append_entries("従業員", doc_type, data,
                                  source_url="http://x")
    return call_log, ws, buf.getvalue(), placeholder_calls, written


class AppendEntriesTotalMismatchTest(unittest.TestCase):
    """[B'] doc 級照合 → 金額列（I列）赤ハイライト。"""

    def _run(self, doc_type, entries, entries_extra=None):
        """append_entries を fake ws で実行し、call log と ws を返す。"""
        data = {"date": "2026/06/01", "vendor": "店", "invoice_num": "",
                "memo": "", "entries": entries}
        if entries_extra:
            data.update(entries_extra)
        call_log, ws, out, _, _ = _run_append_entries(doc_type, data)
        return call_log, ws, out

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
        data = {"date": "2026/06/01", "vendor": "焼鳥の六角堂",
                "invoice_num": "", "memo": "", "entries": entries,
                "doc_category": "receipt"}
        if entries_extra:
            data.update(entries_extra)
        call_log, ws, out, _, _ = _run_append_entries(DocType.RECEIPT, data)
        return call_log, ws, out

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
        # _apply_anomaly_highlight は実物を通す（実経路の塗り順を検証するため）
        data = {"date": "2026/06/01", "vendor": "店",
                "invoice_num": "", "memo": "", "entries": entries}
        if entries_extra:
            data.update(entries_extra)
        call_log, ws, out, _, _ = _run_append_entries(
            DocType.RECEIPT, data, patch_highlight=False)
        return call_log, ws, out

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


class AppendEntriesTagColumnTest(unittest.TestCase):
    """U列(タグ)自動記入: 標色行に「赤系/橙系/黄系」を埋め込む（社長要望 6/18）。"""

    TAG_COL = 20  # U列 = MF_HEADERS index 20

    def _run(self, doc_type, entries, entries_extra=None,
             vendor="店", invoice_num=""):
        data = {"date": "2026/06/01", "vendor": vendor,
                "invoice_num": invoice_num, "memo": "", "entries": entries}
        if entries_extra:
            data.update(entries_extra)
        _, ws, _, _, _ = _run_append_entries(doc_type, data)
        return ws

    def test_clean_row_has_empty_tag(self):
        # Arrange: 異常なし（vendor有・有効T番号・total一致・置信なし）→ 無標色
        ws = self._run(DocType.RECEIPT, [_entry(6456)],
                       {"total_amount": 6456}, invoice_num="T9234567890123")
        self.assertEqual(ws.appended[0][self.TAG_COL], "")

    def test_single_cell_missing_invoice_tags_yellow(self):
        # Arrange: T番号空(low=黄, H列単体)。他は正常 → 行タグ「黄系」
        ws = self._run(DocType.RECEIPT, [_entry(6456)],
                       {"total_amount": 6456}, invoice_num="")
        self.assertEqual(ws.appended[0][self.TAG_COL], "黄系")

    def test_total_mismatch_tags_all_rows_red(self):
        # Arrange: Σ(3000+2870=5870) ≠ 票面6456 → 全行 I列赤 → 全行「赤系」
        ws = self._run(DocType.RECEIPT, [_entry(3000), _entry(2870)],
                       {"total_amount": 6456})
        self.assertEqual([r[self.TAG_COL] for r in ws.appended],
                         ["赤系", "赤系"])

    def test_low_confidence_tags_all_rows_yellow(self):
        # Arrange: 低置信(0.50) かつ total一致 → 全行黄 →「黄系」
        ws = self._run(DocType.RECEIPT, [_entry(5000)],
                       {"ocr_confidence": 0.50, "total_amount": 5000},
                       invoice_num="T9234567890123")
        self.assertEqual(ws.appended[0][self.TAG_COL], "黄系")

    def test_red_overrides_yellow_in_tag(self):
        # Arrange: 低置信(黄) + total不符(赤) 同居 → タグは「赤系」
        ws = self._run(DocType.RECEIPT, [_entry(5000)],
                       {"ocr_confidence": 0.50, "total_amount": 99999},
                       invoice_num="T9234567890123")
        self.assertEqual(ws.appended[0][self.TAG_COL], "赤系")

    def test_missing_vendor_tags_orange(self):
        # Arrange: 取引先空(medium=橙) のみ。total一致・置信なし・T番号あり
        ws = self._run(DocType.RECEIPT, [_entry(5000)],
                       {"total_amount": 5000}, vendor="",
                       invoice_num="T9234567890123")
        self.assertEqual(ws.appended[0][self.TAG_COL], "橙系")


class GridCapacityTest(unittest.TestCase):
    """自動拡容バグ対策: 事前バッファ確保と空尾行の継承色リセット。"""

    class _GridWS(_FakeWorksheet):
        def __init__(self, row_count):
            super().__init__()
            self.row_count = row_count
            self.added = 0

        def add_rows(self, n):
            self.added += n
            self.row_count += n

    def test_ensure_capacity_grows_when_near_boundary(self):
        # Arrange: グリッド1000、データ末尾が990に達する見込み
        from sheets_output import GRID_ROW_BUFFER
        writer = _make_writer()
        ws = self._GridWS(1000)
        # Act
        writer._ensure_row_capacity(ws, 990)
        # Assert: 990 + buffer を満たすまで add_rows された
        self.assertEqual(ws.row_count, 990 + GRID_ROW_BUFFER)
        self.assertGreater(ws.added, 0)

    def test_ensure_capacity_noop_when_enough_room(self):
        # Arrange: 余裕十分
        writer = _make_writer()
        ws = self._GridWS(5000)
        # Act
        writer._ensure_row_capacity(ws, 10)
        # Assert: 拡容なし
        self.assertEqual(ws.added, 0)
        self.assertEqual(ws.row_count, 5000)

    def test_sanitize_clears_empty_tail(self):
        # Arrange: グリッド2000、データ末尾1005 → 1006..2000 を白に戻す
        writer = _make_writer()
        ws = self._GridWS(2000)
        calls = []
        with patch.object(SheetsOutputWriter, "_format_with_retry",
                          lambda self, w, ref, fmt, **k: calls.append(ref)):
            writer._sanitize_trailing_once(ws, "従業員_領収書", 1005)
        self.assertEqual(calls, ["A1006:AB2000"])

    def test_sanitize_noop_when_no_tail(self):
        # Arrange: データがグリッド最下行まで埋まっている
        writer = _make_writer()
        ws = self._GridWS(1005)
        calls = []
        with patch.object(SheetsOutputWriter, "_format_with_retry",
                          lambda self, w, ref, fmt, **k: calls.append(ref)):
            writer._sanitize_trailing_once(ws, "従業員_領収書", 1005)
        self.assertEqual(calls, [])

    def test_sanitize_runs_only_once_per_tab(self):
        # Arrange: 同タブを2回呼ぶ → 2回目は no-op（無駄な API を出さない）
        writer = _make_writer()
        ws = self._GridWS(2000)
        calls = []
        with patch.object(SheetsOutputWriter, "_format_with_retry",
                          lambda self, w, ref, fmt, **k: calls.append(ref)):
            writer._sanitize_trailing_once(ws, "従業員_領収書", 1005)
            writer._sanitize_trailing_once(ws, "従業員_領収書", 1500)
        # Assert: 初回のみ format。2回目は呼ばれない
        self.assertEqual(calls, ["A1006:AB2000"])

    def test_capacity_helpers_swallow_errors_on_plain_worksheet(self):
        # Arrange: row_count/add_rows を持たない素の fake でも例外を投げない
        writer = _make_writer()
        ws = _FakeWorksheet()
        buf = io.StringIO()
        with redirect_stdout(buf):
            writer._ensure_row_capacity(ws, 10)   # row_count 無し → 握り潰す
            writer._sanitize_trailing_once(ws, "t", 10)  # 同上
        # Assert: 例外伝播せず（ここまで到達すればOK）
        self.assertTrue(True)


class AppendEntriesSilentLossGuardTest(unittest.TestCase):
    """仕訳ゼロページの無音データ欠落防止（占位行への兜底）。

    上流が `_unrecognized` を立て忘れた entries=[]、および全 entry が金額
    0/None で書き込み行が残らないケースは、従来 1 行も書かずに return し、
    main は Success 判定 → 原票アーカイブ → 誰も気づかない欠落になっていた。
    どちらも認識不能の占位行（赤タグ）を書くことを固定する。
    """

    def _run(self, data):
        """append_entries を fake ws で実行し、占位行呼び出しと ws を返す。"""
        _, ws, out, placeholder_calls, _ = _run_append_entries(
            DocType.RECEIPT, data, patch_unrecognized=True)
        return placeholder_calls, ws, out

    @staticmethod
    def _data(entries, **extra):
        base = {"date": "", "vendor": "", "invoice_num": "", "memo": "",
                "entries": entries}
        base.update(extra)
        return base

    def test_missing_unrecognized_flag_still_writes_placeholder(self):
        # Arrange: entries=[] かつ _unrecognized 無し（上流のフラグ漏れを模す）
        # Act
        calls, ws, out = self._run(self._data([]))
        # Assert: 無音 return せず占位行を1回書く（通常行は書かない）
        self.assertEqual(len(calls), 1)
        self.assertEqual(ws.appended, [])
        self.assertIn("認識不能として記録", out)

    def test_unrecognized_flag_writes_placeholder(self):
        # Arrange: 既存経路（_unrecognized=True）の挙動維持
        # Act
        calls, ws, _ = self._run(self._data([], _unrecognized=True))
        # Assert
        self.assertEqual(len(calls), 1)
        self.assertEqual(ws.appended, [])

    def test_all_zero_amount_entries_write_placeholder(self):
        # Arrange: entries はあるが全行 金額0/None → rows が空になるケース
        entries = [_entry(0), {"amount": None, "debit_account": "備品・消耗品費",
                               "description": "商品", "credit_account": "未払金"}]
        # Act
        calls, ws, out = self._run(self._data(entries))
        # Assert: 通常行ゼロ + 占位行1回 + 可観測ログ
        self.assertEqual(ws.appended, [])
        self.assertEqual(len(calls), 1)
        self.assertIn("認識不能として記録", out)

    def test_valid_entries_do_not_write_placeholder(self):
        # Arrange: 有効金額があれば従来通り通常行のみ
        # Act
        calls, ws, _ = self._run(self._data([_entry(1000)]))
        # Assert
        self.assertEqual(len(ws.appended), 1)
        self.assertEqual(calls, [])

    def test_zero_amount_placeholder_uses_standard_label_not_gemini_memo(self):
        # Arrange: 全行金額0 かつ Gemini が memo を返しているケース。
        # 摘要ラベルは _write_unrecognized_row の標準文言であるべきで、
        # Gemini の memo が占位行の摘要を乗っ取ってはいけない
        # （memo 通過は _unrecognized を明示した producer だけの特権）。
        data = self._data([_entry(0)], date="2026/06/01", vendor="店",
                          memo="Geminiが返した任意の要約テキスト")
        # Act: _write_unrecognized_row は実物を通す（written で捕捉）
        _, _, _, _, written = _run_append_entries(
            DocType.RECEIPT, data, capture_written=True)
        # Assert: 占位行1行、摘要は標準ラベル（date/vendor 有 → 部分認識）
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0][18], "⚠ 部分認識（金額なし）")

    def test_flagged_unrecognized_keeps_producer_memo(self):
        # Arrange: main の部分エラー占位行など、_unrecognized を明示した
        # producer の memo は従来通り摘要へ通す（挙動維持の固定）
        data = self._data([], _unrecognized=True,
                          memo="⚠ ページ処理エラー 2/8頁 手動再スキャン要")
        # Act
        _, _, _, _, written = _run_append_entries(
            DocType.RECEIPT, data, capture_written=True)
        # Assert
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0][18],
                         "⚠ ページ処理エラー 2/8頁 手動再スキャン要")


from sheets_output import TAG_COL_INDEX  # noqa: E402
from tag_rules import UNRECOGNIZED_TAG  # noqa: E402


def _receipt_result(vendor, entries, total=None, invoice="T9234567890123", **extra):
    """headless 頁経路用の 1 票（result）dict を組み立てる。"""
    data = {"date": "2026/06/01", "vendor": vendor, "invoice_num": invoice,
            "memo": "", "entries": entries, "doc_category": "receipt"}
    if total is not None:
        data["total_amount"] = total
    data.update(extra)
    return data


class BuildPageWriteOffsetTest(unittest.TestCase):
    """T2-1（DoD③・必須）: 頁内複数票の高亮オフセットが票段ごとに正しく、隣票へ漏れない。

    build_page_write は純データ（PageWrite.highlight_ops は頁相対 offset）——ws 不要。
    """

    def test_pure_no_worksheet_side_effect(self):
        writer = _make_writer()
        r1 = _receipt_result("店A", [_entry(5000)], total=5000)
        with patch.object(SheetsOutputWriter, "_get_or_create_tab") as m_tab, \
             patch.object(SheetsOutputWriter, "_write_with_retry") as m_write:
            pw = writer.build_page_write(
                "従業員", DocType.RECEIPT, [r1], ["http://u1"], start_txn_no=1)
        m_tab.assert_not_called()   # 書込副作用ゼロ（tab も引かない）
        m_write.assert_not_called()
        self.assertIsInstance(pw.rows, list)

    def test_second_ticket_red_does_not_leak_to_first(self):
        writer = _make_writer()
        # 票1 正常 1 行（total 一致）/ 票2 合計不符 1 行 → I列赤
        r1 = _receipt_result("店A", [_entry(5000)], total=5000)
        r2 = _receipt_result("店B", [_entry(3000)], total=9999)
        pw = writer.build_page_write(
            "従業員", DocType.RECEIPT, [r1, r2], ["u1", "u2"], start_txn_no=1)
        self.assertEqual(len(pw.rows), 2)
        red_ops = [o for o in pw.highlight_ops
                   if o.col == "I" and o.severity == "high"]
        self.assertEqual(len(red_ops), 1)
        # 票2 は page offset 1。赤は offset 1 のみ（票1=offset0 に漏れない）
        self.assertEqual((red_ops[0].row_start, red_ops[0].row_end), (1, 1))

    def test_first_ticket_red_stays_at_offset_zero(self):
        writer = _make_writer()
        r1 = _receipt_result("店A", [_entry(3000)], total=9999)  # 異常
        r2 = _receipt_result("店B", [_entry(5000)], total=5000)  # 正常
        pw = writer.build_page_write(
            "従業員", DocType.RECEIPT, [r1, r2], ["u1", "u2"], start_txn_no=1)
        red_ops = [o for o in pw.highlight_ops
                   if o.col == "I" and o.severity == "high"]
        self.assertEqual(len(red_ops), 1)
        self.assertEqual((red_ops[0].row_start, red_ops[0].row_end), (0, 0))

    def test_per_entry_flag_lands_on_correct_page_row(self):
        writer = _make_writer()
        # 票1 正常 2 行 / 票2（T番号空）1 行 → per-entry 黄（単体セル, low）
        r1 = _receipt_result("店A", [_entry(5000), _entry(3000)], total=8000)
        r2 = _receipt_result("店B", [_entry(4000)], total=4000, invoice="")
        pw = writer.build_page_write(
            "従業員", DocType.RECEIPT, [r1, r2], ["u1", "u2"], start_txn_no=1)
        self.assertEqual(len(pw.rows), 3)
        # 票2 は page offset 2。単体セル low op は offset 2 に載る（票1 の 0/1 に漏れない）
        single_low = [o for o in pw.highlight_ops
                      if o.severity == "low" and o.col != "full"]
        self.assertTrue(single_low)
        for o in single_low:
            self.assertEqual((o.row_start, o.row_end), (2, 2))

    def test_txn_number_advances_per_ticket(self):
        writer = _make_writer()
        r1 = _receipt_result("店A", [_entry(5000), _entry(3000)], total=8000)
        r2 = _receipt_result("店B", [_entry(4000)], total=4000)
        pw = writer.build_page_write(
            "従業員", DocType.RECEIPT, [r1, r2], ["u1", "u2"], start_txn_no=7)
        # 票内は同一取引No、票ごとに +1
        self.assertEqual([r[0] for r in pw.rows], [7, 7, 8])
        self.assertEqual(pw.txn_range, (7, 8))


class CommitPageTest(unittest.TestCase):
    """T2-2/3: 頁級一度書込（単一 append_rows）＋ offset→絶対行 translation ＋占位入頁。"""

    def _commit(self, writer, pw, ws, fmt_calls):
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                SheetsOutputWriter, "_get_or_create_tab", return_value=ws))
            stack.enter_context(patch.object(
                SheetsOutputWriter, "_format_with_retry",
                lambda self, w, ref, fmt, max_retries=5: fmt_calls.append(ref)))
            stack.enter_context(patch.object(
                SheetsOutputWriter, "_ensure_row_capacity"))
            stack.enter_context(patch.object(
                SheetsOutputWriter, "_sanitize_trailing_once"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                return writer.commit_page(pw)

    def test_single_append_rows_for_whole_page(self):
        writer = _make_writer()
        ws = _FakeWorksheet()
        calls = {"n": 0}
        base = ws.append_rows

        def counting(rows, value_input_option=None):
            calls["n"] += 1
            base(rows, value_input_option=value_input_option)

        ws.append_rows = counting
        r1 = _receipt_result("店A", [_entry(5000)], total=5000)
        r2 = _receipt_result("店B", [_entry(3000)], total=3000)
        pw = writer.build_page_write(
            "従業員", DocType.RECEIPT, [r1, r2], ["u1", "u2"], start_txn_no=1)
        rng = self._commit(writer, pw, ws, [])
        self.assertEqual(calls["n"], 1)          # 頁原子＝append 1 回
        self.assertEqual(len(ws.appended), 2)
        self.assertEqual(rng, (6, 7))            # legend4+header1=5 → start 6

    def test_ops_translate_to_absolute_rows(self):
        writer = _make_writer()
        ws = _FakeWorksheet()
        fmt_calls = []
        r1 = _receipt_result("店A", [_entry(5000)], total=5000)   # 正常
        r2 = _receipt_result("店B", [_entry(3000)], total=9999)   # 赤
        pw = writer.build_page_write(
            "従業員", DocType.RECEIPT, [r1, r2], ["u1", "u2"], start_txn_no=1)
        self._commit(writer, pw, ws, fmt_calls)
        self.assertIn("I7:I7", fmt_calls)        # 票2 = page offset1 → 絶対 row7
        self.assertNotIn("I6:I6", fmt_calls)     # 票1 に漏れない

    def test_unrecognized_result_becomes_normal_page_row(self):
        writer = _make_writer()
        r1 = _receipt_result("店A", [_entry(5000)], total=5000)
        # 票2: 全行金額0 → 占位行（部分認識ラベル、赤タグ）
        r2 = _receipt_result("店B", [_entry(0)], total=None, invoice="")
        pw = writer.build_page_write(
            "従業員", DocType.RECEIPT, [r1, r2], ["u1", "u2"], start_txn_no=1)
        self.assertEqual(len(pw.rows), 2)        # 占位も普通の一行として集約
        self.assertEqual(pw.rows[1][TAG_COL_INDEX], UNRECOGNIZED_TAG)
        self.assertEqual(pw.rows[1][18], "⚠ 部分認識（金額なし）")
        # 占位行（has_partial）は I列赤 op を持つ（offset 1）
        red_ops = [o for o in pw.highlight_ops
                   if o.col == "I" and o.row_start == 1]
        self.assertTrue(red_ops)


from sheets_output import compute_page_fingerprint  # noqa: E402
from posting_ledger import ProbeResult  # noqa: E402


class FingerprintTest(unittest.TestCase):
    """compute_page_fingerprint: 順序非依存＋write/read 境界（int↔str, 列不足）安定。"""

    def test_order_independent(self):
        a = [[1, "x"], [2, "y"]]
        b = [[2, "y"], [1, "x"]]
        self.assertEqual(compute_page_fingerprint(a), compute_page_fingerprint(b))

    def test_int_and_str_cells_match(self):
        # page_write.rows（int 混在）と Sheets 読取り（全 str）が同一値なら一致
        written = [[1, "2026/06/01", 5000]]
        read_back = [["1", "2026/06/01", "5000"]]
        self.assertEqual(compute_page_fingerprint(written),
                         compute_page_fingerprint(read_back))

    def test_different_content_differs(self):
        self.assertNotEqual(compute_page_fingerprint([[1, "x"]]),
                            compute_page_fingerprint([[1, "z"]]))


class _FakeSpreadsheet:
    def __init__(self, tabs):
        self._tabs = tabs  # {tab_name: [[...rows...]]}

    def worksheet(self, name):
        import gspread
        if name not in self._tabs:
            raise gspread.exceptions.WorksheetNotFound(name)
        ws = _FakeWorksheet()
        ws._values = list(self._tabs[name])
        return ws


class ProbePageTest(unittest.TestCase):
    """probe_page: 厳格 witness（PRESENT/ABSENT/ESCALATE、附近探索なし）。"""

    def _writer(self, tabs):
        writer = _make_writer()
        writer.spreadsheet = _FakeSpreadsheet(tabs)
        return writer

    def test_present_when_fingerprint_matches(self):
        rows = [["1", "2026/06/01", "5000"]]
        # legend4+header1 の後、row6 に着地したと想定
        tab_rows = [["L1"], ["L2"], ["L3"], ["L4"], ["H"]] + rows
        writer = self._writer({"従業員_領収書": tab_rows})
        fp = compute_page_fingerprint(rows)
        self.assertIs(
            writer.probe_page("従業員_領収書", (6, 6), fp), ProbeResult.PRESENT)

    def test_absent_when_range_empty(self):
        tab_rows = [["L1"], ["L2"], ["L3"], ["L4"], ["H"]]  # データ行なし
        writer = self._writer({"従業員_領収書": tab_rows})
        self.assertIs(
            writer.probe_page("従業員_領収書", (6, 6), "anyfp"), ProbeResult.ABSENT)

    def test_escalate_when_fingerprint_mismatch(self):
        tab_rows = [["L1"], ["L2"], ["L3"], ["L4"], ["H"],
                    ["1", "2026/06/01", "9999"]]
        writer = self._writer({"従業員_領収書": tab_rows})
        fp = compute_page_fingerprint([["1", "2026/06/01", "5000"]])  # 異なる内容
        self.assertIs(
            writer.probe_page("従業員_領収書", (6, 6), fp), ProbeResult.ESCALATE)

    def test_escalate_when_tab_missing(self):
        writer = self._writer({})  # タブ無し（清空跨ぎ）
        self.assertIs(
            writer.probe_page("消えたタブ", (6, 6), "fp"), ProbeResult.ESCALATE)

    def test_escalate_when_range_exceeds_sheet(self):
        # 範囲末尾が実行を超える（末尾不足）→ ESCALATE
        tab_rows = [["L1"], ["L2"], ["L3"], ["L4"], ["H"], ["1", "x", "5000"]]
        writer = self._writer({"従業員_領収書": tab_rows})
        self.assertIs(
            writer.probe_page("従業員_領収書", (6, 8), "fp"), ProbeResult.ESCALATE)


class HighlightOpsTranslationTest(unittest.TestCase):
    """_result_block_ops / _unrecognized_block_ops の分岐（R1 高亮の要）を純関数で固定。"""

    def _ops(self):
        from sheets_output import (_result_block_ops, _unrecognized_block_ops,
                                   ResultBlock, UnrecognizedBlock)
        return _result_block_ops, _unrecognized_block_ops, ResultBlock, UnrecognizedBlock

    def test_result_ops_full_row_and_col_and_red(self):
        rbo, _, ResultBlock, _u = self._ops()
        block = ResultBlock(
            rows=[["r0"], ["r1"]],
            anomaly_flags=[(0, [{"severity": "high", "full_row": True}]),
                           (1, [{"severity": "low", "col": 7}])],  # H列
            conf_flags=[{"message": "低置信"}],
            red_flags=[{"message": "不符"}],
            next_txn_no=2)
        ops = rbo(block, offset=3)
        cols = {(o.col, o.row_start, o.row_end, o.severity) for o in ops}
        self.assertIn(("full", 3, 4, "low"), cols)      # 黄下地（全行、offset3..4）
        self.assertIn(("full", 3, 3, "high"), cols)     # per-entry full_row（offset3）
        self.assertIn(("H", 4, 4, "low"), cols)         # per-entry 単体セル（offset4）
        self.assertIn(("I", 3, 4, "high"), cols)        # doc 赤（I列、offset3..4）

    def test_unrecognized_ops_partial_missing_date_and_vendor(self):
        _, ubo, _r, UnrecognizedBlock = self._ops()
        block = UnrecognizedBlock(row=["x"], has_partial=True,
                                  has_date=False, has_vendor=False, next_txn_no=2)
        ops = ubo(block, offset=5)
        cols = {o.col for o in ops}
        self.assertEqual(cols, {"I", "B", "F"})         # I＋日付B＋取引先F 赤
        self.assertTrue(all(o.severity == "high" and o.row_start == 5 for o in ops))

    def test_unrecognized_ops_full_when_not_partial(self):
        _, ubo, _r, UnrecognizedBlock = self._ops()
        block = UnrecognizedBlock(row=["x"], has_partial=False,
                                  has_date=False, has_vendor=False, next_txn_no=2)
        ops = ubo(block, offset=0)
        self.assertEqual([(o.col, o.severity) for o in ops], [("full", "high")])


class NextTxnAndPeekTest(unittest.TestCase):
    """headless の start_txn 先読み（next_txn_no）と着地予定範囲（peek_append_range）。"""

    def test_next_txn_no_empty_tab_returns_one(self):
        writer = _make_writer()
        ws = _FakeWorksheet()  # legend4+header1、数値 A 列なし → 1
        with patch.object(SheetsOutputWriter, "_get_or_create_tab", return_value=ws):
            self.assertEqual(writer.next_txn_no("従業員", DocType.RECEIPT), 1)

    def test_peek_append_range_after_existing_rows(self):
        writer = _make_writer()
        ws = _FakeWorksheet()  # 既存 5 行 → 次は row6 から
        import types
        pw = types.SimpleNamespace(rows=[["a"], ["b"]], tab_name="従業員_領収書")
        with patch.object(SheetsOutputWriter, "_get_or_create_tab", return_value=ws):
            self.assertEqual(writer.peek_append_range(pw), (6, 7))
def _call_append_entries(doc_type, data):
    """append_entries を実行し、戻り値そのものを返す最小ハーネス（B7 T3①専用）。

    既存の _run_append_entries は戻り値を握り潰す固定5-tupleを返す設計
    （呼び出し側7箇所が位置引数で unpack 済み）ため、戻り値の追加検証には
    ここを新設する（既存ハーネス・既存テストは一切変更しない）。
    """
    writer = _make_writer()
    ws = _FakeWorksheet()
    with ExitStack() as stack:
        stack.enter_context(patch.object(
            SheetsOutputWriter, "_get_or_create_tab", return_value=ws))
        stack.enter_context(patch.object(
            SheetsOutputWriter, "_format_with_retry", lambda *a, **k: None))
        stack.enter_context(patch.object(
            SheetsOutputWriter, "_apply_anomaly_highlight",
            lambda *a, **k: None))
        with redirect_stdout(io.StringIO()):
            return writer.append_entries("従業員", doc_type, data,
                                         source_url="http://x")


class AppendEntriesReturnValueTest(unittest.TestCase):
    """B7 T3①: append_entries の戻り値（"posted"/"placeholder"）。

    挙動不変の情報開示のみ（中8 裁決）——consumer(main.process_file) が
    entries>0 でも全行金額0で占位行に転落したケースを POSTED と誤報しない
    ようにするための追加情報。書き込み内容・分岐条件は一切変えない。
    """

    def test_contract_constants_are_pinned(self):
        # 契約値の凍結（simcodex R2 裁決）: 定数の「値そのもの」を固定し、
        # リネームや値変更がテスト無風で通らないようにする。本クラスの他
        # テストの裸文字列比較は、この pin とセットで契約を二重に守る。
        self.assertEqual(APPEND_RESULT_POSTED, "posted")
        self.assertEqual(APPEND_RESULT_PLACEHOLDER, "placeholder")

    def test_returns_posted_when_a_normal_row_is_written(self):
        data = {"date": "2026/07/31", "vendor": "店", "invoice_num": "",
                "memo": "", "entries": [_entry(1000)]}
        result = _call_append_entries(DocType.RECEIPT, data)
        self.assertEqual(result, "posted")

    def test_returns_placeholder_when_all_entries_have_zero_amount(self):
        # 中8 裁決の核心ケース: entries は非空だが全行金額0/None →
        # 内部で占位行に転落する。POSTED と誤報してはいけない。
        entries = [_entry(0), {"amount": None,
                               "debit_account": "備品・消耗品費",
                               "description": "商品",
                               "credit_account": "未払金"}]
        data = {"date": "2026/07/31", "vendor": "店", "invoice_num": "",
                "memo": "", "entries": entries}
        result = _call_append_entries(DocType.RECEIPT, data)
        self.assertEqual(result, "placeholder")

    def test_returns_placeholder_when_entries_are_empty(self):
        data = {"date": "", "vendor": "", "invoice_num": "", "memo": "",
                "entries": [], "_unrecognized": True}
        result = _call_append_entries(DocType.RECEIPT, data)
        self.assertEqual(result, "placeholder")


class _FakeAuditWorksheet:
    """監査タブ用の worksheet スタブ（ヘッダ1行のみの素のタブ）。"""

    def __init__(self, values=None):
        self._values = [list(r) for r in values] if values is not None else []
        self.appended = []
        self.row_count = 1000
        self.added_rows = 0

    def get_all_values(self):
        return [list(r) for r in self._values]

    def row_values(self, idx):
        return list(self._values[idx - 1]) if len(self._values) >= idx else []

    def append_row(self, row, value_input_option=None):
        self._values.append(list(row))

    def append_rows(self, rows, value_input_option=None):
        self.appended.extend(rows)
        self._values.extend([list(r) for r in rows])

    def add_rows(self, n):
        self.added_rows += n
        self.row_count += n


class _FakeAuditSpreadsheet:
    """worksheet 取得/作成だけを提供するスプレッドシート代役。"""

    def __init__(self, existing=None):
        self._sheets = dict(existing or {})
        self.created = []

    def worksheet(self, title):
        if title not in self._sheets:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self._sheets[title]

    def add_worksheet(self, title, rows, cols):
        ws = _FakeAuditWorksheet()
        self._sheets[title] = ws
        self.created.append({"title": title, "rows": rows, "cols": cols})
        return ws


def _make_audit_writer(existing=None):
    """gspread 認証を回避した、監査タブ用の SheetsOutputWriter。"""
    writer = SheetsOutputWriter.__new__(SheetsOutputWriter)
    writer._ws_cache = {}
    writer._tab_has_data = {}
    writer._tab_next_txn = {}
    writer._tabs_sanitized = set()
    writer.spreadsheet = _FakeAuditSpreadsheet(existing)
    return writer


class AuditTabTest(unittest.TestCase):
    """IP-401 T2: 除外ページの留痕先＝独立監査タブ。

    MF データ区（仕訳タブ）には書かない。取引No・行 fingerprint を消費して
    MF インポートデータを汚すため（Plan §3.2）。ただし無音で消すのも不可なので
    専用タブへ追記する。
    """

    def test_tab_name_starts_with_underscore(self):
        """GAS の backupAllTabs_ は '_' 始まり以外の全タブを退避後に**削除**する
        （gas/daily_backup_ido.gs の deleteSourceTabs_）。通常名で作ると監査タブが
        毎晩 22:00 に消える。"""
        self.assertTrue(AUDIT_TAB_NAME.startswith("_"),
                        f"監査タブ名は '_' 始まり必須: {AUDIT_TAB_NAME}")

    def test_creates_tab_with_audit_headers_not_mf_headers(self):
        # Arrange: 監査タブが存在しない
        writer = _make_audit_writer()

        # Act
        with redirect_stdout(io.StringIO()):
            writer.append_audit_row(filename="r.pdf", page_num=1,
                                    verdict="除外", reason="envelope",
                                    ocr_text_len=55, source_url="https://x/1")

        # Assert: MF 凡例4行や MF_HEADERS ではなく 7 列の監査 schema
        created = writer.spreadsheet.created
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["title"], AUDIT_TAB_NAME)
        self.assertEqual(created[0]["cols"], len(AUDIT_HEADERS))
        ws = writer.spreadsheet.worksheet(AUDIT_TAB_NAME)
        self.assertEqual(ws.get_all_values()[0], AUDIT_HEADERS)

    def test_appends_row_with_expected_columns(self):
        # Arrange
        writer = _make_audit_writer()

        # Act
        with redirect_stdout(io.StringIO()):
            writer.append_audit_row(filename="r.pdf", page_num=3,
                                    verdict="除外", reason="envelope",
                                    ocr_text_len=55, source_url="https://x/3")

        # Assert: 日時以外の 6 列を固定（日時は実行時刻なので形だけ確認）
        ws = writer.spreadsheet.worksheet(AUDIT_TAB_NAME)
        row = ws.appended[-1]
        self.assertEqual(len(row), len(AUDIT_HEADERS))
        self.assertEqual(row[1:], ["r.pdf", 3, "除外", "envelope", 55, "https://x/3"])
        self.assertRegex(row[0], r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$")

    def test_existing_tab_with_wrong_header_is_not_overwritten(self):
        """R1: 既存タブのヘッダが想定と違う場合は上書きせずエラー報告。"""
        # Arrange: 誰かが同名タブを別 schema で作っていた
        stale = _FakeAuditWorksheet([["取引No", "取引日", "借方勘定科目"]])
        writer = _make_audit_writer({AUDIT_TAB_NAME: stale})

        # Act / Assert: 破壊せず例外
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(Exception):
                writer.append_audit_row(filename="r.pdf", page_num=1,
                                        verdict="除外", reason="envelope",
                                        ocr_text_len=55, source_url="")
        self.assertEqual(stale.get_all_values(),
                         [["取引No", "取引日", "借方勘定科目"]])

    def test_headerless_tab_is_self_healed_not_rejected(self):
        """add_worksheet 成功・ヘッダ書込失敗で残った空タブから自己回復する。

        ここを schema 不一致で撥ねると、監査機能が永久に MF 退避行へ落ち、
        人手で表を直すまで復旧しない（Codex P1 指摘）。空タブなら誰のデータも
        壊さないので書き直す。
        """
        # Arrange: 前回の初期化が中断してヘッダ無しタブだけ残った状態
        orphan = _FakeAuditWorksheet([])
        writer = _make_audit_writer({AUDIT_TAB_NAME: orphan})

        # Act
        with redirect_stdout(io.StringIO()):
            writer.append_audit_row(filename="r.pdf", page_num=1,
                                    verdict="除外", reason="envelope",
                                    ocr_text_len=55, source_url="")

        # Assert: ヘッダが復元され、監査行も書けている
        values = orphan.get_all_values()
        self.assertEqual(values[0], AUDIT_HEADERS)
        self.assertEqual(len(values), 2)

    def test_existing_tab_with_correct_header_is_reused(self):
        # Arrange
        existing = _FakeAuditWorksheet([list(AUDIT_HEADERS),
                                        ["2026/07/29 10:00:00", "old.pdf", 1,
                                         "除外", "envelope", 40, ""]])
        writer = _make_audit_writer({AUDIT_TAB_NAME: existing})

        # Act
        with redirect_stdout(io.StringIO()):
            writer.append_audit_row(filename="new.pdf", page_num=2,
                                    verdict="分岐",
                                    reason="envelope_signal_with_entries",
                                    ocr_text_len=120, source_url="https://x/2")

        # Assert: 新規作成せず追記のみ
        self.assertEqual(writer.spreadsheet.created, [])
        self.assertEqual(len(existing.get_all_values()), 3)


if __name__ == "__main__":
    unittest.main()
