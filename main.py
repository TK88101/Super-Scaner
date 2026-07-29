import os
import sys
import time
import io
import random
import re

# Windows console encoding fix
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from googleapiclient.errors import HttpError
# 引入我們的模塊
from ocr_engine import process_pipeline
from sheets_output import SheetsOutputWriter
from notifier import send_notification
from doc_types import DocType, DOC_TYPE_CONFIG
import config

# ================= 配置區域 =================
load_dotenv()
# Processed / SPLIT_PDF / 出力シートは全て profiles 経由で解決する。
# グローバル定数を残すとプロファイル分離を素通りする経路が生まれるため置かない。
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")
LOCAL_DOWNLOAD_DIR = './temp_downloads'

if not SERVICE_ACCOUNT_FILE:
    print("❌ エラー：.envファイルの設定を確認してください (配置錯誤)")
    exit(1)

# 出力プロファイル読み込み（env が揃ったものだけ有効化される）
profiles = config.load_profiles()
if not profiles:
    print("❌ エラー：有効な出力プロファイルがありません。")
    print("   .env に OUTPUT_SPREADSHEET_ID と PROCESSED_FOLDER_ID を設定してください。")
    exit(1)

# フォルダマッピング読み込み
# 同一フォルダが2プロファイルに割り当てられていたら起動させない（票据の流出防止）
try:
    folder_map = config.load_folder_map(profiles)
except ValueError as e:
    print(f"❌ エラー：{e}")
    exit(1)

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


def upload_file(service, folder_id: str, filename: str, data: bytes,
                mime_type: str = "application/pdf") -> str:
    """バイト列を Drive に新規ファイルとして作成し、その file id を返す。

    単ページ PDF を分割保存先フォルダへ resumable=False でアップロードする用途。
    共有ドライブ(Shared Drive)対応のため supportsAllDrives=True を付与。
    5xx 一時エラーは _call_with_retry で指数バックオフ再試行する。

    引数を検証し、id 欠落時は例外を送出する（呼び出し側の except で
    フォールバックに落とせるよう、サイレント障害を作らない）。
    """
    if not folder_id:
        raise ValueError("folder_id is required")
    if not data:
        raise ValueError("data is empty")
    # MediaIoBaseUpload は io.BytesIO を内包し読み取り位置を進めるため、
    # _call_with_retry のリトライ毎に新しい stream を生成する。
    # （使い回すと2回目以降は EOF で 0 バイトボディの空ファイルになる）
    created = _call_with_retry(lambda: service.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type,
                                     resumable=False),
        supportsAllDrives=True, fields="id").execute())
    fid = created.get("id")
    if not fid:
        raise ValueError(f"Drive create returned no id: {created}")
    return fid


def _drive_view_url(file_id: str) -> str:
    """Drive ファイルの閲覧 URL を組み立てる。"""
    return f"https://drive.google.com/file/d/{file_id}/view"


