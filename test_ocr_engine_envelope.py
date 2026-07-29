"""不要ページ分類器 _is_envelope_page と異体字正規化のテスト（IP-401 T3）。

PaddleOCR は日本語の漢字をしばしば簡体字に取り違える。IP-401 の実事故では
「☆領収証☆」が「☆领収证☆」と読まれ、構造キーワード「領収」に照合せず、
55文字（閾値60未満）と相まって「裏面メモ」と誤分類された。

T1 で「entries を組めたページは棄却されない」構造にしたため、この正規化は
もはや票の消失を防ぐ機能ではない。entries を組めなかったページを
「監査タブ行き」にするか「赤い認識不能行」にするかの**分類精度**を上げる
hardening である（Plan §3.4）。誤ると真の異常が監査タブに埋もれる。

    venv311/bin/python -m unittest test_ocr_engine_envelope -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import ocr_engine

# Plan §1.2 の実測値。161×213pt の小型サーマル領収証（駐車場・現金200円）を
# PaddleOCR が読んだ生テキスト（conf=0.883）。「領収証」→「领収证」。
MAIZURU_OCR_TEXT = (
    "舞鶴パーク\n☆领収证☆\nNo.05\n入庫26/07/1723：33:2\n"
    "精算26/07/1801：10:1\n現金\n200円"
)


class NormalizeForKeywordMatchTest(unittest.TestCase):
    """比較専用の一方向正規化。表示用テキストには使わない（§3.4）。"""

    def test_simplified_variants_map_to_japanese_forms(self):
        # Arrange / Act / Assert: 各写像の正例
        cases = [
            ("领収证", "領収証"),
            ("收入", "収入"),
            ("请求", "請求"),
            ("买上", "買上"),
            ("合计", "合計"),
            # 纳/录 は本ファイル内の既存の場当たり対処（"纳期限" のリテラル、
            # 登[録录]番号 の正規表現クラス）が観測済みであることを示す誤読
            ("纳期限", "納期限"),
            ("登录番号", "登録番号"),
        ]
        for src, expected in cases:
            with self.subTest(src=src):
                self.assertEqual(
                    ocr_engine._normalize_for_keyword_match(src), expected)

    def test_out_of_table_characters_are_left_alone(self):
        # Arrange / Act / Assert: 写像表に無い簡体字は変換しない（過剰変換防止）
        self.assertEqual(
            ocr_engine._normalize_for_keyword_match("请求书"), "請求书")

    def test_already_japanese_text_is_unchanged(self):
        # Arrange / Act / Assert: 負例——正常な日本語は変換されない
        for text in ["領収証", "請求書", "合計", "小計", "お買上"]:
            with self.subTest(text=text):
                self.assertEqual(
                    ocr_engine._normalize_for_keyword_match(text), text)

    def test_nfkc_folds_fullwidth_forms(self):
        # Arrange / Act / Assert: 全角英数字・記号は NFKC で半角へ畳む
        self.assertEqual(
            ocr_engine._normalize_for_keyword_match("Ｎｏ．０５"), "No.05")

    def test_empty_and_none_are_safe(self):
        # Arrange / Act / Assert: 境界値
        self.assertEqual(ocr_engine._normalize_for_keyword_match(""), "")
        self.assertEqual(ocr_engine._normalize_for_keyword_match(None), "")


class EnvelopeClassifierVariantTest(unittest.TestCase):
    """異体字誤認識で構造キーワードが失配しないこと（T3 の中核 DoD）。"""

    def test_misread_receipt_is_not_classified_as_envelope(self):
        # Arrange: IP-401 で実際に消えた票の生 OCR テキスト
        # Act
        result = ocr_engine._is_envelope_page(MAIZURU_OCR_TEXT, {})

        # Assert: 「领収证」が「領収証」として構造キーワードに照合し、
        # 60文字未満でも裏面メモ扱いされない
        self.assertFalse(result)

    def test_normal_receipt_still_not_envelope(self):
        # Arrange / Act / Assert: 既存正常系の回帰保護
        self.assertFalse(ocr_engine._is_envelope_page("領収書 合計 1,100円", {}))
        self.assertFalse(ocr_engine._is_envelope_page("請求書 御中 合計", {}))

    def test_genuine_envelope_still_excluded(self):
        # Arrange / Act / Assert: 本物の封筒は引き続き除外する（負例）
        self.assertTrue(
            ocr_engine._is_envelope_page("〒100-0001 東京都千代田区 御中", {}))

    def test_genuine_short_memo_still_excluded(self):
        # Arrange / Act / Assert: 構造キーワードの無い短文メモは引き続き除外
        self.assertTrue(ocr_engine._is_envelope_page("メモ 明日連絡", {}))

    def test_financial_keywords_are_matched_after_normalization(self):
        """§3.4: 分類器全体を同一 normalized_text で照合する（Codex 中3）。

        構造キーワードだけ正規化して財務キーワード側を素のままにすると、
        同じ誤認識が判定の片側にしか効かず一貫性を欠く。

        封筒キーワード（御中・差出人・親展）が並ぶが金額も印字されている
        面——正規化前は「请求」が財務キーワードに失配して封筒と誤断される。
        60文字超にしてあるのは、短文メモ規則ではなく封筒規則で判定させ、
        テストが狙った経路だけを見るため。
        """
        # Arrange: 「请求」と誤認識された財務キーワードを含む封筒風テキスト
        text = ("〒100-0001 東京都千代田区丸の内一丁目二番三号 "
                "株式会社サンプル商事 経理部 御中 差出人 山田太郎 親展 "
                "书留 请求金额 12,000円")

        # Act / Assert: 財務キーワード扱いになり封筒判定されない
        self.assertFalse(ocr_engine._is_envelope_page(text, {}))

    def test_envelope_without_amount_is_still_excluded_when_long(self):
        # Arrange / Act / Assert: 負例——金額が無ければ長文でも封筒のまま
        text = ("〒100-0001 東京都千代田区丸の内一丁目二番三号 "
                "株式会社サンプル商事 経理部 御中 差出人 山田太郎 親展 "
                "書留 速達 配達証明")
        self.assertTrue(ocr_engine._is_envelope_page(text, {}))


if __name__ == "__main__":
    unittest.main()
