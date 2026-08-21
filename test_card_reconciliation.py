"""card_reconciliation（跨頁合計検算の元帳）の単体テスト。

守りたい実害: 逐行記帳では「明細を 1 行取りこぼした」ことに誰も気づけない。
券面の印字合計は、そのための唯一の外部検査器である（趙裁定 3:
明細相加 ≠ 合計 なら照常記帳 ＋ 赤系マーク。Failed にはしない）。

本モジュールの中心的な契約は **検算集合と記帳集合が別軸**であること（AD-5）。
事実台帳が 3 例で反証している:
  - 負数だが検算に入る:   ENEOS ポイントキャッシュバック −3,000（F-4）
  - 負数で検算にも入らない: 前回分口座振替 −35,808（F-5、別区画）
  - 正数だが記帳しない:   nimoca 入金 現金 5,000（F-7）
符号からどちらの軸も導出できない。だから kind と section の 2 軸を持つ。

外部依存なし（gspread / paddleocr 不要）:
    python -m unittest test_card_reconciliation -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from doc_types import DocType  # noqa: E402
from card_reconciliation import (  # noqa: E402
    KIND_CARRY_OVER,
    KIND_CHARGE,
    KIND_CREDIT_ADJUST,
    KIND_EXPENSE,
    KIND_FEE,
    KIND_UNKNOWN,
    MAX_CARDS_PER_FILE,
    RECON_POLICY,
    SECTION_CURRENT_USAGE,
    SECTION_PAYMENT_SUMMARY,
    SECTION_POINT,
    SECTION_UNKNOWN,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    TOTAL_LABEL_SECTION,
    _coerce_int,
    VERDICT_COUNT_NG,
    VERDICT_COUNT_OK,
    VERDICT_DUPLICATE_PAGE,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_MISMATCH_GAP,
    VERDICT_NOT_APPLICABLE,
    VERDICT_UNVERIFIABLE,
    CardIdent,
    DetailLine,
    FileReconLedger,
    PrintedTotal,
    section_for_label,
)


def _ident(member_no="****-******-26003", issuer="amex", n=None, total=None,
           statement_no=None):
    return CardIdent(member_no=member_no, issuer=issuer, statement_page_n=n,
                     statement_page_total=total, statement_no=statement_no)


def _line(amount, kind=KIND_EXPENSE, section=SECTION_CURRENT_USAGE, description=""):
    return DetailLine(amount=amount, section=section, kind=kind, description=description)


def _total(label, amount, page=1, count=None, is_handwritten=False):
    return PrintedTotal(label=label, amount=amount, count=count, page=page,
                        is_handwritten=is_handwritten)


def _ledger(doc_type=DocType.CREDIT_CARD, total_pages=2, start_page=1):
    return FileReconLedger(doc_type, total_pages, start_page=start_page)


def _only(verdicts):
    assert len(verdicts) == 1, "カードが 1 枚である前提が崩れている: %r" % (verdicts,)
    return verdicts[0]


class GoldenReconciliationTest(unittest.TestCase):
    """F-4 の実測 3 例。ここが崩れたら検算そのものが信用できない。"""

    def test_amex_card_a_two_pages_sum_to_printed_total(self):
        """630×4（p1）+ 6 行（p2）= 17,295。`N/N` が来なくても結算する。"""
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(n=1, total=6), (), [_line(630) for _ in range(4)])
        led.observe_page(2, _ident(n=2, total=6),
                         (_total("今月ご利用額合計", 17295, page=2),),
                         [_line(x) for x in (630, 5799, 3866, 3220, 630, 630)])

        v = _only(led.finalize())
        self.assertEqual(v.detail_sum, 17295)
        self.assertEqual(v.printed_total, 17295)
        self.assertEqual(v.verdict, VERDICT_MATCH)
        self.assertIsNone(v.severity)

    def test_amex_card_b_excludes_prior_settlement_from_current_usage(self):
        """前回分口座振替 −35,808 は別区画（F-5）。当月検算に混ぜない。"""
        led = _ledger(total_pages=1)
        led.observe_page(
            1, _ident(member_no="****-******-11004", n=1, total=3),
            (_total("今月ご利用額合計", 933680),
             _total("お支払い金額合計", 897872)),
            [_line(29640), _line(300000), _line(587440), _line(16600),
             # section を current_usage と申告されても kind で強制上書きされる
             _line(-35808, kind=KIND_CARRY_OVER, section=SECTION_CURRENT_USAGE)])

        v = _only(led.finalize())
        self.assertEqual(v.section_used, SECTION_CURRENT_USAGE)
        self.assertEqual(v.detail_sum, 933680)
        self.assertEqual(v.verdict, VERDICT_MATCH)

    def test_eneos_needs_the_negative_point_line_to_match(self):
        """`7,380 + 11,123 − 3,000 = 15,503`（F-4）。負数行を入れて初めて一致する。"""
        led = _ledger(total_pages=2)
        led.observe_page(
            2, _ident(member_no="XXXX-XXXX-XXXX-8602", issuer="eneos", n=2, total=2),
            (_total("ご請求金額合計", 15503, page=2),),
            [_line(7380), _line(11123),
             _line(-3000, kind=KIND_CREDIT_ADJUST, description="ENEOSポイントキャッシュバック")])

        v = _only(led.finalize())
        self.assertEqual(v.detail_sum, 15503)
        self.assertEqual(v.verdict, VERDICT_MATCH)

    def test_eneos_without_the_negative_line_is_a_mismatch(self):
        """反証。負数行を検算から外す実装だと 18,503 で不一致になる。

        頁欠は無関係な変数なので `total` を宣言しない（宣言すると
        「不一致(頁欠の可能性)」になり、何を検証しているのか曖昧になる）。
        """
        led = _ledger(total_pages=1)
        led.observe_page(
            1, _ident(member_no="XXXX-XXXX-XXXX-8602", issuer="eneos", n=1),
            (_total("ご請求金額合計", 15503),),
            [_line(7380), _line(11123)])

        v = _only(led.finalize())
        self.assertEqual(v.detail_sum, 18503)
        self.assertEqual(v.verdict, VERDICT_MISMATCH)
        self.assertEqual(v.severity, "high")
        self.assertEqual(v.diff, 3000)


class BookingSetIsSeparateFromReconSetTest(unittest.TestCase):
    """AD-5: 記帳集合（A3）と検算集合（A2）は別軸。"""

    def test_eneos_expense_sum_is_18503_while_recon_sum_is_15503(self):
        """同じ 3 行が、検算では 15,503・記帳では 18,503 になる。

        ポイント充当は「借方 未払金 ／ 貸方 雑収入」で記帳されるため
        費用の合計には入らない。この 2 つを 1 つの数で表そうとすると
        必ずどちらかが壊れる。
        """
        led = _ledger()
        led.observe_page(2, _ident(issuer="eneos", n=2, total=2),
                         (_total("ご請求金額合計", 15503, page=2),),
                         [_line(7380), _line(11123),
                          _line(-3000, kind=KIND_CREDIT_ADJUST)])

        v = _only(led.finalize())
        self.assertEqual(v.detail_sum, 15503, "検算集合")
        self.assertEqual(v.expense_sum, 18503, "記帳集合（kind ∈ {expense, fee}）")

    def test_non_bookable_lines_are_reported_not_dropped(self):
        """記帳しない行も件数と金額を残す（IP-401 の行版）。"""
        led = _ledger(doc_type=DocType.TRANSIT_IC, total_pages=1)
        led.observe_page(1, _ident(member_no="", issuer="nimoca"), (),
                         [_line(260, section=SECTION_UNKNOWN),
                          _line(5000, kind=KIND_CHARGE, section=SECTION_UNKNOWN,
                                description="入金 現金")])

        v = _only(led.finalize())
        self.assertEqual(v.bookable_rows, 1)
        self.assertIn((KIND_CHARGE, 1, 5000), v.nonbookable)

    def test_fee_counts_as_expense(self):
        led = _ledger()
        led.observe_page(1, _ident(n=1), (_total("ご請求金額合計", 1500),),
                         [_line(1000), _line(500, kind=KIND_FEE)])

        v = _only(led.finalize())
        self.assertEqual(v.expense_sum, 1500)
        self.assertEqual(v.bookable_rows, 2)

    def test_unknown_kind_is_booked_and_flagged(self):
        """申告外の kind は記帳漏れより過剰記帳に倒し、注記を残す。"""
        led = _ledger()
        led.observe_page(1, _ident(n=1), (_total("ご請求金額合計", 900),),
                         [_line(900, kind=KIND_UNKNOWN)])

        v = _only(led.finalize())
        self.assertEqual(v.bookable_rows, 1)
        self.assertTrue(any("unknown_kind" in n for n in v.notes))

    def test_charge_rows_can_be_booked_by_config_switch(self):
        """TBD-2 が「記帳する」に転んでも動くのは 1 箇所だけであること。

        方針は**構築時に固定する**。途中で変えられると前半の頁が旧方針・
        後半が新方針で集計され、型では防げない不変式違反になる。
        """
        led = FileReconLedger(DocType.TRANSIT_IC, 1, book_charge_rows=True)
        led.observe_page(1, _ident(member_no="", issuer="nimoca"), (),
                         [_line(5000, kind=KIND_CHARGE, section=SECTION_UNKNOWN)])

        self.assertEqual(_only(led.finalize()).bookable_rows, 1)

    def test_charge_rows_are_not_booked_by_default(self):
        led = FileReconLedger(DocType.TRANSIT_IC, 1)
        led.observe_page(1, _ident(member_no="", issuer="nimoca"), (),
                         [_line(5000, kind=KIND_CHARGE, section=SECTION_UNKNOWN)])

        self.assertEqual(_only(led.finalize()).bookable_rows, 0)


class DuplicatePageTest(unittest.TestCase):
    """F-2: 同一原紙の 2 度スキャン。加算すると 17,295 が 34,590 になる。"""

    def test_duplicate_page_is_not_added_twice(self):
        led = _ledger(total_pages=4)
        rows = [_line(x) for x in (630, 5799, 3866, 3220, 630, 630)]
        led.observe_page(2, _ident(n=2, total=6),
                         (_total("今月ご利用額合計", 14775, page=2),), rows)
        # 重複判定は page_dedup（二重署名 AND 規則）の管轄。元帳は自前判定を
        # 持たず、caller の宣言に従って「足さない」だけ（AD-1）。
        led.observe_page(4, _ident(n=2, total=6),
                         (_total("今月ご利用額合計", 14775, page=4),), rows,
                         is_duplicate=True)

        v = _only(led.finalize())
        self.assertEqual(v.detail_sum, 14775, "重複頁が二重加算されている")
        self.assertEqual(v.verdict, VERDICT_DUPLICATE_PAGE)
        self.assertEqual(v.severity, "high")

    def test_duplicate_is_reported_even_when_amounts_match(self):
        """金額が一致していても重複は独立した赤信号（MF には 2 倍書かれている）。"""
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(n=1, total=1),
                         (_total("ご請求金額合計", 1260),), [_line(630), _line(630)])
        led.observe_page(2, _ident(n=1, total=1), (), [_line(630), _line(630)],
                         is_duplicate=True)

        v = _only(led.finalize())
        self.assertEqual(v.verdict, VERDICT_DUPLICATE_PAGE)
        self.assertIn(2, v.dup_pages)


class PageGapTest(unittest.TestCase):
    """`n/N` は結算トリガーではない（F-1: 2 頁しか無いのに合計が一致する）。"""

    def test_missing_pages_with_matching_amount_is_still_a_match(self):
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(n=1, total=6), (), [_line(630) for _ in range(4)])
        led.observe_page(2, _ident(n=2, total=6),
                         (_total("今月ご利用額合計", 17295, page=2),),
                         [_line(x) for x in (630, 5799, 3866, 3220, 630, 630)])

        v = _only(led.finalize())
        self.assertEqual(v.verdict, VERDICT_MATCH)
        self.assertTrue(any("頁欠" in n for n in v.notes), "頁欠の事実は注記に残す")

    def test_missing_pages_with_mismatch_names_the_likely_cause(self):
        """同じ「不一致」でも人の次の行動が違う（再スキャン vs 記帳確認）。"""
        led = _ledger(total_pages=1)
        led.observe_page(1, _ident(n=1, total=6),
                         (_total("今月ご利用額合計", 17295),), [_line(630)])

        v = _only(led.finalize())
        self.assertEqual(v.verdict, VERDICT_MISMATCH_GAP)
        self.assertEqual(v.severity, "high")


class RiboPageZeroBaselineTest(unittest.TestCase):
    """T8 実測（2026-08-19）: リボ頁の 0 円小計を検算基準にしてはいけない。

    実測: アメックス 8 頁券面の p8（ペイフレックス＝リボ頁）を credit_card
    として生産と同一経路に 3 回通したところ、Gemini は毎回
    `printed_totals=[{"label": "今回ご請求金額", "amount": 0}]` と `rows=[]`
    を返した。この「今回ご請求金額」は**リボ区画の小計**であって券面総額ではない。

    現行実装ではこのラベルが `SECTION_UNKNOWN` へ落ち（`TOTAL_LABEL_SECTION`
    は発行体で語義が違うラベルを敢えて登録しない）、`_choose_section_and_total`
    の「unknown が 1 種類だけなら current_usage とみなす」フォールバックで
    **0 円が検算基準に採用される**。明細 0 件なので 0 − 0 = 0 となり
    `VERDICT_MATCH` / severity=None。8 頁のうち 1 頁しか見ていないのに
    「検算一致」と表示される。偽の警告より悪い —— 除外対象の頁が
    「正常に検算済み」に見えるため、誰も異常に気づけない。

    **`PageGapTest` とは衝突しない。** あちらの「`n/N` は結算トリガーではない」
    は印字合計が非 0（17,295）で観測済み明細がそれと一致する形であり、
    F-1 の実測に基づく意図的な仕様。ここで問題にするのは
    **印字合計 0 ＋ 明細 0 件 ＋ 頁欠**の三者が揃った場合だけ。
    真に単頁・当月消費ゼロの券面（頁欠なし）は従来どおり一致でよい。

    **これは既知の欠陥を固定する expectedFailure。装飾子は恒久的に残す。**
    `card_reconciliation` は生産経路に**未接線**（参照はテストのみ）なので
    実害はゼロ。

    **旧記述の訂正（2026-08-21）**: かつてここには「T9 で接線する際に
    この欠陥を塞ぎ、装飾子を外すことが T9 の完了条件」と書いてあった。
    その前提である**接線そのものが実測で否決された** —— カード単位で束ねると
    真票 8 ファイルに偽の不一致が 7 件出る（金額の誤りは 0 件、全部が分組
    起因）。同じ目的（明細行の無音欠落の検出）はファイル単位の検算
    `card_file_recon` が偽警報 0 件で達成している。よって**趙が 2026-08-21 に
    「接線しない」を恒久的に裁定した**。詳細は
    `docs/plans/2026-08-21-t11-e2e-machine-verdict.md` §3.4。

    接線しない以上この欠陥は到達不能であり、塞ぐ動機が無い。将来
    `card_reconciliation` の接線を提案する者は、まず上記の偽不一致 7 件を
    再現し、それを解消できることを示すこと。
    """

    @unittest.expectedFailure
    def test_zero_total_from_ribo_page_must_not_verify_as_match(self):
        led = _ledger(total_pages=1)
        led.observe_page(8, _ident(n=8, total=8),
                         (_total("今回ご請求金額", 0, page=8),), [])

        v = _only(led.finalize())
        self.assertTrue(any("頁欠" in n for n in v.notes),
                        "前提が崩れている: 8 頁中 1 頁しか観測していないはず")
        self.assertEqual(v.printed_total, 0, "前提: 0 円が基準に採られている")
        self.assertEqual(v.detail_sum, 0, "前提: 明細は 0 件")
        self.assertNotEqual(
            v.verdict, VERDICT_MATCH,
            "リボ頁の 0 円小計を基準にした 0 対 0 を「検算一致」と"
            "表示してはいけない（除外対象頁が正常検算済みに見える）")


class CardIdentityTest(unittest.TestCase):
    """F-1: 1 PDF に 3 社分が混在する。頁ごとの帰属判定が要る。"""

    def test_three_cards_in_one_file_are_tallied_independently(self):
        led = _ledger(total_pages=4)
        led.observe_page(1, _ident(member_no="6900-0512-4054-8959", issuer="toyota"),
                         (_total("お支払合計", 44490),), [_line(44490)])
        led.observe_page(2, _ident(member_no="6900-0553-1626-9242", issuer="toyota"),
                         (_total("ご請求金額合計", 97004),), [_line(97004)])
        led.observe_page(3, _ident(member_no="9200-0120-4450-2973", issuer="toyota"),
                         (_total("ご請求金額合計", 131850),), [_line(131850)])

        verdicts = led.finalize()
        self.assertEqual(len(verdicts), 3)
        self.assertEqual({v.verdict for v in verdicts}, {VERDICT_MATCH})

    def test_unreadable_member_no_continues_current_card(self):
        """信頼度の低い頁は**新カードを作らない**（存在しないカードを増やさない）。"""
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(n=1, total=2), (), [_line(7380)])
        led.observe_page(2, _ident(member_no="", n=2, total=2),
                         (_total("ご請求金額合計", 18503, page=2),), [_line(11123)])

        v = _only(led.finalize())
        self.assertEqual(v.detail_sum, 18503)
        self.assertEqual(v.verdict, VERDICT_MATCH)

    def test_masked_suffix_variants_merge_into_one_card(self):
        """`****-******-26003` と `-26003` は同じカード（末尾 5 桁で同定）。"""
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(member_no="****-******-26003", n=1, total=2),
                         (), [_line(630)])
        led.observe_page(2, _ident(member_no="26003", n=2, total=2),
                         (_total("今月ご利用額合計", 1260, page=2),), [_line(630)])

        self.assertEqual(len(led.finalize()), 1)

    def test_same_member_no_merges_when_issuer_is_unreadable_on_some_pages(self):
        """発行体名は先頭頁のヘッダにしか無いことが多い（F-11 の T番号も同様）。

        会員番号は各頁に印字されるので、中頁は `?:26003`、先頭頁は
        `amex:26003` になる。発行体の一致を要求すると 1 通の明細書が
        2 枚のカードに割れ、**両方が偽の不一致（赤系）** になる。
        「?」は「別の発行体」ではなく「読めなかった」を意味する。
        """
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(member_no="****-******-26003", issuer="amex",
                                   n=1, total=2), (), [_line(7380)])
        led.observe_page(2, _ident(member_no="****-******-26003", issuer=None,
                                   n=2, total=2),
                         (_total("ご請求金額合計", 18503, page=2),), [_line(11123)])

        v = _only(led.finalize())
        self.assertEqual(v.detail_sum, 18503)
        self.assertEqual(v.verdict, VERDICT_MATCH)

    def test_merge_direction_does_not_depend_on_page_order(self):
        """発行体が読めない頁が先に来ても同じ結果になること。"""
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(member_no="****-******-26003", issuer=None,
                                   n=1, total=2), (), [_line(7380)])
        led.observe_page(2, _ident(member_no="****-******-26003", issuer="amex",
                                   n=2, total=2),
                         (_total("ご請求金額合計", 18503, page=2),), [_line(11123)])

        self.assertEqual(_only(led.finalize()).verdict, VERDICT_MATCH)

    def test_weak_first_page_is_upgraded_by_a_later_readable_member_no(self):
        """先頭頁の識別が読めず、次頁で会員番号が読めた場合。

        先頭頁は positional_weak の仮キーで始まる。次頁で確かな会員番号が
        読めたときに**新しいカードを作ると**、印字合計（先頭頁）と明細（次頁）が
        別々の元帳へ割れ、両方が偽の不一致になる。頁が連続しているなら
        仮キーのカードを本キーへ引き継ぐ。
        """
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(member_no="", issuer="amex", n=1, total=2),
                         (_total("ご請求金額合計", 18503),), [_line(7380)])
        led.observe_page(2, _ident(member_no="****-******-26003", issuer="amex",
                                   n=2, total=2), (), [_line(11123)])

        v = _only(led.finalize())
        self.assertEqual(v.detail_sum, 18503)
        self.assertEqual(v.verdict, VERDICT_MATCH)
        self.assertEqual(v.key_confidence, "member_no")

    def test_upgrade_does_not_swallow_a_genuinely_new_card(self):
        """頁が連続していないなら引き継がない（別カードの合流を防ぐ）。"""
        led = _ledger(total_pages=3)
        led.observe_page(1, _ident(member_no="", issuer="amex", n=1, total=1),
                         (_total("ご請求金額合計", 7380),), [_line(7380)])
        led.observe_page(3, _ident(member_no="****-******-11004", issuer="amex",
                                   n=1, total=1),
                         (_total("ご請求金額合計", 11123),), [_line(11123)])

        self.assertEqual(len(led.finalize()), 2)

    def test_whitespace_only_issuer_counts_as_unreadable(self):
        """OCR が空白だけを返した発行体欄は「別の発行体」ではない。

        哨兵に畳まないと `_find_mergeable` が衝突と誤認し、同じカードの頁が
        別々の元帳へ割れて偽の不一致が出る。
        """
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(member_no="****-******-26003", issuer="amex",
                                   n=1, total=2), (), [_line(7380)])
        led.observe_page(2, _ident(member_no="****-******-26003", issuer="  　 ",
                                   n=2, total=2),
                         (_total("ご請求金額合計", 18503, page=2),), [_line(11123)])

        self.assertEqual(_only(led.finalize()).verdict, VERDICT_MATCH)

    def test_different_issuers_with_same_tail_digits_stay_separate(self):
        """両方の発行体が読めているなら、末尾 5 桁が同じでも別カード。"""
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(member_no="1111-1111-1111-26003", issuer="amex"),
                         (_total("ご請求金額合計", 100),), [_line(100)])
        led.observe_page(2, _ident(member_no="2222-2222-2222-26003", issuer="jcb"),
                         (_total("ご請求金額合計", 200),), [_line(200)])

        self.assertEqual(len(led.finalize()), 2)

    def test_statement_no_is_used_when_member_no_missing(self):
        led = _ledger(total_pages=1)
        led.observe_page(1, _ident(member_no="", statement_no="S-778"),
                         (_total("ご請求金額合計", 100),), [_line(100)])

        v = _only(led.finalize())
        self.assertEqual(v.key_confidence, "statement_no")

    def test_statement_no_card_still_absorbs_its_following_pages(self):
        """会員番号が 1 頁も読めない明細書でも 1 枚のカードに畳まれること。

        `current_key` を `member_no` 由来のときだけ更新すると、この文書は
        頁ごとに別カードへ割れ、実在しないカードが台帳に増えたうえで
        全カードが偽の不一致になる（D2 §2.2 のフォールバック連鎖 3/4 が
        到達不能になる）。
        """
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(member_no="", statement_no="S-778", n=1, total=2),
                         (), [_line(7380)])
        led.observe_page(2, _ident(member_no="", n=2, total=2),
                         (_total("ご請求金額合計", 18503, page=2),), [_line(11123)])

        v = _only(led.finalize())
        self.assertEqual(v.detail_sum, 18503)
        self.assertEqual(v.verdict, VERDICT_MATCH)

    def test_positional_weak_card_still_absorbs_its_following_pages(self):
        """識別情報が皆無の文書でも頁ごとにカードを増やさない。"""
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(member_no="", issuer=None, n=1, total=2),
                         (), [_line(7380)])
        led.observe_page(2, _ident(member_no="", issuer=None, n=2, total=2),
                         (_total("ご請求金額合計", 18503, page=2),), [_line(11123)])

        v = _only(led.finalize())
        self.assertEqual(v.detail_sum, 18503)
        self.assertEqual(v.verdict, VERDICT_MATCH)

    def test_overflow_bucket_does_not_become_the_current_card(self):
        """溢れバケットに落ちた後も、実カードの帰属推定を汚染しない。"""
        led = _ledger(total_pages=64)
        for i in range(MAX_CARDS_PER_FILE + 4):
            led.observe_page(i + 1, _ident(member_no="4980-0000-%04d-%04d" % (i, i)),
                             (), [_line(100)])

        self.assertNotEqual(led.current_key, "__overflow__")


class SectionSelectionTest(unittest.TestCase):
    """区画の決定は Gemini に委ねず Python の決定表で行う（AD-4 / §3.3）。"""

    def test_known_labels_map_to_sections(self):
        self.assertEqual(section_for_label("今月ご利用額合計"), SECTION_CURRENT_USAGE)
        self.assertEqual(section_for_label("お支払い金額合計"), SECTION_PAYMENT_SUMMARY)
        self.assertEqual(section_for_label("よく分からない見出し"), SECTION_UNKNOWN)

    def test_labels_with_suffixes_resolve_via_partial_match(self):
        """F-3 の実測ラベルは接尾辞つきで現れる（`ご請求金額合計(A)` 等）。

        完全一致だけに頼ると、この形が SECTION_UNKNOWN に落ちて
        「区画曖昧」という**誤った診断名**が出る。変異テストで部分一致経路を
        殺しても全テストが通ってしまったため、ここで縛る。
        """
        cases = (
            ("ご請求金額合計(A)", SECTION_CURRENT_USAGE),      # ENEOS BUSINESS（F-3）
            ("今回ご請求内容 合計(A)", SECTION_PAYMENT_SUMMARY),
            ("  お支払合計  ", SECTION_CURRENT_USAGE),
            ("このカードのポイント明細", SECTION_POINT),
        )
        for label, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(section_for_label(label), expected)

    def test_longer_labels_win_over_their_own_prefixes(self):
        """「ご請求金額合計」が「ご請求金額」に食われないこと。

        食われると current_usage が曖昧ラベル扱いになり、区画選択が
        フォールバックへ落ちる。
        """
        self.assertEqual(section_for_label("ご請求金額合計"), SECTION_CURRENT_USAGE)
        self.assertEqual(section_for_label("ご請求金額"), SECTION_UNKNOWN)

    def test_suffixed_label_actually_reaches_reconciliation(self):
        """決定表単体ではなく、元帳を通しても効いていることを固定する。"""
        led = _ledger(total_pages=1)
        led.observe_page(1, _ident(issuer="toyota"),
                         (_total("ご請求金額合計(A)", 97004),), [_line(97004)])

        v = _only(led.finalize())
        self.assertEqual(v.section_used, SECTION_CURRENT_USAGE)
        self.assertEqual(v.verdict, VERDICT_MATCH)

    def test_single_total_document_is_treated_as_current_usage(self):
        """区画が 1 つしかない文書（ENEOS個人系・UC・JCB 等）の既定。"""
        led = _ledger(total_pages=1)
        led.observe_page(1, _ident(), (_total("今回ご請求金額", 79050),),
                         [_line(79050, section=SECTION_UNKNOWN)])

        v = _only(led.finalize())
        self.assertEqual(v.verdict, VERDICT_MATCH)

    def test_ambiguous_sections_refuse_to_guess(self):
        """区画が複数あって current_usage を特定できないなら推測しない。"""
        led = _ledger(total_pages=1)
        led.observe_page(1, _ident(),
                         (_total("今回ご請求金額", 100), _total("今回のお支払金額", 200)),
                         [_line(100)])

        v = _only(led.finalize())
        self.assertIn("検算不能", v.verdict)
        self.assertEqual(v.severity, "medium")

    def test_conflicting_totals_for_same_section_are_unverifiable(self):
        """同一区画の印字合計が食い違う（F-3: 先頭頁と明細末尾の 2 箇所）。"""
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(n=1), (_total("今月ご利用額合計", 17295),), [_line(630)])
        led.observe_page(2, _ident(n=2), (_total("今月ご利用額合計", 17296, page=2),),
                         [_line(630)])

        v = _only(led.finalize())
        self.assertIn("検算不能", v.verdict)

    def test_same_total_printed_twice_is_not_a_conflict(self):
        """アレコレは 1 枚目・2 枚目の両方に同じ合計を印字する（F-3）。"""
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(n=1), (_total("ご請求金額", 54788),), [_line(20000)])
        led.observe_page(2, _ident(n=2), (_total("ご請求金額", 54788, page=2),),
                         [_line(34788)])

        v = _only(led.finalize())
        self.assertEqual(v.verdict, VERDICT_MATCH)

    def test_payment_summary_alone_is_not_used_as_a_usage_baseline(self):
        """アメックス カードB 型（F-5）で「お支払い金額合計」だけ読めた場合。

        この値は前回分口座振替 −35,808 を含む別区画の小計なので、当月利用の
        明細と突合すると 35,808 円ちょうどずれた**偽の不一致（赤系）**が出る。
        基準が無いなら検算不能へ倒すのが正しい（推測で基準を作らない）。
        """
        led = _ledger(total_pages=1)
        led.observe_page(
            1, _ident(member_no="****-******-11004"),
            (_total("お支払い金額合計", 897872),),
            [_line(29640), _line(300000), _line(587440), _line(16600),
             _line(-35808, kind=KIND_CARRY_OVER)])

        v = _only(led.finalize())
        self.assertEqual(v.verdict, VERDICT_UNVERIFIABLE)
        self.assertIsNone(v.printed_total)

    def test_unknown_section_lines_are_added_and_flagged(self):
        """過小警告（静かな誤り）より過剰警告に倒す。"""
        led = _ledger(total_pages=1)
        led.observe_page(1, _ident(),
                         (_total("今月ご利用額合計", 100),
                          _total("お支払い金額合計", 999)),
                         [_line(60), _line(40, section=SECTION_UNKNOWN)])

        v = _only(led.finalize())
        self.assertEqual(v.detail_sum, 100)
        self.assertTrue(any("unknown_section" in n for n in v.notes))


class NegativeSignParsingTest(unittest.TestCase):
    """負号のゆれを取りこぼすと F-4 の黄金検算が崩れる。

    台帳 F-5 の原文は `ENEOSポイントキャッシュバック −3,000`（U+2212）。
    ASCII の `-` しか認めないと、この行が `unparsable_amount` として
    検算母集合から落ち、15,503 が 18,503 になって偽の不一致が出る。
    """

    def test_japanese_minus_glyphs_parse_as_negative(self):
        for text in ("−3,000", "－3,000", "▲3,000", "△3,000", "‐3,000", "-3,000"):
            with self.subTest(text=text):
                self.assertEqual(_coerce_int(text), -3000)

    def test_eneos_golden_holds_with_a_unicode_minus(self):
        led = _ledger(total_pages=1)
        led.observe_page(
            1, _ident(member_no="XXXX-XXXX-XXXX-8602", issuer="eneos"),
            (_total("ご請求金額合計", "15,503"),),
            [_line("7,380"), _line("11,123"),
             _line("−3,000", kind=KIND_CREDIT_ADJUST)])

        v = _only(led.finalize())
        self.assertEqual(v.detail_sum, 15503)
        self.assertEqual(v.verdict, VERDICT_MATCH)

    def test_dashes_inside_text_are_not_read_as_signs(self):
        """店名や区間名のダッシュを符号として解釈しない。"""
        self.assertIsNone(_coerce_int("1-2-3"))
        self.assertIsNone(_coerce_int("天神−博多"))


class PointSectionTest(unittest.TestCase):
    """ポイント区画の数値は円貨の勘定ではない（F-6）。検算母集合に混ぜない。"""

    def test_point_labels_map_to_the_point_section(self):
        for label in ("このカードのポイント明細", "ポイント明細", "ポイント残高"):
            with self.subTest(label=label):
                self.assertEqual(section_for_label(label), SECTION_POINT)

    def test_point_total_alone_does_not_become_the_reconciliation_baseline(self):
        """ENEOS p1 型: ポイント残高しか読めなかった頁。

        ポイント区画を除外しないと「印字合計が 1 つだけなら current_usage」の
        フォールバックが**ポイント残高 590 を請求金額として検算**してしまい、
        必ず不一致になる（しかも原因が判らない）。
        """
        led = _ledger(total_pages=1)
        led.observe_page(1, _ident(issuer="eneos"),
                         (_total("このカードのポイント明細", 590),), [_line(7380)])

        v = _only(led.finalize())
        self.assertEqual(v.verdict, VERDICT_UNVERIFIABLE)
        self.assertIsNone(v.printed_total)

    def test_point_total_does_not_make_a_real_total_ambiguous(self):
        """請求合計とポイント残高が同居しても、請求合計が選ばれること。"""
        led = _ledger(total_pages=1)
        led.observe_page(1, _ident(issuer="eneos"),
                         (_total("ご請求金額合計", 15503),
                          _total("ポイント残高", 590)),
                         [_line(7380), _line(11123), _line(-3000, kind=KIND_CREDIT_ADJUST)])

        v = _only(led.finalize())
        self.assertEqual(v.printed_total, 15503)
        self.assertEqual(v.verdict, VERDICT_MATCH)


class LabelTableDriftTest(unittest.TestCase):
    """page_family の合計ラベル語彙との乖離を検出する。

    片方にだけ新しい表記を足すと、`section_for_label` が未知ラベル扱いして
    SECTION_UNKNOWN に落ち、「区画曖昧」という**誤った診断名**が出る。
    コードの欠落だとは気づけないので、テストで縛る。
    """

    def test_section_table_keys_are_known_total_labels(self):
        import page_family

        section_labels = {
            label for label, section in TOTAL_LABEL_SECTION.items()
            if section != SECTION_POINT   # ポイント語は page_family 側では別カテゴリ
        }
        unknown = section_labels - set(page_family._TOTAL_LABEL_TOKENS)
        self.assertEqual(unknown, set(),
                         "page_family._TOTAL_LABEL_TOKENS に無い合計ラベル: %s" % unknown)

    def test_ambiguous_labels_are_deliberately_unmapped(self):
        """発行体ごとに指す区画が違うラベルは、敢えて区画表に登録しない。

        登録すると推測で片方の区画に倒すことになる。未登録なら
        「区画が 1 つしかない文書」のフォールバックが効き、複数区画あるなら
        検算不能へ倒れる（どちらも推測しない）。
        """
        for label in ("今回ご請求金額", "今回のお支払金額", "ご請求金額"):
            with self.subTest(label=label):
                self.assertNotIn(label, TOTAL_LABEL_SECTION)

    def test_severity_vocabulary_matches_tag_rules(self):
        """severity は tag_rules の正典語彙と綴りが一致すること。

        配線時（CardVerdict.severity → 遡及標色）に静かに不一致になると、
        赤系マークが付かないまま検算不一致が見逃される。
        """
        from tag_rules import SEVERITY_RANK

        self.assertIn(SEVERITY_HIGH, SEVERITY_RANK)
        self.assertIn(SEVERITY_MEDIUM, SEVERITY_RANK)
        self.assertGreater(SEVERITY_RANK[SEVERITY_HIGH], SEVERITY_RANK[SEVERITY_MEDIUM])

    def test_amount_coercion_agrees_with_page_dedup(self):
        """券面金額の数値化が 3 モジュールで一致していること。

        同じ概念の実装が 3 つあるので、片方だけ直す（例: 全角数字対応）と
        検算と記帳で解釈が割れる。モジュール境界は保ったまま挙動を縛る。
        """
        import invoice_classification
        import page_dedup

        samples = (0, 1, -3000, 17295, "1,234", "¥5,000", "abc", "", None,
                   True, False, 12.7, [], {},
                   # 負号のゆれ（F-5 の台帳原文は U+2212）
                   "-3,000", "−3,000", "－3,000", "▲3,000", "△3,000",
                   "‐3,000", "3,000円", "　1,234　", "1-2-3", "--5",
                   # 実装方式の差が出る入力（`int()` は受け入れ fullmatch は拒む）。
                   # ここを入れないと「3 者一致」の突合が乖離を見逃す。
                   "1_000", "1_0", "+12", "٣", "１２３", " 12 ", "12.0", "1e3")
        for value in samples:
            with self.subTest(value=repr(value)):
                self.assertEqual(_coerce_int(value), page_dedup._coerce_int(value))
                self.assertEqual(_coerce_int(value),
                                 invoice_classification._coerce_amount(value))


class HandwrittenTotalTest(unittest.TestCase):
    """F-10: 楽天 p1 の手書き「ETC代 12,940円」は印字の請求金額欄に重なる。"""

    def test_handwritten_total_is_never_used_as_the_baseline(self):
        led = _ledger(total_pages=1)
        led.observe_page(1, _ident(issuer="rakuten"),
                         (_total("ご請求金額", 12940, is_handwritten=True),),
                         [_line(1460)])

        v = _only(led.finalize())
        self.assertEqual(v.verdict, VERDICT_UNVERIFIABLE)
        self.assertTrue(any("handwritten" in n for n in v.notes))


class TransitIcTest(unittest.TestCase):
    """F-7: nimoca には合計行が構造的に存在しない。件数だけが検算できる。"""

    def test_no_printed_total_is_not_applicable_and_not_a_warning(self):
        """毎月来る文書なので警告色を付けたら意味が磨滅する（狼少年化）。"""
        led = _ledger(doc_type=DocType.TRANSIT_IC, total_pages=1)
        led.observe_page(1, _ident(member_no="", issuer="nimoca"), (),
                         [_line(260, section=SECTION_UNKNOWN) for _ in range(20)])

        v = _only(led.finalize())
        self.assertEqual(v.verdict, VERDICT_NOT_APPLICABLE)
        self.assertIsNone(v.severity)

    def test_printed_count_matches_including_charge_rows(self):
        """「全 XXX 件」は入金行を含む一覧の件数（記帳の可否とは無関係）。"""
        led = _ledger(doc_type=DocType.TRANSIT_IC, total_pages=1)
        lines = [_line(260, section=SECTION_UNKNOWN) for _ in range(19)]
        lines.append(_line(5000, kind=KIND_CHARGE, section=SECTION_UNKNOWN))
        led.observe_page(1, _ident(member_no="", issuer="nimoca"),
                         (_total("全20件", None, count=20),), lines)

        v = _only(led.finalize())
        self.assertEqual(v.verdict, VERDICT_COUNT_OK)
        self.assertEqual(v.detail_count, 20)
        self.assertEqual(v.bookable_rows, 19)

    def test_count_mismatch_is_medium_not_high(self):
        led = _ledger(doc_type=DocType.TRANSIT_IC, total_pages=1)
        led.observe_page(1, _ident(member_no="", issuer="nimoca"),
                         (_total("全20件", None, count=20),),
                         [_line(260, section=SECTION_UNKNOWN) for _ in range(18)])

        v = _only(led.finalize())
        self.assertEqual(v.verdict, VERDICT_COUNT_NG)
        self.assertEqual(v.severity, "medium")

    def test_credit_card_without_total_is_unverifiable_not_not_applicable(self):
        """クレカで合計が取れないのは OCR 失敗を疑うべき事象。nimoca と混ぜない。"""
        led = _ledger(doc_type=DocType.CREDIT_CARD, total_pages=1)
        led.observe_page(1, _ident(), (), [_line(630)])

        v = _only(led.finalize())
        self.assertEqual(v.verdict, VERDICT_UNVERIFIABLE)
        self.assertEqual(v.severity, "medium")


class DegradationTest(unittest.TestCase):
    """検算不能への降格。誤った診断名で人の時間を溶かさない。"""

    def test_page_errors_downgrade_all_cards(self):
        """頁エラーの明細は Sheets に書かれていない＝必ず不一致になる。"""
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(n=1), (_total("ご請求金額合計", 17295),), [_line(630)])

        v = _only(led.finalize(had_page_errors=True))
        self.assertIn("頁エラー", v.verdict)
        self.assertEqual(v.severity, "medium")

    def test_partial_scan_downgrades(self):
        """`--start-page N` の再開実行では前半の明細が元帳に無い。"""
        led = _ledger(total_pages=5, start_page=3)
        led.observe_page(3, _ident(n=3), (_total("ご請求金額合計", 17295),), [_line(630)])

        self.assertIn("部分処理", _only(led.finalize()).verdict)

    def test_card_overflow_degrades_visibly(self):
        """上限超過は捨てるのではなく 1 個の可視な劣化事実にする。"""
        led = _ledger(total_pages=64)
        for i in range(MAX_CARDS_PER_FILE + 24):
            led.observe_page(i + 1, _ident(member_no="4980-0000-%04d-%04d" % (i, i)),
                             (), [_line(100)])

        verdicts = led.finalize()
        self.assertTrue(led.degraded)
        self.assertLessEqual(len(verdicts), MAX_CARDS_PER_FILE + 1)
        self.assertTrue(all("カード数超過" in v.verdict for v in verdicts))


class MemoryInvariantTest(unittest.TestCase):
    """Generator Pipeline のメモリモデルを壊さない（行数非依存）。"""

    def test_ledger_size_does_not_grow_with_row_count(self):
        led = _ledger(total_pages=5)
        for page in range(1, 6):
            led.observe_page(page, _ident(n=page, total=5), (),
                             [_line(500 + i) for i in range(100)])

        self.assertLess(led.stored_object_count(), 200)

    def test_ledger_does_not_retain_detail_lines(self):
        led = _ledger(total_pages=1)
        led.observe_page(1, _ident(n=1), (),
                         [_line(777, description="ユニークな加盟店名XYZ")])

        self.assertNotIn("ユニークな加盟店名XYZ", repr(led.__dict__))


class FailOpenTest(unittest.TestCase):
    """元帳の失敗が記帳経路を壊さない（検算はあくまで検査器）。"""

    def test_observe_page_never_raises(self):
        led = _ledger()
        for bad_lines in (None, [None], [object()], "文字列"):
            with self.subTest(lines=repr(bad_lines)[:20]):
                key = led.observe_page(1, _ident(n=1), (), bad_lines)
                self.assertIsInstance(key, str)

    def test_broken_ident_does_not_raise(self):
        led = _ledger()
        for bad in (None, object()):
            with self.subTest(ident=repr(bad)[:20]):
                self.assertIsInstance(led.observe_page(1, bad, (), []), str)

    def test_broken_printed_total_does_not_raise(self):
        led = _ledger()
        led.observe_page(1, _ident(n=1), (None, object()), [_line(100)])
        self.assertTrue(led.finalize())

    def test_finalize_on_empty_ledger_returns_empty(self):
        self.assertEqual(_ledger().finalize(), ())

    def test_an_input_that_actually_raises_is_swallowed(self):
        """握りつぶし分岐そのものを通す。

        従来の壊れ入力（None / [object()] / "文字列"）はいずれも内部で
        例外を起こさず正常経路を素通りしていたため、except 本体は 1 度も
        実行されていなかった。属性アクセスで例外を投げる ident を渡して
        本当に fail-open するかを確かめる。
        """
        class _Exploding:
            @property
            def member_no(self):
                raise RuntimeError("boom")

        led = _ledger()
        key = led.observe_page(1, _Exploding(), (), [_line(100)])

        self.assertIsInstance(key, str)
        self.assertEqual(led.finalize(), ())     # 何も畳み込まれていない

    def test_a_raising_page_does_not_corrupt_the_pages_around_it(self):
        """1 頁の事故が前後の頁の集計を壊さないこと。"""
        class _Exploding:
            @property
            def member_no(self):
                raise RuntimeError("boom")

        led = _ledger(total_pages=3)
        led.observe_page(1, _ident(n=1), (), [_line(7380)])
        led.observe_page(2, _Exploding(), (), [_line(999999)])
        led.observe_page(3, _ident(n=3),
                         (_total("ご請求金額合計", 18503, page=3),), [_line(11123)])

        v = _only(led.finalize())
        self.assertEqual(v.detail_sum, 18503, "事故頁の金額が混入している")
        self.assertTrue(any("ledger_error" in n for n in v.notes))

    def test_unparsable_amounts_are_noted_not_silently_dropped(self):
        led = _ledger(total_pages=1)
        led.observe_page(1, _ident(n=1), (_total("ご請求金額合計", 100),),
                         [_line(100), _line("金額不明")])

        v = _only(led.finalize())
        self.assertTrue(any("unparsable_amount" in n for n in v.notes))

    def test_genuinely_invalid_kind_string_is_coerced_and_noted(self):
        """Gemini が語彙外の文字列を返した場合の防御分岐。

        従来のテストは正規メンバー `KIND_UNKNOWN` を渡していたため、
        「申告外の kind」を名乗りながら本来の防御経路を通っていなかった。
        """
        led = _ledger(total_pages=1)
        led.observe_page(1, _ident(n=1), (_total("ご請求金額合計", 900),),
                         [_line(900, kind="謎の種別")])

        v = _only(led.finalize())
        self.assertEqual(v.bookable_rows, 1, "語彙外は記帳漏れより過剰記帳に倒す")
        self.assertTrue(any("unknown_kind:謎の種別" in n for n in v.notes))


class DiagnosisNameTest(unittest.TestCase):
    """検算不能の**診断名**を固定する。

    モジュールの目的は「誤った診断名で人の時間を溶かさない」ことなので、
    どの理由で検算できなかったのかが区別できなければ意味がない。
    """

    def test_conflicting_totals_are_named_as_such(self):
        led = _ledger(total_pages=2)
        led.observe_page(1, _ident(n=1), (_total("今月ご利用額合計", 17295),), [_line(630)])
        led.observe_page(2, _ident(n=2), (_total("今月ご利用額合計", 17296, page=2),),
                         [_line(630)])

        self.assertEqual(_only(led.finalize()).verdict, "検算不能(合計不整合)")

    def test_ambiguous_sections_are_named_as_such(self):
        led = _ledger(total_pages=1)
        led.observe_page(1, _ident(),
                         (_total("今回ご請求金額", 100), _total("今回のお支払金額", 200)),
                         [_line(100)])

        self.assertEqual(_only(led.finalize()).verdict, "検算不能(区画曖昧)")

    def test_each_degradation_has_a_distinct_name(self):
        """降格理由が互いに区別できること（人の次の行動が違う）。"""
        page_error = _ledger(total_pages=1)
        page_error.observe_page(1, _ident(n=1), (_total("ご請求金額合計", 1),), [_line(1)])

        partial = _ledger(total_pages=5, start_page=3)
        partial.observe_page(3, _ident(n=3), (_total("ご請求金額合計", 1),), [_line(1)])

        names = {
            _only(page_error.finalize(had_page_errors=True)).verdict,
            _only(partial.finalize()).verdict,
        }
        self.assertEqual(len(names), 2, "降格理由が同じ名前に潰れている")
        self.assertIn("検算不能(頁エラー)", names)
        self.assertIn("検算不能(部分処理)", names)


class RegistryTest(unittest.TestCase):
    """RECON_POLICY の登録漏れは「検算が黙って掛からない」状態を生む。

    CLAUDE.md が名指しする ENTRY_BUILDERS 未登録と同じ破壊様式なので、
    import 時ではなくここでも全 DocType の網羅を固定する。
    """

    def test_recon_policy_covers_every_doc_type(self):
        for doc_type in DocType.ALL:
            with self.subTest(doc_type=doc_type):
                self.assertIn(doc_type, RECON_POLICY)

    def test_new_doc_types_have_the_expected_policy(self):
        self.assertEqual(RECON_POLICY[DocType.CREDIT_CARD], "amount_required")
        self.assertEqual(RECON_POLICY[DocType.TRANSIT_IC], "count_only")

    def test_existing_doc_types_are_not_reconciled(self):
        for doc_type in (DocType.RECEIPT, DocType.PURCHASE_INVOICE,
                         DocType.SALES_INVOICE, DocType.SALARY_SLIP):
            with self.subTest(doc_type=doc_type):
                self.assertEqual(RECON_POLICY[doc_type], "n/a")

    def test_non_reconciled_doc_type_produces_no_verdicts(self):
        led = _ledger(doc_type=DocType.RECEIPT, total_pages=1)
        led.observe_page(1, _ident(), (_total("ご請求金額合計", 100),), [_line(100)])
        self.assertEqual(led.finalize(), ())


if __name__ == "__main__":
    unittest.main()
