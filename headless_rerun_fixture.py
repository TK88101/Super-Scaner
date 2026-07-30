"""IP-304 T4: headless 崩潰重跑テスト夾具（B4/IP-305 復用交付物）。

多頁ファイル模擬＋任意頁 kill＋再跑を駆動する共有部品。fake Firestore 台賬（跨 run で
状態保持）＋fake writer（メモリ Sheets、append ログ）＋patched process_pipeline を
一箇所に集約し、T3（test_process_file_headless）と B4（断点続跑）が同一夾具を import
して崩潰冪等シナリオを組めるようにする。真 Drive/Sheets/Firestore は一切接続しない。

使い方（例）::

    fx = HeadlessRerunFixture()
    fx.run(pages(3))                       # 正常 3 頁
    fx.run(pages(3), crash_commit_on=3)    # 3 頁目 commit で崩潰（例外送出）
    fx.run(pages(3))                       # 再跑：前 2 頁 SKIP・3 頁目補齊、重複なし
    assert fx.landed_row_count() == 3

`crash_commit_on` は「N 回目の commit_page で RuntimeError」＝append 直前 kill（witness
ABSENT）。`crash_confirm` は「CONFIRMED 直前 kill」（append landed・台賬 PENDING 残置）。
"""

from __future__ import annotations

import types
from unittest.mock import patch

import main
from doc_types import DocType
from fake_firestore import FakeFirestore  # 台賬 fake は共有モジュール（Reuse 統合）
from ocr_engine import EXCLUDE_DEST_AUDIT_TAB
from posting_ledger import PostingLedger
from sheets_output import _classify_probe

DEFAULT_BASE = "cust:hash"
DEFAULT_UPLOADER = "田中"
_TAB = f"{DEFAULT_UPLOADER}_領収書"

__all__ = ["FakeFirestore", "FakeReporter", "FakeWriter", "HeadlessRerunFixture",
           "blank_result",
           "excluded_page", "make_ledger", "run_headless", "page", "pages",
           "yield_of", "zero_entry_result"]


class FakeReporter:
    """firestore_report.FirestoreReporter のダブル（B4 T4/T5 共用、真庫不接）。

    呼出し記録（report_posted_calls/report_dead_letter_calls/write_alert_calls）
    に加え、stale-epoch REJECTED を模せる：jobs[job_id]["lease_epoch"] が
    report_posted/report_dead_letter へ渡された lease_epoch と食い違えば
    "REJECTED:stale_lease_epoch" を返す（IP-305 DoD②）。job が無い／
    job に lease_epoch キーが無ければ食い違いチェックをスキップし常に
    "APPLIED"（simcodex Round 1 #2、test_headless_loop_wiring.py と
    test_ip305_resume.py の重複 _FakeReporter を統合）。
    """

    def __init__(self, jobs=None):
        self.client = object()
        self._jobs = jobs or {}
        self.report_posted_calls = []
        self.report_dead_letter_calls = []
        self.write_alert_calls = []

    def get_job(self, base):
        return self._jobs.get(base)

    def write_alert(self, alert_id, payload):
        self.write_alert_calls.append((alert_id, payload))

    def _check_epoch(self, job_id, lease_epoch):
        current = self._jobs.get(job_id, {}).get("lease_epoch")
        if current is not None and current != lease_epoch:
            return "REJECTED:stale_lease_epoch"
        return "APPLIED"

    def report_posted(self, job_id, *, lease_epoch):
        self.report_posted_calls.append((job_id, lease_epoch))
        return self._check_epoch(job_id, lease_epoch)

    def report_dead_letter(self, job_id, *, lease_epoch, error):
        self.report_dead_letter_calls.append((job_id, lease_epoch, error))
        return self._check_epoch(job_id, lease_epoch)


