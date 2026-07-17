"""intake_guard.py の単体テスト（IP-302: base posting_id 受信 / IP-303: 監視夾入口守衛）。

サンデヴィスタン契約 job-state-machine.md v0.12 §3.2/§6 対齊：
    - base posting_id の載体＝Drive 公開 properties、key ＝ POSTING_ID_PROPERTY_KEY
      （契約 v0.10 定名の跨倉庫契約値。改名即破壊交棒）。
    - SS は無 posting_id / job 不一致件を拒絶＋隔離夾退避＋alerts/{file_id} 直書き
      （IP-303、契約 §3.2 交棒行）。

跑法:
    venv311/bin/python -m unittest test_intake_guard -v
"""
import importlib
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

# main.py はモジュール読込時に必須環境変数が無いと exit(1) する。
# 実 .env が無い環境(CI 等)でも import できるよう、未設定のものだけ補完する
# （test_drive_functions.py と同じ防御パターン。python-dotenv の load_dotenv は
#   既存 os.environ を上書きしないので、実 .env がある場合はそちらが優先される）。
os.environ.setdefault("PROCESSED_FOLDER_ID", "test_processed_folder")
os.environ.setdefault("SERVICE_ACCOUNT_FILE", "test_sa.json")
os.environ.setdefault("OUTPUT_SPREADSHEET_ID", "test_spreadsheet")
os.environ.setdefault("FOLDER_RECEIPT_ID", "test_receipt_folder")

import config

importlib.reload(config)  # 先に env 無しで import 済みでも値を反映させる
import main

from intake_guard import (
    POSTING_ID_PROPERTY_KEY,
    IntakeCheck,
    IntakeDecision,
    check_intake,
    handle_intake,
    resolve_posting_id,
)


class ResolvePostingIdTest(unittest.TestCase):
    """IP-302: Drive 公開 properties から base posting_id を読取る純関数。"""

    def test_returns_value_when_property_present(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "base-123"}}
        self.assertEqual("base-123", resolve_posting_id(file))

    def test_returns_none_when_properties_key_entirely_missing(self):
        file = {"id": "f1"}
        self.assertIsNone(resolve_posting_id(file))

    def test_returns_none_when_properties_present_but_key_absent(self):
        file = {"id": "f1", "properties": {"other_key": "x"}}
        self.assertIsNone(resolve_posting_id(file))

    def test_returns_none_for_empty_string(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: ""}}
        self.assertIsNone(resolve_posting_id(file))

    def test_returns_none_for_whitespace_only_string(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "   "}}
        self.assertIsNone(resolve_posting_id(file))

    def test_strips_surrounding_whitespace(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "  base-9  "}}
        self.assertEqual("base-9", resolve_posting_id(file))

    def test_returns_none_for_non_str_value(self):
        # 防御: properties は本来 str→str の Drive API 仕様だが、fake/破損データ対策
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: 12345}}
        self.assertIsNone(resolve_posting_id(file))

    def test_returns_none_when_properties_is_none(self):
        # 防御: Drive API が properties=None を返すケース（未設定文件で稀に発生）
        file = {"id": "f1", "properties": None}
        self.assertIsNone(resolve_posting_id(file))


class FakeJobStore:
    """get_job 呼出のダブル。job_id → job dict、無ければ None。例外注入も可。"""

    def __init__(self, jobs=None, raise_exc=None):
        self._jobs = jobs or {}
        self._raise_exc = raise_exc

    def get_job(self, job_id):
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._jobs.get(job_id)


