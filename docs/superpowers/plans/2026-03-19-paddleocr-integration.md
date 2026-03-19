# PaddleOCR Integration & Accuracy Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Cloud Vision with PaddleOCR, implement three OCR strategies (A/B/C), and benchmark accuracy against a human-verified ground truth CSV of 305 transactions.

**Architecture:** PaddleOCR as local OCR engine feeds text into Gemini for structured extraction. Three strategies differ in how PaddleOCR output routes to Gemini (text-only, confidence-gated, or cross-validated with image). A benchmark script compares all three against ground truth.

**Tech Stack:** PaddleOCR, PaddlePaddle (CPU), pdf2image + poppler, Gemini API, pypdf, Python 3.9+

**Spec:** `docs/superpowers/specs/2026-03-19-paddleocr-integration-design.md`

**Test PDFs:**
- `/Users/ibridgezhao/Downloads/領収書.pdf` (258 pages)
- `/Users/ibridgezhao/Downloads/領収書①.pdf` (36 pages)

**Ground Truth:** `/Users/ibridgezhao/Desktop/MF_Import_Data　インポート用仕訳帳.csv` (305 transactions, 646 rows)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `ocr_engine.py` | Modify: add `_ocr_with_paddleocr()`, `_call_gemini_cross_validate()`, strategy routing in `process_pipeline()`, comment out Cloud Vision calls |
| `config.py` | Modify: add `OCR_STRATEGY`, `OCR_CONFIDENCE_THRESHOLD` constants |
| `benchmark_ocr.py` | Create: automated A/B/C accuracy benchmark script |
| `requirements.txt` | Modify: add PaddleOCR dependencies |
| `Dockerfile` | Modify: add system packages for PaddleOCR + poppler |
| `local_test.py` | Modify: add `--strategy` CLI arg |

---

## Task 1: Install Dependencies & Verify PaddleOCR Works

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Install poppler system package (macOS)**

Run:
```bash
brew install poppler
```
Expected: poppler installed, `pdftoppm` command available

- [ ] **Step 2: Install Python dependencies**

Run:
```bash
cd "/Users/ibridgezhao/Documents/Super Scaner"
source venv/bin/activate
pip install paddleocr paddlepaddle opencv-python-headless pdf2image
```
Expected: All packages install successfully

- [ ] **Step 3: Verify PaddleOCR Japanese OCR on a test page**

Run:
```bash
source venv/bin/activate
python3 -c "
from paddleocr import PaddleOCR
from pdf2image import convert_from_path
import numpy as np

# Convert first page of test PDF to image
images = convert_from_path('/Users/ibridgezhao/Downloads/領収書①.pdf', first_page=1, last_page=1)
img_array = np.array(images[0])

# Run PaddleOCR
ocr = PaddleOCR(use_angle_cls=True, lang='japan')
result = ocr.ocr(img_array, cls=True)

for line in result[0]:
    text = line[1][0]
    conf = line[1][1]
    print(f'[{conf:.3f}] {text}')
"
```
Expected: Japanese text extracted with confidence scores

- [ ] **Step 4: Update requirements.txt**

Add these lines to the end of `requirements.txt`:
```
paddleocr>=2.7
paddlepaddle>=2.6
opencv-python-headless
pdf2image
```

- [ ] **Step 5: Commit**

```bash
git add requirements.txt
git commit -m "deps: add PaddleOCR, PaddlePaddle, pdf2image for local OCR"
```

---

## Task 2: Add OCR Config Constants

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add OCR strategy and confidence threshold to config.py**

Add after line 10 (after `SPLIT_PDF_FOLDER_ID`):
```python
# === OCR 戦略設定 ===
# A: PaddleOCR → Gemini Text (フォールバック: Gemini Vision)
# B: PaddleOCR + 置信度ゲート (低置信度 → Gemini Vision)
# C: PaddleOCR テキスト + 原画像 → Gemini クロスバリデーション
OCR_STRATEGY = os.getenv("OCR_STRATEGY", "B")
OCR_CONFIDENCE_THRESHOLD = float(os.getenv("OCR_CONFIDENCE_THRESHOLD", "0.7"))
```

- [ ] **Step 2: Verify syntax**

