"""T11 の受入判定器。`dump_e2e_rows` の events.jsonl を読み、A1〜A9 を判定する。

母 Plan `docs/plans/2026-08-12-credit-card-doctype.md` §6 の受入基準を、
`docs/plans/2026-08-21-t11-e2e-machine-verdict.md` §3.3 の判定式へ落としたもの。
**目視合格を作らない**のが本モジュールの存在理由なので、次の 3 つを避ける。

- **vacuous PASS**: 対象が 0 件だから緑、という緑を作らない（A9 は高額行の
  存在を要求する）
- **件数一致で済ませない**: 「高額行の数 == タグ付き行の数」は、高額行 1 件
  漏れ＋低額行 1 件誤付与で成立してしまう。逐行の同値で見る
- **訳文に依存しない**: 監査タブの理由列は `audit_reason_ja` で日本語化
  されてから書かれる。判定は必ず機械可読キー（`reason_key`）で行う

gspread に依存しない（列位置は下の定数で持ち、`sheets_output.MF_HEADERS`
との一致は `test_verify_e2e_acceptance` が縛る）。
"""

import json
import sys
from collections import defaultdict
from typing import NamedTuple

PASS = "PASS"
FAIL = "FAIL"

EVENT_VERSION = 1


class SchemaError(Exception):
    """dump の形が想定と違う。**判定を続けない** —— 欄が無いことを
    「条件を満たさない」と誤読して緑を返すのが一番危ない。"""


class Check(NamedTuple):
    id: str
    status: str
    detail: str


# ── MF 28 列の位置（`sheets_output.MF_HEADERS` と同順）──
COL_DEBIT_ACCOUNT = 2
COL_DEBIT_INVOICE = 7      # H列
COL_DEBIT_AMOUNT = 8
COL_CREDIT_INVOICE = 15    # P列
COL_CREDIT_AMOUNT = 16
COL_MEMO = 18
COL_TAG = 20               # U列

INVOICE_CONFIRM_TAG = "1万円以上: インボイス確認"
INVOICE_THRESHOLD = 10000

# 製品に存在しない標識（F4）。A8 は「実装されていれば緑になる」判定式を
# 書いておき、現状 FAIL であることを台帳に残す（趙 2026-08-21 裁定）。
YEAR_ESTIMATED_TAG = "年推定"

# 真票 8 本。実ファイル名は長いのでラベルの部分一致で解決する。
LABELS = ("ENEOS", "TS CUBIC", "アメックス", "アレコレ",
          "JCB", "UC", "楽天", "ニモカ")
# 期待値の根拠は**券面の事実**であって前回の実測ではない。
#
# アレコレ: 録音 3 頁の `statement_page` は "2/2" / "1枚目" / （無し）。
#   `card_file_recon._segments` は「どれか 1 頁でも n が読めなければ段を
#   確定しない」設計（疑わしきは判定しない）なので**検算不能が正しい挙動**。
#   2026-08-20 の memory は「アレコレ＝一致」と記録しているが、これは
#   Gemini の揺れた別の回の観測とみられる（記録再生を入れた理由そのもの）。
# ニモカ: 券面に合計が構造的に存在しない（`RECON_POLICY` の count_only）。
#   よって「検算不能」ではなく「検算不可」。両者は別の語であり、
#   取り違えると「取れる筈が取れなかった」異常を見逃す。
EXPECT_MATCH = ("ENEOS", "TS CUBIC", "アメックス")
EXPECT_UNVERIFIABLE = ("JCB", "UC", "楽天", "アレコレ")
EXPECT_NOT_APPLICABLE = ("ニモカ",)

# **3 分割が `LABELS` を過不足なく覆うことを起動時に固定する。**
# これが無いと `EXPECT_UNVERIFIABLE` は「allowlist に見えて何も検査しない
# 飾り」になる —— 9 本目のラベルを `LABELS` に足して 3 分割を直し忘れると、
# 暗黙の else で「検算不能のはず」に落ち、誰も気付かない。
_partition = set(EXPECT_MATCH) | set(EXPECT_UNVERIFIABLE) | set(EXPECT_NOT_APPLICABLE)
if _partition != set(LABELS):
    raise RuntimeError(
        "期待値の 3 分割が LABELS を覆っていません: 過剰 %s / 欠落 %s"
        % (sorted(_partition - set(LABELS)), sorted(set(LABELS) - _partition)))
