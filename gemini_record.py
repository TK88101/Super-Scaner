"""Gemini 応答の記録再生（record-replay）。**テスト工程専用。本番からは触らない。**

Plan: `docs/plans/2026-08-20-gemini-record-replay.md`（v3 定稿）。

## なぜ在るか

2026-08-20 の真票回帰で、**同じ PDF・同じコード・`temperature=0` で
Gemini の出力が 3 回とも違った**（TS CUBIC p8 の 54 行の日付が有→無→有、
`card_name` の充填が 58/208 → 1/210）。受入判定の外れが「コードの退行」か
「モデルの揺れ」かを切り分けるのに、実行時観測 3 回とコード読解を数十分要した。
`GEMINI_GENERATION_CONFIG` は既に `temperature: 0` なので、
**生成パラメータによる決定化は打ち止め**である。応答を録って再生するしかない。

## 設計の要（AD-3）

**`ocr_engine.py` は 1 行も変えない。** 有効化 API も状態も製品側に置かない。
本モジュールの context manager が、その中でだけ `ocr_engine` の関数を
差し替える。本番には分岐そのものが存在しないので「誤って再生に入る」経路が
**構造的に無い**（Plan R-1）。

その代償として、差し替え対象の関数名が暗黙の契約になる（R-4）——
改名されると patch は**静かに当たらなくなる**（例外も出ず、テストは緑のまま
実 Gemini を呼びうる）。`test_gemini_record_replay.PatchTargetContractTest` が
`PATCH_TARGETS` の 4 本を見張る。

## なぜ side-channel が要るか（v3 §12）

録音の鍵は「prompt が変わったのか OCR が揺れたのか」を言い分けられねばならない
（前者は自分の改修、後者は Plan R-3）。ところが `_generate_content_with_retry`
が受け取る時点では、prompt と OCR テキストは既に 1 本の文字列に連結されている。

区切り文字列で切り戻す案は**成立しない**: cross_validate の contents は
`[image, str]` なので、区切りが変えられると `_call_gemini_bytes` と**完全に
同形**になる。逆解析器からは「OCR を送らない正常系」と区別が付かず、
`ocr` が黙って null になって部位別診断だけが静かに死ぬ。

よって 3 変体（`_call_gemini_text` / `_call_gemini_bytes` /
`_call_gemini_cross_validate`）も薄く包み、`contextvars` で
`call_kind` / `prompt` / `ocr_text` を**実引数のまま**渡す。
`_call_gemini`（尾段）は `_call_gemini_bytes` へ委譲するだけなので**包まない**。

ただし側路だけでは足りない。連結の定型文（「上記のOCRテキストは参考情報です…」）
を書き換えると**実際に送る文字列は変わる**のに `prompt` 実引数は変わらない。
そこで部位 `text`（実送出文字列のハッシュ）を別に持つ。これが無いと、
定型文を変えたのに旧応答を再生し続ける ——「検証していないものを検証したと
誤認する」という、AD-4 が prompt 主キーを駁回したのと同じ破綻になる。

## 使い方

    with gemini_record.recording("fixtures/tscubic"):
        ...                      # 実 Gemini を呼び、応答をディスクへ録る

    with gemini_record.replaying("fixtures/tscubic"):
        ...                      # Gemini を 1 度も呼ばずに同じ結果を再現

録音には顧客の実票の中身が入る。**本 repo は PUBLIC** なので `fixtures/` は
`.gitignore` で全数排除している（`golden/` と同じ政策）。
"""
import contextlib
import contextvars
import datetime
import functools
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional, Tuple

import ocr_engine
import ocr_test_helpers

# 差し替える製品関数。改名されると patch が静かに外れる（R-4）ので、
# 名前の一覧はここ 1 箇所に置き、番人テストがこれを読む。
PATCH_TARGETS = (
    "_generate_content_with_retry",
    "_call_gemini_text",
    "_call_gemini_bytes",
    "_call_gemini_cross_validate",
)

