import os
import io
import json
import re
import gc
import time
import unicodedata
import http.client
from dataclasses import dataclass, replace
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
try:
    from google.cloud import vision
except ImportError:
    vision = None
from dotenv import load_dotenv
from doc_types import (DocType, DOC_TYPE_CONFIG, DOC_TYPE_TAB_SUFFIX,
                       ENV_FOLDER_MAP, LINE_MODE_DOC_TYPES)
# 純関数モジュール（gspread / paddleocr 非依存）。依存は単方向で、
# page_family 側は ocr_engine を import しない（定数は複製し、乖離は
# test_page_family の突合テストが検出する）。
import card_entries
import card_file_state
import card_prompts
import card_salvage
import page_family
from receipt_aggregation import (
    KEIYUZEI_DEBIT_ACCOUNT,
    TOTAL_MISMATCH_TOLERANCE_YEN,
    aggregate_entries_by_tax_rate,
    build_rows_from_tax_summary,
    coerce_tax_amount,
    coerce_tax_rate,
    determine_tax_types,
    is_keiyuzei_text,
    select_aggregated_debit_account,
    sum_row_amounts,
)

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
        import config
        # PP-OCRv5 server モデルは CPU(特に macOS arm64)で ~20GB の内存床があり
        # 巨大票で OOM(SIGKILL)するため、既定は軽量な mobile モデル(config で切替可)
        kwargs = dict(lang='japan', cpu_threads=1)
        if getattr(config, "OCR_MODEL_TIER", "mobile") == "mobile":
            kwargs["text_detection_model_name"] = "PP-OCRv5_mobile_det"
            kwargs["text_recognition_model_name"] = "PP-OCRv5_mobile_rec"
        try:
            _paddle_ocr = PaddleOCR(**kwargs)  # PaddleOCR 3.x
        except (TypeError, ValueError):
            # 旧 2.x 互換: 新パラメータ非対応 → 最小構成で再試行
            _paddle_ocr = PaddleOCR(lang='japan', use_gpu=False, cpu_threads=1)
    return _paddle_ocr


def _downscale_for_ocr(img_array):
    """巨大スキャンを OCR 前に最長辺 config.OCR_MAX_SIDE まで縮小する。

    PP-OCRv5 の前処理/認識は入力画素数に比例してメモリを消費し、巨大スキャン
    (例: 6300x8400)で OOM する実測に基づく対策。通常票(dpi150 で ~1754px)は
    上限未満で無変更。strategy C では Gemini に原寸が渡るため最終精度影響は小。
    """
    import config
    cap = getattr(config, "OCR_MAX_SIDE", 0)
    if not cap:
        return img_array
    h, w = img_array.shape[:2]
    longest = max(h, w)
    if longest <= cap:
        return img_array
    scale = cap / longest
    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
    return np.array(Image.fromarray(img_array).resize(new_size, Image.LANCZOS))

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("⚠️ 重大エラー: GEMINI_API_KEYが見つかりません")

genai.configure(api_key=api_key)
model = genai.GenerativeModel('gemini-2.5-flash')

_GEMINI_RETRY_EXCEPTIONS = (
    http.client.RemoteDisconnected,
    ConnectionError,
    ConnectionResetError,
    TimeoutError,
    google_exceptions.ServiceUnavailable,
    google_exceptions.DeadlineExceeded,
    google_exceptions.InternalServerError,
    google_exceptions.ResourceExhausted,
    google_exceptions.Aborted,
    google_exceptions.GatewayTimeout,
)

_GEMINI_RETRY_DELAYS = [1, 4, 10, 30]


def _generate_content_with_retry(contents, generation_config=None):
    """Gemini 呼出（一時障害は指数退避で再試行）。

    `generation_config` は**呼出単位**で予算を変えるための省略可能引数
    （T5 §3.1）。省略時は従来どおりモジュール既定をそのまま渡す ——
    既存 doc_type の経路は 1 バイトも変わらない。
    """
    last_err = None
    for attempt, delay in enumerate([0] + _GEMINI_RETRY_DELAYS):
        if delay:
            print(f"⏳ Gemini 接続エラー、{delay}s 後に再試行 (attempt {attempt}/{len(_GEMINI_RETRY_DELAYS)})")
            time.sleep(delay)
        try:
            return model.generate_content(
                contents,
                generation_config=generation_config or GEMINI_GENERATION_CONFIG,
            )
        except _GEMINI_RETRY_EXCEPTIONS as e:
            last_err = e
            err_msg = str(e)[:120]
            print(f"⚠️ Gemini 一時エラー ({type(e).__name__}): {err_msg}")
            continue
    raise last_err


# gemini-2.5 系列は thinking tokens が max_output_tokens と予算を共有するため、
# 思考分の余裕を確保する（flash の動的思考上限 24,576 + JSON 本文 <2k）
GEMINI_MAX_OUTPUT_TOKENS = 32768

GEMINI_GENERATION_CONFIG = {
    "temperature": 0,
    "response_mime_type": "application/json",
    "max_output_tokens": GEMINI_MAX_OUTPUT_TOKENS,
}


def _line_generation_config():
    """逐行記帳 doc_type 用の generation_config。既定を使うなら None を返す。

    `config.GEMINI_MAX_OUTPUT_TOKENS_BULK` の **0 は「既存値を流用」** の意味
    であって予算 0 ではない。`max_output_tokens = 0` を SDK へ渡すと全応答が
    即座に截断する —— 毎頁が截断して提示行だらけになる最悪の回帰なので、
    0 が SDK へ届く経路そのものを作らない（None → 呼出側が既定を使う）。
    """
    import config
    bulk = getattr(config, "GEMINI_MAX_OUTPUT_TOKENS_BULK", 0)
    if not bulk or bulk == GEMINI_MAX_OUTPUT_TOKENS:
        return None
    return {**GEMINI_GENERATION_CONFIG, "max_output_tokens": bulk}


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


# proto enum FinishReason.MAX_TOKENS = 2（str() 表現の揺れに備えて文字列も併記）
_MAX_TOKENS_FINISH_REASONS = {"2", "MAX_TOKENS", "FinishReason.MAX_TOKENS"}


def _is_max_tokens_truncated(response):
    """finish_reason が MAX_TOKENS（=2）かを判定する"""
    return _get_finish_reason(response) in _MAX_TOKENS_FINISH_REASONS


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
            img_array = _downscale_for_ocr(img_array)  # 巨大スキャンのメモリ暴走対策
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
        img_array = _downscale_for_ocr(np.array(img.convert("RGB")))

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


def _format_token_usage(response):
    """usage_metadata から思考トークン数の近似値を整形する（取得不能時は '?'）"""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return "思考≈?tok"
    total = getattr(usage, "total_token_count", 0) or 0
    prompt = getattr(usage, "prompt_token_count", 0) or 0
    candidates = getattr(usage, "candidates_token_count", 0) or 0
    return f"思考≈{total - prompt - candidates}tok/全体{total}tok"


def _parse_gemini_response(response, salvage=False):
    """Gemini 応答から JSON を抽出（失敗時は警告ログを出して None を返す）

    `salvage=True`（逐行記帳 doc_type のみ）のときは、MAX_TOKENS で切れた
    応答から**完結した部分だけ**を回収する（T5 §3.2/§3.3）。prompt schema は
    `rows` が最後なので、切れた応答にも `rows_on_page` と完結した明細行が
    N 個残っている —— `extract_json` の all-or-nothing がそれを捨てている。

    1 行も救えなかった場合も **`{"rows": []}` という真値の dict** を返す。
    None にすると呼出側の `if not raw_data:` が真になって Vision 兜底が走り、
    同じ予算で同じ頁を焼き直して同じ截断に終わる（無限ループの燃料）。
    截断は「頁が長い」という決定的性質であって一時障害ではない。
    """
    try:
        text = (getattr(response, "text", "") or "").strip()
    except ValueError:
        # MAX_TOKENS 等で parts が空の場合、response.text は ValueError を送出する
        text = ""
    parsed = extract_json(text)
    if parsed is None:
        finish_reason = _get_finish_reason(response)
        truncated = _is_max_tokens_truncated(response)
        detail = f", {_format_token_usage(response)}" if truncated else ""
        print(
            f"⚠️ Gemini応答のJSON解析失敗 "
            f"(finish_reason={finish_reason or 'unknown'}, len={len(text)}{detail})"
        )
        if salvage and truncated:
            recovered = card_salvage.salvage_truncated_json(text)
            recovered = recovered if isinstance(recovered, dict) else {}
            # `setdefault` では足りない: Gemini が `"rows": null` を**完結した値
            # として**出していれば救出結果に null が残り、下の len() が
            # TypeError になる。そうなると救えたはずの行欠け payload が消えて
            # 兜底 → `_page_error` → 保持 → 再試行の環に戻ってしまう。
            if not isinstance(recovered.get("rows"), list):
                recovered["rows"] = []
            recovered[card_salvage.SALVAGED_KEY] = True
            print(f"🩹 截断応答から {len(recovered['rows'])} 行を回収しました"
                  f"（券面申告: {recovered.get('rows_on_page', '?')} 行）")
            return recovered
    return parsed


def _call_gemini_parts(contents, line_mode=False):
    """Gemini を呼んで JSON を返す、**唯一の**出入口。

    `line_mode`（逐行記帳 doc_type の旗）は 2 つのことを同時に決める:
    出力予算を BULK にすることと、截断応答をサルベージすることである。
    **この 2 つは不可分**（予算だけ上げても救えない頁は救えないままだし、
    サルベージだけ有効にすると本来切れない頁まで部分結果で記帳しかねない）。

    その不可分性を各呼出変体に手で守らせると、4 つ目の変体を足した人が
    片方だけ書いて緑になる —— CLAUDE.md が記録する ENTRY_BUILDERS 未登録
    事故と同じ「登録漏れ」の構造で、露見の仕方まで同じ（截断頁が黙って
    兜底 → `_page_error` → ファイル保持 → 3 秒ごとの永久再試行）。
    だから対はここ 1 箇所にしか書かない。変体は contents を組むだけ。

    BULK が 0／既定値のときは予算だけ既定へ縮退する（サルベージは有効なまま）。
    """
    response = _generate_content_with_retry(
        contents,
        generation_config=_line_generation_config() if line_mode else None)
    return _parse_gemini_response(response, salvage=line_mode)


def _call_gemini_text(ocr_text, prompt, line_mode=False):
    """OCR テキストを Gemini に送って構造化データを抽出"""
    full_prompt = f"{prompt}\n\n--- OCRテキスト ---\n{ocr_text}"
    return _call_gemini_parts([full_prompt], line_mode)


def _call_gemini_bytes(file_data, mime_type, prompt, line_mode=False):
    """Gemini API を呼び出して JSON を返す (フォールバック用)"""
    return _call_gemini_parts(
        [
            {"mime_type": mime_type, "data": file_data},
            prompt,
        ],
        line_mode)


def _call_gemini_cross_validate(ocr_text, file_data, mime_type, prompt,
                                line_mode=False):
    """Strategy C: OCR テキストと原画像の両方を Gemini に送信"""
    cross_prompt = (
        f"{prompt}\n\n"
        f"--- 参考: OCR認識テキスト (誤認識の可能性あり、画像と照合して修正してください) ---\n"
        f"{ocr_text}\n"
        f"--- OCRテキスト終了 ---\n\n"
        f"上記のOCRテキストは参考情報です。画像の内容を直接確認し、"
        f"OCRテキストに誤りがあれば画像を優先してください。"
    )
    return _call_gemini_parts(
        [
            {"mime_type": mime_type, "data": file_data},
            cross_prompt,
        ],
        line_mode)


def _call_gemini(file_path, prompt, line_mode=False):
    with open(file_path, "rb") as f:
        file_data = f.read()
    mime_type = _get_mime_type(file_path)
    return _call_gemini_bytes(file_data, mime_type, prompt, line_mode=line_mode)


class PdfSplitError(Exception):
    """PDF を頁単位に分割できない（**初回 yield 前**の失敗に限る）。

    契約（消費側 `process_pipeline` がこれに依存している）:
      ・この例外は `_split_pdf_pages` が **1 頁も yield していない**時点で
        しか投げてはならない。頁単位の中途失敗は握って `continue` する。
      ・理由: 消費側は `next(page_gen, None)` だけを try で囲む。1 頁でも
        yield した後に例外が出ると `for page_info in chain(...)` の外へ
        抜け、`process_pipeline` の最外 except に吞まれて**残頁も補填占位も
        消える**（Plan §13.0 H-2/H-3）。

    Attributes:
        total_pages: 判明していれば宣言頁数。**全頁書出失敗のときだけ**埋まる
            （pypdf 未導入・読取失敗ではそもそも数えられないので None）。
            消費側が占位の `total_pages` に使う —— ここを落とすと 20 頁の
            PDF が全滅しても進捗タブに「1/1」と出て、単頁文書の失敗と
            区別が付かなくなる（`page_progress.py:311` の `seen/total` 表示）。
    """

    def __init__(self, message, total_pages=None):
        super().__init__(message)
        self.total_pages = total_pages


def _split_pdf_pages(file_path):
    """PDF を 1ページずつ yield するジェネレータ（メモリ節約）。

    頁 i の取り出しに失敗しても**その頁だけ飛ばして継続**する（IP-401
    §12.1①）。以前は `try` が for ループ全体を包んでいたため、20 頁の
    3 頁目が壊れると 3〜20 の 18 頁が丸ごと失われ、しかも消費側からは
    「PDF が本当に 2 頁だった」と区別が付かなかった。

    Raises:
        PdfSplitError: pypdf 未導入 / PDF を開けない / 頁数を数えられない /
            多頁と分かっているのに 1 頁も取り出せなかった。契約と理由は
            `PdfSplitError` の docstring 参照（二重管理を避けるためここには
            条件だけ並べる）。呼出側は尾段（ファイル全体を 1 回の Gemini
            呼出へ送る経路）へ落とさず `_page_error` を出すこと（§12.1②）。
    """
    if PdfReader is None or PdfWriter is None:
        raise PdfSplitError("pypdf未導入のためPDFをページ分割できません")

    try:
        reader = PdfReader(file_path)
        total_pages = len(reader.pages)
    except Exception as e:
        raise PdfSplitError(f"PDF読取失敗: {e}") from e

    if total_pages <= 1:
        return  # 正常な単頁 PDF。従来どおり尾段へ委ねる

    base_name = os.path.splitext(os.path.basename(file_path))[0]
    produced = 0
    for i in range(1, total_pages + 1):
        try:
            page = reader.pages[i - 1]  # _VirtualList はランダムアクセス可
            writer = PdfWriter()
            writer.add_page(page)
            buf = io.BytesIO()
            writer.write(buf)
            data = buf.getvalue()
        except Exception as e:
            print(f"⚠️ p{i} のPDF分割に失敗（この頁を飛ばして継続）: {e}")
            continue
        produced += 1
        # yield は try の外に置く。消費側から throw/close された例外を
        # producer が誤って握り潰さないため（逐頁ループの next() 境界と同じ理由）
        yield {
            "page_num": i,
            "total_pages": total_pages,
            "data": data,
            "filename": f"{base_name}_p{i}.pdf",
        }
        # data / buf は次の周回で再束縛され GC 対象になる

    if produced == 0:
        # 多頁と分かっているのに 1 頁も出せなかった。黙って終わると消費側の
        # `first_page` が None になり**尾段へ落ちる** —— 多頁だと分かって
        # いながらファイル全体を 1 回の Gemini 呼出へ送る、②が塞ぐと決めた
        # 事故再現経路そのもの。まだ 1 頁も yield していないので上の契約に
        # 反しない。
        raise PdfSplitError("全ページのPDF分割に失敗しました",
                            total_pages=total_pages)


