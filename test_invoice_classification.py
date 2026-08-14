"""invoice_classification（税区分・控除判定）の単体テスト。

守りたい実害は 2 つあり、向きが逆である。

1. **過大控除**。F-14（国税庁 質疑応答事例）のとおり、カード会社が作成した
   請求明細書は消費税法第30条第9項の請求書等に該当しない。判定が付かない行が
   黙って「適格」に落ちる経路を作ってはいけない。
2. **事務所の実帳との乖離**。MF 本番の実測（Plan 附録 C）で、貸方＝未払金 133 件中
   111 件が「適格」相当かつ H列は空欄だった。趙裁定 9 は
   **「H列は空欄（事務所慣例の踏襲）＋ amount >= 10000 に U列タグ」**。

この 2 つを両立させるため、本モジュールは
**判定はする（全域関数）／帳簿には税法主張を書かない**という形を取る。
判定結果は監査集計とTBD 発火時の単一切替点としてのみ生き、H列は常に空文字になる。

外部依存は receipt_aggregation のみ（gspread / paddleocr 不要）:
    python -m unittest test_invoice_classification -v
"""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))

from doc_types import DocType  # noqa: E402
from invoice_classification import (  # noqa: E402
    BOOKING_KIND_TO_LINE_KIND,
    CARD_ISSUER_T_NUMBERS,
    DEFAULT_TAX_POLICY,
    REACHABLE_CLASSES,
    derive_line_kind,
    DeductionClass,
    DeductionInput,
    LineKind,
    TaxPolicy,
    build_audit_note,
    classify,
    determine_debit_tax_type,
    invoice_confirm_tag,
    line_kind_from_booking_kind,
    needs_invoice_confirmation,
    render_invoice_column,
    summarize,
)


def _inp(line_kind=LineKind.GENERAL, amount=3300, doc_type=DocType.CREDIT_CARD,
         txn_date=date(2026, 4, 10), is_foreign_currency=False,
         merchant_t_number=""):
    return DeductionInput(doc_type=doc_type, line_kind=line_kind,
                          amount_tax_incl=amount, txn_date=txn_date,
                          is_foreign_currency=is_foreign_currency,
                          merchant_t_number=merchant_t_number)


