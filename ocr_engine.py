import os
import io
import json
import re
import gc
import google.generativeai as genai
from google.cloud import vision
from dotenv import load_dotenv
from doc_types import DocType, DOC_TYPE_CONFIG

try:
    from pypdf import PdfReader, PdfWriter
except Exception:
    PdfReader = None
    PdfWriter = None

from paddleocr import PaddleOCR
from pdf2image import convert_from_bytes
from PIL import Image
import numpy as np

# PaddleOCR singleton
_paddle_ocr = None

def _get_paddle_ocr():
    global _paddle_ocr
    if _paddle_ocr is None:
        # PaddleOCR 3.x は use_gpu 非対応 → try/except で v2/v3 両対応
        try:
            _paddle_ocr = PaddleOCR(lang='japan', use_gpu=False, cpu_threads=1)
        except (TypeError, ValueError):
            # PaddleOCR 3.x: use_gpu パラメータ廃止
            _paddle_ocr = PaddleOCR(lang='japan', cpu_threads=1)
    return _paddle_ocr

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("⚠️ 重大エラー: GEMINI_API_KEYが見つかりません")

genai.configure(api_key=api_key)
model = genai.GenerativeModel('gemini-2.0-flash')

GEMINI_GENERATION_CONFIG = {
    "temperature": 0,
    "response_mime_type": "application/json",
    "max_output_tokens": 8192,
}


# ============================================================
# 共通ユーティリティ
# ============================================================

def verify_tax_math(candidates, rate):
    """数学的検証ロジック (V3.0)"""
    nums = []
    for c in candidates:
        try:
            clean_str = str(c).replace(',', '').replace('¥', '').replace('円', '').strip()
            clean_num = float(clean_str)
            nums.append(clean_num)
        except:
            continue

    nums = sorted(list(set(nums)), reverse=True)

    for amount in nums:
        for tax in nums:
            if amount <= tax:
                continue
            # パターンA: 税抜
            if abs(amount * rate - tax) <= 2.0:
                return int(amount), int(tax)
            # パターンB: 税込
            expected_net = amount / (1 + rate)
            if abs((amount - expected_net) - tax) <= 2.0:
                return int(amount - tax), int(tax)

    return None, None


def extract_json(text):
    """JSON抽出強化関数"""
    if not text:
        return None

    text = text.strip()

    # 1) JSON文字列そのもの
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2) fenced code block 内の JSON
    block_match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if block_match:
        try:
            return json.loads(block_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3) 文中の最初の JSON object / array
    try:
        obj_match = re.search(r'\{.*\}', text, re.DOTALL)
        if obj_match:
            return json.loads(obj_match.group(0))

        arr_match = re.search(r'\[.*\]', text, re.DOTALL)
        if arr_match:
            return json.loads(arr_match.group(0))

        return None
    except json.JSONDecodeError:
        return None


