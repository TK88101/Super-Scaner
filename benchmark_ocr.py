#!/usr/bin/env python3
"""
PaddleOCR 精度ベンチマーク — 戦略 A / B / C 比較

使い方:
  python3 benchmark_ocr.py \
    --pdf /path/to/領収書.pdf /path/to/領収書①.pdf \
    --truth "/path/to/MF_Import_Data　インポート用仕訳帳.csv" \
    --strategies A B C
"""

import argparse
import csv
import io
import os
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher

from dotenv import load_dotenv
load_dotenv()

from pypdf import PdfReader, PdfWriter
from ocr_engine import process_pipeline
from doc_types import DocType


# ============================================================
# Ground Truth 読み込み
# ============================================================

def normalize_date(date_str):
    """日付文字列を正規化: '2025/2/1' と '2025/02/01' を同一視"""
    if not date_str:
        return ""
    parts = date_str.strip().split("/")
    if len(parts) == 3:
        try:
            return f"{int(parts[0])}/{int(parts[1])}/{int(parts[2])}"
        except ValueError:
            pass
    return date_str.strip()


def load_ground_truth(csv_path):
    """Ground truth CSV を読み込み、取引No でグループ化して返す。

    Returns:
        dict[str, list[dict]]: {取引No: [row, row, ...]}
        各 row は: date, debit_account, debit_tax_type, debit_amount,
                   credit_account, vendor, invoice_num
    """
    groups = defaultdict(list)
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            txn_no = row["取引No"].strip()
            # 摘要から取引先を抽出: "取引先名 - 品目 ]" → "取引先名"
            memo = row.get("摘要", "")
            vendor = memo.split(" - ")[0].strip() if " - " in memo else memo.strip()

            groups[txn_no].append({
                "date": normalize_date(row["取引日"]),
                "debit_account": row["借方勘定科目"].strip(),
                "debit_tax_type": row["借方税区分"].strip(),
                "debit_amount": int(row["借方金額(円)"].strip()),
                "credit_account": row["貸方勘定科目"].strip(),
                "vendor": vendor,
                "invoice_num": row.get("借方インボイス", "").strip(),
            })
    return groups


# ============================================================
# PDF 分割 → 戦略別処理
# ============================================================

def process_pdfs_with_strategy(pdf_paths, strategy):
    """複数 PDF を指定戦略で処理し、全仕訳エントリを返す。

    Returns:
        list[dict]: 各要素は process_pipeline の戻り値 (単一 or 複数文書)
    """
    all_results = []
    for pdf_path in pdf_paths:
        print(f"\n{'='*60}")
        print(f"📄 PDF: {os.path.basename(pdf_path)} (Strategy {strategy})")
        print(f"{'='*60}")

        reader = PdfReader(pdf_path)
        for page_idx in range(len(reader.pages)):
            # 1ページずつ PDF バイトに分割
            writer = PdfWriter()
            writer.add_page(reader.pages[page_idx])
            buf = io.BytesIO()
            writer.write(buf)
            page_bytes = buf.getvalue()

            # 一時ファイルに書き出し (process_pipeline はファイルパスを要求)
            tmp_path = f"/tmp/benchmark_page_{page_idx+1}.pdf"
            with open(tmp_path, "wb") as tmp_f:
                tmp_f.write(page_bytes)

            print(f"\n--- Page {page_idx+1}/{len(reader.pages)} ---")
            page_yielded = False
            for page in process_pipeline(tmp_path, doc_type=DocType.RECEIPT, ocr_strategy=strategy):
                all_results.append(page["result"])
                page_yielded = True

            if not page_yielded:
                print(f"  ⚠️ Page {page_idx+1}: 解析失敗")

            # API レート制限対策
            time.sleep(0.5)

    return all_results


# ============================================================
# 結果比較ロジック
# ============================================================

def fuzzy_match(a, b, threshold=0.6):
    """文字列の類似度が threshold 以上、または部分文字列一致なら True"""
    if not a or not b:
        return a == b
    a_str, b_str = str(a).strip(), str(b).strip()
    # 完全一致
    if a_str == b_str:
        return True
    # 類似度チェック
    if SequenceMatcher(None, a_str, b_str).ratio() >= threshold:
        return True
    # 部分文字列: 短い方が長い方に含まれる
    short, long = (a_str, b_str) if len(a_str) <= len(b_str) else (b_str, a_str)
    if len(short) >= 3 and short in long:
        return True
    # 株式会社/有限会社等を除去して再比較
    for prefix in ["株式会社", "有限会社", "(株)", "㈱"]:
        a_clean = a_str.replace(prefix, "").strip()
        b_clean = b_str.replace(prefix, "").strip()
    if a_clean and b_clean and SequenceMatcher(None, a_clean, b_clean).ratio() >= threshold:
        return True
    return False


