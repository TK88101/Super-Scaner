import os
import sys
import time
import io
import random
import re
from dataclasses import dataclass
from enum import Enum

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
import firestore_report
import intake_guard

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
        fields="nextPageToken, files(id, name, lastModifyingUser, md5Checksum, properties)"
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


def _move_file_raw(service, file_id, previous_folder_id, new_folder_id) -> None:
    """move_file の共通実装（例外を握り潰さず伝播、print 無し）。

    HEADLESS_MODE の隔離夾送り（intake_guard.handle_intake の
    move_to_quarantine）は成否を呼び出し元で判定する必要があるため、例外を
    透過させる版を切り出す。既存 move_file の print 文言・吞例外挙動
    （UI 零改動）はここでは一切変えない——move_file はこれを包むだけ。
    """
    service.files().update(
        fileId=file_id,
        addParents=new_folder_id,
        removeParents=previous_folder_id,
        # 共有ドライブ対応: 共有ドライブ内/への移動には supportsAllDrives が必須
        supportsAllDrives=True,
        fields='id, parents'
    ).execute()


def move_file(service, file_id, previous_folder_id, new_folder_id):
    try:
        _move_file_raw(service, file_id, previous_folder_id, new_folder_id)
        print(f"📦 元画像を処理済みフォルダ(Processed)へ移動しました")
    except Exception as e:
        print(f"⚠️ ファイル移動中に警告が発生しました: {e}")


def _init_headless_reporter():
    """HEADLESS_MODE 起動時の Firestore reporter を構築する。

    UI 版（HEADLESS_MODE 未設定）では reporter を作らず None を返す。
    HEADLESS_MODE だが QUARANTINE_FOLDER_ID 未設定は起動時に print + exit(1)
    する（fail fast）。
    """
    if not config.headless_mode():
        return None
    missing = config.validate_headless_config()
    if missing:
        print(f"❌ エラー：HEADLESS_MODE には次のキーの設定が必須です: {', '.join(missing)}")
        exit(1)
    return firestore_report.build_reporter_from_env()


def _headless_intake_gate(service, file, input_folder_id, reporter, alerted=None):
    """監視フォルダ入口守衛（IP-303）を1ファイルに適用し (続行可, base) を返す。

    非 HEADLESS_MODE では常に (True, None)（副作用なし）。HEADLESS_MODE では
    intake_guard.handle_intake_gate の裁決を橋渡しし、PROCESS 時のみ base posting_id
    を第2要素で通す（IP-304 が頁級台賬の job_key に使う、#14）。base の中身には
    関知しない。
    """
    if not config.headless_mode():
        return True, None
    result = intake_guard.handle_intake_gate(
        file,
        get_job=reporter.get_job,
        write_alert=reporter.write_alert,
        move_to_quarantine=lambda: _move_file_raw(
            service, file["id"], input_folder_id, config.quarantine_folder_id()),
        alerted=alerted,
    )
    return result.should_process, result.base


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


class ProcessOutcome(Enum):
    """headless process_file の五態（B4 Plan §2.3、PARTIAL/DEAD_LETTER 追加）。
    UI 経路は現状 bool を返す。"""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"
    PARTIAL = "PARTIAL"
    DEAD_LETTER = "DEAD_LETTER"


@dataclass(frozen=True)
class HeadlessOutcome:
    """process_file(ledger=...) の戻り値（headless 専有、B4 Plan §2.3、T3/T4 共用）。

    outcome：ProcessOutcome。
    retryable：outcome が FAILED のとき、RETRYABLE 頁由来なら True（3秒自癒窗・
    memo 不記）、UNKNOWN 頁由来なら False（memo 記、控制面重投でのみ再試行）。
    他 outcome では意味を持たない（既定 False）——T4 の memo 書込み判定材料。
    dead_letter_payload：outcome が DEAD_LETTER のときのみ非 None
    （reporter.report_dead_letter(error=...) にそのまま渡す、§2.3 payload 釘死）。
    """

    outcome: ProcessOutcome
    retryable: bool = False
    dead_letter_payload: dict | None = None


def _extract_tickets(results):
    """頁内の各 result を F59 対账 advisory の 1 票 TicketSummary へ（去重键には非使用）。

    ticket_count の語義釘死（B4 Plan §2.2）＝有効入賬票数のみ。entries が空の
    result（占位頁・認識不能頁）は票として数えない——占位頁の台賬記録が
    ticket_count=0 になることで、後続の重跑輪が「真に入賬済み」か「占位頁」
    かを confirmed_ticket_count() の値だけで自証できる。
    """
    from posting_ledger import TicketSummary
    from receipt_aggregation import sum_row_amounts
    return [
        TicketSummary(
            date=r.get("date", "") or "",
            amount=sum_row_amounts(r.get("entries", []) or []),
            vendor=r.get("vendor", "") or "",
        )
        for r in results if r.get("entries")
    ]


