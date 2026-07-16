"""IP-301 firestore_report.FirestoreReporter の単体テスト。

SS 側 Firestore 回報客戶端（サンデヴィスタン contract F01②/F27/F49/§3.2 対齊）。
自建輕量 fake（FakeFirestoreClient / FakeDocRef / FakeSnapshot / FakeTransaction）——
不 import サンデヴィスタン倉庫代碼、SS 自包含。事務注入採
transaction_runner=lambda body: body(fake_txn) 繞過真實 SDK transactional 裝飾器。

跑法:
    venv311/bin/python -m unittest test_firestore_report -v
"""
import os
import sys
import unittest
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from firestore_report import (
    ACTOR_SUPER_SCANER,
    STATE_DEAD_LETTER,
    STATE_POSTED,
    STATE_POSTING_IN_PROGRESS,
    FirestoreReporter,
    ReportOutcome,
    ReportResult,
)


class FakeSnapshot:
    """doc_ref.get() の戻り値ダブル（Firestore DocumentSnapshot 鴨子型）。"""

    def __init__(self, exists, data=None):
        self.exists = exists
        self._data = data

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDocRef:
    """collection().document() の戻り値ダブル。store は呼出元と共有する dict。"""

    def __init__(self, doc_id, store):
        self.id = doc_id
        self._store = store

    def get(self, transaction=None):
        data = self._store.get(self.id)
        return FakeSnapshot(exists=data is not None, data=data)

    def set(self, data):
        # 非事務直寫（write_alert 等の事務外パス）
        self._store[self.id] = dict(data)


class FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return FakeDocRef(doc_id, self._store)


class FakeTransaction:
    """transaction.set() の呼出回数を計數（事務内書込＝job 文檔変更の証跡）。"""

    def __init__(self):
        self.set_call_count = 0

    def get(self, doc_ref):
        return doc_ref.get()

    def set(self, doc_ref, data):
        doc_ref.set(data)
        self.set_call_count += 1


class FakeFirestoreClient:
    """google.cloud.firestore.Client 鴨子型 fake。jobs/alerts 二 collection を保持。"""

    def __init__(self):
        self.jobs_store = {}
        self.alerts_store = {}

    def collection(self, name):
        if name == "jobs":
            return FakeCollection(self.jobs_store)
        if name == "alerts":
            return FakeCollection(self.alerts_store)
        raise ValueError(f"未知 collection: {name}")


class CountingTransactionRunner:
    """transaction_runner 注入用ラッパー。事務「実行」回数を計數
    （契約：REJECTED 後も本模組は恰一次事務実行、リトライしない）。
    """

    def __init__(self, transaction):
        self._transaction = transaction
        self.run_count = 0

    def __call__(self, body):
        self.run_count += 1
        return body(self._transaction)


def _make_reporter():
    client = FakeFirestoreClient()
    txn = FakeTransaction()
    runner = CountingTransactionRunner(txn)
    reporter = FirestoreReporter(client, transaction_runner=runner)
    return reporter, client, txn, runner


def _seed_job(client, job_id, **fields):
    base = {
        "current_state": STATE_POSTING_IN_PROGRESS,
        "lease_epoch": 1,
        "source_file_id": "file-abc",
        "attempt_count": 3,
        "state_history": [],
    }
    base.update(fields)
    client.jobs_store[job_id] = base
    return base