def _get_mime_type(file_path):
    """ファイル拡張子からMIMEタイプを判定"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in ('.jpg', '.jpeg'):
        return "image/jpeg"
    elif ext == '.png':
        return "image/png"
    elif ext == '.heic':
        return "image/heic"
    elif ext == '.pdf':
        return "application/pdf"
    return "image/jpeg"


def _get_finish_reason(response):
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return ""
        return str(getattr(candidates[0], "finish_reason", "")) or ""
    except Exception:
        return ""


# === Cloud Vision API — 甲方確認待ち、コード保持 ===
# def _ocr_with_cloud_vision(image_bytes):
#     """Google Cloud Vision API で OCR テキストを取得"""
#     sa_file = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")
#     if os.path.exists(sa_file):
#         from google.oauth2 import service_account as sa_auth
#         credentials = sa_auth.Credentials.from_service_account_file(sa_file)
#         client = vision.ImageAnnotatorClient(credentials=credentials)
#     else:
#         client = vision.ImageAnnotatorClient()
#     image = vision.Image(content=image_bytes)
#     response = client.document_text_detection(image=image)
#
#     if response.error.message:
#         raise Exception(f"Cloud Vision API error: {response.error.message}")
#
#     return response.full_text_annotation.text or ""


def _parse_paddle_result(result):
    """PaddleOCR 結果をパース（v2.x / v3.x 両対応）"""
    texts = []
    scores = []
    if not result:
        return texts, scores
    for page in result:
        if not page:
            continue
        # v3.x predict() format: OCRResult オブジェクト (dict-like, rec_texts/rec_scores キー)
        if hasattr(page, 'keys') and 'rec_texts' in page:
            texts.extend(page['rec_texts'])
            scores.extend([float(s) for s in page['rec_scores']])
            continue
        # v2.x format: [[box, (text, score)], ...]
        if isinstance(page, list) and len(page) > 0:
            for line in page:
                if isinstance(line, (list, tuple)) and len(line) >= 2:
                    text_info = line[-1]
                    if isinstance(text_info, (list, tuple)) and len(text_info) >= 2:
                        texts.append(str(text_info[0]))
                        scores.append(float(text_info[1]))
                    elif isinstance(text_info, dict):
                        texts.extend(text_info.get("rec_texts", []))
                        scores.extend(text_info.get("rec_scores", []))
    return texts, scores


def _ocr_with_paddleocr(image_bytes, mime_type="image/jpeg"):
    """PaddleOCR ローカル OCR エンジン（v2.x / v3.x 両対応）"""
    ocr = _get_paddle_ocr()

    if mime_type == "application/pdf":
        # 逐ページ変換（DPI 150 でメモリ節約、OCR には十分）
        from pypdf import PdfReader as _PR
        page_count = len(_PR(io.BytesIO(image_bytes)).pages)
        all_texts = []
        all_scores = []
        for pg in range(1, page_count + 1):
            images = convert_from_bytes(image_bytes, first_page=pg, last_page=pg, dpi=150)
            if not images:
                continue
            img_array = np.array(images[0])
            del images  # 即座に解放
            if hasattr(ocr, 'predict'):
                page_result = ocr.predict(img_array)
            else:
                page_result = ocr.ocr(img_array, cls=True)
            del img_array
            t, s = _parse_paddle_result(page_result)
            all_texts.extend(t)
            all_scores.extend(s)
        ocr_text = "\n".join(all_texts)
        avg_confidence = sum(all_scores) / len(all_scores) if all_scores else 0.0
        return ocr_text, avg_confidence
    else:
        img = Image.open(io.BytesIO(image_bytes))
        img_array = np.array(img.convert("RGB"))

    if hasattr(ocr, 'predict'):
        result = ocr.predict(img_array)
    else:
        result = ocr.ocr(img_array, cls=True)

    texts, scores = _parse_paddle_result(result)

    if not texts:
        return "", 0.0

    ocr_text = "\n".join(texts)
    avg_confidence = sum(scores) / len(scores) if scores else 0.0

    return ocr_text, avg_confidence


def _call_gemini_text(ocr_text, prompt):
    """OCR テキストを Gemini に送って構造化データを抽出"""
    full_prompt = f"{prompt}\n\n--- OCRテキスト ---\n{ocr_text}"
    response = model.generate_content(
        [full_prompt],
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


def _call_gemini_bytes(file_data, mime_type, prompt):
    """Gemini API を呼び出して JSON を返す (フォールバック用)"""
    response = model.generate_content(
        [
            {"mime_type": mime_type, "data": file_data},
            prompt
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


def _call_gemini_cross_validate(ocr_text, file_data, mime_type, prompt):
    """Strategy C: OCR テキストと原画像の両方を Gemini に送信"""
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


def _call_gemini(file_path, prompt):
    with open(file_path, "rb") as f:
        file_data = f.read()
    mime_type = _get_mime_type(file_path)
    return _call_gemini_bytes(file_data, mime_type, prompt)


def _split_pdf_pages(file_path):
    """PDF を 1ページずつ yield するジェネレータ（メモリ節約）"""
    if PdfReader is None or PdfWriter is None:
        print("⚠️ pypdf未導入のため、PDF分割解析をスキップします")
        return

    try:
        reader = PdfReader(file_path)
        if len(reader.pages) <= 1:
            return

        total_pages = len(reader.pages)
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        for i, page in enumerate(reader.pages, 1):
            writer = PdfWriter()
            writer.add_page(page)
            buf = io.BytesIO()
            writer.write(buf)
            yield {
                "page_num": i,
                "total_pages": total_pages,
                "data": buf.getvalue(),
                "filename": f"{base_name}_p{i}.pdf",
            }
            # buf は yield 後に GC 対象になる
    except Exception as e:
        print(f"⚠️ PDFページ分割失敗: {e}")
        return


# ============================================================
# OCR テキストからのフィールド抽出（Gemini に依存しない）
# ============================================================

def _extract_date_from_ocr(ocr_text):
    """OCR テキストから日付を正規表現で抽出（Gemini より信頼性が高い）"""
    if not ocr_text:
        return None

    # パターン1: 2026年1月27日, 2026年 1月27日（火）, 2026年01月10日
    m = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', ocr_text)
    if m:
        return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"

    # パターン2: 26年 1月14日 (西暦下2桁)
    m = re.search(r'(\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', ocr_text)
    if m:
        year = int(m.group(1))
        if year <= 99:
            year = 2000 + year
        return f"{year}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"

    # パターン3: 2026/01/19, 2026-01-19
    m = re.search(r'(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})', ocr_text)
    if m:
        return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"

    # パターン4: 26#01月13日 (テクノパーキング形式)
    m = re.search(r'(\d{2})[#＃](\d{2})月(\d{1,2})日', ocr_text)
    if m:
        year = 2000 + int(m.group(1))
        return f"{year}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"

    # パターン5: 令和N年M月D日
    m = re.search(r'令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', ocr_text)
    if m:
        year = 2018 + int(m.group(1))
        return f"{year}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"

    # パターン6: 令和N年M月分 / YYYY年M月分（納付書等、日なし→1日とする）
    m = re.search(r'令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*分', ocr_text)
    if m:
        year = 2018 + int(m.group(1))
        return f"{year}/{int(m.group(2)):02d}/01"
    m = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*分', ocr_text)
    if m:
        return f"{m.group(1)}/{int(m.group(2)):02d}/01"

    return None


def _validate_gemini_date(date_str):
    """Gemini が返した日付を検証。怪しければ空文字を返す（高亮対象にする）。

    OCR で抽出できなかった場合の最終チェック:
    - /00 日 → 無効
    - 年が 2024-2027 の範囲外 → 年号誤判定の可能性
    - パースできない → 無効
    """
    if not date_str:
        return ""
    s = str(date_str).strip()
    m = re.match(r'(\d{4})/(\d{2})/(\d{2})', s)
    if not m:
        return ""  # パースできない → 空
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if day == 0:
        return ""  # /00 → 空
    if year < 2020 or year > 2027:
        return ""  # 2014年等 → Gemini の年号誤判定、空にする
    if month < 1 or month > 12 or day > 31:
        return ""
    return s


def _extract_invoice_num_from_ocr(ocr_text):
    """OCR テキストから T番号（適格請求書発行事業者登録番号）を抽出"""
    if not ocr_text:
        return None

    # T + 13桁 (ハイフン・スペース含む可能性)
    m = re.search(r'[TＴ][\s\-]*(\d[\s\-]*){13}', ocr_text)
    if m:
        # 数字のみ抽出
        matched = m.group(0)
        digits = re.sub(r'[^0-9]', '', matched)
        if len(digits) == 13:
            return f"T{digits}"

    # 「登録番号」の近くにある T + 数字
    m = re.search(r'登[録录]番号\s*[TＴ][\s\-]*([\d\s\-]{13,20})', ocr_text)
    if m:
        digits = re.sub(r'[^0-9]', '', m.group(1))[:13]
        if len(digits) == 13:
            return f"T{digits}"

    return None


def _apply_ocr_overrides(raw_data, ocr_text, prefix=""):
    """OCR テキストから抽出した日付・T番号で Gemini の結果を上書きする。

    Gemini は日付の年号解釈を間違えやすい（26年→2014年等）が、
    PaddleOCR のテキストから正規表現で抽出すれば確実。
    """
    if not raw_data:
        return

    ocr_date = _extract_date_from_ocr(ocr_text) if ocr_text else None
    ocr_tnum = _extract_invoice_num_from_ocr(ocr_text) if ocr_text else None

    # documents 配列がある場合（領収書新フォーマット）
    if isinstance(raw_data, dict) and "documents" in raw_data:
        for doc in raw_data.get("documents", []):
            if ocr_date:
                gemini_date = doc.get("date", "")
                if gemini_date != ocr_date:
                    print(f"{prefix}📅 日付上書: Gemini={gemini_date} → OCR={ocr_date}")
                doc["date"] = ocr_date
            else:
                # OCR 抽出できなくても無効日付は修正
                validated = _validate_gemini_date(doc.get("date", ""))
                if not validated:
                    print(f"{prefix}⚠️ 日付不明: Gemini={doc.get('date','')} → 空欄（要確認）")
                doc["date"] = validated
            if ocr_tnum:
                gemini_tnum = doc.get("invoice_num", "")
                if gemini_tnum != ocr_tnum:
                    print(f"{prefix}🔢 T番号上書: Gemini={gemini_tnum} → OCR={ocr_tnum}")
                doc["invoice_num"] = ocr_tnum
    else:
        # 旧フォーマット / 他の文書タイプ
        if ocr_date:
            gemini_date = raw_data.get("date", "")
            if gemini_date != ocr_date:
                print(f"{prefix}📅 日付上書: Gemini={gemini_date} → OCR={ocr_date}")
            raw_data["date"] = ocr_date
        else:
            validated = _validate_gemini_date(raw_data.get("date", ""))
            if not validated:
                print(f"{prefix}⚠️ 日付不明: Gemini={raw_data.get('date','')} → 空欄（要確認）")
            raw_data["date"] = validated
        if ocr_tnum:
            gemini_tnum = raw_data.get("invoice_num", "")
            if gemini_tnum != ocr_tnum:
                print(f"{prefix}🔢 T番号上書: Gemini={gemini_tnum} → OCR={ocr_tnum}")
            raw_data["invoice_num"] = ocr_tnum


# ============================================================
# プロンプト定義
# ============================================================

PROMPTS = {
    DocType.RECEIPT: """
