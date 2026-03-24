# doc_types.py
# 文書タイプ定義と会計科目デフォルトマッピング


class DocType:
    """文書タイプ定数"""
    RECEIPT = "receipt"                       # 領収書
    PURCHASE_INVOICE = "purchase_invoice"     # 支払請求書・仕入請求書
    SALES_INVOICE = "sales_invoice"           # 売上請求書
    SALARY_SLIP = "salary_slip"              # 賃金台帳・給与明細書

    ALL = [RECEIPT, PURCHASE_INVOICE, SALES_INVOICE, SALARY_SLIP]


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
}

# Tab 名のサフィックス（Google Sheets 出力用）
DOC_TYPE_TAB_SUFFIX = {
    DocType.RECEIPT: "領収書",
    DocType.PURCHASE_INVOICE: "支払請求書",
    DocType.SALES_INVOICE: "売上請求書",
    DocType.SALARY_SLIP: "給与明細",
}

# 環境変数名とDocTypeの対応
ENV_FOLDER_MAP = {
    "FOLDER_RECEIPT_ID": DocType.RECEIPT,
    "FOLDER_PURCHASE_INVOICE_ID": DocType.PURCHASE_INVOICE,
    "FOLDER_SALES_INVOICE_ID": DocType.SALES_INVOICE,
    "FOLDER_SALARY_SLIP_ID": DocType.SALARY_SLIP,
}
