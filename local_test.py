#!/usr/bin/env python3
"""
Super Scaner ローカルテストモード
Google Drive / Chatwork 不要。Gemini API のみで動作。

使い方:
  1. test_images/ の各サブフォルダにテストファイルを配置
     （サブフォルダは ensure_dirs() が自動生成する。手動作成は不要）
     - test_images/receipt/          ← 領収書
     - test_images/purchase_invoice/ ← 支払請求書・仕入請求書
     - test_images/sales_invoice/    ← 売上請求書
     - test_images/salary_slip/      ← 賃金台帳・給与明細書
     - test_images/credit_card/      ← クレジットカード利用明細書
     - test_images/transit_ic/       ← 交通系IC利用履歴（nimoca 等）
  2. python3 local_test.py
  3. MF_Import_Data.csv を確認

注意（本番との差異）:
  本番では nimoca はクレカと同じフォルダに混載し、プログラムが頁単位で
  分流する（趙裁定 5、doc_types.py の ENV_FOLDER_MAP 註釈）。
  test_images/transit_ic/ はローカルで nimoca 単独を試すための入口であって、
  本番の構成ではない。

  投入するファイルは複製にすること。成功したファイルは
  test_images/processed/ へ shutil.move される（原本が消費される）。
"""

import os
import sys
import shutil
from dotenv import load_dotenv

# Windows console encoding fix
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

from ocr_engine import process_pipeline
from sheets_output import (AUDIT_VERDICT_BRANCH, AUDIT_VERDICT_EXCLUDED,
                           AUDIT_VERDICT_MISSING, SheetsOutputWriter)
from ocr_engine import EXCLUDE_DEST_AUDIT_TAB, EXCLUDE_DEST_MF_TAB
from doc_types import DocType, DOC_TYPE_CONFIG
import config
import argparse

# ================= 設定 =================
TEST_DIR = "./test_images"
PROCESSED_DIR = os.path.join(TEST_DIR, "processed")

# サブフォルダ名 → DocType マッピング
#
# doc_type 並行登録表の 7 枚目。`DocType.ALL` 全件を覆うことが
# `local_test` の契約であり、`test_local_test_folder_map.py` が縛っている。
# 登録漏れは `scan_local_files` が黙って素通りするため無音で発覚しない。
FOLDER_TYPE_MAP = {
    "receipt": DocType.RECEIPT,
    "purchase_invoice": DocType.PURCHASE_INVOICE,
    "sales_invoice": DocType.SALES_INVOICE,
    "salary_slip": DocType.SALARY_SLIP,
    "credit_card": DocType.CREDIT_CARD,
    "transit_ic": DocType.TRANSIT_IC,
}
# =========================================


def ensure_dirs():
    """テスト用ディレクトリを作成"""
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    for folder_name in FOLDER_TYPE_MAP:
        os.makedirs(os.path.join(TEST_DIR, folder_name), exist_ok=True)


def scan_local_files():
    """各サブフォルダからテストファイルを収集"""
    files = []
    for folder_name, doc_type in FOLDER_TYPE_MAP.items():
        folder_path = os.path.join(TEST_DIR, folder_name)
        if not os.path.isdir(folder_path):
            continue
        for fname in sorted(os.listdir(folder_path)):
            ext = os.path.splitext(fname)[1].lower()
            if ext in config.SUPPORTED_EXTENSIONS:
                files.append({
                    "path": os.path.join(folder_path, fname),
                    "name": fname,
                    "doc_type": doc_type,
                })
    return files


def _unrecognized_placeholder(file_name, memo):
    """MF 区に書く「認識不能」占位行の entries_data。

    `main._build_unrecognized_placeholder`（main.py:338-352）と同形。
    同じ形状が本ファイル内の 3 箇所（除外ページの MF 提示行・監査失敗の
    退避行・部分ページエラーの占位行）に現れるため、逐字コピーだと
    契約変更時の同期漏れを招く。vendor にファイル名を入れるのは、
    行だけ見てどの原票の話か分かるようにするため。
    """
    return {
        "entries": [],
        "_unrecognized": True,
        "memo": memo,
        "date": "",
        "vendor": file_name,
        "uploader": "LocalTest",
    }


