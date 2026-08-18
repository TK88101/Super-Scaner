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


if __name__ == "__main__":
    unittest.main()
