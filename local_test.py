#!/usr/bin/env python3
"""
Super Scaner ローカルテストモード
Google Drive / Chatwork 不要。Gemini API のみで動作。

使い方:
  1. test_images/ の各サブフォルダにテストファイルを配置
     - test_images/receipt/          ← 領収書
     - test_images/purchase_invoice/ ← 支払請求書・仕入請求書
     - test_images/sales_invoice/    ← 売上請求書
     - test_images/salary_slip/      ← 賃金台帳・給与明細書
  2. python3 local_test.py
  3. MF_Import_Data.csv を確認
"""

import os
import shutil
from dotenv import load_dotenv

load_dotenv()

from ocr_engine import process_pipeline
from sheets_output import SheetsOutputWriter
from doc_types import DocType, DOC_TYPE_CONFIG
import config

# ================= 設定 =================
TEST_DIR = "./test_images"
PROCESSED_DIR = os.path.join(TEST_DIR, "processed")

# サブフォルダ名 → DocType マッピング
FOLDER_TYPE_MAP = {
    "receipt": DocType.RECEIPT,
    "purchase_invoice": DocType.PURCHASE_INVOICE,
    "sales_invoice": DocType.SALES_INVOICE,
    "salary_slip": DocType.SALARY_SLIP,
}
# =========================================


def ensure_dirs():
    """テスト用ディレクトリを作成"""
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    for folder_name in FOLDER_TYPE_MAP:
        os.makedirs(os.path.join(TEST_DIR, folder_name), exist_ok=True)


def scan_local_files():
    """各サブフォルダからテストファイルを収集"""
    files = []
    for folder_name, doc_type in FOLDER_TYPE_MAP.items():
        folder_path = os.path.join(TEST_DIR, folder_name)
        if not os.path.isdir(folder_path):
            continue
        for fname in sorted(os.listdir(folder_path)):
            ext = os.path.splitext(fname)[1].lower()
            if ext in config.SUPPORTED_EXTENSIONS:
                files.append({
                    "path": os.path.join(folder_path, fname),
                    "name": fname,
                    "doc_type": doc_type,
                })
    return files


def process_local_file(file_info, sheets_writer):
    """1ファイルを処理: OCR → Google Sheets 書き込み"""
    file_path = file_info["path"]
    doc_type = file_info["doc_type"]
    file_name = file_info["name"]
    type_label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)

    print(f"\n{'='*50}")
    print(f"📄 ファイル: {file_name}")
    print(f"📋 文書タイプ: {type_label}")
    print(f"{'='*50}")

    # Cloud Vision OCR + Gemini
    result = process_pipeline(file_path, doc_type=doc_type)

    if not result:
        print(f"❌ 解析失敗: {file_name}")
        return False

    # マルチドキュメント対応: list に正規化
    if isinstance(result, list):
        results = result
    else:
        results = [result]

    for idx, r in enumerate(results):
        if len(results) > 1:
            print(f"\n  📄 文書 {idx+1}/{len(results)}: {r.get('vendor', '不明')}")

        # 結果表示
        entries = r.get("entries", [])
        print(f"\n🎯 解析結果:")
        print(f"   📅 日付: {r.get('date')}")
        print(f"   🏪 取引先: {r.get('vendor')}")
        print(f"   📊 仕訳行数: {len(entries)}")

        for i, entry in enumerate(entries, 1):
            print(f"   [{i}] 借方: {entry.get('debit_account')} ¥{entry.get('amount')} "
                  f"({entry.get('debit_tax_type')}) → "
                  f"貸方: {entry.get('credit_account')} ({entry.get('credit_tax_type')})")

        # Google Sheets 書き込み
        r["uploader"] = "LocalTest"
        sheets_writer.append_entries(
            employee_name="LocalTest",
            doc_type=doc_type,
            entries_data=r,
            source_url="",
        )

    # 処理済みフォルダへ移動
    dest = os.path.join(PROCESSED_DIR, file_name)
    if os.path.exists(dest):
        base, ext = os.path.splitext(file_name)
        dest = os.path.join(PROCESSED_DIR, f"{base}_dup{ext}")
    shutil.move(file_path, dest)
    print(f"📦 → processed/ へ移動完了")

    return True


def main():
    print("🚀 Super Scaner ローカルテストモード起動 (Sheets出力版)")
    print(f"📂 テストフォルダ: {os.path.abspath(TEST_DIR)}")
    print("-" * 50)

    # Google Sheets 接続
    if not config.OUTPUT_SPREADSHEET_ID:
        print("❌ OUTPUT_SPREADSHEET_ID が未設定です。.env を確認してください。")
        return

    sa_file = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")
    sheets_writer = SheetsOutputWriter(
        spreadsheet_id=config.OUTPUT_SPREADSHEET_ID,
        credentials_file=sa_file,
    )
    print(f"✅ Google Sheets 接続完了")

    # ディレクトリ準備
    ensure_dirs()

    # ファイル収集
    files = scan_local_files()

    if not files:
        print("\n⚠️  テストファイルが見つかりません。")
        print("以下のフォルダにファイルを配置してください:")
        for folder_name, doc_type in FOLDER_TYPE_MAP.items():
            label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)
            print(f"   📁 {TEST_DIR}/{folder_name}/  ← {label}")
        return

    print(f"\n🔎 {len(files)} 件のファイルを検出:")
    for f in files:
        label = DOC_TYPE_CONFIG.get(f["doc_type"], {}).get("label", f["doc_type"])
        print(f"   - [{label}] {f['name']}")

    # 処理実行
    success_count = 0
    fail_count = 0

    for file_info in files:
        try:
            if process_local_file(file_info, sheets_writer):
                success_count += 1
            else:
                fail_count += 1
        except Exception as e:
            print(f"❌ エラー: {file_info['name']} - {e}")
            fail_count += 1

    # 取引No を Sheets に書き戻す
    sheets_writer.flush()

    # サマリー
    print("\n" + "=" * 50)
    print(f"📊 処理結果サマリー")
    print(f"   ✅ 成功: {success_count} 件")
    print(f"   ❌ 失敗: {fail_count} 件")
    print(f"   📗 Sheets: https://docs.google.com/spreadsheets/d/{config.OUTPUT_SPREADSHEET_ID}/edit")
    print("=" * 50)


if __name__ == "__main__":
    main()