_overlap = ((set(EXPECT_MATCH) & set(EXPECT_UNVERIFIABLE))
            | (set(EXPECT_MATCH) & set(EXPECT_NOT_APPLICABLE))
            | (set(EXPECT_UNVERIFIABLE) & set(EXPECT_NOT_APPLICABLE)))
if _overlap:
    raise RuntimeError("期待値が重複しています: %s" % sorted(_overlap))
VERDICT_MATCH = "一致"
VERDICT_UNVERIFIABLE = "検算不能"
VERDICT_NOT_APPLICABLE = "検算不可"
VERDICT_MISMATCH = "不一致"
KNOWN_VERDICTS = (VERDICT_MATCH, VERDICT_UNVERIFIABLE,
                  VERDICT_NOT_APPLICABLE, VERDICT_MISMATCH)

# 借方が未払金 = カード未払金を直接減らす調整行（キャッシュバック等）。
# 「記帳対象行」ではないので A3 の母集団から外す。実測: ENEOS p2 に
# 「ENEOS店頭キャッシュバック 3,000 円（借方 未払金／税区分 対象外）」が在り、
# これを足すと 21,503 になる。券面の利用額 18,503 は燃料 2 行だけの和。
ADJUSTMENT_DEBIT_ACCOUNT = "未払金"

ENEOS_PRINTED_TOTAL = 15503   # A2: 当期調整込みの券面合計
ENEOS_BOOKED_TOTAL = 18503    # A3: 記帳対象行のみの和（A2 と違って正常）

DUPLICATE_PAGES = (("アメックス", 3), ("アメックス", 4))
POINT_ONLY_PAGES = (("アメックス", 6), ("アレコレ", 3))
NEGATIVE_MEMO_MARKERS = ("ENEOSポイントキャッシュバック", "前回分口座振替金額")
VERDICT_EXCLUDED = "除外"
VERDICT_MISSING = "欠落"

REQUIRED_FIELDS = {
    "file_start": ("file", "doc_type", "expected_pages"),
    "page_yield": ("file", "page", "total_pages", "entries_len",
                   "year_estimated_count"),
    "mf_row": ("file", "page", "scope", "is_data_row", "row", "batch"),
    "audit_row": ("file", "page", "scope", "verdict", "reason_key",
                  "reason_ja"),
    "recon": ("file", "verdict", "detail_sum", "statement_total"),
    "file_end": ("file", "success"),
    "file_error": ("file", "error"),
    "token_usage": ("slot",),
}


def label_of(filename):
    """ファイル名をラベルへ解決する。**曖昧なら None**。

    2 つのラベルに当たる名前を黙って片方へ寄せると、期待値が別のファイルに
    適用されて判定が静かにずれる。
    """
    hits = [label for label in LABELS if label in (filename or "")]
    return hits[0] if len(hits) == 1 else None


def validate(events):
    """版・kind・必須欄を検証する。壊れていたら例外（緑を返さない）。"""
    for i, e in enumerate(events):
        if e.get("v") != EVENT_VERSION:
            raise SchemaError("event %d: 版が違う: %r" % (i, e.get("v")))
        kind = e.get("kind")
        if kind not in REQUIRED_FIELDS:
            raise SchemaError("event %d: 未知の kind: %r" % (i, kind))
        for field in REQUIRED_FIELDS[kind]:
            if field not in e:
                raise SchemaError("event %d (%s): 必須欄が無い: %s"
                                  % (i, kind, field))
    return events


def load_events(path):
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return validate(events)


