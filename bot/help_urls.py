"""
Публичные ссылки на справочные статьи (Telegraph и т.п.).
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

# После публикации статей на Telegraph — в .env:
# BYPASS_HELP_TELEGRAPH_URL=https://telegra.ph/ваш-слаг
# EARN_HELP_TELEGRAPH_URL=https://telegra.ph/ваш-слаг


def _telegraph_url(env_key: str) -> str:
    """Читает URL после load_dotenv (модуль импортируется до load_config в main)."""
    load_dotenv()
    return os.getenv(env_key, "").strip()


def bypass_help_link_html() -> str:
    """«(см. 🛈)» в конце строки bypass — иконка ведёт на статью."""
    url = _telegraph_url("BYPASS_HELP_TELEGRAPH_URL")
    if url:
        return f' (<a href="{url}">см. 🛈</a>)'
    return ""


def earn_help_link_html() -> str:
    """«(см. 🛈)» рядом с заголовком «Подарок» — ссылка на правила."""
    url = _telegraph_url("EARN_HELP_TELEGRAPH_URL")
    if url:
        return f' (<a href="{url}">см. 🛈</a>)'
    return ""


def format_bypass_status_line(
    used: float,
    limit: float,
    *,
    bonus: int = 0,
    exceeded: bool = False,
    prefix: str = "\n",
) -> str:
    """Строка «Bypass(Лимит): used / limit ГБ (+N ГБ пакет) (см. 🛈)»."""
    line = f"{prefix}<b>Bypass(Лимит)</b>: {used:.1f} / {limit:.0f} ГБ"
    if exceeded:
        line += " ⚠️"
    if bonus > 0:
        line += f" (+{bonus} ГБ пакет)"
    line += bypass_help_link_html()
    return line
