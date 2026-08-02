"""契約 v0.17 §5.7 provider 事件集合（B8）。

**この集合は何のためにあるか**：§5.4 は「分類段 Gemini と SS 識別段の
Gemini／PaddleOCR を各々 provider 単位で計数する」と要求するが、§6 は SS が
`SUCCESS`／`DEAD_LETTER` 以外を回報しないと定める——retryable な provider 失敗は
沈黙側に落ちる。`postings` には `provider`／`error_class` 字段が無く、§4.1／§4.2
にも該当集合が無い。つまり**契約内に当該信号を控制面へ送達する合法通道が存在
しなかった**。本モジュールがその通道。

**読む側は控制面の断路器のみ**（§5.7）。10 分窓・連続 5 件・15 分半開の裁決は
控制面に残る——SS 側に断路器を置かない理由は §5.7 の表のとおり（控制面が暫停の
理由を見られず沈黙を job 停滞と誤認する／プロセス再起動で状態が飛ぶ／
`next_retry_at` を統一設定できない）。**本モジュールは一切裁決しない。数えられる
形で事実を置くだけ。**

設計上の三拍板（趙 2026-08-02、Codex 対辯を経て）：

1. **粒度＝頁級 1 件**。断路器の閾値は「連続 5 件」なので、頁を跨いで集約すると
   本物の provider 障害が閾値に届かず永久に熔断しない。集約は禁物。
2. **上限＝同一 `job_id`×`provider`×`error_class` で 20 件**（`DEFAULT_JOB_CAP`）。
   500 頁の件で provider が全面障害を起こしたときに 500 件書かないための封頂。
   閾値 5 を十分上回るので、封頂によって熔断信号が埋もれることはない。
3. **書込失敗は本来の OCR 失敗を隠さない**。本モジュールは決して例外を伝播せず、
   書けたか否かを bool で返すだけ——事件記録の失敗で票の処理が落ちたら本末転倒。

**脱敏**（`basic-design/03` §3）：契約 §5.7 の四字段のみ。票据の中身・客戶名・
金額・ファイル名・token・例外メッセージは**禁入**。禁止字段は静かに捨てず
`TypeError` で落とす——静かに捨てると「渡したのに出ない」事故に化ける。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

# --- 契約権威値（§5.7 字段表・§5.4 三分類。勿臆造） ---------------------------

PROVIDERS = frozenset({"gemini_classify", "gemini_ocr", "paddleocr"})
ERROR_CLASSES = frozenset({"RETRYABLE", "NON_RETRYABLE", "UNKNOWN"})

COLLECTION = "provider_events"

# 断路器の閾値は 5 件（§5.4）。封頂はそれを十分上回る値でなければ、封頂自体が
# 熔断を妨げる——`test_cap_does_not_starve_the_breaker_threshold` が番人。
DEFAULT_JOB_CAP = 20

# `firestore_progress.SET_TIMEOUT_SECONDS` と同値・同理由（吊るされた Firestore が
# OCR を止めないための上限）。定数を共有しないのは、あちらが page_progress →
# gspread という重い依存鎖の先にあるため——本モジュールは標準ライブラリだけで
# 完結させる（ocr_engine から import される側なので、依存を足すと全経路に効く）。
# **例外分類のような正誤に関わる重複ではない**：値がずれても片方が 10 秒、片方が
# 15 秒になるだけで、どちらも「止まらない」という目的を果たす。
SET_TIMEOUT_SECONDS = 10

# 例外 → `error_class` の帰因は **`ocr_engine._classify_page_error` が唯一の権威**。
# 本モジュールは意図的に持たない——同じ例外が頁級台帳では RETRYABLE、断路器へは
# UNKNOWN、という食い違いが起きると控制面が誤った provider を熔断する
# （simplify R1・reuse／altitude 双方が独立に指摘）。呼出側（ocr_engine）は
# 自分の分類器を通した結果を `error_class` として渡すこと。


def _utcnow() -> datetime:
    """F49：全時戳 UTC。"""
    return datetime.now(UTC)


def _event_id(job_id: str | None, page: int | None,
              provider: str, error_class: str) -> str:
    """確定的 event_id（採番規則は契約 §9-1 U7 校准待ち・暫定値）。

    同一頁・同一 provider・同一 error_class の再記録が増殖しないよう、乱数や
    連番ではなく内容から決める——リトライや重跑で同じ事象が何度も走っても
    `set()` が同じ文書を上書きするだけで済む。
    """
    page_part = page if page is not None else "-"
    return f"{job_id or '-'}:p{page_part}:{provider}:{error_class}"


class ProviderEventWriter:
    """`provider_events/{event_id}` への書込口（§5.7）。

    上限カウンタはプロセス内 dict。再起動で消えるのは意図どおり——再起動＝
    新しい一巡であり、そこから 20 件の予算を配り直すのが自然。永続化すると
    「昨日の障害の残額で今日の障害が書けない」という理解しにくい挙動になる。
    """

    def __init__(self, client: Any, *, collection: str = COLLECTION,
                 job_cap: int = DEFAULT_JOB_CAP) -> None:
        self._client = client
        self._collection = collection
        self._job_cap = job_cap
        # cap_key → 既書 event_id の集合。**冪等判定と上限計数を同じ構造に
        # 載せる**のが肝（simplify R1 採納）：別々に持つと「冪等な再記録は上限を
        # 食わない」という不変式が「二つとも触らないのを憶えている」ことでしか
        # 保てず、片方だけ進める編集で静かに壊れる。集合の要素数がそのまま
        # 上限判定になるので、構造として壊れなくなる。
        self._seen: dict[tuple[str, str, str], set[str]] = {}
        self._warned = False                     # 書込失敗の日誌は檔内 1 回

    def record(self, *, provider: str, error_class: str,
               page: int | None = None, job_id: str | None = None) -> bool:
        """事象を 1 件記録する。書けたら True、封頂/書込失敗なら False。

        キーワード専用引数なのは、位置引数で provider と error_class を取り違えても
        両方 str なので値域検証を素通りし得るため。
        """
        if provider not in PROVIDERS:
            raise ValueError(
                f"provider は §5.7 の値域 {sorted(PROVIDERS)} のみ。受領: {provider!r}"
            )
        if error_class not in ERROR_CLASSES:
            raise ValueError(
                f"error_class は §5.4 の三分類 {sorted(ERROR_CLASSES)} のみ。"
                f"受領: {error_class!r}"
            )
        if job_id is not None and not isinstance(job_id, str):
            raise TypeError(
                f"job_id は str か None のみ（脱敏白名単）。受領型: {type(job_id).__name__}"
            )
        if page is not None and not isinstance(page, int):
            raise TypeError(
                f"page は int か None のみ（脱敏白名単）。受領型: {type(page).__name__}"
            )

        event_id = _event_id(job_id, page, provider, error_class)
        seen = self._seen.setdefault((job_id or "-", provider, error_class), set())
        if event_id in seen:
            return False                          # 冪等：上限も消費しない
        if len(seen) >= self._job_cap:
            return False

        payload: Mapping[str, Any] = {
            "provider": provider,
            "error_class": error_class,
            "occurred_at": _utcnow(),
            "job_id": job_id,
        }
        try:
            (self._client.collection(self._collection).document(event_id)
             .set(payload, timeout=SET_TIMEOUT_SECONDS))
        except Exception as exc:  # noqa: BLE001 - 事件記録の失敗で票処理を落とさない
            if not self._warned:
                # 檔内 1 回だけ（provider 全面障害の 20 頁で同文 20 行を
                # 無人の mini PC に吐かない。firestore_progress._try_set と同流儀）
                print(
                    f"provider 事件の書込失敗（本来の処理は続行）"
                    f" event_id={event_id} error_type={type(exc).__name__}"
                )
                self._warned = True
            return False                          # 上限も消費しない（復旧後に書ける）

        seen.add(event_id)
        return True