あなたはプロの経理担当者です。以下のOCRテキストから全ての書類（領収書・レシート・受取書・振込控え等）を分析してください。

重要: テキストに複数の書類が含まれる場合は、全ての書類を別々に抽出してください。

【出力JSONフォーマット】
{
    "documents": [
        {
            "doc_category": "receipt | bank_transfer | fee_receipt",
            "date": "YYYY/MM/DD",
            "vendor": "取引先名（店名・屋号を優先。株式会社等の法人格は省略可。例: '株式会社O・B・UCompany' → 'O・B・UCompany'、'(株)喜多村石油店' → '喜多村石油店'。振込の場合は振込依頼人）",
            "invoice_num": "適格請求書発行事業者登録番号 (T+数字13桁, 例: T1234567890123)。レシート/領収書の下部・フッター・店舗情報欄に小さく印字されていることが多い。「登録番号」「Registration No」「T-」で始まる13桁の番号を探す。領収書No/取引番号/伝票番号/レシート番号は含めない。ハイフンは除去。見つからなければ空文字",
            "payment_method": "支払い方法 (現金, クレジットカード, PayPay, 振込, ATM)",
            "memo": "メモ（振込先名、用途など）",
            "items": [
                {
                    "description": "品目・内容",
                    "amount": 税込金額(数値),
                    "tax_rate": 0.08 or 0.10 or 0,
                    "tax_amount": 消費税額(数値, なければ0),
                    "debit_account": "費用の勘定科目を推定"
                }
            ]
        }
    ]
}

