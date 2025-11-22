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

def process_pipeline(file_path):
    filename = os.path.basename(file_path)
    print(f"🧠 Gemini 正在進行財務級別校對 (V5.0): {filename} ...")
    
    try:
        with open(file_path, "rb") as f:
            file_data = f.read()
            
        mime_type = "image/jpeg"
        ext = os.path.splitext(file_path)[1].lower()
        if ".png" in ext: mime_type = "image/png"
        elif ".heic" in ext: mime_type = "image/heic"
        elif ".pdf" in ext: mime_type = "application/pdf"

        # === V5.0 Prompt ===
        prompt = """
        你是一個審計級別的會計助手。請處理這張可能包含混合稅率的單據。
        任務：準確提取 8% 和 10% 的【交易金額】，並生成 JSON。
        
        【⚠️ 核心指令：抓取「對象額」，不要抓「稅額」】
        收據底部通常有兩列數字，請務必區分清楚：
        1. **對象額 (Target Amount)**：通常寫著 "対象額"、"税抜"、"Tax Base"。 -> **這才是我們要的金額！**
        2. **稅額 (Tax Amount)**：通常寫著 "消費税"、"税"、"Tax"。 -> **絕對不要抓這個數字！**
        
        【提取規則】
        1. 請查找 "8% 対象額" (或類似字樣) 對應的數字 -> 作為 8% 金額。
        2. 請查找 "10% 対象額" (或類似字樣) 對應的數字 -> 作為 10% 金額。
        3. 如果找不到 "対象額" 字樣，請找數值較大的那個數字（因為本金通常比稅額大）。
        4. **禁止計算**：嚴禁你自己去加減上面的商品價格。
        
        【摘要填寫】
        1. 10% 金額極小 (< 20円) 且無明顯商品名 -> 填 "レジ袋"。
        2. 8% 部分 -> 填 "食料品" 或具體品名。
        
        請返回如下 JSON 格式：
        {
            "date": "YYYY/MM/DD",
            "vendor": "供應商名稱",
            "category": "推測會計科目",
            "split_items": [
                {
                    "amount": "8%部分的【對象額】 (純數字)",
                    "tax_type": "課対仕入8% (軽)",
                    "description": "8%部分的商品摘要"
                },
                {
                    "amount": "10%部分的【對象額】 (純數字)",
                    "tax_type": "課対仕入10%",
                    "description": "10%部分的商品摘要"
                }
            ]
        }
        """

        response = model.generate_content([
            {"mime_type": mime_type, "data": file_data},
            prompt
        ])
        
        text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)

    except Exception as e:
        print(f"❌ 識別失敗: {e}")
        return None