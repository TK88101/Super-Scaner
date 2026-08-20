"""監査タブの「理由」列を人が読める日本語にする（2026-08-20 趙指摘）。

監査タブの唯一の用途は**人が異常に気づくこと**である。それなのに理由列は
`family_signal_with_entries:info_notice` のような機械可読キーを書いていた。
読むのは会計事務所の担当者で、エンジニアではない。趙の指摘:

> これらの分岐・除外の理由欄、書いてあることが完全に正常人には理解できない。
> ここは日本語で簡単に表現すべき。

**翻訳は 1 箇所に閉じる**（`sheets_output.append_audit_row`）。producer 側
（`ocr_engine` / `page_family`）は機械可読キーのまま流す —— キーは
`_merge_audit_signals` の連結や `_exclude_reason` の突合に使われており、
産地で日本語化すると照合が壊れる。表示の都合は表示層で吸収する。

**未知のキーは原文のまま出す**。翻訳表への登録漏れで理由が消えると、
監査タブが「異常に気づく唯一の場所」でなくなる（IP-401 と同じ形）。
読みにくいのは登録漏れのサインであって、情報を落とす理由にはならない。

文案は `/humanmade` の検査を通している（2026-08-20）。主な裁定:
- 「（要確認）」は直前の「疑いがあります」と重複 → 削除
- 「AI は 1 つしか報告していません」→ 読者に無関係な内部事情 → 結論先行へ
- 文末は「ます」体で統一（13.5: 技術文書は一貫性が最優先）
- 「2 行不足」は前半から計算できるが、100 行を走査する担当者に暗算させない
  ため**意図的に残す**（第0章5 が第3章に優先）

外部依存なし（gspread 不要 —— 翻訳は純関数）:
    python -m unittest test_audit_reason_ja -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from sheets_output import audit_reason_ja  # noqa: E402


class ExcludedPageReasonTest(unittest.TestCase):
    """除外された頁（記帳しなかった頁）の理由。"""

    def test_envelope(self):
        self.assertEqual(audit_reason_ja("envelope"),
                         "封筒・領収書以外のページのため記帳していません")

    def test_info_notice(self):
        self.assertEqual(audit_reason_ja("info_notice"),
                         "案内ページのため記帳していません")

    def test_payment_method_notice(self):
        self.assertEqual(audit_reason_ja("payment_method_notice"),
                         "リボ・分割の案内ページのため記帳していません")

    def test_points_only(self):
        self.assertEqual(audit_reason_ja("points_only"),
                         "ポイント専用ページのため記帳していません")

    def test_cc_summary(self):
        self.assertEqual(audit_reason_ja("cc_summary"),
                         "合計表のみのページのため記帳していません")

    def test_social_insurance_notice(self):
        """社会保険料通知書は業務ルールで記帳しない。理由を顧客に伝える。"""
        got = audit_reason_ja("social_insurance_notice")
        self.assertIn("社会保険", got)
        self.assertIn("記帳していません", got)


class BranchReasonTest(unittest.TestCase):
    """記帳はしたが人手で確かめてほしい頁（verdict は「分岐」）。"""

    def test_envelope_signal_with_entries(self):
        self.assertEqual(audit_reason_ja("envelope_signal_with_entries"),
                         "封筒の疑いがありますが、明細が取れたので記帳しました")

    def test_family_signal_with_entries_is_expanded(self):
        """族名まで日本語にする。`:info_notice` を残すと結局読めない。"""
        self.assertEqual(audit_reason_ja("family_signal_with_entries:info_notice"),
                         "案内ページの疑いがありますが、明細が取れたので記帳しました")

    def test_family_signal_with_an_unknown_family(self):
        """未知の族でも「疑い」の枠は日本語にし、族名だけ原文で残す。"""
        got = audit_reason_ja("family_signal_with_entries:brand_new_family")
        self.assertIn("疑い", got)
        self.assertIn("brand_new_family", got)


class ShortageReasonTest(unittest.TestCase):
    """行の取りこぼし。**数を先に出す**（担当者に暗算させない）。"""

    def test_line_shortage(self):
        self.assertEqual(audit_reason_ja("line_shortage:57/59"),
                         "明細が 2 行不足しています（券面 59 行・取得 57 行）")

    def test_line_shortage_with_unknown_total(self):
        """券面の総数が読めなかった場合。差を計算できないので書かない。"""
        got = audit_reason_ja("line_shortage:57/?")
        self.assertIn("57", got)
        self.assertNotIn("券面 ? 行", got)

    def test_salvaged(self):
        """救済は経たが行数は充足。**不足ではない**ので言い方を変える。"""
        got = audit_reason_ja("salvaged:57/57")
        self.assertIn("57", got)
        self.assertNotIn("不足", got)


class SectionReasonTest(unittest.TestCase):
    """区画（主カード・副カード）の取りこぼし検査。"""

    def test_section_undercount(self):
        self.assertEqual(audit_reason_ja("section_undercount:2/1"),
                         "副カード分を取りこぼした疑いがあります（券面の区画 2・報告 1）")

    def test_section_rows_missing(self):
        got = audit_reason_ja("section_rows_missing:2/1")
        self.assertIn("区画", got)
        self.assertIn("疑い", got)

    def test_section_detection_unknown(self):
        self.assertIn("確認できませんでした",
                      audit_reason_ja("section_detection_unknown"))


class CoverageReasonTest(unittest.TestCase):
    def test_page_coverage_gap(self):
        got = audit_reason_ja("page_coverage_gap:[3, 4]")
        self.assertIn("3", got)
        self.assertIn("4", got)
        self.assertIn("出力されませんでした", got)


class CompositeAndFallbackTest(unittest.TestCase):
    """合成シグナルと、登録漏れの扱い。"""

    def test_two_signals_are_both_translated(self):
        """`;` 連結は両方訳す。片方でも英語のまま残ると読む気が失せる。"""
        got = audit_reason_ja("section_undercount:2/1;section_rows_missing:2/1")
        self.assertIn("副カード分を取りこぼした疑い", got)
        self.assertIn("区画", got)
        self.assertNotIn("section_", got)

    def test_unknown_key_is_passed_through(self):
        """**訳せないものを消さない。** 読みにくさより情報の欠落が悪い。"""
        self.assertEqual(audit_reason_ja("brand_new_signal:9/9"),
                         "brand_new_signal:9/9")

    def test_empty_stays_empty(self):
        self.assertEqual(audit_reason_ja(""), "")
        self.assertEqual(audit_reason_ja(None), "")

    def test_never_raises(self):
        """理由列の整形で例外を出すと監査行そのものが書けなくなる。"""
        for weird in (123, ["a"], {"b": 1}, "line_shortage:こわれた"):
            with self.subTest(value=weird):
                self.assertIsInstance(audit_reason_ja(weird), str)


class WritingStyleTest(unittest.TestCase):
    """/humanmade の裁定を固定する（2026-08-20）。

    文体が揃っていないと、100 行の監査タブを走査するとき読みが引っかかる。
    """

    _ALL = ("envelope", "info_notice", "payment_method_notice", "points_only",
            "cc_summary", "social_insurance_notice",
            "envelope_signal_with_entries",
            "family_signal_with_entries:info_notice",
            "line_shortage:57/59", "section_undercount:2/1",
            "section_rows_missing:2/1", "section_detection_unknown")

    def test_no_machine_key_leaks_into_translated_text(self):
        for key in self._ALL:
            with self.subTest(key=key):
                got = audit_reason_ja(key)
                self.assertNotIn("_", got, "機械キーが訳文に漏れている: %r" % got)

    def test_sentences_end_consistently(self):
        """本文は「ます」体で揃える（13.5: 技術文書は一貫性が最優先）。

        末尾の括弧は数字の補足（「券面 59 行・取得 57 行」など）で、
        /humanmade の裁定で**意図的に残した**もの。担当者に暗算させない
        ためなので、文体の判定からは外して本文だけを見る。
        """
        import re
        for key in self._ALL:
            with self.subTest(key=key):
                got = audit_reason_ja(key)
                body = re.sub(r"（[^）]*）\s*$", "", got).strip()
                self.assertTrue(
                    body.endswith(("ます", "ました", "ません", "ませんでした",
                                   "でした")),
                    "本文の文末が揃っていない: %r（本文 %r）" % (got, body))

    def test_each_line_fits_one_cell(self):
        """1 セル 1 行で読める長さ。長いと横スクロールが要る。"""
        for key in self._ALL:
            with self.subTest(key=key):
                self.assertLessEqual(len(audit_reason_ja(key)), 40)



class DuplicatePageReasonTest(unittest.TestCase):
    """重複ページ（T8d）。**複合形を丸ごと訳せること**が要点。

    `page_dedup._detail` は `dup_key:…;dup_amount:…` を連ねるので、
    単独キーだけ登録して満足すると、実際に監査タブへ出る文字列の
    後半が機械語のまま残る（趙が 2026-08-20 に指摘した状態への逆戻り）。
    """

    def test_excluded_duplicate(self):
        self.assertEqual(audit_reason_ja("duplicate_page:1"),
                         "1 ページ目と同じ内容のため記帳していません")

    def test_unknown_origin_page_still_reads(self):
        self.assertEqual(audit_reason_ja("duplicate_page:?"),
                         "? ページ目と同じ内容のため記帳していません")

    def test_key_conflict(self):
        self.assertIn("内容が違います", audit_reason_ja("dup_key_conflict:1"))

    def test_content_only(self):
        self.assertIn("重複の疑い", audit_reason_ja("dup_content_only:2"))

    def test_amount_is_grouped(self):
        self.assertEqual(audit_reason_ja("dup_amount:17295"),
                         "重複と判定した金額は 17,295 円です")

    def test_more_text_warns_about_handwriting(self):
        self.assertIn("追記", audit_reason_ja("dup_more_text"))

    def test_the_whole_compound_reason_is_japanese(self):
        """実際に監査タブへ出る形（`page_family` ＋ `page_dedup._detail`）。"""
        reason = ("duplicate_page:1;dup_key:AMEX/**********26003/1/6;"
                  "dup_amount:17295;dup_more_text")
        got = audit_reason_ja(reason)
        for machine in ("duplicate_page:", "dup_key:", "dup_amount:",
                        "dup_more_text"):
            self.assertNotIn(machine, got,
                             "機械語が残っている: %r → %r" % (machine, got))
        self.assertIn("1 ページ目と同じ内容", got)
        self.assertIn("17,295 円", got)


class AnchorInheritanceReasonTest(unittest.TestCase):
    """明細書作成日をどの頁から継いだか（T8d B 章）。"""

    def test_origin_anchor_and_row_count_are_all_shown(self):
        """**行数が要る。** 1 行しか出さないので、この 1 行から
        抜き取り検査の母集団が特定できなければ意味がない。"""
        got = audit_reason_ja("date_year_from_page:5@2026/05/15@57")
        self.assertIn("5 ページ目", got)
        self.assertIn("2026/05/15", got)
        self.assertIn("57 行", got)
        self.assertNotIn("date_year_from_page", got)
        self.assertNotIn("@", got)

    def test_origin_and_anchor_without_a_count_still_read(self):
        got = audit_reason_ja("date_year_from_page:5@2026/05/15")
        self.assertIn("5 ページ目", got)
        self.assertIn("2026/05/15", got)
        self.assertNotIn("date_year_from_page", got)

    def test_a_missing_anchor_still_reads(self):
        got = audit_reason_ja("date_year_from_page:5")
        self.assertIn("5 ページ目", got)
        self.assertNotIn("@", got)

if __name__ == "__main__":
    unittest.main()
