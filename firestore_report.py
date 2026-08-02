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

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any, TypeVar

from google.cloud import firestore
from google.oauth2 import service_account

import config

_T = TypeVar("_T")

# --- 常量（契約權威值，勿臆造） -------------------------------------------------
STATE_POSTING_IN_PROGRESS = "POSTING_IN_PROGRESS"
STATE_POSTED = "POSTED"
STATE_DEAD_LETTER = "DEAD_LETTER"
ACTOR_SUPER_SCANER = "super_scaner"

# report_dead_letter(error=...) 必含三鍵（契約 §3.2 事故排障最小欄位集）——
# 校驗與 error_fields 抽取共用的單一真相源
_DEAD_LETTER_ERROR_REQUIRED_KEYS = frozenset({"stage", "error_class", "message"})

# last_error / alert 內自由文本的硬性長度上限（跨倉共享 collection 的最後防線；
# 語意級脫敏——客戶名/金額/文件名禁入——仍是呼叫方責任，basic-design/03 §3）。
# 截斷後含後綴的總長恰等於上限（下游按此上限建 schema 不會收到超長值）。
_MESSAGE_MAX_LEN = 1000
_TRUNCATION_SUFFIX = "…[截斷]"

# alert 文檔の降級標記（D3 付帯・simcodex R2）。既存分の読取に失敗した回は
# `reason_stats` を据置き、この標記を立てて「厳密な現況ではない」と宣言する。
# 跨倉 collection なので控制面も読める形にする（黙って劣化させない）。
ALERT_STATS_STATE_KEY = "reason_stats_state"
ALERT_STATS_STALE = "stale_due_to_read_failure"


class ReportOutcome(Enum):
    """_report 三種終局結果（契約 F01②語意：APPLIED / ALREADY_DONE 皆為「成功」，
    只有 REJECTED 需要控制面裁決）。"""

    APPLIED = "APPLIED"
    ALREADY_DONE = "ALREADY_DONE"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ReportResult:
    """report_posted / report_dead_letter 的回傳值（不可變）。

    alert_delivered：REJECTED 時 alert 是否成功落檔。False＝alert 寫入失敗
    （已列印診斷），但 job 文檔的裁決結果本身已定、不受影響——呼叫方不得
    因此重試寫賬；是否補報 alert 由呼叫方/控制面政策決定。
    """

    outcome: ReportOutcome
    job_id: str
    reason: str | None = None
    alert_delivered: bool = True


@dataclass(frozen=True)
class _TransactionOutcome:
    """事務 body 的內部回傳型別：把裁決結果與寫 alert 所需線索（source_file_id
    ＋事務內觀測值 observed）一起帶出事務邊界（alert 依契約在事務外寫，見 _report）。"""

    result: ReportResult
    source_file_id: str | None = None
    observed: Mapping[str, Any] | None = None


def _utcnow() -> datetime:
    """模組蓋章時戳（F49：一律 UTC）。不信任呼叫方傳入的時戳。"""
    return datetime.now(UTC)


def _read_reason_stats(doc: Any, alert_id: str) -> Mapping[str, Any] | None:
    """既存 alert 文檔から `reason_stats` を読む。**読取失敗は `None`**。

    「正常に読めて空だった」（`{}`）と「読めなかった」（`None`）を区別するのが
    肝（simcodex R2・Codex 指摘採納）。両方を `{}` に潰すと、呼出側は読めな
    かった時にも「累計ゼロから開始」と解釈して `occurrences=1` を書き、
    ディスク上の本物の `occurrences=7` を上書きしてしまう——**嘘をつく
    カウンタは、カウンタが無いことより悪い**。

    読取失敗そのものを握り潰すのは意図的：ここで例外を上げると alert 自体が
    書けなくなり「なぜ隔離されたか」が誰にも見えない不可視状態に戻る。
    失う物は履歴の更新、守る物は可視性——後者が重い（D3 裁決の付帯規則）。
    """
    try:
        snapshot = doc.get()
        if not getattr(snapshot, "exists", False):
            return {}
        existing = snapshot.to_dict() or {}
    except Exception as exc:  # noqa: BLE001 - 履歴 < 可視性
        print(f"alert 既存分の読取失敗（累計は据置） alert_id={alert_id} "
              f"error_type={type(exc).__name__}")
        return None
    stats = existing.get("reason_stats")
    return stats if isinstance(stats, Mapping) else {}