class ReportPostedTest(unittest.TestCase):
    def test_posted_normal_flow_applies_and_writes_history(self):
        # Arrange
        reporter, client, txn, runner = _make_reporter()
        _seed_job(client, "job-1", current_state=STATE_POSTING_IN_PROGRESS, lease_epoch=1)

        # Act
        result = reporter.report_posted("job-1", lease_epoch=1)

        # Assert
        self.assertEqual(result, ReportResult(ReportOutcome.APPLIED, "job-1"))
        doc = client.jobs_store["job-1"]
        self.assertEqual(doc["current_state"], STATE_POSTED)
        self.assertEqual(doc["attempt_count"], 0)
        last_history = doc["state_history"][-1]
        self.assertEqual(set(last_history.keys()), {"state", "at", "by"})
        self.assertEqual(last_history["state"], STATE_POSTED)
        self.assertEqual(last_history["by"], ACTOR_SUPER_SCANER)
        self.assertEqual(txn.set_call_count, 1)
        self.assertEqual(runner.run_count, 1)
        self.assertEqual(len(client.alerts_store), 0)

    def test_already_posted_is_idempotent_no_write_no_alert(self):
        # DoD①: 目標態已達成 → ALREADY_DONE、零寫入、零 alert
        # Arrange
        reporter, client, txn, runner = _make_reporter()
        _seed_job(client, "job-2", current_state=STATE_POSTED, lease_epoch=1)

        # Act
        result = reporter.report_posted("job-2", lease_epoch=1)

        # Assert
        self.assertEqual(result.outcome, ReportOutcome.ALREADY_DONE)
        self.assertEqual(txn.set_call_count, 0)
        self.assertEqual(len(client.alerts_store), 0)
        self.assertEqual(runner.run_count, 1)

    def test_stale_lease_epoch_rejected_job_unchanged_alert_written_once(self):
        # DoD②: 過期 epoch → REJECTED、job 文檔不變、alert 落 source_file_id、事務恰執行 1 次
        # Arrange
        reporter, client, txn, runner = _make_reporter()
        original = _seed_job(
            client, "job-3", current_state=STATE_POSTING_IN_PROGRESS,
            lease_epoch=2, source_file_id="file-xyz",
        )
        before = dict(original)

        # Act
        result = reporter.report_posted("job-3", lease_epoch=1)

        # Assert
        self.assertEqual(result.outcome, ReportOutcome.REJECTED)
        self.assertEqual(result.reason, "stale_lease_epoch")
        self.assertEqual(client.jobs_store["job-3"], before)
        self.assertEqual(txn.set_call_count, 0)
        self.assertIn("file-xyz", client.alerts_store)
        self.assertEqual(len(client.alerts_store), 1)
        self.assertEqual(runner.run_count, 1)

    def test_unexpected_state_rejected_with_alert(self):
        # Arrange: job 現態為 ROUTED（非 POSTING_IN_PROGRESS）
        reporter, client, txn, runner = _make_reporter()
        _seed_job(client, "job-4", current_state="ROUTED", lease_epoch=1, source_file_id="file-4")

        # Act
        result = reporter.report_posted("job-4", lease_epoch=1)

        # Assert
        self.assertEqual(result.outcome, ReportOutcome.REJECTED)
        self.assertEqual(result.reason, "unexpected_state")
        self.assertEqual(txn.set_call_count, 0)
        self.assertIn("file-4", client.alerts_store)

    def test_unexpected_state_without_source_file_id_falls_back_to_job_id(self):
        # Arrange: job 文檔缺 source_file_id 字段 → alert doc ID 退用 job_id
        reporter, client, txn, runner = _make_reporter()
        job = _seed_job(client, "job-5", current_state="ROUTED", lease_epoch=1)
        del job["source_file_id"]

        # Act
        result = reporter.report_posted("job-5", lease_epoch=1)

        # Assert
        self.assertEqual(result.outcome, ReportOutcome.REJECTED)
        self.assertIn("job-5", client.alerts_store)

    def test_job_not_found_rejected_alert_doc_id_is_job_id(self):
        # Arrange: 空 store，job 不存在
        reporter, client, txn, runner = _make_reporter()

        # Act
        result = reporter.report_posted("missing-job", lease_epoch=1)

        # Assert
        self.assertEqual(result.outcome, ReportOutcome.REJECTED)
        self.assertEqual(result.reason, "job_not_found")
        self.assertIn("missing-job", client.alerts_store)
        self.assertEqual(txn.set_call_count, 0)
        self.assertEqual(runner.run_count, 1)

    def test_missing_lease_epoch_field_defaults_to_zero(self):
        # 邊界: job 文檔缺 lease_epoch 字段 → 按預設 0 處理
        reporter, client, txn, runner = _make_reporter()
        job = _seed_job(client, "job-6", current_state=STATE_POSTING_IN_PROGRESS)
        del job["lease_epoch"]

        # Act
        result = reporter.report_posted("job-6", lease_epoch=0)

        # Assert
        self.assertEqual(result.outcome, ReportOutcome.APPLIED)


