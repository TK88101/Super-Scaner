"""ファイル単位の突合（B 案）のテスト。

Plan: docs/plans/2026-08-21-recon-b-missing-line-alert.md

**この検算器の存在理由は 1 つだけ** —— 行や頁が無音で消えるのを防ぐこと。
記帳の正しさは判定しない（趙裁定 2026-08-21）。したがってテストも
「消えたものを検出できるか」「消えていないのに騒がないか」の 2 軸で書く。

入力は**すべて合成データ**。真票（fixtures/）は PUBLIC repo に置けないので、
テストは自足していなければならない。合成データの形は原票の構造を写しているが、
金額・店名は架空である。
"""
import unittest

import card_file_recon as cfr
import card_reconciliation as cr
from doc_types import DocType


# ── 合成データの組み立て ────────────────────────────────────────────
def _obs(page_num, detail_sum, totals=(), n=None, total_n=None,
         is_duplicate=False):
    """PageObservation を 1 つ作る。

    totals は ((ラベル, 金額), ...)。ラベルは `section_for_label` が
    current_usage を返すものだけが実際には載る（observe_page が濾す）ので、
    finalize のテストでは濾過済みの前提で書く。
    """
    return cfr.PageObservation(page_num=page_num, detail_sum=detail_sum,
                               totals=tuple(totals), statement_n=n,
                               statement_total=total_n,
                               is_duplicate=is_duplicate)


def _raw(rows=(), totals=(), statement_page="", sections=None):
    """observe_page に食わせる raw_data（Gemini 応答の形）。"""
    return {
        "card": {"issuer": "テスト発行体", "member_no": "0000-0000-0000-0001",
                 "statement_page": statement_page, "statement_no": ""},
        "sections": [] if sections is None else list(sections),
        "printed_totals": [
            {"label": lab, "amount": amt, "count": None, "page": 1,
             "is_handwritten": False} for lab, amt in totals],
        "rows": [dict(r) for r in rows],
    }


def _row(amount, kind="expense", merchant="テスト店", note="", sec=None):
    return {"line_no": 1, "date": "2026/05/01", "amount": amount, "kind": kind,
            "merchant": merchant, "note": note, "sec": sec,
            "account_hint": "", "debit_account": ""}


CC = DocType.CREDIT_CARD


class BasicMatchTest(unittest.TestCase):
    """1 通の明細書。券面合計と明細和が一致する / しない。"""

    def test_single_statement_matches(self):
        obs = [_obs(1, 0, [("ご請求金額合計", 10000)], n=1, total_n=2),
               _obs(2, 10000, [], n=2, total_n=2)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.verdict, cr.VERDICT_MATCH)
        self.assertEqual(v.printed_total, 10000)
        self.assertEqual(v.detail_sum, 10000)
        self.assertEqual(v.diff, 0)

    def test_missing_line_is_detected(self):
        """明細が 1 行落ちたら不一致になる。**これが本検算器の存在理由。**"""
        obs = [_obs(1, 0, [("ご請求金額合計", 10000)], n=1, total_n=2),
               _obs(2, 7000, [], n=2, total_n=2)]      # 3000 円の行が消えた
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.verdict, cr.VERDICT_MISMATCH)
        self.assertEqual(v.diff, -3000)

    def test_extra_line_is_detected(self):
        obs = [_obs(1, 0, [("ご請求金額合計", 10000)], n=1, total_n=2),
               _obs(2, 12000, [], n=2, total_n=2)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.verdict, cr.VERDICT_MISMATCH)
        self.assertEqual(v.diff, 2000)