PART_TEXT = "text"
PART_PROMPT = "prompt"
PART_OCR = "ocr"
PART_IMAGE = "image"
PART_CONFIG = "config"
PART_CALL_KIND = "call_kind"

# `text` は派生部位 —— `ocr` や `prompt` が動けば必ず一緒に動く。
# 差分の「説明」になるのはこちら側だけ。
EXPLANATORY_PARTS = (PART_PROMPT, PART_OCR, PART_IMAGE, PART_CONFIG,
                     PART_CALL_KIND)
ALL_PARTS = EXPLANATORY_PARTS + (PART_TEXT,)


class RecordError(Exception):
    """記録再生の異常の基底。

    再生時の異常は**すべて例外**にする。本番なら「落とさず兜底」が正しいが
    （IP-401 の教訓）、テスト工程で黙って実 Gemini へフォールバックすると、
    決定的なはずの回帰が**静かに非決定へ戻る** —— 本モジュールの目的そのものが
    壊れる。
    """


class SideChannelMissingError(RecordError):
    """側路が立っていない呼出（分類不能）。録音時も再生時も止める。

    黙って分類不能のまま録ると、使えない fixture が静かに溜まる。
    新しい呼出経路が `_call_gemini_parts` を直接叩くようになったら、
    ここで即座に露見する。
    """


class RecordingMissingError(RecordError):
    """録音ディレクトリが無い、または空（E-5）。"""


class ReplayMismatchError(RecordError):
    """録音と入力が食い違う（E-2）。どの部位が動いたかを本文に出す。"""


class ReplayExhaustedError(RecordError):
    """録音より呼出回数が多い（E-4）。"""


class ReplayIncompleteError(RecordError):
    """再生が終わったのに使われなかった録音が残っている。

    9 件録ったのに 5 件しか使われなかった = **4 頁が処理されていない**。
    IP-401（54 枚上げて 53 件しか記帳されず、枚数を数えるまで気付けなかった）
    と同族の無音欠落なので、再生層で止める（Codex 実装評審 中 5）。
    """


# --------------------------------------------------------------- side-channel

@dataclass(frozen=True)
class CallContext:
    """どの変体が、どんな実引数で呼ばれたか。"""

    call_kind: str
    prompt: str
    ocr_text: Optional[str] = None


_CURRENT_CALL = contextvars.ContextVar("gemini_record_current_call",
                                       default=None)


def current_call():
    """いま実行中の呼出の side-channel（立っていなければ `None`）。"""
    return _CURRENT_CALL.get()


@contextlib.contextmanager
def call_context(call_kind, prompt, ocr_text=None):
    """side-channel を 1 呼出のあいだ立てる。"""
    token = _CURRENT_CALL.set(CallContext(call_kind, prompt, ocr_text))
    try:
        yield
    finally:
        _CURRENT_CALL.reset(token)


def _wrap_text(original):
    @functools.wraps(original)
    def wrapper(ocr_text, prompt, line_mode=False):
        with call_context("text", prompt, ocr_text):
            return original(ocr_text, prompt, line_mode=line_mode)
    return wrapper


def _wrap_bytes(original):
    @functools.wraps(original)
    def wrapper(file_data, mime_type, prompt, line_mode=False):
        with call_context("bytes", prompt, None):
            return original(file_data, mime_type, prompt, line_mode=line_mode)
    return wrapper


def _wrap_cross_validate(original):
    @functools.wraps(original)
    def wrapper(ocr_text, file_data, mime_type, prompt, line_mode=False):
        with call_context("cross_validate", prompt, ocr_text):
            return original(ocr_text, file_data, mime_type, prompt,
                            line_mode=line_mode)
    return wrapper


_VARIANT_WRAPPERS = {
    "_call_gemini_text": _wrap_text,
    "_call_gemini_bytes": _wrap_bytes,
    "_call_gemini_cross_validate": _wrap_cross_validate,
}


