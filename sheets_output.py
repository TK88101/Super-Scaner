# sheets_output.py — Google Sheets 出力モジュール (csv_writer.py の後継)
import re
import time
from dataclasses import dataclass
import gspread
from gspread_formatting import CellFormat, Color, format_cell_range, format_cell_ranges, Border, Borders
from datetime import datetime, timezone, timedelta
from doc_types import DocType
from tag_rules import derive_row_tags, UNRECOGNIZED_TAG

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


# ── 除外ページ監査タブ（IP-401 T2）──
# **タブ名は必ず '_' 始まりにすること。** GAS の backupAllTabs_ は
# SKIP_TABS と「先頭が '_'」以外の全タブを MF_Backup へ退避したうえで
# deleteSourceTabs_ で元タブを削除する（gas/daily_backup_ido.gs）。通常名で
# 作ると監査タブが毎晩 22:00 に消える。
AUDIT_TAB_NAME = "_除外ページ監査"
AUDIT_HEADERS = ["日時", "ファイル名", "ページ", "判定", "理由", "OCR文字数", "原票URL"]

# 「判定」列の値。理由（reason）は機械可読な英字キー、判定は人が読む日本語。
AUDIT_VERDICT_EXCLUDED = "除外"   # 仕訳を作らず MF 区にも書かなかったページ
AUDIT_VERDICT_BRANCH = "分岐"     # MF には正常記帳したが封筒シグナルも命中した
AUDIT_VERDICT_MISSING = "欠落"    # 一度も出力されなかったページ（無音欠落の疑い）
# 過去輪と今輪で分類が食い違ったページ（IP-402 §4.2）。MF に残る旧行（仕訳 or
# 占位）と今輪の判定が矛盾している事実を人手が突合できるよう、理由列に
# `drift:<過去輪の分類>-><今輪の分類>` を入れて両方の分類を符号化する。
AUDIT_VERDICT_DRIFT = "分類変化"

if not AUDIT_TAB_NAME.startswith("_"):
    # import 時に落とす。命名規約を破ると監査タブが毎晩 22:00 に GAS へ削除され、
    # 「無音でデータが消える」——本モジュールが直そうとしている事故そのものが
    # 監査証跡に対して再発する。規約とテスト1本に頼ると、リネーム PR が
    # レビューを素通りして本番で静かに壊れうる。ocr_engine の
    # _validate_doc_type_registries と同じ「起動時に大声で落ちる」方針を採る。
    raise RuntimeError(
        f"監査タブ名は '_' 始まり必須です（GAS backupAllTabs_ が退避後に元タブを"
        f"削除するため）: {AUDIT_TAB_NAME!r}"
    )


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


# タグ列（U列 = MF_HEADERS index 20）。標色行に「赤系/橙系/黄系」を記入する。
TAG_COL_INDEX = MF_HEADERS.index("タグ")

# 自動拡容バグ対策（6/18 社長指摘）: append_rows がグリッド境界（既定1000行）を
# 跨ぐと Google が新規行に直前行の書式を継承し、標色された最終行の色が空尾行へ
# 伝染する。書き込み前に add_rows で空きバッファを確保し、自動拡容自体を起こさない。
GRID_ROW_BUFFER = 200


# --- IP-304 頁級原子書＋共通ビルダの純データ型（定稿 Plan §9 D2'） ------------------
@dataclass(frozen=True)
class HighlightOp:
    """高亮の一操作（ブロック相対 offset。commit 側で絶対行へ translate）。

    row_start/row_end：ブロック先頭からの 0-based 行 offset（inclusive）。
    col："full"（A:AB 全列）または列文字（"I"/"B"/"F"/"H" 等、単列）。
    severity："high"/"medium"/"low" → _severity_color で色に写す（単一ソース）。
    """

    row_start: int
    row_end: int
    col: str
    severity: str


@dataclass(frozen=True)
class ResultBlock:
    """1 result（1 票）分の rows＋異常メタ（純データ、書込・高亮なし）。

    append_entries（UI）は rows/*_flags をそのまま Phase B で使う（挙動保存）。
    headless は _result_ops で highlight_ops へ翻訳する。
    """

    rows: list
    anomaly_flags: list   # [(行 offset, flags)]（per-entry）
    conf_flags: list      # 低置信（RECEIPT・全行黄の下地）
    red_flags: list       # doc 赤（合計不符/規則①・I列赤）
    next_txn_no: int