def match_results_to_ground_truth(results, gt_groups):
    """生成結果を ground truth にマッチングし、フィールド別精度を計算。

    Matching strategy:
    1. 各 result の (date, vendor) で GT グループを検索
    2. グループ内で (debit_account, amount) ペアでマッチ
    3. ペアマッチ失敗時は amount のみでマッチ

    Returns:
        dict: {
            "matched_groups": int,
            "total_groups": int,
            "field_correct": {field: count},
            "field_total": {field: count},
            "errors": [error_detail, ...]
        }
    """
    fields = ["date", "debit_account", "debit_tax_type", "debit_amount",
              "credit_account", "vendor", "invoice_num"]
    field_correct = {f: 0 for f in fields}
    field_total = {f: 0 for f in fields}
    errors = []
    matched_groups = 0

    # GT グループを (date, vendor) → [txn_no, ...] でインデックス化
    # 同一 (date, vendor, amount) の重複に対応するため list で保持
    gt_index = defaultdict(list)
    for txn_no, rows in gt_groups.items():
        date = rows[0]["date"]
        vendor = rows[0]["vendor"]
        total_amount = sum(r["debit_amount"] for r in rows)
        gt_index[(date, vendor, total_amount)].append(txn_no)

    # 生成結果をグループ化 (date + vendor → entries)
    result_groups = defaultdict(list)
    for r in results:
        date = normalize_date(r.get("date", ""))
        vendor = r.get("vendor", "")
        for entry in r.get("entries", []):
            result_groups[(date, vendor)].append({
                "date": date,
                "vendor": vendor,
                "invoice_num": r.get("invoice_num", ""),
                "debit_account": entry.get("debit_account", ""),
                "debit_tax_type": entry.get("debit_tax_type", ""),
                "debit_amount": entry.get("amount", 0),
                "credit_account": entry.get("credit_account", ""),
            })

    # マッチング
    matched_gt_txns = set()
    for (r_date, r_vendor), r_entries in result_groups.items():
        # GT グループを検索: exact date + fuzzy vendor + total amount
        r_total = sum(e["debit_amount"] for e in r_entries)
        best_txn = None

        for (gt_date, gt_vendor, gt_total), txn_nos in gt_index.items():
            for txn_no in txn_nos:
                if txn_no in matched_gt_txns:
                    continue
                if gt_date == r_date and fuzzy_match(gt_vendor, r_vendor) and abs(gt_total - r_total) <= max(gt_total * 0.05, 10):
                    best_txn = txn_no
                    break
            if best_txn:
                break

        if not best_txn:
            # date + fuzzy vendor のみで再試行
            for (gt_date, gt_vendor, gt_total), txn_nos in gt_index.items():
                for txn_no in txn_nos:
                    if txn_no in matched_gt_txns:
                        continue
                    if gt_date == r_date and fuzzy_match(gt_vendor, r_vendor):
                        best_txn = txn_no
                        break
                if best_txn:
                    break

        if not best_txn:
            # date + total amount のみで再試行 (vendor 無視)
            for (gt_date, gt_vendor, gt_total), txn_nos in gt_index.items():
                for txn_no in txn_nos:
                    if txn_no in matched_gt_txns:
                        continue
                    if gt_date == r_date and abs(gt_total - r_total) <= max(gt_total * 0.02, 5):
                        best_txn = txn_no
                        break
                if best_txn:
                    break

        if not best_txn:
            errors.append(f"UNMATCHED result: date={r_date} vendor={r_vendor} amount={r_total}")
            continue

        matched_gt_txns.add(best_txn)
        matched_groups += 1
        gt_rows = gt_groups[best_txn]

        # 構造ミスマッチ検出
        if len(r_entries) != len(gt_rows):
            errors.append(f"取引No {best_txn}: STRUCTURAL MISMATCH entries={len(r_entries)} vs gt={len(gt_rows)}")

        # グループ内エントリマッチング: (debit_account, amount) ペア
        unmatched_gt = list(gt_rows)
        for r_entry in r_entries:
            matched_gt_row = None

            # Step 1: (account, amount) exact pair
            for i, gt_row in enumerate(unmatched_gt):
                if (gt_row["debit_account"] == r_entry["debit_account"] and
                        gt_row["debit_amount"] == r_entry["debit_amount"]):
                    matched_gt_row = unmatched_gt.pop(i)
                    break

            # Step 2: amount only
            if not matched_gt_row:
                for i, gt_row in enumerate(unmatched_gt):
                    if gt_row["debit_amount"] == r_entry["debit_amount"]:
                        matched_gt_row = unmatched_gt.pop(i)
                        break

            if not matched_gt_row:
                errors.append(f"取引No {best_txn}: extra entry amount={r_entry['debit_amount']}")
                continue

            # フィールド別比較
            for field in fields:
                field_total[field] += 1
                r_val = r_entry.get(field, "")
                gt_val = matched_gt_row.get(field, "")

                if field == "vendor":
                    if fuzzy_match(str(r_val), str(gt_val)):
                        field_correct[field] += 1
                    else:
                        errors.append(f"取引No {best_txn}: {field} expected={gt_val} got={r_val}")
                elif field == "debit_amount":
                    if int(r_val) == int(gt_val):
                        field_correct[field] += 1
                    else:
                        errors.append(f"取引No {best_txn}: {field} expected={gt_val} got={r_val}")
                elif field == "invoice_num":
                    # ハイフン除去して比較 (GT: T9-2900-... vs Result: T9290001...)
                    r_inv = str(r_val).replace("-", "").strip()
                    gt_inv = str(gt_val).replace("-", "").strip()
                    if r_inv == gt_inv:
                        field_correct[field] += 1
                    else:
                        errors.append(f"取引No {best_txn}: {field} expected={gt_val} got={r_val}")
                elif field == "debit_account":
                    # ACCOUNT_MAP を適用して比較
                    from config import ACCOUNT_MAP
                    r_mapped = ACCOUNT_MAP.get(str(r_val).strip(), str(r_val).strip())
                    gt_clean = str(gt_val).strip()
                    if r_mapped == gt_clean or str(r_val).strip() == gt_clean:
                        field_correct[field] += 1
                    else:
                        errors.append(f"取引No {best_txn}: {field} expected={gt_val} got={r_val}")
                else:
                    if str(r_val).strip() == str(gt_val).strip():
                        field_correct[field] += 1
                    else:
                        errors.append(f"取引No {best_txn}: {field} expected={gt_val} got={r_val}")

        # 未マッチの GT 行はエラー
        for gt_row in unmatched_gt:
            errors.append(f"取引No {best_txn}: missing entry amount={gt_row['debit_amount']}")

    return {
        "matched_groups": matched_groups,
        "total_groups": len(gt_groups),
        "field_correct": field_correct,
        "field_total": field_total,
        "errors": errors,
    }


