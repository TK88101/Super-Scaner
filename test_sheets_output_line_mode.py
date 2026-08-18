"""`sheets_output` の逐行記帳（`line_mode`）挙動テスト。

対象は T6（`docs/plans/2026-08-18-t6-line-mode-gate.md`）で入れる明示ゲート。
既存 doc_type が変わっていないことは `test_sheets_output_golden.py` が守るので、
本ファイルは**新経路の側**だけを見る。

両方向を必ず固定する: 「`line_mode` でこうなる」だけを書くと、ゲートが常時 True に
なる変異を検出できない。「キーが無ければ従来どおり」を同じテストで assert する。

gspread を import するため venv311 で実行する:
    venv311/bin/python -m unittest test_sheets_output_line_mode -v
"""
import io
import os
import sys
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import datetime as _datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import anomaly_detector
import card_entries
import config
import invoice_classification
import sheets_output
from doc_types import DOC_TYPE_CONFIG, DocType
from invoice_classification import DeductionClass, DeductionVerdict
from test_sheets_output import _FakeWorksheet as _BaseFakeWorksheet
from test_sheets_output import _make_writer as _base_make_writer
from sheets_output import (APPEND_RESULT_PLACEHOLDER, APPEND_RESULT_POSTED,
                          MF_HEADERS, TAG_COL_INDEX, UNRECOGNIZED_TAG,
                          SheetsOutputWriter, is_bookable_row)

# 列 index（MF_HEADERS と同順）。読みやすさのため名前を付ける。
COL_TXN_NO, COL_DATE, COL_DEBIT_ACCOUNT = 0, 1, 2
COL_DEBIT_VENDOR, COL_DEBIT_TAX_TYPE, COL_DEBIT_INVOICE = 5, 6, 7
COL_AMOUNT, COL_CREDIT_ACCOUNT, COL_CREDIT_SUB = 8, 10, 11
COL_DESCRIPTION, COL_MEMO, COL_TAG = 18, 19, 20

_JST = timezone(timedelta(hours=9))
_FROZEN = _datetime(2026, 8, 18, 12, 0, tzinfo=_JST)


class _FrozenDatetime:
    @staticmethod
    def now(tz=None):
        return _FROZEN


class _FakeWorksheet(_BaseFakeWorksheet):
    """既存の最小 worksheet に、本ファイル専用の 2 機能だけ足した派生。

    基底（`test_sheets_output._FakeWorksheet`）を**書き換えず継承する**ので、
    `append_entries` が worksheet に何を要求するかという契約は 1 箇所のまま。
    """

    def __init__(self, existing_txn_nos=()):
        super().__init__()
        # 既存表の A列（採番の実測対象）を持たせる
        for no in existing_txn_nos:
            self._values.append([no] + [""] * (len(MF_HEADERS) - 1))

    def append_row(self, row, value_input_option=None):
        """`start_new_file` の分割線用。"""
        self._values.append(row)


def _start_new_file(writer, ws, doc_type, filename="file.pdf"):
    """`start_new_file` を fake ws 上で流す（分割線と採番リセットの再現）。"""
    with ExitStack() as stack:
        stack.enter_context(patch.object(
            SheetsOutputWriter, "_get_or_create_tab", return_value=ws))
        stack.enter_context(patch.object(sheets_output, "format_cell_range"))
        with redirect_stdout(io.StringIO()):
            writer.start_new_file("従業員", doc_type, filename)


def _make_writer():
    """既存ファクトリで私有属性を揃え、本ファイル用の 1 つだけ足す。

    属性一覧を手写ししない —— `test_sheets_output._make_writer` の docstring が
    「3 本の手抄ファクトリが非同期化した反省」を記録している。
    """
    writer = _base_make_writer()
    # `_capture` が `_get_or_create_tab` の引数を積む。タブ振り分けは
    # **folder doc_type のまま**（AD-T3-1・T4 §10-⑤）という契約を、列値の
    # 契約と同じテストの中で縛れるようにするため。
    writer.captured_tabs = []
    return writer


def _capture(doc_type, entries_data, writer=None, ws=None):
    """`append_entries` を流し `(戻り値, 書かれた行, writer, ws)` を返す。

    writer / ws を渡せば同一タブへの連続 append を再現できる（取引No の
    重複検出テスト用）。
    """
    writer = writer or _make_writer()
    ws = ws or _FakeWorksheet()
    written = []

    with ExitStack() as stack:
        for name in ("_format_with_retry", "_apply_anomaly_highlight",
                     "_ensure_row_capacity", "_sanitize_trailing_once"):
            stack.enter_context(patch.object(SheetsOutputWriter, name))
        def _tab(_self_or_name, *rest):
            writer.captured_tabs.append(_self_or_name)
            return ws

        stack.enter_context(patch.object(
            SheetsOutputWriter, "_get_or_create_tab", side_effect=_tab))
        # 書いた行を捕捉しつつ **fake ws にも反映する**。反映しないと
        # `_get_next_txn_no` が A列を実測できず、連続 append や
        # `start_new_file` を挟むテストが実装ではなくハーネスの都合で落ちる。
        def _write(target_ws, rows_to_write):
            written.extend(rows_to_write)
            target_ws.append_rows(rows_to_write)

        stack.enter_context(patch.object(
            SheetsOutputWriter, "_write_with_retry", side_effect=_write))
        stack.enter_context(patch.object(sheets_output, "datetime",
                                         _FrozenDatetime))
        stack.enter_context(patch.object(sheets_output, "format_cell_range"))
        with redirect_stdout(io.StringIO()):
            result = writer.append_entries("従業員", doc_type, entries_data,
                                           source_url="http://example/src")
    return result, written, writer, ws


def _card_entry(amount, date, merchant, memo, **extra):
    """`card_entries._base_entry` が出す形の最小 entry。

    実物のキー名に揃える —— テスト専用の形を作ると、producer が変わったときに
    テストだけが緑のまま取り残される。
    """
    entry = {
        "amount": amount,
        "date": date,
        "debit_vendor": merchant,
        "memo": memo,
        "description": merchant,
        "debit_account": "旅費交通費",
        "debit_tax_type": "課対仕入10%",
        "credit_account": "未払金",
        "credit_tax_type": "対象外",
        "credit_sub_account": "アメックス・ビジネス",
        "debit_invoice": "",
        "_deduction": None,
    }
    entry.update(extra)
    return entry


_FX_NOTE = "円貨額なし: USD 12.30"


def _placeholder_entry(reason=card_entries.PLACEHOLDER_NO_JPY, note=_FX_NOTE,
                       merchant="AWS", date="2026/05/02"):
    """`card_entries._placeholder` が**実際に**出す占位 entry。

    形を手写ししない —— producer が占位行の形を変えたときにテストだけが
    緑で取り残される（T5 §11.4 の手写し述語と同じ失敗様式）。
    """
    return card_entries._placeholder(
        _card_entry(None, date, merchant, ""), reason, note)


