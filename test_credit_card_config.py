"""config.py とクレカ／交通系IC 3 モジュールの**結線**を検証する。

守りたい実害: 各モジュールは `try: from config import X / except ImportError:`
で設定を読む。この形は**名前を間違えても例外にならず、黙って自前の既定値へ
回帰する**。つまり config を書き換えたのに効いていない、という状態が
テストでも本番でも無音で成立してしまう。

さらに H列（借方インボイス）の写像に穴があると、MF は空欄を「適格」と
解釈する（F-12）ため、判定が付かない行が控除されて**過大控除**になる。
穴は import 時に `invoice_classification._validate_rendering` が落とすが、
「そもそも config 側に項目が在るか」はここで縛る。

外部依存なし（config は os と doc_types のみに依存）:
    python -m unittest test_credit_card_config -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: E402
import card_reconciliation as cr  # noqa: E402
import invoice_classification as ic  # noqa: E402
import page_family as pf  # noqa: E402
from receipt_aggregation import determine_tax_types  # noqa: E402

# Plan §9.5 の 13 項目のうち、**現存する** 11 項目。
# 名前を変えるときはこの表と config の両方を直すこと。
PLAN_SECTION_9_5 = (
    "CREDIT_ADJUST_CREDIT_ACCOUNT",
    "INVOICE_COL_VALUES",
    "INVOICE_COL_RENDERING",
    "INVOICE_CONFIRM_THRESHOLD_YEN",
    "INVOICE_CONFIRM_TAG",
    "TRANSIT_IC_BOOK_CHARGE_ROWS",
    "CREDIT_CARD_DEDUP_MODE",
    "RECON_TOLERANCE_YEN",
    "GEMINI_MAX_OUTPUT_TOKENS_BULK",
    "CC_TAX_TYPE_RENDERING",
    "SMALL_AMOUNT_TAXPAYER_CONFIRMED",
)

# 一度は §9.5 に載ったが**廃止された**項目。単に上の表から消すと、過去設計との
# 追跡が切れるうえ、将来うっかり再追加されても誰も気づかない。
RETIRED_FROM_PLAN_SECTION_9_5 = (
    # 窓分割リトライの廃案（T5 Plan §9.1）。「1 頁 300 行」がカード単位
    # （4 頁合計）の読み違いで、上限 65536 なら実物で一度も発火しないと判明した。
    "CC_WINDOW_SIZE",
    "CC_MAX_WINDOWS",
)


class ConfigItemsExistTest(unittest.TestCase):
    """Plan §9.5 の項目（現存 11 ＋ 廃止 2 ＝ 起案時 13）が揃っていること。"""

    def test_all_current_items_are_present(self):
        missing = [name for name in PLAN_SECTION_9_5 if not hasattr(config, name)]
        self.assertEqual(missing, [], "config.py に未追加: %s" % missing)

    def test_current_and_retired_add_up_to_the_original_thirteen(self):
        both = set(PLAN_SECTION_9_5) | set(RETIRED_FROM_PLAN_SECTION_9_5)
        self.assertEqual(len(both), 13)
        self.assertEqual(
            set(PLAN_SECTION_9_5) & set(RETIRED_FROM_PLAN_SECTION_9_5), set(),
            "現存と廃止の両方に載っている項目がある")


class RetiredItemsTest(unittest.TestCase):
    """廃止した項目が**黙って復活しない**こと。

    読み取り点ゼロの設定が config に残っていると「効いているつもり」の
    誤解を生む。窓分割は設計ごと廃案なので、名前が戻ってきたら気づけるようにする。
    """

    def test_retired_items_are_gone_from_config(self):
        revived = [name for name in RETIRED_FROM_PLAN_SECTION_9_5
                   if hasattr(config, name)]
        self.assertEqual(
            revived, [],
            "廃止済みの設定が復活している。窓分割は T5 Plan §9.1 で廃案: %s"
            % revived)


class WiringTest(unittest.TestCase):
    """読み取り点が在る項目は、**本当に config の値を読んでいる**こと。

    `try/except ImportError` は名前を間違えても静かに既定値へ回帰するので、
    「config を直したのに効かない」が無音で成立する。ここで縛る。
    """

    def test_invoice_column_settings_come_from_config(self):
        self.assertEqual(ic.INVOICE_COL_VALUES, tuple(config.INVOICE_COL_VALUES))
        self.assertEqual(ic.INVOICE_COL_RENDERING, config.INVOICE_COL_RENDERING)
        self.assertEqual(ic.INVOICE_CONFIRM_THRESHOLD_YEN,
                         config.INVOICE_CONFIRM_THRESHOLD_YEN)
        self.assertEqual(ic.INVOICE_CONFIRM_TAG, config.INVOICE_CONFIRM_TAG)

    def test_taxpayer_confirmation_flag_comes_from_config(self):
        self.assertEqual(ic.DEFAULT_TAX_POLICY.small_amount_taxpayer_confirmed,
                         config.SMALL_AMOUNT_TAXPAYER_CONFIRMED)

    def test_reconciliation_settings_come_from_config(self):
        self.assertEqual(cr.RECON_TOLERANCE_YEN, config.RECON_TOLERANCE_YEN)
        self.assertEqual(cr._CFG_BOOK_CHARGE, config.TRANSIT_IC_BOOK_CHARGE_ROWS)

    def test_dedup_mode_comes_from_config(self):
        self.assertEqual(pf._CFG_DEDUP_MODE, config.CREDIT_CARD_DEDUP_MODE)

    def test_dedup_mode_is_a_recognised_value(self):
        """綴り間違いは「重複検出が黙って exclude のまま」になる。"""
        self.assertIn(config.CREDIT_CARD_DEDUP_MODE,
                      (pf.DEDUP_MODE_EXCLUDE, pf.DEDUP_MODE_MARK, pf.DEDUP_MODE_OFF))


class InvoiceColumnSafetyTest(unittest.TestCase):
    """H列の写像に穴が無いこと（穴＝過大控除）。"""

    def test_every_reachable_class_has_a_rendering(self):
        missing = set(ic.REACHABLE_CLASSES) - set(config.INVOICE_COL_RENDERING)
        self.assertEqual(missing, set(), "H列写像の穴: %s" % missing)

    def test_every_rendering_is_a_valid_mf_value(self):
        bad = [v for v in config.INVOICE_COL_RENDERING.values()
               if v not in config.INVOICE_COL_VALUES]
        self.assertEqual(bad, [], "MF 正規値の外: %s" % bad)

    def test_blank_is_permitted_because_the_office_uses_it(self):
        """趙裁定 9: H列は空欄（事務所慣例の踏襲）。空欄が許可値に在ること。"""
        self.assertIn("", config.INVOICE_COL_VALUES)

    def test_transitional_classes_are_deliberately_absent(self):
        """経過措置は判定木から到達しない。写像に載せると到達可能に見える。"""
        for cls in (ic.DeductionClass.TRANSITIONAL_80, ic.DeductionClass.TRANSITIONAL_50):
            with self.subTest(cls=cls):
                self.assertNotIn(cls, config.INVOICE_COL_RENDERING)

    def test_confirmation_threshold_matches_the_small_amount_boundary(self):
        """U列タグの閾値と少額特例の金額要件が同じ境界を指していること。

        ずれると「特例圏外なのにタグが付かない」行が生まれる。
        """
        self.assertEqual(config.INVOICE_CONFIRM_THRESHOLD_YEN,
                         ic.DEFAULT_TAX_POLICY.small_amount_threshold_yen)


class TaxTypeRenderingTest(unittest.TestCase):
    """税区分の省略名変換キーが、実在する正式名と一致すること。

    キーが 1 文字でもずれると変換が起きず、MF へ正式名がそのまま出る
    （`課対仕入8% (軽)` の括弧前の半角スペースが実際の落とし穴）。
    """

    def test_keys_are_the_actual_formal_names(self):
        formal = {determine_tax_types("credit_card", 0.10)[0],
                  determine_tax_types("receipt", 0.08)[0]}
        self.assertEqual(set(config.CC_TAX_TYPE_RENDERING), formal)

    def test_exempt_label_is_not_rewritten(self):
        """`対象外` は変換対象に含めない。

        anomaly_detector が `== "対象外"` の精確等値で判定しているため、
        省略名に変えると既存の規則①が静かに壊れる。
        """
        self.assertNotIn("対象外", config.CC_TAX_TYPE_RENDERING)


class UnwiredItemsTest(unittest.TestCase):
    """まだ読み取り点が無い項目は、その事実を明示しておく。

    config に書いただけでは効かない（T4 / T5 の実装時に読み取りも入れる）。
    このテストは「いつの間にか結線された」ことを検知して、
    ここの一覧と Plan §9.5 の注記を更新させるための番人である。
    """

    # T4 で `CREDIT_ADJUST_CREDIT_ACCOUNT` が結線されたのでこの一覧から外した
    # （`card_entries` が読み、`test_card_entries` が変異で効き目を確認済み）。
    # T5 で `GEMINI_MAX_OUTPUT_TOKENS_BULK` が結線された（`ocr_engine` の
    # `_line_generation_config` が読み、`test_ocr_engine_line_budget` が
    # 「BULK=0 が予算 0 として SDK へ渡らない」ところまで固定している）。
    # 窓 2 項は廃案で config ごと削除（`RetiredItemsTest`）。
    # `CC_TAX_TYPE_RENDERING` は**意図的に残す** —— builder は canonical の
    # 税区分を出し、省略名への変換は T6 の出力層で行う（AD-11）。
    UNWIRED = ("CC_TAX_TYPE_RENDERING",)

    # 結線されうる実装モジュール。T6 で `sheets_output.py` を足すこと
    WATCHED = ("page_family.py", "card_reconciliation.py",
               "invoice_classification.py", "page_dedup.py",
               "card_entries.py", "card_prompts.py", "ocr_engine.py")

    def _watched_sources(self):
        import pathlib

        return "\n".join(
            pathlib.Path(os.path.dirname(__file__), name).read_text(encoding="utf-8")
            for name in self.WATCHED)

    def test_unwired_items_are_still_unread_by_the_implementation(self):
        sources = self._watched_sources()
        # `from config import X` だけでなく `config.X` も見る —— 後者の書き方で
        # 結線されると番人がすり抜け、実装と一覧の食い違いが無音で固定される
        newly_wired = [n for n in self.UNWIRED
                       if ("from config import %s" % n in sources
                           or "config.%s" % n in sources)]
        self.assertEqual(
            newly_wired, [],
            "結線された項目がある。この一覧と Plan §9.5 の「読み取り点が未実装」"
            "注記を更新すること: %s" % newly_wired)

    def test_the_watchdog_can_actually_see_a_wired_item(self):
        """番人が本当に噛むこと（一覧を空にしただけで緑になる番人を作らない）。"""
        # Assert: T4 で実際に結線された項目は、番人の視野に入っている
        self.assertIn("from config import CREDIT_ADJUST_CREDIT_ACCOUNT",
                      self._watched_sources())

    def test_bulk_token_limit_is_the_measured_hard_ceiling(self):
        """65536 = gemini-2.5-flash の出力硬上限（実測 ＋ 趙拍板 2026-08-18）。

        起案時は「実測前に数値を入れない」ための 0 だった。実測
        （`genai.list_models()` で硬上限 65536、`count_tokens` で 134 tok/行）
        と趙の拍板で条件が満たされたので値を入れている。

        **値効果（本当に SDK へ渡るか）はここでは見ない** ——
        このファイルは config と純粋モジュールだけに依存する契約で、
        `ocr_engine` を import した瞬間に venv311 必須へ劣化する。
        値効果は `test_ocr_engine_line_budget` の担当。
        """
        self.assertEqual(config.GEMINI_MAX_OUTPUT_TOKENS_BULK, 65536)


if __name__ == "__main__":
    unittest.main()
