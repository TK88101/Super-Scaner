"""B5 benchmark の実行前提チェック（走らせる前に落とす層）。

Plan＝docs/plans/2026-08-10-b5-benchmark-ss.md §4.1／§5 T3・T9。

ここにある三つの門は、いずれも「黙って間違った数字を出す」経路を塞ぐためにある。

1. **HEADLESS_MODE**（評審 #3）——`main.process_file` に `ledger` を渡すと
   消費側だけが headless 経路に入る。producer 側（`ocr_engine` 末尾段の
   `envelope_filter=config.headless_mode()`）は環境変数を読むので、
   未設定のままでは「headless 消費者 ＋ UI 生産者」という**生産に存在しない
   混合経路**を測ることになる。走る前に落とす。

2. **単頁入力**（契約 v0.19 §65「実行器に渡すのは切片ファイル 1 枚＝page_num
   は恒に 1」・§304）——多頁を食わせると UI 多頁形状を測ってしまい、
   契約 headless の数字だと誤認される。

3. **pypdf 可用性**（評審 #14）——不在時 `_split_pdf_pages` は警告 1 行で空を
   返し（`ocr_engine.py:425-427`）、逐頁分岐が return しないため末尾段の
   `f.read()` へ落ちて全ファイルが内存に載る。そのうえ strategy C は全 PDF を
   inline data として Gemini へ送る。benchmark でこの経路に突入させない。
   ※この fallback 自体は現行 UI 生産で到達可能な欠陥であり、別途立項して
   趙へ上申する（benchmark の成果物として「到達できた」を数えない）。
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import config

# PDF 以外（画像）は頁数の概念がなく、常に 1 単位として扱う。
_PDF_SUFFIX = ".pdf"


class PreflightError(RuntimeError):
    """実行前提が満たされていない。測定を始めてはいけない。"""


def _pypdf_available() -> bool:
    """pypdf が import できるか（テストが差し替えられるよう関数に切る）。"""
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def _count_pdf_pages(path: str) -> int | None:
    """PDF の頁数。読めなければ None（呼出側が拒否に倒す）。"""
    try:
        from pypdf import PdfReader

        return len(PdfReader(path).pages)
    except Exception:
        return None


def assert_headless_mode() -> None:
    """`config.headless_mode()` が真であることを要求する。

    環境変数を直接読まず config を経由する——`config.headless_mode()` は
    呼出時点評価であり（`config.py:273-275`）、判定の一元化がそのまま
    「producer 側と同じ物を見ている」保証になる。
    """
    if not config.headless_mode():
        raise PreflightError(
            "HEADLESS_MODE が立っていない。ledger を渡すだけでは消費側しか "
            "headless にならず、producer 側は UI 挙動のままになる"
            "（生産に存在しない混合経路を測ってしまう）。"
            "benchmark_preflight.headless_env() で囲むこと"
        )


def assert_single_page(path: str) -> int:
    """入力が 1 頁／1 切片であることを要求し、頁数を返す。

    画像は頁数の概念がないので 1 とみなす。PDF で頁数が読めない場合は
    **通さない**——「読めなかったから通す」は測定母数を静かに汚す。
    """
    if not path.lower().endswith(_PDF_SUFFIX):
        return 1

    pages = _count_pdf_pages(path)
    if pages is None:
        raise PreflightError(
            f"頁数を判定できない: {path}。"
            "契約 headless の入力は 1 頁／1 切片でなければならないので、"
            "不明のまま測らない"
        )
    if pages != 1:
        raise PreflightError(
            f"入力が {pages} 頁ある: {path}。"
            "契約 v0.19 §65/§304 により headless へ渡るのは常に 1 頁 or 1 切片。"
            "多頁を測ると UI 多頁形状の数字になる（③ の S-A シナリオで別途測る）"
        )
    return pages


def assert_pypdf_available() -> None:
    """pypdf 不在なら実行を拒否する（危険 fallback へ入れない）。"""
    if not _pypdf_available():
        raise PreflightError(
            "pypdf が使えない。この状態で走らせると _split_pdf_pages が空を返し、"
            "末尾段が全ファイルを内存へ読み込む経路（O(n^2) ディスク書込＋"
            "全 PDF を Gemini へ inline 送出）に落ちる。環境不合格として中止する"
        )


def run_preflight(path: str) -> dict:
    """全ての門を通し、生データに残すメタ情報を返す。

    返り値は測定レコードへそのまま埋めて、後から
    「本当に headless で単頁で走ったのか」を機械検証できるようにする。
    """
    assert_pypdf_available()
    assert_headless_mode()
    page_count = assert_single_page(path)
    return {
        "headless_mode": True,
        "pypdf_available": True,
        "page_count": page_count,
        "input_path": path,
    }


@contextmanager
def headless_env():
    """実行中だけ `HEADLESS_MODE=1` を立て、抜けたら元の値へ復元する。

    生産の `.env` を書き換えずに headless 経路を再現するための一時措置。
    例外が出ても必ず戻す（測定失敗が環境汚染に化けないように）。
    """
    original = os.environ.get("HEADLESS_MODE")
    os.environ["HEADLESS_MODE"] = "1"
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("HEADLESS_MODE", None)
        else:
            os.environ["HEADLESS_MODE"] = original