class CheckIntakeTest(unittest.TestCase):
    """IP-303: check_intake の五分岐（PROCESS/no_posting_id/job_not_found/
    posting_id_mismatch/DEFERRED）。"""

    def test_process_when_posting_id_matches_job(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "base-1"}}
        store = FakeJobStore({"base-1": {"posting_id": "base-1"}})

        result = check_intake(file, store.get_job)

        self.assertEqual(
            IntakeCheck(IntakeDecision.PROCESS, "base-1", None), result)

    def test_rejected_no_posting_id_when_property_missing(self):
        file = {"id": "f1"}
        store = FakeJobStore({})

        result = check_intake(file, store.get_job)

        self.assertEqual(IntakeDecision.REJECTED, result.decision)
        self.assertEqual("no_posting_id", result.reason)
        self.assertIsNone(result.base)

    def test_rejected_job_not_found_when_get_job_returns_none(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "base-2"}}
        store = FakeJobStore({})  # base-2 不存在

        result = check_intake(file, store.get_job)

        self.assertEqual(IntakeDecision.REJECTED, result.decision)
        self.assertEqual("job_not_found", result.reason)
        self.assertEqual("base-2", result.base)

    def test_rejected_posting_id_mismatch_when_job_disagrees(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "base-3"}}
        store = FakeJobStore({"base-3": {"posting_id": "different-base"}})

        result = check_intake(file, store.get_job)

        self.assertEqual(IntakeDecision.REJECTED, result.decision)
        self.assertEqual("posting_id_mismatch", result.reason)
        self.assertEqual("base-3", result.base)

    def test_deferred_when_get_job_raises(self):
        # Firestore 瞬断: DEFERRED、reason に型名を含む（人間可読な最小診断情報）
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "base-4"}}
        store = FakeJobStore(raise_exc=ConnectionError("boom"))

        result = check_intake(file, store.get_job)

        self.assertEqual(IntakeDecision.DEFERRED, result.decision)
        self.assertEqual("firestore_error:ConnectionError", result.reason)
        self.assertEqual("base-4", result.base)


class HandleIntakeTest(unittest.TestCase):
    """handle_intake: 副作用の呼出順序・失敗隔離・白名単ペイロード検証。"""

    def _make_calls(self):
        calls = {"alert": [], "move": []}

        def write_alert(alert_id, payload):
            calls["alert"].append((alert_id, payload))

        def move_to_quarantine():
            calls["move"].append(True)

        return calls, write_alert, move_to_quarantine

    def test_process_decision_returns_true_with_zero_side_effects(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "base-1"}}
        store = FakeJobStore({"base-1": {"posting_id": "base-1"}})
        calls, write_alert, move_to_quarantine = self._make_calls()

        result = handle_intake(
            file, get_job=store.get_job,
            write_alert=write_alert, move_to_quarantine=move_to_quarantine,
        )

        self.assertTrue(result)
        self.assertEqual([], calls["alert"])
        self.assertEqual([], calls["move"])

    def test_deferred_decision_returns_false_with_zero_side_effects(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "base-4"}}
        store = FakeJobStore(raise_exc=TimeoutError("boom"))
        calls, write_alert, move_to_quarantine = self._make_calls()

        result = handle_intake(
            file, get_job=store.get_job,
            write_alert=write_alert, move_to_quarantine=move_to_quarantine,
        )

        self.assertFalse(result)
        self.assertEqual([], calls["alert"])
        self.assertEqual([], calls["move"])

    def test_rejected_calls_alert_before_move_and_returns_false(self):
        file = {"id": "f1"}  # no_posting_id
        store = FakeJobStore({})
        order = []

        def write_alert(alert_id, payload):
            order.append("alert")

        def move_to_quarantine():
            order.append("move")

        result = handle_intake(
            file, get_job=store.get_job,
            write_alert=write_alert, move_to_quarantine=move_to_quarantine,
        )

        self.assertFalse(result)
        self.assertEqual(["alert", "move"], order)

    def test_rejected_alert_failure_skips_move_and_returns_false(self):
        file = {"id": "f1"}  # no_posting_id
        store = FakeJobStore({})
        move_calls = []

        def write_alert(alert_id, payload):
            raise RuntimeError("alert boom")

        def move_to_quarantine():
            move_calls.append(True)

        result = handle_intake(
            file, get_job=store.get_job,
            write_alert=write_alert, move_to_quarantine=move_to_quarantine,
        )

        self.assertFalse(result)
        self.assertEqual([], move_calls)

    def test_rejected_move_failure_still_returns_false_alert_already_written(self):
        file = {"id": "f1"}  # no_posting_id
        store = FakeJobStore({})
        alert_calls = []

        def write_alert(alert_id, payload):
            alert_calls.append((alert_id, payload))

        def move_to_quarantine():
            raise RuntimeError("move boom")

        result = handle_intake(
            file, get_job=store.get_job,
            write_alert=write_alert, move_to_quarantine=move_to_quarantine,
        )

        self.assertFalse(result)
        self.assertEqual(1, len(alert_calls))

    def test_rejected_alert_payload_keys_are_whitelisted_no_posting_id(self):
        # no_posting_id: base は None なので posting_id キーは payload に入らない
        file = {"id": "f1", "name": "顧客の請求書.pdf"}
        store = FakeJobStore({})
        calls, write_alert, move_to_quarantine = self._make_calls()

        handle_intake(
            file, get_job=store.get_job,
            write_alert=write_alert, move_to_quarantine=move_to_quarantine,
        )

        alert_id, payload = calls["alert"][0]
        self.assertEqual("f1", alert_id)
        self.assertLessEqual(
            set(payload.keys()), {"kind", "file_id", "reason", "posting_id"})
        self.assertNotIn("name", payload)
        self.assertNotIn("顧客の請求書.pdf", str(payload))
        self.assertEqual("intake_rejected", payload["kind"])
        self.assertEqual("f1", payload["file_id"])
        self.assertEqual("no_posting_id", payload["reason"])
        self.assertNotIn("posting_id", payload)

    def test_rejected_alert_payload_includes_posting_id_when_base_present(self):
        # job_not_found: base は非 None なので posting_id キーが payload に入る
        file = {"id": "f2", "properties": {POSTING_ID_PROPERTY_KEY: "base-5"}}
        store = FakeJobStore({})
        calls, write_alert, move_to_quarantine = self._make_calls()

        handle_intake(
            file, get_job=store.get_job,
            write_alert=write_alert, move_to_quarantine=move_to_quarantine,
        )

        alert_id, payload = calls["alert"][0]
        self.assertEqual("f2", alert_id)
        self.assertEqual("job_not_found", payload["reason"])
        self.assertEqual("base-5", payload["posting_id"])
        self.assertLessEqual(
            set(payload.keys()), {"kind", "file_id", "reason", "posting_id"})


