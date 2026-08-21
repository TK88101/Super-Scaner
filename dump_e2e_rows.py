"""T11 の E2E 観測 driver。真票を replay で流し、**書かれる筈だったデータ行を拾う**。

生産経路には一切参加しない（`main.py` は本モジュールを参照しない）。
`local_test.py` の制御フローを**そのまま**駆動し、Google Sheets へ到達する
直前で行を横取りする。自作の擬似フローを測ると死経路の観測になるので、
製品の `process_local_file` を必ず通す。

## なぜ「書く直前」で捕まえるのか

MF の 28 列（H列インボイス・U列タグ・税区分…）は `SheetsOutputWriter.
append_entries` の中で組み立てられる。判定側で組み直すのは製品ロジックの
複製であり、必ず漂移する。よって行の構築は製品に任せ、
`_write_with_retry`（データ行の唯一の合流点）で横取りする。

**拾わないもの**: tab 初期化のヘッダ・凡例と、ファイル境界の分隔行。これらは
`_write_with_retry` を通らず `ws.append_row` で直接書かれる（`sheets_output.py:406/421/446`）。
明細ではないので判定の母集団にも入らない —— 「全ての行」ではなく
「データ行」を拾う、と読むこと。

## 触ってはいけない不変量

- **`local_test.process_pipeline` を patch すること。** `local_test.py:41` は
  `from ocr_engine import process_pipeline` でローカル束縛しているので、
  `ocr_engine` 側を包んでも捕捉できない（無音で素通りする）
- **頁 provenance は generator の状態で決めること。** `local_test` の
  writer 呼出のうち 4 箇所は `for page` 循環の外にある。「直前に yield した
  頁」で紐付けると前頁・前ファイルへ誤配される
- **`TEST_DIR` と `PROCESSED_DIR` を両方差し替えること。** 後者は import 時に
  前者から算出済みなので、片方だけだと原票が repo 内へ退避される
"""

import contextlib
import json
import os

EVENT_VERSION = 1

_PDF_EXTS = (".pdf",)


class EventSink:
    """観測イベントを溜めて 1 本の jsonl に落とす。

    CSV と JSONL に同じ事実を二重化しない —— 食い違ったときにどちらが正か
    決められなくなる。人が読む表は判定器が派生物として出す。
    """

    def __init__(self):
        self.events = []

    def emit(self, kind, **fields):
        event = {"v": EVENT_VERSION, "kind": kind}
        event.update(fields)
        self.events.append(event)
        return event

    def write(self, path):
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for e in self.events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return path


class _PageCursor:
    """今どの頁を処理中かを generator の状態だけで持つ。

    `in_loop` は「pipeline が yield した値を消費者が処理している最中」の意。
    generator が尽きた時点（= `for` 循環を抜けた瞬間）に False へ落ちるので、
    循環外の書込は自動的に file scope になる。**行番号に依存しない**のが要点
    —— 製品の行が動いても壊れない。
    """

    def __init__(self):
        self.page = None
        self.in_loop = False
        self.file = None
        self.seen_pages = []

    def scope(self):
        return ("page", self.page) if self.in_loop else ("file", None)

    def wrap(self, pipeline, sink):
        cursor = self

        def wrapped(file_path, doc_type=None, ocr_strategy=None, start_page=1):
            gen = pipeline(file_path, doc_type=doc_type,
                           ocr_strategy=ocr_strategy, start_page=start_page)
            try:
                for page in gen:
                    result = page.get("result") or {}
                    entries = result.get("entries") or []
                    cursor.page = page.get("page_num")
                    cursor.in_loop = True
                    cursor.seen_pages.append(cursor.page)
                    sink.emit("page_yield", file=cursor.file,
                              page=cursor.page,
                              total_pages=page.get("total_pages"),
                              excluded=bool(result.get("_excluded_page")),
                              exclude_reason=result.get("_exclude_reason"),
                              exclude_destination=result.get(
                                  "_exclude_destination"),
                              audit_signal=result.get("_audit_signal"),
                              ocr_text_len=result.get("_ocr_text_len", 0),
                              entries_len=len(entries),
                              year_estimated_count=sum(
                                  1 for e in entries
                                  if e.get("_year_estimated")))
                    yield page
            finally:
                cursor.in_loop = False
                cursor.page = None

        wrapped._dump_e2e_wrapper = cursor
        return wrapped


