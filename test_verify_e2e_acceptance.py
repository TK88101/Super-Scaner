"""`verify_e2e_acceptance`（T11 の受入判定器）の単体テスト。

**判定器のテストは 双方向でなければ意味が無い。** 壊れたデータを FAIL に
することだけを見ると「常に赤」の判定器が通り、正常データを PASS にする
ことだけを見ると「常に緑」の判定器が通る。よって各判定式に

- false-negative 反例: 性質が壊れているのに PASS しないこと
- false-positive 反例: 正常なのに FAIL しないこと

を 1 つずつ置く。

**「当該項だけ FAIL」は要求しない。** A1/A5/A7 は同じ監査データを共有するので、
1 箇所壊すと複数が赤くなるのが自然。人工的な分離を強いると、テスト用データの
都合が実データの意味を歪める（Codex 第 1R #12 の裁決）。
"""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify_e2e_acceptance as V
from dump_e2e_rows import EVENT_VERSION

MF_WIDTH = 28
AUDIT_WIDTH = 7


def _mf_row(amount=1000, memo="商品", tag="", invoice="", credit=0):
    """MF 28 列の最小行。列位置は `sheets_output.MF_HEADERS` に合わせる。"""
    row = [""] * MF_WIDTH
    row[0] = "1"                 # 取引No
    row[1] = "2026/04/15"        # 取引日
    row[2] = "備品・消耗品費"     # 借方勘定科目
    row[7] = invoice             # 借方インボイス（H列）
    row[8] = str(amount)         # 借方金額
    row[10] = "未払金"            # 貸方勘定科目
    row[15] = ""                 # 貸方インボイス（P列）
    row[16] = str(credit)        # 貸方金額
    row[18] = memo               # 摘要
    row[20] = tag                # タグ（U列）
    return row


def _ev(kind, **f):
    e = {"v": EVENT_VERSION, "kind": kind}
    e.update(f)
    return e


def _mf(file, page, **kw):
    return _ev("mf_row", file=file, page=page, scope="page", tab="t",
               is_data_row=kw.pop("is_data_row", True),
               batch=kw.pop("batch", page),
               row=_mf_row(**kw))


def _audit(file, page, verdict="除外", reason_key="duplicate_page:1"):
    row = [""] * AUDIT_WIDTH
    row[3] = verdict
    row[4] = "重複ページ"
    return _ev("audit_row", file=file, page=page, scope="page",
               verdict=verdict, reason_key=reason_key,
               reason_ja="重複ページ", audit_page_num=page, row=row)


# 真票 8 本のラベル。実ファイル名は長いので部分一致で解決する。
FILES = {
    "ENEOS": "ENEOSカード_2枚_20260723-150955.pdf のコピー.pdf",
    "TS CUBIC": "TS CUBIC CARD（トヨタファイナンス）_9枚_20260810-113112.pdf",
    "アメックス": "アメックスカード_6枚_20260723-150955.pdf のコピー.pdf",
    "アレコレ": "アレコレカード明細.pdf のコピー.pdf",
    "JCB": "JCB_2枚_20260810-113443.pdf",
    "UC": "UCカード明細_手入力_2枚_20260525-183358.pdf のコピー.pdf",
    "楽天": "楽天カード_2枚_20260723-150955.pdf のコピー.pdf",
    "ニモカ": "ニモカ_6枚_20260723-150955.pdf のコピー.pdf",
}
PAGES = {"ENEOS": 2, "TS CUBIC": 9, "アメックス": 6, "アレコレ": 3,
         "JCB": 2, "UC": 2, "楽天": 2, "ニモカ": 6}
# 期待値の根拠は券面の事実（`verify_e2e_acceptance` の EXPECT_* と同じ）。
# アレコレは録音の statement_page が "2/2"/"1枚目"/（無し）で段が確定せず
# 検算不能が正しい。ニモカは券面に合計が構造的に無いので「検算不可」。
MATCH = ("ENEOS", "TS CUBIC", "アメックス")
UNVERIFIABLE = ("JCB", "UC", "楽天", "アレコレ")
NOT_APPLICABLE = ("ニモカ",)
EXCLUDED = {"アメックス": (3, 4, 6), "アレコレ": (3,)}


