"""T7: `_apply_ocr_overrides` の doc_type 豁免（AD-3）。

Plan: `docs/plans/2026-08-18-t7-ocr-override-exemption.md`
母 Plan: `docs/plans/2026-08-12-credit-card-doctype.md` AD-3 / §5 T7

OCR 主導の日付・T番号上書きは**領収書の券面を前提にしている**。カード明細に
当てると 2 つの経路で帳簿を汚す:

- **日付**: `_extract_date_from_ocr` の `_skip_keywords` に「カード」「ポイント」が
  入っており、カード明細ではほぼ全ての日付が skip → `fallback_pool[-1]`
  ＝「頁面で最後に読まれた日付」が採用される。それが doc 級 date になり、
  T6 で行級化した B列 の**回帰先**を汚す。
- **T番号**: 券面の登録番号は**カード会社自身**のもの（F-11）。カード明細は
  適格請求書に該当せず（F-14）、行ごとの加盟店登録番号は構造上存在しない。

しかも `card_prompts` の出力 JSON には doc 級 `date` / `invoice_num` が
**そもそも無い**のに、上書きはキーの有無を問わず代入する ——
プロンプト側の改名では防げない。

venv311 必須（`ocr_engine` が google.generativeai / paddleocr を引く）:
    venv311/bin/python -m unittest test_ocr_engine_ocr_override_exempt -v
"""
import copy
import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(__file__))

import ocr_engine
from doc_types import DocType
from ocr_test_fixtures import AMEX_HEAD, NIMOCA_P1_RAW, UC_P2_RAW


# 抽出が**確実に成功する** OCR テキスト。券面標本の複製ではなく
# 「抽出器へのプローブ」である（標本の正本は `ocr_test_fixtures` 側）。
#
# T番号は `AMEX_HEAD` に実在する `T8700150009366` をそのまま使う ——
# これは F-11 が言う「券面の登録番号＝カード会社自身のもの」の実例であり、
# 加盟店のものではない。合成番号に置き換えると、この事実が標本から消える。
_OCR_DATE = "2026/03/31"
_OCR_TNUM = "T8700150009366"
_OCR_TEXT = AMEX_HEAD + " ご利用日 2026年3月31日"


def _run_page(doc_type, raw_data):
    """`_yield_page_results` を 1 頁分回して result の一覧を返す。

    `raw_data` は**必ず deep copy** して渡す。`_apply_ocr_overrides` は
    引数を破壊的に書き換えるので、module-level の共有 fixture をそのまま
    渡すと後続テストが汚染された標本を見る（しかも実行順依存で無症状）。
    """
    with redirect_stdout(io.StringIO()):
        return list(ocr_engine._yield_page_results(
            doc_type, copy.deepcopy(raw_data), _OCR_TEXT, None))


class _ProbeGuardedTest(unittest.TestCase):
    """プローブが劣化したら、この基底を継ぐテスト群を**丸ごと落とす**。

    なぜ要るか（実施後評審 P1-1）: `_apply_ocr_overrides` の date と
    invoice_num は**非対称**である ——

        if ocr_date: ... else: raw_data["date"] = validated   # 必ずキーが立つ
        if ocr_tnum: raw_data["invoice_num"] = ocr_tnum       # else 節が無い

    つまり T番号は「抽出できなかった」ときもキーが立たない。よって
    `assertNotIn("invoice_num", raw)` 系の断言は、**抽出器が本当に
    拾えている**ことに暗黙で依存している。将来 `_extract_invoice_num_from_ocr`
    がカード会社の登録番号を拾わない方向へ改修されると、豁免ゲートを
    削っても T番号側の断言は「たまたま緑」になる。

    `ExtractionProbeTest` はそれを独立に検知するが、**別クラスの緑は
    このクラスの緑を守らない**。前提が崩れた瞬間にここも落とす。
    """

    @classmethod
    def setUpClass(cls):
        date = ocr_engine._extract_date_from_ocr(_OCR_TEXT)
        tnum = ocr_engine._extract_invoice_num_from_ocr(_OCR_TEXT)
        if date != _OCR_DATE or tnum != _OCR_TNUM:
            raise AssertionError(
                "プローブが劣化した（date=%r, T番号=%r）。豁免テストは"
                "「上書きが走れば必ず値が載る」を前提にしており、この前提が"
                "崩れると豁免が効かなくても緑になる。プローブを直すこと。"
                % (date, tnum))


