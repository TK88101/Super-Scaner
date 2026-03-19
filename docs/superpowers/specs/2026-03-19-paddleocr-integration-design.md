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
- PaddleOCR returns per-character confidence scores
- Average confidence below threshold (default: 0.7) triggers Gemini Vision fallback
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
- Converts PDF bytes to image via pypdf + PIL for PaddleOCR input
- Returns concatenated text and average confidence score
- PaddleOCR model loaded once as module-level singleton (avoid repeated initialization)

#### New function: `_call_gemini_cross_validate(ocr_text, file_data, mime_type, prompt)`
```python
def _call_gemini_cross_validate(ocr_text, file_data, mime_type, prompt):
    """Strategy C: Send both OCR text and original image to Gemini.

    Modified prompt includes OCR text as reference while Gemini also sees the image.
    """
```

#### Modified: `process_pipeline(file_path, doc_type, ocr_strategy="B")`
- New parameter `ocr_strategy`: `"A"`, `"B"`, or `"C"`
- Strategy routing logic in the OCR step
- Default strategy set to `"B"` (will be updated after benchmark results)

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
- Match generated entries to ground truth groups by: date + vendor (fuzzy) + amount
- Handle multi-line entries (same 取引No with multiple items)

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
| Strategy | Overall | Cost/page |
|----------|---------|-----------|
| A        | 95.1%   | ~$0.001   |
| B        | 97.3%   | ~$0.003   |
| C        | 96.8%   | ~$0.005   |
```

### 3. Dependency Changes (`requirements.txt`)

```
paddleocr>=2.7
paddlepaddle>=2.6  # CPU version, compatible with macOS ARM64 and Linux x86_64
Pillow              # PDF page → image conversion for PaddleOCR
```

Note: PaddleOCR requires Pillow for image handling. pypdf is already a dependency.

### 4. File Change Summary

| File | Change |
|------|--------|
| `ocr_engine.py` | Add `_ocr_with_paddleocr()`, `_call_gemini_cross_validate()`, add `ocr_strategy` param to `process_pipeline`, comment out Cloud Vision calls |
| `benchmark_ocr.py` | New file — automated A/B/C accuracy benchmark |
| `requirements.txt` | Add paddleocr, paddlepaddle, Pillow |
| `local_test.py` | Add optional `--strategy` CLI arg |

### 5. What Is NOT Changing

- Gemini prompts (PROMPTS dict) — unchanged
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

- **PaddleOCR Japanese accuracy**: PaddleOCR's Japanese model may be weaker than Cloud Vision for certain fonts/layouts. The benchmark will quantify this.
- **PaddleOCR + macOS ARM64**: PaddlePaddle may have compatibility issues on Apple Silicon. Fallback: use Rosetta or x86 Python.
- **Anchoring bias (Strategy C)**: Wrong OCR text may mislead Gemini. The benchmark will reveal if this is a real problem.
- **Memory usage**: PaddleOCR model loads ~300-500MB RAM. EC2 instance needs sufficient memory.
