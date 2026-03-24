# PaddleOCR Integration & Accuracy Benchmark Design

## Summary

Replace Google Cloud Vision API with PaddleOCR (local, free) as the primary OCR engine in Super Scaner's document processing pipeline. Implement three OCR strategies (A/B/C), benchmark each against a human-verified ground truth CSV, and select the strategy with the lowest error rate for production deployment.

## Context

### Current Pipeline
```
Cloud Vision API → OCR text → Gemini Text (structured extraction)
                                    ↓ (failure)
                              Gemini Vision (fallback)
```

### Problem
- Cloud Vision API requires GCP Billing (cost)
- Single OCR engine = single point of failure for accuracy
- No automated accuracy measurement exists

### Goal
- Replace Cloud Vision with PaddleOCR (free, local, no billing)
- Test three integration strategies to find the optimal error rate
- Target: error rate as close to 0% as possible
- Cloud Vision code preserved (commented out) pending client confirmation

## Three OCR Strategies

### Strategy A: Simple Replacement
```
PaddleOCR → OCR text → Gemini Text → structured JSON
                              ↓ (failure)
                        Gemini Vision (fallback)
```
- PaddleOCR produces raw text, Gemini Text extracts structured data
- Fallback to Gemini Vision only when Gemini Text fails (empty/invalid JSON)
- Lowest cost per request

### Strategy B: Confidence Gate
```
PaddleOCR → (text, confidence)
                 ↓
         confidence ≥ threshold?
           ├── yes → Gemini Text → structured JSON
           └── no  → Gemini Vision (direct image analysis)
```
- PaddleOCR returns per-line confidence scores (averaged across all detected lines)
- Average confidence below threshold (default: 0.7, tuned via benchmark sweep 0.5–0.9) triggers Gemini Vision fallback
- Adaptive: low-quality scans automatically route to vision model
- Moderate cost (vision only for low-confidence pages)

### Strategy C: Cross-Validation (Dual Input)
```
PaddleOCR → OCR text ─┐
                       ├→ Gemini (text + image) → structured JSON
Original image ────────┘
```
- Gemini receives both OCR text AND original image
- Modified prompt instructs Gemini to verify OCR text against the image
- Highest information density per request
- Risk: OCR errors may anchor Gemini (bias toward wrong text)
- Highest cost per request (always uses vision tokens)

## Implementation Details

### 1. `ocr_engine.py` Changes

#### New function: `_ocr_with_paddleocr(image_bytes)`
```python
def _ocr_with_paddleocr(image_bytes):
    """PaddleOCR local OCR engine.

    Args:
        image_bytes: Raw file bytes (PDF page or image)

    Returns:
        tuple: (ocr_text: str, avg_confidence: float)
    """
```
- Uses `paddleocr.PaddleOCR(use_angle_cls=True, lang='japan')` for Japanese text
- PDF bytes → image conversion via `pdf2image` (poppler backend), NOT pypdf (pypdf cannot render pages to raster images)
- For image bytes (JPG/PNG): direct PIL.Image.open from BytesIO
- Returns concatenated text and average per-line confidence score (PaddleOCR returns confidence per detected text line, not per character)
- PaddleOCR model loaded once as module-level singleton (avoid repeated initialization; safe given the current single-threaded polling architecture)
- **Error handling:** If PaddleOCR raises an exception or returns empty text, fall back to Gemini Vision (same pattern as the current Cloud Vision failure path)

#### New function: `_call_gemini_cross_validate(ocr_text, file_data, mime_type, prompt)`
```python
def _call_gemini_cross_validate(ocr_text, file_data, mime_type, prompt):
    """Strategy C: Send both OCR text and original image to Gemini.

    The PROMPTS dict is unchanged. This function wraps the existing prompt with
    an additional prefix containing OCR text, then sends both the wrapped prompt
    and the original image to Gemini.
    """
```

#### Modified: `process_pipeline(file_path, doc_type, ocr_strategy=None)`
- New parameter `ocr_strategy`: `"A"`, `"B"`, or `"C"`
- If `None`, reads from `config.OCR_STRATEGY` (default `"B"`)
- Strategy routing logic in the OCR step
- `benchmark_ocr.py` overrides `ocr_strategy` per-run to test all three

#### Strategy configuration: `config.py`
```python
OCR_STRATEGY = os.getenv("OCR_STRATEGY", "B")  # A, B, or C
OCR_CONFIDENCE_THRESHOLD = float(os.getenv("OCR_CONFIDENCE_THRESHOLD", "0.7"))
```
- `main.py` and `local_test.py` use `config.OCR_STRATEGY` (no code change needed, default param)
- Confidence threshold 0.7 is an initial value; benchmark will sweep 0.5–0.9 in 0.05 increments to find optimal

#### Commented out: `_ocr_with_cloud_vision()`
- Function body preserved, all call sites replaced with `_ocr_with_paddleocr()`
- Comment: `# Cloud Vision API — 甲方確認待ち、コード保持`

### 2. New file: `benchmark_ocr.py`

Automated accuracy benchmark script.

#### Input
- Original receipt PDF (to be provided by user)
- Ground truth CSV: `MF_Import_Data インポート用仕訳帳.csv` (305 transactions, 646 rows)

#### Process
1. Split PDF into individual pages using pypdf
2. For each strategy (A, B, C):
   a. Process every page through the strategy's pipeline
   b. Collect all journal entries into a list
   c. Compare entry-by-entry against ground truth

