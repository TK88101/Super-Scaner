"""IP-304 頁級 posting 台賬の単体テスト（T1、fake Firestore＋fake probe/commit）。

真庫不接・純ロジック検証。契約 §5.1/§5.2/§5.5＋定稿 Plan §9（03-b3-detail-plan.md）
の四情況判定・三步 post_page・witness 厳格照合・claim 不可覆盖を固定する。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest import mock

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
    # 契約 v0.16 §5.5 対斉（S1/S2）: page_id は持たない・page_num→page・
    # written_at は updated_at と同一値で双写。
    base = {
        "page": 3,
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
        "written_at": datetime(2026, 7, 21, tzinfo=UTC),
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
        self.assertEqual(doc["page"], 3)
        self.assertNotIn("page_num", doc)
        self.assertNotIn("page_id", doc)
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


class ConfirmedTicketCountTest(unittest.TestCase):
    """B4 Plan §2.2/T2: 只読 accessor `confirmed_ticket_count`（三態、三謂詞簽名は不変）。

    >0＝真に入賬済み頁、==0＝占位頁（占位行のみ、票なし）、None＝CONFIRMED 記録
    が無い（doc 不存在／status != CONFIRMED）。main（T3）が check_page の SKIP
    後にこの値で頁の身分を自証する。
    """

    def _probe_const(self, result):
        return lambda tab, rng, fp: result

    def test_returns_ticket_count_when_confirmed_with_tickets(self) -> None:
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc(status=pl.STATUS_CONFIRMED, ticket_count=2)
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        self.assertEqual(ledger.confirmed_ticket_count(PAGE_ID), 2)

    def test_returns_zero_when_confirmed_placeholder_page(self) -> None:
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc(status=pl.STATUS_CONFIRMED, ticket_count=0)
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        self.assertEqual(ledger.confirmed_ticket_count(PAGE_ID), 0)

    def test_returns_none_when_no_record(self) -> None:
        fs = _FakeFirestore()
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        self.assertIsNone(ledger.confirmed_ticket_count(PAGE_ID))

    def test_returns_none_when_pending_not_confirmed(self) -> None:
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc(status=pl.STATUS_PENDING)
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        self.assertIsNone(ledger.confirmed_ticket_count(PAGE_ID))

    def test_returns_none_when_ticket_count_field_missing_or_malformed(self) -> None:
        # 旧 schema／破損データ防御: ticket_count が int でなければ None（釘死しない）
        fs = _FakeFirestore()
        doc = _pending_doc(status=pl.STATUS_CONFIRMED)
        del doc["ticket_count"]
        fs.store[POSTING_PATH] = doc
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        self.assertIsNone(ledger.confirmed_ticket_count(PAGE_ID))

    def test_does_not_mutate_state_pure_read(self) -> None:
        # 三謂詞（derive_page_id/check_page/post_page）とは独立した単発読取り
        # ——呼出しても check_page の判定に副作用を与えない
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc(status=pl.STATUS_CONFIRMED, ticket_count=3)
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        ledger.confirmed_ticket_count(PAGE_ID)
        self.assertIs(ledger.check_page(PAGE_ID), PageDecision.SKIP)
        self.assertEqual(fs.store[POSTING_PATH]["status"], pl.STATUS_CONFIRMED)

    def test_reuses_check_page_snapshot_no_second_read(self) -> None:
        # simcodex Round 1 #9: check_page が読んだ直後の同一 page_id への
        # confirmed_ticket_count はキャッシュを再利用し、Firestore を再度叩かない。
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc(status=pl.STATUS_CONFIRMED, ticket_count=2)
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        with mock.patch.object(ledger, "_posting_doc",
                               wraps=ledger._posting_doc) as spy:
            self.assertIs(ledger.check_page(PAGE_ID), PageDecision.SKIP)
            self.assertEqual(spy.call_count, 1)
            self.assertEqual(ledger.confirmed_ticket_count(PAGE_ID), 2)
            self.assertEqual(spy.call_count, 1)  # 二回目は増えない（キャッシュ命中）

    def test_different_page_id_is_not_cache_hit(self) -> None:
        # キャッシュは page_id 単位——別頁への呼出しは普通に読みに行く
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc(status=pl.STATUS_CONFIRMED, ticket_count=2)
        other_path = ("jobs", JOB_KEY, "postings", "cust:hash:p9")
        fs.store[other_path] = _pending_doc(
            page=9, status=pl.STATUS_CONFIRMED, ticket_count=9)
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        ledger.check_page(PAGE_ID)
        self.assertEqual(ledger.confirmed_ticket_count("cust:hash:p9"), 9)

    def test_post_page_invalidates_cache_so_later_read_sees_fresh_data(self) -> None:
        # simcodex Round 1 #9: claim/confirm（post_page）はキャッシュを無効化する
        # ——書込前に WRITE 判定でキャッシュされた「無記録」が書込後まで生き残り
        # confirmed_ticket_count が古い None を返す事故を防ぐ。
        fs = _FakeFirestore()
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        self.assertIs(ledger.check_page(PAGE_ID), PageDecision.WRITE)  # (PAGE_ID, None) を快取
        ledger.post_page(PAGE_ID, _summary(ticket_count=5), lambda: (1, 1))
        self.assertEqual(ledger.confirmed_ticket_count(PAGE_ID), 5)  # 快取無効化→最新値


class WrittenAtDoubleWriteTest(unittest.TestCase):
    """S2: written_at 双写・防漂移三律（①同一 now ②同一 txn.set 内 ③恒等）。

    PENDING 直後・CONFIRMED 直後の両態で written_at == updated_at が恒成立する
    ことを断言する。witness PRESENT 補記経路（require_pending=True、
    _recover_pending 経由）でも同一 _confirm を通るため同様に成立する。
    """

    def _probe_const(self, result):
        return lambda tab, rng, fp: result

    def test_pending_written_at_equals_updated_at(self) -> None:
        fs = _FakeFirestore()
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        captured = {}

        def commit():
            captured["pending_doc"] = dict(fs.store[POSTING_PATH])
            return (5, 5)

        ledger.post_page(PAGE_ID, _summary(), commit)

        pending_doc = captured["pending_doc"]
        self.assertEqual(pending_doc["written_at"], pending_doc["updated_at"])
        self.assertIs(pending_doc["written_at"].tzinfo, UTC)

    def test_confirmed_written_at_equals_updated_at(self) -> None:
        fs = _FakeFirestore()
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        ledger.post_page(PAGE_ID, _summary(), lambda: (5, 5))

        doc = fs.store[POSTING_PATH]
        self.assertEqual(doc["written_at"], doc["updated_at"])
        # confirm 時の written_at は claim 時のものから前進している
        # （②同一 txn.set 内で updated_at と一緒に更新される）
        self.assertNotEqual(doc["written_at"], doc["created_at"])

    def test_witness_present_recovery_confirm_also_sets_written_at(self) -> None:
        # _recover_pending の PRESENT 分岐（require_pending=True）経由の
        # _confirm 呼出しでも written_at == updated_at が成立する。
        fs = _FakeFirestore()
        fs.store[POSTING_PATH] = _pending_doc()
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.PRESENT))

        self.assertIs(ledger.check_page(PAGE_ID), PageDecision.SKIP)

        doc = fs.store[POSTING_PATH]
        self.assertEqual(doc["status"], pl.STATUS_CONFIRMED)
        self.assertEqual(doc["written_at"], doc["updated_at"])
        self.assertIs(doc["written_at"].tzinfo, UTC)


# 契約 v0.16 §5.5 字段表（S3、Codex #6 採納で状態別 schema テストへ格上げ）。
# リテラル列挙——posting_ledger 側の定数を再利用すると「実装が変われば
# テストも自動で追従してしまう」ため、契約の字面をここに固定して漂移を検知する。
_CONTRACT_FIELDS_V16 = frozenset(
    {"page", "ticket_count", "status", "sheet_row_range", "written_at", "tickets"}
)


class ContractSchemaV16Test(unittest.TestCase):
    """S3: 契約 §5.5 状態別 schema テスト（PENDING/CONFIRMED、Codex #6 採納）。

    T1/T2 適用前のコード（"page_num"/"page_id" キー・written_at 無し）に
    当てると本クラスは RED になる（歯の証明、T3 DoD）。
    """

    def _probe_const(self, result):
        return lambda tab, rng, fp: result

    def _assert_common_contract_fields(self, doc, summary) -> None:
        # 契約字段集 ⊆ doc.keys()
        self.assertTrue(_CONTRACT_FIELDS_V16.issubset(doc.keys()))
        # page == summary.page_num（int）
        self.assertEqual(doc["page"], summary.page_num)
        self.assertIsInstance(doc["page"], int)
        # ticket_count == len(tickets)
        self.assertEqual(doc["ticket_count"], len(doc["tickets"]))
        # written_at == updated_at（UTC aware datetime）
        self.assertEqual(doc["written_at"], doc["updated_at"])
        self.assertIsInstance(doc["written_at"], datetime)
        self.assertIs(doc["written_at"].tzinfo, UTC)
        # 旧形再発の否定対照（S1）
        self.assertNotIn("page_num", doc)
        self.assertNotIn("page_id", doc)

    def test_pending_doc_matches_contract_schema(self) -> None:
        fs = _FakeFirestore()
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        summary = _summary()
        captured = {}

        def commit():
            captured["pending_doc"] = dict(fs.store[POSTING_PATH])
            return (10, 12)

        ledger.post_page(PAGE_ID, summary, commit)

        doc = captured["pending_doc"]
        self._assert_common_contract_fields(doc, summary)
        # PENDING: sheet_row_range は None
        self.assertIsNone(doc["sheet_row_range"])

    def test_confirmed_doc_matches_contract_schema(self) -> None:
        fs = _FakeFirestore()
        ledger = _make_ledger(fs, self._probe_const(ProbeResult.ABSENT))
        summary = _summary()

        ledger.post_page(PAGE_ID, summary, lambda: (10, 12))

        doc = fs.store[POSTING_PATH]
        self._assert_common_contract_fields(doc, summary)
        # CONFIRMED: sheet_row_range == [start, end]・正整数・start <= end
        row_range = doc["sheet_row_range"]
        start, end = row_range
        self.assertIsInstance(start, int)
        self.assertIsInstance(end, int)
        self.assertGreater(start, 0)
        self.assertGreaterEqual(end, start)
        self.assertEqual([start, end], [10, 12])


if __name__ == "__main__":
    unittest.main()
