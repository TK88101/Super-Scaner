"""T8-3: `page_family` の裁決を生産経路へ接線した部分の単体テスト。

守りたい実害は 2 つある。

1. **半開状態** —— `_yield_page_results` の呼出点は逐頁 PDF ループと
   尾段（単頁 PDF・画像）の 2 箇所ある。片方だけ `page_class` を渡すと、
   その経路のリボ頁だけ従来どおり赤い認識不能行になり、**症状が出るのは
   顧客が単頁でアップロードしたときだけ**になる。AST 番人テストで縛る。
   （実際この改修中、mock の署名が追従しておらず `TypeError` が外層 except に
   吞まれて**全頁が消える**という回帰を出した。签名不一致は静かに効く。）

2. **監査シグナルの食い合い** —— `_with_audit_signal` は無条件上書きなので、
   族シグナルと `card_salvage` の行欠けシグナルが同じ頁で立つと片方が消える。

外部依存なし（gspread / paddleocr 不要）:
    python -m unittest test_page_disposition_wiring -v
"""
import ast
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import card_salvage  # noqa: E402
from ocr_test_fixtures import AMEX_HEAD  # noqa: E402
import ocr_engine  # noqa: E402
import page_family  # noqa: E402
from doc_types import DocType  # noqa: E402

# リボ頁の版面（アメックス p8 の実測構造。顧客実名・カード番号は含めない）
_RIBO_TEXT = (
    "ペイフレックス登録状況 明細書作成日 2023年1月19日 "
    "登録プラン名 ペイフレックスあとリボ 登録日 2020年10月8日 "
    "リボルビング払い利用可能枠 1,500,000 基本手数料率 14.90 実質年率 14.6 "
    "あとリボ変更締切日 2023年1月30日 今回ご請求金額 0"
)
_DETAIL_TEXT = "アメリカン・エキスプレス ご利用代金明細書 1/6 ページ"


def _raw(rows=(), rows_on_page=None, **extra):
    raw = {"card": {}, "rows": list(rows), "printed_totals": [], "sections": [],
           "total_amount": 0}
    if rows_on_page is not None:
        raw["rows_on_page"] = rows_on_page
    raw.update(extra)
    return raw


def _row(amount=1000, date="2023/01/05", merchant="テスト加盟店"):
    """明細 1 行。**键名は生産が読むものに揃える**。

    `card_entries._description` が読むのは `merchant` であり、
    `description` は出力専用の键（入力としては誰も読まない）。誤ると
    商家名が空の entry が黙って出来て、テストは緑のまま死んだ標本を
    検証し続ける（`ocr_test_fixtures` の docstring が警告する漂移）。
    """
    return {"date": date, "merchant": merchant, "amount": amount}


def _run(doc_type, raw, text, page_class=None):
    with contextlib.redirect_stdout(io.StringIO()):
        return list(ocr_engine._yield_page_results(
            doc_type, raw, text, None, page_class=page_class))


class RiboPageIsExcludedTest(unittest.TestCase):
    """要求 #6: リボ頁は記帳せず監査タブへ 1 行だけ残す（趙裁定 2026-08-19）。"""

    def test_ribo_page_with_no_entries_goes_to_audit_tab(self):
        pc = page_family.classify_page(_RIBO_TEXT)
        results = _run(DocType.CREDIT_CARD, _raw(), _RIBO_TEXT, pc)

        self.assertEqual(len(results), 1, "頁は必ず 1 件以上 yield する（IP-401）")
        r = results[0]
        self.assertTrue(r.get("_excluded_page"))
        self.assertEqual(r.get("_exclude_destination"),
                         page_family.EXCLUDE_DEST_AUDIT_TAB)
        self.assertIn("payment_method_notice", r.get("_exclude_reason", ""))
        self.assertFalse(r.get("entries"), "除外頁は仕訳を作らない")

    def test_entries_win_over_the_family_signal(self):
        """IP-401 gate: entries が組めていれば除外経路は存在しない。"""
        pc = page_family.classify_page(_RIBO_TEXT)
        results = _run(DocType.CREDIT_CARD, _raw(rows=[_row()]), _RIBO_TEXT, pc)

        self.assertTrue(any(r.get("entries") for r in results),
                        "記帳されなければならない")
        self.assertFalse(any(r.get("_excluded_page") for r in results))

    def test_booked_ribo_page_still_leaves_a_branch_note(self):
        """記帳はするが「リボ頁で entries が組まれた」痕跡を監査タブへ残す。"""
        pc = page_family.classify_page(_RIBO_TEXT)
        results = _run(DocType.CREDIT_CARD, _raw(rows=[_row()]), _RIBO_TEXT, pc)

        signals = [r.get("_audit_signal") for r in results if r.get("_audit_signal")]
        self.assertTrue(signals, "痕跡が無いと偽 entry が無印で通る")
        self.assertIn("family_signal_with_entries:payment_method_notice", signals[0])


