import os
import time
import io
import random
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError
# 引入我們的模塊
from ocr_engine import process_pipeline
from sheets_output import SheetsOutputWriter
from notifier import send_notification
from doc_types import DocType, DOC_TYPE_CONFIG
import config

# ================= 配置區域 =================
load_dotenv()
PROCESSED_FOLDER_ID = os.getenv("PROCESSED_FOLDER_ID")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")
LOCAL_DOWNLOAD_DIR = './temp_downloads'

if not PROCESSED_FOLDER_ID or not SERVICE_ACCOUNT_FILE:
    print("❌ エラー：.envファイルの設定を確認してください (配置錯誤)")
    exit(1)

if not config.OUTPUT_SPREADSHEET_ID:
    print("❌ エラー：OUTPUT_SPREADSHEET_ID が設定されていません。")
    exit(1)

# フォルダマッピング読み込み
folder_map = config.load_folder_map()
if not folder_map:
    print("❌ エラー：監視フォルダが設定されていません。")
    print("   .env に FOLDER_RECEIPT_ID 等、または INPUT_FOLDER_ID を設定してください。")
    exit(1)
# ==============================================


def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=creds)


def _call_with_retry(func, max_retries=5):
    """Google API 500/503 暫時性エラーに対して指数バックオフでリトライ"""
    for attempt in range(max_retries):
        try:
            return func()
        except HttpError as e:
            if e.resp.status in (500, 503) and attempt < max_retries - 1:
                wait = (2 ** attempt) + random.uniform(0, 1)
                print(f"\n⚠️ Google API 一時エラー (HTTP {e.resp.status})、{wait:.1f}秒後リトライ ({attempt+1}/{max_retries-1})...")
                time.sleep(wait)
            else:
                raise


def list_files(service, folder_id):
    query = f"'{folder_id}' in parents and trashed = false"
    results = _call_with_retry(lambda: service.files().list(
        q=query,
        orderBy='createdTime',
        fields="nextPageToken, files(id, name, lastModifyingUser, md5Checksum)"
    ).execute())
    return results.get('files', [])


def download_file(service, file_id, file_name):
    if not os.path.exists(LOCAL_DOWNLOAD_DIR):
        os.makedirs(LOCAL_DOWNLOAD_DIR)
    file_path = os.path.join(LOCAL_DOWNLOAD_DIR, file_name)
    request = service.files().get_media(fileId=file_id)

    print(f"⬇️  ダウンロード中: {file_name} ...")
    fh = io.FileIO(file_path, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
    return file_path


def move_file(service, file_id, previous_folder_id, new_folder_id):
    try:
        service.files().update(
            fileId=file_id,
            addParents=new_folder_id,
            removeParents=previous_folder_id,
            fields='id, parents'
        ).execute()
        print(f"📦 元画像を処理済みフォルダ(Processed)へ移動しました")
    except Exception as e:
        print(f"⚠️ ファイル移動中に警告が発生しました: {e}")



def is_duplicate_file(service, md5_checksum):
    """Processed フォルダ中の重複チェック"""
    if not md5_checksum:
        return False

    try:
        query = f"'{PROCESSED_FOLDER_ID}' in parents and trashed = false"
        results = _call_with_retry(lambda: service.files().list(
            q=query,
            orderBy='createdTime desc',
            pageSize=200,
            fields="files(id, name, md5Checksum)"
        ).execute())

        files = results.get('files', [])
        for file in files:
            if file.get('md5Checksum') == md5_checksum:
                print(f"🔍 本地比對發現重複: {file.get('name')}")
                return True

        return False

    except Exception as e:
        print(f"⚠️ 查重步驟發生未知錯誤: {e}")
        return False



# CSV 関連関数は sheets_output.py に移行済み (廃止)


def process_file(service, sheets_writer, file_path, uploader_name, chat_id,
                  doc_type=DocType.RECEIPT, drive_file_id=None):
    """ファイルを処理し、Google Sheets に逐次書き込み、通知を送信する。

    process_pipeline がジェネレータなので、1ページ処理→即Sheets書き込み→
    メモリ解放→次ページ の流れでメモリ使用量を最小化する。
    """
    type_label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)
    filename = os.path.basename(file_path)
    print(f"⚙️  処理開始: {filename} [{type_label}] (担当: {uploader_name})")

    base_url = ""
    if drive_file_id:
        base_url = f"https://drive.google.com/file/d/{drive_file_id}/view"

    total_amount = 0
    vendor_names = []
    count = 0
    total_entries = 0

    for page in process_pipeline(file_path, doc_type=doc_type):
        result = page["result"]
        page_num = page["page_num"]
        total_pages = page["total_pages"]
        count += 1

        entries = result.get('entries', [])
        print(f"📄 [{page_num}/{total_pages}] 取引先: {result.get('vendor')} | "
              f"仕訳: {len(entries)}行")

        # 即座に Google Sheets へ書き込み
        if total_pages > 1 and base_url:
            source_url = f"{base_url}#page={page_num}"
        else:
            source_url = base_url
        result['uploader'] = uploader_name
        sheets_writer.append_entries(
            employee_name=uploader_name,
            doc_type=doc_type,
            entries_data=result,
            source_url=source_url,
        )

        # 軽量サマリーのみ保持（フル結果は GC 対象）
        page_amount = sum(int(e.get('amount', 0)) for e in entries)
        total_amount += page_amount
        vendor_names.append(result.get('vendor', ''))
        total_entries += len(entries)

    if count > 0:
        vendor_list = ", ".join(v for v in vendor_names if v)
        print(f"\n✅ 処理完了: {count}文書 / {total_entries}仕訳")
        send_notification(
            filename=filename,
            status="Success",
            uploader_name=uploader_name,
            chat_id=chat_id,
            details=f"文書タイプ: {type_label}\n取引先: {vendor_list}\n合計金額: ¥{total_amount}\n文書数: {count}"
        )
        return True
    else:
        send_notification(
            filename=filename,
            status="Failed",
            uploader_name=uploader_name,
            chat_id=chat_id,
            details="AIによる解析に失敗しました。ファイルを確認してください。"
        )
        print("⚠️ 解析に失敗しました")
        return False


