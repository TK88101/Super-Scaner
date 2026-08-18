"""T3-f: 混載フォルダの頁単位分流と、タブが割れないこと。

Plan: `docs/plans/2026-08-17-t3-page-ocr-resolver.md` §5 T3-f / AD-T3-1。

nimoca（交通系IC）の券面はクレカ明細と**同じ Drive フォルダに混載**される
（趙裁定 5。`FOLDER_TRANSIT_IC_ID` には永久に値を置かない）。したがって
「どの prompt / builder を使うか」はフォルダでは決まらず頁ごとに決まる。

本ファイルが固定する 2 つの契約:

**① prompt と builder は頁ごとに切り替わり、必ず同源**
   nimoca 頁はクレカフォルダにあっても transit_ic の prompt で読まれ、
   transit_ic の builder で組まれる。片方だけ切り替わると、Gemini が
   ある型の構造で返した JSON を別の型の builder が読むことになる。

**② それでもタブは 1 ファイル 1 つ（folder doc_type に従う）**
   タブ名は `sheets_output.start_new_file` / `append_entries` が
   `main.process_file` から渡された folder doc_type で決める。ここが頁ごとに
   割れると、1 つの原票が 2 つのタブに分かれ、取引No の採番も分割 PDF の
   保存先も割れる（D1 §5.3）。`actual_doc_type` の作用範囲は
   **prompt と builder だけ**であって、書込先ではない。

venv311 必須:
    venv311/bin/python -m unittest test_ocr_engine_mixed_folder -v
"""
import contextlib
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import main
import ocr_engine
from doc_types import DocType
from ocr_test_fixtures import AMEX_HEAD, NIMOCA_HEAD, ic_rows, temp_pdf_path


_AMEX_TEXT = AMEX_HEAD
_NIMOCA_TEXT = NIMOCA_HEAD + " " + ic_rows(20)

# prompt はスタブ段階では 2 型とも同一文字列なので、識別できるよう差し替える
_CC_PROMPT = "PROMPT_FOR_CREDIT_CARD"
_IC_PROMPT = "PROMPT_FOR_TRANSIT_IC"

_MINIMAL_RAW = {"date": "2026/08/17", "vendor": "テスト", "entries": []}


def _mixed_two_pages():
    """p1=クレカ明細 / p2=nimoca 履歴 の 2 頁 PDF。"""
    return iter([
        {"page_num": 1, "total_pages": 2, "data": b"%PDF-p1", "filename": "m_p1.pdf"},
        {"page_num": 2, "total_pages": 2, "data": b"%PDF-p2", "filename": "m_p2.pdf"},
    ])


def _patched_prompts():
    """2 型の prompt を識別可能な別文字列にした PROMPTS。"""
    prompts = dict(ocr_engine.PROMPTS)
    prompts[DocType.CREDIT_CARD] = _CC_PROMPT
    prompts[DocType.TRANSIT_IC] = _IC_PROMPT
    return prompts


@contextlib.contextmanager
def _mixed_folder_run(*extra_patches):
    """混載 2 頁を流すための土台（3 つのテストで共有する）。

    p1 にクレカ明細・p2 に nimoca 履歴の OCR テキストを順に返し、
    prompt は 2 型を識別できる別文字列に差し替える。

    Yields:
        (path, cross_validate_mock) — path は使い捨て PDF のパス。
    """
    ocr_texts = iter([(_AMEX_TEXT, 0.95), (_NIMOCA_TEXT, 0.95)])
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(ocr_engine, "PROMPTS", _patched_prompts()))
        stack.enter_context(
            mock.patch.object(ocr_engine, "_split_pdf_pages",
                              return_value=_mixed_two_pages()))
        stack.enter_context(
            mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                              side_effect=lambda *a, **kw: next(ocr_texts)))
        cross = stack.enter_context(
            mock.patch.object(ocr_engine, "_call_gemini_cross_validate",
                              return_value=dict(_MINIMAL_RAW)))
        for patcher in extra_patches:
            stack.enter_context(patcher)
        path = stack.enter_context(temp_pdf_path())
        stack.enter_context(redirect_stdout(io.StringIO()))
        yield path, cross


