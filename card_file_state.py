# card_file_state.py
# 1 ファイル（= 1 回の `ocr_engine.process_pipeline` 呼出）分の頁跨ぎ状態。
#
# 逐頁ジェネレータは「1 頁 yield したら捨てる」ことでメモリを一定に保っている
# （CLAUDE.md の Generator Pipeline）。**本モジュールは明細行を 1 行も保持しない。**
# 持つのは頁あたり数百バイトの指紋要約（`page_dedup.PageFingerprint`。sha256 と
# スカラ 4 個）と、券面の錨 1 個だけ。
#
# 抱える状態は 2 つ:
#   1. 重複頁の索引 —— `page_dedup` は完全実装されていたのに生産経路が一度も
#      呼んでいなかった（T8 と同型の「判定は在るが誰も呼ばない」欠陥）。
#   2. 明細書作成日の錨 —— 実測（2026-08-20）で TS CUBIC p6 は
#      `month_day='3/16'` を 57 行すべてに持ちながら `statement_date=''` で、
#      `card_entries._card_date` が年を確定できず 57 行が空欄になっていた。
#
# **公開メソッドは例外を外へ出さない。** ここで例外が漏れると
# `_yield_page_results` の整形例外になり、その頁が占位行 1 行に潰れる。
# 状態オブジェクトごときで IP-401（頁は無音で消えない・記帳は止めない）を
# 破らない。`page_dedup.safe_fingerprint` と同じ契約。
#
# gspread / paddleocr / google api に非依存。venv 無しで単体テストできる
# （`page_family` / `card_prompts` と同じ方針。`test_card_file_state` が見張る）。

import re
from datetime import timedelta

from card_entries import (_RE_STATEMENT_PAGE, _month_day_of, _nearest_past,
                          _norm as _card_norm, _parse_ymd)
from doc_types import DocType
# `_normalize_for_match`（NFKC ＋ 空白除去）は自前で書くと逐語複製になる。
# 同じ OCR 文字列を `extract_page_identity` と本モジュールが別規則で読むと、
# 頁付けの裏取りと拒否権が食い違って発火する（`test_card_file_state` が
# 同一性を見張る）。
from page_dedup import (PageDedupIndex, safe_fingerprint,
                        _normalize_for_match as _norm)

# 錨を持ち回すのは **credit_card だけ**。
# `transit_ic` の日付規則は `_ic_date`（実行日基準・印字を尊重）で別物であり、
# `statement_date` は交通系IC の prompt に存在しない（`card_prompts`
# `_IC_CARD_FIELDS`）。注入すると `CONSUMED_CARD_KEYS` の番人と食い違う。
_ANCHOR_DOC_TYPES = frozenset({DocType.CREDIT_CARD})

# 重複判定を効かせる doc_type。`page_family.CC_FAMILY_DOC_TYPES` と同値。
# あちらを import しても良いが、判定に使うのは doc_type の集合だけなので
# 依存を増やさず自前で持つ（`test_card_file_state` が両者の一致を突合する）。
_DEDUP_DOC_TYPES = frozenset({DocType.CREDIT_CARD, DocType.TRANSIT_IC})

# OCR テキスト中の頁付け候補。**数字が頁語の直前に在ること**を要求するのが
# 要点で、「ホームページ」「次ページへ続く」のような頁付けでない語で
# 拒否権が誤発火しないようにする（実測 TS CUBIC p7 に「次ペーへ統く」が在る）。
#
# 数字と頁語の間の空白は許す（`1/5 ページ`）。**ただし数字列そのものは
# 空白を跨がない** —— 跨がせると会員番号が頁付けに食い込む。実測:
# `****-******-26003 1/5 ページ` から空白を畳むと候補が `(260031, 5)` になり、
# 正しい Gemini 値 `1/5` が「矛盾」と判定されて錨の run が一度も開かなくなる
# （Codex 実施後評審 2026-08-20）。券面ごとに会員番号と頁付けの前後は違う。
_RE_OCR_PAGE_LABEL = re.compile(r"([0-9/／]{2,8})\s*(?:ページ|ペー|ペ|頁)")