class RuleTreeTest(unittest.TestCase):
    """判定木の各ルール（D5 §3）。優先順位は恣意的に並べていない。"""

    def test_charge_is_out_of_scope(self):
        """SF残高へのチャージは資産の増加であって課税仕入れではない（F-7）。"""
        v = classify(_inp(LineKind.CHARGE, 5000, DocType.TRANSIT_IC))
        self.assertEqual(v.deduction_class, DeductionClass.OUT_OF_SCOPE)
        self.assertEqual(v.rule_id, "R01_charge")

    def test_point_and_settlement_are_out_of_scope(self):
        """F-5: 検算用の集合と記帳用の集合は別物。合計に効くが費用ではない。"""
        for kind in (LineKind.POINT, LineKind.SETTLEMENT):
            with self.subTest(kind=kind):
                v = classify(_inp(kind, -3000))
                self.assertEqual(v.deduction_class, DeductionClass.OUT_OF_SCOPE)

    def test_foreign_currency_is_out_of_scope_with_a_caveat(self):
        """F-9: 現地通貨表記あり。国外取引＝不課税を自動断定はしない。"""
        v = classify(_inp(amount=9238, is_foreign_currency=True))
        self.assertEqual(v.deduction_class, DeductionClass.OUT_OF_SCOPE)
        self.assertTrue(v.caveats)

    def test_merchant_registration_number_qualifies(self):
        v = classify(_inp(merchant_t_number="T1234567890123"))
        self.assertEqual(v.deduction_class, DeductionClass.QUALIFIED)
        self.assertEqual(v.rule_id, "R04_merchant_invoice")

    def test_card_issuer_t_number_is_rejected(self):
        """F-11: 券面の T番号は**カード会社自身**のもの。加盟店のものではない。

        これを加盟店 T番号として扱うと、F-14 が明示的に否定している
        「カード会社作成の請求明細書による控除」を機械的に量産する。
        """
        for t_number in CARD_ISSUER_T_NUMBERS:
            with self.subTest(t_number=t_number):
                v = classify(_inp(merchant_t_number=t_number, amount=140002))
                self.assertNotEqual(v.deduction_class, DeductionClass.QUALIFIED)

    def test_malformed_t_number_is_rejected(self):
        for bad in ("T123", "1234567890123", "", "T12345678901234", "Tabcdefghijklm"):
            with self.subTest(t_number=bad):
                v = classify(_inp(merchant_t_number=bad, amount=140002))
                self.assertNotEqual(v.rule_id, "R04_merchant_invoice")

    def test_public_transport_applies_only_to_transit_ic(self):
        """クレカ明細に種別の印字があるという事実は台帳にない（根拠のない分岐を作らない）。"""
        ic = classify(_inp(LineKind.TRAIN, 260, DocType.TRANSIT_IC))
        self.assertEqual(ic.rule_id, "R05_public_transport")

        cc = classify(_inp(LineKind.TRAIN, 260, DocType.CREDIT_CARD))
        self.assertNotEqual(cc.rule_id, "R05_public_transport")

    def test_public_transport_never_applies_to_non_transport_line_kinds(self):
        """R05 の対象は電車・バスのみ。他の種別へ広げると過大控除になる。

        変異テストで「R05 を GENERAL 行へ広げる」変異が全テストを通過したので、
        その穴を塞ぐ。事業者要件を確認済みにして R07 を有効にしたうえで
        rule_id を見る（そうしないと R07/R08 の陰に隠れて差が出ない）。
        """
        confirmed = DEFAULT_TAX_POLICY._replace(small_amount_taxpayer_confirmed=True)
        for kind in (LineKind.GENERAL, LineKind.FUEL, LineKind.ETC):
            with self.subTest(kind=kind):
                v = classify(_inp(kind, 260, DocType.TRANSIT_IC), confirmed)
                self.assertNotEqual(v.rule_id, "R05_public_transport")

    def test_public_transport_applies_to_exactly_train_and_bus(self):
        """対象種別の集合そのものを固定する（増減の両方向を検出する）。"""
        confirmed = DEFAULT_TAX_POLICY._replace(small_amount_taxpayer_confirmed=True)
        fired = {kind for kind in LineKind.ALL
                 if classify(_inp(kind, 260, DocType.TRANSIT_IC),
                             confirmed).rule_id == "R05_public_transport"}
        self.assertEqual(fired, {LineKind.TRAIN, LineKind.BUS})

    def test_public_transport_threshold_is_30000(self):
        under = classify(_inp(LineKind.BUS, 29999, DocType.TRANSIT_IC))
        over = classify(_inp(LineKind.BUS, 30000, DocType.TRANSIT_IC))
        self.assertEqual(under.rule_id, "R05_public_transport")
        self.assertNotEqual(over.rule_id, "R05_public_transport")

    def test_etc_requires_the_road_company_certificate_caveat(self):
        v = classify(_inp(LineKind.ETC, 630))
        self.assertEqual(v.rule_id, "R06_etc")
        self.assertTrue(any("利用証明書" in c for c in v.caveats))

    def test_small_amount_rule_fires_under_10000_when_taxpayer_confirmed(self):
        policy = DEFAULT_TAX_POLICY._replace(small_amount_taxpayer_confirmed=True)
        v = classify(_inp(LineKind.GENERAL, 9999), policy)
        self.assertEqual(v.rule_id, "R07_small_amount")
        self.assertEqual(v.deduction_class, DeductionClass.QUALIFIED)

    def test_small_amount_without_taxpayer_confirmation_is_not_deductible(self):
        """T2 DoD: 特例に命中せず、かつ少額特例未確認のとき必ず not_deductible。

        実害の非対称性 —— 過大控除は税務リスクだが、過小控除は人が確認して
        直せる。ただし rule_id は R07 と分けて残し、「確認さえ取れれば特例圏内」
        であることを監査集計から読めるようにする。
        """
        v = classify(_inp(LineKind.GENERAL, 9999))
        self.assertEqual(v.rule_id, "R08_small_amount_unconfirmed")
        self.assertEqual(v.deduction_class, DeductionClass.NOT_DEDUCTIBLE)

    def test_default_is_not_deductible(self):
        """F-14 原則。特例に命中しない行は控除不可（判定木の終端）。"""
        v = classify(_inp(LineKind.GENERAL, 140002))
        self.assertEqual(v.deduction_class, DeductionClass.NOT_DEDUCTIBLE)
        self.assertEqual(v.rule_id, "R99_default")