Run:
```bash
source venv/bin/activate
python3 -c "import config; print(f'Strategy={config.OCR_STRATEGY}, Threshold={config.OCR_CONFIDENCE_THRESHOLD}')"
```
Expected: `Strategy=B, Threshold=0.7`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "config: add OCR_STRATEGY and OCR_CONFIDENCE_THRESHOLD settings"
```

---

## Task 3: Implement `_ocr_with_paddleocr()` in ocr_engine.py

**Files:**
- Modify: `ocr_engine.py`

- [ ] **Step 1: Add PaddleOCR imports and singleton at top of ocr_engine.py**

Add after the `load_dotenv()` / `genai.configure()` block (anchor: immediately before the line `GEMINI_GENERATION_CONFIG = {`):

> **Note:** All line numbers in Tasks 3-5 refer to the ORIGINAL file before any edits. After each task inserts code, subsequent line numbers shift. Always locate code by **function name or content anchor**, not line number.
```python
from paddleocr import PaddleOCR
from pdf2image import convert_from_bytes
from PIL import Image
import numpy as np

# PaddleOCR singleton (module-level, loaded once)
_paddle_ocr = None

def _get_paddle_ocr():
    """PaddleOCR インスタンスをシングルトンで取得"""
    global _paddle_ocr
    if _paddle_ocr is None:
        _paddle_ocr = PaddleOCR(use_angle_cls=True, lang='japan', show_log=False)
    return _paddle_ocr
```

- [ ] **Step 2: Implement `_ocr_with_paddleocr()`**

Add immediately after the `_ocr_with_cloud_vision()` function (anchor: after the line `return response.full_text_annotation.text or ""`):
```python
def _ocr_with_paddleocr(image_bytes, mime_type="image/jpeg"):
    """PaddleOCR ローカル OCR エンジン

    Args:
        image_bytes: ファイルのバイトデータ (PDF ページまたは画像)
        mime_type: MIME タイプ (pdf → pdf2image で変換)

    Returns:
        tuple: (ocr_text: str, avg_confidence: float)
    """
    ocr = _get_paddle_ocr()

    # PDF → 画像変換 (pdf2image + poppler)
    if mime_type == "application/pdf":
        images = convert_from_bytes(image_bytes)
        if not images:
            return "", 0.0
        img_array = np.array(images[0])
    else:
        # JPG/PNG → PIL → numpy array
        img = Image.open(io.BytesIO(image_bytes))
        img_array = np.array(img.convert("RGB"))

    result = ocr.ocr(img_array, cls=True)

    if not result or not result[0]:
        return "", 0.0

    lines = []
    confidences = []
    for line in result[0]:
        text = line[1][0]
        conf = line[1][1]
        lines.append(text)
        confidences.append(conf)

    ocr_text = "\n".join(lines)
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    return ocr_text, avg_confidence
```

- [ ] **Step 3: Verify function works standalone**

Run:
```bash
source venv/bin/activate
python3 -c "
from pypdf import PdfReader, PdfWriter
import io

# Extract first page as bytes
reader = PdfReader('/Users/ibridgezhao/Downloads/領収書①.pdf')
writer = PdfWriter()
writer.add_page(reader.pages[0])
buf = io.BytesIO()
writer.write(buf)
page_bytes = buf.getvalue()

from ocr_engine import _ocr_with_paddleocr
text, conf = _ocr_with_paddleocr(page_bytes, 'application/pdf')
print(f'Text length: {len(text)} chars')
print(f'Confidence: {conf:.3f}')
print(f'First 200 chars: {text[:200]}')
"
```
Expected: Non-empty Japanese text with confidence > 0

- [ ] **Step 4: Commit**

```bash
git add ocr_engine.py
git commit -m "feat: add _ocr_with_paddleocr() with PaddleOCR singleton and pdf2image conversion"
```

---

## Task 4: Implement `_call_gemini_cross_validate()` for Strategy C

**Files:**
- Modify: `ocr_engine.py`

- [ ] **Step 1: Implement cross-validation function**

Add immediately after the `_call_gemini_bytes()` function (anchor: after its `return parsed` line, before `def _call_gemini(file_path, prompt):`):
```python
def _call_gemini_cross_validate(ocr_text, file_data, mime_type, prompt):
    """Strategy C: OCR テキストと原画像の両方を Gemini に送信してクロスバリデーション

    PROMPTS dict は変更しない。既存の prompt に OCR テキストをプレフィックスとして付加し、
    原画像と一緒に Gemini に送信する。
    """
    cross_prompt = (
        f"{prompt}\n\n"
        f"--- 参考: OCR認識テキスト (誤認識の可能性あり、画像と照合して修正してください) ---\n"
        f"{ocr_text}\n"
        f"--- OCRテキスト終了 ---\n\n"
        f"上記のOCRテキストは参考情報です。画像の内容を直接確認し、"
        f"OCRテキストに誤りがあれば画像を優先してください。"
    )
    response = model.generate_content(
        [
            {"mime_type": mime_type, "data": file_data},
            cross_prompt,
        ],
        generation_config=GEMINI_GENERATION_CONFIG,
    )
    text = (getattr(response, "text", "") or "").strip()
    parsed = extract_json(text)
    if parsed is None:
        finish_reason = _get_finish_reason(response)
        print(
            f"⚠️ Gemini応答のJSON解析失敗 "
            f"(finish_reason={finish_reason or 'unknown'}, len={len(text)})"
        )
    return parsed
```

- [ ] **Step 2: Verify syntax**

Run:
```bash
source venv/bin/activate
python3 -c "import ast; ast.parse(open('ocr_engine.py').read()); print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add ocr_engine.py
git commit -m "feat: add _call_gemini_cross_validate() for Strategy C dual-input"
```

---

## Task 5: Add Strategy Routing to `process_pipeline()`

**Files:**
- Modify: `ocr_engine.py`

- [ ] **Step 1: Extract strategy routing helper to reduce duplication**

Add a new helper function immediately before `process_pipeline()` (anchor: before `def process_pipeline`):

```python
def _route_ocr_strategy(data_bytes, mime_type, prompt, ocr_strategy, prefix=""):
    """OCR 戦略に基づいてデータをルーティングし、構造化 JSON を返す。

    Args:
        data_bytes: ファイルバイトデータ
        mime_type: MIME タイプ
        prompt: Gemini プロンプト
        ocr_strategy: "A", "B", or "C"
        prefix: ログ出力のプレフィックス

    Returns:
        dict or None: Gemini 解析結果
    """
    import config
    raw_data = None
    try:
        ocr_text, ocr_conf = _ocr_with_paddleocr(data_bytes, mime_type)
        if ocr_text.strip():
            print(f"{prefix}📝 PaddleOCR完了 ({len(ocr_text)}文字, 置信度: {ocr_conf:.3f})")

            if ocr_strategy == "A":
                raw_data = _call_gemini_text(ocr_text, prompt)
            elif ocr_strategy == "B":
                if ocr_conf >= config.OCR_CONFIDENCE_THRESHOLD:
                    raw_data = _call_gemini_text(ocr_text, prompt)
                else:
                    print(f"{prefix}⚠️ 置信度低 ({ocr_conf:.3f} < {config.OCR_CONFIDENCE_THRESHOLD}) → Gemini Vision")
                    raw_data = _call_gemini_bytes(data_bytes, mime_type, prompt)
            elif ocr_strategy == "C":
                raw_data = _call_gemini_cross_validate(ocr_text, data_bytes, mime_type, prompt)
    except Exception as ocr_err:
        print(f"{prefix}⚠️ PaddleOCR失敗: {ocr_err}")

    return raw_data
```

- [ ] **Step 2: Modify `process_pipeline()` signature and add strategy resolution**

Change the function signature (anchor: `def process_pipeline(file_path, doc_type=DocType.RECEIPT):`):
```python
def process_pipeline(file_path, doc_type=DocType.RECEIPT, ocr_strategy=None):
```

Add after the `prompt = PROMPTS.get(doc_type)` null check block (anchor: after `return None` inside the `if not prompt:` block):
```python
        import config
        if ocr_strategy is None:
            ocr_strategy = config.OCR_STRATEGY
```

- [ ] **Step 3: Replace the per-page OCR block with strategy routing**

Replace the Cloud Vision OCR section inside the page loop (anchor: the block starting with `# Cloud Vision OCR → Gemini Text` and ending with `page_raw_data = _call_gemini_bytes(page_data, "application/pdf", prompt)`) with:
```python
```python
                    # OCR 戦略ルーティング (ヘルパー関数使用)
                    page_raw_data = _route_ocr_strategy(
                        page_data, "application/pdf", prompt, ocr_strategy, prefix=prefix
                    )

                    if not page_raw_data:
                        print(f"{prefix}🔄 フォールバック: Gemini Vision で再試行")
                        page_raw_data = _call_gemini_bytes(page_data, "application/pdf", prompt)
```

- [ ] **Step 4: Replace the single-file OCR block with strategy routing**

Replace the single-file Cloud Vision block (anchor: the block starting with `# 単一ファイル: Cloud Vision OCR` and ending with `raw_data = _call_gemini(file_path, prompt)`) with:
```python
        # 単一ファイル: PaddleOCR + 戦略ルーティング
        raw_data = None
        with open(file_path, "rb") as f:
            file_data = f.read()

        raw_data = _route_ocr_strategy(file_data, mime_type, prompt, ocr_strategy)

        if not raw_data:
            print("🔄 フォールバック: Gemini Vision で再試行")
            raw_data = _call_gemini(file_path, prompt)
```

- [ ] **Step 5: Comment out `_ocr_with_cloud_vision`**

Comment out the entire `_ocr_with_cloud_vision` function body (anchor: `def _ocr_with_cloud_vision(image_bytes):`). Add a marker comment above:
```python
# === Cloud Vision API — 甲方確認待ち、コード保持 ===
```
Comment out every line of the function with `#`. Do NOT delete the function.

- [ ] **Step 6: Update log message**

Change the log line (anchor: the `print` containing `Cloud Vision + Gemini で`):
```python
    print(f"🧠 PaddleOCR + Gemini で{type_label}を分析中: {filename} (戦略: {ocr_strategy}) ...")
```

- [ ] **Step 8: Verify syntax**

Run:
```bash
source venv/bin/activate
python3 -c "import ast; ast.parse(open('ocr_engine.py').read()); print('OK')"
```
Expected: `OK`

- [ ] **Step 9: Functional smoke test — process one page with Strategy A**

Run:
```bash
source venv/bin/activate
python3 -c "
from pypdf import PdfReader, PdfWriter
import io, tempfile

reader = PdfReader('/Users/ibridgezhao/Downloads/領収書①.pdf')
writer = PdfWriter()
writer.add_page(reader.pages[0])
tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
writer.write(tmp)
tmp.close()

from ocr_engine import process_pipeline
result = process_pipeline(tmp.name, ocr_strategy='A')
print(f'Result type: {type(result)}')
if result:
    if isinstance(result, list):
        print(f'Entries: {len(result)}')
        for r in result[:2]:
            print(f'  date={r.get(\"date\")} vendor={r.get(\"vendor\")} entries={len(r.get(\"entries\", []))}')
    else:
        print(f'date={result.get(\"date\")} vendor={result.get(\"vendor\")} entries={len(result.get(\"entries\", []))}')
else:
    print('WARNING: result is None')
"
```
Expected: Non-None result with date, vendor, and at least one entry

- [ ] **Step 10: Commit**

```bash
git add ocr_engine.py
git commit -m "feat: add strategy routing (A/B/C) to process_pipeline, comment out Cloud Vision"
```

---

## Task 6: Create `benchmark_ocr.py`

**Files:**
- Create: `benchmark_ocr.py`

- [ ] **Step 1: Create the benchmark script**

Create `benchmark_ocr.py` with the following content:

```python
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

def load_ground_truth(csv_path):
    """Ground truth CSV を読み込み、取引No でグループ化して返す。

    Returns:
        dict[str, list[dict]]: {取引No: [row, row, ...]}
        各 row は: date, debit_account, debit_tax_type, debit_amount,
                   credit_account, vendor, invoice_num
    """
    groups = defaultdict(list)
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            txn_no = row["取引No"].strip()
            # 摘要から取引先を抽出: "取引先名 - 品目 ]" → "取引先名"
            memo = row.get("摘要", "")
            vendor = memo.split(" - ")[0].strip() if " - " in memo else memo.strip()

            groups[txn_no].append({
                "date": row["取引日"].strip(),
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
            result = process_pipeline(tmp_path, doc_type=DocType.RECEIPT, ocr_strategy=strategy)

            if result is None:
                print(f"  ⚠️ Page {page_idx+1}: 解析失敗")
                continue

            if isinstance(result, list):
                all_results.extend(result)
            else:
                all_results.append(result)

            # API レート制限対策
            time.sleep(0.5)

    return all_results


# ============================================================
# 結果比較ロジック
# ============================================================

def fuzzy_match(a, b, threshold=0.8):
    """文字列の類似度が threshold 以上なら True"""
    if not a or not b:
        return a == b
    return SequenceMatcher(None, a, b).ratio() >= threshold


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
        date = r.get("date", "")
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
```

- [ ] **Step 2: Verify syntax**

Run:
```bash
source venv/bin/activate
python3 -c "import ast; ast.parse(open('benchmark_ocr.py').read()); print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add benchmark_ocr.py
git commit -m "feat: add benchmark_ocr.py for A/B/C strategy accuracy comparison"
```

---

## Task 7: Update Dockerfile

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Add system packages for PaddleOCR and poppler**

After `WORKDIR /app` line, add:
```dockerfile
# PaddleOCR + pdf2image system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libglib2.0-0 \
    libgl1-mesa-glx \
    && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2: Verify Dockerfile syntax**

Run:
```bash
docker build --no-cache -t super-scaner:test-paddleocr . 2>&1 | tail -5
```
Expected: `Successfully built` or `Successfully tagged` (full build may take time due to PaddlePaddle download; if it takes too long, at minimum verify the RUN apt-get line parses correctly by checking docker build starts without syntax errors)

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "docker: add poppler-utils and libglib for PaddleOCR support"
```

---

## Task 8: Add `--strategy` CLI Arg to `local_test.py`

**Files:**
- Modify: `local_test.py`

- [ ] **Step 1: Add argparse import**

Add after the existing imports (anchor: after the line `import config`):
```python
import argparse
```

- [ ] **Step 2: Add strategy parameter to `process_local_file()`**

Change the function signature (anchor: `def process_local_file(file_info, sheets_writer):`):
```python
def process_local_file(file_info, sheets_writer, strategy=None):
```

Change the `process_pipeline` call inside `process_local_file` (anchor: `result = process_pipeline(file_path, doc_type=doc_type)`):
```python
    result = process_pipeline(file_path, doc_type=doc_type, ocr_strategy=strategy)
```

- [ ] **Step 3: Add argparse to `main()` and pass strategy**

Add at the beginning of `main()` (anchor: after `print("🚀 Super Scaner ローカルテストモード起動 (Sheets出力版)")`):
```python
    parser = argparse.ArgumentParser(description="Super Scaner Local Test")
    parser.add_argument("--strategy", choices=["A", "B", "C"], default=None,
                        help="OCR strategy override (default: config.OCR_STRATEGY)")
    args = parser.parse_args()
```

Change the `process_local_file` call in the main loop (anchor: `if process_local_file(file_info, sheets_writer):`):
```python
            if process_local_file(file_info, sheets_writer, strategy=args.strategy):
```

- [ ] **Step 2: Verify syntax**

Run:
```bash
source venv/bin/activate
python3 -c "import ast; ast.parse(open('local_test.py').read()); print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add local_test.py
git commit -m "feat: add --strategy CLI arg to local_test.py for OCR strategy selection"
```

---

## Task 9: Run Benchmark & Collect Results

- [ ] **Step 1: Run Strategy A benchmark**

Run:
```bash
cd "/Users/ibridgezhao/Documents/Super Scaner"
source venv/bin/activate
python3 benchmark_ocr.py \
  --pdf "/Users/ibridgezhao/Downloads/領収書.pdf" "/Users/ibridgezhao/Downloads/領収書①.pdf" \
  --truth "/Users/ibridgezhao/Desktop/MF_Import_Data　インポート用仕訳帳.csv" \
  --strategies A 2>&1 | tee benchmark_results_A.txt
```
Expected: Report with field-level accuracy for Strategy A

- [ ] **Step 2: Run Strategy B benchmark**

Run:
```bash
python3 benchmark_ocr.py \
  --pdf "/Users/ibridgezhao/Downloads/領収書.pdf" "/Users/ibridgezhao/Downloads/領収書①.pdf" \
  --truth "/Users/ibridgezhao/Desktop/MF_Import_Data　インポート用仕訳帳.csv" \
  --strategies B 2>&1 | tee benchmark_results_B.txt
```
Expected: Report with field-level accuracy for Strategy B

- [ ] **Step 3: Run Strategy C benchmark**

Run:
```bash
python3 benchmark_ocr.py \
  --pdf "/Users/ibridgezhao/Downloads/領収書.pdf" "/Users/ibridgezhao/Downloads/領収書①.pdf" \
  --truth "/Users/ibridgezhao/Desktop/MF_Import_Data　インポート用仕訳帳.csv" \
  --strategies C 2>&1 | tee benchmark_results_C.txt
```
Expected: Report with field-level accuracy for Strategy C

- [ ] **Step 4: Review comparison and select winning strategy**

Compare the three results. Update `config.py` default `OCR_STRATEGY` to the winning strategy value.

- [ ] **Step 5: Commit benchmark results and final config**

```bash
git add benchmark_results_*.txt config.py
git commit -m "benchmark: A/B/C strategy accuracy results, set default to winning strategy"
```