class MultiStatementTest(unittest.TestCase):
    """1 つの PDF に明細書が複数通（TS CUBIC 型）。分組せずに足すだけで合う。"""

    def test_three_statements_sum_up(self):
        obs = [_obs(1, 0, [], n=1, total_n=1),
               _obs(2, 44490, [("お支払合計", 44490)], n=1, total_n=1),
               _obs(3, 0, [], n=1, total_n=2),
               _obs(4, 97004, [("ご請求金額合計(A)", 97004)], n=2, total_n=2),
               _obs(5, 0, [("今回ご請求金額合計(A)", 131850)], n=1, total_n=5),
               _obs(6, 53290, [], n=2, total_n=5),
               _obs(7, 43540, [], n=3, total_n=5),
               _obs(8, 27860, [], n=4, total_n=5),
               _obs(9, 7160, [("ご請求金額合計(A)", 131850)], n=5, total_n=5)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.printed_total, 44490 + 97004 + 131850)
        self.assertEqual(v.detail_sum, 273344)
        self.assertEqual(v.verdict, cr.VERDICT_MATCH)


class SameAmountDedupTest(unittest.TestCase):
    """AD-2b: 同額の合計は**段の内側だけ**で潰す。

    段をまたぐ同額を潰すと、2 通が偶然同額のときに 1 通が丸ごと落ちても
    「一致」になってしまう（Codex 評審 高-1 の反例）。それは本検算器が
    防ごうとしている当のものなので、絶対に潰してはいけない。
    """

    def test_same_amount_within_one_statement_is_deduped(self):
        """表紙と末頁に同じ合計が印字される（ENEOS 型）。1 回だけ数える。"""
        obs = [_obs(1, 0, [("ご請求金額合計", 15503)], n=1, total_n=2),
               _obs(2, 15503, [("ご請求金額合計", 15503)], n=2, total_n=2)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.printed_total, 15503, "同一段の再印字は 1 回だけ")
        self.assertEqual(v.verdict, cr.VERDICT_MATCH)

    def test_same_amount_across_statements_is_not_deduped(self):
        """Codex 評審 高-1 の反例。2 通が偶然同額で、両方とも健全な場合。"""
        obs = [_obs(1, 10000, [("ご請求金額合計", 10000)], n=1, total_n=1),
               _obs(2, 10000, [("ご請求金額合計", 10000)], n=1, total_n=1)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.printed_total, 20000, "別段の同額は潰さない")
        self.assertEqual(v.verdict, cr.VERDICT_MATCH)

    def test_second_statement_vanishing_is_detected(self):
        """**反例の本体**: 2 通が偶然同額 + 2 通目の明細が丸ごと落ちた。

        素朴な金額去重だと 右辺 10000 / 左辺 10000 で「一致」に化ける。
        段判定を入れれば 右辺 20000 / 左辺 10000 で不一致になる。
        """
        obs = [_obs(1, 10000, [("ご請求金額合計", 10000)], n=1, total_n=1),
               _obs(2, 0, [("ご請求金額合計", 10000)], n=1, total_n=1)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.verdict, cr.VERDICT_MISMATCH,
                         "明細書が丸ごと消えたのに気づかないのは失格")
        self.assertEqual(v.printed_total, 20000)
        self.assertEqual(v.detail_sum, 10000)

    def test_same_amount_on_the_same_page_is_deduped(self):
        """同一頁に同額が別ラベルで複数印字される（TS CUBIC p2 型）。"""
        obs = [_obs(1, 44490, [("お支払合計", 44490),
                               ("今回ご利用・ご請求金額合計", 44490)],
                    n=1, total_n=1)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.printed_total, 44490)
        self.assertEqual(v.verdict, cr.VERDICT_MATCH)