class PageUrlResolver:
    """ページ番号 → 単ページ Drive ファイルの /view URL を解決する。

    多ページ領収書 PDF は Drive ネイティブビューアが #page=N を無視するため、
    各ページを 1ページ PDF として分割保存先フォルダにアップロードし、その単独
    ファイル(永遠に 1/1)へリンクする。同一ページは一頁多票でも 1回だけ
    アップロードして URL をメモ化共有する。アップロード不能・失敗時は今日の
    挙動(base_url#page=N)へ安全にフォールバックし、例外は伝播させない。

    冪等性: 単ページ名に源 PDF の file id を埋め込み(別 PDF が同じ元名でも衝突
    不能)、ファイル単位で 1回だけ分割保存先を照会して既存ページを再利用する。
    これにより処理途中のクラッシュ・再実行で同名ファイルが Drive に重複増殖
    するのを防ぐ。
    """

    def __init__(self, service, base_url: str, original_filename: str,
                 folder_id: str, source_file_id: str = ""):
        self._service = service
        self._base_url = base_url
        self._original_filename = original_filename
        self._folder_id = folder_id
        self._source_file_id = source_file_id or ""
        self._cache: dict[int, str] = {}
        # 既存ページ {page_num: file_id}（遅延照会、ファイル単位で1回だけ）
        self._existing: dict[int, str] | None = None

    def _source_marker(self) -> str:
        """単ページ名に埋め込む「源 file id」セグメント。

        命名(_page_filename)と既存照会(_load_existing)で同一規約を共有する
        ため、ここを唯一の出所にする（片方だけ変えて齟齬が出るのを防ぐ）。
        """
        return f"__{self._source_file_id}_p"

    def _page_filename(self, page_num: int) -> str:
        stem = os.path.splitext(self._original_filename)[0]
        # スラッシュ・バックスラッシュ・制御文字を除去
        # （Drive ファイル名としての誤解釈・表示崩れを防ぐ）
        stem = "".join(
            "_" if (c in "/\\" or ord(c) < 0x20) else c for c in stem)
        # 源 file id を埋め込み命名で衝突不能にする（別 PDF が同名でも区別）
        return f"{stem}{self._source_marker()}{page_num}.pdf"

    def _load_existing(self) -> None:
        """分割保存先からこの源 PDF 由来の既存単ページを 1回だけ照会する。

        再実行・クラッシュ復帰時に既存ページを再利用して重複アップロードを
        防ぐ。照会失敗時は空とみなし新規アップロードに倒す（劣化してもリンクは
        生成される）。
        """
        if self._existing is not None:
            return
        self._existing = {}
        if not self._folder_id or not self._source_file_id:
            return
        marker = self._source_marker()
        try:
            query = (f"'{self._folder_id}' in parents and trashed = false "
                     f"and name contains '{self._source_file_id}'")
            results = _call_with_retry(lambda: self._service.files().list(
                q=query, pageSize=1000,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
                fields="files(id, name)").execute())
            for f in results.get("files", []):
                name = f.get("name", "")
                if marker not in name:
                    continue  # 別 id の部分一致を除外
                m = re.search(r"_p(\d+)\.pdf$", name)
                if m:
                    self._existing[int(m.group(1))] = f["id"]
        except Exception as e:  # noqa: BLE001 - 失敗時は新規アップロードへ倒す
            print(f"⚠️ 既存単ページの照会失敗: "
                  f"{type(e).__name__}: {str(e)[:120]} → 新規アップロードで継続")
            self._existing = {}

    def resolve(self, page_num: int, total_pages: int,
                page_bytes: bytes | None) -> str:
        # 単ページ文書 / base_url 無し → アップロード不要、そのまま返す
        if total_pages <= 1 or not self._base_url:
            return self._base_url

        # 一頁多票: 同一ページは 1回だけ解決し URL を共有
        if page_num in self._cache:
            return self._cache[page_num]

        # 今日の挙動(死んだアンカー)。劣化先として常に安全
        fallback = f"{self._base_url}#page={page_num}"

        if not self._folder_id or not page_bytes:
            url = fallback
        else:
            try:
                self._load_existing()
                fid = self._existing.get(page_num)
                if not fid:
                    # 既存が無いページのみ新規アップロード（冪等）
                    fid = upload_file(
                        self._service, self._folder_id,
                        self._page_filename(page_num), page_bytes)
                    self._existing[page_num] = fid
                url = _drive_view_url(fid)
            except Exception as e:  # noqa: BLE001 - 明示的に握り潰しフォールバック
                print(f"⚠️ 単ページPDF解決失敗 (p{page_num}): "
                      f"{type(e).__name__}: {str(e)[:120]} → #page= に劣化")
                url = fallback

        self._cache[page_num] = url
        return url


