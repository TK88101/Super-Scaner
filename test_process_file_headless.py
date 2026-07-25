"""IP-304/IP-306 T3: process_file の headless 頁級緩衝集約＋硬去重＋占位行併入頁級寫の統合テスト。

B4 Plan (docs/plans/2026-07-24-b4-closing-semantics.md) §2.2/§2.3/T3。真 PostingLedger
（fake Firestore 台賬）＋fake writer（メモリ Sheets）＋patched process_pipeline で、真
Drive/Sheets/Firestore 不接。

headless の戻り値は `main.HeadlessOutcome`（`outcome: ProcessOutcome` ＋ `retryable: bool`
＋ `dead_letter_payload: dict | None`）——T4 の回報/memo 接線が必要とする付帯情報を運ぶ
（§2.3）。UI 経路（ledger=None）は従来通り bool のまま（UiPathUnchangedTest で固定）。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import main
from doc_types import DocType
from posting_ledger import PostingLedger, derive_page_id
from sheets_output import SheetsOutputWriter, TAG_COL_INDEX
# 夾具（T4/T5、B4 復用）から fake 部品を import——二重定義を避け単一ソース化。
from headless_rerun_fixture import (
    FakeFirestore, FakeWriter, _FakeResolver, error_page, make_ledger,
    page as _page, run_headless,
)


def _run(writer, ledger, pages):
    return run_headless(writer, ledger, pages)


def _make_ledger(fs, writer):
    return make_ledger(fs, writer)


def _valid_result(vendor, amount, extra=None):
    result = {"entries": [{"amount": amount, "debit_account": "備品・消耗品費"}],
              "vendor": vendor, "date": "2026/06/01"}
    if extra:
        result.update(extra)
    return result


def _zero_entry_result(**extra):
    result = {"entries": [], "vendor": "", "date": "", "_unrecognized": True}
    result.update(extra)
    return result


def _yield(result, page_num, total):
    return {"result": result, "page_num": page_num, "total_pages": total,
            "page_bytes": b"x"}


class HeadlessIdempotencyTest(unittest.TestCase):
    def test_dod1_second_run_all_skip_zero_new_rows(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_page(1, 3, "店A", 1000), _page(2, 3, "店B", 2000),
                 _page(3, 3, "店C", 3000)]

        out1 = _run(writer, ledger, pages)
        self.assertIs(out1.outcome, main.ProcessOutcome.SUCCESS)
        rows_after_1 = len(writer.sheets["田中_領収書"])
        appends_after_1 = writer.append_calls
        self.assertEqual(rows_after_1, 3)

        # 2 回目（同一 fake ledger/fs＝台賬 CONFIRMED 残存）→ 全頁 SKIP・零新増行
        pages2 = [_page(1, 3, "店A", 1000), _page(2, 3, "店B", 2000),
                  _page(3, 3, "店C", 3000)]
        out2 = _run(writer, ledger, pages2)
        self.assertIs(out2.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(len(writer.sheets["田中_領収書"]), rows_after_1)
        self.assertEqual(writer.append_calls, appends_after_1)  # append 呼出ゼロ増

    def test_dod2_crash_before_confirm_reruns_without_duplicate(self):
        # 3 頁中 2 頁書込後、3 頁目 commit で崩潰（PENDING 残置・未 landed）→ 再跑
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)

        real_commit = writer.commit_page
        state = {"n": 0}

        def crashing_commit(pw):
            state["n"] += 1
            if state["n"] == 3:
                raise RuntimeError("kill: append 直前で崩潰")
            return real_commit(pw)

        writer.commit_page = crashing_commit
        pages = [_page(1, 3, "店A", 1000), _page(2, 3, "店B", 2000),
                 _page(3, 3, "店C", 3000)]
        with self.assertRaises(RuntimeError):
            _run(writer, ledger, pages)
        self.assertEqual(len(writer.sheets["田中_領収書"]), 2)

        writer.commit_page = real_commit
        pages2 = [_page(1, 3, "店A", 1000), _page(2, 3, "店B", 2000),
                  _page(3, 3, "店C", 3000)]
        out = _run(writer, ledger, pages2)
        self.assertIs(out.outcome, main.ProcessOutcome.SUCCESS)
        sheet = writer.sheets["田中_領収書"]
        self.assertEqual(len(sheet), 3)
        self.assertEqual([r[1] for r in sheet], ["店A", "店B", "店C"])

    def test_dod2_crash_after_landed_before_confirm_skips_on_rerun(self):
        fs = FakeFirestore()
        writer = FakeWriter()

        calls = {"n": 0}
        base_runner = fs.runner()

        def flaky_runner(body):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("kill: CONFIRMED 直前で崩潰")
            return base_runner(body)

        ledger = PostingLedger(fs, "cust:hash", sheet_probe=writer.probe_page,
                               transaction_runner=flaky_runner)
        pages = [_page(1, 1, "店A", 1000)]
        with self.assertRaises(RuntimeError):
            _run(writer, ledger, pages)
        self.assertEqual(len(writer.sheets["田中_領収書"]), 1)

        ledger2 = _make_ledger(fs, writer)
        pages2 = [_page(1, 1, "店A", 1000)]
        out = _run(writer, ledger2, pages2)
        self.assertIs(out.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(len(writer.sheets["田中_領収書"]), 1)


class HeadlessEscalateContinuityTest(unittest.TestCase):
    def test_page_num_reappearance_escalates(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_page(1, 3, "店A", 1000), _page(2, 3, "店B", 2000),
                 _page(1, 3, "店A再", 1000)]
        out = _run(writer, ledger, pages)
        self.assertIs(out.outcome, main.ProcessOutcome.ESCALATED)

    def test_check_page_escalate_stops_file(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pid = derive_page_id("cust:hash", 1)
        fs.store[("jobs", "cust:hash", "postings", pid)] = {
            "status": "PENDING", "sheet_tab": "田中_領収書",
            "predicted_row_range": None, "row_fingerprint": "",
        }
        out = _run(writer, ledger, [_page(1, 1, "店A", 1000)])
        self.assertIs(out.outcome, main.ProcessOutcome.ESCALATED)
        self.assertEqual(writer.append_calls, 0)


class MultiTicketPageTest(unittest.TestCase):
    def test_multiple_tickets_one_page_single_append(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_page(1, 1, "店A", 1000), _page(1, 1, "店B", 2000)]
        out = _run(writer, ledger, pages)
        self.assertIs(out.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(writer.append_calls, 1)
        self.assertEqual(len(writer.sheets["田中_領収書"]), 2)


class UiPathUnchangedTest(unittest.TestCase):
    def test_ledger_none_uses_append_entries(self):
        calls = []

        class _UiWriter:
            def append_entries(self, **kw):
                calls.append(kw)

        pages = [_page(1, 1, "店A", 1000)]
        with patch.object(main, "process_pipeline", return_value=iter(pages)), \
             patch.object(main, "PageUrlResolver", _FakeResolver), \
             patch.object(main, "send_notification"):
            out = main.process_file(
                service=None, sheets_writer=_UiWriter(), file_path="d.pdf",
                uploader_name="田中", chat_id=None, doc_type=DocType.RECEIPT,
                drive_file_id=None)
        self.assertIs(out, True)  # UI 経路は bool のまま（HeadlessOutcome ではない）
        self.assertEqual(len(calls), 1)


class IntakeGateBaseWiringTest(unittest.TestCase):
    def test_gate_passes_base_on_process(self):
        file = {"id": "f1", "properties": {"sandevistan_posting_id": "cust:hash"}}

        class _Rep:
            def get_job(self, base):
                # current_state 必須（IP-308/T4 状態白名単、B4 Plan §2.4）
                return {"posting_id": base, "lease_epoch": 1,
                       "current_state": "POSTING_IN_PROGRESS"}

            def write_alert(self, *a):
                pass

        with patch.object(main.config, "headless_mode", return_value=True):
            should, base, epoch = main._headless_intake_gate(
                service=None, file=file, input_folder_id="in", reporter=_Rep())
        self.assertTrue(should)
        self.assertEqual(base, "cust:hash")
        self.assertEqual(epoch, 1)


# ============================================================
# B4 Plan §2.2/§2.3/T3: 占位頁併入頁級寫＋頁級 outcome モデル
# ============================================================

class ContentErrorPlaceholderTest(unittest.TestCase):
    """DoD①②: CONTENT 頁は占位行 1 行として頁緩衝に入り、頁原子で書込まれる。
    台賬 ticket_count=0。重跑は SKIP、占位行は恆に 1 行のまま増えない。
    """

    def test_content_error_page_is_written_as_single_placeholder_row(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_page(1, 2, "店A", 1000),
                 error_page(2, 2, error_class="CONTENT")]

        out = _run(writer, ledger, pages)

        # 全頁 placeholder ではない（p1 は正常）→ PARTIAL（B3/UI の「認識不能でも
        # 成功」とは異なる新語義、§2.3 附帯変化）
        self.assertIs(out.outcome, main.ProcessOutcome.PARTIAL)
        self.assertEqual(len(writer.sheets["田中_領収書"]), 2)  # 正常1行+占位1行
        self.assertEqual(writer.placeholder_calls, [])  # append_entries 経路は廃止

        pid = derive_page_id("cust:hash", 2)
        doc = fs.store[("jobs", "cust:hash", "postings", pid)]
        self.assertEqual(doc["status"], "CONFIRMED")
        self.assertEqual(doc["ticket_count"], 0)  # ticket_count=有効票数のみ

    def test_rerun_skips_and_placeholder_row_count_stays_one(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_page(1, 2, "店A", 1000),
                 error_page(2, 2, error_class="CONTENT")]
        _run(writer, ledger, pages)
        rows_after_1 = len(writer.sheets["田中_領収書"])

        # 再跑（同 fs、新 ledger＝プロセス再起動を模す）: 全頁 SKIP、増行なし
        ledger2 = _make_ledger(fs, writer)
        pages2 = [_page(1, 2, "店A", 1000),
                  error_page(2, 2, error_class="CONTENT")]
        out2 = _run(writer, ledger2, pages2)

        self.assertIs(out2.outcome, main.ProcessOutcome.PARTIAL)
        self.assertEqual(len(writer.sheets["田中_領収書"]), rows_after_1)


class ErrorClassZeroWriteTest(unittest.TestCase):
    """DoD③: RETRYABLE/UNKNOWN 頁は零書込、檔級 FAILED。retryable フラグで
    memo 抑制対象（RETRYABLE）か記録対象（UNKNOWN）かを T4 へ橋渡しする。
    """

    def test_retryable_page_is_zero_write_and_failed_retryable_true(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_page(1, 2, "店A", 1000),
                 error_page(2, 2, error_class="RETRYABLE")]

        out = _run(writer, ledger, pages)

        self.assertIs(out.outcome, main.ProcessOutcome.FAILED)
        self.assertTrue(out.retryable)
        # p1 は WRITE されない（頁境界の flush は行われるが、p2 が FAILED
        # で檔案は全体として保持されるため成功頁の可視化はしない設計ではなく
        # 実際は commit 済み — 台賬に p1 の CONFIRMED は残る。ここでは
        # zero-write なのは p2 のみであることを台賬で確認する）
        pid2 = derive_page_id("cust:hash", 2)
        self.assertNotIn(("jobs", "cust:hash", "postings", pid2), fs.store)

    def test_unknown_page_is_zero_write_and_failed_retryable_false(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [error_page(1, 1, error_class="UNKNOWN")]

        out = _run(writer, ledger, pages)

        self.assertIs(out.outcome, main.ProcessOutcome.FAILED)
        self.assertFalse(out.retryable)
        self.assertEqual(writer.append_calls, 0)

    def test_missing_error_class_key_defaults_to_unknown(self):
        # 消費側デフォルト（B4 Plan §2.1）: 旧 fixture 等でキー欠落 → UNKNOWN
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [error_page(1, 1, error_class=None)]

        out = _run(writer, ledger, pages)

        self.assertIs(out.outcome, main.ProcessOutcome.FAILED)
        self.assertFalse(out.retryable)


class DeadLetterTest(unittest.TestCase):
    """DoD④: 全頁が占位頁 → DEAD_LETTER。payload 精確斷言＋敏感字段缺席。"""

    def test_all_pages_placeholder_returns_dead_letter_with_exact_payload(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [error_page(1, 2, error_class="CONTENT"),
                 error_page(2, 2, error_class="CONTENT")]

        out = _run(writer, ledger, pages)

        self.assertIs(out.outcome, main.ProcessOutcome.DEAD_LETTER)
        self.assertEqual(out.dead_letter_payload, {
            "stage": "ocr",
            "error_class": "NON_RETRYABLE",
            "message": "all_pages_unreadable: 2/2 pages [p1,p2]",
        })
        # 敏感字段缺席（basic-design/03 §3 日誌白名単）
        self.assertEqual(set(out.dead_letter_payload.keys()),
                         {"stage", "error_class", "message"})
        self.assertNotIn("dummy", out.dead_letter_payload["message"])  # ファイル名不入
        # 2 頁とも占位行として書込まれている（DEAD_LETTER でも書込自体は行う）
        self.assertEqual(len(writer.sheets["田中_領収書"]), 2)

    def test_all_pages_zero_entry_unrecognized_without_page_error_is_dead_letter(self):
        # _page_error を伴わない「JSON成功だがentries空」の認識不能頁も占位頁扱い
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_yield(_zero_entry_result(), 1, 1)]

        out = _run(writer, ledger, pages)

        self.assertIs(out.outcome, main.ProcessOutcome.DEAD_LETTER)
        self.assertEqual(out.dead_letter_payload["message"],
                         "all_pages_unreadable: 1/1 pages [p1]")


class PriorityTableDrivenTest(unittest.TestCase):
    """DoD⑤: 混合優先級（RETRYABLE > UNKNOWN > 占位 > 成功；ESCALATE 最高）。"""

    def _run_mixed(self, page_yields):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        return _run(writer, ledger, page_yields)

    def test_success_plus_retryable_is_failed_retryable(self):
        out = self._run_mixed(
            [_page(1, 2, "店A", 1000), error_page(2, 2, error_class="RETRYABLE")])
        self.assertIs(out.outcome, main.ProcessOutcome.FAILED)
        self.assertTrue(out.retryable)

    def test_success_plus_unknown_is_failed_not_retryable(self):
        out = self._run_mixed(
            [_page(1, 2, "店A", 1000), error_page(2, 2, error_class="UNKNOWN")])
        self.assertIs(out.outcome, main.ProcessOutcome.FAILED)
        self.assertFalse(out.retryable)

    def test_retryable_plus_unknown_prefers_retryable(self):
        out = self._run_mixed(
            [error_page(1, 2, error_class="RETRYABLE"),
             error_page(2, 2, error_class="UNKNOWN")])
        self.assertIs(out.outcome, main.ProcessOutcome.FAILED)
        self.assertTrue(out.retryable)

    def test_retryable_plus_placeholder_prefers_retryable(self):
        out = self._run_mixed(
            [error_page(1, 2, error_class="RETRYABLE"),
             error_page(2, 2, error_class="CONTENT")])
        self.assertIs(out.outcome, main.ProcessOutcome.FAILED)
        self.assertTrue(out.retryable)

    def test_unknown_plus_placeholder_prefers_unknown(self):
        out = self._run_mixed(
            [error_page(1, 2, error_class="UNKNOWN"),
             error_page(2, 2, error_class="CONTENT")])
        self.assertIs(out.outcome, main.ProcessOutcome.FAILED)
        self.assertFalse(out.retryable)

    def test_placeholder_plus_success_is_partial(self):
        out = self._run_mixed(
            [_page(1, 2, "店A", 1000), error_page(2, 2, error_class="CONTENT")])
        self.assertIs(out.outcome, main.ProcessOutcome.PARTIAL)

    def test_all_success_is_success(self):
        out = self._run_mixed(
            [_page(1, 2, "店A", 1000), _page(2, 2, "店B", 2000)])
        self.assertIs(out.outcome, main.ProcessOutcome.SUCCESS)

    def test_escalate_wins_over_retryable(self):
        # p1=RETRYABLE（零書込・記録）、p2=混型頁（有効+零entry並存）→ ESCALATE 最高優先
        pages = [error_page(1, 2, error_class="RETRYABLE"),
                 _yield(_valid_result("店A", 1000), 2, 2),
                 _yield(_zero_entry_result(), 2, 2)]
        out = self._run_mixed(pages)
        self.assertIs(out.outcome, main.ProcessOutcome.ESCALATED)

    def test_escalate_detected_mid_loop_at_page_boundary(self):
        # 混型頁が「最終頁ではない」場合、頁境界の flush（ループ内 flush、末尾
        # flush ではない）で ESCALATE が検出される分岐を踏む。
        pages = [_yield(_valid_result("店A", 1000), 1, 2),
                 _yield(_zero_entry_result(), 1, 2),
                 _yield(_valid_result("店B", 2000), 2, 2)]
        out = self._run_mixed(pages)
        self.assertIs(out.outcome, main.ProcessOutcome.ESCALATED)

    def test_unknown_error_class_value_falls_back_to_unknown(self):
        # error_class に三値以外の不正値が来ても防御的に UNKNOWN（DEAD_LETTER/
        # CONTENT に誤って倒さない、B4 Plan §2.1）
        out = self._run_mixed([error_page(1, 1, error_class="BOGUS_VALUE")])
        self.assertIs(out.outcome, main.ProcessOutcome.FAILED)
        self.assertFalse(out.retryable)

    def test_error_page_with_ambiguous_ledger_witness_escalates(self):
        # RETRYABLE 頁だが台賬に witness 欠損の PENDING が既にある → check_page が
        # ESCALATE を返す分岐（_classify_and_flush_page の "error" 枝の中）
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pid = derive_page_id("cust:hash", 1)
        fs.store[("jobs", "cust:hash", "postings", pid)] = {
            "status": "PENDING", "sheet_tab": "田中_領収書",
            "predicted_row_range": None, "row_fingerprint": "",
        }
        out = _run(writer, ledger, [error_page(1, 1, error_class="RETRYABLE")])
        self.assertIs(out.outcome, main.ProcessOutcome.ESCALATED)

    def test_zero_pages_yielded_returns_failed(self):
        # process_pipeline が一件も yield しない（総頁数0、稀有ケース）→ FAILED
        out = self._run_mixed([])
        self.assertIs(out.outcome, main.ProcessOutcome.FAILED)
        self.assertFalse(out.retryable)


class MultiTicketPagePlusFailedPageCountTest(unittest.TestCase):
    """DoD⑥: 一頁多票＋另頁失敗 → 物理頁で計數（3 票頁＋1 敗頁 = 1/2 非 1/4）。

    総頁数（DEAD_LETTER message の分母相当）は物理頁数で数える——票数では
    水増しされない。ここでは 3 票の頁 + CONTENT 占位頁 1 頁の混合で
    PARTIAL になり、DEAD_LETTER 相当の全頁占位ケースで分母を直接検証する。
    """

    def test_dead_letter_denominator_counts_physical_pages_not_tickets(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        # p1: 3 票（同一頁に3 yield）、p2: 1 敗頁（CONTENT）
        pages = [
            _yield(_valid_result("店A", 1000), 1, 2),
            _yield(_valid_result("店B", 2000), 1, 2),
            _yield(_valid_result("店C", 3000), 1, 2),
            error_page(2, 2, error_class="CONTENT"),
        ]
        out = _run(writer, ledger, pages)
        # 物理頁は 2（p1, p2）→ PARTIAL（全頁占位ではない）。旧実装のバグでは
        # yield 数（4）を分母にしてしまう恐れがあったため、3 票が 1 頁として
        # 数えられていることを台賬の commit 回数で確認する。
        self.assertIs(out.outcome, main.ProcessOutcome.PARTIAL)
        self.assertEqual(writer.append_calls, 2)  # p1(3票原子1回)+p2(占位1回)=2回

    def test_dead_letter_message_denominator_is_physical_page_count(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        # p1: 3 票が全て占位（zero-entry）で 1 頁に集約、p2 も占位 → 全頁占位
        pages = [
            _yield(_zero_entry_result(), 1, 2),
            error_page(2, 2, error_class="CONTENT"),
        ]
        out = _run(writer, ledger, pages)
        self.assertIs(out.outcome, main.ProcessOutcome.DEAD_LETTER)
        self.assertEqual(out.dead_letter_payload["message"],
                         "all_pages_unreadable: 2/2 pages [p1,p2]")


class PriorPageKindNoneFallbackTest(unittest.TestCase):
    """`_prior_page_kind` の None 縁（confirmed_ticket_count が読めない理論上の
    局面）は誠實優先で PLACEHOLDER_PRIOR 側へ倒す（POSTED を騙らない）。"""

    def test_none_ticket_count_defaults_to_placeholder_prior(self):
        class _NoneLedger:
            def confirmed_ticket_count(self, page_id):
                return None

        self.assertEqual(
            main._prior_page_kind(_NoneLedger(), "any:p1"), "PLACEHOLDER_PRIOR")

    def test_positive_ticket_count_is_posted_prior(self):
        class _Ledger:
            def confirmed_ticket_count(self, page_id):
                return 3

        self.assertEqual(
            main._prior_page_kind(_Ledger(), "any:p1"), "POSTED_PRIOR")

    def test_zero_ticket_count_is_placeholder_prior(self):
        class _Ledger:
            def confirmed_ticket_count(self, page_id):
                return 0

        self.assertEqual(
            main._prior_page_kind(_Ledger(), "any:p1"), "PLACEHOLDER_PRIOR")


class ExtractTicketsOnlyValidTest(unittest.TestCase):
    """DoD⑦: `_extract_tickets` は entries 非空の result のみ票として数える。"""

    def test_zero_entry_results_are_excluded(self):
        results = [_valid_result("店A", 1000), _zero_entry_result()]
        tickets = main._extract_tickets(results)
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].vendor, "店A")

    def test_all_zero_entry_yields_empty_tickets(self):
        results = [_zero_entry_result(), _zero_entry_result()]
        self.assertEqual(main._extract_tickets(results), [])

    def test_all_valid_counts_all(self):
        results = [_valid_result("店A", 1000), _valid_result("店B", 2000)]
        self.assertEqual(len(main._extract_tickets(results)), 2)


class PriorRoundSemanticStabilityTest(unittest.TestCase):
    """DoD⑧⑨: 前輪台賬状態は今輪の一時的な OCR 結果差異で檔級語義を翻さない。"""

    def test_dod8_prior_placeholder_confirmed_then_success_ocr_stays_partial(self):
        # 前輪: p1 成功・p2 占位（CONTENT）→ PARTIAL、CONFIRMED(ticket_count=0)
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages1 = [_page(1, 2, "店A", 1000),
                 error_page(2, 2, error_class="CONTENT")]
        out1 = _run(writer, ledger, pages1)
        self.assertIs(out1.outcome, main.ProcessOutcome.PARTIAL)
        rows_after_1 = len(writer.sheets["田中_領収書"])

        # 後輪: p2 が今度は OCR 成功（有効 entries）→ check_page は SKIP
        # （既 CONFIRMED）のため書込まれず、恆 PARTIAL のまま POSTED へ翻らない
        ledger2 = _make_ledger(fs, writer)
        pages2 = [_page(1, 2, "店A", 1000), _page(2, 2, "店B復活", 2000)]
        out2 = _run(writer, ledger2, pages2)
        self.assertIs(out2.outcome, main.ProcessOutcome.PARTIAL)
        self.assertEqual(len(writer.sheets["田中_領収書"]), rows_after_1)  # 増行なし

    def test_dod9_prior_success_confirmed_then_retryable_stays_success(self):
        # 前輪: p1, p2 とも成功 → SUCCESS、両頁 CONFIRMED(ticket_count>0)
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages1 = [_page(1, 2, "店A", 1000), _page(2, 2, "店B", 2000)]
        out1 = _run(writer, ledger, pages1)
        self.assertIs(out1.outcome, main.ProcessOutcome.SUCCESS)
        rows_after_1 = len(writer.sheets["田中_領収書"])

        # 後輪: p2 が今度は RETRYABLE（一時故障）→ check_page は SKIP（既
        # CONFIRMED・ticket_count>0）のため POSTED_PRIOR 扱い、檔級語義は
        # SUCCESS のまま翻らない（#1c／DoD⑨）
        ledger2 = _make_ledger(fs, writer)
        pages2 = [_page(1, 2, "店A", 1000),
                 error_page(2, 2, error_class="RETRYABLE")]
        out2 = _run(writer, ledger2, pages2)
        self.assertIs(out2.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(len(writer.sheets["田中_領収書"]), rows_after_1)


class NoAggregateAppendEntriesTest(unittest.TestCase):
    """DoD⑩: main.py:498-520 の檔級聚合占位行塊は削除済み——headless は
    どの outcome でも writer.append_entries（旧集約経路）を一切呼ばない
    （行為 spy、占位行は commit_page 経由の頁原子書込のみ）。
    """

    def _assert_zero_append_entries(self, page_yields):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        _run(writer, ledger, page_yields)
        self.assertEqual(writer.placeholder_calls, [])

    def test_success_zero_append_entries(self):
        self._assert_zero_append_entries([_page(1, 1, "店A", 1000)])

    def test_partial_zero_append_entries(self):
        self._assert_zero_append_entries(
            [_page(1, 2, "店A", 1000), error_page(2, 2, error_class="CONTENT")])

    def test_dead_letter_zero_append_entries(self):
        self._assert_zero_append_entries(
            [error_page(1, 1, error_class="CONTENT")])

    def test_failed_zero_append_entries(self):
        self._assert_zero_append_entries(
            [error_page(1, 1, error_class="UNKNOWN")])


class MixedPageEscalatesTest(unittest.TestCase):
    """DoD⑪: 混型頁（同頁に有効result と零-entry result が並存）→ 零書込・
    即時 ESCALATED。跨重跑: ESCALATE した頁は台賬未着手のため、次輪で
    正常な（非混型の）yield が来れば普通に WRITE される。
    """

    def test_mixed_valid_and_zero_entry_same_page_escalates(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_yield(_valid_result("店A", 1000), 1, 1),
                 _yield(_zero_entry_result(), 1, 1)]
        out = _run(writer, ledger, pages)
        self.assertIs(out.outcome, main.ProcessOutcome.ESCALATED)
        self.assertEqual(writer.append_calls, 0)
        pid = derive_page_id("cust:hash", 1)
        self.assertNotIn(("jobs", "cust:hash", "postings", pid), fs.store)

    def test_error_co_present_with_other_result_escalates(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_yield(_valid_result("店A", 1000), 1, 1),
                 {"result": {"_page_error": True, "_error_class": "CONTENT",
                             "entries": [], "_unrecognized": True},
                  "page_num": 1, "total_pages": 1, "page_bytes": b"x"}]
        out = _run(writer, ledger, pages)
        self.assertIs(out.outcome, main.ProcessOutcome.ESCALATED)

    def test_escalated_page_untouched_ledger_allows_clean_write_on_rerun(self):
        fs = FakeFirestore()
        writer = FakeWriter()
        ledger = _make_ledger(fs, writer)
        pages = [_yield(_valid_result("店A", 1000), 1, 1),
                 _yield(_zero_entry_result(), 1, 1)]
        out1 = _run(writer, ledger, pages)
        self.assertIs(out1.outcome, main.ProcessOutcome.ESCALATED)
        self.assertEqual(len(writer.sheets.get("田中_領収書", [])), 0)

        # 再跑（修正後の正常 yield）→ 台賬未着手だったので普通に WRITE される
        ledger2 = _make_ledger(fs, writer)
        pages2 = [_page(1, 1, "店A", 1000)]
        out2 = _run(writer, ledger2, pages2)
        self.assertIs(out2.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(len(writer.sheets["田中_領収書"]), 1)


class ContentPlaceholderRowShapeTest(unittest.TestCase):
    """占位行物理形態（memo/vendor/URL/紅標列位）を真 SheetsOutputWriter.build_page_write
    の純構造で断言する（FakeWriter を経由しない、Codex 初審 #11）。
    """

    def _real_writer(self):
        writer = SheetsOutputWriter.__new__(SheetsOutputWriter)
        writer._tab_next_txn = {}
        writer._tabs_sanitized = set()
        return writer

    def test_content_override_produces_fixed_memo_vendor_date_and_url(self):
        writer = self._real_writer()
        raw_result = {
            "date": "", "vendor": "", "invoice_num": "",
            "memo": "ページ処理エラー: ValueError",
            "entries": [], "_unrecognized": True, "_page_error": True,
            "_error_class": "CONTENT",
        }
        overridden = main._apply_content_error_override(
            raw_result, "invoice_2026.pdf", page_num=2, total_pages=3)

        pw = writer.build_page_write(
            "田中", DocType.RECEIPT, [overridden], ["http://p2-url"],
            start_txn_no=5)

        self.assertEqual(len(pw.rows), 1)
        row = pw.rows[0]
        self.assertEqual(row[0], 5)                          # 取引No
        self.assertEqual(row[1], "")                          # 取引日
        self.assertEqual(row[5], "invoice_2026.pdf")           # 借方取引先=檔名
        self.assertEqual(row[18], "⚠ ページ処理エラー p2/3 手動再スキャン要")
        self.assertEqual(row[27], "http://p2-url")             # 原票URL
        self.assertEqual(row[TAG_COL_INDEX], "赤系")            # 常に赤系
        # vendor=檔名（非空）のため has_partial=True → I列赤（部分認識）＋
        # B列赤（date 空欄）。F列（vendor 空欄）は vendor 非空のため付かない。
        cols = {(op.col, op.severity) for op in pw.highlight_ops}
        self.assertIn(("I", "high"), cols)
        self.assertIn(("B", "high"), cols)
        self.assertNotIn(("F", "high"), cols)
        self.assertNotIn(("full", "high"), cols)

    def test_override_does_not_mutate_original_result_dict(self):
        writer = self._real_writer()
        raw_result = {"date": "", "vendor": "", "memo": "orig", "entries": [],
                      "_unrecognized": True, "_page_error": True,
                      "_error_class": "CONTENT"}
        main._apply_content_error_override(raw_result, "f.pdf", 1, 1)
        self.assertEqual(raw_result["memo"], "orig")  # 不可変（破壊しない）
        self.assertEqual(raw_result["vendor"], "")


if __name__ == "__main__":
    unittest.main()
