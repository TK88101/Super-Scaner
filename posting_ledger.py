"""IP-304 頁級 posting 台賬（硬去重の権威査重面、SS 側）。

背景：契約 v0.12 §5.5「兩階段台賬」——頁単位の記帳を `jobs/{job_key}/postings/{page_id}`
に PENDING→CONFIRMED で記録し、崩潰重跑時の**硬去重**をこの台賬だけで成立させる
（Sheets を一切査らない、F54）。posting_id は Sheets へ入れない（F15）ので、去重键は
Firestore 側の本台賬が唯一の権威。

責務境界（定稿 Plan §9 D1'、Codex #4 采纳）：
    本モジュールは**状態機のみ**。Sheets への原子 append＋高亮は呼び出し側（sheets_output）
    の責務で、`post_page` へ `commit: Callable[[], SheetRowRange]` として注入される。
    ledger は `PageWrite`/gspread を一切 import しない（抽象境界の厳守）。

PENDING 恢復＝厳格 witness（定稿 Plan §9 D3'、Codex #1/#2/#3 反映）：
    PENDING doc の `predicted_row_range`＋`row_fingerprint` を witness とし、恢復時に
    `sheet_probe` で当日 Sheets 実行を照合する。全符→landed（補 CONFIRMED→SKIP）、
    空→未landed（WRITE 重写）、それ以外（部分不一致/範囲外れ/タブ無/読取例外/witness欠損）
    は一律 ESCALATE（人工核、POST_UNKNOWN へ委ねる）。啓発的探索・最良一致は禁。
    前提＝ADR-007 単進程単線程＋append-only＋人工挿入/削除/並替なし。

並発防護（Codex #5、CRITICAL 降級）：
    ADR-007 で SS は単進程単線程・lease 単 owner のため二 worker 並発は無い。それでも
    PENDING claim は事務 create-if-absent とし、**CONFIRMED を PENDING へ無条件リセット
    しない**（状態機恢復のみ許す。同 owner の重写＝既存 PENDING の更新は許可）。

時戳（F49）：一律 UTC。created_at＝PENDING 初記録、updated_at＝最新遷移（＝打刻 §5.3）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, TypeVar

from google.cloud import firestore

_T = TypeVar("_T")

# --- 台賬 doc 定数（定稿 Plan §9 schema 钉死） --------------------------------------
STATUS_PENDING = "PENDING"
STATUS_CONFIRMED = "CONFIRMED"
SCHEMA_VERSION = 1

# (start_row, end_row) 1-indexed inclusive。Sheets 実行の頁ブロック範囲。
SheetRowRange = tuple[int, int]


class LedgerStateError(RuntimeError):
    """台賬の状態機前提が破れた時に送出（CONFIRMED 上書き試行・PENDING 消失等）。

    fail fast で呼び出し側に気付かせる（ENTRY_BUILDERS 事故の「落として気付かせる」先例）。
    """


def derive_page_id(base: str, page_num: int) -> str:
    """頁 posting_id を base＋物理頁番号から派生（純関数、書式钉死）。

    `f"{base}:p{page_num}"`。base＝交棒契約の posting_id（intake_guard.resolve_posting_id）。
    """
    return f"{base}:p{page_num}"


class PageDecision(Enum):
    """check_page の裁決（定稿 Plan §9 D1'）。"""

    WRITE = "WRITE"        # 未記帳／PENDING かつ未landed → 記帳せよ
    SKIP = "SKIP"          # 既 CONFIRMED／PENDING かつ landed → 冪等スキップ
    ESCALATE = "ESCALATE"  # 判定不能 → 書込中止・人工核（POST_UNKNOWN）


class ProbeResult(Enum):
    """sheet_probe の判定（PENDING witness 照合、定稿 Plan §9 D3'）。"""

    PRESENT = "PRESENT"    # witness 全符＝append landed
    ABSENT = "ABSENT"      # 該範囲空＝append 未landed
    ESCALATE = "ESCALATE"  # 部分不一致/範囲外れ/タブ無/読取例外/row_count 不整合


