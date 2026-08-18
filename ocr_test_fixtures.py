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
import json
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


# ── T4: 固化 raw_data 標本（クレカ / 交通系IC）────────────────────
#
# 形は Plan AD-T4-2（`docs/plans/2026-08-17-t4-card-prompts-builders.md`）。
# **`card` / `rows[].date` / `rows[].amount` / `total_amount` のキー名は
# `page_dedup` が読む既存契約**なので、改名すると重複判定が無症状で死ぬ。
#
# 金額は事実台帳（`2026-08-12-credit-card-sample-facts.md`）の実測値。
# F-4 の 3 例（17,295 / 933,680 / 15,503）は回帰の黄金値であり、
# 「明細相加 = 印字合計」が成立することを確認済みの数字である。


# ETC 明細行の note（券面の逐語）。AMEX_A_P1_RAW と etc_rows_raw が共有する。
_ETC_NOTE = "ETC NO : XX-XXXX-XXXX-XXX0-6684-X 入：野芥西 出：野芥西"


def _cc_row(line_no, date, merchant, amount, **extra):
    """クレカ明細 1 行。省略したフィールドは prompt の既定形に合わせる。"""
    row = {
        "line_no": line_no, "date": date, "month_day": "",
        "merchant": merchant, "note": "", "amount": amount,
        "foreign_amount": None, "currency": "", "fx_rate": None,
        "kind": "expense", "sec": 0, "category": "",
        "place_from": "", "place_to": "",
        "debit_account": "", "account_hint": "",
    }
    row.update(extra)
    return row


# F-4 アメックス カードA: p1 の 630×4 ＋ p2 の 6 行 = 17,295
AMEX_A_P1_OCR = AMEX_HEAD + " 今回ご請求金額 17,295 4月10日 福北高速 ＥＴＣ後納分 630"
AMEX_A_P1_RAW = {
    "card": {
        "issuer": "アメリカン・エキスプレス",
        "member_no": "****-******-26003",
        "statement_page": "1/6", "period": "2026/05",
        "statement_no": "", "statement_date": "2026/05/20",
        "card_name": "アメリカン・エキスプレス・ビジネスカード",
        "issuer_t_number": "T8700150009366",
        "account_hint": "通信費と車輌交通費として",   # F-10: **2 科目**の指示
    },
    "sections": [{"index": 0, "label": "今月ご利用額", "subtotal": 17295}],
    "printed_totals": [{"label": "今回ご請求金額", "amount": 17295, "count": 10,
                        "page": 1, "is_handwritten": False}],
    "rows_on_page": 4, "total_amount": 17295,
    "rows": [
        _cc_row(i + 1, "2026/04/%02d" % (10 + i), "福北高速 ＥＴＣ後納分", 630,
                note=_ETC_NOTE)
        for i in range(4)
    ],
}

AMEX_A_P2_OCR = (
    "アメリカン・エキスプレス ****-******-26003 2/6 ページ 今月ご利用額合計 17,295"
)
AMEX_A_P2_RAW = {
    "card": {**AMEX_A_P1_RAW["card"], "statement_page": "2/6", "account_hint": ""},
    "sections": [{"index": 0, "label": "今月ご利用額", "subtotal": 17295}],
    "printed_totals": [{"label": "今月ご利用額合計", "amount": 17295, "count": 10,
                        "page": 2, "is_handwritten": False}],
    "rows_on_page": 6, "total_amount": 17295,
    "rows": [
        _cc_row(1, "2026/04/14", "福北高速 ＥＴＣ後納分", 630),
        _cc_row(2, "2026/04/16", "ＮＴＴファイナンス", 5799),
        _cc_row(3, "2026/04/18", "九州電力", 3866),
        _cc_row(4, "2026/04/20", "ＥＮＥＯＳ 給油", 3220),
        _cc_row(5, "2026/04/22", "福北高速 ＥＴＣ後納分", 630),
        _cc_row(6, "2026/04/24", "福北高速 ＥＴＣ後納分", 630),
    ],
}

