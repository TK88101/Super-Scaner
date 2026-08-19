"""`_split_pdf_pages` の producer 契約と、0 件で終わる 3 つの出口の区別。

Plan: `docs/plans/2026-08-17-split-pdf-midway-failure.md` §12.1 ①② / §13

守る不変式は 2 つ:

1. **頁単位隔離** —— 頁 i の分割に失敗しても、頁 i+1 以降は失われない
   （旧実装は `try` が for ループ全体を包んでいたので、20 頁の 3 頁目が
   壊れると 3〜20 の 18 頁が丸ごと消えた）
2. **0 件出口の区別** —— 「正常な単頁 PDF」だけが尾段（ファイル全体を
   1 回の Gemini 呼出へ送る経路）へ落ちてよい。pypdf 未導入・PDF 読取
   失敗・全頁の書出失敗は `PdfSplitError` で尾段を禁じる。尾段へ落とすと
   大型 PDF で出力が MAX_TOKENS に達して JSON が途中切断される生産事故
   （`process_pipeline` の逐頁分岐冒頭のコメントに経緯がある）の再現経路になる

`_split_pdf_pages` 本体を**実物で駆動する**テストはこのファイルだけである
（他は全て mock 先）。前提が崩れる形は具体的で、たとえば誰かが per-page の
`continue` を `return` に戻すと補填ロジックは正しく動いたまま「失われる頁が
18 頁に戻る」——mock ベースのテストは全部緑のままになる。

実 API は呼ばない（pypdf も偽物に差し替える）。

    venv311/bin/python -m unittest test_pdf_split_contract -v
"""
import io
import os
import sys
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import ocr_engine
from doc_types import DocType
from ocr_test_fixtures import temp_pdf_path
from ocr_test_helpers import page_ocrs_from_tuples
# 標本は単一定義に収斂させる（`test_ocr_engine_invoice` が
# `_empty_invoice_raw` で採っているのと同じ方針）。複製すると片方だけ
# 直されて「同じことを試しているつもりで違うことを試す」状態になる。
from test_ip401_nondict_rawdata import NORMAL_OCR, _normal_gemini


# ── 偽 pypdf ────────────────────────────────────────────────
# 実際の破損 PDF を repo に置く代わりに、失敗する箇所を注入する。
# 注入点を 3 つ持つのは、`try` の範囲を狭める変異（たとえば
# `writer.write(buf)` だけ try の外へ出す）を殺すため。

class _FakePage:
    def __init__(self, num):
        self.num = num


class _FakePageList:
    """pypdf の `_VirtualList` 代役。**ランダムアクセス可**（H-1 の実測どおり）。"""

    def __init__(self, total, boom_pages=(), len_fails=False):
        self._pages = [_FakePage(i) for i in range(1, total + 1)]
        self._boom_pages = set(boom_pages)
        self._len_fails = len_fails

    def __len__(self):
        if self._len_fails:
            raise RuntimeError("page tree is broken")
        return len(self._pages)

    def __getitem__(self, idx):
        page = self._pages[idx]
        if page.num in self._boom_pages:
            raise RuntimeError(f"page access failed (p{page.num})")
        return page


def _reader_cls(total, boom_pages=(), reader_fails=None):
    class _FakeReader:
        def __init__(self, path):
            if reader_fails == "open":
                raise RuntimeError("EOF marker not found")
            self.pages = _FakePageList(
                total, boom_pages, len_fails=(reader_fails == "len"))
    return _FakeReader


def _writer_cls(boom_pages=(), boom_at="add_page"):
    boom_pages = set(boom_pages)

    class _FakeWriter:
        """頁番号で失敗を決める（呼ばれた順ではなく）。

        順序で決めると、先の頁が skip された瞬間に番号がずれて
        「狙ったのと違う頁を壊すテスト」になる。
        """

        def __init__(self):
            self._page = None

        def add_page(self, page):
            if boom_at == "add_page" and page.num in boom_pages:
                raise RuntimeError(f"add_page failed (p{page.num})")
            self._page = page

        def write(self, buf):
            if boom_at == "write" and self._page.num in boom_pages:
                raise RuntimeError(f"write failed (p{self._page.num})")
            buf.write(b"%PDF-fake")
    return _FakeWriter


