import os
import sys
import time
import io
import random
import re
from dataclasses import dataclass
from datetime import datetime
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
from ocr_engine import (
    EXCLUDE_DEST_AUDIT_TAB, EXCLUDE_DEST_MF_TAB, process_pipeline,
)
from sheets_output import (
    APPEND_RESULT_PLACEHOLDER,
    AUDIT_VERDICT_BRANCH, AUDIT_VERDICT_DRIFT, AUDIT_VERDICT_EXCLUDED,
    AUDIT_VERDICT_MISSING, JST, SheetsOutputWriter,
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
import firestore_report
import intake_guard
# PageDecision は main.py 内 2 箇所（_flush_page/_classify_and_flush_page）で
# 個別に遅延 import されていた重複を統合（simcodex Round 2 #6）。firestore_report
# が既に google-cloud-firestore を無条件 import 済みのため、posting_ledger の
# 軽量シンボル（Enum のみ）をここで top-level import しても既存の遅延 import
# 方針（PostingLedger/TicketSummary/PagePostingSummary/derive_page_id は
# 各関数内のまま）と依存負荷は変わらない。
from posting_ledger import PageDecision

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


# B4 Plan §2.4: 契約が授権する記帳窓口はこの状態のみ（IP-303 注記④の完全実装）。
# POSTED/POST_UNKNOWN/DEAD_LETTER/終態/欠落 None は控制面の管轄——SS は無租約
# 状態で書込まない（POST_UNKNOWN は控制面の重投で POSTING_IN_PROGRESS に戻り
# 自然に放行される）。firestore_report.STATE_POSTING_IN_PROGRESS を参照
# （跨倉契約値の字面量重複を避ける——値の出所は一箇所のみ）。
_INTAKE_ALLOWED_JOB_STATE = firestore_report.STATE_POSTING_IN_PROGRESS


def _headless_intake_gate(service, file, input_folder_id, reporter, alerted=None):
    """監視フォルダ入口守衛（IP-303）+ 状態白名単（IP-308/T4、§2.4）を1ファイルに
    適用し (続行可, base, lease_epoch, state_rejected, customer_id,
    customer_label) を返す。

    非 HEADLESS_MODE では常に (True, None, None, False, None, None)
    （副作用なし）。HEADLESS_MODE では intake_guard.handle_intake_gate の
    裁決を橋渡しし、PROCESS 時のみ base posting_id ＋lease_epoch を通す
    （IP-304 が頁級台賬の job_key に、IP-308 が report_posted/
    report_dead_letter の epoch 引数に使う）。intake_guard 自体の五分岐判定は
    零改動——状態白名単はこの消費側でのみ適用する（should_process=True でも
    job_state が POSTING_IN_PROGRESS でなければ本輪は処理をスキップする）。

    customer_id/customer_label：B7 Plan §5.1-d T4-1。intake_guard の
    IntakeGateResult をそのまま橋渡しする（呼出元 _process_one_file が
    headless の Sheets tab キー＝tab_owner の解決に使う）。

    state_rejected：True は「五分岐判定は PROCESS だが job_state が
    POSTING_IN_PROGRESS でない」局面のみ（simcodex Round 2 #2）。呼出元
    （_process_one_file）はこれを見て intake_state_memo（TTL 付き pre-gate
    スキップ memo）に記録するかを決める。それ以外の REJECTED/DEFERRED
    （no_posting_id 等）は state_rejected=False——REJECTED は intake_guard 自身の
    alerted/隔離で、DEFERRED は意図的な毎輪再試行で扱われるため、ここでは
    memo しない。
    """
    if not config.headless_mode():
        return True, None, None, False, None, None
    result = intake_guard.handle_intake_gate(
        file,
        get_job=reporter.get_job,
        write_alert=reporter.write_alert,
        move_to_quarantine=lambda: _move_file_raw(
            service, file["id"], input_folder_id, config.quarantine_folder_id()),
        alerted=alerted,
    )
    if not result.should_process:
        return (False, result.base, result.lease_epoch, False,
                result.customer_id, result.customer_label)
    if result.job_state != _INTAKE_ALLOWED_JOB_STATE:
        print(f"入口守衛: 状態非対象 file_id={file.get('id')} "
              f"job_state={result.job_state!r} → 本輪スキップ（不下載不OCR不打刻）")
        return (False, result.base, result.lease_epoch, True,
                result.customer_id, result.customer_label)
    return (True, result.base, result.lease_epoch, False,
            result.customer_id, result.customer_label)


# B4 Plan §2.4: ESCALATED memo の TTL（≈20輪。SCAN_INTERVAL=3s 前提で約60s、
# 契約相当の再試行窓——probe 例外分型は採らず check_page 遭遇時の自然な
# witness 再照合に委ねる、Plan 附録B #7 裁決）。
_ESCALATE_MEMO_TTL_CYCLES = 20


def _prune_headless_memo(memo, folder_id, files, *, file_id_index=2):
    """剪枝按夾（B4 Plan §2.4、DoD⑦）。

    この夾の list_files が成功した今回に限り、この夾に属す memo 項のうち
    今回の一覧に無い file_id を剪定する。呼出元は list_files 成功後にのみ
    本関数を呼ぶ前提——「夾列舉失敗不剪」は呼出順序だけで自然に満たされる
    （本関数自体に try/except は不要）。

    file_id_index：memo の種類によりキー内 file_id の位置が異なるため可変
    （outcome memo: (base, lease_epoch, file_id) → 既定 2／intake_state_memo:
    (base, file_id) → 1、simcodex Round 2 #2）。同じ剪定ロジックを両者で共用。
    """
    seen_ids = {f["id"] for f in files}
    stale_keys = [key for key, value in memo.items()
                  if value.get("folder_id") == folder_id
                  and key[file_id_index] not in seen_ids]
    for key in stale_keys:
        del memo[key]


def _headless_memo_skip(memo, key, cycle):
    """memo 命中判定（B4 Plan §2.4）。命中かつ TTL 未過期なら True（本輪スキップ）。

    ESCALATED は expire_cycle を持ち、過期（cycle >= expire_cycle）なら memo
    から削除して False を返す（重試を許可——費用防護は同進程内の緩衝に過ぎず
    正確性は担わない、と docstring で明示された設計どおり）。TTL 無し
    （expire_cycle=None）のエントリは epoch が変わるまで（＝別 key になるまで）
    恆に命中し続ける。
    """
    entry = memo.get(key)
    if entry is None:
        return False
    expire_cycle = entry.get("expire_cycle")
    if expire_cycle is not None and cycle >= expire_cycle:
        del memo[key]
        return False
    return True


def _record_headless_memo(memo, key, outcome_label, folder_id, expire_cycle=None):
    """memo へ書込む（B4 Plan §2.4、値＝{outcome, folder_id, expire_cycle}）。"""
    memo[key] = {"outcome": outcome_label, "folder_id": folder_id,
                 "expire_cycle": expire_cycle}


# epoch 欠落（違約態）時に SUCCESS/DEAD_LETTER と偽記しない誠実なラベル
# （simcodex Round 2 #1）。同鍵で次輪も再打不要（memo は記録される）だが、
# 実際には報告していない事実を memo 自身が語る。epoch が現れれば（鍵の
# lease_epoch が変わる）自然に放行される——既存の memo 鍵設計のまま。
_MEMO_LABEL_EPOCH_MISSING = "EPOCH_MISSING"


def _call_reporter_if_epoch_present(lease_epoch, file_id, call) -> bool:
    """epoch 欠落（違約態）は一律零 reporter 呼出し＋警告日誌（#3）。

    SUCCESS/DEAD_LETTER の両分岐が同じ守衛＋日誌文言を持っていたための抽出
    （simcodex Round 1 #8）。call は引数無しの実呼出しクロージャ（呼出元が
    report_posted/report_dead_letter のどちらを束縛するかを決める）。

    戻り値：実際に reporter を呼んだか（True/False）。呼出元（_report_headless_outcome）
    はこれを見て memo ラベルを選ぶ——epoch 欠落で呼んでいないのに "SUCCESS"/
    "DEAD_LETTER" と記録すると、実際には未報告なのに報告済みと偽ることになる
    （simcodex Round 2 #1、memo 誠実化）。
    """
    if lease_epoch is not None:
        call()
        return True
    print(f"⚠️ epoch欠落のため回報せず（違約態）file_id={file_id}")
    return False


def _report_headless_outcome(reporter, base, lease_epoch, outcome, file_id, cycle):
    """HeadlessOutcome を回報接線へ渡す（B4 Plan §2.3、IP-308 本体）。

    戻り値 (outcome_label, expire_cycle)。outcome_label が None なら memo に
    記録しない（RETRYABLE 由来の FAILED——3秒自癒窗）。epoch 欠落（違約態）は
    一律零 reporter 呼出し＋警告日誌＋ファイル保持（#3、_call_reporter_if_epoch_present）
    ——このとき memo ラベルは "SUCCESS"/"DEAD_LETTER" ではなく
    _MEMO_LABEL_EPOCH_MISSING（誠実化、simcodex Round 2 #1）。スキップ挙動
    そのもの（檔案保持・同鍵不重打）は変わらない——鍵に lease_epoch を含む
    ため epoch が現れれば別鍵として天然放行される。
    回報結果（APPLIED/ALREADY_DONE/REJECTED）の事後処置は firestore_report._report が
    内部で完結する（診断 print＋REJECTED 時の alert 書込）——本関数は呼ぶだけで、
    結果に応じた再試行／move は一切しない（契約：REJECTED でも SS は不重試不move）。
    """
    h_outcome = outcome.outcome
    if h_outcome is ProcessOutcome.SUCCESS:
        called = _call_reporter_if_epoch_present(
            lease_epoch, file_id,
            lambda: reporter.report_posted(base, lease_epoch=lease_epoch))
        return ("SUCCESS" if called else _MEMO_LABEL_EPOCH_MISSING), None
    if h_outcome is ProcessOutcome.DEAD_LETTER:
        called = _call_reporter_if_epoch_present(
            lease_epoch, file_id,
            lambda: reporter.report_dead_letter(
                base, lease_epoch=lease_epoch, error=outcome.dead_letter_payload))
        return ("DEAD_LETTER" if called else _MEMO_LABEL_EPOCH_MISSING), None
    if h_outcome is ProcessOutcome.PARTIAL:
        # TBD 接縫（F06-How 未確定、正式回報は本批の非目標）: 日誌のみ豁免記録
        print(f"⚠️ PARTIAL: 未回報（TBD接縫、F06-How未確定）file_id={file_id}")
        return "PARTIAL", None
    if h_outcome is ProcessOutcome.ESCALATED:
        print("🚨 ESCALATE: ファイル保持・回報せず（控制面へ委ねる）")
        return "ESCALATED", cycle + _ESCALATE_MEMO_TTL_CYCLES
    # FAILED: RETRYABLE 由来は不記（3秒自癒窗）、それ以外（UNKNOWN/稀有な
    # total_pages==0）は記（per epoch、控制面の重投でのみ再試行）
    if outcome.retryable:
        print("⚠️ ファイル処理失敗（一時的、次回サイクルで自癒）。")
        return None, None
    print("⚠️ ファイル処理失敗（保持、memo記録）。")
    return "FAILED", None


# B7 Plan §5.1-d T4-2: Sheets tab 上限（末尾に他要素を足す余地込みで 100 桁）。
_TAB_LABEL_MAX_LEN = 100
# customer_label の区切り記号（契約 §2 想定字面「顧客番号_顧客名」の実体は
# 全角空格区切り、Plan T4 DoD② `20220401　株式会社緒方材木店` 参照）。
_CUSTOMER_LABEL_SEPARATOR = "　"


def _validate_customer_label(customer_id, customer_label):
    """customer_label が §5.1-d T4-2 の 4 条件を満たすか（純関数）。

    ①非空 str ②customer_id で始まる ③区切りは全角空格＋非空の名前部
    ④長さ ≤100。文字列の推測・整形は行わない（不通過は呼出側が
    customer_id 単独へ縮退する）。
    """
    if not isinstance(customer_label, str) or not customer_label:
        return False
    if len(customer_label) > _TAB_LABEL_MAX_LEN:
        return False
    if not customer_label.startswith(customer_id):
        return False
    rest = customer_label[len(customer_id):]
    if not rest.startswith(_CUSTOMER_LABEL_SEPARATOR):
        return False
    name_part = rest[len(_CUSTOMER_LABEL_SEPARATOR):]
    return bool(name_part)


def _resolve_tab_owner(customer_id, customer_label):
    """headless の tab_owner（Sheets tab キー）を決定する（§5.1-d T4-2）。

    customer_id が非空 str でない（None・数値・空文字等の奇形）→ None
    （呼出側が冪等 alert＋処理スキップする、T4-3。codex R1-P2: 非 str を
    startswith や tab 名へ通すと TypeError で輪詢サイクル全体が落ちる——
    奇形は例外でなく既設の alert 経路へ封じ込める）。
    customer_label 検証通過 → label 採用（顧客番号_顧客名 の完全形）。
    不通過／欠落 → customer_id 単独へ縮退＋警告 print（§5.1-d の明示的例外
    状態——顧客分離の担保は job 文書由来の customer_id 自体が持つため、
    label 不正のみで fail-closed 停止はしない）。
    """
    if not isinstance(customer_id, str) or not customer_id:
        return None
    if _validate_customer_label(customer_id, customer_label):
        return customer_label
    print(f"入口守衛: customer_label 不正/未設定 → customer_id 単独 tab へ縮退 "
          f"customer_id={customer_id!r} customer_label={customer_label!r}")
    return customer_id


class RetryAction(Enum):
    """`_process_one_file` が返す「失敗退避記録への操作」（Plan D4）。

    業務成否ではなく**操作**で命名する（Codex 対抗評審 P2 採納）。両者は
    一致しない: headless の `DEAD_LETTER` は業務上は失敗だが、終態を回報
    済みで SS は二度と再試行しないので、退避記録としては CLEAR が正しい。
    `SUCCESS/FAILURE` と名付けると、この種の対応を毎回誤らせる。

    CLEAR     : 退避記録を消す（この file は決着した）
    INCREMENT : 失敗を 1 件加算し、次回試行を後ろへ倒す
    KEEP      : 退避記録に一切触れない（別の抑制機構が担当中）
    """

    CLEAR = "clear"
    INCREMENT = "increment"
    KEEP = "keep"


def _apply_retry_action(retry_state, file_id, file_name, action, now_ts):
    """RetryAction を退避記録へ適用し、INCREMENT なら退避開始を告知する。

    純関数ではない（retry_state をその場で更新する）が、I/O は console 出力
    だけなので main loop を起動せずに単体検証できる。KEEP は意図的な no-op
    ——memo 等の別機構が抑制を担当している経路で、ここで加算すると二重抑制
    になり、控制面が直した後の再開まで最大 1 時間遅れる（Plan D4）。
    """
    if action is RetryAction.CLEAR:
        _record_file_success(retry_state, file_id)
        return
    if action is RetryAction.INCREMENT:
        entry = _record_file_failure(retry_state, file_id, now_ts)
        next_attempt = _format_jst_timestamp(entry["next_attempt_ts"])
        print(f"🛑 退避開始: {file_name}"
              f"（累計失敗 {entry['count']} 回）"
              f" 次回試行予定: {next_attempt}")


def _process_one_file(service, writer, job_reporter, file, input_folder_id, doc_type,
                      processed_folder_id, split_pdf_folder_id, quarantine_alerted,
                      headless_memo, intake_state_memo, cycle, *,
                      progress_reporter=None, customer_meta_alerted=None,
                      provider_sink=None):
    """1 ファイルの取り込み～記帳～終態処理（main() の for file in files: 本体を
    可測化のため抽出、ロジック改変なし——IP-303/304/306/308 全接線の実体）。

    UI 版（job_reporter=None）と headless 版の分岐は本関数の内部で行う。main() 側は
    ファイル一覧の反復と本関数の呼出しのみ担う。

    intake_state_memo：状態白名単で継続的にスキップされる file の pre-gate
    memo（simcodex Round 2 #2、outcome memo=headless_memo とは別鍵形
    (base, file_id)——lease_epoch は gate 呼出し前は未知のため）。
    """
    file_id = file['id']
    file_name = file['name']
    md5 = file.get('md5Checksum')

    # -0.5 intake 状態白名単の pre-gate memo（headless のみ）: 前輪で状態非対象
    # と判定済み（TTL 内）の file は intake gate（Firestore get_job 読取）
    # 自体を省く——無界の毎輪 get_job 連打を防ぐ（simcodex Round 2 #2）。
    # base は Drive properties から純関数で読める（Firestore 不要）ため、
    # gate 呼出し前でもキー参照できる。
    if job_reporter is not None:
        pre_base = intake_guard.resolve_posting_id(file)
        if pre_base is not None and _headless_memo_skip(
                intake_state_memo, (pre_base, file_id), cycle):
            print(f"headless memo 命中（状態非対象・TTL内）→ 本輪スキップ file_id={file_id}")
            print("=" * 30)
            return RetryAction.KEEP  # memo が抑制中（Plan D4 #1）

    # 0. ヘッドレスモード入口守衛（IP-303）+ 状態白名単（IP-308/T4）: 防重檢測より
    # 前に base posting_id ＋ job 状態を検証する。
    (should_process, base, lease_epoch, state_rejected, customer_id,
     customer_label) = _headless_intake_gate(
        service, file, input_folder_id, job_reporter, alerted=quarantine_alerted)
    if not should_process:
        if job_reporter is not None and state_rejected and base is not None:
            _record_headless_memo(
                intake_state_memo, (base, file_id), "STATE_NOT_ALLOWED",
                input_folder_id, cycle + _ESCALATE_MEMO_TTL_CYCLES)
        print("=" * 30)
        return RetryAction.KEEP  # gate 拒否＝intake 側の裁決（Plan D4 #2）

    # 0.5 memo（費用防護、IP-308/T4、headless のみ）: 同 epoch 内で既に終態記録
    # 済みの file は本輪スキップ（零下載零OCR）。
    memo_key = (base, lease_epoch, file_id)
    if job_reporter is not None and _headless_memo_skip(headless_memo, memo_key, cycle):
        print(f"headless memo 命中 → 本輪スキップ file_id={file_id}")
        print("=" * 30)
        return RetryAction.KEEP  # memo が抑制中（Plan D4 #3）

    # 1. 防重檢測（UI 版のみ——headless は頁級台賬(IP-304)が硬去重を担い、
    # SS はファイル単位の move も行わない、B4 Plan §2.6 move 出口全審計）
    if job_reporter is None:
        if is_duplicate_file(service, md5, processed_folder_id):
            print(f"⚠️ 重複アップロードを検出: {file_name}")
            print("   -> 処理をスキップしてアーカイブします")
            move_file(service, file_id, input_folder_id, processed_folder_id)
            print("=" * 30)
            # archive 完了＝決着。以後 list_files に現れないので記録も消す
            # （Plan D4 #4）。
            return RetryAction.CLEAR

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
        # main 側の原実装もここは護欄の外（continue）だった。行為を変えない
        # ため KEEP（Plan D4 #5）。
        return RetryAction.KEEP

    # 3.5 headless tab_owner 解決（§5.1-d T4-2/T4-3、headless のみ）:
    # customer_id 欠落は奇形 job（契約 §2 違反）——ダウンロード前に冪等
    # alert＋処理スキップ・ファイル保持。次輪で job 文書を再読し、控制面が
    # 修復すれば自然回復する（終端性）。customer_label の検証・整形は
    # ここで完結させる（SS 側で推測はしない）。
    tab_owner = None
    if job_reporter is not None:
        tab_owner = _resolve_tab_owner(customer_id, customer_label)
        if tab_owner is None:
            # customer_meta_alerted（進程内快取、simcodex R1）: 3 秒輪詢で同一
            # file に毎輪 write_alert を打たない（alert は文書 ID＝file_id で
            # 冪等だが、控制面が job を直すまで無界の Firestore 写が続く）。
            # 送達失敗時は memo 不記録＝下輪必ず再送（quarantine_alerted と
            # 同じ「alert 未達なら再送させる」規律）。job の再読・解決判定は
            # 毎輪行う（終端性＝控制面修復の次輪に自然回復）。
            already_alerted = (customer_meta_alerted is not None
                               and file_id in customer_meta_alerted)
            if not already_alerted:
                try:
                    job_reporter.write_alert(file_id, {
                        "kind": "customer_metadata_missing",
                        "file_id": file_id,
                        "posting_id": base,
                    })
                except Exception as exc:  # noqa: BLE001 - alert 失敗も保持・下輪再試行
                    print(f"入口守衛: customer_metadata_missing alert 書込失敗 "
                          f"file_id={file_id} error_type={type(exc).__name__}")
                else:
                    if customer_meta_alerted is not None:
                        customer_meta_alerted[file_id] = True
            print(f"入口守衛: customer_id 欠落（奇形 job）→ 処理スキップ・"
                  f"ファイル保持 file_id={file_id} posting_id={base}")
            print("=" * 30)
            # KEEP（Plan D4 #6・§12【3】で辯論の上 維持）。この経路の既存契約は
            # 「控制面が job を直せば次輪で自然回復」であり、INCREMENT すると
            # その回復検知が最大 1 時間遅れて契約自体を変えてしまう。無界の
            # Firestore get_job と console churn は既知問題として §10 に記載
            # （正しい対処は backoff ではなく customer_meta_alerted の TTL 化）。
            return RetryAction.KEEP
        if customer_meta_alerted is not None:
            # 解決成功＝欠落状態の解消。memo を解いておき、将来の再発時に
            # 改めて alert が飛ぶようにする（_finish_quarantine_move の
            # 「move 成功で alerted から削除」と同型）。
            customer_meta_alerted.pop(file_id, None)

    # 4. 下載與處理
    local_path = download_file(service, file_id, file_name)

    try:

        # 頁級台賬（IP-304、headless のみ）: 1 ファイル＝1 job、job_key=base。
        # job_reporter と同一 Firestore client を再利用、witness probe は writer 提供。
        ledger = None
        page_outcomes = None
        if job_reporter is not None and base is not None:
            from posting_ledger import PostingLedger
            ledger = PostingLedger(
                job_reporter.client, base, sheet_probe=writer.probe_page)
            # 頁処理結果台帳（契約 §5.6、B7-2 T3）。1 ファイル＝1 個、
            # client は檔級回報（job_reporter＝job 状態）と同一を再利用。
            from firestore_progress import FirestorePageOutcomesReporter
            page_outcomes = FirestorePageOutcomesReporter(job_reporter.client, base)

        # PDF 間分割線 + 取引No リセットは UI 版のみ（headless は取引No を
        # Sheets A 列から都度再構築＝崩潰重跑冪等、分割線リセットは不要・有害）。
        if ledger is None:
            writer.start_new_file(uploader_name, doc_type, file_name)

        try:
            outcome = process_file(
                service, writer, local_path,
                uploader_name, chat_id,
                doc_type=doc_type, drive_file_id=file_id,
                split_pdf_folder_id=split_pdf_folder_id,
                base=base, ledger=ledger,
                # Sheets 進捗タブは UI 版のみ接続（headless の頁級可視化権威は
                # 控制面 page_outcomes——B7-2 Plan §2 非目標）。
                progress=progress_reporter if ledger is None else None,
                page_outcomes=page_outcomes,
                # §5.1-d T4: headless の Sheets tab キー（None＝UI 版、行為零改動）。
                tab_owner=tab_owner,
                # §5.7 B8: provider 事件の書込口。**ループが持つ 1 個**を貫通させる
                # （檔ごとに作ると配額が檔ごとに再配分され、障害中に小さな檔が
                # 連続した時に総書込量の上界が消える——simcodex R4）。
                # UI 版は None が渡り、行為零改動。
                event_sink=provider_sink if ledger is not None else None,
            )
        finally:
            if page_outcomes is not None:
                # 檔終局の補写一輪（report_posted の**前**——頁時に失敗した行を
                # 回収してから檔級終態を回報する。なお失敗は放行＝reconciler 側で
                # 欠落として扱われる。B7-2 Plan §9 #3/#9 裁決）。finally なのは
                # 途中例外でも真に決算済みの頁の行を落とさないため（flush 自体は
                # 決して raise しない）。
                page_outcomes.flush_pending()

        if ledger is not None:
            # headless 五態（B4 Plan §2.3）。move は全出口で削除済み（§2.6 全審計、
            # SUCCESS も含め move 零呼出——回報 report_posted/report_dead_letter に
            # 代替）。memo は outcome_label が非 None のときのみ記録する。
            outcome_label, expire_cycle = _report_headless_outcome(
                job_reporter, base, lease_epoch, outcome, file_id, cycle)
            if outcome_label is not None:
                _record_headless_memo(headless_memo, memo_key, outcome_label,
                                      input_folder_id, expire_cycle)
            # headless が HeadlessOutcome を正常に返した以上、**五態すべて** CLEAR
            # （Plan D4 #9）。headless は outcome ごとの再試行抑制を memo 側で完結
            # させており、そこへ backoff を重ねると:
            #   ESCALATED          → memo TTL 20 輪との二重抑制。控制面の修復後の
            #                        再判定が最大 1 時間遅れる
            #   FAILED(retryable)  → B4 が意図的に設けた「3 秒自癒窓」（memo 不記）
            #                        が 3s→30s→5min→30min→1h へ変質する＝既定設計の
            #                        静かな転覆
            # backoff が headless で担当するのは例外だけ（outcome も memo も無く、
            # 唯一の無限リトライ経路になるため）。それは main() 側の try で拾う。
            retry_action = RetryAction.CLEAR
        elif outcome:
            move_file(service, file_id, input_folder_id, processed_folder_id)
            retry_action = RetryAction.CLEAR
        else:
            print("⚠️ ファイル処理失敗。")
            # UI 経路の失敗＝backoff が担当する唯一の正常系経路（Plan D4 #8）。
            # ファイルは IP-401 どおり input に保持され、行き先は変わらない。
            retry_action = RetryAction.INCREMENT

        return retry_action
    finally:
        # 一時ファイル削除と flush は finally に置く（Plan D4・Codex 採納）。
        # per-file の例外捕捉を main() 側へ移したので、ここで例外が抜けると
        # そのまま関数を出る——正常系にだけ削除を書くと、失敗のたびに
        # 一時ファイルが残り続ける。
        if os.path.exists(local_path):
            os.remove(local_path)
            print("🧹 一時ファイルを削除しました")

        # 取引No はタブの A 列から都度算出するため書き戻し不要。
        # flush() は将来の後処理フック用に呼び出しだけ残す。
        writer.flush()

        print("=" * 30)


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

    def __post_init__(self) -> None:
        """不変式検証（simcodex Round 1 #10）：retryable/dead_letter_payload は
        対応する outcome でのみ意味を持つ——他 outcome との誤組合せは fail fast。
        """
        if self.retryable and self.outcome is not ProcessOutcome.FAILED:
            raise ValueError(
                f"HeadlessOutcome: retryable=True は outcome==FAILED でのみ許可"
                f"（受取 outcome={self.outcome}）")
        if self.dead_letter_payload is not None and self.outcome is not ProcessOutcome.DEAD_LETTER:
            raise ValueError(
                f"HeadlessOutcome: dead_letter_payload は outcome==DEAD_LETTER でのみ許可"
                f"（受取 outcome={self.outcome}）")


def _has_entries(result) -> bool:
    """result が有効仕訳（entries 非空）を持つか（simcodex Round 2 #9、
    main.py 内 3 箇所に散っていた r.get("entries") 真値判定を一元化）。"""
    return bool(result.get("entries"))


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
        for r in results if _has_entries(r)
    ]