def _flush_page(writer, ledger, base, doc_type, uploader_name, page_num,
                results, urls):
    """1 頁分の查重→記帳。戻り値 'written'|'skipped'|'escalate'。

    check_page で SKIP（既 CONFIRMED/witness PRESENT）→ 何も書かない（硬去重）。
    ESCALATE（witness 不確実）→ 呼出側でファイル保持・回報せず。WRITE → build_page_write
    →（PENDING→append→CONFIRMED）を ledger.post_page が原子封装。results が
    全件 entries 空（占位頁）でも build_page_write は _build_unrecognized_block
    で占位行 1 行を天然生成する（sheets_output.py 零改動、B4 Plan §2.2）。
    """
    from posting_ledger import derive_page_id, PageDecision, PagePostingSummary
    from sheets_output import compute_page_fingerprint

    page_id = derive_page_id(base, page_num)
    decision = ledger.check_page(page_id)
    if decision is PageDecision.SKIP:
        print(f"⏭️ 頁 {page_num} は既記帳（台賬）→ スキップ")
        return "skipped"
    if decision is PageDecision.ESCALATE:
        print(f"🚨 頁 {page_num} 判定不能（witness 不確実）→ ESCALATE（人工核）")
        return "escalate"

    start_txn = writer.next_txn_no(uploader_name, doc_type)
    page_write = writer.build_page_write(
        uploader_name, doc_type, results, urls, start_txn)
    predicted = writer.peek_append_range(page_write)
    fingerprint = compute_page_fingerprint(page_write.rows)
    tickets = _extract_tickets(results)
    summary = PagePostingSummary(
        page_num=page_num,
        ticket_count=len(tickets),
        row_count=len(page_write.rows),
        tickets=tuple(tickets),
        predicted_row_range=predicted,
        row_fingerprint=fingerprint,
        sheet_tab=page_write.tab_name,
    )
    ledger.post_page(page_id, summary, lambda: writer.commit_page(page_write))
    return "written"


# B4 Plan §2.3: 頁級 outcome の分類（"種別" 文字列、main 内部専用の軽量タグ）。
_PLACEHOLDER_KINDS = frozenset({"PLACEHOLDER_WRITTEN", "PLACEHOLDER_PRIOR"})
_POSTED_KINDS = frozenset({"POSTED_NOW", "POSTED_PRIOR"})


def _classify_page_result_shape(results):
    """頁緩衝（同一 page_num の全 yield）の形状を分類する（§2.2/§2.3、純関数）。

    戻り値は (kind, detail) のいずれか：
        ("error", "RETRYABLE"|"UNKNOWN") — 頁エラー（transport/未知例外、単独 result）
        ("content_error", None)          — 頁エラー（CONTENT、票面不可読、単独 result）
        ("valid", None)                  — 全 result が有効仕訳（entries 非空）
        ("placeholder", None)            — 全 result が認識不能（entries 空、非エラー）
        ("escalate", reason)             — 不変式破れ（混型頁/error 共存、頁号重現とは別軸）

    _error_class 欠落／未知値は保守的に "UNKNOWN"（B4 Plan §2.1 消費側デフォルト
    ——CONTENT/DEAD_LETTER には倒さない、認証エラー等の全夾 DEAD_LETTER 風暴防止）。
    """
    error_results = [r for r in results if r.get("_page_error")]
    if error_results:
        if len(results) != 1:
            # 同頁に error と他 result が並存（現行 ocr_engine では起こらないが
            # 将来の生成器変更に対する防御的不変式、Codex 初審 #14 と同族）
            return ("escalate", "mixed_error_and_other_result")
        error_class = error_results[0].get("_error_class") or "UNKNOWN"
        if error_class not in ("RETRYABLE", "UNKNOWN", "CONTENT"):
            error_class = "UNKNOWN"
        if error_class == "CONTENT":
            return ("content_error", None)
        return ("error", error_class)

    valid = [r for r in results if r.get("entries")]
    zero = [r for r in results if not r.get("entries")]
    if valid and zero:
        # 混型頁（Codex 複審 #14 採択）: ticket_count>0 が完全性を自証できなく
        # なるため、書込せず即座に人工核へ委ねる
        return ("escalate", "mixed_valid_and_placeholder_result")
    return ("valid", None) if valid else ("placeholder", None)


