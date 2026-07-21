#!/usr/bin/env python3
"""Fix broken imports and f-string quote issues after emoji migration."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "bot"

IMPORT_PKG = "from .custom_emojis import e, lbl, btn, emoji_button, raw\n"
IMPORT_REL = "from ..custom_emojis import e, lbl, btn, emoji_button, raw\n"

IMPORT_RE = re.compile(
    r"^from \.\.?custom_emojis import e, lbl, btn, emoji_button, raw\s*\n",
    re.MULTILINE,
)


def fix_file(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    original = text

    text = IMPORT_RE.sub("", text)

    rel = path.relative_to(ROOT)
    import_line = IMPORT_REL if str(rel).startswith("handlers/") else IMPORT_PKG

    if "custom_emojis import" not in text:
        lines = text.splitlines(keepends=True)
        insert = 0
        in_doc = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if i == 0 and (stripped.startswith('"""') or stripped.startswith("'''")):
                in_doc = True
            if in_doc:
                if stripped.endswith('"""') or stripped.endswith("'''"):
                    in_doc = False
                continue
            if stripped.startswith("from __future__"):
                insert = i + 1
                continue
            if stripped.startswith("import ") or stripped.startswith("from "):
                insert = i + 1
        lines.insert(insert, import_line)
        text = "".join(lines)

    text = text.replace("{e('", '{e("').replace("')}", '")}')
    text = re.sub(r"\be\('([^']+)'\)", r'e("\1")', text)
    text = re.sub(r'btn\("([^"]*)", \'([^\']+)\',', r'btn("\1", "\2",', text)
    text = re.sub(r'btn\(\'([^\']*)\', "([^"]+)",', r'btn("\1", "\2",', text)

    if text != original:
        path.write_text(text, encoding="utf-8")
        return True
    return False


def main() -> None:
    n = 0
    for path in ROOT.rglob("*.py"):
        if path.name == "custom_emojis.py":
            continue
        if fix_file(path):
            n += 1
    print(f"Fixed {n} files")


if __name__ == "__main__":
    main()