def _flush_page(writer, ledger, page_id, doc_type, tab_owner, page_num,
                results, urls):
    """1 頁分の查重→記帳。戻り値 'written'|'skipped'|'escalate'。

    page_id は呼出元（_classify_and_flush_page）が既に derive_page_id 済みの値を
    そのまま受け取る（同一頁で二重算出しない）。

    tab_owner：writer の tab キー引数（§5.1-d T4、Codex #9 採納の貫通引数名）。
    行内容の投稿者列（result["uploader"]）とは別系統——本関数は tab 識別のみ
    担い、行内容には一切触れない（F5、呼出元 _process_file_headless 参照）。

    check_page で SKIP（既 CONFIRMED/witness PRESENT）→ 何も書かない（硬去重）。
    ESCALATE（witness 不確実）→ 呼出側でファイル保持・回報せず。WRITE → build_page_write
    →（PENDING→append→CONFIRMED）を ledger.post_page が原子封装。results が
    全件 entries 空（占位頁）でも build_page_write は _build_unrecognized_block
    で占位行 1 行を天然生成する（sheets_output.py 零改動、B4 Plan §2.2）。
    """
    from posting_ledger import PagePostingSummary
    from sheets_output import compute_page_fingerprint

    decision = ledger.check_page(page_id)
    if decision is PageDecision.SKIP:
        print(f"⏭️ 頁 {page_num} は既記帳（台賬）→ スキップ")
        return "skipped"
    if decision is PageDecision.ESCALATE:
        print(f"🚨 頁 {page_num} 判定不能（witness 不確実）→ ESCALATE（人工核）")
        return "escalate"

    start_txn = writer.next_txn_no(tab_owner, doc_type)
    page_write = writer.build_page_write(
        tab_owner, doc_type, results, urls, start_txn)
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

