"""B5 ① 単頁 SS データ面コンポーネント耗時の測定ハーネス。

Plan＝docs/plans/2026-08-10-b5-benchmark-ss.md §4.1／§5 T4・T5。

**「端到端 P99」と名乗らない**——Firestore 往復を fake transport へ差し替える
以上、契約の言う整链 E2E ではない（評審 #11）。契約閾値への回填は U14 就緒後の
真整链補測を経てから。

設計の要点（いずれも Codex 対抗評審の裁決）：

- **計時は `main.process_file` の前後**（評審 #1）。旧案の「generator の yield 時刻
  →`commit_page` 返り」は二重に誤りだった：yield 時点では OCR も Gemini も整形も
  既に終わっており、`commit_page` は `ledger.post_page` のコールバックとして
  内側で呼ばれる（`main.py:898`）ので「頁 flush 完了」を指さない。
- **`process_pipeline` を patch しない**（評審 #17）。`main.py:20` は
  `from ocr_engine import (...)` の局所束縛なので `ocr_engine` 側を包んでも
  `main` には効かない。そもそも真 pipeline を走らせるので patch 自体が不要。
- **切出は計時区間の外**（評審 #2）。契約 headless の入力は 1 頁／1 切片であり、
  多頁を食わせると UI 多頁形状の数字になる。
- **自作 ledger 代替を使わない**（評審 #11）。真 `PostingLedger` に
  `FakeFirestore` を挿す——既存の `headless_rerun_fixture.make_ledger` と同じ組立。
- **試行は必ず 1 レコード産む**（評審 #9）。例外で落ちた頁を黙って母数から
  外さない。
"""

from __future__ import annotations

import os
import time

import benchmark_preflight as _preflight_mod

# 測定用の固定投稿者名（Sheets tab キーに使われる。生産の従業員名と衝突させない）
BENCHMARK_UPLOADER = "benchmark"

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")


def split_to_single_pages(paths, out_dir: str) -> list[str]:
    """多頁 PDF を単頁ファイルへ切り出す（計時区間の**外**で呼ぶこと）。

    画像はそのまま 1 単位として通す。出力名に元ファイル名と頁番号を残し、
    後から「どの頁がどの原本の何頁目か」を辿れるようにする。
    """
    os.makedirs(out_dir, exist_ok=True)
    produced: list[str] = []

    for src in paths:
        stem = os.path.splitext(os.path.basename(src))[0]
        if src.lower().endswith(_IMAGE_SUFFIXES):
            dest = os.path.join(out_dir, os.path.basename(src))
            if os.path.abspath(dest) != os.path.abspath(src):
                with open(src, "rb") as rf, open(dest, "wb") as wf:
                    wf.write(rf.read())
            produced.append(dest)
            continue

        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(src)
        for index, page in enumerate(reader.pages, start=1):
            writer = PdfWriter()
            writer.add_page(page)
            dest = os.path.join(out_dir, f"{stem}__p{index:04d}.pdf")
            with open(dest, "wb") as f:
                writer.write(f)
            produced.append(dest)

    return produced


class _KindCapturingReporter:
    """真 reporter を包み、頁級 kind を測定側へ渡すだけの薄い層。

    kind は `main` 内部のタグで戻り値には出てこない（`process_file` が返すのは
    檔級 5 態で、「全頁除外＝SUCCESS」なので POSTED と EXCLUDED を区別できない）。
    書込は本物へそのまま委譲するので、fake Firestore 上の記録内容は変わらない。
    """

    def __init__(self, inner):
        self._inner = inner
        self.kinds: list[str] = []

    def record_page(self, page_num, kind, detail=None):
        self.kinds.append(kind)
        return self._inner.record_page(page_num, kind, detail)

    def __getattr__(self, name):
        # 檔終局の補写など、main が呼ぶ他のメソッドは素通しする
        return getattr(self._inner, name)


