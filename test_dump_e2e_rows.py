"""`dump_e2e_rows`（T11 の E2E 観測 driver）の単体テスト。

**この driver が守らねばならない性質は 6 つある**（Plan
`docs/plans/2026-08-21-t11-e2e-machine-verdict.md` §4 U1 の DoD a〜f）。
どれも「壊れても実行は成功し、出力もそれらしく見える」種類なので、
テストで固定しないと無音でずれる。

- (a) 真の Sheets へ 1 バイトも出さない。`SheetsOutputWriter.__init__` を
  呼ばないだけでは不十分（別経路・flush・module 初期化時の接続がある）
- (b) patch 先は `local_test.process_pipeline`。`local_test.py:41` は
  `from ocr_engine import process_pipeline` でローカル束縛しているので、
  `ocr_engine.process_pipeline` を包んでも**捕捉できない**
- (c) 製品の `local_test.process_local_file` を実際に駆動する。
  自作の擬似フローを測ると死経路の観測になる
- (d) 頁 provenance は generator の状態で決める。`local_test` の
  writer 呼出のうち 264/281/308/343 行は `for page` 循環の**外**にあり、
  「直前に yield した頁」で紐付けると前頁・前ファイルへ誤配される
- (e) 期待頁数は PDF から独立に取る。pipeline が 1 頁も yield しないと
  観測側は空集合になり、`>= 1` 系の判定が vacuous PASS する
- (f) `TEST_DIR` と `PROCESSED_DIR` を**両方**差し替える。後者は import 時に
  前者から算出済みなので、片方だけだと原票が repo 内へ退避される
"""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dump_e2e_rows as D
import local_test
from doc_types import DocType


def _page(page_num, total_pages, entries=None, **extra):
    """`process_pipeline` が yield する形。"""
    result = {
        "date": "2026/04/15",
        "vendor": "テスト取引先",
        "invoice_num": "",
        "memo": "",
        "entries": entries if entries is not None else [
            {"amount": 1000, "debit_account": "備品・消耗品費",
             "debit_tax_type": "課対仕入10%", "description": "商品",
             "credit_account": "未払金"},
        ],
    }
    result.update(extra)
    return {"result": result, "page_num": page_num,
            "total_pages": total_pages, "page_bytes": b""}


def _fake_pipeline(pages):
    """`local_test.process_pipeline` の代役を作る。"""
    def _gen(file_path, doc_type=None, ocr_strategy=None, start_page=1):
        for p in pages:
            yield p
    return _gen


def _make_pdf(path, page_count):
    """指定頁数の空 PDF を作る（期待頁数の独立取得を検証するため）。"""
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(page_count):
        w.add_blank_page(width=200, height=200)
    with open(path, "wb") as f:
        w.write(f)


class NeverTouchesRealSheetsTest(unittest.TestCase):
    """(a) 真の Sheets へ到達する経路が全て塞がれている。"""

    def test_constructing_the_real_writer_raises(self):
        sink = D.EventSink()
        with D.dumping_run(tempfile.mkdtemp(), sink):
            with self.assertRaises(Exception):
                from sheets_output import SheetsOutputWriter
                SheetsOutputWriter("dummy-id", "dummy.json")

    def test_gspread_entry_points_raise(self):
        import gspread
        sink = D.EventSink()
        with D.dumping_run(tempfile.mkdtemp(), sink):
            with self.assertRaises(Exception):
                gspread.service_account(filename="dummy.json")
            with self.assertRaises(Exception):
                gspread.authorize(None)

    def test_spies_are_restored_after_the_context_exits(self):
        import gspread
        before = gspread.service_account
        with D.dumping_run(tempfile.mkdtemp(), D.EventSink()):
            pass
        self.assertIs(gspread.service_account, before,
                      "context を抜けたら元に戻すこと（他テストへ漏らさない）")