def _parse_page_label(raw):
    """`"1/ 5ページ"` → `(1, 5)`。読めなければ `None`。

    実測した表記のゆれ: `'1/ 5ページ'` `'2/5'` `'2/ 2ページ'` `'1/6 ページ'`。

    **正規表現は `card_entries._RE_STATEMENT_PAGE` を借りる。** 自前で書くと
    同じ `card.statement_page` を 2 通りの規則で読むことになる（`card_salvage`
    の警告）。リポジトリには既に 2 本の解析器が在って `'1/100'` で
    `card_entries`=(1,100) / `page_dedup`=1/10 と割れており
    （`test_card_entries.py:233` が記録）、3 本目を足さない。
    `card_entries` 側を借りるのは、本モジュールが読むのが**カード明細の
    頁付け**という同じ対象だからである。
    """
    m = _RE_STATEMENT_PAGE.search(_norm(raw))
    if not m:
        return None
    k, n = int(m.group(1)), int(m.group(2))
    if k < 1 or n < 1 or k > n:
        return None                      # `3/2` のような壊れた値は読めない扱い
    return k, n


def _ocr_page_candidates(ocr_text):
    """OCR テキストから読み取れる頁付けの候補集合。

    **1 つの並びから複数の解釈を出す**のが要点。PaddleOCR は券面の区切りを
    そのままには読まない —— 実測のアメックスは `1/6 ページ` を `116ペー`、
    `2/6 ページ` を `216ペー` と読む（`/` が `1` に化けている）。したがって
    数字の並び `216` からは (2,16) (21,6) に加えて **(2,6)**（区切りが 1 桁
    食われた形）も候補に入れる。

    ここを「数字列の部分一致」で済ませると偽陽性と偽陰性の両方が出る。
    実際 `page_dedup.extract_page_identity` の containment 検査は、
    アメックス p2 で会員番号 `-26003` の中の `26` に**偶然**当たって
    通っていた（本モジュールの実装中に実測で判明）。判定を偶然に
    寄り掛からせない。

    **空白は畳まない。** 畳むと会員番号が頁付けに食い込む —— 実測で
    `****-******-26003 1/5 ページ` が `…260031/5ページ` になり、候補が
    `(260031, 5)` になって正しい Gemini 値 `1/5` を「矛盾」と誤判定した
    （Codex 実施後評審 2026-08-20）。`_ocr_mentions_the_anchor_year` が
    空白を保つ理由と同じで、券面は欄で区切って印字されている。
    """
    out = set()
    for m in _RE_OCR_PAGE_LABEL.finditer(_card_norm(ocr_text)):
        s = m.group(1).replace("／", "/")
        if "/" in s:
            k, _, n = s.partition("/")
            if k.isdigit() and n.isdigit():
                out.add((int(k), int(n)))
            continue
        for i in range(1, len(s)):
            out.add((int(s[:i]), int(s[i:])))          # 区切りが落ちた形
            if i + 1 < len(s):
                out.add((int(s[:i]), int(s[i + 1:])))  # 区切りが数字に化けた形
    return out


def _ocr_vetoes_page_label(ocr_text, label):
    """OCR に頁付けが在って Gemini の値と食い違うか（**拒否権**）。

    OCR 一致を**必須にはしない**（Codex 評審 R2-3 を一部採納）。実測で
    直したい 2 頁（TS CUBIC p6 の 57 行・ENEOS p2 の 2 行）は**どちらも
    OCR テキストに頁付けが無く**、必須にすると 59 行が全部空欄で残る。
    そこで非対称にする —— **在って食い違うときだけ拒否**する。
    「証拠の不在は不在の証拠ではないが、矛盾は矛盾である」。
    """
    if not label:
        return False
    candidates = _ocr_page_candidates(ocr_text)
    if not candidates:
        return False                     # 頁付けの徴候なし → 拒否権を行使しない
    return label not in candidates


# 錨として受け入れる年の範囲。**doc 級経路には既に在る保護が card 系だけ
# 抜けている**（`ocr_engine._validate_gemini_date` が年 2020–2027 を強制する
# のに、card 系は `_apply_ocr_overrides` ごと豁免されている）。
#
# 実測で確認済（HEAD 0588bdc）: `statement_date='1926/05/15'` を渡すと 57 行
# すべてが `1926/03/16` になり、`anomaly_detector` は `missing_vendor` /
# `missing_invoice` しか返さない —— **日付の異常は 0 件**。錨の継承はこの
# 穴を「1 頁」から「連続頁列ぜんぶ」へ広げるので、根元で止める。
# 値の出所は `ocr_engine._validate_gemini_date`（`test_card_file_state` が突合）。
ANCHOR_YEAR_MIN = 2020
ANCHOR_YEAR_MAX = 2027