# ============================================================
# OCR テキストからのフィールド抽出（Gemini に依存しない）
# ============================================================

def _extract_date_from_ocr(ocr_text):
    """OCR テキストから日付を正規表現で抽出（Gemini より信頼性が高い）"""
    if not ocr_text:
        return None

    # ★ パターン0 (最優先): X月分（納付書等、日なし→月のみ、高亮対象）
    # 「納期限 令和8年2月10日」より先に「令和8年1月分」を検出するため最優先
    m = re.search(r'令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*分', ocr_text)
    if m:
        year = 2018 + int(m.group(1))
        return f"{year}/{int(m.group(2)):02d}"
    m = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*分', ocr_text)
    if m:
        return f"{m.group(1)}/{int(m.group(2)):02d}"

    # パターン1: 2026年1月27日, 2026年 1月27日（火）, 2026年01月10日
    # 宣伝文（終了/有効期限/まで/開始）や納期限の日付を除外し、取引日を優先
    # 請求書の支払期日は取引日ではない（Session 16 統一逐頁化で多頁請求書が
    # 本経路に新規露出したため追加。多欄レイアウトは OCR 読み順が不定で、
    # 支払期限が発行日より先に読まれると誤って仕訳日付を上書きしてしまう）
    _skip_keywords = ["終了", "有効期限", "まで", "開始", "お知らせ", "変更",
                      "ご利用ください", "ポイント", "カード", "キャンペーン",
                      "納期限", "纳期限", "提出期限", "届出期限"]
    # 支払期日系語彙は対称窓（前後50文字）ではなく「日付の前」窓のみで判定する。
    # 日本の請求書は「発行日 2026年3月1日 支払期限 2026年3月31日」のように
    # 発行日が支払期限より先に書かれるのが定石（ラベル→日付の順）。対称窓の
    # ままだと発行日側の後方50文字に「支払期限」が入ってしまい発行日まで
    # 誤ってスキップされ、全滅後のフォールバックで支払期限日付を誤採用して
    # しまう（codex review 発見）。前方窓のみにすることで、支払期限自身の
    # 直前ラベルは引き続き検知しつつ、先に書かれた発行日は巻き込まない。
    _due_date_prefix_keywords = ["支払期限", "支払期日", "お支払期限", "お支払期日",
                                 "振込期限", "振込期日"]
    # フォールバック候補プール: _skip_keywords（対称窓）で skip された日付は
    # 従来通りフォールバック対象に残す（receipt 生産調校済みの既存挙動）。
    # 一方 _due_date_prefix_keywords（支払期限等）で skip された日付は
    # 取引日ではないと判定済みなので、プールから完全に除外する（codex Round 3
    # 発見: 従来は matches_p1[-1] がフィルタ前の生マッチ一覧を見ていたため、
    # OCR テキストに支払期限しか無いケースで支払期限日付がそのまま
    # フォールバック採用され、Gemini の正しい発行日を上書きしていた）。
    matches_p1 = list(re.finditer(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', ocr_text))
    fallback_pool_p1 = []
    prev_end_p1 = 0  # 直前マッチの終端。前方窓が前の日付のラベルまで
    # 誤って跨がないよう、prefix 窓の起点を prev_end で切り詰める
    # （「支払期限 A 発行日 B」で B の前方窓に A のラベルが混入し
    # B まで誤って支払期日扱いされるのを防ぐ）。
    for m in matches_p1:
        ctx = ocr_text[max(0, m.start()-50):min(len(ocr_text), m.end()+50)]
        if any(kw in ctx for kw in _skip_keywords):
            fallback_pool_p1.append(m)
            prev_end_p1 = m.end()
            continue
        ctx_prefix = ocr_text[max(0, m.start()-50, prev_end_p1):m.start()]
        if any(kw in ctx_prefix for kw in _due_date_prefix_keywords):
            prev_end_p1 = m.end()
            continue
        return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    if fallback_pool_p1:
        m = fallback_pool_p1[-1]
        return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"

    # パターン2: 26年 1月14日 (西暦下2桁) or 8年 1月7日 (令和1桁)
    # 納期限等の非取引日を除外し、発行日・取引日を優先
    # （支払期日系語彙の前方窓判定はパターン1と同一の理由で一貫して適用する）
    # パターン1 と同一方針: due_date 系は skip されたら完全に除外、
    # 対称窓 skip 系は従来通りフォールバックプールに残す。
    matches_p2 = list(re.finditer(r'(?<!\d)(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', ocr_text))
    fallback_pool_p2 = []
    prev_end_p2 = 0  # パターン1と同一理由の前方窓切り詰め
    for m in matches_p2:
        ctx = ocr_text[max(0, m.start()-50):min(len(ocr_text), m.end()+50)]
        if any(kw in ctx for kw in _skip_keywords):
            fallback_pool_p2.append(m)
            prev_end_p2 = m.end()
            continue
        ctx_prefix = ocr_text[max(0, m.start()-50, prev_end_p2):m.start()]
        if any(kw in ctx_prefix for kw in _due_date_prefix_keywords):
            prev_end_p2 = m.end()
            continue
        year = int(m.group(1))
        if year <= 10:
            year = 2018 + year
        elif year <= 99:
            year = 2000 + year
        return f"{year}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    if fallback_pool_p2:
        m = fallback_pool_p2[-1]
        year = int(m.group(1))
        if year <= 10:
            year = 2018 + year
        elif year <= 99:
            year = 2000 + year
        return f"{year}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"

    # パターン3: 2026/01/19, 2026-01-19（年は2020-2099に限定、電話番号誤検出防止）
    m = re.search(r'(20[2-9]\d)[/\-](\d{1,2})[/\-](\d{1,2})', ocr_text)
    if m:
        month, day = int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{m.group(1)}/{month:02d}/{day:02d}"

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

    # パターン6: YY-MM-DD (ATM明細の令和年号: 07-04-30 = 令和7年4月30日)
    # 「ご利用年月日」等のATMキーワード近くにある場合のみ
    if any(kw in ocr_text for kw in ["ご利用年月日", "キャッシュサービス", "ATM", "お取引"]):
        m = re.search(r'(?<!\d)(0[4-9]|1\d)-(\d{2})-(\d{2})(?!\d)', ocr_text)
        if m:
            year = 2018 + int(m.group(1))
            month, day = int(m.group(2)), int(m.group(3))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year}/{month:02d}/{day:02d}"

    return None


def _validate_gemini_date(date_str):
    """Gemini が返した日付を検証し、ゼロパディング形式に正規化する。

    OCR で抽出できなかった場合の最終チェック:
    - /00 日 → 無効
    - 年が 2020-2027 の範囲外 → 年号誤判定の可能性
    - パースできない → 無効

    正規化:
    - `2024/2/24` → `2024/02/24`
    - `2024/2` → `2024/02`（月のみ、納付書等）
    """
    if not date_str:
        return ""
    s = str(date_str).strip()
    # YYYY/M or YYYY/MM 形式（月のみ、納付書等）
    m = re.match(r'^(\d{4})/(\d{1,2})$', s)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if year < 2020 or year > 2027 or month < 1 or month > 12:
            return ""
        return f"{year}/{month:02d}"
    m = re.match(r'^(\d{4})/(\d{1,2})/(\d{1,2})$', s)
    if not m:
        return ""  # パースできない → 空
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if day == 0:
        return ""  # /00 → 空
    if year < 2020 or year > 2027:
        return ""  # 2014年等 → Gemini の年号誤判定、空にする
    if month < 1 or month > 12 or day > 31:
        return ""
    return f"{year}/{month:02d}/{day:02d}"


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


# ── 不要ページ検出（封筒・送付状・裏面メモ・挨拶状）──
_ENVELOPE_KEYWORDS = ["郵便", "切手", "〒", "封筒", "差出人", "親展", "書留", "速達", "配達証明", "御中"]
_COVER_LETTER_KEYWORDS = ["送付状", "送り状", "ご査収", "同封", "送付いたします", "お届けいたします"]
_GREETING_KEYWORDS = ["よろしくお願い", "お世話になっております", "ご挨拶", "拝啓", "敬具", "謹啓"]
_FINANCIAL_KEYWORDS = ["領収", "請求", "合計", "小計", "税込", "お買上", "￥", "¥", "円",
                       "金額", "振込", "納付", "支払"]
_PAMPHLET_KEYWORDS = ["制度", "仕組み", "チャート", "についてのご案内", "とは？", "とは?",
                      "処分の流れ", "についての留意", "についてのお知らせ"]


# PaddleOCR が日本語漢字を簡体字に取り違えたときの照合用写像（IP-401 T3）。
# **一方向・比較専用**。表示や Sheets へ書き出すテキストには絶対に適用しない
# （canonical direction が無い双方向置換は元テキストを壊す）。
# 実事故: 「☆領収証☆」→「☆领収证☆」で構造キーワード「領収」に失配した。
#
# 収録基準は「本番で実際に観測された誤読」のみ。憶測で簡体字全域を畳むと
# 別語に化けて誤分類を増やすため広げない。纳/录 は本ファイル内の既存の
# 場当たり対処（_extract_date_from_ocr の "纳期限"、_extract_invoice_num_from_ocr
# の 登[録录]番号）が、同じ誤読が既に観測済みであることを示している。
_SIMPLIFIED_TO_JP = {
    "领": "領",
    "证": "証",
    "收": "収",
    "请": "請",
    "买": "買",
    "计": "計",
    "纳": "納",
    "录": "録",
}

_SIMPLIFIED_TRANS = str.maketrans(_SIMPLIFIED_TO_JP)


def _normalize_for_keyword_match(text):
    """キーワード照合専用にテキストを正規化する（比較用、表示用ではない）。

    NFKC で全角英数字・記号を畳んだうえで、PaddleOCR がよく取り違える簡体字を
    日本語字形へ一方向に寄せる。写像表に無い文字は触らない（過剰変換すると
    別語に化けて誤分類を生む）。
    """
    return unicodedata.normalize("NFKC", text or "").translate(_SIMPLIFIED_TRANS)


def _is_envelope_page(ocr_text, raw_data):
    """不要ページ（封筒・送付状・裏面メモ・挨拶状・説明書）を検出する。

    best-effort の分類器であり、これ単体を票の採否に使ってはいけない
    （IP-401: 本関数の単独否決が本番で1票を無音欠落させた）。現在の役割は
    「entries を組めなかったページ」を監査タブ行きにするか赤い認識不能行に
    するかの理由分類のみ（Plan §3.1）。

    照合は全キーワード群を同一の normalized_text に対して行う。片側だけ
    正規化すると同じ誤認識が判定の一方にしか効かず一貫性を欠く（§3.4）。

    NOTE: 空白ページや documents=[] は認識不能として扱い、ここでは除外しない。
    """
    if not ocr_text:
        return False

    text_lower = _normalize_for_keyword_match(ocr_text).replace(" ", "")
    has_financial_kw = any(kw in text_lower for kw in _FINANCIAL_KEYWORDS)

    # 封筒: 封筒キーワードあり + 金額関連なし
    if any(kw in text_lower for kw in _ENVELOPE_KEYWORDS) and not has_financial_kw:
        return True

    # 送付状: 送付状キーワードあり + 金額関連なし
    if any(kw in text_lower for kw in _COVER_LETTER_KEYWORDS) and not has_financial_kw:
        return True

    # 挨拶状: 挨拶キーワードあり + 金額関連なし
    if any(kw in text_lower for kw in _GREETING_KEYWORDS) and not has_financial_kw:
        return True

    # 説明書・パンフレット: 説明キーワードあり + 金額関連なし
    if any(kw in text_lower for kw in _PAMPHLET_KEYWORDS) and not has_financial_kw:
        return True

    # 強化パンフレット検出: 長文(500字超) + 複数パンフレットキーワード → 財務用語があっても除外
    # (例: 放置違反金制度の説明チラシは「納付」「領収書」を含むが、領収書ではない)
    clean_text = re.sub(r'\s+', '', ocr_text)
    pamphlet_hits = sum(1 for kw in _PAMPHLET_KEYWORDS if kw in text_lower)
    if len(clean_text) > 500 and pamphlet_hits >= 2:
        return True

    # 裏面メモ/カード控え裏面/手書きメモ: 短いテキスト（60文字未満）+ 領収書構造なし
    # かつては「正式な領収書は必ず構造キーワードを持つ」と断定していたが、
    # IP-401 の事故（小型サーマル領収証が「领収证」と誤読され構造キーワードに
    # 失配、55文字でメモ扱い）がこれを反証した。best-effort のヒューリスティック
    # であり、閾値 60 にも強い根拠は無い（Plan §3.6）。
    _receipt_structure = ["領収", "請求書", "合計", "小計", "お買上"]
    has_receipt_structure = any(kw in text_lower for kw in _receipt_structure)
    if len(clean_text) < 60 and not has_receipt_structure:
        return True

    return False


# ── 社会保険料通知書の検出（IP-401 T6 / Plan §3.8）──
# 「保険料納入告知額・領収済額通知書」は券面に当月の納入告知額と前月の
# 領収済額の2口が印字されており、Gemini は両方を仕訳化してしまう（顧客の表に
# 実在した誤り: 319,000円 × 2行）。
#
# 社員からの共通ルール宣言（2026-07-30、最優先）:
#   「社会保険料に関する会計処理はアップロードせずに口座振替資料として処理する」
# 口座振替側で既に記帳される以上、SS 側が当月分を1行作れば二重計上になる。
# よって**仕訳は一切作らない**。
#
# 封筒判定との違い（意図的）:
#   封筒     = ヒューリスティック。誤爆が怖いので適用範囲を絞る（§3.5）
#   社会保険 = 確定した業務ルール。全 doc_type・全経路で常時有効にする。
#              顧客が「今後スキャンしない」と述べてもコードはそれに依存しない
#              （§7-5 ユーザー裁定: 「你不能去賭它掃不掃」）
#
# 誤爆の代償は「静かに消える」より重い。仕訳が0件になるうえ MF タブに
# 「社会保険料通知書です」という**断定的に誤ったラベル**が顧客向けに書かれる。
# よって単独で発火させるのは社会保険に固有の複合語だけに限る。
# 特に「納入告知額」は単独では発火させない——「納入告知書/納入告知額」は
# 日本の公的機関の徴収通知に広く使われる一般語であり、労働保険料（労働局）等
# 顧客ルールの対象外の文書まで巻き込む。必ず機関名・保険種別と共起させる。
_SOCIAL_INSURANCE_STRONG = ["保険料納入告知額", "領収済額通知書"]
# 単独では別文書（年金定期便・各種お知らせ・労働保険）に誤爆するため
# 組み合わせで判定する。ペア内の全語が揃って初めて成立。
_SOCIAL_INSURANCE_WEAK_PAIRS = [
    ("日本年金機構", "保険料"),
    ("納入告知額", "厚生年金"),
    ("納入告知額", "健康保険"),
    ("納入告知額", "日本年金機構"),
]

# Gemini 側の取引先名による交差確認。PaddleOCR がキーワードを読み崩しても
# （改行分断・写像表外の異体字）Gemini が機関名を拾えていれば救える。
#
# ただし取引先名**単独では成立させない**。年金機構は通知書以外の文書
# （年金定期便・各種お知らせ）も送ってくるため、単独条件にすると
# 「日本年金機構からのお知らせ」——キーワード判定が意図的に弾いている文書——が
# Gemini の vendor だけで吞まれてしまう。必ず OCR 側の裏付けと組み合わせる。
_SOCIAL_INSURANCE_VENDORS = ["日本年金機構", "年金事務所"]
# 取引先名の裏付けとして OCR 側に最低限必要な語（単独では弱すぎる語群）
_SOCIAL_INSURANCE_CORROBORATION = ["保険料", "納入告知", "領収済額"]

SOCIAL_INSURANCE_REASON = "social_insurance_notice"
SOCIAL_INSURANCE_MEMO = (
    "⚠ 社会保険料通知書です。口座振替資料として処理してください"
    "（仕訳は作成していません）"
)

# 除外ページの留痕先。理由（なぜ除外したか）と行き先（どこに書くか）は別の
# 関心事なので、理由文字列から行き先を推測させない。R2 の先例（分岐記録に
# _exclude_reason を流用せず _audit_signal を新設した）と同じ方針。
EXCLUDE_DEST_AUDIT_TAB = "audit_tab"   # 封筒等。MF 区を汚さず監査タブへ
EXCLUDE_DEST_MF_TAB = "mf_tab"         # 運用ルール違反の通知。顧客が必ず見る場所へ


def _is_social_insurance_notice(ocr_text, raw_data=None):
    """社会保険料の納入告知額・領収済額通知書を検出する。

    日本年金機構の定型書式なので券面キーワードが主判定。照合は §3.4 の
    正規化を共用し PaddleOCR の簡体字誤読に耐える。

    加えて Gemini の取引先名でも交差確認する。OCR だけに頼ると、キーワードが
    改行で分断されたり写像表外の異体字で崩れたときに見逃し、社会保険料の
    仕訳が作られて口座振替側と二重計上になる（§3.8 が防ぎたい事態そのもの）。
    Strategy C の交差検証の精神をここでも使う。
    """
    # 空白は「全部」落とす。PaddleOCR は券面のレイアウトどおりに改行を入れるので
    # 「保険料納入\n告知額」のようにキーワードが行またぎで分断されうる。半角空白
    # だけ落とす実装だと、まさにその分断で検出を取りこぼし、禁止されている
    # 社会保険料の仕訳が生成されて口座振替側と二重計上になる。
    text = re.sub(r'\s+', '', _normalize_for_keyword_match(ocr_text))
    if any(kw in text for kw in _SOCIAL_INSURANCE_STRONG):
        return True
    if any(all(kw in text for kw in pair)
           for pair in _SOCIAL_INSURANCE_WEAK_PAIRS):
        return True

    # 取引先名は「裏付け」であって単独の陽性シグナルではない（上のコメント参照）
    return (_has_social_insurance_vendor(raw_data)
            and any(kw in text for kw in _SOCIAL_INSURANCE_CORROBORATION))


def _has_social_insurance_vendor(raw_data):
    """Gemini が返した取引先名が年金機構系かを見る（交差確認用）。"""
    if not isinstance(raw_data, dict):
        return False
    vendors = [raw_data.get("vendor", "")]
    for doc in raw_data.get("documents") or []:
        if isinstance(doc, dict):
            vendors.append(doc.get("vendor", ""))
    for vendor in vendors:
        if not vendor:
            continue
        normalized = re.sub(r'\s+', '', _normalize_for_keyword_match(vendor))
        if any(kw in normalized for kw in _SOCIAL_INSURANCE_VENDORS):
            return True
    return False


def _extract_partial_data(raw_data):
    """認識不能ページから部分データ（日付・取引先）を抽出"""
    date = ""
    vendor = ""
    if isinstance(raw_data, dict):
        docs = raw_data.get("documents", [])
        if docs:
            date = docs[0].get("date", "") or ""
            vendor = docs[0].get("vendor", "") or ""
        if not date:
            date = raw_data.get("date", "") or ""
        if not vendor:
            vendor = raw_data.get("vendor", "") or ""
    return date, vendor


# ── 取引先名に基づく科目兜底ルール ──
_VENDOR_ACCOUNT_OVERRIDE = {
    # スーパー・日用品店 → 備品・消耗品費
    "フードウェイ": "備品・消耗品費",
    "ダイソー": "備品・消耗品費",
    "セブン-イレブン": "備品・消耗品費",
    "セブンイレブン": "備品・消耗品費",
    "ローソン": "備品・消耗品費",
    "ファミリーマート": "備品・消耗品費",
    "FamilyMart": "備品・消耗品費",
    "エディオン": "備品・消耗品費",
    # メガドン・キホーテ。裸の "MEGA" は casefold 後に omega/mega 系店名を誤爆するため収窄
    "MEGAドン": "備品・消耗品費",
    # 飲食店・弁当店 → 接待交際費
    "Mister Donut": "接待交際費",
    "ミスタードーナツ": "接待交際費",
    "Hotto Motto": "接待交際費",
    "ほっともっと": "接待交際費",
    "マクドナルド": "接待交際費",
    "スターバックス": "接待交際費",
    "STARBUCKS": "接待交際費",
    "ラーメン": "接待交際費",
    "グリル": "接待交際費",
    "Diner": "接待交際費",
    # 駐車場 → 旅費交通費
    "パーキング": "旅費交通費",
    "タイムズ": "旅費交通費",
    "コインパーク": "旅費交通費",
    # 高速道路 → 旅費交通費
    "NEXCO": "旅費交通費",
    "高速道路": "旅費交通費",
    # タクシー → 旅費交通費
    "タクシー": "旅費交通費",
    # 空港 → 旅費交通費
    "空港": "旅費交通費",
    # ゴルフ用品店は物販 → 備品・消耗品費（先勝ちのため除外項を一般語より前に置く）
    "ゴルフ5": "備品・消耗品費",
    "GOLF5": "備品・消耗品費",
    "ゴルフパートナー": "備品・消耗品費",
    "GOLF Partner": "備品・消耗品費",
    "つるやゴルフ": "備品・消耗品費",
    "ゴルフ用品": "備品・消耗品費",
    # ゴルフ場 → 接待交際費（6/11 顧客回答）
    "ゴルフ": "接待交際費",
    "カントリークラブ": "接待交際費",
    "カンツリー": "接待交際費",
    "GOLF": "接待交際費",
}

def _normalize_vendor_key(text):
    """ベンダー照合用の正規化（NFKC + casefold + 空白除去）。

    OCR は「ゴルフ５」のような全角数字・半角カナや、「GOLF 5」のように
    語中へ空白を挟んだ店名を返すことがあるため、全角半角・大文字小文字・
    空白をすべて無視して照合する。
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


# キーワードはモジュール定数のため、照合用正規化をロード時に1回だけ前計算する
_VENDOR_ACCOUNT_OVERRIDE_NORMALIZED = [
    (_normalize_vendor_key(keyword), account)
    for keyword, account in _VENDOR_ACCOUNT_OVERRIDE.items()
]


def _override_account_by_vendor(vendor, gemini_account):
    """取引先名に基づいて科目を強制上書きする。Gemini の分類揺れを防止。

    照合は _normalize_vendor_key（NFKC・casefold・空白除去）で行う。
    辞書の定義順で最初に命中したキーワードを無条件で採用する（厳格な先勝ち）。
    GOLF5 のような除外項は一般キーワード（GOLF 等）より前に定義すること。
    """
    if not vendor:
        return gemini_account
    vendor_norm = _normalize_vendor_key(vendor)
    for keyword_norm, forced_account in _VENDOR_ACCOUNT_OVERRIDE_NORMALIZED:
        if keyword_norm in vendor_norm:
            return forced_account
    return gemini_account


def _should_override_date(gemini_date, ocr_date):
    """OCR 日付で Gemini 日付を上書きすべきか判定する。
    Gemini が近年の有効日付を持ち、OCR がそれより未来の年を返した場合は
    OCR が宣伝日付を誤抽出した可能性が高いため上書きしない。
    """
    if not gemini_date or not ocr_date:
        return bool(ocr_date)
    try:
        g_year = int(gemini_date.split("/")[0])
        o_year = int(ocr_date.split("/")[0])
        # Gemini が近年の日付（>= 2024）で OCR が未来の年 → 宣伝日付の可能性大
        if g_year >= 2024 and o_year > g_year:
            return False
    except (ValueError, IndexError):
        pass
    return True


# OCR 主導の日付・T番号上書きを**当ててはいけない** doc_type（AD-3。T7）。
#
# この上書きは領収書の券面を前提にしている。カード明細に当てると:
# - **日付**: `_extract_date_from_ocr` の `_skip_keywords` に「カード」「ポイント」が
#   入っており、カード明細ではほぼ全ての日付が skip → `fallback_pool[-1]`
#   ＝「頁面で最後に読まれた日付」が採用される。逐行記帳では行ごとの利用日が正で、
#   doc 級のその値は T6 が行級化した B列 の**回帰先**を汚す
# - **T番号**: 券面の登録番号は**カード会社自身**のもの（F-11）。カード明細は
#   適格請求書に該当せず（F-14）、行ごとの加盟店登録番号は構造上存在しない
#
# `card_prompts` の出力 JSON には doc 級 `date` / `invoice_num` が**そもそも無い**
# のに、下の上書きはキーの有無を問わず代入する。プロンプト側の改名では防げない。
#
# **`LINE_MODE_DOC_TYPES` の別名にしない**。「逐行記帳である」と「券面の日付/
# 登録番号が doc 級に存在しない」は**別の軸**である（T6 が抑制表について下したのと
# 同じ裁決）。追随漏れは `test_ocr_engine_ocr_override_exempt` の台帳が塞ぐ。
_OCR_OVERRIDE_EXEMPT_DOC_TYPES = frozenset({
    DocType.CREDIT_CARD,
    DocType.TRANSIT_IC,
})


def _apply_ocr_overrides(doc_type, raw_data, ocr_text, prefix=""):
    """OCR テキストから抽出した日付・T番号で Gemini の結果を上書きする。

    Gemini は日付の年号解釈を間違えやすい（26年→2014年等）が、
    PaddleOCR のテキストから正規表現で抽出すれば確実。
    ただし Gemini が有効な日付を持つ場合、OCR が宣伝日付を誤抽出した可能性があるため
    年が大きく異なる場合は上書きしない。

    Args:
        doc_type: **この頁を解析した種別**（`PageOcr.actual_doc_type`）。
            上書きの可否は「その頁の券面がどういう書類か」で決まるので
            folder doc_type ではなく actual が正しい。
            **既定値を付けてはいけない** —— 書き忘れた新しい呼出側が黙って
            上書き経路へ落ちる footgun になる（`SignatureTest` が固定）。
    """
    if not raw_data:
        return

    # 豁免判定は空判定の**後**に置く。先に置くと豁免 doc_type のときだけ
    # truthy な非 dict が素通りし、非豁免経路（`raw_data.get()` が
    # AttributeError）と扱いが非対称になる（実害は無いが理由も無い）。
    if doc_type in _OCR_OVERRIDE_EXEMPT_DOC_TYPES:
        return

    ocr_date_raw = _extract_date_from_ocr(ocr_text) if ocr_text else None
    # OCR 抽出日付も検証する（2008年等の誤抽出を排除）
    ocr_date = _validate_gemini_date(ocr_date_raw) if ocr_date_raw else None
    ocr_tnum = _extract_invoice_num_from_ocr(ocr_text) if ocr_text else None

    # documents 配列がある場合（領収書新フォーマット）
    if isinstance(raw_data, dict) and "documents" in raw_data:
        docs = raw_data.get("documents", [])
        is_multi_doc = len(docs) > 1
        for doc in docs:
            if ocr_date:
                gemini_date = doc.get("date", "")
                gemini_normalized = _validate_gemini_date(gemini_date)
                # 複数書類ページ: Gemini が有効な日付を持つ場合は上書きしない
                # （OCR はページ全体から1つの日付しか抽出できず、書類ごとの日付を区別できない）
                if is_multi_doc and gemini_normalized:
                    doc["date"] = gemini_normalized
                elif _should_override_date(gemini_date, ocr_date):
                    if gemini_date != ocr_date:
                        print(f"{prefix}📅 日付上書: Gemini={gemini_date} → OCR={ocr_date}")
                    doc["date"] = ocr_date
                else:
                    print(f"{prefix}📅 OCR日付を無視（Gemini={gemini_date}, OCR={ocr_date}）")
                    doc["date"] = gemini_normalized or gemini_date
            else:
                # OCR 抽出できなくても無効日付は修正
                validated = _validate_gemini_date(doc.get("date", ""))
                if not validated:
                    print(f"{prefix}⚠️ 日付不明: Gemini={doc.get('date','')} → 空欄（要確認）")
                doc["date"] = validated
            if ocr_tnum and not is_multi_doc:
                # T番号も複数書類ページでは上書きしない（書類ごとにT番号が異なるため）
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
                    "amount": 品目の票面金額(数値。内税/外税での扱いは下記判定基準参照),
                    "tax_rate": 0.08 or 0.10 or 0,
                    "tax_included": true or false (内税=true / 外税=false。下記判定基準参照),
                    "tax_amount": 消費税額(数値, なければ0),
                    "debit_account": "費用の勘定科目を推定"
                }
            ],
            "tax_summary": [
                {
                    "tax_rate": 0.08 or 0.10 or 0,
                    "tax_included": true or false,
                    "base_amount": その税率区分の対象額(数値),
                    "tax_amount": その税率区分の消費税額(数値, なければ0),
                    "label": "非課税/対象外行(tax_rate=0)のみ票面の名目（例: \"軽油税\", \"ゴルフ場利用税\", \"非課税金額\"）。課税行は空文字"
                }
            ],
            "total_amount": 票面に印字された税込の合計金額(「合計」「お買上計」等, 数値。印字が無ければ 0)
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
- 飲食店・レストラン・居酒屋・カフェ・バー・ドーナツ店・弁当店・ベーカリー等での飲食代 → "接待交際費"
- デパ地下・百貨店催事場での食品購入 → "接待交際費"
- 美容院・サロン・エステ・整体・鍼灸等の施術代 → "接待交際費"
- ホテル・施設のフロントサービス・スタジオ利用・イートイン飲食 → "接待交際費"（"地代家賃"にしないこと）
- ガソリンスタンド・駐車場・高速道路料金 → "旅費交通費"
- レンタカー → "旅費交通費"
- タクシー → "旅費交通費"
- 物品購入（玩具・景品・日用品・電子部品・工具・消耗材等）→ "備品・消耗品費"（"未払金"にしないこと）

【tax_rate の判定基準】
最優先: レシートに税率(8%, 10%)や「※」「軽」マークが印字されていればそれに従う。
印字がない場合は以下のルールで判定:
- 0.10（デフォルト）: ほとんどの品目。外食(店内飲食), 酒類, 日用品, サービス, 交通費等
- 0.08: レシートに軽減税率マーク(※/軽/8%)がある飲食料品のみ。外食は対象外
- 0: 銀行振込本体(bank_transfer)、および消費税の課税対象外・非課税の品目。
  例: ゴルフ場利用税・宿泊税・入湯税・収入印紙・印紙代・行政手数料・軽油税。
  レシートの税率別内訳に「非課税金額」「対象外」として載る品目はこれに該当する
  （品目側の「＊」「※」マークが軽減税率ではなく非課税を示すPOSもあるため、内訳行を優先）
  ガソリンスタンドのレシートでは軽油税が「(内軽油税 @15.0 ¥493)」のように明細内に
  印字される場合がある。これは非課税（対象外）の独立した品目行として出力し
  （tax_rate=0、tax_included=true）、10%課税グループに含めないこと。
  このとき燃料品目側の金額は軽油税を除いた本体額に分解し、二重計上しないこと
  （例: 軽油 ¥5,000・内軽油税 ¥493 → 品目「軽油」¥4,507(10%) と「軽油税」¥493(0%)
  の2行。全品目の金額合計が票面の合計金額と一致すること）。
  軽油税品目の debit_account は "租税公課"（燃料本体は "旅費交通費" のまま）
迷ったら 0.10 を使用してください

【tax_included（内税/外税）の判定基準】
- true（内税・デフォルト）: 票面の品目金額が税込で、消費税が「(内消費税等 ¥…)」
  「○%内税対象額/内税額」のように括弧書き・内訳表示されるレシート。
  amount は税込額、tax_amount は内訳の消費税額（表示があれば）
- false（外税）: 票面が税抜価格で、消費税が「○%外税額」「消費税」等の独立行で
  合計に加算されるレシート（品目に「外」マークが付くことが多い）。
  amount は税抜の印字額のまま、tax_amount はレシート印字の消費税額。
  品目単位の税額が不明な場合は、同じ税率グループの税額合計をそのグループの
  いずれか1品目の tax_amount に付与し、他の品目は 0 とする
- 同一レシート内で内税と外税が混在する場合（例: 外税の商品+税込のレジ袋）は、
  品目ごとに正しく true/false を付けること

【tax_summary（税率別内訳）の抽出 — 最重要】
レシートに税率別の内訳が印字されている場合は、それを必ず tax_summary に
そのまま転記してください（印字値を再計算しない）。
- 1つの税率区分（税率 × 内税/外税）につき1要素。
- base_amount = その区分の「対象額」:
  ・内税表示（"8%内税対象額 ¥124"、"(10%対象 ¥2,400 消費税等 ¥218)"）→ tax_included=true、
    base_amount=税込対象額（124 / 2400）、tax_amount=その消費税額
  ・外税表示（"(8%外税対象額) ¥1,620 / 8%外税額 ¥130"、"10%外税 タイショウ ¥1,132 / 10%外税 ¥113"）
    → tax_included=false、base_amount=税抜対象額（1620 / 1132）、tax_amount=外税額（130 / 113）
  ・非課税/対象外（"非課税金額 ¥500"、"ゴルフ場利用税 ¥200"、"軽油税 ¥493"）→ tax_rate=0、tax_included=true、
    base_amount=その金額、tax_amount=0、label=票面の名目（"軽油税"・"ゴルフ場利用税"・"非課税金額" 等）
- 税率別内訳が印字されていないレシート（単純な品目羅列のみ）は tax_summary=[] とする。
  内訳行を品目(items)に混ぜないこと。
- 税率%（例「10%」「消費税10%」「内消費税10%」）だけが印字され、その区分の
  「対象額」の金額が票面に印字されていない場合は、その区分の行を tax_summary に
  起こさないこと。税抜・対象額を税率から逆算（税込÷1.1 等）して base_amount に
  詰めてはならない。対象額の金額印字が無い税率表記は無視し、品目(items)の金額と
  total_amount を正とする。

【total_amount（票面合計）の抽出】
- 票面に印字された税込の合計金額（「合計」「お買上計」「領収金額」等）をそのまま転記する（items から再計算しない）
- 必ず「税率別内訳（tax_summary）の対象額の税込合計」と一致する金額を選ぶこと
  （この金額は仕訳の各行金額の合計と照合されるため、行と食い違う額を入れない）
- 値引・割引・ポイント・商品券の扱いは上記原則で判断する:
  ・ポイント・商品券・クーポンを「支払い手段」として充当した場合
    （税率別内訳の対象額は減らない）→ 充当前の商品合計（「お買上計」「小計」）を転記する
  ・値引・割引が税率別内訳の対象額そのものを減額している場合
    （内訳が値引後の対象額を表示）→ 値引後の合計（票面の「合計」）を転記する
- お預り・お釣り・現金（お預り金額）と混同しないこと
- 外税レシートでは税抜小計ではなく、消費税加算後の合計金額を使う
- 合計の印字が無い場合は 0 とする

【payment_method の判定基準】
- コンビニ（FamilyMart, セブンイレブン等）での支払い → "現金"
- SMCC(QQ), QUICPay, iD 等の電子決済表記がある場合でも、
  コンビニ払いの振込手数料であれば → "現金"（代収扱い）
- 銀行ATMでの振込 → "ATM"
- 銀行窓口での振込 → "振込"
- クレジットカード明細 → "クレジットカード"

【date の取得方法（重要）】
- 取引日（お買上日・取扱日・発行日）を抽出してください
- 販促文・お知らせ・カード有効期限・キャンペーン期間などの日付は無視してください
- 「〜まで」「〜終了」「有効期限」「〜開始」の前後にある日付は取引日ではありません
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
- 封筒（郵便封筒・切手のみの画像）は書類ではありません。封筒が検出された場合は documents を空配列 [] で返してください
- items には個別の品目のみ含めてください。小計・合計・税込合計・「10%対象」等の集計行は含めないでください
- 乗車券・切符・交通チケット等は、金額が記載されていれば items に含めてください（debit_account: "旅費交通費"）
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
    """支払方法とドキュメントカテゴリから貸方科目を決定。

    **無条件で「未払金」を返す。これは意図的。** 引数 pay_method /
    doc_category は呼出側の形を変えないために受け取るだけで、参照しない。

    顧客確認済み: 領収書・請求書とも貸方は「未払金」に統一。SS は「何の費用が
    発生したか」だけを担い、「どの財布から出たか」は担わない——後者は
    MoneyForward 側で口座連携と突合して消し込む運用（社会保険料を口座振替
    資料として処理するのと同じ思想）。

    **再議しないこと（2026-07-30 ユーザー裁定:「不用改動了，員工就是這樣
    要求的」）。** payment_method が現金でも貸方が未払金になるのは仕様であり
    判定失敗ではない。`_build_entries_from_receipt_legacy`（旧フォーマット兜底）と
    `_build_entries_from_purchase_invoice` は本関数を通らず独自に現金／普通預金へ
    分岐しており統一規則と食い違うが、社員の運用がそれで成立しているため
    現状維持と裁定された。三者を揃える提案は却下済み。
    """
    return "未払金"


def _is_subtotal_line(description, amount, all_items):
    """小計・合計・税額集計行かどうかを判定"""
    # キーワード検出（品目名に含まれうる「対象」等は除外、集計行のみ）
    # 部分一致は税語を含まない集計語のみ。消費税/外税/内税の語を部分一致にすると
    # 「ゴルフ場利用税（消費税等対象外）」のような注記付き品目まで落として
    # 対象外行が欠落するため、税語の行は下の全体一致パターンでのみ除外する
    subtotal_keywords = [
        "小計", "合計",
        "課税対象額", "10%対象額", "8%対象額",
        "10%対象計", "8%対象計",
    ]
    desc_lower = description.lower()
    for kw in subtotal_keywords:
        if kw in desc_lower:
            return True

    # 税率別内訳行・独立税額行（例: "消費税", "内消費税等 ¥218", "消費税額等(10%)",
    # "8%外税額", "(8%外税対象額)", "10%外税 タイショウ", "内税計"）。
    # 行全体が「税語＋税率＋金額」だけの場合のみ除外する（注記付き品目を誤って
    # 落とさないため全体一致）。裸の税額行が品目として流れると二重計上になる
    if re.fullmatch(
        r"\s*[\(（]?\s*(?:[0-9０-９]+\s*[%％])?\s*"
        r"(?:(?:内|うち)?消費税(?:対象)?(?:額|等)*|(?:内税|外税)\s*(?:対象|タイショウ)?\s*(?:額|計)?)"
        r"\s*(?:[\(（]?\s*[0-9０-９]+\s*[%％]\s*[\)）]?)?"
        r"\s*(?:[¥￥]?\s*[0-9０-９][0-9０-９,，]*\s*円?)?\s*[\)）]?\s*",
        description,
    ):
        return True

    # 金額一致チェック: 他の品目の合計と一致する場合はスキップ
    if len(all_items) > 2:
        other_amounts = []
        for item in all_items:
            a = item.get("amount", 0)
            d = str(item.get("description", ""))
            if a and int(a) != 0 and int(a) != amount and d != description:
                other_amounts.append(int(a))
        if other_amounts and amount == sum(other_amounts):
            return True

    return False


def _build_entries_for_single_doc(doc):
    """documents配列の1要素（単一書類）から仕訳エントリを生成"""
    entries = []
    doc_category = doc.get("doc_category", "receipt")

    # 貸方科目決定
    pay_method = str(doc.get("payment_method", "現金"))
    credit_account = _determine_credit_account(pay_method, doc_category)

    # 取引先名は items が空でも参照するためループ前に取得
    vendor = doc.get("vendor", "")

    # 有効金額の品目数を事前カウント（領収証の単一合計行を誤フィルタ防止）
    valid_items = [it for it in doc.get("items", [])
                   if it.get("amount") and int(it.get("amount", 0)) != 0]

    for item in doc.get("items", []):
        amount = item.get("amount", 0)
        if not amount or int(amount) == 0:
            continue

        # 小計/合計行をスキップ（ただし有効品目が1件のみの場合はスキップしない）
        desc = str(item.get("description", "")).strip()
        if len(valid_items) > 1 and _is_subtotal_line(desc, int(amount), doc.get("items", [])):
            continue

        # tax_rate を float に正規化（null/文字列対策）。税区分と集約を整合させる
        tax_rate = coerce_tax_rate(item.get("tax_rate"))
        debit_account = item.get("debit_account", "消耗品費")

        # 取引先名に基づく科目兜底（Gemini 分類揺れ防止）。bank_transfer/
        # fee_receipt は借方科目が固定（未払金/支払手数料）のため対象外
        # （振込先がゴルフ場等でも上書きしない）
        if doc_category == "receipt":
            debit_account = _override_account_by_vendor(vendor, debit_account)

        # 税区分決定
        debit_tax_type, credit_tax_type = determine_tax_types(
            doc_category, tax_rate
        )

        entries.append({
            "debit_account": debit_account,
            "debit_tax_type": debit_tax_type,
            "credit_account": credit_account,
            "credit_tax_type": credit_tax_type,
            "amount": int(amount),
            "description": item.get("description", ""),
            # 以下3つは税率別集計用（集計後は破棄）。tax_included/tax_amount の
            # 正規化は唯一の消費者 aggregate_entries_by_tax_rate 側で一元的に行う
            "tax_rate": tax_rate,
            "tax_included": item.get("tax_included"),
            "tax_amount": item.get("tax_amount"),
        })

    # 6/11 顧客回答: 普通領収書は1枚=1科目（票全体の用途で借方科目を決定）。
    # bank_transfer / fee_receipt は複数科目（振込本体=未払金、手数料=支払手数料）
    # が正当なため対象外。entries が空（items 空/全行小計）でも override を通す:
    # select_aggregated_debit_account([]) は固定科目を返すが、vendor override
    # （ゴルフ場→接待交際費等）で補正できるため、ここで弾かない。
    # 6/12 顧客サンプル: 軽油税（軽油引取税）の品目だけは整票一科目の例外として
    # 租税公課に固定する（代表科目の選出からも除外）。ゴルフ場利用税等の
    # 他の非課税項は従来どおり整票科目に統一される。
    repr_account = None
    keiyuzei_amounts = frozenset()
    if doc_category == "receipt":
        # 例外は rate=0 行限定: Gemini が分解せず課税の燃料行に「内軽油税」
        # 注記を残しても、10%行を租税公課へ流出させない
        flagged = [
            (e, is_keiyuzei_text(e.get("description"))
                and not coerce_tax_rate(e.get("tax_rate")))
            for e in entries
        ]
        keiyuzei_amounts = frozenset(
            e["amount"] for e, is_keiyuzei in flagged if is_keiyuzei)
        normal_entries = [e for e, is_keiyuzei in flagged if not is_keiyuzei]
        repr_account = _override_account_by_vendor(
            vendor, select_aggregated_debit_account(normal_entries or entries))
        entries = [
            {**e, "debit_account": KEIYUZEI_DEBIT_ACCOUNT if is_keiyuzei
             else repr_account}
            for e, is_keiyuzei in flagged
        ]

    # 内訳優先(6/10): レシートに税率別内訳（○%対象額・消費税額）が印字
    # されていれば、それを正解として直接行を起こす。Gemini の逐品目集計
    # （割引の二重控除・税率誤判定・内外税混在の取りこぼし）を回避する。
    # 借方科目は票全体の代表科目を全行へ適用（ゴルフ票=接待交際費等、
    # 顧客サンプルの「同票同科目」に一致）。軽油税の対象外行のみ租税公課
    # （label 一致 + 品目金額一致の保険、6/12）。
    tax_summary = doc.get("tax_summary") or []
    if tax_summary:
        summary_account = (repr_account if repr_account is not None
                           else select_aggregated_debit_account(entries))
        rows = build_rows_from_tax_summary(
            tax_summary, summary_account, doc_category, credit_account,
            keiyuzei_amounts=keiyuzei_amounts,
        )
        if rows:
            # 票面合計照合ガード(6/17): 税率%だけ印字・対象額金額が無い票で Gemini が
            # 税抜を逆算し base_amount に詰める幻覚（丸三タクシー型: 票面¥4,920 を
            # 4920÷1.1≈4473 と誤記）を、票面 total_amount を信頼基準に是正する。
            # tax_summary 行合計が票面合計と乖離し、かつ items 行合計が票面合計と
            # 一致する場合のみ items へ回退する。total 欠損/0・items 空・両者とも
            # 不一致なら従来どおり tax_summary を採用し、乖離は[B']の赤標で人手確認に
            # 委ねる（自動で別の誤値に書き換えない。total と tax_summary は同一 Gemini
            # 出力ゆえ、一致しない時のみ items を第二の証言として採る設計）。
            doc_total = (coerce_tax_amount(doc.get("total_amount"))
                         if doc_category == "receipt" else 0)
            if (doc_total
                    and abs(sum_row_amounts(rows) - doc_total)
                    > TOTAL_MISMATCH_TOLERANCE_YEN):
                item_rows = aggregate_entries_by_tax_rate(entries)
                if (item_rows
                        and abs(sum_row_amounts(item_rows) - doc_total)
                        <= TOTAL_MISMATCH_TOLERANCE_YEN):
                    return item_rows
            return rows

    # 仕様変更(5/25): 内訳が無い場合は明細を税率(8%/10%)別の合計に集約する
    # 摘要の店名は sheets_output 側で前置されるため、ここでは渡さない
    return aggregate_entries_by_tax_rate(entries)


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

    # 貸方科目決定（_determine_credit_account の「一律未払金」とは意図的に別ロジック。
    # 統一規則の導入前からある旧フォーマット兜底経路で、社員の運用がこれで
    # 成立しているため現状維持と裁定済み——2026-07-30 ユーザー裁定
    # 「不用改動了，員工就是這樣要求的」。_determine_credit_account の
    # docstring 参照。揃える提案は却下済みなので再議しないこと）
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

    # 貸方科目決定（_determine_credit_account の「一律未払金」とは意図的に別ロジック。
    # 請求書側は支払手段で分岐する運用のまま——2026-07-30 ユーザー裁定
    # 「不用改動了，員工就是這樣要求的」。_determine_credit_account の
    # docstring 参照。揃える提案は却下済みなので再議しないこと）
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


# ── クレジットカード / 交通系IC（T4）───────────────────────────────
#
# プロンプトと builder の実体は `card_prompts` / `card_entries` に在る。
# ここには**登録だけ**を置く —— このファイルは 2300 行超で CLAUDE.md の
# 800 行上限を大きく超過しており、母 Plan §4 が「これ以上積まない」と決めている。
#
# 両モジュールは venv 非依存（gspread / paddleocr / google api を引かない）で、
# `test_dependency_weight` がそれを機械で見張る。
PROMPTS[DocType.CREDIT_CARD] = card_prompts.CREDIT_CARD_PROMPT
PROMPTS[DocType.TRANSIT_IC] = card_prompts.TRANSIT_IC_PROMPT


# エントリビルダー登録テーブル
ENTRY_BUILDERS = {
    DocType.RECEIPT: _build_entries_from_receipt,
    DocType.PURCHASE_INVOICE: _build_entries_from_purchase_invoice,
    DocType.SALES_INVOICE: _build_entries_from_sales_invoice,
    DocType.SALARY_SLIP: _build_entries_from_salary_slip,
    DocType.CREDIT_CARD: card_entries.build_entries_from_credit_card,
    DocType.TRANSIT_IC: card_entries.build_entries_from_transit_ic,
}

# 逐行記帳（1 明細 = 1 仕訳）を行う doc_type。`_build_doc_result` が
# result dict に `line_mode` を立て、`sheets_output` がそれを見て
# A/B/F/G/H/L/T 列を行級へ切り替える（AD-6 の明示ゲート）。
# **既存 doc_type にはキー自体を書かない** —— `entries_data.get("line_mode")`
# は None（falsy）になり、既存の row は 1 バイトも変わらない。
#
# 実体は `doc_types` に置いてある（T6）。producer の本モジュールと
# consumer の `sheets_output` が同じ集合を見る必要があり、`sheets_output` に
# 本モジュールを import させると google.generativeai まで引き込むため。
# ここは後方互換の再公開（既存の参照経路を壊さない）。


def _is_line_mode(doc_type):
    """この doc_type は逐行記帳か（BULK 予算・截断サルベージ・行欠け検出の可否）。

    集合の直接参照を散らさず 1 つの述語に寄せる。この判定が担うのは
    「予算を変えるか」「救済するか」「行欠けを見るか」の 3 つで、将来
    `RECON_POLICY` のような doc_type 別の方針表へ育つ余地がある。
    そのとき散在していると、直し漏れた 1 箇所が静かな半開状態になる。
    """
    return doc_type in LINE_MODE_DOC_TYPES


def _validate_doc_type_registries(doc_types=None, registries=None):
    """全 DocType が各登録表に漏れなく登録済みかを import 時に検査する。

    登録漏れは起動時エラーにならず、運用中の静かな事故としてしか顕在化しない:
    ENTRY_BUILDERS 漏れ → 1件も yield せず count==0 → Failed → ファイル保持
    → 3秒ごとの再試行で Gemini を無限に焼く。PROMPTS 漏れ → 同じ無限再試行
    ループ（Gemini 消費なしだがフォルダが永久に詰まる）。DOC_TYPE_TAB_SUFFIX
    漏れ → 「領収書」タブへの静かな誤書き込み。ENV_FOLDER_MAP 漏れ →
    フォルダが監視されず新タイプが静かに不活性。真値来源は DocType.ALL
    （CLAUDE.md の同期チェックリストの機械可読版）。
    """
    doc_types = DocType.ALL if doc_types is None else doc_types
    if registries is None:
        registries = {
            "PROMPTS": PROMPTS,
            "ENTRY_BUILDERS": ENTRY_BUILDERS,
            "DOC_TYPE_CONFIG": DOC_TYPE_CONFIG,
            "DOC_TYPE_TAB_SUFFIX": DOC_TYPE_TAB_SUFFIX,
            # ENV_FOLDER_MAP は {環境変数名: DocType} なので values 側で照合
            "ENV_FOLDER_MAP": set(ENV_FOLDER_MAP.values()),
        }
    problems = []
    for name, table in registries.items():
        missing = [dt for dt in doc_types if dt not in table]
        if missing:
            problems.append(f"{name} 未登録: {missing}")
    if problems:
        raise RuntimeError(
            "DocType 登録表に漏れがあります — " + " / ".join(problems) +
            "（新文書タイプ追加時は CLAUDE.md の同期チェックリスト参照）")


_validate_doc_type_registries()


def _build_doc_result(doc_type, raw_data, entries):
    """非領収書（請求書系・給与明細）の result dict を組み立てる。

    entries が空なら必ず `_unrecognized` を立てる。これを怠ると
    sheets_output.append_entries が1行も書かずに return し、main は
    count=1 / error_pages=0 で Success 判定 → 原票がアーカイブされ、
    明細ゼロのまま誰も気づかない（無音のデータ欠落）。

    `_page_error` ではなく `_unrecognized` を使う理由: `_page_error` だと
    count==error_pages で Failed 判定 → ファイル保持 → 明細を持たない書類
    （封筒・挨拶状等）が毎回再試行される無限ループに入る。占位行を1行だけ
    書いてアーカイブし、U列の赤タグで人手確認を促す。

    刻む `result["doc_type"]` は **この頁を解析した種別**（T3 以降。混載
    フォルダでは folder doc_type と異なりうる）。**タブ選択に使ってはいけない**
    —— タブは 1 ファイル 1 つに保つため `main` が渡す folder doc_type で
    決まる（AD-T3-1）。`test_ocr_engine_mixed_folder` が意味を固定している。

    **消費者（T6 以降）**: `sheets_output.append_entries` が異常検知の抑制で
    この键を読む（行級 parent へそのまま引き継ぐ）。混載フォルダでは
    nimoca の頁が `doc_type=credit_card` として `append_entries` に到達する
    ため、引数の folder doc_type で券種を判定すると抑制が漏れる。
    よってこの键は「頁の実際の種別」であり続けなければならない。
    """
    vendor = raw_data.get("vendor", "")
    if doc_type == DocType.SALARY_SLIP:
        vendor = raw_data.get("employee_name", "")

    unrecognized = not entries
    if unrecognized:
        print("⚠️ 有効な仕訳エントリが見つかりません → 認識不能として記録")

    result = {
        "doc_type": doc_type,
        "date": raw_data.get("date"),
        "vendor": vendor,
        "invoice_num": raw_data.get("invoice_num", ""),
        # 認識不能行の摘要は _write_unrecognized_row に決めさせる（領収書経路と同じ）
        "memo": "" if unrecognized else raw_data.get("memo", ""),
        "entries": entries,
        "_unrecognized": unrecognized,
    }

    if not _is_line_mode(doc_type):
        return result

    # ── 逐行記帳 doc_type だけの追加情報（AD-T4-6 / AD-T4-7）──
    # 既存 doc_type には**キー自体を足さない**（上で return 済み）。
    # 「1 明細 = 1 独立取引」であって複合仕訳ではないことを T6 へ伝える旗。
    result["line_mode"] = True
    # 記帳しなかった行（前回分口座振替・チャージ）の件数と金額。
    # doc 級 memo と専用キーの**両方**に入れる —— T6 で T列が行級化すると
    # doc 級 memo は読まれなくなるので、移行の谷間で情報が消えないようにする。
    summary = card_entries.summarize_nonbookable(raw_data, doc_type)
    if summary:
        result["_nonbookable_summary"] = summary
        if not unrecognized:
            result["memo"] = " ".join(x for x in (result["memo"], summary) if x)
    return result


def _normalize_receipt_results(
    raw_data: object,
    prefix: str = "",
    ocr_confidence: float | None = None,
) -> list[dict]:
    """領収書レスポンスを統一結果(list[dict])に正規化。

    ocr_confidence は規則②用の page-level PaddleOCR 置信度。同一ページに複数
    書類があれば全 result dict に同じ page-level 値を押す（page-level 信号で
    意図的、書類との 1:1 対応ではない）。None=無信号（下游は黄を付けない）。
    """
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
                # 票面合計（B' 照合用、票面印字値の転記）。bank_transfer /
                # fee_receipt は合計の語義が曖昧（振込金額 vs 手数料込み）な
                # ため receipt のみ伝搬する（None=照合スキップ）
                "total_amount": (doc.get("total_amount")
                                 if doc_cat == "receipt" else None),
                # 規則② page-level 置信度（同ページ全書類に同値を押す）
                "ocr_confidence": ocr_confidence,
                # 規則①（対象外行異常）は真の receipt のみ対象。bank_transfer /
                # fee_receipt は本体が「対象外」かつ高額になり得るため除外（codex 指摘）
                "doc_category": doc_cat,
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
        "ocr_confidence": ocr_confidence,
        # 旧フォーマットは単一 receipt 書類の fallback 経路（doc_category=receipt 固定）
        "doc_category": "receipt",
        "entries": entries,
    })
    return results


# ============================================================
# メインパイプライン
# ============================================================

@dataclass(frozen=True)
class PageOcr:
    """1 頁の OCR ＋ prompt 解決の結果。`_route_ocr_strategy` の唯一の戻り値型。

    後方互換の tuple unpack は**意図的に用意していない**。3-tuple のまま
    受けられる余地を残すと、直し漏れが実行時に静かに通ってしまう
    （T3 Plan の明文）。`TypeError` で必ず露見する形にしてある。

    `frozen=True` は AD-T3-2 の不変式を型で守るため —— 呼出側が
    「あとから prompt を差し替える」書き方を構造的にできなくする。
    """

    raw_data: object                 # Gemini の生 JSON（失敗時 None）
    ocr_text: str                    # PaddleOCR テキスト（失敗時 ""）
    ocr_confidence: float | None     # PaddleOCR テキスト経由のときだけ数値
    actual_doc_type: str             # **prompt と builder** の引き当てキー
    prompt: str                      # 解決済み prompt（常に非空）
    page_class: object               # page_family.PageClass（T9 が消費）
    family_signal: str | None        # 監査シグナル（T9 が消費）

    @property
    def line_mode(self) -> bool:
        """この頁が逐行記帳か。**兜底の呼出側で導出し直さないため**の窓口。

        `actual_doc_type` は本オブジェクトが確定させた事実なので、呼出点で
        `page_ocr.actual_doc_type in LINE_MODE_DOC_TYPES` と書き直すのは
        同じ導出の再実装になる（frozen dataclass なので property は安全）。
        """
        return _is_line_mode(self.actual_doc_type)


def _resolve_page_prompt(folder_doc_type, ocr_text):
    """この 1 頁に使う prompt を決める（T3-b）。

    クレカと nimoca は同じ Drive フォルダに混載される（趙裁定 5）ため、
    prompt はフォルダでは決まらず**頁ごとに**決まる。

    Returns:
        (actual_doc_type, prompt, page_class, family_signal)

        `actual_doc_type` は **PROMPTS と ENTRY_BUILDERS の両方**を引くキー。
        タブ名・取引No 採番・分割 PDF の保存先には使わない（AD-T3-1。
        それらは 1 ファイル 1 タブを保つため folder doc_type に従う）。

    Raises:
        ValueError: folder_doc_type の prompt すら引けないとき。`None` を
            返さないのは、呼出側の None チェック漏れが別の場所で
            `AttributeError` を出して原因追跡を困難にするため（AD-T3-2）。
    """
    folder_prompt = PROMPTS.get(folder_doc_type)
    if not folder_prompt:
        raise ValueError(
            "PROMPTS に登録の無い doc_type です: %r" % (folder_doc_type,))

    page_class = page_family.classify_page(ocr_text)
    actual, signal = page_family.select_prompt_doc_type(
        folder_doc_type, page_class)

    if actual == folder_doc_type:
        return folder_doc_type, folder_prompt, page_class, signal

    # 分流先が決まったが prompt が未登録 —— actual と prompt を**セットで**
    # folder へ戻す。片方だけ戻すと「prompt は A・builder は B」という
    # 異種混成になり、Gemini の出力構造を別の型の builder が読むことになる。
    actual_prompt = PROMPTS.get(actual)
    if not actual_prompt:
        fallback = "prompt_fallback:%s" % actual
        print(f"⚠️ {actual} の prompt が未登録 → {folder_doc_type} で処理します")
        return (folder_doc_type, folder_prompt, page_class,
                "%s;%s" % (signal, fallback) if signal else fallback)

    return actual, actual_prompt, page_class, signal


def _route_ocr_strategy(
    data_bytes: bytes,
    mime_type: str,
    doc_type: str,
    ocr_strategy: str,
    prefix: str = "",
) -> PageOcr:
    """OCR 戦略に基づいてルーティングし、`PageOcr` を返す。

    第 3 引数は T3 で `prompt` から `doc_type`（＝フォルダが宣言した種別）へ
    変わった。prompt は本関数が頁ごとに解決する —— 解決には PaddleOCR の
    テキストが要るので、呼出側では決められない。

    `ocr_confidence` は規則②（低置信整票送審）用の PaddleOCR 平均置信度。
    raw_data が PaddleOCR テキスト経由でない場合（Gemini-Vision 兜底・例外・
    テキスト無し）は None を返す（Vision 結果に低置信を誤って付けないため）。

    **例外の捕捉範囲は T3 の前後で 1 ミリも変えていない**（AD-T3-4）。
    現行の try は PaddleOCR だけでなく Gemini 呼出まで覆っており、
    Gemini 側の例外も握って raw_data=None を返す。呼出側はそれを見て
    Vision 兜底へ落ちる。ここを PaddleOCR だけに絞ると、兜底で救えるはずの
    頁が「ページ処理エラー」の占位行に転落する（全 doc_type に効く回帰）。
    `test_route_ocr_characterization.py` がこの一点を見張っている。
    """
    import config
    # prompt の確定は try の**外**。PaddleOCR や Gemini が落ちても
    # Vision 兜底が使える prompt が残っていなければならない（AD-T3-2）。
    actual_doc_type, prompt, page_class, family_signal = _resolve_page_prompt(
        doc_type, "")

    raw_data = None
    ocr_text = ""
    ocr_conf: float | None = None
    try:
        ocr_text, paddle_conf = _ocr_with_paddleocr(data_bytes, mime_type)
        if ocr_text.strip():
            print(f"{prefix}📝 PaddleOCR完了 ({len(ocr_text)}文字, 置信度: {paddle_conf:.3f})")
            # テキストが取れたので prompt の上書きを試みる。ここで例外が出ても
            # 上の except が握り、prompt は folder のものが残る。
            actual_doc_type, prompt, page_class, family_signal = (
                _resolve_page_prompt(doc_type, ocr_text))
            if actual_doc_type != doc_type:
                print(f"{prefix}🔀 頁の種別を {doc_type} → {actual_doc_type} と判定")
            # T5: 旗は**この頁を解析する種別**で決める（folder ではない）。
            # 混載フォルダでは頁ごとに変わる。
            line_mode = _is_line_mode(actual_doc_type)
            if ocr_strategy == "A":
                raw_data = _call_gemini_text(ocr_text, prompt,
                                             line_mode=line_mode)
                ocr_conf = paddle_conf
            elif ocr_strategy == "B":
                if paddle_conf >= config.OCR_CONFIDENCE_THRESHOLD:
                    raw_data = _call_gemini_text(ocr_text, prompt,
                                                 line_mode=line_mode)
                    ocr_conf = paddle_conf
                else:
                    # Vision 兜底: raw_data は Vision 由来のため置信度は無信号(None)
                    print(f"{prefix}⚠️ 置信度低 ({paddle_conf:.3f} < {config.OCR_CONFIDENCE_THRESHOLD}) → Gemini Vision")
                    raw_data = _call_gemini_bytes(data_bytes, mime_type, prompt,
                                                  line_mode=line_mode)
            elif ocr_strategy == "C":
                raw_data = _call_gemini_cross_validate(
                    ocr_text, data_bytes, mime_type, prompt,
                    line_mode=line_mode)
                ocr_conf = paddle_conf
    except Exception as ocr_err:
        print(f"{prefix}⚠️ PaddleOCR失敗: {ocr_err}")
    return PageOcr(
        raw_data=raw_data,
        ocr_text=ocr_text,
        ocr_confidence=ocr_conf,
        actual_doc_type=actual_doc_type,
        prompt=prompt,
        page_class=page_class,
        family_signal=family_signal,
    )


def _blank_result(date="", vendor="", **markers):
    """entries を持たない result dict（認識不能・除外ページ）を組み立てる。

    `_page_error_payload` はページ封筒（page_num 等を含む外側）を作るのに対し、
    こちらは result dict そのもの。両者とも「占位行の形状を 1 箇所に集約して
    漂移を防ぐ」という同じ意図で、markers に `_unrecognized=True` や
    `_excluded_page=True` / `_exclude_reason=...` を渡し分ける。
    """
    return {
        "date": date,
        "vendor": vendor,
        "invoice_num": "",
        "memo": "",
        "entries": [],
        **markers,
    }


def _merge_audit_signals(*signals):
    """監査シグナルを `;` で連結する。**どれも落とさない**。

    `_with_audit_signal` は `"_audit_signal": reason` で無条件に上書きする。
    族シグナル（`family_signal_with_entries:...`）と `card_salvage` の行欠け
    シグナルは同じ頁で同時に立ちうるので、素直に載せ直すと**片方が黙って
    消える**（Codex 評審 HIGH-2 で発覚）。監査タブは「人が異常に気づく
    唯一の場所」なので、シグナルの欠落は IP-401 と同じ形の事故になる。
    """
    parts = [s for s in signals if s]
    return ";".join(parts) if parts else None


# MF タブへ出す除外行の摘要（顧客が読む文言）。監査タブ行は摘要列を持たない
# ので空でよいが、MF タブ行を空で渡すと sheets_output._write_unrecognized_row
# が「⚠ 認識不能ページ」にフォールバックし、**正常な合計表ページが OCR 失敗と
# 見分けがつかなくなる**（codex review P2 / 2026-08-19）。
_EXCLUSION_MEMO_BY_FAMILY = {
    page_family.FAMILY_CC_SUMMARY:
        "⚠ 合計表ページ（明細が無いため仕訳を作成していません）",
}


def _exclusion_memo(disposition):
    """除外行の摘要。MF タブ行だけが顧客の目に触れるので文言を持たせる。

    未知の族が将来 MF タブへ回されたときも空にならないよう既定値を置く。
    「摘要が空 → 認識不能に化ける」は静かに効く欠陥なので、族ごとの
    登録漏れで再発しない形にしておく。
    """
    if getattr(disposition, "destination", "") != page_family.EXCLUDE_DEST_MF_TAB:
        return ""
    return _EXCLUSION_MEMO_BY_FAMILY.get(
        getattr(disposition, "family", None),
        "⚠ 仕訳対象外ページ（明細が無いため記帳していません）")


def _resolve_card_disposition(doc_type, page_class, result, raw_data, prefix="",
                              dedup_verdict=None):
    """card 系頁の去向を `page_family` に裁決させる（T8-3 / Plan の P-B 位置）。

    `page_family.resolve_page_disposition` が**唯一の裁決者**（AD-0）。
    ここは多頭裁決を作らないための薄い接続で、判定条件は一切持たない。

    None を返すのは「裁決しない＝従来どおりの経路へ流す」の意。
    - `page_class` が無い（Vision 兜底・旧経路）
    - card 系以外の doc_type（既存 4 型は 1 バイトも挙動を変えない）
    - **除外裁決だが行欠けの疑いがある頁**

    最後の 1 つが要点: 除外して監査タブへ静かに送ると、解析失敗で行を
    落とした頁が「正常に除外された頁」に化ける。`_looks_like_detail_rows`
    の docstring が宣言する「閾値の誤りは赤占位行の側へ倒す」と同じ方向で、
    疑わしいときは**うるさい赤**を選ぶ（Codex 評審 MEDIUM-1）。
    """
    if page_class is None or doc_type not in page_family.CC_FAMILY_DOC_TYPES:
        return None
    entry_count = len(result.get("entries") or [])
    disposition = page_family.resolve_page_disposition(
        dedup_verdict, entry_count, page_class)
    if disposition.action != page_family.ACTION_EXCLUDE:
        return disposition
    shortage, shortage_reason = card_salvage.page_marks(raw_data)
    if disposition.is_duplicate:
        # T8d / A-D1: 重複頁では行欠け guard を**迂回する**。
        # `DUPLICATE` に到達した頁は原本と (日付, 金額) の並びも頁合計も
        # 逐字一致している（二重署名 AND 規則）。原本側に行欠けがあれば
        # 同じ痕跡が原本の監査行に既に出ているので、重複側を除外しても
        # 新たに失う情報は無い。除外しなければ確実に二重計上になる。
        #
        # ただし**痕跡は潰さない**（Codex 評審 C5）。理由列に行欠けの
        # キーも残して、原本を当たり直す手がかりを消さない。
        if shortage_reason:
            disposition = replace(
                disposition,
                reason="%s;%s" % (disposition.reason, shortage_reason))
        print(f"{prefix}📋 重複ページとして除外（監査タブに記録）")
        return disposition
    if shortage is not None:
        print(f"{prefix}⚠️ 除外候補（{disposition.family}）だが行欠けの疑い "
              f"→ 除外せず認識不能として可視化")
        return None
    print(f"{prefix}📋 {disposition.family} として除外（監査タブに記録）")
    return disposition


def _page_audit_signal(disposition, doc_type, ocr_text, raw_data):
    """この頁に載せる監査シグナルを 1 本にまとめる（族 ＋ 区画突合）。

    族シグナルは `page_family` の裁決から、区画シグナルは `section_audit_signals`
    から来る。**どちらも頁単位の注記**で、同じ頁で同時に立ちうるので
    `_merge_audit_signals` を通す（片方が黙って消えるのが Codex HIGH-2 の形）。

    `disposition` は None を取りうる（Vision 兜底など `page_class` が無い経路）。
    区画突合はそこでこそ効かせたい —— OCR テキストが無い頁は検査器も沈黙し、
    `section_detection_unknown` として可視化するのがその経路の唯一の痕跡になる。
    """
    return _merge_audit_signals(
        getattr(disposition, "audit_signal", None),
        *page_family.section_audit_signals(doc_type, ocr_text, raw_data))


def _apply_page_audit_signal(results, signal, ocr_text):
    """頁単位の監査シグナルを line-mode の**後**に先頭 result へ合成する。

    P-B の時点で載せると `_yield_line_mode_results` の `_with_audit_signal`
    に上書きされて消える（Codex 評審 HIGH-2）。頁単位で 1 行にしたいので
    先頭 result にだけ付けるのは既存の封筒シグナルと同じ方針。

    **同居は実在する**（simplify 評審 2026-08-19 が実証）。`card_salvage.
    page_marks` は「救済は経たが行数は充足」のとき `(None, "salvaged:X/Y")`
    を返し、`_yield_line_mode_results` はそれを **entries を持つ同一 result**
    へ載せる。つまり合成は防御ではなく現に効いている経路がある。
    """
    if not signal:
        yield from results
        return
    for i, r in enumerate(results):
        if i == 0:
            # 形の出所は `_with_audit_signal` に一本化する。ここで dict を
            # 組み直すと、シグナル欠落を防ぐために作った単一情報源が
            # 増殖して同じ事故の芽になる（simplify 評審の指摘）。
            r = _with_audit_signal(
                r, _merge_audit_signals(r.get("_audit_signal"), signal), ocr_text)
        yield r


def _yield_page_results(doc_type, raw_data, ocr_text, ocr_conf, prefix="",
                        envelope_filter=False, page_class=None,
                        state=None, page_num=None):
    """1ページ分の解析結果を doc_type 別に整形して result dict を yield する。

    process_pipeline の「PDF 逐頁ループ」と「単ページ PDF/画像（尾段）」は
    以前どちらも _apply_ocr_overrides → doc_type 別ルーティング → result dict
    生成、という同じ整形ロジックを逐字コピーで持っており、2 箇所平行メンテ
    による漂移リスクを抱えていた（CLAUDE.md に記録されている ENTRY_BUILDERS
    未登録事故と同族——片方だけ直して片方を直し忘れる類の事故）。本関数へ
    一本化し、呼び出し側は page_num/total_pages/page_bytes 等のページメタ
    情報を付与してラップするだけにする。

    IP-401 T1: 封筒/非領収書ページ判定 (_is_envelope_page) は本関数へ移設された。
    ただし §3.5 の裁決により有効なのは PDF 逐頁ループから
    `envelope_filter=True` で呼ばれたときだけで、尾段（単頁 PDF・画像）は
    既定の False のまま——尾段には元々この判定が無く、挙動を変えない。

    Args:
        doc_type: **この頁を解析した種別**（T3 以降。`PageOcr.actual_doc_type`）。
            フォルダが宣言した種別**ではない** —— 混載フォルダ（クレカ ＋ nimoca）
            では頁ごとに違う値が来る。用途は prompt / builder の引き当てだけで、
            タブ名・取引No 採番・分割 PDF の保存先には**使ってはいけない**
            （それらは 1 ファイル 1 タブを保つため folder doc_type に従う。
            AD-T3-1）。本関数が返す result dict の `"doc_type"` 键にもこの値が
            入る（`_build_doc_result`）。
        envelope_filter: True なら RECEIPT の封筒/不要ページ判定を有効化する。
            PDF 逐頁ループ専用（§3.5）。

    Yields:
        dict: result dict そのもの（page_num 等は含まない。呼び出し側が付与）
    """
    # ── IP-401: raw_data の型ゲート ──
    # Gemini が JSON 配列（あるいは文字列・数値）を返すと extract_json は
    # それをそのまま返す（arr_match 分岐 / json.loads はスカラーも返す）。
    # 以降の整形は全て dict 前提であり、_apply_ocr_overrides の raw_data.get()
    # が AttributeError になる。尾段（単頁 PDF・画像）はこの例外を最外の
    # except で握り潰して 0 件で終わっており、頁が無音で消えていた。
    #
    # 分類は _unrecognized（占位行を書いて歸檔）であって _page_error では
    # ない。_page_error は「API 5xx・認証・ネットワーク」のような一時障害の
    # ためのもので、Failed → ファイル保持 → 再試行になる。「AI は応答し、
    # JSON も解析でき、型だけが契約違反」は再試行で自癒する保証が無く、
    # そのまま永久ループに入る（_build_doc_result の docstring と同じ判断）。
    #
    # このゲートは社会保険料通知書の判定より**前**に置く。型は「見た」事実
    # であり、社保判定はキーワードによる啓発法だからである（IP-401 T1 が
    # 封筒判定を前置拒否権から事後説明器へ降格したのと同じ原理）。両経路とも
    # 仕訳を 1 件も作らないので帳簿リスクは同一で、差は摘要の文言だけ。
    # 加えて、ここで弾いておけば以降の全コードが dict を仮定してよくなる。
    if not isinstance(raw_data, dict):
        print(f"{prefix}⚠️ AI応答が dict ではありません"
              f"（{type(raw_data).__name__}） → 認識不能として記録")
        yield _blank_result(
            _unrecognized=True,
            memo=f"⚠ AI応答形式不正（{type(raw_data).__name__}）")
        return

    _apply_ocr_overrides(doc_type, raw_data, ocr_text, prefix)

    # ── IP-401 T6: 社会保険料通知書は仕訳を一切作らない（§3.8）──
    # 封筒判定と違い entries の有無を見ない。Gemini はこの券面から堂々と
    # 2口の仕訳を作ってしまう（それが実際の誤りだった）ので、「entries が
    # 空のときだけ効く」設計では止まらない。業務ルールとして先に短絡する。
    # doc_type も envelope_filter も問わない: どのフォルダへ投げられても、
    # 単頁画像でも成立させる（顧客がスキャンしない前提に賭けない）。
    if _is_social_insurance_notice(ocr_text, raw_data):
        print(f"{prefix}🏥 社会保険料通知書を検出 → 仕訳を作成せず提示行のみ")
        yield _blank_result(
            memo=SOCIAL_INSURANCE_MEMO,
            _excluded_page=True,
            _exclude_reason=SOCIAL_INSURANCE_REASON,
            _exclude_destination=EXCLUDE_DEST_MF_TAB,
            _ocr_text_len=len(ocr_text or ""),
        )
        return

    if doc_type == DocType.RECEIPT:
        page_results = _normalize_receipt_results(
            raw_data, prefix=prefix, ocr_confidence=ocr_conf)

        # ── IP-401 T1: 封筒判定を「前置拒否権」から「事後説明器」へ ──
        # 旧実装は Gemini 呼び出しの直後・整形の前にこの判定を置き、命中したら
        # continue で無音 skip していた。PaddleOCR の誤認識テキストだけを見て
        # Gemini の成果を単独で否決できる構造であり、Strategy C（交差検証）の
        # 設計意図がこの一点で潰れていた。実際に舞鶴パークの小型サーマル
        # 領収証（「領収証」→「领収证」と誤認識）が無音で消えている。
        #
        # 改後は entries を組めたかどうかを先に見る。組めていれば棄却経路は
        # 存在しない（目標1が構造的に達成される）。組めなかったときだけ
        # 封筒判定を「なぜ空だったのか」の分類器として使う。
        is_envelope = bool(
            envelope_filter and _is_envelope_page(ocr_text, raw_data))

        if not page_results:
            if is_envelope:
                # 除外はするが**無音にはしない**。呼び出し側が監査タブへ回す。
                # _page_error は立てない: これは失敗ではなく正常な除外であり、
                # 立てると main が Failed 判定 → ファイル保持 → 無限リトライ。
                print(f"{prefix}📨 封筒/非領収書ページとして除外（監査タブに記録）")
                yield _blank_result(_excluded_page=True,
                                    _exclude_reason="envelope",
                                    _exclude_destination=EXCLUDE_DEST_AUDIT_TAB,
                                    _ocr_text_len=len(ocr_text or ""))
                return
            print(f"{prefix}⚠️ 有効な仕訳エントリが見つかりません → 認識不能として記録")
            p_date, p_vendor = _extract_partial_data(raw_data)
            yield _blank_result(date=p_date, vendor=p_vendor,
                                _unrecognized=True)
            return

        # entries は有効だが封筒シグナルも命中 → 記帳は止めず（Gemini 優先＝
        # 交差検証の設計意図）、人手抽査用に監査タブへ「分岐」を残す。
        # Gemini が本物の封筒に偽 entry を捏造する可能性への担保（§6）。
        # 記録はページ単位で 1 行にしたいので先頭 result にだけ付ける。
        if is_envelope:
            yield _with_audit_signal(page_results[0],
                                     "envelope_signal_with_entries", ocr_text)
            yield from page_results[1:]
        else:
            yield from page_results
        return

    # ── 非領収書（請求書系・給与明細）: builder 適用 ──
    builder = ENTRY_BUILDERS.get(doc_type)
    if not builder:
        # DocType.ALL 全件が import 時に _validate_doc_type_registries
        # で検査済みのため到達しないはずだが、防御的に記録だけして進む。
        print(f"{prefix}⚠️ エントリビルダーが未登録: {doc_type}")
        return

    # `state` は `page_class` と同じ形で既定値 `None` を持つ（生産の呼出点は
    # AST 番人が縛る）。None のときは使い捨てを作る —— None 分岐を関数中に
    # 散らすと、片方だけ書き忘れて半開状態になる。
    state = state if state is not None else card_file_state.CardFileState()

    # ── T8d: 重複頁の判定（A 章）──
    # 判定は `page_dedup`、裁決は `page_family` にあり、ここは接線だけ。
    # `token` は中身を見ない不透明な物体で、記帳できた頁だけ `remember` へ返す。
    #
    # **錨の解決より先に取る。** 逆順にすると `resolve_anchor` が書いた
    # `row["date"]` が指紋の材料に混ざり、**同じ頁でも「継承できたか」で
    # 指紋が変わる**。1 回目は継承できて 2 回目は run が閉じていた、という
    # 並びで digest が割れ、重複頁が `duplicate` ではなく `key_conflict` へ
    # 落ちて二重計上される（Codex 実施後評審 2026-08-20）。
    # 指紋は Gemini の生出力だけから作る。
    dedup_verdict, dedup_token = state.classify(
        doc_type, page_num, ocr_text, raw_data)

    # ── T8d: 明細書作成日の錨をファイル内で引き継ぐ（B 章）──
    # 位置は builder の**前**。錨から確定した日付を `rows[*]["date"]` へ
    # 直接書くので、builder より後ろに置くと 1 行も効かない。
    # （`card.statement_date` には注入しない —— 注入すると自前の錨を持つ
    #  頁の挙動まで変わり、改修前から日付が入っていた行が動く）
    anchor_signal = state.resolve_anchor(doc_type, raw_data, ocr_text, page_num)

    result = _build_doc_result(doc_type, raw_data, builder(raw_data))

    # ── T8-3: 頁の去向裁決（P-B）──
    # 位置は `_build_doc_result` の後・`_is_line_mode` 分派の前。ここだけが
    # `result["entries"]` を持ち、かつ逐頁ループと尾段の両方が通る
    # （Plan §2.5 / §15.1 で P-A・P-C を排除済）。
    disposition = _resolve_card_disposition(
        doc_type, page_class, result, raw_data, prefix, dedup_verdict)
    if disposition is not None and disposition.action == page_family.ACTION_EXCLUDE:
        # 無音にはしない。呼出側（main / local_test）が監査タブへ回す。
        yield _blank_result(memo=_exclusion_memo(disposition),
                            _ocr_text_len=len(ocr_text or ""),
                            **page_family.exclusion_fields(disposition))
        return

    # **記帳できた頁だけ**索引に登録する。`_unrecognized` に終わった頁を
    # 入れると、後続の真の重複頁が除外され、その明細はどこにも 1 回も
    # 記帳されない（`CardFileState.remember` の docstring）。
    if result.get("entries"):
        state.remember(dedup_token, page_num)

    if not _is_line_mode(doc_type):
        # ここに族シグナルの合成は要らない。`_resolve_card_disposition` が
        # 非 None を返すのは `CC_FAMILY_DOC_TYPES` の 2 型だけで、それは
        # `LINE_MODE_DOC_TYPES` と同値だからこの枝では disposition が必ず
        # None になる（simplify 評審が coverage で未実行を実証）。
        yield result
        return
    # T8b-2: 区画の取りこぼしを Gemini の外から突合し、監査タブへ載せる。
    # **記帳は止めない**（IP-401 / 趙拍板 2026-08-19）。ここは薄い接線で、
    # 判定条件は 1 つも持たない（`_resolve_card_disposition` と同じ方針）。
    yield from _apply_page_audit_signal(
        _yield_line_mode_results(result, raw_data, ocr_text, prefix),
        _merge_audit_signals(
            anchor_signal,
            _page_audit_signal(disposition, doc_type, ocr_text, raw_data)),
        ocr_text)


def _yield_line_mode_results(result, raw_data, ocr_text, prefix=""):
    """逐行記帳 doc_type の result を、行欠けの痕跡込みで yield する（T5 §3.5）。

    痕跡の落点は趙裁定 2026-08-17 のとおり **MF タブの金額 0 提示行 ＋
    監査タブ 1 行**で、いずれも既存機構をそのまま使う:

    - MF 提示行: `_unrecognized` の payload → `sheets_output` が摘要へ memo を
      書き、タグを自動で赤系にする。金額 0 の entry は `append_entries` に
      行単位で無音 skip されるので、**明細経路では提示行を書けない**
    - 監査タブ: `_audit_signal` → `main` が verdict「分岐」で 1 行。新しい
      verdict 定数は作らない（既存の「欠落」と紛らわしく、reason 列は
      機械可読キーという既存規約でちょうど足りる）

    **明細 result には 1 バイトも触らない**（AD-7: 検算・行欠けの赤は
    カード単位 1 行であって、明細 62 行を赤く塗ることではない）。

    ここが足すのは**注記**であって頁の去向ではない。記帳するか否かは
    明細 result 自身が担っており、AD-0 / T9 の Disposition 軸には乗らない。
    """
    shortage, reason = card_salvage.page_marks(raw_data)
    if shortage is None:
        # 券面申告どおり取れている。reason が付くのは「救済は経たが行数は
        # 充足」の場合だけで、そのときは監査タブにだけ痕跡を残す。
        yield _with_audit_signal(result, reason, ocr_text) if reason else result
        return

    memo = card_salvage.shortage_memo(shortage)
    print(f"{prefix}{memo}")
    if not result.get("entries"):
        # 1 行も記帳できなかった頁。提示行を別に出すと同じ頁に赤い占位行が
        # 2 本並ぶので、既に占位である result 自身へ文言を載せる。
        yield _with_audit_signal(result, reason, ocr_text, memo=memo)
        return
    yield result
    # 提示行は明細の**後**。先に出すと取引No が「注記 → 明細」の順になる。
    # `.get(..., "")` の既定値は T7 の豁免以降**効かない**（result["date"] は
    # キーが在って値が None）。それでも `or ""` にしてはいけない ——
    # `test_ocr_engine_line_shortage` が「提示行の日付 ≡ 明細 result の日付」
    # という同一性を固定しており、片方だけ "" へ寄せると契約が割れる。
    # 下流（sheets_output の B列 / `_write_unrecognized_row`）は両方とも
    # `or ""` で吸うので、見える挙動は同じ。
    yield _blank_result(date=result.get("date", ""),
                        vendor=result.get("vendor", ""),
                        _unrecognized=True, memo=memo,
                        _audit_signal=reason,
                        _ocr_text_len=len(ocr_text or ""))


def _with_audit_signal(result, reason, ocr_text, **overrides):
    """result に監査タブ用の痕跡（`_audit_signal` ＋ `_ocr_text_len`）を載せる。

    この 2 键は `main.process_file` が読む契約なので、綴りが 1 箇所でも
    ずれると監査行が黙って出なくなる。`_blank_result` / `_page_error_payload`
    と同じ理由で、形の出所を 1 つにまとめておく。
    """
    return {**result, **overrides,
            "_audit_signal": reason,
            "_ocr_text_len": len(ocr_text or "")}


def _page_error_payload(memo, page_num, total_pages, page_bytes):
    """ページ失敗時の占位 yield ペイロードを組み立てる（逐頁ループ 3 経路共通）。

    「ページ処理エラー」「AI応答のJSON解析失敗」「整形処理エラー」の 3 経路が
    同一形状の dict を逐字コピーで持っていた。占位行の契約（`_page_error` /
    `_unrecognized` / `page_bytes`）は消費側 `main.process_file` の成否判定と
    原票リンク生成に直結するため、1 箇所でも取りこぼすと無音欠落に戻る。
    単一の出所にまとめて漂移を防ぐ。
    """
    return {
        "result": {
            "date": "",
            "vendor": "",
            "invoice_num": "",
            "memo": memo,
            "entries": [],
            "_unrecognized": True,
            "_page_error": True,
        },
        "page_num": page_num,
        "total_pages": total_pages,
        "page_bytes": page_bytes,
    }


def process_pipeline(file_path, doc_type=DocType.RECEIPT, ocr_strategy=None, start_page=1):
    """
    文書を分析し、仕訳データを逐次 yield するジェネレータ。

    各 yield は dict: {"result": 仕訳dict, "page_num": int, "total_pages": int}

    メモリ最適化: 1ページ処理→yield→GC→次ページ の流れで
    EC2 t2.micro (768MB) でも安定動作する。

    Args:
        file_path: 文書ファイルのパス
        doc_type: 文書タイプ (DocType 定数)
        ocr_strategy: OCR 戦略 (A/B/C, None=config.OCR_STRATEGY)
        start_page: 再開用、このページ未満はスキップ（1 始まり）

    Yields:
        dict: {"result": dict, "page_num": int, "total_pages": int}
    """
    import itertools

    filename = os.path.basename(file_path)
    type_label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)
    print(f"🧠 PaddleOCR + Gemini で{type_label}を分析中: {filename} (戦略: {ocr_strategy}) ...")

    try:
        # T3: prompt はここでは解決しない（頁ごとに _route_ocr_strategy が
        # 決める）。この検査は「未対応の doc_type を早期に弾く」という既存の
        # 防御だけを残したもの。ここを通れば _resolve_page_prompt の
        # ValueError（AD-T3-2）は本番経路では到達しない。
        if not PROMPTS.get(doc_type):
            print(f"⚠️ 未対応の文書タイプ: {doc_type}")
            return

        import config
        if ocr_strategy is None:
            ocr_strategy = config.OCR_STRATEGY

        mime_type = _get_mime_type(file_path)

        # T8d: 1 ファイル分の頁跨ぎ状態を**ここで 1 個だけ**作る。
        # 重複索引と明細書作成日の錨を持つ。頁ごとに作ると毎頁リセットされ、
        # 接線したのに何も効かない（T8 と同じ「繋がって見えるが働かない」形）。
        # `main` / `local_test` / `benchmark_ocr` の 3 消費者はどれも
        # `process_pipeline` を通るので、ここに置けば全経路を同時に覆える。
        file_state = card_file_state.CardFileState()
        if start_page > 1 and doc_type in page_family.CC_FAMILY_DOC_TYPES:
            # 局限: 途中頁から再開すると前頁が索引に入らないので、重複除外も
            # 錨の引き継ぎも効かない。fail-open（＝改修前と同じ挙動）に倒れる
            # だけで帳簿は壊れないが、黙って効かないのは避ける。
            # `start_page` を渡すのは `local_test.py --start-page` だけで、
            # `main.py` は渡さない（生産経路には無い）。
            print(f"⚠️ {start_page} ページ目から再開するため、"
                  f"重複ページの除外と明細書作成日の引き継ぎは効きません")

        # ── PDF: 1ページずつ yield（各ページ独立、全文書タイプ共通）──
        # Session 16 以前は非領収書 PDF のみ「全ページ OCR テキストを結合し
        # 1回だけ Gemini を呼ぶ」分岐が別にあったが、大型 PDF で出力が
        # MAX_TOKENS に達し JSON が途中切断される生産事故が発生した。
        # 「1冊まるごと1レスポンス」を出力させる設計自体が原因のため、
        # 対症療法ではなく全文書タイプをこの逐頁分岐に統一して根絶する。
        if mime_type == "application/pdf":
            page_gen = _split_pdf_pages(file_path)
            try:
                first_page = next(page_gen, None)
            except PdfSplitError as split_err:
                # IP-401 §12.1②: 尾段へ落とさない。多頁か単頁かを判定
                # できないまま**ファイル全体**を 1 回の Gemini 呼出へ送る
                # ことになり、直前のコメント（「1冊まるごと1レスポンス」を
                # 根絶した経緯）が塞いだはずの MAX_TOKENS 事故の再現経路
                # そのものになる。しかも pypdf が壊れていれば全 PDF が
                # 該当するので**系統的**に起きる。分類は `_page_error`
                # （＝全頁失敗となりファイルは保持される。pypdf を入れ直せば
                # 次の周回で救えるため）。
                print(f"❌ PDF分割不可のため解析を中止: {split_err}")
                # 判明した全頁ぶん占位を出す（`never_entered` の補填と同じ
                # 形）。1 件に丸めない理由は 2 つ:
                #   ・進捗タブが「1/1」に化ける（詳細は PdfSplitError の
                #     `total_pages` の項）
                #   ・total だけ N を名乗って占位を 1 件しか出さないと、main の
                #     カバレッジ突合が p2..pN を「欠落」と見なして監査タブへ
                #     書く。この経路は全頁失敗＝ファイル保持なので 3 秒ごとに
                #     再走査され、**再試行のたびに欠落行が増殖**する
                #     （CLAUDE.md「全頁失敗は Sheets に占位行を書かない」）
                # start_page は使わない。これは頁単位の skip ではなく
                # ファイル全体が分割できない状況であり、必ず 1 件以上を
                # yield する保証（IP-401 の不変式）を優先する。
                memo = f"PDF分割不可: {str(split_err)[:120]}"
                declared = split_err.total_pages or 1
                for miss in range(1, declared + 1):
                    yield _page_error_payload(memo, miss, declared, None)
                return
            if first_page is not None:
                total = first_page["total_pages"]
                print(f"📄 大型PDF対応: {total}ページを分割解析します")
                yielded = 0
                failed_pages = 0
                # IP-401 §8-中7: 一度でも何かを yield したページ番号。
                # 「無音でページが消える」バグ全般に対する最終哨戒であり、
                # 個別の欠落経路（封筒・整形例外）を塞いだ後も、将来また別の
                # 経路で欠落が生まれたときに気づけるようにする。
                # 裁決により警告のみ（成否判定は変えない、P2 繰延）。
                seen_pages = set()
                # IP-401 §11.0: 逐頁ループの**本体に入った**頁番号。
                # seen_pages（＝一度でも yield した頁）と分ける理由は、
                # 「頁が消える」には性質の違う 2 種があるため:
                #   ・進入したが何も出さなかった → §8-中7 の裁定で警告のみ
                #   ・そもそも進入しなかった     → producer が静かに尽きた
                # 後者は per-page try の埒外なので占位すら作られず、前 k 頁が
                # 成功していると main は Success 判定で歸檔してしまう。
                # 1 つの集合で見ていると両者を区別できず、片方を直すと
                # もう片方の裁定を巻き添えにする。
                entered_pages = set()

                def _mark(payload):
                    """出力を記録してから payload を返す（yield と対で使う）。

                    記録と yield を1つの式にまとめる。別々の文にすると、将来
                    yield 地点を増やしたときに記録だけ書き忘れ——哨戒自身が
                    「成功したページを欠落と誤報する」という、まさに防ぎたい
                    種類の欠陥を生む。ページ番号は payload から取るので
                    ずれようがない。
                    """
                    seen_pages.add(payload["page_num"])
                    return payload

                for page_info in itertools.chain([first_page], page_gen):
                    idx = page_info["page_num"]
                    prefix = f"[p{idx}] "
                    page_data = page_info["data"]

                    if idx < start_page:
                        continue

                    # 記録は start_page スキップの後。名前どおり「本体に
                    # 入った頁」だけを持たせるためで、**差集合の結果は
                    # 位置に依らない**（被減数が range(start_page, total+1)
                    # なので、飛ばした頁番号はそもそも母集団に居ない）。
                    # 当初「前に置くと誤報になる」と書いていたが、変異検証で
                    # 位置を動かしても 1 件も落ちず、その理由付けが誤りだと
                    # 判明した。効くのは可読性であって正しさではない。
                    entered_pages.add(idx)

                    try:
                        page_ocr = _route_ocr_strategy(
                            page_data, "application/pdf", doc_type, ocr_strategy, prefix=prefix
                        )
                        page_raw_data = page_ocr.raw_data
                        ocr_text = page_ocr.ocr_text
                        ocr_conf = page_ocr.ocr_confidence

                        if not page_raw_data:
                            print(f"{prefix}🔄 フォールバック: Gemini Vision で再試行")
                            # 兜底も頁ごとに解決した prompt を使う（AD-T3-2 により
                            # PaddleOCR が失敗していても非空が保証されている）。
                            # T5: 截断で来た頁はここに落ちない（サルベージが真値の
                            # dict を返すため）。ここに来るのは「AI が JSON を
                            # 返さなかった」頁だけで、兜底の期待値が残っている。
                            page_raw_data = _call_gemini_bytes(
                                page_data, "application/pdf", page_ocr.prompt,
                                line_mode=page_ocr.line_mode)
                            ocr_conf = None  # Vision 兜底は無信号（低置信を誤付しない）
                    except Exception as page_err:
                        failed_pages += 1
                        print(f"{prefix}❌ ページ処理エラーのためスキップ: {type(page_err).__name__}: {str(page_err)[:120]}")
                        yield _mark(_page_error_payload(
                            f"ページ処理エラー: {type(page_err).__name__}",
                            idx, total, page_data))
                        gc.collect()
                        continue

                    if not page_raw_data:
                        # 無声 skip 禁止: yield しないと部分失敗時に main が
                        # error_pages=0 → Success 判定 → 原票アーカイブで
                        # このページのデータが無音欠落する（例外経路と同扱い）
                        failed_pages += 1
                        print(f"{prefix}⚠️ AIの応答がJSONではありませんでした")
                        yield _mark(_page_error_payload(
                            "AI応答のJSON解析失敗", idx, total, page_data))
                        gc.collect()
                        continue

                    # ── 封筒・非領収書ページ検出 ──
                    # IP-401 T1: ここにあった「判定して continue（無音 skip）」は
                    # 廃止。判定は _yield_page_results の内側へ移設し、
                    # 「entries を組めなかったページの理由分類器」に降格した
                    # （envelope_filter=True）。無音 skip は顧客が枚数を数える
                    # までしか発見手段がなく、実際に本番で1票欠落している。
                    # Session 16 の RECEIPT 限定裁決は移設先でも維持される
                    # （判定は RECEIPT 分岐の内側にしか無い）。
                    #
                    # OCR オーバーライド → doc_type別ルーティング → result dict 整形
                    # は _yield_page_results に一本化済み（尾段と共通ロジック）
                    #
                    # IP-401 T0: 整形段階の例外境界。
                    # ここは以前 `for entry in _yield_page_results(...)` を裸で
                    # 回しており、逐頁 try の**外**だった。畸形 Gemini JSON 等で
                    # 整形が例外を投げると最外層の except まで飛び、そのページ
                    # だけでなく **PDF の残り全ページが無音で消えた**。
                    # next() を try で包み、例外は当該ページの占位行に閉じ込めて
                    # 次ページへ進む。yield 自体は try の外に置き、消費側から
                    # throw/close された例外を誤って握り潰さないようにする。
                    # T3: 渡すのは folder ではなく **この頁を解析した種別**。
                    # prompt が transit_ic で組まれた頁は builder も transit_ic
                    # でなければ、Gemini の出力構造を別の型が読むことになる。
                    page_iter = _yield_page_results(
                        page_ocr.actual_doc_type, page_raw_data, ocr_text,
                        ocr_conf, prefix=prefix, envelope_filter=True,
                        page_class=page_ocr.page_class,
                        state=file_state, page_num=idx)
                    while True:
                        try:
                            entry = next(page_iter)
                        except StopIteration:
                            break
                        except Exception as fmt_err:
                            failed_pages += 1
                            print(f"{prefix}❌ 整形処理エラー: "
                                  f"{type(fmt_err).__name__}: {str(fmt_err)[:120]}")
                            yield _mark(_page_error_payload(
                                f"整形処理エラー: {type(fmt_err).__name__}",
                                idx, total, page_data))
                            break
                        yield _mark({
                            "result": entry,
                            "page_num": idx,
                            "total_pages": total,
                            "page_bytes": page_data,
                        })
                        yielded += 1
                    gc.collect()

                # IP-401 §11.0: producer が産出しなかった頁を占位で可視化する。
                # `_split_pdf_pages` は頁単位の失敗を自分で握って `continue`
                # するので（§12.1①）、消費側からは「宣言より少ない頁数しか
                # 来なかった」としか見えない（どの頁が飛んだかは分かるが、
                # 飛んだ理由は producer 側の print にしか無い）。これらの
                # 頁は逐頁ループに一度も入らないため per-page try が届かず、
                # 前 k 頁が成功していると main は error_pages==0 で Success →
                # **歸檔** —— 欠落頁の仕訳はどこにも入らず再試行もされない。
                #
                # 分類が `_page_error` なのは「保持」させたいからではない。
                # 保持か歸檔かはファイル全体の成否で main が決め、本件は必ず
                # 「前頁成功 ＋ 後続失敗」なので partial_error → 歸檔になる。
                # 歸檔でよい: 前 k 頁は既に書かれており、保持して再試行すると
                # その k 頁が重複計上される。
                never_entered = sorted(
                    set(range(start_page, total + 1)) - entered_pages)
                for miss in never_entered:
                    failed_pages += 1
                    print(f"[p{miss}] ❌ PDFページ分割失敗: この頁を取得でき"
                          f"ませんでした（占位行を記録します）")
                    yield _mark(_page_error_payload(
                        "PDFページ分割失敗（このページを取得できませんでした）",
                        miss, total, None))

                # ページカバレッジ突合（IP-401 §8-中7）
                # 個別の欠落経路を塞いだ後の最終哨戒。将来また別経路で無音
                # 欠落が生まれたときに、顧客が枚数を数えるより先に気づく。
                # ここが見るのは「進入したのに何も出さなかった頁」——
                # §8-中7 が「警告のみ」と裁定したまさにその集合。
                # 進入しなかった頁は上で占位を出して片付いている。
                missing = sorted(entered_pages - seen_pages)
                if missing:
                    print(f"⚠️ ページカバレッジ警告: {len(missing)}/{total}頁が"
                          f"一度も出力されませんでした {missing} "
                          f"（無音欠落の疑い。処理は継続します）")

                if yielded > 0:
                    print(
                        f"✅ PDF分割解析完了: {yielded}件抽出 "
                        f"(失敗ページ: {failed_pages})"
                    )
                else:
                    print("⚠️ PDF分割解析でも有効結果を取得できませんでした")
                return

        # ── 単ページ PDF / 画像ファイル: 従来通り処理 ──
        # 複数ページ PDF は上の逐頁分岐で必ず処理・return 済み。ここに到達するのは
        # 画像ファイル、または _split_pdf_pages が何も返さない単ページ PDF のみ。
        # §12.1②以降、PDF がここへ来るのは `len(reader.pages) <= 1` の**正常な
        # 単頁**だけである（pypdf 未導入・読取失敗・全頁書出失敗は
        # `PdfSplitError` で上の分岐が占位を出して return 済み）。
        raw_data = None
        ocr_text = ""

        # IP-401: 逐頁ループの同区間（ファイル読取 ＋ _route_ocr_strategy ＋
        # Vision 兜底）は守られているのに、尾段だけ裸だった。
        # ・`open()` —— 逐頁側は `_split_pdf_pages` が**自前の try** で包んで
        #   `PdfSplitError` へ変換し、消費側が占位に落とす。尾段の裸 open は
        #   無人運用の Windows ミニ PC で現実に起きうる（ウイルス対策の
        #   リアルタイム走査による一時ロック）
        # ・`_call_gemini` —— `_generate_content_with_retry` の
        #   `raise last_err` を素通しするので、再試行を使い切ると例外が上がる
        # どちらも最外 except まで飛べば **0 件 yield** で終わり、頁が無音で
        # 消える（main のカバレッジ哨戒も last_total_pages=0 のため鳴らない）。
        # 逐頁と同じ占位に閉じ込める。
        try:
            with open(file_path, "rb") as f:
                file_data = f.read()

            page_ocr = _route_ocr_strategy(
                file_data, mime_type, doc_type, ocr_strategy)
            raw_data = page_ocr.raw_data
            ocr_text = page_ocr.ocr_text
            ocr_conf = page_ocr.ocr_confidence
            del file_data
            gc.collect()

            if not raw_data:
                print("🔄 フォールバック: Gemini Vision で再試行")
                # T5: 尾段（単頁 PDF・画像）も逐頁ループと同じ扱いにする。
                # ここを落とすと単頁クレカ PDF だけが截断→兜底→_page_error→
                # ファイル保持→3 秒ごとの永久再試行に入る（IP-401 が潰した
                # 「逐頁と尾段の非対称」の再発）。
                raw_data = _call_gemini(file_path, page_ocr.prompt,
                                        line_mode=page_ocr.line_mode)
                ocr_conf = None  # Vision 兜底は無信号（低置信を誤付しない）
        except Exception as page_err:
            print(f"❌ ページ処理エラーのためスキップ: "
                  f"{type(page_err).__name__}: {str(page_err)[:120]}")
            yield _page_error_payload(
                f"ページ処理エラー: {type(page_err).__name__}", 1, 1, None)
            return

        if not raw_data:
            # IP-401: 以前はここで return して 0 件で終わっていた。頁が無音で
            # 消えるだけでなく、last_total_pages が 0 のまま残るので main 側の
            # カバレッジ哨戒（range(1, 0+1) = 空）も鳴らなかった。
            # 分類は逐頁ループの同じ状況と**逐字で同じ** _page_error にする
            # ——「AI から使える応答が無い」は 5xx・タイムアウト・レート制限で
            # 普通に起きる一時障害であり、保持して再試行する価値がある。
            # 終態（ファイル保持）は従来と変わらない。
            print("⚠️ AIの応答がJSONではありませんでした")
            yield _page_error_payload("AI応答のJSON解析失敗", 1, 1, None)
            return

        # OCR オーバーライド → doc_type別ルーティング → result dict 整形
        # は _yield_page_results に一本化済み（PDF 逐頁ループと共通ロジック）。
        # 封筒判定は元々この経路には無かったため呼ばない（挙動を変えない）。
        #
        # IP-401: ここは以前 `for entry in _yield_page_results(...)` の裸ループ
        # で、整形段階の例外が最外の except まで飛んで頁が丸ごと消えていた。
        # 逐頁ループ（上）と同形の next() 境界にして、例外を当該頁の占位行に
        # 閉じ込める。yield 自体は try の外に置き、消費側から throw/close された
        # 例外を誤って握り潰さないようにする（逐頁ループと同じ理由）。
        # 現時点では全ての例外源が最初の next() までに完走するため「部分 yield
        # 後の例外」は起きないが、builder が将来流式化すると成立し、そのときは
        # count>0 → Success → 歸檔で**真の無音欠落**になる。
        page_iter = _yield_page_results(page_ocr.actual_doc_type, raw_data,
                                        ocr_text, ocr_conf,
                                        page_class=page_ocr.page_class,
                                        state=file_state, page_num=1)
        while True:
            try:
                entry = next(page_iter)
            except StopIteration:
                break
            except Exception as fmt_err:
                print(f"❌ 整形処理エラー: "
                      f"{type(fmt_err).__name__}: {str(fmt_err)[:120]}")
                yield _page_error_payload(
                    f"整形処理エラー: {type(fmt_err).__name__}", 1, 1, None)
                break
            yield {
                "result": entry,
                "page_num": 1,
                "total_pages": 1,
            }

    except Exception as e:
        print(f"❌ 解析失敗: {e}")
        return
