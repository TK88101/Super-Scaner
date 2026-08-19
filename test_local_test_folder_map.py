"""`local_test.FOLDER_TYPE_MAP` の登録漏れを構造的に禁じる番人。

## この番人が守っている契約

**`local_test` の契約 ＝ `DocType.ALL` 全件をローカル入力フォルダ経由で
検証可能にすること。**

`FOLDER_TYPE_MAP` は doc_type 並行登録表の **7 枚目**である:

| # | 表 | 所在 | 検査 |
|---|---|---|---|
| 1 | `PROMPTS` | `ocr_engine.py` | `_validate_doc_type_registries`（import 時） |
| 2 | `ENTRY_BUILDERS` | `ocr_engine.py` | 同上 |
| 3 | `DOC_TYPE_CONFIG` | `doc_types.py` | 同上 |
| 4 | `DOC_TYPE_TAB_SUFFIX` | `doc_types.py` | 同上 |
| 5 | `ENV_FOLDER_MAP` | `doc_types.py` | 同上 |
| 6 | `RECON_POLICY` | `card_reconciliation.py` | `_validate_recon_policy`（import 時） |
| 7 | **`FOLDER_TYPE_MAP`** | **`local_test.py`** | **本ファイル** |

1〜6 はいずれも「書いたのに登録し忘れる」で事故を起こし、その都度
**不変式をコード側に自証させる**形へ移した（CLAUDE.md 参照）。7 枚目だけ
無防備にしておく理由が無い。ここの登録漏れの代価は
**「その doc_type がローカル検証不能になり、しかも誰も気づかない」** ——
`scan_local_files` は知らないフォルダを黙って素通りするので、
テストしたつもりで 0 件処理して終わる。

**契約の境界**（Codex 評審 2026-08-19 の複審で明文化を合意した点）:
これは「なんとなく全部揃えたい」ではない。`local_test.py` は真票を本番へ
流す前に人手で確かめる唯一の入口であり、そこに入口が無い doc_type は
**設計が正しいかどうかを実物で確認する手段が無い**。新しい doc_type を
足す人は、ここへ 1 行足すこと。

**1〜6 と違って import 時 `RuntimeError` にしていない理由**: 1〜6 は
生産経路に在り、漏登録が「1 行も yield されない → Failed → ファイル保持 →
3 秒後に再走査」という**無限リトライで Gemini を焼く**事故に直結する
（CLAUDE.md の `ENTRY_BUILDERS` 節）。`local_test.py` は開発ツールで
その帰結が無い。よってここは「起動を止める」ではなく
「`unittest discover` で赤くする」高度で足りる。

**逆に、漏登録は実行時には完全に無音である**（この番人が唯一の信号）。
`main()` は 0 件時に対象フォルダ一覧を印字するが、`scan_local_files` が
数えるのは**全登録フォルダの合計**なので、`receipt/` に旧いファイルが
1 つでも残っていればその分岐は走らない。未登録フォルダへ真票を置いた
開発者は、何の警告も無いまま「その 1 枚が処理されなかった」ことに
気づかない。楽観的な言い訳を書き足さないこと。

## なぜ `import local_test` しないのか

`local_test` は module 直下で `load_dotenv()` を呼び、`ocr_engine` を
import する。つまり import した瞬間に paddleocr / google.generativeai が
引き込まれ、venv311 でしか動かない重いテストになる。
`test_pipeline_consumers.py` が AST 方式を採っているのと同じ理由で、
ここもソースを **AST で読む**。依存は `doc_types` だけ（純データ、
サードパーティ import 無し）。

## 解析の契約（AST 番人が嘘をつかないための境界）

AST で読む以上、「読めた」と「読めなかった」を取り違えた瞬間に番人は
無音で穴を開ける。よって解析器は曖昧な形を一切受けず、読めなければ
`_UnreadableMapValue` を上げる（黙って捨てない）。個々の厳格さと
その理由は各関数の docstring が持つ —— `_top_level_bindings` /
`parse_folder_type_map` / `functions_reading_map` / `_folder_name_of` /
`_doc_type_of`。ここで一覧を再掲すると二重管理になるので置かない。

    venv311/bin/python -m unittest test_local_test_folder_map -v
    python3 -m unittest test_local_test_folder_map -v   # venv 無しでも動く
"""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from doc_types import DocType

REPO = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(REPO, "local_test.py")

MAP_NAME = "FOLDER_TYPE_MAP"

