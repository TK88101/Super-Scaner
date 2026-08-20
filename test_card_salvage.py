"""card_salvage（截断サルベージ解析＋行欠け検出）の単体テスト。

**python3 単体（venv 無し）で走ること**が設計上の性質（stdlib のみ）。
`test_dependency_weight` が機械で見張る。T5 Plan §4 T5-1〜T5-3。

サルベージの安全性の核は「会計数値を絶対に破損させない」——
`"amount": 630` を `63` の位置で切ったテキストから 63 円の行を
組み立ててはならない（T5 Plan §3.2 の完了判定規則）。
"""
import json
import unittest

import card_entries
import card_reconciliation
import card_salvage
from card_salvage import SALVAGED_KEY, LineShortage
from ocr_test_fixtures import (etc_rows_raw, etc_rows_text_before_rows,
                               etc_rows_truncated_text)


def _dump(raw):
    return json.dumps(raw, ensure_ascii=False)


def _short(got, total=100):
    """券面 total 行のうち got 行しか救えなかった頁の raw_data。"""
    raw = etc_rows_raw(total)
    raw["rows"] = raw["rows"][:got]
    raw[SALVAGED_KEY] = True
    return raw


class EtcFixtureShapeTest(unittest.TestCase):
    """T5-1: 100 行フィクスチャの形状（区画境界・外貨行込み）。"""

    def test_100_rows_with_sections_and_fx(self):
        raw = etc_rows_raw(100)
        self.assertEqual(len(raw["rows"]), 100)
        self.assertEqual(raw["rows_on_page"], 100)
        self.assertEqual(len(raw["sections"]), 2)
        # 区画境界: line_no < 50 は sec 0、以降は sec 1
        self.assertEqual(raw["rows"][48]["sec"], 0)
        self.assertEqual(raw["rows"][49]["sec"], 1)
        fx = [r for r in raw["rows"] if r["currency"]]
        self.assertEqual([r["line_no"] for r in fx], [7, 63])
        for row in fx:
            self.assertIsNotNone(row["foreign_amount"])
            self.assertIsNotNone(row["fx_rate"])
        self.assertEqual(raw["total_amount"],
                         sum(r["amount"] for r in raw["rows"]))
        self.assertEqual(raw["printed_totals"][0]["count"], 100)

    def test_top_level_key_order_puts_rows_last(self):
        # サルベージの前提（prompt schema と同じく rows が最後。T5 Plan V1）
        self.assertEqual(list(etc_rows_raw(3).keys())[-1], "rows")

    def test_pure_data_function(self):
        # 同じ引数 → 同じ dict。呼出間で行 dict を共有しない
        first, second = etc_rows_raw(10), etc_rows_raw(10)
        self.assertEqual(first, second)
        first["rows"][0]["amount"] = -1
        self.assertNotEqual(first, second, "呼出間で行 dict が共有されている")