def build_fake_backed_ledger(writer, base: str):
    """真 `PostingLedger` ＋ 真 `FirestorePageOutcomesReporter` ＋ fake transport。

    既存テスト基盤（`fake_firestore.FakeFirestore`）を再利用する——
    `PostingLedger.__init__` の docstring が「client: firestore.Client 或鴨子型
    fake」と明記している通り、fake 注入は設計内の使い方であって迂回ではない。
    """
    from fake_firestore import FakeFirestore
    from firestore_progress import FirestorePageOutcomesReporter
    from posting_ledger import PostingLedger

    fs = FakeFirestore()
    ledger = PostingLedger(
        fs, base, sheet_probe=writer.probe_page, transaction_runner=fs.runner())
    page_outcomes = FirestorePageOutcomesReporter(fs, base)
    return ledger, page_outcomes, fs


def _invoke_process_file(service, writer, path, doc_type, base, ledger,
                         page_outcomes, tab_owner):
    """`main.process_file` を headless 経路で 1 回呼び、(kind, error) を返す。

    単体テストではここを差し替える（重い import と実 API 呼出を避けるため）。
    実測での正しさは T10 の実行そのものが担保する。
    """
    import main

    main.process_file(
        service=service, sheets_writer=writer, file_path=path,
        uploader_name=BENCHMARK_UPLOADER, chat_id=None, doc_type=doc_type,
        drive_file_id=None, base=base, ledger=ledger,
        page_outcomes=page_outcomes, tab_owner=tab_owner or BENCHMARK_UPLOADER)

    kinds = getattr(page_outcomes, "kinds", [])
    if not kinds:
        # 契約 headless は 1 入力＝1 頁なので、kind が 1 件も出ないのは異常。
        # 「不明」として残す——黙って母数から落とさない
        return "UNKNOWN", "頁級 kind が記録されなかった"
    if len(kinds) > 1:
        return kinds[0], f"想定外：1 入力で {len(kinds)} 頁分の kind が出た"
    return kinds[0], None


def measure_one(path, *, writer, doc_type, base, clock=time.perf_counter,
                preflight=None, service=None, tab_owner=None):
    """1 入力を測って 1 レコードを返す。

    preflight が落ちた場合は**レコードを作らず例外を上げる**——前提が破れた
    状態の数字は母数に入れてはいけない（測ってよいかの門であって、結果の
    一種ではない）。逆に処理中の例外は UNKNOWN として必ず 1 件残す。
    """
    preflight_fn = preflight or _preflight_mod.run_preflight
    meta = preflight_fn(path)

    ledger, page_outcomes, _fs = build_fake_backed_ledger(writer, base)
    capture = _KindCapturingReporter(page_outcomes)

    started = clock()
    try:
        kind, error = _invoke_process_file(
            service, writer, path, doc_type, base, ledger, capture, tab_owner)
    except Exception as exc:  # noqa: BLE001 — 試行を消さないための総捕捉
        kind, error = "UNKNOWN", f"{type(exc).__name__}: {exc}"
    finished = clock()

    return {
        "input_path": path,
        "outcome": kind,
        "elapsed_sec": finished - started,
        "error": error,
        "page_count": meta.get("page_count"),
        "base": base,
    }


# --------------------------------------------------------------------------
# 実行入口（T10）
# --------------------------------------------------------------------------

def collect_unique_samples(sample_dir: str) -> list[str]:
    """様本ディレクトリから内容ハッシュで重複を除いたファイル一覧を返す。

    同一内容の副本は独立観測にならない（評審 #13）——重み付けが歪むので、
    切出の前に落とす。
    """
    import hashlib

    seen: dict[str, str] = {}
    for name in sorted(os.listdir(sample_dir)):
        path = os.path.join(sample_dir, name)
        if not os.path.isfile(path) or name.startswith("."):
            continue
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        seen.setdefault(digest, path)
    return list(seen.values())


