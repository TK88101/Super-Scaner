"""`card_entries`（T4-c/d/e）の単体テスト。

**venv 無しで通ること**が設計上の性質（母 Plan §4 / `test_dependency_weight`）:

    python3 -m unittest test_card_entries -v

金額の期待値は事実台帳 F-4 の黄金値（17,295 / 933,680 / 15,503）。
これらは「明細相加 = 印字合計」が実券面で成立することを目視確認した数字で、
負数行を含めて初めて一致する（`f464179` の負号バグはここで捕まる）。
"""
import copy
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import card_entries                                          # noqa: E402
import card_prompts                                          # noqa: E402
import card_reconciliation as recon                          # noqa: E402
import invoice_classification as invc                        # noqa: E402
import page_dedup                                            # noqa: E402
from doc_types import DocType                                # noqa: E402
from ocr_test_fixtures import (                              # noqa: E402
    AMEX_A_P1_OCR, AMEX_A_P1_RAW, AMEX_A_P2_OCR, AMEX_A_P2_RAW,
    AMEX_B_P5_OCR, AMEX_B_P5_RAW, AMEX_B_RETURN_RAW,
    ENEOS_P1_POINT_SECTION_RAW, ENEOS_P2_OCR, ENEOS_P2_RAW,
    JCB_FX_NO_JPY_RAW, JCB_FX_RAW,
    NIMOCA_P1_OCR, NIMOCA_P1_RAW, TSCUBIC_ETC_P6_RAW, TSCUBIC_FUEL_P4_RAW,
    UC_P2_OCR, UC_P2_RAW, _cc_row,
)

CC = DocType.CREDIT_CARD
IC = DocType.TRANSIT_IC


def _ledger(doc_type, pages, total_pages=None):
    """(raw, page_num) の並びを元帳へ流して結算する。"""
    ledger = recon.FileReconLedger(doc_type, total_pages or len(pages))
    for raw, page_num in pages:
        ledger.observe_page(
            page_num,
            card_entries.card_ident_from_raw(raw),
            card_entries.printed_totals_from_raw(raw),
            card_entries.detail_lines_from_raw(raw, doc_type),
        )
    return ledger.finalize()


