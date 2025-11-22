import csv
import os

MF_HEADERS = [
    "取引日", "借方勘定科目", "借方補助科目", "借方税区分", "借方金額(円)", 
    "借方税額", "貸方勘定科目", "貸方補助科目", "貸方税区分", "貸方金額(円)", 
    "貸方税額", "摘要"
]

OUTPUT_FILE = "MF_Import_Data.csv"

def append_to_csv(data):
    file_exists = os.path.isfile(OUTPUT_FILE)
    
    with open(OUTPUT_FILE, mode='a', newline='', encoding='shift_jis') as f:
        writer = csv.writer(f)
        
        if not file_exists:
            writer.writerow(MF_HEADERS)
            
        items = data.get("split_items", [])
        
        for item in items:
            amount = item.get("amount")
            if not amount or int(amount) == 0:
                continue

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
                f"{data.get('vendor')} - {item.get('description')}" 
            ]
            
            writer.writerow(row)
            # 日語日誌：寫入成功
            print(f"💾 CSVに行を追加: {item.get('tax_type')} - ¥{amount}")