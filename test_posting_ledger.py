"""IP-304 頁級 posting 台賬の単体テスト（T1、fake Firestore＋fake probe/commit）。

真庫不接・純ロジック検証。契約 §5.1/§5.2/§5.5＋定稿 Plan §9（03-b3-detail-plan.md）
の四情況判定・三步 post_page・witness 厳格照合・claim 不可覆盖を固定する。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import posting_ledger as pl
from posting_ledger import (
    PageDecision,
    PagePostingSummary,
    PostingLedger,
    ProbeResult,
    TicketSummary,
    derive_page_id,
)


# fake Firestore は共有モジュール（headless_rerun_fixture と単一ソース、Reuse 統合）
from fake_firestore import FakeFirestore as _FakeFirestore


JOB_KEY = "cust:hash"
PAGE_ID = "cust:hash:p3"
POSTING_PATH = ("jobs", JOB_KEY, "postings", PAGE_ID)


def _make_ledger(fs: _FakeFirestore, probe) -> PostingLedger:
    return PostingLedger(
        fs, JOB_KEY, sheet_probe=probe, transaction_runner=fs.runner()
    )


def _pending_doc(**overrides) -> dict:
    base = {
        "page_id": PAGE_ID,
        "page_num": 3,
        "status": pl.STATUS_PENDING,
        "ticket_count": 1,
        "row_count": 2,
        "tickets": [{"date": "2026-07-01", "amount": 1100, "vendor": "A社"}],
        "predicted_row_range": [10, 11],
        "row_fingerprint": "fp-abc",
        "sheet_tab": "田中_領収書",
        "sheet_row_range": None,
        "created_at": datetime(2026, 7, 21, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 21, tzinfo=UTC),
        "schema_version": 1,
    }
    base.update(overrides)
    return base


def _summary(**overrides) -> PagePostingSummary:
    base = {
        "page_num": 3,
        "ticket_count": 1,
        "row_count": 2,
        "tickets": (TicketSummary(date="2026-07-01", amount=1100, vendor="A社"),),
        "predicted_row_range": (10, 11),
        "row_fingerprint": "fp-abc",
        "sheet_tab": "田中_領収書",
    }
    base.update(overrides)
    return PagePostingSummary(**base)


class DerivePageIdTest(unittest.TestCase):
    def test_format_pinned(self) -> None:
        self.assertEqual(derive_page_id("cust:hash", 3), "cust:hash:p3")

    def test_page_zero(self) -> None:
        self.assertEqual(derive_page_id("b", 0), "b:p0")


class CheckPageTest(unittest.TestCase):
    def _probe_const(self, result):
        return lambda tab, rng, fp: result

    def test_no_record_returns_write(self) -> None:
        fs = _FakeFirestore()
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        self.assertIs(ledger.check_page(PAGE_ID), PageDecision.WRITE)

    def test_confirmed_returns_skip(self) -> None:
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc(status=pl.STATUS_CONFIRMED)
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        self.assertIs(ledger.check_page(PAGE_ID), PageDecision.SKIP)

    def test_pending_probe_present_confirms_and_skips(self) -> None:
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc()
        seen = {}

        def probe(tab, rng, fp):
            seen["args"] = (tab, rng, fp)
            return ProbeResult.PRESENT

        ledger = _make_ledger(fs, probe)
        self.assertIs(ledger.check_page(PAGE_ID), PageDecision.SKIP)
        # probe は witness（tab/範囲/指紋）で呼ばれる
        self.assertEqual(seen["args"][0], "田中_領収書")
        self.assertEqual(tuple(seen["args"][1]), (10, 11))
        self.assertEqual(seen["args"][2], "fp-abc")
        # 内部補 CONFIRMED＋sheet_row_range＝predicted が Firestore に書かれる
        doc = fs.store[POSTING_PATH]
        self.assertEqual(doc["status"], pl.STATUS_CONFIRMED)
        self.assertEqual(list(doc["sheet_row_range"]), [10, 11])
        self.assertIs(doc["updated_at"].tzinfo, UTC)

    def test_pending_probe_absent_returns_write(self) -> None:
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc()
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        self.assertIs(ledger.check_page(PAGE_ID), PageDecision.WRITE)
        # ABSENT では CONFIRMED へ遷移させない（PENDING 残置、重写待ち）
        self.assertEqual(fs.store[POSTING_PATH]["status"], pl.STATUS_PENDING)

    def test_pending_probe_escalate_returns_escalate(self) -> None:
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc()
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ESCALATE))
        self.assertIs(ledger.check_page(PAGE_ID), PageDecision.ESCALATE)

    def test_pending_probe_raises_returns_escalate(self) -> None:
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc()

        def probe(tab, rng, fp):
            raise RuntimeError("sheets 読取例外")

        ledger = _make_ledger(fs, probe)
        self.assertIs(ledger.check_page(PAGE_ID), PageDecision.ESCALATE)

    def test_pending_missing_witness_returns_escalate(self) -> None:
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc(row_fingerprint="", predicted_row_range=None)
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.PRESENT))
        self.assertIs(ledger.check_page(PAGE_ID), PageDecision.ESCALATE)

    def test_unknown_status_returns_escalate(self) -> None:
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc(status="WEIRD")
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        self.assertIs(ledger.check_page(PAGE_ID), PageDecision.ESCALATE)


class PostPageTest(unittest.TestCase):
    def _probe_const(self, result):
        return lambda tab, rng, fp: result

    def test_three_step_order_and_confirmed_fields(self) -> None:
        fs = _FakeFirestore()
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        observed = {}

        def commit():
            # commit 時点で PENDING が既に落ちていること（②の前に①）
            snap = fs.store.get(POSTING_PATH)
            observed["status_at_commit"] = snap["status"] if snap else None
            observed["range_at_commit"] = snap["sheet_row_range"] if snap else "MISSING"
            return (20, 21)

        ledger.post_page(PAGE_ID, _summary(), commit)

        self.assertEqual(observed["status_at_commit"], pl.STATUS_PENDING)
        self.assertIsNone(observed["range_at_commit"])
        doc = fs.store[POSTING_PATH]
        self.assertEqual(doc["status"], pl.STATUS_CONFIRMED)
        self.assertEqual(list(doc["sheet_row_range"]), [20, 21])
        self.assertEqual(doc["ticket_count"], 1)
        self.assertEqual(doc["row_count"], 2)
        self.assertEqual(doc["tickets"], [{"date": "2026-07-01", "amount": 1100, "vendor": "A社"}])
        self.assertEqual(doc["page_num"], 3)
        self.assertEqual(doc["schema_version"], 1)
        self.assertIs(doc["created_at"].tzinfo, UTC)
        self.assertIs(doc["updated_at"].tzinfo, UTC)

    def test_commit_raises_leaves_pending(self) -> None:
        fs = _FakeFirestore()
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))

        def commit():
            raise RuntimeError("append_rows 失敗")

        with self.assertRaises(RuntimeError):
            ledger.post_page(PAGE_ID, _summary(), commit)
        # CONFIRMED を書かず PENDING 残置（重跑で witness 恢復）
        doc = fs.store[POSTING_PATH]
        self.assertEqual(doc["status"], pl.STATUS_PENDING)
        self.assertIsNone(doc["sheet_row_range"])

    def test_rewrites_existing_pending_preserves_created_at(self) -> None:
        # witness ABSENT 後の重写：既存 PENDING を同 claim 内で更新、created_at 保持
        fs = _FakeFirestore()
        original_created = datetime(2026, 7, 20, tzinfo=UTC)
        fs.store[POSTING_PATH] = _pending_doc(created_at=original_created)
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))

        ledger.post_page(PAGE_ID, _summary(), lambda: (30, 31))
        doc = fs.store[POSTING_PATH]
        self.assertEqual(doc["status"], pl.STATUS_CONFIRMED)
        self.assertEqual(doc["created_at"], original_created)
        self.assertEqual(list(doc["sheet_row_range"]), [30, 31])

    def test_refuses_overwrite_confirmed(self) -> None:
        # #5：CONFIRMED を PENDING へ無条件リセットしない
        fs = _FakeFirestore()
        confirmed = _pending_doc(status=pl.STATUS_CONFIRMED, sheet_row_range=[10, 11])
        fs.store[POSTING_PATH] = confirmed
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        commit_called = {"n": 0}

        def commit():
            commit_called["n"] += 1
            return (99, 99)

        with self.assertRaises(pl.LedgerStateError):
            ledger.post_page(PAGE_ID, _summary(), commit)
        self.assertEqual(commit_called["n"], 0)  # claim 段で止まり commit へ進まない
        self.assertEqual(fs.store[POSTING_PATH]["status"], pl.STATUS_CONFIRMED)


if __name__ == "__main__":
    unittest.main()
