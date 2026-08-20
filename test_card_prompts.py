"""`card_prompts`（T4-b）の単体テスト。

守るのは **prompt と消費側の同期**だけである。Gemini が実際にこの形で
返すかは T11（実呼出）でしか確認できない。それでもここを縛るのは、
消費側の schema を変えてプロンプトを直し忘れる事故が**無症状**だから ——
`page_dedup.safe_fingerprint` は静かに fail-open するので、重複頁が
二重記帳されても警告は 1 行も出ない。

**venv 無しで通ること**:

    python3 -m unittest test_card_prompts -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import card_prompts as cp                                    # noqa: E402


class GeneratedSchemaTest(unittest.TestCase):

    def test_every_required_key_appears_in_the_credit_card_prompt(self):
        # Assert
        for key in (cp.REQUIRED_CARD_KEYS + cp.CREDIT_CARD_ROW_KEYS
                    + cp.REQUIRED_TOP_KEYS):
            with self.subTest(key=key):
                self.assertIn('"%s"' % key, cp.CREDIT_CARD_PROMPT)

    def test_every_required_key_appears_in_the_transit_ic_prompt(self):
        # Assert
        for key in (cp.REQUIRED_IC_CARD_KEYS + cp.TRANSIT_IC_ROW_KEYS
                    + cp.REQUIRED_IC_TOP_KEYS):
            with self.subTest(key=key):
                self.assertIn('"%s"' % key, cp.TRANSIT_IC_PROMPT)

    def test_page_dedup_contract_keys_are_present(self):
        """訂正 3: これらのキー名がずれると重複判定が無症状で死ぬ。"""
        # Assert: `page_dedup` が実際に読むキー
        for key in ("card", "member_no", "statement_page", "issuer", "period",
                    "rows", "date", "amount", "total_amount"):
            with self.subTest(key=key):
                self.assertIn('"%s"' % key, cp.CREDIT_CARD_PROMPT)

    def test_comma_follows_the_value_not_the_comment(self):
        """注釈の後ろにカンマを置くとコメントの一部に見えて構造を読み違える。"""
        # Assert
        for prompt in (cp.CREDIT_CARD_PROMPT, cp.TRANSIT_IC_PROMPT):
            for line in prompt.splitlines():
                if "//" in line and line.strip().startswith('"'):
                    with self.subTest(line=line.strip()[:40]):
                        self.assertFalse(line.rstrip().endswith(","),
                                         "注釈の後ろにカンマが付いている")

    def test_stub_is_gone(self):
        # Assert: T1 のスタブ文言が残っていないこと
        for prompt in (cp.CREDIT_CARD_PROMPT, cp.TRANSIT_IC_PROMPT):
            self.assertNotIn("【未実装】", prompt)
            self.assertGreater(len(prompt), 1000)


class SchemaInvariantTest(unittest.TestCase):

    def test_merchant_t_number_is_never_asked_for(self):
        """F-11 / F-14: カード明細に加盟店の登録番号は存在しない。

        フィールドを作れば Gemini はカード会社の T番号で埋めてくる。
        存在しない概念を尋ねないのが最も確実な防御。
        """
        # Assert
        for prompt in (cp.CREDIT_CARD_PROMPT, cp.TRANSIT_IC_PROMPT):
            self.assertNotIn('"merchant_t_number"', prompt)

    def test_jpy_amount_is_not_a_separate_key(self):
        """AD-10 の `jpy_amount` は `rows[].amount` そのもの（訂正 3）。

        別名にすると `page_dedup.build_content_digest` が全行を読み飛ばす。
        """
        # Assert
        self.assertNotIn('"jpy_amount"', cp.CREDIT_CARD_PROMPT)
        self.assertIn('"amount"', cp.CREDIT_CARD_PROMPT)

    def test_transit_ic_prompt_forbids_guessing_the_year(self):
        """F-7: nimoca の券面に年は無い。推測させると静かに誤った年で記帳する。"""
        # Assert
        self.assertIn("年を推測しないでください", cp.TRANSIT_IC_PROMPT)

    def test_transit_ic_prompt_keeps_charge_rows(self):
        """行を落とさせない（記帳するかは Python 側が決める）。"""
        # Assert
        self.assertIn("charge", cp.TRANSIT_IC_PROMPT)
        self.assertIn("行を落とさないでください", cp.TRANSIT_IC_PROMPT)

    def test_both_prompts_forbid_putting_totals_into_rows(self):
        # Assert
        self.assertIn("合計行・小計行を rows に入れないでください",
                      cp.CREDIT_CARD_PROMPT)
        self.assertIn("rows に入れないでください", cp.TRANSIT_IC_PROMPT)

    def test_rows_on_page_is_defined_as_the_row_count_not_the_ink_count(self):
        """T5: 行欠け検出（`len(rows) < rows_on_page`）が成立する定義であること。

        `rows_on_page` が「券面の印字行すべて」だと、rows が除外する合計行や
        ポイント区画のぶん常に不足側へ振れ、健全な頁で提示行が出続ける。
        逆に「取得できた数」へ寄せると検出が循環して意味を失うので、
        **物理アンカー句も残っていること**を両方固定する。
        """
        for label, prompt in (("credit_card", cp.CREDIT_CARD_PROMPT),
                              ("transit_ic", cp.TRANSIT_IC_PROMPT)):
            with self.subTest(prompt=label):
                self.assertIn("rows に入れるべき行の数", prompt)
                self.assertIn("取得できた数ではなく券面に見えている数", prompt)


class SectionScopeTest(unittest.TestCase):
    """T8b-3: 「頁内の複数区画」と「跨頁の推測補完」を別の軸として書く。

    実害（2026-08-19 実測）: 旧文の「**この 1 ページに見えているカード
    1 枚分だけ**を報告してください」が、主副カード合印の券面で
    **副カード 11 行・146,671 円を丸ごと落とさせた**。Gemini の失敗ではなく
    指令どおりの動作である。しかも `rows_on_page` の申告も 8（＝取得数）
    だったので行欠け検出も沈黙した。

    ただし旧文の後半（他頁の情報で補わない）は **F-1（1 つの PDF に
    3 社分の明細が混在する）への対策として正しい**。消すと別の事故が戻る。
    2 つの軸を分けて両方残すのがこのテストの守る形（Plan §3.1）。
    """

    def test_the_one_card_limiter_is_gone(self):
        """区画を限縮する旧文が復活していないこと。"""
        self.assertNotIn("カード 1 枚分だけ", cp.CREDIT_CARD_PROMPT)

    def test_all_sections_on_the_page_must_be_reported(self):
        self.assertIn("すべての区画のすべての明細行", cp.CREDIT_CARD_PROMPT)

    def test_cross_page_inference_is_still_forbidden(self):
        """軸の分離であって緩和ではない。F-1 対策は残す。

        2026-08-20 更新: 作用域を**明細行**へ限定した。旧文
        「他ページの情報を推測で補わない」は情報一般を禁じており、
        年の確定（月日だけの日付欄 ＋ 頁頭の作成日から倒推する作業）まで
        巻き添えにして、同じ券面で 7 行が空・18 行が誤年になった。
        経緯と回帰は `test_card_date_year` が持つ。
        """
        self.assertIn("他ページの明細行を推測で補わないでください",
                      cp.CREDIT_CARD_PROMPT)
        self.assertIn("印字されていない明細行を", cp.CREDIT_CARD_PROMPT)

    def test_rows_carry_their_section_index(self):
        """区画を全部報告させるなら、行がどちらに属するかも要る。

        `sec` が空だと T8b-2 の #2 検査（区画は複数・行は片側）が
        distinct=0 で鳴り続け、監査タブが雑音で埋まる。
        """
        self.assertIn("sec", cp.CREDIT_CARD_PROMPT)
        self.assertIn("どの区画に属するか", cp.CREDIT_CARD_PROMPT)

    def test_rows_on_page_counts_every_section(self):
        """甲（Plan §3.2）: 自己申告の側も区画で限縮させない。

        ここを直さないと、Gemini が主カードだけ返したとき申告も 8 のままで
        `card_salvage` の行欠け検出が沈黙する（実害そのものの形）。
        """
        self.assertIn("頁全体・全区画の合計", cp.CREDIT_CARD_PROMPT)

    def test_transit_ic_prompt_is_untouched(self):
        """nimoca に区画（sections）は無い。T8b-3 の変更を漏らさないこと。"""
        for phrase in ("すべての区画のすべての明細行", "頁全体・全区画の合計",
                       "どの区画に属するか"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, cp.TRANSIT_IC_PROMPT)


class DateYearRuleTest(unittest.TestCase):
    """2026-08-20 の回帰対応: 年を確定する推理路を塞がない。

    T8b-3 で F-1 対策として足した「このページに見えているものだけを使います」
    が、**年の確定**まで巻き添えにした。アメックスの日付欄は「12月18日」の
    ように月日だけで、年は頁頭の「明細書作成日」から倒推するしかない。
    結果、同じ券面で頁ごとに「null を返す／推測して当てる／推測して外す」が
    割れ、7 行が空・18 行が 1 年ずれた。

    直し方は**緩和ではなく作用域の限定**。跨頁の禁止は「明細行を作るな」に
    限り、年の確定は券面の錨から行わせる。プログラム側にも
    `card_entries._card_date` の兜底を置いた（prompt が効かなくても直る）。
    """

    def test_credit_card_prompt_states_the_year_rule(self):
        """錨の**名前**が schema に在るだけでは足りない。規則を明文で置く。

        `statement_date` は元から出力フィールドとして書かれていた。それでも
        Gemini は年を確定できなかった —— フィールドが在ることと、それを
        使って年を決めよという指示が在ることは別である。
        """
        self.assertIn("日付の年は券面から確定してください", cp.CREDIT_CARD_PROMPT)
        self.assertIn("明細書作成日から年を", cp.CREDIT_CARD_PROMPT)

    def test_the_over_broad_restriction_is_gone(self):
        """年の推理まで塞いだ一文が復活していないこと。"""
        self.assertNotIn("見えているものだけ", cp.CREDIT_CARD_PROMPT)

    def test_the_cross_page_row_ban_survives(self):
        """F-1（1 つの PDF に別会社の明細が混在）対策は残す。"""
        self.assertIn("他ページ", cp.CREDIT_CARD_PROMPT)
        self.assertIn("推測で補わないでください", cp.CREDIT_CARD_PROMPT)

    def test_transit_ic_still_forbids_guessing_the_year(self):
        """nimoca には錨が無い。**逆の規則**をここへ持ち込ませない。

        券面に作成日も期間も印字されていないので、年を推させると必ず
        当て推量になる（F-7）。credit_card 側の修正が波及したら赤くする。
        """
        self.assertIn("年を推測しないでください", cp.TRANSIT_IC_PROMPT)
        self.assertNotIn("明細書作成日", cp.TRANSIT_IC_PROMPT)


if __name__ == "__main__":
    unittest.main()
