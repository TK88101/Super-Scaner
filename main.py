import os
import sys
import time
import io
import random
import re
from datetime import datetime

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
from ocr_engine import (
    EXCLUDE_DEST_AUDIT_TAB, EXCLUDE_DEST_MF_TAB, process_pipeline,
)
from sheets_output import (
    APPEND_RESULT_PLACEHOLDER,
    AUDIT_VERDICT_BRANCH, AUDIT_VERDICT_EXCLUDED, AUDIT_VERDICT_MISSING,
    JST, SheetsOutputWriter,
)
from notifier import send_notification
from doc_types import DocType, DOC_TYPE_CONFIG
from page_progress import (
    NULL_REPORTER, OUTCOME_EXCLUDED, OUTCOME_FAILED, OUTCOME_PLACEHOLDER,
    OUTCOME_POSTED, STATUS_ABORTED, STATUS_COMPLETED,
    STATUS_COMPLETED_COVERAGE_GAP, STATUS_FAILED_RETAINED,
    STATUS_PARSE_FAILED, STATUS_PARTIAL_ERROR, SheetsProgressReporter,
    utc_now,
)
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

    def anchor_url(self, page_num: int, total_pages: int) -> str:
        """アップロードせず `#page=` アンカーだけを返す（診断用途）。

        除外ページ（封筒等）の監査タブ行はあくまで可観測性設備であり、その
        ために Drive へ単ページ PDF を1枚ずつアップロードするのは割に合わない
        （100頁中20頁が封筒なら 20 回の余計な Drive 書込）。人がページを特定
        できれば足りるので、resolve() が失敗時に倒す劣化先と同じ形を最初から
        使う。既に他の理由で解決済みならその URL を再利用する。
        """
        if total_pages <= 1 or not self._base_url:
            return self._base_url
        cached = self._cache.get(page_num)
        if cached:
            return cached
        return f"{self._base_url}#page={page_num}"

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


def _build_unrecognized_placeholder(uploader_name, filename, memo):
    """MF 区に書く「認識不能」占位行の entries_data を組み立てる。

    同じ形状が process_file 内に複数箇所（部分ページエラー・除外ページの退避）
    あり、逐字コピーだと契約変更時の同期漏れを招く。vendor にファイル名を
    入れるのは、行だけ見てどの原票の話か分かるようにするため。
    """
    return {
        "entries": [],
        "_unrecognized": True,
        "memo": memo,
        "date": "",
        "vendor": filename,
        "uploader": uploader_name,
    }


def _record_excluded_page(sheets_writer, resolver, result, uploader_name,
                          doc_type, filename, page_num, total_pages):
    """除外ページの留痕を、result が宣言した行き先へ書く。

    行き先は producer（ocr_engine）が `_exclude_destination` で宣言する。
    理由文字列から行き先を推測しない——理由（なぜ除外したか）と行き先
    （どこに書くか）は別の関心事であり、reason の増加に合わせて main 側の
    分岐を書き足し忘れる事故を避ける。

      audit_tab（封筒等）: MF 区を汚さない。取引No も消費しない
      mf_tab（社会保険料通知書）: 正常な除外ではなく**運用ルール違反の通知**
        であり、顧客が必ず目にする場所でなければ意味がない（§3.8）

    除外ページのために Drive へ単ページ PDF を上げるのは割に合わないので、
    リンクはアップロード無しの #page= アンカーで足りる。
    """
    reason = result.get("_exclude_reason", "unknown")
    destination = result.get("_exclude_destination", EXCLUDE_DEST_AUDIT_TAB)
    source_url = resolver.anchor_url(page_num, total_pages)

    if destination == EXCLUDE_DEST_MF_TAB:
        # entries が空なので金額列・科目列は空のまま
        # = MF インポート時に金額を持ち込まない。
        print(f"🏥 [{page_num}/{total_pages}] 除外ページ ({reason}) "
              f"→ MFタブに提示行（仕訳は作成しません）")
        try:
            sheets_writer.append_entries(
                employee_name=uploader_name,
                doc_type=doc_type,
                entries_data=_build_unrecognized_placeholder(
                    uploader_name, filename, result.get("memo", "")),
                source_url=source_url,
            )
        except Exception as e:
            # この提示行はこのページの**唯一の出力**であり、監査タブ行のような
            # 「あれば嬉しい」記録ではない。握り潰すと Success 判定でファイルが
            # 歸檔され、顧客は社会保険料通知書を上げたことも、それが記帳
            # されなかったことも知る術がなくなる。失敗として上へ返し、
            # 再試行対象（ファイル保持）に載せる。
            print(f"❌ 除外ページの提示行の書き込み失敗 ({reason}): {e}")
            return False
        return True

    print(f"📨 [{page_num}/{total_pages}] 除外ページ ({reason}) "
          f"→ 監査タブに記録（MF区には書きません）")
    try:
        sheets_writer.append_audit_row(
            filename=filename,
            page_num=page_num,
            verdict=AUDIT_VERDICT_EXCLUDED,
            reason=reason,
            ocr_text_len=result.get("_ocr_text_len", 0),
            source_url=source_url,
        )
    except Exception as e:
        # §3.7: 真の除外は監査タブが唯一の留痕。失敗したら MF の赤い
        # 認識不能占位行へ退避して必ず可視化する（無音欠落に戻さない）。
        print(f"⚠️ 監査タブ書き込み失敗 → MF区の認識不能行へ退避: {e}")
        _write_audit_fallback_row(
            sheets_writer, uploader_name, doc_type, filename,
            page_num, reason, source_url)
    return True


