import requests
import os
import json
from dotenv import load_dotenv

load_dotenv()
API_TOKEN = os.getenv("CHATWORK_API_TOKEN", "")
ROOM_ID = os.getenv("CHATWORK_ROOM_ID", "")
API_URL = f"https://api.chatwork.com/v2/rooms/{ROOM_ID}/messages" if ROOM_ID else None

def send_notification(filename, status, uploader_name, chat_id=None, details=""):
    """
    發送 Chatwork 通知 (抗干擾版)
    """
    # === ⭐ 安全檢查：清洗 Token ===
    # 1. 去除前後空格
    token_clean = API_TOKEN.strip()
    
    # 2. 檢查是否為空，或者是否包含非 ASCII 字符 (比如中文)
    if not token_clean or not token_clean.isascii():
        print(f"ℹ️ 跳過通知 (Token 未配置或格式不正確): {filename}")
        return

    if not ROOM_ID:
        return

    # 構建 @提及
    to_tag = f"[To:{chat_id}] {uploader_name} さん\n" if chat_id else ""
    
    icon = "✅" if status == "Success" else "❌"
    title_str = f"{icon} 処理結果: {status}"

    # Chatwork 消息體
    body = (
        f"{to_tag}"
        f"[info]"
        f"[title]{title_str}[/title]"
        f"ファイル名: {filename}\n"
        f"担当者: {uploader_name}\n"
        f"[hr]\n"
        f"詳細:\n{details}"
        f"[/info]"
    )

    # ⭐ 確保 Header 絕對乾淨
    headers = {"X-ChatWorkToken": token_clean}
    params = {"body": body, "self_unread": "0"}

    try:
        response = requests.post(API_URL, headers=headers, data=params)
        
        if response.status_code == 200:
            print("🔔 Chatwork 通知已發送")
        else:
            print(f"⚠️ Chatwork 發送失敗: {response.status_code} - {response.text}")
            
    except Exception as e:
        print(f"⚠️ 通知系統連線錯誤: {e}")