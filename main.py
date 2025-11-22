import os
import time
import io
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

from ocr_engine import process_pipeline
from csv_writer import append_to_csv

# ================= 1. 加載配置 =================
load_dotenv()
INPUT_FOLDER_ID = os.getenv("INPUT_FOLDER_ID")
PROCESSED_FOLDER_ID = os.getenv("PROCESSED_FOLDER_ID")
CSV_FOLDER_ID = os.getenv("CSV_FOLDER_ID")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")
LOCAL_DOWNLOAD_DIR = './temp_downloads'
CSV_FILENAME = "MF_Import_Data.csv"

# 檢查配置是否完整
if not INPUT_FOLDER_ID or not PROCESSED_FOLDER_ID or not CSV_FOLDER_ID or not SERVICE_ACCOUNT_FILE:
    print("❌ 錯誤：請檢查 .env 文件配置，確保包含 INPUT, PROCESSED, CSV 三個文件夾的 ID")
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
    
    print(f"⬇️  正在下載: {file_name} ...")
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
        print(f"📦 原始圖片已歸檔至 Processed 文件夾")
    except Exception as e:
        print(f"⚠️ 移動文件時發生警告: {e}")

def sync_csv_to_drive(service):
    """
    將 CSV 同步到專用的 CSV_Exports 文件夾
    """
    print("☁️  正在將最新 CSV 同步到 [CSV_Exports] 文件夾...")
    
    # 1. 在 CSV 專用文件夾中查找同名文件
    query = f"name = '{CSV_FILENAME}' and '{CSV_FOLDER_ID}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get('files', [])

    media = MediaFileUpload(CSV_FILENAME, mimetype='text/csv')

    if not files:
        # A. 如果沒有：創建新文件
        file_metadata = {
            'name': CSV_FILENAME,
            'parents': [CSV_FOLDER_ID] # <--- 指定傳到新文件夾
        }
        service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        print("✅ 已在雲端創建新的 CSV 報表")
    else:
        # B. 如果有：更新現有文件
        file_id = files[0]['id']
        service.files().update(
            fileId=file_id,
            media_body=media
        ).execute()
        print("✅ 已更新雲端現有的 CSV 報表")

def process_file_mock(service, file_path):
    print(f"⚙️  開始處理文件: {os.path.basename(file_path)}")
    
    result = process_pipeline(file_path)
    
    if result:
        print("\n" + "="*15 + " 🎯 處理結果 " + "="*15)
        print(f"📅 日期: {result.get('date')}")
        print(f"🏪 廠商: {result.get('vendor')}")
        print(f"🧾 包含稅率: {[item['tax_type'] for item in result.get('split_items', [])]}")
        print("="*40 + "\n")
        
        # 1. 寫入本地 CSV
        append_to_csv(result)
        
        # 2. 同步上傳到 CSV 專用文件夾
        sync_csv_to_drive(service)
        
        return True
    else:
        print("⚠️ 處理失敗")
        return False

def main():
    print("🚀 Super Scaner 全自動版啟動！(分離式存儲)")
    print(f"📂 監聽文件夾: {INPUT_FOLDER_ID}")
    print(f"📂 圖片歸檔夾: {PROCESSED_FOLDER_ID}")
    print(f"📂 報表存放夾: {CSV_FOLDER_ID}")
    print("-" * 30)

    service = get_drive_service()

    while True:
        try:
            files = list_files(service, INPUT_FOLDER_ID)
            
            if not files:
                print(".", end="", flush=True)
                time.sleep(3)
                continue
            
            print("\n\n🔎 發現新文件！")

            for file in files:
                file_id = file['id']
                file_name = file['name']
                
                # 1. 防止 CSV 被錯誤下載
                if file_name == CSV_FILENAME:
                    continue

                # 2. 【新增】格式過濾 (白名單檢查)
                ext = os.path.splitext(file_name)[1].lower() # 獲取後綴名 (如 .jpg)
                # 定義支援的格式
                SUPPORTED_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.heic', '.pdf']
                
                if ext not in SUPPORTED_EXTENSIONS:
                    print(f"⚠️ 跳過不支援的文件格式: {file_name}")
                    continue # 直接跳過，不下載也不處理

                # 3. 下載文件
                local_path = download_file(service, file_id, file_name)
                
                # 4. 處理流程
                success = process_file_mock(service, local_path)
                
                if success:
                    # 成功後，把原始圖片移動到 Processed 歸檔
                    move_file(service, file_id, INPUT_FOLDER_ID, PROCESSED_FOLDER_ID)
                
                if os.path.exists(local_path):
                    os.remove(local_path)
                    print("🧹 本地清理完成")
                
                print("=" * 30)

        except Exception as e:
            print(f"\n❌ 錯誤: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()