def _card_payload(entries, doc_type=DocType.CREDIT_CARD):
    """`ocr_engine._build_doc_result` が line_mode で出す形の result dict。

    カード明細の raw_data には **doc 級の date / vendor / invoice_num が
    存在しない**（`card_prompts.py` の card 級は member_no / statement_date /
    card_name のみ）。その現実を再現する —— doc 級に値を入れてしまうと
    行級化のテストが「たまたま同じ値」で通ってしまう。
    """
    return {
        "doc_type": doc_type,
        "date": None,
        "vendor": "",
        "invoice_num": "",
        "memo": "非記帳: 前回分口座振替 1件",
        "entries": entries,
        "_unrecognized": False,
        "line_mode": True,
    }


def _legacy_entry(amount=100, description="A", **overrides):
    """既存 doc_type（複合仕訳）の entry。`_card_entry` の対照側。

    line_mode 側にだけヘルパを用意して legacy 側を手写しすると、MF の必須列が
    増えたときに直す箇所が N 個になる。両方向テストが本ファイルの中心なので、
    対照側にも同じ道具を置く。
    """
    entry = {"debit_account": "旅費交通費", "debit_tax_type": "対象外",
             "credit_account": "現金", "credit_tax_type": "対象外",
             "amount": amount, "description": description}
    entry.update(overrides)
    return entry


def _legacy_payload(entries, **overrides):
    """既存 doc_type の result dict（`line_mode` キーを**持たない**のが要点）。"""
    payload = {"date": "2026/08/01", "vendor": "店", "invoice_num": "",
               "memo": "", "entries": entries}
    payload.update(overrides)
    return payload


class IsBookableRowTest(unittest.TestCase):
    """記帳可能行の述語（T5 §11.4 の申し送り: 手写しの共有解消）。

    `test_main_process_file._AmountAwareWriter` が `append_entries` の中段に
    埋まっていた判定を**逐字で写して**持っていた。写しは本物と分岐が割れうるので、
    純関数として切り出して両者で共有する。
    """

    # 非 line_mode は現行 `if not amount or int(amount) == 0: continue` と
    # **逐字等価**でなければならない。短絡位置まで含めて一致することが要点
    # —— `int()` を評価するタイミングが変わると例外挙動が変わる。
    LEGACY_CASES = (
        (None, False),
        (0, False),
        ("", False),
        ("0", False),      # truthy だが int で 0
        (0.4, False),      # truthy だが int で 0
        (1, True),
        (-3000, True),     # 負数は記帳対象（ポイント充当等）
        ("5000", True),
    )

    def test_legacy_predicate_matches_the_original_expression(self):
        for amount, expected in self.LEGACY_CASES:
            with self.subTest(amount=amount):
                self.assertEqual(
                    is_bookable_row({"amount": amount}), expected)

    def test_legacy_predicate_is_the_default(self):
        """`line_mode` を渡さなければ旧挙動（ゲートの既定は False）。"""
        self.assertFalse(is_bookable_row({"amount": 0}))

    def test_legacy_raises_on_non_numeric_just_like_the_original(self):
        """非数値は現行どおり例外。ここで防御を足すと既存挙動が変わる。

        既存 doc_type が汚いデータに当たったときの挙動を勝手に変えると、
        golden（きれいなデータ）では検出できない差分になる。
        """
        with self.assertRaises(ValueError):
            is_bookable_row({"amount": "abc"})

    def test_legacy_does_not_evaluate_int_when_amount_is_falsy(self):
        """falsy なら `int()` を評価しない（短絡位置が現行と同じこと）。

        `amount=None` で `TypeError` が出たら短絡していない証拠になる。
        """
        self.assertFalse(is_bookable_row({"amount": None}))
        self.assertFalse(is_bookable_row({}))

    # line_mode は金額 0 の占位 entry も書く（AD-T4-8）。
    # `card_entries._placeholder` が作る外貨占位行・要確認行を無音で
    # 落とさないため。ただし amount 欠損・None は通さない（Codex R3 P1）。
    LINE_MODE_CASES = (
        (0, True),          # 占位行。これが False だと外貨行が無音で消える
        ("0", True),
        (1, True),
        (-3000, True),
        (None, False),      # producer 契約違反。後段の int() が落ちる
    )

    def test_line_mode_keeps_zero_amount_rows(self):
        for amount, expected in self.LINE_MODE_CASES:
            with self.subTest(amount=amount):
                self.assertEqual(
                    is_bookable_row({"amount": amount}, line_mode=True),
                    expected)

    def test_line_mode_rejects_missing_amount_key(self):
        """キーごと無い entry も通さない。"""
        self.assertFalse(is_bookable_row({}, line_mode=True))

    def test_zero_amount_diverges_between_the_two_modes(self):
        """**両方向**の固定: 金額 0 の扱いが 2 つのモードで逆であること。

        片方だけ書くと「ゲートが常時 True」の変異を検出できない。
        """
        zero = {"amount": 0}
        self.assertFalse(is_bookable_row(zero, line_mode=False))
        self.assertTrue(is_bookable_row(zero, line_mode=True))


class LineModeGateTest(unittest.TestCase):
    """ゲートそのもの: キーが無ければ既存経路（AD-6）。"""

    def test_absent_key_keeps_the_legacy_path(self):
        """既存 doc_type は `line_mode` キー自体を持たない → 全行同一取引No。

        golden も同じことを守るが、あちらは 28 列の丸ごと比較なので
        「なぜ落ちたか」が読みにくい。ゲートの語義はここで単独に固定する。
        """
        payload = _legacy_payload(
            [_legacy_entry(100, "A"), _legacy_entry(200, "B")],
            vendor="店名", memo="doc 級メモ")
        _, rows, _, _ = _capture(DocType.RECEIPT, payload)
        self.assertEqual([r[COL_TXN_NO] for r in rows], [1, 1])

    def test_falsy_line_mode_value_also_keeps_the_legacy_path(self):
        """`line_mode: False` を明示された場合も旧経路（`bool()` 判定）。"""
        payload = _legacy_payload(
            [_legacy_entry(100, "A"), _legacy_entry(200, "B")],
            vendor="店名", line_mode=False)
        _, rows, _, _ = _capture(DocType.RECEIPT, payload)
        self.assertEqual([r[COL_TXN_NO] for r in rows], [1, 1])


