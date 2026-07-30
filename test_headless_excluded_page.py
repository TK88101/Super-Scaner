"""IP-402 T1-T6: headless 終態機の除外ページ適配。

Plan: docs/plans/2026-07-30-headless-excluded-page-adaptation.md

IP-401 で producer（ocr_engine）は封筒・社会保険料通知書を
`_excluded_page=True` ＋ `_exclude_destination` 付きで yield するようになったが、
headless 側（`_process_file_headless`）はそれを「占位ページ」と誤認していた：

  ① MF 区へ赤い占位行を書く（帳簿汚染）
  ② 全頁除外の PDF が DEAD_LETTER（死信）に落ちる
  ③ 台賬に ticket_count=0 の page doc が残り、分類が永久固化する

本テストは修正後の不変式を固定する：

  - 除外ページは MF 区へ**一切書かない**（仕訳行も占位行も提示行も、書込回数ゼロ）
  - 留痕は監査タブのみ。page doc は作らない（`ticket_count` 二分を揺らさない）
  - ただし `check_page` は**必ず見る**（載せないことと参照しないことは別）
  - 全頁除外 → PARTIAL（POSTED とは偽らない・DEAD_LETTER にも落とさない）
  - 除外＋記帳頁 → SUCCESS（封筒混在は最頻ケース。永久非終端にしない）
  - 分類 drift（過去輪と今輪で判定が違う頁）は監査タブに両方の分類を残す

    venv311/bin/python -m unittest test_headless_excluded_page -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import main
from doc_types import DocType
from ocr_engine import EXCLUDE_DEST_AUDIT_TAB, EXCLUDE_DEST_MF_TAB
from posting_ledger import derive_page_id
from sheets_output import AUDIT_VERDICT_DRIFT, AUDIT_VERDICT_EXCLUDED
from headless_rerun_fixture import (
    DEFAULT_BASE, FakeFirestore, FakeReporter, FakeWriter, HeadlessRerunFixture,
    _FakeResolver, error_page, excluded_page, make_ledger, page as _page,
    run_headless, yield_of, zero_entry_result,
)

_TAB = "田中_領収書"


def _zero_entry_page(page_num, total):
    """占位頁（entries 空・`_page_error` 無し・除外でもない）の yield。"""
    return yield_of(zero_entry_result(), page_num, total)


def _env():
    fs = FakeFirestore()
    writer = FakeWriter()
    return fs, writer, make_ledger(fs, writer)


def _seed_confirmed_page(fs, page_num, ticket_count):
    """過去輪の CONFIRMED page doc を台賬へ直接置く（drift の前提条件）。

    ticket_count>0 → 過去輪は仕訳を書いた（POSTED_PRIOR）
    ticket_count==0 → 過去輪は占位行を書いた（PLACEHOLDER_PRIOR）
    """
    pid = derive_page_id(DEFAULT_BASE, page_num)
    fs.store[("jobs", DEFAULT_BASE, "postings", pid)] = {
        "status": "CONFIRMED", "ticket_count": ticket_count,
        "sheet_tab": _TAB, "predicted_row_range": [1, 1], "row_fingerprint": "x",
    }
    return pid


def _run_ui(writer, page_yields):
    """UI 版（ledger=None）経路で 1 ファイル流す。"""
    with patch.object(main, "process_pipeline", return_value=iter(page_yields)), \
         patch.object(main, "PageUrlResolver", _FakeResolver), \
         patch.object(main, "send_notification"):
        return main.process_file(
            service=None, sheets_writer=writer, file_path="d.pdf",
            uploader_name="田中", chat_id=None, doc_type=DocType.RECEIPT,
            drive_file_id=None)


# ============================================================
# T1: 頁級 kind の分離（純関数 _classify_page_result_shape）
# ============================================================

class ClassifyExcludedShapeTest(unittest.TestCase):
    """Plan §4.1 の表 5 行＋error 優先の異常入力。"""

    def _results(self, *yields):
        return [y["result"] for y in yields]

    def test_excluded_only_audit_destination(self):
        results = self._results(excluded_page(1, 1))
        self.assertEqual(main._classify_page_result_shape(results),
                         ("excluded", EXCLUDE_DEST_AUDIT_TAB))

    def test_excluded_only_mf_destination(self):
        results = self._results(excluded_page(
            1, 1, reason="social_insurance_notice",
            destination=EXCLUDE_DEST_MF_TAB))
        self.assertEqual(main._classify_page_result_shape(results),
                         ("excluded", EXCLUDE_DEST_MF_TAB))

    def test_excluded_plus_valid_escalates(self):
        results = self._results(excluded_page(1, 1), _page(1, 1, "店A", 1000))
        self.assertEqual(main._classify_page_result_shape(results),
                         ("escalate", "mixed_excluded_and_valid"))

    def test_excluded_plus_placeholder_escalates(self):
        results = self._results(excluded_page(1, 1), _zero_entry_page(1, 1))
        self.assertEqual(main._classify_page_result_shape(results),
                         ("escalate", "mixed_excluded_and_placeholder"))

    def test_multiple_excluded_with_mixed_destinations_escalates(self):
        results = self._results(
            excluded_page(1, 1, destination=EXCLUDE_DEST_AUDIT_TAB),
            excluded_page(1, 1, destination=EXCLUDE_DEST_MF_TAB))
        self.assertEqual(main._classify_page_result_shape(results),
                         ("escalate", "mixed_exclude_destinations"))

    def test_multiple_excluded_same_destination_is_excluded(self):
        results = self._results(excluded_page(1, 1), excluded_page(1, 1))
        self.assertEqual(main._classify_page_result_shape(results),
                         ("excluded", EXCLUDE_DEST_AUDIT_TAB))

    def test_page_error_and_excluded_on_same_result_prefers_error(self):
        # 異常入力（現行 producer では起きない）。error 経路が先に判定する
        # ——保守側デフォルト（除外として黙って捨てない）。
        result = {**excluded_page(1, 1)["result"],
                  "_page_error": True, "_error_class": "RETRYABLE"}
        self.assertEqual(main._classify_page_result_shape([result]),
                         ("error", "RETRYABLE"))

    def test_page_error_result_co_present_with_excluded_result_escalates(self):
        results = self._results(error_page(1, 1, error_class="RETRYABLE"),
                                excluded_page(1, 1))
        self.assertEqual(main._classify_page_result_shape(results),
                         ("escalate", "mixed_error_and_other_result"))

    def test_missing_destination_key_defaults_to_audit_tab(self):
        # producer が宣言し忘れた場合の消費側デフォルト（MF を汚さない側へ倒す）
        result = {k: v for k, v in excluded_page(1, 1)["result"].items()
                  if k != "_exclude_destination"}
        self.assertEqual(main._classify_page_result_shape([result]),
                         ("excluded", EXCLUDE_DEST_AUDIT_TAB))


class ExcludedKindsSetTest(unittest.TestCase):
    """T2 DoD（2026-07-30 訂正）: 除外 kind は `EXCLUDED` 1 種のみ。

    第3版 Plan は drift 用に `EXCLUDED_DRIFT` を置いていたが、集約層では
    `EXCLUDED` と 100% 同一に扱われる別名にすぎず、drift の事実は監査行が
    持続的に持っている。網羅的に kind を分岐する後続コードへ「これは別名」
    という記憶義務を残すだけなので撤回した（simcodex Round 1 → Codex 裁決）。
    """

    def test_only_plain_excluded_kind_is_registered(self):
        self.assertEqual(main._EXCLUDED_KINDS, frozenset({"EXCLUDED"}))

    def test_excluded_kinds_disjoint_from_placeholder_kinds(self):
        self.assertEqual(main._EXCLUDED_KINDS & main._PLACEHOLDER_KINDS,
                         frozenset())


# ============================================================
# T2/T3: ledger 参照つき除外分岐・MF 副作用の遮断
# ============================================================

class ExcludedPageWriteBlockingTest(unittest.TestCase):
    """T3 DoD: 除外頁は MF 区へ書込回数ゼロ、監査タブへ 1 行。"""

    def test_audit_destination_page_writes_only_audit_row(self):
        fs, writer, ledger = _env()
        out = run_headless(writer, ledger, [excluded_page(1, 1)])

        self.assertIs(out.outcome, main.ProcessOutcome.PARTIAL)
        self.assertEqual(writer.append_calls, 0)        # commit_page 経由の書込ゼロ
        self.assertEqual(writer.build_page_write_calls, 0)  # 組立すらしない
        self.assertEqual(writer.placeholder_calls, [])  # append_entries もゼロ
        self.assertEqual(writer.sheets.get(_TAB, []), [])
        self.assertEqual(len(writer.audit_rows), 1)
        row = writer.audit_rows[0]
        self.assertEqual(row["verdict"], AUDIT_VERDICT_EXCLUDED)
        self.assertEqual(row["reason"], "envelope")
        self.assertEqual(row["page_num"], 1)
        self.assertEqual(row["ocr_text_len"], 42)

    def test_mf_destination_page_also_writes_nothing_to_mf(self):
        # 社会保険料通知書。UI 版は MF 提示行を書くが headless は書かない
        # （§4.3、硬冪等の器が無いうちは副作用そのものを起こさない）。
        fs, writer, ledger = _env()
        out = run_headless(writer, ledger, [excluded_page(
            1, 1, reason="social_insurance_notice",
            destination=EXCLUDE_DEST_MF_TAB)])

        self.assertIs(out.outcome, main.ProcessOutcome.PARTIAL)
        self.assertEqual(writer.append_calls, 0)
        self.assertEqual(writer.placeholder_calls, [])
        self.assertEqual(len(writer.audit_rows), 1)
        self.assertEqual(writer.audit_rows[0]["reason"], "social_insurance_notice")

    def test_multiple_excluded_results_merge_reasons_without_losing_any(self):
        """同頁に複数の除外 result（同一 destination）が来たときの留痕。

        destination 不一致は escalate だが、reason は分岐に影響しない診断
        文字列なので落とさず連結する——監査タブは可観測性設備であり、
        情報を捨てるより残すほうが人手の役に立つ。
        """
        fs, writer, ledger = _env()
        run_headless(writer, ledger, [
            excluded_page(1, 1, reason="envelope", ocr_text_len=10),
            excluded_page(1, 1, reason="pamphlet", ocr_text_len=77),
            excluded_page(1, 1, reason="envelope", ocr_text_len=5),
        ])

        self.assertEqual(len(writer.audit_rows), 1)  # 頁ごとに 1 行
        row = writer.audit_rows[0]
        self.assertEqual(row["reason"], "envelope,pamphlet")  # 出現順・去重済み
        self.assertEqual(row["ocr_text_len"], 77)  # 最も読めた result の値
        self.assertEqual(writer.append_calls, 0)

    def test_excluded_page_creates_no_page_doc(self):
        # page doc を作らない＝`ticket_count` による占位/真データの二分を
        # 揺らさない（B4 の身分自証を壊さない）。
        fs, writer, ledger = _env()
        run_headless(writer, ledger, [excluded_page(1, 1)])
        pid = derive_page_id(DEFAULT_BASE, 1)
        self.assertNotIn(("jobs", DEFAULT_BASE, "postings", pid), fs.store)

    def test_excluded_page_uses_anchor_url_not_uploaded_page_pdf(self):
        # 除外頁のために単ページ PDF を Drive へ上げるのは割に合わない
        fs, writer, ledger = _env()
        run_headless(writer, ledger,
                     [excluded_page(1, 2), _page(2, 2, "店A", 1000)])
        self.assertEqual(writer.audit_rows[0]["source_url"], "http://url#page=1")

    def test_check_page_is_called_for_excluded_page(self):
        """T2 DoD: 載せない（page doc を作らない）ことと参照しない（check_page を
        呼ばない）ことは別。呼ばないと過去輪の記帳事実を見落とす。"""
        fs, writer, ledger = _env()
        seen = []
        real_check = ledger.check_page
        ledger.check_page = lambda pid: (seen.append(pid), real_check(pid))[1]

        run_headless(writer, ledger, [excluded_page(1, 1)])
        self.assertEqual(seen, [derive_page_id(DEFAULT_BASE, 1)])

    def test_ledger_escalate_stops_the_file(self):
        fs, writer, ledger = _env()
        pid = derive_page_id(DEFAULT_BASE, 1)
        fs.store[("jobs", DEFAULT_BASE, "postings", pid)] = {
            "status": "PENDING", "sheet_tab": _TAB,
            "predicted_row_range": None, "row_fingerprint": "",
        }
        out = run_headless(writer, ledger, [excluded_page(1, 1)])

        self.assertIs(out.outcome, main.ProcessOutcome.ESCALATED)
        self.assertEqual(writer.append_calls, 0)
        self.assertEqual(writer.audit_rows, [])  # 判定不能なら留痕もしない

    def test_ledger_escalate_returns_named_reason(self):
        """T2 DoD: ESCALATE の reason 文字列を字面で固定する。

        この文字列は控制台 print にしか出ないので、誤改されても檔級終態は
        変わらず、行為テストでは捕まらない。頁級関数を直接叩いて釘を打つ。
        """
        fs, writer, ledger = _env()
        pid = derive_page_id(DEFAULT_BASE, 1)
        fs.store[("jobs", DEFAULT_BASE, "postings", pid)] = {
            "status": "PENDING", "sheet_tab": _TAB,
            "predicted_row_range": None, "row_fingerprint": "",
        }
        classified = main._handle_excluded_page(
            writer, ledger, _FakeResolver(), pid, EXCLUDE_DEST_AUDIT_TAB,
            "d.pdf", 1, 1, [excluded_page(1, 1)["result"]])
        self.assertEqual(classified, ("ESCALATE", "ledger_witness_ambiguous"))


class ExcludedDriftTest(unittest.TestCase):
    """T2 DoD: 分類 drift は黙って捨てず、帳簿の事実を優先しつつ監査へ残す。"""

    def test_posted_prior_keeps_posted_kind_and_records_drift(self):
        # 過去輪に仕訳を書いた頁が今輪 excluded → MF の仕訳行は消えないので
        # POSTED_PRIOR を維持する（零記帳と集計すると終態が嘘になる）。
        fs, writer, ledger = _env()
        _seed_confirmed_page(fs, 1, ticket_count=2)
        out = run_headless(writer, ledger, [excluded_page(1, 1)])

        self.assertIs(out.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(len(writer.audit_rows), 1)
        row = writer.audit_rows[0]
        self.assertEqual(row["verdict"], AUDIT_VERDICT_DRIFT)
        self.assertEqual(row["reason"], "drift:POSTED_PRIOR->EXCLUDED")
        self.assertEqual(writer.append_calls, 0)

    def test_placeholder_prior_is_overridden_by_current_exclusion(self):
        # 過去輪の占位行は「読めなかった」という警告であり記帳ではない。
        # 今輪のより正確な判定（excluded）で上書きしてよい。
        fs, writer, ledger = _env()
        _seed_confirmed_page(fs, 1, ticket_count=0)
        out = run_headless(writer, ledger, [excluded_page(1, 1)])

        self.assertIs(out.outcome, main.ProcessOutcome.PARTIAL)  # DEAD_LETTER ではない
        self.assertEqual(len(writer.audit_rows), 1)
        row = writer.audit_rows[0]
        self.assertEqual(row["verdict"], AUDIT_VERDICT_DRIFT)
        self.assertEqual(row["reason"], "drift:PLACEHOLDER_PRIOR->EXCLUDED")

    def test_multiple_drift_kinds_in_same_file_each_recorded_correctly(self):
        """同一ファイル内で drift の種類が混ざっても、行が互いに汚染しない。

        p1 は過去輪に記帳あり（POSTED_PRIOR 維持）、p2 は過去輪が占位
        （今輪判定で上書き）。檔級は「記帳が実在する」ので SUCCESS。
        """
        fs, writer, ledger = _env()
        _seed_confirmed_page(fs, 1, ticket_count=2)
        _seed_confirmed_page(fs, 2, ticket_count=0)
        out = run_headless(writer, ledger,
                           [excluded_page(1, 2), excluded_page(2, 2)])

        self.assertIs(out.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual([r["reason"] for r in writer.audit_rows],
                         ["drift:POSTED_PRIOR->EXCLUDED",
                          "drift:PLACEHOLDER_PRIOR->EXCLUDED"])
        self.assertEqual([r["page_num"] for r in writer.audit_rows], [1, 2])
        self.assertEqual(writer.append_calls, 0)

    def test_drift_row_carries_both_classifications(self):
        """突合材料として、過去輪と今輪の分類が**両方**読み取れること
        （「1行あること」だけでは人手が何と何の食い違いか判断できない）。"""
        fs, writer, ledger = _env()
        _seed_confirmed_page(fs, 1, ticket_count=0)
        run_headless(writer, ledger, [excluded_page(1, 1)])
        reason = writer.audit_rows[0]["reason"]
        self.assertTrue(reason.startswith("drift:"))
        prior, _, current = reason[len("drift:"):].partition("->")
        self.assertEqual(prior, "PLACEHOLDER_PRIOR")
        self.assertEqual(current, "EXCLUDED")


# ============================================================
# T4: 監査書込失敗の語義分離（headless は MF へ退避しない）
# ============================================================

class AuditWriteFailureTest(unittest.TestCase):
    def test_headless_audit_failure_escalates_without_mf_fallback(self):
        fs, writer, ledger = _env()
        writer.audit_error = RuntimeError("監査タブ 503")
        out = run_headless(writer, ledger, [excluded_page(1, 1)])

        self.assertIs(out.outcome, main.ProcessOutcome.ESCALATED)
        self.assertEqual(writer.append_calls, 0)
        # MF 退避なし（目標1 を障害時も守る）
        self.assertEqual(writer.placeholder_calls, [])

    def test_drift_audit_failure_escalates_without_downgrading(self):
        """drift 分岐（ledger SKIP）の監査書込失敗も ESCALATE。

        WRITE 分岐とは別の if 枝なので独立に固定する。ここで握り潰して
        `EXCLUDED`/`POSTED_PRIOR` を返すと、分類が食い違った事実が
        どこにも残らないまま終態が確定してしまう。
        """
        fs, writer, ledger = _env()
        _seed_confirmed_page(fs, 1, ticket_count=0)
        writer.audit_error = RuntimeError("監査タブ 503")
        out = run_headless(writer, ledger, [excluded_page(1, 1)])

        self.assertIs(out.outcome, main.ProcessOutcome.ESCALATED)
        self.assertEqual(writer.append_calls, 0)
        self.assertEqual(writer.placeholder_calls, [])
        self.assertEqual(writer.audit_rows, [])

    def test_headless_audit_failure_does_not_create_page_doc(self):
        fs, writer, ledger = _env()
        writer.audit_error = RuntimeError("監査タブ 503")
        run_headless(writer, ledger, [excluded_page(1, 1)])
        pid = derive_page_id(DEFAULT_BASE, 1)
        self.assertNotIn(("jobs", DEFAULT_BASE, "postings", pid), fs.store)

    def test_ui_path_still_falls_back_to_mf_row(self):
        """UI 版（ledger=None）は控制面が無く他に可視化先がないので現状維持
        ——監査失敗時は MF の赤い認識不能行へ退避する（挙動不変）。"""
        calls = {"append": [], "audit": 0}

        class _UiWriter:
            def append_entries(self, **kw):
                calls["append"].append(kw)

            def append_audit_row(self, **kw):
                calls["audit"] += 1
                raise RuntimeError("監査タブ 503")

        out = _run_ui(_UiWriter(), [excluded_page(1, 1)])

        self.assertIs(out, True)
        self.assertEqual(calls["audit"], 1)
        self.assertEqual(len(calls["append"]), 1)  # MF 退避行が書かれている

    def test_ui_path_writes_mf_notice_row_for_social_insurance(self):
        """T3 DoD: UI 版の社会保険料 MF 提示行は従来通り書かれる（headless
        だけが書かない）。IP-401 §3.8 の顧客可視性要件。"""
        appended = []

        class _UiWriter:
            def append_entries(self, **kw):
                appended.append(kw)

            def append_audit_row(self, **kw):
                raise AssertionError("mf_tab 行き先で監査タブは使わない")

        _run_ui(_UiWriter(), [excluded_page(
            1, 1, reason="social_insurance_notice",
            destination=EXCLUDE_DEST_MF_TAB)])

        self.assertEqual(len(appended), 1)
        placeholder = appended[0]["entries_data"]
        self.assertTrue(placeholder["_unrecognized"])
        self.assertEqual(placeholder["entries"], [])       # 金額を持ち込まない
        self.assertEqual(placeholder["date"], "")
        self.assertEqual(placeholder["vendor"], "d.pdf")   # 行だけ見て原票が分かる


# ============================================================
# T5: 檔級終態
# ============================================================

class ExcludedFileOutcomeTest(unittest.TestCase):
    def test_all_excluded_is_partial_and_not_reported(self):
        """全頁除外 → PARTIAL。POSTED とも死信とも偽らない（裁定2）。"""
        reporter = FakeReporter({DEFAULT_BASE: {"lease_epoch": 1}})
        fx = HeadlessRerunFixture(reporter=reporter)
        out = fx.run([excluded_page(1, 2), excluded_page(2, 2)], lease_epoch=1)

        self.assertIs(out.outcome, main.ProcessOutcome.PARTIAL)
        self.assertEqual(reporter.report_posted_calls, [])
        self.assertEqual(reporter.report_dead_letter_calls, [])
        self.assertEqual(fx.writer.append_calls, 0)
        self.assertEqual(len(fx.writer.audit_rows), 2)

    def test_excluded_plus_posted_is_success(self):
        """最頻ケース（仕訳頁＋封筒頁）。永久非終端にしない（目標3）。"""
        fs, writer, ledger = _env()
        out = run_headless(writer, ledger,
                           [excluded_page(1, 2), _page(2, 2, "店A", 1000)])

        self.assertIs(out.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(len(writer.sheets[_TAB]), 1)  # 記帳は 1 行のみ
        self.assertEqual(len(writer.audit_rows), 1)

    def test_posted_plus_excluded_as_last_page_is_success(self):
        """除外頁が**末頁**のケース（P1、Round 2 で発覚した穴）。

        `_process_file_headless` は「頁境界での flush」と「ループ終了後の
        末尾 flush」の 2 経路を持つ。既存の混在テストは全て除外を p1 に
        置いていたため、末尾 flush 経路の除外は一度も踏まれていなかった。
        """
        fs, writer, ledger = _env()
        out = run_headless(writer, ledger,
                           [_page(1, 2, "店A", 1000), excluded_page(2, 2)])

        self.assertIs(out.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(len(writer.sheets[_TAB]), 1)
        self.assertEqual(len(writer.audit_rows), 1)
        self.assertEqual(writer.audit_rows[0]["page_num"], 2)

    def test_all_excluded_as_last_page_after_placeholder_is_dead_letter(self):
        # 末尾 flush 経路の除外が終態計算から正しく除かれること（順序反転版）
        fs, writer, ledger = _env()
        out = run_headless(
            writer, ledger,
            [error_page(1, 2, error_class="CONTENT"), excluded_page(2, 2)])
        self.assertIs(out.outcome, main.ProcessOutcome.DEAD_LETTER)
        self.assertEqual(out.dead_letter_payload["message"],
                         "all_pages_unreadable: 1/2 pages [p1]")

    def test_excluded_plus_placeholder_is_dead_letter(self):
        """T5 DoD（2026-07-30 訂正）: 除外を除いた残りが占位だけなら、単頁
        不可読 PDF と意味的に同値＝DEAD_LETTER。封筒が1枚混じったせいで本物の
        不可読頁が PARTIAL（未回報・memo 永久）へ格下げされ、人手へ上がらない
        まま滞留する——という取りこぼしを防ぐ。"""
        fs, writer, ledger = _env()
        out = run_headless(
            writer, ledger,
            [excluded_page(1, 2), error_page(2, 2, error_class="CONTENT")])
        self.assertIs(out.outcome, main.ProcessOutcome.DEAD_LETTER)

    def test_excluded_plus_retryable_is_failed_retryable(self):
        fs, writer, ledger = _env()
        out = run_headless(
            writer, ledger,
            [excluded_page(1, 2), error_page(2, 2, error_class="RETRYABLE")])
        self.assertIs(out.outcome, main.ProcessOutcome.FAILED)
        self.assertTrue(out.retryable)

    def test_excluded_plus_unknown_is_failed_not_retryable(self):
        fs, writer, ledger = _env()
        out = run_headless(
            writer, ledger,
            [excluded_page(1, 2), error_page(2, 2, error_class="UNKNOWN")])
        self.assertIs(out.outcome, main.ProcessOutcome.FAILED)
        self.assertFalse(out.retryable)

    def test_dead_letter_page_list_excludes_excluded_pages(self):
        # 除外頁は「読めなかった頁」ではないので DEAD_LETTER の頁リストに
        # 混ぜない（分母は物理頁数のまま）。
        fs, writer, ledger = _env()
        out = run_headless(writer, ledger,
                           [excluded_page(1, 3),
                            error_page(2, 3, error_class="CONTENT"),
                            error_page(3, 3, error_class="CONTENT")])
        self.assertIs(out.outcome, main.ProcessOutcome.DEAD_LETTER)
        self.assertEqual(out.dead_letter_payload["message"],
                         "all_pages_unreadable: 2/3 pages [p2,p3]")

    def test_aggregate_is_pure_and_excluded_only_yields_partial(self):
        out = main._aggregate_file_outcome({1: "EXCLUDED", 2: "EXCLUDED"},
                                           [1, 2], 2)
        self.assertIs(out.outcome, main.ProcessOutcome.PARTIAL)

    def test_excluded_pages_are_neutral_when_other_pages_remain(self):
        """除外中立性（Codex 単条複審 #6）: 非除外頁が 1 頁以上残る限り、
        除外頁を足しても引いても檔級終態は変わらない。

        除外を母数から除く設計の意味はこれに尽きる——封筒が何枚混ざろうと、
        記帳すべき頁たちの評価を動かしてはならない。
        """
        base_cases = [
            ["POSTED_NOW"],
            ["POSTED_PRIOR"],
            ["PLACEHOLDER_WRITTEN"],
            ["POSTED_NOW", "PLACEHOLDER_WRITTEN"],
            ["RETRYABLE", "POSTED_NOW"],
            ["UNKNOWN", "PLACEHOLDER_PRIOR"],
        ]
        for accounted in base_cases:
            for extra in (["EXCLUDED"], ["EXCLUDED", "EXCLUDED"],
                          ["EXCLUDED", "EXCLUDED", "EXCLUDED"]):
                with self.subTest(accounted=accounted, extra=extra):
                    plain = {i + 1: k for i, k in enumerate(accounted)}
                    mixed = {i + 1: k for i, k in enumerate(accounted + extra)}
                    ref = main._aggregate_file_outcome(
                        plain, sorted(plain), len(plain))
                    got = main._aggregate_file_outcome(
                        mixed, sorted(mixed), len(mixed))
                    self.assertIs(got.outcome, ref.outcome)
                    self.assertEqual(got.retryable, ref.retryable)

    def test_aggregate_regression_without_excluded_kinds_unchanged(self):
        """除外を含まない既存ケースの終態は全て不変（回帰）。"""
        cases = [
            ({1: "POSTED_NOW"}, main.ProcessOutcome.SUCCESS),
            ({1: "POSTED_PRIOR"}, main.ProcessOutcome.SUCCESS),
            ({1: "PLACEHOLDER_WRITTEN"}, main.ProcessOutcome.DEAD_LETTER),
            ({1: "PLACEHOLDER_PRIOR"}, main.ProcessOutcome.DEAD_LETTER),
            ({1: "POSTED_NOW", 2: "PLACEHOLDER_WRITTEN"}, main.ProcessOutcome.PARTIAL),
            ({1: "RETRYABLE", 2: "POSTED_NOW"}, main.ProcessOutcome.FAILED),
            ({1: "UNKNOWN", 2: "POSTED_NOW"}, main.ProcessOutcome.FAILED),
        ]
        for kinds, expected in cases:
            with self.subTest(kinds=kinds):
                nums = sorted(kinds)
                out = main._aggregate_file_outcome(kinds, nums, len(nums))
                self.assertIs(out.outcome, expected)


# ============================================================
# T6: 分類 flip の振る舞い（5 種、二輪跑で実測）
# ============================================================

class ClassificationFlipTest(unittest.TestCase):
    """過去輪と今輪で判定が変わる 5 パターン。page doc を作らない設計が
    「残滞」を生まないことと、drift が黙って消えないことを固定する。"""

    def test_excluded_then_valid_posts_normally(self):
        # 除外輪は page doc を作らないので、今輪の有効票は普通に記帳される
        fs, writer, ledger = _env()
        run_headless(writer, ledger, [excluded_page(1, 1)])
        self.assertEqual(writer.append_calls, 0)

        ledger2 = make_ledger(fs, writer)
        out = run_headless(writer, ledger2, [_page(1, 1, "店A", 1000)])
        self.assertIs(out.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(len(writer.sheets[_TAB]), 1)

    def test_excluded_then_placeholder_takes_normal_placeholder_path(self):
        fs, writer, ledger = _env()
        run_headless(writer, ledger, [excluded_page(1, 1)])

        ledger2 = make_ledger(fs, writer)
        out = run_headless(writer, ledger2,
                           [error_page(1, 1, error_class="CONTENT")])
        self.assertIs(out.outcome, main.ProcessOutcome.DEAD_LETTER)
        self.assertEqual(len(writer.sheets[_TAB]), 1)  # 占位行 1 行
        pid = derive_page_id(DEFAULT_BASE, 1)
        doc = fs.store[("jobs", DEFAULT_BASE, "postings", pid)]
        self.assertEqual(doc["ticket_count"], 0)

    def test_placeholder_then_excluded_is_drift_and_partial(self):
        fs, writer, ledger = _env()
        out1 = run_headless(writer, ledger,
                            [error_page(1, 1, error_class="CONTENT")])
        self.assertIs(out1.outcome, main.ProcessOutcome.DEAD_LETTER)
        rows_after_1 = len(writer.sheets[_TAB])

        ledger2 = make_ledger(fs, writer)
        out2 = run_headless(writer, ledger2, [excluded_page(1, 1)])
        self.assertIs(out2.outcome, main.ProcessOutcome.PARTIAL)
        self.assertEqual(len(writer.sheets[_TAB]), rows_after_1)  # 増行なし
        self.assertEqual(writer.audit_rows[-1]["reason"],
                         "drift:PLACEHOLDER_PRIOR->EXCLUDED")

    def test_valid_then_excluded_keeps_posted_prior(self):
        fs, writer, ledger = _env()
        out1 = run_headless(writer, ledger, [_page(1, 1, "店A", 1000)])
        self.assertIs(out1.outcome, main.ProcessOutcome.SUCCESS)

        ledger2 = make_ledger(fs, writer)
        out2 = run_headless(writer, ledger2, [excluded_page(1, 1)])
        self.assertIs(out2.outcome, main.ProcessOutcome.SUCCESS)  # 記帳は実在する
        self.assertEqual(len(writer.sheets[_TAB]), 1)
        self.assertEqual(writer.audit_rows[-1]["reason"],
                         "drift:POSTED_PRIOR->EXCLUDED")

    def test_destination_flip_makes_no_difference_in_headless(self):
        # audit_tab → mf_tab へ行き先が変わっても headless では MF 書込ゼロ
        fs, writer, ledger = _env()
        run_headless(writer, ledger, [excluded_page(1, 1)])

        ledger2 = make_ledger(fs, writer)
        out = run_headless(writer, ledger2, [excluded_page(
            1, 1, reason="social_insurance_notice",
            destination=EXCLUDE_DEST_MF_TAB)])

        self.assertIs(out.outcome, main.ProcessOutcome.PARTIAL)
        self.assertEqual(writer.append_calls, 0)
        self.assertEqual(writer.placeholder_calls, [])
        self.assertEqual(len(writer.audit_rows), 2)
        self.assertEqual({r["verdict"] for r in writer.audit_rows},
                         {AUDIT_VERDICT_EXCLUDED})  # どちらも drift ではない


class KnownLimitationTest(unittest.TestCase):
    """本版が**意図的に受容している**欠陥を、名前を持った事実として釘で留める。

    Plan §10 のリスク表に「本版では受容」と明記されている既知の限界。
    直すには effect 記録による硬冪等（§9-1、headless 有効化の前置条件）が
    要るので、ここでは直さない。ただし無記述のまま放置すると、後から
    誰かが中途半端に「修正」したときに語義変化が誰にも気づかれない
    （去重だけ入れて原子性を入れない、等）。現状を可視化しておく。
    """

    def test_known_limitation_audit_row_duplicates_on_retry(self):
        # p1 除外（監査行 1 本）＋ p2 一時故障 → ファイル保持で再試行
        fs, writer, ledger = _env()
        out1 = run_headless(
            writer, ledger,
            [excluded_page(1, 2), error_page(2, 2, error_class="RETRYABLE")])
        self.assertIs(out1.outcome, main.ProcessOutcome.FAILED)
        self.assertTrue(out1.retryable)
        self.assertEqual(len(writer.audit_rows), 1)

        # 次輪（p2 は回復）→ p1 の監査行が**もう一度**追記される。
        # 除外頁は page doc を作らないので、監査行の重複を止める術が現状ない。
        ledger2 = make_ledger(fs, writer)
        out2 = run_headless(writer, ledger2,
                            [excluded_page(1, 2), _page(2, 2, "店A", 1000)])
        self.assertIs(out2.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(len(writer.audit_rows), 2)  # ← 受容済みの重複
        self.assertEqual([r["page_num"] for r in writer.audit_rows], [1, 1])
        # 帳簿（MF 区）は汚れていない——重複するのは可観測性設備の側だけ
        self.assertEqual(len(writer.sheets[_TAB]), 1)


if __name__ == "__main__":
    unittest.main()