# ── T4-c: DTO 変換（prompt schema の十分性の証明）──────────────────
class DtoConversionTest(unittest.TestCase):

    def test_card_ident_splits_statement_page(self):
        # Arrange / Act
        ident = card_entries.card_ident_from_raw(AMEX_A_P1_RAW)

        # Assert: "1/6" は n/N へ分解される（プロンプトには逐語 1 本だけ出させる）
        self.assertEqual(ident.statement_page_n, 1)
        self.assertEqual(ident.statement_page_total, 6)
        self.assertEqual(ident.member_no, "****-******-26003")

    def test_card_ident_survives_missing_page_label(self):
        # Arrange / Act: nimoca には頁ラベルが無い
        ident = card_entries.card_ident_from_raw(NIMOCA_P1_RAW)

        # Assert: 例外を出さず None で返す（元帳側が位置推定へ倒す）
        self.assertIsNone(ident.statement_page_n)
        self.assertEqual(ident.issuer, "nimoca")

    def test_printed_totals_keep_handwritten_flag(self):
        # Arrange
        raw = {**AMEX_A_P1_RAW, "printed_totals": [
            {"label": "ご請求金額", "amount": 12940, "count": None,
             "page": 1, "is_handwritten": True}]}

        # Act
        totals = card_entries.printed_totals_from_raw(raw)

        # Assert: F-10 の手書き「ETC代 12,940円」を基準値に採らせないための旗
        self.assertEqual(len(totals), 1)
        self.assertTrue(totals[0].is_handwritten)

    def test_detail_line_section_is_a_string_constant(self):
        """`sec` の数値をそのまま section に入れない（K10）。"""
        # Act
        lines = card_entries.detail_lines_from_raw(AMEX_B_P5_RAW, CC)

        # Assert
        for line in lines:
            self.assertIsInstance(line.section, str)
            self.assertIn(line.section, (recon.SECTION_CURRENT_USAGE,
                                         recon.SECTION_PAYMENT_SUMMARY,
                                         recon.SECTION_POINT,
                                         recon.SECTION_UNKNOWN))

    def test_section_headings_resolve_not_unknown(self):
        """F-5 の 2 区画が両方 UNKNOWN に落ちない（K11。Codex 複審 P1）。"""
        # Assert: 合計ラベル表（"今月ご利用額合計"）では引けない見出し
        self.assertEqual(card_entries.section_for_heading("今月ご利用額"),
                         recon.SECTION_CURRENT_USAGE)
        self.assertEqual(card_entries.section_for_heading("お支払い金額内容"),
                         recon.SECTION_PAYMENT_SUMMARY)
        self.assertEqual(card_entries.section_for_heading("ご利用明細"),
                         recon.SECTION_CURRENT_USAGE)
        self.assertEqual(card_entries.section_for_heading("このカードのポイント明細"),
                         recon.SECTION_POINT)
        self.assertEqual(card_entries.section_for_heading("謎の見出し"),
                         recon.SECTION_UNKNOWN)

    def test_carry_over_line_is_forced_into_payment_summary(self):
        """AD-4 の保険: Gemini の sec 申告に関わらず繰越は別区画へ。"""
        # Arrange: sec=1（今月ご利用額）と偽って申告させる
        raw = {**AMEX_B_P5_RAW, "rows": [
            {**AMEX_B_P5_RAW["rows"][0], "sec": 1}]}

        # Act
        lines = card_entries.detail_lines_from_raw(raw, CC)

        # Assert
        self.assertEqual(lines[0].section, recon.SECTION_PAYMENT_SUMMARY)

    def test_fingerprint_is_eligible_for_dedup(self):
        """訂正 3: schema がずれていると重複判定が**無症状で**死ぬ。"""
        # Act
        fp = page_dedup.safe_fingerprint(AMEX_A_P1_OCR, AMEX_A_P1_RAW)

        # Assert
        self.assertIsNotNone(fp)
        self.assertTrue(fp.is_eligible(),
                        "重複判定の資格を失っている（identity=%r digest=%r）"
                        % (fp.identity, fp.digest[:8]))

    def test_prompt_schema_covers_every_key_the_builder_reads(self):
        """prompt と消費側が同じ正本を見ていること（B15）。"""
        # Assert: 消費側の宣言が prompt の schema に収まっている
        for declared, allowed, label in (
                (card_entries.CONSUMED_ROW_KEYS, card_prompts.ALL_ROW_KEYS, "row"),
                (card_entries.CONSUMED_CARD_KEYS, card_prompts.ALL_CARD_KEYS, "card"),
                (card_entries.CONSUMED_TOP_KEYS, card_prompts.ALL_TOP_KEYS, "top")):
            with self.subTest(scope=label):
                self.assertTrue(
                    set(declared) <= allowed,
                    "prompt に無いキーを builder が読んでいる(%s): %s"
                    % (label, sorted(set(declared) - allowed)))

    def test_consumed_key_constants_match_what_the_code_actually_reads(self):
        """宣言と実装の突合（Codex 評審 R1-#5）。

        `CONSUMED_*_KEYS` を手で書いただけでは**何も検証していない** ——
        builder が新しいキーを読み始めても、定数を更新しなければ上の
        テストは緑のままになる。AST で実際の `.get("...")` を数える。
        """
        # Arrange
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path(card_entries.__file__).read_text(
            encoding="utf-8"))
        actual = {"row": set(), "card": set(), "raw": set()}

        def _scope_of(call):
            """`row.get(..)` / `card.get(..)` / `_dict(raw_data).get(..)` を分類。"""
            recv = call.func.value
            if isinstance(recv, ast.Name):
                return recv.id if recv.id in ("row", "card") else None
            if (isinstance(recv, ast.Call) and recv.args
                    and isinstance(recv.args[0], ast.Name)
                    and recv.args[0].id == "raw_data"):
                return "raw"
            return None

        def _literal_keys(call, comprehensions):
            """引数がリテラルならそれ、変数なら内包表記の `for k in (...)` を辿る。

            `str(row.get(k) or "") for k in ("place_from", "place_to")` という
            書き方をリテラル走査だけで拾うと**取りこぼす** —— しかも
            「宣言したが読んでいない」に見えてしまう（実際 place_from /
            place_to がそう誤検出された）。
            """
            arg = call.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return [arg.value]
            if not isinstance(arg, ast.Name):
                return []
            keys = []
            for comp in comprehensions:
                for gen in comp.generators:
                    if (isinstance(gen.target, ast.Name)
                            and gen.target.id == arg.id
                            and isinstance(gen.iter, (ast.Tuple, ast.List))):
                        keys.extend(e.value for e in gen.iter.elts
                                    if isinstance(e, ast.Constant)
                                    and isinstance(e.value, str))
            return keys

        # 内包表記は入れ子になりうるので、親を辿れるよう先に対応表を作る
        comps_of = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp,
                                 ast.DictComp)):
                for child in ast.walk(node):
                    comps_of.setdefault(id(child), []).append(node)

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and node.args):
                continue
            scope = _scope_of(node)
            if scope:
                actual[scope].update(
                    _literal_keys(node, comps_of.get(id(node), [])))

        # Assert: 宣言と実装が**双方向で**一致すること。片方向（実装 ⊆ 宣言）
        # だけだと「宣言に書いたが実際は読んでいない」に沈黙する ——
        # 実際 `period` がその状態だった（Round 2 評審）
        for scope, declared in (("row", card_entries.CONSUMED_ROW_KEYS),
                                ("card", card_entries.CONSUMED_CARD_KEYS),
                                ("raw", card_entries.CONSUMED_TOP_KEYS)):
            with self.subTest(scope=scope):
                self.assertEqual(
                    actual[scope], set(declared),
                    "CONSUMED_*_KEYS と実装が食い違う。読んでいるのに未宣言=%s / "
                    "宣言したが読んでいない=%s"
                    % (sorted(actual[scope] - set(declared)),
                       sorted(set(declared) - actual[scope])))
        # 番人が実際に何かを見ていること（走査が空振りしていない証明）
        self.assertGreater(len(actual["row"]), 5)

    def test_statement_page_parsers_disagree_only_in_the_safe_direction(self):
        """`page_dedup` は 2 桁まで、こちらは 3 桁まで受ける（Reuse 評審 R1-#2）。

        3 桁の頁番号（実測には無い。UC/JCB でも 2 桁）で解釈が割れる:
        こちらは `100` を得るが、`page_dedup` は `(\\d{1,2})/(\\d{1,2})` で
        **前方から 2 桁だけ拾って `00/12`** になる（取得失敗ではない）。

        これが安全側である理由: 重複判定は**二重署名 AND**（識別キー一致
        **かつ** 内容ダイジェスト一致）なので、頁ラベルが潰れて別頁と
        衝突しても、明細の内容が違えば重複にはならない（AD-1）。
        逆に「頁ラベルが取れない」より「潰れて衝突する」方が危ないので、
        差が在ること自体をここで固定し、将来 `page_dedup` 側の正則が
        変わったら気づけるようにする。
        """
        # Arrange
        raw = {"card": {**AMEX_A_P1_RAW["card"], "statement_page": "100/120"}}

        # Act
        ident = card_entries.card_ident_from_raw(raw)
        label = page_dedup._normalize_page_label("100/120")

        # Assert
        self.assertEqual(ident.statement_page_n, 100)
        self.assertEqual(label, "00/12",
                         "page_dedup 側の解釈が変わった。二重署名 AND が"
                         "効いているかを確認すること（AD-1）")


