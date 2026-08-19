"""`local_test.process_local_file` の監査留痕を `main.process_file` と揃える。

## なぜ要るのか

`process_pipeline` の消費者は `main.process_file` と `local_test` の 2 つ。
`main` 側だけに留痕を実装して `local_test` を直し忘れる——というのは
IP-401 で実際に起きた事故の様式そのもの（`test_pipeline_consumers.py` の
冒頭に記録がある）。あのときは `_excluded_page` で踏み、構造テストで
「参照していること」だけを縛った。だが**参照の有無**を縛っても
**振る舞いが同じか**は縛れない。

本ファイルは残り 2 つの穴を振る舞いレベルで固定する:

| 穴 | 塞がないと起きること | `main` 側 |
|---|---|---|
| `_audit_signal` 未読 | 截断→救済された頁（`salvaged:N/M`）は `card_salvage.page_marks` の規定で**監査タブにしか痕跡が出ない**。読まなければその頁は「綺麗に成功した頁」と見分けが付かない | `main.py:596-608` |
| 頁カバレッジ突合なし | 32 頁のうち 31 頁しか出力されなくても成功扱いで歸檔。枚数を数えるまで気づけない | `main.py:617-635` |

どちらも**真票の結果を額面どおり読めなくする**穴であり、
「読み取り精度を見て T8〜T10 を決める」という使い方を無効化する。

## `main` と意図的に違う 1 点 —— カバレッジの起点

`main.process_file` は `range(1, last_total_pages + 1)` で欠落を出すが、
`local_test` は `--start-page N` を持つ（`main` は `start_page` を渡さない。
`docs/plans/2026-08-17-split-pdf-midway-failure.md` の G8 参照）。
`1` 起点を写経すると **`--start-page 3` のたびに p1・p2 が「欠落」として
監査タブへ書かれる**。留痕の語義は同じ（掃いた範囲の頁を落とさない）まま、
起点だけ実際の走査範囲に合わせる。

    venv311/bin/python -m unittest test_local_test_page_audit -v
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import local_test
from doc_types import DocType
from sheets_output import (AUDIT_VERDICT_BRANCH, AUDIT_VERDICT_EXCLUDED,
                           AUDIT_VERDICT_MISSING)


class _RecordingWriter:
    """`sheets_writer` の代役。MF 書込と監査書込を時系列でも記録する。

    別々のリストだけだと「MF が先・監査が後」を主張するテストが順序を
    入れ替えても通ってしまう（`test_main_process_file._RecordingWriter`
    と同じ理由で `events` を持つ）。
    """

    def __init__(self, audit_error=None, entries_error=None):
        self.entries_calls = []
        self.audit_calls = []
        self.events = []
        self._audit_error = audit_error
        self._entries_error = entries_error

    def start_new_file(self, employee_name, doc_type, file_name):
        self.events.append("start_new_file")

    def append_entries(self, employee_name, doc_type, entries_data, source_url):
        self.entries_calls.append(entries_data)
        self.events.append("entries")
        if self._entries_error:
            raise self._entries_error

    def append_audit_row(self, filename, page_num, verdict, reason,
                         ocr_text_len, source_url=""):
        self.events.append(f"audit:{verdict}")
        self.audit_calls.append({
            "filename": filename, "page_num": page_num, "verdict": verdict,
            "reason": reason, "ocr_text_len": ocr_text_len,
            "source_url": source_url,
        })
        if self._audit_error:
            raise self._audit_error

    def rows_with_verdict(self, verdict):
        return [c for c in self.audit_calls if c["verdict"] == verdict]


def _page(result, page_num=1, total_pages=1):
    return {"result": result, "page_num": page_num, "total_pages": total_pages,
            "page_bytes": None}


def _bookable(**overrides):
    result = {
        "date": "2026/08/19",
        "vendor": "テストカード",
        "entries": [{"debit_account": "旅費交通費", "amount": 200}],
    }
    result.update(overrides)
    return result


def _page_error():
    return {"entries": [], "_page_error": True, "_unrecognized": True,
            "memo": "AI応答のJSON解析失敗"}


def _excluded():
    return {"entries": [], "_excluded_page": True, "_exclude_reason": "envelope",
            "_exclude_destination": local_test.EXCLUDE_DEST_AUDIT_TAB,
            "_ocr_text_len": 42}


def _excluded_to_mf():
    """社会保険料通知書のように MF タブへ提示行を出す除外ページ。"""
    return {"entries": [], "_excluded_page": True,
            "_exclude_reason": "social_insurance_notice",
            "_exclude_destination": local_test.EXCLUDE_DEST_MF_TAB,
            "memo": "社会保険料通知書のため仕訳は作成しません",
            "_ocr_text_len": 77}


def _unrecognized_rows(writer):
    return [c for c in writer.entries_calls if c.get("_unrecognized")]


def _run(pages, start_page=1, writer=None, file_name="card.pdf"):
    """`process_local_file` を偽 pipeline で駆動し `(戻り値, writer)` を返す。

    `shutil.move` は本物の歸檔を起こさないよう差し替える（テストは
    振り分け語義だけを見る。実ファイルを作ると温存すべき真票を
    誤って動かす事故に繋がる）。
    """
    writer = writer or _RecordingWriter()
    file_info = {"path": os.path.join("/tmp", file_name), "name": file_name,
                 "doc_type": DocType.CREDIT_CARD}
    buf = io.StringIO()
    with mock.patch.object(local_test, "process_pipeline",
                           return_value=iter(pages)), \
            mock.patch.object(local_test.shutil, "move"), \
            redirect_stdout(buf):
        ok = local_test.process_local_file(
            file_info, writer, start_page=start_page)
    writer.stdout = buf.getvalue()
    return ok, writer


class AuditSignalIsRecordedTest(unittest.TestCase):
    """穴 1: `_audit_signal` を監査タブへ「分岐」1 行で残すこと。"""

    def test_an_audit_signal_produces_a_branch_row(self):
        """`salvaged:*` 等のシグナルが監査タブに落ちること。

        これが無いと、Gemini 応答が截断され救済で復元された頁が
        「綺麗に成功した頁」と区別できない（MF 側には痕跡が出ない）。
        """
        # シグナルは **4 頁目**に載せる。1 頁ファイルの 1 頁目でしか試さないと
        # `page_num=page_num` と `page_num=1` の区別が付かず、頁番号を
        # ハードコードする変異が生き残る（評審で実測。4 は 1 とも
        # 総頁数 5 とも一致しないので、どの取り違えでも落ちる）。
        # 頁番号は「どの頁を読み直せばよいか」を伝える唯一の欄。
        pages = [_page(_bookable(), n, 5) for n in (1, 2, 3)]
        pages.append(_page(_bookable(_audit_signal="salvaged:60/62",
                                     _ocr_text_len=1234), 4, 5))
        pages.append(_page(_bookable(), 5, 5))
        ok, writer = _run(pages)

        self.assertTrue(ok)
        rows = writer.rows_with_verdict(AUDIT_VERDICT_BRANCH)
        self.assertEqual(len(rows), 1, "分岐行が 1 本も出ていない")
        self.assertEqual(rows[0]["reason"], "salvaged:60/62")
        self.assertEqual(rows[0]["page_num"], 4, "頁番号が実際の頁を指していない")
        self.assertEqual(rows[0]["filename"], "card.pdf")
        self.assertEqual(rows[0]["ocr_text_len"], 1234)

    def test_the_journal_is_written_before_the_audit_row(self):
        """MF が先・監査が後（帳簿を人質に取らせない。`main.py:595` と同語義）。"""
        pages = [_page(_bookable(_audit_signal="salvaged:60/62"))]
        _, writer = _run(pages)

        self.assertIn("entries", writer.events)
        self.assertIn(f"audit:{AUDIT_VERDICT_BRANCH}", writer.events)
        self.assertLess(
            writer.events.index("entries"),
            writer.events.index(f"audit:{AUDIT_VERDICT_BRANCH}"),
            "監査行が MF 書込より先に出ている")

    def test_a_page_without_a_signal_produces_no_branch_row(self):
        """シグナルが無い頁で分岐行を出さないこと（過剰記録の否定対照）。"""
        _, writer = _run([_page(_bookable())])
        self.assertEqual(writer.rows_with_verdict(AUDIT_VERDICT_BRANCH), [])

    def test_an_empty_signal_produces_no_branch_row(self):
        """空文字のシグナルで分岐行を出さないこと。

        `if audit_signal:` の truthy 判定を `is not None` へ変えた変異は、
        キーが在って値が空の頁に理由欄が空の分岐行を出す。監査タブが
        意味の無い行で埋まると、本物の分岐が埋もれる。
        """
        _, writer = _run([_page(_bookable(_audit_signal=""))])
        self.assertEqual(writer.rows_with_verdict(AUDIT_VERDICT_BRANCH), [])

    def test_a_failed_audit_write_does_not_break_the_page(self):
        """監査書込が失敗しても記帳は成功のままにすること。

        MF は既に正しく書けており帳簿は正しい（`main.py:607` の §3.7 語義）。
        ここで例外を伝播させると 1 頁の監査失敗がファイル全体を Failed に
        落とし、再試行で MF 行が重複増殖する。
        """
        writer = _RecordingWriter(audit_error=RuntimeError("boom"))
        pages = [_page(_bookable(_audit_signal="salvaged:60/62"))]
        ok, writer = _run(pages, writer=writer)

        self.assertTrue(ok, "監査書込の失敗でファイルごと Failed になっている")
        self.assertEqual(len(writer.entries_calls), 1)


class PageCoverageIsReconciledTest(unittest.TestCase):
    """穴 2: 掃いた範囲の頁が 1 つでも出力されなければ「欠落」を残すこと。"""

    def test_a_missing_page_produces_a_missing_row(self):
        """p2 が一度も yield されなければ監査タブに残ること。"""
        pages = [_page(_bookable(), 1, 3), _page(_bookable(), 3, 3)]
        ok, writer = _run(pages)

        rows = writer.rows_with_verdict(AUDIT_VERDICT_MISSING)
        self.assertEqual(len(rows), 1, "欠落行が出ていない")
        self.assertEqual(rows[0]["reason"], "page_coverage_gap:[2]")
        self.assertEqual(rows[0]["page_num"], 2)
        self.assertEqual(rows[0]["filename"], "card.pdf")
        self.assertEqual(rows[0]["ocr_text_len"], 0)
        self.assertTrue(ok, "カバレッジ欠落で成否判定を変えてはいけない")

    def test_multiple_missing_pages_are_all_named(self):
        """欠落が複数のとき、全部を reason に載せ page_num は先頭にすること。

        単頁の欠落だけを試すと先頭と末尾が同じ値になるので、
        `missing_pages[-1]` へ変えた変異も、複数頁のときだけ書式が崩れる
        変異も生き残る（Codex 評審で指摘）。
        """
        pages = [_page(_bookable(), 1, 5), _page(_bookable(), 4, 5)]
        _, writer = _run(pages)

        rows = writer.rows_with_verdict(AUDIT_VERDICT_MISSING)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "page_coverage_gap:[2, 3, 5]")
        self.assertEqual(rows[0]["page_num"], 2, "page_num が先頭欠落頁でない")

    def test_a_complete_run_produces_no_missing_row(self):
        """全頁揃っていれば欠落行を出さないこと（誤報の否定対照）。"""
        pages = [_page(_bookable(), n, 3) for n in (1, 2, 3)]
        _, writer = _run(pages)
        self.assertEqual(writer.rows_with_verdict(AUDIT_VERDICT_MISSING), [])

    def test_error_and_excluded_pages_still_count_as_seen(self):
        """`_page_error` / `_excluded_page` の頁を欠落と誤報しないこと。

        これらは「出力された」頁である（失敗も除外も痕跡が残る）。
        seen に数えないと、封筒が 1 枚混ざるだけで毎回欠落行が出る。
        """
        pages = [_page(_bookable(), 1, 3), _page(_page_error(), 2, 3),
                 _page(_excluded(), 3, 3)]
        _, writer = _run(pages)

        self.assertEqual(writer.rows_with_verdict(AUDIT_VERDICT_MISSING), [])
        # 除外頁の監査行は従来どおり出ていること（回帰保護）
        self.assertEqual(len(writer.rows_with_verdict(AUDIT_VERDICT_EXCLUDED)), 1)

    def test_start_page_shifts_the_coverage_window(self):
        """`--start-page N` のとき p1..p(N-1) を欠落と誤報しないこと。

        **`main.process_file` から意図的にずらしている唯一の点**。
        `main` は `start_page` を渡さないので `range(1, ...)` で正しいが、
        `local_test` で写経すると `--start-page 3` のたびに p1・p2 が
        「欠落」として監査タブへ書かれ、留痕が狼少年になる。
        """
        pages = [_page(_bookable(), 3, 4), _page(_bookable(), 4, 4)]
        _, writer = _run(pages, start_page=3)
        self.assertEqual(
            writer.rows_with_verdict(AUDIT_VERDICT_MISSING), [],
            "start_page より前の頁を欠落と誤報している")

    def test_a_gap_at_the_very_first_page_is_detected(self):
        """**窓の下端**が欠けた場合も検出すること。

        既存の欠落 fixture は全部「窓の内側」に穴を開けていたため、
        `range(start_page + 1, ...)` という off-by-one 変異が生き残っていた
        （評審で実測）。1 頁目は消えやすい側であり、かつ 1 頁目が消えると
        「31 頁の仕訳を 32 頁の明細として読む」という IP-401 そのものの
        形になる。ここが番人の効く下端。
        """
        pages = [_page(_bookable(), 2, 3), _page(_bookable(), 3, 3)]
        _, writer = _run(pages)

        rows = writer.rows_with_verdict(AUDIT_VERDICT_MISSING)
        self.assertEqual(len(rows), 1, "先頭頁の欠落を見逃している")
        self.assertEqual(rows[0]["reason"], "page_coverage_gap:[1]")

    def test_a_gap_at_a_shifted_lower_bound_is_detected(self):
        """`--start-page N` のとき、**窓の下端 N 自身**の欠落も検出すること。

        上のテストは start_page=1 固定なので `range(1, ...)` に退化した
        実装でも通る。ここが「窓をずらす」という意図的乖離の下端を押さえる。
        """
        pages = [_page(_bookable(), 4, 5), _page(_bookable(), 5, 5)]
        _, writer = _run(pages, start_page=3)

        rows = writer.rows_with_verdict(AUDIT_VERDICT_MISSING)
        self.assertEqual(len(rows), 1, "窓の下端の欠落を見逃している")
        self.assertEqual(rows[0]["reason"], "page_coverage_gap:[3]")

    def test_total_pages_is_captured_even_on_pages_that_continue(self):
        """除外/失敗しか無いファイルでも総頁数を掴んでいること。

        既存の欠落 fixture は全部が正常頁で始まっていたため、
        `last_total_pages = total_pages` を `continue` の下へ動かす変異が
        生き残っていた（評審で実測）。全頁が封筒の PDF で末尾が消えると、
        総頁数が 0 のまま突合ごとスキップされ、無音欠落に戻る。
        """
        pages = [_page(_excluded(), 1, 3), _page(_excluded(), 2, 3)]
        _, writer = _run(pages)

        rows = writer.rows_with_verdict(AUDIT_VERDICT_MISSING)
        self.assertEqual(len(rows), 1, "全除外ファイルの末尾欠落を見逃している")
        self.assertEqual(rows[0]["reason"], "page_coverage_gap:[3]")

    def test_start_page_still_detects_a_gap_inside_the_window(self):
        """窓をずらしても、窓の**中**の欠落は見逃さないこと。

        上のテストだけだと「start_page 指定時は突合ごと無効化する」
        実装でも緑になる（穴を塞いだつもりで塞いでいない）。
        """
        pages = [_page(_bookable(), 3, 5), _page(_bookable(), 5, 5)]
        _, writer = _run(pages, start_page=3)

        rows = writer.rows_with_verdict(AUDIT_VERDICT_MISSING)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "page_coverage_gap:[4]")

    def test_a_failed_coverage_write_does_not_break_the_file(self):
        """カバレッジ行の書込失敗でファイルごと落とさないこと。"""
        writer = _RecordingWriter(audit_error=RuntimeError("boom"))
        pages = [_page(_bookable(), 1, 3), _page(_bookable(), 3, 3)]
        ok, writer = _run(pages, writer=writer)
        self.assertTrue(ok)

    def test_no_pages_at_all_produces_no_missing_row(self):
        """1 頁も yield されない場合に欠落行を出さないこと。

        総頁数が判らない（`last_total_pages` が 0）ので、何頁欠けたかを
        主張できない。ここは `count == 0` の「解析失敗」経路の担当。
        """
        ok, writer = _run([])
        self.assertFalse(ok)
        self.assertEqual(writer.rows_with_verdict(AUDIT_VERDICT_MISSING), [])


class ExcludedPageWriteFailureTest(unittest.TestCase):
    """穴 3: 除外ページの書込が失敗したときの語義を `main` と揃えること。

    `main._record_excluded_page`（main.py:355-417）は行き先ごとに違う扱いを
    する。`local_test` は裸で呼んでいたため、どちらの失敗も
    `process_local_file` ごと例外で落ちていた —— **除外ページの留痕が
    消えたうえに、そのファイルの他の頁の処理も止まる**。

    | 行き先 | 書込失敗時の `main` の扱い |
    |---|---|
    | 監査タブ（封筒） | MF の認識不能行へ**退避**して必ず可視化。頁は除外として成立 |
    | MF タブ（社保通知書） | その行が頁の**唯一の出力**なので諦めない。頁を失敗として数え、再試行に載せる |
    """

    def test_an_audit_write_failure_falls_back_to_an_mf_row(self):
        """封筒の監査書込が落ちたら MF の認識不能行へ退避すること。

        監査タブは真の除外ページにとって唯一の留痕。握り潰すと
        IP-401 の無音欠落に逆戻りする。
        """
        writer = _RecordingWriter(audit_error=RuntimeError("boom"))
        ok, writer = _run([_page(_excluded())], writer=writer)

        self.assertTrue(ok, "監査書込の失敗でファイルごと落ちている")
        rows = _unrecognized_rows(writer)
        self.assertEqual(len(rows), 1, "退避行が MF 区に出ていない")
        self.assertIn("p1", rows[0]["memo"])
        self.assertIn("envelope", rows[0]["memo"])

    def test_a_failed_fallback_still_does_not_raise(self):
        """退避行まで落ちても例外を投げないこと（それ以上の保底経路は無い）。

        除外ページだけを流す。正常頁を混ぜると `entries_error` が
        **通常の記帳**まで壊してしまい、この経路を測れない
        （通常記帳の失敗は握り潰さないのが正しい —— 帳簿が書けないのは
        本物の失敗であり、`main` も try/except を掛けていない）。
        """
        writer = _RecordingWriter(audit_error=RuntimeError("boom"),
                                  entries_error=RuntimeError("also boom"))
        ok, writer = _run([_page(_excluded())], writer=writer)

        self.assertTrue(ok, "退避行の失敗でファイルごと落ちている")
        # 退避を試みた形跡は残っていること（諦めて何もしないのは不可）
        self.assertEqual(len(_unrecognized_rows(writer)), 1)

    def test_a_normal_journal_write_failure_still_propagates(self):
        """通常の記帳失敗は握り潰さないこと（上の緩和が広がっていない対照）。

        除外ページの書込を寛容にしたぶん、通常の MF 書込まで寛容に
        なっていないかを固定する。帳簿が書けないのは本物の失敗。
        """
        writer = _RecordingWriter(entries_error=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            _run([_page(_bookable())], writer=writer)

    def test_an_mf_destination_write_failure_counts_the_page_as_failed(self):
        """社保通知書の提示行が書けなければ、その頁を失敗として数えること。

        提示行はその頁の唯一の出力。除外として成立させて歸檔すると、
        顧客は「上げたこと」も「記帳されなかったこと」も知る術が無くなる
        （`main.py:392-398` の語義）。
        """
        writer = _RecordingWriter(entries_error=RuntimeError("boom"))
        ok, writer = _run([_page(_excluded_to_mf())], writer=writer)

        self.assertFalse(
            ok, "唯一の出力が書けなかったのに Success（歸檔）になっている")

    def test_a_successful_excluded_page_is_unaffected(self):
        """成功時の振る舞いは従来どおり（回帰保護）。"""
        ok, writer = _run([_page(_bookable(), 1, 2), _page(_excluded(), 2, 2)])

        self.assertTrue(ok)
        self.assertEqual(len(writer.rows_with_verdict(AUDIT_VERDICT_EXCLUDED)), 1)
        self.assertEqual(_unrecognized_rows(writer), [],
                         "成功しているのに退避行が出ている")

    def test_the_excluded_summary_names_the_pages(self):
        """除外の内訳がコンソールに出ること（`main.py:679-681` と同形式）。

        これが無いと `excluded_pages` は数えるだけで誰も読まない死変数になり、
        「32 頁のうち何頁が封筒として除外されたか」を結果から読めない。
        """
        pages = [_page(_bookable(), 1, 3), _page(_excluded(), 2, 3),
                 _page(_excluded(), 3, 3)]
        _, writer = _run(pages)
        self.assertIn("📨 除外ページ: 2/3頁 [p2,p3]", writer.stdout)

    def test_a_page_that_could_not_be_recorded_is_not_counted_as_excluded(self):
        """留痕を残せなかった除外を「除外できた」と数えないこと。

        数えると内訳が実態より多くなり、「除外 1 頁」と表示されているのに
        監査タブにもMF区にも何も無い、という読み違いを生む。
        """
        writer = _RecordingWriter(entries_error=RuntimeError("boom"))
        ok, writer = _run([_page(_excluded_to_mf())], writer=writer)

        self.assertFalse(ok)
        self.assertNotIn("📨 除外ページ", writer.stdout)


class AuditWriteFailureIsNotNarrowedTest(unittest.TestCase):
    """例外語義が `RuntimeError` 専用に狭まっていないこと。

    `RuntimeError` だけでテストすると `except RuntimeError:` へ狭める変異が
    生き残る。gspread が投げるのは `APIError` であって `RuntimeError` では
    ないので、狭めた瞬間に本番相当の失敗が素通りで例外化する。
    """

    def test_a_branch_row_failure_of_any_exception_type_is_tolerated(self):
        writer = _RecordingWriter(audit_error=ValueError("not a RuntimeError"))
        ok, _ = _run([_page(_bookable(_audit_signal="salvaged:60/62"))],
                     writer=writer)
        self.assertTrue(ok)

    def test_a_coverage_row_failure_of_any_exception_type_is_tolerated(self):
        writer = _RecordingWriter(audit_error=ValueError("not a RuntimeError"))
        ok, _ = _run([_page(_bookable(), 1, 3), _page(_bookable(), 3, 3)],
                     writer=writer)
        self.assertTrue(ok)

    def test_an_excluded_audit_failure_of_any_exception_type_falls_back(self):
        writer = _RecordingWriter(audit_error=ValueError("not a RuntimeError"))
        ok, writer = _run([_page(_excluded())], writer=writer)
        self.assertTrue(ok)
        self.assertEqual(len(_unrecognized_rows(writer)), 1)


class ParityWithMainIsDeclaredTest(unittest.TestCase):
    """`main.process_file` と同じ監査語義を使っていることの構造保護。

    振る舞いテストは「今の実装が正しい」ことしか言えない。片方の
    verdict 定数が将来変わったとき、両者が別々の文字列を書き始めても
    上のテストは自分の期待値ごと書き換えられて緑のままになりうる。
    定数の**出所が 1 つ**であることをここで固定する。
    """

    def test_both_consumers_import_the_verdict_constants_from_sheets_output(self):
        import ast
        import pathlib

        for module in ("main.py", "local_test.py"):
            with self.subTest(module=module):
                source = pathlib.Path(
                    os.path.dirname(os.path.abspath(__file__)),
                    module).read_text(encoding="utf-8")
                imported = set()
                for node in ast.walk(ast.parse(source)):
                    if (isinstance(node, ast.ImportFrom)
                            and node.module == "sheets_output"):
                        imported.update(a.name for a in node.names)
                for const in ("AUDIT_VERDICT_BRANCH", "AUDIT_VERDICT_MISSING"):
                    self.assertIn(
                        const, imported,
                        f"{module} が {const} を sheets_output から取っていない。"
                        "verdict 文字列を各自で持つと監査タブの語彙が割れる")


if __name__ == "__main__":
    unittest.main()