# `FOLDER_TYPE_MAP` を走査して仕事をしている関数。ここが map 以外の
# 情報源（ハードコードした一覧など）に切り替わると、map へ足しても
# フォルダが作られない／掃かれない状態が生まれる。
MAP_CONSUMERS = ("ensure_dirs", "scan_local_files")

# 別スコープ。これらの中身は「その関数が map を読んでいる」の証拠にならない。
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

# `match` 文の AST ノードは **Python 3.10+ にしか無い**。`ast.MatchAs` と
# 直書きすると 3.9 では import 時ではなく**呼出時**に `AttributeError` で
# 落ちる。本ファイルは冒頭で「venv 無しの系統 python3 でも動く」と謳って
# おり、mac の系統 python3 は 3.9.6 —— 実際にこれで壊した（venv311 だけ
# 緑で、脱 venv が全滅する典型）。参照は必ず `getattr` 経由にすること。
# `_NoBarePy310AstReferenceTest` が直書きの再発を禁じる。
_PY310_ONLY_AST_NODES = (
    "Match", "MatchAs", "MatchStar", "MatchMapping", "MatchValue",
    "MatchSequence", "MatchClass", "MatchSingleton", "match_case", "pattern")

# isinstance は空タプルを受けて常に False を返すので、3.9 では
# 「match 由来の束縛は存在しない」として素通りするだけで済む。
_MATCH_NAME_NODES = tuple(
    t for t in (getattr(ast, n, None) for n in ("MatchAs", "MatchStar"))
    if t is not None)
_MATCH_REST_NODES = tuple(
    t for t in (getattr(ast, "MatchMapping", None),) if t is not None)


class _UnreadableMapValue(Exception):
    """map の構造が本ファイルの解析契約から外れている。"""


def _as_tree(source_or_tree):
    """ソース文字列でも解析済み `ast.Module` でも受ける。

    実ファイルは 1 回だけ parse して使い回し（`setUpClass`）、
    合成 fixture はその場で文字列を渡す、という両方の呼び方を許すため。
    """
    if isinstance(source_or_tree, ast.Module):
        return source_or_tree
    return ast.parse(source_or_tree)


