"""ファイル単位の突合。**行や頁が無音で消えるのを検出する**（B 案）。

Plan: docs/plans/2026-08-21-recon-b-missing-line-alert.md

## この検算器がやらないこと（趙裁定 2026-08-21）

記帳の正しさは**判定しない**。税区分も勘定科目も見ない。
顧客が SS に求めているのは「券面に載っている名目を如実に Sheet へ反映する」
ことであり、正しいか否かは後続で人が査験する。

したがって本モジュールの存在理由は 1 つだけ ——
**人が目視では気づけない「本来あるはずの行が無い」を鳴らすこと**。
金額の誤りは人が見れば判るが、無いものは見えない
（CLAUDE.md の IP-401: 54 枚上げて仕訳 53 件、枚数を数えて初めて判明した）。

## `card_reconciliation` との棲み分け

あちらは**カード単位**の元帳で、17 フィールドの verdict を出す。
実測ではカード番号で束ねると真票 8 ファイルで偽の不一致が 7 件出た
（金額の誤りは 1 件も無く、全部が分組起因）。

こちらは**ファイル単位**で、判定は 1 つだけ。分組を一切しないので
その故障様式が構造的に存在しない。あちらには 1 行も触れない（Plan AD-1）。

## 突合の式（Plan AD-2 / AD-2b）

    左辺 = Σ 明細行の金額（重複頁を除く／carry_over を除く）
    右辺 = Σ 券面の請求合計（同額は**段の内側だけ**で 1 回に潰す）
    一致 ⇔ 左辺 == 右辺

「段」＝券面が自ら名乗る頁付 `n/N` の連続する塊。1 通の明細書に相当する。
**段をまたぐ同額を潰してはいけない** —— 2 通が偶然同額のとき、片方が
丸ごと落ちても「一致」に化ける（Codex 評審 高-1 の反例）。それは本検算器が
防ごうとしている当のものである。

段は**同額を潰してよいかの判定にだけ**使う。明細行の帰属には使わない
（使った瞬間に分組になり、`card_reconciliation` の故障様式を引き継ぐ）。

外部依存なし（gspread / paddleocr / google api 非依存）。
"""
from typing import NamedTuple, Optional

import card_entries
import card_reconciliation as cr

# ── 語彙は既存から借りる。新造しない（Plan §3 AD-8）─────────────────
# 監査タブの判定列と `tag_rules` の severity 語彙が静かに食い違うと、
# 赤系マークが付かないまま不一致が見逃される。
VERDICT_MATCH = cr.VERDICT_MATCH
VERDICT_MISMATCH = cr.VERDICT_MISMATCH
VERDICT_UNVERIFIABLE = cr.VERDICT_UNVERIFIABLE
VERDICT_NOT_APPLICABLE = cr.VERDICT_NOT_APPLICABLE

# ── 理由キー（監査タブの理由列。`sheets_output.audit_reason_ja` が訳す）──
# **訳語の登録漏れは理由列が英語のまま顧客の目に触れる**ので番人で塞ぐ。
REASON_MISMATCH = "file_total_mismatch"
REASON_MATCH = "file_recon_match"
REASON_NO_TOTAL = "file_recon_no_total"          # ラベル未登録・合計が無い
REASON_PAGE_NUMBER = "file_recon_page_number"    # 頁付が読めない / 矛盾
REASON_PAGE_ERROR = "file_recon_page_error"      # 頁エラーで左辺が不足
REASON_POLICY = "file_recon_not_applicable"      # doc_type が検算対象外
REASON_BROKEN = "file_recon_broken"              # 検算器自身が落ちた

try:
    from config import TRANSIT_IC_BOOK_CHARGE_ROWS as _CFG_BOOK_CHARGE
except ImportError:
    # nimoca の入金（チャージ）を記帳するか。既定は False（乗車を記帳するため）。
    _CFG_BOOK_CHARGE = False