class MfTabRowCarriesAReadableMemoTest(unittest.TestCase):
    """MF タブへ出す除外行は顧客が読める摘要を持つ（codex review P2）。

    監査タブ行は摘要列を持たないので memo は空でよい。しかし MF タブ行は
    **顧客の目に触れる**。空のまま渡すと `sheets_output._write_unrecognized_row`
    が「⚠ 認識不能ページ」にフォールバックし、**正常な合計表ページが
    OCR 失敗と見分けがつかなくなる**。

    合計表ページ（`FAMILY_CC_SUMMARY` → MF タブ）は T8 で**新たに有効化**
    された経路なので、この欠落は T8 が持ち込んだもの。
    """

    _SUMMARY_TEXT = "TS3 ご利用代金合計表 1/1 お支払合計 44,490"

    def test_summary_page_goes_to_the_mf_tab_with_a_memo(self):
        pc = page_family.classify_page(self._SUMMARY_TEXT)
        results = _run(DocType.CREDIT_CARD, _raw(), self._SUMMARY_TEXT, pc)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r.get("_excluded_page"))
        self.assertEqual(r.get("_exclude_destination"),
                         page_family.EXCLUDE_DEST_MF_TAB)
        self.assertTrue(
            r.get("memo"),
            "MF タブ行の摘要が空だと『⚠ 認識不能ページ』に化け、"
            "正常な合計表が OCR 失敗と区別できなくなる")

    def test_audit_tab_rows_do_not_need_a_memo(self):
        """監査タブ行は摘要列を持たないので空でよい（無駄な文言を作らない）。"""
        pc = page_family.classify_page(_RIBO_TEXT)
        r = _run(DocType.CREDIT_CARD, _raw(), _RIBO_TEXT, pc)[0]
        self.assertEqual(r.get("_exclude_destination"),
                         page_family.EXCLUDE_DEST_AUDIT_TAB)
        self.assertFalse(r.get("memo"))


class ShortageBeatsExclusionTest(unittest.TestCase):
    """行欠けの疑いがある頁は除外しない（Codex 評審 MEDIUM-1）。

    除外して監査タブへ静かに送ると、解析失敗で行を落とした頁が
    「正常に除外された頁」に化ける。疑わしいときは**うるさい赤**を選ぶ。
    """

    def test_a_page_suspected_of_missing_lines_is_not_excluded(self):
        pc = page_family.classify_page(_RIBO_TEXT)
        raw = _raw(rows_on_page=12)          # 券面は 12 行と申告、rows は空
        raw[card_salvage.SALVAGED_KEY] = True
        results = _run(DocType.CREDIT_CARD, raw, _RIBO_TEXT, pc)

        self.assertFalse(any(r.get("_excluded_page") for r in results),
                         "行欠けの疑いがある頁を監査タブへ沈めてはいけない")