class SalvageTruncatedJsonTest(unittest.TestCase):
    """T5-2: 截断形態ごとの回収と、会計数値の破損防止。"""

    maxDiff = None

    def setUp(self):
        self.raw = etc_rows_raw(100)
        self.text = _dump(self.raw)

    def test_cut_before_row_63_keeps_first_62_rows_and_all_top_fields(self):
        got = card_salvage.salvage_truncated_json(etc_rows_truncated_text(62))
        self.assertEqual(got["rows"], self.raw["rows"][:62])
        self.assertEqual(got["rows_on_page"], 100)
        self.assertEqual(got["sections"], self.raw["sections"])
        self.assertEqual(got["printed_totals"], self.raw["printed_totals"])
        self.assertEqual(got["total_amount"], self.raw["total_amount"])
        self.assertEqual(got["card"], self.raw["card"])

    def test_cut_inside_amount_digits_drops_the_row_not_corrupts_it(self):
        # 「値の途中」形態（G3）＋ 会計数値破損の防止（T5-9 変異 #4/#15 の的）
        row63 = self.text.index('{"line_no": 63')
        amount_at = self.text.index('"amount": ', row63)
        cut = self.text[:amount_at + len('"amount": ') + 1]   # 数字は 1 桁だけ
        got = card_salvage.salvage_truncated_json(cut)
        self.assertEqual(got["rows"], self.raw["rows"][:62],
                         "途中で切れた行が回収された（金額破損の危険）")

    def test_cut_inside_string_value_drops_the_row(self):
        row63 = self.text.index('{"line_no": 63')
        merchant_at = self.text.index('"merchant": "', row63)
        cut = self.text[:merchant_at + len('"merchant": "') + 2]
        got = card_salvage.salvage_truncated_json(cut)
        self.assertEqual(got["rows"], self.raw["rows"][:62])

    def test_cut_right_after_inner_close_brace(self):
        # 「内側 } 直後」形態（G3）: 閉じた行オブジェクトはそれ自体で完結
        row63 = self.text.index('{"line_no": 63')
        brace = self.text.rindex("}", 0, row63)
        got = card_salvage.salvage_truncated_json(self.text[:brace + 1])
        self.assertEqual(got["rows"], self.raw["rows"][:62])

    def test_array_top_level_returns_none(self):
        # 「配列トップ」形態（G3）: schema 外。dict 以外は回収しない
        arr = json.dumps([{"a": 1}, {"b": 2}])
        self.assertIsNone(card_salvage.salvage_truncated_json(arr[:9]))
        self.assertIsNone(card_salvage.salvage_truncated_json(arr))

    def test_object_embedded_in_prose_is_not_salvaged(self):
        """散文に埋もれた JSON は救わない。

        「テキスト中から最初の `{` を探す」実装（`extract_json` の 3) と同型）
        へ寄せると、截断テキストでは**行オブジェクト 1 個**を応答全体と
        取り違える。schema 違反の応答を「救った」ことにするのが最も危ない。
        """
        prose = "以下が解析結果です:\n" + etc_rows_truncated_text(62)
        self.assertIsNone(card_salvage.salvage_truncated_json(prose))

    def test_cut_inside_first_member_returns_none(self):
        # 「閉じ括弧ゼロ」形態（G3）: 完結した top 級の値が 1 つも無い
        cut = self.text[:self.text.index('"issuer"') + 4]
        self.assertIsNone(card_salvage.salvage_truncated_json(cut))

    def test_cut_before_rows_reached_returns_top_only(self):
        got = card_salvage.salvage_truncated_json(etc_rows_text_before_rows())
        self.assertNotIn("rows", got, "rows の正規化は呼出側（ocr_engine）の責務")
        self.assertEqual(got["rows_on_page"], 100)
        self.assertEqual(got["total_amount"], self.raw["total_amount"])

    def test_fenced_truncated_input(self):
        # 「フェンス截断」形態（G3）: ```json で始まり閉じフェンスが無い
        cut = "```json\n" + etc_rows_truncated_text(62)
        got = card_salvage.salvage_truncated_json(cut)
        self.assertEqual(len(got["rows"]), 62)

    def test_degenerate_inputs_return_none_without_raising(self):
        # 「parts 空」相当（text 空）ほか、JSON オブジェクトでないテキスト
        for label, text in [("empty", ""), ("none", None),
                            ("prose", "応答を生成できませんでした"),
                            ("brace_only", "{"), ("garbage", "}}}]]")]:
            with self.subTest(input=label):
                self.assertIsNone(card_salvage.salvage_truncated_json(text))

    def test_never_raises_even_if_the_parser_itself_blows_up(self):
        """fail-open の契約（サルベージの失敗が記帳経路を壊してはならない）。"""
        original = card_salvage._salvage_object
        card_salvage._salvage_object = lambda text: 1 / 0
        try:
            self.assertIsNone(card_salvage.salvage_truncated_json(self.text[:-5]))
        finally:
            card_salvage._salvage_object = original

    def test_closed_object_with_trailing_garbage(self):
        # json.loads は "Extra data" で失敗するが、中身は完結している
        got = card_salvage.salvage_truncated_json('{"rows_on_page": 12} ゴミ')
        self.assertEqual(got, {"rows_on_page": 12})

    def test_malformed_member_sequence_stops_at_the_break(self):
        # キー位置に文字列でないものが来たら、そこまでで打ち切る
        got = card_salvage.salvage_truncated_json('{"rows_on_page": 12, 5: 2}')
        self.assertEqual(got, {"rows_on_page": 12})

    def test_number_needs_a_real_delimiter_not_just_one_more_char(self):
        """数値の完了判定が `_salvage` の strip に依存していないこと。

        `"amount": 63\\n` は「次の 1 文字がある」を満たすが、63 が 630 の
        途中でない保証にはならない。公開入口は strip でこの形を潰すので、
        規則そのものを突くには内部関数を直に叩く必要がある —— strip を
        誰かが外した日に、会計値の安全が黙って崩れないようにする。
        """
        self.assertEqual(card_salvage._salvage_object('{"total_amount": 63 \n'),
                         {})
        self.assertEqual(card_salvage._salvage_object('{"total_amount": 63 , '),
                         {"total_amount": 63})
        self.assertEqual(card_salvage._salvage_object('{"total_amount": 63}'),
                         {"total_amount": 63})

    def test_truncated_number_inside_an_array_is_dropped(self):
        """配列要素にも「数値は EOF で曖昧」規則が効くこと。

        標本の配列要素は全て dict（自己終端）なので、この規則を消しても
        行のテストは全部緑のまま通る —— 数値要素の標本でしか突けない。
        `23` は `230` の途中かもしれないので採ってはならない。
        """
        got = card_salvage.salvage_truncated_json(
            '{"rows_on_page": 3, "printed_totals": [1, 23')
        self.assertEqual(got, {"rows_on_page": 3, "printed_totals": [1]})

    def test_trailing_comma_array_is_salvaged_element_wise(self):
        # LLM がよく出す末尾カンマ。配列全体の decode は失敗するが要素は無事
        got = card_salvage.salvage_truncated_json(
            '{"rows_on_page": 2, "rows": [{"line_no": 1}, ]}')
        self.assertEqual(got["rows"], [{"line_no": 1}])
        self.assertEqual(got["rows_on_page"], 2)

    def test_full_valid_text_roundtrips(self):
        self.assertEqual(card_salvage.salvage_truncated_json(self.text), self.raw)

    def test_every_cut_position_is_safe_and_lossless(self):
        """全切断位置の掃引（面の保証）: 例外ゼロ・回収物は正本の完全一致部分のみ。

        数値・文字列の途中切断が「別の値」に化けないことをここで面で殺す
        （T5-9 変異 #4/#15）。行 dict は完全一致で比較するので、sec / 外貨
        フィールドの逐字保全も同時に検証される。
        """
        raw = etc_rows_raw(5, section_at=3, fx_at=(2,))
        text = _dump(raw)
        for pos in range(len(text) + 1):
            with self.subTest(pos=pos):
                got = card_salvage.salvage_truncated_json(text[:pos])
                if got is None:
                    continue
                self.assertIsInstance(got, dict)
                for row in got.get("rows", []):
                    self.assertIn(row, raw["rows"],
                                  "正本に無い行が合成された（値の破損）")
                for key, value in got.items():
                    self.assertIn(key, raw)
                    if key == "rows":
                        continue
                    if key in ("sections", "printed_totals"):
                        for elem in value:
                            self.assertIn(elem, raw[key])
                    else:
                        self.assertEqual(
                            value, raw[key],
                            "top 級の値が截断で別の値に化けた: %s" % key)


