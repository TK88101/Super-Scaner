"""page_progress.py — 頁級進捗フック（main/headless 両分支共用の底座）。

Plan: docs/plans/2026-07-31-b7-heartbeat-main.md
契約: contracts/job-state-machine.md v0.15 §5.6（outcome 値域の錨）

reporter は duck-typing（基底クラス強制なし、repo 風）。process_file(...,
progress=None) は `progress or NULL_REPORTER` で受けるため、reporter 未指定
時は既存挙動が完全に不変。job 上下文（headless の job_key 等）は事件載荷に
含めない——main 側は頁級情報のみを扱い、Firestore reporter は構造期に
job 上下文を注入する（高2 裁決、§3.1(c)）。

import 副作用ゼロ: 本モジュールはネットワーク I/O を一切行わない
（タブ名検査は純粋な文字列比較）。
"""
import re
import time
from datetime import datetime, timedelta, timezone

import gspread
from gspread.utils import rowcol_to_a1

JST = timezone(timedelta(hours=9))


# ── outcome 四値（契約 §5.6 と一字一句一致。headless 第3步でそのまま Firestore へ運ぶ）──
OUTCOME_POSTED = "POSTED"            # 仕訳を MF 区へ実際に記帳した頁
OUTCOME_EXCLUDED = "EXCLUDED"        # 除外頁（監査タブ/MF 提示行に留痕済）
OUTCOME_PLACEHOLDER = "PLACEHOLDER"  # 占位行のみの頁（_unrecognized／有効金額ゼロ）
OUTCOME_FAILED = "FAILED"            # 頁エラー（頁単位では未記帳）

# 優先序（低11 裁決）: _excluded_page の頁は落地形式が MF 占位行（社保通知書の
# 提示行）でも恒に EXCLUDED。PLACEHOLDER は非除外頁が占位行に終わった場合のみ。


# ── 檔終局 machine status（中9 裁決）。機械値は英字常数、表示は日本語映射のみ ──
STATUS_PROCESSING = "PROCESSING"
STATUS_COMPLETED = "COMPLETED"
STATUS_COMPLETED_COVERAGE_GAP = "COMPLETED_WITH_COVERAGE_GAP"
STATUS_PARTIAL_ERROR = "PARTIAL_ERROR"
STATUS_FAILED_RETAINED = "FAILED_RETAINED"
STATUS_PARSE_FAILED = "PARSE_FAILED"
STATUS_ABORTED = "ABORTED"

STATUS_LABELS_JA = {
    STATUS_PROCESSING: "処理中",
    STATUS_COMPLETED: "完了",
    STATUS_COMPLETED_COVERAGE_GAP: "完了（ページ欠落あり）",
    STATUS_PARTIAL_ERROR: "部分エラー",
    STATUS_FAILED_RETAINED: "失敗（ファイル保持）",
    STATUS_PARSE_FAILED: "解析失敗",
    STATUS_ABORTED: "異常終了",  # ABORTED は呼出側が error_class を付記して表示
}


def utc_now() -> datetime:
    """発射時に固定する UTC 時刻。

    節流で Sheets への実書込が遅延しても、事件そのものの発生時刻は
    page_done 呼出の瞬間に固定される（高2/中7 裁決）。
    """
    return datetime.now(timezone.utc)


# ── タブ名 '_' 開頭検査（sheets_output.AUDIT_TAB_NAME と同方針・同根拠）──
# GAS の backupAllTabs_ は SKIP_TABS と「先頭が '_'」以外の全タブを
# MF_Backup へ退避したうえで元タブを削除する。通常名で作ると進捗タブが
# 毎晩 22:00 に消える。
PROGRESS_TAB_NAME = "_処理進捗"