class RulePriorityTest(unittest.TestCase):
    """D5 §3.1: より強い根拠を採る（期限・事業者要件の有無で並べている）。"""

    def test_public_transport_beats_small_amount(self):
        """nimoca の 260 円は両方に該当する。公共交通は期限も事業者要件も無い。

        これにより 2029/10/1 以降も nimoca の判定は変わらない（静かに壊れない）。
        """
        v = classify(_inp(LineKind.TRAIN, 260, DocType.TRANSIT_IC))
        self.assertEqual(v.rule_id, "R05_public_transport")

    def test_public_transport_survives_the_small_amount_expiry(self):
        v = classify(_inp(LineKind.TRAIN, 260, DocType.TRANSIT_IC,
                          txn_date=date(2030, 1, 15)))
        self.assertEqual(v.deduction_class, DeductionClass.QUALIFIED)

    def test_etc_beats_small_amount(self):
        """ETC 630 円も少額特例に該当するが、証明書要件の注記が落ちてしまう。"""
        v = classify(_inp(LineKind.ETC, 630))
        self.assertEqual(v.rule_id, "R06_etc")

    def test_non_purchase_beats_everything(self):
        """負数のポイント行が「少額だから適格」になってはいけない。"""
        v = classify(_inp(LineKind.POINT, 3000, merchant_t_number="T1234567890123"))
        self.assertEqual(v.deduction_class, DeductionClass.OUT_OF_SCOPE)


