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
    OUTCOME_PLACEHOLDER, STATUS_COMPLETED, STATUS_FAILED_RETAINED,
    STATUS_PARTIAL_ERROR,
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


def _run_paged_pdf(routes, doc_type=DocType.RECEIPT):
    """逐頁 PDF 経路を通す（`test_ip401_regression._run_pipeline` と同形）。

    同形の runner が既に 2 つある（`test_ip401_regression._run_pipeline` と
    `test_ocr_engine_invoice._run_single_page_pipeline`）。共通化しないのは
    「テストファイル間 import ができないから」ではない —— それは実際には
    可能で、`test_ocr_engine_invoice.py:57` が既にやっている。理由は
    パラメータ集合が三者三様（こちらは `fallback` / `ocr_text` / stdout 捕捉が
    要る）で、束ねるには 3 つの既存テストの改造が要り、本件の範囲を超えるため。
    ページ dict の生成だけは共有した（`ocr_test_helpers.pdf_pages`）。
    """
    pages = pdf_pages(len(routes))
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
                    path, doc_type=doc_type, ocr_strategy="C"))
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


def _run_paged_pdf_truncated(routes, declared_total, doc_type=DocType.RECEIPT,
                             start_page=1):
    """producer が `declared_total` を宣言しつつ `len(routes)` 頁で尽きる状況。

    `_split_pdf_pages` の中途失敗（k 頁目まで成功して k+1 頁目で `PdfReadError`）
    を模す。producer は例外を自分で握って静かに `return` するので、消費側から
    見えるのは「宣言より少ない頁数でジェネレータが尽きた」ことだけである
    —— 実装がその状況をどう扱うかがここで固定される。
    """
    pages = [{"page_num": i, "total_pages": declared_total,
              "data": f"%PDF-p{i}".encode(), "filename": f"p{i}.pdf"}
             for i in range(1, len(routes) + 1)]
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
        # Arrange: 3 頁と宣言しつつ p1 だけ産出して尽きる
        routes = [(_normal_gemini(), NORMAL_OCR, 0.95)]

        # Act
        out, _ = _run_paged_pdf_truncated(routes, declared_total=3)

        # Assert: 3 頁すべてが出力に現れる
        self.assertEqual({p["page_num"] for p in out}, {1, 2, 3})
        by_page = {p["page_num"]: p["result"] for p in out}
        self.assertEqual(len(by_page[1]["entries"]), 1)
        for miss in (2, 3):
            with self.subTest(page=miss):
                self.assertTrue(by_page[miss].get("_page_error"))

    def test_truncated_split_placeholder_names_the_cause(self):
        """汎用の「ページ処理エラー」で埋もれさせない。

        顧客が集計行を見たとき「再アップロードで直るのか、原票が壊れて
        いるのか」を判断できる必要がある。
        """
        # Arrange
        routes = [(_normal_gemini(), NORMAL_OCR, 0.95)]

        # Act
        out, _ = _run_paged_pdf_truncated(routes, declared_total=2)

        # Assert
        by_page = {p["page_num"]: p["result"] for p in out}
        self.assertIn("PDF分割が中断", by_page[2]["memo"])

    def test_truncated_split_does_not_warn_twice(self):
        """占位を出した頁はもう「無音欠落」ではないので警告は出さない。

        カバレッジ哨戒は「一度も出力されなかった頁」への最終防衛なので、
        占位を出した頁まで警告すると哨戒が狼少年になる。
        """
        # Arrange
        routes = [(_normal_gemini(), NORMAL_OCR, 0.95)]

        # Act
        _, log = _run_paged_pdf_truncated(routes, declared_total=3)

        # Assert
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
            out, log = _run_paged_pdf_truncated(routes, declared_total=2)

        # Assert: p1 は占位化されず、従来どおり警告だけが出る
        self.assertEqual({p["page_num"] for p in out}, {2})
        self.assertIn("ページカバレッジ警告", log)

    def test_start_page_skip_is_not_reported_as_missing(self):
        """`--start-page N` で意図的に飛ばした頁を欠落と誤報しない。

        `entered_pages` の記録位置が `if idx < start_page: continue` の
        **後**であることに依存する。前に置くと、飛ばした頁が「入った」
        ことになって欠落判定から漏れる——逆に言えば、記録位置を前に
        動かすとこの test は落ちる。
        """
        # Arrange: 2 頁とも産出されるが start_page=2 で p1 を飛ばす
        routes = [(_normal_gemini(), NORMAL_OCR, 0.95),
                  (_normal_gemini(), NORMAL_OCR, 0.95)]

        # Act
        out, _ = _run_paged_pdf_truncated(routes, declared_total=2,
                                          start_page=2)

        # Assert: p1 の占位は作られない
        self.assertEqual({p["page_num"] for p in out}, {2})


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
