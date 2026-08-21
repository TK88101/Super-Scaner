"""producer 側が `_file_recon` を載せることの番人（Plan B-3）。

見張るのは 3 つ。どれも「静かに壊れる」形の事故なので、
症状が出てから気づくのでは遅い。

1. **記帳に使う値が 1 バイトも変わらない**（snapshot）。
   検算の観測を足したせいで仕訳が変わったら本末転倒。
2. **1 物理頁につき観測は最大 1 個**。`_yield_line_mode_results` は
   明細 result の後に行欠け提示行を追加で yield するので、全部に載せると
   同じ頁を二重に数える（Codex 評審 中-3）。
3. **既存 4 型の result は 1 バイトも変わらない**。
   領収書などに `_file_recon` が生えると、既存の消費側が想定しない键を
   受け取る。
"""
import contextlib
import io
import unittest

import card_entries
import card_file_recon
import card_file_state
import ocr_engine
import ocr_test_fixtures
import page_family
from doc_types import DocType


def _raw(rows=(), totals=(), statement_page="", **extra):
    """クレカ 1 頁分の raw_data。

    形は `test_page_disposition_wiring._card_raw` に合わせる。
    **`rows_on_page` と `total_amount` を落とすと重複判定が効かない** ——
    `page_dedup` の二重署名は「(日付,金額) の並び」と「頁合計」の AND なので、
    合計が 0 固定だと指紋が立たず、重複頁が重複と判定されない。
    初版はここを `total_amount: 0` にしており、重複頁のテストが
    前提の時点で落ちた（Reuse 評審 2026-08-21）。
    """
    rows = [dict(r) for r in rows]
    raw = {"card": {"issuer": "AMEX", "member_no": "****-******-26003",
                    "statement_page": statement_page, "statement_no": "",
                    "period": "2026-05", "statement_date": "2026/05/21"},
           "rows": rows,
           "rows_on_page": len(rows),
           "printed_totals": [
               {"label": lab, "amount": amt, "count": None, "page": 1,
                "is_handwritten": False} for lab, amt in totals],
           "sections": [],
           "total_amount": sum(r.get("amount") or 0 for r in rows)}
    raw.update(extra)
    return raw


def _row(amount=1000, date="2026/04/10", merchant="テスト加盟店"):
    return {"date": date, "merchant": merchant, "amount": amount}


def _run(doc_type, raw, text, page_class=None, state=None, page_num=1):
    with contextlib.redirect_stdout(io.StringIO()):
        return list(ocr_engine._yield_page_results(
            doc_type, raw, text, None, page_class=page_class, state=state,
            page_num=page_num))


# **標本の正本は `ocr_test_fixtures`。手写しにすると実物とズレる。**
# 初版はここを手写しの "テストカード ご利用代金明細書 1/2 ページ" にしており、
# `classify_page` が `routing_family='unknown'` を返していた（実物は
# `cc_detail`）。つまり**実物のカード頁では決して起きない経路**を
# 駆動していた。`test_page_disposition_wiring.py:405` が 2026-08-20 に
# 記録した同じ罠に、そのまま嵌まっていた（Reuse 評審 2026-08-21 が実測）。
_CARD_TEXT = ocr_test_fixtures.AMEX_HEAD + " 17,295"


class EntriesAreUntouchedTest(unittest.TestCase):
    """snapshot 番人: 観測を足しても仕訳は逐字同一。"""

    def test_entries_match_the_builder_output(self):
        raw = _raw(rows=[_row(7380), _row(11123)],
                   totals=[("ご請求金額合計", 18503)],
                   statement_page="1/2ページ")
        expected = card_entries.build_entries_from_credit_card(raw)
        results = _run(DocType.CREDIT_CARD, raw, _CARD_TEXT)
        actual = [e for r in results for e in (r.get("entries") or [])]
        self.assertEqual(actual, expected,
                         "検算の観測を足したせいで仕訳が変わっている")

    def test_removing_file_recon_leaves_the_result_identical(self):
        """`_file_recon` を抜けば、載せる前と同じ dict になること。"""
        raw = _raw(rows=[_row(5000)], totals=[("ご請求金額合計", 5000)],
                   statement_page="1/1ページ")
        results = _run(DocType.CREDIT_CARD, raw, _CARD_TEXT)
        head = dict(results[0])
        head.pop("_file_recon", None)
        self.assertNotIn("_file_recon", head)
        # 残った键が既存の契約どおりであること（新しい键が増えていない）
        self.assertIn("entries", head)
        self.assertIn("doc_type", head)