class LineModeRowLevelColumnsTest(unittest.TestCase):
    """A / B / F / T 列の行級化（AD-6 の列対照表）。"""

    ENTRIES = [
        _card_entry(630, "2026/05/01", "西鉄バス",
                    "ETC NO : 12-3456 入：野芥西"),
        _card_entry(1200, "2026/05/03", "ファミリーマート", ""),
        _card_entry(7380, "2026/05/10", "ENEOS 給油", "軽油"),
    ]

    def _rows(self):
        _, rows, _, _ = _capture(DocType.CREDIT_CARD,
                                 _card_payload(self.ENTRIES))
        return rows

    def test_transaction_no_increments_per_row(self):
        """A列: 1 明細行 = 1 独立取引 なので行ごとに連番。"""
        self.assertEqual([r[COL_TXN_NO] for r in self._rows()], [1, 2, 3])

    def test_date_comes_from_the_entry(self):
        """B列: 行の利用日。doc 級 date は None なので回帰したら空になる。"""
        self.assertEqual([r[COL_DATE] for r in self._rows()],
                         ["2026/05/01", "2026/05/03", "2026/05/10"])

    def test_debit_vendor_comes_from_the_entry(self):
        """F列: 行ごとの加盟店。doc 級 vendor（空）を書いてはいけない。"""
        self.assertEqual([r[COL_DEBIT_VENDOR] for r in self._rows()],
                         ["西鉄バス", "ファミリーマート", "ENEOS 給油"])

    def test_memo_comes_from_the_entry(self):
        """T列: 行級メモ。doc 級 memo（非記帳サマリ）を全行へ配ると誤解を生む。"""
        self.assertEqual([r[COL_MEMO] for r in self._rows()],
                         ["ETC NO : 12-3456 入：野芥西", "", "軽油"])

    def test_empty_entry_date_falls_back_to_the_document_level(self):
        """B列: entry の日付が空なら doc 級へ回帰する（AD-6 の但し書き）。

        クレカ明細は doc 級 date を持たないので通常は空のままだが、
        回帰の経路自体は仕様なので doc 級に値を置いて確認する。
        """
        payload = _card_payload([_card_entry(500, "", "年不明の行", "")])
        payload["date"] = "2026/05/31"
        _, rows, _, _ = _capture(DocType.CREDIT_CARD, payload)
        self.assertEqual(rows[0][COL_DATE], "2026/05/31")

    def test_document_level_values_are_not_leaked_into_rows(self):
        """doc 級の vendor / memo が行へ漏れないこと（両方向の固定）。"""
        payload = _card_payload([_card_entry(500, "2026/05/01", "加盟店", "行メモ")])
        payload["vendor"] = "カード会社名"      # 漏れたら F列に出る
        payload["memo"] = "doc 級メモ"          # 漏れたら T列に出る
        _, rows, _, _ = _capture(DocType.CREDIT_CARD, payload)
        self.assertEqual(rows[0][COL_DEBIT_VENDOR], "加盟店")
        self.assertEqual(rows[0][COL_MEMO], "行メモ")


class TransactionNumberContinuityTest(unittest.TestCase):
    """取引No が衝突しないこと（AD-6 の Codex P0 ＋ R3 の P0）。

    `line_mode` は 1 回の append で N 個消費する。書戻しが `+1` のままだと
    2 回目の append が 2 番から始まり、**独立取引が同じ番号を持つ**。
    """

    ENTRIES_3 = [_card_entry(100, "2026/05/01", "店A", ""),
                 _card_entry(200, "2026/05/02", "店B", ""),
                 _card_entry(300, "2026/05/03", "店C", "")]
    ENTRIES_2 = [_card_entry(400, "2026/05/04", "店D", ""),
                 _card_entry(500, "2026/05/05", "店E", "")]

    def test_two_consecutive_appends_do_not_reuse_numbers(self):
        """同一タブへ連続 2 回 append（1 ファイル内の逐頁書込みに相当）。"""
        _, rows1, writer, ws = _capture(DocType.CREDIT_CARD,
                                        _card_payload(self.ENTRIES_3))
        _, rows2, _, _ = _capture(DocType.CREDIT_CARD,
                                  _card_payload(self.ENTRIES_2),
                                  writer=writer, ws=ws)

        first = [r[COL_TXN_NO] for r in rows1]
        second = [r[COL_TXN_NO] for r in rows2]
        self.assertEqual(first, [1, 2, 3])
        self.assertEqual(second, [4, 5])
        self.assertEqual(set(first) & set(second), set(),
                         "独立取引が同じ取引No を共有している")

    def test_start_new_file_does_not_reset_numbering_in_line_mode(self):
        """**ファイル境界**を跨いでも重複しない（Codex R3 P0）。

        `main` はファイル 1 件ごとに `start_new_file` を呼ぶ（`main.py:1098`）。
        「連続 2 回 append」だけのテストでは本番経路を通らないので、
        リセットを挟む経路を別に固定する。
        """
        writer = _make_writer()
        ws = _FakeWorksheet()

        _start_new_file(writer, ws, DocType.CREDIT_CARD, "cardA.pdf")
        _, rows1, _, _ = _capture(DocType.CREDIT_CARD,
                                  _card_payload(self.ENTRIES_3),
                                  writer=writer, ws=ws)
        _start_new_file(writer, ws, DocType.CREDIT_CARD, "cardB.pdf")
        _, rows2, _, _ = _capture(DocType.CREDIT_CARD,
                                  _card_payload(self.ENTRIES_2),
                                  writer=writer, ws=ws)

        first = [r[COL_TXN_NO] for r in rows1]
        second = [r[COL_TXN_NO] for r in rows2]
        self.assertEqual(set(first) & set(second), set(),
                         "別ファイルの独立取引が同じ取引No を共有している")

    def test_start_new_file_still_resets_for_legacy_doc_types(self):
        """既存 doc_type のリセットは 1 文字も変えない（1 ファイル = 1 取引）。"""
        writer = _make_writer()
        ws = _FakeWorksheet()
        payload = _legacy_payload([_legacy_entry()])

        _start_new_file(writer, ws, DocType.RECEIPT, "r1.pdf")
        _, rows1, _, _ = _capture(DocType.RECEIPT, payload, writer=writer, ws=ws)
        _start_new_file(writer, ws, DocType.RECEIPT, "r2.pdf")
        _, rows2, _, _ = _capture(DocType.RECEIPT, payload, writer=writer, ws=ws)

        self.assertEqual(rows1[0][COL_TXN_NO], 1)
        self.assertEqual(rows2[0][COL_TXN_NO], 1)   # 既存はリセットされるのが正

    def test_legacy_compound_entry_keeps_single_increment(self):
        """既存の複合仕訳（3 行 1 取引）は書戻しが `+1` のまま。

        無条件に `+ len(rows)` にすると既存の取引No が飛ぶ。
        """
        writer = _make_writer()
        ws = _FakeWorksheet()
        payload = _legacy_payload(
            [_legacy_entry(n, str(n)) for n in (100, 200, 300)])
        _, rows1, _, _ = _capture(DocType.RECEIPT, payload, writer=writer, ws=ws)
        _, rows2, _, _ = _capture(DocType.RECEIPT, payload, writer=writer, ws=ws)

        self.assertEqual([r[COL_TXN_NO] for r in rows1], [1, 1, 1])
        self.assertEqual([r[COL_TXN_NO] for r in rows2], [2, 2, 2])

    def test_numbering_continues_from_existing_sheet_content(self):
        """`line_mode` で `start_new_file` 後、既存表の A列 max+1 から続くこと。

        リセット抑止は「1 に戻さない」だけでなく「実測し直す」ことまで含む。
        """
        writer = _make_writer()
        ws = _FakeWorksheet(existing_txn_nos=(1, 2, 3, 4, 5))

        _start_new_file(writer, ws, DocType.CREDIT_CARD, "cardC.pdf")
        _, rows, _, _ = _capture(DocType.CREDIT_CARD,
                                 _card_payload(self.ENTRIES_2),
                                 writer=writer, ws=ws)
        self.assertEqual([r[COL_TXN_NO] for r in rows], [6, 7])


