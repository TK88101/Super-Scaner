import os
import time
import io
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# 引入我們的模塊
from ocr_engine import process_pipeline
from csv_writer import append_to_csv
from notifier import send_notification
from doc_types import DocType, DOC_TYPE_CONFIG
import config

# ================= 配置區域 =================
load_dotenv()
PROCESSED_FOLDER_ID = os.getenv("PROCESSED_FOLDER_ID")
CSV_FOLDER_ID = os.getenv("CSV_FOLDER_ID")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")
LOCAL_DOWNLOAD_DIR = './temp_downloads'
CSV_FILENAME = "MF_Import_Data.csv"

if not PROCESSED_FOLDER_ID or not CSV_FOLDER_ID or not SERVICE_ACCOUNT_FILE:
    print("❌ エラー：.envファイルの設定を確認してください (配置錯誤)")
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


def list_files(service, folder_id):
    query = f"'{folder_id}' in parents and trashed = false"
    results = service.files().list(
        q=query,
        orderBy='createdTime',
        fields="nextPageToken, files(id, name, lastModifyingUser, md5Checksum)"
    ).execute()
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
        results = service.files().list(
            q=query,
            orderBy='createdTime desc',
            pageSize=200,
            fields="files(id, name, md5Checksum)"
        ).execute()

        files = results.get('files', [])
        for file in files:
            if file.get('md5Checksum') == md5_checksum:
                print(f"🔍 本地比對發現重複: {file.get('name')}")
                return True

        return False

    except Exception as e:
        print(f"⚠️ 查重步驟發生未知錯誤: {e}")
        return False


def ensure_latest_csv_from_drive(service):
    query = f"name = '{CSV_FILENAME}' and '{CSV_FOLDER_ID}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get('files', [])

    if files:
        file_id = files[0]['id']
        print(f"🔄 クラウドから最新のCSVを同期中...")
        request = service.files().get_media(fileId=file_id)
        with io.FileIO(CSV_FILENAME, 'wb') as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
        return file_id
    else:
        return None


def sync_csv_to_drive(service, existing_file_id=None):
    print("☁️  最新のCSVをクラウドへアップロード中...")
    media = MediaFileUpload(CSV_FILENAME, mimetype='text/csv')
    try:
        if existing_file_id:
            service.files().update(fileId=existing_file_id, media_body=media).execute()
            print("✅ クラウド上のCSVを更新しました (Update)")
        else:
            query = f"name = '{CSV_FILENAME}' and '{CSV_FOLDER_ID}' in parents and trashed = false"
            results = service.files().list(q=query, fields="files(id)").execute()
            files = results.get('files', [])
            if files:
                service.files().update(fileId=files[0]['id'], media_body=media).execute()
                print("✅ クラウド上のCSVを更新しました (Update)")
            else:
                file_metadata = {'name': CSV_FILENAME, 'parents': [CSV_FOLDER_ID]}
                service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                print("✅ クラウド上に新しいCSVを作成しました (Create)")
    except Exception as e:
        print(f"⚠️ クラウド同期エラー: {e}")


def process_file(service, file_path, uploader_name, chat_id, doc_type=DocType.RECEIPT):
    """ファイルを処理し、CSV に書き込み、通知を送信する"""
    type_label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)
    print(f"⚙️  処理開始: {os.path.basename(file_path)} [{type_label}] (担当: {uploader_name})")

    result = process_pipeline(file_path, doc_type=doc_type)

    if result:
        # マルチドキュメント対応: list に正規化
        if isinstance(result, list):
            results = result
        else:
            results = [result]

        print("\n" + "=" * 15 + " 🎯 解析結果 " + "=" * 15)
        for idx, r in enumerate(results):
            if len(results) > 1:
                print(f"\n📄 文書 {idx+1}/{len(results)}:")
            print(f"📅 日付: {r.get('date')}")
            print(f"🏪 取引先: {r.get('vendor')}")
            print(f"📋 文書タイプ: {type_label}")
            entries = r.get('entries', [])
            print(f"📊 仕訳行数: {len(entries)}")
        print("=" * 40 + "\n")

        # 寫入 CSV
        file_id = ensure_latest_csv_from_drive(service)
        for r in results:
            r['uploader'] = uploader_name
            append_to_csv(r)
        sync_csv_to_drive(service, existing_file_id=file_id)

        # 金額集計（全文書合算）
        total_amount = sum(
            sum(int(e.get('amount', 0)) for e in r.get('entries', []))
            for r in results
        )
        vendor_list = ", ".join(r.get('vendor', '') for r in results)

        send_notification(
            filename=os.path.basename(file_path),
            status="Success",
            uploader_name=uploader_name,
            chat_id=chat_id,
            details=f"文書タイプ: {type_label}\n取引先: {vendor_list}\n合計金額: ¥{total_amount}\n文書数: {len(results)}"
        )

        return True
    else:
        send_notification(
            filename=os.path.basename(file_path),
            status="Failed",
            uploader_name=uploader_name,
            chat_id=chat_id,
            details="AIによる解析に失敗しました。ファイルを確認してください。"
        )
        print("⚠️ 解析に失敗しました")
        return False


def main():
    print("🚀 Super Scaner 自動化システム起動！(Multi-Type)")
    print(f"📂 監視フォルダ数: {len(folder_map)}")
    for fid, dtype in folder_map.items():
        label = DOC_TYPE_CONFIG.get(dtype, {}).get("label", dtype)
        print(f"   - {label}: ...{fid[-5:]}")
    print("-" * 30)

    service = get_drive_service()

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

                    if file_name == CSV_FILENAME:
                        continue

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

                    success = process_file(service, local_path, uploader_name, chat_id, doc_type=doc_type)

                    if success:
                        move_file(service, file_id, input_folder_id, PROCESSED_FOLDER_ID)
                    else:
                        print("⚠️ ファイル処理失敗。")

                    if os.path.exists(local_path):
                        os.remove(local_path)
                        print("🧹 一時ファイルを削除しました")

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