# 継承した錨を前後にこれだけ動かしても年が変わらない行だけを採用する。
#
# 錨が別の明細書へ越境しても、**作成日の差がこの日数以内なら結果は同じ**に
# なる（`_nearest_past` は ref の年と前年しか見ないので、行の月日が 2 つの
# 作成日の間に落ちたときだけ答が割れる）。実測の余裕日数は TS CUBIC p6 が
# 53〜60 日、ENEOS p2 の最小が 3/30 の **16 日**。31 日にすると 3/30 が落ちて
# 修復が 1 行減るので、**15 日が上限かつ最適**。
#
# 副次効果として `_card_date` の既知局限（利用日が作成日より後だと前年へ
# 倒れる）の**拡大分**も塞ぐ。既存分（自前の錨を持つ頁）は触らない。
ANCHOR_SLACK_DAYS = 15


def _ocr_mentions_the_anchor_year(ocr_text, anchor):
    """継承先の頁の OCR 平文に、錨年の下 2 桁が 1 度でも現れるか。

    **これは「正しさを保証する条件」ではなく、継承を許すための保守
    フィルタである。** 低情報頁・短い頁・OCR が荒れた頁へ錨を流さない。

    ── 説明の訂正（2026-08-20 の実施後評審）─────────────────────
    当初この関数は「日付欄の印字品質ガード」と説明していた。
    「日付欄が OCR で読めない頁 ＝ 印字品質が悪い ＝ Gemini も誤読しやすい」
    という因果を主張していたが、**実測でその因果は確認できなかった**:

        'ガソリン 26,400'            → True（金額の中の 26）
        '4/26 セブンイレブン 1,200'   → True（利用日の「日」が 26）
        'ETC 20:15'                 → True（年 2020 のとき。時刻）
        利用日 4/20〜4/27 の 28 行の頁 → 2020〜2027 の**全年**で True

    測っているのは印字品質ではなく「その 2 桁がその頁に出るか」で、
    実効は「行数が多ければほぼ必ず通る」。ENEOS p2 が False だったのは
    行が 3 行しか無かったからで、印字品質とは無関係だった。
    「三段式の日付欄の文脈」に絞る案も試したが `ガソリン 26,400` と
    `ETC 20:15` は依然通る。**平坦化された OCR テキストからは
    「年欄」と他の数字を確実には区別できない**（区別には座標が要る）。

    ── それでも残す理由（趙拍板 2026-08-20 / Codex 同意）───────────
    予測性能のある規則としては弱い。残す正当性は因果ではなく
    **失敗方向が「継承しない＝空欄＋赤い `missing_date`」に限定される**
    ことにある。実測でも 1 件、無音の誤日付を止めている
    （ENEOS p2 の 1 行目。券面の真値 3月7日 を Gemini が `3/3` と返す。
    600dpi で拡大して目視確認済。継承させると `2026/03/03` が書式として
    正当な誤日付になり、`anomaly_detector` は 1 件も鳴らない）。

    **根拠の弱い保守フィルタを積んでよい条件（3 つすべてを満たすこと）**:
      1. 誤って弾いた結果が `missing_date` として顧客に見えること
      2. 誤って通しても「誤った値を確定させる」方向には倒れないこと
      3. 実測で少なくとも 1 件、無音の誤りを防いでいること
    次に似た規則を足したくなったら、この 3 条を先に確認する。

    ── 実装上の注意 ────────────────────────────────────────
    **空白は畳まない。** `26 3 16` を `26316` に潰すと 2 桁トークンが
    消え、逆に `3:21` と `26i` が隣接して偽の一致を作る（実測: 空白除去だと
    TS CUBIC p6 が 23 → 5 回、無関係な p7 が 0 → 2 回になる）。

    **4 桁の西暦は受け付けない。** 年の決定源はあくまで錨であって
    この検査ではない（平成26年＝2014 と西暦下 2 桁＝2026 が同形なので、
    2 桁トークンを年の出所へ昇格させてはいけない）。
    """
    if anchor is None:
        return False
    yy = "%02d" % (anchor.year % 100)
    return re.search(r"(?<!\d)%s(?!\d)" % yy, _card_norm(ocr_text)) is not None


