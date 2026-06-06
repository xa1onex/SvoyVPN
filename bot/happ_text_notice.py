"""
Текстовые строки vless:// для Happ (plain /sub): подзаголовок списка через
``#title?serverDescription=<base64 UTF-8>`` — см. Happ App Management
(server description в URI). Иначе Happ рисует «VLESS | TCP | TLS».
"""

from __future__ import annotations

import base64
from urllib.parse import quote


def happ_info_notice_vless_uri(
    *,
    title: str,
    uuid: str = "00000000-0000-0000-0000-000000000000",
    address: str = "127.0.0.1",
    port: int = 65534,
    query: str = "type=tcp&security=none&flow=none",
) -> str:
    """Информационная строка plain /sub: только название сервера, без serverDescription."""
    return f"vless://{uuid}@{address}:{port}?{query}#{quote(title, safe='')}"


def happ_text_notice_vless_uri(
    *,
    title: str,
    subtitle: str = "Информация",
    uuid: str = "00000000-0000-0000-0000-000000000000",
    address: str = "0.0.0.0",
    port: int = 1,
    query: str = "type=tcp&security=none&flow=none",
) -> str:
    """
    Одна строка подписки: нерабочий vless с человекочитаемым subtitle вместо
    авто-подписи протокола (до 30 символов, как у Happ для serverDescription).
    """
    sd = ((subtitle or "Информация").strip() or "Информация")[:30].rstrip() or "Информация"
    b64 = base64.b64encode(sd.encode("utf-8")).decode("ascii")
    return (
        f"vless://{uuid}@{address}:{port}?{query}"
        f"#{quote(title, safe='')}?serverDescription={b64}"
    )


def vless_link_title_only(link: str, *, title: str) -> str:
    """Рабочий vless:// только с названием в фрагменте, без serverDescription."""
    if not link or not link.startswith("vless://"):
        return link
    base = link.split("#", 1)[0]
    return f"{base}#{quote(title, safe='')}"


def vless_link_with_happ_caption(
    link: str,
    *,
    remark: str = "",
    server_name: str = "",
    is_bypass: bool = False,
    is_tg_relay: bool = False,
    title: str | None = None,
    subtitle: str | None = None,
) -> str:
    """Рабочий vless:// с #title?serverDescription=base64 для Happ plain /sub."""
    if not link or not link.startswith("vless://"):
        return link
    base = link.split("#", 1)[0]
    if title is None or subtitle is None:
        from .happ_catalog import presentation_for_server

        tr, td = presentation_for_server(
            remark=remark or server_name,
            server_name=server_name,
            is_bypass=is_bypass,
            is_tg_relay=is_tg_relay,
        )
        if title is None:
            title = tr
        if subtitle is None:
            subtitle = td
    tr = (title or "").strip()
    sd = ((subtitle or "Информация").strip() or "Информация")[:30].rstrip() or "Информация"
    b64 = base64.b64encode(sd.encode("utf-8")).decode("ascii")
    return f"{base}#{quote(tr, safe='')}?serverDescription={b64}"