class AuditSignalsAreNotSwallowedTest(unittest.TestCase):
    """族シグナルと行欠けシグナルが同じ頁で立っても片方が消えない。

    `_with_audit_signal` は無条件上書きなので、素直に載せ直すと消える
    （Codex 評審 HIGH-2）。
    """

    def test_merge_keeps_both(self):
        self.assertEqual(ocr_engine._merge_audit_signals("a", "b"), "a;b")

    def test_merge_drops_empties_but_keeps_the_rest(self):
        self.assertEqual(ocr_engine._merge_audit_signals(None, "b"), "b")
        self.assertEqual(ocr_engine._merge_audit_signals("a", None), "a")
        self.assertIsNone(ocr_engine._merge_audit_signals(None, None))

    def test_family_signal_survives_the_line_mode_pass(self):
        """族シグナルが `_yield_line_mode_results` を通り抜けても消えないこと。

        **当初の分析は誤りだった**（simplify 評審 2026-08-19 が訂正）。
        「行欠け(shortage)」の枝では確かに別 result に分かれるが、
        `card_salvage.page_marks` にはもう一つの枝がある —— 「救済は経たが
        行数は充足」のとき `(None, "salvaged:X/Y")` を返し、これは
        **entries を持つ同一 result** に `_with_audit_signal` で載る。
        つまり Codex 評審 HIGH-2 の食い合いは**現に起きうる**。
        合成の実在経路は下の test_salvaged_and_family_signals_coexist が固定する。
        """
        pc = page_family.classify_page(_RIBO_TEXT)
        raw = _raw(rows=[_row()], rows_on_page=12)   # 記帳あり ＋ 行欠けあり
        raw[card_salvage.SALVAGED_KEY] = True
        results = _run(DocType.CREDIT_CARD, raw, _RIBO_TEXT, pc)

        signals = [r.get("_audit_signal") for r in results if r.get("_audit_signal")]
        self.assertTrue(
            any("family_signal_with_entries:payment_method_notice" in sig
                for sig in signals),
            "族シグナルが line-mode 通過で消えた")
        self.assertTrue(any(r.get("entries") for r in results),
                        "行欠けが在っても記帳は止めない")

    def test_salvaged_and_family_signals_coexist(self):
        """救済痕跡と族シグナルが**同一 result** に同居する実在経路。

        `card_salvage.page_marks` は「救済は経たが行数は充足」のとき
        `(None, "salvaged:X/Y")` を返し、`_yield_line_mode_results` が
        それを entries 持ちの result に載せる。ここへ族シグナルを素直に
        載せ直すと片方が消える —— `_merge_audit_signals` が防いでいるのは
        この経路であって、仮想の将来ではない。
        """
        pc = page_family.classify_page(_RIBO_TEXT)
        raw = _raw(rows=[_row()], rows_on_page=1)   # 申告 1 行・取得 1 行＝充足
        raw[card_salvage.SALVAGED_KEY] = True
        results = _run(DocType.CREDIT_CARD, raw, _RIBO_TEXT, pc)

        merged = next((r["_audit_signal"] for r in results if r.get("_audit_signal")), "")
        self.assertIn("salvaged", merged, "救済痕跡が消えた")
        self.assertIn("family_signal_with_entries:payment_method_notice", merged,
                      "族シグナルが消えた")
        self.assertIn(";", merged, "片方が上書きで消えている")


class LegacyDocTypesAreUntouchedTest(unittest.TestCase):
    """既存 4 doc_type はこの経路に入らない（1 バイトも挙動を変えない）。"""

    def test_receipt_is_not_routed_through_the_disposition(self):
        pc = page_family.classify_page(_RIBO_TEXT)   # 族シグナルが立つテキスト
        raw = {"documents": [{"date": "2023/01/05", "vendor": "テスト",
                              "items": [{"account": "消耗品費", "amount": 1000}]}]}
        results = _run(DocType.RECEIPT, raw, _RIBO_TEXT, pc)

        self.assertFalse(any(r.get("_excluded_page") for r in results),
                         "RECEIPT が card 系の裁決に巻き込まれてはいけない")

    def test_no_page_class_means_legacy_behaviour(self):
        """Vision 兜底など page_class が無い経路は従来どおり。"""
        results = _run(DocType.CREDIT_CARD, _raw(), _RIBO_TEXT, None)
        self.assertFalse(any(r.get("_excluded_page") for r in results))


class BothCallSitesPassPageClassTest(unittest.TestCase):
    """AST 番人: 生産の呼出点が**両方とも** `page_class` を渡していること。

    片方だけ接続された半開状態は、テストが無ければ誰も気づけない。
    逐頁 PDF ループ（`ocr_engine.py` の PDF 分割経路）と尾段（単頁 PDF・画像）
    の 2 経路があり、後者は「顧客が 1 枚だけアップロードしたとき」にしか
    通らないので、実運用で症状が出るまでに時間がかかる。
    """

    def test_every_production_call_passes_page_class(self):
        with io.open(os.path.join(os.path.dirname(__file__), "ocr_engine.py"),
                     encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "_yield_page_results"]

        self.assertGreaterEqual(len(calls), 2,
                                "生産の呼出点は逐頁ループと尾段の 2 箇所のはず")
        for call in calls:
            kwargs = {k.arg for k in call.keywords if k.arg}
            self.assertIn(
                "page_class", kwargs,
                "ocr_engine.py:%d の `_yield_page_results` 呼出が page_class を"
                "渡していない。この経路だけ裁決が効かない半開状態になる"
                % call.lineno)



# 区画切替が在る頁の最小刺激（合計行 ＝ 区画の終わり ＋ 区画頭 ＝ 次の始まり）。
# **版面そのものの判定は `test_section_audit` が持つ**。ここで見たいのは
# 「検出結果が `_audit_signal` まで届くか」という接線だけなので、
# 券面を丸ごと複製して 3 箇所目の標本を作らない（漂移の芽になる）。
_SECTION_SWITCH_TEXT = ("田中 様今月ご利用額合計 350,218 "
                        "今月ご利用額 花子様 1月1日 テスト 100")


