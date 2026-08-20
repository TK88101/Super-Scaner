"""`gemini_record` の差し替えと再生の意味論。

Plan: `docs/plans/2026-08-20-gemini-record-replay.md`（**v3** 定稿）§5＋§6＋§12。
応答の直列化・鍵・side-channel は `test_gemini_record.py` に分けてある。

**このファイルが守る最重要の性質**（壊れると本 Plan の目的そのものが死ぬ）:

1. 再生中に実 Gemini が呼ばれないこと（`ReplayNeverCallsGeminiTest`）。
   黙って実 API へ兜底すると、決定的なはずの回帰が**静かに非決定へ戻る**。
2. context を抜けたら製品関数が必ず戻ること（`PatchLeakTest`）。
   漏れると後続のテストや本番経路が録音済み応答を食う。
3. 本番モジュールが `gemini_record` を参照しないこと（`ProductionIsolationTest`）。
   Plan R-1（本番が誤って再生経路に入り、無音で顧客の帳簿が壊れる）の構造保証。

    venv311/bin/python -m unittest test_gemini_record_replay -v
"""
import ast
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import gemini_record
import ocr_engine
from ocr_test_helpers import gemini_response

_HERE = os.path.dirname(os.path.abspath(__file__))

# 差し替え対象。改名されると patch は**静かに当たらなくなる**（例外も出ず、
# テストは緑のまま実 Gemini を呼びうる）＝ AD-3 が新たに生んだ負債 R-4。
#
# **実装のリストをそのまま読む。** ここに複製を置くと、実装側から 1 本
# 抜け落ちてもテストは自分の複製を見続けて緑のままになる —— 番人が
# 守ると宣言したものを守らなくなる（Codex 実装評審 低 6）。
PATCH_TARGETS = gemini_record.PATCH_TARGETS


class ExplodingModel:
    """再生中に実 Gemini へ触れたら即座に判る番人。

    例外を投げるだけなので、ネットワークにも課金にも触れない（Codex 高 3）。
    """

    def generate_content(self, *args, **kwargs):
        raise AssertionError("再生中に実 Gemini が呼ばれた")


@contextlib.contextmanager
def recorded_dir(*payloads, prompts=None, line_mode=False):
    """本物の `recording` 経路で録音を作り、そのディレクトリを貸す。

    手で JSON を置くのではなく製品の録音経路を通すのは、保存形式が変わった
    ときに再生側だけ直って**録音側と食い違ったまま両方緑**になるのを防ぐため。
    """
    prompts = list(prompts or [f"P{i}" for i in range(len(payloads))])
    responses = [gemini_response(text=json.dumps(p)) for p in payloads]
    with tempfile.TemporaryDirectory() as directory:
        fake = mock.MagicMock()
        fake.generate_content.side_effect = responses
        with mock.patch.object(ocr_engine, "model", fake):
            with gemini_record.recording(directory):
                for prompt in prompts:
                    ocr_engine._call_gemini_text("OCR", prompt,
                                                 line_mode=line_mode)
        yield directory


@contextlib.contextmanager
def replaying_quietly(directory, **kwargs):
    """再生 context ＋ 実 model 禁止 ＋ 標準出力の捕捉。"""
    buffer = io.StringIO()
    with mock.patch.object(ocr_engine, "model", ExplodingModel()):
        with contextlib.redirect_stdout(buffer):
            with gemini_record.replaying(directory, **kwargs) as session:
                session.stdout = buffer
                yield session


# ------------------------------------------------------------- T-2a / T-2b