def _healthy():
    """A8 以外は全て PASS になる合成 dump。

    A8 は製品に実装が無いので構造的に FAIL する（趙 2026-08-21 裁定）。
    """
    ev = []
    for label, name in FILES.items():
        n = PAGES[label]
        ev.append(_ev("file_start", file=name, doc_type="credit_card",
                      expected_pages=n))
        excluded = EXCLUDED.get(label, ())
        for p in range(1, n + 1):
            ev.append(_ev("page_yield", file=name, page=p, total_pages=n,
                          excluded=p in excluded, exclude_reason=None,
                          exclude_destination=None, audit_signal=None,
                          ocr_text_len=100, entries_len=0 if p in excluded else 1,
                          year_estimated_count=0))
            if p in excluded:
                ev.append(_audit(name, p))
            elif label == "ENEOS":
                # A3: 借方金額の総和が 18,503 になるよう 2 頁で割る
                ev.append(_mf(name, p, amount=7380 if p == 1 else 11123,
                              memo="給油 p%d" % p,
                              tag="1万円以上: インボイス確認" if p == 2 else ""))
            else:
                ev.append(_mf(name, p, amount=1000, memo="通常 p%d" % p))
        if label in MATCH:
            total = 15503 if label == "ENEOS" else 5000
            ev.append(_ev("recon", file=name, verdict="一致",
                          detail_sum=total, statement_total=total,
                          diff=0, reason_key="file_recon_match"))
        elif label in NOT_APPLICABLE:
            ev.append(_ev("recon", file=name, verdict="検算不可",
                          detail_sum=0, statement_total=None, diff=None,
                          reason_key="file_recon_not_applicable"))
        else:
            ev.append(_ev("recon", file=name, verdict="検算不能",
                          detail_sum=0, statement_total=None, diff=None,
                          reason_key="file_recon_no_total"))
        ev.append(_ev("file_end", file=name, success=True,
                      seen_pages=list(range(1, n + 1))))
    return ev


def _status(events, check_id):
    for c in V.evaluate(events):
        if c.id == check_id:
            return c.status
    raise AssertionError("判定 %s が存在しない" % check_id)


