# config.py
# 項目配置文件 - 這裡存放員工名單和其他靜態配置

import os
from doc_types import DocType, ENV_FOLDER_MAP

# === 員工名單映射表 ===
# Key: 員工的 Google 郵箱地址
# Value: 
#    - name: CSV 中顯示的真實姓名
#    - chat_id: 聊天軟體 (如 Slack/Teams) 的用戶 ID，用於 @提及
#               (如果不清楚 ID，可以先填 None)

EMPLOYEE_MAP = {
    # 測試帳號 (您的郵箱)
    "toadeater731@gmail.com": {
        "name": "Administrator",
        "chat_id": "U12345678" # 示例 ID
    },
    
    # 員工 A
    "staff_a@company.com": {
        "name": "田中 太郎",
        "chat_id": None
    },
    
    # 員工 B
    "staff_b@company.com": {
        "name": "佐藤 花子",
        "chat_id": None
    },
    
    # 可以在這裡繼續添加...
}

# === 支持的文件格式 ===
# 只有這些後綴的文件會被機器人處理
SUPPORTED_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.heic', '.pdf']

# === 系統設置 ===
# 掃描間隔 (秒)
SCAN_INTERVAL = 3


def load_folder_map():
    """
    從環境變量構建 {folder_id: doc_type} 映射表。
    支持新的多文件夾模式 (FOLDER_RECEIPT_ID 等) 和
    舊的單文件夾模式 (INPUT_FOLDER_ID → 默認 receipt)。
    """
    folder_map = {}

    # 新模式: 讀取每種文書類型的專用文件夾 ID
    for env_key, doc_type in ENV_FOLDER_MAP.items():
        folder_id = os.getenv(env_key)
        if folder_id:
            folder_map[folder_id] = doc_type

    # 向後兼容: 如果新模式沒有配置任何文件夾，
    # 則使用 INPUT_FOLDER_ID 作為領收書文件夾
    if not folder_map:
        legacy_id = os.getenv("INPUT_FOLDER_ID")
        if legacy_id:
            folder_map[legacy_id] = DocType.RECEIPT

    return folder_map