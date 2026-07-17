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
from datetime import UTC, datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import firestore_report
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


class _ExplodingCollection:
    """set() が必ず失敗する alerts collection ダブル（旁路失敗の隔離検証用）。"""

    def document(self, doc_id):
        return _ExplodingDocRef()


class _ExplodingDocRef:
    def set(self, data):
        raise RuntimeError("boom")


class ExplodingAlertClient(FakeFirestoreClient):
    """alerts への書込だけ必ず失敗する fake client。jobs 側は正常動作。"""

    def collection(self, name):
        if name == "alerts":
            return _ExplodingCollection()
        return super().collection(name)


class _ExplodingGetDocRef:
    """get() が必ず失敗する doc ref ダブル（get_job の例外伝播検証用）。"""

    def get(self, transaction=None):
        raise ConnectionError("boom")


class _ExplodingJobsCollection:
    def document(self, doc_id):
        return _ExplodingGetDocRef()


class ExplodingJobsClient(FakeFirestoreClient):
    """jobs への読取だけ必ず失敗する fake client（get_job 例外伝播検証用）。"""

    def collection(self, name):
        if name == "jobs":
            return _ExplodingJobsCollection()
        return super().collection(name)


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
        # 事務內觀測值須隨 alert 帶出（裁決線索自包含）
        alert = client.alerts_store["file-xyz"]
        self.assertEqual(alert["observed_epoch"], 2)
        self.assertEqual(alert["expected_epoch"], 1)
        self.assertTrue(result.alert_delivered)

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
        self.assertEqual(client.alerts_store["file-4"]["observed_state"], "ROUTED")

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

    def test_alert_write_failure_does_not_mask_rejected_result(self):
        # Altitude review 採納: alert 旁路失敗不得綁架已定案的裁決結果——
        # 不拋例外、以 alert_delivered=False 旗標回傳（防呼叫方誤判「回報失敗」而無限重跑）
        client = ExplodingAlertClient()
        txn = FakeTransaction()
        runner = CountingTransactionRunner(txn)
        reporter = FirestoreReporter(client, transaction_runner=runner)
        _seed_job(client, "job-13", current_state="ROUTED", lease_epoch=1, source_file_id="file-13")

        result = reporter.report_posted("job-13", lease_epoch=1)

        self.assertEqual(result.outcome, ReportOutcome.REJECTED)
        self.assertEqual(result.reason, "unexpected_state")
        self.assertFalse(result.alert_delivered)
        self.assertEqual(txn.set_call_count, 0)
        self.assertEqual(runner.run_count, 1)

    def test_report_alert_extra_key_collision_raises(self):
        # 程序錯誤守衛: alert_extra 與 alert 核心欄位鍵衝突 → ValueError（fail fast）
        reporter, client, txn, runner = _make_reporter()
        _seed_job(client, "job-12", current_state="ROUTED", lease_epoch=1, source_file_id="file-12")
        with self.assertRaises(ValueError):
            reporter._report(
                "job-12", STATE_POSTED, lease_epoch=1,
                patch={}, alert_extra={"reason": "shadow"},
            )

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

    def test_dead_letter_rejected_alert_carries_attempted_error(self):
        # Altitude review 補測: dead_letter 被拒（過期 epoch）時，SS 判死的業務理由
        # （attempted_error）必須進 alert，供控制面/人工裁決；job 文檔不變。
        # Arrange
        reporter, client, txn, runner = _make_reporter()
        original = _seed_job(
            client, "job-11", current_state=STATE_POSTING_IN_PROGRESS,
            lease_epoch=2, source_file_id="file-11",
        )
        before = dict(original)
        error = {
            "stage": "POST",
            "error_class": "NON_RETRYABLE",
            "message": "PDF は壊れているか暗号化されています",
        }

        # Act
        result = reporter.report_dead_letter("job-11", lease_epoch=1, error=error)

        # Assert
        self.assertEqual(result.outcome, ReportOutcome.REJECTED)
        self.assertEqual(result.reason, "stale_lease_epoch")
        self.assertEqual(client.jobs_store["job-11"], before)
        self.assertEqual(txn.set_call_count, 0)
        alert = client.alerts_store["file-11"]
        self.assertEqual(alert["kind"], "transition_rejected")
        self.assertEqual(alert["attempted_error"]["error_class"], "NON_RETRYABLE")
        self.assertEqual(alert["attempted_error"]["stage"], "POST")
        self.assertEqual(alert["attempted_error"]["message"], error["message"])
        self.assertEqual(runner.run_count, 1)

    def test_dead_letter_invalid_error_class_raises_value_error_zero_writes(self):
        # 邊界: RETRYABLE / UNKNOWN / 缺鍵(含空 dict) 一律 ValueError、零寫入
        reporter, client, txn, runner = _make_reporter()
        _seed_job(client, "job-8", current_state=STATE_POSTING_IN_PROGRESS, lease_epoch=1)
        bad_errors = [
            {"stage": "POST", "error_class": "RETRYABLE", "message": "m"},
            {"stage": "POST", "error_class": "UNKNOWN", "message": "m"},
            {"stage": "POST", "error_class": "GARBAGE", "message": "m"},  # 未知値
            {"stage": "POST", "error_class": "NON_RETRYABLE"},  # message 缺
            {"stage": "POST", "error_class": "NON_RETRYABLE", "message": 123},  # message 非 str
            {},  # 空 dict
        ]
        for bad_error in bad_errors:
            with self.subTest(bad_error=bad_error):
                # Act / Assert
                with self.assertRaises(ValueError):
                    reporter.report_dead_letter("job-8", lease_epoch=1, error=bad_error)
                self.assertEqual(txn.set_call_count, 0)
                self.assertEqual(len(client.alerts_store), 0)


    def test_dead_letter_message_truncated_to_cap(self):
        # 邊界: 超長自由文本硬性截斷（跨倉共享 collection 的最後防線）
        reporter, client, txn, runner = _make_reporter()
        _seed_job(client, "job-14", current_state=STATE_POSTING_IN_PROGRESS, lease_epoch=1)
        error = {"stage": "POST", "error_class": "NON_RETRYABLE", "message": "x" * 5000}

        result = reporter.report_dead_letter("job-14", lease_epoch=1, error=error)

        self.assertEqual(result.outcome, ReportOutcome.APPLIED)
        stored = client.jobs_store["job-14"]["last_error"]["message"]
        self.assertEqual(len(stored), 1000)  # 含後綴恰等於上限
        self.assertTrue(stored.endswith("…[截斷]"))


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