@dataclass(frozen=True)
class UnrecognizedBlock:
    """占位行（認識不能/部分認識）の row＋高亮判定材料（UI/headless 共用、#9）。"""

    row: list
    has_partial: bool
    has_date: bool
    has_vendor: bool
    next_txn_no: int


@dataclass(frozen=True)
class PageWrite:
    """1 頁分の書込計画（純データ、書込副作用ゼロ）。commit_page が消費する。"""

    tab_name: str
    rows: list
    highlight_ops: tuple
    txn_range: tuple      # (start_txn_no, last_txn_no)


def _op_cell_ref(op, block_start_row):
    """HighlightOp（ブロック相対）を Sheets の絶対セル範囲文字列へ翻訳する。"""
    r1 = block_start_row + op.row_start
    r2 = block_start_row + op.row_end
    if op.col == "full":
        return f"A{r1}:AB{r2}"
    return f"{op.col}{r1}:{op.col}{r2}"


def _result_block_ops(block, offset):
    """ResultBlock の *_flags を頁相対 HighlightOp 列へ翻訳（塗り順＝黄→per-entry→赤）。"""
    ops = []
    n = len(block.rows)
    if block.conf_flags:
        ops.append(HighlightOp(offset, offset + n - 1, "full", "low"))
    for row_offset, flags in block.anomaly_flags:
        for flag in flags:
            severity = flag.get("severity", "low")
            if flag.get("full_row"):
                ops.append(HighlightOp(offset + row_offset, offset + row_offset,
                                       "full", severity))
            else:
                col_index = flag.get("col")
                if col_index is not None:
                    col_letter = chr(ord('A') + col_index)
                    ops.append(HighlightOp(offset + row_offset, offset + row_offset,
                                           col_letter, severity))
    if block.red_flags:
        ops.append(HighlightOp(offset, offset + n - 1, "I", "high"))
    return ops


def _unrecognized_block_ops(block, offset):
    """UnrecognizedBlock の占位行高亮（_write_unrecognized_row の塗り分けと同型）。"""
    if block.has_partial:
        ops = [HighlightOp(offset, offset, "I", "high")]
        if not block.has_date:
            ops.append(HighlightOp(offset, offset, "B", "high"))
        if not block.has_vendor:
            ops.append(HighlightOp(offset, offset, "F", "high"))
        return ops
    return [HighlightOp(offset, offset, "full", "high")]


def _canonical_row(row):
    """MF 行を指紋用の正規文字列へ（28 列へパディング＋str strip、write/read 境界で安定）。"""
    cells = [str(row[i]).strip() if i < len(row) else "" for i in range(len(MF_HEADERS))]
    return "\x1f".join(cells)


def compute_page_fingerprint(rows):
    """頁の MF rows から順序非依存（multiset）の指紋を計算する（定稿 Plan §9 witness）。

    PENDING 記録時（page_write.rows）と恢復時（Sheets 実行の読取り）で同一値なら一致する
    ように、各セルを 28 列へ正規化・str strip してから行ハッシュを sorted 連結して sha256。
    USER_ENTERED による数値/日付の再整形は真庫聯調（U14）で検証する既知の前提。
    """
    import hashlib
    canon = sorted(_canonical_row(r) for r in rows)
    joined = "\x1e".join(canon)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _tab_name(employee_name, doc_type):
    """`{従業員名}_{文書後缀}` タブ名（4 箇所で共用、後缀既定＝領収書）。"""
    from doc_types import DOC_TYPE_TAB_SUFFIX
    return f"{employee_name}_{DOC_TYPE_TAB_SUFFIX.get(doc_type, '領収書')}"


