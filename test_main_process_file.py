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
from datetime import timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import main
import ocr_engine
import page_progress
from sheets_output import APPEND_RESULT_PLACEHOLDER, APPEND_RESULT_POSTED
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


def _run_process_file(pages, writer=None, progress=None,
                      resolver_side_effect=None, capture=None,
                      doc_type=DocType.RECEIPT):
    """process_pipeline を差替えて process_file を1回走らせる。

    progress は B7 T3 で追加された任意引数。未指定(None)のときは既存呼出と
    完全に同じ挙動になる（main.process_file 内で NULL_REPORTER に落ちる）。
    resolver_side_effect を渡すと resolver.resolve が例外を投げる（未預期
    例外経路のテスト用。例外はそのまま呼び出し元へ伝播する）。

    capture に dict を渡すと `capture["pipeline"]` へ process_pipeline の
    mock を置く。patch を `as` で受けずに使っているため with を抜けた後は
    mock への参照がどこにも残らず `call_args` を読めない —— 呼出引数を
    検査するテスト（IP-401 §12.1③ の番人）にはこの参照が要る。この関数の
    with ブロック全体（process_pipeline / send_notification /
    PageUrlResolver の patch と stdout 抑止）を複製する方が漂移源になるので、
    任意引数 1 つで済ませる。
    """
    writer = writer if writer is not None else _RecordingWriter()
    with mock.patch.object(main, "process_pipeline",
                           return_value=iter(pages)) as pipeline, \
         mock.patch.object(main, "send_notification"), \
         mock.patch.object(main, "PageUrlResolver") as resolver_cls:
        if capture is not None:
            capture["pipeline"] = pipeline
        if resolver_side_effect is not None:
            resolver_cls.return_value.resolve.side_effect = resolver_side_effect
        else:
            resolver_cls.return_value.resolve.return_value = "https://example/doc"
        with redirect_stdout(io.StringIO()):
            ok = main.process_file(
                service=mock.MagicMock(),
                sheets_writer=writer,
                file_path="/tmp/dummy.pdf",
                uploader_name="テスト社員",
                chat_id="",
                doc_type=doc_type,
                progress=progress,
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
        # 監査タブも無痕であること。この経路はファイルを**保持**して 3 秒ごとに
        # 再走査されるので、1 周につき 1 行でも書くと行が無限増殖する
        # （CLAUDE.md「全ページ失敗 → Failed、保留ファイル」がまさにこの理由）。
        self.assertEqual(writer.audit_calls, [])


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


class PageCoverageSentinelTest(unittest.TestCase):
    """IP-401 §8-中7: 出力されなかったページを監査タブへ持続化する。

    本番機は無人の Windows ミニ PC で誰も控制台を見ていない（Chatwork 通知も
    monitoring も廃止済み）。哨戒が print だけなら哨戒していないのと同じ。
    """

    def test_missing_page_is_recorded_in_audit_tab(self):
        # Arrange: 3頁の PDF なのに p2 が一度も出力されなかった
        pages = [_page(_valid_result(), 1, 3), _page(_valid_result(), 3, 3)]

        # Act
        _, writer = _run_process_file(pages)

        # Assert: 欠落として監査タブに1行
        gaps = [a for a in writer.audit_calls if a["verdict"] == "欠落"]
        self.assertEqual(len(gaps), 1)
        self.assertIn("2", gaps[0]["reason"])

    def test_no_audit_row_when_all_pages_present(self):
        # Arrange
        pages = [_page(_valid_result(), 1, 2), _page(_valid_result(), 2, 2)]

        # Act
        _, writer = _run_process_file(pages)

        # Assert
        self.assertEqual(writer.audit_calls, [])

    def test_sentinel_failure_does_not_break_processing(self):
        # Arrange: 監査タブ書込が落ちても記帳は成功のまま
        writer = _RecordingWriter(audit_error=RuntimeError("Sheets 500"))
        pages = [_page(_valid_result(), 1, 3), _page(_valid_result(), 3, 3)]

        # Act
        ok, writer = _run_process_file(pages, writer=writer)

        # Assert
        self.assertTrue(ok)
        self.assertEqual(len(writer.calls), 2)


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


# ─────────────────────── B7 T3: 頁級進捗フックの発射表 ───────────────────────

class _ProgressSpy:
    """process_file(..., progress=...) の発射を記録するテスト用 reporter。"""

    def __init__(self):
        self.file_started_calls = []
        self.page_done_calls = []
        self.file_finished_calls = []

    def file_started(self, filename, uploader_name, doc_type):
        self.file_started_calls.append((filename, uploader_name, doc_type))

    def page_done(self, page_num, total_pages, outcome, reason, occurred_at):
        self.page_done_calls.append(
            (page_num, total_pages, outcome, reason, occurred_at))

    def file_finished(self, status, error_class=None):
        self.file_finished_calls.append((status, error_class))


class _ReturnControlledWriter(_RecordingWriter):
    """append_entries の戻り値を制御できる _RecordingWriter 派生（B7 T3専用）。

    既存の _RecordingWriter は戻り値を検証しない既存テスト群のために一切
    変更しない。"posted"/"placeholder" の発射を検証する新規テストだけが
    こちらを使う。
    """

    def __init__(self, entries_return=APPEND_RESULT_POSTED, audit_error=None):
        super().__init__(audit_error=audit_error)
        self._entries_return = entries_return

    def append_entries(self, employee_name, doc_type, entries_data, source_url):
        super().append_entries(employee_name, doc_type, entries_data, source_url)
        return self._entries_return


class _AmountAwareWriter(_RecordingWriter):
    """payload の中身から戻り値を決める代役（T5 の行欠け頁専用）。

    1 物理頁から明細 result と提示行 result の 2 件が来るので、固定戻り値の
    `_ReturnControlledWriter` では POSTED と PLACEHOLDER を撃ち分けられない。

    判定式は本物（`sheets_output.py` の `append_entries` 冒頭、
    `if not amount or int(amount) == 0: continue` ＋ 行が 1 つも残らなければ
    `_write_unrecognized_row` → `APPEND_RESULT_PLACEHOLDER`）を**逐字で**写す。
    `any(entry.get("amount"))` のような近似だと `amount="0"` や `0.4` で
    本物と分岐が割れ、「行欠け頁が POSTED に化けない」ことを測るはずの物差しが
    別の契約を測ることになる。本物の述語は `append_entries` の中段に埋まって
    いて再利用できないため、写す以外に手が無い（共有は T6 で
    `sheets_output.py` に触るときに解消する）。
    """

    def append_entries(self, employee_name, doc_type, entries_data, source_url):
        super().append_entries(employee_name, doc_type, entries_data, source_url)
        for entry in entries_data.get("entries") or []:
            amount = entry.get("amount")
            if not amount or int(amount) == 0:
                continue
            return APPEND_RESULT_POSTED
        return APPEND_RESULT_PLACEHOLDER


class _AlwaysFailingEntriesWriter(_RecordingWriter):
    """append_entries が常に失敗する writer（除外頁の MF 落地失敗を模す）。"""

    def append_entries(self, employee_name, doc_type, entries_data, source_url):
        raise RuntimeError("Sheets 500")


def _notice_page_dict():
    """社会保険料通知書ページ（_excluded_page かつ MF タブへ提示行、§3.8）。"""
    return {"entries": [], "memo": ocr_engine.SOCIAL_INSURANCE_MEMO,
            "_excluded_page": True,
            "_exclude_reason": ocr_engine.SOCIAL_INSURANCE_REASON,
            "_exclude_destination": ocr_engine.EXCLUDE_DEST_MF_TAB,
            "_ocr_text_len": 90}


class PageDoneEmissionTest(unittest.TestCase):
    """B7 T3: process_file のページ終局5種の発射表（Plan §3.2(b)）。"""

    def test_page_error_emits_failed(self):
        progress = _ProgressSpy()
        pages = [_page({"entries": [], "_unrecognized": True,
                        "_page_error": True}, 1, 1)]
        _run_process_file(pages, progress=progress)
        self.assertEqual(len(progress.page_done_calls), 1)
        _, _, outcome, reason, occurred_at = progress.page_done_calls[0]
        self.assertEqual(outcome, page_progress.OUTCOME_FAILED)
        self.assertEqual(reason, "page_error")
        self.assertEqual(occurred_at.tzinfo, timezone.utc)

    def test_excluded_page_success_emits_excluded_with_reason(self):
        progress = _ProgressSpy()
        pages = [_page(_excluded_result(reason="envelope"), 1, 1)]
        _run_process_file(pages, progress=progress)
        self.assertEqual(len(progress.page_done_calls), 1)
        _, _, outcome, reason, _ = progress.page_done_calls[0]
        self.assertEqual(outcome, page_progress.OUTCOME_EXCLUDED)
        self.assertEqual(reason, "envelope")

    def test_excluded_page_mf_landing_is_still_excluded_not_placeholder(self):
        """低11 裁決: 落地形式が MF 占位行（社保通知書）でも恒に EXCLUDED。"""
        progress = _ProgressSpy()
        pages = [_page(_notice_page_dict(), 1, 1)]
        _run_process_file(pages, progress=progress)
        self.assertEqual(len(progress.page_done_calls), 1)
        _, _, outcome, reason, _ = progress.page_done_calls[0]
        self.assertEqual(outcome, page_progress.OUTCOME_EXCLUDED)
        self.assertEqual(reason, ocr_engine.SOCIAL_INSURANCE_REASON)

    def test_excluded_record_failure_emits_failed(self):
        """MF 落地失敗（recorded=False）は FAILED, reason=exclude_record_failed。"""
        progress = _ProgressSpy()
        writer = _AlwaysFailingEntriesWriter()
        pages = [_page(_notice_page_dict(), 1, 1)]
        _run_process_file(pages, writer=writer, progress=progress)
        self.assertEqual(len(progress.page_done_calls), 1)
        _, _, outcome, reason, _ = progress.page_done_calls[0]
        self.assertEqual(outcome, page_progress.OUTCOME_FAILED)
        self.assertEqual(reason, "exclude_record_failed")

    def test_posted_page_emits_posted(self):
        progress = _ProgressSpy()
        writer = _ReturnControlledWriter(entries_return=APPEND_RESULT_POSTED)
        pages = [_page(_valid_result(), 1, 1)]
        _run_process_file(pages, writer=writer, progress=progress)
        self.assertEqual(len(progress.page_done_calls), 1)
        _, _, outcome, reason, _ = progress.page_done_calls[0]
        self.assertEqual(outcome, page_progress.OUTCOME_POSTED)
        self.assertEqual(reason, "")

    def test_placeholder_page_emits_placeholder(self):
        """中8 裁決: entries 有値でも append_entries が占位行に転落した頁は
        PLACEHOLDER（POSTED ではない）——「entries>0＝POSTED」の誤報排除。"""
        progress = _ProgressSpy()
        writer = _ReturnControlledWriter(
            entries_return=APPEND_RESULT_PLACEHOLDER)
        pages = [_page(_valid_result(), 1, 1)]
        _run_process_file(pages, writer=writer, progress=progress)
        self.assertEqual(len(progress.page_done_calls), 1)
        _, _, outcome, reason, _ = progress.page_done_calls[0]
        self.assertEqual(outcome, page_progress.OUTCOME_PLACEHOLDER)
        self.assertEqual(reason, "unrecognized")


class FileFinishedEmissionTest(unittest.TestCase):
    """B7 T3: process_file の檔終局4状態（Plan §3.2(c)）。"""

    def test_all_pages_error_emits_failed_retained(self):
        progress = _ProgressSpy()
        pages = [_page({"entries": [], "_unrecognized": True,
                        "_page_error": True}, 1, 1)]
        _run_process_file(pages, progress=progress)
        self.assertEqual(len(progress.file_finished_calls), 1)
        status, error_class = progress.file_finished_calls[0]
        self.assertEqual(status, page_progress.STATUS_FAILED_RETAINED)
        self.assertIsNone(error_class)

    def test_success_emits_completed(self):
        progress = _ProgressSpy()
        pages = [_page(_valid_result(), 1, 2), _page(_valid_result(), 2, 2)]
        _run_process_file(pages, progress=progress)
        status, _ = progress.file_finished_calls[0]
        self.assertEqual(status, page_progress.STATUS_COMPLETED)

    def test_missing_pages_emits_completed_with_coverage_gap(self):
        """中6 裁決: 頁欠落があれば「完了」ではなく COMPLETED_WITH_COVERAGE_GAP。"""
        progress = _ProgressSpy()
        pages = [_page(_valid_result(), 1, 3), _page(_valid_result(), 3, 3)]
        _run_process_file(pages, progress=progress)
        status, _ = progress.file_finished_calls[0]
        self.assertEqual(status, page_progress.STATUS_COMPLETED_COVERAGE_GAP)

    def test_partial_error_emits_partial_error_status(self):
        progress = _ProgressSpy()
        pages = [
            _page({"entries": [], "_unrecognized": True,
                  "_page_error": True}, 1, 2),
            _page(_valid_result(), 2, 2),
        ]
        _run_process_file(pages, progress=progress)
        status, _ = progress.file_finished_calls[0]
        self.assertEqual(status, page_progress.STATUS_PARTIAL_ERROR)

    def test_partial_error_takes_priority_over_coverage_gap(self):
        """partial_error は coverage gap より優先（タスク指示の優先順位）。"""
        progress = _ProgressSpy()
        pages = [
            _page({"entries": [], "_unrecognized": True,
                  "_page_error": True}, 1, 3),
            _page(_valid_result(), 3, 3),  # p2 は一度も来ない(欠落) かつ p1 はエラー
        ]
        _run_process_file(pages, progress=progress)
        status, _ = progress.file_finished_calls[0]
        self.assertEqual(status, page_progress.STATUS_PARTIAL_ERROR)

    def test_count_zero_emits_parse_failed(self):
        progress = _ProgressSpy()
        _run_process_file([], progress=progress)
        status, _ = progress.file_finished_calls[0]
        self.assertEqual(status, page_progress.STATUS_PARSE_FAILED)


class UnexpectedExceptionAbortedTest(unittest.TestCase):
    """B7 T3(高1): 未預期例外は ABORTED を best-effort 発射後、例外を原様 re-raise。"""

    def test_unexpected_exception_emits_aborted_and_reraises(self):
        progress = _ProgressSpy()
        pages = [_page(_valid_result(), 1, 1)]
        with self.assertRaises(RuntimeError):
            _run_process_file(pages, progress=progress,
                              resolver_side_effect=RuntimeError("boom"))
        self.assertEqual(len(progress.file_finished_calls), 1)
        status, error_class = progress.file_finished_calls[0]
        self.assertEqual(status, page_progress.STATUS_ABORTED)
        self.assertEqual(error_class, "RuntimeError")


class ProgressOptionalNoRegressionTest(unittest.TestCase):
    """B7 T3: progress 未指定でも process_file は従来どおり動く（無回帰）。"""

    def test_process_file_works_without_progress_argument(self):
        pages = [_page(_valid_result(), 1, 1)]
        ok, writer = _run_process_file(pages)  # progress 未指定
        self.assertTrue(ok)
        self.assertEqual(len(writer.calls), 1)


class FailureBackoffScheduleTest(unittest.TestCase):
    """main.py の失敗退避護欄（2026-08-10 Plan §3.6 T5）。

    main.py には従来 fail_count/backoff/blacklist が皆無で、永続的な失敗
    （Sheets 400・Drive 故障・コードのバグ等）が SCAN_INTERVAL=3s の無限
    リトライになり毎輪 PaddleOCR + Gemini を焼く事故が実際に起きた。
    退避計算を main loop 本体から切り離した純粋関数として検証する
    （main loop を起動せずに検証できることが要件）。IP-401「全頁失敗は
    ファイル保持」の語義はここでは変えない——変えるのは再試行の間隔だけ。
    """

    def test_backoff_schedule_increases_with_failure_count(self):
        # 級距: 3s → 30s → 5min → 30min → 上限1h
        self.assertEqual(main._next_backoff_seconds(1), 3)
        self.assertEqual(main._next_backoff_seconds(2), 30)
        self.assertEqual(main._next_backoff_seconds(3), 300)
        self.assertEqual(main._next_backoff_seconds(4), 1800)
        self.assertEqual(main._next_backoff_seconds(5), 3600)

    def test_backoff_caps_at_one_hour_for_further_failures(self):
        self.assertEqual(main._next_backoff_seconds(6), 3600)
        self.assertEqual(main._next_backoff_seconds(100), 3600)

    def test_record_failure_increments_count_and_sets_next_attempt(self):
        state = {}
        entry = main._record_file_failure(state, "file-A", now_ts=1000.0)
        self.assertEqual(entry["count"], 1)
        self.assertEqual(entry["next_attempt_ts"], 1003.0)
        self.assertIs(state["file-A"], entry)

        entry2 = main._record_file_failure(state, "file-A", now_ts=1003.0)
        self.assertEqual(entry2["count"], 2)
        self.assertEqual(entry2["next_attempt_ts"], 1033.0)

    def test_record_success_clears_state(self):
        state = {"file-A": {"count": 3, "next_attempt_ts": 9999.0}}
        main._record_file_success(state, "file-A")
        self.assertNotIn("file-A", state)

    def test_record_success_on_unknown_file_is_noop(self):
        state = {}
        main._record_file_success(state, "file-Z")  # 存在しなくても例外にならない
        self.assertEqual(state, {})

    def test_backed_off_file_is_skipped_others_are_not_blocked(self):
        # 護欄の核心要件: 1ファイルの退避が他ファイルの処理を止めない
        state = {}
        main._record_file_failure(state, "file-A", now_ts=1000.0)  # 次回 1003.0
        self.assertTrue(main._is_file_backed_off(state, "file-A", now_ts=1001.0))
        self.assertFalse(main._is_file_backed_off(state, "file-B", now_ts=1001.0))

    def test_backed_off_file_becomes_due_after_next_attempt_ts(self):
        state = {}
        main._record_file_failure(state, "file-A", now_ts=1000.0)  # 次回 1003.0
        self.assertFalse(main._is_file_backed_off(state, "file-A", now_ts=1003.0))

    def test_file_with_no_history_is_never_backed_off(self):
        self.assertFalse(main._is_file_backed_off({}, "file-new", now_ts=1000.0))


class BackoffPartitionTest(unittest.TestCase):
    """main.py の走査ループが「退避中でも他ファイルを止めない」を保つための
    分割ロジック（2026-08-10 codex 対抗評審裁決）。

    従来は found_any / バナー印字が退避 continue より先に実行され、退避
    ファイルが input に残り続ける限り「新しいファイルを検出しました！」が
    毎輪（3秒毎）誤って印字され続けた。list_files 直後に ready/backed_off
    へ分割し、found_any はバナーは ready_files の有無だけで決める。
    """

    def test_partition_splits_ready_and_backed_off_mixed(self):
        state = {}
        main._record_file_failure(state, "file-A", now_ts=1000.0)  # 次回 1003.0
        files = [{"id": "file-A"}, {"id": "file-B"}]

        ready, backed_off = main._partition_by_backoff(files, state, now_ts=1001.0)

        self.assertEqual(ready, [{"id": "file-B"}])
        self.assertEqual(backed_off, [{"id": "file-A"}])

    def test_partition_all_backed_off_yields_empty_ready(self):
        state = {}
        main._record_file_failure(state, "file-A", now_ts=1000.0)
        main._record_file_failure(state, "file-B", now_ts=1000.0)
        files = [{"id": "file-A"}, {"id": "file-B"}]

        ready, backed_off = main._partition_by_backoff(files, state, now_ts=1001.0)

        self.assertEqual(ready, [])
        self.assertEqual(backed_off, files)

    def test_partition_boundary_due_file_is_ready(self):
        # next_attempt_ts に到達した瞬間 (now_ts == next_attempt_ts) は
        # 処理可能側に入る（_is_file_backed_off の `<` 境界と一致させる）
        state = {}
        main._record_file_failure(state, "file-A", now_ts=1000.0)  # 次回 1003.0
        files = [{"id": "file-A"}]

        ready, backed_off = main._partition_by_backoff(files, state, now_ts=1003.0)

        self.assertEqual(ready, files)
        self.assertEqual(backed_off, [])

    def test_partition_preserves_order_and_does_not_mutate_state(self):
        state = {}
        main._record_file_failure(state, "file-B", now_ts=1000.0)
        files = [{"id": "file-A"}, {"id": "file-B"}, {"id": "file-C"}]

        ready, backed_off = main._partition_by_backoff(files, state, now_ts=1001.0)

        self.assertEqual(ready, [{"id": "file-A"}, {"id": "file-C"}])
        self.assertEqual(backed_off, [{"id": "file-B"}])
        self.assertEqual(set(state.keys()), {"file-B"})  # 呼び出しで増減しない


class BackoffSummaryThrottleTest(unittest.TestCase):
    """退避中サマリは BACKOFF_SUMMARY_INTERVAL_SECONDS 秒に最大1行（codex 裁決:
    ただ濾すだけだと "." のみになり input が空の正常状態と見分けがつかない）。
    """

    def test_interval_constant_is_60_seconds(self):
        # 運用パラメータであり本文中にマジックナンバーとして散らさない
        self.assertEqual(main.BACKOFF_SUMMARY_INTERVAL_SECONDS, 60)

    def test_should_print_immediately_when_never_printed(self):
        self.assertTrue(
            main._should_print_backoff_summary(last_printed_ts=0.0, now_ts=1000.0))

    def test_should_not_print_within_interval(self):
        self.assertFalse(
            main._should_print_backoff_summary(last_printed_ts=1000.0, now_ts=1030.0))

    def test_should_print_after_interval_elapsed(self):
        self.assertTrue(
            main._should_print_backoff_summary(last_printed_ts=1000.0, now_ts=1060.0))

    def test_should_print_exactly_at_interval_boundary(self):
        self.assertTrue(
            main._should_print_backoff_summary(last_printed_ts=1000.0, now_ts=1060.0))


class BackoffSummaryFormatTest(unittest.TestCase):
    """摘要行のフォーマット。倉庫慣例 (sheets_output.py:558) と同じ JST 明示 +
    "%Y/%m/%d %H:%M:%S" ——ホストのローカルタイムゾーンに依存しないことを、
    JST を明示して作った datetime から逆算した epoch で検証する。
    """

    def test_format_includes_count_and_jst_timestamp(self):
        from datetime import datetime
        from sheets_output import JST

        dt = datetime(2026, 8, 10, 14, 32, 10, tzinfo=JST)
        message = main._format_backoff_summary(3, dt.timestamp())

        self.assertEqual(
            message,
            "⏳ 失敗退避中: 3件（最早次回試行: 2026/08/10 14:32:10）",
        )

    def test_format_reflects_count_argument(self):
        from datetime import datetime
        from sheets_output import JST

        dt = datetime(2026, 8, 10, 9, 0, 0, tzinfo=JST)
        message = main._format_backoff_summary(1, dt.timestamp())

        self.assertIn("1件", message)


class BackoffFailureMessageTimezoneTest(unittest.TestCase):
    """個別ファイルの「🛑 退避開始」メッセージも JST 明示に統一する
    （従来の time.strftime + time.localtime はホストのタイムゾーンに依存し、
    Sheets 側のタイムスタンプと表記が食い違う——2026-08-10 Plan 修正）。
    """

    def test_format_jst_timestamp_matches_repo_convention(self):
        from datetime import datetime
        from sheets_output import JST

        dt = datetime(2026, 1, 5, 3, 4, 5, tzinfo=JST)
        self.assertEqual(
            main._format_jst_timestamp(dt.timestamp()),
            "2026/01/05 03:04:05",
        )


class PipelineCallContractTest(unittest.TestCase):
    """main は常に PDF 全体を処理する（IP-401 §12.1③ の番人）。

    `main.py` のページカバレッジ突合は `range(1, last_total_pages + 1)` で
    「1 頁目から全部あるはず」を前提にしている。この前提は
    「main は `process_pipeline` に `start_page` を渡さない」という、
    **どこにも強制されていない**取り決めだけで支えられている
    （`start_page` を渡すのは開発ツールの `local_test.py --start-page N`）。

    将来バッチ再開のような機能で main まで `start_page` が通ると、
    突合は `start_page` 未満の全頁を「欠落」と誤報し、監査タブが
    偽の欠落行で埋まる。前提が破れた瞬間に赤くなる番人をここに置く。
    """

    def test_main_does_not_pass_start_page_to_the_pipeline(self):
        # Arrange
        capture = {}

        # Act
        _run_process_file([_page(_valid_result(), 1, 1)], capture=capture)

        # Assert
        _, kwargs = capture["pipeline"].call_args
        self.assertNotIn(
            "start_page", kwargs,
            "main が start_page を渡すようになった。この番人を消す前に "
            "main.py のページカバレッジ突合 range(1, last_total_pages + 1) を "
            "range(start_page, ...) へ直すこと（さもないと start_page 未満の "
            "全頁が『欠落』として監査タブに誤報される）")


class LineShortagePageSemanticsTest(unittest.TestCase):
    """T5: 行欠け頁を process_file がどう扱うか（趙裁定 2026-08-17 の落点）。

    producer は 1 物理頁から **明細 result ＋ 提示行 result の 2 件**を
    yield する。既存の封筒分岐（複数 result）と同型の挙動だが、
    「頁数と outcome 件数が一致しなくなる」という可観測なズレを伴うので、
    偶然そうなっているのではなく**仕様である**ことをここで固定する。
    """

    @staticmethod
    def _pages():
        detail = {
            "date": "2026/05/18", "vendor": "ENEOS", "invoice_num": "",
            "memo": "", "entries": [{"debit_account": "旅費交通費",
                                     "amount": 630}],
            "line_mode": True,
        }
        notice = {
            "date": "2026/05/18", "vendor": "ENEOS", "invoice_num": "",
            "memo": "⚠ 明細行の取得漏れ: 券面100行中62行のみ取得"
                    "（原票を確認してください）",
            "entries": [], "_unrecognized": True,
            "_audit_signal": "line_shortage:62/100", "_ocr_text_len": 42,
        }
        return [_page(detail, 1, 1), _page(notice, 1, 1)]

    def test_page_is_archived_not_failed(self):
        # 裁定 3: 行が欠けてもファイルを Failed にしない（保持→再試行しない）
        ok, _ = _run_process_file(self._pages(), doc_type=DocType.CREDIT_CARD)
        self.assertTrue(ok)

    def test_one_physical_page_reports_posted_and_placeholder(self):
        progress = _ProgressSpy()
        _run_process_file(self._pages(), writer=_AmountAwareWriter(),
                          progress=progress, doc_type=DocType.CREDIT_CARD)
        outcomes = [call[2] for call in progress.page_done_calls]
        self.assertEqual(outcomes, [page_progress.OUTCOME_POSTED,
                                    page_progress.OUTCOME_PLACEHOLDER])
        # 頁番号は両方 1（進捗側は set で重複排除する契約）
        self.assertEqual({call[0] for call in progress.page_done_calls}, {1})

    def test_detail_rows_and_notice_row_both_reach_the_mf_tab(self):
        writer = _AmountAwareWriter()
        _run_process_file(self._pages(), writer=writer,
                          doc_type=DocType.CREDIT_CARD)
        self.assertEqual(len(writer.calls), 2)
        detail, notice = writer.calls
        self.assertEqual(len(detail["entries"]), 1)
        self.assertNotIn("_unrecognized", detail)
        # 提示行は entries 空 → sheets_output が摘要へ memo を書き赤系タグを付ける
        self.assertTrue(notice["_unrecognized"])
        self.assertIn("100行中62行", notice["memo"])

    def test_audit_row_is_written_once_with_the_machine_readable_reason(self):
        writer = _AmountAwareWriter()
        _run_process_file(self._pages(), writer=writer,
                          doc_type=DocType.CREDIT_CARD)
        self.assertEqual(len(writer.audit_calls), 1)
        audit = writer.audit_calls[0]
        self.assertEqual(audit["reason"], "line_shortage:62/100")
        self.assertEqual(audit["page_num"], 1)

    def test_ledger_is_written_before_the_audit_tab(self):
        # 帳簿を人質に取らせない（監査書込が落ちても記帳は済んでいる）
        writer = _AmountAwareWriter()
        _run_process_file(self._pages(), writer=writer,
                          doc_type=DocType.CREDIT_CARD)
        self.assertEqual(writer.events, ["entries", "entries", "audit"])


if __name__ == "__main__":
    unittest.main()
