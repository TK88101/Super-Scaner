"""既存 doc_type の MF 行（A:AB 28 列）を丸ごと凍結する特徴づけテスト。

**これは「正しさ」のテストではなく「今の挙動」のテストである。**
T6（`sheets_output` の `line_mode` ゲート。`docs/plans/2026-08-18-t6-line-mode-gate.md`）
は `append_entries` を改造する。その DoD 第一条が「既存 doc_type の golden row
snapshot が完全一致」であり、本ファイルがその snapshot である。

**捕獲の順序が本質**（Plan §2 の鉄則）: 本ファイルは `sheets_output.py` を
1 文字も改造していない状態で作られた。後から作れば、改造後の挙動を「正解」として
記録するだけになり、護欄の意味が消える。

既存 `test_sheets_output.py`（57 テスト・81 断言）は列を抜き出しての部分断言しか
持たない（整行比較はゼロ）。部分断言は「見ていない列が変わったこと」を検出できない
ので、逐欄の丸ごと比較を別ファイルとして足す。既存ファイルは T6 の受入基準 A2 に
より**無修正**で保つ。

gspread を import するため venv311 で実行する:
    venv311/bin/python -m unittest test_sheets_output_golden -v
"""
import io
import os
import sys
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime as _datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import sheets_output
# 私有属性を揃えるファクトリと最小 worksheet は既存テストから借りる。
# `test_sheets_output._make_writer` の docstring が「3 本の手抄
# ファクトリが非同期化した反省」を記録している —— ここで 4 本目を
# 手書きすると、`SheetsOutputWriter.__init__` に快取属性が増えた
# ときに直し漏れる側が 1 つ増える。**あちらは無修正のまま**借りるだけ。
from test_sheets_output import _FakeWorksheet, _make_writer
from doc_types import DocType
from sheets_output import (APPEND_RESULT_PLACEHOLDER, APPEND_RESULT_POSTED,
                           MF_HEADERS, SheetsOutputWriter)

# ── 28 列の並び（MF_HEADERS と同順。期待値を読むときの対照表）──────────
#  0 A 取引No          1 B 取引日        2 C 借方勘定科目   3 D 借方補助科目
#  4 E 借方部門         5 F 借方取引先     6 G 借方税区分     7 H 借方インボイス
#  8 I 借方金額(円)     9 J 借方税額      10 K 貸方勘定科目  11 L 貸方補助科目
# 12 M 貸方部門        13 N 貸方取引先    14 O 貸方税区分    15 P 貸方インボイス
# 16 Q 貸方金額(円)    17 R 貸方税額      18 S 摘要          19 T 仕訳メモ
# 20 U タグ           21 V MF仕訳タイプ  22 W 決算整理仕訳  23 X 作成日時
# 24 Y 作成者         25 Z 最終更新日時  26 AA 最終更新者   27 AB 原票URL

_JST = timezone(timedelta(hours=9))
_FROZEN = _datetime(2026, 8, 18, 12, 0, tzinfo=_JST)
_NOW = "2026/08/18 12:00"      # _FROZEN.strftime("%Y/%m/%d %H:%M")
_WHO = "従業員"
_URL = "http://example/src"

# チェックディジットが正しい法人番号。`_sanitize_invoice_num` を通過する。
# 誤った番号だと H列が空になり missing_invoice の黄タグが付くので、
# 「タグが付かない行」を 1 本も含まない偏った snapshot になってしまう。
_VALID_T = "T8010401088436"


class _FrozenDatetime:
    """`datetime.now(JST)` だけを凍結する最小の代役。

    X列 作成日時 / Z列 最終更新日時 は現在時刻なので、凍結しないと
    snapshot が毎回変わって比較にならない。
    """

    @staticmethod
    def now(tz=None):
        return _FROZEN


def _capture(doc_type, entries_data):
    """`append_entries` を流し、`(戻り値, 実際に書かれた行)` を返す。

    書式設定・容量確保・ハイライトは全て潰す —— 本テストが見るのは
    **行データそのもの**であって、色でも API 呼び出し回数でもない
    （それらは既存 `test_sheets_output.py` の担当）。
    """
    writer = _make_writer()
    ws = _FakeWorksheet()
    written = []

    with ExitStack() as stack:
        for name in ("_format_with_retry", "_apply_anomaly_highlight",
                     "_ensure_row_capacity", "_sanitize_trailing_once"):
            stack.enter_context(patch.object(SheetsOutputWriter, name))
        stack.enter_context(patch.object(
            SheetsOutputWriter, "_get_or_create_tab", return_value=ws))
        stack.enter_context(patch.object(
            SheetsOutputWriter, "_write_with_retry",
            side_effect=lambda _ws, rows: written.extend(rows)))
        stack.enter_context(patch.object(sheets_output, "datetime",
                                         _FrozenDatetime))
        stack.enter_context(patch.object(sheets_output, "format_cell_range"))
        with redirect_stdout(io.StringIO()):
            result = writer.append_entries("従業員", doc_type, entries_data,
                                           source_url=_URL)
    return result, written


