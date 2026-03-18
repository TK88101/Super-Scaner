# sheets_output.py — Google Sheets 出力モジュール (csv_writer.py の後継)
import re
import gspread
from gspread_formatting import CellFormat, Color, format_cell_range, Border, Borders
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

# MF 27 列 + 原票URL (28列目)
MF_HEADERS = [
    "取引No", "取引日", "借方勘定科目", "借方補助科目", "借方部門", "借方取引先",
    "借方税区分", "借方インボイス", "借方金額(円)", "借方税額",
    "貸方勘定科目", "貸方補助科目", "貸方部門", "貸方取引先", "貸方税区分",
    "貸方インボイス", "貸方金額(円)", "貸方税額", "摘要", "仕訳メモ",
    "タグ", "MF仕訳タイプ", "決算整理仕訳", "作成日時", "作成者",
    "最終更新日時", "最終更新者", "原票URL"
]


class SheetsOutputWriter:
    """Google Sheets への仕訳データ出力"""

    def __init__(self, spreadsheet_id, credentials_file):
        gc = gspread.service_account(filename=credentials_file)
        self.spreadsheet = gc.open_by_key(spreadsheet_id)
        self._ensure_config_tab()

    def _ensure_config_tab(self):
        """隠し _config tab を確保し、全局取引No を管理"""
        try:
            self._config_ws = self.spreadsheet.worksheet("_config")
        except gspread.exceptions.WorksheetNotFound:
            self._config_ws = self.spreadsheet.add_worksheet(
                title="_config", rows=10, cols=2
            )
            self._config_ws.update('A1:B1', [["key", "value"]])
            self._config_ws.update('A2:B2', [["next_transaction_no", "1"]])

    def _get_next_transaction_no(self):
        """_config tab から次の取引No を取得"""
        cell = self._config_ws.find("next_transaction_no")
        if not cell:
            self._config_ws.update('A2:B2', [["next_transaction_no", "1"]])
            return 1
        current = int(self._config_ws.cell(cell.row, cell.col + 1).value or "1")
        return current

    def _increment_transaction_no(self, count):
        """取引No を count 分インクリメント"""
        cell = self._config_ws.find("next_transaction_no")
        if cell:
            current = int(self._config_ws.cell(cell.row, cell.col + 1).value or "1")
            self._config_ws.update_cell(cell.row, cell.col + 1, current + count)

    def _get_or_create_tab(self, tab_name):
        """タブを取得、なければ作成してヘッダーを書き込む"""
        try:
            ws = self.spreadsheet.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            ws = self.spreadsheet.add_worksheet(
                title=tab_name, rows=1000, cols=len(MF_HEADERS)
            )
            ws.append_row(MF_HEADERS, value_input_option='USER_ENTERED')
        return ws

    def write_separator(self, worksheet, label):
        """分割線を書き込む（摘要列にラベル、上部ボーダー）"""
        separator_row = [""] * len(MF_HEADERS)
        separator_row[18] = f"── {label} ──"  # 摘要列 (index 18)
        worksheet.append_row(separator_row, value_input_option='USER_ENTERED')

        row_count = len(worksheet.get_all_values())
        border_fmt = CellFormat(
            borders=Borders(
                top=Border("SOLID_MEDIUM", Color(0.3, 0.3, 0.3))
            )
        )
        format_cell_range(worksheet, f"A{row_count}:AB{row_count}", border_fmt)

    def append_entries(self, employee_name, doc_type, entries_data, source_url=""):
        """
        メイン書き込みメソッド。

        Args:
            employee_name: 従業員名
            doc_type: 文書タイプ (DocType value)
            entries_data: OCR結果 dict (date, vendor, invoice_num, memo, entries[])
            source_url: 原票 PDF の webViewLink
        """
        from doc_types import DOC_TYPE_TAB_SUFFIX
        from config import ACCOUNT_MAP

        tab_suffix = DOC_TYPE_TAB_SUFFIX.get(doc_type, "領収書")
        tab_name = f"{employee_name}_{tab_suffix}"
        ws = self._get_or_create_tab(tab_name)

        # 既存データがあれば分割線
        all_values = ws.get_all_values()
        if len(all_values) > 1:
            now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
            label = f"{entries_data.get('vendor', '不明')} ({now_jst})"
            self.write_separator(ws, label)

        entries = entries_data.get("entries", [])
        if not entries:
            print("⚠️ 書き込み対象の仕訳がありません")
            return

        uploader_name = entries_data.get('uploader', employee_name)
        invoice_num = _sanitize_invoice_num(entries_data.get('invoice_num', ''))
        vendor_name = entries_data.get('vendor', '')
        memo = entries_data.get('memo', '')
        transaction_no = self._get_next_transaction_no()

        rows = []
        anomaly_rows = []

        for i, entry in enumerate(entries):
            amount = entry.get("amount")
            if not amount or int(amount) == 0:
                continue

            # 科目マッピング
            debit_account = entry.get("debit_account", "")
            debit_account = ACCOUNT_MAP.get(debit_account, debit_account)

            credit_account = entry.get("credit_account", "")
            credit_account = ACCOUNT_MAP.get(credit_account, credit_account)

            description = f"{vendor_name} - {entry.get('description', '')}"
            if uploader_name:
                description += f" [担当: {uploader_name}]"

            now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M")

            row = [
                transaction_no,
                entries_data.get("date", ""),
                debit_account,
                entry.get("debit_sub_account", ""),
                "",                                             # 借方部門
                vendor_name,
                entry.get("debit_tax_type", ""),
                _sanitize_invoice_num(entry.get("debit_invoice", invoice_num)),
                int(amount),
                entry.get("debit_tax_amount", ""),
                credit_account,
                entry.get("credit_sub_account", uploader_name), # 貸方補助科目 = 経办人
                "",                                             # 貸方部門
                entry.get("credit_vendor", ""),
                entry.get("credit_tax_type", "対象外"),
                entry.get("credit_invoice", ""),
                entry.get("credit_amount", int(amount)),
                entry.get("credit_tax_amount", ""),
                description,
                memo,
                "",                                             # タグ
                "",                                             # MF仕訳タイプ
                "",                                             # 決算整理仕訳
                now_jst,
                uploader_name,
                now_jst,
                uploader_name,
                source_url,
            ]
            rows.append(row)

            # 異常検出
            from anomaly_detector import detect_anomalies
            flags = detect_anomalies(entry, entries_data)
            if flags:
                anomaly_rows.append((len(rows) - 1, flags))

            transaction_no += 1

        if rows:
            ws.append_rows(rows, value_input_option='USER_ENTERED')
            self._increment_transaction_no(len(rows))

            # 異常行の強調表示
            current_total = len(ws.get_all_values())
            start_row = current_total - len(rows) + 1
            for offset, flags in anomaly_rows:
                actual_row = start_row + offset
                self._apply_anomaly_highlight(ws, actual_row, flags)

            print(f"💾 Sheets に {len(rows)} 行追加: {tab_name}")

    def _apply_anomaly_highlight(self, worksheet, row_num, flags):
        """異常行にハイライトを適用"""
        color = Color(1, 1, 0.7)  # デフォルト: 薄い黄色
        for flag in flags:
            if flag.get("severity") == "high":
                color = Color(1, 0.8, 0.8)  # 赤系
                break
            elif flag.get("severity") == "medium":
                color = Color(1, 0.9, 0.7)  # オレンジ系

        fmt = CellFormat(backgroundColor=color)
        format_cell_range(worksheet, f"A{row_num}:AB{row_num}", fmt)


def _sanitize_invoice_num(raw):
    """T番号バリデーション（csv_writer.py から移植）"""
    if not raw:
        return ""
    s = str(raw).strip()
    s = s.replace("-", "")
    if s.startswith("t"):
        s = "T" + s[1:]
    if re.match(r'^T\d{13}$', s):
        return s
    return ""