def _apply_content_error_override(result, filename, page_num, total_pages):
    """CONTENT 頁の占位行文言を釘死する（B4 Plan §2.2、既存 dict は破壊しない）。"""
    return {
        **result,
        "memo": f"⚠ ページ処理エラー p{page_num}/{total_pages} 手動再スキャン要",
        "vendor": filename,
        "date": "",
    }


def _prior_page_kind(ledger, page_id):
    """check_page が SKIP を返した頁の身分を confirmed_ticket_count で自証する（§2.2）。

    >0 → POSTED_PRIOR（真に入賬済み）／==0 → PLACEHOLDER_PRIOR（恆失敗頁）。
    None（CONFIRMED 記録が読めない理論上の縁——check_page が SKIP を返す以上
    通常到達しない）は誠實優先で保守側（POSTED を騙らない）扱いにする。
    """
    count = ledger.confirmed_ticket_count(page_id)
    if count is None:
        return "PLACEHOLDER_PRIOR"
    return "POSTED_PRIOR" if count > 0 else "PLACEHOLDER_PRIOR"


def _classify_and_flush_page(writer, ledger, resolver, base, doc_type,
                             uploader_name, filename, page_num, total_pages,
                             results, page_bytes_list):
    """1 頁分の形状分類→（必要なら）査重・記帳。戻り値は main 内部の軽量タグ:
        ("ESCALATE", reason) — 呼出側は即座に ProcessOutcome.ESCALATED
        (kind, None)         — 頁級 outcome（§2.3 頁級モデルの値集合）

    URL 解決（resolver.resolve）は書込対象と確定した result にのみ行う——
    RETRYABLE/UNKNOWN（零書込・今回限り）や escalate 局面で無駄な単頁 PDF
    アップロードを起こさない（旧実装は _page_error を即 continue していたため
    そもそも解決していなかった、その保証を新モデルでも保つ）。
    """
    from posting_ledger import derive_page_id, PageDecision

    page_id = derive_page_id(base, page_num)
    shape, detail = _classify_page_result_shape(results)

    if shape == "escalate":
        return ("ESCALATE", detail)

    if shape == "error":
        # #1c: 前輪で既に CONFIRMED 済みの頁なら、今回の一時故障で檔級語義を
        # 翻さない（再 OCR は既定口徑——process_pipeline に start_page は渡さない）
        decision = ledger.check_page(page_id)
        if decision is PageDecision.SKIP:
            return (_prior_page_kind(ledger, page_id), None)
        if decision is PageDecision.ESCALATE:
            return ("ESCALATE", "ledger_witness_ambiguous")
        return (detail, None)  # 未確認頁のみ今回の一時故障を反映（零書込）

    if shape == "content_error":
        results = [_apply_content_error_override(
            results[0], filename, page_num, total_pages)]

    is_placeholder = shape in ("content_error", "placeholder")

    urls = [resolver.resolve(page_num, total_pages, pb) for pb in page_bytes_list]
    for result in results:
        entries = result.get("entries", [])
        print(f"📄 [{page_num}/{total_pages}] 取引先: {result.get('vendor')} | "
              f"仕訳: {len(entries)}行")

    outcome = _flush_page(writer, ledger, base, doc_type, uploader_name,
                          page_num, results, urls)
    if outcome == "escalate":
        return ("ESCALATE", "ledger_witness_ambiguous")
    if outcome == "written":
        return ("PLACEHOLDER_WRITTEN" if is_placeholder else "POSTED_NOW", None)
    return (_prior_page_kind(ledger, page_id), None)  # "skipped"


def _build_dead_letter_payload(total_pages, failed_page_nums):
    """DEAD_LETTER report_dead_letter(error=...) の payload（B4 Plan §2.3 釘死）。

    技術字段のみ（頁碼/件数/固定 stage・error_class）。ファイル名/客戶名/金額/
    例外原文は一切含めない（basic-design/03 §3 日誌白名単、事故排障最小欄位集）。
    """
    pages_str = ",".join(f"p{n}" for n in failed_page_nums)
    return {
        "stage": "ocr",
        "error_class": "NON_RETRYABLE",
        "message": (f"all_pages_unreadable: {len(failed_page_nums)}/"
                    f"{total_pages} pages [{pages_str}]"),
    }