def process_local_file(file_info, sheets_writer, strategy=None, start_page=1):
    """1ファイルを処理: OCR → Google Sheets 書き込み"""
    file_path = file_info["path"]
    doc_type = file_info["doc_type"]
    file_name = file_info["name"]
    type_label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)

    print(f"\n{'='*50}")
    print(f"📄 ファイル: {file_name}")
    print(f"📋 文書タイプ: {type_label}")
    print(f"{'='*50}")

    # PaddleOCR + Gemini（ジェネレータ: 逐次処理）
    count = 0
    total_entries = 0
    error_pages = 0
    failed_page_nums = []
    excluded_pages = 0
    excluded_page_nums = []
    first_write_done = False

    # 頁カバレッジ突合用（`main.process_file:502-510` と同じ持ち方）。
    # `continue` する経路より**前**で数える —— 失敗頁も除外頁も
    # 「出力された頁」であり、欠落ではない。
    seen_page_nums = set()
    last_total_pages = 0

    for page in process_pipeline(file_path, doc_type=doc_type, ocr_strategy=strategy, start_page=start_page):
        r = page["result"]
        page_num = page["page_num"]
        total_pages = page["total_pages"]
        seen_page_nums.add(page_num)
        last_total_pages = total_pages
        count += 1
        # 再試可能なページエラーは Sheets へ書き込まない
        # （全頁失敗時は Failed を返しファイルを保持するため、
        #  次回再試行で同じページの占位行が重複生成されるのを防ぐ）
        if r.get("_page_error"):
            error_pages += 1
            failed_page_nums.append(page_num)
            continue

        # IP-401: 除外ページは main.process_file と同じ語義で振り分ける。
        # ここを実装し忘れると、封筒ページが entries=[] かつ _unrecognized 無しの
        # ため sheets_output の最終防衛に落ち、MF 区に赤い認識不能行として
        # 混入する（実際に本 smoke test で発覚した）。process_pipeline の消費者は
        # main.process_file と本関数の2つあり、片方だけ直すと必ず漂移する
        # ——CLAUDE.md の ENTRY_BUILDERS 未登録事故と同族。
        if r.get("_excluded_page"):
            reason = r.get("_exclude_reason", "unknown")
            destination = r.get("_exclude_destination", EXCLUDE_DEST_AUDIT_TAB)
            # 書込失敗時の扱いは行き先で違う（`main._record_excluded_page`
            # と同語義）。裸で呼ぶと、除外頁 1 つの書込失敗が
            # process_local_file ごと例外にして残りの頁の処理も止める。
            recorded = True
            if destination == EXCLUDE_DEST_MF_TAB:
                print(f"🏥 [{page_num}/{total_pages}] 除外ページ ({reason}) "
                      f"→ MFタブに提示行（仕訳は作成しません）")
                if not first_write_done:
                    sheets_writer.start_new_file("LocalTest", doc_type, file_name)
                    first_write_done = True
                try:
                    sheets_writer.append_entries(
                        employee_name="LocalTest",
                        doc_type=doc_type,
                        entries_data=_unrecognized_placeholder(
                            file_name, r.get("memo", "")),
                        source_url="",
                    )
                except Exception as e:
                    # この提示行はこのページの**唯一の出力**。握り潰して
                    # 除外扱いにすると、顧客は上げたことも記帳されなかった
                    # ことも知る術がなくなる（main.py:392-398）。
                    print(f"❌ 除外ページの提示行の書き込み失敗 ({reason}): {e}")
                    recorded = False
            else:
                print(f"📨 [{page_num}/{total_pages}] 除外ページ ({reason}) "
                      f"→ 監査タブに記録（MF区には書きません）")
                try:
                    sheets_writer.append_audit_row(
                        filename=file_name,
                        page_num=page_num,
                        verdict=AUDIT_VERDICT_EXCLUDED,
                        reason=reason,
                        ocr_text_len=r.get("_ocr_text_len", 0),
                        source_url="",
                    )
                except Exception as e:
                    # §3.7: 真の除外は監査タブが唯一の留痕。失敗したら MF の
                    # 赤い認識不能行へ退避して必ず可視化する（無音欠落に
                    # 戻さない）。退避も落ちたらログに残すしかない。
                    print(f"⚠️ 監査タブ書き込み失敗 → MF区の認識不能行へ退避: {e}")
                    try:
                        if not first_write_done:
                            sheets_writer.start_new_file(
                                "LocalTest", doc_type, file_name)
                            first_write_done = True
                        sheets_writer.append_entries(
                            employee_name="LocalTest",
                            doc_type=doc_type,
                            entries_data=_unrecognized_placeholder(
                                file_name,
                                f"⚠ 除外ページ p{page_num} ({reason}) "
                                f"監査タブ書き込み失敗のため退避"),
                            source_url="",
                        )
                    except Exception as e2:
                        print(f"❌ 除外ページの退避行も書き込めませんでした"
                              f"（留痕なし）: {e2}")

            if not recorded:
                # 留痕を残せなかった除外は「静かに消えた」のと同じ。
                # 除外として数えず失敗として扱う（main.py:547-559）。
                error_pages += 1
                failed_page_nums.append(page_num)
                continue
            excluded_pages += 1
            excluded_page_nums.append(page_num)
            continue

        if total_pages > 1:
            print(f"\n  📄 文書 [{page_num}/{total_pages}]: {r.get('vendor', '不明')}")

        entries = r.get("entries", [])
        total_entries += len(entries)
        print(f"\n🎯 解析結果:")
        print(f"   📅 日付: {r.get('date')}")
        print(f"   🏪 取引先: {r.get('vendor')}")
        print(f"   📊 仕訳行数: {len(entries)}")

        for i, entry in enumerate(entries, 1):
            print(f"   [{i}] 借方: {entry.get('debit_account')} ¥{entry.get('amount')} "
                  f"({entry.get('debit_tax_type')}) → "
                  f"貸方: {entry.get('credit_account')} ({entry.get('credit_tax_type')})")

        # 即座に Google Sheets 書き込み（初回のみ分割線+No リセット）
        # count ではなく first_write_done で判定。先頭ページが _page_error で
        # スキップされた場合でも、最初の実書き込み時に必ず separator が入る。
        if not first_write_done:
            sheets_writer.start_new_file("LocalTest", doc_type, file_name)
            first_write_done = True
        r["uploader"] = "LocalTest"
        sheets_writer.append_entries(
            employee_name="LocalTest",
            doc_type=doc_type,
            entries_data=r,
            source_url="",
        )

        # `main.process_file:596-608` と同語義。記帳は済ませたが producer が
        # 注記を付けた頁（封筒シグナル命中、明細の截断→救済 `salvaged:N/M`）を
        # 監査タブへ「分岐」1 行で残す。
        # **これが無いと救済された頁が「綺麗に成功した頁」と区別できない**
        # —— `card_salvage.page_marks` は「shortage なし＋救済あり」を
        # 監査タブのみに落とす規定なので、MF 側には痕跡が一切出ない。
        # 書込順序は MF が先・監査が後（帳簿を人質に取らせない）。
        audit_signal = r.get("_audit_signal")
        if audit_signal:
            try:
                sheets_writer.append_audit_row(
                    filename=file_name,
                    page_num=page_num,
                    verdict=AUDIT_VERDICT_BRANCH,
                    reason=audit_signal,
                    ocr_text_len=r.get("_ocr_text_len", 0),
                    source_url="",
                )
            except Exception as e:
                # MF は既に正しく書けており帳簿は正しい。監査は諦める。
                print(f"⚠️ 監査タブへの分岐記録に失敗（記帳は成功）: {e}")

    # ページカバレッジ突合（`main.process_file:617-635` と同語義）。
    # 掃いた範囲の頁が 1 つでも出力されていなければ監査タブへ「欠落」を残す。
    # 成否判定は変えない（main 側の §8-中7 裁決に合わせる）。
    #
    # **起点だけ main と違う**: main は `start_page` を渡さないので
    # `range(1, ...)` で正しいが、`local_test` は `--start-page N` を持つ。
    # 1 起点を写経すると `--start-page 3` のたびに p1・p2 が「欠落」として
    # 記録され、留痕が狼少年になる（`docs/plans/2026-08-17-split-pdf-
    # midway-failure.md` の G8 が両者の差を記録している）。
    missing_pages = sorted(
        set(range(start_page, last_total_pages + 1)) - seen_page_nums)
    if missing_pages:
        print(f"⚠️ ページカバレッジ警告: {len(missing_pages)}/{last_total_pages}頁が"
              f"一度も出力されませんでした {missing_pages}")
        try:
            sheets_writer.append_audit_row(
                filename=file_name,
                page_num=missing_pages[0],
                verdict=AUDIT_VERDICT_MISSING,
                reason=f"page_coverage_gap:{missing_pages}",
                ocr_text_len=0,
                source_url="",
            )
        except Exception as e:
            print(f"⚠️ カバレッジ警告の監査タブ記録に失敗: {e}")

    # 除外ページの内訳（`main.py:679-681` と同形式）。これを出さないと
    # `excluded_pages` は数えるだけで誰も読まない死変数になり、
    # 「32 頁のうち何頁が封筒として除外されたか」が結果から読めない。
    if excluded_pages:
        excluded_pages_str = ",".join(f"p{n}" for n in excluded_page_nums)
        print(f"📨 除外ページ: {excluded_pages}/{count}頁 [{excluded_pages_str}]")

    if count == 0:
        print(f"❌ 解析失敗: {file_name}")
        return False

    # 全ページがエラー → 上流障害とみなし Failed（ファイル保持）
    # count == error_pages で判定（total_entries ではない）:
    # _unrecognized ページ（封筒・パンフレット等）も entries=0 を生むため、
    # total_entries==0 だけで判定すると「正常な _unrecognized + 1頁エラー」の
    # 混合ケースが無限再試行ループに入り、毎回占位行が重複増殖する。
    if count > 0 and error_pages == count:
        print(f"⚠️ 全ページ処理エラー: {error_pages}/{count} → Failed（ファイル保持）")
        return False

    # 部分ページエラー: 占位行を書き込んで可視化、ファイル自体は歸檔
    if error_pages > 0 and error_pages < count:
        failed_pages_str = ",".join(f"p{n}" for n in failed_page_nums)
        try:
            sheets_writer.append_entries(
                employee_name="LocalTest",
                doc_type=doc_type,
                entries_data=_unrecognized_placeholder(
                    file_name,
                    f"⚠ ページ処理エラー {error_pages}/{count}頁 "
                    f"[{failed_pages_str}] 手動再スキャン要"),
                source_url="",
            )
        except Exception as e:
            print(f"⚠️ 部分エラー占位行の書き込み失敗: {e}")
        print(f"⚠️ 部分ページエラー: {error_pages}/{count}頁失敗 [{failed_pages_str}]（ファイルは歸檔、失敗頁は手動再スキャン要）")

    # 処理済みフォルダへ移動
    dest = os.path.join(PROCESSED_DIR, file_name)
    if os.path.exists(dest):
        base, ext = os.path.splitext(file_name)
        dest = os.path.join(PROCESSED_DIR, f"{base}_dup{ext}")
    shutil.move(file_path, dest)
    print(f"📦 → processed/ へ移動完了")

    return True