def _classify_probe(segment, predicted_row_range, row_fingerprint):
    """読取り済み行ブロックを ProbeResult へ分類（I/O 抜きの純判定、real/fake probe 共用）。

    空/該当行無し→ABSENT／範囲末尾不足→ESCALATE／指紋全符→PRESENT／不一致→ESCALATE
    （定稿 Plan §9 D3' の witness 判定表。啓発的探索なし）。
    """
    from posting_ledger import ProbeResult
    start, end = predicted_row_range
    non_empty = [r for r in segment if any(str(c).strip() for c in r)]
    if not non_empty:
        return ProbeResult.ABSENT
    if len(segment) != (end - start + 1):
        return ProbeResult.ESCALATE
    if compute_page_fingerprint(segment) == row_fingerprint:
        return ProbeResult.PRESENT
    return ProbeResult.ESCALATE


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
        self._tabs_sanitized = set()  # 空尾行の継承色を初回清掃済みのタブ
        self._audit_row_count = 0     # 監査タブの行数（初回だけ実測し以後自前更新）

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
                ["🟡 黄系【行全体が黄】OCR置信度が低い＝システムが読み取りに自信なし→人工確認推奨(データが誤りとは限らない) ／ 【セル単体が黄】T番号空・要確認科目(地代家賃/保険料/雑収入)・高額(修繕費>30万/備品>10万)"],
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
        tab_name = _tab_name(employee_name, doc_type)
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

    def _build_result_rows(self, employee_name, doc_type, entries_data,
                           source_url, txn_no):
        """1 result（1 票）分の MF rows＋異常メタを純ロジックで構築する（書込なし）。

        append_entries（UI）と build_page_write（headless）の共通ビルダ。有効金額の
        行が 1 つも無ければ None を返す（呼び出し側が占位行へ分岐）。txn_no は採番済み
        の値を受け取り（ws 非依存＝純）、全行に同一取引Noを振り、次番号を next_txn_no へ。
        """
        from config import (ACCOUNT_MAP, UNKNOWN_ACCOUNT, CREDIT_ONLY_ACCOUNTS,
                            DOC_LOW_CONFIDENCE_THRESHOLD)
        from anomaly_detector import (detect_anomalies,
                                      detect_document_anomalies,
                                      detect_outlier_exempt_rows,
                                      detect_low_confidence)

        entries = entries_data.get("entries", [])
        uploader_name = entries_data.get('uploader', employee_name)
        invoice_num = _sanitize_invoice_num(entries_data.get('invoice_num', ''))
        vendor_name = entries_data.get('vendor', '')
        memo = entries_data.get('memo', '')
        transaction_no = txn_no

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

        if not rows:
            return None

        transaction_no += 1

        # --- 色判定を書き込み前に確定（色塗りは行番号が要るため後段で実施）---
        conf_flags = []
        red_flags = []
        if doc_type == DocType.RECEIPT:
            conf_flags = detect_low_confidence(
                entries_data, DOC_LOW_CONFIDENCE_THRESHOLD)
            amount_col = [r[8] for r in rows]      # I列=借方金額
            tax_type_col = [r[6] for r in rows]    # G列=借方税区分
            doc_flags = detect_document_anomalies(entries_data, amount_col)
            exempt_flags = []
            if entries_data.get("doc_category") == "receipt":
                exempt_flags = detect_outlier_exempt_rows(
                    amount_col, tax_type_col,
                    entries_data.get("total_amount"))
            red_flags = doc_flags + exempt_flags

        # U列(タグ): 標色される行に「赤系/橙系/黄系」を埋め込む（社長要望 6/18）。
        tags = derive_row_tags(
            len(rows), anomaly_flags_list,
            doc_low_confidence=bool(conf_flags), doc_red=bool(red_flags))
        for i, tag in enumerate(tags):
            rows[i][TAG_COL_INDEX] = tag   # "" = 無標色（既定の空セルと同値）

        return ResultBlock(rows, anomaly_flags_list, conf_flags, red_flags,
                           transaction_no)

    def _build_unrecognized_block(self, entries_data, source_url, txn_no):
        """占位行（認識不能/部分認識）の row＋高亮判定材料を純ロジックで構築する（#9）。

        _write_unrecognized_row（UI）と build_page_write（headless）で row/tag を共用し、
        commit のみ分流する。row の内容は従来の _write_unrecognized_row と一字一句同じ。
        """
        date = entries_data.get("date", "") or ""
        vendor = entries_data.get("vendor", "") or ""
        memo_override = ((entries_data.get("memo") or "")
                         if entries_data.get("_unrecognized") else "")
        has_partial = bool(date or vendor)

        now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M")

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
        row[TAG_COL_INDEX] = UNRECOGNIZED_TAG  # 常に赤系（人手確認後に削除）

        return UnrecognizedBlock(row, has_partial, bool(date), bool(vendor),
                                 txn_no + 1)

    def build_page_write(self, employee_name, doc_type, results, source_urls,
                         start_txn_no):
        """1 頁分（複数票＋占位行）の書込計画を組み立てる（純データ、書込副作用ゼロ）。

        頁内の各 result を _build_result_rows で構築、rows を連結、各 result の頁内
        offset で高亮プランを組む。有効行ゼロの result は _build_unrecognized_block で
        占位行 1 行として取込む（頁級では独立書込路径を使わない）。取引No は票ごとに +1。
        ws には一切触れない（tab 名の算出のみ）——書込は commit_page が行う。
        """
        tab_name = _tab_name(employee_name, doc_type)

        txn = start_txn_no
        all_rows = []
        ops = []
        for entries_data, url in zip(results, source_urls):
            offset = len(all_rows)
            block = self._build_result_rows(
                employee_name, doc_type, entries_data, url, txn)
            if block is None:
                ublock = self._build_unrecognized_block(entries_data, url, txn)
                all_rows.append(ublock.row)
                ops.extend(_unrecognized_block_ops(ublock, offset))
                txn = ublock.next_txn_no
            else:
                all_rows.extend(block.rows)
                ops.extend(_result_block_ops(block, offset))
                txn = block.next_txn_no

        return PageWrite(tab_name, all_rows, tuple(ops),
                         (start_txn_no, txn - 1))

    def commit_page(self, page_write):
        """PageWrite を一度の append_rows で原子書込し、高亮を適用して行範囲を返す。

        ① _ensure_row_capacity → ② 一度 append_rows（頁原子・硬去重の rows 不重複を保証）
        → ③ 取引No メモリ更新（append landed 時点、#7）→ ④ 白リセット→高亮プラン適用
        （頁相対 offset を絶対行へ translate、既存塗り順 白→黄→per-entry→赤 を踏襲）
        → ⑤ 空尾行清掃。高亮は best-effort（op ごと独立 try/except、#8）。
        戻り値＝(start_row, end_row)（ledger の CONFIRMED sheet_row_range へ）。
        """
        ws = self._get_or_create_tab(page_write.tab_name)
        rows = page_write.rows

        existing_data = ws.get_all_values()
        pre_write_count = len(existing_data)
        self._ensure_row_capacity(ws, pre_write_count + len(rows))
        self._write_with_retry(ws, rows)

        # 取引No は append landed 時点で前進（CONFIRMED 成否に依らない、#7）
        start_txn, last_txn = page_write.txn_range
        if last_txn >= start_txn:
            self._tab_next_txn[page_write.tab_name] = last_txn + 1

        start_row = pre_write_count + 1
        end_row = start_row + len(rows) - 1

        # 新規行を白にリセットしてから高亮を被せる（append の書式継承対策）
        try:
            fmt_white = CellFormat(backgroundColor=Color(1, 1, 1))
            format_cell_range(ws, f"A{start_row}:AB{end_row}", fmt_white)
        except Exception as e:
            print(f"⚠️ 新規行の背景リセット失敗: {e}")

        for op in page_write.highlight_ops:
            ref = _op_cell_ref(op, start_row)
            try:
                self._format_with_retry(
                    ws, ref, CellFormat(backgroundColor=_severity_color(op.severity)))
            except Exception as e:
                print(f"⚠️ ハイライト適用失敗 ({ref}): {e}")

        self._sanitize_trailing_once(ws, page_write.tab_name, end_row)
        print(f"💾 Sheets に {len(rows)} 行追加（頁原子）: {page_write.tab_name}")
        return (start_row, end_row)

    def next_txn_no(self, employee_name, doc_type):
        """当該タブの次取引No を返す（headless の build_page_write へ渡す start_txn_no）。"""
        tab_name = _tab_name(employee_name, doc_type)
        ws = self._get_or_create_tab(tab_name)
        return self._get_next_txn_no(tab_name, ws)

    def peek_append_range(self, page_write):
        """append 前に着地予定の行範囲 (start, end) を先読みする（PENDING witness 位置）。

        append-only 前提で「現在の最終行 +1 から num_rows 分」が着地位置。commit_page の
        pre_write_count 算出と同一口径（len(get_all_values())+1）。単進程逐次のため
        peek→commit 間に他の書込は無い。
        """
        ws = self._get_or_create_tab(page_write.tab_name)
        pre = len(ws.get_all_values())
        return (pre + 1, pre + len(page_write.rows))

    def probe_page(self, sheet_tab, predicted_row_range, row_fingerprint):
        """PENDING witness の厳格照合（定稿 Plan §9 D3'、啓発的探索禁）。

        predicted_row_range の当日 Sheets 実行を読み、multiset 指紋を再計算して照合する：
          - 範囲が空/該当行無し（全セル空）           → ABSENT（append 未landed → 重写）
          - 範囲内容の指紋が stored と全符             → PRESENT（landed → 補 CONFIRMED）
          - タブ無し（清空跨ぎ）/範囲外れ/指紋不一致/読取例外 → ESCALATE（人工核）
        """
        from posting_ledger import ProbeResult
        try:
            ws = self.spreadsheet.worksheet(sheet_tab)
        except gspread.exceptions.WorksheetNotFound:
            return ProbeResult.ESCALATE  # タブ無し（清空跨ぎ）
        except Exception:
            return ProbeResult.ESCALATE
        try:
            all_vals = ws.get_all_values()
        except Exception:
            return ProbeResult.ESCALATE  # 読取例外 → 人工核

        start, end = predicted_row_range
        segment = all_vals[start - 1:end]  # 1-indexed inclusive → slice
        return _classify_probe(segment, predicted_row_range, row_fingerprint)

    def append_entries(self, employee_name, doc_type, entries_data, source_url=""):
        """
        メイン書き込みメソッド。

        Args:
            employee_name: 従業員名
            doc_type: 文書タイプ (DocType value)
            entries_data: OCR結果 dict (date, vendor, invoice_num, memo, entries[])
            source_url: 原票 PDF の webViewLink
        """
        # Phase A（rows 構築＋科目/異常判定）は _build_result_rows へ抽出済み。
        # 本メソッド（Phase B）は coerce_tax_amount のみ要する。
        from receipt_aggregation import coerce_tax_amount

        tab_name = _tab_name(employee_name, doc_type)
        ws = self._get_or_create_tab(tab_name)

        transaction_no = self._get_next_txn_no(tab_name, ws)
        vendor_name = entries_data.get('vendor', '')

        # Phase A（rows 構築＋異常メタ）は共通ビルダへ抽出（UI/headless 共用）。
        block = self._build_result_rows(
            employee_name, doc_type, entries_data, source_url, transaction_no)

        if block is None:
            # 書ける MF 行がゼロ（entries 空 / 全行金額0/None）でも無音 return
            # しない。黙って戻ると main が Success 判定 → 原票アーカイブ →
            # 誰も気づかないデータ欠落になるため、必ず認識不能の占位行
            # （赤タグ）で人手確認へ可視化する（producer 側の _unrecognized
            # 立て忘れも含めた最終防衛）。
            entries = entries_data.get("entries", [])
            if not entries_data.get("_unrecognized"):
                reason = ("仕訳ゼロ（_unrecognized 未設定）" if not entries
                          else "有効金額の仕訳がゼロ")
                print(f"⚠️ {reason} → 認識不能として記録")
            self._write_unrecognized_row(ws, tab_name, entries_data, source_url)
            return

        rows = block.rows
        anomaly_flags_list = block.anomaly_flags
        conf_flags = block.conf_flags
        red_flags = block.red_flags
        transaction_no = block.next_txn_no

        # 書き込み前のデータを取得（ハイライト位置計算+重複検出用）
        existing_data = ws.get_all_values()
        pre_write_count = len(existing_data)

        # 自動拡容バグ対策: append が境界を跨ぐ前に空きバッファを確保し、
        # Google の自動拡容（直前行の色を空尾行へ継承）自体を起こさせない。
        self._ensure_row_capacity(ws, pre_write_count + len(rows))

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

        # 票面合計照合[B']+対象外[規則①] → I列を一度だけ赤塗り（重複塗り防止）。
        # severity=high → 赤（_severity_color の high）。黄(下地)・per-entry の
        # 後に塗り、I列を最終的に赤にする。上の一括 try/except とは独立（429 リトライ）。
        # （6/12 E2E 静默錯 2/28 対策。領収書限定の機能）
        if doc_type == DocType.RECEIPT:
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

        # 自動拡容バグ対策（兜底）: 既存タブに旧来残った汚れた空尾行を白へ一掃。
        # 新規伝染は _ensure_row_capacity が防ぐため、ここはタブごと初回1回で足りる
        # （毎書き込みで全域を塗り直す無駄な API 呼び出しを避ける）。
        self._sanitize_trailing_once(ws, tab_name, end_row)

        print(f"💾 Sheets に {len(rows)} 行追加: {tab_name}")

    def _get_or_create_audit_tab(self):
        """除外ページ監査タブを取得（なければ作成）。

        `_get_or_create_tab` は流用できない。同メソッドは `len(MF_HEADERS)` 列
        固定で MF 凡例4行＋`MF_HEADERS` を書き込むため、7 列の監査 schema と
        合わない（Codex R1 指摘）。

        既存タブのヘッダが想定と違う場合は**上書きせず例外**にする。誰かが
        同名タブを別用途で作っていた場合にそのデータを破壊しないため。
        """
        if AUDIT_TAB_NAME in self._ws_cache:
            return self._ws_cache[AUDIT_TAB_NAME]

        try:
            ws = self.spreadsheet.worksheet(AUDIT_TAB_NAME)
            header = ws.row_values(1)
            if header and header != AUDIT_HEADERS:
                raise ValueError(
                    f"監査タブ '{AUDIT_TAB_NAME}' のヘッダが想定と異なります。"
                    f"上書きを避けるため中止しました: {header}"
                )
            # 行数は初回だけ実測し、以後は追記に合わせて自前で進める。
            # 監査タブは全社員・全ファイル共通の単一タブで単調増加するため、
            # 1行書くたびに get_all_values() で全読みすると読み取り量が
            # 運用期間とともに際限なく膨らむ。
            self._audit_row_count = len(ws.get_all_values())
            if not header:
                # ヘッダ無しの空タブ = 前回の初期化が add_worksheet 成功・ヘッダ
                # 書込失敗で中断した残骸。ここを「schema 不一致」で撥ねると
                # 監査機能が永久に退避経路へ落ち、人手で表を直すまで復旧しない。
                # 空なら誰のデータも壊さないので書き直して自己修復する。
                self._write_audit_header(ws)
        except gspread.exceptions.WorksheetNotFound:
            ws = self.spreadsheet.add_worksheet(
                title=AUDIT_TAB_NAME, rows=1000, cols=len(AUDIT_HEADERS)
            )
            self._audit_row_count = 0
            self._write_audit_header(ws)

        self._ws_cache[AUDIT_TAB_NAME] = ws
        return ws

    def _write_audit_header(self, ws):
        """監査タブのヘッダ行を書く（レート制限リトライ付き）。

        素の append_row だと 429/5xx 一発でヘッダ無しタブが残り、以後の起動が
        全部 schema 不一致で撥ねられて監査機能が死ぬ（Codex 指摘）。
        """
        self._write_with_retry(ws, [AUDIT_HEADERS])
        self._audit_row_count += 1

    def append_audit_row(self, filename, page_num, verdict, reason,
                         ocr_text_len, source_url=""):
        """除外/分岐ページを監査タブへ 1 行追記する。

        MF データ区には一切書かない（Plan §3.2）。取引No も消費しない。
        呼び出し側は §3.7 の失敗語義に従って例外を扱うこと:
          分岐記録   → 警告ログのみ（MF は既に正しく書けており帳簿は正しい）
          真の除外   → 監査タブが唯一の留痕なので MF の赤い認識不能行へ退避
        """
        ws = self._get_or_create_audit_tab()
        row = [
            datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S"),
            filename,
            page_num,
            verdict,
            reason,
            ocr_text_len,
            source_url,
        ]
        self._ensure_row_capacity(ws, self._audit_row_count + 1)
        self._write_with_retry(ws, [row])
        self._audit_row_count += 1

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

    def _ensure_row_capacity(self, worksheet, needed_last_row):
        """【主防衛】append が自動拡容を起こさないよう、空きバッファ行を事前確保する。

        Google Sheets は append がグリッド境界を跨ぐと新規行に直前行の書式を
        継承する（標色された最終行の色が空尾行へ伝染するバグの根因）。add_rows は
        resize 経由で書式を継承しない素の行を足すため、ここで余裕を持たせておけば
        append は既存の空行に収まり自動拡容自体が発生しない。これが新規伝染を止める
        正規の修正。前提: worksheet.row_count が実態と一致すること（単一プロセスで
        逐次書き込みする本システムでは成立。将来 append を行番号指定 update に
        置き換えれば自動拡容は原理的に起きず、本メソッドは廃止できる）。
        失敗しても行データ書き込みは無傷（best-effort）。
        """
        try:
            current = worksheet.row_count
            target = needed_last_row + GRID_ROW_BUFFER
            if target > current:
                worksheet.add_rows(target - current)
        except Exception as e:
            print(f"⚠️ 行容量の事前確保に失敗: {e}")

    def _sanitize_trailing_once(self, worksheet, tab_name, last_data_row):
        """【兜底・冪等】既存タブに旧来残った汚れた空尾行を、タブごと初回1回だけ白へ戻す。

        新規伝染は _ensure_row_capacity（主防衛）が防ぐため、ここは過去の自動拡容で
        既に汚れたタブの初期清掃のみ担う。毎書き込みで全域を塗り直すのは無駄な API
        呼び出しなので、プロセス内でタブごと1回に限定する。
        失敗しても行データは無傷（best-effort）。
        """
        if tab_name in self._tabs_sanitized:
            return
        try:
            grid_last_row = worksheet.row_count
            if grid_last_row > last_data_row:
                self._format_with_retry(
                    worksheet, f"A{last_data_row + 1}:AB{grid_last_row}",
                    CellFormat(backgroundColor=Color(1, 1, 1)))
            self._tabs_sanitized.add(tab_name)
        except Exception as e:
            print(f"⚠️ 空尾行の継承色リセットに失敗: {e}")

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
        """認識不能/部分認識ページの占位行を書き込み、ハイライト適用。

        摘要は標準ラベル（date/vendor の有無で選択）。memo を通すのは
        _unrecognized を明示した producer（main の部分エラー占位行等）だけ —
        兜底経路で Gemini の memo が標準ラベルを乗っ取らないよう遮断する。
        """
        txn_no = self._get_next_txn_no(tab_name, ws)
        block = self._build_unrecognized_block(entries_data, source_url, txn_no)
        row = block.row
        has_partial = block.has_partial

        pre_write = len(ws.get_all_values())
        self._ensure_row_capacity(ws, pre_write + 1)
        self._write_with_retry(ws, [row])
        self._tab_next_txn[tab_name] = block.next_txn_no

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
                if not block.has_date:
                    format_cell_range(ws, f"B{actual_row}", fmt_red)
                if not block.has_vendor:
                    format_cell_range(ws, f"F{actual_row}", fmt_red)
            else:
                format_cell_range(ws, f"A{actual_row}:AB{actual_row}", fmt_red)
        except Exception as e:
            print(f"⚠️ 認識不能ハイライト適用失敗: {e}")

        # 自動拡容バグ対策（兜底）: 既存タブの汚れた空尾行をタブごと初回1回だけ清掃
        self._sanitize_trailing_once(ws, tab_name, actual_row)

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