@contextmanager
def _fake_pypdf(total=3, boom_pages=(), boom_at="add_page",
                reader_fails=None, missing=False):
    """`ocr_engine` が見る PdfReader / PdfWriter を差し替える。

    Args:
        total: `len(reader.pages)` が返す頁数。
        boom_pages: 失敗させる頁番号の集合。
        boom_at: 失敗させる箇所。"page_access" / "add_page" / "write"。
        reader_fails: "open"（`PdfReader()` が例外）/ "len"（`len(pages)`）。
        missing: True なら pypdf 未導入（両方 None）。
    """
    if missing:
        reader, writer = None, None
    else:
        page_boom = boom_pages if boom_at == "page_access" else ()
        writer_boom = () if boom_at == "page_access" else boom_pages
        reader = _reader_cls(total, page_boom, reader_fails)
        writer = _writer_cls(writer_boom, boom_at)
    with mock.patch.object(ocr_engine, "PdfReader", reader), \
         mock.patch.object(ocr_engine, "PdfWriter", writer):
        yield


def _run_producer(**kw):
    """実物の `_split_pdf_pages` を回して産出頁のリストを返す。"""
    with temp_pdf_path() as path, _fake_pypdf(**kw):
        with redirect_stdout(io.StringIO()):
            return list(ocr_engine._split_pdf_pages(path))


def _route(ocr_text=NORMAL_OCR):
    return (_normal_gemini(), ocr_text, 0.95)


def _run_pipeline(routes, start_page=1, **kw):
    """**実物の producer を通した** `process_pipeline`。

    `_run_paged_pdf`（`test_ip401_nondict_rawdata`）は producer を丸ごと
    mock するので、producer と消費側の結合はこちらでしか検査できない。

    Returns:
        (yield された全件, stdout, {"route": mock, "gemini": mock, ...})
    """
    with temp_pdf_path() as path, _fake_pypdf(**kw):
        with mock.patch.object(
                ocr_engine, "_route_ocr_strategy",
                side_effect=page_ocrs_from_tuples(routes)) as route, \
             mock.patch.object(ocr_engine, "_call_gemini_bytes",
                               return_value=None) as gemini_bytes, \
             mock.patch.object(ocr_engine, "_call_gemini",
                               return_value=None) as gemini:
            buf = io.StringIO()
            with redirect_stdout(buf):
                out = list(ocr_engine.process_pipeline(
                    path, doc_type=DocType.RECEIPT, ocr_strategy="C",
                    start_page=start_page))
    return out, buf.getvalue(), {
        "route": route, "gemini_bytes": gemini_bytes, "gemini": gemini}