def list_files(service, folder_id):
    query = f"'{folder_id}' in parents and trashed = false"
    results = _call_with_retry(lambda: service.files().list(
        q=query,
        orderBy='createdTime',
        # 共有ドライブ(Shared Drive)対応。両方必須:
        #   supportsAllDrives だけでは list は「0件」を黙って返す(エラー無し)。
        #   個人 My Drive でもこの2フラグは無害なので常時付与し両対応にする。
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        fields="nextPageToken, files(id, name, lastModifyingUser, md5Checksum)"
    ).execute())
    return results.get('files', [])


def download_file(service, file_id, file_name):
    if not os.path.exists(LOCAL_DOWNLOAD_DIR):
        os.makedirs(LOCAL_DOWNLOAD_DIR)
    file_path = os.path.join(LOCAL_DOWNLOAD_DIR, file_name)
    # 共有ドライブ対応: supportsAllDrives が無いと共有ドライブ上のファイル DL が 404/403
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)

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
            # 共有ドライブ対応: 共有ドライブ内/への移動には supportsAllDrives が必須
            supportsAllDrives=True,
            fields='id, parents'
        ).execute()
        print(f"📦 元画像を処理済みフォルダ(Processed)へ移動しました")
    except Exception as e:
        print(f"⚠️ ファイル移動中に警告が発生しました: {e}")