class PatchTargetTest(unittest.TestCase):
    """(b) patch 先は `local_test.process_pipeline`。"""

    def test_patches_the_name_local_test_actually_calls(self):
        import ocr_engine
        original = local_test.process_pipeline
        with D.dumping_run(tempfile.mkdtemp(), D.EventSink()):
            self.assertIsNot(local_test.process_pipeline, original,
                             "local_test 側の名前が差し替わっていない")
        self.assertIs(local_test.process_pipeline, original,
                      "context を抜けたら復元すること")
        self.assertIsNotNone(ocr_engine.process_pipeline)

    def test_yield_count_is_observed(self):
        """捕捉数 0 は「patch が外れている」と区別が付かないので明示検証する。"""
        with tempfile.TemporaryDirectory() as tmp:
            _stage_one_file(tmp)
            sink = D.EventSink()
            # pipeline の差し替えは dumping_run の**外側**。内側で差し替えると
            # wrapper を上書きして観測が無音で消える（`run_one_file` の番人が
            # 検出する。`WrapperDetachmentGuardTest` 参照）。
            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 2), _page(2, 2)])):
                with D.dumping_run(tmp, sink):
                    D.run_one_file(_file_info(tmp), sink)
            yields = [e for e in sink.events if e["kind"] == "page_yield"]
            self.assertEqual(len(yields), 2)


class DrivesTheRealProductFlowTest(unittest.TestCase):
    """(c) 製品の `process_local_file` を実際に呼ぶ。"""

    def test_calls_local_test_process_local_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            _stage_one_file(tmp)
            sink = D.EventSink()
            seen = []
            real = local_test.process_local_file

            def spy(*a, **kw):
                seen.append(a)
                return real(*a, **kw)

            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 1)])):
                with D.dumping_run(tmp, sink):
                    with mock.patch.object(local_test, "process_local_file",
                                           spy):
                        D.run_one_file(_file_info(tmp), sink)
            self.assertEqual(len(seen), 1,
                             "製品の process_local_file を通っていない")


class PageProvenanceTest(unittest.TestCase):
    """(d) 頁内の書込と頁外の書込を取り違えない。"""

    def test_rows_written_inside_the_loop_carry_the_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            _stage_one_file(tmp)
            sink = D.EventSink()
            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 2), _page(2, 2)])):
                with D.dumping_run(tmp, sink):
                    D.run_one_file(_file_info(tmp), sink)
            mf = [e for e in sink.events if e["kind"] == "mf_row"]
            self.assertTrue(mf, "MF 行が 1 つも観測されていない")
            self.assertTrue(all(e["scope"] == "page" for e in mf),
                            "頁内の書込が file scope になっている")
            self.assertEqual({e["page"] for e in mf}, {1, 2})

    def test_rows_written_after_the_generator_is_exhausted_have_no_page(self):
        """検算行（`card_file_recon.report_and_record`）は循環の外で書かれる。"""
        with tempfile.TemporaryDirectory() as tmp:
            _stage_one_file(tmp)
            sink = D.EventSink()

            def _after_loop(*a, **kw):
                # 循環を抜けた後に writer を触る製品経路の代役
                a[4].append_audit_row(filename="f", page_num=0,
                                      verdict="合計不一致", reason="x:1/2/3",
                                      ocr_text_len=0, source_url="")
                return mock.DEFAULT

            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 1)])):
                with D.dumping_run(tmp, sink):
                    with mock.patch("card_file_recon.report_and_record",
                                    side_effect=_after_loop):
                        D.run_one_file(_file_info(tmp), sink)
            audits = [e for e in sink.events if e["kind"] == "audit_row"]
            self.assertTrue(audits, "監査行が観測されていない")
            tail = audits[-1]
            self.assertEqual(tail["scope"], "file")
            self.assertIsNone(tail["page"],
                              "循環外の書込に頁番号を付けてはいけない")

    def test_audit_rows_keep_both_the_key_and_the_translation(self):
        """理由列は `audit_reason_ja` で訳される。判定はキー側で行う。"""
        with tempfile.TemporaryDirectory() as tmp:
            _stage_one_file(tmp)
            sink = D.EventSink()
            with mock.patch.object(
                    local_test, "process_pipeline",
                    _fake_pipeline([_page(1, 1, entries=[],
                                          _excluded_page=True,
                                          _exclude_reason="duplicate_page:1")])):
                with D.dumping_run(tmp, sink):
                    D.run_one_file(_file_info(tmp), sink)
            audits = [e for e in sink.events if e["kind"] == "audit_row"]
            self.assertTrue(audits)
            self.assertTrue(
                any(a["reason_key"].startswith("duplicate_page")
                    for a in audits),
                "機械可読キーが失われている")
            self.assertTrue(all("reason_ja" in a for a in audits),
                            "訳文も残すこと（人が読む欄）")


