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
# ※ 標準化勘定科目　法人.xlsx (2026-03-24 受領) に基づく
ACCOUNT_MAP = {
    # --- AI が出力しがちな別名 → MF 正式名 ---
    "消耗品費": "備品・消耗品費",
    "雑費": "備品・消耗品費",       # 雑費は使わない → 備品・消耗品費に統一
    "交際費": "接待交際費",         # MF 正式名は「接待交際費」
    "食費": "接待交際費",
    "飲食費": "接待交際費",
    "交通費": "旅費交通費",
    "タクシー代": "旅費交通費",
    "電車代": "旅費交通費",
    "駐車場代": "旅費交通費",
    "ガソリン代": "旅費交通費",
    "電話代": "通信費",
    "インターネット代": "通信費",
    "家賃": "地代家賃",
    "手数料": "支払手数料",
    "振込手数料": "支払手数料",
    "書籍代": "新聞図書費",
    "図書費": "新聞図書費",
    "外注加工費": "外注費",
    "委託費": "業務委託料",
    "会費": "諸会費",
    "年会費": "諸会費",
    # --- MF 正式名そのまま（AI が正しく出力した場合のパススルー） ---
    "接待交際費": "接待交際費",
    "旅費交通費": "旅費交通費",
    "車両費": "旅費交通費",
    "通信費": "通信費",
    "水道光熱費": "水道光熱費",
    "修繕費": "修繕費",
    "備品・消耗品費": "備品・消耗品費",
    "地代家賃": "地代家賃",
    "保険料": "保険料",
    "租税公課": "租税公課",
    "支払手数料": "支払手数料",
    "支払報酬": "支払報酬",
    "広告宣伝費": "広告宣伝費",
    "会議費": "会議費",
    "福利厚生費": "福利厚生費",
    "法定福利費": "法定福利費",
    "業務委託料": "業務委託料",
    "荷造運賃": "荷造運賃",
    "新聞図書費": "新聞図書費",
    "リース料": "リース料",
    "諸会費": "諸会費",
    "外注費": "外注費",
    "仮経費": "仮経費",
    "研修採用費": "研修採用費",
    "販売促進費": "販売促進費",
    "寄付金": "寄付金",
    "給料賃金": "給料賃金",
    "役員報酬": "役員報酬",
    "賞与": "賞与",
    "雑給": "雑給",
    "退職給与": "退職給与",
    "仕入高": "仕入高",
    "売上高": "売上高",
    "雑収入": "雑収入",
    "雑損失": "雑損失",
}

# AI が科目を判断できない場合のデフォルト
UNKNOWN_ACCOUNT = "未確定勘定"

# === 貸方補助科目設定 ===
# 領収書: 社長名（立替払いのため）— 会社ごとに異なる
# ※ クライアントから正式名を受領後に更新
CREDIT_SUB_ACCOUNT_RECEIPT = "（社長名未設定）"
# 請求書: 貸方補助科目 = 取引先会社名（vendor_name から自動設定）

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