def _fill_row_dates(rows, anchor):
    """継承した錨で行の日付を確定する。埋めた行数を返す。

    **`card.statement_date` には注入しない。** 行の `date` へ直接書く。
    こうすると下流（`card_entries._card_date`）から見て「Gemini が完全な
    日付を返した頁」と同じ形になり、**自前の錨を持つ頁の挙動は 1 バイトも
    変わらない**（改修前から日付が入っていた 289 行の逐字同一を守る）。

    **±`ANCHOR_SLACK_DAYS` 日動かして年が変わる行は触らない。** 錨が別の
    明細書へ越境していても、この条件を満たす行の答は変わらない。満たさない
    行は空欄のまま残り、`missing_date` の赤タグが立つ ——
    趙の既裁定「空欄より無音の誤年の方が危険」の順序どおり。
    """
    filled = 0
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        if _card_norm(row.get("date")).strip():
            continue                     # Gemini が既に完全な日付を持っている
        md = _month_day_of(row, "")
        if md is None:
            continue                     # 月日が読めない行は推さない
        resolved = _nearest_past(md[0], md[1], anchor)
        if not resolved:
            continue
        early = _nearest_past(md[0], md[1], anchor - timedelta(days=ANCHOR_SLACK_DAYS))
        late = _nearest_past(md[0], md[1], anchor + timedelta(days=ANCHOR_SLACK_DAYS))
        if not early or not late or early[:4] != late[:4]:
            continue                     # 錨を少し動かすと年が変わる → 触らない
        row["date"] = resolved
        filled += 1
    return filled


class _AnchorRun:
    """1 通の明細書ぶんの錨。**開いている連続頁列**として持つ。

    「最後に読めた錨」として持つと、明細書の境界を越えて漏れる
    （Codex 評審 R2-1 / R2-2 が構成した反例 2 つ）。錨に総頁数と
    「次に来るはずの頁番号」を持たせ、連番が崩れたら閉じる。
    """

    __slots__ = ("date", "total_pages", "expected_next", "origin_page")

    def __init__(self, date, total_pages, origin_page):
        self.date = date
        self.total_pages = total_pages
        self.expected_next = 2
        self.origin_page = origin_page