@contextlib.contextmanager
def side_channel():
    """3 変体を包み、実引数を `current_call()` から読めるようにする。"""
    originals = {}
    try:
        for name, wrap in _VARIANT_WRAPPERS.items():
            original = getattr(ocr_engine, name)
            originals[name] = original
            setattr(ocr_engine, name, wrap(original))
        yield
    finally:
        for name, original in originals.items():
            setattr(ocr_engine, name, original)


# ----------------------------------------------------------------------- 鍵

def _sha(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


@dataclass(frozen=True)
class ContentKey:
    overall: str
    parts: dict
    call_kind: str
    prompt: str
    ocr_text: Optional[str]
    images: Tuple[dict, ...]
    text: str


def _split_contents(contents):
    """contents を文字列部品と blob 部品へ分ける（形の検査を兼ねる）。"""
    texts, blobs = [], []
    for part in contents:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict) and "data" in part:
            blobs.append(part)
        else:
            raise RecordError(
                f"未知の contents 部品を録音できない: {type(part).__name__}")
    return texts, blobs


def content_key(contents, generation_config=None, call=None):
    """呼出の指紋。同じ入力なら同じ録音が返る（AD-4 の主キー）。

    `generation_config=None` は `ocr_engine.GEMINI_GENERATION_CONFIG` へ
    **正規化**してから鍵に入れる。素通しにすると、モジュール既定
    （temperature や予算）を変えても fixture が失効せず、**違う設定の応答を
    再生し続ける**。
    """
    if call is None:
        raise SideChannelMissingError(
            "side-channel が立っていない呼出は録音・再生できない。"
            "`_call_gemini_text` / `_call_gemini_bytes` / "
            "`_call_gemini_cross_validate` のいずれも経由していない。")
    texts, blobs = _split_contents(contents)
    images = tuple({"sha256": _sha(blob["data"]),
                    "bytes": len(blob["data"]),
                    "mime_type": blob.get("mime_type", "")}
                   for blob in blobs)
    config = dict(generation_config or ocr_engine.GEMINI_GENERATION_CONFIG)
    # 部品の境界を跨いだ偶然の一致を防ぐため NUL で連結する
    sent_text = "\x00".join(texts)
    parts = {
        PART_TEXT: _sha(sent_text),
        PART_PROMPT: _sha(call.prompt),
        PART_OCR: None if call.ocr_text is None else _sha(call.ocr_text),
        PART_IMAGE: _sha(_canonical(list(images))) if images else None,
        PART_CONFIG: _sha(_canonical(config)),
        # 生値のまま持つ（meta.json をそのまま読んで診断できるように）
        PART_CALL_KIND: call.call_kind,
    }
    return ContentKey(overall=_sha(_canonical(parts)), parts=parts,
                      call_kind=call.call_kind, prompt=call.prompt,
                      ocr_text=call.ocr_text, images=images, text=sent_text)


# ------------------------------------------------------------------- 応答

_USAGE_FIELDS = ("total_token_count", "prompt_token_count",
                 "candidates_token_count")


def capture_response(response):
    """本物の response から `_parse_gemini_response` が読む 4 面だけを抜く。

    画像も候補も安全評価も保存しない —— 再生に必要なのは応答のこの 4 面だけで、
    それ以外は実票の機微を増やすだけである。
    """
    raises = False
    try:
        text = getattr(response, "text", "") or ""
    except ValueError:
        # zero-parts: SDK の `.text` は parts が空のとき ValueError を送出する
        text, raises = "", True
    usage_obj = getattr(response, "usage_metadata", None)
    usage = None
    if usage_obj is not None:
        usage = {}
        for field in _USAGE_FIELDS:
            try:
                usage[field] = int(getattr(usage_obj, field, 0) or 0)
            except (TypeError, ValueError):
                usage[field] = 0
    return {"text": text,
            "raises": raises,
            # `_get_finish_reason` は `str()` した値で判定するので、その形で持つ
            "finish_reason": ocr_engine._get_finish_reason(response),
            "usage": usage}


