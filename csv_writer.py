import csv
import os

MF_HEADERS = [
    "取引No", "取引日", "借方勘定科目", "借方補助科目", "借方部門", "借方取引先",
    "借方税区分", "借方インボイス", "借方金額(円)", "借方税額",
    "貸方勘定科目", "貸方補助科目", "貸方部門", "貸方取引先", "貸方税区分",
    "貸方インボイス", "貸方金額(円)", "貸方税額", "摘要", "仕訳メモ",
    "タグ", "MF仕訳タイプ", "決算整理仕訳", "作成日時", "作成者",
    "最終更新日時", "最終更新者"
]

OUTPUT_FILE = "MF_Import_Data.csv"

def append_to_csv(data):
    file_exists = os.path.isfile(OUTPUT_FILE)
    
    with open(OUTPUT_FILE, mode='a', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        
        if not file_exists:
            writer.writerow(MF_HEADERS)
            
        items = data.get("split_items", [])
        uploader_name = data.get('uploader', '')
        
        # 獲取更多字段
        invoice_num = data.get('invoice_num', '') 
        vendor_name = data.get('vendor', '')
        memo = data.get('memo', '')
        credit_acct = data.get('credit_account', '現金')
        
        for item in items:
            amount = item.get("amount")
            if not amount or int(amount) == 0:
                continue

            # 摘要依然保留担当者信息，雙重保險
            description = f"{vendor_name} - {item.get('description')}"
            if uploader_name:
                description += f" [担当: {uploader_name}]"

            row = [
                "",                              # 取引No
                data.get("date"),                # 取引日
                data.get("category"),            # 借方勘定科目
                "",                              # 借方補助科目
                "",                              # 借方部門
                vendor_name,                     # 借方取引先
                item.get("tax_type"),            # 借方税区分
                invoice_num,                     # 借方インボイス
                amount,                          # 借方金額
                "",                              # 借方税額
                credit_acct,                     # 貸方勘定科目
                "",                              # 貸方補助科目
                "",                              # 貸方部門
                "",                              # 貸方取引先
                "対象外",                         # 貸方税区分
                "",                              # 貸方インボイス
                amount,                          # 貸方金額
                "",                              # 貸方税額
                description,                     # 摘要
                memo,                            # 仕訳メモ
                "",                              # タグ
                "",                              # MF仕訳タイプ
                "",                              # 決算整理仕訳
                "",                              # 作成日時
                uploader_name,                   # 作成者
                "",                              # 最終更新日時
                uploader_name                    # 最終更新者
            ]
            
            writer.writerow(row)
            print(f"💾 CSVに行を追加: {item.get('tax_type')} - ¥{amount} (作成者: {uploader_name})")