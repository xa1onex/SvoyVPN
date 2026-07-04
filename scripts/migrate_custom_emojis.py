#!/usr/bin/env python3
"""Миграция Unicode-эмодзи -> bot.custom_emojis (Telegram UI)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "bot"

TARGET_FILES = None  # None = all except skip

SKIP_FILES = {
    "custom_emojis.py",
    "profile_generator.py",
    "traffic.py",
    "happ_catalog.py",
    "happ_subscription.py",
    "happ_text_notice.py",
    "free_tier_servers.py",
    "remnawave_client.py",
    "webhook_server.py",
    "migration.py",
    "main.py",
}

SKIP_DIRS = {"tests", "__pycache__"}

CHAR_TO_ATTR: list[tuple[str, str]] = sorted(
    [
        ("‼️", "alert_double"),
        ("⚠️", "warning"),
        ("⏸️", "pause_vs"),
        ("◀️", "back"),
        ("▶️", "forward"),
        ("⏪", "main_menu"),
        ("⏫", "limits"),
        ("⏭️", "skip"),
        ("⏳", "wait"),
        ("⏰", "clock"),
        ("⏱", "timer"),
        ("⏻", "power"),
        ("✅", "success"),
        ("❌", "error"),
        ("➕", "add"),
        ("➖", "remove"),
        ("⬆️", "up"),
        ("⭐️", "star"),
        ("⭐", "star"),
        ("🆓", "free"),
        ("🆘", "help"),
        ("🇪🇺", "eu"),
        ("🇵🇱", "pl"),
        ("🔗", "vpn_connect"),
        ("🚀", "subscription"),
        ("💎", "plus"),
        ("🎁", "gift"),
        ("📱", "devices"),
        ("🔐", "admin"),
        ("👋", "wave"),
        ("💳", "card"),
        ("🪙", "coin"),
        ("💰", "money"),
        ("🔥", "hot"),
        ("🌟", "star_plus"),
        ("😢", "sad"),
        ("🎉", "party"),
        ("🔓", "bypass"),
        ("📶", "signal"),
        ("📊", "chart"),
        ("🌐", "globe"),
        ("🔄", "refresh"),
        ("ℹ️", "info"),
        ("🛈", "info_link"),
        ("📥", "download"),
        ("🖥️", "servers"),
        ("🖥", "desktop"),
        ("💻", "laptop"),
        ("🤖", "android"),
        ("🐧", "linux"),
        ("🪟", "windows"),
        ("🗑️", "trash_vs"),
        ("🗑", "trash"),
        ("📤", "invite"),
        ("📑", "copy"),
        ("📋", "clipboard"),
        ("👤", "user"),
        ("👥", "users"),
        ("🤝", "handshake"),
        ("💡", "bulb"),
        ("🤔", "think"),
        ("📨", "envelope_msg"),
        ("📢", "megaphone"),
        ("🛟", "support"),
        ("🏠", "home"),
        ("🔙", "bottom"),
        ("🔝", "top"),
        ("👇", "point_down"),
        ("👉", "point_right"),
        ("🚫", "blocked"),
        ("⛔", "stop"),
        ("⏸", "pause"),
        ("🟢", "on"),
        ("🔴", "off"),
        ("⚡", "activate"),
        ("⚡️", "activate"),
        ("📄", "doc"),
        ("📌", "pin"),
        ("✏️", "edit"),
        ("📅", "calendar"),
        ("⚙️", "gear"),
        ("🔘", "radio"),
        ("📈", "trend_up"),
        ("📉", "trend_down"),
        ("📝", "note"),
        ("📡", "antenna"),
        ("📦", "package"),
        ("📭", "empty"),
        ("📸", "camera"),
        ("🔍", "search"),
        ("🧪", "test"),
        ("🌍", "world"),
        ("🌊", "wave_water"),
        ("🔌", "plug"),
        ("🔑", "key"),
        ("🔢", "numbers"),
        ("⌨️", "keyboard"),
        ("🔔", "bell"),
        ("🕒", "time"),
        ("🙈", "see_no"),
        ("🔧", "wrench"),
        ("📧", "email"),
        ("🙂", "smile"),
        ("🖼️", "photo_vs"),
        ("🖼", "photo"),
        ("🎥", "video"),
        ("🎬", "gif"),
        ("📎", "attach"),
        ("📇", "contact"),
        ("💬", "chat"),
        ("🏷️", "tag_vs"),
        ("🏷", "tag"),
        ("🧭", "compass"),
        ("💫", "sparkle"),
    ],
    key=lambda x: len(x[0]),
    reverse=True,
)

IMPORT_PKG = "from .custom_emojis import E, e, lbl, btn, emoji_button, raw\n"
IMPORT_REL = "from ..custom_emojis import E, e, lbl, btn, emoji_button, raw\n"

BTN_RE = re.compile(
    r"InlineKeyboardButton\(\s*text=(['\"])(.*?)\1\s*,",
    re.DOTALL,
)


def _import_line(path: Path) -> str:
    rel = path.relative_to(ROOT)
    if str(rel).startswith("handlers/"):
        return IMPORT_REL
    return IMPORT_PKG


def _module_import_end(lines: list[str]) -> int:
    i = 0
    n = len(lines)
    if n and lines[0].lstrip().startswith(('"""', "'''")):
        quote = '"""' if '"""' in lines[0] else "'''"
        if lines[0].count(quote) < 2:
            i = 1
            while i < n and quote not in lines[i]:
                i += 1
        i += 1
    while i < n and not lines[i].strip():
        i += 1
    if i < n and lines[i].startswith("from __future__"):
        i += 1
        while i < n and not lines[i].strip():
            i += 1
    last = max(i - 1, 0)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("import ") or line.startswith("from "):
            last = i
            i += 1
            while i < n:
                cont = lines[i]
                cs = cont.strip()
                if not cs:
                    i += 1
                    continue
                if cont[0] in " \t":
                    last = i
                    i += 1
                    continue
                if cs.startswith(")"):
                    last = i
                    i += 1
                    break
                break
            continue
        break
    return last


