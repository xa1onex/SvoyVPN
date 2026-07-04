#!/usr/bin/env python3
"""InlineKeyboardButton(text=f\"{E.key} Label\") -> btn(\"Label\", \"key\")."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "bot"

PATTERNS = [
    (
        re.compile(
            r'InlineKeyboardButton\(\s*text=f"\{E\.(\w+)\}\s([^"]+)"'
        ),
        r'btn("\2", "\1"',
    ),
    (
        re.compile(
            r"InlineKeyboardButton\(\s*text=f'\{E\.(\w+)\}\s([^']+)'"
        ),
        r"btn('\2', '\1', ",
    ),
    (
        re.compile(
            r'InlineKeyboardButton\(\s*text=f"([^"]*)\{E\.(\w+)\}"'
        ),
        r'btn("\1", "\2", ',
    ),
    (
        re.compile(
            r"InlineKeyboardButton\(\s*text=f'([^']*)\{E\.(\w+)\}'"
        ),
        r"btn('\1', '\2', ",
    ),
    (
        re.compile(
            r'InlineKeyboardButton\(\s*text=f"\{E\.(\w+)\}"'
        ),
        r'btn("", "\1", ',
    ),
]


def fix_file(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    original = text
    for pattern, repl in PATTERNS:
        text = pattern.sub(repl, text)
    if text != original:
        path.write_text(text, encoding="utf-8")
        return True
    return False


def main() -> None:
    n = 0
    for path in ROOT.rglob("*.py"):
        if path.name == "custom_emojis.py":
            continue
        if "custom_emojis import" not in path.read_text(encoding="utf-8"):
            continue
        if fix_file(path):
            n += 1
            print(path.relative_to(ROOT.parent))
    print(f"Fixed buttons in {n} files")


if __name__ == "__main__":
    main()
