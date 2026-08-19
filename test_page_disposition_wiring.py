"""T8-3: `page_family` の裁決を生産経路へ接線した部分の単体テスト。

守りたい実害は 2 つある。

1. **半開状態** —— `_yield_page_results` の呼出点は逐頁 PDF ループと
   尾段（単頁 PDF・画像）の 2 箇所ある。片方だけ `page_class` を渡すと、
   その経路のリボ頁だけ従来どおり赤い認識不能行になり、**症状が出るのは
   顧客が単頁でアップロードしたときだけ**になる。AST 番人テストで縛る。
   （実際この改修中、mock の署名が追従しておらず `TypeError` が外層 except に
   吞まれて**全頁が消える**という回帰を出した。签名不一致は静かに効く。）

2. **監査シグナルの食い合い** —— `_with_audit_signal` は無条件上書きなので、
   族シグナルと `card_salvage` の行欠けシグナルが同じ頁で立つと片方が消える。

外部依存なし（gspread / paddleocr 不要）:
    python -m unittest test_page_disposition_wiring -v
"""
import ast
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import card_salvage  # noqa: E402
import ocr_engine  # noqa: E402
import page_family  # noqa: E402
from doc_types import DocType  # noqa: E402

# リボ頁の版面（アメックス p8 の実測構造。顧客実名・カード番号は含めない）
_RIBO_TEXT = (
    "ペイフレックス登録状況 明細書作成日 2023年1月19日 "
    "登録プラン名 ペイフレックスあとリボ 登録日 2020年10月8日 "
    "リボルビング払い利用可能枠 1,500,000 基本手数料率 14.90 実質年率 14.6 "
    "あとリボ変更締切日 2023年1月30日 今回ご請求金額 0"
)
_DETAIL_TEXT = "アメリカン・エキスプレス ご利用代金明細書 1/6 ページ"


def _raw(rows=(), rows_on_page=None, **extra):
    raw = {"card": {}, "rows": list(rows), "printed_totals": [], "sections": [],
           "total_amount": 0}
    if rows_on_page is not None:
        raw["rows_on_page"] = rows_on_page
    raw.update(extra)
    return raw


def _row(amount=1000, date="2023/01/05", desc="テスト加盟店"):
    return {"date": date, "description": desc, "amount": amount}


def _run(doc_type, raw, text, page_class=None):
    with contextlib.redirect_stdout(io.StringIO()):
        return list(ocr_engine._yield_page_results(
            doc_type, raw, text, None, page_class=page_class))


class RiboPageIsExcludedTest(unittest.TestCase):
    """要求 #6: リボ頁は記帳せず監査タブへ 1 行だけ残す（趙裁定 2026-08-19）。"""

    def test_ribo_page_with_no_entries_goes_to_audit_tab(self):
        pc = page_family.classify_page(_RIBO_TEXT)
        results = _run(DocType.CREDIT_CARD, _raw(), _RIBO_TEXT, pc)

        self.assertEqual(len(results), 1, "頁は必ず 1 件以上 yield する（IP-401）")
        r = results[0]
        self.assertTrue(r.get("_excluded_page"))
        self.assertEqual(r.get("_exclude_destination"),
                         page_family.EXCLUDE_DEST_AUDIT_TAB)
        self.assertIn("payment_method_notice", r.get("_exclude_reason", ""))
        self.assertFalse(r.get("entries"), "除外頁は仕訳を作らない")

    def test_entries_win_over_the_family_signal(self):
        """IP-401 gate: entries が組めていれば除外経路は存在しない。"""
        pc = page_family.classify_page(_RIBO_TEXT)
        results = _run(DocType.CREDIT_CARD, _raw(rows=[_row()]), _RIBO_TEXT, pc)

        self.assertTrue(any(r.get("entries") for r in results),
                        "記帳されなければならない")
        self.assertFalse(any(r.get("_excluded_page") for r in results))

    def test_booked_ribo_page_still_leaves_a_branch_note(self):
        """記帳はするが「リボ頁で entries が組まれた」痕跡を監査タブへ残す。"""
        pc = page_family.classify_page(_RIBO_TEXT)
        results = _run(DocType.CREDIT_CARD, _raw(rows=[_row()]), _RIBO_TEXT, pc)

        signals = [r.get("_audit_signal") for r in results if r.get("_audit_signal")]
        self.assertTrue(signals, "痕跡が無いと偽 entry が無印で通る")
        self.assertIn("family_signal_with_entries:payment_method_notice", signals[0])


class ShortageBeatsExclusionTest(unittest.TestCase):
    """行欠けの疑いがある頁は除外しない（Codex 評審 MEDIUM-1）。

    除外して監査タブへ静かに送ると、解析失敗で行を落とした頁が
    「正常に除外された頁」に化ける。疑わしいときは**うるさい赤**を選ぶ。
    """

    def test_a_page_suspected_of_missing_lines_is_not_excluded(self):
        pc = page_family.classify_page(_RIBO_TEXT)
        raw = _raw(rows_on_page=12)          # 券面は 12 行と申告、rows は空
        raw[card_salvage.SALVAGED_KEY] = True
        results = _run(DocType.CREDIT_CARD, raw, _RIBO_TEXT, pc)

        self.assertFalse(any(r.get("_excluded_page") for r in results),
                         "行欠けの疑いがある頁を監査タブへ沈めてはいけない")


