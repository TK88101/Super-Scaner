# anomaly_detector.py — 異常検出モジュール
import re


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

    # 金額 > 10万 → 要確認
    amount = entry.get("amount", 0)
    try:
        amount = int(amount)
    except (ValueError, TypeError):
        amount = 0

    if amount > 100000:
        flags.append({
            "type": "high_amount",
            "message": f"金額が10万円を超えています: ¥{amount:,}",
            "severity": "low",
            "col": 8,  # 借方金額(円) = I列 (0始まり index 8)
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

    # 取引先が空
    vendor = parent_data.get("vendor", "")
    if not vendor:
        flags.append({
            "type": "missing_vendor",
            "message": "取引先が空です",
            "severity": "medium",
            "col": 5,  # 借方取引先 = F列
        })

    # T番号がサニタイズで除去された
    raw_invoice = parent_data.get("invoice_num", "")
    if raw_invoice and not _is_valid_t_number(raw_invoice):
        flags.append({
            "type": "invalid_t_number",
            "message": f"T番号が不正な形式のため除去: {raw_invoice}",
            "severity": "medium",
            "col": 7,  # 借方インボイス = H列
        })

    return flags


def _is_valid_t_number(raw):
    """T番号の形式チェック（サニタイズ前の生値）"""
    if not raw:
        return True
    s = str(raw).strip().replace("-", "")
    if s.startswith("t"):
        s = "T" + s[1:]
    return bool(re.match(r'^T\d{13}$', s))