# ── 検算（記帳集合と検算集合は別軸。AD-5）──────────────────────────
class ReconciliationGoldenTest(unittest.TestCase):

    def test_amex_card_a_detail_sum_is_17295(self):
        # Act: F-4 の p1(630×4) + p2(6 行)
        verdicts = _ledger(CC, [(AMEX_A_P1_RAW, 1), (AMEX_A_P2_RAW, 2)])

        # Assert
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0].detail_sum, 17295)
        self.assertEqual(verdicts[0].printed_total, 17295)
        self.assertEqual(verdicts[0].verdict, recon.VERDICT_MATCH)

    def test_amex_card_b_excludes_carry_over_from_current_usage(self):
        """F-5: 前回分口座振替 −35,808 を混ぜると 933,680 が崩れる。"""
        # Act
        verdicts = _ledger(CC, [(AMEX_B_P5_RAW, 5)])

        # Assert
        self.assertEqual(verdicts[0].detail_sum, 933680)
        self.assertEqual(verdicts[0].verdict, recon.VERDICT_MATCH)

    def test_eneos_golden_15503_requires_the_negative_row(self):
        """F-4: 7,380 + 11,123 − 3,000 = 15,503。"""
        # Act
        verdicts = _ledger(CC, [(ENEOS_P2_RAW, 2)])

        # Assert
        self.assertEqual(verdicts[0].detail_sum, 15503)
        self.assertEqual(verdicts[0].verdict, recon.VERDICT_MATCH)

    def test_eneos_expense_sum_is_18503(self):
        """受入基準 A3: 記帳集合は credit_adjust を含まない別軸。"""
        # Act
        verdicts = _ledger(CC, [(ENEOS_P2_RAW, 2)])

        # Assert
        self.assertEqual(verdicts[0].expense_sum, 18503)

    def test_point_section_rows_do_not_pollute_the_detail_sum(self):
        """ポイント区画が同居しても F-4 の 15,503 が汚れないこと。

        **この不変式は三重に守られている**（変異検証で確認した実測）:
        (1) `section_for_heading` が見出しを引く
        (2) 外れたら `section_for_label` へ回帰する —— 実際
            「このカードのポイント明細」は合計ラベル表にも登録されている
        (3) それも外れて SECTION_UNKNOWN になっても、`_verdict_for` が
            unknown 区画を detail_sum に足すため**金額は変わらず** note が付く

        つまり K11（見出し解決の失敗）は、現行の券面構造では
        金額の誤りではなく note の雑音として現れる。本テストは
        「どの層が効いているか」ではなく**結果の不変式**を固定する。
        """
        # Act
        verdicts = _ledger(CC, [(ENEOS_P1_POINT_SECTION_RAW, 1)])

        # Assert
        self.assertEqual(verdicts[0].detail_sum, 15503)
        self.assertEqual(verdicts[0].verdict, recon.VERDICT_MATCH)

    def test_unclassified_negative_keeps_its_sign_in_the_ledger(self):
        """記帳側は占位でも、検算側は符号を保つ（AD-T4-3）。"""
        # Act
        lines = card_entries.detail_lines_from_raw(AMEX_B_RETURN_RAW, CC)

        # Assert
        self.assertEqual(lines[0].amount, -1200)

    def test_nimoca_counts_only(self):
        """F-7: nimoca に合計金額は無い。件数だけ検算する。"""
        # Act
        verdicts = _ledger(IC, [(NIMOCA_P1_RAW, 1)])

        # Assert: 印字 120 件に対し当頁は 4 件 → 件数不一致（1 頁だけ流したため）
        self.assertEqual(verdicts[0].printed_count, 120)
        self.assertEqual(verdicts[0].detail_count, 4)
        self.assertEqual(verdicts[0].verdict, recon.VERDICT_COUNT_NG)


