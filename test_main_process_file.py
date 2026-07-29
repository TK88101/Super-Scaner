"""main.process_file のページ振り分け語義テスト（IP-401）。

対象は「1ページの result dict をどう扱うか」の分岐だけ:

  _page_error     → Sheets に書かず error_pages に数える（再試行対象）
  _excluded_page  → Sheets の MF 区に書かず error_pages には数えない（除外）
  それ以外        → MF 区へ書き込む

_excluded_page が MF 区に漏れると sheets_output の最終防衛
(_write_unrecognized_row) に落ちて取引No を消費し、赤い「認識不能」占位行が
MF インポートデータに混ざる（Plan §3.2 違反）。無音欠落を直した結果として
MF 区を汚しては本末転倒なので、ここで固定する。

    venv311/bin/python -m unittest test_main_process_file -v
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import main
from doc_types import DocType


class _RecordingWriter:
    """append_entries の呼び出しを記録するだけの sheets_writer 代役。"""

    def __init__(self):
        self.calls = []

    def append_entries(self, employee_name, doc_type, entries_data, source_url):
        self.calls.append(entries_data)


def _page(result, page_num=1, total_pages=1):
    return {"result": result, "page_num": page_num, "total_pages": total_pages}


def _valid_result():
    return {
        "date": "2026/07/18",
        "vendor": "舞鶴パーク",
        "invoice_num": "",
        "memo": "",
        "entries": [{"debit_account": "旅費交通費", "amount": 200}],
    }


def _excluded_result(reason="envelope"):
    return {"entries": [], "_excluded_page": True, "_exclude_reason": reason}


def _run_process_file(pages):
    """process_pipeline を差替えて process_file を1回走らせる。"""
    writer = _RecordingWriter()
    with mock.patch.object(main, "process_pipeline", return_value=iter(pages)), \
         mock.patch.object(main, "send_notification"), \
         mock.patch.object(main, "PageUrlResolver") as resolver_cls:
        resolver_cls.return_value.resolve.return_value = "https://example/doc"
        with redirect_stdout(io.StringIO()):
            ok = main.process_file(
                service=mock.MagicMock(),
                sheets_writer=writer,
                file_path="/tmp/dummy.pdf",
                uploader_name="テスト社員",
                chat_id="",
                doc_type=DocType.RECEIPT,
            )
    return ok, writer


class ExcludedPageRoutingTest(unittest.TestCase):
    """IP-401 T1: 除外ページは MF 区に一切書かれない。"""

    def test_excluded_page_is_not_written_to_mf_tab(self):
        # Arrange: p1=封筒として除外 / p2=正常な領収書
        pages = [_page(_excluded_result(), 1, 2), _page(_valid_result(), 2, 2)]

        # Act
        ok, writer = _run_process_file(pages)

        # Assert: MF 区への書き込みは正常ページの 1 回だけ
        self.assertTrue(ok)
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(writer.calls[0]["vendor"], "舞鶴パーク")

    def test_all_excluded_pdf_is_success_not_failed(self):
        """全頁が除外でも Failed にしない（Failed だとファイル保持→無限リトライ）。"""
        # Arrange
        pages = [_page(_excluded_result(), 1, 2), _page(_excluded_result(), 2, 2)]

        # Act
        ok, writer = _run_process_file(pages)

        # Assert: 成功扱い（歸檔される）かつ MF 区は無傷
        self.assertTrue(ok)
        self.assertEqual(writer.calls, [])

    def test_excluded_page_does_not_trigger_partial_error_row(self):
        """除外は失敗ではないので「部分ページエラー」占位行を誘発しない。"""
        # Arrange
        pages = [_page(_excluded_result(), 1, 2), _page(_valid_result(), 2, 2)]

        # Act
        _, writer = _run_process_file(pages)

        # Assert: 部分エラー占位行 (_unrecognized) は書かれていない
        self.assertFalse(any(c.get("_unrecognized") for c in writer.calls))

    def test_page_error_still_counted_and_skipped(self):
        """回帰保護: _page_error の従来語義は変えていない。"""
        # Arrange: 全頁エラー → Failed（ファイル保持）
        pages = [
            _page({"entries": [], "_unrecognized": True, "_page_error": True}, 1, 1),
        ]

        # Act
        ok, writer = _run_process_file(pages)

        # Assert
        self.assertFalse(ok)
        self.assertEqual(writer.calls, [])


if __name__ == "__main__":
    unittest.main()
