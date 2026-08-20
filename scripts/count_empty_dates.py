"""`LocalTest_カード明細` タブの日付欄（B 列）が空の行を数える（読み取り専用）。

真票回帰の判定 2「日付が空の行が 66 → 大幅減」を**目視で数えない**ための計測器。
書き込みは一切しない。

    venv311/bin/python scripts/count_empty_dates.py [タブ名]
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import gspread  # noqa: E402

from sheets_output import MF_HEADERS  # noqa: E402

TAB = sys.argv[1] if len(sys.argv) > 1 else "LocalTest_カード明細"

# **列番号は `sheets_output.MF_HEADERS` から導く。** 手で書いた番号は静かに
# 別の列を数える —— 実際この計測器は初版で「借方金額(円)」のつもりで 6 を
# 書き、`借方税区分`（index 6）を読んでいた。金額は index 8 である
# （2026-08-20 の実施後評審で発覚。`sheets_output.py:650` が
# `amount_col = [r[8] for r in rows]  # I列=借方金額` と正しく書いている）。
# 受入判定の数字を出す道具なので、1 列ずれると判定そのものが嘘になる。
# `sheets_output.py:64` の `TAG_COL_INDEX = MF_HEADERS.index("タグ")` が前例。
COL_DATE = MF_HEADERS.index("取引日")
COL_DEBIT_AMOUNT = MF_HEADERS.index("借方金額(円)")
COL_MEMO = MF_HEADERS.index("摘要")
HEADER_ROWS = 4          # A1-A4 は高亮凡例（`sheets_output` が書く）
HEADER_LABEL = MF_HEADERS[COL_DATE]   # 見出し行はファイルごとに現れうる


def main():
    gc = gspread.service_account(
        filename=os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json"))
    ws = gc.open_by_key(os.environ["OUTPUT_SPREADSHEET_ID"]).worksheet(TAB)
    rows = ws.get_all_values()[HEADER_ROWS:]

    empty_booked, empty_placeholder, filled = 0, 0, 0
    by_year = Counter()
    for r in rows:
        if not any(c.strip() for c in r[:COL_MEMO + 1]):
            continue                      # 完全な空行
        date = (r[COL_DATE] if len(r) > COL_DATE else "").strip()
        if date == HEADER_LABEL:
            continue                      # 見出し行
        if date:
            filled += 1
            by_year[date[:7]] += 1
            continue
        # 空欄の行は「金額のある仕訳」と「金額 0 の占位行」で意味が違う。
        # 前者だけが顧客の帳簿に日付欠けとして残る。
        amount = (r[COL_DEBIT_AMOUNT] if len(r) > COL_DEBIT_AMOUNT else "").strip()
        if amount and amount not in ("0", "0円"):
            empty_booked += 1
        else:
            empty_placeholder += 1

    total = empty_booked + empty_placeholder + filled
    print("タブ: %s / 明細行 %d" % (TAB, total))
    print("日付が空（金額あり）: %d   ← 判定対象" % empty_booked)
    print("日付が空（占位行）  : %d" % empty_placeholder)
    print("日付あり            : %d" % filled)
    for ym, n in sorted(by_year.items()):
        print("   %s : %d" % (ym, n))


if __name__ == "__main__":
    main()