# ── T4-d: クレカ builder ──────────────────────────────────────────
class CreditCardBuilderTest(unittest.TestCase):

    def test_no_negative_amount_reaches_the_ledger_entries(self):
        # Act
        entries = card_entries.build_entries_from_credit_card(ENEOS_P2_RAW)

        # Assert
        self.assertTrue(all(e["amount"] >= 0 for e in entries),
                        [e["amount"] for e in entries])

    def test_point_cashback_becomes_credit_adjust(self):
        """AD-5: 借方 未払金 ／ 貸方 雑収入、金額は abs。"""
        # Act
        entries = card_entries.build_entries_from_credit_card(ENEOS_P2_RAW)
        adjust = [e for e in entries
                  if e["_booking_kind"] == recon.KIND_CREDIT_ADJUST]

        # Assert
        self.assertEqual(len(adjust), 1)
        self.assertEqual(adjust[0]["debit_account"], "未払金")
        self.assertEqual(adjust[0]["credit_account"], "雑収入")
        self.assertEqual(adjust[0]["amount"], 3000)
        self.assertEqual(adjust[0]["debit_tax_type"], "対象外")
        self.assertEqual(adjust[0]["credit_tax_type"], "対象外")

    def test_credit_adjust_account_comes_from_config(self):
        """config の結線の証明（try/except は名前を間違えても静かに既定へ戻る）。"""
        # Arrange
        original = card_entries.CREDIT_ADJUST_CREDIT_ACCOUNT
        card_entries.CREDIT_ADJUST_CREDIT_ACCOUNT = "雑益テスト"
        try:
            # Act
            entries = card_entries.build_entries_from_credit_card(ENEOS_P2_RAW)
            adjust = [e for e in entries
                      if e["_booking_kind"] == recon.KIND_CREDIT_ADJUST]

            # Assert
            self.assertEqual(adjust[0]["credit_account"], "雑益テスト")
        finally:
            card_entries.CREDIT_ADJUST_CREDIT_ACCOUNT = original

    def test_carry_over_is_not_booked_but_summarized(self):
        # Act
        entries = card_entries.build_entries_from_credit_card(AMEX_B_P5_RAW)
        summary = card_entries.summarize_nonbookable(AMEX_B_P5_RAW, CC)

        # Assert
        self.assertEqual(len(entries), 4)
        self.assertNotIn(recon.KIND_CARRY_OVER,
                         [e["_booking_kind"] for e in entries])
        self.assertIn("35,808", summary)
        self.assertIn("1件", summary)

    def test_unclassified_negative_is_a_placeholder_not_income(self):
        """K8: 返品を「貸方 雑収入」にすると収益が無症状で過大になる。"""
        # Act
        entries = card_entries.build_entries_from_credit_card(AMEX_B_RETURN_RAW)

        # Assert
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["amount"], 0)
        self.assertEqual(entries[0]["_placeholder_reason"], "unclassified_negative")
        self.assertNotEqual(entries[0]["credit_account"], "雑収入")
        self.assertNotEqual(entries[0]["_booking_kind"], recon.KIND_CREDIT_ADJUST)

    def test_positive_credit_adjust_declaration_is_not_trusted(self):
        """Codex 評審 R1-#2: Gemini が負号を落とすと雑収入が過大になる。

        ポイント充当は「当月利用額の減額」なので券面上は必ず負である。
        `kind: credit_adjust` かつ正数は**申告の矛盾**であって、
        信じて記帳してよい情報ではない。
        """
        # Arrange: 負号だけが落ちた形
        raw = {**ENEOS_P2_RAW, "rows": [
            {**ENEOS_P2_RAW["rows"][2], "amount": 3000, "kind": "credit_adjust"}]}

        # Act
        entries = card_entries.build_entries_from_credit_card(raw)

        # Assert
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["amount"], 0)
        self.assertEqual(entries[0]["_placeholder_reason"],
                         "credit_adjust_sign_conflict")
        self.assertNotEqual(entries[0]["credit_account"], "雑収入")

    def test_unknown_gemini_account_never_reaches_the_debit_column(self):
        """Codex 評審 R1-#1: `ACCOUNT_MAP.get(x, x)` は未知キーを潰さない。

        `account_hint`（手書き）だけ白名単にしても、Gemini の推定科目が
        素通りするなら「架空費」が MF の勘定科目列に出る。
        """
        # Arrange
        raw = {**JCB_FX_RAW, "rows": [
            {**JCB_FX_RAW["rows"][0], "debit_account": "架空費"},
            {**JCB_FX_RAW["rows"][1], "debit_account": "通信費"},
        ]}

        # Act
        entries = card_entries.build_entries_from_credit_card(raw)

        # Assert: 未知は既定科目へ、既知はそのまま
        self.assertNotIn("架空費", [e["debit_account"] for e in entries])
        self.assertEqual(entries[0]["debit_account"], "備品・消耗品費")
        self.assertEqual(entries[1]["debit_account"], "通信費")

    def test_rejected_ai_account_is_not_thrown_away(self):
        """却下した AI 推定を無痕跡で消さない（Round 2 評審）。

        `account_hint` が解決できなければ逐語を memo に残すのに、
        Gemini の推定を白名単で弾いたときだけ痕跡が消えると、
        `ACCOUNT_MAP` に無い正当な科目（分割払手数料の `支払利息` 等）が
        在ったことに誰も気づけない。
        """
        # Arrange
        raw = {**JCB_FX_RAW, "rows": [
            {**JCB_FX_RAW["rows"][0], "debit_account": "支払利息"}]}

        # Act
        entries = card_entries.build_entries_from_credit_card(raw)

        # Assert
        self.assertEqual(entries[0]["_debit_account_rejected"], "支払利息")
        self.assertIn("支払利息", entries[0]["memo"])
        self.assertEqual(entries[0]["debit_account"], "備品・消耗品費")

    def test_rejected_ai_account_is_recorded_even_when_a_hint_wins(self):
        """Codex 評審 R3: ヒントが解決しても却下の痕跡は残す。

        「Gemini が実在しない科目名を出した」という事実の価値は、
        その推定が採用されたかどうかとは独立している。
        """
        # Arrange: 手書きは解決でき、かつ Gemini の推定は未登録科目
        raw = {**JCB_FX_RAW, "rows": [
            {**JCB_FX_RAW["rows"][0], "account_hint": "通信費として",
             "debit_account": "支払利息"}]}

        # Act
        entries = card_entries.build_entries_from_credit_card(raw)

        # Assert: 科目は手書きが勝つが、却下の事実も残る
        self.assertEqual(entries[0]["debit_account"], "通信費")
        self.assertTrue(entries[0]["_account_hint_used"])
        self.assertEqual(entries[0]["_debit_account_rejected"], "支払利息")
        self.assertIn("支払利息", entries[0]["memo"])

    def test_accepted_ai_account_leaves_no_rejection_trace(self):
        # Act
        entries = card_entries.build_entries_from_credit_card(
            {**JCB_FX_RAW, "rows": [
                {**JCB_FX_RAW["rows"][0], "debit_account": "通信費"}]})

        # Assert
        self.assertEqual(entries[0]["_debit_account_rejected"], "")
        self.assertNotIn("AI推定科目", entries[0]["memo"])

    def test_revenue_account_from_gemini_is_rejected_too(self):
        # Arrange
        raw = {**JCB_FX_RAW, "rows": [
            {**JCB_FX_RAW["rows"][0], "debit_account": "売上高"}]}

        # Act
        entries = card_entries.build_entries_from_credit_card(raw)

        # Assert
        self.assertEqual(entries[0]["debit_account"], "備品・消耗品費")

    def test_string_sec_still_resolves_the_section(self):
        """Codex 評審 R1-#4: Gemini は "1" を返すことがある。"""
        # Arrange
        raw = {**AMEX_B_P5_RAW, "rows": [
            {**AMEX_B_P5_RAW["rows"][1], "sec": "1"}]}

        # Act
        lines = card_entries.detail_lines_from_raw(raw, CC)

        # Assert
        self.assertEqual(lines[0].section, recon.SECTION_CURRENT_USAGE)

    def test_foreign_currency_books_the_jpy_amount(self):
        """F-9 / AD-10: 57.60 を 円 として記帳する事故を構造的に防ぐ。"""
        # Act
        entries = card_entries.build_entries_from_credit_card(JCB_FX_RAW)
        amounts = [e["amount"] for e in entries]

        # Assert
        self.assertEqual(amounts, [9238, 3494, 1578])
        for forbidden in (57, 58, 22, 23, 14, 15):
            self.assertNotIn(forbidden, amounts)

    def test_foreign_currency_detail_goes_to_the_description(self):
        # Act
        entries = card_entries.build_entries_from_credit_card(JCB_FX_RAW)

        # Assert
        self.assertIn("57.60USD", entries[0]["description"])
        self.assertIn("160.384", entries[0]["description"])

    def test_row_without_jpy_amount_becomes_a_placeholder(self):
        """AD-T4-8: 行を捨てない（IP-401 の思想を行に適用）。"""
        # Act
        entries = card_entries.build_entries_from_credit_card(JCB_FX_NO_JPY_RAW)

        # Assert
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["amount"], 0)
        self.assertEqual(entries[0]["_placeholder_reason"], "no_jpy_amount")

    def test_h_column_is_always_empty(self):
        """AD-8 / AD-T4-5: doc 級 invoice_num（カード会社のT番号）を継がせない。"""
        # Act
        entries = card_entries.build_entries_from_credit_card(AMEX_A_P1_RAW)

        # Assert
        for entry in entries:
            self.assertEqual(entry["debit_invoice"], "")

    def test_invoice_confirm_tag_uses_greater_or_equal(self):
        """少額特例は「税込 1 万円**未満**」。1 万円ちょうどは要確認。"""
        # Arrange
        raw = {**JCB_FX_RAW, "rows": [
            {**JCB_FX_RAW["rows"][0], "amount": 9999},
            {**JCB_FX_RAW["rows"][1], "amount": 10000},
        ]}

        # Act
        entries = card_entries.build_entries_from_credit_card(raw)

        # Assert
        self.assertFalse(entries[0]["_needs_invoice_confirm"])
        self.assertTrue(entries[1]["_needs_invoice_confirm"])

    def test_line_kind_is_derived_from_the_wording(self):
        """derive_line_kind を行ごとに呼ばないと R05/R06 が永久に発火しない。"""
        # Act
        etc = card_entries.build_entries_from_credit_card(AMEX_A_P1_RAW)
        fuel = card_entries.build_entries_from_credit_card(ENEOS_P2_RAW)

        # Assert
        self.assertEqual(etc[0]["_line_kind"], invc.LineKind.ETC)
        self.assertEqual(fuel[0]["_line_kind"], invc.LineKind.FUEL)

    def test_credit_sub_account_carries_the_card_name(self):
        """AD-11。結線は T6（訂正 1: 現在 L列は死んでいる）。"""
        # Act
        entries = card_entries.build_entries_from_credit_card(AMEX_A_P1_RAW)

        # Assert
        self.assertEqual(entries[0]["credit_sub_account"],
                         "アメリカン・エキスプレス・ビジネスカード")

    def test_row_level_fields_are_present_for_t6(self):
        # Act
        entries = card_entries.build_entries_from_credit_card(AMEX_A_P1_RAW)

        # Assert
        self.assertEqual(entries[0]["date"], "2026/04/10")
        self.assertEqual(entries[0]["debit_vendor"], "福北高速 ＥＴＣ後納分")
        self.assertIn("ETC NO", entries[0]["memo"])

    def test_note_is_not_dumped_into_the_description(self):
        """S列を ETC NO の羅列で埋めない（AD-T4-4）。"""
        # Act
        entries = card_entries.build_entries_from_credit_card(AMEX_A_P1_RAW)

        # Assert
        self.assertEqual(entries[0]["description"], "福北高速 ＥＴＣ後納分")


