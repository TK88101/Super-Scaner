# config.py
# 項目配置文件 - 這裡存放員工名單和其他靜態配置

import os
from doc_types import DocType, ENV_FOLDER_MAP

# === 出力先 Google Spreadsheet ID ===
# 分割 PDF / Processed フォルダは load_profiles() 経由で取得する（下部参照）。
OUTPUT_SPREADSHEET_ID = os.getenv("OUTPUT_SPREADSHEET_ID", "")
BACKUP_SPREADSHEET_ID = os.getenv("BACKUP_SPREADSHEET_ID", "")

# === OCR 戦略設定 ===
OCR_STRATEGY = os.getenv("OCR_STRATEGY", "C")
OCR_CONFIDENCE_THRESHOLD = float(os.getenv("OCR_CONFIDENCE_THRESHOLD", "0.7"))

# === PaddleOCR メモリ対策 ===
# PP-OCRv5 server モデルは CPU(特に macOS arm64)で ~20GB の内存床があり、
# 巨大スキャン(例: 600dpi級)で OOM(SIGKILL)する実測結果に基づく対策。
#   OCR_MODEL_TIER: "mobile"(既定/軽量) | "server"(高精度・高メモリ)
#   OCR_MAX_SIDE  : OCR 前に画像の最長辺をこの px まで縮小(0=無効)。
#     通常票は dpi150 で ~1754px のため未満で無影響。巨大スキャンのみ縮小し
#     メモリ暴走を防ぐ。strategy C では Gemini に原寸が渡るため精度影響は小。
OCR_MODEL_TIER = os.getenv("OCR_MODEL_TIER", "mobile")
OCR_MAX_SIDE = int(os.getenv("OCR_MAX_SIDE", "3000"))
# 記账复核门槛（規則②専用）。整票の OCR 置信度がこれ未満なら全行を黄で
# マークし人手複査を促す。OCR エンジン路由门槛（OCR_CONFIDENCE_THRESHOLD,
# 既定 OCR_STRATEGY=="C" では未読）とは語義が異なるため別定数にする。env 可調。
# 既定 0.65 に校正(実測6枚基準): 明瞭な印刷票は ~0.86+、手書き/横向き等の難読票が
# ~0.60-0.78。0.85 は印刷票まで巻き込み過剰標黄(=狼少年化)するため、手書き級
# (<~0.65)のみ標黄する水準に下げた。手書き凑平幻覚は規則①②でも別途兜底。env 可調。
DOC_LOW_CONFIDENCE_THRESHOLD = float(
    os.getenv("DOC_LOW_CONFIDENCE_THRESHOLD", "0.65"))

# === 科目マッピング（AI 出力名 → MF 正確名） ===
# ※ 標準化勘定科目　法人.xlsx (2026-03-24 受領) に基づく
ACCOUNT_MAP = {
    # --- AI が出力しがちな別名 → MF 正式名 ---
    "消耗品費": "備品・消耗品費",
    "雑費": "備品・消耗品費",       # 雑費は使わない → 備品・消耗品費に統一
    "交際費": "接待交際費",         # MF 正式名は「接待交際費」
    "食費": "接待交際費",
    "飲食費": "接待交際費",
    "交通費": "旅費交通費",
    "タクシー代": "旅費交通費",
    "電車代": "旅費交通費",
    "駐車場代": "旅費交通費",
    "ガソリン代": "旅費交通費",
    "電話代": "通信費",
    "インターネット代": "通信費",
    "家賃": "地代家賃",
    "手数料": "支払手数料",
    "振込手数料": "支払手数料",
    "書籍代": "新聞図書費",
    "図書費": "新聞図書費",
    "外注加工費": "外注費",
    "委託費": "業務委託料",
    "会費": "諸会費",
    "年会費": "諸会費",
    # --- MF 正式名そのまま（AI が正しく出力した場合のパススルー） ---
    "接待交際費": "接待交際費",
    "旅費交通費": "旅費交通費",
    "車両費": "旅費交通費",
    "通信費": "通信費",
    "水道光熱費": "水道光熱費",
    "修繕費": "修繕費",
    "備品・消耗品費": "備品・消耗品費",
    "地代家賃": "地代家賃",
    "保険料": "保険料",
    "租税公課": "租税公課",
    "支払手数料": "支払手数料",
    "支払報酬": "支払報酬",
    "広告宣伝費": "広告宣伝費",
    "会議費": "会議費",
    "福利厚生費": "福利厚生費",
    "法定福利費": "法定福利費",
    "業務委託料": "業務委託料",
    "荷造運賃": "荷造運賃",
    "新聞図書費": "新聞図書費",
    "リース料": "リース料",
    "諸会費": "諸会費",
    "外注費": "外注費",
    "仮経費": "仮経費",
    "研修採用費": "研修採用費",
    "販売促進費": "販売促進費",
    "寄付金": "寄付金",
    "給料賃金": "給料賃金",
    "役員報酬": "役員報酬",
    "賞与": "賞与",
    "雑給": "雑給",
    "退職給与": "退職給与",
    "仕入高": "仕入高",
    "売上高": "売上高",
    "雑収入": "雑収入",
    "雑損失": "雑損失",
}

