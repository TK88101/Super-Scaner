"""黄金様本回帰の replay ハーネス（Plan §3 T1）。

fixture（process_pipeline の yield を落としたもの）を fake worksheet 上で書账管線へ
流し、D4 の正規化 JSON を出す。UI 経路（append_entries）と頁級経路（build_page_write
＋commit_page）の両方を同一の土俵へ乗せ、diff で回帰を判定する。

基線（0d304f0）でも動くことが必須のため、頁級 API は path="page" のときだけ触る。
"""
import os
import re
from contextlib import contextmanager
from datetime import datetime as _real_datetime

import sheets_output
from doc_types import DOC_TYPE_TAB_SUFFIX
from sheets_output import (JST, MF_HEADERS, SheetsOutputWriter,
                           _SEVERITY_COLORS)


# --- 定数 ---------------------------------------------------------------

# D4-1 時刻凍結。合成定数（実行時刻に依存しない）。
FROZEN_AT = _real_datetime(2026, 1, 1, 0, 0, tzinfo=JST)
FROZEN_TS = FROZEN_AT.strftime("%Y/%m/%d %H:%M")
# 二重防御：normalize 時に作成日時／最終更新日時列をこの値へ置換する。
FROZEN_MASK = "<FROZEN>"
# 列位置は MF_HEADERS から引く（sheets_output.TAG_COL_INDEX と同じ流儀。
# ハードコードすると MF_HEADERS の並びが変わったとき静かに列がずれる）。
TIMESTAMP_COLS = (MF_HEADERS.index("作成日時"), MF_HEADERS.index("最終更新日時"))

# 白（意味的高亮ではない）＝ reset_ranges へ分離
WHITE_RGB = (1, 1, 1)

EMPLOYEE_NAME = "LocalTest"

# fake tab の初期状態＝凡例 4 行 ＋ ヘッダー 1 行（実タブ生成後と同形）
_LEGEND_ROW_COUNT = 4


def tab_name_for(doc_type, employee_name=EMPLOYEE_NAME):
    """タブ名を自前で組む（sheets_output._tab_name を import しない）。

    _tab_name は 396dbc7 で抽出された HEAD 限定の私有ヘルパで、基線 0d304f0 には
    存在しない。module 級で import すると path="ui" しか使わない T4/DIFF-A まで
    ImportError で落ちる。式は両版で同一（基線は 0d304f0:sheets_output.py:146-148
    でインライン展開している）。
    """
    return f"{employee_name}_{DOC_TYPE_TAB_SUFFIX.get(doc_type, '領収書')}"


# --- fake worksheet -----------------------------------------------------

class FakeWorksheet:
    """gspread Worksheet の最小代替（裁決#8：容量・清掃の呼出も握り潰さない）。

    row_count / add_rows を欠くと _ensure_row_capacity・_sanitize_trailing_once の
    例外が best-effort の try/except に飲まれ、静かに未検証になる（F12）。
    """

    def __init__(self, title, values=None, row_count=1000):
        self.title = title
        self._values = [list(r) for r in (values if values is not None
                                          else self._initial_values())]
        self.row_count = row_count
        self.append_rows_calls = 0
        self.appended_rows = []
        self.warnings = []

    @staticmethod
    def _initial_values():
        legend = [[f"__legend_{i}__"] for i in range(1, _LEGEND_ROW_COUNT + 1)]
        return legend + [list(MF_HEADERS)]

    def get_all_values(self):
        # 浅いコピーで足りる（書账層は返り値を読むだけ）。深いコピーだと
        # 呼出のたびに全表を再構築し、頁数に対して O(n^2) になる。
        return list(self._values)

    def append_rows(self, rows, value_input_option=None):
        self.append_rows_calls += 1
        for row in rows:
            stored = [str(c) for c in row]
            self._values.append(stored)
            self.appended_rows.append(stored)

    def append_row(self, row, value_input_option=None):
        self.append_rows([row], value_input_option=value_input_option)

    def add_rows(self, n):
        self.row_count += n
        self.warnings.append(f"add_rows({n}) on {self.title}")