# 私有属性を揃えるファクトリと最小 worksheet は既存テストから借りる。
# `test_sheets_output._make_writer` の docstring が「3 本の手抄ファクトリが
# 非同期化した反省」を記録しており、`test_sheets_output_golden.py:36` は
# 既に同じ理由でこれを借りている。ここで 4 本目を手書きすると、
# `SheetsOutputWriter.__init__` に快取属性が増えたとき直し漏れる側が 1 つ増える。
# **あちらは無修正のまま**借りるだけ。
from test_sheets_output import _FakeWorksheet as _BaseFakeWorksheet
from test_sheets_output import _make_writer


class _FakeWorksheet(_BaseFakeWorksheet):
    """既存の最小 worksheet に、本 driver が要る 4 機能だけ足した派生。

    基底が持つのは `get_all_values` / `append_rows`。ここで足すのは
    `title`（MF タブか監査タブかの識別）・`id`・`row_count` / `add_rows`
    （`_ensure_row_capacity` の実物が触る）・`append_row`
    （`start_new_file` がファイル境界の分隔行を書く。2 通目以降で発火する）。
    `row_values` は基底にも無いので足すが、`_get_or_create_audit_tab` を
    差し替えている限り呼ばれない。
    """

    def __init__(self, title=""):
        super().__init__()
        self.title = title
        self.id = abs(hash(title)) % 100000
        self.row_count = 1000

    def append_row(self, row, value_input_option=None):
        self._values.append(list(row))

    def row_values(self, _n):
        return []

    def add_rows(self, n):
        self.row_count += n


class _WriterCapture:
    """`SheetsOutputWriter` を「Sheets の代わりに sink へ書く」形に組み替える。

    `__init__` を通さない（gspread 認証を張らない）。`_resolve_tab` を
    差し替えて spreadsheet 層ごと省き、染色系は no-op にする——染色は
    セルの背景色であって行の中身ではないので、判定には要らない。
    """

    def __init__(self, sink, cursor):
        self.sink = sink
        self.cursor = cursor
        self._tabs = {}
        self._audit_ctx = None
        self._placeholder_depth = 0
        # `append_entries` 1 回分 = 1 batch。同じ頁に 2 batch 出たら
        # 「同じ頁を二度書いた」の徴候（A1b）。頁内に同額同名の行が
        # 並ぶこと自体は正当（ETC の同一料金所を何度も通る等）なので、
        # 行の内容で重複を判定してはいけない。
        self._batch = 0

    def build(self):
        from sheets_output import AUDIT_TAB_NAME

        w = _make_writer()          # 私有属性は既存の単一ファクトリに任せる
        capture = self

        def _tab(tab_name):
            if tab_name not in capture._tabs:
                capture._tabs[tab_name] = _FakeWorksheet(tab_name)
            return capture._tabs[tab_name]

        def _write_with_retry(worksheet, rows, max_retries=5):
            for row in rows:
                capture._record(worksheet, row)
            worksheet.append_rows(rows)

        def _noop(*_a, **_kw):
            return None

        real_audit = w.append_audit_row
        real_placeholder = w._write_unrecognized_row
        real_entries = w.append_entries

        def append_entries(employee_name, doc_type, entries_data,
                           source_url=""):
            capture._batch += 1
            return real_entries(employee_name, doc_type, entries_data,
                                source_url)

        def append_audit_row(filename, page_num, verdict, reason,
                             ocr_text_len, source_url=""):
            # 理由列は `audit_reason_ja` で訳されてから行に載る。機械可読な
            # キーは引数にしか無いので、ここで捕まえておく。
            capture._audit_ctx = {"reason_key": reason, "verdict": verdict,
                                  "filename": filename, "page_num": page_num}
            try:
                return real_audit(filename, page_num, verdict, reason,
                                  ocr_text_len, source_url)
            finally:
                capture._audit_ctx = None

        def _write_unrecognized_row(tab_name, entries_data, source_url):
            capture._placeholder_depth += 1
            try:
                return real_placeholder(tab_name, entries_data, source_url)
            finally:
                capture._placeholder_depth -= 1

        # `start_new_file` と `append_entries` は `_resolve_tab` を経由せず
        # `_get_or_create_tab` を直接呼ぶ。ここを塞がないと spreadsheet 層
        # （None）へ落ちて AttributeError になる。`_resolve_tab` 自体は
        # 実物が純粋な振り分け（`sheets_output.py:889`）で spreadsheet を
        # 触らないので、下の 2 つを差し替えれば実物のまま委譲して問題ない。
        w._get_or_create_tab = _tab
        w._get_or_create_audit_tab = lambda: _tab(AUDIT_TAB_NAME)
        w._write_with_retry = _write_with_retry
        # 以下 4 つは `sheets_output.format_cell_range` をモジュール級で
        # 無効化してあるので**厳密には冗長**だが、二重の防波堤として残す
        # （patch が 1 本外れただけで fake worksheet 相手に本物の
        # gspread_formatting が走り、原因の分かりにくい失敗になる）。
        w._apply_anomaly_highlight = _noop
        w._format_with_retry = _noop
        w._sanitize_trailing_once = _noop
        w._ensure_row_capacity = _noop
        w.append_audit_row = append_audit_row
        w.append_entries = append_entries
        w._write_unrecognized_row = _write_unrecognized_row
        self._writer = w
        return w

    def _record(self, worksheet, row):
        scope, page = self.cursor.scope()
        title = getattr(worksheet, "title", "")
        if self._audit_ctx is not None:
            ctx = self._audit_ctx
            self.sink.emit("audit_row", file=self.cursor.file, page=page,
                           scope=scope, verdict=ctx["verdict"],
                           reason_key=ctx["reason_key"],
                           reason_ja=row[4] if len(row) > 4 else "",
                           audit_page_num=ctx["page_num"],
                           row=list(row))
            return
        self.sink.emit("mf_row", file=self.cursor.file, page=page,
                       scope=scope, tab=title, batch=self._batch,
                       is_data_row=self._placeholder_depth == 0,
                       row=list(row))


