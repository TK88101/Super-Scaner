"""`anomaly_detector` の逐行記帳 doc_type 向け抑制表（T6-9 / Plan §3.8）。

`test_anomaly_detector.py` を**無修正のまま**にするために別ファイルにした
（T6 の DoD が「既存 4 doc_type の異常検知が 1 件も変わらない」ことを
あのファイルの無改造＋全緑で証明する形にしてある）。

抑制は `detect_anomalies` のシグネチャを変えない純加算で入れる。
シグネチャを変えると既存 4 doc_type の呼出点にも触ることになり、
リスク面が 1 ファイルから 2 ファイルへ広がる。

venv 非依存（`anomaly_detector` は config / receipt_aggregation / doc_types
しか引かない）:
    python -m unittest test_anomaly_detector_line_mode -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import anomaly_detector
from anomaly_detector import detect_anomalies
from doc_types import LINE_MODE_DOC_TYPES, DocType

_VALID_T = "T8010401088436"      # T + 13 桁
# `anomaly_detector._is_valid_t_number` が見るのは**形式だけ**（T + 13 桁）。
# チェックディジットの検算は `sheets_output._sanitize_invoice_num` 側にある。
# 桁数の合う番号はここでは «正当» 扱いになるので、桁を崩した値を使う。
_INVALID_T = "T12345"

_ENTRY = {"amount": 630, "debit_account": "旅費交通費", "description": "西鉄バス"}


def _types(parent):
    return {f["type"] for f in detect_anomalies(_ENTRY, parent)}


class VendorSuppressionTest(unittest.TestCase):
    """F列（取引先）の空欄。**transit_ic だけ**抑制する（趙裁定 2026-08-18）。

    nimoca の券面には店名欄が構造的に無い（`card_prompts.py` が merchant を
    「空文字でよい」と定義）＝ producer 契約上の空。一方クレカは merchant を
    「利用店名・摘要の主行」として要求しているので、空＝読み落とし＝人に
    見せるべき異常。同じ「空」でも意味が違うので抑制範囲を分ける。
    """

    def _parent(self, doc_type, vendor=""):
        return {"date": "2026/05/01", "vendor": vendor,
                "invoice_num": _VALID_T, "doc_type": doc_type}

    def test_transit_ic_does_not_flag_an_empty_vendor(self):
        self.assertNotIn("missing_vendor",
                         _types(self._parent(DocType.TRANSIT_IC)))

    def test_credit_card_still_flags_an_empty_vendor(self):
        """**両方向**の固定。ここを一緒に抑制すると読み落としが無音になる。"""
        self.assertIn("missing_vendor",
                      _types(self._parent(DocType.CREDIT_CARD)))

    def test_legacy_doc_types_still_flag_an_empty_vendor(self):
        for doc_type in (DocType.RECEIPT, DocType.PURCHASE_INVOICE,
                         DocType.SALES_INVOICE, DocType.SALARY_SLIP):
            with self.subTest(doc_type=doc_type):
                self.assertIn("missing_vendor", _types(self._parent(doc_type)))

    def test_absent_doc_type_key_still_flags(self):
        """`doc_type` キーを持たない旧経路・占位経路は `None` で不成立。"""
        self.assertIn("missing_vendor",
                      _types({"date": "2026/05/01", "vendor": "",
                              "invoice_num": _VALID_T}))

    def test_a_present_vendor_is_never_flagged(self):
        """抑制は「空でも咎めない」だけ。値が在る側の挙動は不変。"""
        self.assertNotIn("missing_vendor",
                         _types(self._parent(DocType.TRANSIT_IC, "西鉄バス")))


class InvoiceSuppressionTest(unittest.TestCase):
    """H列（インボイス）。カード明細・nimoca とも**空欄が正**（AD-8）。

    加盟店の登録番号はカード明細に構造的に存在しない（F-11 / F-14）。
    空欄を異常として扱えば 300 行すべてが黄になり、AD-7 が守ろうとした
    「色の信号価値」が消える。
    """

    def _parent(self, doc_type, invoice_num=""):
        return {"date": "2026/05/01", "vendor": "西鉄バス",
                "invoice_num": invoice_num, "doc_type": doc_type}

    def test_card_doc_types_do_not_flag_an_empty_invoice(self):
        for doc_type in (DocType.CREDIT_CARD, DocType.TRANSIT_IC):
            with self.subTest(doc_type=doc_type):
                self.assertNotIn("missing_invoice", _types(self._parent(doc_type)))

    def test_card_doc_types_do_not_flag_a_malformed_invoice(self):
        """不正な T番号も咎めない。

        H列は判定結果のレンダリング（`_resolve_invoice_cell`）であって
        OCR が拾った生文字列ではないので、形式検査の対象にならない。
        """
        for doc_type in (DocType.CREDIT_CARD, DocType.TRANSIT_IC):
            with self.subTest(doc_type=doc_type):
                self.assertNotIn("invalid_t_number",
                                 _types(self._parent(doc_type, _INVALID_T)))

    def test_legacy_doc_types_still_flag_an_empty_invoice(self):
        self.assertIn("missing_invoice", _types(self._parent(DocType.RECEIPT)))

    def test_legacy_doc_types_still_flag_a_malformed_invoice(self):
        self.assertIn("invalid_t_number",
                      _types(self._parent(DocType.RECEIPT, _INVALID_T)))


class UnaffectedFlagsTest(unittest.TestCase):
    """抑制したのは 3 種だけ。他のフラグは card doc_type でも従来どおり。

    抑制表を「カード明細なら全部黙る」と解釈した実装を検出する。
    """

    def test_missing_date_is_still_flagged(self):
        self.assertIn("missing_date",
                      _types({"date": "", "vendor": "西鉄バス",
                              "invoice_num": _VALID_T,
                              "doc_type": DocType.CREDIT_CARD}))

    def test_undetermined_account_is_still_flagged(self):
        flags = detect_anomalies(
            {"amount": 630, "debit_account": "未確定勘定"},
            {"date": "2026/05/01", "vendor": "西鉄バス",
             "invoice_num": _VALID_T, "doc_type": DocType.CREDIT_CARD})
        self.assertIn("undetermined_account", {f["type"] for f in flags})

    def test_account_review_is_still_flagged(self):
        flags = detect_anomalies(
            {"amount": 630, "debit_account": "雑収入"},
            {"date": "2026/05/01", "vendor": "西鉄バス",
             "invoice_num": _VALID_T, "doc_type": DocType.TRANSIT_IC})
        self.assertIn("account_review", {f["type"] for f in flags})


class SuppressionLedgerTest(unittest.TestCase):
    """逐行記帳 doc_type が増えたとき、抑制の是非を**必ず明記させる**。

    レビューで「抑制表を `doc_types.LINE_MODE_DOC_TYPES` の別名にすれば
    重複が消える」という提案が出たが**駁回した**。「逐行記帳である」と
    「券面に取引先／登録番号が無い」は**別の軸**である。現に
    `_VENDOR_OPTIONAL_DOC_TYPES` は transit_ic だけで、`LINE_MODE_DOC_TYPES`
    と一致しない。別名で結ぶと、加盟店の T番号を持つ逐行 doc_type が将来
    現れたとき、その doc_type のインボイス検査が**無音で抑制される** ——
    余分な黄タグ（うるさいが目に見える）より遥かに悪い。

    ただし提案が指摘した危険（3 つ目の逐行 doc_type を足したとき抑制表が
    追随せず黄タグが復活する）は実在する。別名化ではなく、**判断の記録を
    強制する**ことで塞ぐ。CLAUDE.md が `ENTRY_BUILDERS` / `RECON_POLICY`
    について記録している「登録漏れが無症状で効かなくなる」失敗様式と同じ形。
    """

    # 抑制の是非を明示的に判断済みの doc_type。逐行 doc_type を足す人は、
    # ここに True/False を書くまでテストが緑にならない。
    LEDGER = {
        # クレカ: 加盟店名は券面に在る（読めない＝読み落とし）ので橙を残す。
        #         加盟店の登録番号は構造的に無い（F-11 / F-14）ので黄は抑制。
        DocType.CREDIT_CARD: {"vendor": False, "invoice": True},
        # nimoca: 券面に店名欄そのものが無い（`card_prompts` が空文字を許容）。
        DocType.TRANSIT_IC: {"vendor": True, "invoice": True},
    }

    def test_every_line_mode_doc_type_has_an_explicit_decision(self):
        self.assertEqual(
            set(self.LEDGER), set(LINE_MODE_DOC_TYPES),
            "逐行記帳 doc_type が増減した。抑制の是非をこの台帳へ明記すること "
            "（別名化して自動追随させてはいけない。上の docstring 参照）")

    def test_the_ledger_matches_the_implementation(self):
        for doc_type, decision in self.LEDGER.items():
            with self.subTest(doc_type=doc_type):
                self.assertEqual(
                    doc_type in anomaly_detector._VENDOR_OPTIONAL_DOC_TYPES,
                    decision["vendor"])
                self.assertEqual(
                    doc_type in anomaly_detector._INVOICE_OPTIONAL_DOC_TYPES,
                    decision["invoice"])

    def test_no_legacy_doc_type_is_ever_suppressed(self):
        """既存 4 doc_type が抑制表に紛れ込んでいないこと（集合レベル）。"""
        legacy = set(DocType.ALL) - set(LINE_MODE_DOC_TYPES)
        suppressed = (anomaly_detector._VENDOR_OPTIONAL_DOC_TYPES
                      | anomaly_detector._INVOICE_OPTIONAL_DOC_TYPES)
        self.assertEqual(legacy & suppressed, set())


if __name__ == "__main__":
    unittest.main()