def main():
    print("🚀 Super Scaner ローカルテストモード起動 (Sheets出力版)")

    parser = argparse.ArgumentParser(description="Super Scaner Local Test")
    parser.add_argument("--strategy", choices=["A", "B", "C"], default=None,
                        help="OCR strategy override (default: config.OCR_STRATEGY)")
    parser.add_argument("--start-page", type=int, default=1,
                        help="Resume from this page (1-indexed). Applied to all scanned files.")
    parser.add_argument("--only-file", type=str, default=None,
                        help="Only process files whose name contains this substring.")
    args = parser.parse_args()

    print(f"📂 テストフォルダ: {os.path.abspath(TEST_DIR)}")
    print("-" * 50)

    # Google Sheets 接続
    if not config.OUTPUT_SPREADSHEET_ID:
        print("❌ OUTPUT_SPREADSHEET_ID が未設定です。.env を確認してください。")
        return

    sa_file = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")
    sheets_writer = SheetsOutputWriter(
        spreadsheet_id=config.OUTPUT_SPREADSHEET_ID,
        credentials_file=sa_file,
    )
    print(f"✅ Google Sheets 接続完了")

    # ディレクトリ準備
    ensure_dirs()

    # ファイル収集
    files = scan_local_files()
    if args.only_file:
        files = [f for f in files if args.only_file in f["name"]]

    if not files:
        print("\n⚠️  テストファイルが見つかりません。")
        print("以下のフォルダにファイルを配置してください:")
        for folder_name, doc_type in FOLDER_TYPE_MAP.items():
            label = DOC_TYPE_CONFIG.get(doc_type, {}).get("label", doc_type)
            print(f"   📁 {TEST_DIR}/{folder_name}/  ← {label}")
        return

    print(f"\n🔎 {len(files)} 件のファイルを検出:")
    for f in files:
        label = DOC_TYPE_CONFIG.get(f["doc_type"], {}).get("label", f["doc_type"])
        print(f"   - [{label}] {f['name']}")

    # 処理実行
    success_count = 0
    fail_count = 0

    for file_info in files:
        try:
            if process_local_file(file_info, sheets_writer, strategy=args.strategy, start_page=args.start_page):
                success_count += 1
            else:
                fail_count += 1
        except Exception as e:
            print(f"❌ エラー: {file_info['name']} - {e}")
            fail_count += 1

    # 取引No を Sheets に書き戻す
    sheets_writer.flush()

    # サマリー
    print("\n" + "=" * 50)
    print(f"📊 処理結果サマリー")
    print(f"   ✅ 成功: {success_count} 件")
    print(f"   ❌ 失敗: {fail_count} 件")
    print(f"   📗 Sheets: https://docs.google.com/spreadsheets/d/{config.OUTPUT_SPREADSHEET_ID}/edit")
    print("=" * 50)


if __name__ == "__main__":
    main()