@dataclass(frozen=True)
class TicketSummary:
    """F59 対账摘要の一票（advisory 専用、去重键には使わない、Codex #2 語義钉死）。

    amount＝**票級**の借方合計金額（行級ではない。1 票が複数税率行に展開されても、
    その票の合計を 1 件として持つ）。date＝ISO `YYYY-MM-DD`、vendor＝取引先名。
    """

    date: str
    amount: int
    vendor: str


@dataclass(frozen=True)
class PagePostingSummary:
    """post_page へ渡す頁記帳の純データ（Sheets 型非依存、定稿 Plan §9 D1'）。

    predicted_row_range/row_fingerprint が witness（PENDING 恢復の厳格照合キー）。
    tickets は F59 対账 advisory 専用。
    """

    page_num: int
    ticket_count: int
    row_count: int
    tickets: tuple[TicketSummary, ...]
    predicted_row_range: SheetRowRange
    row_fingerprint: str
    sheet_tab: str


def _utcnow() -> datetime:
    """台賬蓋章時戳（F49：一律 UTC）。"""
    return datetime.now(UTC)


def _as_range(value: Any) -> SheetRowRange | None:
    """Firestore から読んだ範囲値（list[int] 想定）を (start, end) へ正規化。

    None／長さ 2 でない／int でない → None（witness 欠損として ESCALATE へ倒すため）。
    """
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    if len(value) != 2:
        return None
    start, end = value
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    return (start, end)


# sheet_probe: (sheet_tab, predicted_row_range, row_fingerprint) -> ProbeResult
SheetProbe = Callable[[str, SheetRowRange, str], ProbeResult]


