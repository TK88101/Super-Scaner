"""main.py の Drive API ラッパ関数の単体テスト。

主目的: 共有ドライブ(Shared Drive)対応のため、各 Drive API 呼び出しが
正しいフラグ(supportsAllDrives / includeItemsFromAllDrives)を渡している
ことを保証する。これが欠けると共有ドライブ上のファイルが list で「0件」に
なり、エラーも出ず黙って処理されない（本番の静かな停止）ため重要。

main.py は ocr_engine(PaddleOCR)/sheets_output(gspread) を import するため
venv311 で実行する:
    venv311/bin/python -m unittest test_drive_functions -v
    venv311/bin/python -m pytest test_drive_functions.py -v
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

# main.py はモジュール読込時に必須環境変数が無いと exit(1) する。
# 実 .env が無い環境(CI 等)でも import できるよう、未設定のものだけ補完する。
# （python-dotenv の load_dotenv は既存 os.environ を上書きしないので、
#   実 .env がある場合はそちらの値が優先される。）
DEFAULT_PROCESSED_FOLDER = "test_processed_folder"
os.environ.setdefault("PROCESSED_FOLDER_ID", DEFAULT_PROCESSED_FOLDER)
os.environ.setdefault("SERVICE_ACCOUNT_FILE", "test_sa.json")
os.environ.setdefault("OUTPUT_SPREADSHEET_ID", "test_spreadsheet")
os.environ.setdefault("FOLDER_RECEIPT_ID", "test_receipt_folder")

import importlib

import config
importlib.reload(config)  # 先に env 無しで import 済みでも値を反映させる
import main


def _make_service_with_list(files):
    """service.files().list().execute() が {'files': files} を返す mock。"""
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": files
    }
    return service


class ListFilesSharedDriveTest(unittest.TestCase):
    """list_files が共有ドライブ対応フラグを渡すこと + 返り値の検証。"""

    def test_passes_shared_drive_flags(self):
        # Arrange: list が空結果を返す mock service
        service = _make_service_with_list([])
        # Act
        main.list_files(service, "FOLDER_X")
        # Assert: 両フラグが True で渡る（片方だけだと共有ドライブで0件になる）
        kwargs = service.files.return_value.list.call_args.kwargs
        self.assertIs(kwargs.get("supportsAllDrives"), True)
        self.assertIs(kwargs.get("includeItemsFromAllDrives"), True)
        # クエリに対象フォルダと trashed=false が含まれる
        self.assertIn("FOLDER_X", kwargs.get("q", ""))
        self.assertIn("trashed = false", kwargs.get("q", ""))

    def test_returns_files_from_response(self):
        # Arrange
        files = [{"id": "1", "name": "a.pdf"}, {"id": "2", "name": "b.pdf"}]
        service = _make_service_with_list(files)
        # Act / Assert
        self.assertEqual(main.list_files(service, "F"), files)

    def test_returns_empty_list_when_no_files(self):
        # Arrange: 'files' キー欠如のレスポンス
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {}
        # Act / Assert
        self.assertEqual(main.list_files(service, "F"), [])


class ListFilesFieldsTest(unittest.TestCase):
    """main.list_files が properties を含む fields で Drive API を呼ぶこと。

    交棒契約の base posting_id は Drive 公開 properties に載る（v0.10 定名）。
    list の fields にこれを含めないと properties が一切返らず、入口守衛
    （check_intake/resolve_posting_id）が機能しない
    （Drive API は明示指定したフィールドしか返さない仕様のため）。
    """

    def test_fields_include_properties(self):
        service = _make_service_with_list([])

        main.list_files(service, "FOLDER_X")

        kwargs = service.files.return_value.list.call_args.kwargs
        self.assertIn("properties", kwargs.get("fields", ""))


class DownloadFileSharedDriveTest(unittest.TestCase):
    """download_file が get_media に supportsAllDrives を渡すこと。"""

    def test_passes_supports_all_drives(self):
        # Arrange: ディスク I/O とチャンク DL ループを mock
        with patch("main.io.FileIO"), \
                patch("main.MediaIoBaseDownload") as m_dl, \
                patch("main.os.makedirs"):
            m_dl.return_value.next_chunk.return_value = (None, True)
            service = MagicMock()
            # Act
            path = main.download_file(service, "FILE_X", "receipt.pdf")
            # Assert: get_media が fileId + supportsAllDrives で呼ばれる
            service.files.return_value.get_media.assert_called_once_with(
                fileId="FILE_X", supportsAllDrives=True)
            # 返り値はローカル保存パス（ファイル名を含む）
            self.assertIn("receipt.pdf", path)


class MoveFileSharedDriveTest(unittest.TestCase):
    """move_file が update に supportsAllDrives + add/removeParents を渡すこと。"""

    def test_passes_shared_drive_and_parents(self):
        # Arrange
        service = MagicMock()
        # Act
        main.move_file(service, "F1", "OLD_PARENT", "NEW_PARENT")
        # Assert
        kwargs = service.files.return_value.update.call_args.kwargs
        self.assertEqual(kwargs.get("fileId"), "F1")
        self.assertEqual(kwargs.get("addParents"), "NEW_PARENT")
        self.assertEqual(kwargs.get("removeParents"), "OLD_PARENT")
        self.assertIs(kwargs.get("supportsAllDrives"), True)

    def test_swallows_exception_without_raising(self):
        # Arrange: update().execute() が例外を投げる
        service = MagicMock()
        service.files.return_value.update.return_value.execute.side_effect = \
            Exception("boom")
        # Act / Assert: 例外を捕捉し再送出しない（警告を print するのみ）
        with redirect_stdout(io.StringIO()):
            try:
                main.move_file(service, "F1", "OLD", "NEW")
            except Exception:  # noqa: BLE001 - テスト失敗を明示
                self.fail("move_file が例外を再送出した")


class IsDuplicateFileSharedDriveTest(unittest.TestCase):
    """is_duplicate_file が共有ドライブ対応フラグを渡すこと + 重複判定。"""

    def test_passes_shared_drive_flags_and_queries_processed_folder(self):
        # Arrange
        service = _make_service_with_list([{"md5Checksum": "abc"}])
        # Act
        main.is_duplicate_file(service, "abc", "PROC_X")
        # Assert
        kwargs = service.files.return_value.list.call_args.kwargs
        self.assertIs(kwargs.get("supportsAllDrives"), True)
        self.assertIs(kwargs.get("includeItemsFromAllDrives"), True)
        self.assertEqual(kwargs.get("pageSize"), 200)
        # 渡された Processed フォルダ ID をクエリ対象にしている
        self.assertIn("PROC_X", kwargs.get("q", ""))

    def test_queries_the_given_folder_not_the_global_constant(self):
        """プロファイルごとに Processed が異なる。

        グローバル定数を見に行くと、社長専用フォルダへアーカイブ済みの票据が
        共通 Processed に無いため「重複でない」と誤判定され、二重記帳になる。
        """
        # Arrange
        service = _make_service_with_list([])
        # Act: 社長専用プロファイルの Processed を渡す
        main.is_duplicate_file(service, "abc", "IDO_PROCESSED_FOLDER")
        # Assert: 渡した方だけを見る。共通(default)の Processed は参照しない。
        query = service.files.return_value.list.call_args.kwargs.get("q", "")
        self.assertIn("IDO_PROCESSED_FOLDER", query)
        self.assertNotIn(DEFAULT_PROCESSED_FOLDER, query)

    def test_returns_true_on_md5_match(self):
        # Arrange
        service = _make_service_with_list(
            [{"md5Checksum": "xxx"}, {"md5Checksum": "target"}])
        # Act / Assert
        self.assertTrue(main.is_duplicate_file(service, "target", "PROC_X"))

    def test_returns_false_on_no_match(self):
        # Arrange
        service = _make_service_with_list(
            [{"md5Checksum": "xxx"}, {"md5Checksum": "yyy"}])
        # Act / Assert
        self.assertFalse(main.is_duplicate_file(service, "target", "PROC_X"))

    def test_returns_false_for_empty_checksum_without_api_call(self):
        # Arrange
        service = MagicMock()
        # Act / Assert: md5 が None なら即 False、API は呼ばない
        self.assertFalse(main.is_duplicate_file(service, None, "PROC_X"))
        service.files.return_value.list.assert_not_called()

    def test_returns_false_for_empty_folder_without_api_call(self):
        # Arrange: アーカイブ先が無いプロファイルは load_profiles が弾くが、
        # 万一到達しても全件を「重複」と誤判定して無言スキップしないこと。
        service = MagicMock()
        # Act / Assert
        self.assertFalse(main.is_duplicate_file(service, "abc", ""))
        service.files.return_value.list.assert_not_called()

    def test_swallows_exception_and_returns_false(self):
        # Arrange: list().execute() が例外
        service = MagicMock()
        service.files.return_value.list.return_value.execute.side_effect = \
            Exception("boom")
        # Act / Assert
        with redirect_stdout(io.StringIO()):
            self.assertFalse(main.is_duplicate_file(service, "abc", "PROC_X"))


def _transient_error(status=503):
    """gspread.APIError 相当（response.status_code を持つ）の一時エラー。"""
    err = Exception(f"HTTP {status}")
    err.response = MagicMock(status_code=status)
    return err


class TransientSheetErrorTest(unittest.TestCase):
    """再試行すべきエラーと、諦めるべきエラーの切り分け。

    恒久エラー(共有剥奪=404/403)まで再試行すると起動が 15 秒無駄に伸びる。
    逆に一時エラー(429/5xx)を再試行しないと、02:00 の定時再起動が Google の
    一瞬の不調に当たっただけで、そのプロファイルが次の再起動まで最長 24 時間
    沈黙する。
    """

    def test_5xx_is_transient(self):
        for status in (500, 502, 503, 599):
            self.assertTrue(main._is_transient_sheet_error(_transient_error(status)))

    def test_429_rate_limit_is_transient(self):
        self.assertTrue(main._is_transient_sheet_error(_transient_error(429)))

    def test_403_permission_denied_is_permanent(self):
        self.assertFalse(main._is_transient_sheet_error(_transient_error(403)))

    def test_404_not_found_is_permanent(self):
        self.assertFalse(main._is_transient_sheet_error(_transient_error(404)))

    def test_spreadsheet_not_found_is_permanent(self):
        import gspread.exceptions
        err = gspread.exceptions.SpreadsheetNotFound("gone")
        self.assertFalse(main._is_transient_sheet_error(err))

    def test_network_errors_are_transient(self):
        for err in (ConnectionError(), TimeoutError(), OSError("unreachable")):
            self.assertTrue(main._is_transient_sheet_error(err))

    def test_plain_exception_is_permanent(self):
        self.assertFalse(main._is_transient_sheet_error(Exception("boom")))


class OpenWriterWithRetryTest(unittest.TestCase):
    """open_by_key はリポジトリ内で唯一リトライ保護が無かった Google 呼び出し。"""

    PROFILE = {"label": "井戸会計事務所", "spreadsheet_id": "ss_ido",
               "processed_folder_id": "p_i", "split_pdf_folder_id": ""}

    def test_transient_error_is_retried_until_success(self):
        # Arrange: 最初の2回は 503、3回目で成功
        attempts = []

        def factory(spreadsheet_id, credentials_file):
            attempts.append(spreadsheet_id)
            if len(attempts) < 3:
                raise _transient_error(503)
            return MagicMock()

        # Act
        with patch("main.SheetsOutputWriter", side_effect=factory), \
                patch("main.time.sleep") as mock_sleep, \
                redirect_stdout(io.StringIO()):
            writer = main.open_writer_with_retry(self.PROFILE, "sa.json")

        # Assert: 3 回試行して成功、間に 2 回スリープ
        self.assertIsNotNone(writer)
        self.assertEqual(3, len(attempts))
        self.assertEqual(2, mock_sleep.call_count)

    def test_permanent_error_is_not_retried(self):
        # Arrange: 共有剥奪 = 恒久エラー
        attempts = []

        def factory(spreadsheet_id, credentials_file):
            attempts.append(spreadsheet_id)
            raise _transient_error(404)

        # Act / Assert: 1 回で諦めて送出（15 秒待たない）
        with patch("main.SheetsOutputWriter", side_effect=factory), \
                patch("main.time.sleep") as mock_sleep, \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(Exception):
                main.open_writer_with_retry(self.PROFILE, "sa.json")

        self.assertEqual(1, len(attempts))
        mock_sleep.assert_not_called()

    def test_raises_after_retries_are_exhausted(self):
        # Arrange: ずっと 503
        with patch("main.SheetsOutputWriter", side_effect=lambda **kw: (_ for _ in ()).throw(_transient_error(503))), \
                patch("main.time.sleep"), \
                redirect_stdout(io.StringIO()):
            # Act / Assert
            with self.assertRaises(Exception) as ctx:
                main.open_writer_with_retry(self.PROFILE, "sa.json", delays=[1, 1])

        self.assertIn("503", str(ctx.exception))

    def test_no_delays_means_single_attempt(self):
        attempts = []

        def factory(spreadsheet_id, credentials_file):
            attempts.append(1)
            raise _transient_error(503)

        with patch("main.SheetsOutputWriter", side_effect=factory), \
                patch("main.time.sleep"), redirect_stdout(io.StringIO()):
            with self.assertRaises(Exception):
                main.open_writer_with_retry(self.PROFILE, "sa.json", delays=[])

        self.assertEqual(1, len(attempts))


class BuildWritersTest(unittest.TestCase):
    """1つのプロファイルのシートが開けなくても、残りは動き続けること。

    load_profiles() は env 欠落を黙って降格させ「本番の .env 更新漏れで
    全社の記帳が止まる」のを防いでいる。しかし writer 生成は
    gc.open_by_key() という実ネットワーク呼び出しを伴うため、受限フォルダの
    共有設定を誤って外すと ido/rental が開けず、例外が main() を貫いて
    健全な default まで道連れになる。タスクスケジューラが 1 分後に再起動 →
    起動クラッシュループ。Chatwork 通知は廃止済みで監視手段が無く、
    この停止は「今日は誰も票据を上げなかった」と見分けがつかない。
    """

    PROFILES = {
        "default": {"label": "共通", "spreadsheet_id": "ss_default",
                    "processed_folder_id": "p_d", "split_pdf_folder_id": ""},
        "ido": {"label": "井戸会計事務所", "spreadsheet_id": "ss_ido",
                "processed_folder_id": "p_i", "split_pdf_folder_id": ""},
    }

    def test_builds_one_writer_per_profile(self):
        # Arrange / Act
        with patch("main.SheetsOutputWriter") as mock_writer, \
                redirect_stdout(io.StringIO()):
            writers = main.build_writers(self.PROFILES, "sa.json")
        # Assert
        self.assertEqual({"default", "ido"}, set(writers))
        self.assertEqual(mock_writer.call_count, 2)

    def test_one_unopenable_sheet_does_not_kill_the_others(self):
        # Arrange: ido のシートだけ開けない（共有剥奪 / 削除を模す）
        def factory(spreadsheet_id, credentials_file):
            if spreadsheet_id == "ss_ido":
                raise Exception("SpreadsheetNotFound")
            return MagicMock()
        # Act
        with patch("main.SheetsOutputWriter", side_effect=factory), \
                redirect_stdout(io.StringIO()):
            writers = main.build_writers(self.PROFILES, "sa.json")
        # Assert: default は生き残る
        self.assertEqual({"default"}, set(writers))

    def test_returns_empty_when_every_sheet_fails(self):
        # Arrange / Act
        with patch("main.SheetsOutputWriter", side_effect=Exception("boom")), \
                redirect_stdout(io.StringIO()):
            writers = main.build_writers(self.PROFILES, "sa.json")
        # Assert: 呼び出し側が exit(1) を判断できるよう空 dict を返す
        self.assertEqual({}, writers)

    def test_failure_is_reported_not_swallowed_silently(self):
        # Arrange
        buf = io.StringIO()
        # Act
        with patch("main.SheetsOutputWriter", side_effect=Exception("boom")), \
                redirect_stdout(buf):
            main.build_writers(self.PROFILES, "sa.json")
        # Assert: ラベルと原因が標準出力に出る（無言の縮退は禁止）
        out = buf.getvalue()
        self.assertIn("共通", out)
        self.assertIn("井戸会計事務所", out)
        self.assertIn("boom", out)


class BuildProgressReportersTest(unittest.TestCase):
    """B7 T4: writer ごとに SheetsProgressReporter を作る接線ヘルパー。

    生成に失敗したプロファイルは None を割り当てて続行する
    （進捗タブが無くても記帳自体は止めない。build_writers の縮退方針と同型）。
    """

    def test_builds_one_reporter_per_writer(self):
        # Arrange
        writers = {"default": MagicMock(spreadsheet=MagicMock()),
                  "ido": MagicMock(spreadsheet=MagicMock())}
        # Act
        with patch("main.SheetsProgressReporter") as mock_reporter, \
                redirect_stdout(io.StringIO()):
            reporters = main.build_progress_reporters(writers)
        # Assert
        self.assertEqual({"default", "ido"}, set(reporters))
        self.assertEqual(mock_reporter.call_count, 2)

    def test_construction_failure_falls_back_to_none_and_continues(self):
        # Arrange: writer に .spreadsheet が無い（構築時に AttributeError）
        class _NoSpreadsheetWriter:
            pass

        writers = {"default": MagicMock(spreadsheet=MagicMock()),
                  "broken": _NoSpreadsheetWriter()}
        # Act
        with redirect_stdout(io.StringIO()):
            reporters = main.build_progress_reporters(writers)
        # Assert: 壊れたプロファイルは欠落（build_writers と同じ欠落方式、
        # 消費側 .get が None を返す）、残りは生きる
        self.assertNotIn("broken", reporters)
        self.assertIsNotNone(reporters["default"])

    def test_failure_is_reported_not_swallowed_silently(self):
        # Arrange
        class _NoSpreadsheetWriter:
            pass

        buf = io.StringIO()
        # Act
        with redirect_stdout(buf):
            main.build_progress_reporters({"broken": _NoSpreadsheetWriter()})
        # Assert: 無言の縮退は禁止
        self.assertIn("broken", buf.getvalue())

    def test_returns_empty_dict_for_no_writers(self):
        self.assertEqual({}, main.build_progress_reporters({}))


class FilterActiveFoldersTest(unittest.TestCase):
    """writer が作れなかったプロファイルの入力フォルダは監視対象から外す。

    外し忘れると主ループが writers[profile_key] で KeyError を出す。
    """

    FOLDER_MAP = {
        "f_default": ("receipt", "default"),
        "f_ido": ("receipt", "ido"),
        "f_rental": ("receipt", "rental"),
    }

    def test_drops_folders_whose_profile_has_no_writer(self):
        # Arrange / Act
        active = main.filter_active_folders(self.FOLDER_MAP, {"default": object()})
        # Assert
        self.assertEqual({"f_default": ("receipt", "default")}, active)

    def test_keeps_everything_when_all_profiles_have_writers(self):
        # Arrange
        writers = {"default": object(), "ido": object(), "rental": object()}
        # Act / Assert
        self.assertEqual(self.FOLDER_MAP,
                         main.filter_active_folders(self.FOLDER_MAP, writers))

    def test_returns_empty_when_no_writers(self):
        self.assertEqual({}, main.filter_active_folders(self.FOLDER_MAP, {}))


class UploadFileTest(unittest.TestCase):
    """upload_file が単ページPDFを共有ドライブ対応で作成し id を返すこと。"""

    def test_creates_file_with_parents_and_shared_drive_flag(self):
        # Arrange: create().execute() が新ファイル id を返す mock
        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "NEW_FILE_ID"
        }
        # Act: MediaIoBaseUpload はバイト I/O をラップするだけなので patch
        with patch("main.MediaIoBaseUpload") as m_up, \
                redirect_stdout(io.StringIO()):
            file_id = main.upload_file(
                service, "FOLDER_X", "doc_p2.pdf", b"%PDF-1.4 fake")
        # Assert: body(name + parents) と supportsAllDrives + fields="id"
        kwargs = service.files.return_value.create.call_args.kwargs
        self.assertEqual(kwargs.get("body"), {
            "name": "doc_p2.pdf",
            "parents": ["FOLDER_X"],
        })
        self.assertIs(kwargs.get("supportsAllDrives"), True)
        self.assertEqual(kwargs.get("fields"), "id")
        # media_body は MediaIoBaseUpload の戻り値
        self.assertIs(kwargs.get("media_body"), m_up.return_value)
        # 返り値は create が返した id
        self.assertEqual(file_id, "NEW_FILE_ID")


class PageUrlResolverTest(unittest.TestCase):
    """PageUrlResolver: ページ単位ディープリンク + メモ化 + 安全フォールバック。"""

    BASE_URL = "https://drive.google.com/file/d/ORIGINAL_ID/view"

    def _make_resolver(self, service, folder_id, source_file_id="SRC123"):
        # 既存ページ照会(list)はデフォルトで空(=新規アップロード経路)。
        # 既存再利用をテストする場合は呼び出し後に list の戻り値を上書きする。
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        return main.PageUrlResolver(
            service, self.BASE_URL, "scan_doc.pdf", folder_id, source_file_id)

    def test_single_page_returns_base_url_without_upload(self):
        # Arrange
        service = MagicMock()
        resolver = self._make_resolver(service, "FOLDER_X")
        # Act
        url = resolver.resolve(page_num=1, total_pages=1, page_bytes=b"x")
        # Assert: 単ページはアップロードせず base_url をそのまま返す
        self.assertEqual(url, self.BASE_URL)
        service.files.return_value.create.assert_not_called()

    def test_multi_page_with_folder_uploads_and_returns_view_url(self):
        # Arrange
        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "PAGE_FILE_ID"
        }
        resolver = self._make_resolver(service, "FOLDER_X")
        # Act
        with redirect_stdout(io.StringIO()):
            url = resolver.resolve(
                page_num=3, total_pages=5, page_bytes=b"%PDF page3")
        # Assert: 単ページファイルの /view URL を返し、アップロードは1回
        self.assertEqual(
            url, "https://drive.google.com/file/d/PAGE_FILE_ID/view")
        service.files.return_value.create.assert_called_once()
        # アップロード名は "{stem}_p{page}.pdf"
        kwargs = service.files.return_value.create.call_args.kwargs
        self.assertEqual(
            kwargs.get("body", {}).get("name"), "scan_doc__SRC123_p3.pdf")

    def test_same_page_uploaded_only_once_dedup(self):
        # Arrange: 同一ページ(一頁多票)は1回だけアップロードし URL を共有
        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "PAGE_FILE_ID"
        }
        resolver = self._make_resolver(service, "FOLDER_X")
        # Act
        with redirect_stdout(io.StringIO()):
            url1 = resolver.resolve(2, 4, b"%PDF page2")
            url2 = resolver.resolve(2, 4, b"%PDF page2")
        # Assert: アップロードは1回のみ、両 URL は同一
        service.files.return_value.create.assert_called_once()
        self.assertEqual(url1, url2)
        self.assertEqual(
            url1, "https://drive.google.com/file/d/PAGE_FILE_ID/view")

    def test_no_folder_falls_back_to_anchor_without_upload(self):
        # Arrange: folder 未設定("")は今日の挙動(#page=N)に劣化
        service = MagicMock()
        resolver = self._make_resolver(service, "")
        # Act
        with redirect_stdout(io.StringIO()):
            url = resolver.resolve(3, 5, b"%PDF page3")
        # Assert
        self.assertEqual(url, f"{self.BASE_URL}#page=3")
        service.files.return_value.create.assert_not_called()

    def test_upload_failure_falls_back_to_anchor_and_warns(self):
        # Arrange: create().execute() が例外を投げる
        service = MagicMock()
        service.files.return_value.create.return_value.execute.side_effect = \
            Exception("upload boom")
        resolver = self._make_resolver(service, "FOLDER_X")
        # Act / Assert: 例外を握りつぶし #page= フォールバック、警告 print
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                url = resolver.resolve(4, 6, b"%PDF page4")
            except Exception:  # noqa: BLE001 - テスト失敗を明示
                self.fail("resolve がアップロード例外を再送出した")
        self.assertEqual(url, f"{self.BASE_URL}#page=4")
        self.assertNotEqual(buf.getvalue().strip(), "")

    def test_no_page_bytes_falls_back_to_anchor(self):
        # Arrange: page_bytes が無い → アップロードできずフォールバック
        service = MagicMock()
        resolver = self._make_resolver(service, "FOLDER_X")
        # Act
        with redirect_stdout(io.StringIO()):
            url = resolver.resolve(2, 3, page_bytes=None)
        # Assert
        self.assertEqual(url, f"{self.BASE_URL}#page=2")
        service.files.return_value.create.assert_not_called()

    def test_filename_embeds_source_file_id(self):
        # Arrange: アップロード名に源 file id を埋め込み衝突不能にする
        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "PAGE_FILE_ID"
        }
        resolver = self._make_resolver(service, "FOLDER_X", "SRCABC")
        # Act
        with redirect_stdout(io.StringIO()):
            resolver.resolve(2, 4, b"%PDF page2")
        # Assert: "{stem}__{source_id}_p{page}.pdf"
        kwargs = service.files.return_value.create.call_args.kwargs
        self.assertEqual(
            kwargs.get("body", {}).get("name"), "scan_doc__SRCABC_p2.pdf")

    def test_reuses_existing_page_without_upload(self):
        # Arrange: 再実行時、分割保存先に既に同ページが存在 → 再利用(新規無し)
        service = MagicMock()
        resolver = self._make_resolver(service, "FOLDER_X")
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {"id": "EXIST_P3", "name": "scan_doc__SRC123_p3.pdf"},
            ]
        }
        # Act
        with redirect_stdout(io.StringIO()):
            url = resolver.resolve(3, 5, b"%PDF page3")
        # Assert: 既存 id の /view を返し、新規アップロードはしない(冪等)
        self.assertEqual(
            url, "https://drive.google.com/file/d/EXIST_P3/view")
        service.files.return_value.create.assert_not_called()

    def test_uploads_only_missing_pages(self):
        # Arrange: p1 は既存、p2 は未存在
        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "NEW_P2"
        }
        resolver = self._make_resolver(service, "FOLDER_X")
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {"id": "EXIST_P1", "name": "scan_doc__SRC123_p1.pdf"},
            ]
        }
        # Act
        with redirect_stdout(io.StringIO()):
            url1 = resolver.resolve(1, 3, b"%PDF page1")
            url2 = resolver.resolve(2, 3, b"%PDF page2")
        # Assert: p1 は再利用(新規無し)、p2 のみ新規アップロード1回
        self.assertEqual(
            url1, "https://drive.google.com/file/d/EXIST_P1/view")
        self.assertEqual(
            url2, "https://drive.google.com/file/d/NEW_P2/view")
        service.files.return_value.create.assert_called_once()

    def test_existing_lookup_done_once_per_file(self):
        # Arrange: 複数ページを解決しても照会(list)はファイル単位で1回のみ
        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "NEW_PAGE"
        }
        resolver = self._make_resolver(service, "FOLDER_X")
        # Act
        with redirect_stdout(io.StringIO()):
            resolver.resolve(1, 3, b"%PDF p1")
            resolver.resolve(2, 3, b"%PDF p2")
        # Assert: list は per-page ではなく per-file で1回だけ
        service.files.return_value.list.assert_called_once()

    def test_existing_lookup_failure_falls_back_to_upload(self):
        # Arrange: 既存照会(list)が例外 → 空とみなし新規アップロードで継続
        service = MagicMock()
        service.files.return_value.list.return_value.execute.side_effect = \
            Exception("list boom")
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "NEW_AFTER_FAIL"
        }
        resolver = main.PageUrlResolver(
            service, self.BASE_URL, "scan_doc.pdf", "FOLDER_X", "SRC123")
        # Act / Assert: 照会失敗でも握りつぶしてアップロードし /view を返す
        with redirect_stdout(io.StringIO()):
            url = resolver.resolve(2, 4, b"%PDF p2")
        self.assertEqual(
            url, "https://drive.google.com/file/d/NEW_AFTER_FAIL/view")
        service.files.return_value.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
