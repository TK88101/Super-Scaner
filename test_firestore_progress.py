"""B7-2 T2: FirestorePageOutcomesReporter（契約 §5.6 頁処理結果台帳）の単体テスト。

Plan: docs/plans/2026-08-01-b7-step2-headless-merge.md §3 T2。
kind→outcome 映射表（table-driven）・冪等上書・有限重試（頁時 1 回＋檔終局補写）・
closed reason 白名単を fake_firestore 上で検証する。真庫接続なし。
"""
import unittest
from datetime import datetime, timezone

from fake_firestore import FakeFirestore
from posting_ledger import derive_page_id

from firestore_progress import (
    KIND_OUTCOME_MAP,
    REASON_UNCLASSIFIED,
    FirestorePageOutcomesReporter,
)
from page_progress import (
    OUTCOME_EXCLUDED, OUTCOME_FAILED, OUTCOME_PLACEHOLDER, OUTCOME_POSTED,
)

BASE = "cust1:abc123"


def _make(fs=None):
    fs = fs or FakeFirestore()
    return fs, FirestorePageOutcomesReporter(fs, BASE)


def _doc(fs, page_num):
    page_id = derive_page_id(BASE, page_num)
    return fs.store.get(("jobs", BASE, "page_outcomes", page_id))


class KindOutcomeMappingTest(unittest.TestCase):
    """DoD①: 映射表全行の table-driven 検証（Plan §3 T2 の表と一字一句対斉）。"""

    CASES = [
        # (kind, detail, expected_outcome, expected_reason)
        ("POSTED_NOW", None, OUTCOME_POSTED, "posted_now"),
        ("POSTED_PRIOR", None, OUTCOME_POSTED, "prior_confirmed"),
        ("POSTED_PRIOR", "classification_drift", OUTCOME_POSTED,
         "classification_drift"),
        ("PLACEHOLDER_WRITTEN", None, OUTCOME_PLACEHOLDER,
         "placeholder_written"),
        ("PLACEHOLDER_WRITTEN", "content_unreadable", OUTCOME_PLACEHOLDER,
         "content_unreadable"),
        ("PLACEHOLDER_PRIOR", None, OUTCOME_PLACEHOLDER, "prior_confirmed"),
        ("PLACEHOLDER_PRIOR", "classification_drift", OUTCOME_PLACEHOLDER,
         "classification_drift"),
        ("EXCLUDED", "envelope", OUTCOME_EXCLUDED, "excluded_envelope"),
        ("EXCLUDED", "social_insurance_notice", OUTCOME_EXCLUDED,
         "excluded_social_insurance"),
        ("EXCLUDED", None, OUTCOME_EXCLUDED, "excluded"),
        ("RETRYABLE", None, OUTCOME_FAILED, "page_error_retryable"),
        ("UNKNOWN", None, OUTCOME_FAILED, "page_error_unknown"),
    ]

    def test_mapping_table(self):
        for kind, detail, want_outcome, want_reason in self.CASES:
            with self.subTest(kind=kind, detail=detail):
                fs, rep = _make()
                rep.record_page(3, kind, detail)
                doc = _doc(fs, 3)
                self.assertIsNotNone(doc)
                self.assertEqual(doc["page"], 3)
                self.assertEqual(doc["outcome"], want_outcome)
                self.assertEqual(doc["reason"], want_reason)

    def test_map_covers_exactly_the_headless_kind_set(self):
        # _classify_and_flush_page が返し得る全 kind（ESCALATE を除く）と
        # 一対一——漏れも余計もない（表が実装から漂流したら即赤）。
        self.assertEqual(
            set(KIND_OUTCOME_MAP),
            {"POSTED_NOW", "POSTED_PRIOR", "PLACEHOLDER_WRITTEN",
             "PLACEHOLDER_PRIOR", "EXCLUDED", "RETRYABLE", "UNKNOWN"})

    def test_unknown_kind_writes_nothing(self):
        # 未知 kind＝outcome を決められない→不写＋警告（静默 POSTED 化の禁止）。
        fs, rep = _make()
        rep.record_page(1, "ESCALATE", "ledger_witness_ambiguous")
        rep.record_page(2, "SOMETHING_NEW", None)
        self.assertIsNone(_doc(fs, 1))
        self.assertIsNone(_doc(fs, 2))


