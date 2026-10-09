#!/usr/bin/env python3
"""Static safety layer: which repo files does each test file import (transitively)?

Covers what per-test coverage cannot: module-level code (constants, class bodies,
decorators) runs once at import time and is never credited to individual tests.

Also classifies a change as body-only or module-level, so selection can use the
cheap runtime map for body-only edits and the import closure otherwise.

Usage:
  python3 static_imports.py <repo_checkout> --out phase1/static.json
"""
import argparse
import ast
import json
import os
import sys


def module_files(repo):
    """dotted module name -> repo-relative path, for homeassistant/ and tests/."""
    mods = {}
    for top in ("homeassistant", "tests"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(repo, top)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), repo)
                name = rel[:-3].replace(os.sep, ".")
                if name.endswith(".__init__"):
                    name = name[: -len(".__init__")]
                mods[name] = rel
    return mods


def imports_of(path, rel, mods):
    """Repo files imported directly by one file (parents' __init__ included)."""
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=rel)
    except (SyntaxError, UnicodeDecodeError):
        return set()
    pkg = rel[:-3].replace(os.sep, ".")
    is_init = pkg.endswith(".__init__")
    pkg = pkg[: -len(".__init__")] if is_init else pkg.rsplit(".", 1)[0]
    found = set()

    def add(name):
        # importing a.b.c executes a, a.b and a.b.c
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            m = ".".join(parts[:i])
            if m in mods:
                found.add(mods[m])

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".")
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            add(mod)
            for a in node.names:  # `from pkg import submodule`
                add(mod + "." + a.name)
    found.discard(rel)
    return found


def conftests_for(test_rel, mods_by_path):
    parts = test_rel.split("/")[:-1]
    out = set()
    for i in range(1, len(parts) + 1):
        c = "/".join(parts[:i] + ["conftest.py"])
        if c in mods_by_path:
            out.add(c)
    return out


def closure(start, graph):
    seen, stack = set(), list(start)
    while stack:
        f = stack.pop()
        if f in seen:
            continue
        seen.add(f)
        stack.extend(graph.get(f, ()))
    return seen


def body_only_change(old_src, new_src):
    """True if every changed line sits inside a function body (not signature/decorators)."""
    import difflib

    def body_lines(src):
        lines = set()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return None
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
                lines.update(range(n.body[0].lineno, n.end_lineno + 1))
        return lines

    old_b, new_b = body_lines(old_src), body_lines(new_src)
    if old_b is None or new_b is None:
        return False
    sm = difflib.SequenceMatcher(a=old_src.splitlines(), b=new_src.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old_changed = set(range(i1 + 1, i2 + 1))
        new_changed = set(range(j1 + 1, j2 + 1))
        if not old_changed <= old_b or not new_changed <= new_b:
            return False
        if not old_changed and not new_changed:
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--out", default="phase1/static.json")
    args = ap.parse_args()

    mods = module_files(args.repo)
    by_path = set(mods.values())
    graph = {rel: imports_of(os.path.join(args.repo, rel), rel, mods) for rel in by_path}
    test_files = sorted(p for p in by_path if p.startswith("tests/") and os.path.basename(p).startswith("test_"))
    result = {}
    for t in test_files:
        start = graph[t] | conftests_for(t, by_path)
        result[t] = sorted(closure(start, graph) - {t})
    sizes = sorted(len(v) for v in result.values())
    ha_files = sum(1 for p in by_path if p.startswith("homeassistant/"))
    print("modules: %d (homeassistant/: %d), test files: %d" % (len(by_path), ha_files, len(test_files)), file=sys.stderr)
    if sizes:
        print("import closure per test file: median %d, p90 %d, max %d files"
              % (sizes[len(sizes) // 2], sizes[int(len(sizes) * 0.9)], sizes[-1]), file=sys.stderr)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(result, f)


if __name__ == "__main__":
    main()