def _write_audit_fallback_row(sheets_writer, uploader_name, doc_type, filename,
                              page_num, reason, source_url):
    """監査タブ書込に失敗した除外ページを MF 区の認識不能行へ退避する（§3.7）。

    監査タブは可観測性設備であり帳簿データではないが、真の除外ページにとっては
    **唯一の留痕**である。そこが落ちたら無音欠落に逆戻りするため、MF 区を汚す
    コストを払ってでも人の目に触れる場所へ出す。退避行自体の書込も失敗したら
    ログに残すしかない（それ以上の保底経路はない）。
    """
    try:
        sheets_writer.append_entries(
            employee_name=uploader_name,
            doc_type=doc_type,
            entries_data=_build_unrecognized_placeholder(
                uploader_name, filename,
                f"⚠ 除外ページ p{page_num} ({reason}) "
                f"監査タブ書き込み失敗のため退避"),
            source_url=source_url,
        )
    except Exception as e:
        print(f"❌ 除外ページの退避行も書き込めませんでした（留痕なし）: {e}")


def process_file(service, sheets_writer, file_path, uploader_name, chat_id,
                  doc_type=DocType.RECEIPT, drive_file_id=None,
                  split_pdf_folder_id="", progress=None):
    """process_file の薄い外殻（B7 T3）。

    進捗フックの発火（file_started／未預期例外時の ABORTED best-effort 記録）
    だけを担い、実処理は _process_file_impl に委譲する。progress 未指定
    (None) なら NULL_REPORTER に落ちるため、既存の呼び出し側・テストは
    完全に従来どおり動く（無回帰）。
    """
    progress = progress or NULL_REPORTER
    filename = os.path.basename(file_path)
    progress.file_started(filename, uploader_name, doc_type)
    try:
        return _process_file_impl(
            service, sheets_writer, file_path, uploader_name, chat_id,
            doc_type=doc_type, drive_file_id=drive_file_id,
            split_pdf_folder_id=split_pdf_folder_id, progress=progress)
    except Exception as e:
        # best-effort: 進捗記録は reporter 自身が内部で自吞するが、二重の
        # 安全網として元例外を必ずそのまま伝播させる（main loop の既存の
        # 檔滯留・再試行語義は一切不変）。
        progress.file_finished(STATUS_ABORTED, error_class=type(e).__name__)
        raise