# IP-402 §4.1/§4.5: 除外ページ（封筒・社会保険料通知書）の頁級 kind。
# 占位（読めなかった）とは別物——本来記帳すべきでない頁なので、檔級終態の
# 母数から除く。
#
# 分類 drift（過去輪の占位 page doc がある頁を今輪 excluded と判定）にも
# 専用 kind は与えない（第3版 Plan は EXCLUDED_DRIFT を置いていたが、
# simcodex Round 1 の指摘を Codex が支持して撤回）。理由: 頁級 kind は
# `_aggregate_file_outcome` への一過性の入力であって履歴の記録ではない。
# drift の事実は監査行（AUDIT_VERDICT_DRIFT ＋ `drift:<prior>-><current>`）と
# 既存の page doc が持続的に持っており、集約層に別名を増やすと「網羅的に
# kind を分岐する後続コードは EXCLUDED_DRIFT が EXCLUDED の別名だと憶えて
# いなければならない」という負債だけが残る。
_EXCLUDED_KINDS = frozenset({"EXCLUDED"})


def _classify_excluded_results(results):
    """除外 result を 1 件以上含む頁の形状を決める（IP-402 §4.1 の混型不変式）。

    戻り値は ("excluded", destination) か ("escalate", reason)。

    現行 producer は除外を単独 yield＋即 return するので混型は起きない。
    `_yield_page_results` の変更で崩れうる不変式としてここに固定する
    （既存の "mixed_valid_and_placeholder_result" と同じ扱い）——書込先が
    一意でない頁を推測で書くくらいなら、書かずに人手へ渡す。
    """
    others = [r for r in results if not r.get("_excluded_page")]
    if others:
        if any(_has_entries(r) for r in others):
            return ("escalate", "mixed_excluded_and_valid")
        return ("escalate", "mixed_excluded_and_placeholder")

    # others が空＝results 全件が除外。改めて絞り込む必要はない。
    destinations = {r.get("_exclude_destination") or EXCLUDE_DEST_AUDIT_TAB
                    for r in results}
    if len(destinations) != 1:
        return ("escalate", "mixed_exclude_destinations")
    return ("excluded", destinations.pop())