class HeadlessModeConfigTest(unittest.TestCase):
    """config.HEADLESS_MODE の env 解析（IP-303 入口守衛の有効化フラグ）。"""

    def tearDown(self):
        # 他テストへの汚染防止: 常に実環境で再 reload して復元
        importlib.reload(config)

    def test_headless_mode_true_when_env_is_one(self):
        with patch.dict(os.environ, {"HEADLESS_MODE": "1"}):
            importlib.reload(config)
            self.assertTrue(config.HEADLESS_MODE)

    def test_headless_mode_false_for_empty_string_or_zero(self):
        for value in ("", "0"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"HEADLESS_MODE": value}):
                    importlib.reload(config)
                    self.assertFalse(config.HEADLESS_MODE)


class ListFilesFieldsTest(unittest.TestCase):
    """main.list_files が properties を含む fields で Drive API を呼ぶこと。

    交棒契約の base posting_id は Drive 公開 properties に載る（v0.10 定名）。
    list の fields にこれを含めないと properties が一切返らず、入口守衛
    （check_intake/resolve_posting_id）が機能しない
    （Drive API は明示指定したフィールドしか返さない仕様のため）。
    """

    def test_fields_include_properties(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }

        main.list_files(service, "FOLDER_X")

        kwargs = service.files.return_value.list.call_args.kwargs
        self.assertIn("properties", kwargs.get("fields", ""))


class InitHeadlessReporterTest(unittest.TestCase):
    """main._init_headless_reporter: HEADLESS_MODE 起動時の reporter 構築を
    main() 本体から抽出した単体（IP-303 接線ヘルパー抽出、可測化）。"""

    def test_returns_none_when_not_headless(self):
        # UI 版（HEADLESS_MODE 未設定）は reporter を作らず None のまま
        with patch.object(config, "HEADLESS_MODE", False):
            self.assertIsNone(main._init_headless_reporter())

    def test_exits_when_headless_without_quarantine_folder(self):
        # headless なのに隔離夾未設定 → 既存の print + exit(1)（fail fast）を保つ
        with patch.object(config, "HEADLESS_MODE", True), \
                patch.object(config, "QUARANTINE_FOLDER_ID", ""), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                main._init_headless_reporter()

    def test_builds_reporter_from_env_when_headless_and_configured(self):
        sentinel_reporter = object()
        with patch.object(config, "HEADLESS_MODE", True), \
                patch.object(config, "QUARANTINE_FOLDER_ID", "Q_FOLDER"), \
                patch("main.firestore_report.build_reporter_from_env",
                      return_value=sentinel_reporter) as mock_build:
            result = main._init_headless_reporter()

        mock_build.assert_called_once_with()
        self.assertIs(result, sentinel_reporter)