def build_response(payload):
    """`capture_response` の記録から偽 response を組み直す。

    SDK の応答形状のモデルは `ocr_test_helpers.gemini_response` に 1 箇所だけ
    置く約束なので、ここで作り直さず**それを再利用する**。複製を持つと、
    応答形状の語義が変わったとき片方だけ直って「半分が新契約・半分が旧契約」に
    なり、しかも両方緑になる（`ocr_test_helpers` の docstring が記録している
    実害）。

    `finish_reason` は `candidates[0].finish_reason` として復元される
    —— 平坦な値で持たせると `_get_finish_reason` が空を返し、**截断判定が
    静かに変わる**。
    """
    usage = payload.get("usage")
    return ocr_test_helpers.gemini_response(
        text=payload.get("text", "") or "",
        raises=bool(payload.get("raises")),
        finish_reason=payload.get("finish_reason") or "",
        usage=None if usage is None else SimpleNamespace(**usage))


# ------------------------------------------------------------------- 保存

@dataclass(frozen=True)
class Recording:
    seq: int
    call_kind: str
    overall: str
    parts: dict
    generation_config: dict
    prompt: str
    ocr_text: Optional[str]
    images: tuple
    response: dict


def _model_name():
    name = getattr(ocr_engine.model, "model_name", None)
    return name if isinstance(name, str) else "?"


def save_call(directory, seq, key, generation_config, response):
    """1 呼出を `{seq:04d}/` 配下の 3 ファイルへ落とす（Plan §3）。"""
    slot = os.path.join(directory, f"{seq:04d}")
    os.makedirs(slot, exist_ok=True)
    config = dict(generation_config or ocr_engine.GEMINI_GENERATION_CONFIG)
    documents = {
        "meta.json": {"seq": seq,
                      "call_kind": key.call_kind,
                      "content_sha256": key.overall,
                      "parts": key.parts,
                      "model": _model_name(),
                      "generation_config": config,
                      "recorded_at": datetime.datetime.now()
                      .astimezone().isoformat(timespec="seconds")},
        # 画像そのものは**保存しない**。実票の画像は最も機微であり、再生に
        # 必要なのは応答であって入力画像ではない。同一性はハッシュで判る。
        "contents.json": {"prompt": key.prompt,
                          "ocr_text": key.ocr_text,
                          "text": key.text,
                          "images": list(key.images)},
        "response.json": capture_response(response),
    }
    for name, document in documents.items():
        with open(os.path.join(slot, name), "w", encoding="utf-8") as f:
            json.dump(document, f, ensure_ascii=False, indent=2)
    return slot


def _slot_names(directory):
    """録音 slot（4 桁数字のディレクトリ）の名前を seq 順で返す。"""
    if not os.path.isdir(directory):
        return []
    return sorted(name for name in os.listdir(directory)
                  if name.isdigit()
                  and os.path.isdir(os.path.join(directory, name)))