def _classify_page_result_shape(results):
    """頁緩衝（同一 page_num の全 yield）の形状を分類する（§2.2/§2.3、純関数）。

    戻り値は (kind, detail) のいずれか：
        ("error", "RETRYABLE"|"UNKNOWN") — 頁エラー（transport/未知例外、単独 result）
        ("content_error", None)          — 頁エラー（CONTENT、票面不可読、単独 result）
        ("excluded", destination)        — 除外ページ（封筒/社会保険料、IP-402 §4.1）
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

    # 除外ページ（IP-402 §4.1）。error の後・valid/placeholder の前に判定する
    # ——除外は entries 空なので、ここを通さないと占位頁に化けて MF 区へ赤い
    # 占位行が書かれる（IP-402 が是正する誤認そのもの）。
    if any(r.get("_excluded_page") for r in results):
        return _classify_excluded_results(results)

    has_valid = any(_has_entries(r) for r in results)
    has_placeholder = any(not _has_entries(r) for r in results)
    if has_valid and has_placeholder:
        # 混型頁（Codex 複審 #14 採択）: ticket_count>0 が完全性を自証できなく
        # なるため、書込せず即座に人工核へ委ねる
        return ("escalate", "mixed_valid_and_placeholder_result")
    return ("valid", None) if has_valid else ("placeholder", None)


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


def _write_excluded_audit_row(writer, resolver, results, filename, page_num,
                              total_pages, verdict, reason):
    """除外ページの留痕を監査タブへ 1 行書く（headless 専用）。成功なら True。

    UI 版（`_record_excluded_page`）は監査タブが落ちたら MF の赤い占位行へ
    退避するが、headless では退避しない（§4.4）——控制面という別の可視化先が
    あるので、障害時に帳簿を汚してまで留痕する必要がない。失敗は握り潰さず
    False で返し、呼出側が ESCALATE（ファイル保持・未回報）にする。

    単ページ PDF は上げない（anchor_url）——除外ページのために Drive 書込を
    1 頁ずつ増やすのは割に合わない（UI 版と同じ判断）。
    """
    try:
        writer.append_audit_row(
            filename=filename,
            page_num=page_num,
            verdict=verdict,
            reason=reason,
            ocr_text_len=max((r.get("_ocr_text_len") or 0) for r in results),
            source_url=resolver.anchor_url(page_num, total_pages),
        )
    except Exception as e:
        print(f"❌ 監査タブへの除外記録に失敗 → ESCALATE（MF へは退避しない）: {e}")
        return False
    return True


def _handle_excluded_page(writer, ledger, resolver, page_id, destination,
                          filename, page_num, total_pages, results):
    """除外ページ 1 頁分（IP-402 §4.2/§4.3/§4.4）。戻り値は (kind, detail)。

    MF 区へは一切書かない——`_flush_page` を通さないので post_page /
    build_page_write / commit_page のいずれも呼ばれず、取引No も消費しない。
    行き先が `mf_tab`（社会保険料通知書）でも headless では書かない：外部可見・
    append-only の副作用には硬冪等が要るが、その器（effect 記録）は §9 へ繰延
    したので、副作用そのものを起こさないことで要件を満たす。

    page doc も作らない——除外を載せると `_prior_page_kind` が
    `ticket_count==0` を見て占位頁と同一視し、B4 の身分自証（>0 真データ /
    ==0 占位）が揺らぐ。ただし **check_page は必ず見る**。載せないことと
    参照しないことは別で、見ないと過去輪の記帳事実を見落とす。
    """
    decision = ledger.check_page(page_id)
    if decision is PageDecision.ESCALATE:
        # 診断ログは呼出側（_record_page_classification）が reason 付きで
        # 1 回出す。ここで出すと同一事象が二重に流れる。
        return ("ESCALATE", "ledger_witness_ambiguous")

    if decision is PageDecision.SKIP:
        # 過去輪に page doc がある＝今輪の「除外」と食い違う（分類 drift）。
        # 黙って捨てず、MF に残る旧行（仕訳 or 占位）と今輪の判定が矛盾して
        # いる事実を監査タブへ残して人手の突合材料にする。ESCALATE にはしない
        # ——60秒 TTL の無限再 OCR を招くだけで、自癒の見込みが無い（§1.4）。
        prior = _prior_page_kind(ledger, page_id)
        drift_reason = f"drift:{prior}->EXCLUDED"
        if not _write_excluded_audit_row(
                writer, resolver, results, filename, page_num, total_pages,
                AUDIT_VERDICT_DRIFT, drift_reason):
            return ("ESCALATE", "audit_write_failed")
        print(f"🔀 [{page_num}/{total_pages}] 分類変化 ({drift_reason}) → 監査タブに記録")
        # POSTED_PRIOR は維持する——MF に実在する仕訳行は分類が変わっても
        # 消えない。EXCLUDED に倒すと「零記帳」と集計され、実際には記帳済み
        # なのに終態が嘘になる（この kind は檔級終態を実際に変える）。
        # 占位行（PLACEHOLDER_PRIOR）は記帳ではなく「読めなかった」警告な
        # ので、今輪のより正確な判定で上書きしてよい——こちらは終態を変え
        # ないので専用 kind を作らず素の EXCLUDED を返す（drift の事実は
        # 直前に書いた監査行が持つ、_EXCLUDED_KINDS の注記参照）。
        # detail=classification_drift（B7-2 T3）: page_outcomes の reason で
        # drift を残す（outcome は帳務身分のまま、Codex 阻斷2 裁決）。
        return (prior if prior == "POSTED_PRIOR" else "EXCLUDED",
                "classification_drift")

    # 同頁に複数の除外 result が来た場合は理由を出現順に去重して連結する。
    # destination 不一致は escalate（行き先を推測で決めない）だが、reason は
    # 純粋な診断文字列で分岐に影響しないので、人手へ回すより落とさず全部
    # 残すほうが監査の役に立つ。
    reason = ",".join(dict.fromkeys(
        (r.get("_exclude_reason") or "unknown") for r in results))
    print(f"📨 [{page_num}/{total_pages}] 除外ページ ({reason}, 宣言先={destination}) "
          f"→ 監査タブに記録（headless は MF 区に一切書きません）")
    if not _write_excluded_audit_row(
            writer, resolver, results, filename, page_num, total_pages,
            AUDIT_VERDICT_EXCLUDED, reason):
        return ("ESCALATE", "audit_write_failed")
    # detail＝producer の _exclude_reason（単一なら envelope 等の機械キーの
    # まま page_outcomes の白名単翻訳へ届く。複数連結は reporter 側で
    # unclassified に降級——自由文字は不透過、B7-2 T3）。
    return ("EXCLUDED", reason)


def _classify_and_flush_page(writer, ledger, resolver, base, doc_type,
                             tab_owner, filename, page_num, total_pages,
                             results, page_bytes_list):
    """1 頁分の形状分類→（必要なら）査重・記帳。戻り値は main 内部の軽量タグ:
        ("ESCALATE", reason) — 呼出側は即座に ProcessOutcome.ESCALATED
        (kind, None)         — 頁級 outcome（§2.3 頁級モデルの値集合）

    URL 解決（resolver.resolve）は書込対象と確定した result にのみ行う——
    RETRYABLE/UNKNOWN（零書込・今回限り）や escalate 局面で無駄な単頁 PDF
    アップロードを起こさない（旧実装は _page_error を即 continue していたため
    そもそも解決していなかった、その保証を新モデルでも保つ）。
    """
    from posting_ledger import derive_page_id

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

    if shape == "excluded":
        # IP-402: 記帳経路（_flush_page）へは入れない。detail は行き先宣言。
        return _handle_excluded_page(
            writer, ledger, resolver, page_id, detail, filename, page_num,
            total_pages, results)

    if shape == "content_error":
        results = [_apply_content_error_override(
            results[0], filename, page_num, total_pages)]

    is_placeholder = shape in ("content_error", "placeholder")

    urls = [resolver.resolve(page_num, total_pages, pb) for pb in page_bytes_list]
    for result in results:
        entries = result.get("entries", [])
        print(f"📄 [{page_num}/{total_pages}] 取引先: {result.get('vendor')} | "
              f"仕訳: {len(entries)}行")

    outcome = _flush_page(writer, ledger, page_id, doc_type, tab_owner,
                          page_num, results, urls)
    if outcome == "escalate":
        return ("ESCALATE", "ledger_witness_ambiguous")
    if outcome == "written":
        # detail 槽は page_outcomes の reason 機械キー（非 ESCALATE では従来
        # 未使用だった、B7-2 T3）。content_error は「読めなかった」ことを
        # closed キーで運ぶ。
        if shape == "content_error":
            return ("PLACEHOLDER_WRITTEN", "content_unreadable")
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

    優先順位：RETRYABLE 頁存在 → UNKNOWN 頁存在 →（以降は除外頁を母数から
    除いて）零記帳＝全頁除外（SUCCESS、P0-10）→ 全頁占位（DEAD_LETTER）→
    一部頁占位（PARTIAL）→ 全頁 POSTED（SUCCESS）。

    除外頁（IP-402 §4.5）は「読めなかった頁」ではなく「本来記帳すべきでない頁」
    なので母数から除く。除いた結果が空＝全頁除外は**正常完了＝SUCCESS**
    （裁定2・P0-10、趙 2026-08-01 拍板）。記帳頁が 1 つでもあれば、封筒が
    混ざっていても SUCCESS でよい——最頻ケース（仕訳頁＋封筒頁）を永久
    非終端にしない（目標3）。
    """
    if not ordered_page_nums:
        return HeadlessOutcome(ProcessOutcome.FAILED)  # total_pages==0（稀有、現状維持）

    kinds = [page_kinds[n] for n in ordered_page_nums]

    if "RETRYABLE" in kinds:
        return HeadlessOutcome(ProcessOutcome.FAILED, retryable=True)
    if "UNKNOWN" in kinds:
        return HeadlessOutcome(ProcessOutcome.FAILED, retryable=False)

    accounted = [k for k in kinds if k not in _EXCLUDED_KINDS]
    if not accounted:
        # 全頁除外＝正常完了（SUCCESS 側・report_posted で終態へ）。一条も
        # 記帳すべきでないファイルは「直接算過」——契約 v0.15 §5.1-b 裁定2、
        # P0-10（趙 2026-08-01 拍板）。旧 PARTIAL（意図的沈黙）は lease 超時
        # 待ちの偽未完了件を対账に残していた。
        return HeadlessOutcome(ProcessOutcome.SUCCESS)

    if all(k in _PLACEHOLDER_KINDS for k in accounted):
        failed_page_nums = [n for n in ordered_page_nums
                            if page_kinds[n] in _PLACEHOLDER_KINDS]
        payload = _build_dead_letter_payload(total_pages, failed_page_nums)
        return HeadlessOutcome(ProcessOutcome.DEAD_LETTER, dead_letter_payload=payload)
    if any(k in _PLACEHOLDER_KINDS for k in accounted):
        return HeadlessOutcome(ProcessOutcome.PARTIAL)
    return HeadlessOutcome(ProcessOutcome.SUCCESS)


