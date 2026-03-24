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