def _aggregate_file_outcome(page_kinds, ordered_page_nums, total_pages):
    """檔級終態を頁級 outcome から短絡順位で決定する（B4 Plan §2.3 終態マトリクス、
    ESCALATE は呼出側で頁単位に即返しているためここには現れない）。

    優先順位：RETRYABLE 頁存在 → UNKNOWN 頁存在 → 全頁占位（DEAD_LETTER）→
    一部頁占位（PARTIAL）→ 全頁 POSTED（SUCCESS）。
    """
    if not ordered_page_nums:
        return HeadlessOutcome(ProcessOutcome.FAILED)  # total_pages==0（稀有、現状維持）

    kinds = [page_kinds[n] for n in ordered_page_nums]

    if "RETRYABLE" in kinds:
        return HeadlessOutcome(ProcessOutcome.FAILED, retryable=True)
    if "UNKNOWN" in kinds:
        return HeadlessOutcome(ProcessOutcome.FAILED, retryable=False)

    if all(k in _PLACEHOLDER_KINDS for k in kinds):
        failed_page_nums = [n for n in ordered_page_nums
                            if page_kinds[n] in _PLACEHOLDER_KINDS]
        payload = _build_dead_letter_payload(total_pages, failed_page_nums)
        return HeadlessOutcome(ProcessOutcome.DEAD_LETTER, dead_letter_payload=payload)
    if any(k in _PLACEHOLDER_KINDS for k in kinds):
        return HeadlessOutcome(ProcessOutcome.PARTIAL)
    return HeadlessOutcome(ProcessOutcome.SUCCESS)


def _process_file_headless(service, writer, file_path, uploader_name, base,
                           ledger, doc_type, drive_file_id, split_pdf_folder_id):
    """HEADLESS_MODE の頁級緩衝集約経路（IP-304/IP-306）。HeadlessOutcome を返す。

    process_pipeline の yield を page_num で連続緩衝し、頁境界で flush（形状分類→
    査重→原子書込）。緩衝は一頁分のみ（CLAUDE.md メモリ硬約束、頁 flush 後に解放）。
    占位頁（CONTENT/認識不能）は continue で読み飛ばさず、正常頁と同じ頁緩衝→
    頁原子書込の経路を通る（B4 Plan §2.2、旧・檔級聚合占位行塊は廃止済み）。
    page_num の再登場は ESCALATE（#6 連続性 contract、静默拆頁禁）。
    """
    filename = os.path.basename(file_path)
    base_url = _drive_view_url(drive_file_id) if drive_file_id else ""
    resolver = PageUrlResolver(
        service, base_url, filename, split_pdf_folder_id, drive_file_id)

    buffered_num = None
    buffered_total_pages = None
    buffered_results = []
    buffered_page_bytes = []
    seen_pages = set()
    page_kinds: dict[int, str] = {}
    ordered_page_nums: list[int] = []
    file_total_pages = 0

    def flush(page_num_, total_pages_, results_, page_bytes_):
        """緩衝頁を分類→（必要なら）記帳。("ESCALATE", reason) か (kind, None) か None を返す。"""
        if not results_:
            return None
        return _classify_and_flush_page(
            writer, ledger, resolver, base, doc_type, uploader_name, filename,
            page_num_, total_pages_, results_, page_bytes_)

    for page in process_pipeline(file_path, doc_type=doc_type):
        result = page["result"]
        page_num = page["page_num"]
        total_pages = page["total_pages"]
        file_total_pages = total_pages

        # 頁境界：page_num が変わったら前頁を flush してから当頁の緩衝を開始
        if buffered_num is not None and page_num != buffered_num:
            classified = flush(buffered_num, buffered_total_pages,
                               buffered_results, buffered_page_bytes)
            if classified is not None:
                kind, reason = classified
                if kind == "ESCALATE":
                    return HeadlessOutcome(ProcessOutcome.ESCALATED)
                page_kinds[buffered_num] = kind
                ordered_page_nums.append(buffered_num)
            seen_pages.add(buffered_num)
            buffered_results = []
            buffered_page_bytes = []

        # #6 連続性 contract：処理済み頁番号の再登場（静默拆頁）は ESCALATE
        if page_num in seen_pages:
            print(f"🚨 頁番号 {page_num} が非連続に再登場 → ESCALATE（拆頁前提破れ）")
            return HeadlessOutcome(ProcessOutcome.ESCALATED)

        buffered_num = page_num
        buffered_total_pages = total_pages
        result["uploader"] = uploader_name
        buffered_results.append(result)
        buffered_page_bytes.append(page.get("page_bytes"))

    classified = flush(buffered_num, buffered_total_pages,
                       buffered_results, buffered_page_bytes)
    if classified is not None:
        kind, reason = classified
        if kind == "ESCALATE":
            return HeadlessOutcome(ProcessOutcome.ESCALATED)
        page_kinds[buffered_num] = kind
        ordered_page_nums.append(buffered_num)

    return _aggregate_file_outcome(page_kinds, ordered_page_nums, file_total_pages)


