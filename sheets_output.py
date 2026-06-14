# sheets_output.py — Google Sheets 出力モジュール (csv_writer.py の後継)
import re
import time
import gspread
from gspread_formatting import CellFormat, Color, format_cell_range, format_cell_ranges, Border, Borders
from datetime import datetime, timezone, timedelta
from doc_types import DocType

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


# 異常ハイライトの severity → 背景色。凡例・per-entry・doc 級で共用し、
# 同じ severity が複数箇所で別の色にブレる事故を防ぐ（single source of truth）。
_SEVERITY_COLORS = {
    "high": Color(1, 0.8, 0.8),    # 赤系
    "medium": Color(1, 0.9, 0.7),  # 橙系
    "low": Color(1, 1, 0.7),       # 黄系
}


def _severity_color(severity):
    """severity ラベルから背景色を返す（未知値は黄系にフォールバック）。"""
    return _SEVERITY_COLORS.get(severity, _SEVERITY_COLORS["low"])


def _build_description(doc_type, vendor_name, item_description):
    """摘要文字列を組み立てる。

    領収書（6/11 顧客回答）: 「店名 税率」形式（空白区切り、例: ファミマ 10%）。
    vendor が空の場合は strip で先頭空白を付けない。同 tab 内の振込控え
    （bank_transfer）行も tab 内格式統一のため同形式を適用する。
    その他の doc_type は既存の「店名 - 内容」形式を維持する。
    """
    if doc_type == DocType.RECEIPT:
        return f"{vendor_name} {item_description}".strip()
    return f"{vendor_name} - {item_description}"


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
            self._write_legend(ws)  # A1-A4: 凡例（4行）
            ws.append_row(MF_HEADERS, value_input_option='USER_ENTERED')  # Row 5: ヘッダー
            self._tab_has_data[tab_name] = False

        self._ws_cache[tab_name] = ws
        return ws

    def _write_legend(self, ws):
        """ハイライト凡例を A1-A5 に書き込む"""
        try:
            legend_rows = [
                ["【ハイライト凡例】"],
                ["🔴 赤系: 日付空欄 / 認識不能ページ（一行丸ごと） / 票面合計≠行合計（金額列）"],
                ["🟠 橙系: 取引先空欄 / T番号不正"],
                ["🟡 黄系: T番号空 / 要確認科目(地代家賃・保険料・雑収入) / 高額(修繕費>30万・備品>10万)"],
            ]
            ws.append_rows(legend_rows, value_input_option='USER_ENTERED')

            # 色見本を適用（_severity_color と同一ソース、色ブレ防止）
            format_cell_range(ws, "A2:A2",
                              CellFormat(backgroundColor=_severity_color("high")))    # 赤系
            format_cell_range(ws, "A3:A3",
                              CellFormat(backgroundColor=_severity_color("medium")))  # 橙系
            format_cell_range(ws, "A4:A4",
                              CellFormat(backgroundColor=_severity_color("low")))     # 黄系
        except Exception as e:
            print(f"⚠️ 凡例書き込み失敗: {e}")

    def start_new_file(self, employee_name, doc_type, filename=""):
        """新しいPDFファイルの処理開始を通知。分割線+取引No リセット。"""
        from doc_types import DOC_TYPE_TAB_SUFFIX
        tab_suffix = DOC_TYPE_TAB_SUFFIX.get(doc_type, "領収書")
        tab_name = f"{employee_name}_{tab_suffix}"
        ws = self._get_or_create_tab(tab_name)

        # 既存データがあれば太黒線で分割
        try:
            row_count = len(ws.get_all_values())
            if row_count > 6:  # legend(4) + header(1) + 1data row以上
                separator_row = [""] * len(MF_HEADERS)
                separator_row[18] = f"──── {filename} ────"
                ws.append_row(separator_row, value_input_option='USER_ENTERED')
                new_row = len(ws.get_all_values())
                # append_row は直前行の背景色を継承する。
                # 前ファイル最終行の異常ハイライト（H列黄色等）が separator 行に残らないよう、
                # 背景を白にリセットしてから上罫線を適用する。
                fmt_white = CellFormat(backgroundColor=Color(1, 1, 1))
                format_cell_range(ws, f"A{new_row}:AB{new_row}", fmt_white)
                border_fmt = CellFormat(
                    borders=Borders(
                        top=Border("SOLID_THICK", Color(0, 0, 0))
                    )
                )
                format_cell_range(ws, f"A{new_row}:AB{new_row}", border_fmt)
        except Exception as e:
            print(f"⚠️ 分割線書き込み失敗: {e}")

        # 取引No を 1 にリセット
        self._tab_next_txn[tab_name] = 1

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
        from config import (ACCOUNT_MAP, UNKNOWN_ACCOUNT,
                            CREDIT_SUB_ACCOUNT_RECEIPT, CREDIT_ONLY_ACCOUNTS,
                            DOC_LOW_CONFIDENCE_THRESHOLD)
        from anomaly_detector import (detect_anomalies,
                                      detect_document_anomalies,
                                      detect_outlier_exempt_rows,
                                      detect_low_confidence)
        from receipt_aggregation import coerce_tax_amount

        tab_suffix = DOC_TYPE_TAB_SUFFIX.get(doc_type, "領収書")
        tab_name = f"{employee_name}_{tab_suffix}"
        ws = self._get_or_create_tab(tab_name)

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

            description = _build_description(doc_type, vendor_name, entry.get('description', ''))

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

            start_row = pre_write_count + 1
            end_row = start_row + len(rows) - 1

            # Sheets の append は直前行の書式を継承する。
            # 異常ハイライトが下の行に波及しないよう、新規行を白にリセットしてから異常色を被せる。
            try:
                fmt_white = CellFormat(backgroundColor=Color(1, 1, 1))
                format_cell_range(ws, f"A{start_row}:AB{end_row}", fmt_white)
            except Exception as e:
                print(f"⚠️ 新規行の背景リセット失敗: {e}")

            # 規則②: 低置信整票を全行黄で下地マーク（人手複査推奨）。
            # 塗り順は「黄(全行) → per-entry(各セル) → 赤(I列)」: 全行黄を最初に塗り、
            # より高優先度の per-entry 赤橙・doc 赤を後から被せて消さない（codex 指摘）。
            # 領収書限定。独立 try/except（429 リトライ、失敗しても行データは無傷）。
            if doc_type == DocType.RECEIPT:
                conf_flags = detect_low_confidence(
                    entries_data, DOC_LOW_CONFIDENCE_THRESHOLD)
                if conf_flags:
                    print(f"🟡 {conf_flags[0]['message']}: {vendor_name}")
                    try:
                        self._format_with_retry(
                            ws, f"A{start_row}:AB{end_row}",
                            CellFormat(backgroundColor=_severity_color("low")))
                    except Exception as e:
                        print(f"⚠️ 低置信ハイライト適用失敗: {e}")

            # 異常行のハイライト（書き込み前の行数から位置を正確に算出）
            if anomaly_flags_list:
                try:
                    for offset, flags in anomaly_flags_list:
                        actual_row = start_row + offset
                        self._apply_anomaly_highlight(ws, actual_row, flags)
                except Exception as e:
                    print(f"⚠️ 異常ハイライト適用失敗: {e}")

            # 票面合計とΣ行金額の照合（doc 級）。Gemini の外税換算漏れ・幻覚行・
            # 行落ちを金額列（I列）の赤ハイライトで可視化する
            # （6/12 E2E 静默錯 2/28 対策。領収書限定の機能のため他 doc_type では呼ばない）
            if doc_type == DocType.RECEIPT:
                # 票面合計照合（[B']）と対象外行の構造異常（規則①）を合流し、
                # I列（金額列）を一度だけ赤塗りする（重複塗り防止）
                amount_col = [r[8] for r in rows]      # I列=借方金額
                tax_type_col = [r[6] for r in rows]    # G列=借方税区分
                doc_flags = detect_document_anomalies(entries_data, amount_col)
                # 規則①は真の receipt 限定。bank_transfer/fee_receipt は本体が
                # 「対象外」かつ高額になり得るため除外（合計照合は total=None で既に
                # スキップ済み、規則①も doc_category で揃える。codex 指摘）
                exempt_flags = []
                if entries_data.get("doc_category") == "receipt":
                    exempt_flags = detect_outlier_exempt_rows(
                        amount_col, tax_type_col,
                        entries_data.get("total_amount"))
                red_flags = doc_flags + exempt_flags

                # 票面合計照合[B']+対象外[規則①] → I列を一度だけ赤塗り（重複塗り防止）。
                # severity=high → 赤（_severity_color の high）。黄(下地)・per-entry の
                # 後に塗り、I列を最終的に赤にする。上の一括 try/except とは独立（429 リトライ）。
                if red_flags:
                    for flag in red_flags:
                        print(f"⚠️ 異常検出: {flag['message']}")
                    try:
                        self._format_with_retry(
                            ws, f"I{start_row}:I{end_row}",
                            CellFormat(backgroundColor=_severity_color("high")))
                    except Exception as e:
                        print(f"⚠️ 異常ハイライト適用失敗: {e}")
                elif not coerce_tax_amount(entries_data.get("total_amount")):
                    # 照合カバレッジの可観測性: total_amount 欠損で照合が
                    # 無言で蒸発していないか E2E ログで確認できるようにする
                    print(f"ℹ️ 合計照合スキップ（total_amount なし）: {vendor_name}")

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

    def _format_with_retry(self, worksheet, cell_ref, fmt, max_retries=5):
        """レート制限対策付きのセル書式適用（_write_with_retry と同方針）"""
        for attempt in range(max_retries):
            try:
                format_cell_range(worksheet, cell_ref, fmt)
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
        """異常セルにハイライトを適用（該当セルのみ or 全行）"""
        for flag in flags:
            severity = flag.get("severity", "low")
            fmt = CellFormat(backgroundColor=_severity_color(severity))

            if flag.get("full_row"):
                cell_ref = f"A{row_num}:AB{row_num}"
                format_cell_range(worksheet, cell_ref, fmt)
            else:
                col_index = flag.get("col")
                if col_index is not None:
                    col_letter = chr(ord('A') + col_index)
                    cell_ref = f"{col_letter}{row_num}"
                    format_cell_range(worksheet, cell_ref, fmt)


    def _write_unrecognized_row(self, ws, tab_name, entries_data, source_url):
        """認識不能/部分認識ページの占位行を書き込み、ハイライト適用"""
        date = entries_data.get("date", "") or ""
        vendor = entries_data.get("vendor", "") or ""
        memo_override = entries_data.get("memo") or ""
        has_partial = bool(date or vendor)

        now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
        txn_no = self._get_next_txn_no(tab_name, ws)

        row = [""] * len(MF_HEADERS)
        row[0] = txn_no              # 取引No
        row[1] = date                # 取引日
        row[5] = vendor              # 借方取引先
        if memo_override:
            row[18] = memo_override
        elif has_partial:
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
            fmt_white = CellFormat(backgroundColor=Color(1, 1, 1))
            format_cell_range(ws, f"A{actual_row}:AB{actual_row}", fmt_white)
        except Exception as e:
            print(f"⚠️ 認識不能行の背景リセット失敗: {e}")

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
        """重複疑い検出: 同日付+同金額+同取引先+同摘要の行をハイライト"""
        try:
            all_data = existing_data + new_rows

            # Build (date, amount, vendor, description) -> [row_numbers] map
            pair_map = {}
            for i, row in enumerate(all_data):
                txn = row[0] if row else ""
                try:
                    int(txn)
                except (ValueError, TypeError):
                    continue

                date = row[1] if len(row) > 1 else ""
                vendor = row[5] if len(row) > 5 else ""
                raw_amount = row[8] if len(row) > 8 else ""
                desc = row[18] if len(row) > 18 else ""
                amount = self._normalize_amount(raw_amount)
                if not date or amount is None:
                    continue
                key = (date, amount, vendor, desc)
                if key not in pair_map:
                    pair_map[key] = []
                pair_map[key].append(i + 1)  # 1-based row number

            start_new = pre_write_count + 1
            end_new = pre_write_count + len(new_rows)
            dup_fmt = CellFormat(backgroundColor=Color(0.85, 0.8, 1))
            batch_ranges = []

            for key, row_nums in pair_map.items():
                if len(row_nums) < 2:
                    continue
                has_new = any(start_new <= r <= end_new for r in row_nums)
                if not has_new:
                    continue
                # 同一取引No 内の重複はスキップ（同一レシートの複数品目は重複ではない）
                # 異なる取引No で完全一致した場合のみ重複疑い
                txn_nos = set()
                for r in row_nums:
                    row_data = all_data[r - 1]
                    txn_nos.add(str(row_data[0]) if row_data else "")
                if len(txn_nos) <= 1:
                    continue  # 全て同一レシート → 重複ではない
                for r in row_nums:
                    batch_ranges.append((f"B{r}", dup_fmt))
                    batch_ranges.append((f"I{r}", dup_fmt))

            if batch_ranges:
                format_cell_ranges(ws, batch_ranges)
                print(f"🔄 重複疑い検出: {len(batch_ranges) // 2}行をハイライトしました")
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
