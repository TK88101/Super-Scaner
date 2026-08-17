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
