"""クレジットカード明細 / 交通系IC 利用履歴の Gemini プロンプト（T4-b）。

**スキーマは定数から生成する**（Plan AD-T4-2 / Codex 複審）。プロンプト中に
JSON の例を手書きすると、消費側（`card_entries` / `page_dedup`）のキー名を
変えたときにプロンプトを直し忘れる —— しかもその事故は**無症状**で、
`page_dedup.safe_fingerprint` が静かに fail-open して重複頁が二重記帳される。
定数を 1 つの正本にすれば、少なくとも「prompt と消費側の同期」は機械で守れる。

**この保証の届く範囲**: Gemini が実際にこの形で返すかは T11（実呼出）でしか
確認できない。ここで守れるのは prompt と消費側の同期だけである。

`ocr_engine` は本モジュールを import して `PROMPTS` に登録するだけ
（`ocr_engine.py` は 2368 行で 800 行上限を超過しており、これ以上積まない。
母 Plan §4）。**本モジュールは stdlib すら import しない**
（`test_dependency_weight` が venv 非依存を見張る）。
"""

# ── スキーマ定義（key -> (JSON 例, 説明)）──────────────────────────
# dict は挿入順を保つ（Python 3.7+）。生成される JSON 例の並びもこの順になる。

_CARD_FIELDS = {
    "issuer": ('"アメリカン・エキスプレス"',
               "カード発行会社名。券面の逐語。読めなければ空文字"),
    "member_no": ('"****-******-26003"',
                  "会員番号・カード番号。**マスク記号（* や X）もそのまま**残す"),
    "statement_page": ('"1/6"',
                       "券面の「1/6 ページ」の逐語。無ければ空文字"),
    "period": ('"2026/05"', "請求月。無ければ空文字"),
    "statement_no": ('""', "明細書番号。無ければ空文字"),
    "statement_date": ('"2026/05/20"', "明細書作成日 YYYY/MM/DD。無ければ空文字"),
    "card_name": ('"アメリカン・エキスプレス・ビジネスカード"',
                  "券面のカード名称。無ければ空文字"),
    "issuer_t_number": ('"T8700150009366"',
                        "券面のT番号。**カード会社自身のもの**。無ければ空文字"),
    "account_hint": ('""',
                     "文書全体に対する手書き注記（例「ガソリン代として」）。"
                     "**逐語のまま**。解釈しない。無ければ空文字"),
}

_IC_CARD_FIELDS = {
    "issuer": ('"nimoca"', "カード種別。券面のブランド名"),
    "card_name": ('"nimoca"', "カード名称"),
    "account_hint": ('""',
                     "文書全体への手書き注記（例「交通費として」）。逐語のまま"),
}

_TOP_FIELDS = {
    "sections": ('[{"index": 0, "label": "今月ご利用額", "subtotal": 17295}]',
                 "明細の区画（見出しとその小計）。label は券面の見出しの逐語。"
                 "subtotal が読めなければ null。区画が無ければ空配列"),
    "printed_totals": ('[{"label": "今回ご請求金額", "amount": 17295, '
                       '"count": 10, "page": 1, "is_handwritten": false}]',
                       "券面に**印字された**合計。label は逐語。count は"
                       "「12口」等の件数（無ければ null）。手書きの数値は"
                       "is_handwritten を true にする"),
    # T5: 定義を `rows` と揃える。以前は「券面に見えている数」とだけ書いており、
    # rows が除外する合計行・小計行・ポイント区画まで数え得たので、
    # `len(rows) < rows_on_page` の行欠け検出が健全な頁でも恒常発火した。
    # 物理アンカー（券面）は残す —— 「取得できた数」を数えさせると
    # 検出が循環して意味を失う。
    # T8b-3（甲）: 「頁全体・全区画」を明示する。区画で限縮されると、主カード
    # だけ返したときに申告も取得数と一致して自洽し、`card_salvage` の行欠け
    # 検出まで沈黙する（2026-08-19 の実害そのものの形）。
    "rows_on_page": ("12", "この頁の券面に印字された**明細行**の総数"
                           "（**頁全体・全区画の合計**。rows に入れるべき"
                           "行の数。合計行・小計行・ポイント区画は数えない。"
                           "**取得できた数ではなく券面に見えている数**）"),
    "total_amount": ("17295", "この明細書の請求合計。読めなければ null"),
}

