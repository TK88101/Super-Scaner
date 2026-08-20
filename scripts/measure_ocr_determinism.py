"""同じ PDF に PaddleOCR を N 回かけ、テキストが逐字同一かを**測る**。

判定器ではなく計測器である。Plan
`docs/plans/2026-08-20-gemini-record-replay.md` の TBD-2 / R-3 に答えるために書いた。

**なぜ要るか**: 記録再生の主キーは部位別ハッシュを持ち、そのひとつが
`ocr`（PaddleOCR が出したテキスト）である。OCR が揺れると鍵が総崩れになり、
fixture が毎回失効する。Gemini が `temperature=0` でも揺れることは
2026-08-20 に実測した —— ローカルモデルだから揺れない、は**思い込みであって
測定ではない**ので、測る。

    venv311/bin/python scripts/measure_ocr_determinism.py <PDF パス> [回数] [--show-text]

出力は「何頁中何頁が逐字同一だったか」と、違った頁の最初の相違位置。
**OCR の本文は既定で 1 文字も出さない**（実票の店名・金額・カード末尾が
ターミナルの履歴に残るため）。中身を見たいときだけ `--show-text`。
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

# `load_dotenv()` を引数なしで呼ぶと**呼出ファイルの位置**から探すので、
# scripts/ 配下からはリポジトリ直下の .env を見つけられない
# （`count_empty_dates.py` と同じ理由で明示する）。
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import ocr_engine  # noqa: E402

PDF_MIME = "application/pdf"


def first_difference(left, right):
    """最初に食い違う位置と、その前後を返す（同一なら None）。"""
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            window = slice(max(0, index - 20), index + 20)
            return index, left[window], right[window]
    if len(left) != len(right):
        return limit, left[limit:limit + 40], right[limit:limit + 40]
    return None


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def format_page_report(page_num, texts, confidences, show_text=False):
    """1 頁分の報告行を組む（純関数。OCR は呼ばない）。

    **既定では OCR の本文を 1 文字も出さない。** この計測器は顧客の実票 PDF に
    かけるものなので、本文を出すと店名・カード末尾・金額がターミナルの履歴や
    リダイレクト先に残る。既存コードの慣例も同じで、`ocr_engine` は
    「📝 PaddleOCR完了 (13文字, 置信度: 0.880)」と長さと置信度しか出さない。

    同一かどうか・どこで違うかは、長さ・ハッシュ・相違位置だけで判る。
    本文が本当に要るとき（誤認識の中身を見たいとき）だけ `show_text=True`。
    """
    same = all(text == texts[0] for text in texts)
    lengths = sorted({len(text) for text in texts})
    digests = sorted({_digest(text) for text in texts})
    rounded = sorted({round(value, 6) for value in confidences})
    lines = [f"  p{page_num}: {'同一' if same else '★相違★'} "
             f"文字数={lengths} 置信度={rounded} sha={digests}"]
    if same:
        return lines
    for other in texts[1:]:
        found = first_difference(texts[0], other)
        if not found:
            continue
        index, left, right = found
        lines.append(f"      最初の相違 idx={index}")
        if show_text:
            lines.append(f"        1 回目: {left!r}")
            lines.append(f"        別の回: {right!r}")
        break
    return lines


def measure(pdf_path, repeats, show_text=False):
    print(f"📄 {os.path.basename(pdf_path)} / {repeats} 回")
    identical_pages = 0
    total_pages = 0

    for page in ocr_engine._split_pdf_pages(pdf_path):
        total_pages += 1
        runs = [ocr_engine._ocr_with_paddleocr(page["data"], PDF_MIME)
                for _ in range(repeats)]
        texts = [text for text, _ in runs]
        confidences = [confidence for _, confidence in runs]
        if all(text == texts[0] for text in texts):
            identical_pages += 1
        for line in format_page_report(page["page_num"], texts, confidences,
                                       show_text=show_text):
            print(line)

    print(f"\n📊 逐字同一だった頁: {identical_pages}/{total_pages}")
    if identical_pages == total_pages:
        print("   → この実行では決定的。鍵に `ocr` を含めてよい（TBD-2）")
        print("   ※ sha を控えて**別プロセスで**もう一度走らせると、"
              "跨プロセスの決定性まで確かめられる")
    else:
        print("   → **揺れる**。鍵設計の見直しが要る（Plan AD-4 / R-3）")
    return identical_pages, total_pages


if __name__ == "__main__":
    argv = [arg for arg in sys.argv[1:] if arg != "--show-text"]
    if not argv:
        raise SystemExit(__doc__)
    measure(argv[0], int(argv[1]) if len(argv) > 1 else 2,
            show_text="--show-text" in sys.argv)