class CardFileState:
    """1 ファイル分の頁跨ぎ状態。**明細行は保持しない。**"""

    __slots__ = ("_dedup", "_run", "_announced")

    def __init__(self):
        self._dedup = PageDedupIndex()
        self._run = None
        self._announced = False

    # ── 重複頁 ────────────────────────────────────────────────────
    def classify(self, doc_type, page_num, ocr_text, raw_data):
        """`(DedupVerdict, token)` を返す。card 系以外は `(None, None)`。

        `token` は呼出側からは**中身を見ない不透明な物体**（実体は
        `PageFingerprint`）。`remember` に渡し返すためだけに存在する。
        内部に「直前の指紋」を退避して `remember(page_num)` にする案も
        あったが、classify と remember の間で頁が入れ替わる事故を型で
        防げない（Codex 評審 C8）。
        """
        try:
            if doc_type not in _DEDUP_DOC_TYPES:
                return None, None
            fingerprint = safe_fingerprint(ocr_text, raw_data)
            return self._dedup.classify(fingerprint, page_num), fingerprint
        except Exception as e:  # noqa: BLE001 - 索引ごときで頁を落とさない
            print("⚠️ 重複判定に失敗（判定をスキップ）: %s: %s"
                  % (type(e).__name__, str(e)[:80]))
            return None, None

    def remember(self, token, page_num):
        """**実際に記帳できた頁だけ**を索引へ登録する。

        `_unrecognized` に終わった頁（builder が 1 行も組めなかった頁）を
        登録してはいけない。その頁は Sheets に赤い占位行しか残さないのに、
        指紋は `raw_data["rows"]` から作られるので `is_eligible()` を
        満たしうる。登録すると後続の真の重複頁が除外され、その明細は
        **どこにも 1 回も記帳されない**（最悪の欠陥）。
        """
        try:
            if token is None:
                return
            self._dedup.remember(token, page_num)
        except Exception as e:  # noqa: BLE001
            print("⚠️ 重複索引への登録に失敗: %s: %s"
                  % (type(e).__name__, str(e)[:80]))

    # ── 明細書作成日の錨 ──────────────────────────────────────────
    def resolve_anchor(self, doc_type, raw_data, ocr_text, page_num):
        """この頁の錨を確定する。**補ったときだけ**監査シグナルを返す。

        **書き換えるのは `raw_data["rows"][*]["date"]`** であって
        `card["statement_date"]` ではない（`_fill_row_dates` を参照）。
        `card_entries._build` が builder の中で行の日付を読むので、
        呼出側は builder より**前**に本メソッドを通す必要がある。

        補う値は**同一 PDF 内の券面に印字されている**明細書作成日である。
        prompt の「跨頁禁止」は Gemini に対する制約（他頁の**明細行**を
        捏造させない）であって、プログラムが同一明細書内の券面記載を
        使うことを禁じてはいない。

        戻り値の監査シグナルは**ファイルにつき 1 回だけ**返す。毎頁鳴らすと
        監査タブが狼少年になる（`cc_summary` を注記しないと決めたのと同じ理由）。
        """
        try:
            return self._resolve_anchor(doc_type, raw_data, ocr_text, page_num)
        except Exception as e:  # noqa: BLE001 - 錨ごときで頁を落とさない
            print("⚠️ 明細書作成日の引き継ぎに失敗（引き継ぎをスキップ）: %s: %s"
                  % (type(e).__name__, str(e)[:80]))
            return ""

    def _resolve_anchor(self, doc_type, raw_data, ocr_text, page_num):
        if doc_type not in _ANCHOR_DOC_TYPES or not isinstance(raw_data, dict):
            return ""
        card = raw_data.get("card")
        if not isinstance(card, dict):
            return ""

        label = _parse_page_label(card.get("statement_page"))
        if _ocr_vetoes_page_label(ocr_text, label):
            label = None                 # 券面と食い違う頁付けは読めない扱い

        own = _parse_ymd(card.get("statement_date"))
        if own is not None:
            # 自前の錨を持つ頁。**行には一切触らない** ——
            # `card_entries._card_date` が従来どおり自分で年を決める。
            # 1 頁目でなければ（あるいは頁付けが読めなければ）構造が判らない
            # ので、継承の連鎖は作らずここで閉じる。
            self._run = (_AnchorRun(own, label[1], page_num)
                         if (label and label[0] == 1 and label[1] >= 2
                             and ANCHOR_YEAR_MIN <= own.year <= ANCHOR_YEAR_MAX)
                         else None)
            return ""

        if label is None or label[0] == 1:
            # 錨も頁付けも読めない頁、あるいは錨を持たない 1 頁目。
            # どちらも **run を閉じる**（fail-closed）。
            #
            # `label is None` を入れ忘れると、頁付けが読めない頁が run を
            # 素通りして、その先の頁が前の明細書の錨を継ぐ。実測の反例:
            #   p1: 錨あり `1/2`      → run(total=2, expected_next=2)
            #   p2: 錨なし 頁付けなし  → 素通り（run が開いたまま）
            #   p3: 錨なし `2/2`      → k==expected_next で**継承してしまう**
            # p2/p3 が別明細書なら書式の正当な誤年になる。±15 日ガードも
            # 通り抜ける（実施後評審 Round 2）。
            self._run = None
            return ""

        run = self._run
        if run is None or label is None:
            return ""                    # 読めないときは推さない（趙の裁定）
        k, n = label
        if n != run.total_pages or k != run.expected_next:
            self._run = None             # 連番が崩れた → 閉じる（fail-closed）
            return ""

        # 頁付けの連番は繋がっている。あとはこの頁の日付欄が読める品質か。
        # **品質不足で run は閉じない** —— 構造は繋がっているのだから、
        # 後続の頁は後続の頁で判定させる（この頁だけ空欄に倒す）。
        if _ocr_mentions_the_anchor_year(ocr_text, run.date):
            filled = _fill_row_dates(raw_data.get("rows"), run.date)
        else:
            filled = 0
        run.expected_next = k + 1
        origin = run.origin_page
        if k >= n:
            self._run = None             # 最終頁まで歩いた

        if filled == 0 or self._announced:
            return ""
        self._announced = True
        return "date_year_from_page:%s@%s@%d" % (
            origin, run.date.strftime("%Y/%m/%d"), filled)
