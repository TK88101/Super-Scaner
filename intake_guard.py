"""IP-302/IP-303 監視夾入口守衛（サンデヴィスタン統合、ヘッドレスモード）。

背景：控制面（サンデヴィスタン）が交棒契約に従い、監視夾へ file を投入する前に
Drive 公開 properties へ base posting_id を書込む（契約 job-state-machine.md
v0.12 §3.2「先寫属性、後 move」）。SS 側はこの property を読取り、控制面の
Firestore `jobs/{job_id}` 文檔と照合してから記帳処理へ進む——本モジュールは
その受信/照合ロジックを担う。

契約権威値（v0.10 定名、跨倉庫契約値：控制面書込・SS 読取、同名 key）：
    POSTING_ID_PROPERTY_KEY = "sandevistan_posting_id"
    改名即破壊交棒——単体でも複数リポジトリでも勝手に変えない。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

# 契約 v0.10 §6 跨倉庫契約値（控制面寫、SS 讀同名 key）——改名即破壊交棒
POSTING_ID_PROPERTY_KEY = "sandevistan_posting_id"


def resolve_posting_id(file: Mapping[str, Any]) -> str | None:
    """Drive file dict の公開 properties から base posting_id を読取る（純関数）。

    以下いずれも None（防御的仕様。Drive API は本来 str→str のみ返すが、
    fake/破損データでも安全に縮退させる）：
        - properties キー自体が無い、または None
        - properties はあるが POSTING_ID_PROPERTY_KEY が無い
        - 値が str でない
        - 値が空文字列／空白のみ

    正常値は前後の空白を strip して返す。
    """
    properties = file.get("properties") or {}
    value = properties.get(POSTING_ID_PROPERTY_KEY)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped


class IntakeDecision(Enum):
    """check_intake の裁決結果（IP-303）。"""

    PROCESS = "PROCESS"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"


@dataclass(frozen=True)
class IntakeCheck:
    """check_intake の戻り値（不可変）。

    base：resolve_posting_id が読取れた値（無ければ None）。REJECTED/DEFERRED
    でも base が取れていれば alert payload に含める（裁決線索、白名単内）。
    reason：PROCESS 時は None、それ以外は技術字段の理由文字列
    （no_posting_id / job_not_found / posting_id_mismatch / firestore_error:<型名>）。
    """

    decision: IntakeDecision
    base: str | None
    reason: str | None


def check_intake(
    file: Mapping[str, Any],
    get_job: Callable[[str], Mapping[str, Any] | None],
) -> IntakeCheck:
    """監視夾入口の五分岐判定（IP-303、契約 job-state-machine.md v0.12 §3.2）。

    分岐順序（上から評価）：
        1. resolve_posting_id が None                → REJECTED("no_posting_id")
        2. get_job(base) が例外送出（Firestore 瞬断）  → DEFERRED("firestore_error:<型名>")
        3. job が None（base に対応する job が無い）    → REJECTED("job_not_found")
        4. job["posting_id"] != base（契約不一致）      → REJECTED("posting_id_mismatch")
        5. それ以外                                    → PROCESS

    DEFERRED は「今回は判定不能」を意味し、REJECTED（恒久的に拒絶）とは扱いが
    異なる——瞬断で正常件を誤って隔離してはならないため、呼び出し側
    （handle_intake）は DEFERRED を隔離/alert 対象にしない（下輪再試行に委ねる）。
    """
    base = resolve_posting_id(file)
    if base is None:
        return IntakeCheck(IntakeDecision.REJECTED, None, "no_posting_id")

    try:
        job = get_job(base)
    except Exception as exc:  # noqa: BLE001 - Firestore 瞬断を意図的に広く捕捉
        return IntakeCheck(
            IntakeDecision.DEFERRED, base, f"firestore_error:{type(exc).__name__}")

    if job is None:
        return IntakeCheck(IntakeDecision.REJECTED, base, "job_not_found")

    if job.get("posting_id") != base:
        return IntakeCheck(IntakeDecision.REJECTED, base, "posting_id_mismatch")

    return IntakeCheck(IntakeDecision.PROCESS, base, None)


def _finish_quarantine_move(
    move_to_quarantine: Callable[[], None],
    file_id: Any,
    reason: str | None,
    alerted: MutableMapping[str, str] | None,
) -> bool:
    """move_to_quarantine 実行から結果に応じた alerted 更新／print までの尾部処理
    （メモ命中分岐と REJECTED 分岐が同型で持っていた重複を解消、恒に False を返す）。

    move 失敗 → print（file_id/error_type）→ alerted は不動 → False。
    move 成功 → alerted が非 None なら該当キーを削除 → print（file_id/reason）→ False。
    """
    try:
        move_to_quarantine()
    except Exception as exc:  # noqa: BLE001 - alert は既に処理済み、move のみ再試行対象
        print(f"入口守衛: 隔離夾への移動失敗 file_id={file_id} error_type={type(exc).__name__}")
        return False

    if alerted is not None:
        del alerted[file_id]

    print(f"入口守衛: 拒絶 file_id={file_id} reason={reason}")
    return False


def handle_intake(
    file: Mapping[str, Any],
    *,
    get_job: Callable[[str], Mapping[str, Any] | None],
    write_alert: Callable[[str, Mapping[str, Any]], None],
    move_to_quarantine: Callable[[], None],
    alerted: MutableMapping[str, str] | None = None,
) -> bool:
    """check_intake の裁決を副作用に変換する（IP-303 監視夾入口守衛の本体）。

    戻り値 True＝呼び出し側は処理続行してよい。刻意不收 writer 参数——
    構造上 Sheets へ書込む経路を持たない（「全程 Sheets 零新增行」の DoD 証拠）。

    alerted：拒絶件記憶層（進程内快取、file_id → 拒絶 reason）。ライフサイクルは
    呼び出し方（main() の while True ループ）が持つ——プロセス再起動で自然に
    消える。alert 文檔 ID＝file_id で天然冪等のため再書込しても無害であり、
    このキャッシュは「alert は送達済みだが move だけ未完了」の件で
    check_intake/get_job/write_alert を再実行させない（＝跨輪での Firestore
    無界重打を防ぐ）ためだけに存在する。可変 Mapping を受けるのは意図的——
    このキャッシュの本義は複数回の呼出しをまたいで共有される状態そのもの。

    分岐別の挙動：
        メモ命中（alerted が非 None かつ file_id が既に in alerted）→
                   check_intake/get_job/write_alert を一切実行せず
                   move_to_quarantine() のみ再試行する。
                       成功 → alerted から該当キーを削除 → print（reason は
                              旧値から）→ False。
                       失敗 → alerted は不変 → print（file_id/error_type）
                              → False。
        PROCESS  → 副作用なし、True。
        DEFERRED → print（file_id/reason のみ、白名単）→ False。
                   alert も隔離も行わない——瞬断で正常件を誤って隔離しない
                   ため、下輪の再試行に委ねる。alerted も不動（下輪で
                   get_job を再照会し復旧を検知する必要があるため）。
        REJECTED → ①write_alert(file_id, payload) を先に実行
                       （payload は {kind, file_id, reason} ＋ base が非 None
                        なら posting_id。write_alert が例外送出 → print → False、
                        move は実行しない、alerted も不動、下輪で再試行される
                        ——alert が未送達なら下輪も必ず再送させる）。
                   ②alerted が非 None → alerted[file_id]=reason を記入
                       （move の前に記す——alert 送達済みという事実は move の
                        成否と無関係なため）。
                   ③move_to_quarantine() を実行
                       （例外送出 → print → False、alerted は残す。alert は
                        文檔 ID＝file_id で天然冪等のため、下輪の再試行で
                        再度 write_alert しても無害＝上書きされるだけ）。
                   ④move 成功 → alerted から該当キーを削除。
                   ⑤print 一行（file_id/reason）→ False。

    順序を「先 alert 後 move」に固定した理由：隔離だけ済んで alert が無い状態は
    「なぜ隔離されたか」が誰にも見えない不可視状態になる。alert→move の順なら、
    どちらかで失敗しても次回の再試行で無害に補完できる
    （alert は文檔 ID 冪等、move は隔離先に既に無ければ再実行されるだけ）。
    """
    file_id = file.get("id")

    if alerted is not None and file_id in alerted:
        reason = alerted[file_id]
        return _finish_quarantine_move(move_to_quarantine, file_id, reason, alerted)

    check = check_intake(file, get_job)

    if check.decision is IntakeDecision.PROCESS:
        return True

    if check.decision is IntakeDecision.DEFERRED:
        print(f"入口守衛: 判定保留 file_id={file_id} reason={check.reason}")
        return False

    # REJECTED
    payload: dict[str, Any] = {
        "kind": "intake_rejected",
        "file_id": file_id,
        "reason": check.reason,
    }
    if check.base is not None:
        payload = {**payload, "posting_id": check.base}

    try:
        write_alert(file_id, payload)
    except Exception as exc:  # noqa: BLE001 - 旁路失敗を隔離、呼び出し方針は下輪再試行
        print(f"入口守衛: alert 書込失敗 file_id={file_id} error_type={type(exc).__name__}")
        return False

    if alerted is not None:
        alerted[file_id] = check.reason

    return _finish_quarantine_move(move_to_quarantine, file_id, check.reason, alerted)
