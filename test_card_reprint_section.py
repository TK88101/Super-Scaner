"""インボイス再掲区画の行を会計対象から外す（Plan 2026-08-20-recon-wiring 第 1 部）。

**python3 単体（venv 無し）で走ること**が設計上の性質。
`test_dependency_weight` が機械で見張る。

## 何を守るか

日本のクレカ明細はインボイス制度対応で、明細の下に次の区画を置くことがある
（TS CUBIC の原票 p4 / p9 で目視確認済み）:

    上記に含まれる課税取引の明細
    26 5 5  ご利用代金明細書発行手数料              220
    ＊＊課税取引合計（消費税10％対象）＊＊  税込合計額 220円 消費税額 20円

見出しが「**上記に含まれる**」と明言しているとおり、既に上で計上済みの
課税行の**再掲**であって新規取引ではない。これを記帳すると明細書 1 通あたり
220 円を過剰計上する（実測: 券面 97,004 に対し明細合計 97,224）。

## 二つの数え方が正当に分かれる

`card_salvage._visible_rows` は `rows_on_page`（券面が申告した行数）と
突合するためのもので、**再掲行も券面には印字されている**。だから
そちらは `_all_rows`（全行）を数える。会計側だけが `_rows`（再掲を除く）を使う。
両者を同じにすると、除外した分だけ偽の行欠け警告が出る。

## 既定を安全側にする

`_rows()` が**フィルタ済み**の方である。将来 4 つ目の会計消費者が
素朴に `_rows()` を呼んでも、既定で正しい方を得る。
生の全行が要る側だけが明示的に `_all_rows()` を呼ぶ。

    python3 -m unittest test_card_reprint_section -v
"""
import ast
import os
import unittest

import card_entries
import card_salvage
from card_reconciliation import KIND_EXPENSE
from doc_types import DocType

REPRINT_HEADING = "上記に含まれる課税取引の明細"


def _row(line_no, amount, merchant, sec, kind="fee"):
    return {"line_no": line_no, "date": "2026/05/05", "amount": amount,
            "kind": kind, "merchant": merchant, "note": "", "sec": sec,
            "debit_account": "支払手数料"}


def p4_type_raw():
    """区画ラベルが出る形（原票 p4 相当）。**L2 で判別できる。**"""
    return {
        "card": {"issuer": "トヨタファイナンス", "card_name": "TS CUBIC"},
        "sections": [
            {"index": 0, "label": "9-040091-041-00068 (ナンバー 9714)",
             "subtotal": 24911},
            {"index": 1, "label": "9-040091-041-00084 (ナンバー 4822)",
             "subtotal": 220},
            {"index": 2, "label": REPRINT_HEADING, "subtotal": 220},
        ],
        "printed_totals": [{"label": "ご請求金額合計(A)", "amount": 25131,
                            "count": None, "page": 2}],
        "rows_on_page": 3,
        "rows": [
            _row(1, 24911, "サンプル給油所SS", 0, kind="expense"),
            _row(2, 220, "ご利用代金明細書発行手数料", 1),
            _row(3, 220, "ご利用代金明細書発行手数料", 2),   # ← 再掲
        ],
    }


def p9_type_raw():
    """区画が空で出る形（原票 p9 相当）。**L2 では判別できない。**"""
    return {
        "card": {"issuer": "トヨタファイナンス", "card_name": "TS CUBIC"},
        "sections": [],
        "printed_totals": [{"label": "税込合計額", "amount": 220,
                            "count": None, "page": 5}],
        "rows_on_page": 2,
        "rows": [
            _row(1, 520, "ETC後納分/福北公社", None, kind="expense"),
            _row(2, 220, "ご利用代金明細書発行手数料", None),  # ← 再掲だが無印
        ],
    }


class HeadingTableTest(unittest.TestCase):
    """再掲見出しの判定表。**未知は再掲扱いにしない。**"""

    def test_known_reprint_heading(self):
        self.assertTrue(card_entries.is_reprint_section_heading(REPRINT_HEADING))

    def test_full_width_and_spacing_variants(self):
        """券面は全角記号を使う。NFKC 正規化を通すこと。"""
        for text in ("上記に含まれる課税取引の明細  ",
                     "　上記に含まれる課税取引の明細"):
            with self.subTest(text=text):
                self.assertTrue(card_entries.is_reprint_section_heading(text))

    def test_unknown_heading_is_not_reprint(self):
        """**見逃す側へ倒す。** 誤って再掲と判定すると実在の経費が消える。

        過剰計上は金額検算（第 2 部）が拾えるが、消えた経費は誰も拾えない。
        母 Plan 附録 B-1 の「誤検知と見逃しの非対称性」。
        """
        for text in ("今月ご利用額", "9-040091-041-00084 (ナンバー 4822)",
                     "ポイント明細", "", None, "課税取引"):
            with self.subTest(text=text):
                self.assertFalse(card_entries.is_reprint_section_heading(text))


class RowFilterTest(unittest.TestCase):
    """`_rows`（会計用・既定）と `_all_rows`（全行）の分岐。"""

    def test_accounting_rows_drop_the_reprint_row(self):
        rows = card_entries._rows(p4_type_raw())
        self.assertEqual([r["line_no"] for r in rows], [1, 2],
                         "再掲行（line_no=3）が落ちていない")

    def test_all_rows_keep_the_reprint_row(self):
        rows = card_entries._all_rows(p4_type_raw())
        self.assertEqual([r["line_no"] for r in rows], [1, 2, 3])

    def test_rows_without_sections_are_untouched(self):
        """p9 型は判別材料が無い。**落とさない**（既知の限界）。

        ここを落とすと、区画を報告しない券面の実在行が消える。
        p9 型の停止保証は L1（prompt）が担い、漏れたら第 2 部の検算が検出する。
        """
        rows = card_entries._rows(p9_type_raw())
        self.assertEqual(len(rows), 2)

    def test_no_sections_key_at_all(self):
        raw = {"rows": [_row(1, 100, "店", None)]}
        self.assertEqual(len(card_entries._rows(raw)), 1)

    def test_sec_pointing_out_of_range_is_kept(self):
        raw = p4_type_raw()
        raw["rows"][2]["sec"] = 99
        self.assertEqual(len(card_entries._rows(raw)), 3)


