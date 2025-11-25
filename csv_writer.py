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
    數據寫入 CSV (支持混合稅率 + 上傳者記錄)
    """
    file_exists = os.path.isfile(OUTPUT_FILE)
    
    with open(OUTPUT_FILE, mode='a', newline='', encoding='shift_jis') as f:
        writer = csv.writer(f)
        
        if not file_exists:
            writer.writerow(MF_HEADERS)
            
        items = data.get("split_items", [])
        
        # === ⭐ 第一步：獲取上傳者名字 ===
        # 這個 'uploader' 是我們在 main.py 裡塞進 result 字典的
        uploader_name = data.get('uploader', '')
        
        for item in items:
            amount = item.get("amount")
            if not amount or int(amount) == 0:
                continue

            # === ⭐ 第二步：拼接摘要 ===
            # 格式：店鋪名 - 商品名 [担当: 田中 太郎]
            description = f"{data.get('vendor')} - {item.get('description')}"
            
            if uploader_name:
                description += f" [担当: {uploader_name}]"
            # ==========================

            row = [
                data.get("date"),                
                data.get("category"),            
                "",
                item.get("tax_type"),            
                amount,                          
                "",
                "現金",
                "",
                "対象外",
                amount,
                "",
                description
            ]
            
            writer.writerow(row)
            print(f"💾 CSVに行を追加: {item.get('tax_type')} - ¥{amount}")