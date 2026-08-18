"""テスト用ヘルパ（本番経路からは import されない）。

ファイル名を `test_` で始めていないのは、`unittest discover -p "test_*.py"` に
テストファイルとして拾わせないため。

T3（`docs/plans/2026-08-17-t3-page-ocr-resolver.md`）で
`ocr_engine._route_ocr_strategy` の戻り値が 3-tuple から `PageOcr` へ変わった。
既存テストは `(raw_data, ocr_text, ocr_confidence)` の 3-tuple をモックの
`side_effect` / `return_value` に直接与えていたので、そのままでは通らない。

**各テストが持つ 3-tuple のフィクスチャは書き換えない**方針を取り、
モックへ渡す直前にここで変換する。フィクスチャを機械的に置換すると差分が
巨大になり、「テストの意図が変わっていないこと」をレビューで確認できなくなる
——「テストを直して緑にした」のか「実装が正しいから緑」なのかが判別不能になる。
"""
import contextlib
from types import SimpleNamespace
from unittest import mock

import ocr_engine
import page_family
from doc_types import DocType


def page_ocr_from_tuple(route_tuple, doc_type=DocType.RECEIPT):
    """`(raw_data, ocr_text, ocr_confidence)` を `PageOcr` へ変換する。

    Args:
        route_tuple: 既存テストが持つ 3-tuple。
        doc_type: そのテストが流している doc_type。`actual_doc_type` と
            `prompt` の両方に使う（AD-T3-1: prompt と builder は同源）。

    Returns:
        ocr_engine.PageOcr
    """
    raw_data, ocr_text, ocr_confidence = route_tuple
    return ocr_engine.PageOcr(
        raw_data=raw_data,
        ocr_text=ocr_text,
        ocr_confidence=ocr_confidence,
        actual_doc_type=doc_type,
        prompt=ocr_engine.PROMPTS[doc_type],
        page_class=page_family.PageClass(),
        family_signal=None,
    )


def page_ocrs_from_tuples(route_tuples, doc_type=DocType.RECEIPT):
    """3-tuple のリストを `PageOcr` のリストへ（`side_effect` 用）。"""
    return [page_ocr_from_tuple(t, doc_type) for t in route_tuples]


class _FakeText:
    """`response.text` を模す descriptor（設定により ValueError を送出する）。

    SDK 0.8.5 の `.text` は **parts が空のときだけ** ValueError を投げる
    （`generation_types.py` の text/parts property）。parts が非空なら
    finish_reason が MAX_TOKENS でも全 part を連結して返す —— つまり
    「截断したが本文は部分的に出ている」状況は `raises=False` ＋ 途中で
    切れた text で表現するのが正しい。
    """

    def __init__(self, value, raises=False):
        self._value = value
        self._raises = raises

    def __get__(self, obj, objtype=None):
        if self._raises:
            raise ValueError("Invalid operation: response.text quick accessor "
                             "requires the response to contain a valid Part")
        return self._value


def legacy_doc_types():
    """逐行記帳**ではない** doc_type（H4 ゲートの検査母集合）。

    手書きで列挙すると新 doc_type が永久に無検査で残るので `DocType.ALL` から
    導出する。導出式を各テストファイルに置くと、片方だけ列挙へ退行しても
    気づけない —— 母集合の定義はここ 1 箇所に置く。
    """
    return sorted(set(DocType.ALL) - set(ocr_engine.LINE_MODE_DOC_TYPES))


# proto enum `Candidate.FinishReason` の生値。応答形状の語彙なので
# `gemini_response` と同じ場所に置く（呼出側に数値リテラルを散らさない）。
FINISH_STOP = 1
FINISH_MAX_TOKENS = 2


@contextlib.contextmanager
def fake_gemini_model(response):
    """`ocr_engine.model` を差し替え、呼出記録を持つ mock を貸す。

    `generation_config=` というキーワードで SDK を叩いていること自体が
    契約なので、その読み書きは `sent_generation_config` と対で 1 箇所に置く。
    """
    fake = mock.MagicMock()
    fake.generate_content.return_value = response
    with mock.patch.object(ocr_engine, "model", fake):
        yield fake


def sent_generation_config(fake):
    """直近の `generate_content` に渡された generation_config。"""
    _, kwargs = fake.generate_content.call_args
    return kwargs["generation_config"]


def gemini_response(text="", raises=False, finish_reason=FINISH_STOP,
                    usage=None):
    """`model.generate_content` の戻り値を模した偽 response。

    finish_reason は proto enum の整数（1=STOP, 2=MAX_TOKENS）。
    `text` を property にする必要があるため動的にクラスを作る。

    SDK の応答形状のモデルは**ここ 1 箇所**に置く。`.text` / `finish_reason`
    の語義が変わったとき（あるいは `prompt_feedback` / `safety_ratings` を
    足すとき）、複製が在ると片方だけ直って「半分のテストが新契約・半分が
    旧契約」になり、しかも両方緑になる。
    """
    attrs = {"text": _FakeText(text, raises=raises)}
    response = type("_FakeResponse", (), attrs)()
    response.candidates = [SimpleNamespace(finish_reason=finish_reason)]
    if usage is not None:
        response.usage_metadata = usage
    return response


def pdf_pages(n, total_pages=None):
    """`_split_pdf_pages` の戻り値を模した n ページ分のリスト。

    `total_pages` を明示すると「宣言した総頁数」と「実際に産出する頁数」を
    食い違わせられる（producer が中途で尽きた状況の再現）。既定は `n` なので
    既存の呼出は無変更で動く。

    ページ dict の形（`page_num` / `total_pages` / `data` / `filename`）は
    `process_pipeline` の逐頁ループが直接読む契約なので、コピーが増えると
    「片方にキーを足して片方に足し忘れる」漂移が起きる。共有はここに置く。

    `test_ip401_regression.py` にも同形のローカル定義が在るが、そちらは
    IP-401 の原始事故の回帰テストで「無修正で緑であること」自体が受入基準に
    なっているため、この整理では触っていない（統合は別タスク）。
    """
    declared = n if total_pages is None else total_pages
    return [{"page_num": i, "total_pages": declared,
             "data": f"%PDF-p{i}".encode(),
             "filename": f"p{i}.pdf"} for i in range(1, n + 1)]