class DetectShortageTest(unittest.TestCase):
    """T5-3: 行欠け判定。中身までの等値 assert と got の正規化。"""

    def test_shortage_after_salvage_reports_exact_numbers(self):
        raw = etc_rows_raw(100)
        raw["rows"] = raw["rows"][:62]
        raw[SALVAGED_KEY] = True
        self.assertEqual(card_salvage.detect_shortage(raw),
                         LineShortage(expected=100, got=62))

    def test_row_skip_without_truncation_is_also_a_shortage(self):
        # T-b: 有効 JSON だが Gemini が行を読み飛ばした
        raw = etc_rows_raw(100)
        raw["rows"] = raw["rows"][:97]
        self.assertEqual(card_salvage.detect_shortage(raw),
                         LineShortage(expected=100, got=97))

    def test_unknown_expected_after_salvage_is_still_a_shortage(self):
        # 「分からない＝問題なし」に倒さない（評審 M4）
        shortage = card_salvage.detect_shortage({"rows": [], SALVAGED_KEY: True})
        self.assertIsNotNone(shortage)
        self.assertEqual(shortage,
                         LineShortage(expected=None, got=0))

    def test_satisfied_page_reports_nothing(self):
        self.assertIsNone(card_salvage.detect_shortage(etc_rows_raw(4)))

    def test_no_expected_without_salvage_reports_nothing(self):
        self.assertIsNone(card_salvage.detect_shortage({"rows": [{}, {}]}))

    def test_expected_accepts_digit_strings_and_rejects_junk(self):
        base = {"rows": [{}], SALVAGED_KEY: True}
        self.assertEqual(
            card_salvage.detect_shortage(dict(base, rows_on_page="3")),
            LineShortage(expected=3, got=1))
        # 解釈不能な expected は「不明」扱い（salvaged なので shortage は残る）
        self.assertEqual(
            card_salvage.detect_shortage(dict(base, rows_on_page="多数")),
            LineShortage(expected=None, got=1))

    def test_expected_accepts_whole_floats_and_rejects_booleans(self):
        base = {"rows": [{}], SALVAGED_KEY: True}
        self.assertEqual(
            card_salvage.detect_shortage(dict(base, rows_on_page=100.0)),
            LineShortage(expected=100, got=1))
        # bool は int の subclass なので、明示的に弾かないと True が 1 行になる
        self.assertEqual(
            card_salvage.detect_shortage(dict(base, rows_on_page=True)),
            LineShortage(expected=None, got=1))

    ROW_SHAPES = [("dict", {"a": 1}, 0), ("str", "x", 0), ("null", None, 0),
                  ("int", 7, 0),
                  ("tuple", ({"line_no": 1}, {"line_no": 2}), 2),
                  ("mixed", [{"line_no": 1}, "junk", None, {"line_no": 2}], 2)]

    def test_got_counts_only_rows_the_builder_sees(self):
        # rows が非 list・非 dict 要素混入でも誤計数しない
        for label, rows, want in self.ROW_SHAPES:
            with self.subTest(rows=label):
                shortage = card_salvage.detect_shortage(
                    {"rows": rows, "rows_on_page": 5, SALVAGED_KEY: True})
                self.assertEqual(
                    shortage, LineShortage(expected=5, got=want))

    def test_got_never_disagrees_with_the_builder(self):
        """`got` は**記帳側が数える行数**と常に一致すること。

        ここがズレると顧客が読む「券面100行中62行のみ取得」が帳簿の実態と
        食い違う。定義を 2 箇所に書けば必ず漂移する（実際 tuple の扱いで
        既に割れていた）ので、委譲していることを機械で固定する。
        """
        for label, rows, _ in self.ROW_SHAPES:
            with self.subTest(rows=label):
                raw = {"rows": rows}
                self.assertEqual(card_salvage._visible_rows(raw),
                                 list(card_entries._all_rows(raw)))
        raw = etc_rows_raw(12)
        self.assertEqual(card_salvage._visible_rows(raw),
                         list(card_entries._all_rows(raw)))

    def test_expected_reads_numbers_the_same_way_as_the_reconciler(self):
        """券面申告の件数は検算側（`printed_totals[].count`）と同じ規則で読む。"""
        for value in (100, "3", "多数", True, 100.0, 100.9, "１００", "12件",
                      0, -5, None, [1]):
            with self.subTest(value=value):
                expected = card_salvage._expected_rows({"rows_on_page": value})
                coerced = card_reconciliation._coerce_int(value)
                want = coerced if coerced and coerced > 0 else None
                self.assertEqual(expected, want)

    def test_got_always_equals_the_builder_count(self):
        """`got` の二重帳簿禁止 —— 基準は**記帳側**であって手写しではない。

        期待値をここで `[r for r in rows if isinstance(r, dict)]` と書き直すと、
        `_visible_rows` が委譲をやめても両方が同じ間違いをして緑のままになる。
        """
        scenarios = {
            "salvage_62": _short(62),
            "skip_97": dict(etc_rows_raw(100), rows=etc_rows_raw(100)["rows"][:97]),
            "empty": {"rows": [], "rows_on_page": 9, SALVAGED_KEY: True},
        }
        for label, raw in scenarios.items():
            with self.subTest(scenario=label):
                self.assertEqual(card_salvage.detect_shortage(raw).got,
                                 len(card_entries._all_rows(raw)))

    def test_non_dict_raw_data_reports_nothing(self):
        for raw in (None, [], "x", 0):
            with self.subTest(raw=type(raw).__name__):
                self.assertIsNone(card_salvage.detect_shortage(raw))


