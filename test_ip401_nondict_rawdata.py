"""IP-401: `raw_data` が dict でないときページが無音で消える欠陥の回帰テスト。

Plan: `docs/plans/2026-08-17-ip401-nondict-rawdata.md`

守る不変式は 1 つ——**進入したページは必ず 1 件以上 yield する**。
ここで固定するのはその不変式が破れていた 3 つの経路である:

1. truthy 非 dict（Gemini が JSON 配列を返す）→ `_apply_ocr_overrides` が
   `AttributeError` → 尾段は最外 except で 0 件、逐頁は `_page_error`
2. falsy / None（Vision 兜底も空）→ 尾段が `return` で 0 件
3. 整形段階の例外一般 → 尾段は裸の for なので 0 件

1 だけが終態を変える（保持・再試行 → 占位行を書いて歸檔）。2 と 3 は
終態不変で、不変式とカバレッジ哨戒が効くようになるだけ（Plan §3.4）。

実 API は呼ばない（`test_ip401_regression` の Codex 低2 裁決を踏襲）。

    venv311/bin/python -m unittest test_ip401_nondict_rawdata -v
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import main
import ocr_engine
from doc_types import DocType
from ocr_test_helpers import (
    page_ocr_from_tuple, page_ocrs_from_tuples, pdf_pages,
)
from page_progress import (
    OUTCOME_FAILED, OUTCOME_PLACEHOLDER, STATUS_COMPLETED,
    STATUS_FAILED_RETAINED, STATUS_PARTIAL_ERROR,
)
from sheets_output import APPEND_RESULT_PLACEHOLDER, APPEND_RESULT_POSTED

NORMAL_OCR = "領収書 テスト店 合計1,100円 現金"

# 社保通知書の券面キーワード。§3.1 の「形式不正優先」裁決を固定する
# テスト（`SocialInsurancePrecedenceTest`）で使う。
SOCIAL_INSURANCE_OCR = "健康保険・厚生年金保険 保険料納入告知額・領収済額通知書"


def _normal_gemini():
    return {"documents": [{
        "doc_category": "receipt", "payment_method": "現金",
        "vendor": "テスト店",
        "items": [{"description": "商品", "amount": 1100,
                   "tax_rate": 0.10, "debit_account": "備品・消耗品費"}],
    }]}


def _boom(*args, **kwargs):
    """最初の `next()` で例外を投げる `_yield_page_results` の代役。

    3 箇所で使うので 1 つに寄せる（コピーすると片方にだけ手が入って
    「同じことを試しているつもりで違うことを試している」状態になる）。
    """
    raise RuntimeError("boom")
    yield  # pragma: no cover — generator にするためだけ


def _half(*args, **kwargs):
    """1 件 yield したあとで例外を投げる `_yield_page_results` の代役。

    entries の中身は検査対象ではない（見るのは件数と占位の有無だけ）が、
    実在しうる形にしておく。
    """
    yield {"date": "2026/07/18", "vendor": "テスト店",
           "invoice_num": "", "memo": "",
           "entries": [{"debit_account": "備品・消耗品費", "amount": 1100}]}
    raise RuntimeError("boom")


def _run_single_page(raw_data, ocr_text="", doc_type=DocType.RECEIPT,
                     fallback=None):
    """尾段（画像 / 単ページ PDF）を通し、yield された全件を返す。

    形は `test_ocr_engine_invoice._run_single_page_pipeline` と同じ。
    共通化しない理由は `_run_paged_pdf` の docstring を参照。

    Args:
        fallback: `_call_gemini`（Vision 兜底）の戻り値。raw_data が falsy の
            ときだけ効く。
    """
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(b"dummy")
        path = tmp.name
    try:
        with mock.patch.object(ocr_engine, "_split_pdf_pages",
                               return_value=iter([])), \
             mock.patch.object(ocr_engine, "_route_ocr_strategy",
                               return_value=page_ocr_from_tuple(
                                   (raw_data, ocr_text, 0.9), doc_type)), \
             mock.patch.object(ocr_engine, "_call_gemini",
                               return_value=fallback):
            buf = io.StringIO()
            with redirect_stdout(buf):
                out = list(ocr_engine.process_pipeline(
                    path, doc_type=doc_type, ocr_strategy="C"))
        return out, buf.getvalue()
    finally:
        os.unlink(path)


def _run_paged_pdf(routes, doc_type=DocType.RECEIPT, declared_total=None,
                   start_page=1):
    """逐頁 PDF 経路を通す（`test_ip401_regression._run_pipeline` と同形）。

    Args:
        declared_total: producer が申告する総頁数。`len(routes)` と食い違わせ
            ると「中途で尽きた」状況になる（`_split_pdf_pages` は例外を自分で
            握って静かに return するので、消費側から見えるのは産出数が申告より
            少ないことだけ）。既定 None なら `len(routes)`。
        start_page: `process_pipeline` へ渡す開始頁（`local_test.py
            --start-page N` 相当）。

    同形の runner が既に 2 つある（`test_ip401_regression._run_pipeline` と
    `test_ocr_engine_invoice._run_single_page_pipeline`）。共通化しないのは
    「テストファイル間 import ができないから」ではない —— それは実際には
    可能で、`test_ocr_engine_invoice.py:57` が既にやっている。理由は
    パラメータ集合が三者三様（こちらは `fallback` / `ocr_text` / stdout 捕捉が
    要る）で、束ねるには 3 つの既存テストの改造が要り、本件の範囲を超えるため。
    ページ dict の生成だけは共有した（`ocr_test_helpers.pdf_pages`）。
    """
    pages = pdf_pages(len(routes), total_pages=declared_total)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(b"%PDF-1.4 dummy")
        path = tmp.name
    try:
        with mock.patch.object(ocr_engine, "_split_pdf_pages",
                               return_value=iter(pages)), \
             mock.patch.object(ocr_engine, "_route_ocr_strategy",
                               side_effect=page_ocrs_from_tuples(
                                   routes, doc_type)), \
             mock.patch.object(ocr_engine, "_call_gemini_bytes",
                               return_value=None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                out = list(ocr_engine.process_pipeline(
                    path, doc_type=doc_type, ocr_strategy="C",
                    start_page=start_page))
        return out, buf.getvalue()
    finally:
        os.unlink(path)


class TruthyNonDictGateTest(unittest.TestCase):
    """Gemini が JSON 配列（等）を返したページが消えないこと。"""

    def test_single_page_nondict_yields_placeholder(self):
        # Arrange: extract_json の arr_match 分岐が返す形
        raw = ["bad"]

        # Act
        out, _ = _run_single_page(raw)

        # Assert: 頁は消えず、**型ゲート由来**の占位である
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["result"].get("_unrecognized"))
        # `_page_error` が立たない＝歸檔される側。立つと main が Failed →
        # ファイル保持 → 再試行になり、AI が同じ構造を返し続ける限り永久
        # ループになる（Plan §3.1）。
        #
        # この 1 行は分類の主張であると同時に、この test がゲートを本当に
        # 見張っている証でもある: `_unrecognized` だけを見ていると、ゲートを
        # 外しても尾段の例外境界が拾った占位——そちらも `_unrecognized=True`
        # を含む——で緑のまま通ってしまう（変異検証で実測した）。
        self.assertFalse(out[0]["result"].get("_page_error"))

    def test_paged_pdf_nondict_yields_unrecognized_not_page_error(self):
        # Arrange: p1 が配列、p2 は正常
        routes = [(["bad"], "", 0.9), (_normal_gemini(), NORMAL_OCR, 0.95)]

        # Act
        out, _ = _run_paged_pdf(routes)

        # Assert: 2 頁とも出力され、p1 は歸檔される側の分類
        self.assertEqual({p["page_num"] for p in out}, {1, 2})
        by_page = {p["page_num"]: p["result"] for p in out}
        self.assertTrue(by_page[1].get("_unrecognized"))
        self.assertFalse(by_page[1].get("_page_error"))
        # p2 は無傷
        self.assertEqual(len(by_page[2]["entries"]), 1)

    def test_nondict_scalar_types_are_gated(self):
        """list 決め打ちにしない。`json.loads` は str/int/bool も返す（F2）。"""
        for raw in ("文字列だけ", 123, 4.5, True):
            with self.subTest(raw=raw):
                # Act
                out, _ = _run_single_page(raw)

                # Assert: 型ゲート由来の占位（`_page_error` を併せて見る理由は
                # test_single_page_nondict_yields_placeholder のコメント参照）
                self.assertEqual(len(out), 1)
                self.assertTrue(out[0]["result"].get("_unrecognized"))
                self.assertFalse(out[0]["result"].get("_page_error"))

    def test_placeholder_memo_names_the_type(self):
        """無人運用では Sheets の 1 行が唯一の診断材料なので型名を残す。"""
        # Act
        out, _ = _run_single_page(["bad"])

        # Assert
        memo = out[0]["result"].get("memo", "")
        self.assertIn("AI応答形式不正", memo)
        self.assertIn("list", memo)

    def test_dict_rawdata_path_is_unchanged(self):
        """無回帰の錨: 正常 dict では余分な yield が 1 件も増えない。"""
        # Act
        out, _ = _run_single_page(_normal_gemini(), ocr_text=NORMAL_OCR)

        # Assert
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["result"].get("_unrecognized"))
        self.assertEqual(len(out[0]["result"]["entries"]), 1)


class SocialInsurancePrecedenceTest(unittest.TestCase):
    """§3.1 の裁決「形式不正を社保判定より優先する」を固定する。

    Codex 評審 #5 が指摘した論点。型は「見た」事実、社保判定はキーワードに
    よる啓発法であり、確定した事実を啓発法に上書きさせない（IP-401 T1 が
    封筒判定を事後説明器へ降格したのと同じ原理）。両経路とも仕訳を 1 件も
    作らないので帳簿リスクは同一であり、差は顧客が読む文言だけである。

    意図した選択であってバグではない。後続の評審者がここを蒸し返さないよう
    テストとして残す。
    """

    def test_nondict_wins_over_social_insurance_keywords(self):
        # Arrange: OCR は社保通知に強命中しているが Gemini は配列を返した
        # Act
        out, _ = _run_single_page(["bad"], ocr_text=SOCIAL_INSURANCE_OCR)

        # Assert: 形式不正として記録される（社保の除外行にはならない）
        self.assertEqual(len(out), 1)
        result = out[0]["result"]
        self.assertTrue(result.get("_unrecognized"))
        self.assertFalse(result.get("_excluded_page"))
        self.assertIn("AI応答形式不正", result.get("memo", ""))

    def test_social_insurance_still_wins_when_rawdata_is_a_dict(self):
        """無回帰の錨: dict なら従来どおり社保判定が効く。"""
        # Act
        out, _ = _run_single_page(_normal_gemini(),
                                  ocr_text=SOCIAL_INSURANCE_OCR)

        # Assert
        self.assertTrue(out[0]["result"].get("_excluded_page"))
        self.assertEqual(out[0]["result"]["entries"], [])


class TailExceptionBoundaryTest(unittest.TestCase):
    """尾段の整形例外が頁を丸ごと飲み込まないこと（P-1・趙裁定 08-17）。

    逐頁ループは `while True: next()` を try で包んでいるのに、尾段だけが
    裸の for だった。現時点で実害は無い（例外源が最初の next() までに
    完走する）が、T5 が builder を流式化した瞬間「1 件 yield 後に例外」が
    成立し、count>0 → Success → 歸檔で真の無音欠落になる。
    """

    def test_tail_formatting_exception_does_not_swallow_the_page(self):
        # Act
        with mock.patch.object(ocr_engine, "_yield_page_results",
                               side_effect=_boom):
            out, _ = _run_single_page(_normal_gemini())

        # Assert: 頁は消えず、保持・再試行される側の分類
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["result"].get("_page_error"))
        self.assertIn("整形処理エラー", out[0]["result"]["memo"])

    def test_tail_partial_yield_then_exception_is_visible(self):
        """成功分と占位行の両方が出る（歸檔で無音に消えない）。"""
        # Act
        with mock.patch.object(ocr_engine, "_yield_page_results",
                               side_effect=_half):
            out, _ = _run_single_page(_normal_gemini())

        # Assert
        self.assertEqual(len(out), 2)
        self.assertEqual(len(out[0]["result"]["entries"]), 1)
        self.assertTrue(out[1]["result"].get("_page_error"))

    def test_tail_placeholder_shape_matches_paged_loop(self):
        """尾段と逐頁の占位 result が同一形状であること。

        片方だけ直す漂移を機械的に禁じる。CLAUDE.md が記録している
        ENTRY_BUILDERS 未登録事故と同族の失敗（2 箇所平行メンテ）を防ぐ。

        限界を 2 つ明記しておく:

        1. 见ているのは占位 dict の**形**であって「どんなときに占位へ落ちるか」
           ではない。実際、simcodex Round 1 で尾段の Vision 兜底が try の外に
           ある（＝逐頁には在る保護が尾段に無い）ことが**この test を素通り
           して**見つかった。構造的に塞ぐには両経路を 1 つの generator へ
           寄せるしかない（Plan §11.1 の申し送り）。
        2. 相互比較だけだと、両者が共有する `_page_error_payload` 自身が
           壊れたときに**両側が同じように壊れて集合は一致したまま**になる
           （simcodex Round 2 の指摘）。そこで下では相互比較に加えて
           **契約そのものを字面で**押さえる —— 消費側 `main.process_file` が
           読む键（`_page_error` / `_unrecognized` / `entries`）は、
           片方が消えれば成否判定と原票リンクが静かにずれる。
        """
        # Act
        with mock.patch.object(ocr_engine, "_yield_page_results",
                               side_effect=_boom):
            tail_out, _ = _run_single_page(_normal_gemini())
            paged_out, _ = _run_paged_pdf([(_normal_gemini(), NORMAL_OCR, 0.9)])

        # Assert: (1) 両経路が同形
        self.assertEqual(set(tail_out[0]["result"]),
                         set(paged_out[0]["result"]))
        # (2) その「同形」が正しい形であること（共有 helper 自体の変異を捕る）
        for label, out in (("tail", tail_out), ("paged", paged_out)):
            with self.subTest(path=label):
                result = out[0]["result"]
                self.assertIs(result["_page_error"], True)
                self.assertIs(result["_unrecognized"], True)
                self.assertEqual(result["entries"], [])
                self.assertIn("整形処理エラー", result["memo"])

    def test_tail_vision_fallback_exception_does_not_swallow_the_page(self):
        """Vision 兜底が例外を投げても頁は消えない（simcodex R1・altitude）。

        `_call_gemini` は `_generate_content_with_retry` の `raise last_err`
        を素通しするので、再試行を使い切ると例外が上がる。逐頁ループでは
        同じ呼び出しが per-page try の中に在るが、尾段だけ裸だった
        —— G5「尾段に 0 件 yield で終わる経路を 1 本も残さない」の漏れ。
        """
        # Arrange: PaddleOCR は空、Vision 兜底が例外
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(b"dummy")
            path = tmp.name
        try:
            with mock.patch.object(ocr_engine, "_split_pdf_pages",
                                   return_value=iter([])), \
                 mock.patch.object(ocr_engine, "_route_ocr_strategy",
                                   return_value=page_ocr_from_tuple(
                                       (None, "", None), DocType.RECEIPT)), \
                 mock.patch.object(ocr_engine, "_call_gemini",
                                   side_effect=RuntimeError("gemini down")):
                with redirect_stdout(io.StringIO()):
                    # Act
                    out = list(ocr_engine.process_pipeline(
                        path, doc_type=DocType.RECEIPT, ocr_strategy="C"))
        finally:
            os.unlink(path)

        # Assert: 逐頁ループと同じ「ページ処理エラー」占位
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["result"].get("_page_error"))
        self.assertIn("ページ処理エラー", out[0]["result"]["memo"])


class TailFalsyRawDataTest(unittest.TestCase):
    """Vision 兜底も空だった尾段が 0 件で終わらないこと（Codex 評審 #1）。

    ここは `_page_error`（保持・再試行）のままで**終態は変わらない**。
    「AI から使える応答が無い」は 5xx・タイムアウト・レート制限で普通に
    起きる一時障害であり、再試行の価値がある（Plan §3.3 の分類表）。
    変わるのは IP-401 不変式が満たされることと、カバレッジ哨戒が
    機能するようになること（現状は last_total_pages=0 で鳴らない）。
    """

    def test_tail_falsy_rawdata_yields_page_error(self):
        # Arrange: PaddleOCR も Vision 兜底も空
        # Act
        out, _ = _run_single_page(None, fallback=None)

        # Assert
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["result"].get("_page_error"))
        self.assertIn("AI応答のJSON解析失敗", out[0]["result"]["memo"])

    def test_tail_falsy_variants_all_yield(self):
        """`[]` / `""` / `0` / `False` / `None` の 5 種すべてで 1 件以上。

        この 5 種を `None` と同じ分類に置くのは**意図的**である（Codex 評審
        #1 後半への回答。区別しても終態も行動も変わらないため分岐を増やさない）。
        意図であることをここで機械可読な形に固定する。
        """
        for raw in ([], "", 0, False, None):
            with self.subTest(raw=repr(raw)):
                # Act
                out, _ = _run_single_page(raw, fallback=None)

                # Assert
                self.assertEqual(len(out), 1)
                self.assertTrue(out[0]["result"].get("_page_error"))

    def test_tail_file_read_failure_does_not_swallow_the_page(self):
        """ファイル読取の例外でも頁は消えない（simcodex R2・zero-yield 監査）。

        逐頁側は `_split_pdf_pages` が自前の try で `open()` を包んで優雅に
        降格するが、尾段の `open()` は裸だった。無人運用の Windows ミニ PC
        では、ウイルス対策のリアルタイム走査が落としたばかりの一時ファイルを
        一瞬ロックする——現実に起きうる `PermissionError` である。
        """
        # Arrange
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(b"dummy")
            path = tmp.name
        try:
            with mock.patch.object(ocr_engine, "_split_pdf_pages",
                                   return_value=iter([])), \
                 mock.patch("builtins.open",
                            side_effect=PermissionError("locked by AV")):
                with redirect_stdout(io.StringIO()):
                    # Act
                    out = list(ocr_engine.process_pipeline(
                        path, doc_type=DocType.RECEIPT, ocr_strategy="C"))
        finally:
            os.unlink(path)

        # Assert: 保持・再試行される側の占位（次回スキャンで自癒する）
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["result"].get("_page_error"))
        self.assertIn("ページ処理エラー", out[0]["result"]["memo"])

    def test_tail_falsy_recovered_by_vision_fallback_is_unaffected(self):
        """無回帰の錨: 兜底が dict を返せば従来どおり正常処理される。"""
        # Act
        out, _ = _run_single_page(None, fallback=_normal_gemini())

        # Assert
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["result"].get("_page_error"))
        self.assertEqual(len(out[0]["result"]["entries"]), 1)


class SplitProducerContractTest(unittest.TestCase):
    """本件の修正が乗っている**前提**そのものを固定する。

    `TruncatedSplitTest` は `_split_pdf_pages` を丸ごと mock して「宣言より
    少なく産出して尽きる」状況を注入する。それは消費側から見える姿として
    正しいが、**producer が本当にそう振る舞うか**は 1 行も検査していない
    （suite 全体を見ても `_split_pdf_pages` 本体を駆動するテストは無く、
    常に mock 先だった）。

    前提が崩れる形は具体的である: 誰かが `except Exception` を `raise` に
    変えると、例外は消費側の `for` 文（per-page try の**外**）へ飛んで
    最外 except に落ち、補填ロジックには一度も到達しない —— 補填は静かに
    無力化されるのに、mock ベースのテストは全部緑のままになる。

    ここでは実物を走らせて「中途で失敗しても例外を外へ出さず、それまでの
    頁だけを産出して終わる」ことを押さえる。
    """

    def test_producer_swallows_midway_failure_and_stops_quietly(self):
        # Arrange: 3 頁の PDF を装い、2 頁目の書き出しで壊れる
        class _FakePage:
            pass

        class _FakeReader:
            def __init__(self, path):
                self.pages = [_FakePage(), _FakePage(), _FakePage()]

        calls = {"n": 0}

        class _FakeWriter:
            def __init__(self):
                calls["n"] += 1
                self._boom = calls["n"] == 2

            def add_page(self, page):
                if self._boom:
                    raise RuntimeError("corrupt page object")

            def write(self, buf):
                buf.write(b"%PDF-fake")

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(b"%PDF-1.4 dummy")
            path = tmp.name
        try:
            with mock.patch.object(ocr_engine, "PdfReader", _FakeReader), \
                 mock.patch.object(ocr_engine, "PdfWriter", _FakeWriter):
                with redirect_stdout(io.StringIO()):
                    # Act: 例外が外へ出るならここで送出される
                    produced = list(ocr_engine._split_pdf_pages(path))
        finally:
            os.unlink(path)

        # Assert: 1 頁だけ産出して静かに終わる（例外は外へ出ない）
        self.assertEqual([p["page_num"] for p in produced], [1])
        # 宣言した総頁数は壊れる前に確定しているので 3 のまま —— これが
        # 消費側で「宣言 3 / 実産出 1」の食い違いとして観測でき、補填が
        # 働く根拠になる。
        self.assertEqual(produced[0]["total_pages"], 3)


class TruncatedSplitTest(unittest.TestCase):
    """`_split_pdf_pages` が中途で尽きた頁が無音で消えないこと。

    従来はこの頁が逐頁ループに**一度も入らない**ため per-page try の埒外で、
    占位も作られず `seen_pages` にも載らなかった。前 k 頁が成功していると
    `count > 0` かつ `error_pages == 0` になるので main は Success 判定 →
    **歸檔**し、欠落頁の仕訳はどこにも入らず自動再試行も無い（留痕は監査
    タブの「欠落」1 行だけで、顧客が見る MF タブ側は無傷だった）。

    Plan: `docs/plans/2026-08-17-split-pdf-midway-failure.md`
    """

    def test_truncated_split_yields_placeholder_for_missing_pages(self):
        """欠落頁が占位として現れ、しかも**この経路由来**だと分かること。

        memo まで見る理由: `_page_error` だけだと、他の 3 経路
        （ページ処理エラー / AI応答のJSON解析失敗 / 整形処理エラー）が
        出した占位でも通ってしまい、補填が効いているのか別経路が
        たまたま拾ったのかを区別できない。「PDF分割が中断」は補填分岐に
        しか無い文字列なので、これで経路を特定する。
        """
        # Arrange: 3 頁と宣言しつつ p1 だけ産出して尽きる
        routes = [(_normal_gemini(), NORMAL_OCR, 0.95)]

        # Act
        out, _ = _run_paged_pdf(routes, declared_total=3)

        # Assert: 3 頁すべてが出力に現れる
        self.assertEqual({p["page_num"] for p in out}, {1, 2, 3})
        by_page = {p["page_num"]: p["result"] for p in out}
        self.assertEqual(len(by_page[1]["entries"]), 1)
        for miss in (2, 3):
            with self.subTest(page=miss):
                self.assertTrue(by_page[miss].get("_page_error"))
                # 汎用文言で埋もれさせない。顧客が集計行を見たとき
                # 「再アップロードで直るのか、原票が壊れているのか」を
                # 判断できる必要がある。
                self.assertIn("PDF分割が中断", by_page[miss]["memo"])

    def test_truncated_split_does_not_warn_twice(self):
        """占位を出した頁はもう「無音欠落」ではないので警告は出さない。

        カバレッジ哨戒は「一度も出力されなかった頁」への最終防衛なので、
        占位を出した頁まで警告すると哨戒が狼少年になる。

        警告文字列の**不在**だけを見ると、警告ブロックごと消す変異でも
        緑のまま通ってしまう（不在は「機構が正しく黙った」とも
        「機構が死んだ」とも解釈できる）。そこで同じ test の中に
        「補填自体は動いている」正の対照を置く。
        """
        # Arrange
        routes = [(_normal_gemini(), NORMAL_OCR, 0.95)]

        # Act
        out, log = _run_paged_pdf(routes, declared_total=3)

        # Assert: 正の対照 —— 補填は動いている
        self.assertEqual({p["page_num"] for p in out}, {1, 2, 3})
        # そのうえで、片付いた頁について警告は鳴らない
        self.assertNotIn("ページカバレッジ警告", log)

    def test_entered_but_silent_page_is_not_placeholdered_after_entered_pages(
            self):
        """§8-中7 の既存裁定を新機構が侵していないこと（H3 の番人）。

        「循環に入ったが何も出さなかった」頁は**警告のみ**という裁定は
        `test_ip401_regression.test_coverage_warning_fires_when_a_page_yields_nothing`
        が固定している。`entered_pages` を導入した後もその頁が占位化
        されないことを、こちら側からも確かめる（既存テストの写しではなく、
        新機構が既存裁定を侵していないことの検査）。
        """
        # Arrange: p1 は循環に入るが _yield_page_results が空を返す
        real = ocr_engine._yield_page_results
        calls = {"n": 0}

        def silent_first(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return iter([])
            return real(*args, **kwargs)

        routes = [(_normal_gemini(), NORMAL_OCR, 0.95),
                  (_normal_gemini(), NORMAL_OCR, 0.95)]

        # Act
        with mock.patch.object(ocr_engine, "_yield_page_results",
                               side_effect=silent_first):
            out, log = _run_paged_pdf(routes, declared_total=2)

        # Assert: p1 は占位化されず、従来どおり警告だけが出る。
        # 警告は「鳴った」だけでなく**どの頁について鳴ったか**まで見る
        # （`if missing:` を `if True:` にする変異が素通りするため）。
        self.assertEqual({p["page_num"] for p in out}, {2})
        self.assertIn("ページカバレッジ警告", log)
        self.assertIn("[1]", log)

    def test_start_page_skip_is_not_reported_as_missing(self):
        """`--start-page N` で意図的に飛ばした頁を欠落と誤報しない。

        当初この docstring は「`entered_pages` の記録位置が start_page
        スキップの後であることに依存し、前に動かすとこの test が落ちる」と
        書いていた。**変異検証したら落ちなかった** —— 差集合の被減数が
        `range(start_page, total+1)` なので、飛ばした頁番号はそもそも母集団に
        居らず、`entered_pages` に混ざっても結果は変わらない。
        この test が実際に守っているのは記録位置ではなく
        「`never_entered` の母集団が start_page 起点であること」である
        （母集団を `range(1, total+1)` に変えるとこの test は落ちる）。
        """
        # Arrange: 2 頁とも産出されるが start_page=2 で p1 を飛ばす
        routes = [(_normal_gemini(), NORMAL_OCR, 0.95),
                  (_normal_gemini(), NORMAL_OCR, 0.95)]

        # Act
        out, _ = _run_paged_pdf(routes, declared_total=2, start_page=2)

        # Assert: p1 の占位は作られない
        self.assertEqual({p["page_num"] for p in out}, {2})
        # 頁番号の集合だけでは足りない —— `idx < start_page` を
        # `idx <= start_page` にする変異では p2 自身がスキップされ、
        # 補填で占位に化ける。頁番号は {2} のままなので集合比較は素通りし、
        # 実データが失われたことに気づけない。中身まで見る。
        result = out[0]["result"]
        self.assertFalse(result.get("_page_error"))
        self.assertEqual(len(result["entries"]), 1)


class _RecordingWriter:
    """append_entries の呼び出しを記録する sheets_writer 代役。

    戻り値は本物の `sheets_output.append_entries` と同じ規則にする
    （entries が空なら占位行なので `APPEND_RESULT_PLACEHOLDER`）。ここを
    固定値にすると main の OUTCOME_PLACEHOLDER 判定が素通りしてしまう。

    `test_main_process_file.py` には代役が 2 つ在る —— 戻り値を返さない
    `_RecordingWriter` と、戻り値を 1 つ固定できる `_ReturnControlledWriter`。
    こちらが 3 つ目になるのは、本件のテストが「同じファイルの中で
    成功頁（POSTED）と占位頁（PLACEHOLDER）が混ざる」状況
    （`test_tail_partial_yield_exception_is_partial_error`）を見るからで、
    固定値では 1 回の実行で両方を出せない。3 つを 1 つに束ねるのは
    `test_main_process_file.py` 側の改造になり本件の範囲を超える。
    """

    def __init__(self):
        self.calls = []
        self.audit_calls = []

    def append_entries(self, employee_name, doc_type, entries_data, source_url):
        self.calls.append(entries_data)
        if entries_data.get("entries"):
            return APPEND_RESULT_POSTED
        return APPEND_RESULT_PLACEHOLDER

    def append_audit_row(self, filename, page_num, verdict, reason,
                         ocr_text_len, source_url=""):
        self.audit_calls.append({"page_num": page_num, "verdict": verdict,
                                 "reason": reason})


def _results_from_producer(raw_data, ocr_text="", doc_type=DocType.RECEIPT):
    """**本物の producer** から result dict を取り出す。

    main 側のテストで result dict を手書きすると、producer が marker 名を
    変えたときに consumer 側テストだけ緑のまま残る——「両方直したつもりで
    片方だけ直っている」という、この Plan がまさに防いでいる種類の欠陥を
    テストが再生産することになる。ここは実物を繋ぐ。
    """
    with redirect_stdout(io.StringIO()):
        return list(ocr_engine._yield_page_results(
            doc_type, raw_data, ocr_text, None))


def _run_process_file(pages, writer=None, progress=None):
    """process_pipeline を差替えて main.process_file を 1 回走らせる。

    形は `test_main_process_file._run_process_file` と同じ。
    """
    writer = writer if writer is not None else _RecordingWriter()
    with mock.patch.object(main, "process_pipeline", return_value=iter(pages)), \
         mock.patch.object(main, "send_notification"), \
         mock.patch.object(main, "PageUrlResolver") as resolver_cls:
        resolver_cls.return_value.resolve.return_value = "https://example/doc"
        with redirect_stdout(io.StringIO()):
            ok = main.process_file(
                service=mock.MagicMock(),
                sheets_writer=writer,
                file_path="/tmp/dummy.jpg",
                uploader_name="テスト社員",
                chat_id="",
                doc_type=DocType.RECEIPT,
                progress=progress,
            )
    return ok, writer


class ProcessFileTerminalStateTest(unittest.TestCase):
    """producer の分類が main の終態に正しく落ちること（Plan §3.4）。

    ここが本件の実質——「頁が 1 件 yield される」だけでは顧客は何も得ない。
    Sheets に行が出るか、ファイルが歸檔されるか再試行され続けるかが、
    無人運用の miniPC で顧客が体験する全てである。
    """

    @staticmethod
    def _nondict_pages():
        """本物の producer が出す占位 result をページ封筒に包む。

        result dict を手書きしないのが要点（`_results_from_producer` の
        docstring 参照）。3 つの test が同じ arrange を持つのでここに寄せる。
        """
        return [{"result": r, "page_num": 1, "total_pages": 1}
                for r in _results_from_producer(["bad"])]

    def test_nondict_single_page_is_archived_not_retained(self):
        """truthy 非 dict は歸檔（True）。永久再試行ループを断つ。"""
        # Arrange
        pages = self._nondict_pages()

        # Act
        ok, writer = _run_process_file(pages)

        # Assert
        self.assertTrue(ok)
        self.assertEqual(len(writer.calls), 1)

    def test_nondict_page_writes_placeholder_row(self):
        """占位行が Sheets へ渡り、摘要に原因（型名）が載ること。"""
        # Arrange
        pages = self._nondict_pages()

        # Act
        _, writer = _run_process_file(pages)

        # Assert: sheets_output._write_unrecognized_row は
        # _unrecognized が立っているときだけ memo を S 列へ通す
        written = writer.calls[0]
        self.assertTrue(written.get("_unrecognized"))
        self.assertEqual(written["entries"], [])
        self.assertIn("AI応答形式不正", written["memo"])
        self.assertIn("list", written["memo"])

    def test_nondict_page_emits_placeholder_outcome(self):
        # Arrange
        pages = self._nondict_pages()
        progress = mock.MagicMock()

        # Act
        _run_process_file(pages, progress=progress)

        # Assert
        outcomes = [c.args[2] for c in progress.page_done.call_args_list]
        self.assertEqual(outcomes, [OUTCOME_PLACEHOLDER])
        progress.file_finished.assert_called_once_with(STATUS_COMPLETED)

    def test_tail_first_next_exception_is_failed_retained(self):
        """尾段の整形例外は保持・再試行（§3.4 行 5「終態不変」）。

        ページは**尾段の実出力**を使う。占位 dict を手書きして main へ渡すと、
        尾段が実際にそれを産んでいるかを 1 行も検査しないテストになる
        （変異検証で実測: 手書き版は尾段を裸 for に戻しても緑のままだった）。
        """
        # Arrange: 最初の next() で例外を投げさせ、尾段の実出力を得る
        with mock.patch.object(ocr_engine, "_yield_page_results",
                               side_effect=_boom):
            pages, _ = _run_single_page(_normal_gemini())
        progress = mock.MagicMock()

        # Act
        ok, writer = _run_process_file(pages, progress=progress)

        # Assert: ファイル保持（False）／ Sheets には書かない。
        # 尾段が 0 件で終わると STATUS_PARSE_FAILED になり、この assert が落ちる
        self.assertFalse(ok)
        self.assertEqual(writer.calls, [])
        progress.file_finished.assert_called_once_with(STATUS_FAILED_RETAINED)

    def test_tail_partial_yield_exception_is_partial_error(self):
        """成功 1 件＋占位 1 件 → 歸檔＋集計行（無音欠落にならない）。

        現在の builder では起きないが、T5 が流式化した瞬間に効き始める錨。
        尾段が裸 for に戻ると 1 件目だけが残って STATUS_COMPLETED になる
        ——それが「Success と言いながらデータが欠けている」状態そのものなので、
        ここは終態まで見る。
        """
        # Arrange: 1 件 yield したあとで例外
        with mock.patch.object(ocr_engine, "_yield_page_results",
                               side_effect=_half):
            pages, _ = _run_single_page(_normal_gemini())
        progress = mock.MagicMock()

        # Act
        ok, writer = _run_process_file(pages, progress=progress)

        # Assert: 歸檔しつつ、成功分と部分エラー集計行の両方が書かれる
        self.assertTrue(ok)
        self.assertEqual(len(writer.calls), 2)
        self.assertEqual(len(writer.calls[0]["entries"]), 1)
        self.assertIn("ページ処理エラー", writer.calls[1]["memo"])
        progress.file_finished.assert_called_once_with(STATUS_PARTIAL_ERROR)

    def test_truncated_split_is_archived_with_summary_row(self):
        """欠落頁が在っても歸檔し、MF タブに集計行を残す（§3.2）。

        歸檔でよい理由: 前頁は既に Sheets へ書かれているので、保持して
        再試行するとその頁が重複計上される。顧客への通知は集計行が担う。
        """
        # Arrange: 尾段ではなく逐頁経路の実出力を使う（producer が尽きた形）
        pages, _ = _run_paged_pdf(
            [(_normal_gemini(), NORMAL_OCR, 0.95)], declared_total=3)
        progress = mock.MagicMock()

        # Act
        ok, writer = _run_process_file(pages, progress=progress)

        # Assert: 歸檔（True）／ 成功頁 ＋ 集計行の 2 行
        self.assertTrue(ok)
        self.assertEqual(len(writer.calls), 2)
        summary = writer.calls[1]["memo"]
        self.assertIn("ページ処理エラー", summary)
        # 頁の羅列は**そのままの形**で見る。`assertIn("p2")` を個別に
        # 並べると、補填範囲を `total+2` に広げて幻の p4 を作る変異が
        # 素通りする（p2/p3 は在るので個別 assertIn は全部通り、
        # writer.calls も集計行 1 行のまま変わらない）。
        self.assertIn("[p2,p3]", summary)
        progress.file_finished.assert_called_once_with(STATUS_PARTIAL_ERROR)

    def test_truncated_split_emits_failed_outcome_per_missing_page(self):
        # Arrange
        pages, _ = _run_paged_pdf(
            [(_normal_gemini(), NORMAL_OCR, 0.95)], declared_total=3)
        progress = mock.MagicMock()

        # Act
        _run_process_file(pages, progress=progress)

        # Assert: どの頁が失敗扱いかまで見る。件数だけを数えると、補填の
        # 頁番号を 1 ずらす変異（欠落 {2,3} が {3,4} になる）で件数が
        # 変わらず素通りし、実在しない頁を捏造しつつ p2 を落としたことに
        # 気づけない。
        by_page = {c.args[0]: c.args[2]
                   for c in progress.page_done.call_args_list}
        self.assertEqual(by_page[2], OUTCOME_FAILED)
        self.assertEqual(by_page[3], OUTCOME_FAILED)
        self.assertNotEqual(by_page[1], OUTCOME_FAILED)

    def test_page_error_memo_reaches_the_summary_row(self):
        """`_page_error` 頁の memo が集計行に届く（G11 の是正の番人）。

        本件の「PDF分割が中断」に限らず、既存の `_page_error`（AI応答の
        JSON解析失敗・整形処理エラー等）でも効く改善であることを、
        本件と無関係な memo で 1 件示す。従来は `failed_page_notes` が
        `_excluded_page` の監査失敗経路でしか埋まらず、集計行には頁番号
        しか出なかった。
        """
        # Arrange: 成功頁 ＋ 既存経路の _page_error 占位
        good = _results_from_producer(_normal_gemini(), ocr_text=NORMAL_OCR)
        pages = [{"result": good[0], "page_num": 1, "total_pages": 2},
                 ocr_engine._page_error_payload(
                     "AI応答のJSON解析失敗", 2, 2, None)]

        # Act
        _, writer = _run_process_file(pages)

        # Assert: memo が**どの頁のものとして**載ったかまで見る。
        # 本文の存在だけを見ると、`failed_page_notes[page_num]` を
        # `failed_page_notes[1]` のような固定キーにする変異が素通りする ——
        # 文字列は集計行に現れるが、顧客は壊れた頁を取り違える。それは
        # まさに G11 が是正しようとした失敗そのものである。
        self.assertIn("p2: AI応答のJSON解析失敗", writer.calls[1]["memo"])

    def test_tail_falsy_is_retained_not_archived(self):
        """falsy / None は従来どおり保持・再試行（§3.4 行 4）。

        ここも尾段の実出力を使う。0 件に戻ると STATUS_PARSE_FAILED になり、
        `file_finished` の assert が落ちる。
        """
        # Arrange
        pages, _ = _run_single_page(None, fallback=None)
        progress = mock.MagicMock()

        # Act
        ok, writer = _run_process_file(pages, progress=progress)

        # Assert
        self.assertFalse(ok)
        self.assertEqual(writer.calls, [])
        progress.file_finished.assert_called_once_with(STATUS_FAILED_RETAINED)


if __name__ == "__main__":
    unittest.main()