class SmallAmountExpiryTest(unittest.TestCase):
    """少額特例は 2023/10/1〜2029/9/30。期限後に静かに適用され続けない。"""

    def test_expired_transaction_is_not_deductible(self):
        v = classify(_inp(LineKind.GENERAL, 3300, txn_date=date(2029, 10, 1)))
        self.assertEqual(v.deduction_class, DeductionClass.NOT_DEDUCTIBLE)

    def test_expiry_is_enforced_by_the_rule_not_by_the_unconfirmed_fallback(self):
        """期限判定そのものを縛る（`deduction_class` だけ見ると R08 に隠れる）。

        既定 policy では期限内でも R08（事業者要件 未確認）で not_deductible に
        なるため、`deduction_class` の比較では期限判定を削除しても検出できない
        （変異テストで実証済み）。**事業者要件を確認済みにして R07 が出るはずの
        条件に固定し、rule_id で縛る**ことで、期限の上下限が本当に効いている
        ことを保証する。
        """
        confirmed = DEFAULT_TAX_POLICY._replace(small_amount_taxpayer_confirmed=True)
        cases = (
            (date(2023, 9, 30), "R99_default"),          # 開始日の前日
            (date(2023, 10, 1), "R07_small_amount"),     # 開始日
            (date(2029, 9, 30), "R07_small_amount"),     # 満了日
            (date(2029, 10, 1), "R99_default"),          # 満了日の翌日
        )
        for day, expected_rule in cases:
            with self.subTest(day=day):
                v = classify(_inp(LineKind.GENERAL, 3300, txn_date=day), confirmed)
                self.assertEqual(v.rule_id, expected_rule)

    def test_missing_date_is_enforced_by_the_rule_too(self):
        """日付欠損ガードも rule_id で縛る（同じく R08 に隠れる）。"""
        confirmed = DEFAULT_TAX_POLICY._replace(small_amount_taxpayer_confirmed=True)
        v = classify(_inp(LineKind.GENERAL, 3300, txn_date=None), confirmed)
        self.assertEqual(v.rule_id, "R99_default")

    def test_date_guard_lives_in_the_predicate_not_in_the_exception_handler(self):
        """述語自身が日付欠損を弾くこと（`classify` の try/except に頼らない）。

        `classify` は述語の例外を握りつぶして「発火しなかった」と扱うので、
        ガードを外しても**外から見た挙動は変わらない**（実際、変異テストでは
        等価変異になった）。しかしそれに頼ると、述語内の本物のバグまで同じ
        except に吞まれ、「ルールが発火しない」という区別のつかない症状に
        化ける。だから述語を直接呼んで、ガードの所在そのものを固定する。
        """
        import invoice_classification as ic

        norm = ic._normalize(_inp(LineKind.GENERAL, 3300, txn_date=None))
        # 例外ではなく False を返すこと（例外なら TypeError で ERROR になる）
        self.assertIs(ic._is_small_amount_line(norm, DEFAULT_TAX_POLICY), False)

    def test_predicates_do_not_raise_for_any_reachable_input(self):
        """全ルールの述語が、正規化済み入力に対して例外を出さないこと。

        `classify` の try/except は最後の安全網であって、通常経路で
        踏むものではない。踏んでいたら設計の穴を隠していることになる。
        """
        import invoice_classification as ic

        policies = (DEFAULT_TAX_POLICY,
                    DEFAULT_TAX_POLICY._replace(small_amount_taxpayer_confirmed=True))
        dates = (None, date(2020, 5, 1), date(2026, 4, 10), date(2030, 1, 1))
        for rule in ic.RULES:
            for policy in policies:
                for kind in LineKind.ALL:
                    for amount in (None, -35808, 0, 9999, 10000, 999999):
                        for day in dates:
                            norm = ic._normalize(DeductionInput(
                                doc_type=DocType.TRANSIT_IC, line_kind=kind,
                                amount_tax_incl=amount, txn_date=day))
                            try:
                                rule.predicate(norm, policy)
                            except Exception as e:  # noqa: BLE001
                                self.fail("%s の述語が例外: %s: %s"
                                          % (rule.rule_id, type(e).__name__, e))

    def test_transactions_before_the_start_date_are_not_covered(self):
        """特例の期間は 2023/10/1〜2029/9/30（F-14）。開始日より前は対象外。

        会計事務所は往年の資料も扱うので、開始日を見ないと 2023/9 以前の
        古い明細が「少額特例で適格」として集計される。
        """
        confirmed = DEFAULT_TAX_POLICY._replace(small_amount_taxpayer_confirmed=True)
        for day, covered in ((date(2023, 9, 30), False), (date(2023, 10, 1), True)):
            with self.subTest(day=day):
                v = classify(_inp(LineKind.GENERAL, 3300, txn_date=day), confirmed)
                self.assertEqual(v.rule_id == "R07_small_amount", covered)

    def test_before_start_date_does_not_hit_the_unconfirmed_rule_either(self):
        v = classify(_inp(LineKind.GENERAL, 3300, txn_date=date(2020, 5, 1)))
        self.assertEqual(v.rule_id, "R99_default")

    def test_missing_date_does_not_apply_the_rule(self):
        """日付が読めない行に期限つき特例を当てない。"""
        v = classify(_inp(LineKind.GENERAL, 3300, txn_date=None))
        self.assertEqual(v.deduction_class, DeductionClass.NOT_DEDUCTIBLE)

    def test_taxpayer_condition_is_flagged_as_unconfirmed(self):
        """基準期間課税売上高 1 億円以下等はプログラムに判定不能。"""
        v = classify(_inp(LineKind.GENERAL, 3300))
        self.assertTrue(any("基準期間" in c for c in v.caveats))

    def test_confirmed_taxpayer_drops_the_caveat(self):
        policy = DEFAULT_TAX_POLICY._replace(small_amount_taxpayer_confirmed=True)
        v = classify(_inp(LineKind.GENERAL, 3300), policy)
        self.assertFalse(any("基準期間" in c for c in v.caveats))

    def test_reduced_rate_risk_is_flagged_for_general_lines(self):
        """§4 の 182 円リスク（食料品等が 10% で控除される可能性）。"""
        v = classify(_inp(LineKind.GENERAL, 3300))
        self.assertTrue(any("8%" in c for c in v.caveats))

    def test_fuel_lines_do_not_get_the_reduced_rate_caveat(self):
        v = classify(_inp(LineKind.FUEL, 7380))
        self.assertFalse(any("8%" in c for c in v.caveats))