class PostingLedger:
    """頁級 posting 台賬（1 ファイル＝1 job 束縛、PageUrlResolver と同じ per-file 構築）。

    check_page（査重）と post_page（三步記帳）の状態機。Firestore client は reporter と
    同一を再利用（main で 1 個構築）。transaction_runner は reporter と同型の DI。
    """

    def __init__(
        self,
        client: Any,
        job_key: str,
        *,
        sheet_probe: SheetProbe,
        transaction_runner: Callable[[Callable[[Any], _T]], _T] | None = None,
        jobs_collection: str = "jobs",
        postings_subcollection: str = "postings",
    ) -> None:
        """client: firestore.Client 或鴨子型 fake。job_key: base posting_id（＝jobs doc id）。

        sheet_probe: PENDING witness 照合の注入関数（sheets 層のクロージャ、fake 差替可）。
        transaction_runner: 事務執行器（reporter._default_transaction_runner と同型）。
        """
        self._client = client
        self._job_key = job_key
        self._sheet_probe = sheet_probe
        self._jobs_collection = jobs_collection
        self._postings_subcollection = postings_subcollection
        self._transaction_runner: Callable[[Callable[[Any], Any]], Any] = (
            transaction_runner
            if transaction_runner is not None
            else self._default_transaction_runner
        )
        # 単発読キャッシュ（simcodex Round 1 #9）: (page_id, data) の直近1件のみ
        # 保持。check_page と confirmed_ticket_count が同一 page_id を連続で
        # 読む重跑場景の同文檔二重読を避ける。ADR-007（単進程単線程・lease 単
        # owner）前提でのみ安全——claim/confirm（書込）で無効化する。
        self._last_read: tuple[str, dict | None] | None = None

    def _default_transaction_runner(self, body: Callable[[Any], _T]) -> _T:
        """SDK 預設事務路徑（reporter._default_transaction_runner と同型）。"""
        txn = self._client.transaction()

        @firestore.transactional
        def _wrapped(transaction: Any) -> _T:
            return body(transaction)

        return _wrapped(txn)

    def _posting_doc(self, page_id: str) -> Any:
        return (
            self._client.collection(self._jobs_collection)
            .document(self._job_key)
            .collection(self._postings_subcollection)
            .document(page_id)
        )

    def _cached_or_read(self, page_id: str) -> dict | None:
        """check_page/confirmed_ticket_count 共用の単発読（キャッシュ命中時は
        Firestore を叩かない、simcodex Round 1 #9）。doc 不存在 → None、
        存在すれば dict（空なら {}、check_page の従来挙動と同一）。
        """
        if self._last_read is not None and self._last_read[0] == page_id:
            return self._last_read[1]
        snap = self._posting_doc(page_id).get()
        data = (snap.to_dict() or {}) if snap.exists else None
        self._last_read = (page_id, data)
        return data

    def _invalidate_cache(self, page_id: str) -> None:
        """claim/confirm（書込）でキャッシュを無効化する（同一 page_id のみ、
        他頁の直近読取りは無関係なので残す）。"""
        if self._last_read is not None and self._last_read[0] == page_id:
            self._last_read = None

    # --- 査重 -----------------------------------------------------------------
    def check_page(self, page_id: str) -> PageDecision:
        """`jobs/{job_key}/postings/{page_id}` を単発読して四情況判定（副作用は PRESENT 補記のみ）。

        無記録 → WRITE ／ CONFIRMED → SKIP ／ 未知 status → ESCALATE ／
        PENDING → witness 厳格照合（_recover_pending）。
        """
        data = self._cached_or_read(page_id)
        if data is None:
            return PageDecision.WRITE
        status = data.get("status")
        if status == STATUS_CONFIRMED:
            return PageDecision.SKIP
        if status != STATUS_PENDING:
            # 未知／破損 status → 保守 ESCALATE（静默スキップも静默重写もしない）
            return PageDecision.ESCALATE
        return self._recover_pending(page_id, data)

    def _recover_pending(self, page_id: str, data: dict) -> PageDecision:
        """PENDING の厳格 witness 恢復（定稿 Plan §9 D3'、啓発的探索禁）。"""
        sheet_tab = data.get("sheet_tab")
        predicted = _as_range(data.get("predicted_row_range"))
        fingerprint = data.get("row_fingerprint")
        if not isinstance(sheet_tab, str) or predicted is None or not fingerprint:
            # witness 欠損（旧 schema／破損）→ 判定不能 → 人工核
            return PageDecision.ESCALATE
        try:
            probe = self._sheet_probe(sheet_tab, predicted, fingerprint)
        except Exception:  # noqa: BLE001 - Sheets 読取例外は判定不能へ倒す（安全側）
            return PageDecision.ESCALATE
        if probe is ProbeResult.PRESENT:
            self._confirm(page_id, predicted, require_pending=True)
            return PageDecision.SKIP
        if probe is ProbeResult.ABSENT:
            return PageDecision.WRITE
        return PageDecision.ESCALATE

    # --- 只読 accessor（B4 Plan §2.2/T2、三謂詞シグネチャは不変） -----------------
    def confirmed_ticket_count(self, page_id: str) -> int | None:
        """該頁の CONFIRMED 記録の ticket_count を読む（単発読、副作用ゼロ）。

        無 CONFIRMED 記録（doc 不存在／status != CONFIRMED）／ticket_count が
        int でない（旧 schema・破損防御）→ None。存在すれば記録済み値
        （0 も正常値＝占位頁）をそのまま返す。check_page が SKIP を返した後、
        呼び出し側（main、T3）がこの値の >0/==0 で「真に入賬済み」か「占位頁」
        かを自証する（§2.2 ticket_count 語義釘死）。check_page と同一 page_id
        を直前に読んでいればその結果を再利用する（_cached_or_read、同文檔
        二重読の回避）。
        """
        data = self._cached_or_read(page_id)
        if data is None:
            return None
        if data.get("status") != STATUS_CONFIRMED:
            return None
        ticket_count = data.get("ticket_count")
        if not isinstance(ticket_count, int):
            return None
        return ticket_count

    # --- 記帳（三步） ---------------------------------------------------------
    def post_page(
        self,
        page_id: str,
        summary: PagePostingSummary,
        commit: Callable[[], SheetRowRange],
    ) -> None:
        """頁記帳の三步封装（① PENDING claim → ② commit（Sheets 原子書）→ ③ CONFIRMED）。

        commit が例外送出 → ③ を実行せず PENDING 残置（重跑で witness 恢復）。
        claim は create-if-absent（既存 PENDING は同 owner 重写として更新、CONFIRMED は拒否）。
        """
        self._claim_pending(page_id, summary)
        row_range = commit()
        self._confirm(page_id, row_range, require_pending=False)

    def _claim_pending(self, page_id: str, summary: PagePostingSummary) -> None:
        doc_ref = self._posting_doc(page_id)
        now = _utcnow()

        def body(txn: Any) -> None:
            snap = doc_ref.get(transaction=txn)
            created_at = now
            if snap.exists:
                existing = snap.to_dict() or {}
                if existing.get("status") == STATUS_CONFIRMED:
                    raise LedgerStateError(
                        f"post_page: {page_id} は既に CONFIRMED——"
                        "重複記帳を拒否（check_page が SKIP を返すべき局面）"
                    )
                # 既存 PENDING＝同 owner の重写（witness ABSENT 後）。created_at は保持。
                created_at = existing.get("created_at", now)
            txn.set(doc_ref, _pending_payload(summary, created_at, now))

        self._transaction_runner(body)
        self._invalidate_cache(page_id)

    def _confirm(
        self, page_id: str, row_range: SheetRowRange, *, require_pending: bool
    ) -> None:
        """PENDING → CONFIRMED（sheet_row_range 確定記録、updated_at 更新）。

        require_pending=True（PRESENT 補記経路）：既に非 PENDING なら二重確定せず黙って戻る。
        require_pending=False（post_page ③）：doc 消失は前提破れ＝LedgerStateError。
        """
        doc_ref = self._posting_doc(page_id)
        now = _utcnow()

        def body(txn: Any) -> None:
            snap = doc_ref.get(transaction=txn)
            if not snap.exists:
                if require_pending:
                    return
                raise LedgerStateError(
                    f"_confirm: {page_id} の PENDING が消失（claim 前提破れ）"
                )
            data = snap.to_dict() or {}
            if data.get("status") != STATUS_PENDING:
                # PRESENT 補記の競合等：既に遷移済みなら何もしない（冪等）
                return
            new_data = {
                **data,
                "status": STATUS_CONFIRMED,
                "sheet_row_range": [row_range[0], row_range[1]],
                "updated_at": now,
                # S2: written_at 双写（契約 §5.3 打卡制の数据源）。updated_at と
                # 同一 now 変数・同一 txn.set 内（防漂移三律①②）。
                "written_at": now,
            }
            txn.set(doc_ref, new_data)

        self._transaction_runner(body)
        self._invalidate_cache(page_id)