class PatchLeakTest(unittest.TestCase):
    """T-2a / T-2b: context を抜けたら必ず製品関数へ戻る。"""

    def _originals(self):
        return {name: getattr(ocr_engine, name) for name in PATCH_TARGETS}

    def test_t2a_all_targets_restored_after_recording(self):
        originals = self._originals()
        with tempfile.TemporaryDirectory() as directory:
            with gemini_record.recording(directory):
                for name, original in originals.items():
                    with self.subTest(target=name):
                        self.assertIsNot(getattr(ocr_engine, name), original)
        for name, original in originals.items():
            with self.subTest(target=name):
                self.assertIs(getattr(ocr_engine, name), original)

    def test_t2b_all_targets_restored_after_exception(self):
        originals = self._originals()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                with gemini_record.recording(directory):
                    raise RuntimeError("boom")
        for name, original in originals.items():
            with self.subTest(target=name):
                self.assertIs(getattr(ocr_engine, name), original)

    def test_t2b_targets_restored_after_replay_exception(self):
        originals = self._originals()
        with recorded_dir({"a": 1}) as directory:
            with self.assertRaises(RuntimeError):
                with gemini_record.replaying(directory):
                    raise RuntimeError("boom")
        for name, original in originals.items():
            with self.subTest(target=name):
                self.assertIs(getattr(ocr_engine, name), original)


# ------------------------------------------------------------------- T-2c'

class PatchTargetContractTest(unittest.TestCase):
    """T-2c': 差し替え対象 4 本は暗黙の契約（AD-3 の負債 R-4）。"""

    def test_all_patch_targets_exist(self):
        for name in PATCH_TARGETS:
            with self.subTest(target=name):
                self.assertTrue(callable(getattr(ocr_engine, name, None)))

    def test_variants_route_through_the_retry_funnel(self):
        """3 変体が実際に `_generate_content_with_retry` を通ることを示す。"""
        sentinel = gemini_response(text=json.dumps({"ok": 1}))
        calls = {
            "text": lambda: ocr_engine._call_gemini_text("O", "P"),
            "bytes": lambda: ocr_engine._call_gemini_bytes(
                b"i", "image/jpeg", "P"),
            "cross_validate": lambda: ocr_engine._call_gemini_cross_validate(
                "O", b"i", "image/jpeg", "P"),
        }
        for name, call in calls.items():
            with self.subTest(variant=name):
                with mock.patch.object(ocr_engine,
                                       "_generate_content_with_retry",
                                       return_value=sentinel) as spy:
                    self.assertEqual(call(), {"ok": 1})
                self.assertEqual(spy.call_count, 1)

    def test_model_generate_content_has_a_single_product_call_site(self):
        """漏斗が 1 本であること（AD-1 の前提）をソースで見張る。"""
        with open(os.path.join(_HERE, "ocr_engine.py"), encoding="utf-8") as f:
            hits = [line for line in f
                    if "model.generate_content(" in line
                    and not line.lstrip().startswith("#")]
        self.assertEqual(len(hits), 1, f"漏斗が分岐した: {hits}")


# ------------------------------------------------------------------- T-2d'

def _gemini_record_references(path):
    """AST 上の `gemini_record` 参照を列挙する（コメントは AST に載らない）。

    純粋な import 文の検出だけでは `importlib.import_module("gemini_record")`
    や `__import__("gemini_record")` を漏らすので、**docstring 以外**の
    文字列定数も見る。説明をコメントに書く自由は残る。
    """
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            docstrings.add(id(first.value))

    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits += [f"import {a.name}" for a in node.names
                     if a.name.split(".")[0] == "gemini_record"]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "gemini_record":
                hits.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Name) and node.id == "gemini_record":
            hits.append("name gemini_record")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and "gemini_record" in node.value:
                hits.append(f"string {node.value[:40]!r}")
    return hits


class ProductionIsolationTest(unittest.TestCase):
    """T-2d': 本番モジュールは `gemini_record` を参照しない（Plan R-1 の構造保証）。

    Plan v2 の原案は「`git diff` に `ocr_engine.py` が現れない」だったが、
    それを恒久テストにすると**将来 ocr_engine を正当に編集した瞬間に誤発火**する。
    「二度と変えるな」は不変量として成立しない。恒久的に守る価値があるのは
    「本番経路に記録再生が配線されていないこと」の方である。
    （「今回の実装が製品コード無改変であること」は Plan §9-2 の受入基準として
    実施完了時に `git diff --stat` で人が 1 度確認する。1 回の検証と番人は別物。）
    """

    PRODUCTION_MODULES = ("ocr_engine.py", "main.py")

    def test_production_modules_never_reference_gemini_record(self):
        for name in self.PRODUCTION_MODULES:
            with self.subTest(module=name):
                self.assertEqual(
                    _gemini_record_references(os.path.join(_HERE, name)), [])

    def test_the_detector_itself_catches_every_form(self):
        """番人が実際に発火することを確かめる（空を返し続ける番人を防ぐ）。"""
        forms = (
            "import gemini_record",
            "from gemini_record import recording",
            'importlib.import_module("gemini_record")',
            '__import__("gemini_record")',
        )
        for source in forms:
            with self.subTest(form=source):
                with tempfile.NamedTemporaryFile(
                        "w", suffix=".py", delete=False, encoding="utf-8") as f:
                    f.write('"""gemini_record の説明は docstring なら許す。"""\n')
                    f.write(source + "\n")
                    path = f.name
                try:
                    self.assertNotEqual(_gemini_record_references(path), [])
                finally:
                    os.unlink(path)


