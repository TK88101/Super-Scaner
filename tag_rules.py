# tag_rules.py — U列(タグ)向け 色→文字タグ導出（純関数・gspread非依存）
#
# 目的（社長要望 6/18）: 行のいずれかのセルが標色された場合、対応する U列(タグ列)
# に「赤系/橙系/黄系」を自動記入する。従業員は人手確認後に当該タグ文字を削除して
# から MF へアップロードする。削除を怠ると MF の Tag 列に文字が残るため、審査側が
# 「未確認のままアップロードした」と一目で検出できる（抑止メカニズム）。
#
# 色は anomaly_detector の severity フラグ（high=赤/medium=橙/low=黄）から決まるため、
# セル背景色を読み戻す必要はなく、同じ severity からタグ文字を導出する（色ブレ防止）。

# severity の正典テーブル（単一の真実源）: label → (rank, tag)。
# rank が高いほど深刻。色塗りは黄→橙→赤の順で上書きされ各行は「最高 severity の色」
# になるため、タグも最高 severity 1 個に揃える（社長確認済み）。新しい severity を
# 追加する場合はここだけを変更する（rank と tag の二重管理・乖離を避ける）。
SEVERITY_LEVELS = (
    ("low", 1, "黄系"),
    ("medium", 2, "橙系"),
    ("high", 3, "赤系"),
)

SEVERITY_RANK = {label: rank for label, rank, _ in SEVERITY_LEVELS}
RANK_TAG = {rank: tag for _, rank, tag in SEVERITY_LEVELS}

# 認識不能/部分認識ページは常に赤（_write_unrecognized_row で共用）。
UNRECOGNIZED_TAG = RANK_TAG[SEVERITY_RANK["high"]]


def severity_rank(severity: str) -> int:
    """severity ラベルをランクに変換（未知値は最弱の low 扱い）。"""
    return SEVERITY_RANK.get(severity, SEVERITY_RANK["low"])


def rank_to_tag(rank: int) -> str:
    """ランクをタグ文字に変換（0=無標色は空文字）。"""
    return RANK_TAG.get(rank, "")


def derive_row_tags(num_rows: int,
                    per_entry_flags: list,
                    doc_low_confidence: bool = False,
                    doc_red: bool = False) -> list:
    """各行の U列タグ文字を導出する（新規 list を返す。入力は変更しない）。

    Args:
        num_rows: 書き込む行数。
        per_entry_flags: [(offset, flags), ...]。offset は 0始まりの行位置、
            flags は detect_anomalies が返す dict のリスト（各 dict に severity）。
        doc_low_confidence: 低置信で全行が黄塗りされる場合 True。
        doc_red: 合計不符/規則①で全行 I列が赤塗りされる場合 True。

    Returns:
        list[str]: 各行のタグ文字。標色なしの行は "" 。
        各行は「per-entry フラグの最高 severity」と「doc 級シグナル」の最大を採る。
    """
    base_rank = 0
    if doc_low_confidence:   # 低置信 → 全行 黄
        base_rank = max(base_rank, SEVERITY_RANK["low"])
    if doc_red:              # 合計不符/規則① → 全行 I列 赤
        base_rank = max(base_rank, SEVERITY_RANK["high"])

    ranks = [base_rank] * num_rows
    for offset, flags in per_entry_flags:
        if not (0 <= offset < num_rows):
            continue
        for flag in flags:
            ranks[offset] = max(ranks[offset],
                                severity_rank(flag.get("severity", "low")))

    return [rank_to_tag(rank) for rank in ranks]