class GetJobTest(unittest.TestCase):
    """IP-303: get_job（intake_guard.check_intake が呼ぶ單發非事務讀）。"""

    def test_returns_dict_when_job_exists(self):
        reporter, client, txn, runner = _make_reporter()
        _seed_job(client, "job-x", current_state=STATE_POSTING_IN_PROGRESS,
                   lease_epoch=1, source_file_id="file-x")

        result = reporter.get_job("job-x")

        self.assertEqual(STATE_POSTING_IN_PROGRESS, result["current_state"])
        self.assertEqual("file-x", result["source_file_id"])

    def test_returns_none_when_job_does_not_exist(self):
        reporter, client, txn, runner = _make_reporter()

        self.assertIsNone(reporter.get_job("missing-job"))

    def test_propagates_exception_from_client(self):
        # SDK 例外は握り潰さず伝播（呼出方 intake_guard.check_intake が
        # DEFERRED へ倒す判断をする）
        reporter = FirestoreReporter(ExplodingJobsClient())

        with self.assertRaises(ConnectionError):
            reporter.get_job("job-y")


class BuildReporterFromEnvTest(unittest.TestCase):
    """IP-303: build_reporter_from_env（HEADLESS_MODE 起動時のモジュール級工廠）。

    実 SDK 呼出し（firestore.Client / service_account.Credentials）は
    patch で差し替え、環境変数の有無による分岐のみを検証する。
    """

    def test_builds_client_with_credentials_when_service_account_file_exists(self):
        with patch.dict(
            os.environ,
            {"SERVICE_ACCOUNT_FILE": "sa.json", "FIRESTORE_PROJECT_ID": "proj-1"},
        ), patch("firestore_report.os.path.exists", return_value=True), \
                patch("firestore_report.service_account.Credentials"
                      ".from_service_account_file") as mock_creds, \
                patch("firestore_report.firestore.Client") as mock_client:
            reporter = firestore_report.build_reporter_from_env()

        mock_creds.assert_called_once_with("sa.json")
        mock_client.assert_called_once_with(
            credentials=mock_creds.return_value, project="proj-1")
        self.assertIsInstance(reporter, FirestoreReporter)

    def test_builds_client_without_credentials_when_file_missing(self):
        with patch.dict(
            os.environ, {"SERVICE_ACCOUNT_FILE": "sa.json", "FIRESTORE_PROJECT_ID": ""},
        ), patch("firestore_report.os.path.exists", return_value=False), \
                patch("firestore_report.firestore.Client") as mock_client:
            firestore_report.build_reporter_from_env()

        mock_client.assert_called_once_with()

    def test_propagates_exception_from_client_construction(self):
        with patch.dict(os.environ, {"SERVICE_ACCOUNT_FILE": "", "FIRESTORE_PROJECT_ID": ""}), \
                patch("firestore_report.firestore.Client", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                firestore_report.build_reporter_from_env()


if __name__ == "__main__":
    unittest.main()