class _GoldenCase(unittest.TestCase):
    """payload → 28 列 の逐欄比較を行う共通の型。"""

    def assert_golden(self, doc_type, payload, expected_rows, expected_result):
        # Act
        result, rows = _capture(doc_type, payload)

        # Assert: 戻り値・行数・各行の 28 列すべて
        self.assertEqual(result, expected_result)
        self.assertEqual(len(rows), len(expected_rows))
        for i, (actual, expected) in enumerate(zip(rows, expected_rows)):
            with self.subTest(row=i):
                self.assertEqual(len(actual), len(MF_HEADERS))
                self.assertEqual(actual, expected)


class ReceiptGoldenTest(_GoldenCase):
    """領収書: ACCOUNT_MAP 写像 / 有効T番号 / 税額列 / 要確認科目の黄タグ。"""

    PAYLOAD = {
        "date": "2026/08/01",
        "vendor": "ファミリーマート福岡店",
        "invoice_num": _VALID_T,
        "memo": "出張時の備品購入",
        "ocr_confidence": 0.95,        # 低置信の全行黄を避ける
        "doc_category": "receipt",
        "total_amount": 8000,          # 5000 + 3000 = 一致（合計照合の赤を避ける）
        "entries": [
            # 「消耗品費」→ ACCOUNT_MAP →「備品・消耗品費」。タグは付かない
            {"debit_account": "消耗品費", "debit_tax_type": "課対仕入10%",
             "credit_account": "未払金", "credit_tax_type": "対象外",
             "amount": 5000, "description": "10%", "debit_tax_amount": 454},
            # 「地代家賃」は要確認科目 → account_review(low) → U列「黄系」
            {"debit_account": "地代家賃", "debit_tax_type": "課対仕入8% (軽)",
             "credit_account": "未払金", "credit_tax_type": "対象外",
             "amount": 3000, "description": "8%", "debit_tax_amount": 222},
        ],
    }

    EXPECTED = [
        [1, '2026/08/01', '備品・消耗品費', '', '', 'ファミリーマート福岡店',
         '課対仕入10%', _VALID_T, 5000, 454, '未払金', '', '', '', '対象外', '',
         5000, '', 'ファミリーマート福岡店 10%', '出張時の備品購入', '', '', '',
         _NOW, _WHO, _NOW, _WHO, _URL],
        [1, '2026/08/01', '地代家賃', '', '', 'ファミリーマート福岡店',
         '課対仕入8% (軽)', _VALID_T, 3000, 222, '未払金', '', '', '', '対象外', '',
         3000, '', 'ファミリーマート福岡店 8%', '出張時の備品購入', '黄系', '', '',
         _NOW, _WHO, _NOW, _WHO, _URL],
    ]

    def test_golden_row(self):
        self.assert_golden(DocType.RECEIPT, self.PAYLOAD, self.EXPECTED,
                           APPEND_RESULT_POSTED)

    def test_both_rows_share_one_transaction_no(self):
        """複合仕訳: 1 文書 = 1 取引 なので A列は全行同じ。

        T6 の `line_mode` はここを行ごと連番へ変える。既存はこのままであること。
        """
        _, rows = _capture(DocType.RECEIPT, self.PAYLOAD)
        self.assertEqual([r[0] for r in rows], [1, 1])