class SegmentAnomalyTest(unittest.TestCase):
    """段判定の異常系（複審の 3 条件）。疑わしきは検算不能へ倒す。"""

    def test_unreadable_page_number_is_unverifiable(self):
        obs = [_obs(1, 0, [("ご請求金額合計", 10000)], n=None, total_n=None),
               _obs(2, 10000, [], n=None, total_n=None)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.verdict, cr.VERDICT_UNVERIFIABLE)

    def test_partial_page_number_is_unverifiable(self):
        """一部の頁だけ n が読めないのも検算不能（段が確定しないため）。"""
        obs = [_obs(1, 0, [("ご請求金額合計", 10000)], n=1, total_n=2),
               _obs(2, 10000, [], n=None, total_n=None)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.verdict, cr.VERDICT_UNVERIFIABLE)

    def test_total_pages_changing_within_a_segment_is_unverifiable(self):
        """同一段の途中で N が変わる＝券面の読み違い。判定しない。"""
        obs = [_obs(1, 0, [("ご請求金額合計", 10000)], n=1, total_n=2),
               _obs(2, 10000, [], n=2, total_n=3)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.verdict, cr.VERDICT_UNVERIFIABLE)

    def test_page_number_going_backwards_starts_a_new_segment(self):
        obs = [_obs(1, 5000, [("ご請求金額合計", 5000)], n=1, total_n=2),
               _obs(2, 0, [], n=2, total_n=2),
               _obs(3, 8000, [("ご請求金額合計", 8000)], n=1, total_n=2),
               _obs(4, 0, [], n=2, total_n=2)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.printed_total, 13000)
        self.assertEqual(v.verdict, cr.VERDICT_MATCH)


class DuplicatePageTest(unittest.TestCase):
    """重複スキャンされた頁は左辺にも右辺にも入れない。"""

    def test_duplicate_page_is_excluded_from_both_sides(self):
        obs = [_obs(1, 0, [("ご請求金額合計", 10000)], n=1, total_n=2),
               _obs(2, 10000, [], n=2, total_n=2),
               _obs(3, 0, [("ご請求金額合計", 10000)], n=1, total_n=2,
                    is_duplicate=True),
               _obs(4, 10000, [], n=2, total_n=2, is_duplicate=True)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.detail_sum, 10000, "重複頁の明細を足すと 2 倍になる")
        self.assertEqual(v.printed_total, 10000)
        self.assertEqual(v.verdict, cr.VERDICT_MATCH)

    def test_duplicate_page_does_not_break_segments(self):
        """重複頁を段判定に混ぜると n が後退して偽の新段ができる。"""
        obs = [_obs(1, 0, [], n=1, total_n=6),
               _obs(2, 17295, [("今回ご利用・ご請求金額合計", 17295)],
                    n=2, total_n=6),
               _obs(3, 0, [], n=1, total_n=6, is_duplicate=True),
               _obs(4, 17295, [("今回ご利用・ご請求金額合計", 17295)],
                    n=2, total_n=6, is_duplicate=True),
               _obs(5, 933680, [("今回ご利用・ご請求金額合計", 933680)],
                    n=1, total_n=3),
               _obs(6, 0, [], n=2, total_n=3)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.printed_total, 17295 + 933680)
        self.assertEqual(v.detail_sum, 950975)
        self.assertEqual(v.verdict, cr.VERDICT_MATCH)