# ── 給油所型の欄取り違えの是正（案 D。2026-08-20）────────────────────
class VendorColumnSwapTest(unittest.TestCase):
    """F列に商品名・T列に店名が入る給油所型の取り違えを**表示欄だけ**で直す。

    根因は prompt の merchant 定義（"利用店名・摘要の主行"）が両方を許すこと。
    Gemini にも raw にも触らず、`_base_entry` が表示欄を組むその場で是正する。

    **両方向で固定する**: 直す行が直ること と、直してはいけない行
    （ETC・通常店名）が 1 バイトも動かないこと。T6-7 が踏んだ
    「顧客の帳簿の見た目が黙って変わる」失敗様式の再発防止。
    """

    def _fuel(self):
        return card_entries.build_entries_from_credit_card(TSCUBIC_FUEL_P4_RAW)

    def test_vendor_becomes_the_station_name(self):
        """V1: F列は元の T列（ステーション名）と**逐値**一致する。

        「油種語が 0 件」では不十分 —— 空欄や OCR ゴミでも通ってしまう
        （Codex 評審 P2）。
        """
        # Act
        entries = self._fuel()

        # Assert
        self.assertEqual(entries[0]["debit_vendor"], "バイパス奈良中央SS")
        self.assertEqual(entries[1]["debit_vendor"], "スーパーセルフ近江大橋SS")
        self.assertEqual(entries[2]["debit_vendor"], "CSCネオス塚口")

    def test_description_keeps_both_station_and_goods(self):
        """V2: S列は「店名 商品名」（趙拍板 2026-08-20）。油種を落とさない。

        F列だけ直して S列も店名にすると、帳簿で最も目に付く列から
        `レギュラー` が消える —— 片方を直して片方を壊す（Codex 評審 P1）。
        """
        # Act
        entries = self._fuel()

        # Assert
        self.assertEqual(entries[0]["description"], "バイパス奈良中央SS レギュラー")
        self.assertEqual(entries[1]["description"],
                         "スーパーセルフ近江大橋SS 洗車")
        self.assertEqual(entries[2]["description"], "CSCネオス塚口 パーツ")

    def test_memo_is_left_alone(self):
        """V3: T列は触らない。F と重複するが、重複は誤りではない。"""
        # Act
        entries = self._fuel()

        # Assert
        self.assertEqual(entries[0]["memo"], "バイパス奈良中央SS")

    def test_ordinary_merchant_row_is_untouched(self):
        """逆方向の固定: 取り違えていない行は 1 バイトも変えない。"""
        # Act
        entry = self._fuel()[3]

        # Assert
        self.assertEqual(entry["debit_vendor"], "セブン-イレブン博多三井ビル店")
        self.assertEqual(entry["description"], "セブン-イレブン博多三井ビル店")
        self.assertEqual(entry["memo"], "")

    def test_etc_rows_are_untouched(self):
        """V5: ETC 頁に店名欄は無い。F=入口 / T=出口＋車種 は両方が実内容。

        語彙表に ETC 語が紛れ込むと約 55 行が壊れる（Plan §1.4）。
        """
        # Act
        entries = card_entries.build_entries_from_credit_card(TSCUBIC_ETC_P6_RAW)

        # Assert
        self.assertEqual(entries[0]["debit_vendor"], "ETC 東大阪入")
        self.assertEqual(entries[0]["description"], "ETC 東大阪入")
        self.assertEqual(entries[0]["memo"], "東大阪荒本 軽自動")
        self.assertEqual(entries[1]["debit_vendor"], "ETC 紫川")

    def test_raw_rows_are_not_mutated(self):
        """Codex 評審 P1: raw を書き換えると検算側の逐語まで変わる。"""
        # Arrange
        before = copy.deepcopy(TSCUBIC_FUEL_P4_RAW)

        # Act
        card_entries.build_entries_from_credit_card(TSCUBIC_FUEL_P4_RAW)

        # Assert
        self.assertEqual(TSCUBIC_FUEL_P4_RAW, before)

    def test_reconciliation_still_reads_the_raw_wording(self):
        """検算集合は raw のまま。表示の都合で検算が動いてはいけない。"""
        # Act
        lines = card_entries.detail_lines_from_raw(TSCUBIC_FUEL_P4_RAW, CC)

        # Assert
        self.assertEqual(lines[0].description, "レギュラー")

    def test_row_text_for_classification_reads_the_raw(self):
        """V6: 記帳軸の判定は raw を読む。表示の組み替えの影響を受けない。"""
        # Act
        text = card_entries._row_text(TSCUBIC_FUEL_P4_RAW["rows"][0])

        # Assert
        self.assertIn("レギュラー", text)
        self.assertIn("バイパス奈良中央SS", text)

    def test_foreign_currency_suffix_survives_the_swap(self):
        """外貨 suffix は入れ替え後も末尾に残る（AD-10）。"""
        # Arrange
        raw = {**TSCUBIC_FUEL_P4_RAW, "rows": [
            _cc_row(1, "2026/04/10", "レギュラー", 5495,
                    note="バイパス奈良中央SS", foreign_amount=57.6,
                    currency="USD", fx_rate=160.384)]}

        # Act
        entries = card_entries.build_entries_from_credit_card(raw)

        # Assert
        self.assertEqual(entries[0]["description"],
                         "バイパス奈良中央SS レギュラー 57.60USD @160.384")

    def test_real_merchants_starting_with_a_goods_token_are_not_swapped(self):
        """Codex 実施後評審 P1: 前方一致だと本物の取引先が破壊される。

        `オイル` `タイヤ` `パーツ` `作業` `プレミアム` は実在の店名の接頭辞
        でもある。**誤検知は取引先欄を壊すが、見逃しは商品名が残るだけで
        店名は T列に在る** —— 誤検知の方が重いので完全一致で照合する。
        """
        # Arrange: いずれも「商品語で始まる**本物の店名**」＋ note 非空
        names = ("パーツワン福岡店", "オイルボーイ博多店", "作業服センター博多",
                 "タイヤ館 春日", "プレミアムアウトレット鳥栖",
                 "ガソリンスタンド山田商会")
        raw = {**TSCUBIC_FUEL_P4_RAW, "rows": [
            _cc_row(i, "2026/04/10", name, 1000, note="ご利用分")
            for i, name in enumerate(names, 1)]}

        # Act
        entries = card_entries.build_entries_from_credit_card(raw)

        # Assert: 1 件も入れ替わらない
        self.assertEqual([e["debit_vendor"] for e in entries], list(names))
        self.assertEqual([e["description"] for e in entries], list(names))

    def test_goods_label_without_a_note_is_left_alone(self):
        """note が空なら入れ替えない —— 空の F列を作らない。"""
        # Arrange
        raw = {**TSCUBIC_FUEL_P4_RAW, "rows": [
            _cc_row(1, "2026/04/10", "レギュラー", 5495)]}

        # Act
        entries = card_entries.build_entries_from_credit_card(raw)

        # Assert
        self.assertEqual(entries[0]["debit_vendor"], "レギュラー")
        self.assertEqual(entries[0]["description"], "レギュラー")