def _process_file_impl(service, sheets_writer, file_path, uploader_name,
                       chat_id, doc_type=DocType.RECEIPT, drive_file_id=None,
                       split_pdf_folder_id="", *, progress):
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
    failed_page_notes = {}  # {page_num: 具体的な説明} 占位行で汎用文言に埋もれさせない

    seen_page_nums = set()
    last_total_pages = 0

    for page in process_pipeline(file_path, doc_type=doc_type):
        result = page["result"]
        page_num = page["page_num"]
        total_pages = page["total_pages"]
        seen_page_nums.add(page_num)
        last_total_pages = total_pages
        count += 1

        def _emit(outcome, reason):
            # 頁終局の進捗発射を一点に集約（発射時 UTC の刻印込み）。同一
            # 迭代内で同期呼出しかされないため閉包の遅延束縛対策は不要
            # （simcodex R2 裁決）。
            progress.page_done(page_num, total_pages, outcome, reason,
                               utc_now())
        # 再試可能なページエラーは Sheets へ書き込まない
        # （全頁失敗時は Failed を返しファイルを保持するため、
        #  次回再試行で同じページの占位行が重複生成されるのを防ぐ）
        if result.get("_page_error"):
            error_pages += 1
            failed_page_nums.append(page_num)
            # producer は memo に具体的な原因を書いている（「PDF分割が中断」
            # 「AI応答のJSON解析失敗」「整形処理エラー: XxxError」等）のに、
            # ここで捨てると顧客が見る集計行は頁番号だけになり、
            # 「再アップロードで直るのか、原票が壊れているのか」を判断できない。
            # failed_page_notes の宣言時コメント（上記）が元々意図していた
            # 用途でもある（従来は _excluded_page の監査失敗経路でしか
            # 埋まっておらず、意図が _page_error では兌現されていなかった）。
            if result.get("memo"):
                failed_page_notes[page_num] = result["memo"]
            _emit(OUTCOME_FAILED, "page_error")
            continue

        # IP-401 T1/T2: 封筒等の除外ページは MF データ区に一切書かず、独立の
        # 監査タブへ留痕する。MF 区に書くと取引No・行 fingerprint を消費して
        # MF インポートデータを汚す（Plan §3.2）。かといって無音で消すと顧客は
        # 枚数を数えるまで気づけない（本件 IP-401 の事故そのもの）。
        # error_pages には数えない（除外は失敗ではない）。count には既に
        # 数えており、全頁封筒でも count>0 → Failed 無限リトライを回避する。
        if result.get("_excluded_page"):
            recorded = _record_excluded_page(
                sheets_writer, resolver, result, uploader_name, doc_type,
                filename, page_num, total_pages)
            if not recorded:
                # 留痕を残せなかった除外は「静かに消えた」のと同じ。除外として
                # 数えず失敗として扱う。全頁がこれならファイル保持→再試行。
                # 混在ファイルは既存語義どおり歸檔されるが（再試行すると成功頁の
                # 仕訳が重複するため）、そのとき書かれる占位行に「どの頁が何
                # だったか」を残し、汎用文言で情報を落とさないようにする。
                error_pages += 1
                failed_page_nums.append(page_num)
                failed_page_notes[page_num] = (
                    result.get("memo")
                    or f"除外ページ({result.get('_exclude_reason', 'unknown')})")
                _emit(OUTCOME_FAILED, "exclude_record_failed")
                continue
            excluded_pages += 1
            excluded_page_nums.append(page_num)
            # 低11 裁決: 落地形式（監査タブ/MF 提示行）を問わず、_excluded_page
            # の頁は恒に EXCLUDED。PLACEHOLDER は非除外頁が占位行に終わった
            # 場合のみ（append_entries 戻り値の分岐、下記）。
            _emit(OUTCOME_EXCLUDED, result.get("_exclude_reason", "unknown"))
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
        write_result = sheets_writer.append_entries(
            employee_name=uploader_name,
            doc_type=doc_type,
            entries_data=result,
            source_url=source_url,
        )
        # 中8 裁決: entries>0 でも append_entries 内部で占位行に転落した頁を
        # POSTED と誤報しない（sheets_output.append_entries の戻り値で正確に
        # 判定。戻り値を返さない duck-typed writer は POSTED 扱いに倒す）。
        if write_result == APPEND_RESULT_PLACEHOLDER:
            _emit(OUTCOME_PLACEHOLDER, "unrecognized")
        else:
            _emit(OUTCOME_POSTED, "")

        # IP-401 T2/R2: entries は有効だが封筒シグナルも命中したページ。
        # 記帳は止めず（Gemini 優先＝Strategy C 交差検証の設計意図）、Gemini が
        # 本物の封筒に偽 entry を捏造した場合に備えて人手抽査用の「分岐」を
        # 監査タブへ残す。書込順序は MF が先・監査が後（帳簿を人質に取らせない）。
        audit_signal = result.get("_audit_signal")
        if audit_signal:
            try:
                sheets_writer.append_audit_row(
                    filename=filename,
                    page_num=page_num,
                    verdict=AUDIT_VERDICT_BRANCH,
                    reason=audit_signal,
                    ocr_text_len=result.get("_ocr_text_len", 0),
                    source_url=source_url,
                )
            except Exception as e:
                # §3.7: MF は既に正しく書けており帳簿は正しい。監査は諦める。
                print(f"⚠️ 監査タブへの分岐記録に失敗（記帳は成功）: {e}")

        # 軽量サマリーのみ保持（フル結果は GC 対象）
        page_amount = sum(int(e.get('amount', 0)) for e in entries)
        total_amount += page_amount
        vendor_names.append(result.get('vendor', ''))
        total_entries += len(entries)

    # ページカバレッジ突合（IP-401 §8-中7）。ocr_engine 側も同じ検査をして
    # 警告を出すが、本番機は無人の Windows ミニ PC で誰も控制台を見ておらず
    # （Chatwork 通知も monitoring も廃止済み、CLAUDE.md 参照）、02:00 の
    # 再起動で流れる。哨戒が控制台にしか出ないなら哨戒していないのと同じ。
    # 顧客・運用者が実際に見る監査タブへ持続化する。
    # 成否判定は変えない（§8-中7 の裁決どおり警告のみ、P2 繰延）。
    missing_pages = sorted(set(range(1, last_total_pages + 1)) - seen_page_nums)
    if missing_pages:
        print(f"⚠️ ページカバレッジ警告: {len(missing_pages)}/{last_total_pages}頁が"
              f"一度も出力されませんでした {missing_pages}")
        try:
            sheets_writer.append_audit_row(
                filename=filename,
                page_num=missing_pages[0],
                verdict=AUDIT_VERDICT_MISSING,
                reason=f"page_coverage_gap:{missing_pages}",
                ocr_text_len=0,
                source_url=base_url,
            )
        except Exception as e:
            print(f"⚠️ カバレッジ警告の監査タブ記録に失敗: {e}")

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
        progress.file_finished(STATUS_FAILED_RETAINED)
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
                entries_data=_build_unrecognized_placeholder(
                    uploader_name, filename,
                    f"⚠ ページ処理エラー {error_pages}/{count}頁 "
                    f"[{failed_pages_str}] 手動再スキャン要"
                    + "".join(f" / p{n}: {note}"
                              for n, note in sorted(failed_page_notes.items()))),
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
        # 中6 裁決: partial_error は coverage gap より優先。頁欠落があるだけの
        # 完了は「完了（頁欠落あり）」——「完了」と偽って矛盾させない。
        if partial_error:
            file_status = STATUS_PARTIAL_ERROR
        elif missing_pages:
            file_status = STATUS_COMPLETED_COVERAGE_GAP
        else:
            file_status = STATUS_COMPLETED
        progress.file_finished(file_status)
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
        progress.file_finished(STATUS_PARSE_FAILED)
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