class TotalityTest(unittest.TestCase):
    """classify は全域関数（None も例外も返さない）。

    IP-401 の思想を行レベルに適用したもの: 判定が付かない行が黙って
    H列空欄（＝MF が『適格』と解釈, F-12）へ落ちる経路を作らない。
    """

    def test_every_line_kind_gets_a_verdict(self):
        for kind in LineKind.ALL:
            for doc_type in (DocType.CREDIT_CARD, DocType.TRANSIT_IC):
                for amount in (-35808, 0, 1, 9999, 10000, 999999):
                    with self.subTest(kind=kind, doc=doc_type, amount=amount):
                        v = classify(_inp(kind, amount, doc_type))
                        self.assertIn(v.deduction_class, REACHABLE_CLASSES)
                        self.assertTrue(v.rule_id)

    def test_hostile_input_does_not_raise(self):
        hostile = (
            DeductionInput(doc_type=None, line_kind=None, amount_tax_incl=None,
                           txn_date=None),
            DeductionInput(doc_type="", line_kind="謎の種別", amount_tax_incl="3,300",
                           txn_date="2026-04-10"),
        )
        for inp in hostile:
            with self.subTest(inp=inp):
                v = classify(inp)
                self.assertIn(v.deduction_class, REACHABLE_CLASSES)

    def test_zero_amount_is_not_qualified_by_the_small_amount_rule(self):
        """`0 < 額` の境界。金額 0 の行に特例を当てない。"""
        v = classify(_inp(LineKind.GENERAL, 0))
        self.assertEqual(v.rule_id, "R99_default")

    def test_negative_amount_is_not_qualified_by_the_small_amount_rule(self):
        v = classify(_inp(LineKind.GENERAL, -500))
        self.assertEqual(v.rule_id, "R99_default")


class TransitionalMeasuresAreUnreachableTest(unittest.TestCase):
    """経過措置（80%/50%）は enum に予約するのみ。判定木からは到達しない。

    2026/9/30 で 80%→50% に切り替わるため、自動適用すると 1 ヶ月半後に
    全件の控除率が静かに変わる。
    """

    def test_transitional_classes_are_not_reachable(self):
        self.assertNotIn(DeductionClass.TRANSITIONAL_80, REACHABLE_CLASSES)
        self.assertNotIn(DeductionClass.TRANSITIONAL_50, REACHABLE_CLASSES)

    def test_no_rule_outputs_a_transitional_class(self):
        for kind in LineKind.ALL:
            for amount in (500, 9999, 10000, 500000):
                with self.subTest(kind=kind, amount=amount):
                    v = classify(_inp(kind, amount))
                    self.assertNotIn("transitional", v.deduction_class)


class InvoiceColumnTest(unittest.TestCase):
    """趙裁定 9（AD-8）: H列は空欄。事務所の記帳慣例をそのまま踏襲する。"""

    def test_every_reachable_class_renders_as_blank(self):
        for cls in REACHABLE_CLASSES:
            with self.subTest(cls=cls):
                self.assertEqual(render_invoice_column(cls), "")

    def test_rendering_accepts_a_verdict_too(self):
        self.assertEqual(render_invoice_column(classify(_inp())), "")

    def test_confirmation_tag_uses_greater_or_equal(self):
        """少額特例は「税込1万円**未満**」なので、1万円ちょうどは対象外＝要確認。"""
        self.assertFalse(needs_invoice_confirmation(9999))
        self.assertTrue(needs_invoice_confirmation(10000))
        self.assertTrue(needs_invoice_confirmation(140002))

    def test_confirmation_tag_ignores_sign_and_bad_values(self):
        self.assertFalse(needs_invoice_confirmation(-140002))
        self.assertFalse(needs_invoice_confirmation(None))
        self.assertFalse(needs_invoice_confirmation("たくさん"))

    def test_confirmation_tag_wording_makes_no_tax_law_claim(self):
        """「少額特例により適格」とは書かない（事業者要件を機械判定できない）。"""
        tag = invoice_confirm_tag()
        self.assertIn("インボイス", tag)
        self.assertNotIn("少額特例", tag)
        self.assertNotIn("適格", tag)


