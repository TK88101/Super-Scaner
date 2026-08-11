"""B5 benchmark の生データ層（JSONL 落盘 ＋ 機械検証）。

Plan＝docs/plans/2026-08-10-b5-benchmark-ss.md §4.5／§5 T6。

評審 #10・#18 の裁決：DoD を「報告の欄が埋まった」で判定してはいけない。
標本 1 件でも、全件失敗でも、単位が違っても、worker が殺された後に古い結果を
流用しても、文言上の DoD は満たせてしまう。だから**機械判定可能な述語**を
validator として持ち、報告はその生データから導く。

Markdown の golden 文字列テストは作らない（評審 #21）——書式を直すたびに
無意味に落ちるだけで、守りたいのは書式ではなく数字の出所だから。
"""

from __future__ import annotations

import json
import math

import benchmark_stats as bs

# validator が実在を要求するメタ欄（欠けたら報告の再現性が失われる）
REQUIRED_META = ("git_sha", "platform", "headless_mode", "attempted",
                 "quantile_algorithm", "p99_min_samples")


def write_jsonl(path: str, records) -> None:
    """1 行 1 レコードで書く。日本語パスを壊さない（ensure_ascii=False）。"""
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: str) -> list:
    """write_jsonl の逆。空行は読み飛ばす。"""
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def validate_records(records, meta) -> list[str]:
    """生データとメタを検査し、問題を**全部**集めて返す（空なら合格）。

    最初の 1 件で止めない——実測は時間もお金もかかるので、直すべき点は
    一度に全部見せる。
    """
    issues: list[str] = []

    for key in REQUIRED_META:
        if key not in meta:
            issues.append(f"メタ欄 {key} が無い（再現性を担保できない）")

    if meta.get("headless_mode") is not True:
        issues.append(
            "HEADLESS_MODE が真でない状態の測定値は使えない"
            "（producer 側が UI 挙動のままの混合経路になる）")

    attempted = meta.get("attempted")
    if attempted is not None and attempted != len(records):
        issues.append(
            f"試行数が合わない: attempted={attempted} / レコード={len(records)}。"
            "試行が黙って母数から消えている")

    for index, record in enumerate(records):
        where = f"[{index}] {record.get('input_path', '?')}"

        outcome = record.get("outcome")
        if outcome not in bs.OUTCOMES:
            issues.append(f"{where}: 未知の終態 {outcome!r}")

        elapsed = record.get("elapsed_sec")
        if elapsed is None or not isinstance(elapsed, (int, float)) \
                or math.isnan(elapsed) or elapsed < 0:
            issues.append(f"{where}: elapsed_sec が不正 ({elapsed!r})")

        page_count = record.get("page_count")
        if page_count != 1:
            issues.append(
                f"{where}: 頁数が {page_count}（契約 headless の入力は 1 頁／1 切片）")

    return issues