class ExtractionProbeTest(unittest.TestCase):
    """プローブ文字列が本当に抽出されることを先に固定する。

    これが取れていないと、豁免テストの緑が「豁免が効いた」なのか
    「そもそも抽出できていなかった」なのか区別できない ——
    番人が何も見ていないのに緑を出す典型形。
    """

    def test_the_probe_yields_a_date(self):
        self.assertEqual(ocr_engine._extract_date_from_ocr(_OCR_TEXT), _OCR_DATE)

    def test_the_probe_yields_the_card_issuers_t_number(self):
        self.assertEqual(
            ocr_engine._extract_invoice_num_from_ocr(_OCR_TEXT), _OCR_TNUM)


class ExemptDocTypesAreNotOverriddenTest(_ProbeGuardedTest):
    """豁免 doc_type の result に OCR 由来の doc 級 date / T番号が載らない。

    ここは**結線層**である。`_apply_ocr_overrides` 単体のテストは
    「呼出側が正しい doc_type を渡している」ことを一切証明しない
    （T6 で番人が結線を見落として緑を出した事故と同じ形）。

    台帳の豁免側を**全件**回す（実施後評審 P2-1）。券種ごとに列挙すると、
    3 つ目の豁免 doc_type を足した人は `OverrideLedgerTest` で「豁免する」と
    書かされるが、**それが結線層でも無害化される証明は誰にも強制されない**。
    """

    def test_no_exempt_doc_type_carries_an_ocr_date_or_t_number(self):
        # 断言は**全て** subTest の中に置く。頁数の断言だけ外に出すと、
        # 1 つ目の券種で yield 0 件だった瞬間にループごと中断し、2 つ目の
        # 券種は**一度も実行されない** —— 結線が 2 箇所同時に壊れていても
        # 1 箇所しか見えない。この番人が探しているのは正にその種の事故。
        for doc_type in _EXEMPT_DOC_TYPES:
            results = _run_page(doc_type, _exempt_raw_for(doc_type))

            with self.subTest(doc_type=doc_type, check="yielded"):
                self.assertTrue(results, "%s の頁が 1 件も yield しなかった"
                                         % doc_type)
            for result in results:
                with self.subTest(doc_type=doc_type,
                                  memo=result.get("memo")):
                    self.assertFalse(
                        result.get("date"),
                        "doc 級 date に OCR 値が載った: %r"
                        % (result.get("date"),))
                    self.assertEqual(result.get("invoice_num", ""), "")


class NonExemptDocTypesStillOverriddenTest(_ProbeGuardedTest):
    """豁免が**効きすぎていない**ことの対照（既存 doc_type は不変）。

    豁免集合へ既存 doc_type が混入すると、客の帳簿の日付が
    Gemini の年号幻覚（26年→2014年）に戻る。T7 の最大のリスクはこちら側。
    """

    def test_purchase_invoice_date_is_still_overridden_by_ocr(self):
        raw = {"date": "2014/01/01", "vendor": "テスト商事",
               "invoice_num": "", "items": []}
        results = _run_page(DocType.PURCHASE_INVOICE, raw)

        self.assertTrue(results)
        self.assertEqual(results[0].get("date"), _OCR_DATE)
        self.assertEqual(results[0].get("invoice_num"), _OCR_TNUM)