class LedgerDoesNotAssertTaxLawTest(unittest.TestCase):
    """判定根拠は監査集計にのみ出す。帳簿（摘要・仕訳メモ）には書かない。

    H列を空欄にするのは**事務所慣例の踏襲**であって税法判断の自動化ではない
    （AD-8）。摘要に「少額特例」と書けば、それは帳簿上の税法主張になってしまう。
    """

    def test_audit_note_carries_the_basis(self):
        note = build_audit_note(classify(_inp(LineKind.ETC, 630)))
        self.assertIn("R06_etc", note)
        self.assertIn("利用証明書", note)

    def test_unverified_citations_are_marked(self):
        note = build_audit_note(classify(_inp(LineKind.ETC, 630)))
        self.assertIn("要確認", note)

    def test_summarize_folds_by_rule_id_not_by_row(self):
        """287 行の監査行は読めない。判定コード単位に畳む。"""
        rows = [(classify(_inp(LineKind.ETC, 630)), 630) for _ in range(287)]
        rows.append((classify(_inp(LineKind.GENERAL, 140002)), 140002))

        result = summarize(rows)
        self.assertEqual(len(result), 2)
        self.assertEqual(result["R06_etc"]["count"], 287)
        self.assertEqual(result["R06_etc"]["sum_amount"], 630 * 287)
        self.assertEqual(result["R99_default"]["count"], 1)

    def test_summarize_tolerates_broken_rows(self):
        self.assertEqual(summarize([]), {})
        self.assertEqual(summarize([(None, 100), ("", None)]), {})


class TaxTypeTest(unittest.TestCase):
    """G列（税区分）は H列とは独立（D5 §1）。"""

    def test_out_of_scope_maps_to_exempt(self):
        v = classify(_inp(LineKind.CHARGE, 5000, DocType.TRANSIT_IC))
        self.assertEqual(determine_debit_tax_type(v), "対象外")

    def test_everything_else_is_taxable_purchase_10_percent(self):
        for kind in (LineKind.GENERAL, LineKind.ETC, LineKind.FUEL, LineKind.TRAIN):
            with self.subTest(kind=kind):
                v = classify(_inp(kind, 630, DocType.TRANSIT_IC))
                self.assertEqual(determine_debit_tax_type(v), "課対仕入10%")

    def test_not_deductible_is_still_a_taxable_purchase(self):
        """控除できないことと課税仕入れであることは別（F-14 は後者を否定しない）。"""
        v = classify(_inp(LineKind.GENERAL, 140002))
        self.assertEqual(v.deduction_class, DeductionClass.NOT_DEDUCTIBLE)
        self.assertEqual(determine_debit_tax_type(v), "課対仕入10%")

    def test_vocabulary_comes_from_receipt_aggregation(self):
        """新しい文字列リテラルを書き起こさない。

        anomaly_detector.EXEMPT_TAX_TYPE は精確等値で判定しているので、
        語彙がズレると既存の規則が静かに壊れる。
        """
        from receipt_aggregation import determine_tax_types
        self.assertEqual(determine_tax_types("credit_card", 0.10)[0], "課対仕入10%")
        self.assertEqual(determine_tax_types("credit_card", 0)[0], "対象外")


class BookingKindBridgeTest(unittest.TestCase):
    """記帳軸（card_reconciliation の kind）と実体軸（LineKind）の対応。

    Gemini に 2 つの軸を出させると自己矛盾しうるので、非購入の 3 種だけは
    記帳軸から一意に導けるようにしておく（T4 の builder の取り違え防止）。
    """

    def test_non_purchase_kinds_map_deterministically(self):
        self.assertEqual(line_kind_from_booking_kind("charge", LineKind.GENERAL),
                         LineKind.CHARGE)
        self.assertEqual(line_kind_from_booking_kind("credit_adjust", LineKind.GENERAL),
                         LineKind.POINT)
        self.assertEqual(line_kind_from_booking_kind("carry_over", LineKind.GENERAL),
                         LineKind.SETTLEMENT)

    def test_expense_kinds_keep_the_declared_line_kind(self):
        for kind in ("expense", "fee", "unknown", None, "謎"):
            with self.subTest(kind=kind):
                self.assertEqual(line_kind_from_booking_kind(kind, LineKind.ETC),
                                 LineKind.ETC)

    def test_fallback_defaults_to_general(self):
        self.assertEqual(line_kind_from_booking_kind("expense", None), LineKind.GENERAL)
        self.assertEqual(line_kind_from_booking_kind("expense", "謎の種別"),
                         LineKind.GENERAL)


