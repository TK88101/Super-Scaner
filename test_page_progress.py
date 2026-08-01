"""page_progress.py の単体テスト（B7 第1步 T1/T2）。

page_progress は gspread の例外型だけを参照する（実 API 呼び出しはしない）ため
import に .env や service_account.json は不要——import 副作用ゼロを保つ設計の
裏付けとして、本ファイルは main.py 用の env 補完を一切行わない。

    venv311/bin/python -m unittest test_page_progress -v
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import gspread
from gspread.utils import rowcol_to_a1

import page_progress as pp


class OutcomeConstantsTest(unittest.TestCase):
    """契約 job-state-machine.md v0.15 §5.6 outcome 値域と一字一句一致。"""

    def test_outcome_values_match_contract_literally(self):
        self.assertEqual(pp.OUTCOME_POSTED, "POSTED")
        self.assertEqual(pp.OUTCOME_EXCLUDED, "EXCLUDED")
        self.assertEqual(pp.OUTCOME_PLACEHOLDER, "PLACEHOLDER")
        self.assertEqual(pp.OUTCOME_FAILED, "FAILED")


class StatusConstantsTest(unittest.TestCase):
    """檔終局 machine status。機械値は英字常数、日本語は表示専用の映射のみ。"""

    def test_status_constants_exist_with_expected_values(self):
        self.assertEqual(pp.STATUS_PROCESSING, "PROCESSING")
        self.assertEqual(pp.STATUS_COMPLETED, "COMPLETED")
        self.assertEqual(pp.STATUS_COMPLETED_COVERAGE_GAP,
                         "COMPLETED_WITH_COVERAGE_GAP")
        self.assertEqual(pp.STATUS_PARTIAL_ERROR, "PARTIAL_ERROR")
        self.assertEqual(pp.STATUS_FAILED_RETAINED, "FAILED_RETAINED")
        self.assertEqual(pp.STATUS_PARSE_FAILED, "PARSE_FAILED")
        self.assertEqual(pp.STATUS_ABORTED, "ABORTED")

    def test_every_status_has_a_japanese_display_label(self):
        for status in (pp.STATUS_PROCESSING, pp.STATUS_COMPLETED,
                       pp.STATUS_COMPLETED_COVERAGE_GAP,
                       pp.STATUS_PARTIAL_ERROR, pp.STATUS_FAILED_RETAINED,
                       pp.STATUS_PARSE_FAILED, pp.STATUS_ABORTED):
            self.assertIn(status, pp.STATUS_LABELS_JA)
            # 機械値そのものを表示に使わない（Codex 中9 指摘）
            self.assertNotEqual(pp.STATUS_LABELS_JA[status], status)


class NullReporterTest(unittest.TestCase):
    """NULL_REPORTER は全メソッド no-op。progress 未指定時の既定実装。"""

    def test_all_methods_are_noop_and_return_none(self):
        self.assertIsNone(
            pp.NULL_REPORTER.file_started("a.pdf", "テスト社員", "receipt"))
        self.assertIsNone(
            pp.NULL_REPORTER.page_done(1, 1, pp.OUTCOME_POSTED, "",
                                       pp.utc_now()))
        self.assertIsNone(pp.NULL_REPORTER.file_finished(pp.STATUS_COMPLETED))
        self.assertIsNone(
            pp.NULL_REPORTER.file_finished(pp.STATUS_ABORTED,
                                           error_class="ValueError"))


class TabNameGuardTest(unittest.TestCase):
    """'_' 開頭検査（sheets_output.AUDIT_TAB_NAME と同方針）。"""

    def test_progress_tab_name_starts_with_underscore(self):
        self.assertTrue(pp.PROGRESS_TAB_NAME.startswith("_"),
                        f"進捗タブ名は '_' 始まり必須: {pp.PROGRESS_TAB_NAME}")

    def test_check_tab_name_rejects_non_underscore_name(self):
        with self.assertRaises(RuntimeError):
            pp._check_tab_name("処理進捗")  # 先頭 '_' なし

    def test_check_tab_name_accepts_underscore_name(self):
        # 例外を投げなければ OK
        pp._check_tab_name("_処理進捗")


# ─────────────────────────── T2: SheetsProgressReporter ───────────────────────────

class _FakeProgressWorksheet:
    """進捗タブ用の worksheet スタブ（append_rows の応答に updatedRange を含む）。"""

    def __init__(self, values=None, sheet_title="_処理進捗"):
        self._values = [list(r) for r in values] if values is not None else []
        self._sheet_title = sheet_title
        self.appended = []
        self.updates = []
        self.row_count = 1000
        self.append_error = None
        self.update_error = None

    def get_all_values(self):
        return [list(r) for r in self._values]

    def row_values(self, idx):
        return list(self._values[idx - 1]) if len(self._values) >= idx else []

    def append_rows(self, rows, value_input_option=None):
        if self.append_error is not None:
            raise self.append_error
        start_row = len(self._values) + 1
        self._values.extend([list(r) for r in rows])
        end_row = len(self._values)
        self.appended.extend(rows)
        return {
            "spreadsheetId": "fake",
            "updates": {
                "spreadsheetId": "fake",
                "updatedRange": f"'{self._sheet_title}'!A{start_row}:"
                                f"{rowcol_to_a1(end_row, len(rows[0]))}",
                "updatedRows": len(rows),
            },
        }

    def update(self, range_name, values, value_input_option=None):
        if self.update_error is not None:
            raise self.update_error
        self.updates.append((range_name, values))


class _FakeSpreadsheet:
    """worksheet 取得/作成だけを提供するスプレッドシート代役。"""

    def __init__(self, existing=None):
        self._sheets = dict(existing or {})
        self.created = []

    def worksheet(self, title):
        if title not in self._sheets:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self._sheets[title]

    def add_worksheet(self, title, rows, cols):
        ws = _FakeProgressWorksheet(sheet_title=title)
        self._sheets[title] = ws
        self.created.append({"title": title, "rows": rows, "cols": cols})
        return ws


def _make_reporter(existing=None, clock=None):
    spreadsheet = _FakeSpreadsheet(existing)
    reporter = pp.SheetsProgressReporter(
        spreadsheet, clock=clock or (lambda: 0.0))
    return reporter, spreadsheet


class WorksheetSetupTest(unittest.TestCase):
    def test_creates_tab_with_progress_headers_when_missing(self):
        reporter, spreadsheet = _make_reporter()
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
        self.assertEqual(len(spreadsheet.created), 1)
        self.assertEqual(spreadsheet.created[0]["title"], pp.PROGRESS_TAB_NAME)
        ws = spreadsheet.worksheet(pp.PROGRESS_TAB_NAME)
        self.assertEqual(ws.get_all_values()[0], pp.PROGRESS_HEADERS)

    def test_self_heals_headerless_existing_tab(self):
        orphan = _FakeProgressWorksheet([])
        reporter, spreadsheet = _make_reporter({pp.PROGRESS_TAB_NAME: orphan})
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
        self.assertEqual(orphan.get_all_values()[0], pp.PROGRESS_HEADERS)
        self.assertEqual(spreadsheet.created, [])

    def test_disables_itself_on_header_mismatch_without_overwriting(self):
        stale = _FakeProgressWorksheet([["日付", "件名"]])
        reporter, spreadsheet = _make_reporter({pp.PROGRESS_TAB_NAME: stale})
        with redirect_stdout(io.StringIO()) as out:
            reporter.file_started("a.pdf", "テスト社員", "receipt")
            reporter.page_done(1, 1, pp.OUTCOME_POSTED, "", pp.utc_now())
            reporter.file_finished(pp.STATUS_COMPLETED)
        # 上書きされていない・追記もされていない
        self.assertEqual(stale.get_all_values(), [["日付", "件名"]])
        self.assertIn("自己無効化", out.getvalue())

    def test_reuses_existing_tab_with_matching_header(self):
        existing = _FakeProgressWorksheet([list(pp.PROGRESS_HEADERS)])
        reporter, spreadsheet = _make_reporter({pp.PROGRESS_TAB_NAME: existing})
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
        self.assertEqual(spreadsheet.created, [])
        self.assertEqual(len(existing.get_all_values()), 2)  # header + 新規行


class RowNumberFromAppendResponseTest(unittest.TestCase):
    """行番号は append_rows 応答の updatedRange から権威的に取得する（高3 裁決）。"""

    def test_row_number_is_parsed_from_updated_range(self):
        existing = _FakeProgressWorksheet([list(pp.PROGRESS_HEADERS)] + [
            ["s", "f.pdf", "u", "receipt", "1/1", 1, 0, 0, 0, "完了", "h"]
            for _ in range(5)
        ])
        reporter, _ = _make_reporter({pp.PROGRESS_TAB_NAME: existing})
        with redirect_stdout(io.StringIO()):
            reporter.file_started("new.pdf", "テスト社員", "receipt")
            reporter.file_finished(pp.STATUS_COMPLETED)
        # header(1) + 既存5行 + 新規1行 = 7 行目を更新しているはず
        self.assertEqual(existing.updates[-1][0], "A7:K7")


class ContractShapeTest(unittest.TestCase):
    """update 呼出の range・値形状を fake で固定する（低12 裁決）。"""

    def test_update_call_shape(self):
        reporter, spreadsheet = _make_reporter()
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
            reporter.file_finished(pp.STATUS_COMPLETED)
        ws = spreadsheet.worksheet(pp.PROGRESS_TAB_NAME)
        self.assertEqual(len(ws.updates), 1)
        range_name, values = ws.updates[0]
        self.assertEqual(range_name, "A2:K2")  # header=1行目、新規は2行目
        self.assertEqual(len(values), 1)
        row = values[0]
        self.assertEqual(len(row), len(pp.PROGRESS_HEADERS))
        # 頁進捗・4カウンタ・状態列の形を固定
        self.assertEqual(row[1], "a.pdf")
        self.assertEqual(row[2], "テスト社員")
        self.assertEqual(row[3], "receipt")
        self.assertEqual(row[9], "完了")


class PageNumeratorTest(unittest.TestCase):
    """頁進捗の分子＝distinct 頁数（codex R1 P2 裁決）。

    最大頁番号だと中間頁欠落で「3/3＋頁欠落あり」と自己矛盾し、
    yield 事件数だと一頁多票（同一 page_num 複数 yield）で水増しになる。
    """

    def _final_row(self, spreadsheet):
        ws = spreadsheet.worksheet(pp.PROGRESS_TAB_NAME)
        return ws.updates[-1][1][0]

    def test_missing_middle_page_shows_seen_count(self):
        reporter, spreadsheet = _make_reporter()
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
            reporter.page_done(1, 3, pp.OUTCOME_POSTED, "", pp.utc_now())
            reporter.page_done(3, 3, pp.OUTCOME_POSTED, "", pp.utc_now())
            reporter.file_finished(pp.STATUS_COMPLETED_COVERAGE_GAP)
        self.assertEqual(self._final_row(spreadsheet)[4], "2/3")

    def test_multi_yield_same_page_counts_once(self):
        reporter, spreadsheet = _make_reporter()
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
            reporter.page_done(1, 3, pp.OUTCOME_POSTED, "", pp.utc_now())
            reporter.page_done(1, 3, pp.OUTCOME_POSTED, "", pp.utc_now())
            reporter.file_finished(pp.STATUS_COMPLETED)
        row = self._final_row(spreadsheet)
        self.assertEqual(row[4], "1/3")          # 頁は1頁だけ
        self.assertEqual(row[5], 2)              # POSTED 件数は2（票数）


class ThrottleTest(unittest.TestCase):
    """節流: 20秒 interval 内の連続 page_done は1回の Sheets 書込に合流する。"""

    def test_initial_page_flushes_immediately(self):
        clock = [0.0]
        reporter, spreadsheet = _make_reporter(clock=lambda: clock[0])
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
        ws = spreadsheet.worksheet(pp.PROGRESS_TAB_NAME)
        with redirect_stdout(io.StringIO()):
            reporter.page_done(1, 3, pp.OUTCOME_POSTED, "", pp.utc_now())
        self.assertEqual(len(ws.updates), 1)  # 初頁は即時 flush

    def test_pages_within_interval_are_coalesced(self):
        clock = [0.0]
        reporter, spreadsheet = _make_reporter(clock=lambda: clock[0])
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
            reporter.page_done(1, 3, pp.OUTCOME_POSTED, "", pp.utc_now())
        ws = spreadsheet.worksheet(pp.PROGRESS_TAB_NAME)
        before = len(ws.updates)
        clock[0] = 5.0  # 20秒未満
        with redirect_stdout(io.StringIO()):
            reporter.page_done(2, 3, pp.OUTCOME_POSTED, "", pp.utc_now())
        self.assertEqual(len(ws.updates), before)  # 合流（書込なし）

    def test_flush_after_interval_elapses(self):
        clock = [0.0]
        reporter, spreadsheet = _make_reporter(clock=lambda: clock[0])
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
            reporter.page_done(1, 3, pp.OUTCOME_POSTED, "", pp.utc_now())
        ws = spreadsheet.worksheet(pp.PROGRESS_TAB_NAME)
        before = len(ws.updates)
        clock[0] = 25.0  # 20秒以上経過
        with redirect_stdout(io.StringIO()):
            reporter.page_done(2, 3, pp.OUTCOME_POSTED, "", pp.utc_now())
        self.assertEqual(len(ws.updates), before + 1)

    def test_failed_outcome_flushes_immediately(self):
        clock = [0.0]
        reporter, spreadsheet = _make_reporter(clock=lambda: clock[0])
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
            reporter.page_done(1, 3, pp.OUTCOME_POSTED, "", pp.utc_now())
        ws = spreadsheet.worksheet(pp.PROGRESS_TAB_NAME)
        before = len(ws.updates)
        clock[0] = 1.0  # interval 未満
        with redirect_stdout(io.StringIO()):
            reporter.page_done(2, 3, pp.OUTCOME_FAILED, "page_error",
                               pp.utc_now())
        self.assertEqual(len(ws.updates), before + 1)  # 初回 FAILED は即時 flush

    def test_second_failed_within_interval_respects_throttle(self):
        """故障批次（全頁エラー等）で毎頁同期書込にならないこと。

        即時 flush は檔内最初の FAILED だけ。以後の FAILED は通常節流に
        落ちる（simcodex R1 Efficiency 裁決——O(頁数) の同期書込防止）。
        """
        clock = [0.0]
        reporter, spreadsheet = _make_reporter(clock=lambda: clock[0])
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
            reporter.page_done(1, 3, pp.OUTCOME_FAILED, "page_error",
                               pp.utc_now())
        ws = spreadsheet.worksheet(pp.PROGRESS_TAB_NAME)
        before = len(ws.updates)
        clock[0] = 1.0  # interval 未満
        with redirect_stdout(io.StringIO()):
            reporter.page_done(2, 3, pp.OUTCOME_FAILED, "page_error",
                               pp.utc_now())
        self.assertEqual(len(ws.updates), before)  # 2件目は節流に従う


class DegradeTest(unittest.TestCase):
    """degrade: 連続3失敗で途中更新停止。file_finished は独立に1回試行。"""

    def test_single_failure_retries_on_next_tick(self):
        clock = [0.0]
        reporter, spreadsheet = _make_reporter(clock=lambda: clock[0])
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
        ws = spreadsheet.worksheet(pp.PROGRESS_TAB_NAME)
        ws.update_error = RuntimeError("Sheets 500")
        clock[0] = 25.0
        with redirect_stdout(io.StringIO()):
            reporter.page_done(1, 3, pp.OUTCOME_POSTED, "", pp.utc_now())
        ws.update_error = None  # 復旧
        clock[0] = 50.0
        with redirect_stdout(io.StringIO()):
            reporter.page_done(2, 3, pp.OUTCOME_POSTED, "", pp.utc_now())
        self.assertEqual(len(ws.updates), 1)  # 単発失敗後、次tickで再試行し成功

    def test_three_consecutive_failures_stop_interim_updates(self):
        clock = [0.0]
        reporter, spreadsheet = _make_reporter(clock=lambda: clock[0])
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
        ws = spreadsheet.worksheet(pp.PROGRESS_TAB_NAME)
        ws.update_error = RuntimeError("Sheets 500")
        for i, t in enumerate([20.0, 40.0, 60.0, 80.0], start=1):
            clock[0] = t
            with redirect_stdout(io.StringIO()):
                reporter.page_done(i, 5, pp.OUTCOME_POSTED, "", pp.utc_now())
        # 3回失敗した時点で degrade、4回目は試行すらしない = updates は空のまま
        self.assertEqual(ws.updates, [])

    def test_file_finished_always_attempts_final_write_even_when_degraded(self):
        clock = [0.0]
        reporter, spreadsheet = _make_reporter(clock=lambda: clock[0])
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
        ws = spreadsheet.worksheet(pp.PROGRESS_TAB_NAME)
        ws.update_error = RuntimeError("Sheets 500")
        for i, t in enumerate([20.0, 40.0, 60.0], start=1):
            clock[0] = t
            with redirect_stdout(io.StringIO()):
                reporter.page_done(i, 5, pp.OUTCOME_POSTED, "", pp.utc_now())
        ws.update_error = None  # file_finished 時点で復旧
        with redirect_stdout(io.StringIO()):
            reporter.file_finished(pp.STATUS_COMPLETED)
        self.assertEqual(len(ws.updates), 1)  # 最終書込は degrade を無視して試行


class NeverRaisesTest(unittest.TestCase):
    """常時失敗注入でも例外が漏れない（best-effort 鉄則）。"""

    def test_permanently_failing_sheet_never_raises(self):
        spreadsheet = _FakeSpreadsheet()
        reporter = pp.SheetsProgressReporter(spreadsheet, clock=lambda: 0.0)
        # worksheet()/add_worksheet() 自体が壊れているケースを模す
        spreadsheet.add_worksheet = lambda title, rows, cols: (_ for _ in ()).throw(
            RuntimeError("network down"))
        with redirect_stdout(io.StringIO()):
            reporter.file_started("a.pdf", "テスト社員", "receipt")
            reporter.page_done(1, 1, pp.OUTCOME_POSTED, "", pp.utc_now())
            reporter.file_finished(pp.STATUS_COMPLETED)
        # 例外が漏れなければ OK（ここまで到達すれば成功）


class RetryOn429Test(unittest.TestCase):
    """429 リトライは reporter 独自予算（max 2）で記帳側の退避予算を食わない。"""

    def test_succeeds_after_one_retry_within_budget(self):
        reporter, _ = _make_reporter(clock=lambda: 0.0)
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise gspread.exceptions.APIError(_fake_429_response())
            return "ok"

        with patch.object(pp.time, "sleep"), redirect_stdout(io.StringIO()):
            result = reporter._retry_write(flaky)
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 2)

    def test_raises_after_exhausting_retry_budget(self):
        reporter, _ = _make_reporter(clock=lambda: 0.0)
        calls = {"n": 0}

        def always_429():
            calls["n"] += 1
            raise gspread.exceptions.APIError(_fake_429_response())

        with patch.object(pp.time, "sleep"), redirect_stdout(io.StringIO()):
            with self.assertRaises(gspread.exceptions.APIError):
                reporter._retry_write(always_429)
        # max 2 = 予算を使い切ったら諦めて呼出元へ raise（記帳側の5回予算とは別勘定）
        self.assertEqual(calls["n"], pp._MAX_RETRY_429)


def _fake_429_response():
    class _Resp:
        status_code = 429

        def json(self):
            return {"error": {"code": 429, "message": "rate limited"}}

    return _Resp()


if __name__ == "__main__":
    unittest.main()