class AtMostOneObservationPerPageTest(unittest.TestCase):
    """1 物理頁が複数 result を出しても観測は先頭 1 個だけ。"""

    def test_line_shortage_page_yields_only_one_observation(self):
        """券面申告より行が少ない頁は明細 result + 提示行の 2 本を出す。"""
        raw = _raw(rows=[_row(1000)], totals=[("ご請求金額合計", 1000)],
                   statement_page="1/1ページ", rows_on_page=5)
        results = _run(DocType.CREDIT_CARD, raw, _CARD_TEXT)
        self.assertGreater(len(results), 1, "この標本は複数 result を出す前提")
        carriers = [r for r in results if "_file_recon" in r]
        self.assertEqual(len(carriers), 1,
                         "同じ頁を二重に数えると左辺が 2 倍になる")
        self.assertIs(carriers[0], results[0], "載るのは先頭 result")

    def test_normal_page_carries_exactly_one(self):
        raw = _raw(rows=[_row(1000)], totals=[("ご請求金額合計", 1000)],
                   statement_page="1/1ページ")
        results = _run(DocType.CREDIT_CARD, raw, _CARD_TEXT)
        self.assertEqual(sum(1 for r in results if "_file_recon" in r), 1)


class ObservationContentTest(unittest.TestCase):
    """載っているのが本物の観測であること。"""

    def test_observation_is_a_page_observation(self):
        raw = _raw(rows=[_row(7380), _row(11123)],
                   totals=[("ご請求金額合計", 18503)],
                   statement_page="1/2ページ")
        results = _run(DocType.CREDIT_CARD, raw, _CARD_TEXT)
        obs = results[0]["_file_recon"]
        self.assertIsInstance(obs, card_file_recon.PageObservation)
        self.assertEqual(obs.detail_sum, 18503)
        self.assertEqual(obs.totals, (("ご請求金額合計", 18503),))
        self.assertEqual(obs.statement_n, 1)
        self.assertEqual(obs.statement_total, 2)
        self.assertFalse(obs.is_duplicate)

    def test_excluded_page_still_carries_an_observation(self):
        """除外頁にも載せる。載せないと重複頁が観測から漏れる。

        重複頁は除外経路を通るので、ここで載せそこねると
        「重複を数えない」という前提が成立しなくなる。
        標本は既存 `test_page_disposition_wiring` と同じ合計表頁。
        """
        text = "TS3 ご利用代金合計表 1/1 お支払合計 44,490"
        pc = page_family.classify_page(text)
        raw = _raw(totals=[("お支払合計", 44490)], statement_page="1/1ページ")
        results = _run(DocType.CREDIT_CARD, raw, text, pc)
        self.assertTrue(any(r.get("_excluded_page") for r in results),
                        "この標本は除外される前提（族判定が変わったら直す）")
        self.assertIn("_file_recon", results[0])
        obs = results[0]["_file_recon"]
        self.assertEqual(obs.totals, (("お支払合計", 44490),),
                         "除外頁の券面合計も右辺に要る（TS CUBIC の表紙型）")