class ZeroAmountPlaceholderRowTest(unittest.TestCase):
    """T6-4 / AD-T4-8: `line_mode` では金額 0 の占位 entry も 1 行書く。

    現行の `if not amount or int(amount) == 0: continue` は、`card_entries`
    が「読めなかったが消さない」ために作った占位行（外貨のみ・金額 0・
    分類できない負数）を**無音で落とす**。落ちると Sheets 上に痕跡が無く、
    顧客は明細の枚数を数えるまで欠落に気づけない（IP-401 と同じ事故形）。
    """

    BOOKABLE = _card_entry(630, "2026/05/01", "西鉄バス", "")
    BOOKABLE2 = _card_entry(1200, "2026/05/03", "ファミリーマート", "")

    def test_placeholder_entry_becomes_one_row(self):
        """占位行が MF の 1 行として出力され、頁は POSTED 扱いになること。

        POSTED なのは痕跡が帳簿上に在るから —— 占位行が書けているので
        `_write_unrecognized_row` の兜底へ落とす必要がない。
        """
        result, rows, _, _ = _capture(
            DocType.CREDIT_CARD, _card_payload([_placeholder_entry()]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][COL_AMOUNT], 0)
        self.assertEqual(result, APPEND_RESULT_POSTED)

    def test_placeholder_row_carries_the_reason_to_the_human(self):
        """S列 摘要に「なぜ記帳できないか」が出ること。

        金額 0 の行だけが並んでも、理由が読めなければ人手確認に回せない。
        """
        _, rows, _, _ = _capture(
            DocType.CREDIT_CARD, _card_payload([_placeholder_entry()]))
        self.assertIn(_FX_NOTE, rows[0][COL_DESCRIPTION])

    def test_internal_reason_key_never_reaches_the_sheet(self):
        """`_placeholder_reason` は producer 内部の識別子。列へ漏らさない。

        漏れると顧客の帳簿に `no_jpy_amount` のような英字 ID が出る。
        """
        _, rows, _, _ = _capture(
            DocType.CREDIT_CARD, _card_payload([_placeholder_entry()]))
        self.assertNotIn(card_entries.PLACEHOLDER_NO_JPY,
                         [str(cell) for cell in rows[0]])

    def test_placeholder_rows_do_not_disturb_the_numbering(self):
        """占位行も 1 取引として採番を消費する（間を詰めない）。

        詰めると A列が「明細の何行目か」と対応しなくなり、突合できない。
        """
        payload = _card_payload(
            [self.BOOKABLE, _placeholder_entry(), self.BOOKABLE2])
        _, rows, _, _ = _capture(DocType.CREDIT_CARD, payload)
        self.assertEqual([r[COL_TXN_NO] for r in rows], [1, 2, 3])
        self.assertEqual([r[COL_AMOUNT] for r in rows], [630, 0, 1200])

    def test_every_placeholder_reason_survives(self):
        """`card_entries` が作る 4 種の占位理由すべてが行になること。

        理由ごとに分岐は無いが、片方向テストだと「たまたま 1 種だけ通る」
        実装（reason で選り分ける等）を検出できない。
        """
        reasons = (card_entries.PLACEHOLDER_NO_JPY,
                   card_entries.PLACEHOLDER_NO_AMOUNT,
                   card_entries.PLACEHOLDER_UNCLASSIFIED_NEGATIVE,
                   card_entries.PLACEHOLDER_SIGN_CONFLICT)
        entries = [_placeholder_entry(reason=r, note="理由 %s" % r)
                   for r in reasons]
        _, rows, _, _ = _capture(DocType.CREDIT_CARD, _card_payload(entries))
        self.assertEqual(len(rows), len(reasons))

    def test_transit_ic_placeholder_rows_are_written_too(self):
        """nimoca 側も同じ（ゲートは doc_type ではなく `line_mode`）。"""
        payload = _card_payload([_placeholder_entry()],
                                doc_type=DocType.TRANSIT_IC)
        _, rows, _, _ = _capture(DocType.TRANSIT_IC, payload)
        self.assertEqual(len(rows), 1)

    def test_legacy_doc_type_still_drops_the_zero_amount_row(self):
        """**両方向**の固定: 同じ 0 円 entry が既存 doc_type では消えること。

        `line_mode` 側だけ書くと「ゲートを常時 True にする」変異を
        検出できない。既存 doc_type の 1 文字不変が T6 の第一条件。
        """
        legacy = _legacy_payload([_legacy_entry(630, "A"),
                                  _legacy_entry(0, "B（金額 0）")])
        _, rows, _, _ = _capture(DocType.RECEIPT, legacy)
        self.assertEqual([r[COL_AMOUNT] for r in rows], [630])


class MissingAmountContractTest(unittest.TestCase):
    """A14: `line_mode` でも `amount` 欠損 / `None` の entry は書かない。

    占位行は producer が `amount: 0` を**明示して**作る（`card_entries.py`
    の `_placeholder`）。キーが無い・`None` は占位行ではなく producer の
    契約違反であって、通せば後段の `int(amount)` が `TypeError` で落ちるか、
    不正な payload がそのまま帳簿になる。
    """

    def test_none_amount_entry_is_skipped_in_line_mode(self):
        payload = _card_payload([
            _card_entry(None, "2026/05/01", "壊れた行", ""),
            _card_entry(630, "2026/05/02", "西鉄バス", ""),
        ])
        _, rows, _, _ = _capture(DocType.CREDIT_CARD, payload)
        self.assertEqual([r[COL_AMOUNT] for r in rows], [630])

    def test_missing_amount_key_is_skipped_in_line_mode(self):
        broken = _card_entry(0, "2026/05/01", "壊れた行", "")
        del broken["amount"]
        payload = _card_payload([broken,
                                 _card_entry(630, "2026/05/02", "西鉄バス", "")])
        _, rows, _, _ = _capture(DocType.CREDIT_CARD, payload)
        self.assertEqual([r[COL_AMOUNT] for r in rows], [630])

    def test_all_rows_missing_amount_falls_to_the_unrecognized_row(self):
        """全行が契約違反でも**無音では消えない**（IP-401 の不変式）。

        既存経路の兜底（赤タグの占位行）へ落ちること。
        """
        payload = _card_payload([
            _card_entry(None, "2026/05/01", "壊れた行1", ""),
            _card_entry(None, "2026/05/02", "壊れた行2", ""),
        ])
        result, rows, _, _ = _capture(DocType.CREDIT_CARD, payload)
        self.assertEqual(result, APPEND_RESULT_PLACEHOLDER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][TAG_COL_INDEX], UNRECOGNIZED_TAG)

    def test_zero_amount_is_not_confused_with_missing_amount(self):
        """0 は書く / 欠損は書かない —— 隣り合う 2 つの語義を同時に固定する。"""
        broken = _card_entry(0, "2026/05/01", "契約違反", "")
        del broken["amount"]
        payload = _card_payload([_placeholder_entry(merchant="占位"), broken])
        _, rows, _, _ = _capture(DocType.CREDIT_CARD, payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][COL_DEBIT_VENDOR], "占位")