def _pending_payload(
    summary: PagePostingSummary,
    created_at: datetime,
    updated_at: datetime,
) -> dict[str, Any]:
    """PENDING doc の全文檔ペイロード（契約 v0.16 §5.5 対齊、S1/S2）。

    S1: 台賬キーは `page`（旧 `page_num`）。`page_id` は文書 ID に一本化済み
    （Firestore の doc id そのものが page_id）のため字段としては持たない
    （dataclass `PagePostingSummary.page_num` 属性名は不変——変わるのは
    Firestore 文書のキー面だけ）。
    S2: `written_at`＝`updated_at` と同一値（防漂移三律③、PENDING 記録時点）。
    """
    return {
        "page": summary.page_num,
        "status": STATUS_PENDING,
        "ticket_count": summary.ticket_count,
        "row_count": summary.row_count,
        "tickets": [
            {"date": t.date, "amount": t.amount, "vendor": t.vendor}
            for t in summary.tickets
        ],
        "predicted_row_range": [
            summary.predicted_row_range[0],
            summary.predicted_row_range[1],
        ],
        "row_fingerprint": summary.row_fingerprint,
        "sheet_tab": summary.sheet_tab,
        "sheet_row_range": None,
        "created_at": created_at,
        "updated_at": updated_at,
        "written_at": updated_at,
        "schema_version": SCHEMA_VERSION,
    }