# ── AD-T4-11: account_hint の解決 ─────────────────────────────────
class AccountHintTest(unittest.TestCase):

    def test_unknown_hint_never_reaches_the_debit_account(self):
        """K9: ACCOUNT_MAP.get(x, x) は未知キーを潰さない。"""
        # Act
        entries = card_entries.build_entries_from_credit_card(UC_P2_RAW)

        # Assert
        self.assertNotIn("HP代", [e["debit_account"] for e in entries])
        self.assertNotIn("さくら備品", [e["debit_account"] for e in entries])
        self.assertEqual(entries[0]["_account_hint_unresolved"], "HP代")
        self.assertFalse(entries[0]["_account_hint_used"])

    def test_unresolved_hint_is_kept_in_the_memo(self):
        """客が手書きで指示したものを黙って捨てない。"""
        # Act
        entries = card_entries.build_entries_from_credit_card(UC_P2_RAW)

        # Assert
        self.assertIn("HP代", entries[0]["memo"])

    def test_document_level_hint_resolves_to_one_account(self):
        # Act
        entries = card_entries.build_entries_from_credit_card(ENEOS_P2_RAW)

        # Assert: 「ガソリン代として」→ ガソリン代 → 旅費交通費
        self.assertEqual(entries[0]["debit_account"], "旅費交通費")
        self.assertTrue(entries[0]["_account_hint_used"])

    def test_hint_naming_two_accounts_is_not_resolved(self):
        """F-10「通信費と車輌交通費として」はどちらを当てるか機械には決まらない。"""
        # Act / Assert
        self.assertEqual(
            card_entries.resolve_account_hint("通信費と車輌交通費として"), "")

    def test_hint_naming_two_words_of_one_account_is_resolved(self):
        """複審 P2: 一致語数ではなく canonical 科目数で判定する。"""
        # Act / Assert: どちらも旅費交通費に落ちるので実質 1 科目
        self.assertEqual(
            card_entries.resolve_account_hint("ガソリン代・駐車場代"), "旅費交通費")

    def test_revenue_accounts_are_excluded_from_hints(self):
        """複審 P2: ACCOUNT_MAP には 売上高 / 雑収入 も在る。"""
        # Act / Assert
        self.assertEqual(card_entries.resolve_account_hint("雑収入として"), "")
        self.assertEqual(card_entries.resolve_account_hint("売上高"), "")
        self.assertEqual(card_entries.resolve_account_hint("未払金"), "")