def _sec_raw(sections, rows, sec=0):
    """`sections` 個の区画を申告し、`rows` 行すべてを区画 `sec` に属させた raw_data。"""
    return _raw(rows=[dict(_row(), sec=sec) for _ in range(rows)],
                rows_on_page=rows,
                sections=[{"index": i, "label": "今月ご利用額", "subtotal": None}
                          for i in range(sections)])


def _signals(results):
    return [r.get("_audit_signal", "") for r in results if r.get("_audit_signal")]


class SectionAuditIsWiredTest(unittest.TestCase):
    """T8b-2: 区画の突合結果が監査タブまで届く。**記帳は 1 行も減らない**。

    純関数側（`page_family.section_audit_signals`）の判定は
    `test_section_audit` が固定する。ここが見張るのは接線だけ ——
    T8 の教訓そのもので、**判定は実装済なのに誰も呼んでいない**という
    形の欠陥は判定側のテストでは捕まらない。
    """

    def test_undercount_reaches_the_audit_signal(self):
        """p5 の形（券面 2 区画・申告 1 区画）で警告が出る。"""
        results = _run(DocType.CREDIT_CARD, _sec_raw(1, 8), _SECTION_SWITCH_TEXT)
        self.assertTrue(
            any("section_undercount:2/1" in s for s in _signals(results)),
            "取りこぼしが監査タブに届いていない")

    def test_booking_is_never_stopped(self):
        """IP-401: 警告が出ても entries は 1 件も減らない。"""
        results = _run(DocType.CREDIT_CARD, _sec_raw(1, 8), _SECTION_SWITCH_TEXT)
        booked = sum(len(r.get("entries") or []) for r in results)
        self.assertEqual(booked, 8, "警告が記帳を削った")
        self.assertFalse(any(r.get("_excluded_page") for r in results))

    def test_single_section_page_stays_silent(self):
        """単一カード券面で誤報しない（T8b-2 の DoD）。"""
        text = "今月ご利用額 田中 太郎様 1月1日 テスト 100"
        results = _run(DocType.CREDIT_CARD, _sec_raw(1, 4), text)
        self.assertFalse([s for s in _signals(results) if "section_" in s])

    def test_vision_fallback_page_is_marked_unknown(self):
        """OCR テキストが無い経路（Vision 兜底）は unknown で可視化する。

        `page_class` が無いので `disposition` は None になる。ここで
        `getattr(None, ...)` を踏むと**頁ごと例外で消える**ので、
        接線は disposition 無しでも成立しなければならない。
        """
        results = _run(DocType.CREDIT_CARD, _sec_raw(1, 4), "")
        self.assertTrue(
            any("section_detection_unknown" in s for s in _signals(results)))
        self.assertEqual(sum(len(r.get("entries") or []) for r in results), 4)

    def test_transit_ic_is_not_checked(self):
        """nimoca の prompt に `sections` は無い。効かせると鳴り続ける。"""
        results = _run(DocType.TRANSIT_IC, _sec_raw(0, 4), _SECTION_SWITCH_TEXT)
        self.assertFalse([s for s in _signals(results) if "section_" in s])

    def test_section_signal_coexists_with_the_family_signal(self):
        """族シグナルと同居しても片方が消えない（Codex HIGH-2 と同じ形）。"""
        text = _RIBO_TEXT + " " + _SECTION_SWITCH_TEXT
        pc = page_family.classify_page(text)
        results = _run(DocType.CREDIT_CARD, _sec_raw(1, 4), text, pc)

        merged = next(iter(_signals(results)), "")
        self.assertIn("section_undercount", merged, "区画シグナルが消えた")
        self.assertIn("family_signal_with_entries", merged, "族シグナルが消えた")