class LineModeTaxTypeRenderingTest(unittest.TestCase):
    """T6-5 / AD-11 / T4 §10-④: G列の税区分を省略名にする（出力層のみ）。

    MF の列幅では canonical 名（`課対仕入10%`）が読みにくい。builder は
    canonical のまま出し、**Sheets に書く直前だけ**省略名へ落とす。
    canonical を producer 側で潰すと `anomaly_detector.EXEMPT_TAX_TYPE` の
    精確等値判定や検算が壊れる。
    """

    def _row(self, tax_type, doc_type=DocType.CREDIT_CARD):
        entry = _card_entry(630, "2026/05/01", "西鉄バス", "",
                            debit_tax_type=tax_type)
        _, rows, _, _ = _capture(doc_type,
                                 _card_payload([entry], doc_type=doc_type))
        return rows[0]

    def test_standard_rate_is_abbreviated(self):
        self.assertEqual(self._row("課対仕入10%")[COL_DEBIT_TAX_TYPE],
                         "課仕 10%")

    def test_reduced_rate_is_abbreviated(self):
        self.assertEqual(self._row("課対仕入8% (軽)")[COL_DEBIT_TAX_TYPE],
                         "課仕 8%(軽)")

    def test_exempt_tax_type_is_left_alone(self):
        """`対象外` は変換表に**入れない**（Plan §3.7）。

        `anomaly_detector.EXEMPT_TAX_TYPE` が精確等値で判定しているため、
        省略名に落とすと対象外行の構造異常検知が無音で死ぬ。
        """
        self.assertEqual(self._row("対象外")[COL_DEBIT_TAX_TYPE], "対象外")

    def test_unmapped_value_passes_through(self):
        """表に無い値はそのまま（`.get(v, v)`）。握り潰さない。"""
        self.assertEqual(self._row("課対仕入5%")[COL_DEBIT_TAX_TYPE],
                         "課対仕入5%")

    def test_transit_ic_is_abbreviated_too(self):
        self.assertEqual(
            self._row("課対仕入10%", doc_type=DocType.TRANSIT_IC)[
                COL_DEBIT_TAX_TYPE], "課仕 10%")

    def test_legacy_doc_type_keeps_the_canonical_name(self):
        """**両方向**の固定: 既存 doc_type の G列は 1 文字も変わらない。"""
        legacy = _legacy_payload([_legacy_entry(
            630, debit_account="消耗品費", debit_tax_type="課対仕入10%")])
        _, rows, _, _ = _capture(DocType.RECEIPT, legacy)
        self.assertEqual(rows[0][COL_DEBIT_TAX_TYPE], "課対仕入10%")

    def test_the_config_table_is_actually_consulted(self):
        """結線の証明: 変換表を差し替えたら出力が追随すること。

        期待値の直書きだけだと「表を読まず if で書き分ける」実装や、
        表の値と偶然一致しただけの実装を検出できない。
        """
        with patch.dict(config.CC_TAX_TYPE_RENDERING,
                        {"課対仕入10%": "◆哨兵◆"}, clear=False):
            self.assertEqual(self._row("課対仕入10%")[COL_DEBIT_TAX_TYPE],
                             "◆哨兵◆")

    def test_credit_side_tax_type_is_not_rendered(self):
        """O列 貸方税区分は対象外（AD-11 が言うのは借方 G列だけ）。

        両側を変換すると、後で貸方に課税区分を持つ行を足したとき
        気づかないまま省略名になる。
        """
        entry = _card_entry(630, "2026/05/01", "西鉄バス", "",
                            credit_tax_type="課対仕入10%")
        _, rows, _, _ = _capture(DocType.CREDIT_CARD, _card_payload([entry]))
        self.assertEqual(rows[0][14], "課対仕入10%")

    def test_anomaly_detection_sees_the_canonical_name(self):
        """省略名は**表示だけ**。判定は canonical のまま行うこと。

        省略名を detector に渡すと `対象外` 以外の等値判定が全部ずれる。
        """
        seen = []

        def _spy(entry, parent):
            seen.append(entry.get("debit_tax_type"))
            return []

        with patch.object(anomaly_detector, "detect_anomalies",
                          side_effect=_spy):
            self._row("課対仕入10%")
        self.assertEqual(seen, ["課対仕入10%"])


_VALID_T = "T8010401088436"     # チェックディジット正当（実装で検算済み）


def _verdict(deduction_class=DeductionClass.QUALIFIED):
    """`card_entries` が entry に載せる `_deduction` と同じ型を作る。

    `DeductionVerdict` は NamedTuple なので、テスト用の別形を作らず
    実型をそのまま組む。`classify()` を通さないのは、入力契約の変更を
    H列のテストに巻き込まないため（判定木自体は
    `test_invoice_classification` が見ている）。
    """
    return DeductionVerdict(deduction_class=deduction_class, rule_id="R-test",
                            basis_short="", basis_detail="", caveats=())