# AI が科目を判断できない場合のデフォルト
UNKNOWN_ACCOUNT = "未確定勘定"

# 貸方専用科目（借方に出現した場合は未確定勘定に置換）
# Gemini が訓練データから誤学習し、借方に貸方科目を入れるケースへの対策
CREDIT_ONLY_ACCOUNTS = {
    "未払金", "買掛金", "仮受金", "前受金", "預り金",
    "現金", "普通預金", "当座預金", "定期預金",
}

# === 貸方補助科目設定 ===
# 領収書: 社長名（立替払いのため）— 会社ごとに異なる
# ※ クライアントから正式名を受領後に更新
CREDIT_SUB_ACCOUNT_RECEIPT = "（社長名未設定）"
# 請求書: 貸方補助科目 = 取引先会社名（vendor_name から自動設定）

# === 員工名單映射表 ===
# Key: 員工的 Google 郵箱地址
# Value: 
#    - name: CSV 中顯示的真實姓名
#    - chat_id: 聊天軟體 (如 Slack/Teams) 的用戶 ID，用於 @提及
#               (如果不清楚 ID，可以先填 None)

EMPLOYEE_MAP = {
    # 測試帳號 (您的郵箱)
    "toadeater731@gmail.com": {
        "name": "Administrator",
        "chat_id": "U12345678" # 示例 ID
    },
    
    # 員工 A
    "staff_a@company.com": {
        "name": "田中 太郎",
        "chat_id": None
    },
    
    # 員工 B
    "staff_b@company.com": {
        "name": "佐藤 花子",
        "chat_id": None
    },
    
    # 可以在這裡繼續添加...
}

# === 支持的文件格式 ===
# 只有這些後綴的文件會被機器人處理
SUPPORTED_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.heic', '.pdf']

# === 系統設置 ===
# 掃描間隔 (秒)
SCAN_INTERVAL = 3


# === クレジットカード明細 / 交通系IC 設定 ===
#
# Plan §9.5（docs/plans/2026-08-12-credit-card-doctype.md）の 13 項目。
# 各モジュールは**自前の既定値を持ち**、ここは override として読まれる
# （`try: from config import X / except ImportError:` の形）。したがって
# ここの値は各モジュールの既定値と**一致していなければ挙動が変わる**。
#
# ⚠ 下の「読み取り点が未実装」と注記した 5 項目は、ここに書いても現時点では
#   **効かない**（T4 / T5 で実装する側がまだ無いため）。該当タスクの実装時に
#   `try/except ImportError` の読み取りも一緒に入れること。

# --- 記帳（仕訳の作り方）---
# ポイント充当行（F-5: ENEOSポイントキャッシュバック −3,000）の貸方科目。
# 借方 未払金 ／ 貸方 雑収入 で計上し、カード未払金を直接減らす（Plan AD-5）。
# 『クレカ相殺』は MF 全 112 社中 7 社にしか無く、しかも貸借対照表側の
# 一時科目だったため採らない（附録 D）。`雑収入` は抽査 14/14 社に存在。
# 読み取り点: card_entries._build（T4 で結線済み）
CREDIT_ADJUST_CREDIT_ACCOUNT = "雑収入"

# nimoca「入金（チャージ）」行を記帳対象に含めるか（Plan TBD-2）。
# 既定 False = 記帳しない。乗車を逐行記帳するので、チャージまで計上すると
# 二重計上になる。件数と金額は監査側に残る。
TRANSIT_IC_BOOK_CHARGE_ROWS = False

# 借方税区分の**出力層のみ**の省略名変換（Plan AD-11）。
# `receipt_aggregation.determine_tax_types()` が返す正式名はグローバルには
# 変えない（anomaly_detector が精確等値で判定しているため）。
# ⚠ 8% 側の省略名は MF 実帳で**未確認**。10% 側のみ実測済み（附録 C）。
#   8% 行を出す前に井戸会計事務所へ要確認。
# ※ 読み取り点が未実装（T4 の MF レンダリング層で使う）
CC_TAX_TYPE_RENDERING = {
    "課対仕入10%": "課仕 10%",
    "課対仕入8% (軽)": "課仕 8%(軽)",
}

# --- H列（借方インボイス）---
# F-12: MF「借方インボイス」列の正規値。**空欄を明示的に許可**する
# （事務所慣例が空欄であり、MF は空欄を「適格」と解釈する）。
INVOICE_COL_VALUES = ("", "適格", "80％控除", "70％控除",
                      "50％控除", "30％控除", "控除なし")