class ExcludedPageSkipsTheSectionAuditTest(unittest.TestCase):
    """T8b-2 の既知の穴（simplify 評審 Altitude F3）。**独立課題として持ち越し中**。

    `_yield_page_results` は去向が EXCLUDE なら早期 return するので、
    その頁では区画突合が消費されない。今それが安全なのは
    `resolve_page_disposition` の優先序 3（行らしさは除外を拒否する）が
    「EXCLUDE に落ちる頁は has_detail_rows=False」を保証しているからで、
    **別関数の不変式に寄り掛かった安全性**である。検査器が「被検査者の外に
    在る」ことの意味には、内部の制御フロー判断からも独立であることが含まれる。

    到達条件（実測で構成可能）: Gemini が rows を 1 行も返さず（entry_count=0）、
    OCR は区画見出しと合計行を読めていて（markers=2）、明細本体は
    has_detail_rows の閾値（日付 5 ＋ 金額 5）に届かない。この頁は
    `cc_summary` として MF タブへ提示行が出るが、**その提示行の文言は
    「合計表ページ」** —— 実際は読み落とした明細頁なのに、そう見えない。

    今回直さない理由: 消費側が塞がっている。`main.process_file` の除外分岐
    （`_excluded_page` → `_record_excluded_page` → `continue`）は
    `_audit_signal` の処理に到達せず、しかも MF タブ行き（本ケース）は
    監査タブに何も書かない。producer 側に `_audit_signal` を積むだけでは
    **書き込まれない信号**になり、穴より質が悪い。塞ぐには main と
    local_test 双方の制御フローと「同一頁に監査 2 行」の語義を決める必要が
    あり、T8b の範囲を超える（趙の拍板事項）。

    **旧記述の訂正（2026-08-21）**: かつてここには「T9 でこの装飾子を外すこと」
    と書いてあった。趙は 2026-08-21 に**この 1 件を T9 から分離し、独立課題として
    持ち越す**ことを裁定した（T9 の残りより T11 の機械判定を先に通すため）。
    よってこれは「T9 の完了条件」ではない。着手時期は TBD。経緯は
    `docs/plans/2026-08-21-t11-e2e-machine-verdict.md` §3.4。

    **課題そのものは生きている。** 塞ぐときは装飾子を外すこと。外し忘れると
    `unexpectedSuccesses=1` で赤くなる（`RiboPageZeroBaselineTest` と同じ運用）。
    """

    @unittest.expectedFailure
    def test_excluded_page_should_still_carry_the_section_signal(self):
        pc = page_family.classify_page(_SECTION_SWITCH_TEXT)
        results = _run(DocType.CREDIT_CARD, _sec_raw(0, 0), _SECTION_SWITCH_TEXT, pc)

        self.assertTrue(any(r.get("_excluded_page") for r in results),
                        "前提が崩れている: この頁は除外されるはず")
        self.assertTrue(
            any("section_" in (r.get("_audit_signal") or "") for r in results),
            "除外頁でも区画の取りこぼしは可視化されるべき")

# ── T8d: 重複頁の接線 ＋ 錨のファイル内引き継ぎ ──────────────────
#
# T8 の教訓そのもの: **判定は実装済なのに誰も呼んでいない**という形の欠陥は
# 判定側のテスト（`test_page_dedup` / `test_card_file_state`）では捕まらない。
# ここが見張るのは接線だけ。

import card_file_state  # noqa: E402


def _card_raw(page="1/6", stmt="2026/05/21", rows=4, amount=630,
              month_day=None, member="****-******-26003"):
    """クレカ 1 頁分の raw_data（`card_prompts` の出力形）。"""
    row = {"date": "2026/04/10", "amount": amount, "merchant": "テスト加盟店"}
    if month_day is not None:
        row = {"date": None, "month_day": month_day, "amount": amount,
               "merchant": "テスト加盟店"}
    return {"card": {"member_no": member, "issuer": "AMEX",
                     "statement_page": page, "period": "2026-05",
                     "statement_date": stmt},
            "rows": [dict(row) for _ in range(rows)],
            "rows_on_page": rows, "printed_totals": [], "sections": [],
            "total_amount": amount * rows}


# **標本の正本は `ocr_test_fixtures`。** 手写しにすると実物とズレる ——
# 初版は発行体名を落としており、`classify_page` が `routing_family='unknown'`
# を返していた（実物は `cc_detail`）。つまり接線テストが**実物のアメックス頁
# では決して起きない経路**を駆動していた（Reuse 評審 2026-08-20 が実測）。
_CARD_OCR = AMEX_HEAD + " 17,295"
# ETC 明細頁の OCR（券面どおり年の下 2 桁が見えている＝日付欄が読める品質）。
# この背書が無い頁へは錨を継承しない（趙拍板 2026-08-20）。
_ETC_OCR = "ETC ご利用明細書 ご利用年月日 26 3 16 普通車 ETC通行料金 610"


def _run_page(doc_type, raw, text, page_class=None, state=None, page_num=1):
    with contextlib.redirect_stdout(io.StringIO()):
        return list(ocr_engine._yield_page_results(
            doc_type, raw, text, None, page_class=page_class,
            state=state, page_num=page_num))