class InvoiceColumnResolverTest(unittest.TestCase):
    """T6-6 / AD-8: H列は `_resolve_invoice_cell` が決める。

    **結線の証明が難しい**: `INVOICE_COL_RENDERING` は現行全値が空文字
    （事務所慣例）なので、結線しなくても結果が同じになる。よって
    ①変換表を非空へ差し替える ②resolver 自体を mock する の二重で縛る。
    片方だけでは「resolver を経由せず偶然同じ値」または「表からの経路を
    見ていない」を排除できない。

    差し替え先は **`invoice_classification.INVOICE_COL_RENDERING`**。
    `config` 側ではない —— `invoice_classification.py:149` が import 時に
    `dict(...)` でコピーを作る（config 不在でも動くための設計）ので、
    config を patch しても実行時には届かない。config → コピー の一段は
    `test_config_table_is_copied_faithfully` で別に縛る。
    """

    def test_config_table_is_copied_faithfully(self):
        """config → `invoice_classification` のコピーが忠実であること。

        コピーは import 時に 1 回だけ起きる。ここがずれると、config を
        書き換えたのに H列が変わらない（またはその逆）状態になる。
        """
        self.assertEqual(invoice_classification.INVOICE_COL_RENDERING,
                         config.INVOICE_COL_RENDERING)

    def _row(self, entry_extra, payload_extra=None,
             doc_type=DocType.CREDIT_CARD):
        entry = _card_entry(630, "2026/05/01", "西鉄バス", "", **entry_extra)
        payload = _card_payload([entry], doc_type=doc_type)
        payload.update(payload_extra or {})
        _, rows, _, _ = _capture(doc_type, payload)
        return rows[0]

    # ── 純関数そのもの ──

    def test_resolver_returns_empty_without_a_verdict(self):
        """占位行・ポイント充当行は判定を通っていない → 空文字。"""
        self.assertEqual(sheets_output._resolve_invoice_cell({}), "")
        self.assertEqual(
            sheets_output._resolve_invoice_cell({"_deduction": None}), "")

    def test_resolver_delegates_to_invoice_classification(self):
        """判定木は 1 箇所（`invoice_classification`）。ここで再判定しない。"""
        with patch.object(invoice_classification, "render_invoice_column",
                          return_value="◆哨兵◆") as spy:
            cell = sheets_output._resolve_invoice_cell(
                {"_deduction": _verdict()})
        self.assertEqual(cell, "◆哨兵◆")
        spy.assert_called_once()

    # ── 層 1: config → H列 の経路 ──

    def test_config_rendering_table_reaches_the_column(self):
        with patch.dict(invoice_classification.INVOICE_COL_RENDERING,
                        {DeductionClass.QUALIFIED: "適格"}, clear=False):
            row = self._row({"_deduction": _verdict()})
        self.assertEqual(row[COL_DEBIT_INVOICE], "適格")

    def test_each_deduction_class_uses_its_own_entry(self):
        """クラスごとに表を引くこと（1 個だけ結線して残りが素通り、を排除）。"""
        table = {DeductionClass.QUALIFIED: "◆適格◆",
                 DeductionClass.NOT_DEDUCTIBLE: "◆不可◆",
                 DeductionClass.OUT_OF_SCOPE: "◆対象外◆"}
        with patch.dict(invoice_classification.INVOICE_COL_RENDERING, table, clear=False):
            for cls, expected in table.items():
                with self.subTest(deduction_class=cls):
                    row = self._row({"_deduction": _verdict(cls)})
                    self.assertEqual(row[COL_DEBIT_INVOICE], expected)

    # ── 層 2: H列が resolver の戻り値であること ──

    def test_column_comes_from_the_resolver_not_the_sanitizer(self):
        """mock した resolver の値が H列に出ること。

        `_sanitize_invoice_num(entry["debit_invoice"])` が残っていると
        哨兵ではなく元の値（空 or T番号）が出る。
        """
        with patch.object(sheets_output, "_resolve_invoice_cell",
                          return_value="◆哨兵◆"):
            row = self._row({})
        self.assertEqual(row[COL_DEBIT_INVOICE], "◆哨兵◆")

    # ── 現行運用（全値空文字）と doc 級の非継承 ──

    def test_current_policy_renders_empty(self):
        """現行の変換表は全値空文字（AD-8 の慣例踏襲）。"""
        self.assertEqual(self._row({"_deduction": _verdict()})[
            COL_DEBIT_INVOICE], "")

    def test_card_companys_t_number_is_not_copied_to_every_row(self):
        """doc 級 T番号（カード会社のもの）を全行へ配らない（AD-8 / AD-T4-5）。

        配ると 300 行すべてが「加盟店の登録番号」を騙る。
        """
        row = self._row({"_deduction": _verdict()},
                        payload_extra={"invoice_num": _VALID_T})
        self.assertEqual(row[COL_DEBIT_INVOICE], "")

    def test_entry_level_invoice_string_is_ignored_in_line_mode(self):
        """`line_mode` では H列は判定結果のみ。生の文字列は使わない。"""
        row = self._row({"debit_invoice": _VALID_T, "_deduction": _verdict()})
        self.assertEqual(row[COL_DEBIT_INVOICE], "")

    # ── 両方向: 既存 doc_type は resolver を通らない ──

    LEGACY_PAYLOAD = _legacy_payload(
        [_legacy_entry(630, debit_account="消耗品費")], invoice_num=_VALID_T)

    def test_legacy_doc_type_still_inherits_the_document_t_number(self):
        _, rows, _, _ = _capture(DocType.RECEIPT, self.LEGACY_PAYLOAD)
        self.assertEqual(rows[0][COL_DEBIT_INVOICE], _VALID_T)

    def test_legacy_doc_type_never_calls_the_resolver(self):
        with patch.object(sheets_output, "_resolve_invoice_cell",
                          return_value="◆哨兵◆") as spy:
            _, rows, _, _ = _capture(DocType.RECEIPT, self.LEGACY_PAYLOAD)
        spy.assert_not_called()
        self.assertEqual(rows[0][COL_DEBIT_INVOICE], _VALID_T)


class CreditSubAccountTest(unittest.TestCase):
    """T6-7 / AD-11 / T4 §10-①: L列 貸方補助科目にカード名を出す。

    `_determine_credit_sub_account` は T6-7 まで**死コード**だった（呼出元が
    存在せず L列は恒常空欄）。本テストが縛るのは「`line_mode` のときだけ
    呼ぶ」こと —— 既存 doc_type でも呼ぶと receipt の L列が
    `""` から社長名へ、purchase_invoice が取引先名へ変わり、顧客の既存帳簿の
    見た目が黙って変わる。それは T6 の依頼ではない。
    """

    CARD_NAME = "アメックス・ビジネス"

    def test_line_mode_writes_the_card_name(self):
        _, rows, _, _ = _capture(
            DocType.CREDIT_CARD,
            _card_payload([_card_entry(630, "2026/05/01", "西鉄バス", "")]))
        self.assertEqual(rows[0][COL_CREDIT_SUB], self.CARD_NAME)

    def test_line_mode_without_the_key_leaves_it_empty(self):
        """キーが無ければ「その他」分岐の既定（空文字）。捏造しない。"""
        entry = _card_entry(630, "2026/05/01", "西鉄バス", "")
        del entry["credit_sub_account"]
        _, rows, _, _ = _capture(DocType.CREDIT_CARD, _card_payload([entry]))
        self.assertEqual(rows[0][COL_CREDIT_SUB], "")

    def test_legacy_doc_type_ignores_the_key_entirely(self):
        """**両方向**の固定: 既存 doc_type は値が在っても L列を空のまま。

        「キーが在れば書く」実装だと既存 doc_type を巻き込む。ゲートが
        `line_mode` であることをここで縛る。
        """
        legacy = _legacy_payload([_legacy_entry(
            630, debit_account="消耗品費",
            credit_sub_account="漏れたら出る値")])
        _, rows, _, _ = _capture(DocType.RECEIPT, legacy)
        self.assertEqual(rows[0][COL_CREDIT_SUB], "")

    def test_mixed_folder_keeps_the_folder_tab_but_uses_the_entry_value(self):
        """A12: 混載 nimoca（folder=クレカ / actual=nimoca）の二重契約。

        タブは **folder** doc_type のまま「_カード明細」（AD-T3-1。T4 §10-⑤
        がここを変える改修を明示的に禁じている）。L列は **entry** 由来の
        nimoca 側の値。片方だけ縛ると、もう片方が黙って逆に倒れる。
        """
        entry = _card_entry(630, "2026/05/01", "", "",
                            credit_sub_account="nimoca")
        payload = _card_payload([entry], doc_type=DocType.TRANSIT_IC)
        _, rows, writer, _ = _capture(DocType.CREDIT_CARD, payload)
        self.assertEqual(rows[0][COL_CREDIT_SUB], "nimoca")
        # 回数ではなく**名前が 1 種類であること**を見る（`_get_or_create_tab`
        # は書込復旧の経路からも呼ばれるので呼出回数は契約ではない）。
        self.assertEqual(set(writer.captured_tabs), {"従業員_カード明細"})

    def test_existing_branches_of_the_helper_are_untouched(self):
        """既存 2 分岐（AD-11 が「1 文字も変えない」と書いた側）の直接確認。

        `line_mode` から呼ぶようにしただけで分岐そのものは触っていないこと。
        """
        helper = SheetsOutputWriter._determine_credit_sub_account
        self.assertEqual(
            helper(DocType.RECEIPT, {}, "取引先"),
            config.CREDIT_SUB_ACCOUNT_RECEIPT)
        self.assertEqual(
            helper(DocType.PURCHASE_INVOICE, {}, "取引先"), "取引先")
        self.assertEqual(helper(DocType.CREDIT_CARD, {}, "取引先"), "")


