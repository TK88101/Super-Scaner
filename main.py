import os
import time
import io
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

from ocr_engine import process_pipeline
from csv_writer import append_to_csv

# ================= 配置區域 =================
load_dotenv()
INPUT_FOLDER_ID = os.getenv("INPUT_FOLDER_ID")
PROCESSED_FOLDER_ID = os.getenv("PROCESSED_FOLDER_ID")
CSV_FOLDER_ID = os.getenv("CSV_FOLDER_ID")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")
LOCAL_DOWNLOAD_DIR = './temp_downloads'
CSV_FILENAME = "MF_Import_Data.csv"

if not INPUT_FOLDER_ID or not PROCESSED_FOLDER_ID or not CSV_FOLDER_ID or not SERVICE_ACCOUNT_FILE:
    print("❌ エラー：.envファイルの設定を確認してください (配置錯誤)")
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
        q=query, fields="nextPageToken, files(id, name)"
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

def sync_csv_to_drive(service):
    """
    將 CSV 同步到雲端 (日語日誌)
    """
    print("☁️  最新のCSVをクラウドへ同期中...")
    
    try:
        query = f"name = '{CSV_FILENAME}' and '{CSV_FOLDER_ID}' in parents and trashed = false"
        results = service.files().list(q=query, fields="files(id)").execute()
        files = results.get('files', [])

        media = MediaFileUpload(CSV_FILENAME, mimetype='text/csv')

        if not files:
            # 創建新文件
            file_metadata = {'name': CSV_FILENAME, 'parents': [CSV_FOLDER_ID]}
            service.files().create(body=file_metadata, media_body=media, fields='id').execute()
            print("✅ クラウド上に新しいCSVを作成しました")
        else:
            # 更新現有文件
            file_id = files[0]['id']
            service.files().update(fileId=file_id, media_body=media).execute()
            print("✅ クラウド上のCSVを更新しました")

    except Exception as e:
        print(f"⚠️ クラウド同期エラー (権限/容量の問題の可能性があります): {e}")
        print("👉 ローカルのCSV (MF_Import_Data.csv) は正常に保存されています。")

def process_file_mock(service, file_path):
    print(f"⚙️  処理開始: {os.path.basename(file_path)}")
    
    result = process_pipeline(file_path)
    
    if result:
        print("\n" + "="*15 + " 🎯 解析結果 " + "="*15)
        print(f"📅 日付: {result.get('date')}")
        print(f"🏪 取引先: {result.get('vendor')}")
        # 為了簡潔，這裡只打印基本信息，詳細信息在CSV中
        print("="*40 + "\n")
        
        # 1. 寫入本地 CSV
        append_to_csv(result)
        
        # 2. 同步上傳
        sync_csv_to_drive(service)
        
        return True
    else:
        print("⚠️ 解析に失敗しました")
        return False

def main():
    print("🚀 Super Scaner 自動化システム起動！")
    print(f"📂 監視フォルダID: ...{INPUT_FOLDER_ID[-5:]}")
    print("-" * 30)

    service = get_drive_service()

    while True:
        try:
            files = list_files(service, INPUT_FOLDER_ID)
            
            if not files:
                print(".", end="", flush=True)
                time.sleep(3)
                continue
            
            print("\n\n🔎 新しいファイルを検出しました！")

            for file in files:
                file_id = file['id']
                file_name = file['name']
                
                if file_name == CSV_FILENAME: continue

                # 後綴名過濾
                ext = os.path.splitext(file_name)[1].lower()
                SUPPORTED_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.heic', '.pdf']
                
                if ext not in SUPPORTED_EXTENSIONS:
                    print(f"⚠️ 未対応のフォーマットです。スキップします: {file_name}")
                    continue 

                local_path = download_file(service, file_id, file_name)
                
                # 處理流程
                success = process_file_mock(service, local_path)
                
                if success:
                    move_file(service, file_id, INPUT_FOLDER_ID, PROCESSED_FOLDER_ID)
                else:
                    print("⚠️ ファイル処理失敗。ログを確認してください。")

                if os.path.exists(local_path):
                    os.remove(local_path)
                    print("🧹 一時ファイルを削除しました")
                
                print("=" * 30)

        except Exception as e:
            print(f"\n❌ システムエラー: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()