def _num(value):
    """MF の金額欄を int にする。読めなければ None（0 にしない —— 0 は
    「金額が 0 円」であって「読めなかった」ではない）。"""
    text = str(value or "").replace(",", "").replace("¥", "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (ValueError, TypeError):
        return None


class _View:
    """events を判定しやすい形に畳んだもの。"""

    def __init__(self, events):
        self.events = events
        self.expected = {}          # label -> expected_pages
        self.unknown_files = []
        self.label_collisions = []  # 2 つのファイルが同じラベルへ落ちた
        self._label_source = {}     # label -> 最初にそのラベルを取ったファイル名
        self.pages = defaultdict(set)          # label -> {page}
        self.year_est = {}                     # (label, page) -> count
        self.mf = defaultdict(list)            # (label, page) -> [event]
        self.audit = defaultdict(list)         # (label, page) -> [event]
        self.recon = {}                        # label -> event
        self.missing_audits = []
        for e in events:
            kind = e["kind"]
            if kind == "token_usage":
                continue
            label = label_of(e.get("file"))
            if label is None:
                if kind == "file_start":
                    self.unknown_files.append(e.get("file"))
                continue
            if kind == "file_start":
                # **同じラベルに 2 つのファイルが落ちると、以降の頁・行・
                # recon が 1 つのキーへ混ざる。** 上書きは静かに起きるので、
                # A1 が「想定外の頁」を報告するだけになり原因が見えない。
                # 最悪、混ざった結果が偶然つじつまを合わせて PASS しうる。
                prev = self._label_source.get(label)
                if prev is not None and prev != e["file"]:
                    self.label_collisions.append((label, prev, e["file"]))
                self._label_source[label] = e["file"]
                self.expected[label] = e["expected_pages"]
            elif kind == "page_yield":
                self.pages[label].add(e["page"])
                self.year_est[(label, e["page"])] = e["year_estimated_count"]
            elif kind == "mf_row":
                self.mf[(label, e["page"])].append(e)
            elif kind == "audit_row":
                self.audit[(label, e["page"])].append(e)
                if e["verdict"] == VERDICT_MISSING:
                    self.missing_audits.append(e)
            elif kind == "recon":
                self.recon[label] = e

    def data_rows(self, label=None):
        for (lab, page), rows in self.mf.items():
            if label is not None and lab != label:
                continue
            for e in rows:
                if e["is_data_row"]:
                    yield lab, page, e["row"]


def _check(cid, bad, ok_detail):
    if bad:
        return Check(cid, FAIL, "; ".join(str(b) for b in bad[:6]))
    return Check(cid, PASS, ok_detail)


def _a1(v):
    bad = []
    for label, expected in v.expected.items():
        if expected <= 0:
            bad.append("%s: 期待頁数が %d（入力が読めていない）"
                       % (label, expected))
            continue
        want = set(range(1, expected + 1))
        got = v.pages[label]
        if want != got:
            bad.append("%s: 未観測頁 %s / 想定外頁 %s"
                       % (label, sorted(want - got), sorted(got - want)))
        for page in sorted(want):
            n = len(v.mf[(label, page)]) + len(v.audit[(label, page)])
            if n == 0:
                bad.append("%s p%d: 出力が 1 件も無い" % (label, page))
    for e in v.missing_audits:
        bad.append("欠落監査行: %s" % e.get("reason_key"))
    return _check("A1", bad, "全 %d ファイルの全頁が 1 件以上出力"
                  % len(v.expected))


def _a1b(v):
    """同じ頁が**二度書かれて**いないこと。

    判定の単位は行ではなく **batch**（`append_entries` 1 回分）。頁内に
    同額・同名の行が並ぶこと自体は正当で、実測でも TS CUBIC p6 に
    「ETC通行料金/N西日本 760 円」が 5 行ある（同じ料金所を何度も通れば
    そうなる）。行の内容で重複を判定すると、この正当な券面を毎回赤にする
    —— 偽警報は「無視される警報」になり、本物の二重記帳を隠す。
    """
    bad = []
    for (label, page), rows in v.mf.items():
        batches = defaultdict(list)
        for e in rows:
            if e["is_data_row"]:
                batches[e.get("batch")].append(tuple(e["row"]))
        seen = {}
        for batch, sig in sorted(batches.items(), key=lambda kv: str(kv[0])):
            key = tuple(sig)
            if key in seen and sig:
                bad.append("%s p%d: batch %s と %s が同一内容（%d 行）"
                           % (label, page, seen[key], batch, len(sig)))
            seen[key] = batch
    return _check("A1b", bad, "同一頁の二重書き込みなし")


def _a2(v):
    bad = ["未知のファイルが dump に居る: %s" % f for f in v.unknown_files]
    bad += ["ラベル衝突: %s に %s と %s の 2 ファイルが落ちている" % c
            for c in v.label_collisions]
    for label in LABELS:
        e = v.recon.get(label)
        if e is None:
            bad.append("%s: recon が無い" % label)
            continue
        if e["verdict"] not in KNOWN_VERDICTS:
            bad.append("%s: 未知の verdict %r" % (label, e["verdict"]))
            continue
        if label in EXPECT_MATCH:
            if e["verdict"] != VERDICT_MATCH:
                bad.append("%s: 一致のはずが %s" % (label, e["verdict"]))
            elif e["detail_sum"] != e["statement_total"]:
                bad.append("%s: detail %s != printed %s"
                           % (label, e["detail_sum"], e["statement_total"]))
        elif label in EXPECT_NOT_APPLICABLE:
            if e["verdict"] != VERDICT_NOT_APPLICABLE:
                bad.append("%s: 検算不可のはずが %s" % (label, e["verdict"]))
        elif label in EXPECT_UNVERIFIABLE:
            if e["verdict"] != VERDICT_UNVERIFIABLE:
                bad.append("%s: 検算不能のはずが %s" % (label, e["verdict"]))
        else:
            # 起動時の網羅性検査があるのでここへは来ない。来たら分割の穴。
            bad.append("%s: 期待値が定義されていない" % label)
    eneos = v.recon.get("ENEOS")
    if eneos is not None and eneos.get("statement_total") != ENEOS_PRINTED_TOTAL:
        bad.append("ENEOS: 券面合計が %s（期待 %d）"
                   % (eneos.get("statement_total"), ENEOS_PRINTED_TOTAL))
    counts = {}
    for label in LABELS:
        e = v.recon.get(label)
        if e:
            counts[e["verdict"]] = counts.get(e["verdict"], 0) + 1
    return _check("A2", bad, "verdict 内訳 %s" % counts)


def _a3(v):
    """記帳対象行の和。調整行（借方＝未払金）は母集団に入れない。

    母 Plan の A3 は `Σ(kind ∈ {expense, fee, unknown})` と書かれているが、
    その `kind` を持つ `card_reconciliation` は生産経路へ接線しない裁定
    （趙 2026-08-21）なので、MF 行から同じ母集団を作り直す。借方が
    未払金の行は費用ではなく負債の減少なので除く。
    """
    total = 0
    for _label, _page, row in v.data_rows("ENEOS"):
        if str(row[COL_DEBIT_ACCOUNT] or "").strip() == ADJUSTMENT_DEBIT_ACCOUNT:
            continue
        total += _num(row[COL_DEBIT_AMOUNT]) or 0
    bad = ([] if total == ENEOS_BOOKED_TOTAL
           else ["ENEOS 記帳合計 %d（期待 %d）" % (total, ENEOS_BOOKED_TOTAL)])
    return _check("A3", bad, "ENEOS 記帳合計 %d" % total)


def _a4(v):
    bad = ["%s p%d: 除外頁が MF に %d 行" % (label, page, len(v.mf[(label, page)]))
           for label, page in DUPLICATE_PAGES if v.mf[(label, page)]]
    return _check("A4", bad, "重複頁は MF に 0 行")


def _a5(v):
    bad = []
    for label, page in DUPLICATE_PAGES:
        rows = v.audit[(label, page)]
        hit = [e for e in rows
               if e["verdict"] == VERDICT_EXCLUDED
               and str(e["reason_key"]).startswith("duplicate_page")]
        if not hit:
            bad.append("%s p%d: duplicate_page の除外監査行が無い（実際: %s）"
                       % (label, page, [e["reason_key"] for e in rows]))
    return _check("A5", bad, "重複頁 2 件とも監査タブに留痕")


def _a6(v):
    bad = []
    for label, page, row in v.data_rows():
        for col in (COL_DEBIT_AMOUNT, COL_CREDIT_AMOUNT):
            n = _num(row[col])
            if n is not None and n < 0:
                bad.append("%s p%d: 負数 %d" % (label, page, n))
        memo = str(row[COL_MEMO] or "")
        for marker in NEGATIVE_MEMO_MARKERS:
            if marker in memo:
                bad.append("%s p%d: abs() 転正の疑い %r" % (label, page, marker))
    return _check("A6", bad, "負数 0 件・転正の痕跡なし")


def _a7(v):
    bad = []
    for label, page in POINT_ONLY_PAGES:
        if v.mf[(label, page)]:
            bad.append("%s p%d: MF に %d 行（0 行のはず）"
                       % (label, page, len(v.mf[(label, page)])))
        if not [e for e in v.audit[(label, page)]
                if e["verdict"] == VERDICT_EXCLUDED]:
            bad.append("%s p%d: 除外の監査行が無い" % (label, page))
    return _check("A7", bad, "ポイント専用頁 2 件とも監査タブのみ")


def _a8(v):
    """年推定に標識が付いているか。**現状の製品には標識が無い**（F4）。"""
    estimated = [(label, page) for (label, page), n in v.year_est.items()
                 if n and n > 0]
    if not estimated:
        return Check("A8", FAIL,
                     "年推定の行が 1 件も観測されていない。"
                     "`_year_estimated` は算出されるが誰も消費しないため"
                     "（製品側に標識の実装が無い）判定材料が出てこない")
    bad = []
    for label, page in sorted(estimated):
        rows = [r for _l, p, r in v.data_rows(label) if p == page]
        if not rows:
            bad.append("%s p%d: 推定 %d 件だが MF 行が無い"
                       % (label, page, v.year_est[(label, page)]))
        for row in rows:
            if YEAR_ESTIMATED_TAG not in str(row[COL_TAG] or ""):
                bad.append("%s p%d: 推定年の行に標識が無い" % (label, page))
    return _check("A8", bad, "推定年の行に全て標識あり")


def _a9(v):
    bad = []
    high = 0
    for label, page, row in v.data_rows():
        if str(row[COL_DEBIT_INVOICE] or "").strip():
            bad.append("%s p%d: H列が空でない %r"
                       % (label, page, row[COL_DEBIT_INVOICE]))
        if str(row[COL_CREDIT_INVOICE] or "").strip():
            bad.append("%s p%d: P列が空でない %r"
                       % (label, page, row[COL_CREDIT_INVOICE]))
        amount = _num(row[COL_DEBIT_AMOUNT]) or 0
        tagged = INVOICE_CONFIRM_TAG in str(row[COL_TAG] or "")
        if amount >= INVOICE_THRESHOLD:
            high += 1
            if not tagged:
                bad.append("%s p%d: %d 円にタグが無い" % (label, page, amount))
        elif tagged:
            bad.append("%s p%d: %d 円なのにタグが在る" % (label, page, amount))
    if high == 0:
        bad.append("1 万円以上の行が 1 件も無い（判定が空振りしている）")
    return _check("A9", bad, "H/P列 空・高額 %d 行に全てタグ" % high)


CHECKS = (_a1, _a1b, _a2, _a3, _a4, _a5, _a6, _a7, _a8, _a9)


def evaluate(events):
    v = _View(events)
    return [fn(v) for fn in CHECKS]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    path = argv[0] if argv else "dump/events.jsonl"
    checks = evaluate(load_events(path))
    width = max(len(c.id) for c in checks)
    for c in checks:
        print("%-*s  %s  %s" % (width, c.id, c.status, c.detail))
    failed = [c for c in checks if c.status != PASS]
    print("\n%d/%d PASS" % (len(checks) - len(failed), len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
