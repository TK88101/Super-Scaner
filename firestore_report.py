"""IP-301 Firestore 回報模組（SS 側回報客戶端）。

背景：控制面（サンデヴィスタン）以 Firestore 為單一權威狀態源，Super Scaner (SS) 是被動
回報執行器——本模組負責把 SS 完成記帳（POSTED）或判定為死信（DEAD_LETTER）的結果回報進
`jobs/{job_id}` 文檔，並在被拒（REJECTED）時把裁決線索寫入 `alerts/{alert_id}` 供控制面
/人工判斷。

讀-校-寫事務語意（含幂等判定 / lease_epoch 校驗）刻意對齊サンデヴィスタン
`src/sandevistan/jobstore/firestore_store.py` 的 `transition()`（僅供語義參考，本模組不
import 該倉庫代碼、SS 自包含、獨立實現）。

契約要點（F## 對應サンデヴィスタン contract，方便 review 對賬）：
    - F01②：目標態已達成 → 冪等成功、靜默忽略（零寫入、零 alert）。
    - F27：跨階段流轉時 attempt_count 歸零（本模組每次回報恰為一次跨階段流轉，恆歸零）。
    - F49：所有時戳一律以 UTC 儲存。
    - §3.2：SS→DEAD_LETTER 僅限 NON_RETRYABLE（票面損壞/加密/空白等不可重試錯誤）。

REJECTED 語意（重要）：本模組每次呼叫恰執行一次 Firestore 事務、絕不自行重試寫賬——
契約 F01② 規定「被拒」的裁決權在控制面，呼叫方（main.py 等）同樣不得因收到 REJECTED
就自行重試同一次回報；重試與否是控制面的決策，不是本模組或呼叫方的職責。

邊界（Non-goals，故意不處理，理由如下）：
    - `lease_epoch` 參數型別不做執行期防禦：型別提示已聲明為 int，若呼叫方誤傳非 int
      （契約假設不會發生），`actual_epoch != lease_epoch` 比較多半直接不等而導致
      REJECTED("stale_lease_epoch")，不會靜默通過、也不會腐蝕已存資料，故不需要額外
      的 isinstance 防禦。
    - 不做任何重試/退避——「REJECTED 後不重試」是契約要求本身，不是待補的功能。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, TypeVar

from google.cloud import firestore

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# --- 常量（契約權威值，勿臆造） -------------------------------------------------
STATE_POSTING_IN_PROGRESS = "POSTING_IN_PROGRESS"
STATE_POSTED = "POSTED"
STATE_DEAD_LETTER = "DEAD_LETTER"
ACTOR_SUPER_SCANER = "super_scaner"
ERROR_CLASSES = {"RETRYABLE", "NON_RETRYABLE", "UNKNOWN"}

# report_dead_letter(error=...) 必含三鍵（契約 §3.2 事故排障最小欄位集）
_DEAD_LETTER_ERROR_REQUIRED_KEYS = frozenset({"stage", "error_class", "message"})

# state_history 條目白名單制：僅此三鍵，不得夾帶客戶名/金額等敏感字段
_STATE_HISTORY_KEYS = ("state", "at", "by")


class ReportOutcome(Enum):
    """_report 三種終局結果（契約 F01②語意：APPLIED / ALREADY_DONE 皆為「成功」，
    只有 REJECTED 需要控制面裁決）。"""

    APPLIED = "APPLIED"
    ALREADY_DONE = "ALREADY_DONE"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ReportResult:
    """report_posted / report_dead_letter 的回傳值（不可變）。"""

    outcome: ReportOutcome
    job_id: str
    reason: str | None = None


@dataclass(frozen=True)
class _TransactionOutcome:
    """事務 body 的內部回傳型別：把裁決結果與寫 alert 所需線索（source_file_id）
    一起帶出事務邊界（alert 依契約在事務外寫，見 _report）。"""

    result: ReportResult
    source_file_id: str | None = None


def _utcnow() -> datetime:
    """模組蓋章時戳（F49：一律 UTC）。不信任呼叫方傳入的時戳。"""
    return datetime.now(UTC)


def _validate_dead_letter_error(error: Mapping[str, Any]) -> None:
    """report_dead_letter 的 error 參數校驗（fail fast，零寫入）。

    契約 §3.2：SS→DEAD_LETTER 僅限 NON_RETRYABLE（損壞/加密/空白等不可重試錯誤）；
    RETRYABLE/UNKNOWN 一律視為呼叫方誤用，直接 ValueError，不寫任何 Firestore 文檔。
    """
    missing = _DEAD_LETTER_ERROR_REQUIRED_KEYS - set(error.keys())
    if missing:
        raise ValueError(
            f"report_dead_letter: error 缺少必要鍵 {sorted(missing)}"
            f"（需含 {sorted(_DEAD_LETTER_ERROR_REQUIRED_KEYS)}）"
        )
    error_class = error["error_class"]
    if error_class not in ERROR_CLASSES:
        raise ValueError(
            f"report_dead_letter: error_class={error_class!r} 不在合法集合 {sorted(ERROR_CLASSES)}"
        )
    if error_class != "NON_RETRYABLE":
        raise ValueError(
            "report_dead_letter: 契約 §3.2 規定 SS→DEAD_LETTER 僅限 NON_RETRYABLE，"
            f"收到 error_class={error_class!r}"
        )


class FirestoreReporter:
    """SS → 控制面 Firestore 回報客戶端（IP-301）。"""

    def __init__(
        self,
        client: Any,
        *,
        jobs_collection: str = "jobs",
        alerts_collection: str = "alerts",
        transaction_runner: Callable[[Callable[[Any], _T]], _T] | None = None,
    ) -> None:
        """client: google.cloud.firestore.Client 或鴨子型 fake（測試用）。

        transaction_runner: 事務執行器注入點，型別為
        `Callable[[Callable[[txn], T]], T]`；測試可傳
        `lambda body: body(fake_txn)` 繞過真實 SDK。None 時使用 SDK 預設路徑
        （寫法對齊 firestore_store.py:85-94 `_run_txn`：`client.transaction()` +
        `firestore.transactional` 裝飾）。
        """
        self._client = client
        self._jobs_collection = jobs_collection
        self._alerts_collection = alerts_collection
        self._transaction_runner: Callable[[Callable[[Any], Any]], Any] = (
            transaction_runner if transaction_runner is not None else self._default_transaction_runner
        )

    def _default_transaction_runner(self, body: Callable[[Any], _T]) -> _T:
        """SDK 預設事務路徑（對齊 firestore_store.py `_run_txn`）。"""
        txn = self._client.transaction()

        @firestore.transactional
        def _wrapped(transaction: Any) -> _T:
            return body(transaction)

        result: _T = _wrapped(txn)
        return result

    def _job_doc(self, job_id: str) -> Any:
        return self._client.collection(self._jobs_collection).document(job_id)

    def _alert_doc(self, alert_id: str) -> Any:
        return self._client.collection(self._alerts_collection).document(alert_id)

    def report_posted(self, job_id: str, *, lease_epoch: int) -> ReportResult:
        """回報「已記帳完成」：POSTING_IN_PROGRESS → POSTED。"""
        return self._report(job_id, STATE_POSTED, lease_epoch=lease_epoch, patch={})

    def report_dead_letter(
        self, job_id: str, *, lease_epoch: int, error: Mapping[str, Any]
    ) -> ReportResult:
        """回報「不可重試錯誤，轉死信」：POSTING_IN_PROGRESS → DEAD_LETTER。

        error 必含 stage/error_class/message 三鍵且 error_class=="NON_RETRYABLE"
        （契約 §3.2），否則 ValueError、零寫入（_validate_dead_letter_error 先行校驗，
        校驗失敗時連事務都不會開啟）。
        """
        _validate_dead_letter_error(error)
        now = _utcnow()
        patch: dict[str, Any] = {
            "error_class": error["error_class"],
            "last_error": {
                "stage": error["stage"],
                "error_class": error["error_class"],
                "message": error["message"],
                "at": now,
            },
        }
        return self._report(job_id, STATE_DEAD_LETTER, lease_epoch=lease_epoch, patch=patch)

    def _report(
        self,
        job_id: str,
        to_state: str,
        *,
        lease_epoch: int,
        patch: Mapping[str, Any],
    ) -> ReportResult:
        """共通讀-校-寫路徑（report_posted / report_dead_letter 都走此處）。

        事務內完成讀取 + 四項校驗（存在性 / 幂等 / 狀態 / epoch）+ 寫入；REJECTED
        情形下的 alert 寫入刻意放在事務**外**執行——alert 落哪個文檔 ID 需要 job 的
        source_file_id 欄位，讀出即可離開事務邊界，沒有理由把它綁進同一個事務。
        本方法對每次呼叫恰呼叫一次 self._transaction_runner（不重試，見模組 docstring）。
        """
        doc_ref = self._job_doc(job_id)
        serialized_patch = dict(patch)

        def body(transaction: Any) -> _TransactionOutcome:
            snap = doc_ref.get(transaction=transaction)
            if not snap.exists:
                return _TransactionOutcome(
                    ReportResult(ReportOutcome.REJECTED, job_id, reason="job_not_found")
                )
            data: dict[str, Any] = snap.to_dict() or {}
            source_file_id = data.get("source_file_id")
            current_state = data.get("current_state")

            if current_state == to_state:
                # F01②：目標態已達成 → 冪等成功、零寫入、零 alert
                return _TransactionOutcome(ReportResult(ReportOutcome.ALREADY_DONE, job_id))

            if current_state != STATE_POSTING_IN_PROGRESS:
                return _TransactionOutcome(
                    ReportResult(ReportOutcome.REJECTED, job_id, reason="unexpected_state"),
                    source_file_id=source_file_id,
                )

            actual_epoch = int(data.get("lease_epoch", 0))
            if actual_epoch != lease_epoch:
                return _TransactionOutcome(
                    ReportResult(ReportOutcome.REJECTED, job_id, reason="stale_lease_epoch"),
                    source_file_id=source_file_id,
                )

            now = _utcnow()
            new_data: dict[str, Any] = {**data, **serialized_patch}
            new_data["current_state"] = to_state
            new_data["updated_at"] = now
            new_data["attempt_count"] = 0  # F27：跨階段流轉歸零
            history = list(data.get("state_history") or [])
            history.append(dict(zip(_STATE_HISTORY_KEYS, (to_state, now, ACTOR_SUPER_SCANER))))
            new_data["state_history"] = history
            transaction.set(doc_ref, new_data)
            return _TransactionOutcome(ReportResult(ReportOutcome.APPLIED, job_id))

        outcome = self._transaction_runner(body)

        if outcome.result.outcome is ReportOutcome.REJECTED:
            alert_id = (
                job_id
                if outcome.result.reason == "job_not_found"
                else (outcome.source_file_id or job_id)
            )
            self.write_alert(
                alert_id,
                {"job_id": job_id, "reason": outcome.result.reason, "state": to_state},
            )
            logger.warning(
                "回報被拒 job_id=%s reason=%s state=%s",
                job_id,
                outcome.result.reason,
                to_state,
            )
        elif outcome.result.outcome is ReportOutcome.APPLIED:
            logger.info("回報成功 job_id=%s state=%s", job_id, to_state)
        else:
            logger.info("目標態已達成，冪等忽略 job_id=%s state=%s", job_id, to_state)

        return outcome.result

    def write_alert(self, alert_id: str, payload: Mapping[str, Any]) -> None:
        """寫入 `alerts/{alert_id}`（覆蓋語意，一次 `set()`；文檔 ID 天然冪等鍵）。

        不可變風格：呼叫方傳入的 payload 先淺拷貝再補時戳/actor 欄位，絕不就地
        修改呼叫方的 dict。
        """
        enriched: dict[str, Any] = {**dict(payload), "at": _utcnow(), "by": ACTOR_SUPER_SCANER}
        self._alert_doc(alert_id).set(enriched)
        logger.info("alert 已寫入 alert_id=%s", alert_id)