_ACTIVE = {}


def expected_page_count(path):
    """入力の頁数を**観測に依存しない源**から取る。

    pipeline が 1 頁も yield しない事故（`local_test.py:302` の欠落検出は
    `last_total_pages=0` だと空集合になり留痕すら残さない）を、観測側だけ
    見ていると取り逃す。画像は 1 頁。読めない PDF は 0 を返す（0 は
    「入力が壊れている」という判定材料であって、成功ではない）。
    """
    if not path.lower().endswith(_PDF_EXTS):
        return 1
    try:
        from pypdf import PdfReader
        return len(PdfReader(path).pages)
    except Exception:
        return 0


@contextlib.contextmanager
def dumping_run(staging_dir, sink):
    """観測に必要な差し替えを全部張る。**製品コードは 1 行も変えない。**"""
    import gspread
    import local_test
    import sheets_output

    cursor = _PageCursor()
    capture = _WriterCapture(sink, cursor)
    writer = capture.build()

    def _forbidden(*_a, **_kw):
        raise AssertionError(
            "本番 Sheets への到達を検出しました。dump は読み取り専用です")

    import card_file_recon

    real_recon = card_file_recon.report_and_record

    def _recon(observations, doc_type, filename, source_url, w,
               audit_verdict, had_page_errors=False):
        verdict = real_recon(observations, doc_type, filename, source_url,
                             w, audit_verdict, had_page_errors)
        sink.emit("recon", file=cursor.file or filename,
                  verdict=getattr(verdict, "verdict", None),
                  detail_sum=getattr(verdict, "detail_sum", None),
                  statement_total=getattr(verdict, "printed_total", None),
                  diff=getattr(verdict, "diff", None),
                  reason_key=getattr(verdict, "reason_key", None))
        return verdict

    saved = {
        "report_and_record": card_file_recon.report_and_record,
        "format_cell_range": sheets_output.format_cell_range,
        "TEST_DIR": local_test.TEST_DIR,
        "PROCESSED_DIR": local_test.PROCESSED_DIR,
        "process_pipeline": local_test.process_pipeline,
        "service_account": gspread.service_account,
        "authorize": gspread.authorize,
        "writer_init": sheets_output.SheetsOutputWriter.__init__,
    }

    # **入れ子を拒否する。** `_ACTIVE` は 1 組しか持てないので、入れ子にすると
    # 内側の後始末が外側の状態を消し、以後の `run_one_file` が別の run の
    # writer へ書き込む —— 例外は出ず、events だけが静かに混ざる。
    if _ACTIVE:
        raise RuntimeError("dumping_run は入れ子にできません")

    # **patch の設置も try の中に置く。** 途中の代入が失敗したとき、それまでに
    # 設置した patch が復元されないまま他のテストへ漏れる（gspread が
    # 例外を投げる状態のまま残る等）。設置と解除を同じ try/finally に入れる。
    try:
        card_file_recon.report_and_record = _recon
        # 染色は gspread_formatting が worksheet.spreadsheet を触るので fake
        # では落ちる。**行の内容には影響しない**（背景色）ので無効化する。
        sheets_output.format_cell_range = lambda *_a, **_kw: None
        local_test.TEST_DIR = staging_dir
        local_test.PROCESSED_DIR = os.path.join(staging_dir, "processed")
        local_test.process_pipeline = cursor.wrap(saved["process_pipeline"],
                                                  sink)
        gspread.service_account = _forbidden
        gspread.authorize = _forbidden
        sheets_output.SheetsOutputWriter.__init__ = _forbidden

        _ACTIVE["writer"] = writer
        _ACTIVE["cursor"] = cursor
        _ACTIVE["capture"] = capture
        yield writer
    finally:
        card_file_recon.report_and_record = saved["report_and_record"]
        sheets_output.format_cell_range = saved["format_cell_range"]
        local_test.TEST_DIR = saved["TEST_DIR"]
        local_test.PROCESSED_DIR = saved["PROCESSED_DIR"]
        local_test.process_pipeline = saved["process_pipeline"]
        gspread.service_account = saved["service_account"]
        gspread.authorize = saved["authorize"]
        sheets_output.SheetsOutputWriter.__init__ = saved["writer_init"]
        _ACTIVE.clear()