class ReportDeadLetterTest(unittest.TestCase):
    def test_dead_letter_normal_flow(self):
        # Arrange
        reporter, client, txn, runner = _make_reporter()
        _seed_job(client, "job-7", current_state=STATE_POSTING_IN_PROGRESS, lease_epoch=1)
        error = {
            "stage": "POST",
            "error_class": "NON_RETRYABLE",
            "message": "PDF は壊れているか暗号化されています",
        }

        # Act
        result = reporter.report_dead_letter("job-7", lease_epoch=1, error=error)

        # Assert
        self.assertEqual(result.outcome, ReportOutcome.APPLIED)
        doc = client.jobs_store["job-7"]
        self.assertEqual(doc["current_state"], STATE_DEAD_LETTER)
        self.assertEqual(doc["error_class"], "NON_RETRYABLE")
        last_error = doc["last_error"]
        self.assertEqual(last_error["stage"], "POST")
        self.assertEqual(last_error["error_class"], "NON_RETRYABLE")
        self.assertEqual(last_error["message"], error["message"])
        self.assertIn("at", last_error)

    def test_dead_letter_invalid_error_class_raises_value_error_zero_writes(self):
        # 邊界: RETRYABLE / UNKNOWN / 缺鍵(含空 dict) 一律 ValueError、零寫入
        reporter, client, txn, runner = _make_reporter()
        _seed_job(client, "job-8", current_state=STATE_POSTING_IN_PROGRESS, lease_epoch=1)
        bad_errors = [
            {"stage": "POST", "error_class": "RETRYABLE", "message": "m"},
            {"stage": "POST", "error_class": "UNKNOWN", "message": "m"},
            {"stage": "POST", "error_class": "GARBAGE", "message": "m"},  # 未知値
            {"stage": "POST", "error_class": "NON_RETRYABLE"},  # message 缺
            {},  # 空 dict
        ]
        for bad_error in bad_errors:
            with self.subTest(bad_error=bad_error):
                # Act / Assert
                with self.assertRaises(ValueError):
                    reporter.report_dead_letter("job-8", lease_epoch=1, error=bad_error)
                self.assertEqual(txn.set_call_count, 0)
                self.assertEqual(len(client.alerts_store), 0)


class UtcTimestampTest(unittest.TestCase):
    def test_all_written_timestamps_are_utc(self):
        # DoD③: updated_at / state_history[].at / last_error.at / alert.at 全部 tzinfo==UTC
        # Arrange
        reporter, client, txn, runner = _make_reporter()
        _seed_job(client, "job-9", current_state=STATE_POSTING_IN_PROGRESS, lease_epoch=1)
        error = {"stage": "POST", "error_class": "NON_RETRYABLE", "message": "m"}

        # Act: dead_letter 正常流（覆蓋 updated_at/state_history/last_error 三處）
        reporter.report_dead_letter("job-9", lease_epoch=1, error=error)
        # Act: 再造一個 REJECTED 場景以覆蓋 alert.at
        _seed_job(client, "job-10", current_state=STATE_POSTING_IN_PROGRESS, lease_epoch=5)
        reporter.report_posted("job-10", lease_epoch=1)

        # Assert
        doc = client.jobs_store["job-9"]
        self.assertEqual(doc["updated_at"].tzinfo, UTC)
        self.assertEqual(doc["state_history"][-1]["at"].tzinfo, UTC)
        self.assertEqual(doc["last_error"]["at"].tzinfo, UTC)
        alert_id = next(iter(client.alerts_store))
        self.assertEqual(client.alerts_store[alert_id]["at"].tzinfo, UTC)


class WriteAlertTest(unittest.TestCase):
    def test_write_alert_idempotent_overwrite_and_payload_not_mutated(self):
        # Arrange
        reporter, client, txn, runner = _make_reporter()
        payload = {"reason": "unexpected_state"}
        original_payload_copy = dict(payload)

        # Act: 同 alert_id 兩次寫入
        reporter.write_alert("alert-1", payload)
        first_at = client.alerts_store["alert-1"]["at"]
        reporter.write_alert("alert-1", payload)
        second_at = client.alerts_store["alert-1"]["at"]

        # Assert: 恰一文檔（覆蓋語意）、呼叫方 payload 未被就地修改
        self.assertEqual(len(client.alerts_store), 1)
        self.assertEqual(payload, original_payload_copy)
        self.assertEqual(client.alerts_store["alert-1"]["reason"], "unexpected_state")
        self.assertEqual(client.alerts_store["alert-1"]["by"], ACTOR_SUPER_SCANER)
        self.assertIn("at", client.alerts_store["alert-1"])
        # 覆蓋語意：兩次寫入都成功執行（第二次的 at 也應存在且為 datetime）
        self.assertIsInstance(first_at, datetime)
        self.assertIsInstance(second_at, datetime)

    def test_write_alert_empty_payload(self):
        # 邊界: alert payload 為空 dict
        reporter, client, txn, runner = _make_reporter()

        # Act
        reporter.write_alert("alert-2", {})

        # Assert
        stored = client.alerts_store["alert-2"]
        self.assertEqual(set(stored.keys()), {"at", "by"})
        self.assertEqual(stored["by"], ACTOR_SUPER_SCANER)


if __name__ == "__main__":
    unittest.main()
