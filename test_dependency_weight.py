"""純関数モジュールの「venv 非依存」を機械的に見張る。

母 Plan（`docs/plans/2026-08-12-credit-card-doctype.md` §4）は
`page_family` / `page_dedup` / `card_reconciliation` / `invoice_classification`
の 4 モジュールについて **gspread / paddleocr / google api に非依存 ＝
venv 無しで単体テスト可能** と定めている。

**なぜ機械で見張る必要があるか**: この性質は壊れても**全量テストが緑のまま**に
なる —— CI も手元も venv311 で走らせるので、重依存が紛れ込んでも誰も気づかない。
実際 2026-08-17（T3 Round 1）に、テスト標本を `ocr_test_helpers`（`ocr_engine`
経由で `google.generativeai` を引く）へ置いたせいで `test_page_family` が
`python3` 単体で `ModuleNotFoundError: No module named 'google'` になった。
全 701 テストは緑のままだった。人間のレビューでも 4 エージェントのレビューでも
一次では気づかず、脱 venv 実行を試して初めて露見した。

本テストは import 文を静的に辿るだけなので、それ自体が重依存を引かない。

    venv311/bin/python -m unittest test_dependency_weight -v
    python3 -m unittest test_dependency_weight -v   # これも通ること
"""
import ast
import os
import unittest


_HERE = os.path.dirname(os.path.abspath(__file__))

# venv311 でしか入らない依存。これらを（間接的にでも）引いたら違反。
_HEAVY = frozenset({
    "google", "paddleocr", "gspread", "pdf2image", "PIL", "numpy",
    "pypdf", "dotenv", "googleapiclient", "google_auth_oauthlib",
})

# venv 無しで実行できなければならないテストと、その被験モジュール。
_MUST_STAY_LIGHT = (
    "test_page_family",
    "test_page_dedup",
    "test_card_reconciliation",
    "test_invoice_classification",
    "test_card_entries",
    "test_card_prompts",
    "page_family",
    "page_dedup",
    "card_reconciliation",
    "invoice_classification",
    # T4 の 2 本。`ocr_engine` は登録するだけで、実体はこちらに在る
    # （母 Plan §4「ocr_engine にこれ以上積まない」）
    "card_entries",
    "card_prompts",
)


def _local_module_path(name):
    """リポジトリ直下の `<name>.py` を返す（無ければ None＝外部モジュール）。"""
    path = os.path.join(_HERE, name + ".py")
    return path if os.path.isfile(path) else None


def _imports_of(path):
    """**そのモジュールを import した時点で実行される** import 名の集合。

    関数本体・クラス本体の中の import は**数えない**。守りたい性質は
    「`import page_family` が重依存無しで通ること」であって、
    「重依存に一切触れないこと」ではないため。

    実例（この区別が要る理由）: `test_page_family` は
    `def test_exclude_destination_constants_match` の中で
    `try: import ocr_engine / except ImportError: skip` として
    定数の突合を行っている。venv があるときだけ走る意図的な設計で、
    venv 無しでは skip されるだけ。ここを違反と見なすと、番人が
    正しい設計を否定してしまう。

    一方、トップレベルの `try:` / `if:` / `with:` の中の import は
    module import 時に実行されるので**数える**。
    """
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    names = set()

    def visit(body):
        for node in body:
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                # `from . import x` 等の相対 import は本リポジトリでは未使用
                if node.level == 0 and node.module:
                    names.add(node.module.split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                continue  # 呼ばれるまで実行されない。ここで打ち切る
            else:
                # トップレベルの try / if / with / for 等は import 時に走る
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(node, field, []) or [])
                for handler in getattr(node, "handlers", []) or []:
                    visit(handler.body)

    visit(tree.body)
    return names


def _transitive_imports(root_module):
    """ローカルモジュールを辿って到達する全 import 名と、その経路を返す。

    Returns:
        dict: {import 名: そこへ至る経路（モジュール名のリスト）}
    """
    reached = {}
    # (モジュール名, そこまでの経路)
    stack = [(root_module, [root_module])]
    visited = set()

    while stack:
        module, trail = stack.pop()
        if module in visited:
            continue
        visited.add(module)

        path = _local_module_path(module)
        if path is None:
            continue  # 外部モジュール。ここから先は辿らない

        for name in _imports_of(path):
            if name not in reached:
                reached[name] = trail + [name]
            if _local_module_path(name) is not None:
                stack.append((name, trail + [name]))
    return reached


class PureModulesStayVenvIndependentTest(unittest.TestCase):
    """4 つの純関数モジュールとそのテストが重依存を引かないこと。"""

    def test_no_heavy_dependency_is_reachable(self):
        for module in _MUST_STAY_LIGHT:
            with self.subTest(module=module):
                self.assertIsNotNone(
                    _local_module_path(module),
                    "%s.py が見つからない（改名または削除された？）" % module)

                reached = _transitive_imports(module)
                violations = {
                    name: trail for name, trail in reached.items()
                    if name in _HEAVY
                }
                self.assertEqual(
                    violations, {},
                    "%s が重依存に到達している。venv 無しで実行できなくなり、"
                    "しかも全量テストは緑のままになる。経路: %s"
                    % (module, ["→".join(t) for t in violations.values()]))

    def test_the_guard_itself_would_catch_a_violation(self):
        # Arrange: 番人が本当に噛むことを確かめる。`ocr_engine` は重依存の塊で、
        # `ocr_test_helpers` はそれを引く —— 検出できなければ番人が壊れている。
        reached = _transitive_imports("ocr_test_helpers")

        # Assert
        self.assertTrue(
            _HEAVY & set(reached),
            "番人が重依存を検出できていない（_HEAVY の綴りが実態とずれた？）")


if __name__ == "__main__":
    unittest.main()
