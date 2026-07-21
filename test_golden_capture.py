"""golden_capture の単体テスト（Plan §3 T2）。

process_pipeline は注入で差し替える。**真 Gemini は呼ばない**（quota を焼かない）。
"""
import hashlib
import json
import os
import tempfile
import unittest

import golden_capture as gc
from doc_types import DocType


def _fake_pipeline(pages):
    """process_pipeline 互換のジェネレータを返す（page_bytes 付き）。"""

    def _gen(file_path, doc_type=None, ocr_strategy=None, start_page=1):
        for page in pages:
            yield dict(page)

    return _gen


def _page(page_num, total, result, blob=b"\x89PNG-fake"):
    return {"page_num": page_num, "total_pages": total,
            "result": result, "page_bytes": blob}


class _SourceFileCase(unittest.TestCase):
    """臨時ディレクトリに合成ソースを 1 本置く共通土台。"""

    FILENAME = "sample.pdf"
    CONTENT = b"%PDF-1.4 synthetic"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = os.path.join(self.tmp.name, self.FILENAME)
        with open(self.src, "wb") as f:
            f.write(self.CONTENT)


class CaptureFixtureTest(_SourceFileCase):

    def test_page_bytes_are_stripped(self):
        pages = [_page(1, 1, {"vendor": "A", "entries": []})]
        fixture, _ = gc.capture(self.src, DocType.RECEIPT,
                                pipeline=_fake_pipeline(pages))
        self.assertEqual(len(fixture), 1)
        self.assertNotIn("page_bytes", fixture[0])
        # JSON 直列化できる＝バイナリが混入していない
        json.dumps(fixture, ensure_ascii=False)

    def test_yield_order_is_preserved(self):
        pages = [
            _page(1, 3, {"vendor": "p1-a", "entries": []}),
            _page(1, 3, {"vendor": "p1-b", "entries": []}),   # 同頁複数票
            _page(2, 3, {"vendor": "p2", "entries": []}),
            _page(3, 3, {"vendor": "p3", "entries": []}),
        ]
        fixture, _ = gc.capture(self.src, DocType.RECEIPT,
                                pipeline=_fake_pipeline(pages))
        self.assertEqual([f["page_num"] for f in fixture], [1, 1, 2, 3])
        self.assertEqual([f["result"]["vendor"] for f in fixture],
                         ["p1-a", "p1-b", "p2", "p3"])

    def test_page_error_pages_are_recorded(self):
        """skip 判断は replay 側の責務。capture は素通しで記録する。"""
        pages = [
            _page(1, 2, {"vendor": "ok", "entries": []}),
            _page(2, 2, {"_page_error": True, "entries": []}),
        ]
        fixture, manifest = gc.capture(self.src, DocType.RECEIPT,
                                       pipeline=_fake_pipeline(pages))
        self.assertEqual(len(fixture), 2)
        self.assertTrue(fixture[1]["result"]["_page_error"])
        self.assertEqual(manifest["page_count"], 2)


class ManifestTest(_SourceFileCase):

    CONTENT = b"%PDF-1.4 synthetic manifest"

    def test_manifest_has_required_keys_and_real_sha256(self):
        pages = [_page(1, 1, {"vendor": "A", "entries": []})]
        _, manifest = gc.capture(self.src, DocType.RECEIPT, strategy="C",
                                 pipeline=_fake_pipeline(pages))
        for key in ("source_file", "source_sha256", "commit", "strategy",
                    "gemini_model", "captured_at", "page_count"):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["source_sha256"],
                         hashlib.sha256(self.CONTENT).hexdigest())
        self.assertEqual(manifest["source_file"], "sample.pdf")
        self.assertEqual(manifest["strategy"], "C")
        self.assertEqual(manifest["page_count"], 1)

    def test_injected_pipeline_does_not_resolve_the_real_model(self):
        """pipeline 注入時は ocr_engine（paddleocr＋genai）を踏まない。"""
        pages = [_page(1, 1, {"vendor": "A", "entries": []})]
        _, manifest = gc.capture(self.src, DocType.RECEIPT, strategy="C",
                                 pipeline=_fake_pipeline(pages))
        self.assertEqual(manifest["gemini_model"], "<injected-pipeline>")


class OutputTest(_SourceFileCase):
    """出力の配置＝fixture は gitignore 先、manifest は仓追跡先へ分離する。"""

    FILENAME = "税区分テスト.pdf"
    CONTENT = b"%PDF-1.4 synthetic out"

    def test_write_outputs_splits_fixture_and_manifest(self):
        pages = [_page(1, 1, {"vendor": "A", "entries": []})]
        fixture, manifest = gc.capture(self.src, DocType.RECEIPT,
                                       pipeline=_fake_pipeline(pages))
        out_dir = os.path.join(self.tmp.name, "golden")
        man_dir = os.path.join(self.tmp.name, "golden_manifest")
        fx_path, mf_path = gc.write_outputs(fixture, manifest, out_dir, man_dir)

        self.assertEqual(os.path.basename(fx_path), "税区分テスト.fixture.json")
        self.assertEqual(os.path.basename(mf_path), "税区分テスト.manifest.json")
        with open(fx_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), fixture)
        with open(mf_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), manifest)

    def test_main_drives_the_full_capture_path(self):
        """T3 の実行経路（CLI）そのものを fake pipeline で駆動する。"""
        pages = [_page(1, 2, {"vendor": "A", "entries": []}),
                 _page(2, 2, {"_page_error": True, "entries": []})]
        out_dir = os.path.join(self.tmp.name, "g")
        man_dir = os.path.join(self.tmp.name, "gm")
        fx_path, mf_path = gc.main(
            [self.src, "--doc-type", "receipt", "--strategy", "C",
             "--out", out_dir, "--manifest-out", man_dir],
            pipeline=_fake_pipeline(pages))

        with open(fx_path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)), 2)
        with open(mf_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["page_count"], 2)


class DefaultPipelineTest(unittest.TestCase):

    def test_default_pipeline_resolves_to_ocr_engine(self):
        """既定経路が本物の process_pipeline を指すこと（呼び出しはしない）。"""
        from ocr_engine import process_pipeline
        self.assertIs(gc._default_pipeline(), process_pipeline)


if __name__ == "__main__":
    unittest.main()