# F-5 アメックス カードB: 区画 2 つ。前回分口座振替 −35,808 は「別集計」
AMEX_B_P5_OCR = (
    "アメリカン・エキスプレス ****-******-11004 1/3 ページ 今月ご利用額合計 933,680"
)
AMEX_B_P5_RAW = {
    "card": {
        "issuer": "アメリカン・エキスプレス",
        "member_no": "****-******-11004",
        "statement_page": "1/3", "period": "2026/05",
        "statement_no": "", "statement_date": "2026/05/20",
        "card_name": "アメリカン・エキスプレス・ビジネスカード",
        "issuer_t_number": "T8700150009366", "account_hint": "",
    },
    "sections": [
        {"index": 0, "label": "お支払い金額内容", "subtotal": -35808},
        {"index": 1, "label": "今月ご利用額", "subtotal": 933680},
    ],
    "printed_totals": [{"label": "今月ご利用額合計", "amount": 933680, "count": 4,
                        "page": 5, "is_handwritten": False}],
    "rows_on_page": 5, "total_amount": 933680,
    "rows": [
        # 券面の負号は U+2212（F-5 の原文がまさにこれ）
        _cc_row(1, "2026/05/11", "前回分口座振替金額", "−35,808",
                kind="carry_over", sec=0),
        _cc_row(2, "2026/05/12", "株式会社サンプル商事", 29640, sec=1),
        _cc_row(3, "2026/05/13", "サンプル建設", 300000, sec=1),
        _cc_row(4, "2026/05/14", "サンプル物産", 587440, sec=1),
        _cc_row(5, "2026/05/15", "サンプル電機", 16600, sec=1),
    ],
}

# F-4 ENEOS 個人系: 7,380 + 11,123 − 3,000 = 15,503（負数を含めて初めて一致）
ENEOS_P2_OCR = "ＥＮＥＯＳカード XXXX-XXXX-XXXX-8602 2/2 ページ ご請求金額合計 15,503"
ENEOS_P2_RAW = {
    "card": {
        "issuer": "ENEOS", "member_no": "XXXX-XXXX-XXXX-8602",
        "statement_page": "2/2", "period": "2026/05",
        "statement_no": "", "statement_date": "2026/05/18",
        "card_name": "ＥＮＥＯＳカード", "issuer_t_number": "T8010601027383",
        "account_hint": "ガソリン代として",          # F-10: 1 科目に解決できる指示
    },
    "sections": [{"index": 0, "label": "ご利用明細", "subtotal": 15503}],
    "printed_totals": [{"label": "ご請求金額合計", "amount": 15503, "count": 3,
                        "page": 2, "is_handwritten": False}],
    "rows_on_page": 3, "total_amount": 15503,
    "rows": [
        _cc_row(1, "2026/04/12", "ＥＮＥＯＳ 給油", 7380),
        _cc_row(2, "2026/04/25", "ＥＮＥＯＳ 給油", 11123),
        _cc_row(3, "2026/04/28", "ＥＮＥＯＳポイントキャッシュバック", "−3,000"),
    ],
}

# F-6 ENEOS p1 型: 明細区画と**ポイント区画**が同居する頁。
# プロンプトは「ポイント区画の数値を rows に入れるな」と指示しているが、
# Gemini が入れてきたときに検算を汚さないことを確かめるための標本。
# 区画見出しの解決が壊れると ポイント交換 −3,000 が明細に合流し、
# 15,503 が 12,503 になる（K11 の実害はこの形でしか出ない）。
ENEOS_P1_POINT_SECTION_RAW = {
    **ENEOS_P2_RAW,
    "sections": [
        {"index": 0, "label": "ご利用明細", "subtotal": 15503},
        {"index": 1, "label": "このカードのポイント明細", "subtotal": 590},
    ],
    "rows_on_page": 4,
    "rows": ENEOS_P2_RAW["rows"] + [
        _cc_row(4, "2026/04/28", "ポイント交換", "−3,000", sec=1),
    ],
}