# ============================================================
# レポート出力
# ============================================================

def print_report(strategy, comparison):
    """1 戦略の結果レポートを出力"""
    print(f"\n{'='*60}")
    print(f"=== Strategy {strategy} Results ===")
    print(f"{'='*60}")
    print(f"Total groups (取引No): {comparison['total_groups']}")
    print(f"Matched: {comparison['matched_groups']} / {comparison['total_groups']} "
          f"({comparison['matched_groups']/max(comparison['total_groups'],1)*100:.1f}%)")

    print(f"\nField accuracy:")
    for field in ["date", "debit_account", "debit_tax_type", "debit_amount",
                   "credit_account", "vendor", "invoice_num"]:
        total = comparison["field_total"][field]
        correct = comparison["field_correct"][field]
        pct = correct / max(total, 1) * 100
        print(f"  {field:20s}: {correct:4d}/{total:4d} ({pct:5.1f}%)")

    if comparison["errors"]:
        print(f"\nError details ({len(comparison['errors'])} errors):")
        for err in comparison["errors"][:50]:  # 最大50件表示
            print(f"  {err}")
        if len(comparison["errors"]) > 50:
            print(f"  ... and {len(comparison['errors'])-50} more")


def print_comparison(all_comparisons):
    """全戦略の比較表を出力"""
    print(f"\n{'='*60}")
    print(f"=== Strategy Comparison ===")
    print(f"{'='*60}")
    print(f"{'Strategy':10s} {'Matched':10s} {'Overall':10s} {'Errors':10s}")
    print("-" * 40)
    for strategy, comp in sorted(all_comparisons.items()):
        matched = comp["matched_groups"]
        total = comp["total_groups"]
        pct = matched / max(total, 1) * 100
        err_count = len(comp["errors"])
        print(f"{strategy:10s} {matched:4d}/{total:4d}   {pct:5.1f}%      {err_count:4d}")


# ============================================================
# メイン
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="PaddleOCR Benchmark")
    parser.add_argument("--pdf", nargs="+", required=True, help="Input PDF file paths")
    parser.add_argument("--truth", required=True, help="Ground truth CSV path")
    parser.add_argument("--strategies", nargs="+", default=["A", "B", "C"],
                        help="Strategies to benchmark (default: A B C)")
    args = parser.parse_args()

    # Validate files
    for pdf_path in args.pdf:
        if not os.path.exists(pdf_path):
            print(f"❌ PDF not found: {pdf_path}")
            sys.exit(1)
    if not os.path.exists(args.truth):
        print(f"❌ Ground truth CSV not found: {args.truth}")
        sys.exit(1)

    # Load ground truth
    print("📊 Loading ground truth...")
    gt_groups = load_ground_truth(args.truth)
    print(f"   {len(gt_groups)} transaction groups loaded")

    # Run benchmarks
    all_comparisons = {}
    for strategy in args.strategies:
        strategy = strategy.upper()
        print(f"\n{'#'*60}")
        print(f"# BENCHMARK: Strategy {strategy}")
        print(f"{'#'*60}")

        start_time = time.time()
        results = process_pdfs_with_strategy(args.pdf, strategy)
        elapsed = time.time() - start_time

        print(f"\n⏱️ Strategy {strategy}: {len(results)} results in {elapsed:.1f}s")

        comparison = match_results_to_ground_truth(results, gt_groups)
        all_comparisons[strategy] = comparison
        print_report(strategy, comparison)

    # Final comparison
    if len(all_comparisons) > 1:
        print_comparison(all_comparisons)


if __name__ == "__main__":
    main()