class CreditOnlyAccountExemptionTest(unittest.TestCase):
    """T6-8 / AD-5 / T4 §10-②: `line_mode` の借方「未払金」を潰さない。

    `未払金` は `CREDIT_ONLY_ACCOUNTS` に載っている（貸方専用科目が借方へ
    出たら Gemini の取り違えとみなす兜底）。ところがポイント充当行は
    **借方 未払金 / 貸方 雑収入** が正しい仕訳 —— カード未払金を直接減らす。
    兜底が働くとこの行が丸ごと `未確定勘定` へ倒れ、負債が減らない。

    豁免は `DOC_TYPE_CONFIG[doc_type]["default_credit"]` と一致する **1 語だけ**。
    `現金`・`普通預金` 等の他の 8 語は `line_mode` でも従来どおり置換する。
    """

    def _debit(self, account, doc_type=DocType.CREDIT_CARD,
               actual_doc_type=None):
        entry = _card_entry(-500, "2026/05/01", "ポイント充当", "",
                            debit_account=account, credit_account="雑収入")
        payload = _card_payload([entry],
                                doc_type=actual_doc_type or doc_type)
        _, rows, _, _ = _capture(doc_type, payload)
        return rows[0][COL_DEBIT_ACCOUNT]

    def test_unpaid_survives_on_the_debit_side(self):
        self.assertEqual(self._debit("未払金"), "未払金")

    def test_other_credit_only_accounts_are_still_replaced(self):
        """豁免は 1 語だけ。兜底そのものを無効化したのではないこと。"""
        for account in ("現金", "普通預金", "買掛金", "預り金"):
            with self.subTest(account=account):
                self.assertEqual(self._debit(account),
                                 config.UNKNOWN_ACCOUNT)

    def test_transit_ic_is_exempt_too(self):
        self.assertEqual(self._debit("未払金", doc_type=DocType.TRANSIT_IC),
                         "未払金")

    def test_mixed_folder_is_exempt(self):
        """混載 nimoca（folder=クレカ / actual=nimoca）。両者とも `未払金`。"""
        self.assertEqual(
            self._debit("未払金", doc_type=DocType.CREDIT_CARD,
                        actual_doc_type=DocType.TRANSIT_IC),
            "未払金")

    def test_legacy_doc_type_still_replaces_unpaid(self):
        """**両方向**の固定: 既存 doc_type の兜底は 1 文字も変わらない。"""
        legacy = _legacy_payload([_legacy_entry(
            630, debit_account="未払金")])
        _, rows, _, _ = _capture(DocType.RECEIPT, legacy)
        self.assertEqual(rows[0][COL_DEBIT_ACCOUNT], config.UNKNOWN_ACCOUNT)

    def test_exemption_is_read_from_the_doc_type_config(self):
        """豁免語は `DOC_TYPE_CONFIG` 由来（`未払金` の直書きではない）。

        既定値を `現金` へ差し替えると、豁免対象が入れ替わること。
        直書き実装だと `未払金` が生き残ったままになり検出できる。
        """
        swapped = {**DOC_TYPE_CONFIG[DocType.CREDIT_CARD],
                   "default_credit": "現金"}
        with patch.dict(DOC_TYPE_CONFIG,
                        {DocType.CREDIT_CARD: swapped}, clear=False):
            self.assertEqual(self._debit("現金"), "現金")
            self.assertEqual(self._debit("未払金"), config.UNKNOWN_ACCOUNT)


@contextmanager
def _spying_on_anomalies():
    """`detect_anomalies` を**実物のまま**呼びつつ引数と戻り値を積む。

    mock で戻り値を差し替えると「行級 parent を渡している」ことしか測れず、
    抑制表が実際に効いているかが見えない。実物を通す。

    `_anomaly_calls` は「flags だけ見たい」用の薄い包み。書かれた行も同時に
    見たいテストは本 contextmanager を直接使い、中で `_capture` を呼ぶ
    —— spy を各テストで手写しすると、`detect_anomalies` の署名や
    `_capture` の戻り値が変わったとき直す箇所が N 個になる。
    """
    calls = []
    real = anomaly_detector.detect_anomalies

    def _spy(entry, parent):
        flags = real(entry, parent)
        calls.append((dict(parent), {f["type"] for f in flags}))
        return flags

    with patch.object(anomaly_detector, "detect_anomalies", side_effect=_spy):
        yield calls


def _anomaly_calls(doc_type, payload):
    """`_capture` を流し `[(parent, flag 型集合), ...]` を返す。"""
    with _spying_on_anomalies() as calls:
        _capture(doc_type, payload)
    return calls