class PromptAndBuilderSwitchPerPageTest(unittest.TestCase):
    """① 頁ごとに prompt / builder が切り替わり、両者が同源であること。"""

    def _run(self):
        """Returns: (cross_validate_mock, _yield_page_results が受けた doc_type の順リスト)"""
        yield_calls = []
        real_yield = ocr_engine._yield_page_results

        def recording_yield(doc_type, *a, **kw):
            yield_calls.append(doc_type)
            for entry in real_yield(doc_type, *a, **kw):
                yield entry

        recorder = mock.patch.object(ocr_engine, "_yield_page_results",
                                     side_effect=recording_yield)
        with _mixed_folder_run(recorder) as (path, cross):
            list(ocr_engine.process_pipeline(
                path, doc_type=DocType.CREDIT_CARD, ocr_strategy="C"))
        return cross, yield_calls

    def test_each_page_gets_the_prompt_of_its_own_family(self):
        # Arrange / Act
        cross, _ = self._run()

        # Assert: p1 はクレカ prompt、p2 は nimoca prompt で Gemini が呼ばれる
        self.assertEqual(cross.call_count, 2)
        prompts_used = [c.args[3] for c in cross.call_args_list]
        self.assertEqual(prompts_used, [_CC_PROMPT, _IC_PROMPT])

    def test_builder_doc_type_follows_the_prompt(self):
        # Arrange / Act
        _, yield_calls = self._run()

        # Assert: builder 側も頁ごとに切り替わる（prompt と同源）
        self.assertEqual(
            yield_calls, [DocType.CREDIT_CARD, DocType.TRANSIT_IC],
            "prompt と builder が別々の doc_type になっている（異種混成）")


class _DocTypeRecordingWriter:
    """書込先の決定に使われた doc_type だけを記録する sheets_writer 代役。

    `start_new_file` は `main.process_file` からは呼ばれない（呼ぶのは 1 つ上の
    `main()` のポーリングループ）。それでも定義してあるのは、将来 process_file が
    呼び始めたとき AttributeError で落ちるのではなく記録されるようにするため。
    「呼ばれていない」こと自体は下のテストが明示的に固定する。
    """

    def __init__(self):
        self.entries_doc_types = []
        self.start_doc_types = []

    def start_new_file(self, employee_name, doc_type, filename=""):
        self.start_doc_types.append(doc_type)

    def append_entries(self, employee_name, doc_type, entries_data, source_url):
        self.entries_doc_types.append(doc_type)

    def append_audit_row(self, filename, page_num, verdict, reason,
                         ocr_text_len, source_url=""):
        pass


class TabDoesNotSplitAcrossOneFileTest(unittest.TestCase):
    """② 混載で内部が割れてもタブは割れない（AD-T3-1）。

    検証点を `main.process_file` 層に置いているのが要点。タブ名は
    `process_pipeline` の yield 形では決まっていない —— `sheets_output` が
    `main` から渡された folder doc_type で決めている。yield の形を見張っても、
    将来 `main` が `result["doc_type"]` を読んでタブを決め始めたら検出できない。
    """

    def _run_process_file(self):
        writer = _DocTypeRecordingWriter()
        notifier = mock.patch.object(main, "send_notification")
        resolver = mock.patch.object(main, "PageUrlResolver")
        with _mixed_folder_run(notifier, resolver) as (path, _cross):
            main.PageUrlResolver.return_value.resolve.return_value = (
                "https://example/doc")
            main.process_file(
                service=mock.MagicMock(),
                sheets_writer=writer,
                file_path=path,
                uploader_name="テスト社員",
                chat_id="",
                doc_type=DocType.CREDIT_CARD,
            )
        return writer

    def test_all_writes_use_the_folder_doc_type(self):
        # Arrange / Act
        writer = self._run_process_file()

        # Assert: 内部で頁が transit_ic として解析されても、書込先の判断は
        # 全件 folder doc_type（＝1 ファイル 1 タブ）
        self.assertEqual(len(writer.entries_doc_types), 2,
                         "混載 2 頁が両方とも書き込まれていない")
        self.assertEqual(
            set(writer.entries_doc_types), {DocType.CREDIT_CARD},
            "書込先の doc_type が頁ごとに割れている（タブが分裂する）")

    def test_process_file_does_not_open_the_tab_itself(self):
        # Arrange / Act: タブを開くのは main() のポーリングループの仕事で、
        # process_file ではない。ここが変わると「1 ファイル 1 タブ」を
        # 守っている場所が動くので、事実として固定しておく。
        writer = self._run_process_file()

        # Assert
        self.assertEqual(writer.start_doc_types, [])

    def test_result_doc_type_is_the_builder_doc_type(self):
        # Arrange: payload 内部の result["doc_type"] は builder 側の値になる。
        # T6 以降 sheets_output が異常検知の抑制でこの键を読むので（混載
        # nimoca の橙を抑えるのに folder ではなく実種別が要る）、意味が
        # ずれると抑制が無音で漏れる。「フォルダ種別」と誤読してタブ選択に
        # 使う事故も併せて防ぐため、意味をここで固定しておく。
        with _mixed_folder_run() as (path, _cross):
            # Act
            pages = list(ocr_engine.process_pipeline(
                path, doc_type=DocType.CREDIT_CARD, ocr_strategy="C"))

        # Assert
        by_page = {p["page_num"]: p["result"] for p in pages}
        self.assertEqual(by_page[1].get("doc_type"), DocType.CREDIT_CARD)
        self.assertEqual(by_page[2].get("doc_type"), DocType.TRANSIT_IC)


if __name__ == "__main__":
    unittest.main()
