import os
import json
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("⚠️ 重大エラー: GEMINI_API_KEYが見つかりません。.envを確認してください")

genai.configure(api_key=api_key)
model = genai.GenerativeModel('gemini-2.0-flash-exp') 

def process_pipeline(file_path):
    filename = os.path.basename(file_path)
    # 日語日誌
    print(f"🧠 Geminiが画像を解析中: {filename} ...")
    
    try:
        with open(file_path, "rb") as f:
            file_data = f.read()
            
        mime_type = "image/jpeg"
        ext = os.path.splitext(file_path)[1].lower()
        if ".png" in ext: mime_type = "image/png"
        elif ".heic" in ext: mime_type = "image/heic"
        elif ".pdf" in ext: mime_type = "application/pdf"

        # === Prompt 已漢化為日語，以提高對日本收據的理解力 ===
        prompt = """
        あなたはプロの経理担当者です。この領収書（または請求書）を分析し、混合税率を考慮してデータを抽出してください。
        
        【⚠️ 重要：金額の正確性について】
        レシート下部にある「税率別内訳（対象額・税抜金額）」を最優先で参照してください。
        1. **対象額 (Target Amount)**： "税抜"、"対象額"、"Tax Base" と記載されている数字を取得してください。（消費税額ではありません！）
        2. **計算禁止**：商品価格を自分で足し算しないでください。必ずレシートの集計値を読み取ってください。
        
        【抽出ルール】
        1. "8% 対象額" (軽減税率) の数字を探す -> 8%金額とする。
        2. "10% 対象額" (標準税率) の数字を探す -> 10%金額とする。
        3. "対象額"が見つからない場合は、税額よりも大きい数字（本体価格）を選んでください。
        
        【摘要(Description)の入力ルール】
        1. 10%対象の金額が小さく(< 20円)、品名が不明な場合は "レジ袋" と記入してください。
        2. 8%対象は "食料品" または具体的な品名を記入してください。
        
        以下のJSONフォーマットのみを返してください（Markdownタグ不要）：
        {
            "date": "YYYY/MM/DD",
            "vendor": "店舗名・業者名",
            "category": "勘定科目 (例: 会議費, 消耗品費, 仕入高)",
            "split_items": [
                {
                    "amount": "8%対象額 (半角数字)",
                    "tax_type": "課対仕入8% (軽)",
                    "description": "8%部分の摘要"
                },
                {
                    "amount": "10%対象額 (半角数字)",
                    "tax_type": "課対仕入10%",
                    "description": "10%部分の摘要"
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
        print(f"❌ 解析失敗: {e}")
        return None