def build_progress_reporters(writers):
    """writer ごとに SheetsProgressReporter を作る（B7 T4）。

    build_writers と同じ縮退方針: 進捗レポーターの初期化失敗（例:
    writer.spreadsheet 参照エラー等）はプロファイル単位で吸収し、当該
    プロファイルは辞書から欠落させて続行する（消費側は .get(None) で受ける。
    進捗タブが無くても記帳自体は止めない——進捗可視化は付帯機能であり、
    記帳の可用性を道連れにしない）。

    Returns: {profile_key: SheetsProgressReporter}（失敗プロファイルは欠落）
    """
    reporters = {}
    for profile_key, writer in writers.items():
        try:
            reporters[profile_key] = SheetsProgressReporter(writer.spreadsheet)
        except Exception as e:
            print(f"⚠️ [{profile_key}] 進捗レポーターを初期化できません"
                  f"（進捗タブなしで続行）: {e}")
    return reporters


def filter_active_folders(folder_map, writers):
    """writer を作れなかったプロファイルの入力フォルダを監視対象から外す。"""
    return {fid: entry for fid, entry in folder_map.items()
            if entry[1] in writers}


# ============ 失敗退避護欄（2026-08-10 Plan §3.6） ============
# main.py には従来 fail_count/backoff/blacklist が皆無だった。永続的な失敗
# （Sheets 400・Drive 故障・コードのバグ等）は SCAN_INTERVAL=3s のまま無限
# リトライになり、毎輪 PaddleOCR + Gemini を焼き続ける（実際に事故が発生済
# み）。ここでは IP-401「全頁失敗はファイル保持」の語義を一切変えず、
# 再試行の"間隔"だけを制御する。状態はプロセス内メモリの dict のみ
# （main() のローカル変数として保持）——再起動でリセットされて構わない
# (Plan で明示許容)。ファイルの行き先（保持/移動）はこの節では一切決めない。