class DedupIsWiredIntoProductionTest(unittest.TestCase):
    """**帳簿が黙って二重になる**唯一の欠陥への接線（新 P0）。

    実測（2026-08-20）: アメックス 6 枚 は p1≡p3・p2≡p4 のスキャン重複で、
    現行コードでも `PageDedupIndex.classify` は `duplicate` を返す。
    それでも 24 行が丸ごと記帳されていたのは、生産経路が
    `resolve_page_disposition(None, ...)` とハードコードで渡していたから。
    """

    def test_the_second_scan_of_the_same_page_is_excluded(self):
        state = card_file_state.CardFileState()
        pc = page_family.classify_page(_CARD_OCR)

        first = _run_page(DocType.CREDIT_CARD, _card_raw(), _CARD_OCR,
                          pc, state, page_num=1)
        self.assertTrue(any(r.get("entries") for r in first),
                        "前提: 1 枚目は記帳される")
        self.assertFalse(any(r.get("_excluded_page") for r in first))

        again = _run_page(DocType.CREDIT_CARD, _card_raw(), _CARD_OCR,
                          pc, state, page_num=3)
        self.assertTrue(any(r.get("_excluded_page") for r in again),
                        "重複頁が除外されていない（二重計上）")
        self.assertFalse(any(r.get("entries") for r in again))

    def test_the_excluded_duplicate_goes_to_the_audit_tab(self):
        state = card_file_state.CardFileState()
        pc = page_family.classify_page(_CARD_OCR)
        _run_page(DocType.CREDIT_CARD, _card_raw(), _CARD_OCR, pc, state, 1)
        again = _run_page(DocType.CREDIT_CARD, _card_raw(), _CARD_OCR,
                          pc, state, 3)
        excluded = [r for r in again if r.get("_excluded_page")][0]
        self.assertEqual(excluded["_exclude_destination"],
                         page_family.EXCLUDE_DEST_AUDIT_TAB)
        self.assertTrue(excluded["_exclude_reason"].startswith("duplicate_page:1"),
                        excluded["_exclude_reason"])

    def test_a_different_page_is_not_excluded(self):
        """偽陽性は仕訳を消す。別内容の頁は必ず記帳される。"""
        state = card_file_state.CardFileState()
        pc = page_family.classify_page(_CARD_OCR)
        _run_page(DocType.CREDIT_CARD, _card_raw(), _CARD_OCR, pc, state, 1)
        other = _run_page(DocType.CREDIT_CARD,
                          _card_raw(page="2/6", amount=3866), _CARD_OCR,
                          pc, state, 2)
        self.assertFalse(any(r.get("_excluded_page") for r in other))
        self.assertTrue(any(r.get("entries") for r in other))

    def test_a_page_that_booked_nothing_is_not_remembered(self):
        """記帳できなかった頁を索引に入れない。

        入れると後続の**真の**重複頁が除外され、その明細はどこにも
        1 回も記帳されない（最悪の欠陥）。

        **刺激の作り方が要点。** 単に `rows=[]` にすると指紋そのものが
        `is_eligible()` を満たさず、`entries` の条件が無くても登記されない
        —— つまりこの不変式を駆動しない（変異検証 2026-08-20 で実際に
        空振りしていた）。指紋は合格するが builder が 1 行も組めない形、
        すなわち **全行が `carry_over`**（前回分の口座振替。銀行明細側で
        起票されるので `card_entries._build` が `continue` する）を使う。
        """
        state = card_file_state.CardFileState()
        pc = page_family.classify_page(_CARD_OCR)
        no_entries = _card_raw()
        for row in no_entries["rows"]:
            row["kind"] = "carry_over"
            row["merchant"] = "前回分口座振替"

        first = _run_page(DocType.CREDIT_CARD, no_entries, _CARD_OCR,
                          pc, state, 1)
        self.assertFalse(any(r.get("entries") for r in first),
                         "前提: この頁は 1 行も記帳しない")

        good = _run_page(DocType.CREDIT_CARD, _card_raw(), _CARD_OCR,
                         pc, state, 3)
        self.assertFalse(any(r.get("_excluded_page") for r in good),
                         "記帳できなかった頁が後続の頁を消した"
                         "（この明細は 0 回しか記帳されない）")
        self.assertTrue(any(r.get("entries") for r in good))

    def test_an_ineligible_page_is_also_harmless(self):
        """否定対照: 指紋そのものが不合格な頁（rows 空）でも同じ結末。"""
        state = card_file_state.CardFileState()
        pc = page_family.classify_page(_CARD_OCR)
        broken = _card_raw()
        broken["rows"] = []
        _run_page(DocType.CREDIT_CARD, broken, _CARD_OCR, pc, state, 1)
        good = _run_page(DocType.CREDIT_CARD, _card_raw(), _CARD_OCR,
                         pc, state, 3)
        self.assertFalse(any(r.get("_excluded_page") for r in good))

    def test_legacy_doc_types_never_reach_the_index(self):
        state = card_file_state.CardFileState()
        raw = {"documents": [{"date": "2026/01/05", "vendor": "テスト",
                              "items": [{"account": "消耗品費", "amount": 1000}]}]}
        for page_num in (1, 3):
            results = _run_page(DocType.RECEIPT, dict(raw), "領収書",
                                None, state, page_num)
            self.assertFalse(any(r.get("_excluded_page") for r in results))

    def test_without_a_state_nothing_changes(self):
        """`state=None`（既存テスト・旧呼出）では従来どおり記帳する。"""
        pc = page_family.classify_page(_CARD_OCR)
        for page_num in (1, 3):
            results = _run_page(DocType.CREDIT_CARD, _card_raw(), _CARD_OCR,
                                pc, None, page_num)
            self.assertFalse(any(r.get("_excluded_page") for r in results))


