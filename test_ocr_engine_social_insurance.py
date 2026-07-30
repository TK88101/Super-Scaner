"""社会保険料通知書の零仕訳化テスト（IP-401 T6 / Plan §3.8）。

顧客の表に実在した誤り（IDO、2026/07/29 書込、両行とも黄系）:

    取引No 1 | 2026/07/21 | 保険料 | 319,000 | 6月分保険料納入告知
    取引No 2 | 2026/06/30 | 保険料 | 319,000 | 5月分保険料領収済

「保険料納入告知額・領収済額通知書」は券面に当月の**納入告知額**と前月の
**領収済額**の2口が印字されており、両方を仕訳化していた。

社員からの共通ルール宣言（2026-07-30、最優先）:
「社会保険料に関する会計処理はアップロードせずに口座振替資料として処理すると
共通ルールとして統一しています」

したがって「当月分のみ反映」は採らない。口座振替側で既に記帳される以上、
SS 側が当月分を1行作れば銀行側と二重計上になる。**仕訳は一切作らない。**

封筒判定との違い（意図的）:
  封筒     = ヒューリスティック。適用範囲を PDF 逐頁ループに絞る（§3.5）
  社会保険 = 確定した業務ルール。全 doc_type・全経路で常時有効。顧客が
             「今後スキャンしない」と述べてもコードはそれに依存しない（§7-5）

    venv311/bin/python -m unittest test_ocr_engine_social_insurance -v
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(__file__))

import ocr_engine
from doc_types import DocType

# 日本年金機構の定型書式。納入告知額（当月）と領収済額（前月）の2口が
# 1枚に印字されている。事業所名・金額は合成値。
NOTICE_OCR_TEXT = (
    "健康保険・厚生年金保険\n"
    "保険料納入告知額・領収済額通知書\n"
    "日本年金機構\n"
    "事業所名 サンプル商事株式会社\n"
    "納入告知額 6月分 319,000円\n"
    "領収済額 5月分 319,000円\n"
    "納付期限 2026年7月31日\n"
)


def _two_doc_receipt_raw():
    """Gemini が2口とも仕訳化してしまった状態（実事故の再現）。"""
    return {"documents": [
        {"doc_category": "receipt", "vendor": "日本年金機構",
         "date": "2026/07/21",
         "items": [{"description": "6月分保険料納入告知", "amount": 319000,
                    "tax_rate": 0, "debit_account": "保険料"}]},
        {"doc_category": "receipt", "vendor": "日本年金機構",
         "date": "2026/06/30",
         "items": [{"description": "5月分保険料領収済", "amount": 319000,
                    "tax_rate": 0, "debit_account": "保険料"}]},
    ]}


class SocialInsuranceDetectorTest(unittest.TestCase):
    """券面キーワードによる検出（日本年金機構の定型書式）。"""

    def test_detects_real_notice_text(self):
        # Arrange / Act / Assert
        self.assertTrue(ocr_engine._is_social_insurance_notice(NOTICE_OCR_TEXT))

    def test_detects_by_strong_keyword_alone(self):
        # Arrange / Act / Assert: 単独で十分に特異な複合語のみ
        for kw in ["保険料納入告知額", "領収済額通知書"]:
            with self.subTest(kw=kw):
                self.assertTrue(
                    ocr_engine._is_social_insurance_notice(f"...{kw}..."))

    def test_bare_nounyu_kokuchi_gaku_alone_is_not_enough(self):
        """「納入告知額」単独では発火させない（誤爆の代償が重い）。

        「納入告知書/納入告知額」は日本の公的機関の徴収通知に広く使われる
        一般語であり、労働保険料（労働局）等、顧客ルール「社会保険料は
        アップロードしない」の**対象外**の文書まで巻き込む。巻き込むと
        仕訳0件になるうえ MF タブに「社会保険料通知書です」と断定的に
        誤ったラベルが顧客向けに書かれる——静かに消えるより悪い。
        """
        # Arrange / Act / Assert: 労働保険の徴収通知を模したテキスト
        self.assertFalse(ocr_engine._is_social_insurance_notice(
            "労働保険料等納入告知額 厚生労働省 労働局 12,340円"))

    def test_agency_name_alone_is_not_enough(self):
        """「日本年金機構」だけでは判定しない（年金定期便等に誤爆する）。"""
        # Arrange / Act / Assert
        self.assertFalse(
            ocr_engine._is_social_insurance_notice("日本年金機構からのお知らせ"))

    def test_bare_keyword_with_insurance_type_is_enough(self):
        # Arrange / Act / Assert: 機関名・保険種別と共起すれば成立
        for text in [
            "納入告知額 厚生年金保険 319,000円",
            "納入告知額 健康保険 319,000円",
            "納入告知額 日本年金機構",
        ]:
            with self.subTest(text=text):
                self.assertTrue(ocr_engine._is_social_insurance_notice(text))

    def test_gemini_vendor_cross_check_catches_garbled_ocr(self):
        """OCR が崩れても Gemini が機関名を拾えていれば検出する。

        OCR だけに頼ると、キーワードが改行で分断されたり写像表外の異体字で
        崩れたときに見逃し、社会保険料の仕訳が作られて口座振替側と二重計上に
        なる（§3.8 が防ぎたい事態そのもの）。
        """
        # Arrange: OCR は通知書の複合語を組み立てられなかったが「保険料」は
        # 拾えている。Gemini は取引先を読めている
        raw = {"documents": [{"vendor": "日本年金機構", "items": []}]}

        # Act / Assert
        self.assertTrue(ocr_engine._is_social_insurance_notice(
            "健康保険 厚生年金保 険料 ###読取崩れ###", raw))

    def test_vendor_cross_check_does_not_fire_on_ordinary_vendor(self):
        # Arrange / Act / Assert: 負例
        raw = {"documents": [{"vendor": "ファミリーマート", "items": []}]}
        self.assertFalse(
            ocr_engine._is_social_insurance_notice("領収書 合計 500円", raw))

    def test_vendor_alone_cannot_suppress_other_agency_documents(self):
        """取引先名は裏付けであって単独の陽性シグナルではない。

        年金機構は通知書以外（年金定期便・各種お知らせ）も送ってくる。
        vendor 単独で成立させると、キーワード判定が意図的に弾いている
        「日本年金機構からのお知らせ」が Gemini の vendor だけで吞まれ、
        その文書の会計データが失われる。
        """
        # Arrange: OCR は通知書の語を含まない。Gemini は機関名を vendor に入れた
        raw = {"documents": [{"vendor": "日本年金機構", "items": []}]}

        # Act / Assert
        self.assertFalse(ocr_engine._is_social_insurance_notice(
            "日本年金機構からのお知らせ 事務手続きのご案内", raw))

    def test_agency_name_with_premium_is_enough(self):
        # Arrange / Act / Assert: 弱いキーワードは組み合わせで成立させる
        self.assertTrue(
            ocr_engine._is_social_insurance_notice("日本年金機構 保険料 のご案内"))

    def test_ordinary_receipt_does_not_false_positive(self):
        # Arrange / Act / Assert: 負例——通常の領収書は誤爆しない
        for text in [
            "領収書 ファミリーマート 合計 1,100円",
            "請求書 株式会社サンプル 御中 合計 55,000円",
            "駐車場 領収証 現金 200円",
            "生命保険料控除証明書 保険料 120,000円",  # 「保険料」単独では出ない
        ]:
            with self.subTest(text=text):
                self.assertFalse(ocr_engine._is_social_insurance_notice(text))

    def test_detection_survives_variant_misreading(self):
        """§3.4 の正規化を共用する（PaddleOCR の簡体字誤読に耐える）。"""
        # Arrange: 「領収済額通知書」の 領 が 领 と誤読された想定
        # Act / Assert
        self.assertTrue(
            ocr_engine._is_social_insurance_notice("保険料納入告知額 领収済額通知書"))

    def test_keyword_split_across_lines_is_still_detected(self):
        """PaddleOCR は券面レイアウトどおりに改行するのでキーワードが行またぎ
        になりうる。半角空白だけ落とす実装だとここで取りこぼし、禁止された
        仕訳が生成されて口座振替側と二重計上になる。"""
        # Arrange / Act / Assert
        self.assertTrue(ocr_engine._is_social_insurance_notice(
            "健康保険・厚生年金保険\n保険料納入\n告知額・領収済額\n通知書"))

    def test_empty_text_is_safe(self):
        # Arrange / Act / Assert
        self.assertFalse(ocr_engine._is_social_insurance_notice(""))
        self.assertFalse(ocr_engine._is_social_insurance_notice(None))


class SocialInsuranceYieldsZeroEntriesTest(unittest.TestCase):
    """中核 DoD: 仕訳を一切生成せず、提示行を1行だけ出す。"""

    def test_no_entries_and_single_placeholder(self):
        # Arrange: Gemini は2口とも仕訳化してしまっている（実事故の状態）
        # Act
        with redirect_stdout(io.StringIO()):
            results = list(ocr_engine._yield_page_results(
                DocType.RECEIPT, _two_doc_receipt_raw(), NOTICE_OCR_TEXT, 0.9))

        # Assert: Gemini の2行を採らず、提示行1件だけ
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["entries"], [])
        self.assertTrue(results[0].get("_excluded_page"))
        self.assertEqual(results[0].get("_exclude_reason"),
                         "social_insurance_notice")

    def test_destination_is_mf_tab_not_audit_tab(self):
        """§3.8: 行き先は producer が宣言する（reason から推測させない）。"""
        # Arrange / Act
        with redirect_stdout(io.StringIO()):
            results = list(ocr_engine._yield_page_results(
                DocType.RECEIPT, _two_doc_receipt_raw(), NOTICE_OCR_TEXT, 0.9))

        # Assert
        self.assertEqual(results[0].get("_exclude_destination"),
                         ocr_engine.EXCLUDE_DEST_MF_TAB)

    def test_applies_to_non_receipt_doc_types(self):
        """請求書フォルダへ投げられても仕訳を作らない（全 doc_type 常時有効）。

        封筒判定（envelope_filter で PDF 逐頁ループに限定、§3.5）と違い、
        この検査は全 doc_type・全経路で常時有効。顧客が「井戸会計では今後
        スキャンしない」と述べてもコードはそれに依存しない（§7-5 ユーザー裁定:
        「你不能去賭它掃不掃」）。
        """
        # Arrange / Act
        with redirect_stdout(io.StringIO()):
            results = list(ocr_engine._yield_page_results(
                DocType.PURCHASE_INVOICE, {"vendor": "日本年金機構"},
                NOTICE_OCR_TEXT, 0.9))

        # Assert
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["entries"], [])
        self.assertEqual(results[0].get("_exclude_reason"),
                         "social_insurance_notice")

    def test_normal_receipt_is_unaffected(self):
        # Arrange: 回帰保護——通常の領収書は従来通り仕訳化される
        raw = {"documents": [{
            "doc_category": "receipt", "vendor": "テスト店",
            "items": [{"description": "商品", "amount": 1100,
                       "tax_rate": 0.10, "debit_account": "備品・消耗品費"}],
        }]}

        # Act
        with redirect_stdout(io.StringIO()):
            results = list(ocr_engine._yield_page_results(
                DocType.RECEIPT, raw, "領収書 テスト店 合計1,100円", 0.95))

        # Assert
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]["entries"]), 1)
        self.assertFalse(results[0].get("_excluded_page"))


if __name__ == "__main__":
    unittest.main()