def main():
    print("🚀 Super Scaner 自動化システム起動！(Sheets出力版)")
    print(f"📂 監視フォルダ数: {len(folder_map)}")
    for fid, dtype in folder_map.items():
        label = DOC_TYPE_CONFIG.get(dtype, {}).get("label", dtype)
        print(f"   - {label}: ...{fid[-5:]}")
    print("-" * 30)

    service = get_drive_service()

    # Google Sheets 出力ライター初期化
    sheets_writer = SheetsOutputWriter(
        spreadsheet_id=config.OUTPUT_SPREADSHEET_ID,
        credentials_file=SERVICE_ACCOUNT_FILE,
    )
    print(f"✅ Google Sheets 接続完了: ...{config.OUTPUT_SPREADSHEET_ID[-5:]}")

    while True:
        try:
            found_any = False

            for input_folder_id, doc_type in folder_map.items():
                files = list_files(service, input_folder_id)

                if not files:
                    continue

                found_any = True
                type_label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)
                print(f"\n\n🔎 [{type_label}] 新しいファイルを検出しました！")

                for file in files:
                    file_id = file['id']
                    file_name = file['name']
                    md5 = file.get('md5Checksum')

                    # 1. 防重檢測
                    if is_duplicate_file(service, md5):
                        print(f"⚠️ 重複アップロードを検出: {file_name}")
                        print("   -> 処理をスキップしてアーカイブします")
                        move_file(service, file_id, input_folder_id, PROCESSED_FOLDER_ID)
                        print("=" * 30)
                        continue

                    # 2. 獲取上傳者信息
                    user_info = file.get('lastModifyingUser', {})
                    email = user_info.get('emailAddress', '')
                    display_name = user_info.get('displayName', 'Unknown')

                    user_data = config.EMPLOYEE_MAP.get(email, {})
                    uploader_name = user_data.get("name", display_name)
                    chat_id = user_data.get("chat_id")

                    # 3. 格式過濾
                    ext = os.path.splitext(file_name)[1].lower()
                    if ext not in config.SUPPORTED_EXTENSIONS:
                        print(f"⚠️ 未対応のフォーマットです: {file_name}")
                        continue

                    # 4. 下載與處理
                    local_path = download_file(service, file_id, file_name)

                    success = process_file(
                        service, sheets_writer, local_path,
                        uploader_name, chat_id,
                        doc_type=doc_type, drive_file_id=file_id
                    )

                    if success:
                        move_file(service, file_id, input_folder_id, PROCESSED_FOLDER_ID)
                    else:
                        print("⚠️ ファイル処理失敗。")

                    if os.path.exists(local_path):
                        os.remove(local_path)
                        print("🧹 一時ファイルを削除しました")

                    # 取引No を Sheets に書き戻す
                    sheets_writer.flush()

                    print("=" * 30)

            if not found_any:
                print(".", end="", flush=True)
                if int(time.time()) % 60 == 0:
                    print("")

            time.sleep(config.SCAN_INTERVAL)

        except Exception as e:
            print(f"\n❌ システムエラー: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
