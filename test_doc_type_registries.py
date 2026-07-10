"""ocr_engine の DocType 登録表漏れ防止（import 時防呆）の単体テスト。

新文書タイプを追加したとき PROMPTS / ENTRY_BUILDERS / DOC_TYPE_CONFIG /
DOC_TYPE_TAB_SUFFIX のどれかへ登録し忘れると、起動時エラーにはならず、
運用中に「無限再試行で Gemini を焼く」「領収書タブへ誤書き込み」等の
静かな事故としてしか顕在化しない。_validate_doc_type_registries が
import 時に DocType.ALL を真値来源として全表を検査する。

ocr_engine は paddleocr / google.generativeai 等の重依存を import するため
venv311 で実行する:
    venv311/bin/python -m unittest test_doc_type_registries -v
    venv311/bin/python -m pytest test_doc_type_registries.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from ocr_engine import _validate_doc_type_registries


class DocTypeRegistryTest(unittest.TestCase):
    """DocType.ALL の全タイプが各登録表に登録されていること。"""

    def test_current_registries_are_complete(self):
        # Arrange / Act / Assert: 現行登録では例外を出さない
        # （import 成功自体が import 時検査の通過証明、ここは明示的に固定）
        _validate_doc_type_registries()

    def test_raises_listing_registry_and_doc_type_on_missing(self):
        # Arrange: 登録が欠けた仮の登録表
        # Act / Assert: RuntimeError で「どの表」「どの DocType」を報告する
        with self.assertRaises(RuntimeError) as ctx:
            _validate_doc_type_registries(
                doc_types=["fake_doc_type"],
                registries={"FAKE_TABLE": {}})
        msg = str(ctx.exception)
        self.assertIn("fake_doc_type", msg)
        self.assertIn("FAKE_TABLE", msg)


if __name__ == "__main__":
    unittest.main()