class RowLevelAnomalyTest(unittest.TestCase):
    """T6-9 / AD-6 §3.8: `line_mode` の異常検知は**行の値**で判定する。

    カード明細の raw_data には doc 級 date / vendor / invoice_num が存在しない
    （`card_prompts.py` の card 級は member_no / statement_date / card_name のみ）。
    doc 級 parent のまま検知器へ渡すと**全行に赤＋橙＋黄**が付く。300 行の
    ETC 明細なら 900 個のタグが立ち、AD-7 が守ろうとした「色の信号価値」が消える。
    """

    def test_parent_carries_the_row_date_and_vendor(self):
        payload = _card_payload([
            _card_entry(630, "2026/05/01", "西鉄バス", ""),
            _card_entry(1200, "2026/05/03", "ファミリーマート", ""),
        ])
        calls = _anomaly_calls(DocType.CREDIT_CARD, payload)
        self.assertEqual([(p["date"], p["vendor"]) for p, _ in calls],
                         [("2026/05/01", "西鉄バス"),
                          ("2026/05/03", "ファミリーマート")])

    def test_missing_date_is_judged_per_row(self):
        """日付が読めた行は咎めず、読めなかった行だけ赤にする。"""
        payload = _card_payload([
            _card_entry(630, "2026/05/01", "西鉄バス", ""),
            _card_entry(1200, "", "ファミリーマート", ""),
        ])
        calls = _anomaly_calls(DocType.CREDIT_CARD, payload)
        self.assertEqual([("missing_date" in t) for _, t in calls],
                         [False, True])

    def test_credit_card_flags_an_empty_merchant(self):
        payload = _card_payload([_card_entry(630, "2026/05/01", "", "")])
        _, types = _anomaly_calls(DocType.CREDIT_CARD, payload)[0]
        self.assertIn("missing_vendor", types)

    def test_transit_ic_does_not_flag_an_empty_merchant(self):
        """**両方向**の固定（趙裁定 2026-08-18・選択肢1）。"""
        payload = _card_payload([_card_entry(630, "2026/05/01", "", "")],
                                doc_type=DocType.TRANSIT_IC)
        _, types = _anomaly_calls(DocType.TRANSIT_IC, payload)[0]
        self.assertNotIn("missing_vendor", types)

    def test_mixed_folder_suppression_follows_the_actual_doc_type(self):
        """§3.2 の核心: 判定キーは `entries_data["doc_type"]`（actual）。

        `append_entries` の引数は **folder** doc_type。混載フォルダでは
        nimoca の頁が `doc_type=credit_card` として到達するので、引数で
        判定すると抑制が漏れて nimoca 全行が橙になる。
        """
        payload = _card_payload([_card_entry(630, "2026/05/01", "", "")],
                                doc_type=DocType.TRANSIT_IC)
        _, types = _anomaly_calls(DocType.CREDIT_CARD, payload)[0]
        self.assertNotIn("missing_vendor", types)

    def test_invoice_flags_are_suppressed_for_card_doc_types(self):
        for doc_type in (DocType.CREDIT_CARD, DocType.TRANSIT_IC):
            with self.subTest(doc_type=doc_type):
                payload = _card_payload(
                    [_card_entry(630, "2026/05/01", "西鉄バス", "")],
                    doc_type=doc_type)
                _, types = _anomaly_calls(doc_type, payload)[0]
                self.assertNotIn("missing_invoice", types)
                self.assertNotIn("invalid_t_number", types)

    def test_a_clean_card_row_gets_no_tag_at_all(self):
        """結果の確認: 正常な明細行は U列が空（色の信号価値が残ること）。"""
        _, rows, _, _ = _capture(
            DocType.CREDIT_CARD,
            _card_payload([_card_entry(630, "2026/05/01", "西鉄バス", "")]))
        self.assertEqual(rows[0][COL_TAG], "")

    # ── 両方向: 既存 doc_type は doc 級 parent のまま ──

    def test_legacy_parent_stays_document_level(self):
        """entry 側に別の値が在っても doc 級を渡すこと（既存挙動）。"""
        # doc 級の 2 値は**両方とも明示する**。片方をヘルパの既定値に任せると、
        # 既定値が別の理由で変わったとき「なぜこの期待値なのか」が失われ、
        # 落ちたテストを literal の書き換えで «直して» しまう。
        legacy = _legacy_payload(
            [_legacy_entry(630, debit_account="消耗品費",
                           date="2026/08/09", debit_vendor="行の店")],
            date="2026/08/01", vendor="doc級の店")
        calls = _anomaly_calls(DocType.RECEIPT, legacy)
        self.assertEqual((calls[0][0]["date"], calls[0][0]["vendor"]),
                         ("2026/08/01", "doc級の店"))



class OcrOverrideExemptionReachesTheSheetTest(unittest.TestCase):
    """T7: 豁免後の `date=None` が B列 空欄 ＋ `missing_date` の赤になること。

    Plan: `docs/plans/2026-08-18-t7-ocr-override-exemption.md` §5 T7-4。

    **後半が本質**。T7 の価値は「空欄になる」ことではなく「**人手確認へ回る**」
    ことである。豁免前は `_apply_ocr_overrides` が doc 級 date に
    「頁面で最後に読まれた日付」を書き込み、行の日付が空の entry はそこへ回帰する。
    すると `anomaly_detector` の `missing_date` は `date_val` が truthy なので
    **立たない** —— 誤った日付が赤タグ無しで帳簿に載る。豁免後は空欄になり、
    赤（severity=high, col=1）が立って人が見る。

    B列 `""` だけを断言すると、この「無音の誤り → 可視の空欄」という
    転換そのものを測っていないことになる。
    """

    @staticmethod
    def _rows_and_flags(entries_data):
        """`append_entries` を流し `(書かれた行, [flag 型集合, ...])` を返す。

        spy は `_spying_on_anomalies`（本ファイル共用）。手写しすると
        `detect_anomalies` の署名が変わったとき直す箇所が増える。
        """
        with _spying_on_anomalies() as calls:
            _, written, _, _ = _capture(DocType.CREDIT_CARD, entries_data)
        return written, [types for _, types in calls]

    def test_a_row_without_a_date_stays_empty_and_raises_missing_date(self):
        # 豁免後の形: doc 級 date は None（キーが生えない）、行の日付も空
        # （nimoca の月日が読めなかった行）。
        payload = _card_payload([_card_entry(1200, "", "西鉄バス", "")])

        written, flag_types = self._rows_and_flags(payload)

        self.assertEqual(written[0][COL_DATE], "")
        self.assertIn("missing_date", flag_types[0])
        self.assertEqual(written[0][COL_TAG], UNRECOGNIZED_TAG)   # 赤系

    def test_a_polluted_doc_level_date_would_silently_fill_the_row(self):
        """対照: 豁免しないとどうなるか（T7 が防いでいるものの形）。

        `_apply_ocr_overrides` が doc 級へ書き込む値を手で置いた payload。
        B列 が埋まり、`missing_date` が**立たない** —— 帳簿には
        「頁面で最後に読まれた日付」が赤タグ無しで載る。
        """
        payload = {**_card_payload([_card_entry(1200, "", "西鉄バス", "")]),
                   "date": "2026/03/31"}

        written, flag_types = self._rows_and_flags(payload)

        self.assertEqual(written[0][COL_DATE], "2026/03/31")
        self.assertNotIn("missing_date", flag_types[0])


if __name__ == "__main__":
    unittest.main()
