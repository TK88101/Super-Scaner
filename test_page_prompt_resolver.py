"""T3-b: 頁ごとの prompt 解決（`ocr_engine._resolve_page_prompt`）。

Plan: `docs/plans/2026-08-17-t3-page-ocr-resolver.md` §5 T3-b。

この関数が守る契約:

1. **prompt と builder は必ず同源**（AD-T3-1）。`actual_doc_type` は
   `PROMPTS` と `ENTRY_BUILDERS` の両方を引くキーなので、片方だけ
   フォールバックさせて異種混成を作ってはいけない。
2. **既存 4 doc_type の prompt は絶対に変わらない**。混載分流は新 2 型の
   相互取り違えに限る（`page_family.select_prompt_doc_type` の第 1 分岐）。
3. **空 prompt を返さない**。folder の prompt すら引けない場合は
   `PageOcr` を組ませないため `ValueError` を送出する（AD-T3-2）。

OCR テキストの標本は `ocr_test_fixtures` の正本を使う（自前で作ると
「そもそも強シグナルになっていない」テストが緑になりうる）。`SampleSanityTest`
が標本の強度そのものを先に確かめているのはそのため。

venv311 必須（ocr_engine が paddleocr / google.generativeai を引くため）:
    venv311/bin/python -m unittest test_page_prompt_resolver -v
"""
import unittest
from unittest import mock

import ocr_engine
import page_family
from doc_types import DocType
from ocr_test_fixtures import AMEX_HEAD, NIMOCA_HEAD, ic_rows


_NIMOCA_STRONG = NIMOCA_HEAD + " " + ic_rows(20)
_CREDIT_STRONG = AMEX_HEAD
_BOTH_STRONG = _NIMOCA_STRONG + " " + AMEX_HEAD

_EXISTING_DOC_TYPES = (
    DocType.RECEIPT,
    DocType.PURCHASE_INVOICE,
    DocType.SALES_INVOICE,
    DocType.SALARY_SLIP,
)


class SampleSanityTest(unittest.TestCase):
    """標本が本当に狙った強度になっていることを先に確かめる。

    これが無いと、以下の全テストが「シグナルが立っていないので folder の
    ままだった」という理由で緑になり、何も検証していない状態に陥る。
    """

    def test_nimoca_sample_is_ic_strong(self):
        pc = page_family.classify_page(_NIMOCA_STRONG)
        self.assertEqual(pc.ic_strength, page_family.STRENGTH_STRONG)

    def test_amex_sample_is_cc_strong(self):
        pc = page_family.classify_page(_CREDIT_STRONG)
        self.assertEqual(pc.cc_strength, page_family.STRENGTH_STRONG)

    def test_both_sample_is_strong_on_both_axes(self):
        pc = page_family.classify_page(_BOTH_STRONG)
        self.assertEqual(pc.ic_strength, page_family.STRENGTH_STRONG)
        self.assertEqual(pc.cc_strength, page_family.STRENGTH_STRONG)


class MixedFolderRoutingTest(unittest.TestCase):
    """混載フォルダ（趙裁定 5）の頁単位分流。"""

    def test_nimoca_page_in_card_folder_gets_transit_ic_prompt(self):
        # Arrange / Act: クレカフォルダに nimoca 頁が混ざっている
        actual, prompt, _pc, signal = ocr_engine._resolve_page_prompt(
            DocType.CREDIT_CARD, _NIMOCA_STRONG)

        # Assert: prompt も doc_type も transit_ic へ切り替わる
        self.assertEqual(actual, DocType.TRANSIT_IC)
        self.assertEqual(prompt, ocr_engine.PROMPTS[DocType.TRANSIT_IC])
        self.assertIn("family_override", signal)

    def test_card_page_in_card_folder_stays_credit_card(self):
        # Arrange / Act
        actual, prompt, _pc, signal = ocr_engine._resolve_page_prompt(
            DocType.CREDIT_CARD, _CREDIT_STRONG)

        # Assert
        self.assertEqual(actual, DocType.CREDIT_CARD)
        self.assertEqual(prompt, ocr_engine.PROMPTS[DocType.CREDIT_CARD])
        self.assertIsNone(signal)

    def test_both_strong_trusts_the_folder(self):
        # Arrange / Act: 両軸 strong は人間の明示的行為（フォルダ）を信じる
        actual, prompt, _pc, signal = ocr_engine._resolve_page_prompt(
            DocType.CREDIT_CARD, _BOTH_STRONG)

        # Assert
        self.assertEqual(actual, DocType.CREDIT_CARD)
        self.assertEqual(prompt, ocr_engine.PROMPTS[DocType.CREDIT_CARD])
        self.assertEqual(signal, "family_ambiguous")