class PageMarksTest(unittest.TestCase):
    """痕跡の優先規則（§3.4/§3.5）が 1 箇所に閉じていること。"""

    def test_shortage_page_gets_both_marks(self):
        raw = _short(62)
        shortage, reason = card_salvage.page_marks(raw)
        self.assertEqual(shortage, LineShortage(expected=100, got=62))
        self.assertEqual(reason, "line_shortage:62/100")

    def test_satisfied_salvage_gets_the_audit_only_mark(self):
        raw = etc_rows_raw(4)
        raw[SALVAGED_KEY] = True
        shortage, reason = card_salvage.page_marks(raw)
        self.assertIsNone(shortage, "帳簿へ提示行が出てしまう")
        self.assertEqual(reason, "salvaged:4/4")

    def test_healthy_page_gets_no_marks(self):
        self.assertEqual(card_salvage.page_marks(etc_rows_raw(4)), (None, None))

    def test_non_dict_gets_no_marks(self):
        self.assertEqual(card_salvage.page_marks(None), (None, None))

    def test_shortage_reason_wins_over_the_salvaged_reason(self):
        """救済かつ行欠けの頁で `salvaged:` が出ない（両方出すと二重痕跡）。"""
        _, reason = card_salvage.page_marks(_short(62))
        self.assertTrue(reason.startswith("line_shortage:"), reason)


class WordingTest(unittest.TestCase):
    """提示行 S 列文言と監査タブ reason の機械可読形式（§3.4/§3.5）。"""

    def test_memo_with_known_expected(self):
        self.assertEqual(
            card_salvage.shortage_memo(LineShortage(100, 62)),
            "⚠ 明細行の取得漏れ: 券面100行中62行のみ取得（原票を確認してください）")

    def test_memo_with_unknown_expected(self):
        self.assertEqual(
            card_salvage.shortage_memo(LineShortage(None, 7)),
            "⚠ 明細行の取得漏れ: AI応答が途中で切断"
            "（7行のみ取得・総数不明。原票を確認してください）")

    def test_audit_reason_is_machine_readable(self):
        self.assertEqual(
            card_salvage.shortage_audit_reason(LineShortage(100, 62)),
            "line_shortage:62/100")
        self.assertEqual(
            card_salvage.shortage_audit_reason(LineShortage(None, 0)),
            "line_shortage:0/?")


if __name__ == "__main__":
    unittest.main()
