import csv
import os

MF_HEADERS = [
    "取引日", "借方勘定科目", "借方補助科目", "借方税区分", "借方金額(円)", 
    "借方税額", "貸方勘定科目", "貸方補助科目", "貸方税区分", "貸方金額(円)", 
    "貸方税額", "摘要"
]

OUTPUT_FILE = "MF_Import_Data.csv"

def append_to_csv(data):
    """
    支持寫入多行數據 (針對混合稅率)
    """
    file_exists = os.path.isfile(OUTPUT_FILE)
    
    with open(OUTPUT_FILE, mode='a', newline='', encoding='shift_jis') as f:
        writer = csv.writer(f)
        
        if not file_exists:
            writer.writerow(MF_HEADERS)
            
        # 獲取 AI 拆分好的列表
        items = data.get("split_items", [])
        
        # 遍歷列表，有幾種稅率就寫幾行
        for item in items:
            amount = item.get("amount")
            if not amount or int(amount) == 0:
                continue # 跳過金額為0的空行

            row = [
                data.get("date"),                # 日期 (共用)
                data.get("category"),            # 科目 (共用)
                "",
                item.get("tax_type"),            # 各自的稅率
                amount,                          # 各自的金額
                "",
                "現金",
                "",
                "対象外",
                amount,
                "",
                f"{data.get('vendor')} - {item.get('description')}" # 摘要帶上具體內容
            ]
            
            writer.writerow(row)
            print(f"💾 已寫入 CSV 行 ({item.get('tax_type')}): {amount}")