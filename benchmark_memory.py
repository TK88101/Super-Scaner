"""B5 ③ 内存峰値の採取層（親監視プロセス ＋ 独立 worker）。

Plan＝docs/plans/2026-08-10-b5-benchmark-ss.md §4.3／§5 T7・T8。

口径は Codex 対抗評審の裁決に従う：

- **主指標＝「同一時刻に生存するプロセス木の RSS 合計」の最大値**。
  `pdftoppm` は `Popen` の子プロセスなので、親の RSS だけを見ると大件の峰値を
  系統的に過小評価する（評審 候補#3）。
- **各プロセスの高水位を足し合わせない**（評審 #5・#7）。異なる時刻に立った
  峰値を重畳した数字は、実際には一度も存在しなかった使用量になる。
  RSS 合計が共有頁を二重に数える点も報告に明記すること。
- **Mac と Windows の数値を直接比較しない**。`ru_maxrss`（macOS・**バイト**）と
  `peak_wset`／`peak_pagefile`（Windows・プロセス毎の生涯高水位）は対象も時間
  意味も違う。附加指標として別枠で出す。
- **合格判定は峰値の一致ではなく worker の終了状態**（評審 #8）。OOM のとき
  worker は OS に殺されて自分では報告を書けないので、親が別プロセスとして
  終了コードと信号を記録する。「欄が埋まった」は OOM しなかった証明にならない。
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field


def tree_rss_bytes(pid: int) -> int:
    """指定 PID とその全子孫の、**その瞬間の** RSS 合計（バイト）。

    走査中にプロセスが消えるのは正常（短命な pdftoppm など）なので、
    消えた分は 0 として続行する。存在しない PID は 0。
    """
    import psutil

    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return 0

    total = 0
    try:
        procs = [root] + root.children(recursive=True)
    except psutil.Error:
        return 0

    for proc in procs:
        try:
            total += proc.memory_info().rss
        except psutil.Error:
            continue  # 採取中に終了した＝この瞬間には生きていない
    return total


def platform_peak_metrics() -> dict:
    """プラットフォーム固有の高水位指標（**附加**であって主指標ではない）。

    主指標（`peak_bytes`）はここに入れない——意味の違う数字を同じ名前で
    並べると比較してしまうから。
    """
    metrics: dict = {"platform": sys.platform}

    if sys.platform == "win32":
        try:
            import psutil

            info = psutil.Process().memory_info()
            metrics["peak_wset_bytes"] = getattr(info, "peak_wset", None)
            metrics["peak_pagefile_bytes"] = getattr(info, "peak_pagefile", None)
            metrics["private_bytes"] = getattr(info, "private", None)
        except Exception:  # noqa: BLE001
            metrics["error"] = "psutil から Windows 指標を取得できず"
    else:
        try:
            import resource

            raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # macOS はバイト、Linux は KB（本 session で macOS 側を実測確認）
            metrics["ru_maxrss_bytes"] = raw if sys.platform == "darwin" else raw * 1024
        except Exception:  # noqa: BLE001
            metrics["error"] = "resource から ru_maxrss を取得できず"

    return metrics


@dataclass
class SampleResult:
    """worker 1 回分の採取結果。

    peak_bytes は「同一時刻の木の RSS 合計」の最大値。sample_count と
    interval_sec を必ず残すのは、短命な子プロセスを取りこぼした可能性を
    後から判断できるようにするため。
    """

    peak_bytes: int
    sample_count: int
    interval_sec: float
    exit_code: int | None
    killed_by_signal: int | None
    platform_peak: dict = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """終了コード 0 かつ信号で殺されていないこと。"""
        return self.exit_code == 0 and self.killed_by_signal is None


def run_sampled(argv, interval_sec: float = 0.2) -> SampleResult:
    """worker を独立プロセスで起こし、木の RSS を周期採取しながら完了を待つ。

    OOM で worker が落ちても親は生き残って記録できる——これが worker 内で
    自己計測しない理由（評審 #8）。
    """
    proc = subprocess.Popen(argv)

    peak = 0
    samples = 0
    while proc.poll() is None:
        peak = max(peak, tree_rss_bytes(proc.pid))
        samples += 1
        time.sleep(interval_sec)

    # 終了直後にもう一度読む（最後の瞬間を取りこぼさないため。既に消えていれば 0）
    peak = max(peak, tree_rss_bytes(proc.pid))
    samples += 1

    returncode = proc.returncode
    if returncode is not None and returncode < 0:
        # POSIX の慣習：信号で終了した場合 returncode は負の信号番号
        killed_by, exit_code = -returncode, None
    else:
        killed_by, exit_code = None, returncode

    return SampleResult(
        peak_bytes=peak,
        sample_count=samples,
        interval_sec=interval_sec,
        exit_code=exit_code,
        killed_by_signal=killed_by,
        platform_peak=platform_peak_metrics(),
    )


# --------------------------------------------------------------------------
# 実行入口（T11／T12）
#
# 同じファイルが二役を担う：
#   --worker    … 実際に PDF を食う側（OOM ならここが OS に殺される）
#   --supervise … worker を起こして木の RSS を採る側（殺されても生き残る）
# --------------------------------------------------------------------------

def _worker_main(args) -> int:
    """S-A シナリオ：現行 UI 生産形状で 1 檔を最後まで消費する。

    `HEADLESS_MODE` は**立てない**——478 頁 OOM は契約 headless（常に単頁）
    ではなく現行 UI 多頁経路の問題だから（K6・評審 #4）。
    """
    import os

    import main
    from doc_types import DocType
    from sheets_output import SheetsOutputWriter

    writer = SheetsOutputWriter(
        args.spreadsheet,
        os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json"))

    main.process_file(
        service=None, sheets_writer=writer, file_path=args.input,
        uploader_name="benchmark-mem", chat_id=None,
        doc_type=DocType.RECEIPT, drive_file_id=None)
    return 0


def _supervise_main(args) -> int:
    import json
    import os
    import subprocess as sp

    argv = [sys.executable, os.path.abspath(__file__), "--worker",
            "--input", args.input, "--spreadsheet", args.spreadsheet]
    print(f"worker 起動: {os.path.basename(args.input)}（採取周期 {args.interval}s）")
    result = run_sampled(argv, interval_sec=args.interval)

    try:
        from pypdf import PdfReader
        page_count = len(PdfReader(args.input).pages)
    except Exception:  # noqa: BLE001
        page_count = None

    try:
        git_sha = sp.check_output(["git", "rev-parse", "--short", "HEAD"],
                                  text=True).strip()
    except Exception:  # noqa: BLE001
        git_sha = "unknown"

    payload = {
        "scenario": "S-A（UI 多頁形状・HEADLESS_MODE 未設定）",
        "input_path": args.input,
        "page_count": page_count,
        "peak_bytes": result.peak_bytes,
        "peak_mb": round(result.peak_bytes / 1024 / 1024, 1),
        "sample_count": result.sample_count,
        "interval_sec": result.interval_sec,
        "exit_code": result.exit_code,
        "killed_by_signal": result.killed_by_signal,
        "succeeded": result.succeeded,
        "platform_peak": result.platform_peak,
        "git_sha": git_sha,
    }

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2))

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    # 合格判定は峰値ではなく worker の終了状態（評審 #8）
    return 0 if result.succeeded else 1


def _prefix_pdf(src: str, pages: int, dest: str) -> str:
    """src の先頭 pages 頁だけを持つ PDF を作る（頁数档の生成）。"""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(src)
    writer = PdfWriter()
    for page in reader.pages[:pages]:
        writer.add_page(page)
    with open(dest, "wb") as f:
        writer.write(f)
    return dest


def _curve_main(args) -> int:
    """頁数を振って峰値曲線を採る（趙 2026-08-11 裁定＝点値ではなく傾き）。

    同一ファイルの先頭 N 頁を使う——票面の内容が同源なので「档ごとに難易度が
    違う」という交絡を持ち込まずに、頁数だけを動かせる。
    """
    import json
    import os
    import subprocess as sp

    os.makedirs(args.out, exist_ok=True)
    steps = [int(x) for x in args.pages.split(",")]
    results = []

    for pages in steps:
        clipped = os.path.join(args.out, f"prefix_{pages:04d}p.pdf")
        _prefix_pdf(args.input, pages, clipped)
        argv = [sys.executable, os.path.abspath(__file__), "--worker",
                "--input", clipped, "--spreadsheet", args.spreadsheet]
        print(f"--- {pages} 頁档 開始 ---", flush=True)
        result = run_sampled(argv, interval_sec=args.interval)
        row = {
            "pages": pages,
            "peak_bytes": result.peak_bytes,
            "peak_mb": round(result.peak_bytes / 1024 / 1024, 1),
            "sample_count": result.sample_count,
            "exit_code": result.exit_code,
            "killed_by_signal": result.killed_by_signal,
            "succeeded": result.succeeded,
        }
        results.append(row)
        print(f"--- {pages} 頁档 完了: {row['peak_mb']} MB "
              f"({'正常' if result.succeeded else '異常終了'}) ---", flush=True)

    try:
        git_sha = sp.check_output(["git", "rev-parse", "--short", "HEAD"],
                                  text=True).strip()
    except Exception:  # noqa: BLE001
        git_sha = "unknown"

    payload = {
        "scenario": "S-A 頁数-峰値曲線（UI 多頁形状・HEADLESS_MODE 未設定）",
        "source_path": args.input,
        "interval_sec": args.interval,
        "platform_peak": platform_peak_metrics(),
        "git_sha": git_sha,
        "points": results,
    }
    with open(os.path.join(args.out, "curve.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2))

    print("\n=== 頁数-峰値 ===")
    for row in results:
        print(f"  {row['pages']:4d} 頁 → {row['peak_mb']:8.1f} MB"
              f"  {'' if row['succeeded'] else '★異常終了'}")

    return 0 if all(r["succeeded"] for r in results) else 1


def _env_main(args) -> int:
    """測定機の素性を吐く（Mac の数字を持ち込めるか判定するための前提）。

    物理内存＝天井、単核性能＝耗時の可比性、依存版＝そもそも同じ物を測って
    いるかを決める。これが無いまま測定方案は組めない。
    """
    import json
    import os
    import platform as pf
    import subprocess as sp

    info = {
        "platform": pf.platform(),
        "machine": pf.machine(),
        "processor": pf.processor(),
        "python": sys.version.split()[0],
        "python_bits": 64 if sys.maxsize > 2**32 else 32,
    }

    try:
        import psutil

        info["cpu_count_physical"] = psutil.cpu_count(logical=False)
        info["cpu_count_logical"] = psutil.cpu_count(logical=True)
        vm = psutil.virtual_memory()
        info["total_memory_mb"] = round(vm.total / 1024 / 1024)
        info["available_memory_mb"] = round(vm.available / 1024 / 1024)
        try:
            info["swap_total_mb"] = round(psutil.swap_memory().total / 1024 / 1024)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        info["psutil_error"] = str(exc)

    packages = {}
    for name in ("paddleocr", "paddlepaddle", "paddlex", "pypdf", "numpy",
                 "pdf2image", "opencv-python-headless", "opencv-contrib-python",
                 "psutil", "gspread"):
        try:
            from importlib.metadata import version

            packages[name] = version(name)
        except Exception:  # noqa: BLE001
            packages[name] = None
    info["packages"] = packages

    # pdftoppm（poppler）はラスタ化の実行主体。版が違えば内存挙動も変わる
    try:
        out = sp.run(["pdftoppm", "-v"], capture_output=True, text=True)
        info["pdftoppm"] = (out.stderr or out.stdout).strip().splitlines()[0]
    except Exception:  # noqa: BLE001
        info["pdftoppm"] = "見つからない（pdf2image が動かない可能性）"

    # 生産の既定値（測定時に変えていないことの証跡にもなる）
    info["ocr_env"] = {k: os.getenv(k) for k in
                       ("OCR_MODEL_TIER", "OCR_MAX_SIDE", "OCR_STRATEGY",
                        "OCR_CONFIDENCE_THRESHOLD", "HEADLESS_MODE")}

    print(json.dumps(info, ensure_ascii=False, indent=2))
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="B5 ③ 内存峰値の採取")
    parser.add_argument("--env", action="store_true",
                        help="測定機の素性を吐く（配置調査）")
    parser.add_argument("--worker", action="store_true", help="被測側として走る")
    parser.add_argument("--supervise", action="store_true", help="監視側として走る")
    parser.add_argument("--curve", action="store_true",
                        help="頁数を振って峰値曲線を採る")
    parser.add_argument("--pages", default="1,5,10,20,35",
                        help="--curve の頁数档（カンマ区切り）")
    parser.add_argument("--input", help="入力 PDF（--env では不要）")
    parser.add_argument("--spreadsheet", help="測定用 Sheets ID（--env では不要）")
    parser.add_argument("--interval", type=float, default=0.2, help="採取周期（秒）")
    parser.add_argument("--out", default=None, help="結果 JSON の出力先")
    args = parser.parse_args(argv)

    if args.env:
        return _env_main(args)
    if not args.input or not args.spreadsheet:
        parser.error("--input と --spreadsheet は測定モードでは必須")
    if args.worker:
        return _worker_main(args)
    if args.supervise:
        return _supervise_main(args)
    if args.curve:
        return _curve_main(args)
    parser.error("--worker / --supervise / --curve のいずれかを指定すること")


if __name__ == "__main__":
    raise SystemExit(main())