class AuditSignalsAreNotSwallowedTest(unittest.TestCase):
    """族シグナルと行欠けシグナルが同じ頁で立っても片方が消えない。

    `_with_audit_signal` は無条件上書きなので、素直に載せ直すと消える
    （Codex 評審 HIGH-2）。
    """

    def test_merge_keeps_both(self):
        self.assertEqual(ocr_engine._merge_audit_signals("a", "b"), "a;b")

    def test_merge_drops_empties_but_keeps_the_rest(self):
        self.assertEqual(ocr_engine._merge_audit_signals(None, "b"), "b")
        self.assertEqual(ocr_engine._merge_audit_signals("a", None), "a")
        self.assertIsNone(ocr_engine._merge_audit_signals(None, None))

    def test_family_signal_survives_the_line_mode_pass(self):
        """族シグナルが `_yield_line_mode_results` を通り抜けても消えないこと。

        **実装で判ったこと（2026-08-19）**: 現行の構造では族シグナルと行欠け
        シグナルは**同じ result に載らない**。`_yield_line_mode_results` は
        entries が在る頁では `yield result`（無シグナル）＋ 別途「提示行」を
        出す形なので、行欠けの痕跡は後続の別 result に付く。一方 
        `_with_audit_signal` による上書きが走るのは entries=0 の枝だけで、
        そのとき族シグナルは（優先序 2 を通らないので）そもそも存在しない。

        つまり Codex 評審 HIGH-2 の食い合いは**現行コードでは発生しない**。
        `_merge_audit_signals` は「`_yield_line_mode_results` が将来
        entries 有りでも `_with_audit_signal` を使うようになったとき」への
        防御として残す。ここで固定するのは合成の有無ではなく
        **族シグナルが生き残ること**そのもの。
        """
        pc = page_family.classify_page(_RIBO_TEXT)
        raw = _raw(rows=[_row()], rows_on_page=12)   # 記帳あり ＋ 行欠けあり
        raw[card_salvage.SALVAGED_KEY] = True
        results = _run(DocType.CREDIT_CARD, raw, _RIBO_TEXT, pc)

        signals = [r.get("_audit_signal") for r in results if r.get("_audit_signal")]
        self.assertTrue(
            any("family_signal_with_entries:payment_method_notice" in sig
                for sig in signals),
            "族シグナルが line-mode 通過で消えた")
        self.assertTrue(any(r.get("entries") for r in results),
                        "行欠けが在っても記帳は止めない")


class LegacyDocTypesAreUntouchedTest(unittest.TestCase):
    """既存 4 doc_type はこの経路に入らない（1 バイトも挙動を変えない）。"""

    def test_receipt_is_not_routed_through_the_disposition(self):
        pc = page_family.classify_page(_RIBO_TEXT)   # 族シグナルが立つテキスト
        raw = {"documents": [{"date": "2023/01/05", "vendor": "テスト",
                              "items": [{"account": "消耗品費", "amount": 1000}]}]}
        results = _run(DocType.RECEIPT, raw, _RIBO_TEXT, pc)

        self.assertFalse(any(r.get("_excluded_page") for r in results),
                         "RECEIPT が card 系の裁決に巻き込まれてはいけない")

    def test_no_page_class_means_legacy_behaviour(self):
        """Vision 兜底など page_class が無い経路は従来どおり。"""
        results = _run(DocType.CREDIT_CARD, _raw(), _RIBO_TEXT, None)
        self.assertFalse(any(r.get("_excluded_page") for r in results))


class BothCallSitesPassPageClassTest(unittest.TestCase):
    """AST 番人: 生産の呼出点が**両方とも** `page_class` を渡していること。

    片方だけ接続された半開状態は、テストが無ければ誰も気づけない。
    逐頁 PDF ループ（`ocr_engine.py` の PDF 分割経路）と尾段（単頁 PDF・画像）
    の 2 経路があり、後者は「顧客が 1 枚だけアップロードしたとき」にしか
    通らないので、実運用で症状が出るまでに時間がかかる。
    """

    def test_every_production_call_passes_page_class(self):
        with io.open(os.path.join(os.path.dirname(__file__), "ocr_engine.py"),
                     encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "_yield_page_results"]

        self.assertGreaterEqual(len(calls), 2,
                                "生産の呼出点は逐頁ループと尾段の 2 箇所のはず")
        for call in calls:
            kwargs = {k.arg for k in call.keywords if k.arg}
            self.assertIn(
                "page_class", kwargs,
                "ocr_engine.py:%d の `_yield_page_results` 呼出が page_class を"
                "渡していない。この経路だけ裁決が効かない半開状態になる"
                % call.lineno)


if __name__ == "__main__":
    unittest.main()
