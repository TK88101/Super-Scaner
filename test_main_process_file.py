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
import ocr_engine
from doc_types import DocType


class _RecordingWriter:
    """append_entries / append_audit_row の呼び出しを記録する sheets_writer 代役。

    audit_error に例外を差すと監査タブ書込が失敗する状況を再現できる（§3.7）。
    """

    def __init__(self, audit_error=None):
        self.calls = []
        self.audit_calls = []
        # 書込順序を検証できるよう、両方を単一の時系列へも記録する。
        # 別々のリストだけだと「MF が先」を主張するテストが順序を入れ替えても
        # 通ってしまう（歯が無い）。
        self.events = []
        self._audit_error = audit_error

    def append_entries(self, employee_name, doc_type, entries_data, source_url):
        self.calls.append(entries_data)
        self.events.append("entries")

    def append_audit_row(self, filename, page_num, verdict, reason,
                         ocr_text_len, source_url=""):
        self.events.append("audit")
        self.audit_calls.append({
            "filename": filename, "page_num": page_num, "verdict": verdict,
            "reason": reason, "ocr_text_len": ocr_text_len,
            "source_url": source_url,
        })
        if self._audit_error:
            raise self._audit_error


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


def _excluded_result(reason="envelope", ocr_text_len=55):
    return {"entries": [], "_excluded_page": True, "_exclude_reason": reason,
            "_exclude_destination": ocr_engine.EXCLUDE_DEST_AUDIT_TAB,
            "_ocr_text_len": ocr_text_len}


def _run_process_file(pages, writer=None):
    """process_pipeline を差替えて process_file を1回走らせる。"""
    writer = writer if writer is not None else _RecordingWriter()
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


class AuditTabRoutingTest(unittest.TestCase):
    """IP-401 T2: 除外ページ／分岐ページの監査タブ振り分け。"""

    def test_excluded_page_is_recorded_in_audit_tab(self):
        # Arrange
        pages = [_page(_excluded_result(), 1, 2), _page(_valid_result(), 2, 2)]

        # Act
        _, writer = _run_process_file(pages)

        # Assert: MF は正常ページのみ、監査タブに除外1行
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(len(writer.audit_calls), 1)
        audit = writer.audit_calls[0]
        self.assertEqual(audit["verdict"], "除外")
        self.assertEqual(audit["reason"], "envelope")
        self.assertEqual(audit["page_num"], 1)
        self.assertEqual(audit["ocr_text_len"], 55)

    def test_branch_page_writes_both_mf_and_audit(self):
        """R2: entries 有効＋封筒シグナル命中 → MF に正常記帳 + 監査タブにも追記。"""
        # Arrange
        branch = {**_valid_result(),
                  "_audit_signal": "envelope_signal_with_entries",
                  "_ocr_text_len": 120}
        pages = [_page(branch, 1, 1)]

        # Act
        ok, writer = _run_process_file(pages)

        # Assert: 記帳は止まらない（Gemini 優先＝交差検証の設計意図）
        self.assertTrue(ok)
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(writer.calls[0]["vendor"], "舞鶴パーク")
        self.assertEqual(len(writer.audit_calls), 1)
        self.assertEqual(writer.audit_calls[0]["verdict"], "分岐")
        self.assertEqual(writer.audit_calls[0]["reason"],
                         "envelope_signal_with_entries")

    def test_audit_failure_on_branch_page_only_warns(self):
        """§3.7: 分岐記録の監査書込失敗は記帳を阻害しない（帳簿は既に正しい）。"""
        # Arrange
        branch = {**_valid_result(),
                  "_audit_signal": "envelope_signal_with_entries",
                  "_ocr_text_len": 120}
        writer = _RecordingWriter(audit_error=RuntimeError("Sheets 500"))

        # Act
        ok, writer = _run_process_file([_page(branch, 1, 1)], writer=writer)

        # Assert: 成功扱いのまま、MF 行は 1 本だけ（退避行を足さない）
        self.assertTrue(ok)
        self.assertEqual(len(writer.calls), 1)
        self.assertFalse(any(c.get("_unrecognized") for c in writer.calls))

    def test_audit_failure_on_excluded_page_falls_back_to_mf_row(self):
        """§3.7: 真の除外は監査タブが唯一の留痕。失敗したら MF の赤い
        認識不能占位行へ退避して必ず可視化する。"""
        # Arrange
        writer = _RecordingWriter(audit_error=RuntimeError("Sheets 500"))

        # Act
        ok, writer = _run_process_file(
            [_page(_excluded_result(), 1, 2), _page(_valid_result(), 2, 2)],
            writer=writer)

        # Assert: 正常行 + 退避の認識不能行
        self.assertTrue(ok)
        self.assertEqual(len(writer.calls), 2)
        self.assertTrue(any(c.get("_unrecognized") for c in writer.calls))

    def test_mf_is_written_before_audit_tab(self):
        """§3.7: 書込順序は MF 区が先、監査タブが後（帳簿を人質に取らせない）。"""
        # Arrange
        branch = {**_valid_result(),
                  "_audit_signal": "envelope_signal_with_entries",
                  "_ocr_text_len": 120}

        # Act
        _, writer = _run_process_file([_page(branch, 1, 1)])

        # Assert: 単一時系列で順序そのものを断言する
        # （別リストの件数だけ見るテストは順序を入れ替えても通ってしまう）
        self.assertEqual(writer.events, ["entries", "audit"])