def _sha256_of(path: str) -> str:
    import hashlib

    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def run_suite(pages, writer, doc_type, out_dir, *, seed=20260811, limit=None):
    """全頁を測って生データを落とす。warmup 1 頁は母数に入れない。

    実行順はランダム化する（Sheets の行増加や Drive キャッシュの効果が
    特定の標本に偏らないように。評審 #13）。順序は seed 固定で再現可能。
    """
    import platform
    import random
    import subprocess

    import benchmark_record as br

    ordered = list(pages)
    random.Random(seed).shuffle(ordered)
    if limit:
        ordered = ordered[:limit]

    # warmup：PaddleOCR は遅延生成なので、初回だけモデル読込が耗時に混ざる
    # （smoke 実測で 26.9 秒のうち相当分がこれ）。1 頁空回しして母数から外す。
    warmup_done = False
    if ordered:
        try:
            measure_one(ordered[0], writer=writer, doc_type=doc_type,
                        base="bench-warmup")
            warmup_done = True
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ warmup 失敗（測定は続行）: {exc}")

    records = []
    for index, page in enumerate(ordered, start=1):
        base = f"bench-{index:04d}"
        print(f"[{index}/{len(ordered)}] {os.path.basename(page)}")
        try:
            record = measure_one(page, writer=writer, doc_type=doc_type, base=base)
        except Exception as exc:  # noqa: BLE001 — preflight 落ちも記録に残す
            print(f"  ⚠️ 測定不能: {exc}")
            continue
        record["sha256"] = _sha256_of(page)
        records.append(record)

    try:
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        git_sha = "unknown"

    meta = {
        "git_sha": git_sha,
        "platform": platform.platform(),
        "headless_mode": True,
        "attempted": len(records),
        "quantile_algorithm": "nearest-rank",
        "p99_min_samples": 100,
        "warmup_done": warmup_done,
        "seed": seed,
    }

    os.makedirs(out_dir, exist_ok=True)
    br.write_jsonl(os.path.join(out_dir, "records.jsonl"), records)
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        import json as _json
        f.write(_json.dumps(meta, ensure_ascii=False, indent=2))

    return records, meta


def main(argv=None):
    import argparse

    from doc_types import DocType
    from sheets_output import SheetsOutputWriter

    parser = argparse.ArgumentParser(description="B5 ① 単頁耗時の測定")
    parser.add_argument("--samples", required=True, help="様本ディレクトリ")
    parser.add_argument("--out", required=True, help="出力ディレクトリ")
    parser.add_argument("--spreadsheet", required=True, help="測定用 Sheets ID")
    parser.add_argument("--limit", type=int, default=None, help="先頭 N 件のみ")
    args = parser.parse_args(argv)

    sources = collect_unique_samples(args.samples)
    pages_dir = os.path.join(args.out, "pages")
    pages = split_to_single_pages(sources, pages_dir)
    print(f"様本 {len(sources)} 件（重複除去済）→ {len(pages)} 頁")

    with _preflight_mod.headless_env():
        writer = SheetsOutputWriter(
            args.spreadsheet,
            os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json"),
            tab_namer=lambda owner, doc_type: owner)
        records, meta = run_suite(pages, writer, DocType.RECEIPT, args.out,
                                  limit=args.limit)

    import benchmark_record as br
    import benchmark_stats as bs

    issues = br.validate_records(records, meta)
    print(f"\n=== validator: {'合格' if not issues else str(len(issues)) + ' 件の問題'}")
    for issue in issues:
        print("  -", issue)

    result = bs.stratify(records)
    overall = result.overall
    print(f"\n=== 全体 {overall.sample_count} 件 ===")
    print(f"  P50={overall.p50:.2f}s  max={overall.max_value:.2f}s"
          if overall.p50 is not None else "  (標本なし)")
    print(f"  P99={'算出可' if overall.p99_reportable else '標本不足で不可'}")
    for kind, summary in result.strata.items():
        if summary.sample_count:
            print(f"  {kind}: {summary.sample_count} 件 / P50={summary.p50:.2f}s")

    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
