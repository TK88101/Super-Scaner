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


class HandleIntakeAlertedMemoTest(unittest.TestCase):
    """handle_intake の alerted（進程内快取）: 拒絶件記憶層（Firestore 無界重打
    の防止）。alerted は file_id → 拒絶 reason の進程内快取で、呼び出し方
    （main() 循環）がライフサイクルを持つ。"""

    def _make_calls(self):
        calls = {"get_job": 0, "alert": [], "move": []}

        def get_job(job_id):
            calls["get_job"] += 1
            return None

        def write_alert(alert_id, payload):
            calls["alert"].append((alert_id, payload))

        def move_to_quarantine():
            calls["move"].append(True)

        return calls, get_job, write_alert, move_to_quarantine

    def test_memo_hit_skips_check_intake_and_only_retries_move(self):
        file = {"id": "f1"}
        calls, get_job, write_alert, move_to_quarantine = self._make_calls()
        alerted = {"f1": "no_posting_id"}

        result = handle_intake(
            file, get_job=get_job, write_alert=write_alert,
            move_to_quarantine=move_to_quarantine, alerted=alerted,
        )

        self.assertFalse(result)
        self.assertEqual(0, calls["get_job"])
        self.assertEqual([], calls["alert"])
        self.assertEqual([True], calls["move"])

    def test_memo_hit_and_move_succeeds_removes_key_and_returns_false(self):
        file = {"id": "f1"}
        calls, get_job, write_alert, move_to_quarantine = self._make_calls()
        alerted = {"f1": "no_posting_id"}

        result = handle_intake(
            file, get_job=get_job, write_alert=write_alert,
            move_to_quarantine=move_to_quarantine, alerted=alerted,
        )

        self.assertFalse(result)
        self.assertNotIn("f1", alerted)

    def test_memo_hit_and_move_fails_keeps_key(self):
        file = {"id": "f1"}
        alerted = {"f1": "no_posting_id"}

        def get_job(job_id):
            raise AssertionError("get_job must not be called on memo hit")

        def write_alert(alert_id, payload):
            raise AssertionError("write_alert must not be called on memo hit")

        def move_to_quarantine():
            raise RuntimeError("move boom")

        result = handle_intake(
            file, get_job=get_job, write_alert=write_alert,
            move_to_quarantine=move_to_quarantine, alerted=alerted,
        )

        self.assertFalse(result)
        self.assertIn("f1", alerted)
        self.assertEqual("no_posting_id", alerted["f1"])

    def test_full_path_alert_success_move_failure_records_reason_in_memo(self):
        file = {"id": "f1"}  # no_posting_id
        store = FakeJobStore({})
        alerted: dict = {}

        def write_alert(alert_id, payload):
            pass

        def move_to_quarantine():
            raise RuntimeError("move boom")

        result = handle_intake(
            file, get_job=store.get_job, write_alert=write_alert,
            move_to_quarantine=move_to_quarantine, alerted=alerted,
        )

        self.assertFalse(result)
        self.assertEqual("no_posting_id", alerted.get("f1"))

    def test_full_path_alert_and_move_both_succeed_key_not_left_in_memo(self):
        file = {"id": "f1"}  # no_posting_id
        store = FakeJobStore({})
        alerted: dict = {}

        result = handle_intake(
            file, get_job=store.get_job, write_alert=lambda *a: None,
            move_to_quarantine=lambda: None, alerted=alerted,
        )

        self.assertFalse(result)
        self.assertNotIn("f1", alerted)

    def test_alert_failure_does_not_touch_memo(self):
        file = {"id": "f1"}  # no_posting_id
        store = FakeJobStore({})
        alerted: dict = {}

        def write_alert(alert_id, payload):
            raise RuntimeError("alert boom")

        result = handle_intake(
            file, get_job=store.get_job, write_alert=write_alert,
            move_to_quarantine=lambda: None, alerted=alerted,
        )

        self.assertFalse(result)
        self.assertEqual({}, alerted)

    def test_deferred_does_not_touch_memo(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "base-4"}}
        store = FakeJobStore(raise_exc=TimeoutError("boom"))
        alerted: dict = {}

        result = handle_intake(
            file, get_job=store.get_job, write_alert=lambda *a: None,
            move_to_quarantine=lambda: None, alerted=alerted,
        )

        self.assertFalse(result)
        self.assertEqual({}, alerted)