def _prepare_recording_dir(directory, overwrite):
    """録音先を空にする。既存の録音がある場合は既定で拒否する。

    `exist_ok=True` で重ねて録ると、前回の方が長かったときに古い slot が
    残り、再生時の録音数も fingerprint の候補も実行と食い違う ——
    しかも**黙って**混ざる（Codex 実装評審 高 3）。
    """
    existing = _slot_names(directory)
    if existing and not overwrite:
        raise RecordError(
            f"録音先に既に {len(existing)} 件の録音があります: {directory}\n"
            "  そのまま録ると古い録音が残って再生時に混ざります。\n"
            "  別のディレクトリを使うか、overwrite=True "
            "（CLI では --record-overwrite）を指定してください。")
    for name in existing:
        shutil.rmtree(os.path.join(directory, name))
    os.makedirs(directory, exist_ok=True)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_recordings(directory):
    """seq 順に読む。無い / 空なら `RecordingMissingError`（E-5）。"""
    if not os.path.isdir(directory):
        raise RecordingMissingError(f"録音ディレクトリが無い: {directory}")
    slots = _slot_names(directory)
    recordings = []
    for name in slots:
        slot = os.path.join(directory, name)
        meta = _read_json(os.path.join(slot, "meta.json"))
        contents = _read_json(os.path.join(slot, "contents.json"))
        recordings.append(Recording(
            seq=meta["seq"], call_kind=meta["call_kind"],
            overall=meta["content_sha256"], parts=meta["parts"],
            generation_config=meta.get("generation_config", {}),
            prompt=contents.get("prompt", ""),
            ocr_text=contents.get("ocr_text"),
            images=tuple(contents.get("images", ())),
            response=_read_json(os.path.join(slot, "response.json"))))
    if not recordings:
        raise RecordingMissingError(f"録音が 1 件も無い: {directory}")
    return recordings


# ----------------------------------------------------------------- 差し替え

class Session:
    """1 回の録音 / 再生の実績。"""

    def __init__(self, directory, mode):
        self.directory = directory
        self.mode = mode
        self.calls = 0
        self.drifts = []
        # 順序が録音と違った呼出（AD-4 により順序は主キーではないので通すが、
        # 「コードが変わった証拠」なので黙って通さない）
        self.reorders = []
        # 実行中に記録再生の例外を投げたか。完走時の未消費検査を二重に
        # 叱らないために持つ。
        self.failed = False


@contextlib.contextmanager
def _patched_retry(handler):
    """`_generate_content_with_retry` を差し替え、必ず戻す。"""
    original = getattr(ocr_engine, "_generate_content_with_retry")
    setattr(ocr_engine, "_generate_content_with_retry", handler(original))
    try:
        with side_channel():
            yield
    finally:
        setattr(ocr_engine, "_generate_content_with_retry", original)


def _require_call():
    call = current_call()
    if call is None:
        raise SideChannelMissingError(
            "side-channel が立っていない呼出。`_call_gemini_parts` を直接叩く"
            "新しい経路が増えた可能性がある。分類できないものは録らない。")
    return call


@contextlib.contextmanager
def recording(directory, overwrite=False):
    """実 Gemini を呼び、その応答をディスクへ録る。

    既存の録音がある場合は既定で拒否する（`overwrite=True` で消してから録る）。
    """
    _prepare_recording_dir(directory, overwrite)
    session = Session(directory, "record")

    def handler(original):
        def patched(contents, generation_config=None):
            try:
                key = content_key(contents, generation_config, _require_call())
            except RecordError:
                session.failed = True
                raise
            response = original(contents, generation_config=generation_config)
            save_call(directory, session.calls, key, generation_config,
                      response)
            session.calls += 1
            return response
        return patched

    with _patched_retry(handler):
        yield session


def _drifted_parts(expected, actual):
    return [part for part in ALL_PARTS
            if expected.get(part) != actual.get(part)]


def _mismatch_message(index, key, recording_, drifted):
    lines = [f"再生できない: {index + 1} 回目の呼出"
             f"（call_kind={key.call_kind}）で "
             f"{', '.join(drifted)} が録音 seq={recording_.seq} と違う"]
    for part in drifted:
        lines.append(f"  {part}: 録音={recording_.parts.get(part)} "
                     f"実際={key.parts.get(part)}")
    return "\n".join(lines)


