"""テスト標本（**stdlib 以外を一切 import しない**）。

`ocr_test_helpers` と分けてあるのは依存の重さが違うため。あちらは
`PageOcr` を組むのに `ocr_engine` を import する ——`ocr_engine` は
`google.generativeai` / `paddleocr` を引くので、import した時点でその
テストは venv311 必須になる。

`page_family` / `page_dedup` / `card_reconciliation` / `invoice_classification`
の 4 モジュールは **gspread / paddleocr / google api 非依存**（母 Plan §4）で、
`python3 -m unittest test_page_family` が venv 無しで通ることが設計上の性質。
標本をあちら側に置くと、その性質が**静かに**壊れる —— 全量テストは venv311 で
走るので緑のままになり、誰も気づかない（実際 2026-08-17 に一度壊した）。

`test_dependency_weight.py` がこの分離を機械的に見張っている。

ファイル名を `test_` で始めていないのは `unittest discover -p "test_*.py"` に
テストとして拾わせないため。
"""
import contextlib
import os
import tempfile


# ── OCR テキスト標本（正本）──────────────────────────────────
# **複製してはいけない**。これらの文字列は実券面の OCR 癖を符号化している
# —— マスクされたカード番号の並び、nimoca の「カンマの付かない 3 桁金額」など。
# この種の細部は過去に実害を出している（`f464179`: 券面の負号が解析できず
# 検算が 3,000 円ずれた）。正本を直したとき複製側へ伝播しないと、複製側は
# 古い（誤った）標本を検証し続け、しかも誰も気づかない。

AMEX_HEAD = (
    "アメリカン・エキスプレス ご利用代金明細書 T8700150009366 "
    "****-******-26003 1/6 ページ"
)
NIMOCA_HEAD = (
    "nimoca 利用履歴の確認 月日 種別 施設1 ～ 施設2 利用額 "
    "カードポイント センターポイント"
)


def ic_rows(n, amount=260):
    """nimoca の明細行（F-7: 月日 / 種別 / 施設 / 利用額）。

    **カンマの付かない 3 桁金額**（150〜1,300 円）が主であることが要点。
    金額トークンをカンマ有りに限定すると、nimoca 頁の has_detail_rows が
    False になり「除外に対する拒否権」（AD-0 優先序 3）が効かなくなる。
    """
    return " ".join(
        "%d月%d日 電車 西鉄天神 ～ 薬院 %d" % ((i % 2) + 5, (i % 27) + 1, amount + i * 3)
        for i in range(n)
    )


@contextlib.contextmanager
def temp_pdf_path(content=b"%PDF-1.4 dummy"):
    """使い捨ての PDF ファイルパスを貸し出し、抜けるときに必ず消す。

    `_split_pdf_pages` をモックするテストでは中身は読まれないが、
    `process_pipeline` が `_get_mime_type` で拡張子を見るため実ファイルが要る。
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(content)
        path = tmp.name
    try:
        yield path
    finally:
        os.unlink(path)