class UnverifiableTest(unittest.TestCase):
    """判定できないときは黙る。監査タブに何も書かせない。"""

    def test_no_recognized_total_is_unverifiable(self):
        """ラベルが 1 つも登録済みでない（JCB / UC / 楽天 型）。"""
        obs = [_obs(1, 0, [], n=1, total_n=2),
               _obs(2, 157226, [], n=2, total_n=2)]
        v = cfr.finalize(obs, CC)
        self.assertEqual(v.verdict, cr.VERDICT_UNVERIFIABLE)
        self.assertIsNone(v.printed_total)

    def test_page_errors_force_unverifiable(self):
        """失敗頁の明細は Sheets に書かれていないので左辺が必ず不足する。

        「不一致」と診断すると人が記帳内容を疑って時間を溶かす。
        """
        obs = [_obs(1, 0, [("ご請求金額合計", 10000)], n=1, total_n=2),
               _obs(2, 6000, [], n=2, total_n=2)]
        v = cfr.finalize(obs, CC, had_page_errors=True)
        self.assertEqual(v.verdict, cr.VERDICT_UNVERIFIABLE)

    def test_transit_ic_is_not_applicable(self):
        """nimoca は券面に金額合計が構造的に無い（RECON_POLICY = count_only）。"""
        obs = [_obs(1, 9740, [], n=None, total_n=None)]
        v = cfr.finalize(obs, DocType.TRANSIT_IC)
        self.assertEqual(v.verdict, cr.VERDICT_NOT_APPLICABLE)

    def test_receipt_is_not_applicable(self):
        """既存 4 型は検算の対象外（RECON_POLICY = n/a）。"""
        obs = [_obs(1, 1000, [("ご請求金額合計", 9999)], n=1, total_n=1)]
        v = cfr.finalize(obs, DocType.RECEIPT)
        self.assertEqual(v.verdict, cr.VERDICT_NOT_APPLICABLE)

    def test_empty_observations_is_unverifiable(self):
        self.assertEqual(cfr.finalize([], CC).verdict, cr.VERDICT_UNVERIFIABLE)


class ObservePageTest(unittest.TestCase):
    """raw_data から観測値を作る側。既存の card_entries だけを使う。"""

    def test_bookable_rows_are_summed(self):
        raw = _raw(rows=[_row(7380), _row(11123)],
                   totals=[("ご請求金額合計", 18503)],
                   statement_page="2/2ページ")
        o = cfr.observe_page(raw, CC, page_num=2)
        self.assertEqual(o.detail_sum, 18503)
        self.assertEqual(o.statement_n, 2)
        self.assertEqual(o.statement_total, 2)

    def test_carry_over_row_is_excluded(self):
        """前回分口座振替は当期の取引ではない（アメックス型）。"""
        raw = _raw(rows=[_row(-35808, merchant="前回分口座振替金額"),
                         _row(29640), _row(300000),
                         _row(587440), _row(16600)],
                   totals=[("今回ご利用・ご請求金額合計", 933680)],
                   statement_page="1/3ページ")
        o = cfr.observe_page(raw, CC, page_num=1)
        self.assertEqual(o.detail_sum, 933680, "前期の清算行を足してはいけない")

    def test_cashback_row_is_included(self):
        """キャッシュバックは当期の調整なので足す（ENEOS 型）。"""
        raw = _raw(rows=[_row(7380), _row(11123),
                         _row(-3000, merchant="ENEOS店頭キャッシュバック")],
                   totals=[("ご請求金額合計", 15503)],
                   statement_page="2/2ページ")
        o = cfr.observe_page(raw, CC, page_num=2)
        self.assertEqual(o.detail_sum, 15503)

    def test_unmapped_label_is_dropped(self):
        """曖昧ラベルは右辺に載せない（既存裁定を維持）。"""
        raw = _raw(rows=[_row(79050)],
                   totals=[("今回ご請求金額", 79050)],
                   statement_page="1/2ページ")
        o = cfr.observe_page(raw, CC, page_num=1)
        self.assertEqual(o.totals, ())

    def test_handwritten_total_is_dropped(self):
        raw = _raw(rows=[_row(12940)], statement_page="1/1ページ")
        raw["printed_totals"] = [{"label": "ご請求金額合計", "amount": 12940,
                                  "count": None, "page": 1,
                                  "is_handwritten": True}]
        o = cfr.observe_page(raw, CC, page_num=1)
        self.assertEqual(o.totals, ())

    def test_unreadable_statement_page_yields_none(self):
        raw = _raw(rows=[_row(1000)], statement_page="1/最終")
        o = cfr.observe_page(raw, CC, page_num=1)
        self.assertIsNone(o.statement_n)

    def test_never_raises(self):
        """検算は検査器。壊れた入力で例外を投げて記帳を止めてはいけない。"""
        for bad in (None, {}, {"rows": None}, {"printed_totals": "x"},
                    {"card": "not a dict"}, []):
            with self.subTest(bad=bad):
                o = cfr.observe_page(bad, CC, page_num=1)
                self.assertIsInstance(o, cfr.PageObservation)


