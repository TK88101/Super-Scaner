"""不要ページ分類器 _is_envelope_page と異体字正規化のテスト（IP-401 T3）。

PaddleOCR は日本語の漢字をしばしば簡体字に取り違える。IP-401 の実事故では
「☆領収証☆」が「☆领収证☆」と読まれ、構造キーワード「領収」に照合せず、
55文字（閾値60未満）と相まって「裏面メモ」と誤分類された。

T1 で「entries を組めたページは棄却されない」構造にしたため、この正規化は
もはや票の消失を防ぐ機能ではない。entries を組めなかったページを
「監査タブ行き」にするか「赤い認識不能行」にするかの**分類精度**を上げる
hardening である（Plan §3.4）。誤ると真の異常が監査タブに埋もれる。

本ファイルは B7 T5（契約 v0.15 §5.1-b 裁定-5）で、尾段（単頁 PDF・画像）の
envelope_filter を headless に限り有効化する変更のテストも併せ持つ
（EnvelopeFilterTailSegmentHeadlessGateTest 以降）。

    venv311/bin/python -m unittest test_ocr_engine_envelope -v
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

# main.py はモジュール読込時に必須環境変数が無いと exit(1) する
# （headless 統合テストが main を import するため、他ファイルと同じ防御）。
os.environ.setdefault("PROCESSED_FOLDER_ID", "test_processed_folder")
os.environ.setdefault("SERVICE_ACCOUNT_FILE", "test_sa.json")
os.environ.setdefault("OUTPUT_SPREADSHEET_ID", "test_spreadsheet")
os.environ.setdefault("FOLDER_RECEIPT_ID", "test_receipt_folder")

import config
import ocr_engine
from doc_types import DocType

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


# ============================================================
# B7 T5（契約 v0.15 §5.1-b 裁定-5）: 尾段 envelope_filter の headless 有効化
# ============================================================

def _valid_receipt_raw():
    """1品目の正常な領収書 JSON（test_ocr_engine_receipt_pipeline.py と同型）。"""
    return {"documents": [{
        "doc_category": "receipt",
        "payment_method": "現金",
        "vendor": "テスト店",
        "items": [{
            "description": "商品",
            "amount": 1100,
            "tax_rate": 0.10,
            "debit_account": "備品・消耗品費",
        }],
    }]}


def _empty_receipt_raw():
    """Gemini が有効な仕訳を組めなかったときの戻り値（documents 空）。"""
    return {"documents": []}


# 封筒キーワードのみ・金額関連キーワード無し → _is_envelope_page が True を返す
_TAIL_ENVELOPE_OCR_TEXT = "〒100-0001 東京都千代田区 御中"


def _run_single_page_pipeline(gemini_raw, ocr_text, doc_type=DocType.RECEIPT,
                              suffix=".jpg"):
    """尾段（単頁 PDF・画像）の通常パスを通す（test_ocr_engine_invoice.py の
    _run_single_page_pipeline と同型・同方針: mime image/* で PDF 分岐を
    素通りさせ、_route_ocr_strategy/_call_gemini を fake で遮断する）。
    """
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(b"dummy")
        path = tmp.name

    try:
        with mock.patch.object(ocr_engine, "_split_pdf_pages",
                               return_value=iter([])), \
             mock.patch.object(ocr_engine, "_route_ocr_strategy",
                               return_value=(gemini_raw, ocr_text, 0.9)), \
             mock.patch.object(ocr_engine, "_call_gemini", return_value=None):
            with redirect_stdout(io.StringIO()):
                return list(ocr_engine.process_pipeline(
                    path, doc_type=doc_type, ocr_strategy="C"))
    finally:
        os.unlink(path)


class EnvelopeFilterTailSegmentHeadlessGateTest(unittest.TestCase):
    """T5 DoD①-④: 尾段の envelope_filter は headless のみ有効
    （`process_pipeline` 実経路、`config.headless_mode` を patch）。
    """

    def test_headless_single_page_receipt_empty_envelope_text_is_excluded(self):
        # ①headless・単頁・RECEIPT・entries 空・封筒テキスト → _excluded_page
        # yield（監査行き）
        with mock.patch.object(config, "headless_mode", return_value=True):
            pages = _run_single_page_pipeline(
                _empty_receipt_raw(), _TAIL_ENVELOPE_OCR_TEXT)

        self.assertEqual(len(pages), 1)
        result = pages[0]["result"]
        self.assertTrue(result.get("_excluded_page"))
        self.assertEqual(result.get("_exclude_reason"), "envelope")
        self.assertFalse(result.get("_page_error"))
        self.assertFalse(result.get("_unrecognized"))

    def test_headless_single_page_receipt_valid_entries_keep_posting_with_audit_signal(self):
        # ②headless・entries 有効＋封筒シグナル → _audit_signal で記帳継続
        # （不変式「entries 否決禁止」維持）
        with mock.patch.object(config, "headless_mode", return_value=True):
            pages = _run_single_page_pipeline(
                _valid_receipt_raw(), _TAIL_ENVELOPE_OCR_TEXT)

        self.assertEqual(len(pages), 1)
        result = pages[0]["result"]
        self.assertEqual(len(result["entries"]), 1)
        self.assertFalse(result.get("_excluded_page"))
        self.assertEqual(result.get("_audit_signal"), "envelope_signal_with_entries")

    def test_non_headless_single_page_receipt_keeps_legacy_unrecognized(self):
        # ③非 headless・同入力 → 従来（認識不能占位、envelope_filter 不発火）
        with mock.patch.object(config, "headless_mode", return_value=False):
            pages = _run_single_page_pipeline(
                _empty_receipt_raw(), _TAIL_ENVELOPE_OCR_TEXT)

        self.assertEqual(len(pages), 1)
        result = pages[0]["result"]
        self.assertTrue(result.get("_unrecognized"))
        self.assertFalse(result.get("_excluded_page"))

    def test_headless_single_page_non_receipt_skips_envelope_judgement(self):
        # ④非 RECEIPT は headless でも判定対象外（Session 16 裁決の維持）
        with mock.patch.object(ocr_engine, "_is_envelope_page") as envelope, \
             mock.patch.object(config, "headless_mode", return_value=True):
            pages = _run_single_page_pipeline(
                {"date": "2026/07/09", "vendor": "テスト商事", "items": []},
                _TAIL_ENVELOPE_OCR_TEXT, doc_type=DocType.PURCHASE_INVOICE)

        envelope.assert_not_called()
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0]["result"].get("_unrecognized"))


class EnvelopeFilterTailSegmentHeadlessIntegrationTest(unittest.TestCase):
    """T5 統合検収（Codex #7 採納）: main.process_file（headless）経由で
    尾段の除外ページが監査タブへ回り MF 区へは零書込であることを検収する。

    OCR/Gemini は既存様式の fake で遮断し、`ocr_engine.process_pipeline` は
    実物を通す（main.process_pipeline は patch しない、test_ip401_regression.py
    と同方針）。
    """

    def _run(self, path, *, headless):
        import main
        from headless_rerun_fixture import FakeFirestore, FakeWriter, make_ledger

        writer = FakeWriter()
        fs = FakeFirestore()
        base = "cust:hash"

        with mock.patch.object(ocr_engine, "_split_pdf_pages",
                               return_value=iter([])), \
             mock.patch.object(ocr_engine, "_route_ocr_strategy",
                               return_value=(_empty_receipt_raw(),
                                            _TAIL_ENVELOPE_OCR_TEXT, 0.9)), \
             mock.patch.object(ocr_engine, "_call_gemini", return_value=None), \
             mock.patch.object(config, "headless_mode", return_value=headless), \
             mock.patch.object(main, "send_notification"), \
             mock.patch.object(main, "PageUrlResolver") as resolver_cls:
            resolver_cls.return_value.resolve.side_effect = (
                lambda page_num, total_pages, page_bytes: f"http://url/p{page_num}")
            resolver_cls.return_value.anchor_url.side_effect = (
                lambda page_num, total_pages: f"http://url#page={page_num}")
            with redirect_stdout(io.StringIO()):
                if headless:
                    ledger = make_ledger(fs, writer, base)
                    outcome = main.process_file(
                        service=None, sheets_writer=writer, file_path=path,
                        uploader_name="田中", chat_id=None,
                        doc_type=DocType.RECEIPT, drive_file_id=None,
                        base=base, ledger=ledger, tab_owner="田中")
                else:
                    outcome = main.process_file(
                        service=None, sheets_writer=writer, file_path=path,
                        uploader_name="田中", chat_id=None,
                        doc_type=DocType.RECEIPT, drive_file_id=None)
        return writer, outcome

    def _single_page_path(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(b"dummy")
        tmp.close()
        return tmp.name

    def test_headless_tail_segment_excluded_page_writes_zero_mf_rows(self):
        import main

        path = self._single_page_path()
        try:
            writer, outcome = self._run(path, headless=True)
        finally:
            os.unlink(path)

        self.assertIs(outcome.outcome, main.ProcessOutcome.SUCCESS)
        self.assertEqual(len(writer.audit_rows), 1)
        self.assertEqual(writer.audit_rows[0]["verdict"], "除外")
        self.assertEqual(writer.append_calls, 0)
        self.assertEqual(writer.placeholder_calls, [])

    def test_ui_path_same_input_produces_placeholder_row(self):
        # ④同入力を UI 経路（headless_mode()==False）に流すと占位行になる
        # （envelope_filter が尾段では発火しないため従来どおり認識不能）。
        # 共有 _run（headless=False 分岐）＋共有 FakeWriter を使う——
        # 内聯 mock 脚手架の重複は simcodex R1 で解消済（FakeWriter の
        # placeholder_calls は entries_data を記録する）。
        path = self._single_page_path()
        try:
            writer, _ = self._run(path, headless=False)
        finally:
            os.unlink(path)

        self.assertEqual(len(writer.placeholder_calls), 1)
        self.assertTrue(writer.placeholder_calls[0].get("_unrecognized"))


if __name__ == "__main__":
    unittest.main()