class FakeWriter:
    """メモリ Sheets writer（headless 経路が使う 5 メソッドのみ実装）。"""

    def __init__(self):
        self.sheets: dict = {}      # tab_name -> [row, ...]（着地済み）
        self.append_calls = 0
        self.build_page_write_calls = 0  # MF 行の組立回数（IP-402、commit とは別軸）
        self.placeholder_calls = []  # append_entries（部分エラー占位行）の記録
        self.start_new_file_calls = []  # UI 版のみ呼ばれる（B4 T4 適配）
        self.flush_calls = 0            # UI/headless 共通の後処理フック（B4 T4 適配）
        # IP-402 適配: 除外ページの留痕先（監査タブ）。audit_error に例外を
        # 差すと監査書込失敗（IP-402 Plan §4.4 の語義分離）を模せる。
        self.audit_rows: list = []
        self.audit_error = None

    def _tab(self, employee_name, doc_type):
        return f"{employee_name}_領収書"

    def start_new_file(self, employee_name, doc_type, file_name):
        """UI 版のみが呼ぶ PDF 間分割線＋取引No リセット（記録のみ、no-op）。"""
        self.start_new_file_calls.append((employee_name, doc_type, file_name))

    def flush(self):
        """将来の後処理フック（本番は no-op、呼出し回数のみ記録）。"""
        self.flush_calls += 1

    def append_entries(self, *, employee_name, doc_type, entries_data, source_url=""):
        """部分ページエラーの占位行書込（headless 経路が UI 経路と同一に呼ぶ）。記録のみ。"""
        self.placeholder_calls.append(entries_data)

    def append_audit_row(self, filename, page_num, verdict, reason,
                         ocr_text_len, source_url=""):
        """除外/分岐/分類変化ページの監査タブ追記（IP-402）。記録のみ。

        audit_error が設定されていれば送出する——監査タブが落ちたときの
        語義（headless は ESCALATE、UI は MF 退避）を分けて検証するため。
        """
        if self.audit_error is not None:
            raise self.audit_error
        self.audit_rows.append({
            "filename": filename, "page_num": page_num, "verdict": verdict,
            "reason": reason, "ocr_text_len": ocr_text_len,
            "source_url": source_url,
        })

    def next_txn_no(self, employee_name, doc_type):
        tab = self._tab(employee_name, doc_type)
        return len(self.sheets.get(tab, [])) + 1

    def build_page_write(self, employee_name, doc_type, results, urls, start_txn):
        # IP-402: 除外ページで「MF 行を組み立てすらしない」ことを直接断言
        # できるようにする——commit_page の回数だけでは、組み立てたが commit
        # しなかった実装（取引No を消費しうる）と区別できない。
        self.build_page_write_calls += 1
        tab = self._tab(employee_name, doc_type)
        rows = []
        txn = start_txn
        for r in results:
            rows.append([txn, r.get("vendor", ""), r.get("date", ""),
                         sum(int(e.get("amount", 0) or 0)
                             for e in r.get("entries", []))])
            txn += 1
        return types.SimpleNamespace(rows=rows, tab_name=tab)

    def peek_append_range(self, page_write):
        pre = len(self.sheets.get(page_write.tab_name, []))
        return (pre + 1, pre + len(page_write.rows))

    def commit_page(self, page_write):
        self.append_calls += 1
        sheet = self.sheets.setdefault(page_write.tab_name, [])
        start = len(sheet) + 1
        sheet.extend([list(r) for r in page_write.rows])
        return (start, len(sheet))

    def probe_page(self, sheet_tab, predicted_row_range, row_fingerprint):
        # 分類は本番 probe_page と同一ロジック（_classify_probe 共用）、I/O だけ in-memory
        sheet = self.sheets.get(sheet_tab, [])
        start, end = predicted_row_range
        segment = sheet[start - 1:end]
        return _classify_probe(segment, predicted_row_range, row_fingerprint)


class _FakeResolver:
    def __init__(self, *a, **k):
        pass

    def resolve(self, page_num, total_pages, page_bytes):
        return f"http://url/p{page_num}"

    def anchor_url(self, page_num, total_pages):
        """除外ページ用の非アップロード URL（IP-402）。resolve と区別できる形に
        して、除外経路が単ページ PDF を上げていないことをテストで断言できる。"""
        return f"http://url#page={page_num}"