【doc_categoryの判定基準】
- "receipt": 通常の領収書・レシート（コンビニ、店舗等での購入）
- "bank_transfer": 銀行振込の受取書・振込控え（振込金額本体。税区分は「対象外」）
- "fee_receipt": 振込手数料の領収証（ATM手数料、コンビニ手数料等。課税対象）

【勘定科目の選択肢】以下の科目名を優先して使用してください：
備品・消耗品費, 旅費交通費, 通信費, 水道光熱費, 修繕費,
地代家賃, 保険料, 租税公課, 広告宣伝費, 支払手数料, 支払報酬,
接待交際費, 会議費, 福利厚生費, 業務委託料, 荷造運賃, 新聞図書費,
リース料, 諸会費, 外注費, 研修採用費, 未払金, 普通預金
上記にない場合のみ一般的な科目名を使用してください。

【勘定科目の推定基準】
- bank_transfer: debit_account は "未払金"（既存の買掛金・未払金の支払い）
- fee_receipt: debit_account は "支払手数料"
- receipt: 内容から推定（上記の選択肢から選ぶ）
- 飲食店・レストラン・居酒屋・カフェ・バー等での飲食代 → "接待交際費"
- ガソリンスタンド・駐車場・高速道路料金 → "旅費交通費"
- レンタカー → "旅費交通費"
- タクシー → "旅費交通費"

【tax_rate の判定基準】
最優先: レシートに税率(8%, 10%)や「※」「軽」マークが印字されていればそれに従う。
印字がない場合は以下のルールで判定:
- 0.10（デフォルト）: ほとんどの品目。外食(店内飲食), 酒類, 日用品, サービス, 交通費等
- 0.08: レシートに軽減税率マーク(※/軽/8%)がある飲食料品のみ。外食は対象外
- 0: 銀行振込本体(bank_transfer)
迷ったら 0.10 を使用してください

【payment_method の判定基準】
- コンビニ（FamilyMart, セブンイレブン等）での支払い → "現金"
- SMCC(QQ), QUICPay, iD 等の電子決済表記がある場合でも、
  コンビニ払いの振込手数料であれば → "現金"（代収扱い）
- 銀行ATMでの振込 → "ATM"
- 銀行窓口での振込 → "振込"
- クレジットカード明細 → "クレジットカード"

【date の取得方法（重要）】
- 書類に記載された日付を最優先
- 日付欄が空白の場合は、以下の順で日付を探す:
  1. 取扱日付印・受付印の中の数字（例: "9.16" → 当年の9月16日）
  2. 書類下部の「ご依頼人」欄付近のスタンプ日付
  3. 同一テキスト内の他の書類の日付（同日の取引である可能性が高い）
- 印章の日付形式: "M.DD", "MM.DD", "R7.9.16", "2025.9.16" など → 西暦 YYYY/MM/DD に変換
- 年が不明な場合は、同一テキスト内の他の書類の年、または現在の年（2026年）を使用
- dateは可能な限り必ず出力してください。空文字は最終手段です

注意:
- テキストに1枚の書類しかなくても、必ず documents 配列で返してください
- 金額は数値型(カンマなし)で返してください
- 振込受取書では、vendor は振込依頼人（支払い元の会社名）を記載してください
""",

    DocType.PURCHASE_INVOICE: """
あなたはプロの経理担当者です。以下のOCRテキストから支払請求書・仕入請求書を分析し、会計ソフト用データを抽出してください。

【出力JSONフォーマット】
{
    "date": "YYYY/MM/DD (請求日または発行日)",
    "vendor": "取引先名（請求元の会社名・屋号）",
    "invoice_num": "適格請求書発行事業者登録番号 (T+数字13桁, 例: T1234567890123)。「登録番号」として記載されているもののみ。請求書番号・伝票番号は含めない。ハイフンは除去。なければ空文字",
    "memo": "メモ",
    "items": [
        {
            "description": "品目・サービス名",
            "amount": 税抜金額(数値),
            "tax_rate": 税率(0.08 or 0.10),
            "tax_amount": 消費税額(数値),
            "debit_account": "費用の勘定科目を推定"
        }
    ],
    "total_amount": 合計金額(税込, 数値),
    "payment_method": "支払い方法 (振込, 口座振替, 現金 など)",
    "due_date": "支払期日 (あれば YYYY/MM/DD)"
}