# F-9 JCB 外貨: 円貨額で記帳し、現地通貨額は摘要へ
JCB_FX_OCR = "ＪＣＢカード 3541-0409-9687-2XXX 2/2 ページ お支払い小計 14,310"
JCB_FX_RAW = {
    "card": {
        "issuer": "JCB", "member_no": "3541-0409-9687-2XXX",
        "statement_page": "2/2", "period": "2026/05",
        "statement_no": "", "statement_date": "2026/05/15",
        "card_name": "ＪＣＢ法人カード", "issuer_t_number": "", "account_hint": "",
    },
    "sections": [{"index": 0, "label": "ご利用明細", "subtotal": 14310}],
    "printed_totals": [{"label": "お支払い小計", "amount": 14310, "count": 3,
                        "page": 2, "is_handwritten": False}],
    "rows_on_page": 3, "total_amount": 14310,
    "rows": [
        _cc_row(1, "2026/04/03", "BYTESIM LIMITED ADMIRALTY", 9238,
                foreign_amount=57.60, currency="USD", fx_rate=160.384),
        _cc_row(2, "2026/04/05", "OPENAI *CHATGPT SUBSCR", 3494,
                foreign_amount=22.60, currency="USD", fx_rate=154.856),
        _cc_row(3, "2026/04/09", "ALMASURFERSPARADISE SURFERS PARA", 1578,
                foreign_amount=14.76, currency="AUD", fx_rate=106.970),
    ],
}

# AD-T4-8: 円貨が読めず外貨だけの行（占位になること）
JCB_FX_NO_JPY_RAW = {
    **JCB_FX_RAW,
    "rows": [{**JCB_FX_RAW["rows"][0], "amount": None}],
    "rows_on_page": 1,
}

# F-10 UC: 明細行ごとに手書き科目メモ。**どちらも ACCOUNT_MAP に無い**
UC_P2_OCR = "ＵＣカード 4542-3200-1424-2*** 2/2 ページ お支払合計 25,000"
UC_P2_RAW = {
    "card": {
        "issuer": "UC", "member_no": "4542-3200-1424-2***",
        "statement_page": "2/2", "period": "2026/05",
        "statement_no": "", "statement_date": "2026/05/10",
        "card_name": "ＵＣカード", "issuer_t_number": "", "account_hint": "",
    },
    "sections": [{"index": 0, "label": "ご利用明細", "subtotal": 25000}],
    "printed_totals": [{"label": "お支払合計", "amount": 25000, "count": 2,
                        "page": 2, "is_handwritten": False}],
    "rows_on_page": 2, "total_amount": 25000,
    "rows": [
        _cc_row(1, "2026/04/08", "サンプルウェブ", 15000, account_hint="HP代"),
        _cc_row(2, "2026/04/09", "さくらホームセンター", 10000,
                account_hint="さくら備品"),
    ],
}

# AD-T4-3: ラベルで同定できない負数（台帳に実例なし。合成）
AMEX_B_RETURN_RAW = {
    **AMEX_B_P5_RAW,
    "rows": [_cc_row(1, "2026/05/16", "サンプル物産 返品", "−1,200", sec=1)],
    "rows_on_page": 1,
}


# T5: 截断サルベージ用の大型標本。**関数**にしてあるのは、100 行を
# module-level 定数にすると venv 非依存経路（`python3 -m unittest test_page_family`）
# の import ごとに 100 dict を組み立てることになるため。
#
# 本関数は**純データ関数**（引数 → dict。副作用・遅延 import なし）に限定する。
# `test_dependency_weight` の番人は関数本体の import を意図的に見ない（呼ばれる
# まで実行されないため）ので、ここだけは規約で縛るしかない。