class PageObservation(NamedTuple):
    """1 頁分の観測。**畳んだ数値だけ**を持ち、raw_data は持たない。

    producer（`ocr_engine`）が作り、消費側（`main` / `local_test`）が畳む。
    raw_data を運ばないのは、消費側が勝手に再解釈する余地を消すため
    （解釈は `card_entries` の 3 関数に一本化されている）。
    """
    page_num: int
    detail_sum: int
    totals: tuple                    # ((ラベル, 金額), ...) 濾過済み
    statement_n: Optional[int]       # 券面が名乗る「n/N」の n
    statement_total: Optional[int]   # 同じく N
    is_duplicate: bool


class FileVerdict(NamedTuple):
    """1 ファイル分の判定。監査タブへ書くのは verdict が不一致のときだけ。"""
    verdict: str
    printed_total: Optional[int]
    detail_sum: int
    diff: Optional[int]
    reason_key: str


def is_bookable(kind, book_charge_rows=None):
    """検算の左辺に足してよい行か。

    **`FileReconLedger._is_bookable` の重複実装である**（Plan AD-10）。
    公開純関数へ抽出しないのは `card_reconciliation.py` の diff を 0 に
    保つため。代償として `test_card_file_recon.BookableDriftTest` が
    `KIND_ALL` の全値・`book_charge_rows` の両値で一致を見張る。
    """
    if book_charge_rows is None:
        book_charge_rows = _CFG_BOOK_CHARGE
    if kind == cr.KIND_CHARGE:
        return bool(book_charge_rows)
    return kind != cr.KIND_CARRY_OVER


def observe_page(raw_data, doc_type, page_num, is_duplicate=False):
    """1 頁を観測する。**例外を絶対に外へ出さない**（Plan AD-7）。

    検算は検査器であって記帳経路ではない。ここで落ちて頁の記帳が止まるのは
    本末転倒なので、壊れた入力は「何も観測できなかった頁」に落とす。
    頁付が None になるので、結果としてファイル全体が検算不能へ倒れる。
    """
    try:
        return _observe_page(raw_data, doc_type, page_num, is_duplicate)
    except Exception as e:  # noqa: BLE001 - 検算ごときで記帳を止めない
        print("⚠️ ファイル検算の観測に失敗（記帳は継続）p%s: %s: %s"
              % (page_num, type(e).__name__, e))
        return PageObservation(page_num=page_num, detail_sum=0, totals=(),
                               statement_n=None, statement_total=None,
                               is_duplicate=is_duplicate)


def _observe_page(raw_data, doc_type, page_num, is_duplicate):
    ident = card_entries.card_ident_from_raw(raw_data)
    totals = tuple(
        (pt.label, pt.amount)
        for pt in card_entries.printed_totals_from_raw(raw_data)
        if pt.amount and not pt.is_handwritten
        and cr.section_for_label(pt.label) == cr.SECTION_CURRENT_USAGE)
    detail_sum = sum(
        line.amount
        for line in card_entries.detail_lines_from_raw(raw_data, doc_type)
        if is_bookable(line.kind))
    return PageObservation(page_num=page_num, detail_sum=detail_sum,
                           totals=totals,
                           statement_n=ident.statement_page_n,
                           statement_total=ident.statement_page_total,
                           is_duplicate=is_duplicate)


def finalize(observations, doc_type, had_page_errors=False):
    """EOF で 1 ファイル分を結算する。**例外を絶対に外へ出さない**。

    呼ぶのは**ファイルが歸檔される経路だけ**（Plan AD-6）。保持されるファイルは
    3 秒後に再スキャンされるので、毎回呼ぶと監査タブに同じ行が無限に増える。
    """
    try:
        return _finalize(observations, doc_type, had_page_errors)
    except Exception as e:  # noqa: BLE001
        print("⚠️ ファイル検算の結算に失敗（記帳は継続）: %s: %s"
              % (type(e).__name__, e))
        return FileVerdict(VERDICT_UNVERIFIABLE, None, 0, None, REASON_BROKEN)


