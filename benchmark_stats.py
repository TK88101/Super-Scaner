"""B5 benchmark の統計層（分位数・層別集計）。

Plan＝docs/plans/2026-08-10-b5-benchmark-ss.md §4.1／§5 T1・T2。

設計上の二つの決定（いずれも Codex 対抗評審の裁決事項）：

1. **分位数は nearest-rank に固定**し、標本量が `P99_MIN_SAMPLES` 未満なら
   P99 を名乗らない。理由は数学的：nearest-rank の P99 は
   `ceil(0.99 * N)` 番目であり、N < 100 では常に N 番目＝**最大値へ退化**する。
   99 件の「P99」は最大値の別名でしかなく、尾部の推定になっていない。
   真票の供給量は物理的に有限なので、同一標本の反復では独立観測を作れない
   ——不足時は P50・max・標本数だけを誠実に出す（評審 #8・複審認可）。

2. **層別集計は入力試行数を保存する**。記帳に至らなかった頁（除外・占位・
   再試行・不明・退避）も OCR と Gemini のコストを払っており、それらを
   母数から落とすと成功者バイアスのかかった分布になる。とりわけ
   MAX_TOKENS や配額切れの遅い失敗こそ stall_threshold が覆うべき尾部
   （評審 #9）。よって未知の終態は黙って捨てず例外にする。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# P99 を名乗ってよい最小標本数。N=100 で初めて ceil(0.99*N)=99 となり
# P99 が最大値（100 番目）と分離する——この分離点が閾値の根拠。
P99_MIN_SAMPLES = 100

# 層別軸＝**系統既存の頁級 kind**（main 内部タグ。firestore_progress.KIND_OUTCOME_MAP
# の鍵と同一）。benchmark が独自の語彙を発明していないことは
# test_benchmark_stats.OutcomeVocabularyTest が担保する。
#
# なぜ契約 §5.6 の outcome（4 値）ではなく kind（7 値）で層別するか：
# 契約 outcome は RETRYABLE と UNKNOWN を共に FAILED へ畳むが、
# stall_threshold が覆うべき尾部はまさにこの二つの区別にある——遅い失敗
# （MAX_TOKENS・配額切れ）と帰結不明では待ち方が違う（評審 #9）。
# 報告では両方出す：kind で層別し、`to_contract_outcome` で契約語彙へも畳む。
OUTCOMES = (
    "POSTED_NOW",           # 今回記帳した
    "POSTED_PRIOR",         # 前輪で記帳済（重跑時）
    "PLACEHOLDER_WRITTEN",  # 占位行を今回書いた
    "PLACEHOLDER_PRIOR",    # 占位行が前輪で書かれていた
    "EXCLUDED",             # 除外頁（封筒・社会保険料通知書）——仕訳も占位も作らない
    "RETRYABLE",            # 再試行可能な頁エラー
    "UNKNOWN",              # 帰結不明な頁エラー
    "ESCALATE",             # 人手へ退避（契約 outcome を持たない。下記参照）
)

# ESCALATE は KIND_OUTCOME_MAP に無い——`firestore_progress.py:88-89` の裁決で
# page_outcomes へ**意図的に書かない**（契約 §5.6 に対応 outcome が無い）。
# しかしその頁は OCR も Gemini も実際に払っているので、benchmark の母数からは
# 外さない。契約語彙へ畳む時だけ None になる。
_NO_CONTRACT_OUTCOME = frozenset({"ESCALATE"})


def to_contract_outcome(kind: str) -> str | None:
    """頁級 kind を契約 §5.6 の outcome（4 値）へ畳む。

    ESCALATE は契約上そもそも記録されないので None を返す（「畳めない」のでは
    なく「畳む先が定義されていない」）。単一真相源は firestore_progress 側なので
    値を写経せず遅延 import で引く（本モジュールは純粋な統計層に保つ）。
    """
    if kind in _NO_CONTRACT_OUTCOME:
        return None

    from firestore_progress import KIND_OUTCOME_MAP

    try:
        return KIND_OUTCOME_MAP[kind][0]
    except KeyError:
        raise ValueError(
            f"未知の kind: {kind!r}。契約 §5.6 outcome へ畳めない"
        ) from None


@dataclass(frozen=True)
class QuantileReport:
    """一つの標本群の分位数要約。

    p99 は名乗ってよい場合のみ値が入る（不足時は None かつ
    p99_reportable=False）。報告側はこのフラグを見て文言を変える。
    """

    sample_count: int
    p50: float | None
    p99: float | None
    p99_reportable: bool
    max_value: float | None


@dataclass(frozen=True)
class StratifiedResult:
    """終態ごとの要約と、全体の要約。

    strata は OUTCOMES 全てをキーに持つ（該当ゼロ件でも枠を出す——
    「その終態が一件も出なかった」ことも情報だから）。
    """

    strata: dict[str, QuantileReport]
    overall: QuantileReport


def nearest_rank(values, percentile: int) -> float | None:
    """nearest-rank 方式の分位数。

    定義：昇順に並べた N 件のうち `ceil(percentile/100 * N)` 番目（1 始まり）。
    補間はしない——実測値そのものを返すので「存在しない所要時間」を報告に
    載せずに済む。

    空なら None。
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(percentile / 100 * len(ordered))
    # percentile>0 なら rank>=1 だが、丸め誤差での 0 を防ぐ
    index = max(1, rank) - 1
    return ordered[index]


def summarize(values) -> QuantileReport:
    """標本群を要約する。P99 は標本量が足りる時だけ値を入れる。"""
    count = len(values)
    reportable = count >= P99_MIN_SAMPLES
    return QuantileReport(
        sample_count=count,
        p50=nearest_rank(values, 50),
        p99=nearest_rank(values, 99) if reportable else None,
        p99_reportable=reportable,
        max_value=max(values) if values else None,
    )


def stratify(records) -> StratifiedResult:
    """試行レコード列を終態で層別し、全体分布も併せて返す。

    records の各要素は少なくとも `outcome`（OUTCOMES のいずれか）と
    `elapsed_sec`（float 秒）を持つ dict。

    未知の終態は ValueError。静かに無視すると母数が欠け、
    「入力試行数 == 出力レコード数」の不変式が破れる。
    """
    buckets: dict[str, list] = {outcome: [] for outcome in OUTCOMES}
    all_values: list = []

    for record in records:
        outcome = record["outcome"]
        if outcome not in buckets:
            raise ValueError(
                f"未知の終態: {outcome!r}（既知＝{', '.join(OUTCOMES)}）。"
                "母数が欠けるので握り潰さない"
            )
        elapsed = record["elapsed_sec"]
        buckets[outcome].append(elapsed)
        all_values.append(elapsed)

    return StratifiedResult(
        strata={outcome: summarize(values) for outcome, values in buckets.items()},
        overall=summarize(all_values),
    )