class DuplicateBeatsTheShortageGuardTest(unittest.TestCase):
    """重複除外は「行欠けの疑い」より先に効く（A-D1）。

    `DUPLICATE` に到達した頁は原本と (日付, 金額) が逐字一致している。
    原本側に行欠けの監査行が既に出ているので、重複側を除外しても新たに
    失う情報は無い。除外しなければ確実に二重計上になる。
    """

    def test_a_duplicate_with_a_line_shortage_is_still_excluded(self):
        state = card_file_state.CardFileState()
        pc = page_family.classify_page(_CARD_OCR)
        short = _card_raw()
        short["rows_on_page"] = 9              # 券面 9 行・取得 4 行

        _run_page(DocType.CREDIT_CARD, short, _CARD_OCR, pc, state, 1)
        again = _run_page(DocType.CREDIT_CARD, short, _CARD_OCR, pc, state, 3)
        self.assertTrue(any(r.get("_excluded_page") for r in again),
                        "行欠けの疑いで重複除外が取り消された（二重計上）")

    def test_the_shortage_trace_is_kept_in_the_reason(self):
        """痕跡を潰さない（Codex 評審 C5）。"""
        state = card_file_state.CardFileState()
        pc = page_family.classify_page(_CARD_OCR)
        short = _card_raw()
        short["rows_on_page"] = 9
        _run_page(DocType.CREDIT_CARD, short, _CARD_OCR, pc, state, 1)
        again = _run_page(DocType.CREDIT_CARD, short, _CARD_OCR, pc, state, 3)
        reason = [r for r in again if r.get("_excluded_page")][0]["_exclude_reason"]
        self.assertIn("line_shortage:", reason,
                      "重複側の行欠けの痕跡が消えた: %r" % reason)

    def test_a_non_duplicate_shortage_still_blocks_exclusion(self):
        """重複でない除外候補は従来どおり赤い認識不能行へ倒す。"""
        state = card_file_state.CardFileState()
        pc = page_family.classify_page(_RIBO_TEXT)
        short = _card_raw(stmt="")
        short["rows"] = []
        short["rows_on_page"] = 9
        results = _run_page(DocType.CREDIT_CARD, short, _RIBO_TEXT,
                            pc, state, 1)
        self.assertFalse(any(r.get("_excluded_page") for r in results))