@contextlib.contextmanager
def replaying(directory, accept_drift=()):
    """録音から応答を返す。**実 Gemini は 1 度も呼ばない。**

    `accept_drift` に部位名を挙げると、その部位**だけ**が動いた不一致を許して
    続行する（既定は厳格停止）。`ocr` が動けば `text` も必ず動くので、
    説明の付く `text` 差は許す。しかし **`text` だけ**が動いたなら、それは
    連結の定型文や区切りが変わったということで、送った物が変わっている
    —— `--accept-drift ocr` では許さない。
    """
    recordings = load_recordings(directory)
    accepted = frozenset(accept_drift)
    unused = {}
    for item in recordings:
        unused.setdefault(item.overall, []).append(item)
    session = Session(directory, "replay")

    consumed = set()

    def take(item):
        """録音を消費済みにする。**唯一の消費点**にして二重使用を防ぐ。"""
        consumed.add(item.seq)
        bucket = unused.get(item.overall)
        if bucket and item in bucket:
            bucket.remove(item)
        return item

    def resolve(key, index):
        """この呼出に当てる録音を決める（当てられなければ例外）。"""
        bucket = unused.get(key.overall)
        if bucket:                                                  # E-1
            item = bucket[0]
            if item.seq != index:
                # 順序を主キーにしない裁定は AD-4（3 ラウンド裁決済み）——
                # コードの差分が「録音の取り違え」として現れ、無関係な行まで
                # 全部ずれるからである。よって通す。ただし順序が変わったこと
                # 自体はコードが変わった証拠なので、黙っては通さない。
                session.reorders.append({"seq": item.seq, "at": index})
                print(f"⚠️ 呼出順序が録音と違います: {index + 1} 回目の呼出に "
                      f"seq={item.seq} の録音を当てました")
            return take(item)
        if len(consumed) >= len(recordings):                        # E-4
            raise ReplayExhaustedError(
                f"録音は {len(recordings)} 件しかないのに "
                f"{index + 1} 回目の呼出が来ました（seq={index}）")
        # 差分の比較相手は「この呼出に対応するはずの録音」。ただし順序が
        # 入れ替わって既に消費済みなら、残っている中で最も早いものを使う ——
        # 消費済みを掴むと**同じ録音が 2 回答え**、別の録音が未消費で残る。
        # 未消費検査（E-6）が最後に叫ぶまで気付けず、その間の仕訳は
        # 間違った応答から組まれてしまう。
        item = recordings[index]
        if item.seq in consumed:
            item = next(r for r in recordings if r.seq not in consumed)
        drifted = _drifted_parts(item.parts, key.parts)
        explained = [part for part in drifted if part != PART_TEXT]
        forgivable = (set(explained) <= accepted if explained
                      else PART_TEXT in accepted)
        if not forgivable:                                          # E-2
            raise ReplayMismatchError(
                _mismatch_message(index, key, item, drifted))
        take(item)
        session.drifts.append({"seq": item.seq, "parts": drifted})
        print(f"⚠️ 録音との差分を許可して再生: seq={item.seq} "
              f"部位={', '.join(drifted)}（accept_drift 指定）")       # E-3
        return item

    def handler(_original):
        def patched(contents, generation_config=None):
            try:
                key = content_key(contents, generation_config, _require_call())
                item = resolve(key, session.calls)
            except RecordError:
                # 既に大声で落ちた。完走時の未消費検査で二重に叱らない。
                session.failed = True
                raise
            session.calls += 1
            return build_response(item.response)
        return patched

    with _patched_retry(handler):
        yield session

    # ここに到達するのは**本体が正常に終わったとき**だけ（本体が例外を投げた
    # 場合は yield から送出されて下は走らない）。原因を上書きしない。
    unconsumed = sorted(item.seq for bucket in unused.values() for item in bucket)
    if unconsumed and not session.failed:                           # E-6
        raise ReplayIncompleteError(
            f"再生は完走しましたが、使われなかった録音が "
            f"{len(unconsumed)} 件残っています（seq={unconsumed}）。\n"
            f"  呼出が {session.calls} 回しかありませんでした ——"
            f"頁が丸ごと処理されていない可能性があります。\n"
            "  録音の範囲と再生の範囲を揃えてください"
            "（--only-file で絞ったなら、その範囲だけを録り直す）。")