class PurchaseInvoiceGoldenTest(_GoldenCase):
    """支払請求書: 「店名 - 内容」摘要 / 貸方専用科目の借方置換。"""

    PAYLOAD = {
        "date": "2026/07/31",
        "vendor": "株式会社サンプル商事",
        "invoice_num": _VALID_T,
        "memo": "7月分請求",
        "entries": [
            {"debit_account": "備品・消耗品費", "debit_tax_type": "課対仕入10%",
             "credit_account": "普通預金", "credit_tax_type": "対象外",
             "amount": 120000, "description": "部品代"},
            # 借方に貸方専用科目「未払金」→ CREDIT_ONLY_ACCOUNTS で
            # 「未確定勘定」へ置換 → undetermined_account(medium) → U列「橙系」。
            # T6-8 はこの置換を line_mode でのみ豁免する。既存はここが正。
            {"debit_account": "未払金", "debit_tax_type": "課対仕入10%",
             "credit_account": "普通預金", "credit_tax_type": "対象外",
             "amount": 30000, "description": "貸方専用科目が借方に来た場合"},
        ],
    }

    EXPECTED = [
        [1, '2026/07/31', '備品・消耗品費', '', '', '株式会社サンプル商事',
         '課対仕入10%', _VALID_T, 120000, '', '普通預金', '', '', '', '対象外', '',
         120000, '', '株式会社サンプル商事 - 部品代', '7月分請求', '黄系', '', '',
         _NOW, _WHO, _NOW, _WHO, _URL],
        [1, '2026/07/31', '未確定勘定', '', '', '株式会社サンプル商事',
         '課対仕入10%', _VALID_T, 30000, '', '普通預金', '', '', '', '対象外', '',
         30000, '', '株式会社サンプル商事 - 貸方専用科目が借方に来た場合',
         '7月分請求', '橙系', '', '', _NOW, _WHO, _NOW, _WHO, _URL],
    ]

    def test_golden_row(self):
        self.assert_golden(DocType.PURCHASE_INVOICE, self.PAYLOAD,
                           self.EXPECTED, APPEND_RESULT_POSTED)


class SalesInvoiceGoldenTest(_GoldenCase):
    """売上請求書: 貸方側に税区分が乗る / T番号なしの黄タグ。"""

    PAYLOAD = {
        "date": "2026/07/20",
        "vendor": "得意先株式会社",
        "invoice_num": "",
        "memo": "",
        "entries": [
            {"debit_account": "売掛金", "debit_tax_type": "対象外",
             "credit_account": "売上高", "credit_tax_type": "課税売上10%",
             "amount": 250000, "description": "コンサル料"},
        ],
    }

    EXPECTED = [
        [1, '2026/07/20', '売掛金', '', '', '得意先株式会社', '対象外', '',
         250000, '', '売上高', '', '', '', '課税売上10%', '', 250000, '',
         '得意先株式会社 - コンサル料', '', '黄系', '', '',
         _NOW, _WHO, _NOW, _WHO, _URL],
    ]

    def test_golden_row(self):
        self.assert_golden(DocType.SALES_INVOICE, self.PAYLOAD, self.EXPECTED,
                           APPEND_RESULT_POSTED)


class SalarySlipGoldenTest(_GoldenCase):
    """給与明細: producer が出す `credit_sub_account` が L列に**出ない**ことの固定。

    `_build_entries_from_salary_slip`（`ocr_engine.py:1743` 他）は
    `credit_sub_account`（社会保険料 / 源泉所得税 / 住民税 / その他控除）を
    entry に載せているが、`append_entries` は L列を `""` 直書きしており
    （`_determine_credit_sub_account` は既存 doc_type にとっては死コードのまま。T6-7 が結線するのは `line_mode` 経路だけ）、この情報は届いていない。

    T6-7 は L列を `line_mode` の**ときだけ**結線する。全 doc_type へ広げると
    ここが `""` → `社会保険料` に変わってしまうため、その事故を本テストで止める。
    """

    PAYLOAD = {
        "date": "2026/07/25",
        "vendor": "山田太郎",
        "invoice_num": "",
        "memo": "7月給与",
        "entries": [
            {"debit_account": "給料手当", "debit_tax_type": "対象外",
             "credit_account": "普通預金", "credit_tax_type": "対象外",
             "amount": 280000, "description": "給与 山田太郎 (差引支給額)"},
            {"debit_account": "給料手当", "debit_tax_type": "対象外",
             "credit_account": "預り金", "credit_tax_type": "対象外",
             "amount": 45000, "credit_sub_account": "社会保険料",
             "description": "社会保険料控除 山田太郎"},
        ],
    }

    EXPECTED = [
        [1, '2026/07/25', '給料手当', '', '', '山田太郎', '対象外', '',
         280000, '', '普通預金', '', '', '', '対象外', '', 280000, '',
         '山田太郎 - 給与 山田太郎 (差引支給額)', '7月給与', '黄系', '', '',
         _NOW, _WHO, _NOW, _WHO, _URL],
        [1, '2026/07/25', '給料手当', '', '', '山田太郎', '対象外', '',
         45000, '', '預り金', '', '', '', '対象外', '', 45000, '',
         '山田太郎 - 社会保険料控除 山田太郎', '7月給与', '黄系', '', '',
         _NOW, _WHO, _NOW, _WHO, _URL],
    ]

    def test_golden_row(self):
        self.assert_golden(DocType.SALARY_SLIP, self.PAYLOAD, self.EXPECTED,
                           APPEND_RESULT_POSTED)

    def test_credit_sub_account_does_not_reach_column_l(self):
        """L列が空であることを単独でも固定する（既存 doc_type の現状維持）。"""
        _, rows = _capture(DocType.SALARY_SLIP, self.PAYLOAD)
        self.assertEqual(rows[1][11], "")
        self.assertEqual(self.PAYLOAD["entries"][1]["credit_sub_account"],
                         "社会保険料")   # producer 側には値が在る


