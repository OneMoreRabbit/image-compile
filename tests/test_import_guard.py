"""Constitution principle 2: image_compile imports no other compiler.

One test, source-read with `ast` (never by importing the modules), per the
estate's compiler-import-guard need: every Python file under src/image_compile
is parsed, and the test fails if any import names another compiler's package.
Each compiler carries its own copy of this check — sharing it would itself be
an import between compilers.
"""
from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN = {"rbac_compiler", "sync_compiler", "agent_compile"}

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "image_compile"


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and node.level == 0:
                roots.add(node.module.split(".")[0])
    return roots


def test_no_other_compiler_is_imported() -> None:
    assert PACKAGE_ROOT.is_dir(), f"package root missing: {PACKAGE_ROOT}"
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    # A guard that read nothing proves nothing: a moved src/ must fail here,
    # not pass vacuously (need point 5).
    assert files, f"no Python files found under {PACKAGE_ROOT} — nothing was checked"
    offences: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = _imported_roots(tree) & FORBIDDEN
        for name in sorted(hits):
            offences.append(f"{path.relative_to(PACKAGE_ROOT.parent)}: imports {name}")
    assert not offences, (
        "compiler imports another compiler (constitution principle 2):\n  "
        + "\n  ".join(offences)
    )
