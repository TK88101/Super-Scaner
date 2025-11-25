import os
import json
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("⚠️ 嚴重錯誤: 未找到 GEMINI_API_KEY")

genai.configure(api_key=api_key)
model = genai.GenerativeModel('gemini-2.0-flash-exp') 

def verify_tax_math(candidates, rate):
    """
    數學驗算V2.0:
    同時支持「稅前價」和「含稅價」的反推驗證
    """
    # 1. 數據清洗
    nums = []
    for c in candidates:
        try:
            # 去除逗號、日圓符號、空格
            clean_str = str(c).replace(',', '').replace('¥', '').replace('円', '').strip()
            clean_num = float(clean_str)
            nums.append(clean_num)
        except:
            continue
            
    # 從大到小排序
    nums = sorted(list(set(nums)), reverse=True)

    # 2. 暴力比對
    for amount in nums:
        # 遍歷剩下的數字尋找稅額
        for tax in nums:
            if amount <= tax: continue # 本金通常大於稅額
            
            # === 情況 A：假設 amount 是「稅前金額 (Net)」===
            # 公式：稅前 * 稅率 = 稅額
            expected_tax_a = amount * rate
            if abs(expected_tax_a - tax) <= 2.0: # 允許 2日圓誤差
                return int(amount), int(tax)
            
            # === 情況 B：假設 amount 是「含稅金額 (Gross)」===
            # 公式：含稅 - (含稅 / (1+稅率)) = 稅額
            # 也就是：倒推出稅前金額
            expected_net = amount / (1 + rate)
            expected_tax_b = amount - expected_net
            
            if abs(expected_tax_b - tax) <= 2.0:
                # 驗算成功！這是一個含稅價。
                # 我們需要返回「稅前金額」給 CSV (MoneyForward 標準)
                true_net = int(amount - tax)
                return true_net, int(tax)
                
    return None, None

def process_pipeline(file_path):
    filename = os.path.basename(file_path)
    print(f"🧠 Gemini 正在進行雙重數學驗算: {filename} ...")
    
    try:
        with open(file_path, "rb") as f:
            file_data = f.read()
            
        mime_type = "image/jpeg"
        ext = os.path.splitext(file_path)[1].lower()
        if ".png" in ext: mime_type = "image/png"
        elif ".heic" in ext: mime_type = "image/heic"
        elif ".pdf" in ext: mime_type = "application/pdf"

        # Prompt 保持不變，依然是讓它抓取所有數字
        prompt = """
        你是一個數據提取機器人。請提取收據底部「税率別内訳 (Tax Breakdown)」區域出現的所有數字。
        
        請返回 JSON：
        {
            "date": "YYYY/MM/DD",
            "vendor": "供應商名稱",
            "description_raw": "主要商品摘要",
            "tax_8_area": {
                "candidates": ["數字1", "數字2", "數字3", "合計金額"]
            },
            "tax_10_area": {
                "candidates": ["數字1", "數字2", "數字3", "合計金額"]
            }
        }
        """

        response = model.generate_content([
            {"mime_type": mime_type, "data": file_data},
            prompt
        ])
        
        text = response.text.replace("```json", "").replace("```", "").strip()
        raw_data = json.loads(text)
        
        final_split_items = []
        
        # 驗算 8%
        candidates_8 = raw_data.get("tax_8_area", {}).get("candidates", [])
        amount_8, tax_8 = verify_tax_math(candidates_8, 0.08)
        
        if amount_8:
            final_split_items.append({
                "amount": amount_8,
                "tax_type": "課対仕入8% (軽)",
                "description": raw_data.get("description_raw") + " (食品等)"
            })
            
        # 驗算 10%
        candidates_10 = raw_data.get("tax_10_area", {}).get("candidates", [])
        amount_10, tax_10 = verify_tax_math(candidates_10, 0.10)
        
        if amount_10:
            desc = raw_data.get("description_raw")
            if amount_10 < 50: desc = "レジ袋"
            final_split_items.append({
                "amount": amount_10,
                "tax_type": "課対仕入10%",
                "description": desc
            })

        return {
            "date": raw_data.get("date"),
            "vendor": raw_data.get("vendor"),
            "category": "未分類",
            "split_items": final_split_items
        }

    except Exception as e:
        print(f"❌ 識別失敗: {e}")
        return None