def run_one_file(file_info, sink):
    """1 ファイルを製品経路で流す。`dumping_run` の中でだけ呼べる。"""
    import local_test

    writer = _ACTIVE.get("writer")
    cursor = _ACTIVE.get("cursor")
    if writer is None or cursor is None:
        raise RuntimeError("dumping_run の外では実行できません")

    # **番人**: `dumping_run` を張った後で誰かが `local_test.process_pipeline`
    # を差し替えると、wrapper は無音で外れる —— 例外も出ず、events は空で、
    # 「このファイルには行が無かった」と見分けが付かない。観測器が観測して
    # いないことに気付けないのは、この Plan が潰そうとしている事故そのもの。
    if getattr(local_test.process_pipeline, "_dump_e2e_wrapper", None) is not cursor:
        raise RuntimeError(
            "local_test.process_pipeline が dumping_run の外で差し替えられて"
            "います。頁の観測が無音で失われるため中断します"
            "（テストで pipeline を差し替えるときは dumping_run の"
            "**外側**で patch すること）")

    cursor.file = file_info["name"]
    cursor.seen_pages = []
    sink.emit("file_start", file=file_info["name"],
              doc_type=file_info["doc_type"],
              expected_pages=expected_page_count(file_info["path"]))
    try:
        ok = local_test.process_local_file(file_info, writer)
    except Exception as e:  # noqa: BLE001 - 1 ファイルの失敗で観測を止めない
        sink.emit("file_error", file=file_info["name"],
                  error="%s: %s" % (type(e).__name__, e))
        ok = False
    sink.emit("file_end", file=file_info["name"], success=bool(ok),
              seen_pages=sorted(set(cursor.seen_pages)))
    cursor.file = None
    return ok


# ── 実行入口（U3）────────────────────────────────────────

DEFAULT_SAMPLES = "~/Downloads/クレジットカード訓練樣本"
DEFAULT_REPLAY = "fixtures/full"
DEFAULT_OUT = "dump/events.jsonl"

# ニモカだけ transit_ic。タブは folder の doc_type で決まる（母 Plan AD-T3-1）。
_TRANSIT_MARKERS = ("ニモカ", "nimoca")
_INPUT_EXTS = (".pdf", ".jpg", ".jpeg", ".png")