class DeriveLineKindTest(unittest.TestCase):
    """券面の逐語から実体軸を決める（AD-4 と同じ「分類は Python 側」方針）。

    この関数が無いと、Gemini プロンプトには `sec`（検算軸）と `kind`（記帳軸）
    しか無いので nimoca の電車行も ETC 通行料も一律 GENERAL に落ち、
    R05/R06 が永久に発火しない。しかも「要:税率8%(軽減)の可能性」という
    通行料・給油には無意味な caveat が全行に付く。
    """

    def test_booking_kind_wins_over_the_text(self):
        """記帳軸が非購入なら逐語を見ない（チャージ行が特例で適格になる経路を断つ）。"""
        self.assertEqual(
            derive_line_kind(DocType.TRANSIT_IC, "charge", "入金 現金"), LineKind.CHARGE)
        self.assertEqual(
            derive_line_kind(DocType.CREDIT_CARD, "credit_adjust",
                             "ENEOSポイントキャッシュバック"), LineKind.POINT)
        self.assertEqual(
            derive_line_kind(DocType.CREDIT_CARD, "carry_over",
                             "前回分口座振替金額"), LineKind.SETTLEMENT)

    def test_etc_is_detected_from_the_printed_wording(self):
        """F-8 実測: アメックス「ＥＴＣ後納分」/ 楽天「ＥＴＣカード売上」。"""
        for text in ("福北高速 ＥＴＣ後納分", "ＥＴＣカード売上", "ETC 後納分"):
            with self.subTest(text=text):
                self.assertEqual(derive_line_kind(DocType.CREDIT_CARD, "expense", text),
                                 LineKind.ETC)

    def test_fuel_is_detected_from_the_printed_wording(self):
        self.assertEqual(derive_line_kind(DocType.CREDIT_CARD, "expense", "給油"),
                         LineKind.FUEL)

    def test_transit_types_come_from_the_nimoca_category_column(self):
        """F-7 実測: 種別列の値は `電車` `バス` `入金`。"""
        self.assertEqual(
            derive_line_kind(DocType.TRANSIT_IC, "expense", "電車 西鉄天神 ～ 薬院"),
            LineKind.TRAIN)
        self.assertEqual(
            derive_line_kind(DocType.TRANSIT_IC, "expense", "バス 天神 ～ 博多駅"),
            LineKind.BUS)

    def test_credit_card_never_derives_public_transport(self):
        """クレカ明細に種別の印字があるという事実は台帳に無い。

        加盟店名（「JR」「西鉄バス」等）からの推定は根拠が無いので作らない。
        """
        for text in ("西鉄バス", "JR九州 電車"):
            with self.subTest(text=text):
                self.assertEqual(
                    derive_line_kind(DocType.CREDIT_CARD, "expense", text),
                    LineKind.GENERAL)

    def test_derived_etc_line_actually_reaches_the_etc_rule(self):
        """導出 → 判定木の接続を通しで固定する（片方だけ直す事故の防止）。"""
        kind = derive_line_kind(DocType.CREDIT_CARD, "expense", "福北高速 ＥＴＣ後納分")
        v = classify(_inp(kind, 630))

        self.assertEqual(v.rule_id, "R06_etc")
        self.assertFalse(any("8%" in c for c in v.caveats),
                         "通行料に軽減税率の caveat が付いている")

    def test_derived_train_line_actually_reaches_the_public_transport_rule(self):
        kind = derive_line_kind(DocType.TRANSIT_IC, "expense", "電車 西鉄天神 ～ 薬院")
        v = classify(_inp(kind, 260, DocType.TRANSIT_IC))

        self.assertEqual(v.rule_id, "R05_public_transport")
        self.assertEqual(v.deduction_class, DeductionClass.QUALIFIED)

    def test_falls_back_to_declared_then_general(self):
        self.assertEqual(
            derive_line_kind(DocType.CREDIT_CARD, "expense", "謎の加盟店",
                             declared_line_kind=LineKind.FUEL), LineKind.FUEL)
        self.assertEqual(
            derive_line_kind(DocType.CREDIT_CARD, "expense", "謎の加盟店",
                             declared_line_kind="でたらめ"), LineKind.GENERAL)

    def test_never_raises_on_hostile_text(self):
        for text in (None, "", 12345, "🙂" * 100):
            with self.subTest(text=repr(text)[:16]):
                self.assertIn(derive_line_kind(DocType.CREDIT_CARD, "expense", text),
                              LineKind.ALL)