class ExistingDocTypesAreUntouchedTest(unittest.TestCase):
    """既存 4 型は nimoca 頁が混ざっても prompt を変えない（本番不可侵）。"""

    def test_all_existing_doc_types_keep_their_prompt(self):
        for folder in _EXISTING_DOC_TYPES:
            with self.subTest(folder=folder):
                # Arrange / Act
                actual, prompt, _pc, signal = ocr_engine._resolve_page_prompt(
                    folder, _NIMOCA_STRONG)

                # Assert: doc_type も prompt も folder のまま
                self.assertEqual(actual, folder)
                self.assertEqual(prompt, ocr_engine.PROMPTS[folder])
                # シグナルだけは記録する（T9 が消費する）
                self.assertEqual(signal, "family_mismatch:ic_history")

    def test_existing_doc_types_with_card_page_emit_no_signal(self):
        for folder in _EXISTING_DOC_TYPES:
            with self.subTest(folder=folder):
                actual, prompt, _pc, signal = ocr_engine._resolve_page_prompt(
                    folder, _CREDIT_STRONG)
                self.assertEqual(actual, folder)
                self.assertEqual(prompt, ocr_engine.PROMPTS[folder])
                self.assertIsNone(signal)


class NoOcrTextTest(unittest.TestCase):
    """OCR テキストが無い頁（Vision 兜底・PaddleOCR 失敗）。"""

    def test_empty_text_falls_back_to_folder(self):
        actual, prompt, pc, signal = ocr_engine._resolve_page_prompt(
            DocType.CREDIT_CARD, "")
        self.assertEqual(actual, DocType.CREDIT_CARD)
        self.assertEqual(prompt, ocr_engine.PROMPTS[DocType.CREDIT_CARD])
        self.assertIsNone(signal)
        self.assertIsInstance(pc, page_family.PageClass)

    def test_none_text_does_not_raise(self):
        actual, prompt, _pc, signal = ocr_engine._resolve_page_prompt(
            DocType.CREDIT_CARD, None)
        self.assertEqual(actual, DocType.CREDIT_CARD)
        self.assertEqual(prompt, ocr_engine.PROMPTS[DocType.CREDIT_CARD])
        self.assertIsNone(signal)


class PromptAndBuilderStaySynchronizedTest(unittest.TestCase):
    """PROMPTS 登録漏れ時、actual と prompt を**セットで**戻す（AD-T3-1）。

    片方だけ戻すと「prompt=credit_card / builder=transit_ic」という
    異種混成になり、Gemini の出力構造を別の型の builder が読む。
    """

    def test_missing_target_prompt_reverts_both_actual_and_prompt(self):
        # Arrange: transit_ic の prompt が登録されていない状況を作る
        prompts_without_ic = {
            k: v for k, v in ocr_engine.PROMPTS.items()
            if k != DocType.TRANSIT_IC
        }
        with mock.patch.object(ocr_engine, "PROMPTS", prompts_without_ic):
            # Act: nimoca 頁なので本来 transit_ic へ行きたいが prompt が無い
            actual, prompt, _pc, signal = ocr_engine._resolve_page_prompt(
                DocType.CREDIT_CARD, _NIMOCA_STRONG)

            # Assert: doc_type も prompt も folder へ戻る（片側だけ戻らない）
            self.assertEqual(actual, DocType.CREDIT_CARD)
            self.assertEqual(prompt, prompts_without_ic[DocType.CREDIT_CARD])
        # 無音で縮退しない
        self.assertIn("prompt_fallback", signal)


class InvariantPromptIsNeverEmptyTest(unittest.TestCase):
    """AD-T3-2: 空 prompt を返す経路が存在しない。"""

    def test_prompt_is_non_empty_for_every_registered_doc_type(self):
        texts = ("", None, _NIMOCA_STRONG, _CREDIT_STRONG, _BOTH_STRONG,
                 "まったく無関係な文書 ご案内")
        for folder in DocType.ALL:
            for text in texts:
                with self.subTest(folder=folder, text=str(text)[:20]):
                    _actual, prompt, _pc, _signal = (
                        ocr_engine._resolve_page_prompt(folder, text))
                    self.assertTrue(
                        prompt,
                        "folder=%s text=%r で prompt が空になった" % (folder, text))

    def test_unknown_folder_doc_type_raises_instead_of_returning_none(self):
        # Arrange / Act / Assert: None を返すと呼出側で AttributeError が
        # 別の場所で出て原因が追えない。発生地点＝原因地点にする。
        with self.assertRaises(ValueError):
            ocr_engine._resolve_page_prompt("no_such_doc_type", _CREDIT_STRONG)


if __name__ == "__main__":
    unittest.main()
