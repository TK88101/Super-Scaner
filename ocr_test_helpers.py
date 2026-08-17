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
