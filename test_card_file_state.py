"""`card_file_state`（1 ファイル分の頁跨ぎ状態）の単体テスト。

守りたい実害は 2 つある。

1. **重複頁が二重記帳される** —— `page_dedup` は完全実装されているのに
   生産経路が一度も呼んでいなかった（T8 と同型の「判定は在るが誰も
   呼ばない」欠陥）。本モジュールがその接線の受け皿になる。

2. **明細書作成日（錨）が頁跨ぎで持ち回されず、年が確定できない** ——
   実測（2026-08-20）で TS CUBIC p6 は `month_day='3/16'` を 57 行すべてに
   持ちながら `statement_date=''` のため 57 行が空欄になっていた。

そして錨の継承それ自体が新しい危険を作る。**錨が別の明細書へ漏れると
無音の誤年になる**（趙の既裁定「空欄より無音の誤年の方が危険」）。
実測で TS CUBIC の PDF は混在文書だった —— p1 が トヨタファイナンスの
合計表、p5〜p9 が別建ての ETC 副明細書。したがって「同じファイルなら
継承してよい」は成立しない。

**同一明細書性の判定に使えるのは券面自身の頁付けだけ**というのが実測の
結論である（`issuer` は同一副明細書内で トヨタファイナンス/NEXCO と
食い違い、`member_no` は同一明細書内の副カードで食い違う）。

外部依存なし（gspread / paddleocr 不要）なので venv 無しでも動く:
    python -m unittest test_card_file_state -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from card_file_state import CardFileState  # noqa: E402
from doc_types import DocType  # noqa: E402
from ocr_test_fixtures import AMEX_HEAD  # noqa: E402
from page_dedup import VERDICT_DUPLICATE, VERDICT_UNIQUE  # noqa: E402


# ── 実測した券面の形（`probe2.py` / `probe3.py` 2026-08-20）────────────
#
# 合成 fixture ではなく**実物の値の構造**を使う。ここを作り物にすると、
# 「テストは緑だが実物では発火しない」という T8 の形に戻る。
# カード番号は券面のマスク表記そのままで、実在の数字は含めない。

def _card(statement_date="", statement_page="", member_no="", issuer=""):
    return {"statement_date": statement_date, "statement_page": statement_page,
            "member_no": member_no, "issuer": issuer}


def _raw(card=None, rows=None, total=0):
    return {"card": card if card is not None else _card(),
            "rows": list(rows or []), "total_amount": total}


# TS CUBIC: p5 が ETC 副明細書の 1 頁目（自前の錨あり）、p6 がその 2 頁目
_TS_P5 = _card(statement_date="2026/05/15", statement_page="1/ 5ページ",
               member_no="9200-0120-4450-2973", issuer="トヨタファイナンス株式会社")
_TS_P6 = _card(statement_date=None, statement_page="2/5",
               member_no="9200-0120-4450-2973", issuer="NEXCO")
# TS CUBIC p1 は 1 頁完結の合計表（ここから継承できてはいけない）
_TS_P1 = _card(statement_date="2026/05/15", statement_page="1/ 1ページ",
               member_no="6900-0512-4054-8959", issuer="トヨタファイナンス")

# ENEOS: p1 と p2 は同一明細書だが member_no が副カードで食い違う
_EN_P1 = _card(statement_date="2026/04/15", statement_page="1/2ページ",
               member_no="XXXX-XXXX-XXXX-8602", issuer="TS CUBIC")
_EN_P2 = _card(statement_date="", statement_page="2/ 2ページ",
               member_no="XXXX-XXXX-XXXX-3726", issuer="TS CUBIC")

# 実測の OCR テキスト（頁付けが読めているか否かが要点）
_OCR_TS_P6 = ("普通車 長田出 南森町 ETC 610 26:323 ETC通行料金/阪神 "
              "普通車 平井本線 東大阪了 ETC 1:140 ETC通行料金/N西日本 26:3:21")
_OCR_EN_P2 = ("利用 ボイント 二利用 お支払 今回回数 年月日 二利用店名 "
              "3 ケイニ 51.97 7:3801回松 1 7380Dr.Drivetル7麦野SS")
_OCR_EN_P1 = "カードご利用代金明細書兼請求書 2026年4月15日発行 1/2ペ 812-0884"


# 明細行は「月日だけ在って年が無い」形（実測 TS CUBIC p6 / ENEOS p2 の形）。
def _rows(*month_days):
    return [{"month_day": md, "amount": 610} for md in month_days]


# 錨年の下 2 桁を含む OCR テキスト（＝日付欄が読める品質の頁）。
# **構造の規則を見るテストの既定値**にする。年の背書という別軸の規則を
# 毎回書くと、構造テストが何を見ているのか読めなくなるため。
# 背書の規則そのものは `AnchorNeedsAReadableDateColumnTest` が見張る。
_OCR_WITH_YEAR = "ご利用年月日 26 3 16 ETC通行料金/N西日本 610"


def _resolve(state, card, ocr_text=_OCR_WITH_YEAR, page_num=1,
             doc_type=DocType.CREDIT_CARD, rows=None):
    """1 頁分の錨処理。戻り値は (行ごとの date 一覧, 監査シグナル)。

    **`statement_date` は注入しない。** 錨から確定した日付を行へ直接書く。
    こうすると `card_entries._card_date` から見て「Gemini が完全な日付を
    返した頁」と同じ形になり、**自前の錨を持つ頁の挙動は 1 バイトも
    変わらない**（§9 判定 7「改修前 289 行が逐字同一」を守る）。
    """
    raw = _raw(card=dict(card), rows=rows if rows is not None else _rows("3/16"))
    signal = state.resolve_anchor(doc_type, raw, ocr_text, page_num)
    return [r.get("date") for r in raw["rows"]], signal


class AnchorIsInheritedWithinOneStatementTest(unittest.TestCase):
    """実測した 2 つの券面で、錨が正しく引き継がれる。"""

    def test_ts_cubic_p6_inherits_from_p5(self):
        """57 行が空だった頁。p5（1/5 ページ）の作成日を継ぐ。"""
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        got, _ = _resolve(state, _TS_P6, _OCR_TS_P6, 6)
        self.assertEqual(got, ["2026/03/16"])

    def test_the_page_structure_of_eneos_is_recognised(self):
        """member_no が副カードで食い違っても、頁付けが連番なら構造は繋がる。

        ただし ENEOS p2 は**年の背書が無い**ので実際には継承しない
        （趙拍板 2026-08-20）。ここで見たいのは構造の判定だけなので、
        日付欄が読める品質の OCR を与えた場合を固定する。
        実物の ENEOS p2 の挙動は
        `AnchorNeedsAReadableDateColumnTest` が持つ。
        """
        state = CardFileState()
        _resolve(state, _EN_P1, _OCR_EN_P1, 1)
        got, _ = _resolve(state, _EN_P2, _OCR_WITH_YEAR, 2)
        self.assertEqual(got, ["2026/03/16"])

    def test_a_page_with_its_own_anchor_is_left_alone(self):
        """自前の錨を持つ頁の行には**触らない**。

        `card_entries._card_date` が従来どおり自分で年を決める。ここで
        先回りすると、改修前から日付が入っていた 289 行の挙動が変わる。
        """
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        own = _card(statement_date="2026/06/20", statement_page="2/5")
        got, _ = _resolve(state, own, _OCR_WITH_YEAR, 6)
        self.assertEqual(got, [None], "自前の錨を持つ頁の行を書き換えた")

    def test_the_run_walks_the_whole_statement(self):
        """1/5 で開いた run は 2/5・3/5・4/5・5/5 まで歩く。"""
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        for k, page_num in ((2, 6), (3, 7), (4, 8), (5, 9)):
            got, _ = _resolve(
                state, _card(statement_date="", statement_page="%d/5" % k),
                _OCR_WITH_YEAR, page_num)
            self.assertEqual(got, ["2026/03/16"], "k=%d で継承が切れた" % k)


class AnchorNeverCrossesAStatementBoundaryTest(unittest.TestCase):
    """**無音の誤年を作らない。** 疑わしければ空欄に倒す。

    実測で TS CUBIC の PDF は混在文書だった。ここが甘いと、合計表の
    作成日が別会社の ETC 明細に流れ込み、書式として正当な誤年になる。
    """

    def test_a_single_page_statement_never_lends_its_anchor(self):
        """`1/ 1ページ` の合計表から誰も継承できない（実測 TS CUBIC p1）。"""
        state = CardFileState()
        _resolve(state, _TS_P1, _OCR_WITH_YEAR, 1)
        got, _ = _resolve(state, _TS_P6, _OCR_TS_P6, 6)
        self.assertEqual(got, [None], "1 頁完結の錨が別明細書へ漏れた")

    def test_a_different_total_page_count_blocks_inheritance(self):
        """分母が違えば別の明細書。継承しない。"""
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        other = _card(statement_date="", statement_page="2/9")
        got, _ = _resolve(state, other, _OCR_WITH_YEAR, 6)
        self.assertEqual(got, [None])

    def test_a_gap_in_the_page_run_closes_it(self):
        """連番が飛んだら run を閉じる。"""
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        # 3/5 が先に来る（2/5 を飛ばした）→ 閉じる
        got, _ = _resolve(state, _card(statement_page="3/5"), _OCR_WITH_YEAR, 6)
        self.assertEqual(got, [None])
        # 閉じた後は 2/5 が来ても継承しない
        got2, _ = _resolve(state, _card(statement_page="2/5"), _OCR_WITH_YEAR, 7)
        self.assertEqual(got2, [None])

    def test_a_new_first_page_without_an_anchor_closes_the_run(self):
        """Codex 反例 1: 錨を持たない `1/N` を見たら run を閉じる。

        **この規則を単独で駆動する形にしてある。** 素直に
        「A 1/2 → A 2/2 → B 1/2 → B 2/2」と並べると、A の 2/2 で
        `k == N` に達して run が閉じてしまい、**本条件が無くてもテストが
        通る**（変異検証 2026-08-20 で実際に空振りしていた）。
        run が開いたままの位置に `1/N` を挟むこと。
        """
        state = CardFileState()
        _resolve(state, _card(statement_date="2026/05/15",
                              statement_page="1/ 5ページ"), _OCR_WITH_YEAR, 1)
        # ここで B の 1 頁目（錨が読めなかった）が割り込む。run はまだ開いている
        # （expected_next=2 で、次に来る 2/5 と番号が一致してしまう位置）。
        _resolve(state, _card(statement_page="1/5"), _OCR_WITH_YEAR, 2)
        got, _ = _resolve(state, _card(statement_page="2/5"), _OCR_WITH_YEAR, 3)
        self.assertEqual(got, [None],
                         "錨を持たない 1 頁目を見ても run が閉じていない")

    def test_the_same_shape_but_without_the_intruder_still_inherits(self):
        """否定対照: 割り込みが無ければ 2/5 は継承する。

        上のテストが「割り込み」で落ちていることの証明。これが無いと
        「そもそも 2/5 が継承できていないだけ」でも上が緑になる。
        """
        state = CardFileState()
        _resolve(state, _card(statement_date="2026/05/15",
                              statement_page="1/ 5ページ"), _OCR_WITH_YEAR, 1)
        got, _ = _resolve(state, _card(statement_page="2/5"), _OCR_WITH_YEAR, 3)
        self.assertEqual(got, ["2026/03/16"])

    def test_an_interleaved_statement_does_not_steal_the_run(self):
        """Codex 反例 2: A 1/5(錨) → A 2/5 → B 1/5(錨あり) → A 3/5。

        A の 3 頁目が B の年を継いではいけない。
        """
        state = CardFileState()
        _resolve(state, _card(statement_date="2026/05/15",
                              statement_page="1/ 5ページ"), _OCR_WITH_YEAR, 1)
        _resolve(state, _card(statement_page="2/5"), _OCR_WITH_YEAR, 2)
        _resolve(state, _card(statement_date="2024/01/10",
                              statement_page="1/5"), _OCR_WITH_YEAR, 3)
        got, _ = _resolve(state, _card(statement_page="3/5"), _OCR_WITH_YEAR, 4)
        self.assertEqual(got, [None], "別明細書の錨が A の後続頁へ漏れた")

    def test_a_page_with_no_label_at_all_closes_the_run(self):
        """**頁付けも錨も読めない頁は run を閉じる**（実施後評審 Round 2）。

        素通りさせると、その先の頁が前の明細書の錨を継ぐ:
          p1: 錨あり `1/2`      → run(total=2, expected_next=2)
          p2: 錨なし 頁付けなし  → 素通り（run が開いたまま）
          p3: 錨なし `2/2`      → k==expected_next で継承してしまう
        p2/p3 が別明細書なら書式として正当な誤年になり、±15 日ガードも
        通り抜ける。
        """
        # 錨は行の月日（3/16）から十分離す。近すぎると ±15 日ガードの方が
        # 先に効いてしまい、**この規則が無くてもテストが緑になる**
        # （最初に書いたときは錨を 2026/03/16 にしていて実際にそうなった）。
        state = CardFileState()
        _resolve(state, _card(statement_date="2026/05/15",
                              statement_page="1/2"), _OCR_WITH_YEAR, 1)
        _resolve(state, _card(statement_page=""), _OCR_WITH_YEAR, 2)
        got, _ = _resolve(state, _card(statement_page="2/2"),
                          _OCR_WITH_YEAR, 3)
        self.assertEqual(got, [None],
                         "頁付け不明の頁を跨いで錨が漏れた")

    def test_the_same_shape_without_the_unreadable_page_inherits(self):
        """否定対照: 頁付け不明の頁が無ければ 2/2 は継承する。

        これが無いと、上のテストは「そもそも 2/2 が継承できていないだけ」
        でも緑になる。
        """
        state = CardFileState()
        _resolve(state, _card(statement_date="2026/05/15",
                              statement_page="1/2"), _OCR_WITH_YEAR, 1)
        got, _ = _resolve(state, _card(statement_page="2/2"),
                          _OCR_WITH_YEAR, 2)
        self.assertEqual(got, ["2026/03/16"])

    def test_an_unreadable_page_label_blocks_inheritance(self):
        """頁付けが読めない頁は継承しない（趙の裁定「読めないときは推さない」）。"""
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        got, _ = _resolve(state, _card(statement_page=""), _OCR_WITH_YEAR, 6)
        self.assertEqual(got, [None])

    def test_an_anchor_page_without_a_readable_structure_closes_the_run(self):
        """自前の錨は持つが頁付けが読めない頁は、継承の連鎖を作らない。"""
        state = CardFileState()
        _resolve(state, _card(statement_date="2026/05/15", statement_page=""),
                 "", 1)
        got, _ = _resolve(state, _card(statement_page="2/5"), _OCR_WITH_YEAR, 2)
        self.assertEqual(got, [None])


class OcrHasVetoNotVoteTest(unittest.TestCase):
    """OCR は**拒否権**であって必須の裏取りではない（Codex 評審 R2-3）。

    実測: 直したい 2 頁（TS CUBIC p6 / ENEOS p2）は**どちらも OCR に
    頁付けが無い**。一致を必須にすると 59 行が全部空欄のまま残るので、
    「在って食い違うときだけ拒否」という非対称にしてある。
    """

    def test_no_page_label_in_ocr_means_no_veto(self):
        """OCR に頁付けの徴候が無ければ Gemini を使う（TS CUBIC p6 の形）。"""
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        got, _ = _resolve(state, _TS_P6, _OCR_TS_P6, 6)
        self.assertEqual(got, ["2026/03/16"])

    def test_a_contradicting_page_label_vetoes(self):
        """OCR が `4/5 ページ` と読めているのに Gemini が `2/5` → 継承しない。"""
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        got, _ = _resolve(state, _card(statement_page="2/5"),
                          "ETC ご利用明細書 4/5 ページ 26 3 16 通行料金", 6)
        self.assertEqual(got, [None])

    def test_a_matching_page_label_passes(self):
        """OCR と Gemini が一致していれば当然通る。"""
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        got, _ = _resolve(state, _card(statement_page="2/5"),
                          "ETC ご利用明細書 2/5 ページ 26 3 16 通行料金", 6)
        self.assertEqual(got, ["2026/03/16"])

    def test_page_words_without_digits_are_not_a_page_label(self):
        """「次ページへ続く」「ホームページ」は頁付けではない（実測 TS CUBIC p7）。"""
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        got, _ = _resolve(state, _card(statement_page="2/5"),
                          "次ペーへ統く ホームページからお手続き 26 3 16 可能です", 6)
        self.assertEqual(got, ["2026/03/16"],
                         "頁付けでない「ページ」語で拒否権が誤発火した")

    def test_the_amex_ocr_shape_is_not_a_contradiction(self):
        """実測のアメックス OCR は `116ペー`（区切りが落ちている）。矛盾ではない。"""
        state = CardFileState()
        _resolve(state, _card(statement_date="2026/05/21",
                              statement_page="1/6 ページ"),
                 "ご利用代金明細書 116ペー 26 3 16 17,295", 1)
        got, _ = _resolve(state, _card(statement_page="2/6 ページ"),
                          "利用明細 216ペー 26 3 16 中嶋秀一様", 2)
        self.assertEqual(got, ["2026/03/16"])


class InheritanceIsNeverSilentTest(unittest.TestCase):
    """継承を使ったら監査タブに残す。**ただしファイルにつき 1 回**。

    毎頁鳴らすと監査タブが狼少年になる（`cc_summary` を注記しないと
    決めたのと同じ理由）。
    """

    def test_the_first_inheritance_announces_itself(self):
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        _, signal = _resolve(state, _TS_P6, _OCR_TS_P6, 6)
        self.assertTrue(signal.startswith("date_year_from_page:"),
                        "継承が無音だった: %r" % signal)
        self.assertIn("5", signal, "どの頁から継いだかが残っていない")
        self.assertIn("2026/05/15", signal, "継いだ日付が残っていない")

    def test_later_inheritances_stay_quiet(self):
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        _resolve(state, _TS_P6, _OCR_TS_P6, 6)
        _, signal = _resolve(state, _card(statement_page="3/5"), _OCR_WITH_YEAR, 7)
        self.assertEqual(signal, "", "同一ファイルで 2 回鳴った")

    def test_a_page_that_did_not_inherit_says_nothing(self):
        state = CardFileState()
        _resolve(state, _TS_P1, _OCR_WITH_YEAR, 1)
        _, signal = _resolve(state, _TS_P6, _OCR_TS_P6, 6)
        self.assertEqual(signal, "")


class OnlyCreditCardIsTouchedTest(unittest.TestCase):
    """交通系IC と既存 4 doc_type は 1 バイトも変わらない。

    `transit_ic` の日付規則は `_ic_date`（実行日基準）で別物であり、
    `statement_date` は prompt にも無い。注入すると
    `CONSUMED_CARD_KEYS` の番人と食い違う。
    """

    def test_transit_ic_is_untouched(self):
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        raw = _raw(card=dict(_TS_P6), rows=_rows("3/16"))
        signal = state.resolve_anchor(DocType.TRANSIT_IC, raw, _OCR_WITH_YEAR, 6)
        self.assertIsNone(raw["rows"][0].get("date"))
        self.assertEqual(signal, "")

    def test_receipt_is_untouched(self):
        state = CardFileState()
        raw = {"documents": [{"date": "2026/01/05"}]}
        signal = state.resolve_anchor(DocType.RECEIPT, raw, _OCR_WITH_YEAR, 1)
        self.assertEqual(signal, "")
        self.assertEqual(raw, {"documents": [{"date": "2026/01/05"}]})


class DedupIsWiredThroughTheSameStateTest(unittest.TestCase):
    """重複判定の接線。判定そのものは `test_page_dedup` が持つ。"""

    # 標本の正本は `ocr_test_fixtures`（手写しは実物とズレる）。
    _AMEX_OCR = AMEX_HEAD + " 17,295"

    def _amex_raw(self, page="1/6"):
        return {
            "card": {"member_no": "****-******-26003", "issuer": "AMEX",
                     "statement_page": page, "period": "2026-05"},
            "rows": [{"date": "2026/04/10", "amount": 630},
                     {"date": "2026/04/10", "amount": 630},
                     {"date": "2026/04/21", "amount": 630},
                     {"date": "2026/04/21", "amount": 630}],
            "total_amount": 17295,
        }

    def test_a_rescanned_page_is_reported_as_duplicate(self):
        """実測のアメックス p1≡p3 の形。"""
        state = CardFileState()
        v1, t1 = state.classify(DocType.CREDIT_CARD, 1, self._AMEX_OCR,
                                self._amex_raw())
        self.assertEqual(v1.kind, VERDICT_UNIQUE)
        state.remember(t1, 1)

        v3, _ = state.classify(DocType.CREDIT_CARD, 3, self._AMEX_OCR,
                               self._amex_raw())
        self.assertEqual(v3.kind, VERDICT_DUPLICATE)
        self.assertEqual(v3.origin_page, 1)

    def test_an_unremembered_page_does_not_shadow_the_next_one(self):
        """記帳できなかった頁を登録しないので、次の同じ頁は素通りする。

        ここが逆になると、明細が **0 回**記帳される（最悪の欠陥）。
        """
        state = CardFileState()
        state.classify(DocType.CREDIT_CARD, 1, self._AMEX_OCR, self._amex_raw())
        # remember を呼ばない（＝ builder が 1 行も組めなかった頁）
        v3, _ = state.classify(DocType.CREDIT_CARD, 3, self._AMEX_OCR,
                               self._amex_raw())
        self.assertEqual(v3.kind, VERDICT_UNIQUE)

    def test_non_card_doc_types_get_no_verdict(self):
        state = CardFileState()
        verdict, token = state.classify(
            DocType.RECEIPT, 1, "領収書", {"documents": []})
        self.assertIsNone(verdict)
        self.assertIsNone(token)

    def test_remember_ignores_a_missing_token(self):
        state = CardFileState()
        state.remember(None, 1)          # 例外を投げないこと


class NothingEscapesTest(unittest.TestCase):
    """**公開メソッドは例外を外へ出さない**（IP-401 / Codex 評審 C10）。

    ここが漏れると `_yield_page_results` の整形例外になり、頁が占位行 1 行に
    潰れる。記帳を止めないという不変式を、状態オブジェクトごときで破らない。
    """

    class _Exploding(dict):
        def __init__(self):
            # 空 dict は falsy で `raw_data or {}` に差し替えられ、敵対的入力に
            # ならない（`test_page_dedup` が同じ罠を記録している）。
            super().__init__(rows=[])

        def get(self, *a, **k):
            raise RuntimeError("boom")

    def test_resolve_anchor_survives_a_hostile_raw_data(self):
        state = CardFileState()
        signal = state.resolve_anchor(
            DocType.CREDIT_CARD, self._Exploding(), _OCR_WITH_YEAR, 1)
        self.assertEqual(signal, "")

    def test_classify_survives_a_hostile_raw_data(self):
        state = CardFileState()
        verdict, token = state.classify(
            DocType.CREDIT_CARD, 1, "", self._Exploding())
        self.assertIsNone(token)

    def test_resolve_anchor_survives_a_non_dict(self):
        state = CardFileState()
        for hostile in (None, [], "text", 42):
            self.assertEqual(
                state.resolve_anchor(DocType.CREDIT_CARD, hostile, _OCR_WITH_YEAR, 1), "")

    def test_remember_survives_a_hostile_token(self):
        state = CardFileState()
        state.remember(object(), 1)      # 例外を投げないこと


class NoDriftFromTheSourcesOfTruthTest(unittest.TestCase):
    """自前で持っている値が、正本と食い違っていないことを機械で見張る。

    docstring で「同値である」と宣言しただけの約束は守られない
    （CLAUDE.md の ENTRY_BUILDERS 未登録事故と同族）。
    """

    def test_dedup_doc_types_match_page_family(self):
        import card_file_state
        import page_family

        self.assertEqual(
            set(card_file_state._DEDUP_DOC_TYPES),
            set(page_family.CC_FAMILY_DOC_TYPES),
            "重複判定の対象 doc_type が page_family と食い違っている")

    def test_anchor_doc_types_are_credit_card_only(self):
        """交通系IC を足したくなったら、`_ic_date` の規則を先に読むこと。"""
        import card_file_state

        self.assertEqual(set(card_file_state._ANCHOR_DOC_TYPES),
                         {DocType.CREDIT_CARD})

    def test_date_parsing_is_shared_with_card_entries(self):
        """錨が読めるかの判定は `_card_date` と**同じ関数**で行う。

        別の正規表現を書くと、`card_file_state` が「読める」と判断した値を
        `_card_date` が「読めない」と落とす（あるいはその逆の）ズレが出る。
        同じ raw_data を 2 通りの規則で読むな、という `card_salvage` の警告。
        """
        import card_entries
        import card_file_state

        self.assertIs(card_file_state._parse_ymd, card_entries._parse_ymd)

    def test_page_label_parsing_is_shared_with_card_entries(self):
        """頁付けの解析も `card_entries` と**同じ正規表現**で行う。

        リポジトリには既に 2 本の解析器が在り `'1/100'` で結果が割れる
        （`card_entries`=(1,100) / `page_dedup`=1/10。`test_card_entries.py:233`
        が意図的な差として記録）。3 本目を作ると、同じ `statement_page` を
        3 通りに読む状態になる。
        """
        import card_entries
        import card_file_state

        self.assertIs(card_file_state._RE_STATEMENT_PAGE,
                      card_entries._RE_STATEMENT_PAGE)

    def test_ocr_normalisation_is_shared_with_page_dedup(self):
        """OCR 文字列の正規化も `page_dedup` と**同じ関数**で行う。

        自前で書くと逐語複製になる。同じ OCR 文字列を
        `extract_page_identity`（頁付けの裏取り）と `_ocr_page_candidates`
        （拒否権）が別規則で読むと、両者の判定が食い違って発火する。
        """
        import card_file_state
        import page_dedup

        self.assertIs(card_file_state._norm, page_dedup._normalize_for_match)


class OcrPageCandidatesTest(unittest.TestCase):
    """拒否権の材料。**実測した OCR の並びで固定する。**

    ここを合成文字列で固めると、実物の癖（PaddleOCR が `/` を `1` と読む）を
    取り逃す。実際この実装中に、数字列の部分一致方式が
    アメックス p2（`216ペー` に対し Gemini `2/6`）で誤発火することが判った。
    """

    def _cand(self, text):
        import card_file_state
        return card_file_state._ocr_page_candidates(text)

    def test_amex_shape_with_the_separator_read_as_a_digit(self):
        self.assertIn((1, 6), self._cand("ご利用代金明細書 116ペー 17,295"))
        self.assertIn((2, 6), self._cand("利用明細 216ペー"))

    def test_a_kept_separator_is_read_directly(self):
        self.assertEqual(self._cand("2026年4月15日発行 1/2ペ"), {(1, 2)})

    def test_page_words_without_digits_yield_nothing(self):
        self.assertEqual(self._cand("次ペーへ統く ホームページからお手続き"), set())

    def test_the_two_pages_we_must_fix_have_no_page_label(self):
        """TS CUBIC p6 と ENEOS p2。ここが空でなくなったら拒否権が効きだす。"""
        self.assertEqual(self._cand(_OCR_TS_P6), set())
        self.assertEqual(self._cand(_OCR_EN_P2), set())

class AnchorYearMustBePlausibleTest(unittest.TestCase):
    """錨の年に妥当域を課す（趙拍板 2026-08-20・防護 A）。

    doc 級経路には `ocr_engine._validate_gemini_date` が年 2020–2027 を
    強制しているのに、card 系は `_apply_ocr_overrides` ごと豁免されて
    **この保護だけが抜けている**。実測（HEAD 0588bdc）:

        _card_date({'month_day':'3/16'}, {'statement_date':'1926/05/15'})
          → ('1926/03/16', True)
        anomaly_detector.detect_anomalies({'amount':610},{'date':'1926/03/16'})
          → missing_vendor / missing_invoice のみ（**日付の異常は 0 件**）

    錨の継承はこの穴を「1 頁」から「連続頁列ぜんぶ」へ広げるので、
    run を開く時点で止める。
    """

    def _run_with_anchor(self, anchor):
        # 年の背書は錨年に合わせる（ここで見たいのは妥当域の規則だけ）。
        backed = "ご利用年月日 %s 3 16 ETC通行料金" % anchor[2:4]
        state = CardFileState()
        _resolve(state, _card(statement_date=anchor,
                              statement_page="1/ 5ページ"), backed, 5)
        got, _ = _resolve(state, _card(statement_page="2/5"), backed, 6)
        return got

    def test_a_wild_year_never_opens_a_run(self):
        for anchor in ("1926/05/15", "2014/05/15", "2099/05/15", "2019/12/31"):
            with self.subTest(anchor=anchor):
                self.assertEqual(self._run_with_anchor(anchor), [None],
                                 "年 %s の錨で継承が起きた" % anchor)

    def test_the_boundaries_are_inclusive(self):
        self.assertEqual(self._run_with_anchor("2020/05/15"), ["2020/03/16"])
        self.assertEqual(self._run_with_anchor("2027/05/15"), ["2027/03/16"])

    def test_the_range_matches_the_doc_level_guard(self):
        """値の出所は `ocr_engine._validate_gemini_date`。乖離を機械で見張る。

        `ocr_engine` は paddleocr を引き連れているので import せず、
        ソースから読む（本モジュールの venv 非依存を壊さない）。
        """
        import ast
        import io as _io
        import card_file_state

        path = os.path.join(os.path.dirname(__file__), "ocr_engine.py")
        with _io.open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_validate_gemini_date")
        years = sorted({n.value for n in ast.walk(fn)
                        if isinstance(n, ast.Constant)
                        and isinstance(n.value, int) and n.value > 1900})
        self.assertEqual(
            years, [card_file_state.ANCHOR_YEAR_MIN,
                    card_file_state.ANCHOR_YEAR_MAX],
            "年の妥当域が doc 級経路と食い違う（ocr_engine 側=%s）" % years)


class AnchorMustBeRobustToSmallShiftsTest(unittest.TestCase):
    """継承した錨は「±15 日動かしても年が変わらない行」にだけ使う
    （趙拍板 2026-08-20・防護 B）。

    錨が別の明細書へ越境しても、**作成日の差が 15 日以内なら答は同じ**に
    なる。`_nearest_past` は ref の年と前年しか見ないので、行の月日が
    2 つの作成日の間に落ちたときだけ答が割れるからである。

    実測の余裕日数（行の月日 → 錨まで）: TS CUBIC p6 は 53〜60 日、
    ENEOS p2 の最小が 3/30 の **16 日**。したがって閾値 15 日は
    **1 行も落とさない**。
    """

    def _inherit(self, month_day, anchor="2026/05/15"):
        # 年の背書は錨年に合わせる（ここで見たいのは ±15 日の規則だけ）。
        backed = "ご利用年月日 %s 3 16 ETC通行料金" % anchor[2:4]
        state = CardFileState()
        _resolve(state, _card(statement_date=anchor,
                              statement_page="1/ 5ページ"), backed, 5)
        got, _ = _resolve(state, _card(statement_page="2/5"), backed, 6,
                          rows=_rows(month_day))
        return got[0]

    def test_the_measured_rows_all_survive(self):
        """実測 59 行のうち代表を固定。**閾値がこれらを落としたら回帰**。"""
        # TS CUBIC p6（錨 2026/05/15。余裕 53〜60 日）
        self.assertEqual(self._inherit("3/16"), "2026/03/16")
        self.assertEqual(self._inherit("3/23"), "2026/03/23")
        # ENEOS p2（錨 2026/04/15。3/30 は余裕 16 日 ＝ 最小）
        self.assertEqual(self._inherit("3/30", anchor="2026/04/15"),
                         "2026/03/30")
        self.assertEqual(self._inherit("3/7", anchor="2026/04/15"),
                         "2026/03/07")

    def test_a_row_on_the_year_boundary_is_refused(self):
        """錨を少し動かすと年が変わる行は空欄のまま残す。

        錨 2026/01/10 に対する 1月5日 は、錨が 15 日前（2025/12/26）だと
        2025/01/05 になる。**どちらが正しいか決められない行**なので
        触らない（空欄＋赤い missing_date）。
        """
        self.assertIsNone(self._inherit("1/5", anchor="2026/01/10"))

    def test_a_row_just_inside_the_window_survives(self):
        """境界の内側（16 日前）は採用される。閾値が効きすぎていない証明。"""
        self.assertEqual(self._inherit("12/25", anchor="2026/01/10"),
                         "2025/12/25")

    def test_a_row_that_already_has_a_full_date_is_untouched(self):
        """Gemini が完全な日付を返した行には触らない。"""
        state = CardFileState()
        _resolve(state, _card(statement_date="2026/05/15",
                              statement_page="1/ 5ページ"), _OCR_WITH_YEAR, 5)
        rows = [{"date": "2026/03/16", "month_day": "3/16", "amount": 610}]
        got, _ = _resolve(state, _card(statement_page="2/5"), "", 6, rows=rows)
        self.assertEqual(got, ["2026/03/16"])


class TheAuditLineCarriesThePopulationTest(unittest.TestCase):
    """監査行に「何行の年を・どの頁の・どの作成日から決めたか」を載せる。

    ファイルにつき 1 行しか出さない設計なので、その 1 行から
    **抜き取り検査の母集団が特定できる**必要がある。頁と作成日だけでは
    「何行が対象か」が判らず、会計士が機械推論値を受け入れられない。
    """

    def test_the_signal_carries_origin_anchor_and_row_count(self):
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        _, signal = _resolve(state, _TS_P6, _OCR_TS_P6, 6,
                             rows=_rows("3/16", "3/17", "3/20"))
        self.assertEqual(signal, "date_year_from_page:5@2026/05/15@3")

    def test_a_page_where_nothing_was_filled_stays_quiet(self):
        """1 行も埋まらなかった頁は鳴らない（狼少年にしない）。"""
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        _, signal = _resolve(state, _TS_P6, _OCR_TS_P6, 6,
                             rows=[{"month_day": "", "amount": 610}])
        self.assertEqual(signal, "")

class AnchorNeedsTheYearTokenOnThePageTest(unittest.TestCase):
    """OCR 平文に錨年の下 2 桁が出ない頁へは継承しない（趙拍板 2026-08-20）。

    **「日付欄の印字品質ガード」ではない。** 当初そう説明していたが実測で
    因果は否定された（`ガソリン 26,400` も `ETC 20:15` も通る）。
    現在の位置づけは**低情報頁への継承を抑える保守フィルタ**で、
    正当性は因果ではなく「失敗方向が空欄＋赤タグに限られること」にある。

    **年の決定源は錨であって、この検査ではない。** 下 2 桁を年の出所へ
    昇格させると、平成26年（＝2014）と西暦下 2 桁（＝2026）が同形である
    という既知の罠に嵌る。ここは拒否条件にとどめる。

    実測（2026-08-20。継承する母集団はこの 2 頁が全部）:

    | 頁 | OCR の `26` | Gemini の月日 |
    |---|---|---|
    | TS CUBIC p6 | **23 回** | 57 行すべて券面と一致 |
    | ENEOS p2 | **0 回** | 1/3 が誤り（券面 3月7日 → `3/3`） |

    ENEOS p2 を継承させると `2026/03/03` が**書式の正当な誤日付**として
    静かに記帳される。改修前は空欄＋赤い `missing_date` だった。
    趙の既裁定「空欄より無音の誤りの方が危険」に照らして継承しない。
    """

    def test_the_ts_cubic_page_is_backed_and_inherits(self):
        """実測の OCR テキストそのままで継承する（`26` が 23 回）。"""
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        got, _ = _resolve(state, _TS_P6, _OCR_TS_P6, 6)
        self.assertEqual(got, ["2026/03/16"])

    def test_the_eneos_page_is_not_backed_and_is_refused(self):
        """実測の OCR テキストそのままで継承しない（`26` が 0 回）。

        この 1 行が本規則の存在理由。落ちたら誤日付が帳簿へ入る。
        """
        state = CardFileState()
        _resolve(state, _EN_P1, _OCR_EN_P1, 1)
        got, signal = _resolve(state, _EN_P2, _OCR_EN_P2, 2)
        self.assertEqual(got, [None],
                         "日付欄が読めない頁へ錨が流れた（誤日付が記帳される）")
        self.assertEqual(signal, "", "継承していないのに監査行が出た")

    def test_an_empty_ocr_page_is_refused(self):
        """OCR テキストが無い頁（Vision 兜底）も継承しない。

        品質を判定する材料が無いので、推さない側へ倒す。
        """
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        got, _ = _resolve(state, _TS_P6, "", 6)
        self.assertEqual(got, [None])

    def test_the_run_survives_a_page_that_lacks_backing(self):
        """品質不足で **run は閉じない**。構造は繋がっているので後続は後続で判定。

        閉じてしまうと、たまたま OCR が荒れた 1 頁のせいで
        残りの明細頁すべてが道連れになる。
        """
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        blind, _ = _resolve(state, _card(statement_page="2/5"), "", 6)
        self.assertEqual(blind, [None], "前提: 2/5 は背書が無く継承しない")
        got, _ = _resolve(state, _card(statement_page="3/5"), _OCR_WITH_YEAR, 7)
        self.assertEqual(got, ["2026/03/16"], "1 頁の品質不足で run が閉じた")

    def test_the_year_token_is_not_the_source_of_the_year(self):
        """OCR に別の年が見えていても、年は**錨**が決める。

        下 2 桁を年の出所に昇格させない（平成26年＝2014 との同形問題）。
        """
        state = CardFileState()
        _resolve(state, _card(statement_date="2026/05/15",
                              statement_page="1/ 5ページ"), _OCR_WITH_YEAR, 5)
        # OCR には錨年の 26 と、無関係な 24 が混在する
        got, _ = _resolve(state, _card(statement_page="2/5"),
                          "ご利用年月日 26 3 16 ／ 平成 24 年の注記", 6)
        self.assertEqual(got, ["2026/03/16"], "OCR の別の年に引きずられた")

    def test_a_year_token_glued_to_other_digits_does_not_count(self):
        """`126` や `260` は年トークンではない（前後が数字なら不採用）。"""
        state = CardFileState()
        _resolve(state, _TS_P5, _OCR_WITH_YEAR, 5)
        got, _ = _resolve(state, _card(statement_page="2/5"),
                          "合計 126 円 260 円 1260 円", 6)
        self.assertEqual(got, [None])

    def test_a_four_digit_year_is_deliberately_not_accepted(self):
        """4 桁西暦は背書にしない。**見落としではなく設計**。

        探しているのは「年」ではなく「日付欄が欄で区切られて印字され、
        OCR がそれを読めた」証拠。独立した 2 桁トークンは分欄印字でしか
        出ないが、4 桁西暦は頁尾の注意書きにも出る。4 桁を許すと
        「日付欄は読めないが頁尾に年がある」頁が通り、この検査を入れた
        理由（ENEOS p2 型の誤日付）がそのまま戻る。

        このテストが赤くなったら、それは仕様変更であって修正ではない。
        """
        import card_file_state
        from datetime import date

        anchor = date(2026, 5, 15)
        for text in ("利用日 2026/3/16 ETC通行料金", "利用日 2026年3月16日",
                     "※本明細書は 2026年 の利用分です"):
            with self.subTest(text=text):
                self.assertFalse(
                    card_file_state._ocr_mentions_the_anchor_year(text, anchor),
                    "4 桁西暦を背書として受け入れてしまった")
        # 分欄印字（＝日付欄が読めた証拠）は通る
        for text in ("ご利用年月日 26 3 16 ETC", "26.3.16 ETC", "26/3/16 ETC"):
            with self.subTest(text=text):
                self.assertTrue(
                    card_file_state._ocr_mentions_the_anchor_year(text, anchor))

    def test_an_unrelated_number_containing_the_year_does_not_back_it(self):
        """`2026年` の中の `20`、電話番号の中の `20` などで通らない。"""
        import card_file_state
        from datetime import date

        self.assertFalse(card_file_state._ocr_mentions_the_anchor_year(
            "2026年4月15日発行", date(2020, 5, 15)), "`2026` の中の `20` で通った")
        self.assertFalse(card_file_state._ocr_mentions_the_anchor_year(
            "お問合せ 0120-965877", date(2020, 5, 15)))
        self.assertFalse(card_file_state._ocr_mentions_the_anchor_year(
            "合計 2,026 円", date(2026, 5, 15)))

    def test_whitespace_is_not_collapsed(self):
        """空白を畳むと判定が壊れる（実測: p6 が 23 → 5 回、p7 が 0 → 2 回）。

        券面は `26 | 3 | 16` と欄で分かれて印字されるので、空白を潰すと
        `26316` になって 2 桁トークンが消える。逆に無関係な数字が隣接して
        偽の一致を作る。
        """
        import card_file_state
        from datetime import date

        spaced = "ご利用年月日 26 3 16 ETC"
        self.assertTrue(card_file_state._ocr_mentions_the_anchor_year(
            spaced, date(2026, 5, 15)))
        self.assertFalse(card_file_state._ocr_mentions_the_anchor_year(
            spaced.replace(" ", ""), date(2026, 5, 15)))

class PageLabelIsNotGluedToTheCardNumberTest(unittest.TestCase):
    """券面で会員番号が頁付けの**前**に来る形でも誤否決しない。

    Codex 実施後評審（2026-08-20）の指摘。当初の実装は候補抽出の前に
    空白を畳んでおり、`****-******-26003 1/5 ページ` が
    `…260031/5ページ` になって候補が `(260031, 5)` になっていた。
    正しい Gemini 値 `1/5` が「矛盾」と判定され、**錨の run が一度も
    開かない** —— 継承先の頁の日付が全部空欄のまま残る。

    実測のアメックスは頁付けが会員番号より前に来るので露呈しなかったが、
    順序は券面ごとに違う。空白を保つことで数字列が欄で区切られる。
    """

    def _cand(self, text):
        import card_file_state
        return card_file_state._ocr_page_candidates(text)

    def test_a_card_number_before_the_page_label_does_not_glue(self):
        import card_file_state
        ocr = "ご利用代金明細書 ****-******-26003 1/5 ページ 26 3 16"
        self.assertIn((1, 5), self._cand(ocr))
        self.assertFalse(card_file_state._ocr_vetoes_page_label(ocr, (1, 5)),
                         "会員番号に引きずられて正しい頁付けを否決した")

    def test_a_space_between_the_number_and_the_page_word_is_allowed(self):
        """`1/5 ページ`（間に空白）も頁付けとして読む。"""
        self.assertIn((1, 5), self._cand("明細 1/5 ページ"))

    def test_the_amex_shape_still_works(self):
        """区切りが数字に化けた形（`116ペー`）は従来どおり読める。"""
        self.assertIn((1, 6), self._cand("ご利用代金明細書 116ペー 17,295"))
        self.assertIn((2, 6), self._cand("利用明細 216ペー 中嶋秀一様"))

    def test_a_long_digit_run_is_not_a_page_label(self):
        """電話番号のような長い数字列は頁付けにしない。"""
        self.assertEqual(self._cand("TEL 052-239-2298 ホームページ"), set())


class DedupFingerprintIsIndependentOfTheAnchorTest(unittest.TestCase):
    """指紋は**錨の副作用に依存しない**（Codex 実施後評審 2026-08-20）。

    `resolve_anchor` は行の `date` を書き換える。指紋はその `date` から
    作られるので、**同じ頁でも「継承できたか」で指紋が変わる**。
    継承できた 1 回目と、run が開いていない 2 回目とで digest が割れ、
    重複頁が `key_conflict` へ落ちて**二重計上される**。

    指紋は Gemini の生出力から作ること。
    """

    _OCR = "ご利用代金明細書 ****-******-26003 2/5 ページ 26 3 16 610"

    def _raw(self):
        return {"card": {"member_no": "****-******-26003", "issuer": "AMEX",
                         "statement_page": "2/5", "period": "2026-05",
                         "statement_date": ""},
                "rows": _rows("3/16", "3/17"),
                "total_amount": 1220}

    def test_the_digest_does_not_change_when_the_anchor_is_applied(self):
        import page_dedup

        # 錨が開いている状態で 1 回（＝ date が埋まる）
        anchored = CardFileState()
        _resolve(anchored, _card(statement_date="2026/05/15",
                                 statement_page="1/ 5ページ"), _OCR_WITH_YEAR, 5)
        raw_a = self._raw()
        anchored.resolve_anchor(DocType.CREDIT_CARD, raw_a, self._OCR, 6)
        self.assertTrue(raw_a["rows"][0].get("date"), "前提: 継承が起きている")

        # 錨が無い状態で 1 回（＝ date は空のまま）
        raw_b = self._raw()
        CardFileState().resolve_anchor(DocType.CREDIT_CARD, raw_b, self._OCR, 6)
        self.assertFalse(raw_b["rows"][0].get("date"), "前提: 継承は起きていない")

        # ★ どちらの頁も **Gemini の生出力は同一**。指紋も同一でなければ
        #   「1 回継承した頁」と「継承しなかった同じ頁」が別物になり、
        #   重複判定が duplicate ではなく key_conflict へ落ちる。
        fp_a = page_dedup.safe_fingerprint(self._OCR, self._raw())
        fp_b = page_dedup.safe_fingerprint(self._OCR, self._raw())
        self.assertEqual(fp_a.digest, fp_b.digest)

    def test_classify_runs_before_the_anchor_mutates_the_rows(self):
        """接線の順序を固定する。`classify` が `resolve_anchor` より前。

        AST で `_yield_page_results` の中の呼出順を見る。順序が入れ替わると
        上の性質が黙って壊れる（テストは両方とも緑のままになりうる）。
        """
        import ast
        import io as _io

        path = os.path.join(os.path.dirname(__file__), "ocr_engine.py")
        with _io.open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_yield_page_results")
        order = [n.func.attr for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr in ("classify", "resolve_anchor")]
        self.assertEqual(
            order, ["classify", "resolve_anchor"],
            "指紋は錨より先に取ること。逆だと『継承できたか』で指紋が変わり、"
            "重複頁が二重計上される（実際の呼出順=%s）" % order)

class TheIndexNeverHoldsPageContentTest(unittest.TestCase):
    """**逐頁ジェネレータのメモリモデルを守る**（効率評審 2026-08-20）。

    `CardFileState` は 1 ファイル分の寿命を持つ。ここに明細行・OCR
    テキスト・画像 bytes への参照が 1 つでも入ると、per-file メモリが
    O(頁数 × 頁の中身) に化ける。無人稼働の低メモリ miniPC で
    600dpi OOM の実測記録が既に在る（`ocr_engine.py` 冒頭）。

    docstring が主張している性質を、機械にも書いておく。
    """

    # 標本の正本は `ocr_test_fixtures`（手写しは実物とズレる）。
    _AMEX_OCR = AMEX_HEAD + " 17,295"

    def _raw(self):
        return {"card": {"member_no": "****-******-26003", "issuer": "AMEX",
                         "statement_page": "1/6", "period": "2026-05"},
                "rows": [{"date": "2026/04/10", "amount": 630},
                         {"date": "2026/04/21", "amount": 630}],
                "total_amount": 1260}

    def test_the_fingerprint_slots_hold_only_scalars(self):
        """`PageFingerprint.__slots__` に頁の中身を足させない。"""
        import page_dedup

        self.assertEqual(
            set(page_dedup.PageFingerprint.__slots__),
            {"identity", "digest", "line_count", "positive_total",
             "ocr_text_len"},
            "指紋の保持項目が増減した。`rows` や `ocr_text` を足すと"
            "per-file メモリが頁数に比例して増える")

    def test_the_state_holds_only_slots(self):
        import card_file_state

        self.assertEqual(set(card_file_state.CardFileState.__slots__),
                         {"_dedup", "_run", "_announced"})
        self.assertFalse(hasattr(CardFileState(), "__dict__"),
                         "`__slots__` が効いていない（属性が自由に生える）")

    def test_remembering_a_page_does_not_retain_its_rows(self):
        """登録した頁の `rows` / OCR テキストが索引から到達不能であること。"""
        state = CardFileState()
        raw = self._raw()
        rows_id = id(raw["rows"])
        _verdict, token = state.classify(
            DocType.CREDIT_CARD, 1, self._AMEX_OCR, raw)
        state.remember(token, 1)

        reachable = set()
        stack = [state._dedup]
        while stack:
            obj = stack.pop()
            if id(obj) in reachable:
                continue
            reachable.add(id(obj))
            if isinstance(obj, dict):
                stack.extend(obj.keys())
                stack.extend(obj.values())
            elif isinstance(obj, (list, tuple, set, frozenset)):
                stack.extend(obj)
            else:
                for slot in getattr(type(obj), "__slots__", ()):
                    if hasattr(obj, slot):
                        stack.append(getattr(obj, slot))
        self.assertNotIn(rows_id, reachable,
                         "索引が明細行への参照を握っている（per-file リーク）")
        self.assertNotIn(id(self._AMEX_OCR), reachable,
                         "索引が OCR テキストへの参照を握っている")


if __name__ == "__main__":
    unittest.main()
