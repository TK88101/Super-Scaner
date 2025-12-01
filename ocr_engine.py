import os
import json
import re
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("⚠️ 重大エラー: GEMINI_API_KEYが見つかりません")

genai.configure(api_key=api_key)
model = genai.GenerativeModel('gemini-2.0-flash-exp') 

def verify_tax_math(candidates, rate):
    """
    数学的検証ロジック (V3.0)
    """
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
            if amount <= tax: continue
            
            # パターンA: 税抜
            if abs(amount * rate - tax) <= 2.0:
                return int(amount), int(tax)
            
            # パターンB: 税込
            expected_net = amount / (1 + rate)
            if abs((amount - expected_net) - tax) <= 2.0:
                return int(amount - tax), int(tax)
                
    return None, None

def extract_json(text):
    """
    JSON抽出強化関数：AIが余計な文字（```json や 説明文）をつけても
    強制的に { ... } の中身だけを抜き出す
    """
    try:
        # 正規表現で最初に見つかった { から 最後の } までを抜き出す
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return json.loads(text) # マッチしなければそのままトライ
    except json.JSONDecodeError:
        return None

def process_pipeline(file_path):
    filename = os.path.basename(file_path)
    print(f"🧠 Geminiが画像を詳細分析中: {filename} ...")
    
    try:
        with open(file_path, "rb") as f:
            file_data = f.read()
            
        mime_type = "image/jpeg"
        ext = os.path.splitext(file_path)[1].lower()
        if ".png" in ext: mime_type = "image/png"
        elif ".heic" in ext: mime_type = "image/heic"
        elif ".pdf" in ext: mime_type = "application/pdf"

        # === Prompt ===
        prompt = """
        あなたはプロの経理担当者です。画像を分析し、会計ソフト用データを抽出してください。
        
        【出力JSONフォーマット】
        {
            "date": "YYYY/MM/DD",
            "vendor": "取引先名",
            "invoice_num": "Tから始まる13桁番号 (なければ空文字)",
            "payment_method": "支払い方法 (現金, クレジットカード, PayPay, 振込)",
            "memo": "メモ",
            "description_raw": "品代",
            "tax_8_area": { "candidates": ["8%エリアの全数字"] },
            "tax_10_area": { "candidates": ["10%エリアの全数字"] }
        }
        """

        response = model.generate_content([
            {"mime_type": mime_type, "data": file_data},
            prompt
        ])
        
        # 強化されたJSON解析
        raw_data = extract_json(response.text)
        
        if not raw_data:
            print("⚠️ AIの応答がJSONではありませんでした")
            return None
        
        final_split_items = []
        
        # 8% 検証
        candidates_8 = raw_data.get("tax_8_area", {}).get("candidates", [])
        amount_8, tax_8 = verify_tax_math(candidates_8, 0.08)
        if amount_8:
            final_split_items.append({
                "amount": amount_8,
                "tax_type": "課対仕入8% (軽)",
                "description": raw_data.get("description_raw", "") + " (食品等)"
            })
            
        # 10% 検証
        candidates_10 = raw_data.get("tax_10_area", {}).get("candidates", [])
        amount_10, tax_10 = verify_tax_math(candidates_10, 0.10)
        if amount_10:
            desc = raw_data.get("description_raw", "")
            if amount_10 < 50: desc = "レジ袋"
            final_split_items.append({
                "amount": amount_10,
                "tax_type": "課対仕入10%",
                "description": desc
            })

        # 貸方科目決定
        pay_method = str(raw_data.get("payment_method", "現金"))
        credit_account = "現金"
        
        if any(x in pay_method for x in ["クレジット", "Credit", "Card", "VISA", "Master"]):
            credit_account = "未払金"
        elif "振込" in pay_method:
            credit_account = "普通預金"

        return {
            "date": raw_data.get("date"),
            "vendor": raw_data.get("vendor"),
            "invoice_num": raw_data.get("invoice_num"),
            "memo": raw_data.get("memo"),
            "credit_account": credit_account,
            "category": "未分類",
            "split_items": final_split_items
        }

    except Exception as e:
        print(f"❌ 解析失敗: {e}")
        return None