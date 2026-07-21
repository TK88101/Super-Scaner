"""golden_replay ハーネス自身の回帰ガード（Plan §3 T1、R6 緩和策）。

ハーネスに誤差があると DIFF-A/B は自洽的に緑になる。本テストはハーネスの
決定性・時刻凍結・高亮捕獲・wrapper 語義を釘で留め、その自洽緑を防ぐ。
"""
import json
import unittest
from datetime import datetime, timedelta

from gspread_formatting import CellFormat, Color

import golden_replay as gr
from doc_types import DocType


JST = gr.JST

# 「凍結を掛けない＝時計が動く」側を表すための別名（実体は freeze_time と同じ）。
# 凍結機構の対検証（T1-2）でのみ使う。
_at_clock = gr.freeze_time


def _entry(amount, debit="消耗品費", tax="課税仕入 10%"):
    return {
        "amount": amount,
        "debit_account": debit,
        "debit_tax_type": tax,
        "credit_account": "現金",
        "description": "テスト品",
    }


def _normal_page(page_num=1, total=3):
    """正常票（異常なし）。"""
    return {
        "page_num": page_num,
        "total_pages": total,
        "result": {
            "date": "2026/01/15",
            "vendor": "テスト商店",
            "invoice_num": "T1234567890123",
            "memo": "",
            "doc_category": "receipt",
            "total_amount": 1100,
            "entries": [_entry(1100)],
        },
    }


def _error_page(page_num=2, total=3):
    return {
        "page_num": page_num,
        "total_pages": total,
        "result": {"_page_error": True, "entries": []},
    }


def _four_outlet_page(page_num=1, total=1):
    """4 出口（白リセット／低置信全行黄／per-entry／doc 赤）を同時に踏む票。"""
    return {
        "page_num": page_num,
        "total_pages": total,
        "result": {
            "date": "2026/01/15",
            "vendor": "テスト商店",
            "invoice_num": "",          # T番号空 → per-entry 黄
            "memo": "",
            "doc_category": "receipt",
            "ocr_confidence": 0.30,     # < 0.65 → 低置信・全行黄
            "total_amount": 9999,       # 行合計 1100 と不一致 → doc 赤（I列）
            "entries": [_entry(1100)],
        },
    }


class NormalizeDeterminismTest(unittest.TestCase):
    """T1-1: 同一入力に対し normalize がバイト等価な JSON を返す。"""

    def test_replay_ui_is_byte_identical_across_runs(self):
        fixture = [_normal_page(1, 1)]
        first = gr.replay_ui(fixture, DocType.RECEIPT, source="fx")
        second = gr.replay_ui(fixture, DocType.RECEIPT, source="fx")
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )


class FreezeTimeTest(unittest.TestCase):
    """T1-2: 時刻凍結機構そのものの回帰ガード（凍結あり／なしの対検証）。"""

    @staticmethod
    def _raw_timestamps(fixture, freeze):
        writer = gr.make_offline_writer()
        if freeze:
            with gr.freeze_time(), gr.capture_formats():
                gr.drive_ui(writer, fixture, DocType.RECEIPT)
        else:
            with gr.capture_formats():
                gr.drive_ui(writer, fixture, DocType.RECEIPT)
        ws = writer._fake_tabs[gr.tab_name_for(DocType.RECEIPT)]
        return [r[23] for r in ws.appended_rows]

    def test_without_freeze_timestamp_follows_the_clock(self):
        fixture = [_normal_page(1, 1)]
        base = datetime(2026, 3, 3, 10, 0, tzinfo=JST)
        with _at_clock(base):
            first = self._raw_timestamps(fixture, freeze=False)
        with _at_clock(base + timedelta(hours=5)):
            second = self._raw_timestamps(fixture, freeze=False)
        self.assertNotEqual(first, second)

    def test_with_freeze_timestamp_is_constant(self):
        fixture = [_normal_page(1, 1)]
        base = datetime(2026, 3, 3, 10, 0, tzinfo=JST)
        with _at_clock(base):
            first = self._raw_timestamps(fixture, freeze=True)
        with _at_clock(base + timedelta(hours=5)):
            second = self._raw_timestamps(fixture, freeze=True)
        self.assertEqual(first, second)
        self.assertEqual(first, [gr.FROZEN_TS])

    def test_normalize_masks_timestamp_columns(self):
        out = gr.replay_ui([_normal_page(1, 1)], DocType.RECEIPT, source="fx")
        rows = out["tabs"][gr.tab_name_for(DocType.RECEIPT)]["rows"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row[23], gr.FROZEN_MASK)
            self.assertEqual(row[25], gr.FROZEN_MASK)


