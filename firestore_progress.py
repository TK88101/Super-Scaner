"""B7-2: 頁処理結果台帳 reporter（契約 §5.6・`jobs/{job_key}/page_outcomes/{page_id}`）。

Plan: docs/plans/2026-08-01-b7-step2-headless-merge.md §3 T2/T3。
役割分担：
    - 本モジュール＝**頁粒度**の外部可視化（控制面 reconciliation の完了判定材料）。
    - 檔級状態回報（POSTED/DEAD_LETTER）は firestore_report.py が担う——二重回報を
      作らない。file_started/file_finished 相当の義務は本モジュールには無い。
    - Sheets 進捗タブ（page_progress.py）＝人向け可視化・main 由来。headless 既定
      では接続しない（Plan §2 非目標）。

outcome 権威＝「該頁の最終帳務身分」（Codex 阻斷2 裁決）：
    POSTED_PRIOR/PLACEHOLDER_PRIOR は ledger の既確認事実をそのまま運び、本輪の
    OCR 初判では上書きしない。分類 drift は reason（classification_drift）で残す。

ESCALATE は書かない（Plan §9 #14 裁決）：
    行の欠落＝「未決」の正しい信号。FAILED と書くと「処理済・記帳失敗」と誤読され、
    件級 POST_UNKNOWN の人工核信号と矛盾する。

degrade（Plan §9 #3/#9 裁決＝有限重試）：
    頁時の書込は 1 回だけ試し、失敗は pending へ退避（頁処理は絶対に落とさない）。
    檔終局（report_posted の前）に flush_pending() で補写一輪、なお失敗は放行。
    残余リスクは過渡期並集判定（page_outcomes ∪ postings CONFIRMED）が POSTED 頁を
    兜底する。多段 backoff は置かない（best-effort 通道に不相称）。

reason は closed 白名単のみ（Codex 高2）：
    自由文字（檔名/客户名を含み得る）は REASON_UNCLASSIFIED へ降級して不透過。
    日誌白名単制（basic-design/03 §3）と同方針。
"""
from __future__ import annotations

from page_progress import (
    OUTCOME_EXCLUDED, OUTCOME_FAILED, OUTCOME_PLACEHOLDER, OUTCOME_POSTED,
    utc_now,
)
from posting_ledger import derive_page_id

# Firestore set() の明示 timeout 秒（SDK 既定の長い deadline で頁処理を
# 停滞させない、Codex 中3）。fake は無視する。
SET_TIMEOUT_SECONDS = 10

REASON_UNCLASSIFIED = "unclassified"

# kind →（§5.6 outcome, 既定 reason）。単一真相源（R6）——
# _classify_and_flush_page が返し得る全 kind（ESCALATE を除く）と一対一。
KIND_OUTCOME_MAP = {
    "POSTED_NOW": (OUTCOME_POSTED, "posted_now"),
    "POSTED_PRIOR": (OUTCOME_POSTED, "prior_confirmed"),
    "PLACEHOLDER_WRITTEN": (OUTCOME_PLACEHOLDER, "placeholder_written"),
    "PLACEHOLDER_PRIOR": (OUTCOME_PLACEHOLDER, "prior_confirmed"),
    "EXCLUDED": (OUTCOME_EXCLUDED, "excluded"),
    "RETRYABLE": (OUTCOME_FAILED, "page_error_retryable"),
    "UNKNOWN": (OUTCOME_FAILED, "page_error_unknown"),
}

# detail（呼出側が添える機械キー）→ closed reason への翻訳表。
# ここに無い detail は REASON_UNCLASSIFIED へ降級（自由文字不透過）。
_DETAIL_REASON_MAP = {
    "envelope": "excluded_envelope",
    "social_insurance_notice": "excluded_social_insurance",
    "content_unreadable": "content_unreadable",
    "classification_drift": "classification_drift",
}


class FirestorePageOutcomesReporter:
    """1 ファイル＝1 インスタンス（job_key＝base、posting_ledger と同前提）。

    record_page() は頁決算点（_record_page_classification）から恰好一回、
    flush_pending() は檔終局（report_posted の前）から一回呼ばれる。
    どちらも例外を呼出側へ伝播させない。
    """

    def __init__(self, client, base):
        # client には初回書込まで触れない（構築は base 確定直後＝檔処理の
        # 入口で行われるため、ここで client を叩くと除外/エラー早退の檔でも
        # 無条件に Firestore へ往復してしまう）。
        self._client = client
        self._base = base
        self._pending: dict[int, tuple[str, dict]] = {}
        self._warned = False

    def record_page(self, page_num, kind, detail=None):
        """頁決算 1 件を §5.6 行へ映射して書く（失敗は pending へ退避）。"""
        mapped = KIND_OUTCOME_MAP.get(kind)
        if mapped is None:
            # 未知 kind＝outcome を決められない→不写＋警告。静默で POSTED 等に
            # 化けさせない（R6）。ESCALATE も意図的にここに落ちる（不写裁決）。
            if kind != "ESCALATE":
                print(f"⚠️ page_outcomes: 未知 kind '{kind}' p{page_num} → 不記録")
            return
        outcome, default_reason = mapped
        if detail is None:
            reason = default_reason
        else:
            reason = _DETAIL_REASON_MAP.get(detail, REASON_UNCLASSIFIED)
        page_id = derive_page_id(self._base, page_num)
        doc = {"page": page_num, "outcome": outcome, "reason": reason,
               "written_at": utc_now()}
        self._try_set(page_num, page_id, doc)

    def flush_pending(self):
        """檔終局の補写一輪。未回収の残数を返す（例外は出さない）。"""
        retry = dict(self._pending)
        self._pending = {}
        for page_num, (page_id, doc) in retry.items():
            self._try_set(page_num, page_id, doc)
        remaining = len(self._pending)
        if remaining:
            print(f"⚠️ page_outcomes: 補写後も {remaining} 頁未記録のまま放行"
                  f"（reconciler 側で欠落として扱われる）")
        return remaining

    def _try_set(self, page_num, page_id, doc):
        try:
            (self._client.collection("jobs").document(self._base)
             .collection("page_outcomes").document(page_id)
             .set(doc, timeout=SET_TIMEOUT_SECONDS))
        except Exception as e:
            # 頁処理を絶対に落とさない。初回のみ日誌（毎頁の連呼を避ける）。
            if not self._warned:
                print(f"⚠️ page_outcomes 書込失敗（檔終局に補写します）: "
                      f"{type(e).__name__}")
                self._warned = True
            self._pending[page_num] = (page_id, doc)
