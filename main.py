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
import config                          

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
    # ⭐ 修改：請求 md5Checksum 和 lastModifyingUser
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

# === ⭐ 新增：MD5 查重函數 ===
def is_duplicate_file(service, md5_checksum):
    """
    檢查 Processed 文件夾中是否已存在相同指紋的文件。
    策略：不再使用 API 搜索 MD5 (容易報錯)，而是列出歸檔文件夾的文件，在本地進行比對。
    """
    if not md5_checksum:
        return False
        
    try:
        # 1. 查詢 Processed 文件夾裡的文件 (只查最近的 50 個，按時間倒序)
        # 這樣寫法絕對安全，不會報 400 錯誤
        query = f"'{PROCESSED_FOLDER_ID}' in parents and trashed = false"
        
        results = service.files().list(
            q=query, 
            orderBy='createdTime desc', 
            pageSize=200,                
            fields="files(id, name, md5Checksum)"
        ).execute()
        
        files = results.get('files', [])
        
        # 2. 在 Python 本地進行指紋比對
        for file in files:
            if file.get('md5Checksum') == md5_checksum:
                print(f"🔍 本地比對發現重複: {file.get('name')}")
                return True # 找到了！確實重複
                
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

# === ⭐ 修改：接收 uploader 信息 ===
def process_file_mock(service, file_path, uploader_name, chat_id):
    print(f"⚙️  処理開始: {os.path.basename(file_path)} (担当: {uploader_name})")
    
    result = process_pipeline(file_path)
    
    if result:
        print("\n" + "="*15 + " 🎯 解析結果 " + "="*15)
        print(f"📅 日付: {result.get('date')}")
        print(f"🏪 取引先: {result.get('vendor')}")
        print("="*40 + "\n")
        
        # 寫入 CSV (將上傳者名字傳給 csv_writer)
        result['uploader'] = uploader_name # <--- 注入名字到數據中
        
        file_id = ensure_latest_csv_from_drive(service)
        append_to_csv(result)
        sync_csv_to_drive(service, existing_file_id=file_id)
        
        # === ⭐ 發送成功通知 ===
        amount_info = 0
        for item in result.get('split_items', []):
            amount_info += int(item.get('amount', 0))
            
        send_notification(
            filename=os.path.basename(file_path),
            status="Success",
            uploader_name=uploader_name,
            chat_id=chat_id,
            details=f"店舗: {result.get('vendor')}\n合計金額: ¥{amount_info}"
        )
        
        return True
    else:
        # === ⭐ 發送失敗通知 ===
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
    print("🚀 Super Scaner 自動化システム起動！(Full Features)")
    print(f"📂 監視フォルダID: ...{INPUT_FOLDER_ID[-5:]}")
    print("-" * 30)

    service = get_drive_service()

    while True:
        try:
            files = list_files(service, INPUT_FOLDER_ID)
            
            if not files:
                print(".", end="", flush=True)
                if int(time.time()) % 60 == 0: print("")
                time.sleep(config.SCAN_INTERVAL)
                continue
            
            print("\n\n🔎 新しいファイルを検出しました！")

            for file in files:
                file_id = file['id']
                file_name = file['name']
                md5 = file.get('md5Checksum')
                
                if file_name == CSV_FILENAME: continue

                # === 1. 防重檢測 ===
                if is_duplicate_file(service, md5):
                    print(f"⚠️ 重複アップロードを検出: {file_name}")
                    print("   -> 処理をスキップしてアーカイブします")
                    move_file(service, file_id, INPUT_FOLDER_ID, PROCESSED_FOLDER_ID)
                    print("=" * 30)
                    continue 

                # === 2. 獲取上傳者信息 ===
                user_info = file.get('lastModifyingUser', {})
                email = user_info.get('emailAddress', '')
                display_name = user_info.get('displayName', 'Unknown')
                
                # 查表找人
                user_data = config.EMPLOYEE_MAP.get(email, {})
                uploader_name = user_data.get("name", display_name)
                chat_id = user_data.get("chat_id")

                # === 3. 格式過濾 ===
                ext = os.path.splitext(file_name)[1].lower()
                if ext not in config.SUPPORTED_EXTENSIONS:
                    print(f"⚠️ 未対応のフォーマットです: {file_name}")
                    continue 

                # === 4. 下載與處理 ===
                local_path = download_file(service, file_id, file_name)
                
                # 傳入 uploader_name 和 chat_id
                success = process_file_mock(service, local_path, uploader_name, chat_id)
                
                if success:
                    move_file(service, file_id, INPUT_FOLDER_ID, PROCESSED_FOLDER_ID)
                else:
                    print("⚠️ ファイル処理失敗。")

                if os.path.exists(local_path):
                    os.remove(local_path)
                    print("🧹 一時ファイルを削除しました")
                
                print("=" * 30)

        except Exception as e:
            print(f"\n❌ システムエラー: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()