def make_offline_writer(employee_name=EMPLOYEE_NAME):
    """gspread 認証を通さない SheetsOutputWriter を組み立てる（F6 の手法）。

    _get_or_create_tab を fake 返却へ差し替えるので、実タブ生成（凡例書込＝
    severity 色の format_cell_range 3 発）は起きない＝高亮記録が汚れない。
    """
    writer = SheetsOutputWriter.__new__(SheetsOutputWriter)
    # spreadsheet は本ハーネスが駆動する経路（append_entries／build_page_write／
    # commit_page／next_txn_no）では一切参照されない。probe_page 等を将来
    # 駆動するなら fake を差す必要がある。
    writer.spreadsheet = None
    # §5.1-d T4: `_tab_namer` の既定は SheetsOutputWriter のクラス属性が供給する
    # （__new__ 迂回でも属性探索で到達。基線 0d304f0 は参照自体が無い＝設定不要）。
    writer._ws_cache = {}
    writer._tab_has_data = {}
    writer._tab_next_txn = {}
    writer._tabs_sanitized = set()
    writer._fake_tabs = {}
    writer._replay_warnings = []   # ハーネス側の注記（ESCALATE 等）

    def _get_or_create_tab(tab_name):
        ws = writer._fake_tabs.get(tab_name)
        if ws is None:
            ws = FakeWorksheet(tab_name)
            writer._fake_tabs[tab_name] = ws
            writer._tab_has_data[tab_name] = False
        return ws

    writer._get_or_create_tab = _get_or_create_tab
    return writer


# --- 時刻凍結 -----------------------------------------------------------

class _FixedDatetime:
    """sheets_output.datetime の差替。now(tz) だけを固定する。"""

    def __init__(self, moment):
        self._moment = moment

    def now(self, tz=None):
        return self._moment.astimezone(tz) if tz else self._moment

    def __getattr__(self, name):
        return getattr(_real_datetime, name)


@contextmanager
def _swap_datetime(obj):
    original = sheets_output.datetime
    sheets_output.datetime = obj
    try:
        yield
    finally:
        sheets_output.datetime = original


@contextmanager
def freeze_time(moment=FROZEN_AT):
    """D4-1：書账層の now を指定時刻へ固定する（既定＝合成定数）。"""
    with _swap_datetime(_FixedDatetime(moment)):
        yield


# --- 高亮の捕獲 ---------------------------------------------------------

_CELL_RE = re.compile(r"^([A-Z]+)(\d+)$")


def _col_index(letters):
    """列文字 → 0 始まりの列番号（A=0, Z=25, AA=26, AB=27）。"""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def cell_a1(row, col):
    """(行番号 1 始まり, 列番号 0 始まり) → A1 記法。"""
    letters = ""
    n = col + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def parse_range(ref):
    """A1 記法の範囲を (r1, c1, r2, c2) へ。解析不能なら ValueError。

    静默無視は禁止（Codex #8）——ref を読み飛ばすと「高亮が消えたのに緑」という、
    本改修が潰そうとしている失敗様態そのものを新設してしまう。
    """
    parts = (ref or "").split(":")
    if len(parts) > 2:
        raise ValueError(f"解析不能な cell ref: {ref!r}")
    bounds = []
    for part in parts:
        m = _CELL_RE.match(part)
        if not m:
            raise ValueError(f"解析不能な cell ref: {ref!r}")
        row = int(m.group(2))
        if row < 1:
            raise ValueError(f"行番号は 1 以上: {ref!r}")
        bounds.append((row, _col_index(m.group(1))))
    (r1, c1), (r2, c2) = bounds[0], bounds[-1]
    if r1 > r2 or c1 > c2:
        raise ValueError(f"逆順の範囲: {ref!r}")
    return (r1, c1, r2, c2)


