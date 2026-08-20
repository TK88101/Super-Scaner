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
    """T8b-2 の既知の穴（simplify 評審 Altitude F3）。**T9 の完了条件**。

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

    **T9 でこの装飾子を外すこと。** 外し忘れると `unexpectedSuccesses=1` で
    赤くなる（`RiboPageZeroBaselineTest` と同じ運用）。
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


if __name__ == "__main__":
    unittest.main()
