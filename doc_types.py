# doc_types.py
# 文書タイプ定義と会計科目デフォルトマッピング


class DocType:
    """文書タイプ定数"""
    RECEIPT = "receipt"                       # 領収書
    PURCHASE_INVOICE = "purchase_invoice"     # 支払請求書・仕入請求書
    SALES_INVOICE = "sales_invoice"           # 売上請求書
    SALARY_SLIP = "salary_slip"              # 賃金台帳・給与明細書
    CREDIT_CARD = "credit_card"               # クレジットカード利用明細書
    TRANSIT_IC = "transit_ic"                 # 交通系IC利用履歴（nimoca 等）

    ALL = [RECEIPT, PURCHASE_INVOICE, SALES_INVOICE, SALARY_SLIP,
           CREDIT_CARD, TRANSIT_IC]


# 各文書タイプのデフォルト会計科目マッピング
# - default_debit: 借方勘定科目（デフォルト値、AIが推定した科目で上書き可能）
# - default_credit: 貸方勘定科目
# - debit_tax_type: 借方税区分（デフォルト値）
# - credit_tax_type: 貸方税区分
DOC_TYPE_CONFIG = {
    DocType.RECEIPT: {
        "label": "領収書",
        "default_debit": "備品・消耗品費",
        "default_credit": "未払金",
        "debit_tax_type": "課対仕入10%",
        "credit_tax_type": "対象外",
    },
    DocType.PURCHASE_INVOICE: {
        "label": "支払請求書・仕入請求書",
        "default_debit": "仕入高",
        "default_credit": "未払金",
        "debit_tax_type": "課対仕入10%",
        "credit_tax_type": "対象外",
    },
    DocType.SALES_INVOICE: {
        "label": "売上請求書",
        "default_debit": "売掛金",
        "default_credit": "売上高",
        "debit_tax_type": "対象外",
        "credit_tax_type": "課税売上10%",
    },
    DocType.SALARY_SLIP: {
        "label": "賃金台帳・給与明細書",
        "default_debit": "給料賃金",
        "default_credit": "普通預金",
        "debit_tax_type": "対象外",
        "credit_tax_type": "対象外",
    },
    # クレジットカード利用明細書（逐行記帳。1 明細 = 1 仕訳）
    # default_debit は利用店名から科目を推定できなかった行の落とし先。
    # 貸方は一律「未払金」（趙裁定済み）。補助科目にはカード名を入れる
    # （井戸会計事務所の MF 実帳に倣う。Plan AD-11）。
    DocType.CREDIT_CARD: {
        "label": "クレジットカード利用明細書",
        "default_debit": "備品・消耗品費",
        "default_credit": "未払金",
        "debit_tax_type": "課対仕入10%",
        "credit_tax_type": "対象外",
    },
    # 交通系IC 利用履歴（nimoca 等）。電車・バスは公共交通機関特例の対象。
    # 「入金（チャージ）」行は費用ではないため記帳しない（Plan TBD-2 既定）。
    DocType.TRANSIT_IC: {
        "label": "交通系IC利用履歴",
        "default_debit": "旅費交通費",
        "default_credit": "未払金",
        "debit_tax_type": "課対仕入10%",
        "credit_tax_type": "対象外",
    },
}

# Tab 名のサフィックス（Google Sheets 出力用）
DOC_TYPE_TAB_SUFFIX = {
    DocType.RECEIPT: "領収書",
    DocType.PURCHASE_INVOICE: "支払請求書",
    DocType.SALES_INVOICE: "売上請求書",
    DocType.SALARY_SLIP: "給与明細",
    DocType.CREDIT_CARD: "カード明細",
    DocType.TRANSIT_IC: "交通IC",
}

# 環境変数名とDocTypeの対応
#
# TRANSIT_IC はここに登録するが .env には値を置かない運用を想定している
# （趙裁定 5: nimoca はクレカと同じフォルダに混載し、プログラムが頁単位で分流する）。
# load_folder_map は os.getenv で拾えないキーを素通りするため、
# 「型は在るがフォルダは単独監視しない」状態になる。default プロファイルの
# FOLDER_SALES_INVOICE_ID が現にこの状態にあり、実績のある使い方。
ENV_FOLDER_MAP = {
    "FOLDER_RECEIPT_ID": DocType.RECEIPT,
    "FOLDER_PURCHASE_INVOICE_ID": DocType.PURCHASE_INVOICE,
    "FOLDER_SALES_INVOICE_ID": DocType.SALES_INVOICE,
    "FOLDER_SALARY_SLIP_ID": DocType.SALARY_SLIP,
    "FOLDER_CREDIT_CARD_ID": DocType.CREDIT_CARD,
    "FOLDER_TRANSIT_IC_ID": DocType.TRANSIT_IC,
}


# 逐行記帳（1 明細 = 1 独立取引）を行う doc_type。
#
# **本モジュールに置く理由**: 読み手が `ocr_engine`（producer 側。予算・
# サルベージ・行欠け検出の可否）と `sheets_output`（consumer 側。取引No の
# 採番単位）に分かれるため。`ocr_engine` 側に置いたままだと
# `sheets_output` が google.generativeai まで引き込むことになる。
# 複製して突合テストで縛る手もあるが（`page_family` の EXCLUDE_DEST_* が
# その形）、あれは「venv 非依存を保つ」ための妥協であって、両者とも本
# モジュールを既に import している以上ここで複製する理由が無い。
#
# 既存 doc_type をこの集合に入れてはいけない —— 取引No の採番語義
# （1 ファイル = 1 取引 か、1 明細行 = 1 取引 か）が変わる。
LINE_MODE_DOC_TYPES = frozenset({DocType.CREDIT_CARD, DocType.TRANSIT_IC})