def page(page_num, total, vendor, amount, extra=None):
    """1 result（1 票）の pipeline yield 相当 dict を作る。"""
    result = {"entries": [{"amount": amount, "debit_account": "備品・消耗品費"}],
              "vendor": vendor, "date": "2026/06/01"}
    if extra:
        result.update(extra)
    return {"result": result, "page_num": page_num, "total_pages": total,
            "page_bytes": b"x"}


def error_page(page_num, total, error_class="CONTENT"):
    """頁エラー（_page_error）の pipeline yield 相当 dict（B4 T3/T4/T5 適配）。

    error_class 既定 "CONTENT"（占位頁として頁緩衝に入り書込対象になる、
    旧 error_page 呼び出し元の「部分失敗でも可視化されて処理は続く」という
    意図を新語義でも保つ）。"RETRYABLE"/"UNKNOWN" 指定で暫時故障/未知故障
    （零書込）を模せる。error_class=None は _error_class キー自体を省略し、
    消費側デフォルト（main._classify_page_result_shape の "UNKNOWN" 倒し、
    B4 Plan §2.1）を検証する経路に使う。
    """
    result: dict = {"_page_error": True, "entries": [], "_unrecognized": True}
    if error_class is not None:
        result["_error_class"] = error_class
    return {"result": result, "page_num": page_num,
            "total_pages": total, "page_bytes": b""}


def blank_result(**markers):
    """entries を持たない result（`ocr_engine._blank_result` の写し）。

    本物の producer は占位頁も除外頁もこの形（date/vendor/invoice_num/memo/
    entries ＋ markers）で yield する。markers だけを渡し分けるのも同じ——
    夾具が本物と違う形を作ると、消費者がどのキーを見るかを変えた瞬間に
    「テストは通るが本番では通らない」が生まれる。
    """
    return {"date": "", "vendor": "", "invoice_num": "", "memo": "",
            "entries": [], **markers}


def zero_entry_result(**extra):
    """占位頁の bare result（entries 空・`_page_error` 無し・除外でもない）。

    「JSON は返ったが票面から仕訳を組めなかった」頁。page() が entries 非空
    専用なので、この形状はここで一元化する（simcodex Round 2 #4 と同じ方針
    ——yield 形状の重複実装は同期漏れの温床）。
    """
    return blank_result(_unrecognized=True, **extra)


def yield_of(result, page_num, total):
    """bare result dict を pipeline yield 形へ包む汎用ラッパ。"""
    return {"result": result, "page_num": page_num, "total_pages": total,
            "page_bytes": b"x"}


def excluded_page(page_num, total, reason="envelope",
                  destination=EXCLUDE_DEST_AUDIT_TAB, ocr_text_len=42):
    """除外ページ（封筒・社会保険料通知書）の pipeline yield 相当 dict（IP-402）。

    ocr_engine._yield_page_results が実際に yield する形（entries 空・
    `_page_error` 無し・`_excluded_page=True`・行き先を `_exclude_destination`
    で宣言）を再現する。これを占位頁と誤認するのが IP-402 の是正対象。

    `_unrecognized` は**付けない**——本物の除外頁（`_blank_result(_excluded_page=
    True, ...)`）はこのキーを持たない。夾具側で勝手に付けると、除外と認識不能を
    `_unrecognized` で見分ける消費者を書いた瞬間、この夾具は本物に到達できない
    形だけを試し続けることになる。
    """
    return yield_of(
        blank_result(
            memo=f"除外ページ（{reason}）",
            _excluded_page=True,
            _exclude_reason=reason,
            _exclude_destination=destination,
            _ocr_text_len=ocr_text_len,
        ),
        page_num, total)


def pages(n):
    """1..n の単票各頁（vendor=店1..店n、amount=1000*i）。"""
    return [page(i, n, f"店{i}", 1000 * i) for i in range(1, n + 1)]


def make_ledger(fs, writer, base=DEFAULT_BASE, runner=None):
    return PostingLedger(fs, base, sheet_probe=writer.probe_page,
                         transaction_runner=runner or fs.runner())