def _folder_name_of(node):
    """dict のキーノードからフォルダ名を得る。

    キーは常に文字列リテラル（既存 4 件の規約）。`**other` 展開は AST 上
    キーが `None` になるので、ここで明示的に弾く —— `_doc_type_of` を
    キーに流用すると `ast.dump(None)` で無関係な `TypeError` になり、
    エラーメッセージが原因を指さなくなる。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if node is None:
        raise _UnreadableMapValue(
            f"{MAP_NAME} で dict 展開（**other）は使えない。"
            "登録の全体像が 1 箇所で読めなくなる")
    raise _UnreadableMapValue(
        f"フォルダ名が文字列リテラルでない: {ast.dump(node)[:80]}")


def _doc_type_of(node):
    """dict の**値**ノードから doc_type 文字列を得る。

    `DocType.CREDIT_CARD` 形式と生の文字列の両方を受ける。前者が既存 4 件の
    書き方で、後者は将来誰かが直書きした場合の保険。どちらでもない形
    （関数呼出・変数参照など）は**黙って無視せず例外**にする。

    `DocType` の属性なら何でも良いわけではない: `DocType.ALL` は list であり
    doc_type ではない。型を検査しないと `set(mapping.values())` の段階で
    unhashable の `TypeError` になり、原因を指さないエラーで死ぬ。
    """
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id != "DocType":
            raise _UnreadableMapValue(f"未知の属性参照: {node.value.id}.{node.attr}")
        value = getattr(DocType, node.attr, None)
        if value is None:
            raise _UnreadableMapValue(f"DocType に存在しない属性: {node.attr}")
        if not isinstance(value, str):
            raise _UnreadableMapValue(
                f"DocType.{node.attr} は doc_type 文字列ではない "
                f"({type(value).__name__})")
        return value
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise _UnreadableMapValue(f"解釈できない値ノード: {ast.dump(node)[:80]}")


def _same_scope_nodes(body):
    """`body` と同じスコープで評価されるノード（入れ子スコープには潜らない）。

    入れ子の def / class / lambda の中で map に触れていても、その外側が
    map を読んでいる証拠にはならない（呼ばれない内部関数かもしれない）。
    内包表記には潜る —— Python 3 では実行時スコープが別だが、レキシカルには
    そのコードであり、`for x in MAP` を内包表記へ書き換える正当なリファクタで
    番人が赤くなるのは行き過ぎ。内包表記の**ターゲット**が map 名を
    shadow する病的な書き方（`[MAP for MAP in xs]`）は下の束縛検査に
    引っかかって「読んでいない」側へ倒れる —— 誤検知だが、無音で通す
    より人が一度見る方が安い（fail closed）。
    """
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _NESTED_SCOPES):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _binds_name(node, name):
    """このノード単体が `name` を束縛するか。

    `ast.Name(Store/Del)` だけを見てはいけない —— `def X()` / `class X` /
    `import ... as X` / `except ... as X` / `match` の捕捉はどれも
    `ast.Name` を作らないまま同じ名前を上書きする。数え漏らすと
    「モジュール直下の dict リテラルが実行時に効いている」という
    本ファイルの前提が静かに崩れる。
    """
    if isinstance(node, ast.Name):
        return node.id == name and isinstance(node.ctx, (ast.Store, ast.Del))
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    if isinstance(node, ast.alias):
        # `import a.b as X` は X、`import a.b` は a を束縛する
        return (node.asname or node.name.split(".")[0]) == name
    if isinstance(node, ast.ExceptHandler):
        return node.name == name
    if isinstance(node, _MATCH_NAME_NODES):     # 3.10+ のみ。3.9 では空タプル
        return node.name == name
    if isinstance(node, _MATCH_REST_NODES):     # 同上
        return node.rest == name
    return False


def _count_bindings(nodes, name):
    """`nodes` の中で `name` を束縛している箇所の数。

    **型註釈だけの `X: dict`（値なし）も数える。** モジュール直下の
    「先に註釈、後で代入」は `_assigns_name` の側で先に除かれるので
    ここで特別扱いする必要が無く、関数スコープでは PEP 526 のとおり
    裸の註釈がその名前をローカルにする（＝本物の束縛）。ここで除くと
    `def f(): FOLDER_TYPE_MAP: dict` を「module の map を読んでいる」と
    誤認する。除外を入れて変異検証したら誰も殺さず、かつ意味も逆だった。
    """
    return sum(1 for n in nodes if _binds_name(n, name))


def _assigns_name(stmt, name):
    """その文が**直接** `name` へ代入しているか（`a = X = {...}` の多目標も含む）。"""
    if isinstance(stmt, ast.Assign):
        return any(isinstance(t, ast.Name) and t.id == name for t in stmt.targets)
    if isinstance(stmt, ast.AnnAssign):
        return isinstance(stmt.target, ast.Name) and stmt.target.id == name
    return False


def _top_level_bindings(tree, name):
    """**モジュール直下**で `name` に代入している値ノードを出現順に返す。

    `ast.walk` で拾ってはいけない —— 関数の中のローカル変数や、条件分岐の
    中の代入まで拾ってしまい、実行時に効く定義とずれる。
    """
    bindings = []
    for node in tree.body:
        if not _assigns_name(node, name):
            continue
        if isinstance(node, ast.AnnAssign) and node.value is None:
            continue  # `X: dict` だけ。実行時には何も束縛しない
        bindings.append(node.value)
    return bindings


def _binds_outside_top_level_assignments(tree, name):
    """直下の `name = ...` **以外**に、モジュール実行時の束縛が在るか。

    在ると「直下の dict リテラルが実行時に効いている」という前提が崩れる:
    `if cond: name = {}` / `for name in xs:` / `def name()` /
    `import x as name` / `except E as name` など。
    どれが実際に効くかの path analysis は**しない** —— 在るだけで拒否する。
    """
    for stmt in tree.body:
        if _assigns_name(stmt, name):
            # 直下の代入。**ターゲット**は `_top_level_bindings` が数えて
            # いるが、右辺に隠れた束縛（walrus）は別物なので値側だけ見る。
            # `X = {k: (X := v)}` は直下代入に見えて実は 2 回束縛している。
            if stmt.value is not None and _count_bindings(
                    list(_same_scope_nodes([stmt.value])), name):
                return True
            continue
        if _count_bindings(list(_same_scope_nodes([stmt])), name):
            return True
    return False


def parse_folder_type_map(source_or_tree):
    """`FOLDER_TYPE_MAP` を `{folder_name: doc_type}` で返す。

    見つからなければ `None`（空 dict と区別する。空 dict は「在るが空」で、
    `None` は「そもそも定義が無い＝解析器が壊れたか名前が変わった」）。
    """
    tree = _as_tree(source_or_tree)
    bindings = _top_level_bindings(tree, MAP_NAME)

    # 直下の完全な定義だけ読んで緑を出すと、実行時に効いている不完全な方を
    # 見逃す（`FOLDER_TYPE_MAP = {...完全...}` の後ろに `if DEBUG:
    # FOLDER_TYPE_MAP = {}` が在る、など）。
    if _binds_outside_top_level_assignments(tree, MAP_NAME):
        raise _UnreadableMapValue(
            f"{MAP_NAME} がモジュール直下の代入以外でも束縛されている"
            "（if/for/try の中、def/class、import as、except as 等）。"
            "実行時にどれが効くか静的に決められない")

    if not bindings:
        return None
    if len(bindings) > 1:
        # 実行時に効くのは最後の 1 つだが、黙ってそれを選ぶと
        # 「完全な定義の後ろに不完全な再定義」を緑で通してしまう。
        raise _UnreadableMapValue(
            f"{MAP_NAME} がモジュール直下で {len(bindings)} 回定義されている。"
            "登録表の再定義は常に事故")
    node = bindings[0]
    if not isinstance(node, ast.Dict):
        raise _UnreadableMapValue(f"{MAP_NAME} が dict リテラルではない")
    return {_folder_name_of(k): _doc_type_of(v)
            for k, v in zip(node.keys, node.values)}


def functions_reading_map(source_or_tree, name=MAP_NAME):
    """モジュール直下の関数のうち、`name` を**読んで**いるものの名前集合。

    3 つの絞りが要る。どれを落としても番人が過大にカウントし、
    map を実際には使っていない関数が `MAP_CONSUMERS` を満たしてしまう:

    1. **モジュール直下の関数だけ**見る。`ast.walk` で全 `FunctionDef` を
       拾って `node.name` で数えると、入れ子関数やクラスメソッドに
       `ensure_dirs` という名前を付けるだけで満たせてしまう
    2. **`ast.Load` に限る**。`FOLDER_TYPE_MAP = ...` のような代入
       （`Store`）を「読んでいる」と数えると、map を潰す関数が通る
    3. **ローカルに束縛している関数は除く**。`def ensure_dirs():
       FOLDER_TYPE_MAP = ("receipt",); return [x for x in FOLDER_TYPE_MAP]`
       が読んでいるのは module の map ではなく自前のハードコード一覧。
       `global` 宣言が在る場合だけは module の map への代入なので除外しない
    """
    reading = set()
    tree = _as_tree(source_or_tree)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body_nodes = list(_same_scope_nodes(node.body))
        declares_global = any(
            isinstance(n, ast.Global) and name in n.names for n in body_nodes)
        if not declares_global and _count_bindings(body_nodes, name):
            continue  # 自前の同名ローカルを読んでいるだけ
        name_nodes = [n for n in body_nodes
                      if isinstance(n, ast.Name) and n.id == name]
        if any(isinstance(n.ctx, ast.Load) for n in name_nodes):
            reading.add(node.name)
    return reading


def missing_doc_types(mapping):
    """`DocType.ALL` のうち `mapping` に登録が無いもの。

    **本ファイルの主張の中身**。実テストも変異テストもこの 1 つの述語を
    通す —— 変異テストが「parser が読まなかった」だけを確かめる形だと、
    番人の判定そのものが壊れても変異が生き残る（T7 で踏んだ、
    番人の前提が成立していないと断言が空になる型と同じ）。
    """
    registered = set(mapping.values())
    return [dt for dt in DocType.ALL if dt not in registered]


class FolderTypeMapCoversAllDocTypesTest(unittest.TestCase):
    """7 枚目の登録表に漏れが無いこと。"""

    @classmethod
    def setUpClass(cls):
        with open(TARGET, encoding="utf-8") as f:
            cls.source = f.read()
        # 実ファイルの parse は 1 回。以降は解析済み tree を使い回す。
        cls.tree = ast.parse(cls.source)
        cls.mapping = parse_folder_type_map(cls.tree)

    def test_the_map_is_found_at_all(self):
        """解析器が空振りしていないこと（否定対照）。

        `None` のまま以降のテストを回すと「漏れが無い」ではなく
        「何も見ていない」で緑になる。
        """
        self.assertIsNotNone(
            self.mapping,
            f"{TARGET} に {MAP_NAME} の代入が見つからない。"
            "名前が変わったか、モジュール直下から移動した")
        self.assertGreater(len(self.mapping), 0, f"{MAP_NAME} が空")

    def test_every_doc_type_has_a_local_test_folder(self):
        """`DocType.ALL` 全件にローカル入力フォルダの入口が在ること。

        これが本ファイルの主張。漏れた doc_type は `scan_local_files` が
        黙って素通りするため、ローカルで真票を流す手段が無くなる。
        """
        missing = missing_doc_types(self.mapping)
        self.assertEqual(
            missing, [],
            f"{MAP_NAME} に未登録の doc_type がある: {missing}。"
            f"local_test.py の {MAP_NAME} へ追加すること"
            "（ensure_dirs が test_images/<名前>/ を自動生成する）")

    def test_no_unknown_doc_type_is_registered(self):
        """逆向き: 存在しない doc_type が登録されていないこと。

        `DocType` から値を消したのに `FOLDER_TYPE_MAP` に残っていると、
        そのフォルダのファイルは未知の doc_type で pipeline へ渡る。
        """
        unknown = sorted(set(self.mapping.values()) - set(DocType.ALL))
        self.assertEqual(
            unknown, [],
            f"{MAP_NAME} に DocType.ALL 外の値がある: {unknown}")

    def test_folder_name_equals_doc_type_value(self):
        """フォルダ名 ＝ doc_type の値、という既存 4 件の規約を保つ。

        規約が崩れると docstring の使い方欄・`scan_local_files` の出力・
        `test_images/` の実物の三者が食い違い、どれが正しいか判らなくなる。
        """
        mismatched = {k: v for k, v in self.mapping.items() if k != v}
        self.assertEqual(
            mismatched, {},
            f"フォルダ名と doc_type の値が食い違う: {mismatched}。"
            "既存 4 件は全て一致しており、この規約に揃えること")

    def test_map_consumers_still_read_the_map(self):
        """`ensure_dirs` / `scan_local_files` が map を読み続けていること。

        どちらかがハードコード一覧へ退化すると、map へ 1 行足しても
        フォルダが作られない／掃かれないのに、上のテストは緑のままになる。
        """
        reading = functions_reading_map(self.tree)
        not_reading = [f for f in MAP_CONSUMERS if f not in reading]
        self.assertEqual(
            not_reading, [],
            f"{MAP_NAME} を読まなくなった関数がある: {not_reading}。"
            f"map へ登録しても効かない状態になっている")


class ParserActuallyDetectsOmissionTest(unittest.TestCase):
    """番人が本当に噛むこと（変異検証を兼ねる）。

    上のテスト群は `local_test.py` が正しければ緑になる。だが解析器が
    壊れていても緑になりうる。ここでは**わざと壊した入力**を食わせて、
    赤くなることを実測する。T6 で踏んだ `UnwiredItemsTest` の substring
    事故——番人が最も必要な瞬間にすり抜けた——と同型を避けるため。
    """

    FULL = (
        "from doc_types import DocType\n"
        "FOLDER_TYPE_MAP = {\n"
        + "".join(f'    "{dt}": DocType.{dt.upper()},\n' for dt in DocType.ALL)
        + "}\n"
    )

    def test_the_fixture_itself_is_complete(self):
        """対照群が本当に全件揃っていること（対照が壊れていたら実験にならない）。"""
        self.assertEqual(missing_doc_types(parse_folder_type_map(self.FULL)), [])

    def test_a_missing_entry_is_actually_reported_as_missing(self):
        """1 件抜いたら**主張の述語が**それを名指しすること。

        全 doc_type について実測する（1 件だけ試すと、たまたまその 1 件を
        見ている実装でも通る）。`missing_doc_types` を通すのが要点 ——
        「parser が読まなかった」だけを確かめる形だと、番人の判定そのものが
        壊れた変異を見逃す。
        """
        for dropped in DocType.ALL:
            with self.subTest(dropped=dropped):
                mutated = "".join(
                    line + "\n" for line in self.FULL.splitlines()
                    if f'"{dropped}"' not in line)
                missing = missing_doc_types(parse_folder_type_map(mutated))
                self.assertEqual(
                    missing, [dropped],
                    f"{dropped} を削ったのに missing が {missing} になった")

    def test_a_differently_named_map_is_not_mistaken_for_it(self):
        """別名の dict を `FOLDER_TYPE_MAP` と取り違えないこと。"""
        decoy = self.FULL.replace(MAP_NAME, "SOME_OTHER_MAP")
        self.assertIsNone(
            parse_folder_type_map(decoy),
            f"{MAP_NAME} でない dict を拾ってしまった")

    def test_a_definition_inside_a_function_is_not_picked_up(self):
        """関数内のローカル変数を module 直下の登録表と取り違えないこと。

        取り違えると、実行時に効く map が不完全でも「完全な map を
        見つけた」として緑になる。
        """
        local_only = "def build():\n" + "".join(
            "    " + line + "\n" for line in self.FULL.splitlines()[1:])
        self.assertIsNone(
            parse_folder_type_map("from doc_types import DocType\n" + local_only),
            "関数内の代入を module 直下の定義として拾ってしまった")

    def test_a_redefinition_raises_instead_of_silently_choosing_one(self):
        """直下に 2 回定義されていたら例外にすること。

        実行時に効くのは後ろの 1 つ。前の完全な定義を読んで緑を出すと、
        **不完全な方が実際に使われている**のに番人が通す。
        """
        doubled = self.FULL + '\nFOLDER_TYPE_MAP = {"receipt": DocType.RECEIPT}\n'
        with self.assertRaises(_UnreadableMapValue):
            parse_folder_type_map(doubled)

    def test_an_annotated_assignment_is_found(self):
        """`FOLDER_TYPE_MAP: dict = {...}` も見つけること。

        見つけられないと `None` になり `test_the_map_is_found_at_all` が
        赤くなる —— 誤検知ではあるが無音の見落としではない。それでも
        正当な書き方を弾く理由が無いので受ける。
        """
        annotated = self.FULL.replace(
            f"{MAP_NAME} = ", f"{MAP_NAME}: dict = ")
        self.assertEqual(missing_doc_types(parse_folder_type_map(annotated)), [])

    def test_annotation_first_then_assignment_is_not_a_double_binding(self):
        """`X: dict` を先に書いてから代入する形を**誤って**拒否しないこと。

        型註釈だけの `AnnAssign`（value=None）は実行時に何も束縛しない。
        これを束縛として数えると、正当な書き方が「直下以外でも束縛が在る」
        と誤報されて赤くなる（Codex 評審 R3-P2 で実際に踏んだ）。
        """
        annotated_first = f"{MAP_NAME}: dict\n" + self.FULL
        self.assertEqual(
            missing_doc_types(parse_folder_type_map(annotated_first)), [])

    # 「曖昧な形は読めたと言わない」の実測表。左＝壊し方、右＝ソース。
    # どれか 1 つでも素通しすると、番人は「登録が無い」と「読めなかった」を
    # 取り違えて嘘のエラーメッセージを出す。
    UNREADABLE_SOURCES = {
        "値が関数呼出（読めない）":
            'FOLDER_TYPE_MAP = {"receipt": pick_doc_type()}\n',
        "DocType 属性の綴り間違い":
            'FOLDER_TYPE_MAP = {"receipt": DocType.RECEIPTT}\n',
        "DocType.ALL は list であって doc_type ではない":
            'FOLDER_TYPE_MAP = {"receipt": DocType.ALL}\n',
        "dict 展開（**BASE）は展開元を見落とす":
            'FOLDER_TYPE_MAP = {**BASE, "receipt": DocType.RECEIPT}\n',
        "キーが文字列リテラルでない":
            'FOLDER_TYPE_MAP = {folder_name(): DocType.RECEIPT}\n',
        "モジュール直下以外（if の中）でも束縛している":
            'FOLDER_TYPE_MAP = {"receipt": DocType.RECEIPT}\n'
            'if DEBUG:\n    FOLDER_TYPE_MAP = {}\n',
        "module 級 for でも束縛している":
            'FOLDER_TYPE_MAP = {"receipt": DocType.RECEIPT}\n'
            'for FOLDER_TYPE_MAP in candidates:\n    pass\n',
        # ここから下は `ast.Name` を作らない束縛形。ノード種別で数えないと
        # 全部すり抜ける（Codex 評審 R3-P1）。
        "同名の def が後から上書きする":
            'FOLDER_TYPE_MAP = {"receipt": DocType.RECEIPT}\n'
            'def FOLDER_TYPE_MAP():\n    pass\n',
        "同名の class が後から上書きする":
            'FOLDER_TYPE_MAP = {"receipt": DocType.RECEIPT}\n'
            'class FOLDER_TYPE_MAP:\n    pass\n',
        "import ... as が後から上書きする":
            'FOLDER_TYPE_MAP = {"receipt": DocType.RECEIPT}\n'
            'import os as FOLDER_TYPE_MAP\n',
        "except ... as が後から上書きする":
            'FOLDER_TYPE_MAP = {"receipt": DocType.RECEIPT}\n'
            'try:\n    pass\n'
            'except Exception as FOLDER_TYPE_MAP:\n    pass\n',
        "with ... as が後から上書きする":
            'FOLDER_TYPE_MAP = {"receipt": DocType.RECEIPT}\n'
            'with open("x") as FOLDER_TYPE_MAP:\n    pass\n',
    }

    def test_unreadable_shapes_raise_instead_of_being_skipped(self):
        """曖昧な形を黙って部分的に読まないこと。

        `if cond: FOLDER_TYPE_MAP = {}` の類は特に危険 —— 直下の完全な
        定義だけ読むと、実行時に効いている不完全な方を見逃して緑になる。
        path analysis はせず、在るだけで拒否する。
        """
        for label, broken in self.UNREADABLE_SOURCES.items():
            with self.subTest(case=label):
                with self.assertRaises(_UnreadableMapValue):
                    parse_folder_type_map(broken)


class ConsumerDetectionIsNotFooledTest(unittest.TestCase):
    """`functions_reading_map` が「読んでいる」を過大に数えないこと。

    過大に数えると、map を実際には使っていない関数が
    `test_map_consumers_still_read_the_map` を通ってしまう。
    """

    def test_a_plain_read_counts(self):
        """素直な `for x in MAP` は当然カウントされること（肯定対照）。"""
        src = "def ensure_dirs():\n    for n in FOLDER_TYPE_MAP:\n        pass\n"
        self.assertIn("ensure_dirs", functions_reading_map(src))

    def test_a_comprehension_read_counts(self):
        """内包表記へ書き換えても読んでいると数えること（正当なリファクタ）。"""
        src = "def ensure_dirs():\n    return [n for n in FOLDER_TYPE_MAP]\n"
        self.assertIn("ensure_dirs", functions_reading_map(src))

    def test_a_write_only_reference_does_not_count(self):
        """代入（`Store`）は「読んでいる」ではないこと。

        map を潰すだけの関数が番人を通ってはいけない。
        """
        src = "def ensure_dirs():\n    global FOLDER_TYPE_MAP\n    FOLDER_TYPE_MAP = {}\n"
        self.assertNotIn("ensure_dirs", functions_reading_map(src))

    # 入れ子スコープ 4 種。どれも「外側の関数が map を読んでいる」証拠には
    # ならない（呼ばれない内部関数／メソッドかもしれない）。
    NESTED_SCOPE_SOURCES = {
        "入れ子の def": ("def ensure_dirs():\n"
                         "    def unused():\n"
                         "        return FOLDER_TYPE_MAP\n"
                         "    return None\n"),
        "入れ子の async def": ("def ensure_dirs():\n"
                               "    async def unused():\n"
                               "        return FOLDER_TYPE_MAP\n"
                               "    return None\n"),
        "入れ子の class": ("def ensure_dirs():\n"
                           "    class Holder:\n"
                           "        m = FOLDER_TYPE_MAP\n"
                           "    return None\n"),
        "lambda": ("def ensure_dirs():\n"
                   "    f = lambda: FOLDER_TYPE_MAP\n"
                   "    return None\n"),
    }

    def test_a_read_inside_a_nested_scope_does_not_count(self):
        """入れ子スコープの中の参照は外側の証拠にならないこと。

        4 例とも関数名は `ensure_dirs` で固定し、**読みの位置だけ**を
        入れ子へ動かしている。`test_a_plain_read_counts` が同じ名前・
        同じ読みを本体に置いて「数える」ことを固定しているので、
        両者の差分がそのまま「入れ子だから除いた」の証拠になる
        —— 全部「読んでいない」に倒す実装は肯定側で落ちる。
        """
        for label, src in self.NESTED_SCOPE_SOURCES.items():
            with self.subTest(case=label):
                self.assertNotIn("ensure_dirs", functions_reading_map(src))

    def test_a_nested_function_cannot_impersonate_a_top_level_consumer(self):
        """入れ子関数に `ensure_dirs` と名付けても満たせないこと。

        名前だけで数えると、本物のモジュール直下 `ensure_dirs` が map を
        読まなくなっても、どこかの入れ子に同名の関数が在れば緑になる。
        """
        src = ("def ensure_dirs():\n"
               "    return ['receipt']\n"
               "def wrapper():\n"
               "    def ensure_dirs():\n"
               "        return FOLDER_TYPE_MAP\n"
               "    return None\n")
        self.assertNotIn("ensure_dirs", functions_reading_map(src))

    def test_a_locally_shadowed_name_does_not_count(self):
        """自前のローカル一覧を読んでいるだけの関数を数えないこと。

        `FOLDER_TYPE_MAP = ("receipt",)` とハードコードしてから読む形は、
        module の登録表を読んでいない。これを数えると、map へ登録しても
        効かない退化を番人が通してしまう。
        """
        src = ("def ensure_dirs():\n"
               "    FOLDER_TYPE_MAP = ('receipt',)\n"
               "    return [n for n in FOLDER_TYPE_MAP]\n")
        self.assertNotIn("ensure_dirs", functions_reading_map(src))

    def test_a_global_declaration_is_not_treated_as_shadowing(self):
        """`global` 宣言付きの代入は module の map への代入なので除外しない。"""
        src = ("def ensure_dirs():\n"
               "    global FOLDER_TYPE_MAP\n"
               "    FOLDER_TYPE_MAP = dict(FOLDER_TYPE_MAP)\n"
               "    return None\n")
        self.assertIn("ensure_dirs", functions_reading_map(src))


class WalrusInAssignedValueTest(unittest.TestCase):
    """直下代入の**右辺**に隠れた束縛も見ること（Codex 評審 R4-P2）。

    `_binds_outside_top_level_assignments` を直接呼ぶ。
    `parse_folder_type_map` 経由だと、鍵・値の厳格検査が先に別の理由で
    例外を投げてしまい、右辺走査が効いているのか判別できない。
    """

    def test_a_clean_assignment_reports_no_extra_binding(self):
        """肯定対照: 素直な代入は「他に束縛は無い」と答えること。"""
        tree = ast.parse('FOLDER_TYPE_MAP = {"receipt": DocType.RECEIPT}\n')
        self.assertFalse(_binds_outside_top_level_assignments(tree, MAP_NAME))

    def test_a_walrus_in_the_assigned_value_is_detected(self):
        """`X = {k: (X := v)}` の右辺束縛を見落とさないこと。"""
        tree = ast.parse(
            'FOLDER_TYPE_MAP = {"receipt": (FOLDER_TYPE_MAP := DocType.RECEIPT)}\n')
        self.assertTrue(_binds_outside_top_level_assignments(tree, MAP_NAME))


class NoBarePy310AstReferenceTest(unittest.TestCase):
    """本ファイルが Python 3.9 でも動き続けること（環境依存の破壊を禁じる）。

    実際に踏んだ事故: `_binds_name` に `ast.MatchAs` を直書きしたところ、
    venv311（3.11）では 20 tests 全緑のまま、mac の系統 python3（3.9.6）で
    `AttributeError` により 26 errors で全滅した。**実行環境が 1 つだと
    環境依存の破壊は無症状**で、テストの緑は「壊していない」の証明に
    ならない。だから構造で禁じる。

    冒頭 docstring の「venv 無しでも動く」は、この番人が支えている。
    3.10+ 専用ノードを使うときは `getattr(ast, "X", None)` 経由にすること。
    """

    def test_no_python_310_only_ast_node_is_referenced_directly(self):
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            own_source = f.read()
        bare = sorted({
            node.attr
            for node in ast.walk(ast.parse(own_source))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "ast"
            and node.attr in _PY310_ONLY_AST_NODES})
        self.assertEqual(
            bare, [],
            f"Python 3.10+ にしか無い ast ノードを直書きしている: {bare}。"
            "3.9 では呼出時に AttributeError で落ちる。"
            'getattr(ast, "X", None) 経由にすること')

    def test_the_watchdog_can_actually_see_a_bare_reference(self):
        """番人が本当に噛むこと（一覧を空にしただけで緑になる番人を作らない）。"""
        offending = "x = ast.MatchAs\n"
        bare = sorted({
            node.attr
            for node in ast.walk(ast.parse(offending))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "ast"
            and node.attr in _PY310_ONLY_AST_NODES})
        self.assertEqual(bare, ["MatchAs"])


if __name__ == "__main__":
    unittest.main()