class FailOpenTest(unittest.TestCase):
    """AD-7: 検算の失敗で記帳を止めない。"""

    def test_finalize_never_raises(self):
        for bad in (None, [None], [{"not": "an observation"}], "x"):
            with self.subTest(bad=bad):
                v = cfr.finalize(bad, CC)
                self.assertIsInstance(v, cfr.FileVerdict)
                self.assertEqual(v.verdict, cr.VERDICT_UNVERIFIABLE)


class BookableDriftTest(unittest.TestCase):
    """AD-10: `_is_bookable` を重複実装した代償を番人で払う。

    `card_reconciliation.py` の diff を 0 に保つため公開純関数への抽出は
    しない（AD-1 の中核的な約束）。代わりに KIND_ALL の全値について
    両者の判定が一致することをここで縛る。

    **`_book_charge_rows` の両値で回すこと**（複審の指摘）。
    `KIND_CHARGE` の扱いは設定に依存するので、片側だけ見ると
    設定を変えた瞬間に静かに drift する。
    """

    def test_agrees_with_ledger_for_every_kind(self):
        for book_charge in (True, False):
            led = cr.FileReconLedger(DocType.CREDIT_CARD, total_pages=1,
                                     book_charge_rows=book_charge)
            for kind in cr.KIND_ALL:
                with self.subTest(kind=kind, book_charge=book_charge):
                    self.assertEqual(
                        cfr.is_bookable(kind, book_charge_rows=book_charge),
                        led._is_bookable(kind),
                        "kind の扱いが元帳と食い違っている（静かな drift）")

    def test_kind_all_is_covered(self):
        """KIND_ALL に値が増えたらこのテストが教える。"""
        self.assertEqual(set(cr.KIND_ALL),
                         {cr.KIND_EXPENSE, cr.KIND_FEE, cr.KIND_CREDIT_ADJUST,
                          cr.KIND_CARRY_OVER, cr.KIND_CHARGE, cr.KIND_UNKNOWN})


class VocabularyTest(unittest.TestCase):
    """判定語彙は既存の card_reconciliation から借りる（新造しない）。"""

    def test_verdicts_come_from_card_reconciliation(self):
        for v in (cfr.VERDICT_MATCH, cfr.VERDICT_MISMATCH,
                  cfr.VERDICT_UNVERIFIABLE, cfr.VERDICT_NOT_APPLICABLE):
            self.assertIn(v, (cr.VERDICT_MATCH, cr.VERDICT_MISMATCH,
                              cr.VERDICT_UNVERIFIABLE,
                              cr.VERDICT_NOT_APPLICABLE))

    def test_no_private_label_table(self):
        """AD-9: ラベル表を自前で持たない。既存の表だけを見る。

        自前の表を持つと「結果に合わせてラベルを選ぶ」ことが可能になり、
        標本が検証ではなく訓練データに堕ちる。
        """
        import ast
        import inspect
        src = inspect.getsource(cfr)
        tree = ast.parse(src)
        suspicious = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, (ast.Dict, ast.Set, ast.Tuple,
                                           ast.List)):
                continue
            literals = [n.value for n in ast.walk(node.value)
                        if isinstance(n, ast.Constant)
                        and isinstance(n.value, str)]
            # 券面ラベルらしき日本語文字列を含む集合を禁じる
            if any(("請求" in s or "支払" in s or "利用" in s) for s in literals):
                suspicious.append([t.id for t in node.targets
                                   if isinstance(t, ast.Name)])
        self.assertEqual(suspicious, [],
                         "card_file_recon が独自のラベル表を持っている")


if __name__ == "__main__":
    unittest.main()