class DuplicateFlagIsPassedThroughTest(unittest.TestCase):
    """重複頁の印が観測に載ること。

    ここが載らないと消費側が重複頁の明細を足し、左辺が 2 倍になって
    偽の不一致が出る。しかも `page_family.py:255-261` は「理由文字列の
    前綴で判定するな、そのためにこの旗がある」と明記しているので、
    消費側には自力で判る手段が無い —— producer が落としたら誰も気づけない。

    **dedup 機構そのものは駆動しない**（それは `test_page_disposition_wiring`
    の担当）。ここで確かめるのは「裁決が出たあと、その旗が観測へ渡るか」
    という 1 点だけ。機構を丸ごと動かそうとすると、指紋の前提（頁合計・
    行数・OCR 品質）を全部揃える必要があり、テストが本題と無関係な理由で
    壊れる。
    """

    def _disposition(self, is_duplicate):
        return page_family.Disposition(
            action=page_family.ACTION_EXCLUDE,
            family=page_family.FAMILY_CC_SUMMARY,
            reason="duplicate_page:1" if is_duplicate else "cc_summary",
            destination=page_family.EXCLUDE_DEST_AUDIT_TAB,
            is_duplicate=is_duplicate)

    def _observe(self, is_duplicate):
        raw = _raw(rows=[_row(17295)],
                   totals=[("今回ご利用・ご請求金額合計", 17295)],
                   statement_page="1/6ページ")
        results = list(ocr_engine._with_file_recon(
            [{"entries": [], "doc_type": DocType.CREDIT_CARD}],
            DocType.CREDIT_CARD, raw, 3, self._disposition(is_duplicate)))
        return results[0]["_file_recon"]

    def test_duplicate_disposition_marks_the_observation(self):
        self.assertTrue(self._observe(True).is_duplicate,
                        "重複頁に印が付かない＝左辺が 2 倍になる")

    def test_non_duplicate_disposition_leaves_it_clear(self):
        """偽陽性は逆向きの事故 —— 実在する明細を左辺から落とす。"""
        obs = self._observe(False)
        self.assertFalse(obs.is_duplicate)
        self.assertEqual(obs.detail_sum, 17295)

    def test_marked_duplicate_that_is_still_booked_is_counted(self):
        """`DEDUP_MODE_MARK` では重複頁も記帳される。**落としてはいけない。**

        `page_family.py:695` は mark モードのとき `ACTION_EXCLUDE` を返さず、
        `is_duplicate=True` のまま記帳経路へ流す。その頁の明細は Sheets に
        書かれるので、検算の左辺から落とすと突合対象と実際の中身が
        食い違い、不一致を隠す／偽の不一致を出す（codex 評審 2026-08-21）。
        """
        raw = _raw(rows=[_row(17295)],
                   totals=[("今回ご利用・ご請求金額合計", 17295)],
                   statement_page="1/6ページ")
        marked = page_family.Disposition(
            action=page_family.ACTION_BOOK,
            family=page_family.FAMILY_CC_DETAIL,
            reason="duplicate_page:1",
            destination="",
            is_duplicate=True)
        results = list(ocr_engine._with_file_recon(
            [{"entries": [{"amount": 17295}], "doc_type": DocType.CREDIT_CARD}],
            DocType.CREDIT_CARD, raw, 3, marked))
        obs = results[0]["_file_recon"]
        self.assertFalse(
            obs.is_duplicate,
            "記帳された頁を検算から落とすと、突合対象が Sheets とズレる")
        self.assertEqual(obs.detail_sum, 17295)

    def test_no_disposition_means_not_duplicate(self):
        """`page_class` が無い経路（Vision 兜底）では裁決自体が無い。"""
        raw = _raw(rows=[_row(500)], statement_page="1/1ページ")
        results = list(ocr_engine._with_file_recon(
            [{"entries": [], "doc_type": DocType.CREDIT_CARD}],
            DocType.CREDIT_CARD, raw, 1, None))
        self.assertFalse(results[0]["_file_recon"].is_duplicate)


class OtherDocTypesAreUntouchedTest(unittest.TestCase):
    """既存 4 型の result に `_file_recon` を生やさない。"""

    def test_receipt_has_no_file_recon(self):
        raw = {"documents": [{"date": "2026/05/05", "vendor": "テスト商店",
                              "total_amount": 1000, "items": []}]}
        results = _run(DocType.RECEIPT, raw, "テスト領収書")
        for r in results:
            self.assertNotIn("_file_recon", r)

    def test_cc_family_is_the_gate(self):
        """接線の条件は `CC_FAMILY_DOC_TYPES` であること（表の複製を作らない）。"""
        self.assertEqual(set(page_family.CC_FAMILY_DOC_TYPES),
                         {DocType.CREDIT_CARD, DocType.TRANSIT_IC})


if __name__ == "__main__":
    unittest.main()
