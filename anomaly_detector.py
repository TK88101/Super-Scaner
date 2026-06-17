# anomaly_detector.py — 異常検出モジュール
import re
from config import UNKNOWN_ACCOUNT
from receipt_aggregation import coerce_tax_amount, TOTAL_MISMATCH_TOLERANCE_YEN


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


# TOTAL_MISMATCH_TOLERANCE_YEN は receipt_aggregation から import（[B'] 合計照合と
# tax_summary 回退ガードで共用する単一の真実源。許容差の語義は import 元を参照）。

# 規則①（対象外行の構造異常）用の定数。
# EXEMPT_TAX_TYPE: determine_tax_types が rate=0 で固定出力する税区分ラベル。
#   精確等値で判定する（"課対仕入…" も「対」を含むため in/含「対」は使わない）。
EXEMPT_TAX_TYPE: str = "対象外"
# EXEMPT_RATIO_THRESHOLD: 対象外金額合計 / 票面合計 の上限（規則①b）。
EXEMPT_RATIO_THRESHOLD: float = 0.5
# EXEMPT_ABSOLUTE_THRESHOLD_YEN: 単個対象外行の絶対閾値（規則①c）。
EXEMPT_ABSOLUTE_THRESHOLD_YEN: int = 50000


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


def _exempt_outlier_triggered(
    max_exempt: int,
    sum_taxable: int,
    sum_exempt_positive: int,
    total: int,
) -> bool:
    """規則①の3閾値（OR）を評価する。命中で True。

    a) 単個の対象外行が課税行合計を上回る（taxable 空=分母0なら除零回避でスキップ）。
    b) 正額対象外の合計が票面合計の EXEMPT_RATIO_THRESHOLD を超える
       （total 欠損=0、または taxable 空=純税費小票 ならスキップ）。
    c) 単個の対象外行が EXEMPT_ABSOLUTE_THRESHOLD_YEN を超える（無依存の兜底）。

    NOTE: a/b はいずれも taxable>0 を前提にする。純税費小票（印紙税・利用税の
    みで課税本体が無い票）は対象外比率が当然 100% になるため、taxable 空では
    比率判定をスキップし c（絶対額）だけで守る（誤報防止）。
    """
    if sum_taxable > 0 and max_exempt > sum_taxable:
        return True
    if (sum_taxable > 0 and total > 0
            and sum_exempt_positive / total > EXEMPT_RATIO_THRESHOLD):
        return True
    return max_exempt > EXEMPT_ABSOLUTE_THRESHOLD_YEN


def detect_outlier_exempt_rows(
    row_amounts: list,
    row_tax_types: list,
    total_amount: object = None,
) -> list:
    """規則①: 対象外（rate=0）行の構造的不合理を検出する純関数。

    Σ行金額==票面合計を満たす幻覚（六角堂: 課税7,310 + 対象外90,000 で
    Σ=票面=97,310）は detect_document_anomalies を素通りするため、税区分の
    構造（debit_tax_type 精確等値 "対象外"）から不合理な対象外行を拾う。
    row_amounts と row_tax_types は同序・同長。命中時は 1 件 high flag（I列）。
    非 list / 長さ不一致 / 対象外行なし / 正額対象外ゼロ（純返品）は空を返す。
    """
    if not isinstance(row_amounts, list) or not isinstance(row_tax_types, list):
        return []
    if not row_amounts or len(row_amounts) != len(row_tax_types):
        return []

    paired = list(zip(row_amounts, row_tax_types))
    exempt = [coerce_tax_amount(a) for a, t in paired
              if str(t).strip() == EXEMPT_TAX_TYPE]
    taxable = [coerce_tax_amount(a) for a, t in paired
               if str(t).strip() and str(t).strip() != EXEMPT_TAX_TYPE]
    if not exempt:
        return []

    max_exempt = max((x for x in exempt if x > 0), default=0)
    if max_exempt == 0:  # 全額が負（返品・値引）の対象外は誤判しない
        return []

    sum_taxable = sum(x for x in taxable if x > 0)
    sum_exempt_positive = sum(x for x in exempt if x > 0)
    total = coerce_tax_amount(total_amount)
    if not _exempt_outlier_triggered(
            max_exempt, sum_taxable, sum_exempt_positive, total):
        return []

    return [{
        "type": "outlier_exempt_row",
        "message": (f"対象外¥{max_exempt:,} が課税¥{sum_taxable:,}/"
                    f"票面50%/5万円基準を超過"),
        "severity": "high",
        "col": 8,
    }]


def detect_low_confidence(parent_data: object, threshold: float) -> list:
    """規則②: 整票の OCR 置信度が低い場合に人手複査フラグを返す純関数。

    Args:
        parent_data: doc 級 dict。ocr_confidence（PaddleOCR 平均置信度）を参照。
        threshold: 黄マークの閾値（厳密小なり）。記账复核门槛（config 注入）。

    Returns:
        list[dict]: conf < threshold で 1 件の low（full_row）flag。
        ocr_confidence の欠損・None・非数値は無信号として空リスト
        （Gemini-Vision 兜底票を誤報しない）。
    """
    conf = (parent_data or {}).get("ocr_confidence")
    if conf is None:
        return []
    try:
        conf_val = float(conf)
    except (TypeError, ValueError):
        return []
    if conf_val >= threshold:
        return []
    return [{
        "type": "low_confidence",
        "message": f"低置信・人工複査推奨（OCR置信度 {conf_val:.2f}）",
        "severity": "low",
        "full_row": True,
    }]


def _is_valid_t_number(raw):
    """T番号の形式チェック（サニタイズ前の生値）"""
    if not raw:
        return True
    s = str(raw).strip().replace("-", "")
    if s.startswith("t"):
        s = "T" + s[1:]
    return bool(re.match(r'^T\d{13}$', s))