class HighlightCaptureTest(unittest.TestCase):
    """T1-3 / T1-4: 4 出口の捕獲と cell ref の canonicalize。"""

    def test_captures_all_four_outlets(self):
        out = gr.replay_ui([_four_outlet_page()], DocType.RECEIPT, source="fx")
        tab = gr.tab_name_for(DocType.RECEIPT)
        rows = out["tabs"][tab]["rows"]
        start = 6                      # legend 4 + header 1 の直後
        end = start + len(rows) - 1

        # (a) 白リセットは reset_ranges へ分離
        self.assertIn(f"A{start}:AB{end}", out["reset_ranges"])

        highlights = out["tabs"][tab]["highlights"]
        # (b) 低置信・全行黄
        self.assertIn({"cell": f"A{start}:AB{end}", "severity": "low"}, highlights)
        # (d) doc 赤・I列
        self.assertIn({"cell": f"I{start}:I{end}", "severity": "high"}, highlights)
        # (c) per-entry（上記 2 件以外が最低 1 件）
        others = [h for h in highlights
                  if h["cell"] not in (f"A{start}:AB{end}", f"I{start}:I{end}")]
        self.assertTrue(others, f"per-entry 高亮が捕獲されていない: {highlights}")

    # 生産の白リセットは 3 箇所（append_entries / commit_page /
    # _write_unrecognized_row）。前 2 つは通常票で、3 つ目は部分失敗の占位行でしか
    # 踏まないため、fixture を 2 種類流す必要がある（1 種類だと変異が生き残る）。
    ORDER_FIXTURES = {
        "通常票": [_four_outlet_page()],
        "部分失敗の占位行": [_normal_page(1, 2), _error_page(2, 2)],
    }

    def test_white_reset_precedes_highlights_on_both_paths(self):
        """不変条件: 真の書込経路で白リセットが高亮を消してはならない。

        生産の書込尾部は「append → 白リセット → 高亮」の順に依存する。この順序が
        壊れると顧客シート上で赤が消えるが、旧口径（highlights / reset_ranges）は
        集合へ畳むため byte 一致で緑になる（順序盲）。
        合成 records ではなく**実際の書込経路**を駆動して恒空を釘で留める。
        """
        for label, fixture in self.ORDER_FIXTURES.items():
            for path, replay in (("ui", gr.replay_ui), ("page", gr.replay_page)):
                out = replay(fixture, DocType.RECEIPT, source="fx")
                self.assertTrue(out["tabs"], f"{label}/{path}: tab がゼロ")
                for tab, block in out["tabs"].items():
                    self.assertEqual(
                        block["white_erased"], [],
                        f"{label}/{path}/{tab}: 白リセットが高亮を消している")
                    self.assertTrue(
                        block["final_highlights"],
                        f"{label}/{path}/{tab}: 最終態が空——判定が空振りしている")

    def test_severity_priority_survives_on_the_real_path(self):
        """塗り順「黄(全行) → per-entry → 赤(I列)」の優先順位を最終態で釘付ける。

        非空だけを見ると、全行黄を赤の後に塗る改変（＝票面合計不一致の赤が黄へ
        降格し、会計士が警告レベルを読み違える）を取り逃がす。
        """
        for path, replay in (("ui", gr.replay_ui), ("page", gr.replay_page)):
            out = replay([_four_outlet_page()], DocType.RECEIPT, source="fx")
            block = out["tabs"][gr.tab_name_for(DocType.RECEIPT)]
            final = {h["cell"]: h["severity"] for h in block["final_highlights"]}
            # I 列＝票面合計≠行合計 の赤。全行黄に上書きされてはならない。
            self.assertEqual(final.get("I6"), "high",
                             f"{path}: I 列の赤が降格している: {final.get('I6')}")
            # 全行黄の下地は赤以外の列に残る。
            self.assertEqual(final.get("A6"), "low", f"{path}: 全行黄が消えた")

    def test_canonical_ref_unifies_single_cell_forms(self):
        self.assertEqual(gr.canonical_ref("I7"), gr.canonical_ref("I7:I7"))
        self.assertEqual(gr.canonical_ref("I7"), "I7:I7")
        self.assertEqual(gr.canonical_ref("A7:AB7"), "A7:AB7")


