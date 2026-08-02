"""契約 v0.17 §5.7 provider 事件集合の単体テスト（B8・TDD RED 先行）。

読む側は控制面の断路器**のみ**（§5.7）。したがって本テストの主眼は
「断路器が数えられる形で出るか」と「脱敏白名単を破れないか」の二点。
"""

import io
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime

import provider_events as pe


class FakeDoc:
    def __init__(self, client, doc_id):
        self._client = client
        self._id = doc_id

    def set(self, payload, timeout=None):
        # timeout を受けるのは飾りではない：本番は必ず timeout 付きで呼ぶので、
        # 受け取らない fake なら「timeout を渡さない」退行が TypeError で露見する。
        self._client.timeouts.append(timeout)
        self._client.store[self._id] = dict(payload)


class FakeCollection:
    def __init__(self, client):
        self._client = client

    def document(self, doc_id):
        return FakeDoc(self._client, doc_id)


class FakeClient:
    """firestore.Client 鴨子型 fake。provider_events 単一 collection のみ保持。

    `fail` を True にすると書込が必ず失敗する。障害の入切を**同一インスタンスの
    属性**で表すのは、テスト途中で復旧を演じるため——別クラスに分けると
    `__class__` の差し替えという読み手を止める小細工が要る（simplify R1 採納）。
    """

    def __init__(self, fail=False):
        self.store = {}
        self.collection_names = []
        self.timeouts = []
        self.fail = fail

    def collection(self, name):
        if self.fail:
            raise RuntimeError("firestore down")
        self.collection_names.append(name)
        return FakeCollection(self)


def _writer(client=None, **kwargs):
    return pe.ProviderEventWriter(client or FakeClient(), **kwargs)


# --- 契約 §5.7 の文書形状（跨倉 schema 固定・趙裁定＝schema 契約テスト） --------


class DocumentShapeTest(unittest.TestCase):
    def test_written_document_has_exactly_contract_fields(self):
        client = FakeClient()
        w = _writer(client)

        w.record(provider="gemini_ocr", error_class="RETRYABLE",
                 job_id="job-1", page=3)

        (doc,) = client.store.values()
        self.assertEqual(
            set(doc), {"provider", "error_class", "occurred_at", "job_id"}
        )

    def test_collection_path_is_top_level_provider_events(self):
        client = FakeClient()
        _writer(client).record(provider="paddleocr", error_class="UNKNOWN",
                               job_id="job-1", page=1)

        self.assertEqual(client.collection_names, ["provider_events"])

    def test_occurred_at_is_utc_aware(self):
        client = FakeClient()
        _writer(client).record(provider="gemini_ocr", error_class="RETRYABLE",
                               job_id="job-1", page=1)

        (doc,) = client.store.values()
        self.assertIsInstance(doc["occurred_at"], datetime)
        self.assertEqual(doc["occurred_at"].tzinfo, UTC)

    def test_job_id_is_optional_per_contract(self):
        """§5.7 の job_id は「任意・追跡用」。無指定でも書ける。"""
        client = FakeClient()
        _writer(client).record(provider="paddleocr", error_class="UNKNOWN",
                               page=1)

        (doc,) = client.store.values()
        self.assertIsNone(doc["job_id"])


class ValueDomainTest(unittest.TestCase):
    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            _writer().record(provider="openai", error_class="RETRYABLE",
                             job_id="j", page=1)

    def test_unknown_error_class_is_rejected(self):
        """§5.4 の三分類以外は控制面の断路器が解釈できない。"""
        with self.assertRaises(ValueError):
            _writer().record(provider="gemini_ocr", error_class="CONTENT",
                             job_id="j", page=1)

    def test_contract_value_domains_are_frozen(self):
        self.assertEqual(
            pe.PROVIDERS, frozenset({"gemini_classify", "gemini_ocr", "paddleocr"})
        )
        self.assertEqual(
            pe.ERROR_CLASSES,
            frozenset({"RETRYABLE", "NON_RETRYABLE", "UNKNOWN"}),
        )


# --- 脱敏（否定対照＝禁止字段を渡そうとすると落ちる） -------------------------


class RedactionTest(unittest.TestCase):
    def test_extra_field_is_rejected_not_silently_dropped(self):
        """静かに捨てると「渡したのに出ない」事故に化ける。落として気付かせる。"""
        for forbidden in ("vendor", "amount", "file_name", "message", "ocr_text"):
            with self.subTest(field=forbidden):
                with self.assertRaises(TypeError):
                    _writer().record(
                        provider="gemini_ocr", error_class="RETRYABLE",
                        job_id="j", page=1, **{forbidden: "秘"},
                    )

    def test_job_id_non_str_is_rejected(self):
        """job_id に dict/例外オブジェクト等を渡すと票面情報が漏れ得る。"""
        with self.assertRaises(TypeError):
            _writer().record(provider="gemini_ocr", error_class="RETRYABLE",
                             job_id={"vendor": "株式会社秘密"}, page=1)

    def test_page_non_int_is_rejected(self):
        """page も同様——生テキストを頁番号の顔で通されると event_id に混入する。"""
        with self.assertRaises(TypeError):
            _writer().record(provider="gemini_ocr", error_class="RETRYABLE",
                             job_id="j", page="領収証 1,000円")


# --- 冪等（同一頁・同一 provider・同一 error_class は増殖しない） -------------