def run_headless(writer, ledger, page_yields, base=DEFAULT_BASE,
                 uploader=DEFAULT_UPLOADER):
    """process_file を headless 経路で駆動（process_pipeline/PageUrlResolver は fake）。"""
    with patch.object(main, "process_pipeline", return_value=iter(page_yields)), \
         patch.object(main, "PageUrlResolver", _FakeResolver):
        return main.process_file(
            service=None, sheets_writer=writer, file_path="dummy.pdf",
            uploader_name=uploader, chat_id=None, doc_type=DocType.RECEIPT,
            drive_file_id=None, base=base, ledger=ledger)


class HeadlessRerunFixture:
    """跨 run で fs（台賬）と writer（Sheets）を保持する崩潰重跑ハーネス。

    各 run は毎回新しい ledger を張る（＝プロセス再起動でメモリ状態が消えるが
    Firestore/Sheets は永続、という本番の崩潰復帰を再現）。
    """

    def __init__(self, base=DEFAULT_BASE, uploader=DEFAULT_UPLOADER, reporter=None):
        self.fs = FakeFirestore()
        self.writer = FakeWriter()
        self.base = base
        self.uploader = uploader
        # B4 T5 適配（IP-305 斷點續跑）: report 層の fake（任意、既定 None＝
        # 従来通り process_file 層のみ駆動）。設定時は run() が
        # main._report_headless_outcome も駆動し、report_log に記録する。
        self.reporter = reporter
        self.report_log: list = []

    def new_ledger(self, runner=None):
        return make_ledger(self.fs, self.writer, self.base, runner)

    def run(self, page_yields, *, crash_commit_on=None, crash_confirm=False,
           lease_epoch=None, cycle=1, file_id="fixture-file"):
        """1 回の process_file 実行（毎回 new_ledger＝プロセス再起動を模す）。

        crash_commit_on=N → N 回目の commit_page で RuntimeError（append 直前 kill）。
        crash_confirm=True → CONFIRMED 直前（post_page の 2 回目 txn）で 1 度 kill。
        どちらも None/False なら正常実行。kill モードは注入層が違う（writer 差替 vs
        runner 差替）ため分岐するが、実行本体は 1 つの run_headless に集約する。

        self.reporter が設定されていれば、process_file の戻り値
        （HeadlessOutcome）を main._report_headless_outcome へそのまま渡し
        （lease_epoch/cycle 指定可）、report_log に (outcome_label, expire_cycle)
        を追記する——IP-305 の断点続跑シナリオで「report 層まで通した」ことを
        同一夾具で検証できるようにする（B4 Plan T5、夾具適配は明示許容）。
        """
        writer = self.writer
        runner = None
        restore = None

        if crash_commit_on is not None:
            real_commit = writer.commit_page
            state = {"n": 0}

            def crashing(pw):
                state["n"] += 1
                if state["n"] == crash_commit_on:
                    raise RuntimeError(f"kill: commit #{crash_commit_on}（append 直前）")
                return real_commit(pw)

            writer.commit_page = crashing
            restore = lambda: setattr(writer, "commit_page", real_commit)  # noqa: E731
        elif crash_confirm:
            base_runner = self.fs.runner()
            calls = {"n": 0}

            def flaky(body):
                calls["n"] += 1
                if calls["n"] == 2:   # claim→(kill)confirm
                    raise RuntimeError("kill: CONFIRMED 直前")
                return base_runner(body)

            runner = flaky

        try:
            outcome = run_headless(writer, self.new_ledger(runner), page_yields,
                                   self.base, self.uploader)
        finally:
            if restore is not None:
                restore()

        if self.reporter is not None:
            label, expire = main._report_headless_outcome(
                self.reporter, self.base, lease_epoch, outcome, file_id, cycle)
            self.report_log.append((label, expire))

        return outcome

    def landed_rows(self):
        return self.writer.sheets.get(_TAB, [])

    def landed_row_count(self):
        return len(self.landed_rows())

    def append_calls(self):
        return self.writer.append_calls