def _record_page_classification(classified, page_kinds, ordered_page_nums,
                                page_num, *, page_outcomes=None):
    """flush() の戻り値を頁級 outcome 記録へ反映する（main.py 内 2 箇所の重複統合、
    simcodex Round 1 #5/#6）。

    classified が None（緩衝が空、flush 未実施）なら何もせず None を返す
    （呼出側はループを継続してよい）。("ESCALATE", reason) なら reason を診断
    ログへ落とし（旧実装は reason を捨てるだけの write-only 穿線だった、#6）
    HeadlessOutcome(ESCALATED) を返す（呼出側は即座に return する）。それ以外
    (kind, None) は page_kinds/ordered_page_nums へ記録して None を返す。

    page_kinds/ordered_page_nums は呼出元のループ累積変数をそのまま可変更新
    する（_process_file_headless 内の既存の蓄積スタイルに合わせる）。
    """
    if classified is None:
        return None
    kind, reason = classified
    if kind == "ESCALATE":
        # ESCALATE は page_outcomes へ**書かない**（B7-2 Plan §9 #14 裁決）：
        # 行の欠落＝「未決」の正しい信号。FAILED と書くと人工核信号と矛盾する。
        print(f"🚨 頁 {page_num} ESCALATE: {reason}")
        return HeadlessOutcome(ProcessOutcome.ESCALATED)
    page_kinds[page_num] = kind
    ordered_page_nums.append(page_num)
    if page_outcomes is not None:
        # B7-2 T3: 頁決算の唯一の発射点（恰好一回）。kind→§5.6 outcome の
        # 映射・reason 白名単・degrade は reporter 側が担う（emit adapter）。
        page_outcomes.record_page(page_num, kind, reason)
    return None