class BookingKindVocabularyTest(unittest.TestCase):
    """記帳軸の語彙は card_reconciliation が単一の真実源。"""

    def test_bridge_keys_come_from_card_reconciliation(self):
        import card_reconciliation as cr

        self.assertEqual(set(BOOKING_KIND_TO_LINE_KIND),
                         {cr.KIND_CHARGE, cr.KIND_CREDIT_ADJUST, cr.KIND_CARRY_OVER})

    def test_every_bridge_key_is_a_known_booking_kind(self):
        import card_reconciliation as cr

        for kind in BOOKING_KIND_TO_LINE_KIND:
            with self.subTest(kind=kind):
                self.assertIn(kind, cr.KIND_ALL)


class RuleOrderTest(unittest.TestCase):
    """`_active_rules` が実行時ソートを省ける根拠を固定する。"""

    def test_rules_are_declared_in_ascending_priority(self):
        import invoice_classification as ic

        priorities = [r.priority for r in ic.RULES]
        self.assertEqual(priorities, sorted(priorities))

    def test_validation_rejects_out_of_order_rules(self):
        import invoice_classification as ic

        shuffled = (ic.RULES[-1],) + ic.RULES[:-1]
        with self.assertRaises(RuntimeError):
            ic._validate_rule_order(shuffled)

    def test_active_rules_preserves_priority_order(self):
        import invoice_classification as ic

        active = ic._active_rules(DEFAULT_TAX_POLICY)
        self.assertEqual([r.priority for r in active],
                         sorted(r.priority for r in active))


class PolicySwitchTest(unittest.TestCase):
    """特例の一括 OFF（D5 §11-6）。実害が出たとき即座に止められること。"""

    def test_disabling_etc_rule_falls_through_to_the_small_amount_rules(self):
        policy = DEFAULT_TAX_POLICY._replace(etc_special_enabled=False)
        v = classify(_inp(LineKind.ETC, 630), policy)
        self.assertEqual(v.rule_id, "R08_small_amount_unconfirmed")

        confirmed = policy._replace(small_amount_taxpayer_confirmed=True)
        self.assertEqual(classify(_inp(LineKind.ETC, 630), confirmed).rule_id,
                         "R07_small_amount")

    def test_disabling_all_specials_yields_not_deductible(self):
        policy = TaxPolicy(small_amount_enabled=False, public_transport_enabled=False,
                           etc_special_enabled=False)
        for kind in (LineKind.ETC, LineKind.TRAIN, LineKind.GENERAL):
            with self.subTest(kind=kind):
                v = classify(_inp(kind, 630, DocType.TRANSIT_IC), policy)
                self.assertEqual(v.deduction_class, DeductionClass.NOT_DEDUCTIBLE)

    def test_travel_expense_rule_is_disabled_by_default(self):
        """出張旅費等特例は台帳に該当の事実がない（根拠のない分岐を作らない）。"""
        self.assertFalse(DEFAULT_TAX_POLICY.travel_expense_enabled)


class RenderingValidationTest(unittest.TestCase):
    """import 時の自己検証（起動時に大声で落ちる方針）。"""

    def test_validation_rejects_a_class_missing_from_the_map(self):
        import invoice_classification as ic
        with self.assertRaises(RuntimeError):
            ic._validate_rendering({"qualified": ""}, ic.INVOICE_COL_VALUES)

    def test_validation_rejects_a_value_outside_mf_vocabulary(self):
        import invoice_classification as ic
        bad = {cls: "適格控除" for cls in REACHABLE_CLASSES}
        with self.assertRaises(RuntimeError):
            ic._validate_rendering(bad, ic.INVOICE_COL_VALUES)

    def test_blank_is_an_allowed_mf_value(self):
        import invoice_classification as ic
        self.assertIn("", ic.INVOICE_COL_VALUES)


if __name__ == "__main__":
    unittest.main()
