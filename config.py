# config.py
# 項目配置文件 - 這裡存放員工名單和其他靜態配置

import os
from doc_types import DocType, ENV_FOLDER_MAP

# === 出力先 Google Spreadsheet ID ===
OUTPUT_SPREADSHEET_ID = os.getenv("OUTPUT_SPREADSHEET_ID", "")
BACKUP_SPREADSHEET_ID = os.getenv("BACKUP_SPREADSHEET_ID", "")
SPLIT_PDF_FOLDER_ID = os.getenv("SPLIT_PDF_FOLDER_ID", "")

# === OCR 戦略設定 ===
OCR_STRATEGY = os.getenv("OCR_STRATEGY", "C")
OCR_CONFIDENCE_THRESHOLD = float(os.getenv("OCR_CONFIDENCE_THRESHOLD", "0.7"))

# === 科目マッピング（AI 出力名 → MF 正確名） ===
ACCOUNT_MAP = {
    "消耗品費": "備品・消耗品費",
    "雑費": "市場調査費",       # 要确认：是否所有雑費都映射
    "旅費交通費": "旅費交通費",
    "車両費": "車両費",
    "通信費": "通信費",
    "租税公課": "租税公課",
    "広告宣伝費": "広告宣伝費",
    # 注意: 現金→未払金 はここに入れない（現金払いの貸方科目が壊れる）
    # 貸方デフォルト変更は ocr_engine.py の _determine_credit_account() で対応
}

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