class HeadlessAccessorCallTimeEvalTest(unittest.TestCase):
    """config の headless 鍵は呼び出し時点で os.environ を評価すること（codex review
    P1、B2 裁決）。

    動機: main.py は `import config` の**後**に `load_dotenv()` を呼ぶ
    （main.py:22-27）。もし headless_mode()/quarantine_folder_id() 等が
    モジュール読込時に一度だけ評価される定数だったら、import 時点で
    まだ .env が読み込まれていない os.environ の値（＝大抵は未設定）に
    固化されてしまい、.env に HEADLESS_MODE=1 等を書いても headless が
    永遠に非活性化する。この単体テストはそれを再現する: import 済みの
    `config` モジュールに対し、import 後（このテスト実行時）に初めて
    os.environ を設定しても、accessor がその値を正しく読めることを示す
    ＝ load_dotenv が import の後で呼ばれても壊れないことの保証。
    """

    def test_headless_mode_reads_env_set_after_config_already_imported(self):
        with patch.dict(os.environ, {"HEADLESS_MODE": "1"}):
            self.assertTrue(config.headless_mode())

    def test_quarantine_folder_id_reads_env_set_after_config_already_imported(self):
        with patch.dict(os.environ, {"QUARANTINE_FOLDER_ID": "Q1"}):
            self.assertEqual("Q1", config.quarantine_folder_id())

    def test_handoff_folder_id_reads_env_set_after_config_already_imported(self):
        # B2＝鍵の予約のみ・未接線（folder_map への接線は後続批）だが、accessor
        # 自体は他の3関数と同じ呼び出し時点評価であること。
        with patch.dict(os.environ, {"HANDOFF_FOLDER_ID": "H1"}):
            self.assertEqual("H1", config.handoff_folder_id())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HANDOFF_FOLDER_ID", None)
            self.assertEqual("", config.handoff_folder_id())

    def test_firestore_project_id_reads_env_set_after_config_already_imported(self):
        # U14（真 Firestore 環境）未就緒のため鍵先行。build_reporter_from_env は
        # 本 accessor 経由でこの値を読む（単一真相源、B1 裁決反映）。
        with patch.dict(os.environ, {"FIRESTORE_PROJECT_ID": "proj-9"}):
            self.assertEqual("proj-9", config.firestore_project_id())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FIRESTORE_PROJECT_ID", None)
            self.assertEqual("", config.firestore_project_id())


class HeadlessModeConfigTest(unittest.TestCase):
    """config.headless_mode(): env 解析（IP-303 入口守衛の有効化フラグ）。"""

    def test_headless_mode_false_for_empty_string_or_zero(self):
        for value in ("", "0"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"HEADLESS_MODE": value}):
                    self.assertFalse(config.headless_mode())

    def test_headless_mode_false_when_env_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEADLESS_MODE", None)
            self.assertFalse(config.headless_mode())


class ValidateHeadlessConfigTest(unittest.TestCase):
    """config.validate_headless_config: HEADLESS_MODE 起動必須鍵の缺落検査
    （宣言側で一元管理、B3/B4 で鍵が増えたらここに足す）。"""

    def test_returns_missing_key_when_quarantine_folder_id_empty(self):
        with patch.dict(os.environ, {"QUARANTINE_FOLDER_ID": ""}):
            self.assertEqual(
                ["QUARANTINE_FOLDER_ID"], config.validate_headless_config())

    def test_returns_empty_list_when_quarantine_folder_id_set(self):
        with patch.dict(os.environ, {"QUARANTINE_FOLDER_ID": "Q_FOLDER"}):
            self.assertEqual([], config.validate_headless_config())