#### Comparison Fields
| Field | Match Type | CSV Column |
|-------|-----------|------------|
| 取引日 | Exact | 取引日 |
| 借方勘定科目 | Exact | 借方勘定科目 |
| 借方税区分 | Exact | 借方税区分 |
| 借方金額 | Numeric (exact) | 借方金額(円) |
| 貸方勘定科目 | Exact | 貸方勘定科目 |
| 取引先 | Fuzzy (≥80% similarity) | Extracted from 摘要 |
| インボイス番号 | Exact | 借方インボイス |

#### Matching Logic
- Group ground truth rows by 取引No (same 取引No = same receipt/document)
- Match generated entries to ground truth groups by: date + vendor (fuzzy) + total amount of group
- **Intra-group matching for multi-line entries:**
  1. Within each 取引No group, match generated items to ground truth rows by `(借方勘定科目, 借方金額)` pair (exact match)
  2. If no exact pair match, attempt match by `借方金額` alone (account name may differ)
  3. Unmatched rows on either side (extra generated items or missing ground truth items) count as errors
  4. If generated entry count != ground truth row count for a group, flag as structural mismatch

#### Output
```
=== Strategy A Results ===
Total transactions: 305
Matched: 290 / 305 (95.1%)
Field accuracy:
  取引日:       98.2%
  借方勘定科目:  93.5%
  借方税区分:    96.1%
  借方金額:      97.8%
  貸方勘定科目:  99.0%
  取引先:        91.2%
  インボイス:    88.5%

Error details:
  取引No 24: 取引日 expected=2025/12/27 got=2025/12/21
  取引No 31: 借方金額 expected=41700 got=47100
  ...

=== Strategy B Results ===
...

=== Strategy C Results ===
...

=== Comparison ===
| Strategy | Overall | Gemini API cost/page |
|----------|---------|---------------------|
| A        | 95.1%   | text tokens only    |
| B        | 97.3%   | text or vision (adaptive) |
| C        | 96.8%   | always vision+text  |
(PaddleOCR cost is $0 — local inference. Cost differences come from Gemini API token usage.)
```

### 3. Dependency Changes (`requirements.txt`)

```
paddleocr>=2.7
paddlepaddle>=2.6       # CPU version, compatible with macOS ARM64 and Linux x86_64
opencv-python-headless  # PaddleOCR dependency; headless variant avoids libGL issues in Docker
pdf2image               # PDF page → raster image conversion (requires poppler system package)
```

Note: PaddleOCR transitively requires Pillow and OpenCV. Pin `opencv-python-headless` explicitly to avoid pulling the full `opencv-python` which requires libGL. pypdf is already a dependency (used for PDF page splitting, not rendering).

### 4. File Change Summary

| File | Change |
|------|--------|
| `ocr_engine.py` | Add `_ocr_with_paddleocr()`, `_call_gemini_cross_validate()`, add `ocr_strategy` param to `process_pipeline`, comment out Cloud Vision calls |
| `config.py` | Add `OCR_STRATEGY` and `OCR_CONFIDENCE_THRESHOLD` env-driven constants |
| `benchmark_ocr.py` | New file — automated A/B/C accuracy benchmark |
| `requirements.txt` | Add paddleocr, paddlepaddle, opencv-python-headless, pdf2image |
| `Dockerfile` | Add `apt-get install poppler-utils` for pdf2image; add `apt-get install libglib2.0-0` for PaddleOCR |
| `local_test.py` | Add optional `--strategy` CLI arg |

### 5. What Is NOT Changing

- Gemini prompts (PROMPTS dict) — unchanged. Strategy C wraps the existing prompt at runtime in `_call_gemini_cross_validate()`, the PROMPTS dict itself is not modified
- Entry builder logic — unchanged
- Sheets output — unchanged
- Anomaly detection — unchanged
- Daily backup — unchanged
- Production deployment — deferred until benchmark results confirm best strategy

## Testing Plan

1. Install PaddleOCR locally, verify basic Japanese OCR works
2. Receive original PDF from user
3. Run `benchmark_ocr.py` with all three strategies
4. Compare error rates across A/B/C
5. Select winning strategy as production default
6. Optionally tune confidence threshold (Strategy B) if B wins

## Risks

- **PaddleOCR Japanese accuracy**: PaddleOCR's Japanese model may be weaker than Cloud Vision for certain fonts/layouts. The benchmark will quantify this. Consider testing `lang='japan'` vs multi-language mode if numeric fields show poor accuracy.
- **PaddleOCR + macOS ARM64**: PaddlePaddle may have compatibility issues on Apple Silicon. Fallback: use Rosetta or x86 Python.
- **Anchoring bias (Strategy C)**: Wrong OCR text may mislead Gemini. The benchmark will reveal if this is a real problem.
- **Memory usage**: PaddleOCR model loads ~300-500MB RAM. EC2 instance needs sufficient memory.
- **Docker image size**: PaddleOCR + PaddlePaddle + OpenCV add ~2-3 GB to the Docker image. Current base `python:3.9-slim` needs additional system packages (`poppler-utils`, `libglib2.0-0`). Consider multi-stage build if image size becomes a concern.
- **pdf2image + poppler**: pdf2image requires poppler to be installed at system level. Must be added to Dockerfile (`apt-get install poppler-utils`). macOS: `brew install poppler`.