def is_duplicate_file(service, md5_checksum, processed_folder_id):
    """指定された Processed フォルダ中の重複チェック。

    processed_folder_id はプロファイルごとに異なる。グローバル定数を見ると
    社長専用フォルダへアーカイブ済みの票据を検出できず二重記帳になるため、
    呼び出し側が必ず対応するプロファイルの値を渡すこと。
    """
    if not md5_checksum or not processed_folder_id:
        return False

    try:
        query = f"'{processed_folder_id}' in parents and trashed = false"
        results = _call_with_retry(lambda: service.files().list(
            q=query,
            orderBy='createdTime desc',
            pageSize=200,
            # 共有ドライブ対応（list は supportsAllDrives だけだと0件になる）
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
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
                  doc_type=DocType.RECEIPT, drive_file_id=None,
                  split_pdf_folder_id=""):
    """ファイルを処理し、Google Sheets に逐次書き込み、通知を送信する。

    process_pipeline がジェネレータなので、1ページ処理→即Sheets書き込み→
    メモリ解放→次ページ の流れでメモリ使用量を最小化する。

    split_pdf_folder_id もプロファイルごとに異なる。共通の分割保存先へ
    アップロードすると、社長専用シートの原票 URL が全社から閲覧可能になる。
    """
    type_label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)
    filename = os.path.basename(file_path)
    print(f"⚙️  処理開始: {filename} [{type_label}] (担当: {uploader_name})")

    base_url = ""
    if drive_file_id:
        base_url = _drive_view_url(drive_file_id)

    # 多ページ領収書 PDF のページ単位ディープリンク解決器
    # （ページ毎に単ページ PDF を分割保存先へアップロードし永続リンク化）
    resolver = PageUrlResolver(
        service, base_url, filename, split_pdf_folder_id, drive_file_id)

    total_amount = 0
    vendor_names = []
    count = 0
    total_entries = 0
    error_pages = 0
    failed_page_nums = []
    excluded_pages = 0
    excluded_page_nums = []

    for page in process_pipeline(file_path, doc_type=doc_type):
        result = page["result"]
        page_num = page["page_num"]
        total_pages = page["total_pages"]
        count += 1
        # 再試可能なページエラーは Sheets へ書き込まない
        # （全頁失敗時は Failed を返しファイルを保持するため、
        #  次回再試行で同じページの占位行が重複生成されるのを防ぐ）
        if result.get("_page_error"):
            error_pages += 1
            failed_page_nums.append(page_num)
            continue

        # IP-401 T1: 封筒等の除外ページは MF データ区に一切書かない。
        # ここで continue しないと entries=[] かつ _unrecognized 無しのため
        # sheets_output の最終防衛 (_write_unrecognized_row) に落ち、取引No を
        # 消費して赤い「認識不能」占位行が MF 区に混ざる（Plan §3.2 違反）。
        # error_pages には数えない（除外は失敗ではない）。count には既に
        # 数えており、全頁封筒でも count>0 → Failed 無限リトライを回避する。
        # 留痕先（監査タブ）への振り分けは T2 で実装する。
        if result.get("_excluded_page"):
            excluded_pages += 1
            excluded_page_nums.append(page_num)
            print(f"📨 [{page_num}/{total_pages}] 除外ページ "
                  f"({result.get('_exclude_reason', 'unknown')}) → MF区には書きません")
            continue

        entries = result.get('entries', [])
        print(f"📄 [{page_num}/{total_pages}] 取引先: {result.get('vendor')} | "
              f"仕訳: {len(entries)}行")

        # 即座に Google Sheets へ書き込み
        # ページ専用の単ページ PDF へリンク（多ページ時のみ実アップロード、
        # 単ページ/画像/folder未設定/失敗時は base_url または #page= に劣化）
        page_bytes = page.get("page_bytes")
        source_url = resolver.resolve(page_num, total_pages, page_bytes)
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

    # 全ページがエラー → 上流障害とみなし Failed（再試行対象として残す）
    # count == error_pages で判定（total_entries ではない）:
    # _unrecognized ページ（封筒・パンフレット等）も entries=0 を生むため、
    # total_entries==0 だけで判定すると「正常な _unrecognized + 1頁エラー」の
    # 混合ケースが無限再試行ループに入り、毎回占位行が重複増殖する。
    if count > 0 and error_pages == count:
        send_notification(
            filename=filename,
            status="Failed",
            uploader_name=uploader_name,
            chat_id=chat_id,
            details=f"全ページ処理エラー（{error_pages}/{count}頁）。API障害または認証エラーの可能性。ファイルは保持されます。"
        )
        print(f"⚠️ 全ページ処理エラー: {error_pages}/{count} → Failed（ファイル保持）")
        return False

    # 部分ページエラー: 成功頁は既に書き込み済み、失敗頁は占位行で可視化
    # ファイルは歸檔（重試による重複行を防ぐため）、人手で失敗頁を再スキャン要
    partial_error = error_pages > 0 and error_pages < count
    if partial_error:
        failed_pages_str = ",".join(f"p{n}" for n in failed_page_nums)
        try:
            sheets_writer.append_entries(
                employee_name=uploader_name,
                doc_type=doc_type,
                entries_data={
                    "entries": [],
                    "_unrecognized": True,
                    "memo": f"⚠ ページ処理エラー {error_pages}/{count}頁 [{failed_pages_str}] 手動再スキャン要",
                    "date": "",
                    "vendor": filename,
                    "uploader": uploader_name,
                },
                source_url=base_url,
            )
        except Exception as e:
            print(f"⚠️ 部分エラー占位行の書き込み失敗: {e}")

    if count > 0:
        vendor_list = ", ".join(v for v in vendor_names if v)
        print(f"\n✅ 処理完了: {count}文書 / {total_entries}仕訳")
        if excluded_pages:
            excluded_pages_str = ",".join(f"p{n}" for n in excluded_page_nums)
            print(f"📨 除外ページ: {excluded_pages}/{count}頁 [{excluded_pages_str}]")
        if partial_error:
            failed_pages_str = ",".join(f"p{n}" for n in failed_page_nums)
            print(f"⚠️ 部分ページエラー: {error_pages}/{count}頁失敗 [{failed_pages_str}]")
            details = (
                f"⚠ 部分ページ処理エラー {error_pages}/{count}頁\n"
                f"失敗頁: {failed_pages_str}\n"
                f"該当頁を手動で再スキャンしてください（該当頁以外は成功）\n"
                f"---\n"
                f"文書タイプ: {type_label}\n取引先: {vendor_list}\n"
                f"合計金額: ¥{total_amount}\n文書数: {count}"
            )
        else:
            details = f"文書タイプ: {type_label}\n取引先: {vendor_list}\n合計金額: ¥{total_amount}\n文書数: {count}"
        send_notification(
            filename=filename,
            status="Success",
            uploader_name=uploader_name,
            chat_id=chat_id,
            details=details,
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


# シート接続の再試行間隔（秒）。合計 15 秒。
# sheets_output.SheetsOutputWriter.__init__ 内の open_by_key() は、本リポジトリで
# 唯一リトライ保護の無い Google 呼び出しだった (main._call_with_retry /
# sheets_output._write_with_retry がそれ以外を守っている)。保護が無いと、02:00 の
# 定時再起動が Google の一時的な 503 に当たっただけで、そのプロファイルが次の
# 再起動まで最長 24 時間沈黙する。
_SHEET_OPEN_RETRY_DELAYS = [1, 2, 4, 8]


def _is_transient_sheet_error(err):
    """再試行する価値のあるエラーか。

    一時: HTTP 429 / 5xx、ネットワーク断。→ 待てば直る。
    恒久: HTTP 403 / 404、SpreadsheetNotFound (共有剥奪・シート削除)。
          → 何度試しても直らないので即諦め、起動を 15 秒無駄にしない。
    """
    status = getattr(getattr(err, "response", None), "status_code", None)
    if status is not None:
        return status == 429 or 500 <= status < 600
    return isinstance(err, (ConnectionError, TimeoutError, OSError))


def open_writer_with_retry(profile, credentials_file, delays=None):
    """SheetsOutputWriter を作る。一時エラーのみ指数バックオフで再試行する。

    SheetsOutputWriter の構築は冪等 (open_by_key も空シート削除も再実行安全) な
    ため、丸ごと再試行してよい。恒久エラーは再試行せず送出し、呼び出し側で
    そのプロファイルだけを縮退させる。
    """
    if delays is None:
        delays = _SHEET_OPEN_RETRY_DELAYS

    last_err = None
    for attempt, delay in enumerate([0] + list(delays)):
        if delay:
            print(f"   ⏳ {delay}s 後に再試行 ({attempt}/{len(delays)})")
            time.sleep(delay)
        try:
            return SheetsOutputWriter(
                spreadsheet_id=profile["spreadsheet_id"],
                credentials_file=credentials_file,
            )
        except Exception as e:
            if not _is_transient_sheet_error(e):
                raise
            last_err = e
            print(f"   ⚠️ シート接続の一時エラー: {e}")
    raise last_err


def build_writers(profiles, credentials_file):
    """プロファイルごとに SheetsOutputWriter を作る。開けないシートは飛ばす。

    SheetsOutputWriter.__init__ は gc.open_by_key() で実際にネットワークへ出る。
    社長専用シートは Drive の「アクセス制限」フォルダ内にあり共有設定が手動の
    ため、共有を外すと開けなくなる。ここで例外を素通しすると健全な共通通道
    まで巻き添えで落ち、タスクスケジューラが起動クラッシュループに入る。
    load_profiles() の env 欠落降格と同じ粒度で、シート単位でも縮退させる。

    一時エラーは open_writer_with_retry が吸収するので、ここまで来るのは
    恒久エラーか、再試行を使い切った一時エラーのみ。

    Returns: {profile_key: writer}。全滅なら空 dict（呼び出し側が exit を判断）。
    """
    writers = {}
    for profile_key, profile in profiles.items():
        try:
            writers[profile_key] = open_writer_with_retry(profile, credentials_file)
            print(f"✅ Google Sheets 接続完了 [{profile['label']}]: "
                  f"...{profile['spreadsheet_id'][-5:]}")
        except Exception as e:
            print(f"⚠️ [{profile['label']}] シートを開けません: {e}")
            print("   -> このプロファイルを無効化して継続します。"
                  "共有設定(SA がコンテンツ管理者か)を確認してください。")
    return writers


def filter_active_folders(folder_map, writers):
    """writer を作れなかったプロファイルの入力フォルダを監視対象から外す。"""
    return {fid: entry for fid, entry in folder_map.items()
            if entry[1] in writers}


def main():
    print("🚀 Super Scaner 自動化システム起動！(Sheets出力版)")

    service = get_drive_service()

    # Google Sheets 出力ライター初期化（プロファイルごとに1インスタンス）
    # SheetsOutputWriter は 1 インスタンス = 1 スプレッドシート。取引No も
    # タブ単位で算出されるため、プロファイル間で番号が混ざることはない。
    writers = build_writers(profiles, SERVICE_ACCOUNT_FILE)
    if not writers:
        print("❌ エラー：どの出力シートにも接続できませんでした。")
        exit(1)

    active_folder_map = filter_active_folders(folder_map, writers)
    if not active_folder_map:
        # 接続できたプロファイルに入力フォルダが1つも無い状態。ループに入ると
        # ドットを打ち続けるだけで、外からは正常稼働と区別がつかない
        # (Chatwork 通知は廃止済みで監視手段が無い)。落として気付かせる。
        print("❌ エラー：監視可能な入力フォルダがありません。")
        print("   接続できたシートのプロファイルに入力フォルダが設定されていません。")
        exit(1)

    print("-" * 30)
    print(f"📂 監視フォルダ数: {len(active_folder_map)} / "
          f"有効プロファイル数: {len(writers)}")
    for profile_key in writers:
        profile = profiles[profile_key]
        print(f"   [{profile['label']}] → シート ...{profile['spreadsheet_id'][-5:]}")
        for fid, (dtype, pkey) in active_folder_map.items():
            if pkey != profile_key:
                continue
            label = DOC_TYPE_CONFIG.get(dtype, {}).get("label", dtype)
            print(f"      - {label}: ...{fid[-5:]}")
    print("-" * 30)

    while True:
        try:
            found_any = False

            for input_folder_id, (doc_type, profile_key) in active_folder_map.items():
                files = list_files(service, input_folder_id)

                if not files:
                    continue

                profile = profiles[profile_key]
                writer = writers[profile_key]
                processed_folder_id = profile["processed_folder_id"]

                found_any = True
                type_label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)
                print(f"\n\n🔎 [{profile['label']}/{type_label}] "
                      f"新しいファイルを検出しました！")

                for file in files:
                    file_id = file['id']
                    file_name = file['name']
                    md5 = file.get('md5Checksum')

                    # 1. 防重檢測（同一プロファイルのアーカイブ内のみを見る）
                    if is_duplicate_file(service, md5, processed_folder_id):
                        print(f"⚠️ 重複アップロードを検出: {file_name}")
                        print("   -> 処理をスキップしてアーカイブします")
                        move_file(service, file_id, input_folder_id, processed_folder_id)
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

                    # PDF 間分割線 + 取引No リセット
                    writer.start_new_file(uploader_name, doc_type, file_name)

                    success = process_file(
                        service, writer, local_path,
                        uploader_name, chat_id,
                        doc_type=doc_type, drive_file_id=file_id,
                        split_pdf_folder_id=profile["split_pdf_folder_id"],
                    )

                    if success:
                        move_file(service, file_id, input_folder_id, processed_folder_id)
                    else:
                        print("⚠️ ファイル処理失敗。")

                    if os.path.exists(local_path):
                        os.remove(local_path)
                        print("🧹 一時ファイルを削除しました")

                    # 取引No はタブの A 列から都度算出するため書き戻し不要。
                    # flush() は将来の後処理フック用に呼び出しだけ残す。
                    writer.flush()

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
