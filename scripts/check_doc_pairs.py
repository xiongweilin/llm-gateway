#!/usr/bin/env python3
"""Fail when a public Markdown document lacks its language pair."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "build",
    ".local",
    "assets",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
}
EXCLUDED_PREFIXES = (".github/", "skills/")
EXEMPT_NAMES = {
    "LICENSE",
    "LICENSE.md",
    "CODE_OF_CONDUCT.md",
    "CODE_OF_CONDUCT.zh-CN.md",
    "CODE_OF_CONDUCT.en.md",
    "AGENTS.md",
    "AGENTS.zh-CN.md",
    "AGENTS.en.md",
}


def markdown_files():
    """返回需要成对检查的 Markdown 文件。"""
    for path in ROOT.rglob("*.md"):
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDED_PARTS for part in rel.parts):
            continue
        if rel.as_posix().startswith(EXCLUDED_PREFIXES):
            continue
        if rel.name in EXEMPT_NAMES:
            continue
        yield rel


def pair_candidates(rel):
    """根据现有命名约定返回可能的配对文件。"""
    name = rel.name
    if name.endswith(".zh-CN.md"):
        return [rel.with_name(name[: -len(".zh-CN.md")] + ".md")]
    if name.endswith(".en.md"):
        return [rel.with_name(name[: -len(".en.md")] + ".md")]
    stem = name[:-3]
    return [rel.with_name(stem + ".zh-CN.md"), rel.with_name(stem + ".en.md")]


def main() -> int:
    files = {path.as_posix() for path in markdown_files()}
    errors = []
    for rel in sorted(markdown_files()):
        candidates = pair_candidates(rel)
        if not any(candidate.as_posix() in files for candidate in candidates):
            names = ", ".join(candidate.as_posix() for candidate in candidates)
            errors.append(f"{rel.as_posix()} lacks one of: {names}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"checked {len(files)} Markdown files: all public documents have language pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