# 級距: 3s → 30s → 5min → 30min → 上限1h（Plan §3.6 の目安どおり）
FAILURE_BACKOFF_SCHEDULE_SECONDS = (3, 30, 300, 1800, 3600)


def _next_backoff_seconds(fail_count):
    """累計失敗回数 (1始まり) から次回試行までの待機秒数を返す純粋関数。

    main loop を起動せずに単体検証できるよう、状態を一切持たない。
    スケジュール長を超えたら最後の要素 (上限1h) に頭打ちする。
    唯一の呼び出し元 _record_file_failure は常に fail_count>=1 を渡すが、
    念のため下限は式の中で吸収する（引数を書き換えない。CLAUDE.md §7 不変）。
    """
    index = min(max(fail_count - 1, 0), len(FAILURE_BACKOFF_SCHEDULE_SECONDS) - 1)
    return FAILURE_BACKOFF_SCHEDULE_SECONDS[index]


def _record_file_failure(retry_state, file_id, now_ts):
    """このファイルの失敗を1件加算し、次回試行可能時刻を設定する。

    retry_state は {file_id: {"count": n, "next_attempt_ts": t}}。
    呼び出し側が保持する辞書をその場で更新する（tab キャッシュ等、この
    プロジェクトの他の行程内状態と同じ寄せ方）。更新後のエントリを返す。
    """
    prev_count = retry_state.get(file_id, {}).get("count", 0)
    count = prev_count + 1
    entry = {
        "count": count,
        "next_attempt_ts": now_ts + _next_backoff_seconds(count),
    }
    retry_state[file_id] = entry
    return entry


def _record_file_success(retry_state, file_id):
    """処理成功時にこのファイルの退避記録を消す（存在しなくても no-op）。"""
    retry_state.pop(file_id, None)


def _is_file_backed_off(retry_state, file_id, now_ts):
    """走査時にこのファイルを今回だけ飛ばすべきかを判定する。

    記録が無い（未失敗）ファイルは常に False——他ファイルの退避に一切
    引きずられない（護欄の核心要件: 1ファイルの退避が全体を止めない）。
    """
    entry = retry_state.get(file_id)
    if entry is None:
        return False
    return now_ts < entry["next_attempt_ts"]


# ---- 退避中でも他ファイルを止めない・console が沈黙しない（codex 対抗評審裁決）----
# 従来は found_any / 「新しいファイルを検出しました！」バナーが退避 continue
# より前に実行されており、失敗ファイルは IP-401 の語義どおり input に残り
# 続けるため list_files が永続的に非空になり、3秒毎に誤ってバナーが印字され
# 続けた（1h 級距まで進むと約1200回の空撃ち）。同時に、ループ末尾の "." 心拍
# （found_any=False のときだけ出る）も退避ウィンドウ中は一切出ず、事故当時の
# 「無限リトライで刷り潰れる」画面と見分けがつかなくなっていた。
#
# 対処: list_files 直後に ready/backed_off へ分割し、found_any とバナーは
# ready_files の有無だけで決める（backed_off は無視）。退避中ファイルが
# あることは、ただ濾すだけ（"." のみになり input 空の正常状態と区別不能）
# ではなく、間引いた摘要行で可視化する——運用者が「護欄が効いている」と
# 「また壊れて無限リトライしている」を見分けられる唯一の窓が console。