【勘定科目の選択肢】以下の科目名を優先して使用してください：
仕入高, 外注費, 備品・消耗品費, 通信費, 広告宣伝費, 旅費交通費, 租税公課,
支払手数料, 支払報酬, 業務委託料, 荷造運賃, 接待交際費
上記にない場合のみ一般的な科目名を使用してください。

注意:
- 複数品目がある場合はitems配列に全て含めてください
- 品目が1つの場合でもitems配列で返してください
- 金額は数値型(カンマなし)で返してください
""",

    DocType.SALES_INVOICE: """
あなたはプロの経理担当者です。以下のOCRテキストから売上請求書を分析し、会計ソフト用データを抽出してください。

【出力JSONフォーマット】
{
    "date": "YYYY/MM/DD (請求日または発行日)",
    "vendor": "取引先名（請求先・顧客名の会社名・屋号）",
    "invoice_num": "適格請求書発行事業者登録番号 (T+数字13桁, 例: T1234567890123)。「登録番号」として記載されているもののみ。請求書番号は含めない。ハイフンは除去。なければ空文字",
    "memo": "メモ",
    "items": [
        {
            "description": "品目・サービス名",
            "amount": 税抜金額(数値),
            "tax_rate": 税率(0.08 or 0.10),
            "tax_amount": 消費税額(数値)
        }
    ],
    "total_amount": 合計金額(税込, 数値)
}

注意:
- これは売上（収益）の請求書です。借方は売掛金、貸方は売上高になります
- 金額は数値型(カンマなし)で返してください
""",

    DocType.SALARY_SLIP: """
あなたはプロの経理担当者です。以下のOCRテキストから賃金台帳・給与明細書を分析し、会計ソフト用データを抽出してください。

【出力JSONフォーマット】
{
    "date": "YYYY/MM/DD (支給日)",
    "employee_name": "従業員名",
    "memo": "メモ (対象期間など)",
    "gross_salary": 総支給額(数値),
    "social_insurance": 社会保険料合計(数値, 健康保険+厚生年金+雇用保険),
    "income_tax": 所得税額(数値),
    "resident_tax": 住民税額(数値),
    "other_deductions": その他控除合計(数値, あれば),
    "net_salary": 差引支給額(数値)
}

