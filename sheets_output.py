# sheets_output.py — Google Sheets 出力モジュール (csv_writer.py の後継)
import re
import time
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
        self._cleanup_default_sheet()
        # キャッシュ: tab 参照と取引No をメモリに保持し API 呼び出しを削減
        self._ws_cache = {}
        self._tab_has_data = {}
        self._tab_next_txn = {}  # タブごとの取引No

    def _cleanup_default_sheet(self):
        """デフォルトの空シート（シート1）を削除"""
        try:
            default_ws = self.spreadsheet.worksheet("シート1")
            # シートが2つ以上ある場合のみ削除（最低1シート必要）
            if len(self.spreadsheet.worksheets()) > 1:
                self.spreadsheet.del_worksheet(default_ws)
        except (gspread.exceptions.WorksheetNotFound, Exception):
            pass

    def _get_next_txn_no(self, tab_name, ws):
        """タブごとの次の取引No を取得（A列の最大値 + 1）"""
        if tab_name in self._tab_next_txn:
            return self._tab_next_txn[tab_name]
        # シートのA列から最大取引Noを取得
        try:
            all_vals = ws.get_all_values()
            max_no = 0
            for row in all_vals:
                if row and row[0]:
                    try:
                        n = int(row[0])
                        if n > max_no:
                            max_no = n
                    except (ValueError, TypeError):
                        pass
            self._tab_next_txn[tab_name] = max_no + 1
        except Exception:
            self._tab_next_txn[tab_name] = 1
        return self._tab_next_txn[tab_name]

    def _get_or_create_tab(self, tab_name):
        """タブを取得（キャッシュ付き）、なければ作成してヘッダーを書き込む"""
        if tab_name in self._ws_cache:
            return self._ws_cache[tab_name]

        try:
            ws = self.spreadsheet.worksheet(tab_name)
            # 既存 tab にデータがあるかチェック（1回だけ）
            self._tab_has_data[tab_name] = len(ws.get_all_values()) > 1
        except gspread.exceptions.WorksheetNotFound:
            ws = self.spreadsheet.add_worksheet(
                title=tab_name, rows=1000, cols=len(MF_HEADERS)
            )
            self._write_legend(ws)  # A1-A5: 凡例（5行）
            ws.append_row(MF_HEADERS, value_input_option='USER_ENTERED')  # Row 6: ヘッダー
            self._tab_has_data[tab_name] = False

        self._ws_cache[tab_name] = ws
        return ws

    def _write_legend(self, ws):
        """ハイライト凡例を A1-A6 に書き込む"""
        try:
            legend_rows = [
                ["【ハイライト凡例】"],
                ["🔴 赤系: 日付空欄 / 認識不能ページ（一行丸ごと）"],
                ["🟠 橙系: 取引先空欄 / T番号不正"],
                ["🟡 黄系: T番号空 / 要確認科目(地代家賃・保険料・雑収入) / 高額(修繕費>30万・備品>10万)"],
                ["🟣 紫系: 重複疑い（同日付・同金額）"],
            ]
            ws.append_rows(legend_rows, value_input_option='USER_ENTERED')

            # 色見本を適用
            format_cell_range(ws, "A2:A2",
                              CellFormat(backgroundColor=Color(1, 0.8, 0.8)))   # 赤系
            format_cell_range(ws, "A3:A3",
                              CellFormat(backgroundColor=Color(1, 0.9, 0.7)))   # 橙系
            format_cell_range(ws, "A4:A4",
                              CellFormat(backgroundColor=Color(1, 1, 0.7)))     # 黄系
            format_cell_range(ws, "A5:A5",
                              CellFormat(backgroundColor=Color(0.85, 0.8, 1)))  # 紫系
        except Exception as e:
            print(f"⚠️ 凡例書き込み失敗: {e}")

    def write_separator(self, worksheet, tab_name, label):
        """分割線を書き込む（摘要列にラベル、上部ボーダー）"""
        separator_row = [""] * len(MF_HEADERS)
        separator_row[18] = f"── {label} ──"  # 摘要列 (index 18)
        worksheet.append_row(separator_row, value_input_option='USER_ENTERED')

        try:
            row_count = len(worksheet.get_all_values())
            border_fmt = CellFormat(
                borders=Borders(
                    top=Border("SOLID_MEDIUM", Color(0.3, 0.3, 0.3))
                )
            )
            format_cell_range(worksheet, f"A{row_count}:AB{row_count}", border_fmt)
        except Exception:
            pass  # ボーダー適用失敗は無視

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
        from config import ACCOUNT_MAP, UNKNOWN_ACCOUNT, CREDIT_SUB_ACCOUNT_RECEIPT, CREDIT_ONLY_ACCOUNTS
        from anomaly_detector import detect_anomalies

        tab_suffix = DOC_TYPE_TAB_SUFFIX.get(doc_type, "領収書")
        tab_name = f"{employee_name}_{tab_suffix}"
        ws = self._get_or_create_tab(tab_name)

        # 既存データがあれば分割線（同一 tab への初回書き込み時のみ）
        if self._tab_has_data.get(tab_name, False):
            now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
            label = f"{entries_data.get('vendor', '不明')} ({now_jst})"
            self.write_separator(ws, tab_name, label)
            self._tab_has_data[tab_name] = False  # 以降の書き込みでは分割線不要

        entries = entries_data.get("entries", [])
        if not entries:
            if entries_data.get("_unrecognized"):
                self._write_unrecognized_row(ws, tab_name, entries_data, source_url)
            else:
                print("⚠️ 書き込み対象の仕訳がありません")
            return

        uploader_name = entries_data.get('uploader', employee_name)
        invoice_num = _sanitize_invoice_num(entries_data.get('invoice_num', ''))
        vendor_name = entries_data.get('vendor', '')
        memo = entries_data.get('memo', '')
        transaction_no = self._get_next_txn_no(tab_name, ws)

        rows = []
        anomaly_flags_list = []

        for entry in entries:
            amount = entry.get("amount")
            if not amount or int(amount) == 0:
                continue

            # 科目マッピング（マップにない未知科目は「未確定勘定」）
            debit_account = entry.get("debit_account", "")
            debit_account = ACCOUNT_MAP.get(debit_account, debit_account)
            if not debit_account:
                debit_account = UNKNOWN_ACCOUNT
            # 貸方専用科目が借方に出現した場合は未確定勘定に置換
            if debit_account in CREDIT_ONLY_ACCOUNTS:
                debit_account = UNKNOWN_ACCOUNT

            credit_account = entry.get("credit_account", "")
            credit_account = ACCOUNT_MAP.get(credit_account, credit_account)

            description = f"{vendor_name} - {entry.get('description', '')}"

            now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M")

            row = [
                transaction_no,
                entries_data.get("date", ""),
                debit_account,
                "",                                             # 借方補助科目（空白）
                "",                                             # 借方部門
                vendor_name,
                entry.get("debit_tax_type", ""),
                _sanitize_invoice_num(entry.get("debit_invoice", invoice_num)),
                int(amount),
                entry.get("debit_tax_amount", ""),
                credit_account,
                "",                                             # 貸方補助科目（空白）
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

            # 異常検出（実際に書き込む値で判定）
            actual_invoice = _sanitize_invoice_num(entry.get("debit_invoice", invoice_num))
            mapped_entry = {**entry, "debit_account": debit_account}
            actual_parent = {**entries_data, "invoice_num": actual_invoice}
            flags = detect_anomalies(mapped_entry, actual_parent)
            if flags:
                anomaly_flags_list.append((len(rows) - 1, flags))

        transaction_no += 1

        if rows:
            # 書き込み前のデータを取得（ハイライト位置計算+重複検出用）
            existing_data = ws.get_all_values()
            pre_write_count = len(existing_data)

            # 一括書き込み（リトライ付き）
            self._write_with_retry(ws, rows)

            # 取引No をタブごとにメモリ更新
            self._tab_next_txn[tab_name] = transaction_no

            # 異常行のハイライト（書き込み前の行数から位置を正確に算出）
            if anomaly_flags_list:
                try:
                    start_row = pre_write_count + 1
                    for offset, flags in anomaly_flags_list:
                        actual_row = start_row + offset
                        self._apply_anomaly_highlight(ws, actual_row, flags)
                except Exception as e:
                    print(f"⚠️ 異常ハイライト適用失敗: {e}")

            # 重複疑い検出（同日付+同金額）
            self._detect_and_highlight_duplicates(ws, existing_data, rows, pre_write_count)

            print(f"💾 Sheets に {len(rows)} 行追加: {tab_name}")

    def flush(self):
        """互換性のため残す（タブごと管理なので書き戻し不要）"""
        pass

    def _write_with_retry(self, worksheet, rows, max_retries=5):
        """レート制限対策付きの行書き込み"""
        for attempt in range(max_retries):
            try:
                worksheet.append_rows(rows, value_input_option='USER_ENTERED')
                return
            except gspread.exceptions.APIError as e:
                if '429' in str(e) and attempt < max_retries - 1:
                    wait = (2 ** attempt) + 1
                    print(f"⏳ Sheets API レート制限、{wait}秒待機中...")
                    time.sleep(wait)
                else:
                    raise

    @staticmethod
    def _determine_debit_sub_account(debit_account, entry, invoice_num):
        """借方補助科目をインボイス制度に基づいて自動決定"""
        tax_type = entry.get("debit_tax_type", "")

        # 接待交際費 → 飲食贈答等 / 軽減税率対象
        if debit_account == "接待交際費":
            if "8%" in tax_type or "軽" in tax_type:
                return "軽減税率対象"
            return "飲食贈答等"

        # 福利厚生費 → 軽減税率対象 / 一般
        if debit_account == "福利厚生費":
            if "8%" in tax_type or "軽" in tax_type:
                return "軽減税率対象"
            return "一般"

        # 旅費交通費 → 適格 / 非適格（T番号の有無で判定）
        if debit_account == "旅費交通費":
            sanitized = _sanitize_invoice_num(invoice_num)
            if sanitized:
                return "適格"
            return "非適格"

        return ""

    @staticmethod
    def _determine_credit_sub_account(doc_type, entry, vendor_name):
        """貸方補助科目を文書タイプに応じて決定"""
        from config import CREDIT_SUB_ACCOUNT_RECEIPT
        from doc_types import DocType
        # 領収書: 社長名（立替払い）
        if doc_type == DocType.RECEIPT:
            return entry.get("credit_sub_account", CREDIT_SUB_ACCOUNT_RECEIPT)
        # 請求書: 取引先会社名（その会社に支払っているため）
        if doc_type == DocType.PURCHASE_INVOICE:
            return entry.get("credit_sub_account", vendor_name or "")
        # その他
        return entry.get("credit_sub_account", "")

    def _apply_anomaly_highlight(self, worksheet, row_num, flags):
        """異常セルにハイライトを適用（該当セルのみ）"""
        for flag in flags:
            severity = flag.get("severity", "low")
            if severity == "high":
                color = Color(1, 0.8, 0.8)    # 赤系
            elif severity == "medium":
                color = Color(1, 0.9, 0.7)    # オレンジ系
            else:
                color = Color(1, 1, 0.7)       # 薄い黄色

            col_index = flag.get("col")
            if col_index is not None:
                # 該当セルのみハイライト (0始まり → A=1)
                col_letter = chr(ord('A') + col_index)
                cell_ref = f"{col_letter}{row_num}"
                fmt = CellFormat(backgroundColor=color)
                format_cell_range(worksheet, cell_ref, fmt)


    def _write_unrecognized_row(self, ws, tab_name, entries_data, source_url):
        """認識不能/部分認識ページの占位行を書き込み、ハイライト適用"""
        date = entries_data.get("date", "") or ""
        vendor = entries_data.get("vendor", "") or ""
        has_partial = bool(date or vendor)

        now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
        txn_no = self._get_next_txn_no(tab_name, ws)

        row = [""] * len(MF_HEADERS)
        row[0] = txn_no              # 取引No
        row[1] = date                # 取引日
        row[5] = vendor              # 借方取引先
        if has_partial:
            row[18] = "⚠ 部分認識（金額なし）"
        else:
            row[18] = "⚠ 認識不能ページ"
        row[23] = now_jst            # 作成日時
        row[25] = now_jst            # 最終更新日時
        row[27] = source_url         # 原票URL

        pre_write = len(ws.get_all_values())
        self._write_with_retry(ws, [row])
        self._tab_next_txn[tab_name] = txn_no + 1

        actual_row = pre_write + 1

        try:
            fmt_red = CellFormat(backgroundColor=Color(1, 0.8, 0.8))
            if has_partial:
                format_cell_range(ws, f"I{actual_row}", fmt_red)
                if not date:
                    format_cell_range(ws, f"B{actual_row}", fmt_red)
                if not vendor:
                    format_cell_range(ws, f"F{actual_row}", fmt_red)
            else:
                format_cell_range(ws, f"A{actual_row}:AB{actual_row}", fmt_red)
        except Exception as e:
            print(f"⚠️ 認識不能ハイライト適用失敗: {e}")

        label = "部分認識" if has_partial else "認識不能"
        print(f"⚠️ {label}ページを記録: {tab_name} (Row {actual_row})")

    @staticmethod
    def _normalize_amount(val):
        """金額文字列を正規化（カンマ・小数点を除去して int 比較可能にする）"""
        try:
            return int(str(val).replace(",", "").replace(".", "").strip())
        except (ValueError, TypeError):
            return None

    def _detect_and_highlight_duplicates(self, ws, existing_data, new_rows, pre_write_count):
        """重複疑い検出: 同じ日付+金額の行をハイライト"""
        try:
            all_data = existing_data + new_rows

            # Build (date, amount) -> [row_numbers] map
            pair_map = {}
            for i, row in enumerate(all_data):
                txn = row[0] if row else ""
                try:
                    int(txn)
                except (ValueError, TypeError):
                    continue

                date = row[1] if len(row) > 1 else ""
                raw_amount = row[8] if len(row) > 8 else ""
                amount = self._normalize_amount(raw_amount)
                if not date or amount is None:
                    continue
                key = (date, amount)
                if key not in pair_map:
                    pair_map[key] = []
                pair_map[key].append(i + 1)  # 1-based row number

            start_new = pre_write_count + 1
            end_new = pre_write_count + len(new_rows)
            dup_fmt = CellFormat(backgroundColor=Color(0.85, 0.8, 1))
            highlighted = 0

            for key, row_nums in pair_map.items():
                if len(row_nums) < 2:
                    continue
                has_new = any(start_new <= r <= end_new for r in row_nums)
                if not has_new:
                    continue
                for r in row_nums:
                    format_cell_range(ws, f"B{r}", dup_fmt)
                    format_cell_range(ws, f"I{r}", dup_fmt)
                    highlighted += 1

            if highlighted > 0:
                print(f"🔄 重複疑い検出: {highlighted}行をハイライトしました")
        except Exception as e:
            print(f"⚠️ 重複検出処理失敗: {e}")


def _sanitize_invoice_num(raw):
    """T番号バリデーション（形式 + 法人番号チェックディジット検証）"""
    if not raw:
        return ""
    s = str(raw).strip()
    s = s.replace("-", "")
    if s.startswith("t"):
        s = "T" + s[1:]
    if not re.match(r'^T\d{13}$', s):
        return ""
    # 法人番号チェックディジット検証
    digits = s[1:]  # T を除いた13桁
    check = int(digits[0])
    body = digits[1:]  # 12桁の本体
    total = sum(int(d) * (1 + (i % 2)) for i, d in enumerate(reversed(body)))
    expected = 9 - (total % 9) if total % 9 != 0 else 9
    if check != expected:
        return ""
    return s