class IdempotencyTest(unittest.TestCase):
    def test_same_page_same_provider_same_class_does_not_multiply(self):
        client = FakeClient()
        w = _writer(client)

        for _ in range(5):
            w.record(provider="gemini_ocr", error_class="RETRYABLE",
                     job_id="job-1", page=7)

        self.assertEqual(len(client.store), 1)

    def test_different_pages_are_separate_events(self):
        """趙裁定＝頁級 1 件。断路器の「連続 5 件」が成立する粒度。"""
        client = FakeClient()
        w = _writer(client)

        for page in range(1, 6):
            w.record(provider="gemini_ocr", error_class="RETRYABLE",
                     job_id="job-1", page=page)

        self.assertEqual(len(client.store), 5)

    def test_different_error_class_on_same_page_is_separate_event(self):
        client = FakeClient()
        w = _writer(client)

        w.record(provider="gemini_ocr", error_class="RETRYABLE",
                 job_id="job-1", page=2)
        w.record(provider="gemini_ocr", error_class="NON_RETRYABLE",
                 job_id="job-1", page=2)

        self.assertEqual(len(client.store), 2)


# --- 書込頻度上限（趙裁定＝job×provider×error_class ごと 20 件で打止め） -----


class CapTest(unittest.TestCase):
    def test_cap_stops_writing_beyond_limit(self):
        client = FakeClient()
        w = _writer(client, job_cap=20)

        for page in range(1, 101):
            w.record(provider="gemini_ocr", error_class="RETRYABLE",
                     job_id="job-1", page=page)

        self.assertEqual(len(client.store), 20)

    def test_cap_does_not_starve_the_breaker_threshold(self):
        """断路器は 5 件で熔断。上限 20 はそれを埋めない（回帰防止の明示）。"""
        self.assertGreater(pe.DEFAULT_JOB_CAP, 5)

    def test_cap_is_per_job_provider_error_class(self):
        client = FakeClient()
        w = _writer(client, job_cap=2)

        for page in (1, 2, 3):
            w.record(provider="gemini_ocr", error_class="RETRYABLE",
                     job_id="job-1", page=page)
        for page in (1, 2, 3):
            w.record(provider="paddleocr", error_class="RETRYABLE",
                     job_id="job-1", page=page)

        self.assertEqual(len(client.store), 4)

    def test_record_reports_whether_it_wrote(self):
        client = FakeClient()
        w = _writer(client, job_cap=1)

        self.assertTrue(w.record(provider="gemini_ocr", error_class="RETRYABLE",
                                 job_id="job-1", page=1))
        self.assertFalse(w.record(provider="gemini_ocr", error_class="RETRYABLE",
                                  job_id="job-1", page=2))

    def test_cap_counts_are_independent_across_jobs(self):
        client = FakeClient()
        w = _writer(client, job_cap=1)

        w.record(provider="gemini_ocr", error_class="RETRYABLE",
                 job_id="job-1", page=1)
        w.record(provider="gemini_ocr", error_class="RETRYABLE",
                 job_id="job-2", page=1)

        self.assertEqual(len(client.store), 2)

    def test_repeated_same_event_does_not_consume_cap(self):
        """冪等な再記録で上限を食い潰すと、後続の別頁が黙って落ちる。"""
        client = FakeClient()
        w = _writer(client, job_cap=2)

        for _ in range(10):
            w.record(provider="gemini_ocr", error_class="RETRYABLE",
                     job_id="job-1", page=1)
        w.record(provider="gemini_ocr", error_class="RETRYABLE",
                 job_id="job-1", page=2)

        self.assertEqual(len(client.store), 2)


# --- 書込失敗が本来の OCR 失敗を隠さない（Codex R1 P1 由来の規則） ------------


class WriteFailureIsolationTest(unittest.TestCase):
    def test_firestore_failure_does_not_propagate(self):
        w = _writer(FakeClient(fail=True))

        wrote = w.record(provider="gemini_ocr", error_class="RETRYABLE",
                         job_id="job-1", page=1)

        self.assertFalse(wrote)

    def test_firestore_failure_does_not_consume_cap(self):
        """失敗を上限に数えると、復旧後に本物の事象が書けなくなる。"""
        client = FakeClient(fail=True)
        w = _writer(client, job_cap=1)
        w.record(provider="gemini_ocr", error_class="RETRYABLE",
                 job_id="job-1", page=1)

        client.fail = False
        wrote = w.record(provider="gemini_ocr", error_class="RETRYABLE",
                         job_id="job-1", page=2)

        self.assertTrue(wrote)

    def test_repeated_write_failure_logs_only_once(self):
        """無人の mini PC に同文を 20 行吐かない（provider 全面障害時）。"""
        w = _writer(FakeClient(fail=True))

        with redirect_stdout(io.StringIO()) as out:
            for page in range(1, 21):
                w.record(provider="gemini_ocr", error_class="RETRYABLE",
                         job_id="job-1", page=page)

        self.assertEqual(out.getvalue().count("provider 事件の書込失敗"), 1)


class WriteCallShapeTest(unittest.TestCase):
    def test_set_is_called_with_a_timeout(self):
        """吊るされた Firestore が OCR を止めないための上限（§実装ノート）。"""
        client = FakeClient()
        _writer(client).record(provider="gemini_ocr", error_class="RETRYABLE",
                               job_id="job-1", page=1)

        self.assertEqual(client.timeouts, [pe.SET_TIMEOUT_SECONDS])


class NoClassifierHereTest(unittest.TestCase):
    """例外帰因の権威は `ocr_engine._classify_page_error` **のみ**。

    ここに二つ目の分類器が復活すると、同じ例外が頁級台帳では RETRYABLE、
    断路器へは UNKNOWN という食い違いが起き、控制面が無実の provider を
    熔断する（simplify R1 で reuse／altitude が独立に指摘）。
    """

    def test_module_exposes_no_exception_classifier(self):
        self.assertFalse(
            [n for n in dir(pe) if "classify" in n.lower()],
            "provider_events に分類器を再導入しないこと（帰因の権威は ocr_engine）")


if __name__ == "__main__":
    unittest.main()