def _process_file_headless(service, writer, file_path, uploader_name, base,
                           ledger, doc_type, drive_file_id, split_pdf_folder_id,
                           *, tab_owner, page_outcomes=None, event_sink=None):
    """HEADLESS_MODE の頁級緩衝集約経路（IP-304/IP-306）。HeadlessOutcome を返す。

    tab_owner は真必須（codex R1-P1 裁決＝fallback 復活ではなく fail-fast）：
    None のまま書込へ進むと「None」という名の tab へ静かに記帳される。
    生産呼出（_process_one_file）は解決済みの顧客キーを必ず渡し、テストは
    headless_rerun_fixture.run_headless が既定を供給する——ここに None が
    届くのは呼出し側のバグなので大声で落とす。

    process_pipeline の yield を page_num で連続緩衝し、頁境界で flush（形状分類→
    査重→原子書込）。緩衝は一頁分のみ（CLAUDE.md メモリ硬約束、頁 flush 後に解放）。
    占位頁（CONTENT/認識不能）は continue で読み飛ばさず、正常頁と同じ頁緩衝→
    頁原子書込の経路を通る（B4 Plan §2.2、旧・檔級聚合占位行塊は廃止済み）。
    page_num の再登場は ESCALATE（#6 連続性 contract、静默拆頁禁）。

    uploader_name：実投稿者（result["uploader"]、行内容の作成者/最終更新者列）。
    tab_owner：Sheets tab キー（§5.1-d T4、Codex #9 採納）。headless では
    顧客番号_顧客名（または縮退時は顧客番号単独）——uploader_name とは別系統
    （F5「行内容の投稿者列は別系統」）。tab_owner が顧客系でも result["uploader"]
    は実投稿者のまま変わらない。
    """
    if tab_owner is None:
        raise ValueError(
            "_process_file_headless: tab_owner が None（§5.1-d T4）——"
            "呼出し側で顧客キーを解決してから渡すこと（None のまま進むと"
            "「None」という名の tab へ静かに記帳される）")
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
            writer, ledger, resolver, base, doc_type, tab_owner, filename,
            page_num_, total_pages_, results_, page_bytes_)

    # event_sink＝契約 §5.7 provider 事件（headless のみ。UI 版は注入しない）。
    # job_id は §5.7 の任意字段で、job_key＝base と同一（1 ファイル＝1 job）。
    for page in process_pipeline(file_path, doc_type=doc_type,
                                 event_sink=event_sink, job_id=base):
        result = page["result"]
        page_num = page["page_num"]
        total_pages = page["total_pages"]
        file_total_pages = total_pages

        # 頁境界：page_num が変わったら前頁を flush してから当頁の緩衝を開始
        if buffered_num is not None and page_num != buffered_num:
            classified = flush(buffered_num, buffered_total_pages,
                               buffered_results, buffered_page_bytes)
            escalated = _record_page_classification(
                classified, page_kinds, ordered_page_nums, buffered_num,
                page_outcomes=page_outcomes)
            if escalated is not None:
                return escalated
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
    escalated = _record_page_classification(
        classified, page_kinds, ordered_page_nums, buffered_num,
        page_outcomes=page_outcomes)
    if escalated is not None:
        return escalated

    return _aggregate_file_outcome(page_kinds, ordered_page_nums, file_total_pages)


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
                  split_pdf_folder_id="", base=None, ledger=None,
                  progress=None, page_outcomes=None, tab_owner=None,
                  event_sink=None):
    """process_file の薄い外殻（B7 T3）。

    進捗フックの発火（file_started／未預期例外時の ABORTED best-effort 記録）
    だけを担い、実処理は _process_file_impl に委譲する。progress 未指定
    (None) なら NULL_REPORTER に落ちるため、既存の呼び出し側・テストは
    完全に従来どおり動く（無回帰）。base/ledger（headless 経路）も
    そのまま impl へ貫通する（merge B7-2：main 外殻×headless 分流の合成）。

    tab_owner：headless の Sheets tab キー（§5.1-d T4、Codex #9 採納）。
    UI 経路（ledger=None）は None のまま渡され一切参照されない（零改動）。
    headless 経路では必須——None のまま渡ると _process_file_headless が
    fail-fast で ValueError を送出する（simcodex R1・codex R1-P1 裁決。
    テストの既定供給は headless_rerun_fixture.run_headless）。
    """
    progress = progress or NULL_REPORTER
    filename = os.path.basename(file_path)
    progress.file_started(filename, uploader_name, doc_type)
    try:
        return _process_file_impl(
            service, sheets_writer, file_path, uploader_name, chat_id,
            doc_type=doc_type, drive_file_id=drive_file_id,
            split_pdf_folder_id=split_pdf_folder_id, base=base, ledger=ledger,
            progress=progress, page_outcomes=page_outcomes, tab_owner=tab_owner,
            event_sink=event_sink)
    except Exception as e:
        # best-effort: 進捗記録は reporter 自身が内部で自吞するが、二重の
        # 安全網として元例外を必ずそのまま伝播させる（main loop の既存の
        # 檔滯留・再試行語義は一切不変）。
        progress.file_finished(STATUS_ABORTED, error_class=type(e).__name__)
        raise