def _unverifiable(detail_sum, reason):
    """「判定しない」を 1 箇所で組む。

    分岐が増えたとき `printed_total=None` / `diff=None` の書き忘れで
    欄がズレるのを防ぐ（simplify 評審 2026-08-21）。
    """
    return FileVerdict(VERDICT_UNVERIFIABLE, None, detail_sum, None, reason)


def _finalize(observations, doc_type, had_page_errors):
    policy = cr.RECON_POLICY.get(doc_type, cr.POLICY_NONE)
    if policy != cr.POLICY_AMOUNT_REQUIRED:
        # 既存 4 型は検算対象外。nimoca は券面に金額合計が構造的に無い。
        return FileVerdict(VERDICT_NOT_APPLICABLE, None, 0, None, REASON_POLICY)

    live = [o for o in observations if not o.is_duplicate]
    if not live:
        return _unverifiable(0, REASON_NO_TOTAL)

    detail_sum = sum(o.detail_sum for o in live)

    if had_page_errors:
        # 失敗頁の明細は Sheets に書かれていないので左辺が必ず不足する。
        # 「不一致」と診断すると人が記帳内容を疑って時間を溶かす。
        return _unverifiable(detail_sum, REASON_PAGE_ERROR)

    segments = _segments(live)
    if segments is None:
        return _unverifiable(detail_sum, REASON_PAGE_NUMBER)

    printed_total = _printed_sum(live, segments)
    if printed_total is None:
        return _unverifiable(detail_sum, REASON_NO_TOTAL)

    diff = detail_sum - printed_total
    if abs(diff) <= cr.RECON_TOLERANCE_YEN:
        return FileVerdict(VERDICT_MATCH, printed_total, detail_sum, diff,
                           REASON_MATCH)
    return FileVerdict(VERDICT_MISMATCH, printed_total, detail_sum, diff,
                       REASON_MISMATCH)


class ReconReport(NamedTuple):
    """判定を「何を出すか」へ翻訳したもの。"""
    log_line: str
    audit_reason: Optional[str]   # None なら監査タブに書かない


def report(file_verdict, filename=""):
    """運用者向けログ 1 行と、監査タブ行の要否を決める。

    **ログは常に出す**（Plan AD-5 / Codex 複審の争点 3）。
    監査タブに書くのは不一致のときだけなので、機構が壊れて全件
    「検算不能」になっても顧客側には何も出ない —— それでは検算器自身が
    無音で死んだことに誰も気づけない。検算器が防ごうとしている事故と
    同じ形なので、コンソールには必ず痕跡を残す。
    """
    v = file_verdict
    if v.verdict == VERDICT_NOT_APPLICABLE:
        # 領収書など検算対象外の doc_type。毎ファイル 1 行出すと、
        # **唯一の人手監視経路であるコンソール**が零情報で埋まり、
        # IP-401 型の事故信号が流れて消える（simplify 評審 2026-08-21）。
        return ReconReport("", None)
    parts = ["file_recon: verdict=%s" % v.verdict]
    if v.printed_total is not None:
        parts.append("printed=%d" % v.printed_total)
    parts.append("detail=%d" % v.detail_sum)
    if v.diff is not None:
        parts.append("diff=%d" % v.diff)
    if v.verdict != VERDICT_MATCH:
        parts.append("reason=%s" % v.reason_key)
    if filename:
        parts.append("file=%s" % filename)
    log_line = " ".join(parts)

    if v.verdict != VERDICT_MISMATCH:
        return ReconReport(log_line, None)
    return ReconReport(log_line, "%s:%s/%s/%s" % (
        REASON_MISMATCH, v.printed_total, v.detail_sum, v.diff))


