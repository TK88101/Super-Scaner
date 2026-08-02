"""契約 §5.7 provider 事件の結線テスト（B8 T3）。

**主題は帰因**。契約 §5.7 は SS 側の前提修正として「現在は Gemini の例外を
PaddleOCR の失敗として出力し得る」を名指ししている——`_route_ocr_strategy` の
単一 try が PaddleOCR 呼出と 3 つの Gemini 呼出をまとめて包み、except が一律
「PaddleOCR失敗」と出力していた。誤った provider を計数すると控制面の断路器が
誤作動する（無実の PaddleOCR を熔断し、真犯人の Gemini は放置される）。

したがって本ファイルの中核は `GeminiFailureAttributionTest`。他は付帯。

    venv311/bin/python -m unittest test_provider_events_wiring -v
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("PROCESSED_FOLDER_ID", "test_processed_folder")
os.environ.setdefault("SERVICE_ACCOUNT_FILE", "test_sa.json")
os.environ.setdefault("OUTPUT_SPREADSHEET_ID", "test_spreadsheet")
os.environ.setdefault("FOLDER_RECEIPT_ID", "test_receipt_folder")

import ocr_engine
import provider_events as pe


class RecordingSink:
    """ProviderEventWriter の記録面だけを持つ替玉（Firestore を触らない）。"""

    def __init__(self):
        self.calls = []

    def record(self, *, provider, error_class, page=None, job_id=None):
        if provider not in pe.PROVIDERS:
            raise ValueError(provider)
        if error_class not in pe.ERROR_CLASSES:
            raise ValueError(error_class)
        self.calls.append(
            {"provider": provider, "error_class": error_class,
             "page": page, "job_id": job_id}
        )
        return True

    @property
    def providers(self):
        return [c["provider"] for c in self.calls]


def _route(sink=None, **overrides):
    """`_route_ocr_strategy` を戦略 C（既定）で 1 回呼ぶ。"""
    kwargs = dict(
        data_bytes=b"fake-image-bytes",
        mime_type="image/jpeg",
        prompt="dummy prompt",
        ocr_strategy="C",
        event_sink=sink,
        page=3,
        job_id="job-1",
    )
    kwargs.update(overrides)
    with redirect_stdout(io.StringIO()):
        return ocr_engine._route_ocr_strategy(**kwargs)


# --- 中核：Gemini の例外が PaddleOCR に濡れ衣を着せない（§5.7 前提） ---------


class GeminiFailureAttributionTest(unittest.TestCase):
    def test_gemini_exception_is_attributed_to_gemini_not_paddleocr(self):
        sink = RecordingSink()
        with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                               return_value=("領収証 1,000円", 0.95)), \
             mock.patch.object(ocr_engine, "_call_gemini_cross_validate",
                               side_effect=ConnectionError("gemini down")):
            _route(sink)

        self.assertEqual(sink.providers, ["gemini_ocr"])

    def test_gemini_transport_exception_maps_to_retryable(self):
        sink = RecordingSink()
        with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                               return_value=("text", 0.9)), \
             mock.patch.object(ocr_engine, "_call_gemini_cross_validate",
                               side_effect=ConnectionError("boom")):
            _route(sink)

        self.assertEqual(sink.calls[0]["error_class"], "RETRYABLE")

    def test_gemini_unknown_exception_maps_to_unknown(self):
        """認証エラー等を NON_RETRYABLE に倒すと DEAD_LETTER 風暴を招く。"""
        sink = RecordingSink()
        with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                               return_value=("text", 0.9)), \
             mock.patch.object(ocr_engine, "_call_gemini_cross_validate",
                               side_effect=RuntimeError("bad credentials")):
            _route(sink)

        self.assertEqual(sink.calls[0]["error_class"], "UNKNOWN")


class PaddleFailureAttributionTest(unittest.TestCase):
    def test_paddle_exception_is_attributed_to_paddleocr(self):
        sink = RecordingSink()
        with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                               side_effect=RuntimeError("paddle blew up")):
            _route(sink)

        self.assertEqual(sink.providers, ["paddleocr"])

    def test_paddle_failure_still_yields_no_raw_data(self):
        """帰因の是正で既存の戻り値契約を変えてはいけない（回帰の番人）。"""
        with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                               side_effect=RuntimeError("paddle blew up")):
            raw_data, ocr_text, ocr_conf = _route(RecordingSink())

        self.assertIsNone(raw_data)
        self.assertEqual(ocr_text, "")
        self.assertIsNone(ocr_conf)


# --- 「呼出し成功 ≠ 業務成功」（Plan §4 T3） ---------------------------------


class ResponseAnomalyTest(unittest.TestCase):
    def test_gemini_returning_none_is_recorded_as_response_anomaly(self):
        """HTTP 200 でも JSON 解析不能／MAX_TOKENS 截断なら事象である。"""
        sink = RecordingSink()
        with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                               return_value=("text", 0.9)), \
             mock.patch.object(ocr_engine, "_call_gemini_cross_validate",
                               return_value=None):
            _route(sink)

        self.assertEqual(sink.calls,
                         [{"provider": "gemini_ocr", "error_class": "NON_RETRYABLE",
                           "page": 3, "job_id": "job-1"}])

    def test_successful_call_records_nothing(self):
        sink = RecordingSink()
        with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                               return_value=("text", 0.9)), \
             mock.patch.object(ocr_engine, "_call_gemini_cross_validate",
                               return_value={"date": "2026-08-02"}):
            _route(sink)

        self.assertEqual(sink.calls, [])

    def test_empty_ocr_text_alone_is_not_a_provider_event(self):
        """白紙頁は provider 障害ではない。断路器に雑音を送らない。"""
        sink = RecordingSink()
        with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                               return_value=("   ", 0.0)):
            _route(sink)

        self.assertEqual(sink.calls, [])


# --- 注入されない経路（UI 版）は一切変わらない ------------------------------


class NoSinkTest(unittest.TestCase):
    def test_absent_sink_does_not_crash_on_failure(self):
        with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                               side_effect=RuntimeError("paddle blew up")):
            raw_data, ocr_text, ocr_conf = _route(sink=None)

        self.assertIsNone(raw_data)

    def test_sink_failure_does_not_break_ocr(self):
        """事象記録が落ちても票の処理は続く（書込失敗は本来の失敗を隠さない）。"""
        class ExplodingSink:
            def record(self, **kwargs):
                raise RuntimeError("sink exploded")

        with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                               return_value=("text", 0.9)), \
             mock.patch.object(ocr_engine, "_call_gemini_cross_validate",
                               return_value={"date": "2026-08-02"}):
            raw_data, _, _ = _route(sink=ExplodingSink())

        self.assertEqual(raw_data, {"date": "2026-08-02"})


class RedactionGuardSurvivesWiringTest(unittest.TestCase):
    """脱敏／値域の違反は**生産経路でも**落ちること（simplify R1 の要害）。

    `ProviderEventWriter.record` は値域違反と脱敏違反に対して意図的に
    `ValueError`/`TypeError` を送出する。呼出側が `except Exception` で
    一括して吞むと、その設計が**唯一の生産経路でだけ無効化**され、テストは
    record を直接叩くので緑のまま——という最悪の形になる。ここが番人。
    """

    def _route_with(self, sink):
        with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                               side_effect=RuntimeError("paddle blew up")):
            return _route(sink=sink)

    def test_value_error_from_sink_propagates(self):
        class BadDomainSink:
            def record(self, **kwargs):
                raise ValueError("provider の値域違反")

        with self.assertRaises(ValueError):
            self._route_with(BadDomainSink())

    def test_type_error_from_sink_propagates(self):
        class RedactionViolationSink:
            def record(self, **kwargs):
                raise TypeError("禁止字段が渡された")

        with self.assertRaises(TypeError):
            self._route_with(RedactionViolationSink())

    def test_real_writer_rejects_forbidden_field_through_the_wiring(self):
        """替玉ではなく本物の writer で、生産経路の末端まで通して確認する。"""
        class LeakySink:
            """呼出側が誤って票面情報を混ぜてしまった状況を模す。"""

            def __init__(self):
                self._writer = pe.ProviderEventWriter(_NullClient())

            def record(self, **kwargs):
                return self._writer.record(vendor="株式会社秘密", **kwargs)

        with self.assertRaises(TypeError):
            self._route_with(LeakySink())


class _NullClient:
    def collection(self, name):
        raise AssertionError("値域検証で落ちるので書込には到達しないはず")


class SinkLifetimeTest(unittest.TestCase):
    """sink は**ループが 1 個持つ**（檔ごとに作らない）＝simcodex R4 の修正点。

    檔ごとに作り直すと配額が檔ごとに再配分され、provider 全面障害中に小さな
    檔が次々来た時に各檔が上限ぶん書く——総書込量の上界が消え、封頂が目的を
    果たさなくなる。配額の挙動そのものは `test_provider_events.CapTest` が
    単体で守るが、**「誰が writer を持つか」は外から観測できない**ので、
    ここは意図的に源コードを検査する（生産経路を二檔ぶん実走させるには
    Drive/Sheets/Firestore の依存を丸ごと組む必要があり、割に合わない）。
    """

    def test_per_file_helper_does_not_construct_its_own_writer(self):
        import inspect

        import main

        src = inspect.getsource(main._process_one_file)
        self.assertNotIn(
            "ProviderEventWriter", src,
            "檔ごとの writer 生成は配額を檔ごとに配り直す＝封頂が効かなくなる。"
            "ループ側で 1 個作り provider_sink として渡すこと")

    def test_per_file_helper_receives_the_shared_sink(self):
        import inspect

        import main

        self.assertIn("provider_sink",
                      inspect.signature(main._process_one_file).parameters)


class VisionFallbackAnomalyTest(unittest.TestCase):
    """Vision 兜底が None を返した頁も断路器から見えること（simcodex R3）。

    PaddleOCR が落ちる／空文字 → `_route_ocr_strategy` は Gemini を呼ばずに
    None を返す → pipeline が Vision 兜底を撃つ → その応答が JSON として
    解けない。ここを記録しないと、主経路の応答異常は記録されるのに兜底の
    それだけ沈黙するという非対称ができ、同じ障害が経路次第で見えたり
    見えなかったりする。
    """

    def _run_tail_segment(self, sink):
        """尾段（単頁・画像）経路を実ファイルで通す。"""
        import tempfile

        from doc_types import DocType

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(b"not-a-real-jpeg-but-never-decoded")
            path = tmp.name
        try:
            with mock.patch.object(ocr_engine, "_ocr_with_paddleocr",
                                   return_value=("", 0.0)), \
                 mock.patch.object(ocr_engine, "_call_gemini",
                                   return_value=None), \
                 redirect_stdout(io.StringIO()):
                list(ocr_engine.process_pipeline(
                    path, doc_type=DocType.RECEIPT, ocr_strategy="C",
                    event_sink=sink, job_id="job-1"))
        finally:
            os.unlink(path)

    def test_fallback_returning_none_is_recorded(self):
        sink = RecordingSink()

        self._run_tail_segment(sink)

        self.assertEqual(
            sink.calls,
            [{"provider": "gemini_ocr", "error_class": "NON_RETRYABLE",
              "page": 1, "job_id": "job-1"}])




# --- headless 経路だけが sink を注入する（UI 版は従来どおり） ----------------


class HeadlessWiringTest(unittest.TestCase):
    """`_process_file_headless` が sink と job_key を pipeline へ貫通させるか。

    ここが繋がっていないと、本 session の実装は全部「書けるが誰も呼ばない」
    死んだコードになる（ENTRY_BUILDERS 未登録事故と同族）。
    """

    def _pipeline_kwargs(self, **headless_kwargs):
        import main
        seen = {}
        called = []

        def fake_pipeline(file_path, **kwargs):
            called.append(True)
            seen.update(kwargs)
            return iter(())

        with mock.patch.object(main, "process_pipeline", fake_pipeline), \
             mock.patch.object(main, "PageUrlResolver", lambda *a, **k: None), \
             redirect_stdout(io.StringIO()):
            try:
                main._process_file_headless(**headless_kwargs)
            except Exception:
                # 空 pipeline の後段（頁ゼロの終局判定）は本テストの対象外。
                # ただし pipeline に到達すらしていない＝呼出し自体の失敗
                # （引数不一致など）は握り潰さず晒す。
                if not called:
                    raise
        return seen

    def test_headless_passes_sink_and_job_id_to_pipeline(self):
        sink = RecordingSink()
        seen = self._pipeline_kwargs(
            service=None, writer=None, file_path="/tmp/x.pdf",
            uploader_name="u", base="job-1", ledger=None,
            doc_type="receipt", drive_file_id="fid",
            split_pdf_folder_id="", tab_owner="0001_客A",
            event_sink=sink,
        )

        self.assertIs(seen.get("event_sink"), sink)
        self.assertEqual(seen.get("job_id"), "job-1")

    def test_headless_without_sink_passes_none(self):
        seen = self._pipeline_kwargs(
            service=None, writer=None, file_path="/tmp/x.pdf",
            uploader_name="u", base="job-1", ledger=None,
            doc_type="receipt", drive_file_id="fid",
            split_pdf_folder_id="", tab_owner="0001_客A",
        )

        self.assertIsNone(seen.get("event_sink"))

    def test_ui_path_never_injects_a_sink(self):
        """UI 版（`_process_file_impl`）は §5.7 の対象外。零改動の番人。

        以前は `inspect.getsource` に部分文字列が在るかで見ていたが、それは
        整形（改行・末尾カンマ）が変わるだけで割れる＝formatter がテストの
        依存になる。守りたいのは「UI 経路が sink を注入しない」という**挙動**
        なので、実際に呼ばせて渡された kwargs を見る（simplify R1 採納）。
        """
        import main
        seen = {}

        def fake_pipeline(file_path, **kwargs):
            seen.update(kwargs)
            return iter(())

        with mock.patch.object(main, "process_pipeline", fake_pipeline), \
             redirect_stdout(io.StringIO()):
            main._process_file_impl(
                service=None, sheets_writer=mock.MagicMock(),
                file_path="/tmp/x.pdf", uploader_name="u", chat_id=None,
                doc_type="receipt", ledger=None, progress=main.NULL_REPORTER)

        # 先に「本当に pipeline まで到達した」ことを示す——ここが無いと、
        # UI 経路が手前で return しても seen が空のまま下の 2 行が通ってしまい、
        # 何も守っていないテストが緑を出す。
        self.assertIn("doc_type", seen)
        self.assertNotIn("event_sink", seen)
        self.assertNotIn("job_id", seen)


if __name__ == "__main__":
    unittest.main()