class SocialInsuranceNoticeRoutingTest(unittest.TestCase):
    """IP-401 T6 / §3.8: 社会保険料通知書だけは MF タブへ提示行を書く。

    封筒（監査タブ行き）とは性質が違う。これは正常な除外ではなく**運用ルール
    違反の通知**であり、顧客が必ず目にする場所でなければ意味がない。
    """

    def _notice_page(self):
        return _page({
            "entries": [],
            "memo": ocr_engine.SOCIAL_INSURANCE_MEMO,
            "_excluded_page": True,
            "_exclude_reason": ocr_engine.SOCIAL_INSURANCE_REASON,
            "_exclude_destination": ocr_engine.EXCLUDE_DEST_MF_TAB,
            "_ocr_text_len": 90,
        }, 1, 1)

    def test_unknown_destination_defaults_to_audit_tab(self):
        """行き先が宣言されていない result は監査タブへ倒す（MF 区を汚さない）。"""
        # Arrange: _exclude_destination を持たない古い形の result
        page = _page({"entries": [], "_excluded_page": True,
                      "_exclude_reason": "envelope"}, 1, 1)

        # Act
        _, writer = _run_process_file([page])

        # Assert
        self.assertEqual(writer.calls, [])
        self.assertEqual(len(writer.audit_calls), 1)

    def test_writes_placeholder_to_mf_tab_not_audit_tab(self):
        # Arrange / Act
        ok, writer = _run_process_file([self._notice_page()])

        # Assert: MF タブに1行、監査タブには書かない
        self.assertTrue(ok)
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(writer.audit_calls, [])

    def test_placeholder_carries_the_guidance_memo(self):
        # Arrange / Act
        _, writer = _run_process_file([self._notice_page()])

        # Assert: 顧客が読む文言がそのまま摘要に載る
        self.assertEqual(writer.calls[0]["memo"],
                         ocr_engine.SOCIAL_INSURANCE_MEMO)

    def test_placeholder_has_no_entries_and_no_amount(self):
        """DoD: 占位行の金額列・科目列が空で、MF インポート時に金額を持ち込まない。"""
        # Arrange / Act
        _, writer = _run_process_file([self._notice_page()])

        # Assert: entries が空 = 金額行が1本も無い。_unrecognized 経路で書かれる
        data = writer.calls[0]
        self.assertEqual(data["entries"], [])
        self.assertTrue(data.get("_unrecognized"))

    def test_failed_notice_write_is_not_swallowed_into_success(self):
        """提示行はこのページの唯一の出力。書けなければ成功扱いにしない。

        握り潰すと Success 判定でファイルが歸檔され、顧客は社会保険料通知書を
        上げたことも記帳されなかったことも知る術がなくなる。
        """
        # Arrange: MF タブへの書き込みが失敗する
        class _FailingWriter(_RecordingWriter):
            def append_entries(self, employee_name, doc_type, entries_data,
                               source_url):
                raise RuntimeError("Sheets 500")

        # Act: 全頁が社会保険料通知書で、その書込が全部失敗
        ok, writer = _run_process_file([self._notice_page()],
                                       writer=_FailingWriter())

        # Assert: Failed → ファイル保持 → 次回再試行
        self.assertFalse(ok)

    def test_mixed_file_keeps_the_specific_notice_in_the_fallback_row(self):
        """混在ファイルは既存語義どおり歸檔されるが、情報は落とさない。

        再試行すると成功頁の仕訳が重複するため歸檔は変えられない（CLAUDE.md
        「部分页失败 → 归档（防重试产生重复行）」）。代わりに占位行へ
        「どの頁が何だったか」を残す。
        """
        # Arrange: p1=社会保険料通知書（MF書込が失敗）/ p2=正常な領収書
        class _NoticeFailingWriter(_RecordingWriter):
            def append_entries(self, employee_name, doc_type, entries_data,
                               source_url):
                if entries_data.get("memo") == ocr_engine.SOCIAL_INSURANCE_MEMO:
                    raise RuntimeError("Sheets 500")
                super().append_entries(employee_name, doc_type, entries_data,
                                       source_url)

        notice = self._notice_page()
        notice["page_num"], notice["total_pages"] = 1, 2

        # Act
        ok, writer = _run_process_file(
            [notice, _page(_valid_result(), 2, 2)],
            writer=_NoticeFailingWriter())

        # Assert: 歸檔されるが、占位行に社会保険料通知書だった旨が残る
        self.assertTrue(ok)
        placeholders = [c for c in writer.calls if c.get("_unrecognized")]
        self.assertEqual(len(placeholders), 1)
        self.assertIn("p1:", placeholders[0]["memo"])
        self.assertIn("社会保険料通知書", placeholders[0]["memo"])

    def test_envelope_exclusion_still_goes_to_audit_tab(self):
        """回帰保護: 封筒は従来通り監査タブ（MF には書かない）。"""
        # Arrange / Act
        _, writer = _run_process_file([_page(_excluded_result(), 1, 1)])

        # Assert
        self.assertEqual(writer.calls, [])
        self.assertEqual(len(writer.audit_calls), 1)


if __name__ == "__main__":
    unittest.main()
