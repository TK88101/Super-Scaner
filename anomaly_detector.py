# anomaly_detector.py — 異常検出モジュール
import re
from config import UNKNOWN_ACCOUNT
from receipt_aggregation import coerce_tax_amount


def detect_anomalies(entry, parent_data=None):
    """
    仕訳エントリの異常を検出する。

    Args:
        entry: 仕訳エントリ dict (amount, debit_account, etc.)
        parent_data: 親データ dict (date, vendor, invoice_num, etc.)

    Returns:
        list[dict]: 異常フラグのリスト。各フラグは {"type", "message", "severity"} 形式。
        severity: "high" (赤), "medium" (オレンジ), "low" (黄)
    """
    flags = []
    parent_data = parent_data or {}

    amount = entry.get("amount", 0)
    try:
        amount = int(amount)
    except (ValueError, TypeError):
        amount = 0

    debit_account = entry.get("debit_account", "")

    # 科目別ハイライトルール（顧客確認済み）
    # 地代家賃・保険料・雑収入: 金額に関係なく全件ハイライト
    always_highlight = ["地代家賃", "保険料", "雑収入"]
    if debit_account in always_highlight:
        flags.append({
            "type": "account_review",
            "message": f"{debit_account}: 要確認科目です（¥{amount:,}）",
            "severity": "low",
            "col": 8,
        })

    # 修繕費 > 30万
    if debit_account == "修繕費" and amount > 300000:
        flags.append({
            "type": "high_amount",
            "message": f"修繕費が30万円を超えています: ¥{amount:,}",
            "severity": "low",
            "col": 8,
        })

    # 租税公課: 宿泊税・軽油税を含む場合（伊藤提案 3/24）
    if debit_account == "租税公課":
        desc = entry.get("description", "")
        for keyword in ("宿泊税", "軽油税"):
            if keyword in desc:
                flags.append({
                    "type": "tax_review",
                    "message": f"租税公課に{keyword}が含まれています（¥{amount:,}）",
                    "severity": "low",
                    "full_row": True,
                })
                break

    # 未確定勘定: debit_account fallback or CREDIT_ONLY_ACCOUNTS replacement
    if debit_account == UNKNOWN_ACCOUNT:
        flags.append({
            "type": "undetermined_account",
            "message": f"未確定勘定です（¥{amount:,}）— 手動で科目を確定してください",
            "severity": "medium",
            "full_row": True,
        })

    # 備品・消耗品費 > 10万
    if debit_account == "備品・消耗品費" and amount > 100000:
        flags.append({
            "type": "high_amount",
            "message": f"備品・消耗品費が10万円を超えています: ¥{amount:,}",
            "severity": "low",
            "col": 8,
        })

    # 日付が空
    date_val = parent_data.get("date", "")
    if not date_val:
        flags.append({
            "type": "missing_date",
            "message": "取引日が空です",
            "severity": "high",
            "col": 1,  # 取引日 = B列
        })

    # 日付が月のみ（YYYY/MM、日なし）→ 高亮で要確認
    elif re.match(r'^\d{4}/\d{2}$', str(date_val)):
        flags.append({
            "type": "partial_date",
            "message": f"日付に日が未記載です: {date_val}",
            "severity": "low",
            "col": 1,
        })

    # 取引先が空
    vendor = parent_data.get("vendor", "")
    if not vendor:
        flags.append({
            "type": "missing_vendor",
            "message": "取引先が空です",
            "severity": "medium",
            "col": 5,  # 借方取引先 = F列
        })

    # T番号が空（適格請求書なし）
    raw_invoice = parent_data.get("invoice_num", "")
    if not raw_invoice or not raw_invoice.strip():
        flags.append({
            "type": "missing_invoice",
            "message": "T番号が空です",
            "severity": "low",
            "col": 7,  # 借方インボイス = H列
        })
    elif not _is_valid_t_number(raw_invoice):
        # T番号があるが形式不正（サニタイズで除去された）
        flags.append({
            "type": "invalid_t_number",
            "message": f"T番号が不正な形式のため除去: {raw_invoice}",
            "severity": "medium",
            "col": 7,
        })

    return flags


# 票面合計とΣ行金額の照合の許容差（円）。外税→税込換算（ROUND_HALF_UP 兜底）
# で税率グループごとに±1円の丸め差が生じうるため、外税2グループ分の2円まで許容
TOTAL_MISMATCH_TOLERANCE_YEN = 2


def detect_document_anomalies(parent_data, row_amounts):
    """文書（票）単位の異常を検出する。

    detect_anomalies が逐 entry 検査なのに対し、本関数は1票の書き込み行全体を
    対象とする（sheets_output.append_entries から票ごとに1回呼ばれる）。
    現状は票面合計 vs Σ行金額の照合のみ（6/12 静默錯対策: D票=外税換算漏れ、
    C票=幻覚行 を機械検出する）。

    Args:
        parent_data: doc 級 dict。total_amount（票面の税込合計）を参照する。
        row_amounts: 実際に書き込む行の借方金額 list（amount==0 行は除外済み）。

    Returns:
        list[dict]: 異常フラグ（detect_anomalies と同形式）。
        total_amount が欠損・0・非数値の場合は照合せず空リストを返す
        （誤報防止。スキップの可視化は呼び出し側 sheets_output が行う）。
    """
    total = coerce_tax_amount((parent_data or {}).get("total_amount"))
    if not total or not row_amounts:
        return []
    row_sum = sum(row_amounts)
    diff = row_sum - total
    if abs(diff) <= TOTAL_MISMATCH_TOLERANCE_YEN:
        return []
    return [{
        "type": "total_mismatch",
        "message": (f"票面合計¥{total:,} ≠ 行合計¥{row_sum:,}"
                    f"（差額 ¥{diff:+,}）"),
        "severity": "high",
        "col": 8,
    }]


def _is_valid_t_number(raw):
    """T番号の形式チェック（サニタイズ前の生値）"""
    if not raw:
        return True
    s = str(raw).strip().replace("-", "")
    if s.startswith("t"):
        s = "T" + s[1:]
    return bool(re.match(r'^T\d{13}$', s))