注意:
- 金額は数値型(カンマなし)で返してください
- 各控除項目が0の場合は0と記載してください
- 社会保険料は健康保険料+厚生年金+雇用保険の合計値を記載してください
""",
}


# ============================================================
# エントリビルダー（各文書タイプの仕訳生成ロジック）
# ============================================================

def _determine_credit_account(pay_method, doc_category="receipt"):
    """支払方法とドキュメントカテゴリから貸方科目を決定"""
    # 顧客確認済み: 領収書・請求書とも貸方は「未払金」に統一
    return "未払金"


def _determine_tax_types(doc_category, tax_rate):
    """ドキュメントカテゴリと税率から借方・貸方税区分を決定"""
    if doc_category == "bank_transfer" or tax_rate == 0:
        return "対象外", "対象外"
    elif tax_rate == 0.08:
        return "課対仕入8% (軽)", "対象外"
    else:  # 0.10
        return "課対仕入10%", "対象外"


def _build_entries_for_single_doc(doc):
    """documents配列の1要素（単一書類）から仕訳エントリを生成"""
    entries = []
    doc_category = doc.get("doc_category", "receipt")

    # 貸方科目決定
    pay_method = str(doc.get("payment_method", "現金"))
    credit_account = _determine_credit_account(pay_method, doc_category)

    for item in doc.get("items", []):
        amount = item.get("amount", 0)
        if not amount or int(amount) == 0:
            continue

        tax_rate = item.get("tax_rate", 0.10)
        debit_account = item.get("debit_account", "消耗品費")

        # 税区分決定
        debit_tax_type, credit_tax_type = _determine_tax_types(
            doc_category, tax_rate
        )

        entries.append({
            "debit_account": debit_account,
            "debit_tax_type": debit_tax_type,
            "credit_account": credit_account,
            "credit_tax_type": credit_tax_type,
            "amount": int(amount),
            "description": item.get("description", ""),
        })

    return entries


def _build_entries_from_receipt(raw_data):
    """領収書データから仕訳エントリを生成（新旧フォーマット両対応）"""
    # 旧フォーマット（documents キーなし）: レガシーロジック
    if "documents" not in raw_data:
        return _build_entries_from_receipt_legacy(raw_data)

    # 新フォーマット: documents 配列
    # NOTE: 複数文書の場合は process_pipeline 側で処理するため、
    #       ここでは単一文書のフォールバックのみ対応
    documents = raw_data.get("documents", [])
    if len(documents) == 1:
        return _build_entries_for_single_doc(documents[0])

    # 複数文書は process_pipeline で処理済みのはずだが、
    # 万が一ここに来た場合は全文書のエントリを結合して返す
    all_entries = []
    for doc in documents:
        all_entries.extend(_build_entries_for_single_doc(doc))
    return all_entries


def _build_entries_from_receipt_legacy(raw_data):
    """旧フォーマット用: tax_8_area/tax_10_area ベースのロジック（後方互換）"""
    entries = []
    debit_account = raw_data.get("debit_account", "消耗品費")

    # 貸方科目決定
    pay_method = str(raw_data.get("payment_method", "現金"))
    credit_account = "現金"
    if any(x in pay_method for x in ["クレジット", "Credit", "Card", "VISA", "Master"]):
        credit_account = "未払金"
    elif "振込" in pay_method:
        credit_account = "普通預金"

    # 8% 検証
    candidates_8 = raw_data.get("tax_8_area", {}).get("candidates", [])
    amount_8, tax_8 = verify_tax_math(candidates_8, 0.08)
    if amount_8:
        entries.append({
            "debit_account": debit_account,
            "debit_tax_type": "課対仕入8% (軽)",
            "credit_account": credit_account,
            "credit_tax_type": "対象外",
            "amount": amount_8,
            "description": raw_data.get("description_raw", "") + " (食品等)",
        })

    # 10% 検証
    candidates_10 = raw_data.get("tax_10_area", {}).get("candidates", [])
    amount_10, tax_10 = verify_tax_math(candidates_10, 0.10)
    if amount_10:
        desc = raw_data.get("description_raw", "")
        if amount_10 < 50:
            desc = "レジ袋"
        entries.append({
            "debit_account": debit_account,
            "debit_tax_type": "課対仕入10%",
            "credit_account": credit_account,
            "credit_tax_type": "対象外",
            "amount": amount_10,
            "description": desc,
        })

    return entries


def _build_entries_from_purchase_invoice(raw_data):
    """支払請求書・仕入請求書データから仕訳エントリを生成"""
    entries = []
    items = raw_data.get("items", [])

    # 貸方科目決定
    pay_method = str(raw_data.get("payment_method", "振込"))
    credit_account = "買掛金"
    if "振込" in pay_method or "口座" in pay_method:
        credit_account = "普通預金"
    elif "現金" in pay_method:
        credit_account = "現金"

    config = DOC_TYPE_CONFIG[DocType.PURCHASE_INVOICE]

    for item in items:
        amount = item.get("amount", 0)
        if not amount or int(amount) == 0:
            continue

        tax_rate = item.get("tax_rate", 0.10)
        debit_account = item.get("debit_account", config["default_debit"])

        if tax_rate == 0.08:
            debit_tax_type = "課対仕入8% (軽)"
        else:
            debit_tax_type = "課対仕入10%"

        entries.append({
            "debit_account": debit_account,
            "debit_tax_type": debit_tax_type,
            "credit_account": credit_account,
            "credit_tax_type": "対象外",
            "amount": int(amount),
            "description": item.get("description", ""),
        })

    return entries


def _build_entries_from_sales_invoice(raw_data):
    """売上請求書データから仕訳エントリを生成"""
    entries = []
    items = raw_data.get("items", [])
    config = DOC_TYPE_CONFIG[DocType.SALES_INVOICE]

    for item in items:
        amount = item.get("amount", 0)
        if not amount or int(amount) == 0:
            continue

        tax_rate = item.get("tax_rate", 0.10)
        if tax_rate == 0.08:
            credit_tax_type = "課税売上8% (軽)"
        else:
            credit_tax_type = "課税売上10%"

        entries.append({
            "debit_account": config["default_debit"],      # 売掛金
            "debit_tax_type": "対象外",
            "credit_account": config["default_credit"],    # 売上高
            "credit_tax_type": credit_tax_type,
            "amount": int(amount),
            "description": item.get("description", ""),
        })

    return entries


def _build_entries_from_salary_slip(raw_data):
    """賃金台帳・給与明細書データから仕訳エントリを生成"""
    entries = []
    employee = raw_data.get("employee_name", "")

    gross = int(raw_data.get("gross_salary", 0))
    social_ins = int(raw_data.get("social_insurance", 0))
    income_tax = int(raw_data.get("income_tax", 0))
    resident_tax = int(raw_data.get("resident_tax", 0))
    other_ded = int(raw_data.get("other_deductions", 0))
    net = int(raw_data.get("net_salary", 0))

    if gross <= 0:
        return entries

    # 借方: 給料手当（総支給額）
    entries.append({
        "debit_account": "給料手当",
        "debit_tax_type": "対象外",
        "credit_account": "普通預金",
        "credit_tax_type": "対象外",
        "amount": net,
        "description": f"給与 {employee} (差引支給額)",
    })

    # 貸方控除: 社会保険料預り金
    if social_ins > 0:
        entries.append({
            "debit_account": "給料手当",
            "debit_tax_type": "対象外",
            "credit_account": "預り金",
            "credit_tax_type": "対象外",
            "amount": social_ins,
            "credit_sub_account": "社会保険料",
            "description": f"社会保険料控除 {employee}",
        })

    # 貸方控除: 源泉所得税
    if income_tax > 0:
        entries.append({
            "debit_account": "給料手当",
            "debit_tax_type": "対象外",
            "credit_account": "預り金",
            "credit_tax_type": "対象外",
            "amount": income_tax,
            "credit_sub_account": "源泉所得税",
            "description": f"源泉所得税控除 {employee}",
        })

    # 貸方控除: 住民税
    if resident_tax > 0:
        entries.append({
            "debit_account": "給料手当",
            "debit_tax_type": "対象外",
            "credit_account": "預り金",
            "credit_tax_type": "対象外",
            "amount": resident_tax,
            "credit_sub_account": "住民税",
            "description": f"住民税控除 {employee}",
        })

    # 貸方控除: その他
    if other_ded > 0:
        entries.append({
            "debit_account": "給料手当",
            "debit_tax_type": "対象外",
            "credit_account": "預り金",
            "credit_tax_type": "対象外",
            "amount": other_ded,
            "credit_sub_account": "その他控除",
            "description": f"その他控除 {employee}",
        })

    return entries


# エントリビルダー登録テーブル
ENTRY_BUILDERS = {
    DocType.RECEIPT: _build_entries_from_receipt,
    DocType.PURCHASE_INVOICE: _build_entries_from_purchase_invoice,
    DocType.SALES_INVOICE: _build_entries_from_sales_invoice,
    DocType.SALARY_SLIP: _build_entries_from_salary_slip,
}


def _normalize_receipt_results(raw_data, prefix=""):
    """領収書レスポンスを統一結果(list[dict])に正規化"""
    results = []

    # 新フォーマット: documents 配列
    if isinstance(raw_data, dict) and "documents" in raw_data:
        documents = raw_data.get("documents") or []
        if not documents:
            print(f"{prefix}⚠️ documents配列が空です")
            return []

        print(f"{prefix}📑 {len(documents)} 件の書類を検出")

        for i, doc in enumerate(documents, 1):
            doc_cat = doc.get("doc_category", "receipt")
            vendor = doc.get("vendor", "不明")
            print(f"{prefix}  [{i}] {doc_cat}: {vendor}")

            entries = _build_entries_for_single_doc(doc)
            if not entries:
                print(f"{prefix}  ⚠️ エントリなし（スキップ）")
                continue

            results.append({
                "doc_type": DocType.RECEIPT,
                "date": doc.get("date"),
                "vendor": vendor,
                "invoice_num": doc.get("invoice_num", ""),
                "memo": doc.get("memo", ""),
                "entries": entries,
            })
        return results

    # 旧フォーマット: 単一書類
    entries = _build_entries_from_receipt(raw_data or {})
    if not entries:
        return []

    results.append({
        "doc_type": DocType.RECEIPT,
        "date": (raw_data or {}).get("date"),
        "vendor": (raw_data or {}).get("vendor", ""),
        "invoice_num": (raw_data or {}).get("invoice_num", ""),
        "memo": (raw_data or {}).get("memo", ""),
        "entries": entries,
    })
    return results


# ============================================================
# メインパイプライン
# ============================================================

def _route_ocr_strategy(data_bytes, mime_type, prompt, ocr_strategy, prefix=""):
    """OCR 戦略に基づいてルーティング。(raw_data, ocr_text) を返す。"""
    import config
    raw_data = None
    ocr_text = ""
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
    return raw_data, ocr_text


def process_pipeline(file_path, doc_type=DocType.RECEIPT, ocr_strategy=None):
    """
    文書を分析し、仕訳データを逐次 yield するジェネレータ。

    各 yield は dict: {"result": 仕訳dict, "page_num": int, "total_pages": int}

    メモリ最適化: 1ページ処理→yield→GC→次ページ の流れで
    EC2 t2.micro (768MB) でも安定動作する。

    Args:
        file_path: 文書ファイルのパス
        doc_type: 文書タイプ (DocType 定数)
        ocr_strategy: OCR 戦略 (A/B/C, None=config.OCR_STRATEGY)

    Yields:
        dict: {"result": dict, "page_num": int, "total_pages": int}
    """
    import itertools

    filename = os.path.basename(file_path)
    type_label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)
    print(f"🧠 PaddleOCR + Gemini で{type_label}を分析中: {filename} (戦略: {ocr_strategy}) ...")

    try:
        prompt = PROMPTS.get(doc_type)
        if not prompt:
            print(f"⚠️ 未対応の文書タイプ: {doc_type}")
            return

        import config
        if ocr_strategy is None:
            ocr_strategy = config.OCR_STRATEGY

        mime_type = _get_mime_type(file_path)

        # ── 領収書 PDF: 1ページずつ yield（各ページ独立）──
        if doc_type == DocType.RECEIPT and mime_type == "application/pdf":
            page_gen = _split_pdf_pages(file_path)
            first_page = next(page_gen, None)
            if first_page is not None:
                total = first_page["total_pages"]
                print(f"📄 大型PDF対応: {total}ページを分割解析します")
                yielded = 0
                failed_pages = 0

                for page_info in itertools.chain([first_page], page_gen):
                    idx = page_info["page_num"]
                    prefix = f"[p{idx}] "
                    page_data = page_info["data"]

                    page_raw_data, ocr_text = _route_ocr_strategy(
                        page_data, "application/pdf", prompt, ocr_strategy, prefix=prefix
                    )

                    if not page_raw_data:
                        print(f"{prefix}🔄 フォールバック: Gemini Vision で再試行")
                        page_raw_data = _call_gemini_bytes(page_data, "application/pdf", prompt)

                    if not page_raw_data:
                        failed_pages += 1
                        print(f"{prefix}⚠️ AIの応答がJSONではありませんでした")
                        continue

                    # OCR テキストから日付・T番号を抽出し Gemini 結果を上書き
                    _apply_ocr_overrides(page_raw_data, ocr_text, prefix)

                    page_results = _normalize_receipt_results(page_raw_data, prefix=prefix)
                    if not page_results:
                        print(f"{prefix}⚠️ 有効な仕訳エントリが見つかりません")
                        continue

                    for entry in page_results:
                        yield {
                            "result": entry,
                            "page_num": idx,
                            "total_pages": total,
                        }
                        yielded += 1
                    gc.collect()

                if yielded > 0:
                    print(
                        f"✅ PDF分割解析完了: {yielded}件抽出 "
                        f"(失敗ページ: {failed_pages})"
                    )
                else:
                    print("⚠️ PDF分割解析でも有効結果を取得できませんでした")
                return

        # ── 非領収書 PDF: 逐ページ OCR → 合併テキスト → 1回 Gemini ──
        if mime_type == "application/pdf":
            page_gen = _split_pdf_pages(file_path)
            first_page = next(page_gen, None)
            if first_page is not None:
                total = first_page["total_pages"]
                print(f"📄 多ページPDF: {total}ページを逐次OCR→統合解析します")
                all_ocr_texts = []
                for pg in itertools.chain([first_page], page_gen):
                    prefix = f"[p{pg['page_num']}] "
                    pg_text, _ = _ocr_with_paddleocr(pg["data"], "application/pdf")
                    all_ocr_texts.append(pg_text)
                    print(f"{prefix}OCR完了 ({len(pg_text)}文字)")
                    gc.collect()

                combined_ocr = "\n---PAGE BREAK---\n".join(all_ocr_texts)
                del all_ocr_texts

                with open(file_path, "rb") as f:
                    file_data = f.read()
                if ocr_strategy == "C":
                    raw_data = _call_gemini_cross_validate(combined_ocr, file_data, mime_type, prompt)
                elif ocr_strategy == "B":
                    raw_data = _call_gemini_cross_validate(combined_ocr, file_data, mime_type, prompt)
                else:
                    raw_data = _call_gemini_text(combined_ocr, prompt)
                del file_data
                gc.collect()

                if not raw_data:
                    print("🔄 フォールバック: Gemini Vision で再試行")
                    raw_data = _call_gemini(file_path, prompt)

                if raw_data:
                    builder = ENTRY_BUILDERS.get(doc_type)
                    if builder:
                        entries = builder(raw_data)
                        vendor = raw_data.get("vendor", "")
                        if doc_type == DocType.SALARY_SLIP:
                            vendor = raw_data.get("employee_name", "")
                        yield {
                            "result": {
                                "doc_type": doc_type,
                                "date": raw_data.get("date"),
                                "vendor": vendor,
                                "invoice_num": raw_data.get("invoice_num", ""),
                                "memo": raw_data.get("memo", ""),
                                "entries": entries,
                            },
                            "page_num": 1,
                            "total_pages": 1,
                        }
                else:
                    print("⚠️ AIの応答がJSONではありませんでした")
                return

        # ── 単ページ PDF / 画像ファイル: 従来通り処理 ──
        raw_data = None
        ocr_text = ""
        with open(file_path, "rb") as f:
            file_data = f.read()

        raw_data, ocr_text = _route_ocr_strategy(file_data, mime_type, prompt, ocr_strategy)
        del file_data
        gc.collect()

        if not raw_data:
            print("🔄 フォールバック: Gemini Vision で再試行")
            raw_data = _call_gemini(file_path, prompt)

        if not raw_data:
            print("⚠️ AIの応答がJSONではありませんでした")
            return

        # OCR テキストから日付・T番号を抽出し Gemini 結果を上書き
        _apply_ocr_overrides(raw_data, ocr_text)

        # ── 領収書処理（単ページ PDF / 画像）──
        if doc_type == DocType.RECEIPT:
            results = _normalize_receipt_results(raw_data)
            if not results:
                return
            for entry in results:
                yield {
                    "result": entry,
                    "page_num": 1,
                    "total_pages": 1,
                }
            return

        # ── 通常パス（他の文書タイプ、単ページ / 画像）──
        builder = ENTRY_BUILDERS.get(doc_type)
        if not builder:
            print(f"⚠️ エントリビルダーが未登録: {doc_type}")
            return

        entries = builder(raw_data)

        vendor = raw_data.get("vendor", "")
        if doc_type == DocType.SALARY_SLIP:
            vendor = raw_data.get("employee_name", "")

        yield {
            "result": {
                "doc_type": doc_type,
                "date": raw_data.get("date"),
                "vendor": vendor,
                "invoice_num": raw_data.get("invoice_num", ""),
                "memo": raw_data.get("memo", ""),
                "entries": entries,
            },
            "page_num": 1,
            "total_pages": 1,
        }

    except Exception as e:
        print(f"❌ 解析失敗: {e}")
        return