def report_and_record(observations, doc_type, filename, source_url,
                      writer, audit_verdict, had_page_errors=False):
    """結算 → ログ → 監査タブ。**`main` と `local_test` の両方がこれを呼ぶ。**

    片方だけ直すと必ず漂移する（CLAUDE.md が名指しする ENTRY_BUILDERS
    未登録事故と同族）。`test_card_file_recon_wiring` が両経路とも
    この関数を呼ぶことを番人として縛る。

    `audit_verdict` を引数で受けるのは `sheets_output` を import しないため
    （本モジュールは gspread 非依存を保つ）。
    """
    verdict = finalize(observations, doc_type, had_page_errors)
    rep = report(verdict, filename)
    if rep.log_line:
        print(rep.log_line)
    if rep.audit_reason is None:
        return verdict
    try:
        writer.append_audit_row(filename=filename, page_num=0,
                                verdict=audit_verdict,
                                reason=rep.audit_reason,
                                ocr_text_len=0, source_url=source_url or "")
    except Exception as e:  # noqa: BLE001 - 検算ごときで記帳を止めない
        print("❌ 検算行の書き込みに失敗（記帳は継続）: %s: %s"
              % (type(e).__name__, e))
    return verdict


def _segments(observations):
    """頁付 `n/N` で段に切り、各頁の段番号を返す。切れなければ `None`。

    段の境界は「n が 1 に戻る／後退する」ところ。券面 1 通が `1/N .. N/N` を
    名乗る単位に対応する。

    `None` を返す（＝検算不能へ倒す）条件は 2 つ:
      - どれか 1 頁でも n が読めない（段が確定しない）
      - 同一段の中で N が読めていて途中で変わる（券面の読み違い）

    どちらも「疑わしきは判定しない」側へ倒す。誤って潰す／誤って分けるより、
    黙る方が害が小さい（監査タブが偽の赤で埋まると狼少年になる）。

    **同種の分段器がもう 1 本ある**: `card_file_state._AnchorRun`
    （`card_file_state.py:246-260`）も「1 通の明細書の連続頁」を n/N で
    追うが、目的が違う（あちらは日付の年を継承するため、こちらは同額の
    合計を潰してよいかの判定のため）。規則も違う ——
    あちらは `k == expected_next` の厳密連号で fail-closed、こちらは
    `n <= prev_n` で新段。**統合しない**のは目的が違うからだが、
    片方だけ直して片方を忘れる形の事故は在りうる。
    あちらが持つ「OCR による頁付の拒否権」（`_ocr_vetoes_page_label`）を
    こちらは持っていない —— Plan の TBD-5 に記録した既知の欠口。
    """
    result = []
    seg = 0
    prev_n = None
    seg_total = None
    for o in observations:
        n = o.statement_n
        if n is None:
            return None
        if prev_n is None or n <= prev_n:
            seg += 1
            seg_total = o.statement_total
        else:
            if (o.statement_total is not None and seg_total is not None
                    and o.statement_total != seg_total):
                return None
            if seg_total is None:
                seg_total = o.statement_total
        result.append(seg)
        prev_n = n
    return result


def _printed_sum(observations, segments):
    """券面の請求合計を足す。**同額は段の内側だけで 1 回に潰す**。

    合計は表紙と末頁の両方に印字されることが多い（実測: ENEOS は p1/p2、
    TS CUBIC の 1 通は p5/p9）。潰さないと必ず 2 倍になる。

    一方、**段が違えば同額でも潰さない**。2 通が偶然同額のとき片方が丸ごと
    落ちると、潰した場合に「一致」へ化けて無音欠落を見逃す。

    1 つも見つからなければ `None`（＝検算不能。ラベル未登録の発行体）。
    """
    seen = set()
    total = 0
    found = False
    for o, seg in zip(observations, segments):
        for _label, amount in o.totals:
            key = (seg, amount)
            if key in seen:
                continue
            seen.add(key)
            total += amount
            found = True
    return total if found else None
