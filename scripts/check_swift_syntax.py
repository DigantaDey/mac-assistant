#!/usr/bin/env python3
"""A fast Swift *syntax* gate that runs anywhere (including Linux CI).

Type-checking Swift needs a Mac, but most of the mistakes that make a build
fail outright — an unbalanced brace, a stray paste, a broken string — are
syntax errors, and those can be caught in a second on any machine with
tree-sitter's Swift grammar:

    pip install tree-sitter tree-sitter-swift
    python scripts/check_swift_syntax.py

Exits non-zero and prints `file:line:col` for every ERROR node found.
If the grammar isn't installed the check reports "skipped" and succeeds, so it
can never block a contributor who hasn't installed the extras.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SWIFT_GLOBS = ("macos/Sources", "macos/Tests")


def swift_files() -> list[Path]:
    files: list[Path] = []
    for pattern in SWIFT_GLOBS:
        files.extend(sorted((ROOT / pattern).rglob("*.swift")))
    return files


def main() -> int:
    try:
        import tree_sitter_swift
        from tree_sitter import Language, Parser
    except Exception as exc:  # pragma: no cover - depends on the environment
        print(f"swift syntax check skipped — {exc.__class__.__name__}: {exc}")
        print("(install it with: pip install tree-sitter tree-sitter-swift)")
        return 0

    parser = Parser(Language(tree_sitter_swift.language()))
    files = swift_files()
    if not files:
        print("no Swift sources found")
        return 1

    failures = 0
    for path in files:
        source = path.read_bytes()
        tree = parser.parse(source)
        if not tree.root_node.has_error:
            continue
        failures += 1
        relative = path.relative_to(ROOT)
        lines = source.split(b"\n")
        for node in walk(tree.root_node):
            if node.type != "ERROR" and not node.is_missing:
                continue
            row, column = node.start_point
            snippet = lines[row][:120].decode("utf-8", "replace") if row < len(lines) else ""
            print(f"{relative}:{row + 1}:{column + 1}: error: {node.type} — {snippet.strip()}")
    if failures:
        print(f"\n{failures} file(s) with syntax errors")
        return 1
    print(f"swift syntax ok ({len(files)} files)")
    return 0


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


if __name__ == "__main__":
    raise SystemExit(main())
