"""anomaly_detector.detect_document_anomalies の単体テスト（[B'] 票面合計照合）。

依存ゼロ（config / receipt_aggregation のみ）。系統 python3 で実行可:
    python3 -m unittest test_anomaly_detector -v
    venv311/bin/python -m unittest test_anomaly_detector -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from anomaly_detector import detect_document_anomalies


class DetectDocumentAnomaliesTest(unittest.TestCase):
    """票面合計 vs Σ行金額の照合（6/12 静默錯 D票・C票対策）。"""

    def test_duskin_tax_exclusive_output_flagged(self):
        # Arrange: D票ダスキン（外税換算漏れ）。票面6456 だが行は税抜5870
        # Act
        flags = detect_document_anomalies({"total_amount": 6456}, [5870])
        # Assert: 1 flag、形式は detect_anomalies と同契約
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["type"], "total_mismatch")
        self.assertEqual(flags[0]["severity"], "high")
        self.assertEqual(flags[0]["col"], 8)
        self.assertIn("6,456", flags[0]["message"])
        self.assertIn("5,870", flags[0]["message"])
        self.assertIn("-586", flags[0]["message"])

    def test_rokkakudo_hallucinated_row_flagged(self):
        # Arrange: C票六角堂（幻覚で対象外行2420を多出）。票面7310、Σ=9730
        # Act
        flags = detect_document_anomalies({"total_amount": 7310}, [7310, 2420])
        # Assert: 差額 +2,420 が message に出る
        self.assertEqual(len(flags), 1)
        self.assertIn("+2,420", flags[0]["message"])

    def test_exact_match_no_flag(self):
        # Arrange / Act: 票面と Σ が一致
        flags = detect_document_anomalies({"total_amount": 7310}, [7310])
        # Assert
        self.assertEqual(flags, [])

    def test_rounding_diff_within_tolerance_no_flag(self):
        # Arrange: 外税換算の丸め差（±1〜±2円）は許容
        for diff in (1, -1, 2, -2):
            with self.subTest(diff=diff):
                # Act
                flags = detect_document_anomalies(
                    {"total_amount": 7310}, [7310 + diff])
                # Assert
                self.assertEqual(flags, [])

    def test_diff_just_over_tolerance_flagged(self):
        # Arrange: 許容差を1円超えた境界（±3円）は検出
        for diff in (3, -3):
            with self.subTest(diff=diff):
                # Act
                flags = detect_document_anomalies(
                    {"total_amount": 7310}, [7310 + diff])
                # Assert
                self.assertEqual(len(flags), 1)

    def test_missing_or_none_total_skipped(self):
        # Arrange / Act / Assert: key 欠損 / None は照合せずスキップ
        self.assertEqual(detect_document_anomalies({}, [5870]), [])
        self.assertEqual(
            detect_document_anomalies({"total_amount": None}, [5870]), [])

    def test_zero_total_skipped(self):
        # Arrange / Act / Assert: total=0（印字なし）はスキップ
        self.assertEqual(
            detect_document_anomalies({"total_amount": 0}, [5870]), [])

    def test_non_numeric_total_skipped(self):
        # Arrange / Act / Assert: 非数値はスキップ（誤報防止）
        self.assertEqual(
            detect_document_anomalies({"total_amount": "不明"}, [5870]), [])

    def test_money_string_total_coerced(self):
        # Arrange / Act: "¥6,456" は coerce で 6456 になり、行5870 と不一致→flag
        flags = detect_document_anomalies({"total_amount": "¥6,456"}, [5870])
        # Assert
        self.assertEqual(len(flags), 1)
        # Arrange / Act: "6,456" と行6456 は一致→ flag なし
        flags2 = detect_document_anomalies({"total_amount": "6,456"}, [6456])
        # Assert
        self.assertEqual(flags2, [])

    def test_empty_row_amounts_skipped(self):
        # Arrange / Act / Assert: 行が空（全行 amount==0 でスキップ済み）
        self.assertEqual(
            detect_document_anomalies({"total_amount": 6456}, []), [])

    def test_negative_refund_total_checked(self):
        # Arrange / Act: 純返金票（負合計）も照合する。一致→ flag なし
        self.assertEqual(
            detect_document_anomalies({"total_amount": -1000}, [-1000]), [])
        # Arrange / Act: 不一致→ flag
        flags = detect_document_anomalies({"total_amount": -1000}, [-700])
        # Assert
        self.assertEqual(len(flags), 1)

    def test_none_parent_data_skipped(self):
        # Arrange / Act / Assert: parent_data=None でも落ちずスキップ
        self.assertEqual(detect_document_anomalies(None, [5870]), [])

    def test_point_payment_total_uses_pre_discount_amount(self):
        # Arrange: ポイント800円充当・支払額200円の票でも、total は値引前の
        # お買上計1000（tax_summary 対象額由来）を転記する前提。行 Σ も対象額由来
        # Act
        flags = detect_document_anomalies({"total_amount": 1000}, [1000])
        # Assert: 値引前合計と一致するため flag なし（語義文檔化）
        self.assertEqual(flags, [])


if __name__ == "__main__":
    unittest.main()