class AccountingConsumersTest(unittest.TestCase):
    """記帳・検算・非記帳集計の **3 経路すべて**で再掲が消えること。

    実コードではこの 3 つが同じ raw rows を**別々に走査**している。
    片方だけ除外する事故は CLAUDE.md が名指しする ENTRY_BUILDERS 未登録と
    同じ破壊様式なので、3 経路を個別に固定する。
    """

    def test_booking_excludes_the_reprint(self):
        entries = card_entries.build_entries_from_credit_card(p4_type_raw())
        amounts = [e.get("debit_amount") or e.get("amount") for e in entries]
        self.assertEqual(amounts.count(220), 1,
                         f"220 が 2 回記帳されている: {amounts}")

    def test_reconciliation_excludes_the_reprint(self):
        lines = card_entries.detail_lines_from_raw(p4_type_raw(),
                                                   DocType.CREDIT_CARD)
        self.assertEqual(sum(l.amount for l in lines), 25131,
                         "検算の明細合計が券面 (A) と合わない")

    def test_nonbookable_summary_excludes_the_reprint(self):
        summary = card_entries.summarize_nonbookable(p4_type_raw(),
                                                     DocType.CREDIT_CARD)
        blob = repr(summary)
        self.assertNotIn("440", blob)

    def test_other_rows_are_untouched(self):
        """除外対象**以外**が 1 行も変わらないこと。"""
        raw = p4_type_raw()
        kept = card_entries._rows(raw)
        original = raw["rows"][:2]
        self.assertEqual(list(kept), original)


class ShortageCountingTest(unittest.TestCase):
    """行数の数え方は**再掲を含む**。含めないと偽の行欠け警告が出る。

    `rows_on_page` は券面に印字された行数で、再掲行も印字されている。
    会計側で 1 行落としたぶんを数え方にも波及させると
    「券面 3 行中 2 行のみ取得」という嘘の警告が立つ。
    """

    def test_visible_rows_counts_the_reprint(self):
        self.assertEqual(len(card_salvage._visible_rows(p4_type_raw())), 3)

    def test_no_false_shortage_after_the_filter(self):
        self.assertIsNone(card_salvage.detect_shortage(p4_type_raw()),
                          "再掲を除外したせいで偽の行欠けが出ている")

    def test_a_real_shortage_still_fires(self):
        raw = p4_type_raw()
        raw["rows_on_page"] = 5          # 券面 5 行と申告したのに 3 行しかない
        self.assertIsNotNone(card_salvage.detect_shortage(raw))


class FilterBypassGuardTest(unittest.TestCase):
    """`_all_rows` を会計経路から呼んでいないことを AST で見張る。

    `_rows` が安全な既定である設計は、誰かが `_all_rows` を直接呼んだ瞬間に
    黙って崩れる（例外も出ず、テストも緑のまま過剰計上が復活する）。
    """

    ALLOWED = {"_rows"}          # `_rows` の中からだけ呼んでよい

    def test_all_rows_is_only_called_from_the_filter(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "card_entries.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "_all_rows"
                        and node.name not in self.ALLOWED):
                    offenders.append(node.name)
        self.assertEqual(offenders, [],
                         f"会計経路が全行を直接読んでいる: {offenders}")


if __name__ == "__main__":
    unittest.main()


class PromptInstructionTest(unittest.TestCase):
    """L1: prompt 側で再掲区画の行を出させない（Plan T1-2）。

    L2（Python）は区画ラベルが報告された頁でしか効かない ——
    原票 p9 は `sections` が空で出た。そこは prompt が担う。

    逆に prompt が漏らしても L2 が拾えるよう、**見出しの逐語を sections へ
    必ず入れる**ことも要求する。二層が互いの穴を塞ぐ形にする。
    """

    def _prompt(self):
        # `ocr_engine.PROMPTS` 経由にすると google.generativeai を引いて
        # venv 無しで走らなくなる（`test_dependency_weight` が赤くなる）
        import card_prompts
        return card_prompts.CREDIT_CARD_PROMPT

    def test_prompt_forbids_emitting_reprint_rows(self):
        text = self._prompt()
        self.assertIn(REPRINT_HEADING, text,
                      "再掲区画の見出しが prompt に無い")
        head = text[text.index(REPRINT_HEADING):]
        self.assertIn("rows に入れないでください", head[:200])

    def test_prompt_requires_reporting_the_section_verbatim(self):
        """prompt が漏らしたとき L2 が拾えるようにするための要求。"""
        text = self._prompt()
        head = text[text.index(REPRINT_HEADING):]
        self.assertIn("sections", head[:400])

    def test_reprint_label_in_prompt_matches_the_python_table(self):
        """prompt の逐語と Python の判定表が**同じ文字列**であること。

        片方だけ書き換えると、Gemini は新しい見出しを報告するのに Python は
        古い表で照合し、二層とも素通りする（しかもテストは緑のまま）。
        """
        text = self._prompt()
        for label in card_entries.REPRINT_SECTION_LABELS:
            with self.subTest(label=label):
                self.assertIn(label, text)