class AnchorIsCarriedAcrossPagesTest(unittest.TestCase):
    """明細書作成日をファイル内で引き継ぐ（新 P1）。

    実測: TS CUBIC p6 は `month_day='3/16'` を 57 行すべてに持ちながら
    `statement_date=''` で、`_card_date` が年を確定できず 57 行が空欄だった。
    """

    def test_the_ts_cubic_shape_gets_its_year(self):
        state = card_file_state.CardFileState()
        pc = page_family.classify_page("ETC ご利用明細書")

        # p5: ETC 副明細書の 1 頁目（自前の錨あり）
        _run_page(DocType.CREDIT_CARD,
                  _card_raw(page="1/ 5ページ", stmt="2026/05/15", rows=1),
                  _ETC_OCR, pc, state, 5)
        # p6: 錨なし・month_day だけ。OCR には券面どおり年 `26` が見えている
        results = _run_page(DocType.CREDIT_CARD,
                            _card_raw(page="2/5", stmt="", rows=3,
                                      month_day="3/16"),
                            _ETC_OCR, pc, state, 6)
        dates = [e["date"] for r in results for e in r.get("entries", [])]
        self.assertEqual(dates, ["2026/03/16"] * 3,
                         "錨が引き継がれず日付が空のまま: %r" % dates)

    def test_a_page_whose_date_column_is_unreadable_stays_empty(self):
        """ENEOS p2 の形（OCR に年が 1 度も出ない）は継承しない。

        継承させると、Gemini が月日を誤読していても書式の正当な日付に
        化けて赤タグが消える（実測: 券面 3月7日 を `3/3` と返す行が在る）。
        """
        state = card_file_state.CardFileState()
        pc = page_family.classify_page("ご利用明細")
        _run_page(DocType.CREDIT_CARD,
                  _card_raw(page="1/2ページ", stmt="2026/04/15", rows=1),
                  "カードご利用代金明細書 2026年4月15日発行", pc, state, 1)
        results = _run_page(DocType.CREDIT_CARD,
                            _card_raw(page="2/ 2ページ", stmt="", rows=2,
                                      month_day="3/30"),
                            "利用 ボイント 51.97 7:3801回松 Dr.Drive麦野SS",
                            pc, state, 2)
        dates = [e["date"] for r in results for e in r.get("entries", [])]
        self.assertEqual(dates, ["", ""],
                         "日付欄が読めない頁へ錨が流れた: %r" % dates)

    def test_without_the_carry_the_year_stays_empty(self):
        """前提の確認: 錨が無ければ空欄のまま（規則が効いている証拠）。"""
        state = card_file_state.CardFileState()
        pc = page_family.classify_page("ETC ご利用明細書")
        results = _run_page(DocType.CREDIT_CARD,
                            _card_raw(page="2/5", stmt="", rows=2,
                                      month_day="3/16"),
                            _ETC_OCR, pc, state, 6)
        dates = [e["date"] for r in results for e in r.get("entries", [])]
        self.assertEqual(dates, ["", ""])

    def test_the_inheritance_is_announced_once(self):
        state = card_file_state.CardFileState()
        pc = page_family.classify_page("ETC ご利用明細書")
        _run_page(DocType.CREDIT_CARD,
                  _card_raw(page="1/ 5ページ", stmt="2026/05/15", rows=1),
                  _ETC_OCR, pc, state, 5)
        p6 = _run_page(DocType.CREDIT_CARD,
                       _card_raw(page="2/5", stmt="", rows=2, month_day="3/16"),
                       _ETC_OCR, pc, state, 6)
        p7 = _run_page(DocType.CREDIT_CARD,
                       _card_raw(page="3/5", stmt="", rows=2, month_day="3/17"),
                       _ETC_OCR, pc, state, 7)
        self.assertTrue(any("date_year_from_page:5" in (r.get("_audit_signal") or "")
                            for r in p6), "継承が無音だった")
        self.assertFalse(any("date_year_from_page" in (r.get("_audit_signal") or "")
                             for r in p7), "同一ファイルで 2 回鳴った")

    def test_transit_ic_is_untouched(self):
        """交通系IC は `_ic_date`（実行日基準）のまま。錨を注入しない。"""
        state = card_file_state.CardFileState()
        raw = _card_raw(page="2/5", stmt="", rows=2, month_day="3/16")
        _run_page(DocType.TRANSIT_IC, raw, _ETC_OCR, None, state, 6)
        self.assertEqual(raw["card"]["statement_date"], "")


class BothCallSitesPassTheFileStateTest(unittest.TestCase):
    """AST 番人: `_yield_page_results` の**両方**の呼出点が state を渡すこと。

    `page_class` で確立した前例と同じ形。片方だけ接続された半開状態は、
    テストが無ければ誰も気づけない —— 尾段（単頁 PDF・画像）は
    「顧客が 1 枚だけアップロードしたとき」にしか通らないので、
    実運用で症状が出るまでに時間がかかる。
    """

    def test_every_production_call_passes_the_state(self):
        with io.open(os.path.join(os.path.dirname(__file__), "ocr_engine.py"),
                     encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "_yield_page_results"]

        self.assertGreaterEqual(len(calls), 2,
                                "生産の呼出点は逐頁ループと尾段の 2 箇所のはず")
        for call in calls:
            kwargs = {k.arg for k in call.keywords if k.arg}
            for required in ("page_class", "state", "page_num"):
                self.assertIn(
                    required, kwargs,
                    "ocr_engine.py:%d の `_yield_page_results` 呼出が %s を"
                    "渡していない。この経路だけ接線が効かない半開状態になる"
                    % (call.lineno, required))

    def test_the_pipeline_builds_exactly_one_state_per_file(self):
        """`process_pipeline` の中で 1 個だけ作る。

        頁ごとに作ると索引も錨も毎頁リセットされ、接線したのに何も
        効かない（T8 と同じ「見た目は繋がっているが働かない」形）。
        """
        with io.open(os.path.join(os.path.dirname(__file__), "ocr_engine.py"),
                     encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        pipeline = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef)
                        and n.name == "process_pipeline")
        builds = [n for n in ast.walk(pipeline)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "CardFileState"]
        self.assertEqual(len(builds), 1,
                         "process_pipeline での CardFileState 生成は 1 箇所のはず")


if __name__ == "__main__":
    unittest.main()
