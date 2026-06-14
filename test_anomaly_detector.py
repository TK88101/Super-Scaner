"""anomaly_detector.detect_document_anomalies の単体テスト（[B'] 票面合計照合）。

依存ゼロ（config / receipt_aggregation のみ）。系統 python3 で実行可:
    python3 -m unittest test_anomaly_detector -v
    venv311/bin/python -m unittest test_anomaly_detector -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from anomaly_detector import (
    detect_document_anomalies,
    detect_low_confidence,
    detect_outlier_exempt_rows,
)


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


class DetectOutlierExemptRowsTest(unittest.TestCase):
    """規則①（主力・対象外行の構造異常）。

    Σ行金額==票面合計を満たす幻覚（六角堂: 課税7,310 + 対象外90,000）は
    detect_document_anomalies を素通りするため、対象外行の構造的不合理を
    別途検出する。debit_tax_type の精確等値（"対象外"）で配対し、
    3 閾値（a:単個対象外>課税合計 / b:対象外合計/票面>50% / c:単個対象外>5万）
    のいずれか命中で 1 件 high flag を返す純関数。
    """

    def test_rokkakudo_all_three_conditions_hit(self):
        # Arrange: 六角堂（課税7,310[課対仕入10%] + 対象外90,000）, 票面97,310
        # Act
        flags = detect_outlier_exempt_rows(
            [7310, 90000], ["課対仕入10%", "対象外"], 97310)
        # Assert: 1 件 high flag、I列（col=8）、message に 90,000
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["type"], "outlier_exempt_row")
        self.assertEqual(flags[0]["severity"], "high")
        self.assertEqual(flags[0]["col"], 8)
        self.assertIn("90,000", flags[0]["message"])

    def test_only_condition_a_hits(self):
        # Arrange: 対象外40,000 > 課税合計30,000 だが <5万 かつ 占比<50%
        #   票面=200,000 で b を外す（40000/200000=0.2）
        # Act
        flags = detect_outlier_exempt_rows(
            [30000, 40000], ["課対仕入10%", "対象外"], 200000)
        # Assert: a 単独で 1 件（OR ロジック）
        self.assertEqual(len(flags), 1)

    def test_only_condition_c_hits_taxable_empty(self):
        # Arrange: 全対象外（taxable 空, 分母0）かつ単行60,000>5万
        # Act
        flags = detect_outlier_exempt_rows([60000], ["対象外"], 60000)
        # Assert: 分母0で a/b をスキップし c のみ命中（除零しない）
        self.assertEqual(len(flags), 1)

    def test_enejet_fuel_receipt_no_flag(self):
        # Arrange: EneJet 軽油正常票（旅費6,387課税 + 租税公課708対象外）
        # Act
        flags = detect_outlier_exempt_rows(
            [6387, 708], ["課対仕入10%", "対象外"], 7095)
        # Assert: a)708<6387 b)0.10 c)708<5万 全外 → 誤報なし
        self.assertEqual(flags, [])

    def test_normal_stamp_receipt_no_flag(self):
        # Arrange: 印紙小票（課税5,000 + 対象外200）
        # Act
        flags = detect_outlier_exempt_rows(
            [5000, 200], ["課対仕入10%", "対象外"], 5200)
        # Assert
        self.assertEqual(flags, [])

    def test_all_taxable_no_exempt_row(self):
        # Arrange: 全課税（対象外行なし）
        # Act
        flags = detect_outlier_exempt_rows(
            [5000, 3000], ["課対仕入10%", "課対仕入8% (軽)"], 8000)
        # Assert: 対象外なし → 早返り
        self.assertEqual(flags, [])

    def test_empty_and_none_and_length_mismatch(self):
        # Arrange / Act / Assert: 空・長さ不一致は誤報防止で早返り
        self.assertEqual(detect_outlier_exempt_rows([], [], None), [])
        self.assertEqual(
            detect_outlier_exempt_rows([7310], ["課対仕入10%", "対象外"], 1), [])
        self.assertEqual(detect_outlier_exempt_rows(None, ["対象外"], 1), [])

    def test_all_exempt_small_no_flag(self):
        # Arrange: 純税費小票（対象外300 + 対象外200、各<5万）
        # Act
        flags = detect_outlier_exempt_rows(
            [300, 200], ["対象外", "対象外"], 500)
        # Assert: taxable 空で a/b スキップ、c も不中 → 誤報なし
        self.assertEqual(flags, [])

    def test_negative_refund_exempt_no_flag(self):
        # Arrange: 返品（対象外 -90,000）
        # Act
        flags = detect_outlier_exempt_rows([-90000], ["対象外"], None)
        # Assert: 正額の対象外が0 → 早返り（退货を誤判しない）
        self.assertEqual(flags, [])

    def test_missing_total_skips_b_but_a_and_c_fire(self):
        # Arrange: total 欠損でも a/c は依存なしで命中（六角堂値）
        # Act
        flags = detect_outlier_exempt_rows(
            [7310, 90000], ["課対仕入10%", "対象外"], None)
        # Assert: a+c で 1 件
        self.assertEqual(len(flags), 1)

    def test_money_string_normalization(self):
        # Arrange: money-string（カンマ）でも coerce_tax_amount で正規化
        # Act
        flags = detect_outlier_exempt_rows(
            ["7,310", "90,000"], ["課対仕入10%", "対象外"], "97,310")
        # Assert
        self.assertEqual(len(flags), 1)

    def test_kazei_label_not_misjudged_as_exempt(self):
        # Arrange: 「課対仕入」は「対」を含むが対象外ではない（精確等値）
        # Act
        flags = detect_outlier_exempt_rows(
            [90000, 7310], ["課対仕入10%", "課対仕入8% (軽)"], 97310)
        # Assert: 対象外行ゼロ → 早返り（in/含「対」誤判しない）
        self.assertEqual(flags, [])


class DetectLowConfidenceTest(unittest.TestCase):
    """規則②（兜底・低置信整票送審）。

    ocr_confidence が閾値未満なら整票を黄でマークし人手複査を促す純関数。
    None/非数値は無信号として誤報を避ける。六角堂 conf=0.91 は抓えない
    （①が主力・②は補充）ことを境界テストで保証する。
    """

    def test_below_threshold_flagged(self):
        # Arrange / Act
        flags = detect_low_confidence({"ocr_confidence": 0.60}, 0.85)
        # Assert: full_row 黄 flag、message に 0.60
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["type"], "low_confidence")
        self.assertEqual(flags[0]["severity"], "low")
        self.assertTrue(flags[0]["full_row"])
        self.assertIn("0.60", flags[0]["message"])

    def test_rokkakudo_091_not_flagged(self):
        # Arrange / Act: 六角堂 0.91（①主力の裏付け）
        flags = detect_low_confidence({"ocr_confidence": 0.91}, 0.85)
        # Assert
        self.assertEqual(flags, [])

    def test_exact_threshold_not_flagged(self):
        # Arrange / Act: 閾値ちょうど（厳密小なり）
        flags = detect_low_confidence({"ocr_confidence": 0.85}, 0.85)
        # Assert
        self.assertEqual(flags, [])

    def test_missing_none_and_non_numeric_skipped(self):
        # Arrange / Act / Assert: 無信号は誤報防止で空リスト
        self.assertEqual(detect_low_confidence({}, 0.85), [])
        self.assertEqual(
            detect_low_confidence({"ocr_confidence": None}, 0.85), [])
        self.assertEqual(
            detect_low_confidence({"ocr_confidence": "abc"}, 0.85), [])
        self.assertEqual(detect_low_confidence(None, 0.85), [])


if __name__ == "__main__":
    unittest.main()