class ExpectedPagesTest(unittest.TestCase):
    """(e) 期待頁数は観測に依存しない源から取る。"""

    def test_reads_page_count_from_the_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.pdf")
            _make_pdf(path, 3)
            self.assertEqual(D.expected_page_count(path), 3)

    def test_images_count_as_one_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.jpg")
            io.open(path, "wb").write(b"\xff\xd8\xff\xe0not-a-real-jpeg")
            self.assertEqual(D.expected_page_count(path), 1)

    def test_expected_pages_survive_a_pipeline_that_yields_nothing(self):
        """全頁欠落（F15）。観測が空でも期待頁数は出ること。"""
        with tempfile.TemporaryDirectory() as tmp:
            _stage_one_file(tmp, page_count=4)
            sink = D.EventSink()
            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([])):
                with D.dumping_run(tmp, sink):
                    D.run_one_file(_file_info(tmp), sink)
            starts = [e for e in sink.events if e["kind"] == "file_start"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["expected_pages"], 4,
                             "観測 0 頁でも期待頁数は PDF から取れること")
            self.assertEqual(
                [e for e in sink.events if e["kind"] == "page_yield"], [],
                "前提: 1 頁も yield していない")


class StagingTest(unittest.TestCase):
    """(f) `TEST_DIR` と `PROCESSED_DIR` を両方差し替える。"""

    def test_both_directory_constants_are_redirected(self):
        with tempfile.TemporaryDirectory() as tmp:
            before_test_dir = local_test.TEST_DIR
            before_processed = local_test.PROCESSED_DIR
            with D.dumping_run(tmp, D.EventSink()):
                self.assertTrue(
                    os.path.abspath(local_test.TEST_DIR).startswith(
                        os.path.abspath(tmp)),
                    "TEST_DIR が一時領域を指していない")
                self.assertTrue(
                    os.path.abspath(local_test.PROCESSED_DIR).startswith(
                        os.path.abspath(tmp)),
                    "PROCESSED_DIR が repo 内を指したまま（原票が repo へ落ちる）")
            self.assertEqual(local_test.TEST_DIR, before_test_dir)
            self.assertEqual(local_test.PROCESSED_DIR, before_processed)

    def test_repo_test_images_is_untouched_by_a_run(self):
        repo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "test_images")
        before = _snapshot(repo_dir)
        with tempfile.TemporaryDirectory() as tmp:
            _stage_one_file(tmp)
            sink = D.EventSink()
            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 1)])):
                with D.dumping_run(tmp, sink):
                    D.run_one_file(_file_info(tmp), sink)
        self.assertEqual(_snapshot(repo_dir), before,
                         "repo 内 test_images/ が実行で変化した")


class DataRowClassificationTest(unittest.TestCase):
    """占位行を明細行と混ぜない。

    `append_entries` は書ける行が 0 本のとき「認識不能の占位行」を 1 本
    書く（`sheets_output.py:661`）。これは券面の明細ではないので、
    A6（負数）や A9（H列空欄・1万円タグ）の母集団に混ぜてはいけない。
    混ざると「H列が空である」が占位行のおかげで成立し、**判定が本来見る
    べき明細行を見ないまま緑になる**。
    """

    def test_placeholder_rows_are_flagged_as_non_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            _stage_one_file(tmp)
            sink = D.EventSink()
            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 1, entries=[])])):
                with D.dumping_run(tmp, sink):
                    D.run_one_file(_file_info(tmp), sink)
            mf = [e for e in sink.events if e["kind"] == "mf_row"]
            self.assertTrue(mf, "占位行すら観測できていない")
            self.assertTrue(all(not e["is_data_row"] for e in mf),
                            "占位行が明細行として数えられている")

    def test_normal_rows_are_flagged_as_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            _stage_one_file(tmp)
            sink = D.EventSink()
            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 1)])):
                with D.dumping_run(tmp, sink):
                    D.run_one_file(_file_info(tmp), sink)
            mf = [e for e in sink.events if e["kind"] == "mf_row"]
            self.assertTrue(mf)
            self.assertTrue(all(e["is_data_row"] for e in mf),
                            "明細行が占位行として除外されている")