def etc_rows_raw(n, section_at=50, fx_at=(7, 63)):
    """ETC 明細 n 行の raw_data。**区画切替と外貨行を必ず含む**。

    全行同質だと「サルベージが sec を落とす」「外貨を円貨に昇格させる」種の
    変異が生き残る（T5 Plan §4 T5-1、AD-10）。

    Args:
        n: 明細行数（`rows_on_page` と `printed_totals[].count` も n になる）
        section_at: この `line_no` 以降が第 2 区画（`sec=1`）
        fx_at: 外貨行にする `line_no` の集合

    キーの並びは prompt schema と同じく **`rows` が最後**（T5 Plan V1）。
    サルベージは「截断しても先頭側の top 級が残る」ことに依存するので、
    この順序自体が標本の検証対象である。
    """
    fx_set = set(fx_at)
    rows = []
    for i in range(n):
        line_no = i + 1
        sec = 1 if line_no >= section_at else 0
        date = "2026/04/%02d" % ((i % 28) + 1)
        if line_no in fx_set:
            # 外貨行の正本は JCB_FX_RAW（円貨・外貨・レートの組が黄金値）
            rows.append({**JCB_FX_RAW["rows"][0],
                         "line_no": line_no, "date": date, "sec": sec})
        else:
            rows.append(_cc_row(line_no, date, "福北高速 ＥＴＣ後納分", 630,
                                sec=sec, note=_ETC_NOTE))
    total = sum(r["amount"] for r in rows)
    return {
        # card の正本は ENEOS_P2_RAW。頁番号と券種だけを ETC 用に差し替える
        "card": {**ENEOS_P2_RAW["card"],
                 "statement_page": "2/5",
                 "card_name": "ＥＮＥＯＳ ＢＵＳＩＮＥＳＳ ＥＴＣカード",
                 "account_hint": "高速代として"},
        "sections": [
            {"index": 0, "label": "今月ご利用額",
             "subtotal": sum(r["amount"] for r in rows if r["sec"] == 0)},
            {"index": 1, "label": "ＥＴＣご利用分",
             "subtotal": sum(r["amount"] for r in rows if r["sec"] == 1)},
        ],
        "printed_totals": [{"label": "今月ご利用額合計", "amount": total,
                            "count": n, "page": 2, "is_handwritten": False}],
        "rows_on_page": n,
        "total_amount": total,
        "rows": rows,
    }


def etc_rows_truncated_text(rows_kept, total=100):
    """`etc_rows_raw(total)` の JSON を rows_kept 行の直後で切ったテキスト。

    「どこで切るか」は `_cc_row` のキー順と `json.dumps` の空白に逐字で
    依存する。各テストが自前で `text.index('{"line_no": N')` を書くと、
    標本の形が変わったとき片方だけ落ちる（あるいは意図と違う位置で切れても
    誰も気づかない）ので、切り方の定義は標本と同じ場所に置く。
    """
    text = json.dumps(etc_rows_raw(total), ensure_ascii=False)
    return text[:text.index('{"line_no": %d' % (rows_kept + 1))]


def etc_rows_text_before_rows(total=100):
    """`rows` 配列に一度も到達せずに切れたテキスト（top 級だけが残る形）。"""
    text = json.dumps(etc_rows_raw(total), ensure_ascii=False)
    return text[:text.index('"rows": [')]


def _ic_row(line_no, month_day, category, amount, place_from="", place_to=""):
    """nimoca 明細 1 行（F-7）。**年は券面に無い**ので date は None。"""
    return {
        "line_no": line_no, "date": None, "month_day": month_day,
        "merchant": "", "note": "", "amount": amount,
        "foreign_amount": None, "currency": "", "fx_rate": None,
        "kind": "expense", "sec": 0, "category": category,
        "place_from": place_from, "place_to": place_to,
        "debit_account": "", "account_hint": "",
    }


# F-7 nimoca: 合計金額の行が無い。`入金 現金 5,000` が実在
NIMOCA_P1_OCR = NIMOCA_HEAD + " 全120件 (1)〜(20) 5月1日 電車 西鉄福岡 ～ 薬院 150"
NIMOCA_P1_RAW = {
    "card": {
        "issuer": "nimoca", "member_no": "", "statement_page": "",
        "period": "", "statement_no": "", "statement_date": "",
        "card_name": "nimoca", "issuer_t_number": "",
        "account_hint": "交通費として",
    },
    "sections": [],
    "printed_totals": [{"label": "全120件", "amount": None, "count": 120,
                        "page": 1, "is_handwritten": False}],
    "rows_on_page": 4, "total_amount": None,
    "rows": [
        _ic_row(1, "5/1", "電車", 150, "西鉄福岡", "薬院"),
        _ic_row(2, "5/1", "バス", 260, "天神"),
        _ic_row(3, "5/2", "入金", 5000, "現金"),
        _ic_row(4, "12/20", "電車", 380, "博多", "香椎"),   # 年跨ぎ検証用
    ],
}


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
