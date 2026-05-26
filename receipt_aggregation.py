"""領収書の仕訳エントリを税率別合計に集約するロジック（5/25 仕様変更）。

会議(5/25)で確定した仕様:
- 領収書(レシート/發票)は明細を逐行出力せず、税率(8%/10%)別に1行ずつ合計を出力する。
- 総合計行は出力しない。
- 単一税率のみの場合は合計1行のみ出力する。
- 對象は領収書フォルダの書類（DocType.RECEIPT）のみ。請求書・給与明細等は従来通り。
  NOTE: 領収書フォルダは PDF/画像の両方が _build_entries_for_single_doc を通るため、
        本集約は両方に適用される（顧客の運用上ほぼ PDF）。

外部依存（gemini/paddleocr 等）を持たない純粋ロジックとして切り出し、単体テスト可能にする。
"""

# 集約後の借方勘定科目の決定戦略
#   "max_amount": 同一税率グループ内で金額最大の品目の科目を採用（既定）
#   "fixed":      AGGREGATED_DEBIT_ACCOUNT_FIXED に固定
# NOTE: 顧客の最終確認待ち。確定後はこの定数を切り替えるだけでよい。
AGGREGATED_DEBIT_ACCOUNT_STRATEGY = "max_amount"
AGGREGATED_DEBIT_ACCOUNT_FIXED = "備品・消耗品費"

DEFAULT_TAX_RATE = 0.10
DEFAULT_CREDIT_ACCOUNT = "未払金"
DEFAULT_CREDIT_TAX_TYPE = "対象外"


def coerce_tax_rate(value):
    """tax_rate を float に正規化する。

    OCR/Gemini は数値(0.08/0.10/0)を返す想定だが、実際には null や
    文字列("0.08") を返すことがある。None・bool・変換不能な値は既定税率(10%)に
    寄せ、_determine_tax_types の fallback(=10%) と整合させる。
    取得元で正規化しておくことで、税区分(debit_tax_type)と集約グループの
    税率が食い違わないようにする。
    """
    if value is None or isinstance(value, bool):
        return DEFAULT_TAX_RATE
    try:
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_TAX_RATE


def _select_aggregated_debit_account(group_entries):
    """税率グループの代表借方科目を決定する。"""
    if AGGREGATED_DEBIT_ACCOUNT_STRATEGY == "fixed":
        return AGGREGATED_DEBIT_ACCOUNT_FIXED
    # 既定: グループ内で金額最大の品目の科目を採用
    largest = max(group_entries, key=lambda e: e.get("amount", 0))
    return largest.get("debit_account", AGGREGATED_DEBIT_ACCOUNT_FIXED)


def _format_tax_label(tax_rate):
    """集約行の摘要に使う税率ラベルを生成する。

    NOTE: 店名は付けない。sheets_output 側で「{店名} - {description}」と
    前置されるため、ここで店名を入れると二重になる。
    """
    return f"{int(round(tax_rate * 100))}%対象" if tax_rate else "対象外"


def aggregate_entries_by_tax_rate(entries):
    """明細エントリを税率(8%/10%/対象外)別の合計エントリに集約する。

    Args:
        entries: 明細単位の仕訳エントリ list。各 dict は debit_account /
            debit_tax_type / credit_account / credit_tax_type / amount /
            description / tax_rate を持つ。

    Returns:
        税率ごとに1行集約した仕訳エントリ list。複数税率なら税率分の行、
        単一税率なら1行のみ。合計(総和)行は出力しない。摘要(description)は
        税率ラベルのみ。店名は sheets_output 側で前置される。
    """
    if not entries:
        return entries

    # 出現順を保持しつつ税率でグループ化（dict は挿入順を保持）
    # 上流で正規化済みだが、直接呼び出しや異常値に備え防御的に coerce する。
    groups = {}
    for entry in entries:
        rate = coerce_tax_rate(entry.get("tax_rate"))
        groups.setdefault(rate, []).append(entry)

    aggregated = []
    for rate, group in groups.items():
        total_amount = sum(int(e.get("amount", 0)) for e in group)
        if total_amount == 0:
            continue

        # 税区分・貸方科目はグループ内で一致するため先頭から採用
        head = group[0]
        aggregated.append({
            "debit_account": _select_aggregated_debit_account(group),
            "debit_tax_type": head.get("debit_tax_type", ""),
            "credit_account": head.get("credit_account", DEFAULT_CREDIT_ACCOUNT),
            "credit_tax_type": head.get("credit_tax_type", DEFAULT_CREDIT_TAX_TYPE),
            "amount": total_amount,
            "description": _format_tax_label(rate),
        })

    return aggregated
