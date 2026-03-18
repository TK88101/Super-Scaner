import os
import time
import io
import random
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

# 引入我們的模塊
from ocr_engine import process_pipeline, _split_pdf_pages, _get_mime_type
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


def upload_page_to_drive(service, page_bytes, filename, folder_id):
    """単ページ PDF を Drive にアップロードし、webViewLink を返す"""
    if not folder_id:
        return ""
    try:
        media = MediaInMemoryUpload(page_bytes, mimetype='application/pdf')
        file_metadata = {'name': filename, 'parents': [folder_id]}
        uploaded = _call_with_retry(lambda: service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink'
        ).execute())
        link = uploaded.get('webViewLink', '')
        print(f"📤 単ページPDFアップロード: {filename}")
        return link
    except Exception as e:
        print(f"⚠️ 単ページPDFアップロード失敗: {e}")
        return ""


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
    """ファイルを処理し、Google Sheets に書き込み、通知を送信する"""
    type_label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)
    filename = os.path.basename(file_path)
    print(f"⚙️  処理開始: {filename} [{type_label}] (担当: {uploader_name})")

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

        # 単ページ PDF アップロード → source_url 生成
        split_folder = config.SPLIT_PDF_FOLDER_ID
        mime_type = _get_mime_type(file_path)

        # 多ページ PDF の場合：各ページを Drive にアップロード
        page_urls = {}
        if mime_type == "application/pdf":
            page_payloads = _split_pdf_pages(file_path)
            if page_payloads and split_folder:
                for page_info in page_payloads:
                    url = upload_page_to_drive(
                        service, page_info["data"],
                        page_info["filename"], split_folder
                    )
                    page_urls[page_info["page_num"]] = url

        # source_url: 単ページ/画像は元ファイルの Drive URL を使用
        if not page_urls and drive_file_id:
            default_source_url = f"https://drive.google.com/file/d/{drive_file_id}/view"
        else:
            default_source_url = ""

        # Google Sheets に書き込み
        for idx, r in enumerate(results):
            r['uploader'] = uploader_name
            # ページ番号に基づく source_url（多ページ PDF の場合）
            source_url = page_urls.get(idx + 1, default_source_url)
            sheets_writer.append_entries(
                employee_name=uploader_name,
                doc_type=doc_type,
                entries_data=r,
                source_url=source_url,
            )

        # 金額集計（全文書合算）
        total_amount = sum(
            sum(int(e.get('amount', 0)) for e in r.get('entries', []))
            for r in results
        )
        vendor_list = ", ".join(r.get('vendor', '') for r in results)

        send_notification(
            filename=filename,
            status="Success",
            uploader_name=uploader_name,
            chat_id=chat_id,
            details=f"文書タイプ: {type_label}\n取引先: {vendor_list}\n合計金額: ¥{total_amount}\n文書数: {len(results)}"
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