class PagewiseIsolationTest(unittest.TestCase):
    """§12.1①: 壊れた頁 1 枚が後続頁を道連れにしない。"""

    def test_broken_page_is_skipped_and_later_pages_survive(self):
        """p2 が壊れても p3 は産出される（旧実装は `[1]` で終わっていた）。

        注入点を 3 つ回すのは、`try` の範囲を狭める変異を殺すため。
        たとえば `writer.write(buf)` を try の外へ出す変異は、
        `add_page` だけを壊すテストでは生き残る。
        """
        for boom_at in ("page_access", "add_page", "write"):
            with self.subTest(boom_at=boom_at):
                # Arrange & Act
                produced = _run_producer(total=3, boom_pages={2},
                                         boom_at=boom_at)

                # Assert
                self.assertEqual([p["page_num"] for p in produced], [1, 3])
                # 宣言した総頁数は壊れる前に確定しているので 3 のまま ——
                # これが消費側で「宣言 3 / 実産出 2」の食い違いとして
                # 観測でき、p2 の補填占位が出る根拠になる。
                self.assertTrue(all(p["total_pages"] == 3 for p in produced))

    def test_last_page_failure_ends_quietly_without_raising(self):
        """**最終頁**が壊れた場合も、例外を出さず「そこまでの頁」で終わる。

        （`test_ip401_nondict_rawdata.SplitProducerContractTest` から移設・
        改訂。旧テストは p2 を壊して産出 `[1]` を期待していたが、それは
        「p2 以降が全部消える」旧挙動の固定であり §12.1① で**意図して**
        変更した。中間頁の生存は上の subTest が見ているので、こちらは
        上が見ていない**末尾**の境界へ寄せる。）

        ここが押さえる契約は 3 つ:
          ・per-page `except` を `raise` に戻す → 例外が外へ出る
          ・中途で `PdfSplitError` を投げる → 消費側は `next()` しか try で
            囲まないので、最外 except に吞まれて残頁も補填も消える
          ・`produced == 0` の判定を `produced < total_pages` に緩める →
            1 頁でも出せていれば例外にしてはいけない（後続は消費側の
            補填占位で可視化される）のに、ここで例外に化ける
        """
        # Arrange & Act
        with temp_pdf_path() as path, _fake_pypdf(total=3, boom_pages={3}):
            with redirect_stdout(io.StringIO()):
                try:
                    produced = list(ocr_engine._split_pdf_pages(path))
                except Exception as e:  # PdfSplitError も含む
                    self.fail(f"末尾頁の失敗が外へ漏れた: "
                              f"{type(e).__name__}: {e}")

        # Assert
        self.assertEqual([p["page_num"] for p in produced], [1, 2])

    def test_skipped_page_gets_a_placeholder_from_the_consumer(self):
        """飛ばした頁が消費側の補填で占位になる（producer と消費側の結合）。

        どちらか一方を mock すると証明できない: producer だけ見ると
        「p2 が無い」で終わり、消費側だけ見ると「宣言より少ない産出」を
        人工的に注入しているだけになる。
        """
        # Arrange: 3 頁のうち p2 が壊れる（成功するのは p1 と p3）
        routes = [_route(), _route()]

        # Act
        out, _, _ = _run_pipeline(routes, total=3, boom_pages={2})

        # Assert
        by_page = {p["page_num"]: p["result"] for p in out}
        self.assertEqual(set(by_page), {1, 2, 3})
        self.assertEqual(len(by_page[1]["entries"]), 1)
        self.assertEqual(len(by_page[3]["entries"]), 1)
        self.assertTrue(by_page[2].get("_page_error"))
        # 汎用文言に埋もれさせない。「中断」ではなく「分割失敗」なのは、
        # ①の後は「p2 だけ壊れて p3 以降は正常」が普通に起きるため。
        self.assertIn("PDFページ分割失敗", by_page[2]["memo"])