def _merged_reason_stats(existing: Mapping[str, Any], reason_code: str,
                         now: datetime) -> dict[str, Any]:
    """`reason_stats` を 1 原因ぶん進めた**新しい** dict を返す（就地改変せず）。

    同一原因＝`write_count` を増やし `last_seen_at` のみ進める（`first_seen_at`
    は初回の値を保つ）。別原因＝新しいキーとして並置する——これが D3 の眼目で、
    異因が覆い消されないことそのもの。

    **`write_count` は「書込に成功した回数」であって「業務事象の発生回数」では
    ない**（simcodex R4・Codex 指摘採納）。この加算自体は冪等でなく、次の二つで
    実際より多く数え得る：①`alerted` はプロセス内キャッシュなので、再起動を
    挟むと「alert 済み・move 未了」の件が再び書かれる ②書込がサーバ側で成功
    したのに応答受領前に失敗した場合の再試行。正確な業務事象数が要るなら別建て
    の事象流が要る——名前で嘘をつかないため `occurrences` とは呼ばない。
    """
    prior = existing.get(reason_code)
    prior = prior if isinstance(prior, Mapping) else {}
    # `occurrences` は改名前の同義字段（commit ab634c9 のみ・main 未到達なので
    # 本番には存在しないはずだが、遺留の「真 Firestore 往返検証」を本分支で
    # 行った環境には残り得る）。読めたら引き継ぐ——数え直しは静かなデータ損失。
    # 真往返の検証が済み、旧字段が無いと確認できた時点で削ってよい。
    write_count = prior.get("write_count", prior.get("occurrences"))
    write_count = write_count + 1 if isinstance(write_count, int) else 1
    return {
        **existing,
        reason_code: {
            "write_count": write_count,
            "first_seen_at": prior.get("first_seen_at", now),
            "last_seen_at": now,
        },
    }