class DriverFailureModeTest(unittest.TestCase):
    """観測器自身の失敗は、黙って空の観測にせず信号にする。"""

    def test_running_outside_the_context_is_rejected(self):
        with self.assertRaises(RuntimeError):
            D.run_one_file({"path": "x.pdf", "doc_type": "credit_card",
                            "name": "x.pdf"}, D.EventSink())

    def test_a_crashing_file_is_recorded_and_does_not_stop_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            _stage_one_file(tmp)
            sink = D.EventSink()
            boom = mock.Mock(side_effect=RuntimeError("boom"))
            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 1)])):
                with D.dumping_run(tmp, sink):
                    with mock.patch.object(local_test, "process_local_file",
                                           boom):
                        ok = D.run_one_file(_file_info(tmp), sink)
            self.assertFalse(ok)
            errors = [e for e in sink.events if e["kind"] == "file_error"]
            self.assertEqual(len(errors), 1)
            self.assertIn("boom", errors[0]["error"])
            self.assertTrue(
                [e for e in sink.events if e["kind"] == "file_end"],
                "落ちたファイルにも file_end を残すこと（観測の欠落と区別する）")

    def test_unreadable_pdf_reports_zero_expected_pages(self):
        """0 は「入力が壊れている」という判定材料。成功ではない。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.pdf")
            io.open(path, "wb").write(b"not a pdf at all")
            self.assertEqual(D.expected_page_count(path), 0)


class WrapperDetachmentGuardTest(unittest.TestCase):
    """wrapper が外れたら**黙って観測を失う**のではなく止まること。

    `dumping_run` を張った後に誰かが `local_test.process_pipeline` を差し替え
    ると、頁 provenance も `page_yield` も消えるが、実行は成功し出力も
    「行が無いファイル」として自然に見える。観測器が観測していないことに
    気付けない状態は、この Plan が潰そうとしている事故そのもの。
    """

    def test_raises_when_the_wrapper_was_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            _stage_one_file(tmp)
            sink = D.EventSink()
            with D.dumping_run(tmp, sink):
                with mock.patch.object(local_test, "process_pipeline",
                                       _fake_pipeline([_page(1, 1)])):
                    with self.assertRaises(RuntimeError) as cm:
                        D.run_one_file(_file_info(tmp), sink)
            self.assertIn("無音", str(cm.exception))


class EventSchemaTest(unittest.TestCase):
    """出力は 1 本の versioned なイベントストリーム。"""

    def test_every_event_carries_the_schema_version(self):
        sink = D.EventSink()
        sink.emit("file_start", file="a", doc_type=DocType.CREDIT_CARD,
                  expected_pages=1)
        self.assertEqual(sink.events[0]["v"], D.EVENT_VERSION)

    def test_writes_one_jsonl_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = D.EventSink()
            sink.emit("file_start", file="a", doc_type="x", expected_pages=1)
            out = os.path.join(tmp, "events.jsonl")
            sink.write(out)
            lines = io.open(out, encoding="utf-8").read().strip().split("\n")
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["kind"], "file_start")


# ── ヘルパ ──────────────────────────────────────────────

def _stage_one_file(tmp, page_count=1):
    """一時領域に credit_card/ を作り、PDF を 1 本置く。"""
    folder = os.path.join(tmp, "credit_card")
    os.makedirs(folder, exist_ok=True)
    os.makedirs(os.path.join(tmp, "processed"), exist_ok=True)
    _make_pdf(os.path.join(folder, "sample.pdf"), page_count)


def _file_info(tmp):
    return {"path": os.path.join(tmp, "credit_card", "sample.pdf"),
            "doc_type": DocType.CREDIT_CARD, "name": "sample.pdf"}


def _ctx(value):
    """`with X() as v:` の形を満たす最小の context factory。"""
    import contextlib

    @contextlib.contextmanager
    def factory(*_a, **_kw):
        yield value
    return factory


def _snapshot(root):
    if not os.path.isdir(root):
        return []
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for f in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, f), root))
    return sorted(out)


if __name__ == "__main__":
    unittest.main()


class DumpMustNeverEnterTheRepoTest(unittest.TestCase):
    """`dump/` には顧客の実データ（店名・金額・カード末 4 桁）が入る。

    **この repo は PUBLIC。** 見るのは「規則が在るか」ではなく
    「実際に追跡されていないか」—— `.gitignore` は `git add -f` も
    既に tracked なファイルも止められない（`fixtures/` の番人と同型）。
    """

    def test_no_dump_file_is_tracked_by_git(self):
        import subprocess
        root = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run(["git", "ls-files", "dump/**"], cwd=root,
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "",
                         "dump/ 配下が git に追跡されています")

    def test_no_dump_directory_entry_is_tracked(self):
        import subprocess
        root = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run(["git", "ls-files", "dump"], cwd=root,
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "")


class StageInputsTest(unittest.TestCase):
    """原票の振り分け。**ニモカだけ transit_ic**（母 Plan AD-T3-1）。

    タブは folder の doc_type で決まるので、ここを間違えると nimoca の
    19 行/頁がクレカのタブへ落ちる —— 出力は出るので気付きにくい。
    """

    def test_nimoca_goes_to_transit_ic_and_the_rest_to_credit_card(self):
        with tempfile.TemporaryDirectory() as src, \
                tempfile.TemporaryDirectory() as dst:
            for name in ("ニモカ_6枚_x.pdf", "ENEOSカード_2枚.pdf",
                         "アメックスカード.pdf"):
                _make_pdf(os.path.join(src, name), 1)
            staged = D.stage_inputs(src, dst)
            self.assertEqual(len(staged), 3)
            self.assertEqual(os.listdir(os.path.join(dst, "transit_ic")),
                             ["ニモカ_6枚_x.pdf"])
            self.assertEqual(
                sorted(os.listdir(os.path.join(dst, "credit_card"))),
                ["ENEOSカード_2枚.pdf", "アメックスカード.pdf"])

    def test_copies_rather_than_moves_so_the_originals_survive(self):
        """製品は入力を `shutil.move` で消費する。原票を直接食わせない。"""
        with tempfile.TemporaryDirectory() as src, \
                tempfile.TemporaryDirectory() as dst:
            _make_pdf(os.path.join(src, "ENEOSカード.pdf"), 1)
            D.stage_inputs(src, dst)
            self.assertTrue(os.path.exists(os.path.join(src, "ENEOSカード.pdf")),
                            "原票が移動されている")

    def test_non_input_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as src, \
                tempfile.TemporaryDirectory() as dst:
            _make_pdf(os.path.join(src, "ENEOSカード.pdf"), 1)
            io.open(os.path.join(src, "メモ.txt"), "w").write("x")
            io.open(os.path.join(src, ".DS_Store"), "wb").write(b"\x00")
            self.assertEqual(D.stage_inputs(src, dst), ["ENEOSカード.pdf"])

    def test_creates_the_processed_directory(self):
        with tempfile.TemporaryDirectory() as src, \
                tempfile.TemporaryDirectory() as dst:
            D.stage_inputs(src, dst)
            self.assertTrue(os.path.isdir(os.path.join(dst, "processed")))


class CollectTokenUsageTest(unittest.TestCase):
    """録音の usage は**実測値**。文字数からの見積りに落とさない。"""

    def _slot(self, root, name, text, usage, finish="1"):
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        with io.open(os.path.join(d, "response.json"), "w",
                     encoding="utf-8") as f:
            json.dump({"text": text, "usage": usage, "finish_reason": finish,
                       "raises": False}, f, ensure_ascii=False)

    def test_emits_usage_and_identity_for_each_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._slot(tmp, "0000",
                       '{"card": {"issuer": "TS CUBIC", '
                       '"card_name": "ENEOS BUSINESS ETC カード", '
                       '"statement_page": "1/ 5ページ"}, '
                       '"rows": [{"amount": 760}, {"amount": 390}]}',
                       {"total_token_count": 100, "prompt_token_count": 60,
                        "candidates_token_count": 30})
            sink = D.EventSink()
            self.assertEqual(D.collect_token_usage(tmp, sink), 1)
            e = sink.events[0]
            self.assertEqual(e["kind"], "token_usage")
            self.assertEqual(e["slot"], "0000")
            self.assertEqual(e["usage"]["candidates_token_count"], 30)
            self.assertEqual(e["issuer"], "TS CUBIC")
            self.assertIn("ETC", e["card_name"])
            self.assertEqual(e["statement_page"], "1/ 5ページ")
            self.assertEqual(e["row_brace_count"], 2,
                             "明細行数が数えられていない（ETC 頁の特定に要る）")

    def test_a_missing_directory_yields_nothing_rather_than_raising(self):
        sink = D.EventSink()
        self.assertEqual(D.collect_token_usage("/no/such/dir", sink), 0)
        self.assertEqual(sink.events, [])

    def test_a_broken_slot_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "0000"))
            io.open(os.path.join(tmp, "0000", "response.json"),
                    "w").write("{ not json")
            self._slot(tmp, "0001", "{}", {"candidates_token_count": 5})
            sink = D.EventSink()
            self.assertEqual(D.collect_token_usage(tmp, sink), 1)


class MainEntryPointTest(unittest.TestCase):
    """入口が「複製 → 再生 → 観測 → 書き出し」を実際に繋いでいること。"""

    def test_runs_end_to_end_against_a_fake_replay(self):
        import gemini_record

        with tempfile.TemporaryDirectory() as src, \
                tempfile.TemporaryDirectory() as out:
            _make_pdf(os.path.join(src, "ENEOSカード_2枚.pdf"), 1)
            target = os.path.join(out, "events.jsonl")

            class _Session:
                calls = 1
                mode = "replay"

            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 1)])), \
                 mock.patch.object(gemini_record, "replaying",
                                   _ctx(_Session())):
                rc = D.main(["--samples", src, "--replay", "no-such-dir",
                             "--out", target])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(target))
            kinds = {json.loads(l)["kind"]
                     for l in io.open(target, encoding="utf-8")}
            self.assertIn("file_start", kinds)
            self.assertIn("mf_row", kinds)

    def test_aborts_when_the_session_is_not_replaying(self):
        """実 Gemini を呼ぶ mode で走らせない（課金と非決定性の両方を防ぐ）。"""
        import gemini_record

        with tempfile.TemporaryDirectory() as src, \
                tempfile.TemporaryDirectory() as out:
            _make_pdf(os.path.join(src, "ENEOSカード.pdf"), 1)

            class _Session:
                calls = 3
                mode = "record"

            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 1)])), \
                 mock.patch.object(gemini_record, "replaying",
                                   _ctx(_Session())):
                rc = D.main(["--samples", src, "--replay", "no-such-dir",
                             "--out", os.path.join(out, "e.jsonl")])
            self.assertEqual(rc, 1)


class PatchLifecycleTest(unittest.TestCase):
    """patch は**例外経路でも**全て戻ること。戻らないと他のテストへ漏れる。

    漏れ方が特に悪いのは `gspread.service_account` と
    `SheetsOutputWriter.__init__` —— 「呼ばれたら即例外」の状態が残ると、
    無関係なテストが Sheets 接続で落ち、原因がこの driver だと分からない。
    """

    def _snapshot_patched(self):
        import gspread
        import card_file_recon
        import sheets_output
        return {
            "TEST_DIR": local_test.TEST_DIR,
            "PROCESSED_DIR": local_test.PROCESSED_DIR,
            "process_pipeline": local_test.process_pipeline,
            "service_account": gspread.service_account,
            "authorize": gspread.authorize,
            "writer_init": sheets_output.SheetsOutputWriter.__init__,
            "report_and_record": card_file_recon.report_and_record,
            "format_cell_range": sheets_output.format_cell_range,
        }

    def test_every_patch_is_restored_after_a_normal_exit(self):
        before = self._snapshot_patched()
        with D.dumping_run(tempfile.mkdtemp(), D.EventSink()):
            pass
        self.assertEqual(self._snapshot_patched(), before)

    def test_every_patch_is_restored_when_the_body_raises(self):
        before = self._snapshot_patched()
        with self.assertRaises(ValueError):
            with D.dumping_run(tempfile.mkdtemp(), D.EventSink()):
                raise ValueError("boom")
        self.assertEqual(self._snapshot_patched(), before,
                         "例外経路で patch が戻っていない")

    def test_active_state_is_cleared_even_when_the_body_raises(self):
        with self.assertRaises(ValueError):
            with D.dumping_run(tempfile.mkdtemp(), D.EventSink()):
                raise ValueError("boom")
        with self.assertRaises(RuntimeError):
            D.run_one_file(_file_info(tempfile.mkdtemp()), D.EventSink())

    def test_nesting_is_rejected_rather_than_silently_mixing_runs(self):
        """入れ子を許すと内側の後始末が外側の状態を消し、events が静かに混ざる。"""
        with D.dumping_run(tempfile.mkdtemp(), D.EventSink()):
            with self.assertRaises(RuntimeError):
                with D.dumping_run(tempfile.mkdtemp(), D.EventSink()):
                    pass

    def test_a_rejected_nested_run_does_not_disturb_the_outer_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            _stage_one_file(tmp)
            sink = D.EventSink()
            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 1)])):
                with D.dumping_run(tmp, sink):
                    try:
                        with D.dumping_run(tmp, D.EventSink()):
                            pass
                    except RuntimeError:
                        pass
                    D.run_one_file(_file_info(tmp), sink)
            self.assertTrue([e for e in sink.events if e["kind"] == "mf_row"],
                            "外側の run が壊れている")


class MultipleFilesShareOneTabTest(unittest.TestCase):
    """真の dump は 7 ファイルが 1 つの MF タブを共有する。

    2 通目以降は `start_new_file` がファイル境界の分隔行を
    `ws.append_row` で直接書く（`sheets_output.py:446`）。これは
    `_write_with_retry` を通らないので**イベントには出ない** —— 明細では
    ないので判定の母集団にも入らず、それが正しい。ここで固定するのは
    「2 通目でも落ちない」「行が前のファイルに紐付かない」の 2 点。
    既存テストは全て単一ファイルなので、この経路を一度も通っていなかった。
    """

    def _two_files(self, tmp):
        folder = os.path.join(tmp, "credit_card")
        os.makedirs(folder, exist_ok=True)
        os.makedirs(os.path.join(tmp, "processed"), exist_ok=True)
        for name in ("a.pdf", "b.pdf"):
            _make_pdf(os.path.join(folder, name), 1)
        return [{"path": os.path.join(folder, n),
                 "doc_type": DocType.CREDIT_CARD, "name": n}
                for n in ("a.pdf", "b.pdf")]

    def test_second_file_does_not_crash_on_the_separator_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            infos = self._two_files(tmp)
            sink = D.EventSink()
            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 1)])):
                with D.dumping_run(tmp, sink):
                    for info in infos:
                        D.run_one_file(info, sink)
            ends = [e for e in sink.events if e["kind"] == "file_end"]
            self.assertEqual(len(ends), 2)
            self.assertTrue(all(e["success"] for e in ends))

    def test_rows_stay_attributed_to_their_own_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            infos = self._two_files(tmp)
            sink = D.EventSink()
            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 1)])):
                with D.dumping_run(tmp, sink):
                    for info in infos:
                        D.run_one_file(info, sink)
            mf = [e for e in sink.events if e["kind"] == "mf_row"]
            self.assertEqual({e["file"] for e in mf}, {"a.pdf", "b.pdf"},
                             "行が前のファイルへ紐付いている")
            for name in ("a.pdf", "b.pdf"):
                self.assertTrue([e for e in mf if e["file"] == name])

    def test_the_separator_row_is_not_emitted_as_a_data_row(self):
        """分隔行が明細に混ざると A6/A9 の母集団が汚れる。"""
        with tempfile.TemporaryDirectory() as tmp:
            infos = self._two_files(tmp)
            sink = D.EventSink()
            with mock.patch.object(local_test, "process_pipeline",
                                   _fake_pipeline([_page(1, 1)])):
                with D.dumping_run(tmp, sink):
                    for info in infos:
                        D.run_one_file(info, sink)
            rows = [e["row"] for e in sink.events
                    if e["kind"] == "mf_row" and e["is_data_row"]]
            for row in rows:
                self.assertTrue(any(str(c).strip() for c in row),
                                "空だけの行（分隔行）が明細に混ざっている")