_IC_TOP_FIELDS = {
    "printed_totals": ('[{"label": "全120件", "amount": null, "count": 120, '
                       '"page": 1, "is_handwritten": false}]',
                       "頁下部の「全 XXX 件」。**nimoca に合計金額は無い**ので"
                       "amount は必ず null"),
    # T5: CC 側と同じ理由で `rows` の範囲に揃える（IC は入金行を rows に
    # 含めさせる一方、ポイント列と手書き線は除外する）。
    "rows_on_page": ("20", "この頁の券面に印字された**明細行**の総数"
                           "（rows に入れるべき行の数。入金行も数える。"
                           "ポイント列・手書きの線は数えない。"
                           "**取得できた数ではなく券面に見えている数**）"),
    "total_amount": ("null", "**必ず null**。nimoca に合計金額の行は存在しない"),
}

_ROW_FIELDS_COMMON = {
    "line_no": ("1", "この頁の中での通番（1 から）"),
    "date": ('"2026/04/10"',
             "利用日 YYYY/MM/DD。**年が券面に無ければ null**（推測しない）"),
    "amount": ("630",
               "利用額（**円貨・税込**）。カンマなしの数値。負数は -3000 の形。"
               "円貨が読めなければ null"),
    "kind": ('"expense"',
             "expense（通常利用）/ fee（年会費・手数料）/ credit_adjust"
             "（ポイント充当・キャッシュバック）/ carry_over（前回分口座振替）/ "
             "charge（チャージ入金）のいずれか。判らなければ expense"),
    "account_hint": ('""',
                     "その行に対する手書き注記。**逐語のまま**。無ければ空文字"),
}

_ROW_FIELDS_CC = {
    "month_day": ('""', '券面に年が無く月日だけのときの逐語（例 "4/10"）'),
    "merchant": ('"福北高速 ＥＴＣ後納分"', "利用店名・摘要の主行"),
    "note": ('"ETC NO : XX-XXXX 入：野芥西 出：野芥西"',
             "副行（ETC 番号・入口/出口・区間名など）を空白で連結。無ければ空文字"),
    "foreign_amount": ("null", "外貨取引の現地通貨額。無ければ null"),
    "currency": ('""', "通貨コード（USD / AUD 等）。無ければ空文字"),
    "fx_rate": ("null", "換算レート。無ければ null"),
    "sec": ("0", "この行が属する sections の index。判らなければ null"),
    "debit_account": ('"旅費交通費"', "費用の勘定科目の推定。判らなければ空文字"),
}

_ROW_FIELDS_IC = {
    "month_day": ('"5/1"', "券面の月日の逐語。**年は書かない**"),
    "merchant": ('""', "空文字でよい（nimoca に店名欄は無い）"),
    "category": ('"電車"', "種別列の値。実測は 電車 / バス / 入金 の 3 種"),
    "place_from": ('"西鉄福岡"', "施設1"),
    "place_to": ('"薬院"', "施設2。片道表示で 2 つ目が無ければ空文字"),
    "debit_account": ('""', "空文字でよい（Python 側で旅費交通費に決める）"),
}


def _render(fields, indent, trailing=False):
    """`{key: (例, 説明)}` から注釈付きの JSON 断片を組み立てる。

    カンマは**必ず値の直後**に置く。注釈の後ろに置くとコメントの一部に見えて
    Gemini が構造を読み違えるので、呼出側のテンプレートで `%(...)s,` と
    書いてはいけない —— 続きの要素があるときは `trailing=True` を渡すこと
    （`test_card_prompts` がこの規約を機械で見張る）。
    """
    pad = " " * indent
    items = list(fields.items())
    last = len(items) - 1
    return "\n".join(
        '%s"%s": %s%s   // %s'
        % (pad, key, example, "," if trailing or i < last else "", note)
        for i, (key, (example, note)) in enumerate(items)
    )


# ── 消費側と共有する正本（テストがこれと実装の突合を見張る）──────────
REQUIRED_CARD_KEYS = tuple(_CARD_FIELDS)
REQUIRED_IC_CARD_KEYS = tuple(_IC_CARD_FIELDS)
REQUIRED_TOP_KEYS = ("card", "rows") + tuple(_TOP_FIELDS)
REQUIRED_IC_TOP_KEYS = ("card", "rows") + tuple(_IC_TOP_FIELDS)
CREDIT_CARD_ROW_KEYS = tuple(_ROW_FIELDS_COMMON) + tuple(_ROW_FIELDS_CC)
TRANSIT_IC_ROW_KEYS = tuple(_ROW_FIELDS_COMMON) + tuple(_ROW_FIELDS_IC)

# 消費側（`card_entries`）が読んでよいキーの全体。静的検査に使う
ALL_ROW_KEYS = frozenset(CREDIT_CARD_ROW_KEYS) | frozenset(TRANSIT_IC_ROW_KEYS)
ALL_CARD_KEYS = frozenset(REQUIRED_CARD_KEYS) | frozenset(REQUIRED_IC_CARD_KEYS)
ALL_TOP_KEYS = frozenset(REQUIRED_TOP_KEYS) | frozenset(REQUIRED_IC_TOP_KEYS)