def _process_file_impl(service, sheets_writer, file_path, uploader_name,
                       chat_id, doc_type=DocType.RECEIPT, drive_file_id=None,
                       split_pdf_folder_id="", base=None, ledger=None,
                       page_outcomes=None, tab_owner=None, event_sink=None,
                       *, progress):
    """ファイルを処理し、Google Sheets に逐次書き込み、通知を送信する。

    ledger 非 None（HEADLESS_MODE）は頁級緩衝集約＋硬去重の経路へ分流し ProcessOutcome
    を返す。ledger None（UI 版）は従来の逐 result append_entries で bool を返す（挙動不変）。

    process_pipeline がジェネレータなので、1ページ処理→即Sheets書き込み→
    メモリ解放→次ページ の流れでメモリ使用量を最小化する。

    split_pdf_folder_id もプロファイルごとに異なる。共通の分割保存先へ
    アップロードすると、社長専用シートの原票 URL が全社から閲覧可能になる。

    tab_owner：§5.1-d T4。headless 呼出しでは必須——_process_one_file が intake
    の customer_id/customer_label から解決した値を渡す（テストの既定は
    headless_rerun_fixture.run_headless が夹具層で供給、simcodex R1）。
    UI 版（ledger None）は不使用。
    """
    if ledger is not None:
        return _process_file_headless(
            service, sheets_writer, file_path, uploader_name, base, ledger,
            doc_type, drive_file_id, split_pdf_folder_id,
            tab_owner=tab_owner,
            page_outcomes=page_outcomes,
            event_sink=event_sink)

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


