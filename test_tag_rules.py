"""tag_rules.derive_row_tags の単体テスト（純関数・gspread非依存）。

    python -m pytest test_tag_rules.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from tag_rules import derive_row_tags, severity_rank, rank_to_tag


def _flag(severity):
    return {"type": "x", "message": "m", "severity": severity}


class SeverityHelpersTest(unittest.TestCase):
    def test_severity_rank_orders_high_above_medium_above_low(self):
        # Arrange / Act / Assert
        self.assertGreater(severity_rank("high"), severity_rank("medium"))
        self.assertGreater(severity_rank("medium"), severity_rank("low"))

    def test_unknown_severity_falls_back_to_low(self):
        self.assertEqual(severity_rank("???"), severity_rank("low"))

    def test_rank_to_tag_maps_japanese_labels(self):
        self.assertEqual(rank_to_tag(1), "黄系")
        self.assertEqual(rank_to_tag(2), "橙系")
        self.assertEqual(rank_to_tag(3), "赤系")

    def test_rank_zero_is_empty(self):
        self.assertEqual(rank_to_tag(0), "")


class DeriveRowTagsTest(unittest.TestCase):
    def test_no_flags_no_doc_signal_yields_all_empty(self):
        # Arrange / Act
        tags = derive_row_tags(3, [])
        # Assert
        self.assertEqual(tags, ["", "", ""])

    def test_per_entry_low_yields_yellow_only_for_that_row(self):
        tags = derive_row_tags(3, [(1, [_flag("low")])])
        self.assertEqual(tags, ["", "黄系", ""])

    def test_per_entry_medium_yields_orange(self):
        tags = derive_row_tags(2, [(0, [_flag("medium")])])
        self.assertEqual(tags, ["橙系", ""])

    def test_per_entry_high_yields_red(self):
        tags = derive_row_tags(2, [(0, [_flag("high")])])
        self.assertEqual(tags, ["赤系", ""])

    def test_multiple_flags_on_row_take_highest_severity(self):
        # Arrange: 同行に low+medium+high → 最高(赤)を採る
        tags = derive_row_tags(
            1, [(0, [_flag("low"), _flag("high"), _flag("medium")])])
        # Assert
        self.assertEqual(tags, ["赤系"])

    def test_doc_low_confidence_tags_all_rows_yellow(self):
        tags = derive_row_tags(3, [], doc_low_confidence=True)
        self.assertEqual(tags, ["黄系", "黄系", "黄系"])

    def test_doc_red_tags_all_rows_red(self):
        tags = derive_row_tags(2, [], doc_red=True)
        self.assertEqual(tags, ["赤系", "赤系"])

    def test_doc_red_overrides_low_confidence_yellow(self):
        # Arrange: 低置信(黄) かつ 合計不符(赤) が同居 → 赤が勝つ
        tags = derive_row_tags(2, [], doc_low_confidence=True, doc_red=True)
        self.assertEqual(tags, ["赤系", "赤系"])

    def test_doc_red_overrides_per_entry_low(self):
        # Arrange: doc 赤(全行) + ある行の per-entry low → その行も赤
        tags = derive_row_tags(2, [(0, [_flag("low")])], doc_red=True)
        self.assertEqual(tags, ["赤系", "赤系"])

    def test_per_entry_medium_above_doc_low_confidence(self):
        # Arrange: 低置信(全行黄) + 1行に medium → その行は橙、他は黄
        tags = derive_row_tags(
            2, [(0, [_flag("medium")])], doc_low_confidence=True)
        self.assertEqual(tags, ["橙系", "黄系"])

    def test_offset_out_of_range_is_ignored(self):
        tags = derive_row_tags(2, [(5, [_flag("high")])])
        self.assertEqual(tags, ["", ""])

    def test_returns_new_list_does_not_mutate_input(self):
        flags = [(0, [_flag("high")])]
        derive_row_tags(1, flags)
        # 入力フラグ構造が破壊されていないこと
        self.assertEqual(flags, [(0, [_flag("high")])])


if __name__ == "__main__":
    unittest.main()