def process_file(service, sheets_writer, file_path, uploader_name, chat_id,
                  doc_type=DocType.RECEIPT, drive_file_id=None,
                  split_pdf_folder_id="", base=None, ledger=None):
    """ファイルを処理し、Google Sheets に逐次書き込み、通知を送信する。

    ledger 非 None（HEADLESS_MODE）は頁級緩衝集約＋硬去重の経路へ分流し ProcessOutcome
    を返す。ledger None（UI 版）は従来の逐 result append_entries で bool を返す（挙動不変）。

    process_pipeline がジェネレータなので、1ページ処理→即Sheets書き込み→
    メモリ解放→次ページ の流れでメモリ使用量を最小化する。

    split_pdf_folder_id もプロファイルごとに異なる。共通の分割保存先へ
    アップロードすると、社長専用シートの原票 URL が全社から閲覧可能になる。
    """
    if ledger is not None:
        return _process_file_headless(
            service, sheets_writer, file_path, uploader_name, base, ledger,
            doc_type, drive_file_id, split_pdf_folder_id)

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

    # ヘッドレスモード（サンデヴィスタン統合、IP-303）: 入口守衛が使う
    # Firestore reporter を起動時に一度だけ構築する。UI 版
    # （HEADLESS_MODE 未設定）では reporter は None のまま、既存挙動に
    # 一切影響しない。
    reporter = _init_headless_reporter()

    # 拒絶件記憶層（IP-303、進程内快取）: alert 送達済みだが move が未完了の
    # file_id を憶えておき、次輪で check_intake/get_job/write_alert を
    # 再実行させない（Firestore 無界重打の防止）。プロセス再起動で自然に消える。
    quarantine_alerted: dict[str, str] = {}

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

                    # 0. ヘッドレスモード入口守衛（IP-303）: 防重檢測より前に
                    # base posting_id を検証する。無 posting_id / job 不一致件
                    # はここで隔離夾へ退避し、以降の処理（Sheets 書込含む）へ
                    # 進ませない。
                    should_process, base = _headless_intake_gate(
                        service, file, input_folder_id, reporter,
                        alerted=quarantine_alerted)
                    if not should_process:
                        print("=" * 30)
                        continue

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

                    # 頁級台賬（IP-304、headless のみ）: 1 ファイル＝1 job、job_key=base。
                    # reporter と同一 Firestore client を再利用、witness probe は writer 提供。
                    ledger = None
                    if reporter is not None and base is not None:
                        from posting_ledger import PostingLedger
                        ledger = PostingLedger(
                            reporter.client, base, sheet_probe=writer.probe_page)

                    # PDF 間分割線 + 取引No リセットは UI 版のみ（headless は取引No を
                    # Sheets A 列から都度再構築＝崩潰重跑冪等、分割線リセットは不要・有害）。
                    if ledger is None:
                        writer.start_new_file(uploader_name, doc_type, file_name)

                    outcome = process_file(
                        service, writer, local_path,
                        uploader_name, chat_id,
                        doc_type=doc_type, drive_file_id=file_id,
                        split_pdf_folder_id=profile["split_pdf_folder_id"],
                        base=base, ledger=ledger,
                    )

                    if ledger is not None:
                        # headless 五態（B4 Plan §2.3）。HeadlessOutcome.outcome を
                        # 取り出す——本ブロックは T3 時点の暫定適配（move の要否・
                        # reporter 回報・memo・intake 白名単の本接線は IP-308/T4）。
                        h_outcome = outcome.outcome
                        if h_outcome is ProcessOutcome.SUCCESS:
                            move_file(service, file_id, input_folder_id, processed_folder_id)
                        elif h_outcome is ProcessOutcome.ESCALATED:
                            print("🚨 ESCALATE: ファイル保持・回報せず（控制面へ委ねる）")
                        elif h_outcome in (ProcessOutcome.PARTIAL, ProcessOutcome.DEAD_LETTER):
                            print(f"⚠️ {h_outcome.value}: ファイル保持（IP-308 回報接線待ち）。")
                        else:
                            print("⚠️ ファイル処理失敗（保持）。")
                    elif outcome:
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