def _inject_import(content: str, import_line: str) -> str:
    if "custom_emojis import" in content:
        content = re.sub(
            r"^from \.\.?custom_emojis import .+\n",
            "",
            content,
            flags=re.MULTILINE,
        )
    lines = content.splitlines(keepends=True)
    pos = _module_import_end(lines) + 1
    lines.insert(pos, import_line)
    return "".join(lines)


def _contains_emoji(text: str) -> bool:
    return any(ch in text for ch, _ in CHAR_TO_ATTR)


def _replace_emojis(text: str) -> tuple[str, bool]:
    if not _contains_emoji(text):
        return text, False
    out = text
    changed = False
    for ch, attr in CHAR_TO_ATTR:
        if ch not in out:
            continue
        token = "{E." + attr + "}"
        if token in out:
            continue
        out = out.replace(ch, token)
        changed = True
    return out, changed


def _ensure_f_prefix(s: str) -> str:
    s = s.strip()
    if s.startswith('f"') or s.startswith("f'"):
        return s
    if s.startswith('"') and "{E." in s:
        return "f" + s
    if s.startswith("'") and "{E." in s:
        return "f" + s
    return s


def _migrate_strings(content: str) -> tuple[str, bool]:
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        quote = m.group(1)
        body = m.group(2)
        new_body, ch = _replace_emojis(body)
        if not ch:
            return m.group(0)
        changed = True
        new_s = f"{quote}{new_body}{quote}"
        return _ensure_f_prefix(new_s)

    # only touch f-strings and plain strings that contain emoji
    content = re.sub(r'f?(["\'])((?:\\.|(?!\1).)*)\1', repl, content)
    return content, changed


def _strip_leading_emoji(text: str) -> tuple[str | None, str]:
    for ch, attr in CHAR_TO_ATTR:
        if text.startswith(ch):
            return attr, text[len(ch) :].lstrip()
    return None, text


def _migrate_buttons(content: str) -> tuple[str, bool]:
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        text = m.group(2)
        attr, rest = _strip_leading_emoji(text)
        if not attr:
            return m.group(0)
        changed = True
        label = rest if rest else text
        return f'btn("{label}", "{attr}", '

    return BTN_RE.sub(repl, content), changed


def migrate_file(path: Path) -> bool:
    original = path.read_text(encoding="utf-8")
    content = original
    content, c1 = _migrate_strings(content)
    content, c2 = _migrate_buttons(content)
    if not (c1 or c2):
        return False
    content = _inject_import(content, _import_line(path))
    if content != original:
        path.write_text(content, encoding="utf-8")
        return True
    return False


def main() -> None:
    updated = []
    for path in sorted(ROOT.rglob("*.py")):
        if any(p in SKIP_DIRS for p in path.parts):
            continue
        if path.name in SKIP_FILES:
            continue
        if migrate_file(path):
            updated.append(str(path.relative_to(ROOT.parent)))
    print(f"Updated {len(updated)} files")
    for p in updated:
        print(" ", p)


if __name__ == "__main__":
    main()
