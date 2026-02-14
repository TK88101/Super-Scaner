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


def _get_next_transaction_no():
    """CSVファイルから現在の最大取引Noを取得し、+1を返す"""
    if not os.path.isfile(OUTPUT_FILE):
        return 1

    max_no = 0
    try:
        with open(OUTPUT_FILE, mode='r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            next(reader, None)  # ヘッダーをスキップ
            for row in reader:
                if row and row[0]:
                    try:
                        no = int(row[0])
                        if no > max_no:
                            max_no = no
                    except ValueError:
                        continue
    except Exception:
        pass

    return max_no + 1


def _convert_legacy_split_items(data):
    """
    旧 split_items 形式を新 entries 形式に変換する（後方互換）。
    旧形式: { split_items: [{amount, tax_type, description}], category, credit_account, ... }
    新形式: { entries: [{debit_account, debit_tax_type, credit_account, credit_tax_type, amount, description}], ... }
    """
    entries = []
    split_items = data.get("split_items", [])
    category = data.get("category", "未分類")
    credit_account = data.get("credit_account", "現金")

    for item in split_items:
        amount = item.get("amount")
        if not amount or int(amount) == 0:
            continue
        entries.append({
            "debit_account": category,
            "debit_tax_type": item.get("tax_type", ""),
            "credit_account": credit_account,
            "credit_tax_type": "対象外",
            "amount": amount,
            "description": item.get("description", ""),
        })

    return entries


def append_to_csv(data):
    file_exists = os.path.isfile(OUTPUT_FILE)

    with open(OUTPUT_FILE, mode='a', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow(MF_HEADERS)

        # entries 形式を優先、なければ旧 split_items から変換
        entries = data.get("entries")
        if entries is None:
            entries = _convert_legacy_split_items(data)

        if not entries:
            print("⚠️ 書き込み対象の仕訳がありません")
            return

        uploader_name = data.get('uploader', '')
        invoice_num = data.get('invoice_num', '')
        vendor_name = data.get('vendor', '')
        memo = data.get('memo', '')
        transaction_no = _get_next_transaction_no()

        for entry in entries:
            amount = entry.get("amount")
            if not amount or int(amount) == 0:
                continue

            description = f"{vendor_name} - {entry.get('description', '')}"
            if uploader_name:
                description += f" [担当: {uploader_name}]"

            row = [
                transaction_no,                          # 取引No
                data.get("date"),                        # 取引日
                entry.get("debit_account", ""),          # 借方勘定科目
                entry.get("debit_sub_account", ""),      # 借方補助科目
                "",                                      # 借方部門
                vendor_name,                             # 借方取引先
                entry.get("debit_tax_type", ""),         # 借方税区分
                entry.get("debit_invoice", invoice_num), # 借方インボイス
                amount,                                  # 借方金額
                entry.get("debit_tax_amount", ""),       # 借方税額
                entry.get("credit_account", ""),         # 貸方勘定科目
                entry.get("credit_sub_account", ""),     # 貸方補助科目
                "",                                      # 貸方部門
                entry.get("credit_vendor", ""),          # 貸方取引先
                entry.get("credit_tax_type", "対象外"),   # 貸方税区分
                entry.get("credit_invoice", ""),         # 貸方インボイス
                entry.get("credit_amount", amount),      # 貸方金額
                entry.get("credit_tax_amount", ""),      # 貸方税額
                description,                             # 摘要
                memo,                                    # 仕訳メモ
                "",                                      # タグ
                "",                                      # MF仕訳タイプ
                "",                                      # 決算整理仕訳
                "",                                      # 作成日時
                uploader_name,                           # 作成者
                "",                                      # 最終更新日時
                uploader_name                            # 最終更新者
            ]

            writer.writerow(row)
            print(f"💾 CSVに行を追加: {entry.get('debit_account', '')} - ¥{amount} (取引No: {transaction_no}, 作成者: {uploader_name})")