class ZeroYieldExitsTest(unittest.TestCase):
    """§12.1②: 0 件で終わる 3 出口のうち、尾段へ落ちてよいのは 1 つだけ。

    尾段の観測に `_route_ocr_strategy` を使う理由: 尾段は必ずこれを通る
    （尾段側の呼出は `prefix=` を渡さない方）。Gemini の未呼出だけを見ると
    「尾段に入ったがその手前で落ちた」変異が素通りする —— 塞ぎたいのは
    課金ではなく**ファイル全体を 1 回の Gemini 呼出へ送る経路**そのものである。
    """

    def _assert_tail_not_entered(self, out, mocks, total_pages=1):
        mocks["route"].assert_not_called()
        mocks["gemini"].assert_not_called()
        mocks["gemini_bytes"].assert_not_called()
        self.assertTrue(all(p["result"].get("_page_error") for p in out))
        # 判明している全頁ぶん占位が出ること。頁数の主張（total）と実際の
        # 占位数が食い違うと 2 つ壊れる: 進捗タブの `seen/total` 表示
        # （`page_progress.py:311`）が「1/20」で止まり、さらに main の
        # カバレッジ突合が残りを「欠落」と見なして監査タブへ書く ——
        # 全頁失敗はファイル保持なので、それが 3 秒ごとに増殖する。
        self.assertEqual([p["page_num"] for p in out],
                         list(range(1, total_pages + 1)))
        self.assertTrue(all(p["total_pages"] == total_pages for p in out))

    def test_missing_pypdf_does_not_enter_tail(self):
        """pypdf 未導入。系統的（全 PDF が該当）なので特に危ない。"""
        # Arrange & Act
        out, _, mocks = _run_pipeline([], missing=True)

        # Assert
        self._assert_tail_not_entered(out, mocks)

    def test_unreadable_pdf_does_not_enter_tail(self):
        """`PdfReader()` / `len(reader.pages)` が失敗（破損・暗号化等）。"""
        for fails in ("open", "len"):
            with self.subTest(reader_fails=fails):
                # Arrange & Act
                out, _, mocks = _run_pipeline([], reader_fails=fails)

                # Assert
                self._assert_tail_not_entered(out, mocks)

    def test_all_pages_broken_does_not_enter_tail(self):
        """多頁と読めたのに 1 頁も書き出せなかった。

        ①の頁単位隔離が入ると全頁 skip で 0 件産出になり、消費側の
        `first_page` が None → 尾段、という経路が残る。多頁だと**分かって
        いる**のにファイル全体を Gemini へ送るので、他の 2 出口と同じ事故
        再現経路である。
        """
        # Arrange & Act
        out, _, mocks = _run_pipeline([], total=3, boom_pages={1, 2, 3})

        # Assert: 頁数は分かっているので占位もそれを名乗る（1/1 に潰さない）
        self._assert_tail_not_entered(out, mocks, total_pages=3)

    def test_single_page_pdf_still_falls_through_to_tail(self):
        """正常な単頁 PDF は**従来どおり**尾段で処理する（変更しない出口）。

        既存テストは `_split_pdf_pages` を mock しているので、実物の
        `len(reader.pages) <= 1` 分岐は今まで無検査だった。②が単頁 PDF まで
        巻き込んで壊していないことをここで押さえる。
        """
        # Arrange & Act
        out, _, mocks = _run_pipeline([_route()], total=1)

        # Assert: 尾段が動いた（逐頁ループは `prefix=` 付きで呼ぶので区別できる）
        mocks["route"].assert_called_once()
        _, kwargs = mocks["route"].call_args
        self.assertNotIn("prefix", kwargs)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["result"].get("_page_error"))

    def test_split_error_placeholders_ignore_start_page(self):
        """`start_page` は**この経路では効かない**（意図した非対称）。

        `start_page` は「頁単位に途中から流す」開発用の指定
        （`local_test.py --start-page N`。本番 `main` は渡さない）。
        しかしここはファイル全体が分割できない状況であり、飛ばした頁が
        「無事だった」わけではない。`range(start_page, ...)` にすると
        start_page が declared を超えたとき 0 件 yield になり、
        「進入した頁は必ず 1 件以上 yield する」という IP-401 の不変式を
        破って**ファイルが無音で滞留**する。

        逐頁ループ側の `entered_pages` 補填が `range(start_page, total+1)`
        を使うのとは扱いが違うので、その非対称をここで固定する
        （実装時に注釈だけで決めていた —— simcodex R3 の指摘）。
        """
        # Arrange & Act: p2 から流すよう指定しても
        out, _, mocks = _run_pipeline([], total=3, boom_pages={1, 2, 3},
                                      start_page=2)

        # Assert: 占位は 1..3 の全頁ぶん出る（p1 も落とさない）
        self._assert_tail_not_entered(out, mocks, total_pages=3)

    def test_split_error_placeholder_names_the_cause(self):
        """占位 memo に原因が残る（固定文言に潰す変異を殺す）。

        運用者はこの memo だけを手がかりに「pypdf を入れ直すのか、原票を
        再スキャンするのか」を決める。3 出口が同じ文言に潰れると判断できない。
        """
        cases = [
            ({"missing": True}, "pypdf未導入"),
            ({"reader_fails": "open"}, "PDF読取失敗"),
            ({"total": 3, "boom_pages": {1, 2, 3}}, "全ページ"),
        ]
        for kw, expected in cases:
            with self.subTest(cause=expected):
                # Arrange & Act
                out, _, _ = _run_pipeline([], **kw)

                # Assert
                memo = out[0]["result"]["memo"]
                self.assertIn("PDF分割不可", memo)
                self.assertIn(expected, memo)


if __name__ == "__main__":
    unittest.main()