class UnrecognizedRowGoldenTest(_GoldenCase):
    """占位行（`_write_unrecognized_row`）の 28 列。

    T6-4 は `line_mode` で「金額 0 でも 1 行書く」ようにする。その結果
    占位行へ落ちる条件が変わるので、既存側の形をここで固定しておく。
    """

    FULL_PAYLOAD = {
        "date": "", "vendor": "", "invoice_num": "",
        "memo": "整形処理エラー", "entries": [], "_unrecognized": True,
    }
    FULL_EXPECTED = [
        [1, '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '',
         '整形処理エラー', '', '赤系', '', '', _NOW, '', _NOW, '', _URL],
    ]

    PARTIAL_PAYLOAD = {
        "date": "2026/08/05", "vendor": "読めた店名", "invoice_num": "",
        "memo": "",
        "entries": [
            {"debit_account": "消耗品費", "debit_tax_type": "課対仕入10%",
             "credit_account": "現金", "credit_tax_type": "対象外",
             "amount": 0, "description": "金額なし"},
        ],
    }
    PARTIAL_EXPECTED = [
        [1, '2026/08/05', '', '', '', '読めた店名', '', '', '', '', '', '', '',
         '', '', '', '', '', '⚠ 部分認識（金額なし）', '', '赤系', '', '',
         _NOW, '', _NOW, '', _URL],
    ]

    def test_fully_unrecognized_page(self):
        self.assert_golden(DocType.RECEIPT, self.FULL_PAYLOAD,
                           self.FULL_EXPECTED, APPEND_RESULT_PLACEHOLDER)

    def test_zero_amount_entry_falls_through_to_placeholder(self):
        """金額 0 の entry は既存 doc_type では**書かれない**（skip → 占位行）。

        T6-4 が変えるのは `line_mode` 側だけで、ここは不変であること。
        """
        self.assert_golden(DocType.RECEIPT, self.PARTIAL_PAYLOAD,
                           self.PARTIAL_EXPECTED, APPEND_RESULT_PLACEHOLDER)

    def test_placeholder_rows_have_no_uploader(self):
        """占位行は Y列 作成者 / AA列 最終更新者 が空（既存挙動）。

        通常行は uploader 名が入るので、ここだけ違うことを明示的に固定する。
        """
        _, rows = _capture(DocType.RECEIPT, self.FULL_PAYLOAD)
        self.assertEqual((rows[0][24], rows[0][26]), ("", ""))


class GoldenCoverageTest(unittest.TestCase):
    """snapshot が既存 4 doc_type を漏れなく覆っていること。

    doc_type が増えたのに golden を足し忘れると、その doc_type は
    「28 列を誰も見ていない」状態で T6 の改造を通過してしまう。
    """

    COVERED = {DocType.RECEIPT, DocType.PURCHASE_INVOICE,
               DocType.SALES_INVOICE, DocType.SALARY_SLIP}

    # T6 時点で golden を持たない doc_type とその理由。
    # T6 の実装後、line_mode 側の snapshot は
    # `test_sheets_output_line_mode.py` が持つ。
    DEFERRED = {DocType.CREDIT_CARD, DocType.TRANSIT_IC}

    def test_every_doc_type_is_either_covered_or_deliberately_deferred(self):
        classified = self.COVERED | self.DEFERRED
        missing = set(DocType.ALL) - classified
        self.assertEqual(missing, set(),
                         "golden も繰延理由も無い doc_type: %s" % missing)

    def test_covered_and_deferred_do_not_overlap(self):
        self.assertEqual(self.COVERED & self.DEFERRED, set())


if __name__ == "__main__":
    unittest.main()