# ── T4-e: nimoca builder ─────────────────────────────────────────
class TransitIcBuilderTest(unittest.TestCase):

    REF = date(2026, 6, 15)

    def test_charge_row_is_not_booked_by_default(self):
        """TBD-2: 乗車を逐行記帳するのでチャージまで計上すると二重計上。"""
        # Act
        entries = card_entries.build_entries_from_transit_ic(
            NIMOCA_P1_RAW, reference_date=self.REF)

        # Assert
        self.assertEqual(len(entries), 3)
        self.assertNotIn(5000, [e["amount"] for e in entries])

    def test_charge_count_and_amount_stay_in_the_summary(self):
        # Act
        summary = card_entries.summarize_nonbookable(NIMOCA_P1_RAW, IC)

        # Assert
        self.assertIn("1件", summary)
        self.assertIn("5,000", summary)

    def test_charge_row_is_booked_when_configured(self):
        """設定が結線されていることの証明（TBD-2 が転んでも config 1 行で済む）。"""
        # Arrange
        original = card_entries.TRANSIT_IC_BOOK_CHARGE_ROWS
        card_entries.TRANSIT_IC_BOOK_CHARGE_ROWS = True
        try:
            # Act
            entries = card_entries.build_entries_from_transit_ic(
                NIMOCA_P1_RAW, reference_date=self.REF)

            # Assert
            self.assertEqual(len(entries), 4)
            self.assertIn(5000, [e["amount"] for e in entries])
        finally:
            card_entries.TRANSIT_IC_BOOK_CHARGE_ROWS = original

    def test_year_is_estimated_from_the_reference_date(self):
        # Act
        entries = card_entries.build_entries_from_transit_ic(
            NIMOCA_P1_RAW, reference_date=self.REF)

        # Assert
        self.assertEqual(entries[0]["date"], "2026/05/01")
        self.assertTrue(entries[0]["_year_estimated"])

    def test_future_month_day_falls_back_to_the_previous_year(self):
        """年跨ぎ: 履歴は過去の記録なので未来日付は前年の徴候。"""
        # Act
        entries = card_entries.build_entries_from_transit_ic(
            NIMOCA_P1_RAW, reference_date=date(2026, 1, 15))
        dates = [e["date"] for e in entries]

        # Assert
        self.assertIn("2025/12/20", dates)
        self.assertIn("2025/05/01", dates)

    def test_printed_year_is_not_overwritten(self):
        # Arrange
        raw = {**NIMOCA_P1_RAW, "rows": [
            {**NIMOCA_P1_RAW["rows"][0], "date": "2024/05/01"}]}

        # Act
        entries = card_entries.build_entries_from_transit_ic(
            raw, reference_date=self.REF)

        # Assert
        self.assertEqual(entries[0]["date"], "2024/05/01")
        self.assertFalse(entries[0]["_year_estimated"])

    def test_line_kind_uses_the_category_column(self):
        # Act
        entries = card_entries.build_entries_from_transit_ic(
            NIMOCA_P1_RAW, reference_date=self.REF)

        # Assert
        self.assertEqual(entries[0]["_line_kind"], invc.LineKind.TRAIN)
        self.assertEqual(entries[1]["_line_kind"], invc.LineKind.BUS)

    def test_debit_account_defaults_to_travel_expense(self):
        # Act
        entries = card_entries.build_entries_from_transit_ic(
            NIMOCA_P1_RAW, reference_date=self.REF)

        # Assert: 文書級 hint「交通費として」→ 旅費交通費（既定とも一致）
        self.assertEqual(entries[0]["debit_account"], "旅費交通費")

    def test_description_is_built_from_the_stations(self):
        # Act
        entries = card_entries.build_entries_from_transit_ic(
            NIMOCA_P1_RAW, reference_date=self.REF)

        # Assert
        self.assertEqual(entries[0]["description"], "電車 西鉄福岡〜薬院")
        self.assertEqual(entries[1]["description"], "バス 天神")

    def test_public_transport_rows_are_classified_as_qualified(self):
        """F-14 公共交通機関特例（3 万円未満）。R05 が発火すること。"""
        # Act
        entries = card_entries.build_entries_from_transit_ic(
            NIMOCA_P1_RAW, reference_date=self.REF)

        # Assert
        self.assertEqual(entries[0]["_deduction"].deduction_class,
                         invc.DeductionClass.QUALIFIED)


