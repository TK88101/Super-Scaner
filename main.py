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
    """獲取 Google Drive API 服務實例"""
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=creds)

def list_files(service, folder_id):
    """列出指定文件夾中的文件"""
    query = f"'{folder_id}' in parents and trashed = false"
    results = service.files().list(
        q=query, fields="nextPageToken, files(id, name)"
    ).execute()
    return results.get('files', [])

def download_file(service, file_id, file_name):
    """下載文件到本地臨時目錄"""
    if not os.path.exists(LOCAL_DOWNLOAD_DIR):
        os.makedirs(LOCAL_DOWNLOAD_DIR)
    file_path = os.path.join(LOCAL_DOWNLOAD_DIR, file_name)
    request = service.files().get_media(fileId=file_id)
    
    # 日誌：下載中
    print(f"⬇️  ダウンロード中: {file_name} ...")
    fh = io.FileIO(file_path, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
    return file_path

def move_file(service, file_id, previous_folder_id, new_folder_id):
    """將處理完的文件移動到 Processed 文件夾 (歸檔)"""
    try:
        service.files().update(
            fileId=file_id,
            addParents=new_folder_id,
            removeParents=previous_folder_id,
            fields='id, parents'
        ).execute()
        # 日誌：已歸檔
        print(f"📦 元画像を処理済みフォルダ(Processed)へ移動しました")
    except Exception as e:
        print(f"⚠️ ファイル移動中に警告が発生しました: {e}")

# === ⭐ 新增核心函數：確保本地 CSV 是雲端最新的 ===
def ensure_latest_csv_from_drive(service):
    """
    在寫入之前，先嘗試從雲端 (CSV_Exports) 下載最新的 CSV。
    如果雲端有文件，就下載覆蓋本地的，防止財務人員的修改被覆蓋。
    返回值: 找到的文件 ID (如果沒找到則返回 None)
    """
    query = f"name = '{CSV_FILENAME}' and '{CSV_FOLDER_ID}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get('files', [])

    if files:
        file_id = files[0]['id']
        # 日誌：同步最新版中
        print(f"🔄 クラウドから最新のCSVを同期中...")
        
        request = service.files().get_media(fileId=file_id)
        # 下載並覆蓋本地文件
        with io.FileIO(CSV_FILENAME, 'wb') as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
        
        return file_id  # 返回 ID 以便稍後更新
    else:
        return None # 雲端沒有文件，說明需要新建

def sync_csv_to_drive(service, existing_file_id=None):
    """
    將 CSV 同步回雲端
    existing_file_id: 如果有 ID 則執行更新(Update)，否則執行創建(Create)
    """
    # 日誌：上傳中
    print("☁️  最新のCSVをクラウドへアップロード中...")
    
    media = MediaFileUpload(CSV_FILENAME, mimetype='text/csv')

    try:
        if existing_file_id:
            # 如果已知 ID，直接更新該文件 (Update)
            service.files().update(
                fileId=existing_file_id,
                media_body=media
            ).execute()
            # 日誌：更新成功
            print("✅ クラウド上のCSVを更新しました (Update)")
        else:
            # 如果沒有 ID，嘗試再次檢查 (防止並發衝突)
            query = f"name = '{CSV_FILENAME}' and '{CSV_FOLDER_ID}' in parents and trashed = false"
            results = service.files().list(q=query, fields="files(id)").execute()
            files = results.get('files', [])
            
            if files:
                # 發現文件其實存在，更新它
                file_id = files[0]['id']
                service.files().update(fileId=file_id, media_body=media).execute()
                print("✅ クラウド上のCSVを更新しました (Update)")
            else:
                # 真的不存在，創建新文件 (Create)
                # 注意：個人版帳號在此處可能會報錯 (權限不足)，但企業版 Shared Drive 沒問題
                # 解決方案：手動上傳一個空文件即可繞過此處
                file_metadata = {'name': CSV_FILENAME, 'parents': [CSV_FOLDER_ID]}
                service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                print("✅ クラウド上に新しいCSVを作成しました (Create)")

    except Exception as e:
        print(f"⚠️ クラウド同期エラー (権限/容量の問題の可能性があります): {e}")
        print("👉 ローカルのCSV (MF_Import_Data.csv) は正常に保存されています。")

def process_file_mock(service, file_path):
    # 日誌：處理開始
    print(f"⚙️  処理開始: {os.path.basename(file_path)}")
    
    result = process_pipeline(file_path)
    
    if result:
        print("\n" + "="*15 + " 🎯 解析結果 " + "="*15)
        print(f"📅 日付: {result.get('date')}")
        print(f"🏪 取引先: {result.get('vendor')}")
        print("="*40 + "\n")
        
        # === ⭐ 關鍵流程：防覆蓋同步 ===
        # 1. 寫入前，先從雲端拉取最新版覆蓋本地
        file_id = ensure_latest_csv_from_drive(service)
        
        # 2. 在最新版基礎上追加新數據
        append_to_csv(result)
        
        # 3. 將更新後的文件推回雲端
        sync_csv_to_drive(service, existing_file_id=file_id)
        
        return True
    else:
        print("⚠️ 解析に失敗しました")
        return False

def main():
    print("🚀 Super Scaner 自動化システム起動！(完全同期モード)")
    print(f"📂 監視フォルダID: ...{INPUT_FOLDER_ID[-5:]}")
    print("-" * 30)

    service = get_drive_service()

    while True:
        try:
            # 獲取文件列表
            files = list_files(service, INPUT_FOLDER_ID)
            
            if not files:
                print(".", end="", flush=True)
                if int(time.time()) % 60 == 0: 
                    print("")
                time.sleep(3)
                continue
            
            print("\n\n🔎 新しいファイルを検出しました！")

            for file in files:
                file_id = file['id']
                file_name = file['name']
                
                if file_name == CSV_FILENAME: continue

                # 後綴名過濾 (只處理圖片和PDF)
                ext = os.path.splitext(file_name)[1].lower()
                SUPPORTED_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.heic', '.pdf']
                
                if ext not in SUPPORTED_EXTENSIONS:
                    print(f"⚠️ 未対応のフォーマットです。スキップします: {file_name}")
                    continue 

                # 下載文件
                local_path = download_file(service, file_id, file_name)
                
                # 執行處理流程
                success = process_file_mock(service, local_path)
                
                # 如果成功，移動文件到 Processed
                if success:
                    move_file(service, file_id, INPUT_FOLDER_ID, PROCESSED_FOLDER_ID)
                else:
                    print("⚠️ ファイル処理失敗。ログを確認してください。")

                # 清理本地臨時文件
                if os.path.exists(local_path):
                    os.remove(local_path)
                    print("🧹 一時ファイルを削除しました")
                
                print("=" * 30)

        except Exception as e:
            print(f"\n❌ システムエラー: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()