def _customer_tab_namer(owner, doc_type):
    """headless の tab_namer 注入（§5.1-d T4-5）: doc_type 後缀を捨て owner を
    そのまま tab 名にする（1 社 1 tab、doc_type 跨ぎで集約）。owner は
    _process_one_file が job の customer_id/customer_label から解決した
    tab_owner（顧客番号_顧客名 または顧客番号単独、DoD⑦）。
    """
    return owner


def open_writer_with_retry(profile, credentials_file, delays=None):
    """SheetsOutputWriter を作る。一時エラーのみ指数バックオフで再試行する。

    SheetsOutputWriter の構築は冪等 (open_by_key も空シート削除も再実行安全) な
    ため、丸ごと再試行してよい。恒久エラーは再試行せず送出し、呼び出し側で
    そのプロファイルだけを縮退させる。

    tab_namer：headless（config.headless_mode()）時のみ _customer_tab_namer を
    注入し、tab を顧客キー単位へ切替える（§5.1-d T4）。UI 版は None のまま
    ＝ SheetsOutputWriter 既定の従業員 tab（_tab_name）——挙動零改動。
    """
    if delays is None:
        delays = _SHEET_OPEN_RETRY_DELAYS

    tab_namer = _customer_tab_namer if config.headless_mode() else None

    last_err = None
    for attempt, delay in enumerate([0] + list(delays)):
        if delay:
            print(f"   ⏳ {delay}s 後に再試行 ({attempt}/{len(delays)})")
            time.sleep(delay)
        try:
            return SheetsOutputWriter(
                spreadsheet_id=profile["spreadsheet_id"],
                credentials_file=credentials_file,
                tab_namer=tab_namer,
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

    # ヘッドレスモード（サンデヴィスタン統合、IP-303）: 入口守衛が使う
    # Firestore reporter を起動時に一度だけ構築する。UI 版
    # （HEADLESS_MODE 未設定）では reporter は None のまま、既存挙動に
    # 一切影響しない。
    job_reporter = _init_headless_reporter()

    # 拒絶件記憶層（IP-303、進程内快取）: alert 送達済みだが move が未完了の
    # file_id を憶えておき、次輪で check_intake/get_job/write_alert を
    # 再実行させない（Firestore 無界重打の防止）。プロセス再起動で自然に消える。
    quarantine_alerted: dict[str, str] = {}

    # headless memo（IP-308/T4、進程内快取、B4 Plan §2.4）: (base, lease_epoch,
    # file_id) → {outcome, folder_id, expire_cycle}。同 epoch 内で既に終態記録
    # 済みの file を次輪以降スキップし、Gemini 呼出しの無界重打を防ぐ（費用防護の
    # みで正確性は担わない——プロセス再起動で自然に消える）。
    headless_memo: dict = {}
    # intake 状態白名単 pre-gate memo（IP-308/T4、simcodex Round 2 #2）:
    # (base, file_id) → {outcome, folder_id, expire_cycle}。job_state が
    # POSTING_IN_PROGRESS でないと判定された file を TTL 内は intake gate
    # （Firestore get_job 読取）自体を省いてスキップする——鍵に epoch を含まない
    # （gate 呼出し前は epoch 未知のため）。outcome memo とは別 dict
    # （キー形が違うため剪定 file_id_index も別途指定）。
    intake_state_memo: dict = {}
    # customer_metadata_missing の alert 送達 memo（file_id → True、simcodex R1）:
    # 顧客メタ欠落 job の alert を毎輪 Firestore へ再書込しない。解決成功で
    # 該当キーを解除（再発時に再 alert）。プロセス再起動で自然に消える
    # （alert 自体は文書 ID 冪等なので再送は無害＝重複可視化にはならない）。
    customer_meta_alerted: dict = {}
    # provider 事件の書込口（契約 §5.7、B8）。**ループで 1 個**を持ち回す——
    # 配額（provider×error_class ごと滑動 10 分窓）が檔ごとに再配分されると、
    # provider 全面障害中に小さな檔が次々来た時に各檔が上限ぶん書き、総書込量の
    # 上界が消える（simcodex R4 の Codex 独立評審で判明）。断路器は provider 単位・
    # 10 分窓で裁くので、封じる側も同じ軸・同じ寿命に揃える。
    # UI 版（job_reporter=None）では作らない＝行為零改動。
    provider_sink = None
    if job_reporter is not None:
        from provider_events import ProviderEventWriter
        provider_sink = ProviderEventWriter(job_reporter.client)
    cycle = 0

    # B7 T4: プロファイルごとの進捗レポーター（Sheets `_処理進捗` タブ）。
    # 生成失敗は build_writers と同じ粒度で縮退——進捗タブが無くても記帳は続行。
    # 変数名は headless の Firestore 入口守衛 job_reporter と衝突させない
    # （progress_reporter ＝人向け進捗可視化／job_reporter ＝ Firestore 回報）。
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
            cycle += 1
            # この輪で全ファイルに同じ now を使う（ファイルごとに取り直さない
            # ——codex 対抗評審裁決・Plan §1）。
            now_ts = time.time()
            backed_off_total = 0
            earliest_next_attempt_ts = None

            for input_folder_id, (doc_type, profile_key) in active_folder_map.items():
                files = list_files(service, input_folder_id)

                # 剪枝按夾（IP-308/T4、B4 Plan §2.4 DoD⑦）: list_files 成功後にのみ
                # 呼ぶ（「夾列舉失敗不剪」は呼出順序で自然に満たす）。
                if job_reporter is not None:
                    _prune_headless_memo(headless_memo, input_folder_id, files)
                    _prune_headless_memo(intake_state_memo, input_folder_id, files,
                                         file_id_index=1)

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
                progress_reporter = progress_reporters.get(profile_key)
                processed_folder_id = profile["processed_folder_id"]

                found_any = True
                type_label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)
                print(f"\n\n🔎 [{profile['label']}/{type_label}] "
                      f"新しいファイルを検出しました！")

                # 回すのは ready_files（Plan C4・Codex P0）。HEAD 側は
                # `for file in files:` だったので字義どおり採ると、直前で
                # 算出した ready_files を無視して退避中ファイルを毎輪処理
                # する＝護欄が完全に無力化する。
                for file in ready_files:
                    # per-file の例外隔離は**ここ**に置く（Plan D4・Codex 採納）。
                    # _process_one_file の内側で download/start/process だけを
                    # 包む案だと、_report_headless_outcome（Firestore 書込）や
                    # move_file、intake gate の例外が main() の外殻 try まで
                    # 逃げ、同バッチの後続ファイルを巻き添えにして走査を
                    # 止める——1 個の壊れたファイルが行列全体を塞いではならない。
                    try:
                        action = _process_one_file(
                            service, writer, job_reporter, file, input_folder_id,
                            doc_type, processed_folder_id,
                            profile["split_pdf_folder_id"], quarantine_alerted,
                            headless_memo, intake_state_memo, cycle,
                            progress_reporter=progress_reporter,
                            customer_meta_alerted=customer_meta_alerted,
                            provider_sink=provider_sink)
                    except Exception as e:
                        print(f"❌ ファイル処理で例外発生: {file['name']}: {e}")
                        # 例外は UI/headless 共通で backoff の担当（Plan D4 #10）:
                        # outcome も memo も残らないので、ここを INCREMENT に
                        # しないと唯一の無限リトライ経路が塞がらない。
                        action = RetryAction.INCREMENT

                    _apply_retry_action(file_retry_state, file['id'],
                                        file['name'], action, time.time())

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