# 判定コード → H列の実値（Plan AD-8 / 趙裁定 9）。
# 現行は**全て空欄** —— 井戸会計事務所の記帳慣例をそのまま踏襲する。
# MF 実測では 貸方＝未払金 133 件中 111 件が「適格」相当かつ H列は空欄で、
# 1 万円超の行は適格請求書が別途保存されているとみられる（附録 C）。
# つまりこの運用では**カード明細は記帳のトリガであって控除の証憑ではない**。
# 空欄にするのは慣例の踏襲であって税法判断の自動化ではない。
# TBD が動いたら**ここだけ**を書き換える（判定木は残してある）。
# キーは invoice_classification.DeductionClass の値。到達しない
# transitional_80 / transitional_50 は意図的に載せない。
INVOICE_COL_RENDERING = {
    "qualified": "",
    "not_deductible": "",
    "out_of_scope": "",
}

# この額**以上**の行に U列タグを付けて人の確認を促す。
# 少額特例の要件が「税込 1 万円**未満**」なので、1 万円ちょうどは対象外＝要確認。
# 判定は `>=` で行う（`>` ではない）。
INVOICE_CONFIRM_THRESHOLD_YEN = 10000

# U列タグの文言。「少額特例により適格」とは**書かない** ——
# 少額特例には事業者要件（基準期間の課税売上高 1 億円以下等）があり、
# プログラムには判定できない。タグは注意喚起であって判定の保証ではない。
INVOICE_CONFIRM_TAG = "1万円以上: インボイス確認"

# 少額特例の**事業者要件**を事務所が確認済みか（Plan T2 DoD）。
# 既定 False = 未確認。未確認の行は控除なしへ倒す（過大控除の回避）。
# 事務所が確認したら True にするだけで判定が R08 → R07 へ移る。
SMALL_AMOUNT_TAXPAYER_CONFIRMED = False

# --- 検算・重複検出 ---
# 検算の許容差。領収書の 2 円許容（receipt_aggregation）は税抜/税込の丸め由来。
# カード明細は印字済み税込金額の純粋な加算で、F-4 の 3 例すべてが厳密一致
# しているので 0 円とする。E2E で実際の不一致率を測ってから再検討（Plan R9）。
RECON_TOLERANCE_YEN = 0

# 重複頁の扱い: "exclude"（記帳しない・監査タブへ）/ "mark"（記帳しつつ注記）/
# "off"（重複判定を効かせない）。
# 偽陽性は仕訳を消すので、実運用で誤検出が出たときにコード変更なしで
# 段階的に弱められる逃げ道として用意している。
CREDIT_CARD_DEDUP_MODE = "exclude"

# --- 出力切断への備え（Plan T5）---
# ETC は 1 頁 60〜100 行あり、逐行記帳では Gemini の出力が途中で切れうる。
#
# 逐行記帳 doc_type（クレカ・交通系IC）専用の出力トークン上限。
# **0 = 既存値（`ocr_engine.GEMINI_MAX_OUTPUT_TOKENS`）を流用**。
#
# 65536 は gemini-2.5-flash の出力硬上限（`genai.list_models()` で実測）。
# thinking の動的上限 24,576 を引いても本文に 40,960 残り、実測 134 tok/行
# （`model.count_tokens()`）から約 305 行/頁まで無分割で入る —— 実物の最悪頁
# 100 行に対して 3 倍の余裕がある。上限は天井であって課金は実生成量なので、
# 上げても費用は増えない（截断 → 再試行の空焼きが減るぶん、むしろ下がる）。
#
# ⚠ 0 に戻しても機能は退行しない: 32768 で切れた場合は `card_salvage` が
# 完結行を救い、足りなければ提示行と監査行が残る。
GEMINI_MAX_OUTPUT_TOKENS_BULK = 65536

# 窓分割リトライ（`CC_WINDOW_SIZE` / `CC_MAX_WINDOWS`）は**廃案**につき削除した。
# 「1 頁 300 行」を根拠に設計していたが、その 300 行はカード単位（4 頁の合計）の
# 読み違いで、頁単位の実測最悪は 100 行だった。上限を 65536 にすれば実物では
# 一度も発火せず、発火しないコードは検証不能な死蔵経路になる。
# 経緯は `docs/plans/2026-08-17-t5-window-retry.md` §9.1、
# 再追加を検知する番人は `test_credit_card_config.RetiredItemsTest`。


