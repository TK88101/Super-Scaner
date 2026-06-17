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
os.environ.setdefault("PROCESSED_FOLDER_ID", "test_processed_folder")
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
        main.is_duplicate_file(service, "abc")
        # Assert
        kwargs = service.files.return_value.list.call_args.kwargs
        self.assertIs(kwargs.get("supportsAllDrives"), True)
        self.assertIs(kwargs.get("includeItemsFromAllDrives"), True)
        self.assertEqual(kwargs.get("pageSize"), 200)
        # Processed フォルダ ID をクエリ対象にしている
        self.assertIn(main.PROCESSED_FOLDER_ID, kwargs.get("q", ""))

    def test_returns_true_on_md5_match(self):
        # Arrange
        service = _make_service_with_list(
            [{"md5Checksum": "xxx"}, {"md5Checksum": "target"}])
        # Act / Assert
        self.assertTrue(main.is_duplicate_file(service, "target"))

    def test_returns_false_on_no_match(self):
        # Arrange
        service = _make_service_with_list(
            [{"md5Checksum": "xxx"}, {"md5Checksum": "yyy"}])
        # Act / Assert
        self.assertFalse(main.is_duplicate_file(service, "target"))

    def test_returns_false_for_empty_checksum_without_api_call(self):
        # Arrange
        service = MagicMock()
        # Act / Assert: md5 が None なら即 False、API は呼ばない
        self.assertFalse(main.is_duplicate_file(service, None))
        service.files.return_value.list.assert_not_called()

    def test_swallows_exception_and_returns_false(self):
        # Arrange: list().execute() が例外
        service = MagicMock()
        service.files.return_value.list.return_value.execute.side_effect = \
            Exception("boom")
        # Act / Assert
        with redirect_stdout(io.StringIO()):
            self.assertFalse(main.is_duplicate_file(service, "abc"))


if __name__ == "__main__":
    unittest.main()