class HeadlessIntakeGateTest(unittest.TestCase):
    """main._headless_intake_gate: 監視フォルダ入口守衛の main() 循環内接線を
    抽出した単体（IP-303 接線ヘルパー抽出）。

    functools.partial の引数順序守護：quarantine 隔離の move 先/元が入れ替わる
    実バグ（addParents と removeParents の取り違え）を、fake service への
    実際の呼出し引数で直接検証する（handle_intake 自体の単体テストは
    move_to_quarantine を差し替え済みのため、この取り違えを検知できない）。
    """

    def _make_service(self):
        service = MagicMock()
        service.files.return_value.update.return_value.execute.return_value = {
            "id": "F1", "parents": ["Q_FOLDER"],
        }
        return service

    def test_non_headless_returns_true_with_zero_side_effects(self):
        service = self._make_service()
        reporter = MagicMock()
        file = {"id": "f1"}

        with patch.object(config, "HEADLESS_MODE", False):
            result = main._headless_intake_gate(service, file, "INPUT_FOLDER", reporter)

        self.assertTrue(result)
        reporter.get_job.assert_not_called()
        reporter.write_alert.assert_not_called()
        service.files.return_value.update.assert_not_called()

    def test_headless_rejected_file_moves_to_quarantine_with_correct_folder_direction(self):
        # no_posting_id → REJECTED。addParents=隔離夾、removeParents=入力夾
        # であること（順序が逆転すると隔離夾から入力夾へ戻す誤動作になる）。
        service = self._make_service()
        reporter = MagicMock()
        reporter.get_job.return_value = None
        file = {"id": "f1"}  # properties 無し → no_posting_id

        with patch.object(config, "HEADLESS_MODE", True), \
                patch.object(config, "QUARANTINE_FOLDER_ID", "Q_FOLDER"), \
                redirect_stdout(io.StringIO()):
            result = main._headless_intake_gate(service, file, "INPUT_FOLDER", reporter)

        self.assertFalse(result)
        kwargs = service.files.return_value.update.call_args.kwargs
        self.assertEqual("Q_FOLDER", kwargs.get("addParents"))
        self.assertEqual("INPUT_FOLDER", kwargs.get("removeParents"))
        reporter.write_alert.assert_called_once()

    def test_headless_process_decision_returns_true_without_moving(self):
        service = self._make_service()
        reporter = MagicMock()
        reporter.get_job.return_value = {"posting_id": "base-1"}
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "base-1"}}

        with patch.object(config, "HEADLESS_MODE", True), \
                patch.object(config, "QUARANTINE_FOLDER_ID", "Q_FOLDER"):
            result = main._headless_intake_gate(service, file, "INPUT_FOLDER", reporter)

        self.assertTrue(result)
        service.files.return_value.update.assert_not_called()

    def test_get_job_and_write_alert_are_bound_to_reporter_methods(self):
        # get_job/write_alert が reporter インスタンスの対応メソッドへ束縛
        # されていること（別オブジェクトの同名メソッドにすり替わっていない）。
        service = self._make_service()
        reporter = MagicMock()
        reporter.get_job.return_value = None
        file = {"id": "f2"}  # no_posting_id → REJECTED（alert/move まで到達）

        with patch.object(config, "HEADLESS_MODE", True), \
                patch.object(config, "QUARANTINE_FOLDER_ID", "Q_FOLDER"), \
                redirect_stdout(io.StringIO()):
            main._headless_intake_gate(service, file, "INPUT_FOLDER", reporter)

        # no_posting_id は resolve_posting_id 段階で弾かれるため get_job 自体は
        # 呼ばれない。write_alert は reporter の実インスタンスに束縛されている
        # ことを、呼出し元 (=reporter.write_alert) が起動されたことで確認する。
        reporter.write_alert.assert_called_once()
        alert_id = reporter.write_alert.call_args.args[0]
        self.assertEqual("f2", alert_id)


if __name__ == "__main__":
    unittest.main()