def _validate_dead_letter_error(error: Mapping[str, Any]) -> None:
    """report_dead_letter 的 error 參數校驗（fail fast，零寫入）。

    契約 §3.2：SS→DEAD_LETTER 僅限 NON_RETRYABLE（損壞/加密/空白等不可重試錯誤）；
    其他一切值（RETRYABLE/UNKNOWN/未知值）一律視為呼叫方誤用，直接 ValueError，
    不寫任何 Firestore 文檔——單一判等即可覆蓋，毋須另設合法值枚舉。
    """
    missing = _DEAD_LETTER_ERROR_REQUIRED_KEYS - set(error.keys())
    if missing:
        raise ValueError(
            f"report_dead_letter: error 缺少必要鍵 {sorted(missing)}"
            f"（需含 {sorted(_DEAD_LETTER_ERROR_REQUIRED_KEYS)}）"
        )
    if not isinstance(error["message"], str):
        raise ValueError(
            f"report_dead_letter: message 必須是 str（收到 {type(error['message']).__name__}）"
            "——自由文本以外的型別禁入 last_error/alert"
        )
    error_class = error["error_class"]
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

    @property
    def client(self) -> Any:
        """保持している Firestore client（IP-304 の PostingLedger が同一 client を再利用）。"""
        return self._client

    def _default_transaction_runner(self, body: Callable[[Any], _T]) -> _T:
        """SDK 預設事務路徑（對齊 firestore_store.py `_run_txn`）。

        每次呼叫重建 wrapper 是刻意為之：SDK 的 _Transactional 持有 current_id/
        retry_id 等可變狀態，共享單一實例在並發下是足槍；參照實裝 _run_txn 亦同型。
        """
        txn = self._client.transaction()

        @firestore.transactional
        def _wrapped(transaction: Any) -> _T:
            return body(transaction)

        return _wrapped(txn)

    def _job_doc(self, job_id: str) -> Any:
        return self._client.collection(self._jobs_collection).document(job_id)

    def _alert_doc(self, alert_id: str) -> Any:
        return self._client.collection(self._alerts_collection).document(alert_id)

    def get_job(self, job_id: str) -> Mapping[str, Any] | None:
        """単發非事務讀 `jobs/{job_id}`（IP-303、intake_guard.check_intake が呼ぶ）。

        report_posted/report_dead_letter の読-校-写事務とは別経路——事務を張らない
        単純読取り。job が存在しなければ None。SDK 例外はそのまま伝播させる
        （Firestore 瞬断と「job が実在しない」を呼び出し側が区別できるよう、
        ここで握り潰さない。intake_guard.check_intake が例外を捕捉して
        DEFERRED に倒す判断をする——本メソッドの責務ではない）。
        """
        snap = self._job_doc(job_id).get()
        if not snap.exists:
            return None
        return snap.to_dict() or {}

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

        時戳語義（刻意雙軌，與控制面 mark_failed 同型——呼叫方自帶 error 時戳）：
        last_error.at＝SS 觀測到錯誤的時刻（進事務前蓋章）；updated_at /
        state_history[].at＝流轉提交時刻（事務 body 內蓋章）。兩者本是不同事件，
        SDK 對 Aborted 的重試可使後者晚於前者，屬預期行為而非偏差。

        本次回報若被拒（REJECTED），error 內容以 attempted_error 併入 alert——
        控制面/人工裁決時可得知 SS 當初為何要判死（僅技術字段；message 由呼叫方
        負責不夾帶客戶名/金額/文件名，日誌白名單 basic-design/03 §3）。
        """
        _validate_dead_letter_error(error)
        now = _utcnow()
        # last_error 與 attempted_error 共用同一組三鍵，以必含鍵集合為單一真相源派生
        error_fields: dict[str, Any] = {
            k: error[k] for k in sorted(_DEAD_LETTER_ERROR_REQUIRED_KEYS)
        }
        if len(error_fields["message"]) > _MESSAGE_MAX_LEN:
            capped = error_fields["message"][: _MESSAGE_MAX_LEN - len(_TRUNCATION_SUFFIX)]
            error_fields = {**error_fields, "message": capped + _TRUNCATION_SUFFIX}
        patch: dict[str, Any] = {
            "error_class": error["error_class"],
            "last_error": {**error_fields, "at": now},
        }
        alert_extra: dict[str, Any] = {"attempted_error": error_fields}
        return self._report(
            job_id,
            STATE_DEAD_LETTER,
            lease_epoch=lease_epoch,
            patch=patch,
            alert_extra=alert_extra,
        )

    def _report(
        self,
        job_id: str,
        to_state: str,
        *,
        lease_epoch: int,
        patch: Mapping[str, Any],
        alert_extra: Mapping[str, Any] | None = None,
    ) -> ReportResult:
        """共通讀-校-寫路徑（report_posted / report_dead_letter 都走此處）。

        事務內完成讀取 + 四項校驗（存在性 / 幂等 / 狀態 / epoch）+ 寫入；REJECTED
        情形下的 alert 寫入刻意放在事務**外**執行——alert 落哪個文檔 ID 需要 job 的
        source_file_id 欄位，讀出即可離開事務邊界，沒有理由把它綁進同一個事務。
        alert_extra＝呼叫方附帶的裁決上下文（僅技術字段），REJECTED 時併入 alert
        payload——上下文由知情層（呼叫方）提供、組裝由本層負責。
        本方法對每次呼叫恰呼叫一次 self._transaction_runner（不重試，見模組 docstring）。
        """
        doc_ref = self._job_doc(job_id)

        def _rejected(
            reason: str,
            source_file_id: str | None = None,
            observed: Mapping[str, Any] | None = None,
        ) -> _TransactionOutcome:
            return _TransactionOutcome(
                ReportResult(ReportOutcome.REJECTED, job_id, reason=reason),
                source_file_id=source_file_id,
                observed=observed,
            )

        def body(transaction: Any) -> _TransactionOutcome:
            snap = doc_ref.get(transaction=transaction)
            if not snap.exists:
                return _rejected("job_not_found")
            data: dict[str, Any] = snap.to_dict() or {}
            source_file_id = data.get("source_file_id")
            current_state = data.get("current_state")

            if current_state == to_state:
                # F01②：目標態已達成 → 冪等成功、零寫入、零 alert
                return _TransactionOutcome(ReportResult(ReportOutcome.ALREADY_DONE, job_id))

            if current_state != STATE_POSTING_IN_PROGRESS:
                # 事務內觀測值隨結果帶出——alert 須自帶「實際卡在哪」的裁決線索
                return _rejected(
                    "unexpected_state", source_file_id,
                    observed={"observed_state": current_state},
                )

            actual_epoch = int(data.get("lease_epoch", 0))
            if actual_epoch != lease_epoch:
                return _rejected(
                    "stale_lease_epoch", source_file_id,
                    observed={"observed_epoch": actual_epoch, "expected_epoch": lease_epoch},
                )

            now = _utcnow()
            # 全文檔 set（讀-改-寫）是刻意為之：與控制面參照實裝 transition() 同型。
            # 勿改成 update()+ArrayUnion——ArrayUnion 帶去重語義，違反契約
            # 「state_history 只追加」；且回報頻度低（每 job 一次）、文檔小，
            # >200 條歷史本身即対賬報警線（契約 §2）。
            new_data: dict[str, Any] = {**data, **patch}
            new_data["current_state"] = to_state
            new_data["updated_at"] = now
            new_data["attempt_count"] = 0  # F27：跨階段流轉歸零
            history = list(data.get("state_history") or [])
            # state_history 條目白名單制：恰此三鍵，不得夾帶客戶名/金額等敏感字段
            history.append({"state": to_state, "at": now, "by": ACTOR_SUPER_SCANER})
            new_data["state_history"] = history
            transaction.set(doc_ref, new_data)
            return _TransactionOutcome(ReportResult(ReportOutcome.APPLIED, job_id))

        outcome = self._transaction_runner(body)

        # 診斷輸出走倉庫慣例的 print（生產監控＝人工盯控制台；全倉未配置 logging，
        # logger.info 在預設 WARNING level 下不可見）。輸出僅限技術字段白名單。
        if outcome.result.outcome is ReportOutcome.REJECTED:
            # job_not_found 時 body 不附 source_file_id → 自然退用 job_id 作 alert 鍵
            alert_id = outcome.source_file_id or job_id
            payload: dict[str, Any] = {
                "kind": "transition_rejected",  # 與 F20「無 id 文件」上報的判別欄
                "job_id": job_id,
                "reason": outcome.result.reason,
                "state": to_state,
            }
            for extra in (outcome.observed, alert_extra):
                if not extra:
                    continue
                overlap = set(payload) & set(extra)
                if overlap:
                    # 程序錯誤守衛：附加欄位覆蓋既有欄位＝呼叫方 bug，fail fast
                    raise ValueError(f"_report: alert 附加欄位鍵衝突 {sorted(overlap)}")
                payload = {**payload, **extra}
            result = outcome.result
            try:
                self.write_alert(alert_id, payload)
            except Exception as exc:
                # alert 是從屬旁路：其失敗不得綁架已定案的裁決結果（防止呼叫方誤判
                # 「回報失敗」而無限重跑整件——ENTRY_BUILDERS 事故同型）。失敗以
                # 旗標回傳＋列印，交呼叫方/控制面政策處置，不靜默。
                print(
                    f"alert 寫入失敗（裁決結果不受影響） alert_id={alert_id} "
                    f"error_type={type(exc).__name__}"
                )
                result = replace(result, alert_delivered=False)
            print(f"回報被拒 job_id={job_id} reason={outcome.result.reason} state={to_state}")
            return result
        elif outcome.result.outcome is ReportOutcome.APPLIED:
            print(f"回報成功 job_id={job_id} state={to_state}")
        else:
            print(f"目標態已達成、冪等忽略 job_id={job_id} state={to_state}")

        return outcome.result

    def write_alert(self, alert_id: str, payload: Mapping[str, Any]) -> None:
        """寫入 `alerts/{alert_id}`（單一文檔・`set()`；文檔 ID 天然冪等鍵）。

        **D3 裁決（趙 2026-08-02、Codex 二輪対辯を経た共同推薦）**：舊實裝は
        同一 alert_id が先後で**別の**原因により拒否されると後寫が前寫を覆い、
        早先の線索が消えた。純粋な追加（auto_id 文檔）は却下——文檔 ID＝file_id
        という F20 の「天然冪等」は刷屏防止の護欄であり、壊す代償が大き過ぎる。
        採った形は**單一文檔のまま原因ごとの累計を持つ**：

            payload そのもの   ＝ 当前快照（最新の原因。従来どおり）
            reason_stats[code] ＝ {write_count, first_seen_at, last_seen_at}

        控制面は依然として**一文檔を一度読むだけ**でよい（読取面は未建設
        なので、子集合を掃く負担を先回りして負わせない）。

        reason code は `reason` →（無ければ）`kind` の順で採る。どちらも無い
        場合は `reason_stats` 自体を置かない——累計する対象が無いのに空の器を
        作らない（空 payload の既存契約も維持される）。

        既存分の読取が落ちても alert 自体は必ず出す（履歴より可視性が優先。
        alert が消えると「なぜ隔離されたか」が誰にも見えなくなる）。

        不可變風格：spread 展開本身即產生新 dict，絕不就地修改呼叫方的 payload。
        """
        doc = self._alert_doc(alert_id)
        reason_code = payload.get("reason") or payload.get("kind")
        enriched: dict[str, Any] = {**payload, "at": _utcnow(), "by": ACTOR_SUPER_SCANER}

        if not reason_code:
            doc.set(enriched)                      # 累計する対象が無い＝従来どおり
            print(f"alert 已寫入 alert_id={alert_id}")
            return

        prior = _read_reason_stats(doc, alert_id)
        if prior is None:
            # 読取失敗。**捏造した累計を書くくらいなら書かない**——`merge=True`
            # でディスク上の `reason_stats` を素通りさせ、当前快照だけ更新する。
            # 代償として本回分は計上されず、payload に無い旧字段も残り得るので
            # （merge は欠落字段を消さない）、消費側が「厳密な現況」と誤読しない
            # よう降級標記を残す。次に読めた回の全量 set でこの標記ごと消える。
            enriched[ALERT_STATS_STATE_KEY] = ALERT_STATS_STALE
            doc.set(enriched, merge=True)
        else:
            enriched["reason_stats"] = _merged_reason_stats(
                prior, str(reason_code), enriched["at"])
            doc.set(enriched)
        print(f"alert 已寫入 alert_id={alert_id}")


def build_reporter_from_env() -> FirestoreReporter:
    """`.env` から `FirestoreReporter` を構築するモジュール級工廠（IP-303）。

    HEADLESS_MODE 起動時に main.py が一度だけ呼ぶ想定。Drive/Sheets と同一の
    `SERVICE_ACCOUNT_FILE`（SA 共用、docs/headless-deploy-checklist.md）を流用
    する——ファイルが実在すればそこから明示的に credentials を組み立てる。
    `FIRESTORE_PROJECT_ID` が設定されていれば project を明示指定する。どちらも
    未設定／ファイル不在なら google-cloud-firestore の既定解決
    （Application Default Credentials 等）に委ねる。

    失敗（ファイル破損・権限不足・プロジェクト未検出等）は例外をそのまま
    伝播させる——main 起動時に fail fast する
    （ENTRY_BUILDERS 事故と同じ「落として気付かせる」先例、CLAUDE.md 参照）。
    """
    kwargs: dict[str, Any] = {}
    sa_file = os.getenv("SERVICE_ACCOUNT_FILE", "")
    if sa_file and os.path.exists(sa_file):
        kwargs["credentials"] = (
            service_account.Credentials.from_service_account_file(sa_file)
        )
    project_id = config.firestore_project_id()
    if project_id:
        kwargs["project"] = project_id
    client = firestore.Client(**kwargs)
    return FirestoreReporter(client)