# 摘要行の最大表示頻度（秒）。運用パラメータなので名前付き定数にする——
# 短くすれば追跡性は上がるがログが埋まる、長くすれば逆。調整時はここだけ変える。
BACKOFF_SUMMARY_INTERVAL_SECONDS = 60


def _format_jst_timestamp(ts):
    """UNIX 秒 (float) を JST 明示の "%Y/%m/%d %H:%M:%S" 表記に変換する。

    倉庫慣例 (sheets_output.py:558・page_progress.py) に合わせる。ホストの
    ローカルタイムゾーンに依存する time.strftime + time.localtime を使うと、
    開発機やタイムゾーン設定が変わったとき、console に出る「次回試行予定」が
    Sheets 側の全タイムスタンプと表記（区切り文字・時差）がずれる——console は
    無人稼働機で唯一の照合手段なので、ここで JST を明示する。
    """
    return datetime.fromtimestamp(ts, JST).strftime("%Y/%m/%d %H:%M:%S")


def _format_backoff_summary(count, earliest_next_attempt_ts):
    """退避中ファイル数と最早の次回試行時刻から console 用の一行摘要を作る。

    純粋関数——main loop を起動せずに検証できる（_next_backoff_seconds 等と
    同じ寄せ方）。
    """
    earliest_str = _format_jst_timestamp(earliest_next_attempt_ts)
    return f"⏳ 失敗退避中: {count}件（最早次回試行: {earliest_str}）"


def _should_print_backoff_summary(last_printed_ts, now_ts):
    """直近の摘要表示から BACKOFF_SUMMARY_INTERVAL_SECONDS 秒以上経過したか。

    last_printed_ts=0（未表示）なら常に True——退避が始まった最初の輪で
    即座に見えるようにする。
    """
    return now_ts - last_printed_ts >= BACKOFF_SUMMARY_INTERVAL_SECONDS