def _folder_for(name):
    return ("transit_ic" if any(m in name for m in _TRANSIT_MARKERS)
            else "credit_card")


def stage_inputs(samples_dir, staging_dir):
    """原票を一時領域へ**コピー**する（move しない）。

    製品の `process_local_file` は成功後に入力を `shutil.move` で
    `processed/` へ動かす。原本を直接食わせると 1 回しか実行できず、
    かつ顧客の実 PDF が repo ツリー内に残る。
    """
    import shutil

    samples_dir = os.path.expanduser(samples_dir)
    staged = []
    os.makedirs(os.path.join(staging_dir, "processed"), exist_ok=True)
    for name in sorted(os.listdir(samples_dir)):
        if not name.lower().endswith(_INPUT_EXTS):
            continue
        folder = os.path.join(staging_dir, _folder_for(name))
        os.makedirs(folder, exist_ok=True)
        shutil.copy2(os.path.join(samples_dir, name),
                     os.path.join(folder, name))
        staged.append(name)
    return staged


def collect_token_usage(replay_dir, sink):
    """録音の `usage` を拾う（U7）。**推定ではなく実測値**。

    `fixtures/full/<slot>/response.json` に Gemini が返した
    `{total_token_count, prompt_token_count, candidates_token_count}` と
    `finish_reason` が入っている。出力予算 `GEMINI_MAX_OUTPUT_TOKENS`
    に対する余裕はここから直接出せる（文字数からの見積りに落とさない）。

    `row_brace_count` は応答 JSON 中の `"amount"` の出現数＝明細行数の近似。
    ETC の 100 行頁を**特定する**ために要る —— 全 slot の最大値を取るだけでは、
    その最大が ETC 頁である保証が無い。
    """
    import re

    if not os.path.isdir(replay_dir):
        return 0
    n = 0
    for slot in sorted(os.listdir(replay_dir)):
        d = os.path.join(replay_dir, slot)
        if not os.path.isdir(d):
            continue
        try:
            with open(os.path.join(d, "response.json"), encoding="utf-8") as f:
                resp = json.load(f)
        except (OSError, ValueError):
            continue
        text = resp.get("text") or ""
        issuer = re.search(r'"issuer"\s*:\s*"([^"]*)"', text)
        card = re.search(r'"card_name"\s*:\s*"([^"]*)"', text)
        page = re.search(r'"statement_page"\s*:\s*"([^"]*)"', text)
        sink.emit("token_usage", slot=slot,
                  usage=resp.get("usage") or {},
                  finish_reason=resp.get("finish_reason"),
                  raises=bool(resp.get("raises")),
                  row_brace_count=text.count('"amount"'),
                  issuer=issuer.group(1) if issuer else "",
                  card_name=card.group(1) if card else "",
                  statement_page=page.group(1) if page else "")
        n += 1
    return n


def main(argv=None):
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(
        description="T11 の E2E 観測（真の Sheets には書きません）")
    parser.add_argument("--samples", default=DEFAULT_SAMPLES)
    parser.add_argument("--replay", default=DEFAULT_REPLAY)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    import local_test

    sink = EventSink()
    with tempfile.TemporaryDirectory(prefix="ss-e2e-") as staging:
        staged = stage_inputs(args.samples, staging)
        print("📥 %d 件を一時領域へ複製: %s" % (len(staged), staging))

        import gemini_record
        with gemini_record.replaying(args.replay) as session:
            with dumping_run(staging, sink):
                files = local_test.scan_local_files()
                print("🔎 %d 件を検出" % len(files))
                for info in files:
                    run_one_file(info, sink)
        # `calls` は **再生回数**。実 Gemini を呼んだか否かは `mode` が持つ。
        print("🎬 録音の再生 %d 回 (mode=%s)" % (session.calls, session.mode))
        if session.mode != "replay":
            print("❌ mode が replay ではありません（実 Gemini を呼びます）")
            return 1

    n_tokens = collect_token_usage(args.replay, sink)
    print("🔢 録音 %d slot の usage を収録" % n_tokens)
    sink.write(args.out)
    kinds = {}
    for e in sink.events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print("📝 %s へ %d イベント %s" % (args.out, len(sink.events), kinds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