class InitHeadlessReporterTest(unittest.TestCase):
    """main._init_headless_reporter: HEADLESS_MODE 起動時の reporter 構築を
    main() 本体から抽出した単体（IP-303 接線ヘルパー抽出、可測化）。"""

    def test_returns_none_when_not_headless(self):
        # UI 版（HEADLESS_MODE 未設定）は reporter を作らず None のまま
        with patch.dict(os.environ, {"HEADLESS_MODE": "0"}):
            self.assertIsNone(main._init_headless_reporter())

    def test_exits_when_headless_without_quarantine_folder(self):
        # headless なのに隔離夾未設定 → 既存の print + exit(1)（fail fast）を保つ
        with patch.dict(os.environ, {"HEADLESS_MODE": "1", "QUARANTINE_FOLDER_ID": ""}), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                main._init_headless_reporter()

    def test_builds_reporter_from_env_when_headless_and_configured(self):
        sentinel_reporter = object()
        with patch.dict(os.environ, {"HEADLESS_MODE": "1", "QUARANTINE_FOLDER_ID": "Q_FOLDER"}), \
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

        with patch.dict(os.environ, {"HEADLESS_MODE": "0"}):
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

        with patch.dict(os.environ, {"HEADLESS_MODE": "1", "QUARANTINE_FOLDER_ID": "Q_FOLDER"}), \
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

        with patch.dict(os.environ, {"HEADLESS_MODE": "1", "QUARANTINE_FOLDER_ID": "Q_FOLDER"}):
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

        with patch.dict(os.environ, {"HEADLESS_MODE": "1", "QUARANTINE_FOLDER_ID": "Q_FOLDER"}), \
                redirect_stdout(io.StringIO()):
            main._headless_intake_gate(service, file, "INPUT_FOLDER", reporter)

        # no_posting_id は resolve_posting_id 段階で弾かれるため get_job 自体は
        # 呼ばれない。write_alert は reporter の実インスタンスに束縛されている
        # ことを、呼出し元 (=reporter.write_alert) が起動されたことで確認する。
        reporter.write_alert.assert_called_once()
        alert_id = reporter.write_alert.call_args.args[0]
        self.assertEqual("f2", alert_id)

    def test_alerted_memo_survives_across_calls_and_suppresses_second_write_alert(self):
        # move が初回失敗 → alerted に f1 が記憶される。同一 file を二回目に
        # 呼んだとき、write_alert は再実行されず（旁路失敗の無界重打を防止）、
        # move だけが再試行される。二回目の move が成功すれば memo から除去される。
        service = self._make_service()
        service.files.return_value.update.return_value.execute.side_effect = [
            RuntimeError("move boom"),
            {"id": "f1", "parents": ["Q_FOLDER"]},
        ]
        reporter = MagicMock()
        reporter.get_job.return_value = None
        file = {"id": "f1"}  # properties 無し → no_posting_id
        alerted: dict = {}

        with patch.dict(os.environ, {"HEADLESS_MODE": "1", "QUARANTINE_FOLDER_ID": "Q_FOLDER"}), \
                redirect_stdout(io.StringIO()):
            first = main._headless_intake_gate(
                service, file, "INPUT_FOLDER", reporter, alerted=alerted)
            self.assertFalse(first)
            self.assertIn("f1", alerted, "初回 move 失敗直後は memo に残るはず")

            second = main._headless_intake_gate(
                service, file, "INPUT_FOLDER", reporter, alerted=alerted)

        self.assertFalse(second)
        self.assertNotIn("f1", alerted, "二回目の move 成功で memo から除去されるはず")
        reporter.write_alert.assert_called_once()
        self.assertEqual(2, service.files.return_value.update.call_count)


if __name__ == "__main__":
    unittest.main()