# F〜I 列は**ページ数の内訳**（E 列「ページ進捗」の分解）。表を読むのは
# 会計事務所の担当者なので、OUTCOME_* の定数名（英語）をそのまま見出しに出さない。
#
# 単位は「頁」ではなく**「ページ」と書く**（趙指示 2026-08-19）。「頁」は
# 日本語として古風で読み手に負担がかかるうえ、顧客が既に他タブで見ている
# 語とも食い違う。**顧客可視の文字列は全て「ページ」で統一されている**:
#   `AUDIT_HEADERS` の "ページ" 列 / タブ名 `_除外ページ監査` /
#   MF タブの赤い占位行 `⚠ 認識不能ページ` / A1-A4 の凡例
# （コード内のコメントや console print は日中混在の「頁」のままでよい。
#   あれは顧客の目に触れない）
#
# **顧客可視の文字列はこの定数だけではない。** 同じタブの J 列には
# `STATUS_LABELS_JA` の値がそのまま出るので、片方だけ直すと同じ行に
# 「ページ」と「頁」が並ぶ（実際に一度やった）。用語を変えるときは
# 両方を一緒に見ること。
# K 列の「最終更新」は元「最終心跳」だった —— 心跳は中国語であり、
# 日本語では通じない（心拍／ハートビート）。
#
# 語彙も同じ理由で顧客可視の語に揃える（新語を作らない）:
#   除外ページ      = `AUDIT_VERDICT_EXCLUDED` / `_除外ページ監査`
#   認識不能ページ  = MF タブの `⚠ 認識不能ページ`
# 「〜ページ」を付けるのは、1 行 ＝ 1 ファイルなので単位を書かないと
# 「除外 2」が 2 ページなのか 2 ファイルなのか読めなくなるため。
#
# **この定数を変えたら本番タブの 1 行目も同時に直すこと。**
# `_ensure_worksheet` は既存 header との完全一致を要求し、不一致なら
# 上書きせず自己無効化する。`_disabled` はプロセス生存中ずっと立ったままなので、
# 直さずに配備すると次の再起動まで進捗タブが更新されない。
PROGRESS_HEADERS = [
    "開始時刻", "ファイル名", "担当", "文書タイプ", "ページ進捗",
    "記帳済ページ", "除外ページ", "認識不能ページ", "エラーページ",
    "状態", "最終更新(JST)",
]


def _check_tab_name(name: str) -> None:
    """タブ名が '_' 始まりでなければ RuntimeError で大声に落とす。

    ocr_engine._validate_doc_type_registries / sheets_output.AUDIT_TAB_NAME の
    「起動時に大声で落ちる」方針と同じ（規約とテスト1本に頼ると、
    リネーム PR がレビューを素通りして本番で静かに壊れうるため）。
    """
    if not name.startswith("_"):
        raise RuntimeError(
            f"進捗タブ名は '_' 始まり必須です（GAS backupAllTabs_ が退避後に"
            f"元タブを削除するため）: {name!r}"
        )


_check_tab_name(PROGRESS_TAB_NAME)


_UPDATED_RANGE_RE = re.compile(r"![A-Z]+(\d+)")


def _parse_row_number(updated_range: str) -> int:
    """gspread append_rows 応答の updatedRange から権威的に行番号を得る（高3 裁決）。

    自前の行数カウントは人手挿入等で競態になり得るため、API 応答を
    唯一の真実源とする。
    """
    m = _UPDATED_RANGE_RE.search(updated_range)
    if not m:
        raise ValueError(f"想定外の updatedRange 形式です: {updated_range!r}")
    return int(m.group(1))


class _NullReporter:
    """全メソッド no-op。progress 未指定時の既定実装（既存挙動を完全維持）。"""

    def file_started(self, filename, uploader_name, doc_type):
        pass

    def page_done(self, page_num, total_pages, outcome, reason, occurred_at):
        pass

    def file_finished(self, status, error_class=None):
        pass


NULL_REPORTER = _NullReporter()


# ── 節流・degrade・リトライの定数 ──
PROGRESS_FLUSH_INTERVAL = 20  # 秒。定常負荷 ≦3 writes/min に収める（高4 裁決）
_MAX_CONSECUTIVE_FAILURES = 3  # これに達すると当該檔の途中更新を停止（中5 裁決）
_MAX_RETRY_429 = 2  # reporter 独自の 429 リトライ予算（記帳側の退避予算を食わない）