class ReasonWhitelistTest(unittest.TestCase):
    """DoD⑤: closed 白名単——自由文字（檔名/客户名混入し得る値）は不透過。"""

    def test_free_text_detail_degrades_to_unclassified(self):
        fs, rep = _make()
        rep.record_page(1, "EXCLUDED", "封筒っぽい 山田様 20260801.pdf")
        doc = _doc(fs, 1)
        self.assertEqual(doc["reason"], REASON_UNCLASSIFIED)

    def test_drift_prefixed_detail_degrades_to_unclassified(self):
        # _handle_excluded_page の "drift:POSTED_PRIOR->EXCLUDED" 形式の
        # 自由文字も closed キーへ落ちる（監査タブ側に原文が残る）。
        fs, rep = _make()
        rep.record_page(1, "POSTED_PRIOR", "drift:POSTED_PRIOR->EXCLUDED")
        self.assertEqual(_doc(fs, 1)["reason"], REASON_UNCLASSIFIED)


class IdempotenceTest(unittest.TestCase):
    """DoD②: 同一頁の再発射→doc 1 件のまま・written_at は最新（最後観察時刻）。"""

    def test_rewrite_same_page_keeps_single_doc_and_updates(self):
        fs, rep = _make()
        rep.record_page(1, "RETRYABLE", None)
        first = _doc(fs, 1)
        rep.record_page(1, "POSTED_NOW", None)
        second = _doc(fs, 1)
        docs = [p for p in fs.store
                if p[:3] == ("jobs", BASE, "page_outcomes")]
        self.assertEqual(len(docs), 1)
        self.assertEqual(second["outcome"], OUTCOME_POSTED)
        self.assertGreaterEqual(second["written_at"], first["written_at"])


class WrittenAtUtcTest(unittest.TestCase):
    """DoD④: written_at は UTC aware。"""

    def test_written_at_is_utc_aware(self):
        fs, rep = _make()
        rep.record_page(1, "POSTED_NOW", None)
        ts = _doc(fs, 1)["written_at"]
        self.assertIsInstance(ts, datetime)
        self.assertEqual(ts.utcoffset(), timezone.utc.utcoffset(None))


class DegradeAndFlushTest(unittest.TestCase):
    """DoD③: 頁時書込例外→伝播せず→檔終局 flush_pending 補写で回収。
    補写も失敗なら放行（戻り値で未回収数を報告、例外は出さない）。"""

    def test_transient_failure_recovered_by_flush(self):
        fs = FakeFirestore()
        fails = {"n": 1}

        def hook(path):
            if fails["n"] > 0:
                fails["n"] -= 1
                raise RuntimeError("transient")
        fs.set_hook = hook
        rep = FirestorePageOutcomesReporter(fs, BASE)
        rep.record_page(1, "POSTED_NOW", None)   # 失敗→pending（伝播しない）
        self.assertIsNone(_doc(fs, 1))
        remaining = rep.flush_pending()          # 補写一輪で回収
        self.assertEqual(remaining, 0)
        self.assertEqual(_doc(fs, 1)["outcome"], OUTCOME_POSTED)

    def test_persistent_failure_never_raises_and_reports_remaining(self):
        fs = FakeFirestore()

        def hook(path):
            raise RuntimeError("permanent")
        fs.set_hook = hook
        rep = FirestorePageOutcomesReporter(fs, BASE)
        rep.record_page(1, "POSTED_NOW", None)
        rep.record_page(2, "EXCLUDED", "envelope")
        remaining = rep.flush_pending()
        self.assertEqual(remaining, 2)
        self.assertIsNone(_doc(fs, 1))
        self.assertIsNone(_doc(fs, 2))

    def test_pending_rewrite_keeps_latest_settlement(self):
        # 失敗中に同頁が再決算されたら pending も最新値へ置換される。
        fs = FakeFirestore()
        fails = {"n": 2}

        def hook(path):
            if fails["n"] > 0:
                fails["n"] -= 1
                raise RuntimeError("transient")
        fs.set_hook = hook
        rep = FirestorePageOutcomesReporter(fs, BASE)
        rep.record_page(1, "RETRYABLE", None)
        rep.record_page(1, "POSTED_NOW", None)
        self.assertEqual(rep.flush_pending(), 0)
        self.assertEqual(_doc(fs, 1)["outcome"], OUTCOME_POSTED)


class PageIdReuseTest(unittest.TestCase):
    """R2: 採番は posting_ledger.derive_page_id と同一（別採番禁止）。"""

    def test_doc_id_equals_derive_page_id(self):
        fs, rep = _make()
        rep.record_page(7, "POSTED_NOW", None)
        want = derive_page_id(BASE, 7)   # "{base}:p7"
        self.assertIn(("jobs", BASE, "page_outcomes", want), fs.store)


if __name__ == "__main__":
    unittest.main()
