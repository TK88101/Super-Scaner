"""黄金様本 fixture の capture スクリプト（Plan §3 T2）。

真 Gemini を **1 回だけ** 走らせ、process_pipeline の yield を JSON へ落とす。
以降の回帰判定は golden_replay がこの fixture を決定論的に再生して行う
（モデル揺らぎと書账層の回帰を分離するための二段構成）。

責務は狭く保つ：process_pipeline 呼出・page_bytes 除去・出力のみ。
書账層のロジックは一切持ち込まない。
"""
import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone, timedelta
from functools import lru_cache

from doc_types import DocType

JST = timezone(timedelta(hours=9))

FIXTURE_KEYS = ("page_num", "total_pages", "result")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@lru_cache(maxsize=1)
def git_commit():
    """HEAD の SHA（プロセス内で不変なのでキャッシュ。golden_replay からも使う）。"""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, check=True).stdout.strip()
    except Exception as e:
        return f"<unavailable: {e}>"


def _gemini_model(pipeline):
    """実際に使われるモデル名を best-effort で取る（記録目的、失敗しても止めない）。

    pipeline 注入時（＝単測）は真経路を使わないのでモデル名に意味が無く、かつ
    ocr_engine の import は paddleocr／genai クライアント構築を伴う重い連鎖なので
    踏まない。
    """
    if pipeline is not None:
        return "<injected-pipeline>"
    try:
        import ocr_engine
        return getattr(ocr_engine.model, "model_name", "<unknown>")
    except Exception as e:
        return f"<unavailable: {e}>"


def _default_pipeline():
    """真 Gemini 経路。テストは pipeline 引数で差し替えるためここへ来ない。"""
    from ocr_engine import process_pipeline
    return process_pipeline


def capture(path, doc_type=DocType.RECEIPT, strategy=None, pipeline=None,
            start_page=1):
    """1 ファイルを capture して (fixture, manifest) を返す。

    fixture は yield 順をそのまま保持する（同頁複数票の連続も含む）。
    _page_error 頁も素通しで記録する——skip 判断は replay 側の責務。
    """
    runner = pipeline or _default_pipeline()
    if strategy is None:
        from config import OCR_STRATEGY
        strategy = OCR_STRATEGY

    fixture = []
    for page in runner(path, doc_type=doc_type, ocr_strategy=strategy,
                       start_page=start_page):
        fixture.append({k: page.get(k) for k in FIXTURE_KEYS})

    manifest = {
        "source_file": os.path.basename(path),
        "source_sha256": _sha256(path),
        "commit": git_commit(),
        "strategy": strategy,
        "gemini_model": _gemini_model(pipeline),
        "captured_at": datetime.now(JST).isoformat(timespec="seconds"),
        "page_count": len(fixture),
    }
    return fixture, manifest


def write_outputs(fixture, manifest, out_dir, manifest_dir):
    """fixture は out_dir（gitignore）へ、manifest は manifest_dir（仓追跡）へ。"""
    stem = os.path.splitext(manifest["source_file"])[0]
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(manifest_dir, exist_ok=True)

    fixture_path = os.path.join(out_dir, f"{stem}.fixture.json")
    manifest_path = os.path.join(manifest_dir, f"{stem}.manifest.json")
    with open(fixture_path, "w", encoding="utf-8") as f:
        json.dump(fixture, f, ensure_ascii=False, indent=2)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return fixture_path, manifest_path


def main(argv=None, pipeline=None):
    """CLI 入口。pipeline は T3 実行経路そのものを単測で駆動するための注入点。"""
    parser = argparse.ArgumentParser(
        description="黄金様本 fixture の capture（真 Gemini を消費する）")
    parser.add_argument("path", help="対象 PDF / 画像のパス")
    parser.add_argument("--doc-type", default=DocType.RECEIPT,
                        choices=sorted(DocType.ALL))
    parser.add_argument("--strategy", default=None,
                        help="既定＝config.OCR_STRATEGY")
    parser.add_argument("--start-page", type=int, default=1,
                        help="断点続 capture（local_test.py と同口径）")
    parser.add_argument("--out", default="golden", help="fixture 出力先")
    parser.add_argument("--manifest-out", default="golden_manifest",
                        help="manifest 出力先（仓追跡）")
    args = parser.parse_args(argv)

    fixture, manifest = capture(args.path, args.doc_type,
                                strategy=args.strategy, pipeline=pipeline,
                                start_page=args.start_page)
    fixture_path, manifest_path = write_outputs(
        fixture, manifest, args.out, args.manifest_out)
    print(f"fixture : {fixture_path} ({manifest['page_count']} 頁)")
    print(f"manifest: {manifest_path}")
    return fixture_path, manifest_path


if __name__ == "__main__":
    main()
