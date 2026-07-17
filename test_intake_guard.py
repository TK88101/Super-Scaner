"""intake_guard.py の単体テスト（IP-302: base posting_id 受信）。

サンデヴィスタン契約 job-state-machine.md v0.12 §3.2/§6 対齊：
    - base posting_id の載体＝Drive 公開 properties、key ＝ POSTING_ID_PROPERTY_KEY
      （契約 v0.10 定名の跨倉庫契約値。改名即破壊交棒）。

跑法:
    venv311/bin/python -m unittest test_intake_guard -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from intake_guard import POSTING_ID_PROPERTY_KEY, resolve_posting_id


class ResolvePostingIdTest(unittest.TestCase):
    """IP-302: Drive 公開 properties から base posting_id を読取る純関数。"""

    def test_returns_value_when_property_present(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "base-123"}}
        self.assertEqual("base-123", resolve_posting_id(file))

    def test_returns_none_when_properties_key_entirely_missing(self):
        file = {"id": "f1"}
        self.assertIsNone(resolve_posting_id(file))

    def test_returns_none_when_properties_present_but_key_absent(self):
        file = {"id": "f1", "properties": {"other_key": "x"}}
        self.assertIsNone(resolve_posting_id(file))

    def test_returns_none_for_empty_string(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: ""}}
        self.assertIsNone(resolve_posting_id(file))

    def test_returns_none_for_whitespace_only_string(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "   "}}
        self.assertIsNone(resolve_posting_id(file))

    def test_strips_surrounding_whitespace(self):
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: "  base-9  "}}
        self.assertEqual("base-9", resolve_posting_id(file))

    def test_returns_none_for_non_str_value(self):
        # 防御: properties は本来 str→str の Drive API 仕様だが、fake/破損データ対策
        file = {"id": "f1", "properties": {POSTING_ID_PROPERTY_KEY: 12345}}
        self.assertIsNone(resolve_posting_id(file))

    def test_returns_none_when_properties_is_none(self):
        # 防御: Drive API が properties=None を返すケース（未設定文件で稀に発生）
        file = {"id": "f1", "properties": None}
        self.assertIsNone(resolve_posting_id(file))


if __name__ == "__main__":
    unittest.main()
