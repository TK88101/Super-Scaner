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
あなたはプロの経理担当者です。画像に写っている全ての書類（領収書・レシート・受取書・振込控え等）を分析してください。

重要: 画像に複数の書類が含まれる場合は、全ての書類を別々に抽出してください。

【出力JSONフォーマット】
{
    "documents": [
        {
            "doc_category": "receipt | bank_transfer | fee_receipt",
            "date": "YYYY/MM/DD",
            "vendor": "取引先名（発行者、または振込の場合は振込依頼人）",
            "invoice_num": "Tから始まる13桁番号 (なければ空文字)",
            "payment_method": "支払い方法 (現金, クレジットカード, PayPay, 振込, ATM)",
            "memo": "メモ（振込先名、用途など）",
            "items": [
                {
                    "description": "品目・内容",
                    "amount": 税込金額(数値),
                    "tax_rate": 0.08 or 0.10 or 0,
                    "tax_amount": 消費税額(数値, なければ0),
                    "debit_account": "費用の勘定科目を推定"
                }
            ]
        }
    ]
}

【doc_categoryの判定基準】
- "receipt": 通常の領収書・レシート（コンビニ、店舗等での購入）
- "bank_transfer": 銀行振込の受取書・振込控え（振込金額本体。税区分は「対象外」）
- "fee_receipt": 振込手数料の領収証（ATM手数料、コンビニ手数料等。課税対象）

【勘定科目の推定基準】
- bank_transfer: debit_account は "未払金"（既存の買掛金・未払金の支払い）
- fee_receipt: debit_account は "支払手数料"
- receipt: 内容から推定 (消耗品費, 旅費交通費, 交際費, 会議費, 通信費, 福利厚生費, 雑費 など)

【tax_rate の判定基準】
- 食品・飲料(酒類除く): 0.08
- それ以外の課税品目: 0.10
- 銀行振込本体(bank_transfer): 0（非課税・対象外）

【payment_method の判定基準】
- コンビニ（FamilyMart, セブンイレブン等）での支払い → "現金"
- SMCC(QQ), QUICPay, iD 等の電子決済表記がある場合でも、
  コンビニ払いの振込手数料であれば → "現金"（代収扱い）
- 銀行ATMでの振込 → "ATM"
- 銀行窓口での振込 → "振込"
- クレジットカード明細 → "クレジットカード"

【date の取得方法（重要）】
- 書類に印字された日付を最優先
- 印字の日付欄が空白の場合は、以下の順で日付を探す:
  1. 取扱日付印・受付印（赤いスタンプ/印章）の中の数字（例: "9.16" → 当年の9月16日）
  2. 書類下部の「ご依頼人」欄付近のスタンプ日付
  3. 同一画像内の他の書類の日付（同日の取引である可能性が高い）
- 印章の日付形式: "M.DD", "MM.DD", "R7.9.16", "2025.9.16" など → 西暦 YYYY/MM/DD に変換
- 年が不明な場合は、同一画像内の他の書類の年、または現在の年（2025年）を使用
- dateは可能な限り必ず出力してください。空文字は最終手段です

注意:
- 画像に1枚の書類しかなくても、必ず documents 配列で返してください
- 金額は数値型(カンマなし)で返してください
- 振込受取書では、vendor は振込依頼人（支払い元の会社名）を記載してください
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

def _determine_credit_account(pay_method, doc_category="receipt"):
    """支払方法とドキュメントカテゴリから貸方科目を決定"""
    # 銀行振込・振込手数料: 口座から引き落とされるため普通預金
    if doc_category in ("bank_transfer", "fee_receipt"):
        return "普通預金"

    if any(x in pay_method for x in ["クレジット", "Credit", "Card", "VISA", "Master"]):
        return "未払金"
    elif any(x in pay_method for x in ["振込", "ATM"]):
        return "普通預金"
    elif "PayPay" in pay_method:
        return "未払金"

    return "現金"


def _determine_tax_types(doc_category, tax_rate):
    """ドキュメントカテゴリと税率から借方・貸方税区分を決定"""
    if doc_category == "bank_transfer" or tax_rate == 0:
        return "対象外", "対象外"
    elif tax_rate == 0.08:
        return "課対仕入8% (軽)", "対象外"
    else:  # 0.10
        return "課対仕入10%", "対象外"


def _build_entries_for_single_doc(doc):
    """documents配列の1要素（単一書類）から仕訳エントリを生成"""
    entries = []
    doc_category = doc.get("doc_category", "receipt")

    # 貸方科目決定
    pay_method = str(doc.get("payment_method", "現金"))
    credit_account = _determine_credit_account(pay_method, doc_category)

    for item in doc.get("items", []):
        amount = item.get("amount", 0)
        if not amount or int(amount) == 0:
            continue

        tax_rate = item.get("tax_rate", 0.10)
        debit_account = item.get("debit_account", "消耗品費")

        # 税区分決定
        debit_tax_type, credit_tax_type = _determine_tax_types(
            doc_category, tax_rate
        )

        entries.append({
            "debit_account": debit_account,
            "debit_tax_type": debit_tax_type,
            "credit_account": credit_account,
            "credit_tax_type": credit_tax_type,
            "amount": int(amount),
            "description": item.get("description", ""),
        })

    return entries


def _build_entries_from_receipt(raw_data):
    """領収書データから仕訳エントリを生成（新旧フォーマット両対応）"""
    # 旧フォーマット（documents キーなし）: レガシーロジック
    if "documents" not in raw_data:
        return _build_entries_from_receipt_legacy(raw_data)

    # 新フォーマット: documents 配列
    # NOTE: 複数文書の場合は process_pipeline 側で処理するため、
    #       ここでは単一文書のフォールバックのみ対応
    documents = raw_data.get("documents", [])
    if len(documents) == 1:
        return _build_entries_for_single_doc(documents[0])

    # 複数文書は process_pipeline で処理済みのはずだが、
    # 万が一ここに来た場合は全文書のエントリを結合して返す
    all_entries = []
    for doc in documents:
        all_entries.extend(_build_entries_for_single_doc(doc))
    return all_entries


def _build_entries_from_receipt_legacy(raw_data):
    """旧フォーマット用: tax_8_area/tax_10_area ベースのロジック（後方互換）"""
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
        dict: 単一文書の場合（通常）
        list[dict]: 複数文書検出時（領収書のみ）
        None: 解析失敗時
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

        # ── 領収書マルチドキュメント処理 ──
        if doc_type == DocType.RECEIPT and "documents" in raw_data:
            documents = raw_data["documents"]
            if not documents:
                print("⚠️ documents配列が空です")
                return None

            print(f"📑 {len(documents)} 件の書類を検出")

            results = []
            for i, doc in enumerate(documents, 1):
                doc_cat = doc.get("doc_category", "receipt")
                vendor = doc.get("vendor", "不明")
                print(f"  [{i}] {doc_cat}: {vendor}")

                entries = _build_entries_for_single_doc(doc)
                if not entries:
                    print(f"  ⚠️ エントリなし（スキップ）")
                    continue

                results.append({
                    "doc_type": doc_type,
                    "date": doc.get("date"),
                    "vendor": vendor,
                    "invoice_num": doc.get("invoice_num", ""),
                    "memo": doc.get("memo", ""),
                    "entries": entries,
                })

            if len(results) == 0:
                return None
            elif len(results) == 1:
                return results[0]       # 単一文書: dict を返す（後方互換）
            else:
                return results          # 複数文書: list を返す

        # ── 通常パス（他の文書タイプ、または旧フォーマット領収書）──
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