class SheetsProgressReporter:
    """Sheets 進捗タブ `_処理進捗` へ 1 檔 1 行・節流付きで書き込む reporter。

    duck-typing で main.process_file の reporter 協議を実装する
    （file_started / page_done / file_finished）。全公開メソッドは内部で
    例外を自吞し console 警告するのみ——進捗記録の失敗が記帳を止めることは
    絶対にない（best-effort 鉄則）。

    clock は節流判定用の注入可能な時計（既定 time.time）。occurred_at の
    ような事件時刻とは独立——節流のタイマーはプロセス内時間経過だけを見る。
    """

    def __init__(self, spreadsheet, clock=time.time):
        self._spreadsheet = spreadsheet
        self._clock = clock
        self._ws = None
        self._disabled = False
        self._reset_file_state()

    def _reset_file_state(self, filename="", uploader_name="", doc_type=""):
        """ファイル単位の状態を初期化する（__init__ と file_started の共通部）。

        _seen_pages は呼び出し側（main.process_file の seen_page_nums）にも
        同等の集合があるが、reporter は消費者非依存の共用底座（headless の
        第2消費者が第3步で控えている）なので自前で持つ——呼び出し側の
        簿記への復用は結合を生む（simcodex R2 裁決）。
        """
        self._filename = filename
        self._uploader_name = uploader_name
        self._doc_type = doc_type
        self._row = None
        self._started_jst = ""
        self._counts = {
            OUTCOME_POSTED: 0, OUTCOME_EXCLUDED: 0,
            OUTCOME_PLACEHOLDER: 0, OUTCOME_FAILED: 0,
        }
        self._total_pages = None
        self._seen_pages = set()
        self._last_status = STATUS_PROCESSING
        self._last_error_class = None
        self._consecutive_failures = 0
        self._degraded = False
        self._last_event_at = None
        self._last_flush_ts = None

    # ──────────────────────── 公開 reporter 協議 ────────────────────────

    def file_started(self, filename, uploader_name, doc_type):
        try:
            self._file_started_impl(filename, uploader_name, doc_type)
        except Exception as e:
            print(f"⚠️ 進捗タブ初期化に失敗（進捗記録なしで続行）: {e}")

    def page_done(self, page_num, total_pages, outcome, reason, occurred_at):
        try:
            self._page_done_impl(page_num, total_pages, outcome, occurred_at)
        except Exception as e:
            print(f"⚠️ 進捗記録に失敗（処理は継続）: {e}")

    def file_finished(self, status, error_class=None):
        try:
            self._file_finished_impl(status, error_class)
        except Exception as e:
            print(f"⚠️ 進捗終局記録に失敗: {e}")

    # ──────────────────────── 内部実装 ────────────────────────

    def _file_started_impl(self, filename, uploader_name, doc_type):
        self._reset_file_state(filename, uploader_name, doc_type)
        self._started_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")

        self._ensure_worksheet()
        if self._disabled:
            return

        row = self._build_row()
        response = self._append_row_with_retry(row)
        self._row = _parse_row_number(response["updates"]["updatedRange"])
        self._last_flush_ts = self._clock()

    def _page_done_impl(self, page_num, total_pages, outcome, occurred_at):
        if self._disabled or self._row is None:
            return

        is_first_page = not self._seen_pages
        # 頁進捗の分子＝distinct 頁数（codex R1 裁決）。最大頁番号だと中間頁の
        # 欠落時に「3/3＋頁欠落あり」の自己矛盾表示になり、yield 事件数だと
        # 一頁多票（同一 page_num の複数 yield）で逆に水増しになる。
        self._seen_pages.add(page_num)
        self._total_pages = total_pages
        # 心跳＝最後の事件時刻（発射時固定、高2/中7 裁決）。節流で実書込が
        # 遅延しても、表示される心跳は flush 時刻でなく事件時刻を指す。
        self._last_event_at = occurred_at or utc_now()
        if outcome in self._counts:
            self._counts[outcome] += 1

        if self._degraded:
            # 途中更新は停止済み。file_finished の最終書込だけが残る（中5 裁決）。
            return

        now = self._clock()
        elapsed = (None if self._last_flush_ts is None
                  else now - self._last_flush_ts)
        # FAILED の即時 flush は「その檔の最初の失敗」だけ。全頁エラーの
        # 故障批次で毎頁同期書込になると、節流が守るはずの配額・壁鐘を
        # O(頁数) で撃ち抜くため（操作者が最初の失敗を素早く見られれば
        # 「即時可見」の目的は足りる。以後は通常節流に落ちる）。
        is_first_failure = (outcome == OUTCOME_FAILED
                            and self._counts[OUTCOME_FAILED] == 1)
        should_flush = (
            is_first_page
            or is_first_failure
            or elapsed is None
            or elapsed >= PROGRESS_FLUSH_INTERVAL
        )
        if should_flush:
            self._flush(now)

    def _file_finished_impl(self, status, error_class):
        self._last_status = status
        self._last_error_class = error_class
        self._last_event_at = utc_now()  # 終局自体も心跳事件
        if self._disabled or self._row is None:
            return
        # file_finished は degrade 状態に関わらず恒に独立の最終書込を1回試みる
        self._flush(self._clock(), force=True)

    # ──────────────────────── worksheet セットアップ ────────────────────────

    def _ensure_worksheet(self):
        """監査タブ先例踏襲: 既存タブ header 不一致→上書きせず自己無効化。

        監査タブ(sheets_output._get_or_create_audit_tab)は不一致で例外を
        投げて記帳を止めるが、進捗タブは可観測性の付帯機能に過ぎないため
        自己無効化して管線を続行させる（中5 裁決と同じ「壊れても記帳は守る」
        思想）。
        """
        if self._ws is not None or self._disabled:
            return
        try:
            ws = self._spreadsheet.worksheet(PROGRESS_TAB_NAME)
        except gspread.exceptions.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(
                title=PROGRESS_TAB_NAME, rows=1000, cols=len(PROGRESS_HEADERS))
            self._write_header(ws)
            self._ws = ws
            return

        header = ws.row_values(1)
        if header and header != PROGRESS_HEADERS:
            self._disabled = True
            print(f"⚠️ 進捗タブ '{PROGRESS_TAB_NAME}' のヘッダが想定と異なるため"
                  f"自己無効化します（上書きしません）: {header}")
            return
        if not header:
            # ヘッダ無しの空タブ = 前回初期化の中断残骸。誰のデータも壊さない
            # ので書き直して自己修復する（監査タブと同じ判断）。
            self._write_header(ws)
        self._ws = ws

    def _write_header(self, ws):
        self._retry_write(lambda: ws.append_rows(
            [PROGRESS_HEADERS], value_input_option="USER_ENTERED"))

    # ──────────────────────── 行の組み立て・書込 ────────────────────────

    def _build_row(self):
        total_label = (self._total_pages if self._total_pages is not None
                       else "?")
        # 心跳表示＝最後の事件時刻（JST 換算）。事件前（file_started 直後）は
        # 開始時刻に等しい現在時刻で埋める。
        heartbeat_src = self._last_event_at or utc_now()
        heartbeat = heartbeat_src.astimezone(JST).strftime("%Y/%m/%d %H:%M:%S")
        label = STATUS_LABELS_JA.get(self._last_status, self._last_status)
        if self._last_status == STATUS_ABORTED and self._last_error_class:
            label = f"{label}（{self._last_error_class}）"
        return [
            self._started_jst,
            self._filename,
            self._uploader_name,
            self._doc_type,
            f"{len(self._seen_pages)}/{total_label}",
            self._counts[OUTCOME_POSTED],
            self._counts[OUTCOME_EXCLUDED],
            self._counts[OUTCOME_PLACEHOLDER],
            self._counts[OUTCOME_FAILED],
            label,
            heartbeat,
        ]

    def _flush(self, now, force=False):
        if self._degraded and not force:
            return
        row = self._build_row()
        try:
            self._update_row(self._row, row)
        except Exception as e:
            if force:
                print(f"⚠️ 進捗終局の最終書込に失敗: {e}")
                return
            self._consecutive_failures += 1
            print(f"⚠️ 進捗行の更新に失敗（{self._consecutive_failures}回目）: {e}")
            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                self._degraded = True
                print(f"⚠️ 連続{_MAX_CONSECUTIVE_FAILURES}回の書込失敗のため、"
                      f"このファイルの途中進捗更新を停止します: {self._filename}")
            return
        self._consecutive_failures = 0
        self._last_flush_ts = now

    def _update_row(self, row_number, row_values):
        # 列文字変換は gspread 同梱の rowcol_to_a1 を使う（自前実装は
        # sheets_output.py の chr(ord('A')+col) 26列バグの轍を踏むだけ）。
        range_name = (f"A{row_number}:"
                      f"{rowcol_to_a1(row_number, len(PROGRESS_HEADERS))}")
        self._retry_write(lambda: self._ws.update(
            range_name, [row_values], value_input_option="USER_ENTERED"))

    def _append_row_with_retry(self, row):
        return self._retry_write(lambda: self._ws.append_rows(
            [row], value_input_option="USER_ENTERED"))

    def _retry_write(self, func):
        """429 一時エラーに対して reporter 独自予算（max 2）で指数バックオフ再試行。

        sheets_output._write_with_retry と同型だが、記帳側の退避予算
        （max 5）を消費しないよう独立の小予算に絞る（高4 裁決）。
        """
        for attempt in range(_MAX_RETRY_429):
            try:
                return func()
            except gspread.exceptions.APIError as e:
                if "429" in str(e) and attempt < _MAX_RETRY_429 - 1:
                    wait = (2 ** attempt) + 1
                    print(f"⏳ 進捗タブ API レート制限、{wait}秒待機中...")
                    time.sleep(wait)
                else:
                    raise