class WrapperSemanticsTest(unittest.TestCase):
    """T1-5: _page_error の skip と部分失敗時の集約占位行（F10）。"""

    def test_page_error_rows_are_not_written(self):
        fixture = [_normal_page(1, 2), _error_page(2, 2)]
        out = gr.replay_ui(fixture, DocType.RECEIPT, source="fx")
        rows = out["tabs"][gr.tab_name_for(DocType.RECEIPT)]["rows"]
        # 正常票 1 行 ＋ 集約占位行 1 行
        self.assertEqual(len(rows), 2)
        self.assertIn("ページ処理エラー", rows[-1][18])
        self.assertIn("p2", rows[-1][18])

    def test_all_pages_failed_writes_nothing(self):
        fixture = [_error_page(1, 2), _error_page(2, 2)]
        out = gr.replay_ui(fixture, DocType.RECEIPT, source="fx")
        self.assertEqual(out["tabs"], {})

    def test_no_placeholder_when_all_pages_succeed(self):
        fixture = [_normal_page(1, 2), _normal_page(2, 2)]
        out = gr.replay_ui(fixture, DocType.RECEIPT, source="fx")
        rows = out["tabs"][gr.tab_name_for(DocType.RECEIPT)]["rows"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertNotIn("ページ処理エラー", row[18])


class PageAtomicityTest(unittest.TestCase):
    """T1-6 / T1-7: 頁原子書込と基線互換の担保。"""

    def test_replay_page_uses_one_append_per_page(self):
        writer = gr.make_offline_writer()
        fixture = [_normal_page(1, 2), _normal_page(2, 2)]
        with gr.freeze_time(), gr.capture_formats():
            gr.drive_page(writer, fixture, DocType.RECEIPT)
        ws = writer._fake_tabs[gr.tab_name_for(DocType.RECEIPT)]
        self.assertEqual(ws.append_rows_calls, 2)

    def test_same_page_multi_ticket_commits_as_one_page(self):
        """同頁複数票は 1 頁へ束ねる（main._process_file_headless の頁緩衝と同口径）。

        束ねないと append 原子性・reset/高亮の範囲が票ごとになり、DIFF-B が
        同頁複数票の fixture で偽陽性／偽陰性を出す。
        """
        writer = gr.make_offline_writer()
        fixture = [_normal_page(1, 2), _normal_page(1, 2), _normal_page(2, 2)]
        with gr.freeze_time(), gr.capture_formats():
            gr.drive_page(writer, fixture, DocType.RECEIPT)
        ws = writer._fake_tabs[gr.tab_name_for(DocType.RECEIPT)]
        self.assertEqual(ws.append_rows_calls, 2)   # 3 票だが 2 頁
        self.assertEqual(len(ws.appended_rows), 3)

    def test_non_contiguous_page_reappearance_escalates_on_page_path(self):
        """頁番号 1,2,1 は headless では ESCALATE され、以降が書かれない。

        束ね直して全部書いてしまうと、実際には書込・歸檔が起きない入力に対して
        DIFF-B が「等価」と報告してしまう。

        頁 1,2 は再登場の検出**前**に flush 済みなので生産でも書かれている
        （main.py は頁境界で flush してから再登場を判定する）。書かれないのは
        再登場した頁と占位行だけ。
        """
        fixture = [_normal_page(1, 2), _normal_page(2, 2), _normal_page(1, 2)]
        out = gr.replay_page(fixture, DocType.RECEIPT, source="fx")
        rows = out["tabs"][gr.tab_name_for(DocType.RECEIPT)]["rows"]
        self.assertEqual(len(rows), 2)   # 頁 1・頁 2 のみ、再登場頁は書かない
        self.assertTrue(any("ESCALATE" in w for w in out["warnings"]),
                        f"ESCALATE の注記が無い: {out['warnings']}")

    def test_ui_path_is_not_truncated_by_page_reappearance(self):
        """UI/local_test に頁番号連続性の contract は無い＝全票を書く。

        頁級の ESCALATE 打切りが UI 側にも漏れると、DIFF-A が同じ入力で
        両側とも欠落し「一致」に見えてしまう。
        """
        fixture = [_normal_page(1, 2), _normal_page(2, 2), _normal_page(1, 2)]
        out = gr.replay_ui(fixture, DocType.RECEIPT, source="fx")
        rows = out["tabs"][gr.tab_name_for(DocType.RECEIPT)]["rows"]
        self.assertEqual(len(rows), 3)
        self.assertFalse([w for w in out["warnings"] if "ESCALATE" in w])

    def test_placeholder_leaves_author_columns_empty(self):
        """占位行は作成者/最終更新者を書かない——生産の忠実な再現。

        sheets_output._build_unrecognized_block は row[0]/[1]/[5]/[18]/[23]/
        [25]/[27]/[TAG] しか埋めず、24/26（作成者・最終更新者）は空のまま。
        entries_data["uploader"] は占位行では読まれない。ここを「埋まるべき」
        と直すとハーネスが生産から乖離するので、空であることを釘で留める。
        """
        fixture = [_normal_page(1, 2), _error_page(2, 2)]
        out = gr.replay_ui(fixture, DocType.RECEIPT, source="fx",
                           employee_name="田中")
        rows = out["tabs"][gr.tab_name_for(DocType.RECEIPT, "田中")]["rows"]
        self.assertIn("ページ処理エラー", rows[-1][18])
        self.assertEqual(rows[-1][24], "")   # 作成者
        self.assertEqual(rows[-1][26], "")   # 最終更新者
        # 通常票の側は uploader が入る（対比）
        self.assertEqual(rows[0][24], "田中")

    def test_contiguous_repeat_is_not_an_escalation(self):
        """連続する同頁複数票は正常（束ねるだけ）。"""
        fixture = [_normal_page(1, 2), _normal_page(1, 2), _normal_page(2, 2)]
        out = gr.replay_page(fixture, DocType.RECEIPT, source="fx")
        self.assertEqual(len(out["tabs"][gr.tab_name_for(DocType.RECEIPT)]["rows"]), 3)
        self.assertFalse([w for w in out["warnings"] if "ESCALATE" in w])

    def test_placeholder_is_written_outside_the_page_ledger(self):
        """集約占位行は頁級経路でも append_entries 側で書く（main.py:498-510）。

        build_page_write に混ぜると生産の headless（占位行は台賬外）と挙動が
        ずれ、DIFF-B が実在しない差を検出する／実在する差を隠す。
        """
        writer = gr.make_offline_writer()
        fixture = [_normal_page(1, 2), _error_page(2, 2)]
        seen = []
        original = gr.SheetsOutputWriter.build_page_write

        def spy(self, employee, doc_type, results, urls, start_txn):
            seen.append(len(results))
            return original(self, employee, doc_type, results, urls, start_txn)

        gr.SheetsOutputWriter.build_page_write = spy
        try:
            with gr.freeze_time(), gr.capture_formats():
                gr.drive_page(writer, fixture, DocType.RECEIPT)
        finally:
            gr.SheetsOutputWriter.build_page_write = original

        self.assertEqual(seen, [1])   # 正常頁 1 回のみ。占位行は通っていない
        ws = writer._fake_tabs[gr.tab_name_for(DocType.RECEIPT)]
        self.assertIn("ページ処理エラー", ws.appended_rows[-1][18])

    def test_ui_and_page_agree_on_same_page_multi_ticket(self):
        """同頁複数票でも両経路の rows／高亮が一致する（DIFF-B の前提）。"""
        fixture = [_normal_page(1, 2), _normal_page(1, 2), _normal_page(2, 2)]
        ui = gr.replay_ui(fixture, DocType.RECEIPT, source="fx")
        page = gr.replay_page(fixture, DocType.RECEIPT, source="fx")
        tab = gr.tab_name_for(DocType.RECEIPT)
        self.assertEqual(ui["tabs"][tab]["rows"], page["tabs"][tab]["rows"])
        self.assertEqual(ui["tabs"][tab]["highlights"],
                         page["tabs"][tab]["highlights"])

    def test_replay_ui_never_touches_build_page_write(self):
        calls = []
        original = gr.SheetsOutputWriter.build_page_write

        def spy(self, *args, **kwargs):
            calls.append(args)
            return original(self, *args, **kwargs)

        gr.SheetsOutputWriter.build_page_write = spy
        try:
            gr.replay_ui([_normal_page(1, 1)], DocType.RECEIPT, source="fx")
        finally:
            gr.SheetsOutputWriter.build_page_write = original
        self.assertEqual(calls, [])


class OriginReportTest(unittest.TestCase):
    """T1-8: 出自証明が基線条件（build_page_write 不在）を検出できる。"""

    def test_detects_head_revision(self):
        report = gr.origin_report()
        self.assertTrue(report["has_build_page_write"])
        self.assertIn("sheets_output", report["modules"])
        self.assertTrue(report["head"])

    def test_detects_baseline_without_build_page_write(self):
        class BaselineWriter:
            pass

        report = gr.origin_report(writer_cls=BaselineWriter)
        self.assertFalse(report["has_build_page_write"])


class BaselineCompatTest(unittest.TestCase):
    """T4/DIFF-A は基線 0d304f0 上で本ハーネスを import できることが前提。

    module 級で HEAD 限定シンボルを import すると path="ui" しか使わない DIFF-A
    まで ImportError で落ちる（実際 _tab_name でそれが起きていた）。基線に無い
    シンボルを import していないことを機械で釘付けする。
    """

    #: 0d304f0 の sheets_output に存在するトップレベルシンボル
    BASELINE_SYMBOLS = {"JST", "MF_HEADERS", "SheetsOutputWriter",
                        "_SEVERITY_COLORS", "_severity_color",
                        "GRID_ROW_BUFFER", "TAG_COL_INDEX"}

    # 実行時に `sheets_output.X` として触る名前（monkeypatch 対象）。
    # import 文ではないので上の ImportFrom 走査には引っかからない。
    BASELINE_ATTRS = {"datetime", "format_cell_range", "format_cell_ranges"}

    def test_runtime_attribute_access_is_declared(self):
        """属性アクセスも基線互換の対象——AST ガードの穴を塞ぐ。

        `capture_formats` は sheets_output の名前を差し替えるが、それは import 文
        ではないため ImportFrom 走査では見えない。基線に無い名前を触り始めても
        テストは緑のまま、基線で実行した瞬間に AttributeError になる。
        """
        import ast, inspect
        tree = ast.parse(inspect.getsource(gr))
        touched = {n.attr for n in ast.walk(tree)
                   if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                   and n.value.id == "sheets_output"}
        self.assertTrue(touched, "sheets_output への属性アクセスが検出できない")
        self.assertLessEqual(
            touched, self.BASELINE_ATTRS,
            f"未申告の実行時依存: {sorted(touched - self.BASELINE_ATTRS)} "
            f"——基線 0d304f0 に存在するか確認し、BASELINE_ATTRS へ追加すること")

    def test_module_level_sheets_output_imports_exist_in_baseline(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(gr))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "sheets_output":
                imported.update(alias.name for alias in node.names)
        self.assertTrue(imported, "sheets_output からの import が見つからない")
        self.assertLessEqual(
            imported, self.BASELINE_SYMBOLS,
            f"基線に存在しないシンボルを import している: "
            f"{sorted(imported - self.BASELINE_SYMBOLS)}")


class _RecordsMixin:
    """records を組んで normalize する共通土台（TestCase ではないので重複実行しない）。"""

    TAB = "T_順序"

    @staticmethod
    def _fmt(name):
        """severity 名（または "white"）から CellFormat を作る。"""
        color = (Color(*gr.WHITE_RGB) if name == "white"
                 else gr._SEVERITY_COLORS[name])
        return CellFormat(backgroundColor=color)

    def _normalize(self, ops):
        """(cell_ref, severity 名) の列を捕獲順の records として normalize する。"""
        records = [(self.TAB, ref, self._fmt(name)) for ref, name in ops]
        return gr.normalize("fx", "ui", gr.make_offline_writer(), records)

    def _tab(self, out):
        return out["tabs"][self.TAB]


class FinalStateTest(_RecordsMixin, unittest.TestCase):
    """順序盲の修復（Plan T1）。

    旧口径は highlights を集合へ畳むため「白が高亮を上書きして消した」を表現できない。
    最終態（final_highlights）と白による消去記録（white_erased）でそれを可視化する。
    """

    # --- 上書き語義 ---

    def test_white_after_highlight_erases_it(self):
        out = self._normalize([("B10", "high"), ("B10", "white")])
        self.assertEqual(self._tab(out)["final_highlights"], [])
        self.assertEqual(self._tab(out)["white_erased"],
                         [{"cell": "B10", "was": "high"}])

    def test_highlight_after_white_survives(self):
        out = self._normalize([("B10", "white"), ("B10", "high")])
        self.assertEqual(self._tab(out)["final_highlights"],
                         [{"cell": "B10", "severity": "high"}])
        self.assertEqual(self._tab(out)["white_erased"], [])

    def test_partial_range_overwrite(self):
        """行全体を塗った後 1 セルだけ白 → そのセルだけが消える。"""
        out = self._normalize([("A5:AB5", "high"), ("B5", "white")])
        cells = [h["cell"] for h in self._tab(out)["final_highlights"]]
        self.assertEqual(len(cells), 27)          # 28 列 - B5
        self.assertNotIn("B5", cells)
        self.assertIn("A5", cells)
        self.assertIn("AB5", cells)
        self.assertEqual(self._tab(out)["white_erased"],
                         [{"cell": "B5", "was": "high"}])

    def test_non_white_overwrite_is_not_erasure(self):
        """黄(全行下地) → 赤(I列) は正しい優先順位であり消去ではない。"""
        out = self._normalize([("A6:AB6", "low"), ("I6", "high")])
        by_cell = {h["cell"]: h["severity"]
                   for h in self._tab(out)["final_highlights"]}
        self.assertEqual(by_cell["I6"], "high")
        self.assertEqual(by_cell["A6"], "low")
        self.assertEqual(len(by_cell), 28)
        self.assertEqual(self._tab(out)["white_erased"], [])

    # --- D1 の中核主張：順序が違っても最終態は同じ ---

    def test_different_operation_history_same_final_state_is_equal(self):
        """UI 風と頁級風で白リセットの範囲が違っても final は一致する。"""
        ui = self._normalize([("A6:AB6", "white"), ("A6:AB6", "low"),
                              ("I6", "high")])
        page = self._normalize([("A6:AB7", "white"), ("A6:AB6", "low"),
                                ("I6", "high")])
        # 非空を先に固定する——[] == [] で通ってしまう空転を防ぐ
        self.assertEqual(len(self._tab(ui)["final_highlights"]), 28)
        self.assertEqual(self._tab(ui)["final_highlights"],
                         self._tab(page)["final_highlights"])
        self.assertEqual(self._tab(ui)["white_erased"], [])
        self.assertEqual(self._tab(page)["white_erased"], [])

    # --- 範囲パーサ ---

    def test_expand_range_forms(self):
        self.assertEqual(gr.expand_range("I7"), [(7, 8)])
        self.assertEqual(gr.expand_range("I7:I7"), [(7, 8)])
        self.assertEqual(gr.expand_range("AB6"), [(6, 27)])
        self.assertEqual(len(gr.expand_range("A5:AB6")), 56)   # 28 列 x 2 行
        self.assertEqual(len(gr.expand_range("B5:B9")), 5)     # 単列 5 行

    def test_cell_a1_column_letters(self):
        """Z→AA 境界は列文字変換の古典的な off-by-one 罠。"""
        self.assertEqual(gr.cell_a1(10, 1), "B10")
        self.assertEqual(gr.cell_a1(7, 25), "Z7")
        self.assertEqual(gr.cell_a1(7, 26), "AA7")
        self.assertEqual(gr.cell_a1(6, 27), "AB6")

    def test_range_parser_rejects_malformed_refs(self):
        """解析不能な ref は fail fast。静默無視は「消えたのに緑」を生む。"""
        for bad in ("A:AB", "", "AB6:A6", "A6:AB", "??", "A0", "A1:B2:C3"):
            with self.assertRaises(ValueError, msg=f"{bad!r} を拒否していない"):
                gr.expand_range(bad)

    # --- 並び順（A1 文字列順だと "A10" < "A6" になる） ---

    def test_final_highlights_sorted_by_row_then_column(self):
        out = self._normalize([("B10", "high"), ("B6", "low"), ("A6", "medium")])
        self.assertEqual([h["cell"] for h in self._tab(out)["final_highlights"]],
                         ["A6", "B6", "B10"])

    def test_every_tab_has_the_new_fields(self):
        """records が無い tab にもキーが存在する（byte diff を安定させる）。

        capture_formats で包むのは、包まないと本物の format_cell_range が
        FakeWorksheet を相手に走り、生産の best-effort ハンドラへ落ちて
        別の分岐を踏んでしまうため。
        """
        writer = gr.make_offline_writer()
        with gr.capture_formats():
            gr.drive_ui(writer, [_normal_page(1, 1)], DocType.RECEIPT)
        out = gr.normalize("fx", "ui", writer, [])
        self.assertTrue(out["tabs"])
        for tab in out["tabs"].values():
            self.assertEqual(tab["final_highlights"], [])
            self.assertEqual(tab["white_erased"], [])

    def test_unknown_color_keeps_its_rgb_in_both_new_fields(self):
        """_SEVERITY_COLORS に無い色は rgb を落とさない（旧 highlights と同口径）。

        判別力が旧口径より弱くなると、「旧が盲だったから直す」という本改修の
        前提が新フィールド側で崩れる。
        """
        purple = CellFormat(backgroundColor=Color(0.85, 0.8, 1))
        white = self._fmt("white")
        rows = [(self.TAB, "B5", purple), (self.TAB, "B5", white)]
        out = gr.normalize("fx", "ui", gr.make_offline_writer(), rows[:1])
        self.assertEqual(self._tab(out)["final_highlights"],
                         [{"cell": "B5", "severity": "unknown",
                           "rgb": [0.85, 0.8, 1]}])
        erased = gr.normalize("fx", "ui", gr.make_offline_writer(), rows)
        self.assertEqual(self._tab(erased)["white_erased"],
                         [{"cell": "B5", "was": "unknown",
                           "rgb": [0.85, 0.8, 1]}])


class OrderBlindnessProofTest(_RecordsMixin, unittest.TestCase):
    """T3-a: 否定対照——旧口径が盲であり新口径が盲でないことの実証。

    同一の多重集合・異なる順序の 2 本を流す。生産コードには一切触れずに
    「盲点そのもの」を証明する（Codex #5）。
    """

    # 白リセットが高亮の前（正しい）／後（バグ）の 2 本。op の多重集合は同一。
    CORRECT = [("A6:AB6", "white"), ("I6", "high")]
    BROKEN = [("I6", "high"), ("A6:AB6", "white")]

    def test_the_two_orders_use_the_same_operations(self):
        """前提: 差は順序のみ（多重集合は同一）。"""
        self.assertEqual(sorted(self.CORRECT), sorted(self.BROKEN))
        self.assertNotEqual(self.CORRECT, self.BROKEN)

    def test_old_fields_are_blind_to_the_reordering(self):
        """旧口径は集合へ畳むため差分ゼロ＝盲。"""
        correct, broken = self._normalize(self.CORRECT), self._normalize(self.BROKEN)
        self.assertEqual(self._tab(correct)["highlights"],
                         self._tab(broken)["highlights"])
        self.assertEqual(correct["reset_ranges"], broken["reset_ranges"])

    def test_new_fields_detect_the_reordering(self):
        """新口径は赤くなる——赤が消えたことを検出する。"""
        correct, broken = self._normalize(self.CORRECT), self._normalize(self.BROKEN)
        self.assertEqual(self._tab(correct)["final_highlights"],
                         [{"cell": "I6", "severity": "high"}])
        self.assertEqual(self._tab(broken)["final_highlights"], [])
        self.assertEqual(self._tab(broken)["white_erased"],
                         [{"cell": "I6", "was": "high"}])


if __name__ == "__main__":
    unittest.main()
