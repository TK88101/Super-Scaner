import os
import json
import re
import google.generativeai as genai
from dotenv import load_dotenv
from doc_types import DocType, DOC_TYPE_CONFIG

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("⚠️ 重大エラー: GEMINI_API_KEYが見つかりません")

genai.configure(api_key=api_key)
model = genai.GenerativeModel('gemini-2.0-flash')


# ============================================================
# 共通ユーティリティ
# ============================================================

def verify_tax_math(candidates, rate):
    """数学的検証ロジック (V3.0)"""
    nums = []
    for c in candidates:
        try:
            clean_str = str(c).replace(',', '').replace('¥', '').replace('円', '').strip()
            clean_num = float(clean_str)
            nums.append(clean_num)
        except:
            continue

    nums = sorted(list(set(nums)), reverse=True)

    for amount in nums:
        for tax in nums:
            if amount <= tax:
                continue
            # パターンA: 税抜
            if abs(amount * rate - tax) <= 2.0:
                return int(amount), int(tax)
            # パターンB: 税込
            expected_net = amount / (1 + rate)
            if abs((amount - expected_net) - tax) <= 2.0:
                return int(amount - tax), int(tax)

    return None, None


def extract_json(text):
    """JSON抽出強化関数"""
    try:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _get_mime_type(file_path):
    """ファイル拡張子からMIMEタイプを判定"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in ('.jpg', '.jpeg'):
        return "image/jpeg"
    elif ext == '.png':
        return "image/png"
    elif ext == '.heic':
        return "image/heic"
    elif ext == '.pdf':
        return "application/pdf"
    return "image/jpeg"


def _call_gemini(file_path, prompt):
    """Gemini API を呼び出して JSON を返す"""
    with open(file_path, "rb") as f:
        file_data = f.read()
    mime_type = _get_mime_type(file_path)

    response = model.generate_content([
        {"mime_type": mime_type, "data": file_data},
        prompt
    ])
    return extract_json(response.text)


# ============================================================
# プロンプト定義
# ============================================================

PROMPTS = {
    DocType.RECEIPT: """
あなたはプロの経理担当者です。領収書・レシート画像を分析し、会計ソフト用データを抽出してください。

【出力JSONフォーマット】
{
    "date": "YYYY/MM/DD",
    "vendor": "取引先名",
    "invoice_num": "Tから始まる13桁番号 (なければ空文字)",
    "payment_method": "支払い方法 (現金, クレジットカード, PayPay, 振込)",
    "memo": "メモ",
    "description_raw": "品代",
    "debit_account": "費用の勘定科目を推定 (消耗品費, 旅費交通費, 交際費, 会議費, 通信費, 福利厚生費, 雑費 など)",
    "tax_8_area": { "candidates": ["8%エリアの全数字"] },
    "tax_10_area": { "candidates": ["10%エリアの全数字"] }
}

注意: debit_account は画像の内容から最も適切な費用科目を推定してください。
""",

    DocType.PURCHASE_INVOICE: """
あなたはプロの経理担当者です。支払請求書・仕入請求書を分析し、会計ソフト用データを抽出してください。

【出力JSONフォーマット】
{
    "date": "YYYY/MM/DD (請求日または発行日)",
    "vendor": "取引先名（請求元）",
    "invoice_num": "Tから始まる13桁の適格請求書番号 (なければ空文字)",
    "memo": "メモ",
    "items": [
        {
            "description": "品目・サービス名",
            "amount": 税抜金額(数値),
            "tax_rate": 税率(0.08 or 0.10),
            "tax_amount": 消費税額(数値),
            "debit_account": "費用の勘定科目を推定 (仕入高, 外注費, 消耗品費, 通信費 など)"
        }
    ],
    "total_amount": 合計金額(税込, 数値),
    "payment_method": "支払い方法 (振込, 口座振替, 現金 など)",
    "due_date": "支払期日 (あれば YYYY/MM/DD)"
}

注意:
- 複数品目がある場合はitems配列に全て含めてください
- 品目が1つの場合でもitems配列で返してください
- 金額は数値型(カンマなし)で返してください
""",

    DocType.SALES_INVOICE: """
あなたはプロの経理担当者です。売上請求書を分析し、会計ソフト用データを抽出してください。

【出力JSONフォーマット】
{
    "date": "YYYY/MM/DD (請求日または発行日)",
    "vendor": "取引先名（請求先・顧客名）",
    "invoice_num": "Tから始まる13桁の適格請求書番号 (なければ空文字)",
    "memo": "メモ",
    "items": [
        {
            "description": "品目・サービス名",
            "amount": 税抜金額(数値),
            "tax_rate": 税率(0.08 or 0.10),
            "tax_amount": 消費税額(数値)
        }
    ],
    "total_amount": 合計金額(税込, 数値)
}