def _partition_by_backoff(files, retry_state, now_ts):
    """files を処理可能 (ready) と退避中 (backed_off) に分ける純粋関数。

    退避判定は _is_file_backed_off に委譲し、呼び出し側が輪ごとに1回だけ
    取った now_ts を全ファイルへ同じ値で適用する（ファイルごとに
    time.time() を取り直さない）。retry_state は読み取りのみで書き換えず、
    順序は入力の files をそのまま保つ。
    """
    ready_files = []
    backed_off_files = []
    for file in files:
        if _is_file_backed_off(retry_state, file['id'], now_ts):
            backed_off_files.append(file)
        else:
            ready_files.append(file)
    return ready_files, backed_off_files


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

    # B7 T4: プロファイルごとの進捗レポーター（Sheets `_処理進捗` タブ）。
    # 生成失敗は build_writers と同じ粒度で縮退——進捗タブが無くても記帳は続行。
    progress_reporters = build_progress_reporters(writers)

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

    # {drive_file_id: {"count": n, "next_attempt_ts": t}}。プロセス内メモリ
    # のみで保持し、再起動でリセットされてよい（Plan §3.6 で明示許容）。
    file_retry_state = {}
    # 退避中サマリの直近表示時刻（0=未表示）。file_retry_state 同様プロセス内
    # メモリのみで、再起動でリセットされてよい。
    last_backoff_summary_ts = 0

    while True:
        try:
            found_any = False
            # この輪で全ファイルに同じ now を使う（ファイルごとに取り直さない
            # ——codex 対抗評審裁決・Plan §1）。
            now_ts = time.time()
            backed_off_total = 0
            earliest_next_attempt_ts = None

            for input_folder_id, (doc_type, profile_key) in active_folder_map.items():
                files = list_files(service, input_folder_id)

                if not files:
                    continue

                # 護欄（Plan §3.6）: 退避中ファイルは list_files 直後に濾す。
                # found_any・バナーは ready_files の有無だけで決める——退避
                # ファイルが input に残り続けても（IP-401 の語義どおり）、
                # 誤って「新しいファイルを検出しました！」を毎輪印字しない
                # （codex 対抗評審裁決: 旧実装はここで found_any=True にしていた）。
                ready_files, backed_off_files = _partition_by_backoff(
                    files, file_retry_state, now_ts)

                if backed_off_files:
                    backed_off_total += len(backed_off_files)
                    folder_earliest_ts = min(
                        file_retry_state[f['id']]["next_attempt_ts"]
                        for f in backed_off_files
                    )
                    if (earliest_next_attempt_ts is None
                            or folder_earliest_ts < earliest_next_attempt_ts):
                        earliest_next_attempt_ts = folder_earliest_ts

                if not ready_files:
                    continue

                profile = profiles[profile_key]
                writer = writers[profile_key]
                reporter = progress_reporters.get(profile_key)
                processed_folder_id = profile["processed_folder_id"]

                found_any = True
                type_label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)
                print(f"\n\n🔎 [{profile['label']}/{type_label}] "
                      f"新しいファイルを検出しました！")

                for file in ready_files:
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
                    #
                    # このファイル 1 件分の処理を丸ごとここで受け止める
                    # （護欄・Plan §3.6）。main() 全体の外殻 try に漏らすと、
                    # この 1 ファイルの失敗が同バッチの以降のファイルまで
                    # 巻き添えにして走査を止めてしまう（1 個の壊れたファイルが
                    # 行列全体を塞いではならない）。
                    #
                    # download_file と start_new_file も範囲に含めるのが要
                    # （codex review P1）。特に start_new_file は内部の
                    # _get_or_create_tab が sheets_output.py:180 の try の外に
                    # あるため APIError をそのまま投げる——tab 消失こそ本護欄が
                    # 守るべき当の事象なのに、そこだけ護欄の外に落ちていた。
                    #
                    # process_file 自体の戻り値・例外語義は一切変えない。
                    # ファイルの行き先（失敗時は input に保持）も従来どおりで、
                    # IP-401 の語義は不変。変わるのは再試行の間隔だけ。
                    local_path = None
                    try:
                        local_path = download_file(service, file_id, file_name)

                        # PDF 間分割線 + 取引No リセット
                        writer.start_new_file(uploader_name, doc_type, file_name)

                        success = process_file(
                            service, writer, local_path,
                            uploader_name, chat_id,
                            doc_type=doc_type, drive_file_id=file_id,
                            split_pdf_folder_id=profile["split_pdf_folder_id"],
                            progress=reporter,
                        )
                    except Exception as e:
                        print(f"❌ ファイル処理で例外発生: {file_name}: {e}")
                        success = False

                    if success:
                        move_file(service, file_id, input_folder_id, processed_folder_id)
                        _record_file_success(file_retry_state, file_id)
                    else:
                        print("⚠️ ファイル処理失敗。")
                        entry = _record_file_failure(
                            file_retry_state, file_id, time.time())
                        next_attempt = _format_jst_timestamp(entry["next_attempt_ts"])
                        print(f"🛑 退避開始: {file_name}"
                              f"（累計失敗 {entry['count']} 回）"
                              f" 次回試行予定: {next_attempt}")

                    # local_path は download_file 自体が失敗すると None のまま
                    # （護欄の try に download を含めたため）。None チェックを
                    # 挟まないとここで NameError/TypeError になり、せっかく
                    # 受け止めた例外の直後に別の例外で走査が止まる。
                    if local_path and os.path.exists(local_path):
                        os.remove(local_path)
                        print("🧹 一時ファイルを削除しました")

                    # 取引No はタブの A 列から都度算出するため書き戻し不要。
                    # flush() は将来の後処理フック用に呼び出しだけ残す。
                    writer.flush()

                    print("=" * 30)

            # 退避中ファイルの可視化（護欄・Plan §3.6）。ただ濾すだけだと
            # "." のみになり input が完全に空の正常状態と見分けがつかない
            # ——codex 対抗評審裁決。BACKOFF_SUMMARY_INTERVAL_SECONDS 秒に
            # 最大1行まで間引く（毎輪印字するとログが埋まる）。
            if backed_off_total > 0 and _should_print_backoff_summary(
                    last_backoff_summary_ts, now_ts):
                print(_format_backoff_summary(
                    backed_off_total, earliest_next_attempt_ts))
                last_backoff_summary_ts = now_ts

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