# ------------------------------------------------------------------ T-5-NF

class ReplayNeverCallsGeminiTest(unittest.TestCase):
    """T-5-NF: 本 Plan の目的そのもの。ここが緑でないと全部が無意味。"""

    def test_replay_does_not_touch_the_model(self):
        with recorded_dir({"a": 1}, {"b": 2}) as directory:
            with replaying_quietly(directory):
                first = ocr_engine._call_gemini_text("OCR", "P0")
                second = ocr_engine._call_gemini_text("OCR", "P1")
        self.assertEqual([first, second], [{"a": 1}, {"b": 2}])


# ------------------------------------------------------------- §5 の異常系

class ReplayErrorSemanticsTest(unittest.TestCase):
    """§5 の異常系。**すべて例外**（黙って実 Gemini へ兜底しない）。"""

    def test_e1_matching_recording_is_returned(self):
        with recorded_dir({"a": 1}) as directory:
            with replaying_quietly(directory):
                self.assertEqual(ocr_engine._call_gemini_text("OCR", "P0"),
                                 {"a": 1})

    def test_e2_prompt_drift_raises_and_names_the_part(self):
        with recorded_dir({"a": 1}) as directory:
            with replaying_quietly(directory):
                with self.assertRaises(
                        gemini_record.ReplayMismatchError) as caught:
                    ocr_engine._call_gemini_text("OCR", "CHANGED-PROMPT")
        self.assertIn("prompt", str(caught.exception))

    def test_e2_ocr_drift_is_distinguishable_from_prompt_drift(self):
        """R-3（PaddleOCR の揺れ）を自分の prompt 改修と区別できること。"""
        with recorded_dir({"a": 1}) as directory:
            with replaying_quietly(directory):
                with self.assertRaises(
                        gemini_record.ReplayMismatchError) as caught:
                    ocr_engine._call_gemini_text("DIFFERENT-OCR", "P0")
        message = str(caught.exception)
        self.assertIn("ocr", message)
        self.assertNotIn("prompt", message)

    def test_e3_accept_drift_continues_and_records_the_fact(self):
        with recorded_dir({"a": 1}) as directory:
            with replaying_quietly(directory,
                                   accept_drift=("ocr",)) as session:
                got = ocr_engine._call_gemini_text("DIFFERENT-OCR", "P0")
                log = session.stdout.getvalue()
        self.assertEqual(got, {"a": 1})
        self.assertEqual(len(session.drifts), 1)
        self.assertIn("ocr", log)

    def test_e3_accept_drift_does_not_forgive_other_parts(self):
        """許した部位**以外**も違ったら止まる。"""
        with recorded_dir({"a": 1}) as directory:
            with replaying_quietly(directory, accept_drift=("ocr",)):
                with self.assertRaises(gemini_record.ReplayMismatchError):
                    ocr_engine._call_gemini_text("DIFFERENT-OCR",
                                                 "CHANGED-PROMPT")

    def test_e6_unexplained_text_drift_is_never_forgiven_by_ocr(self):
        """定型文だけの改変は `--accept-drift ocr` では許されない（v3 §12）。

        `ocr` が動けば `text` も動くので、`text` の差は「説明が付いた」ものとして
        許す。しかし **`text` だけ**が動いたなら、それは連結の定型文や区切りが
        変わったということで、送った物が変わっている。許してはならない。
        """
        with recorded_dir({"a": 1}) as directory:
            with replaying_quietly(directory, accept_drift=("ocr",)):
                with gemini_record.call_context("text", "P0", "OCR"):
                    with self.assertRaises(
                            gemini_record.ReplayMismatchError) as caught:
                        ocr_engine._generate_content_with_retry(
                            ["P0\n\n--DIFFERENT-JOINER---\nOCR"])
        self.assertIn("text", str(caught.exception))

    def test_e7_call_kind_mismatch_stops(self):
        with recorded_dir({"a": 1}) as directory:
            with replaying_quietly(directory):
                with self.assertRaises(
                        gemini_record.ReplayMismatchError) as caught:
                    ocr_engine._call_gemini_cross_validate(
                        "OCR", b"img", "image/jpeg", "P0")
        self.assertIn("call_kind", str(caught.exception))

    def test_e4_exhausted_recordings_raise(self):
        with recorded_dir({"a": 1}) as directory:
            with replaying_quietly(directory):
                ocr_engine._call_gemini_text("OCR", "P0")
                with self.assertRaises(
                        gemini_record.ReplayExhaustedError) as caught:
                    ocr_engine._call_gemini_text("OCR", "P0")
        self.assertIn("2", str(caught.exception))

    def test_e5_empty_directory_raises_on_enter(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(gemini_record.RecordingMissingError):
                with gemini_record.replaying(directory):
                    pass

    def test_e5_missing_directory_raises_on_enter(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(gemini_record.RecordingMissingError):
                with gemini_record.replaying(os.path.join(directory, "nope")):
                    pass

    def test_t1f_unclassified_call_is_fail_closed_in_both_modes(self):
        """側路が立っていない呼出は録音時も再生時も例外（v3 §12）。

        黙って分類不能のまま録ると、使えない fixture が静かに溜まる。
        """
        with recorded_dir({"a": 1}) as directory:
            with replaying_quietly(directory):
                with self.assertRaises(gemini_record.SideChannelMissingError):
                    ocr_engine._call_gemini_parts(["bare"])
        with tempfile.TemporaryDirectory() as directory:
            fake = mock.MagicMock()
            fake.generate_content.return_value = gemini_response(text="{}")
            with mock.patch.object(ocr_engine, "model", fake):
                with gemini_record.recording(directory):
                    with self.assertRaises(
                            gemini_record.SideChannelMissingError):
                        ocr_engine._call_gemini_parts(["bare"])


# ------------------------------------------------------------------- T-3a

class RecordReplayRoundTripTest(unittest.TestCase):
    """T-3a: 録音 → 再生で結果が逐字同一。**揺れる model** で証明する。

    固定応答の model で往復させても「再生が効いた」証拠にならない
    ——録音を無視して実 model を呼んでも同じ結果になるからである。
    2 回目に**違う応答**を返す model を置き、それでも録音時の値が返ることで
    再生経路を通ったことを示す。
    """

    def test_roundtrip_is_identical_even_when_the_model_drifts(self):
        with tempfile.TemporaryDirectory() as directory:
            drifting = mock.MagicMock()
            drifting.generate_content.side_effect = [
                gemini_response(text=json.dumps({"v": "recorded"})),
                gemini_response(text=json.dumps({"v": "DRIFTED"})),
            ]
            with mock.patch.object(ocr_engine, "model", drifting):
                with gemini_record.recording(directory):
                    recorded = ocr_engine._call_gemini_text("OCR", "P")
                with gemini_record.replaying(directory):
                    replayed = ocr_engine._call_gemini_text("OCR", "P")
        self.assertEqual(recorded, {"v": "recorded"})
        self.assertEqual(replayed, recorded)
        self.assertEqual(drifting.generate_content.call_count, 1)

    def test_line_mode_budget_is_part_of_the_key(self):
        """`line_mode` は generation_config を変える。鍵に入っていること。"""
        with recorded_dir({"a": 1}, line_mode=False) as directory:
            with replaying_quietly(directory):
                with self.assertRaises(
                        gemini_record.ReplayMismatchError) as caught:
                    ocr_engine._call_gemini_text("OCR", "P0", line_mode=True)
        self.assertIn("config", str(caught.exception))

    def test_recordings_survive_a_process_boundary(self):
        """録音は**ファイル**であること（プロセス内 dict では回帰に使えない）。"""
        with recorded_dir({"a": 1}) as directory:
            entries = sorted(os.listdir(directory))
            self.assertTrue(entries, "録音ディレクトリが空")
            files = sorted(os.listdir(os.path.join(directory, entries[0])))
            self.assertEqual(files,
                             ["contents.json", "meta.json", "response.json"])


# ------------------------------------------------------------------- T-5a

class FixtureSecrecyTest(unittest.TestCase):
    """T-5a: 録音資料が PUBLIC repo に入らないこと。

    「`git check-ignore` が効くか」では**不十分**（Codex 高 4）——
    それは通常の追加しか防げず、`git add -f` と既に tracked になった
    fixture を素通しする。「規則が存在するか」ではなく
    **実際に追跡されている物があるか**を見る。
    """

    def test_no_fixture_is_tracked_by_git(self):
        tracked = subprocess.run(
            ["git", "ls-files", "fixtures", "fixtures/**"],
            cwd=_HERE, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(tracked, "", f"実票データが追跡されている: {tracked}")

    def test_gitignore_declares_fixtures(self):
        with open(os.path.join(_HERE, ".gitignore"), encoding="utf-8") as f:
            self.assertIn("fixtures/", f.read())


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------- Codex 実装評審（2026-08-20）の回帰

class PatchTargetRegistryTest(unittest.TestCase):
    """番人の母集合が実装と食い違わないこと（Codex 低 6）。"""

    def test_patch_targets_covers_the_retry_funnel_and_every_variant(self):
        self.assertEqual(
            set(gemini_record.PATCH_TARGETS),
            {"_generate_content_with_retry"}
            | set(gemini_record._VARIANT_WRAPPERS),
            "PATCH_TARGETS と実際に差し替える関数の集合がずれている")


class RecordingDirectoryTest(unittest.TestCase):
    """既存の録音の上に録り直すと、古い slot が混ざる（Codex 高 3）。

    9 回録ったあとに 5 回録り直すと `0005`〜`0008` が残り、再生時の
    録音数も fingerprint の候補も実行と食い違う。**黙って混ざる**のが悪い。
    """

    def test_recording_refuses_a_directory_that_already_has_slots(self):
        with recorded_dir({"a": 1}) as directory:
            with self.assertRaises(gemini_record.RecordError) as caught:
                with gemini_record.recording(directory):
                    pass
            self.assertIn("overwrite", str(caught.exception))

    def test_overwrite_clears_the_old_slots(self):
        with recorded_dir({"a": 1}, {"b": 2}) as directory:
            self.assertEqual(len(gemini_record.load_recordings(directory)), 2)
            fake = mock.MagicMock()
            fake.generate_content.return_value = gemini_response(
                text=json.dumps({"c": 3}))
            with mock.patch.object(ocr_engine, "model", fake):
                with gemini_record.recording(directory, overwrite=True):
                    ocr_engine._call_gemini_text("OCR", "P0")
            remaining = gemini_record.load_recordings(directory)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].response["text"], json.dumps({"c": 3}))

    def test_recording_into_a_fresh_directory_is_fine(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = os.path.join(parent, "new")
            with gemini_record.recording(directory):
                pass
            self.assertTrue(os.path.isdir(directory))


class ReplayCompletenessTest(unittest.TestCase):
    """使われなかった録音を黙って見逃さない（Codex 中 5）。

    9 件録ったのに 5 件しか使われなかった = **4 頁が処理されていない**。
    IP-401（54 枚上げて 53 件しか記帳されず、枚数を数えるまで気付けなかった）
    と同族の無音欠落なので、再生層で止める。
    """

    def test_unconsumed_recordings_raise_on_exit(self):
        with recorded_dir({"a": 1}, {"b": 2}) as directory:
            with self.assertRaises(gemini_record.ReplayIncompleteError) as caught:
                with replaying_quietly(directory):
                    ocr_engine._call_gemini_text("OCR", "P0")
        message = str(caught.exception)
        self.assertIn("1", message)

    def test_full_consumption_exits_cleanly(self):
        with recorded_dir({"a": 1}, {"b": 2}) as directory:
            with replaying_quietly(directory):
                ocr_engine._call_gemini_text("OCR", "P0")
                ocr_engine._call_gemini_text("OCR", "P1")

    def test_a_body_exception_is_not_masked_by_the_completeness_check(self):
        """本体が落ちているのに「未消費だ」で上書きすると原因が消える。"""
        with recorded_dir({"a": 1}, {"b": 2}) as directory:
            with self.assertRaises(RuntimeError):
                with gemini_record.replaying(directory):
                    raise RuntimeError("本体の失敗")

    def test_a_mismatch_during_the_run_is_not_masked_either(self):
        """不一致で既に大声で落ちた後、未消費で二重に叱らない。"""
        with recorded_dir({"a": 1}, {"b": 2}) as directory:
            with replaying_quietly(directory):
                with self.assertRaises(gemini_record.ReplayMismatchError):
                    ocr_engine._call_gemini_text("OCR", "CHANGED")


class ReplayOrderTest(unittest.TestCase):
    """呼出順が変わっても再生は通す。ただし**黙っては通さない**（Codex 高 4）。

    順序を主キーにしない理由は AD-4 で 3 ラウンド裁決済み ——
    コードの差分が「録音の取り違え」として現れ、無関係な行まで全部ずれる。
    よって順序非依存の照合は残す。だが「順序が変わった」こと自体は
    コードが変わった証拠なので、記録して人に見せる。
    """

    def test_out_of_order_replay_succeeds_but_is_reported(self):
        with recorded_dir({"a": 1}, {"b": 2}) as directory:
            with replaying_quietly(directory) as session:
                second = ocr_engine._call_gemini_text("OCR", "P1")
                first = ocr_engine._call_gemini_text("OCR", "P0")
                log = session.stdout.getvalue()
        self.assertEqual([second, first], [{"b": 2}, {"a": 1}])
        self.assertEqual(len(session.reorders), 2)
        self.assertIn("順序", log)

    def test_in_order_replay_reports_nothing(self):
        with recorded_dir({"a": 1}, {"b": 2}) as directory:
            with replaying_quietly(directory) as session:
                ocr_engine._call_gemini_text("OCR", "P0")
                ocr_engine._call_gemini_text("OCR", "P1")
        self.assertEqual(session.reorders, [])


class DriftReuseTest(unittest.TestCase):
    """差分を許す経路が、既に消費した録音を二度使わないこと。

    Codex 評審後の自己点検で見つけた欠陥。`resolve` は fingerprint 一致で
    先に `recordings[1]` を消費していても、次の呼出が漂移すると
    `recordings[index=1]` を**もう一度**返してしまう。
    `unused` からの削除は「まだ入っていれば」なので黙って素通りする。

    症状は「1 件の録音が 2 回答える ＋ 別の録音が未消費で残る」で、
    未消費検査（E-6）が最後に叫ぶまで気付けない。それでは遅い ——
    その間の仕訳は**間違った応答**から組まれている。
    """

    def test_drift_never_reuses_a_consumed_recording(self):
        with recorded_dir({"a": 1}, {"b": 2}) as directory:
            with replaying_quietly(directory,
                                   accept_drift=("prompt",)) as session:
                # 1) 順序を入れ替えて呼ぶ → fingerprint 一致で seq=1 を消費
                first = ocr_engine._call_gemini_text("OCR", "P1")
                # 2) 録音に無い prompt → 差分許可の経路へ。seq=1 は消費済みなので
                #    残っている seq=0 を使わねばならない
                second = ocr_engine._call_gemini_text("OCR", "DRIFTED")
        self.assertEqual(first, {"b": 2})
        self.assertEqual(second, {"a": 1},
                         "消費済みの録音を二度使っている")
        self.assertEqual(session.calls, 2)

    def test_drift_after_everything_is_consumed_raises(self):
        with recorded_dir({"a": 1}) as directory:
            with replaying_quietly(directory, accept_drift=("prompt",)):
                ocr_engine._call_gemini_text("OCR", "P0")
                with self.assertRaises(gemini_record.ReplayExhaustedError):
                    ocr_engine._call_gemini_text("OCR", "DRIFTED")