注意:
- これは売上（収益）の請求書です。借方は売掛金、貸方は売上高になります
- 金額は数値型(カンマなし)で返してください
""",

    DocType.SALARY_SLIP: """
あなたはプロの経理担当者です。賃金台帳・給与明細書を分析し、会計ソフト用データを抽出してください。

【出力JSONフォーマット】
{
    "date": "YYYY/MM/DD (支給日)",
    "employee_name": "従業員名",
    "memo": "メモ (対象期間など)",
    "gross_salary": 総支給額(数値),
    "social_insurance": 社会保険料合計(数値, 健康保険+厚生年金+雇用保険),
    "income_tax": 所得税額(数値),
    "resident_tax": 住民税額(数値),
    "other_deductions": その他控除合計(数値, あれば),
    "net_salary": 差引支給額(数値)
}

注意:
- 金額は数値型(カンマなし)で返してください
- 各控除項目が0の場合は0と記載してください
- 社会保険料は健康保険料+厚生年金+雇用保険の合計値を記載してください
""",
}


# ============================================================
# エントリビルダー（各文書タイプの仕訳生成ロジック）
# ============================================================

def _build_entries_from_receipt(raw_data):
    """領収書データから仕訳エントリを生成"""
    entries = []
    debit_account = raw_data.get("debit_account", "消耗品費")

    # 貸方科目決定
    pay_method = str(raw_data.get("payment_method", "現金"))
    credit_account = "現金"
    if any(x in pay_method for x in ["クレジット", "Credit", "Card", "VISA", "Master"]):
        credit_account = "未払金"
    elif "振込" in pay_method:
        credit_account = "普通預金"

    # 8% 検証
    candidates_8 = raw_data.get("tax_8_area", {}).get("candidates", [])
    amount_8, tax_8 = verify_tax_math(candidates_8, 0.08)
    if amount_8:
        entries.append({
            "debit_account": debit_account,
            "debit_tax_type": "課対仕入8% (軽)",
            "credit_account": credit_account,
            "credit_tax_type": "対象外",
            "amount": amount_8,
            "description": raw_data.get("description_raw", "") + " (食品等)",
        })

    # 10% 検証
    candidates_10 = raw_data.get("tax_10_area", {}).get("candidates", [])
    amount_10, tax_10 = verify_tax_math(candidates_10, 0.10)
    if amount_10:
        desc = raw_data.get("description_raw", "")
        if amount_10 < 50:
            desc = "レジ袋"
        entries.append({
            "debit_account": debit_account,
            "debit_tax_type": "課対仕入10%",
            "credit_account": credit_account,
            "credit_tax_type": "対象外",
            "amount": amount_10,
            "description": desc,
        })

    return entries


def _build_entries_from_purchase_invoice(raw_data):
    """支払請求書・仕入請求書データから仕訳エントリを生成"""
    entries = []
    items = raw_data.get("items", [])

    # 貸方科目決定
    pay_method = str(raw_data.get("payment_method", "振込"))
    credit_account = "買掛金"
    if "振込" in pay_method or "口座" in pay_method:
        credit_account = "普通預金"
    elif "現金" in pay_method:
        credit_account = "現金"

    config = DOC_TYPE_CONFIG[DocType.PURCHASE_INVOICE]

    for item in items:
        amount = item.get("amount", 0)
        if not amount or int(amount) == 0:
            continue

        tax_rate = item.get("tax_rate", 0.10)
        debit_account = item.get("debit_account", config["default_debit"])

        if tax_rate == 0.08:
            debit_tax_type = "課対仕入8% (軽)"
        else:
            debit_tax_type = "課対仕入10%"

        entries.append({
            "debit_account": debit_account,
            "debit_tax_type": debit_tax_type,
            "credit_account": credit_account,
            "credit_tax_type": "対象外",
            "amount": int(amount),
            "description": item.get("description", ""),
        })

    return entries


def _build_entries_from_sales_invoice(raw_data):
    """売上請求書データから仕訳エントリを生成"""
    entries = []
    items = raw_data.get("items", [])
    config = DOC_TYPE_CONFIG[DocType.SALES_INVOICE]

    for item in items:
        amount = item.get("amount", 0)
        if not amount or int(amount) == 0:
            continue

        tax_rate = item.get("tax_rate", 0.10)
        if tax_rate == 0.08:
            credit_tax_type = "課税売上8% (軽)"
        else:
            credit_tax_type = "課税売上10%"

        entries.append({
            "debit_account": config["default_debit"],      # 売掛金
            "debit_tax_type": "対象外",
            "credit_account": config["default_credit"],    # 売上高
            "credit_tax_type": credit_tax_type,
            "amount": int(amount),
            "description": item.get("description", ""),
        })

    return entries


def _build_entries_from_salary_slip(raw_data):
    """賃金台帳・給与明細書データから仕訳エントリを生成"""
    entries = []
    employee = raw_data.get("employee_name", "")

    gross = int(raw_data.get("gross_salary", 0))
    social_ins = int(raw_data.get("social_insurance", 0))
    income_tax = int(raw_data.get("income_tax", 0))
    resident_tax = int(raw_data.get("resident_tax", 0))
    other_ded = int(raw_data.get("other_deductions", 0))
    net = int(raw_data.get("net_salary", 0))

    if gross <= 0:
        return entries

    # 借方: 給料手当（総支給額）
    entries.append({
        "debit_account": "給料手当",
        "debit_tax_type": "対象外",
        "credit_account": "普通預金",
        "credit_tax_type": "対象外",
        "amount": net,
        "description": f"給与 {employee} (差引支給額)",
    })

    # 貸方控除: 社会保険料預り金
    if social_ins > 0:
        entries.append({
            "debit_account": "給料手当",
            "debit_tax_type": "対象外",
            "credit_account": "預り金",
            "credit_tax_type": "対象外",
            "amount": social_ins,
            "credit_sub_account": "社会保険料",
            "description": f"社会保険料控除 {employee}",
        })

    # 貸方控除: 源泉所得税
    if income_tax > 0:
        entries.append({
            "debit_account": "給料手当",
            "debit_tax_type": "対象外",
            "credit_account": "預り金",
            "credit_tax_type": "対象外",
            "amount": income_tax,
            "credit_sub_account": "源泉所得税",
            "description": f"源泉所得税控除 {employee}",
        })

    # 貸方控除: 住民税
    if resident_tax > 0:
        entries.append({
            "debit_account": "給料手当",
            "debit_tax_type": "対象外",
            "credit_account": "預り金",
            "credit_tax_type": "対象外",
            "amount": resident_tax,
            "credit_sub_account": "住民税",
            "description": f"住民税控除 {employee}",
        })

    # 貸方控除: その他
    if other_ded > 0:
        entries.append({
            "debit_account": "給料手当",
            "debit_tax_type": "対象外",
            "credit_account": "預り金",
            "credit_tax_type": "対象外",
            "amount": other_ded,
            "credit_sub_account": "その他控除",
            "description": f"その他控除 {employee}",
        })

    return entries


# エントリビルダー登録テーブル
ENTRY_BUILDERS = {
    DocType.RECEIPT: _build_entries_from_receipt,
    DocType.PURCHASE_INVOICE: _build_entries_from_purchase_invoice,
    DocType.SALES_INVOICE: _build_entries_from_sales_invoice,
    DocType.SALARY_SLIP: _build_entries_from_salary_slip,
}


# ============================================================
# メインパイプライン
# ============================================================

def process_pipeline(file_path, doc_type=DocType.RECEIPT):
    """
    文書を分析し、統一された仕訳データを返す。

    Args:
        file_path: 文書ファイルのパス
        doc_type: 文書タイプ (DocType 定数)

    Returns:
        {
            "doc_type": str,
            "date": str,
            "vendor": str,
            "invoice_num": str,
            "memo": str,
            "entries": [{debit_account, debit_tax_type, credit_account, ...}]
        }
        or None if parsing fails
    """
    filename = os.path.basename(file_path)
    type_label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)
    print(f"🧠 Geminiが{type_label}を分析中: {filename} ...")

    try:
        prompt = PROMPTS.get(doc_type)
        if not prompt:
            print(f"⚠️ 未対応の文書タイプ: {doc_type}")
            return None

        raw_data = _call_gemini(file_path, prompt)

        if not raw_data:
            print("⚠️ AIの応答がJSONではありませんでした")
            return None

        builder = ENTRY_BUILDERS.get(doc_type)
        if not builder:
            print(f"⚠️ エントリビルダーが未登録: {doc_type}")
            return None

        entries = builder(raw_data)

        # 給与明細は vendor の代わりに employee_name を使用
        vendor = raw_data.get("vendor", "")
        if doc_type == DocType.SALARY_SLIP:
            vendor = raw_data.get("employee_name", "")

        return {
            "doc_type": doc_type,
            "date": raw_data.get("date"),
            "vendor": vendor,
            "invoice_num": raw_data.get("invoice_num", ""),
            "memo": raw_data.get("memo", ""),
            "entries": entries,
        }

    except Exception as e:
        print(f"❌ 解析失敗: {e}")
        return None