def expand_range(ref):
    """範囲を (行, 列) のセル列へ展開する（行優先・昇順）。"""
    r1, c1, r2, c2 = parse_range(ref)
    return [(r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]


def canonical_ref(ref):
    """D4-4：`I7` を `I7:I7` へ寄せ、範囲形はそのまま返す。"""
    m = _CELL_RE.match(ref)
    if m:
        return f"{m.group(1)}{m.group(2)}:{m.group(1)}{m.group(2)}"
    return ref


def _rgb(color):
    if color is None:
        return None
    return (color.red or 0, color.green or 0, color.blue or 0)


def severity_of(fmt):
    """D4-3：記録した色を (severity 名, rgb) へ逆写像する。

    色無し（罫線のみ等）は (None, None)、白は ("white", rgb)。既知 3 色に無い色は
    ("unknown", rgb) —— 名前欄には float の repr を混ぜず、rgb を別枠で持たせる
    （severity 欄は enum のまま保ち、T7 の証拠包を読む人が源を読まずに済む）。
    """
    rgb = _rgb(getattr(fmt, "backgroundColor", None))
    if rgb is None:
        return (None, None)
    if rgb == WHITE_RGB:
        return ("white", rgb)
    for name, color in _SEVERITY_COLORS.items():
        if _rgb(color) == rgb:
            return (name, rgb)
    return ("unknown", rgb)


@contextmanager
def capture_formats():
    """D4-2：モジュール級 format_cell_range のみを patch し (ref, fmt) を記録する。

    _format_with_retry は内部でこの同じ名前を呼ぶ（sheets_output.py:718）ため、
    ここ 1 箇所で F8 の 4 出口＋頁級経路の全出口を捕獲できる。
    """
    records = []
    original = sheets_output.format_cell_range
    original_batch = sheets_output.format_cell_ranges

    def _spy(worksheet, cell_ref, fmt, *args, **kwargs):
        records.append((getattr(worksheet, "title", "?"), cell_ref, fmt))

    def _spy_batch(worksheet, ranges, *args, **kwargs):
        """複数形も捕獲する（現状 `_detect_and_highlight_duplicates` 専用で、
        その関数は仓内に呼出点が無い＝死コード。将来接続された際に静默で
        取りこぼさないための防御であり、今は 1 件も流れてこない）。"""
        for cell_ref, fmt in ranges:
            records.append((getattr(worksheet, "title", "?"), cell_ref, fmt))

    sheets_output.format_cell_range = _spy
    sheets_output.format_cell_ranges = _spy_batch
    try:
        yield records
    finally:
        sheets_output.format_cell_range = original
        sheets_output.format_cell_ranges = original_batch


# --- wrapper 語義の再現（D2、F10） --------------------------------------

def _placeholder_result(error_pages, count, failed_page_nums, source_name,
                        employee_name):
    """local_test.process_local_file の集約占位行と一字一句同じ entries_data。"""
    failed_pages_str = ",".join(f"p{n}" for n in failed_page_nums)
    return {
        "entries": [],
        "_unrecognized": True,
        "memo": (f"⚠ ページ処理エラー {error_pages}/{count}頁 "
                 f"[{failed_pages_str}] 手動再スキャン要"),
        "date": "",
        "vendor": source_name,
        "uploader": employee_name,
    }


def _partition(fixture):
    """fixture を (書账層へ渡す頁, 失敗頁番号, 総頁数) へ分ける（_page_error は skip）。"""
    live, failed = [], []
    for page in fixture:
        result = page["result"]
        if result.get("_page_error"):
            failed.append(page["page_num"])
        else:
            live.append(page)
    return live, failed, len(fixture)


def _plan_writes(fixture, source_name, employee_name):
    """両経路が共有する「何を書くか」の計画（partition・全頁失敗ガード・占位行）。

    戻り値＝(groups, placeholder)。groups＝[(page_num, [result, ...]), ...] で、
    page_num が同じ連続 yield は 1 頁ぶんへ束ねる（main._process_file_headless の
    頁緩衝と同口径、main.py:454-467）。

    count は **yield 数**であって頁数ではない——生産側も local_test.py:95 と
    main.py:468 の双方が yield ごとに +1 しており、占位行の「N/M頁」表記はその
    まま yield 計数である。ここを頁数へ「直す」と再現対象からズレるので直さない。

    placeholder は両経路とも append_entries で書く（headless も同じ、
    main.py:498-510「UI 経路と同一挙動」）ため、頁級の書込計画には混ぜない。

    escalation：頁番号の**非連続な再登場**（例 1,2,1）は headless では拆頁前提の
    破れとして ESCALATE される（main.py:462-465）が、**UI/local_test 側にこの
    contract は無い**。よって groups は常に完全な計画を返し、頁級だけが
    `escalation.cut` で打ち切る。検出は「前頁の flush の**後**」に起きるので、
    生産でも再登場より前の頁は書込済み——cut は再登場した頁の直前を指す。
    escalation は None または (理由, cut) のタプル。
    """
    live, failed, count = _partition(fixture)
    if count == 0 or len(failed) == count:
        return [], None, None   # 全頁失敗 → Failed 相当・書込ゼロ

    groups = []
    seen_pages = set()
    escalation = None
    for page in live:
        result = dict(page["result"], uploader=employee_name)
        page_num = page["page_num"]
        if groups and groups[-1][0] == page_num:
            groups[-1][1].append(result)
            continue
        if groups:
            seen_pages.add(groups[-1][0])   # 頁境界＝前頁を flush（＝書込済み）
        if page_num in seen_pages and escalation is None:
            escalation = ((f"頁番号 {page_num} が非連続に再登場 → ESCALATE"
                           f"（拆頁前提破れ、main.py:462-465）。"
                           f"頁級では再登場頁以降と占位行を書かない"),
                          len(groups))
        groups.append((page_num, [result]))

    placeholder = (_placeholder_result(len(failed), count, failed, source_name,
                                       employee_name)
                   if failed else None)
    return groups, placeholder, escalation


def drive_ui(writer, fixture, doc_type, source_name="fixture",
             employee_name=EMPLOYEE_NAME):
    """UI 経路（append_entries）で fixture を書账層へ流す。基線でも動く。

    UI 側は local_test 同様「票ごとに 1 回 append_entries」であり、頁で束ねない
    （頁原子書込は頁級経路の性質）。start_new_file は最初の実書込の直前に 1 回。
    """
    # UI 経路（local_test）に頁番号連続性の contract は無いので escalation は無視。
    groups, placeholder, _esc = _plan_writes(fixture, source_name, employee_name)
    first_write_done = False
    for _page_num, results in groups:
        for result in results:
            if not first_write_done:
                writer.start_new_file(employee_name, doc_type, source_name)
                first_write_done = True
            writer.append_entries(employee_name=employee_name,
                                  doc_type=doc_type, entries_data=result,
                                  source_url="")
    if placeholder is not None:
        writer.append_entries(employee_name=employee_name, doc_type=doc_type,
                              entries_data=placeholder, source_url="")


def drive_page(writer, fixture, doc_type, source_name="fixture",
               employee_name=EMPLOYEE_NAME):
    """頁級経路（build_page_write＋commit_page）で同じ fixture を流す。

    1 頁＝1 回の build_page_write＋commit_page（同頁複数票は 1 頁へ束ねる）。
    部分失敗の集約占位行だけは頁級台賬の外で append_entries で書く——生産の
    headless もそうしている（main.py:498-510）。
    """
    groups, placeholder, escalation = _plan_writes(fixture, source_name,
                                                   employee_name)
    if escalation is not None:
        # 検出前に flush 済みの頁までは生産でも書かれている。以降は書かない。
        reason, cut = escalation
        writer._replay_warnings.append(reason)
        groups, placeholder = groups[:cut], None
    for _page_num, results in groups:
        start_txn = writer.next_txn_no(employee_name, doc_type)
        plan = writer.build_page_write(employee_name, doc_type, results,
                                       [""] * len(results), start_txn)
        writer.commit_page(plan)
    if placeholder is not None:
        writer.append_entries(employee_name=employee_name, doc_type=doc_type,
                              entries_data=placeholder, source_url="")


# --- 正規化 -------------------------------------------------------------

def _mask_row(row):
    cells = [str(row[i]) if i < len(row) else "" for i in range(len(MF_HEADERS))]
    for col in TIMESTAMP_COLS:
        cells[col] = FROZEN_MASK
    return cells


def _empty_block(rows=None):
    """tab ブロックの雛形。フィールドを増やす際の唯一の変更点にする。"""
    return {"rows": rows if rows is not None else [], "highlights": []}


def normalize(source, path, writer, records):
    """D4 の正規化 JSON を組む（浮動要素は一切入れない）。"""
    by_tab = {}
    reset_ranges = []
    warnings = []
    seen = {}   # tab -> set of (cell, severity, rgb)
    warnings.extend(getattr(writer, "_replay_warnings", []))

    for tab, ws in sorted(writer._fake_tabs.items()):
        by_tab[tab] = _empty_block([_mask_row(r) for r in ws.appended_rows])
        warnings.extend(ws.warnings)

    # 最終態（D1）: 捕獲順に再生し「後勝ち」を再現する。旧口径は集合へ畳むため
    # 「白が高亮を上書きして消した」を表現できない（順序盲）。
    #
    # 限界（Plan D5）: FakeWorksheet は append の書式継承を建模しないため、ここで
    # 得られるのは「**明示 format op のみ**を適用した最終非白態」であって真の
    # Sheets 背景色ではない。白リセット自体が削除／失敗して継承色が残るケースは
    # 明示 op を伴わないので現れない——それは真 Sheets readback でしか捕まらない。
    final = {}      # tab -> {(row, col): (severity, rgb)}
    erased = {}     # tab -> [((row, col), 消された severity)]

    for tab, cell_ref, fmt in records:
        severity, rgb = severity_of(fmt)
        if severity is None:
            continue  # 罫線のみ等、背景色を持たない書式は高亮ではない
        ref = canonical_ref(cell_ref)
        if severity == "white":
            reset_ranges.append(ref)
            # 白は「消す操作」。範囲を展開せず final の既存キーだけを走査する
            # （A7:AB1000 は 27,832 セル。非白側は生産の全出口が「今 append した
            # 行塊」に界されるため展開して安全——実測で最大 1 セル。D2）。
            # `get` であって `setdefault` でないのは、全 record が白の tab に
            # 空エントリを作らないため（存在しない tab が出力に現れるのを防ぐ）。
            r1, c1, r2, c2 = parse_range(cell_ref)
            tab_final = final.get(tab, {})
            hit = [k for k in tab_final
                   if r1 <= k[0] <= r2 and c1 <= k[1] <= c2]
            if hit:
                # pop で走査中の dict を変異させないよう、先に snapshot を取る
                erased.setdefault(tab, []).extend(
                    (k, tab_final.pop(k)) for k in hit)
            continue
        by_tab.setdefault(tab, _empty_block())
        for key in expand_range(cell_ref):
            final.setdefault(tab, {})[key] = (severity, rgb)
        seen.setdefault(tab, set()).add((ref, severity, rgb))

    for tab, entries in seen.items():
        by_tab[tab]["highlights"] = [
            ({"cell": c, "severity": s} if s != "unknown"
             else {"cell": c, "severity": s, "rgb": list(g)})
            for c, s, g in sorted(entries)]

    # 並びは (行, 列) の数値順。A1 文字列順だと "A10" < "A6" になる。
    # final / erased の tab は必ず by_tab にも居る（非白 record が先行するため）
    # ので by_tab を回すだけで足りる。
    for tab, block in by_tab.items():
        block["final_highlights"] = [
            ({"cell": cell_a1(r, c), "severity": sev} if sev != "unknown"
             else {"cell": cell_a1(r, c), "severity": sev, "rgb": list(g)})
            for (r, c), (sev, g) in sorted(final.get(tab, {}).items())]
        block["white_erased"] = [
            ({"cell": cell_a1(r, c), "was": was} if was != "unknown"
             else {"cell": cell_a1(r, c), "was": was, "rgb": list(g)})
            for (r, c), (was, g) in sorted(erased.get(tab, []))]

    return {
        "source": source,
        "path": path,
        "tabs": by_tab,
        "reset_ranges": sorted(set(reset_ranges)),
        "warnings": sorted(set(warnings)),
    }


def _replay(driver, path, fixture, doc_type, source, employee_name):
    writer = make_offline_writer(employee_name)
    with freeze_time(), capture_formats() as records:
        driver(writer, fixture, doc_type, source_name=source,
               employee_name=employee_name)
    return normalize(source, path, writer, records)


def replay_ui(fixture, doc_type, source="fixture", employee_name=EMPLOYEE_NAME):
    return _replay(drive_ui, "ui", fixture, doc_type, source, employee_name)


def replay_page(fixture, doc_type, source="fixture", employee_name=EMPLOYEE_NAME):
    return _replay(drive_page, "page", fixture, doc_type, source, employee_name)


# --- 出自証明（D3、diff 対象外） ----------------------------------------

_ORIGIN_MODULES = ("sheets_output", "config", "doc_types", "anomaly_detector",
                   "tag_rules")


def origin_report(writer_cls=SheetsOutputWriter):
    """worktree の取り違えを機械検出する（基線側は has_build_page_write=False）。"""
    import importlib
    from golden_capture import git_commit   # HEAD 取得は 1 箇所へ集約

    head = git_commit()
    modules = {}
    for name in _ORIGIN_MODULES:
        try:
            modules[name] = importlib.import_module(name).__file__
        except Exception as e:
            modules[name] = f"<unavailable: {e}>"

    return {
        "head": head,
        "cwd": os.getcwd(),
        "pythonpath": os.environ.get("PYTHONPATH", ""),
        "modules": modules,
        "has_build_page_write": hasattr(writer_cls, "build_page_write"),
    }