CREDIT_CARD_PROMPT = """
あなたはプロの経理担当者です。以下はクレジットカード利用明細書の**1 ページ分**です。
明細を 1 行ずつ、会計ソフト用データとして抽出してください。

【最重要】
- **この 1 ページに印字されている明細は全部報告してください。** 同じ明細書の中に
  会員ごとのカード区画（「今月ご利用額 <氏名> 様」の見出しで始まる塊）が複数
  あるときは、**すべての区画のすべての明細行**を報告してください。主カードの
  区画で止めないでください。各行がどの区画に属するかは sec に入れてください。
- **他ページの情報を推測で補わないでください。** 1 つの PDF に別会社・別明細書の
  カード明細が混在することがあります。このページに見えているものだけを使います。
- **合計行・小計行を rows に入れないでください。** それらは printed_totals と
  sections[].subtotal に入れます。二重計上になります。
- **ポイント区画（ポイント明細・永久不滅ポイント等）の数値を rows に
  入れないでください。** ポイントは円貨の勘定ではありません。
- **読めない値は null**（または空文字）にしてください。0 で埋めないでください。
  0 と「読めなかった」は検算で意味が変わります。
- 金額はカンマなしの数値で返してください。負数は -3000 の形にしてください。

【出力JSONフォーマット】
{
  "card": {
%(card)s
  },
%(top)s
  "rows": [
    {
%(row)s
    }
  ]
}

【明細行の注意】
- 1 つの明細が複数行に分かれている場合（ETC 番号・入口/出口・区間名などの副行）、
  主行を 1 件として note に副行を連結してください。副行を別の明細にしないでください。
- 外貨取引は円貨額を amount に、現地通貨額を foreign_amount に、通貨を currency に、
  換算レートを fx_rate に分けてください。**現地通貨の数値を amount に入れないでください。**
- 前回分の口座振替（前月分の清算）は kind を carry_over にしてください。
- ポイント充当・キャッシュバックは kind を credit_adjust にしてください。
- 手書きの注記は解釈せず account_hint に逐語のまま入れてください。

【勘定科目の選択肢】debit_account には以下を優先して使ってください:
備品・消耗品費, 通信費, 水道光熱費, 旅費交通費, 接待交際費, 支払手数料,
新聞図書費, 広告宣伝費, 諸会費, 租税公課, 荷造運賃, 外注費
上記にない場合のみ一般的な科目名を使用してください。
""" % {
    "card": _render(_CARD_FIELDS, 4),
    "top": _render(_TOP_FIELDS, 2, trailing=True),
    "row": _render(dict(_ROW_FIELDS_COMMON, **_ROW_FIELDS_CC), 6),
}


TRANSIT_IC_PROMPT = """
あなたはプロの経理担当者です。以下は交通系IC カード（nimoca 等）の
**利用履歴画面を印刷したもの**の 1 ページ分です。乗車 1 件ずつ抽出してください。

【最重要】
- 券面の列は「月日 / 種別 / 施設1 ～ 施設2 / 利用額 / カードポイント /
  センターポイント」です。**カードポイント・センターポイントの数値を
  rows に入れないでください。** ポイントは円貨の勘定ではありません。
- **年は券面に印字されていません。** date は必ず null にし、month_day に
  券面の逐語（例 "5/1"）を入れてください。**年を推測しないでください。**
- 「入金」（チャージ）の行も**必ず rows に含めて** kind を charge にしてください。
  記帳するかどうかは Python 側が決めます。行を落とさないでください。
- 頁下部の「全 XXX 件」は printed_totals に count として入れてください。
  **nimoca に合計金額の行はありません**（amount は null）。
- 金額はカンマなしの数値で返してください。

【出力JSONフォーマット】
{
  "card": {
%(card)s
  },
%(top)s
  "rows": [
    {
%(row)s
    }
  ]
}

【明細行の注意】
- 種別の実測値は 電車 / バス / 入金 の 3 種です。それ以外なら券面の逐語を入れてください。
- 手書きの横線・矢印は範囲の目印であり、明細ではありません。rows に入れないでください。
- 手書きの注記は解釈せず account_hint に逐語のまま入れてください。
""" % {
    "card": _render(_IC_CARD_FIELDS, 4),
    "top": _render(_IC_TOP_FIELDS, 2, trailing=True),
    "row": _render(dict(_ROW_FIELDS_COMMON, **_ROW_FIELDS_IC), 6),
}
