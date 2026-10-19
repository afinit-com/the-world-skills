"""Every text read and write in the kit names its encoding.

Without one, Python uses the locale's code page, which on a Korean Windows install is
cp949: WORLD.md, a persona's voice, or a finding with an emoji then fails to load or
comes back as mojibake, and a guard hook reading the tool call from stdin dies, which
the host treats as "allow". macOS and Linux default to UTF-8, so no other test notices.
This one reads the source instead of waiting for a Windows run.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
SOURCES = [KIT / "world", KIT / "bin", KIT / ".claude" / "hooks", KIT / ".codex" / "hooks",
           KIT.parents[1] / "scripts"]


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_true(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _mode(call: ast.Call, position: int) -> str | None:
    node = _kw(call, "mode") or (call.args[position] if len(call.args) > position else None)
    if node is None:
        return "r"
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def problems(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if not isinstance(node, ast.Call) or _kw(node, "encoding") is not None:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in ("read_text", "write_text"):
            bad = True
        elif name == "open" or name == "fdopen":
            # builtins.open(path, mode) / os.fdopen(fd, mode) vs Path.open(mode)
            position = 0 if isinstance(func, ast.Attribute) and name == "open" and not (
                isinstance(func.value, ast.Name) and func.value.id in ("os", "io")) else 1
            mode = _mode(node, position)
            bad = mode is not None and "b" not in mode
        elif name in ("run", "Popen", "check_output"):
            bad = _is_true(_kw(node, "text")) or _is_true(_kw(node, "universal_newlines"))
        else:
            bad = False
        if bad:
            found.append(f"{path.relative_to(KIT.parents[1])}:{node.lineno} {name}()")
    return found


class TextEncodingTests(unittest.TestCase):
    def test_no_text_io_relies_on_the_locale_encoding(self) -> None:
        files = [p for root in SOURCES for p in sorted(root.rglob("*.py"))]
        self.assertGreater(len(files), 30)
        found = [line for path in files for line in problems(path)]
        self.assertEqual(found, [], "add encoding=\"utf-8\" to:\n" + "\n".join(found))

    def test_guard_hooks_read_stdin_as_bytes(self) -> None:
        for path in [*(KIT / ".claude" / "hooks").glob("*.py"), KIT / ".codex" / "hooks" / "world-guard.py"]:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("json.load(sys.stdin)", text, path.name)


if __name__ == "__main__":
    unittest.main()