# ── 頁の由来が違っても壊れないこと ────────────────────────────────
class RobustnessTest(unittest.TestCase):

    def test_builders_never_raise_on_degenerate_input(self):
        # Arrange
        degenerate = [None, {}, {"rows": None}, {"rows": [None, 3, "x"]},
                      {"rows": [{}], "card": None}]

        # Act / Assert
        for raw in degenerate:
            with self.subTest(raw=raw):
                self.assertIsInstance(
                    card_entries.build_entries_from_credit_card(raw), list)
                self.assertIsInstance(
                    card_entries.build_entries_from_transit_ic(
                        raw, reference_date=date(2026, 6, 15)), list)
                self.assertIsInstance(
                    card_entries.detail_lines_from_raw(raw, CC), tuple)
                self.assertIsInstance(
                    card_entries.summarize_nonbookable(raw, CC), str)

    def test_ocr_fixtures_are_shared_not_duplicated(self):
        """標本の正本は ocr_test_fixtures（複製すると修正が伝播しない）。"""
        # Assert
        for text in (AMEX_A_P1_OCR, AMEX_A_P2_OCR, AMEX_B_P5_OCR,
                     ENEOS_P2_OCR, UC_P2_OCR, NIMOCA_P1_OCR):
            self.assertTrue(text.strip())


if __name__ == "__main__":
    unittest.main()