class HealthyBaselineTest(unittest.TestCase):
    """false-positive 側の総括: 正常な dump は A8 以外 PASS する。"""

    def test_everything_but_a8_passes(self):
        checks = V.evaluate(_healthy())
        failed = [c.id for c in checks if c.status != V.PASS]
        self.assertEqual(failed, ["A8"],
                         "A8 以外が赤い（判定器が過検出している）: %s"
                         % [(c.id, c.detail) for c in checks
                            if c.status != V.PASS])

    def test_a8_fails_because_the_product_has_no_marking(self):
        self.assertEqual(_status(_healthy(), "A8"), V.FAIL)

    def test_every_criterion_is_evaluated(self):
        ids = [c.id for c in V.evaluate(_healthy())]
        self.assertEqual(sorted(ids), sorted(
            ["A1", "A1b", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9"]))


class A1PageCoverageTest(unittest.TestCase):
    def test_fails_when_a_page_was_never_yielded(self):
        ev = [e for e in _healthy()
              if not (e["kind"] == "page_yield"
                      and e["file"] == FILES["ENEOS"] and e["page"] == 2)]
        self.assertEqual(_status(ev, "A1"), V.FAIL)

    def test_fails_when_the_whole_file_yielded_nothing(self):
        """F15: 観測が空でも期待頁数から欠落と分かること。"""
        ev = [e for e in _healthy()
              if not (e.get("file") == FILES["JCB"]
                      and e["kind"] in ("page_yield", "mf_row", "audit_row"))]
        self.assertEqual(_status(ev, "A1"), V.FAIL)

    def test_fails_when_a_page_produced_no_output_at_all(self):
        ev = [e for e in _healthy()
              if not (e["kind"] == "mf_row"
                      and e["file"] == FILES["楽天"] and e["page"] == 1)]
        self.assertEqual(_status(ev, "A1"), V.FAIL)

    def test_fails_on_a_missing_page_audit_row(self):
        ev = _healthy() + [_audit(FILES["UC"], 1, verdict="欠落",
                                  reason_key="page_coverage_gap:[2]")]
        self.assertEqual(_status(ev, "A1"), V.FAIL)


class A1bDuplicateBatchTest(unittest.TestCase):
    """判定の単位は行ではなく batch（`append_entries` 1 回分）。"""

    def test_fails_when_the_same_page_was_written_twice(self):
        ev = _healthy()
        src = [e for e in ev if e["kind"] == "mf_row"
               and e["file"] == FILES["楽天"] and e["page"] == 1][0]
        ev.append(dict(src, batch=999))
        self.assertEqual(_status(ev, "A1b"), V.FAIL)

    def test_repeated_identical_rows_within_one_batch_are_legitimate(self):
        """実測: TS CUBIC p6 に「ETC通行料金/N西日本 760 円」が 5 行ある。

        同じ料金所を何度も通ればこうなる。ここを赤にする判定器は、
        正常な券面のたびに鳴る偽警報になり、本物の二重記帳を隠す。
        """
        ev = _healthy()
        src = [e for e in ev if e["kind"] == "mf_row"
               and e["file"] == FILES["楽天"] and e["page"] == 1][0]
        for _ in range(4):
            ev.append(dict(src))          # 同じ batch のまま増やす
        self.assertEqual(_status(ev, "A1b"), V.PASS)

    def test_identical_rows_on_different_pages_are_fine(self):
        ev = _healthy()
        src = [e for e in ev if e["kind"] == "mf_row"
               and e["file"] == FILES["楽天"] and e["page"] == 1][0]
        ev.append(dict(src, page=2, batch=2))
        self.assertEqual(_status(ev, "A1b"), V.PASS)


class A2ReconTest(unittest.TestCase):
    def test_fails_when_an_expected_match_is_unverifiable(self):
        ev = [dict(e, verdict="検算不能", statement_total=None)
              if e["kind"] == "recon" and e["file"] == FILES["アメックス"] else e
              for e in _healthy()]
        self.assertEqual(_status(ev, "A2"), V.FAIL)

    def test_fails_when_nimoca_reports_unverifiable_instead_of_na(self):
        """「取れる筈が取れなかった」と「構造的に無い」は別の異常。

        取り違えると、ニモカで本当に合計が読めなくなった日に気付けない。
        """
        ev = [dict(e, verdict="検算不能")
              if e["kind"] == "recon" and e["file"] == FILES["ニモカ"] else e
              for e in _healthy()]
        self.assertEqual(_status(ev, "A2"), V.FAIL)

    def test_fails_when_detail_sum_disagrees_with_the_printed_total(self):
        ev = [dict(e, detail_sum=999) if e["kind"] == "recon"
              and e["file"] == FILES["TS CUBIC"] else e for e in _healthy()]
        self.assertEqual(_status(ev, "A2"), V.FAIL)

    def test_fails_when_eneos_printed_total_is_not_15503(self):
        ev = [dict(e, detail_sum=15000, statement_total=15000)
              if e["kind"] == "recon" and e["file"] == FILES["ENEOS"] else e
              for e in _healthy()]
        self.assertEqual(_status(ev, "A2"), V.FAIL)

    def test_fails_when_a_file_has_no_recon_at_all(self):
        ev = [e for e in _healthy()
              if not (e["kind"] == "recon" and e["file"] == FILES["UC"])]
        self.assertEqual(_status(ev, "A2"), V.FAIL)

    def test_fails_when_an_unverifiable_file_silently_reports_match(self):
        """偽の「一致」は偽の警告より悪い。誰も異常に気づけない。"""
        ev = [dict(e, verdict="一致", statement_total=0, detail_sum=0)
              if e["kind"] == "recon" and e["file"] == FILES["JCB"] else e
              for e in _healthy()]
        self.assertEqual(_status(ev, "A2"), V.FAIL)


class A3BookedTotalTest(unittest.TestCase):
    def test_fails_when_eneos_booked_total_is_not_18503(self):
        ev = [dict(e, row=_mf_row(amount=1)) if e["kind"] == "mf_row"
              and e["file"] == FILES["ENEOS"] and e["page"] == 1 else e
              for e in _healthy()]
        self.assertEqual(_status(ev, "A3"), V.FAIL)

    def test_passes_on_the_expected_18503(self):
        self.assertEqual(_status(_healthy(), "A3"), V.PASS)

    def test_cashback_adjustment_rows_are_excluded_from_the_total(self):
        """実測: ENEOS p2 に「店頭キャッシュバック 3,000 円（借方 未払金）」。

        これを足すと 21,503 になり券面の利用額 18,503 と合わない。負債の
        減少であって費用ではないので、記帳対象行の和には入れない。
        """
        row = _mf_row(amount=3000, memo="ENEOS店頭キャッシュバック")
        row[2] = "未払金"
        ev = _healthy() + [_ev("mf_row", file=FILES["ENEOS"], page=2,
                               scope="page", tab="t", batch=2,
                               is_data_row=True, row=row)]
        self.assertEqual(_status(ev, "A3"), V.PASS)

    def test_an_expense_row_is_still_counted(self):
        """調整行の除外が広すぎないこと（借方が費用科目なら数える）。"""
        ev = _healthy() + [_mf(FILES["ENEOS"], 2, amount=1, batch=2)]
        self.assertEqual(_status(ev, "A3"), V.FAIL)


class A4DuplicatePageTest(unittest.TestCase):
    def test_fails_when_an_excluded_amex_page_reached_the_mf_tab(self):
        ev = _healthy() + [_mf(FILES["アメックス"], 3, amount=500)]
        self.assertEqual(_status(ev, "A4"), V.FAIL)

    def test_passes_when_p3_and_p4_have_no_mf_rows(self):
        self.assertEqual(_status(_healthy(), "A4"), V.PASS)


class A5ExclusionAuditTest(unittest.TestCase):
    def test_fails_when_one_of_the_two_pages_lost_its_audit_row(self):
        ev = [e for e in _healthy()
              if not (e["kind"] == "audit_row"
                      and e["file"] == FILES["アメックス"] and e["page"] == 4)]
        self.assertEqual(_status(ev, "A5"), V.FAIL)

    def test_fails_when_the_reason_key_is_not_a_duplicate(self):
        ev = [_audit(FILES["アメックス"], e["page"], reason_key="envelope_page")
              if e["kind"] == "audit_row"
              and e["file"] == FILES["アメックス"] and e["page"] in (3, 4) else e
              for e in _healthy()]
        self.assertEqual(_status(ev, "A5"), V.FAIL)

    def test_judges_on_the_key_not_the_translation(self):
        """訳文を変えても判定は動かないこと（訳は人が読む欄）。"""
        ev = [dict(e, reason_ja="なにか別の日本語")
              if e["kind"] == "audit_row" else e for e in _healthy()]
        self.assertEqual(_status(ev, "A5"), V.PASS)


class A6NegativeAmountTest(unittest.TestCase):
    def test_fails_on_a_negative_amount(self):
        ev = _healthy() + [_mf(FILES["楽天"], 2, amount=-500)]
        self.assertEqual(_status(ev, "A6"), V.FAIL)

    def test_fails_on_a_point_cashback_memo_even_when_positive(self):
        """abs() で正に転んだ行は金額だけ見ても捕まらない。"""
        ev = _healthy() + [_mf(FILES["ENEOS"], 2, amount=500,
                               memo="ENEOSポイントキャッシュバック")]
        self.assertEqual(_status(ev, "A6"), V.FAIL)

    def test_fails_on_a_previous_direct_debit_memo(self):
        ev = _healthy() + [_mf(FILES["TS CUBIC"], 2, amount=500,
                               memo="前回分口座振替金額")]
        self.assertEqual(_status(ev, "A6"), V.FAIL)

    def test_passes_when_all_amounts_are_positive(self):
        self.assertEqual(_status(_healthy(), "A6"), V.PASS)


class A7PointOnlyPageTest(unittest.TestCase):
    def test_fails_when_only_one_of_the_two_point_pages_is_audited(self):
        ev = [e for e in _healthy()
              if not (e["kind"] == "audit_row"
                      and e["file"] == FILES["アレコレ"] and e["page"] == 3)]
        self.assertEqual(_status(ev, "A7"), V.FAIL)

    def test_fails_when_a_point_page_produced_mf_rows(self):
        ev = _healthy() + [_mf(FILES["アメックス"], 6, amount=100)]
        self.assertEqual(_status(ev, "A7"), V.FAIL)


class A8YearEstimateTest(unittest.TestCase):
    def test_fails_while_the_product_has_no_marking(self):
        ev = [dict(e, year_estimated_count=3)
              if e["kind"] == "page_yield" and e["file"] == FILES["ニモカ"] else e
              for e in _healthy()]
        self.assertEqual(_status(ev, "A8"), V.FAIL)

    def test_would_pass_if_every_estimated_row_carried_a_marker(self):
        """実装されたときに緑へ転じることを示す（判定式が本物である証拠）。

        これが無いと A8 は「常に赤を返すだけの飾り」と区別が付かない。
        """
        ev = []
        for e in _healthy():
            if e["kind"] == "page_yield" and e["file"] == FILES["ニモカ"] \
                    and e["page"] == 1:
                e = dict(e, year_estimated_count=1)
            if e["kind"] == "mf_row" and e["file"] == FILES["ニモカ"] \
                    and e["page"] == 1:
                e = dict(e, row=_mf_row(tag=V.YEAR_ESTIMATED_TAG))
            ev.append(e)
        self.assertEqual(_status(ev, "A8"), V.PASS)


class A9InvoiceColumnTest(unittest.TestCase):
    def test_fails_when_the_h_column_is_not_empty(self):
        ev = _healthy() + [_mf(FILES["楽天"], 2, invoice="適格")]
        self.assertEqual(_status(ev, "A9"), V.FAIL)

    def test_fails_when_a_high_amount_row_lacks_the_tag(self):
        ev = _healthy() + [_mf(FILES["楽天"], 2, amount=20000, tag="")]
        self.assertEqual(_status(ev, "A9"), V.FAIL)

    def test_fails_when_a_low_amount_row_carries_the_tag(self):
        """件数一致では捕まらない側（高額 1 件漏れ＋低額 1 件誤付与）。"""
        ev = _healthy() + [_mf(FILES["楽天"], 2, amount=100,
                               tag="1万円以上: インボイス確認")]
        self.assertEqual(_status(ev, "A9"), V.FAIL)

    def test_fails_when_no_high_amount_row_exists_at_all(self):
        """対象 0 件の vacuous PASS を潰す。"""
        ev = [dict(e, row=_mf_row(amount=100)) if e["kind"] == "mf_row" else e
              for e in _healthy()]
        self.assertEqual(_status(ev, "A9"), V.FAIL)

    def test_placeholder_rows_do_not_count_as_data(self):
        """占位行は明細ではない。母集団に混ぜると判定が汚れる。"""
        ev = _healthy() + [_mf(FILES["楽天"], 2, amount=0, invoice="",
                               is_data_row=False)]
        self.assertEqual(_status(ev, "A9"), V.PASS)


class SchemaTest(unittest.TestCase):
    """欄名のずれを「条件を満たさない」と誤読して緑にしないため。"""

    def test_rejects_an_unknown_kind(self):
        with self.assertRaises(V.SchemaError):
            V.validate(_healthy() + [_ev("mystery", file="x")])

    def test_rejects_a_version_mismatch(self):
        bad = _healthy()
        bad[0] = dict(bad[0], v=EVENT_VERSION + 1)
        with self.assertRaises(V.SchemaError):
            V.validate(bad)

    def test_rejects_a_missing_required_field(self):
        bad = _healthy()
        bad[0] = {k: v for k, v in bad[0].items() if k != "expected_pages"}
        with self.assertRaises(V.SchemaError):
            V.validate(bad)

    def test_accepts_the_healthy_dump(self):
        V.validate(_healthy())

    def test_load_reads_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "e.jsonl")
            with io.open(path, "w", encoding="utf-8") as f:
                for e in _healthy():
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            self.assertEqual(len(V.load_events(path)), len(_healthy()))


class FileLabelTest(unittest.TestCase):
    """期待値をどのファイルに当てるかの解決が曖昧だと、判定は静かにずれる。"""

    def test_every_sample_resolves_to_exactly_one_label(self):
        for label, name in FILES.items():
            self.assertEqual(V.label_of(name), label)

    def test_labels_are_injective_over_the_sample_set(self):
        got = [V.label_of(n) for n in FILES.values()]
        self.assertEqual(len(set(got)), len(FILES),
                         "2 つのファイルが同じラベルへ落ちている")

    def test_unknown_file_is_reported_not_silently_skipped(self):
        self.assertIsNone(V.label_of("まったく無関係.pdf"))

    def test_an_unknown_file_in_the_dump_fails_a2(self):
        ev = _healthy() + [_ev("file_start", file="謎.pdf",
                               doc_type="credit_card", expected_pages=1)]
        self.assertEqual(_status(ev, "A2"), V.FAIL)


class ColumnIndexContractTest(unittest.TestCase):
    """列位置の定数が製品の `MF_HEADERS` とずれていないこと。

    判定器は gspread に依存しないよう列位置を自前の定数で持つ。列が 1 つ
    増えただけで「H列が空である」の判定が別の列を見に行き、**赤くならずに
    緑のまま無意味になる**。ずれたら赤くなる番人をここに置く。
    """

    def test_column_constants_match_the_product_headers(self):
        from sheets_output import MF_HEADERS, AUDIT_HEADERS
        self.assertEqual(MF_HEADERS[V.COL_DEBIT_ACCOUNT], "借方勘定科目")
        self.assertEqual(MF_HEADERS[V.COL_DEBIT_INVOICE], "借方インボイス")
        self.assertEqual(MF_HEADERS[V.COL_DEBIT_AMOUNT], "借方金額(円)")
        self.assertEqual(MF_HEADERS[V.COL_CREDIT_INVOICE], "貸方インボイス")
        self.assertEqual(MF_HEADERS[V.COL_CREDIT_AMOUNT], "貸方金額(円)")
        self.assertEqual(MF_HEADERS[V.COL_MEMO], "摘要")
        self.assertEqual(MF_HEADERS[V.COL_TAG], "タグ")
        self.assertEqual(len(MF_HEADERS), MF_WIDTH)
        self.assertEqual(len(AUDIT_HEADERS), AUDIT_WIDTH)

    def test_verdict_words_match_the_reconciliation_module(self):
        """判定器は `card_reconciliation` を import しない（dump だけで走れる
        独立性を保つため）。代わりに語の一致をここで縛る —— 産地で語を
        変えたら赤くなる。import しない判断の対価がこの番人テスト。
        """
        import card_reconciliation as cr
        self.assertEqual(V.VERDICT_MATCH, cr.VERDICT_MATCH)
        self.assertEqual(V.VERDICT_MISMATCH, cr.VERDICT_MISMATCH)
        self.assertEqual(V.VERDICT_UNVERIFIABLE, cr.VERDICT_UNVERIFIABLE)
        self.assertEqual(V.VERDICT_NOT_APPLICABLE, cr.VERDICT_NOT_APPLICABLE)
        self.assertEqual(sorted(V.KNOWN_VERDICTS), sorted(
            [cr.VERDICT_MATCH, cr.VERDICT_MISMATCH,
             cr.VERDICT_UNVERIFIABLE, cr.VERDICT_NOT_APPLICABLE]))

    def test_credit_account_constant_matches_the_product_default(self):
        """A3 が除外する調整行の目印。貸方の既定科目と同じ語であること。"""
        from doc_types import DOC_TYPE_CONFIG, DocType
        self.assertEqual(
            V.ADJUSTMENT_DEBIT_ACCOUNT,
            DOC_TYPE_CONFIG[DocType.CREDIT_CARD]["default_credit"])

    def test_invoice_tag_matches_the_product_constant(self):
        from config import INVOICE_CONFIRM_TAG
        self.assertEqual(V.INVOICE_CONFIRM_TAG, INVOICE_CONFIRM_TAG)

    def test_audit_verdict_words_match_the_product_constants(self):
        from sheets_output import (AUDIT_VERDICT_EXCLUDED,
                                   AUDIT_VERDICT_MISSING)
        self.assertEqual(V.VERDICT_EXCLUDED, AUDIT_VERDICT_EXCLUDED)
        self.assertEqual(V.VERDICT_MISSING, AUDIT_VERDICT_MISSING)

    def test_event_version_matches_the_producer(self):
        self.assertEqual(V.EVENT_VERSION, EVENT_VERSION)


class ExpectationPartitionTest(unittest.TestCase):
    """期待値の 3 分割が `LABELS` を過不足なく覆うこと。

    覆っていないと `EXPECT_UNVERIFIABLE` は「allowlist に見えて何も
    検査しない飾り」になる。モジュール読込時に落ちる設計なので、
    ここでは読込が成功した事実と分割の内容を固定する。
    """

    def test_the_three_expectation_sets_partition_labels(self):
        union = (set(V.EXPECT_MATCH) | set(V.EXPECT_UNVERIFIABLE)
                 | set(V.EXPECT_NOT_APPLICABLE))
        self.assertEqual(union, set(V.LABELS))

    def test_the_three_expectation_sets_do_not_overlap(self):
        total = (len(V.EXPECT_MATCH) + len(V.EXPECT_UNVERIFIABLE)
                 + len(V.EXPECT_NOT_APPLICABLE))
        self.assertEqual(total, len(V.LABELS))

    def test_a_label_outside_every_set_is_reported_not_defaulted(self):
        """暗黙の else で「検算不能のはず」に落とさないこと。"""
        with mock.patch.object(V, "EXPECT_UNVERIFIABLE", ("JCB", "UC")):
            ev = _healthy()
            self.assertEqual(_status(ev, "A2"), V.FAIL)


class LabelCollisionTest(unittest.TestCase):
    """2 つのファイルが同じラベルへ落ちたら止めること。

    落ちると頁・行・recon が 1 つのキーへ混ざる。上書きは静かに起きるので
    A1 が「想定外の頁」と言うだけになり原因が見えない。最悪、混ざった結果が
    偶然つじつまを合わせて PASS しうる。
    """

    def test_two_files_resolving_to_one_label_fails_a2(self):
        ev = _healthy() + [_ev("file_start", file="楽天カード_別の明細.pdf",
                               doc_type="credit_card", expected_pages=1)]
        self.assertEqual(_status(ev, "A2"), V.FAIL)

    def test_the_same_file_appearing_twice_is_not_a_collision(self):
        """再送・重複イベントは衝突ではない（同じファイル名なので混ざらない）。"""
        ev = _healthy()
        first = [e for e in ev if e["kind"] == "file_start"][0]
        ev.append(dict(first))
        self.assertEqual(_status(ev, "A2"), V.PASS)


if __name__ == "__main__":
    unittest.main()