# === 出力プロファイル ===
# 「どの入力フォルダに入れた票据を、どのスプレッドシートへ書き、
#   どこへアーカイブするか」を束ねた単位。
#   default : 全社共有 (SuperScaner_Root 配下)
#   ido     : 社長専用 (SuperScaner_Internal/井戸会計事務所 配下)
#   rental  : 社長専用 (SuperScaner_Internal/レンタル経理部 配下)
# ido / rental は Google Drive の「アクセス制限」フォルダ内にあり、
# 社長・夫人・SA・共有ドライブ管理者のみが閲覧できる。
PROFILE_DEFAULT = "default"

# key → {prefix: env 変数名の接頭辞, label: 起動ログ用の表示名}
# 会社を1社増やす際に触るのはこの1箇所だけ（env 変数名は接頭辞から自動導出）。
PROFILES_META = {
    PROFILE_DEFAULT: {"prefix": "",        "label": "共通"},
    "ido":           {"prefix": "IDO_",    "label": "井戸会計事務所"},
    "rental":        {"prefix": "RENTAL_", "label": "株式会社レンタル経理部"},
}


def load_profiles() -> dict:
    """
    有効な出力プロファイルのみを {key: 設定dict} で返す。

    有効条件は OUTPUT_SPREADSHEET_ID と PROCESSED_FOLDER_ID が両方揃うこと。
    片方でも欠けたプロファイルは黙って除外する — 本番 Windows PC の .env
    更新漏れで git pull 後にボット全体が停止し、全社の記帳が止まるのを防ぐ。
    SPLIT_PDF_FOLDER_ID は任意 (未設定なら PageUrlResolver が
    base_url#page=N へ降格する)。
    """
    profiles = {}
    for key, meta in PROFILES_META.items():
        prefix = meta["prefix"]
        spreadsheet_id = os.getenv(f"{prefix}OUTPUT_SPREADSHEET_ID", "")
        processed_folder_id = os.getenv(f"{prefix}PROCESSED_FOLDER_ID", "")
        if not spreadsheet_id or not processed_folder_id:
            continue

        profiles[key] = {
            "label": meta["label"],
            "spreadsheet_id": spreadsheet_id,
            "processed_folder_id": processed_folder_id,
            "split_pdf_folder_id": os.getenv(f"{prefix}SPLIT_PDF_FOLDER_ID", ""),
        }
    return profiles


def load_folder_map(profiles: dict = None) -> dict:
    """
    從環境變量構建 {folder_id: (doc_type, profile_key)} 映射表。

    有効なプロファイルの入力フォルダのみを収集する。無効なプロファイルの
    フォルダを拾うと、書き込み先シートが存在せず処理が落ちる。

    支持新的多文件夾模式 (FOLDER_RECEIPT_ID / IDO_FOLDER_RECEIPT_ID 等) 和
    舊的單文件夾模式 (INPUT_FOLDER_ID → 默認 receipt, default プロファイル)。

    Raises:
        ValueError: 同一フォルダ ID が複数の (プロファイル, 文書タイプ) に
            割り当てられている場合。黙って後勝ちさせると、社長専用の票据が
            共通シートへ書き込まれる（＝アクセス制限の意味が消える）。
    """
    if profiles is None:
        profiles = load_profiles()

    folder_map = {}

    def claim(folder_id, doc_type, profile_key):
        """フォルダ ID を1つの (doc_type, profile) に排他的に割り当てる。"""
        if folder_id in folder_map:
            prev_doc, prev_profile = folder_map[folder_id]
            raise ValueError(
                f"入力フォルダ ID が重複しています: {folder_id}\n"
                f"  「{prev_profile} / {prev_doc}」と「{profile_key} / {doc_type}」が"
                f"同じフォルダを指しています。\n"
                f"  .env を確認してください。このまま起動すると、社長専用の票据が"
                f"共通シートへ書き込まれる恐れがあります。"
            )
        folder_map[folder_id] = (doc_type, profile_key)

    # 新模式: プロファイル × 文書タイプ ごとの専用フォルダ ID
    for profile_key in profiles:
        prefix = PROFILES_META[profile_key]["prefix"]
        for env_key, doc_type in ENV_FOLDER_MAP.items():
            folder_id = os.getenv(f"{prefix}{env_key}")
            if folder_id:
                claim(folder_id, doc_type, profile_key)

    # 向後兼容: default に新模式のフォルダが1つも無い場合のみ、
    # INPUT_FOLDER_ID を領収書フォルダとして扱う。
    # default 自体が無効なら legacy も拾わない (書き込み先が無いため)。
    default_has_folders = any(
        profile_key == PROFILE_DEFAULT for _, profile_key in folder_map.values()
    )
    if PROFILE_DEFAULT in profiles and not default_has_folders:
        legacy_id = os.getenv("INPUT_FOLDER_ID")
        if legacy_id:
            claim(legacy_id, DocType.RECEIPT, PROFILE_DEFAULT)

    return folder_map