# ── 台帳: 全 doc_type について「上書きを豁免するか」を明記させる ────────
#
# `SuppressionLedgerTest`（`test_anomaly_detector_line_mode.py`）の写し。
# **実装定数から導出してはいけない** —— 導出すると「豁免集合に既存 doc_type を
# 混入させる」変異が台帳ごと一緒に動いてしまい、番人が自分の見張る対象と
# 同じ嘘をつく。ここは手書きの literal であることに意味がある。
_OVERRIDE_LEDGER = {
    # 領収書: OCR 上書きの本来の対象。Gemini の年号幻覚（26年→2014年）を
    #         PaddleOCR の正規表現で正す、という元々の設計意図そのもの。
    DocType.RECEIPT: {"exempt": False},
    # 請求書 2 種: 券面に doc 級の発行日と発行者 T番号が実在する。
    DocType.PURCHASE_INVOICE: {"exempt": False},
    DocType.SALES_INVOICE: {"exempt": False},
    # 給与明細: 券面に doc 級の支給日が実在する。T番号は無いが、
    #           上書きは「無ければ空欄」で終わるので害が無い。
    DocType.SALARY_SLIP: {"exempt": False},
    # クレカ: 日付は行ごとの利用日が正（doc 級は存在しない）。
    #         券面の登録番号はカード会社自身のもの（F-11 / F-14）。
    DocType.CREDIT_CARD: {"exempt": True},
    # nimoca: 年すら券面に無い（`card_entries._ic_date` が月日から復元する）。
    #         店名欄も登録番号も構造的に存在しない。
    DocType.TRANSIT_IC: {"exempt": True},
}

_NON_EXEMPT_DOC_TYPES = tuple(
    dt for dt, d in _OVERRIDE_LEDGER.items() if not d["exempt"])
_EXEMPT_DOC_TYPES = tuple(
    dt for dt, d in _OVERRIDE_LEDGER.items() if d["exempt"])


def _flat_raw(gemini_date="2014/01/01"):
    """非 documents フォーマット（請求書・給与明細・カード系が通る側）。"""
    return {"date": gemini_date, "vendor": "テスト商事", "invoice_num": ""}


def _documents_raw(gemini_date="2014/01/01"):
    """領収書の新フォーマット。単一書類（`is_multi_doc` を False にする）。"""
    return {"documents": [{"date": gemini_date, "vendor": "店",
                           "invoice_num": "", "items": []}]}


# 豁免 doc_type ごとの**実標本**。合成形で済ませないのは、
# 「本物のカード raw_data がこの形をしている」という事実こそが
# 「キーが生える＝上書きが新設した」の根拠だから。
_EXEMPT_RAW_SAMPLES = {
    DocType.CREDIT_CARD: UC_P2_RAW,
    DocType.TRANSIT_IC: NIMOCA_P1_RAW,
}


def _exempt_raw_for(doc_type):
    """豁免 doc_type の raw_data —— **doc 級 date / invoice_num を持たない**。

    標本が無い doc_type（将来足された 3 つ目）は最小合成形へ落とす。
    引き当て表の維持を強制すると、台帳と二重管理になって追随漏れが増える。
    """
    sample = _EXEMPT_RAW_SAMPLES.get(doc_type)
    if sample is not None:
        return copy.deepcopy(sample)
    return {"card": {"card_name": "テストカード"}, "rows": []}


class RawDataIsNotPollutedTest(_ProbeGuardedTest):
    """豁免 doc_type では raw_data に**キーが生えない**（単体層）。

    結線層（`ExemptDocTypesAreNotOverriddenTest`）は「result に載らない」しか
    見ていない。`_build_doc_result` が別のキーを読むようになったとき、
    raw_data 自体が汚れていれば同じ事故が別の列で再発する。

    台帳の豁免側を**全件**回す。券種ごとに列挙すると、3 つ目の豁免 doc_type を
    足した人が列挙を書き忘れても緑のまま通る。
    """

    def test_no_exempt_doc_type_grows_a_date_or_invoice_key(self):
        for doc_type in _EXEMPT_DOC_TYPES:
            with self.subTest(doc_type=doc_type):
                raw = _exempt_raw_for(doc_type)
                ocr_engine._apply_ocr_overrides(doc_type, raw, _OCR_TEXT)
                self.assertNotIn("date", raw)
                self.assertNotIn("invoice_num", raw)


class EveryNonExemptDocTypeIsStillOverriddenTest(_ProbeGuardedTest):
    """非豁免の**全件**対照（Codex R1①）。

    `test_sheets_output_golden.py` は `append_entries` の consumer 側 snapshot で
    あり、`_apply_ocr_overrides` を 1 行も通らない。よって「豁免集合へ既存
    doc_type が混入する」変異を golden では殺せない。列挙式に 2 件だけ書くと
    sales_invoice / salary_slip の混入を見逃すので、台帳の非豁免側を全件回す。
    """

    @staticmethod
    def _overridden(doc_type):
        """上書き後の書類 dict を返す（領収書だけ `documents` 形式）。

        日付と T番号で**テストを分けたまま** body の逐字コピーを消す。
        1 件に畳むと、どちらが壊れたのかが失敗名から読めなくなる。
        """
        raw = (_documents_raw() if doc_type == DocType.RECEIPT
               else _flat_raw())
        with redirect_stdout(io.StringIO()):
            ocr_engine._apply_ocr_overrides(doc_type, raw, _OCR_TEXT)
        return raw["documents"][0] if doc_type == DocType.RECEIPT else raw

    def test_date_is_overridden_for_every_non_exempt_doc_type(self):
        for doc_type in _NON_EXEMPT_DOC_TYPES:
            with self.subTest(doc_type=doc_type):
                self.assertEqual(self._overridden(doc_type)["date"], _OCR_DATE)

    def test_t_number_is_overridden_for_every_non_exempt_doc_type(self):
        for doc_type in _NON_EXEMPT_DOC_TYPES:
            with self.subTest(doc_type=doc_type):
                self.assertEqual(
                    self._overridden(doc_type)["invoice_num"], _OCR_TNUM)


class OverrideLedgerTest(unittest.TestCase):
    """doc_type が増えたとき、豁免の是非を**必ず明記させる**。

    CLAUDE.md が `ENTRY_BUILDERS` / `RECON_POLICY` について記録している
    「登録漏れが無症状で効かなくなる」失敗様式と同じ形。新 doc_type が
    黙って receipt 型の上書きを継承すると、その券種の帳簿が静かに汚れる
    —— 誰も気づかない種類の事故である。
    """

    def test_every_doc_type_has_an_explicit_decision(self):
        self.assertEqual(
            set(_OVERRIDE_LEDGER), set(DocType.ALL),
            "doc_type が増減した。OCR 上書きを豁免するか否かをこの台帳へ明記する "
            "こと（`LINE_MODE_DOC_TYPES` の別名にして自動追随させてはいけない。"
            "『逐行記帳である』と『doc 級の日付/登録番号が券面に無い』は別の軸）")

    def test_the_ledger_matches_the_implementation(self):
        for doc_type, decision in _OVERRIDE_LEDGER.items():
            with self.subTest(doc_type=doc_type):
                self.assertEqual(
                    doc_type in ocr_engine._OCR_OVERRIDE_EXEMPT_DOC_TYPES,
                    decision["exempt"])


class SignatureTest(unittest.TestCase):
    """`doc_type` は**必須の第 1 引数**であり続けること。

    既定値（`doc_type=None` 等）を付けると、書き忘れた新しい呼出側が黙って
    上書き経路へ落ちる。関数の内側にゲートを寄せた意味がそこで消える。
    必須引数なら `TypeError` で即死する ——「無音で誤る」より遥かに良い。

    **この番人の射程はこの関数の署名だけ**（実施後評審 P1-2）。
    `_apply_ocr_overrides_default_receipt(raw_data, ...)` のような
    doc_type を固定した別名ラッパーを新設してそちらを呼ぶ形なら、
    本関数の署名は無傷のまま同じ footgun が復活する。そこは署名検査では
    原理的に塞げない（まだ存在しないコードだから）——「呼出点は 1 箇所で
    あり続けよ」という番人は将来の正当な呼出者まで誤爆するので**置かない**。
    ラッパー新設は意図的な行為であり、評審で捕まえる。
    """

    def test_doc_type_is_the_first_parameter_and_has_no_default(self):
        import inspect

        params = list(
            inspect.signature(ocr_engine._apply_ocr_overrides).parameters.values())

        self.assertEqual(params[0].name, "doc_type")
        self.assertIs(params[0].default, inspect.Parameter.empty,
                      "doc_type に既定値を付けてはいけない（上の docstring 参照）")


if __name__ == "__main__":
    unittest.main()
