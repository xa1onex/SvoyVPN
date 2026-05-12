"""
Отпечаток «устройства» для /sub без опоры на сырой IP.

Клиенты (Happ, Hiddify и т.д.) обычно шлют стабильный User-Agent; часть
добавляет хвост с билдом/идентификатором. Дополнительно учитываем
Client Hints (Sec-CH-UA-*), если прокси их пробрасывает — это ближе к
тому, как браузеры различают устройства, без привязки к IP VPN.
"""
from __future__ import annotations

import hashlib
import re
from typing import Mapping


def _normalize_user_agent(ua: str) -> str:
    s = (ua or "").strip()
    if not s:
        return "unknown"
    return " ".join(s.split()).lower()


def _extract_client_hints(headers: Mapping[str, str] | None) -> str:
    if not headers:
        return ""
    keys = (
        "Sec-CH-UA-Mobile",
        "Sec-CH-UA-Platform",
        "Sec-CH-UA-Platform-Version",
        "Sec-CH-UA-Model",
        "Sec-CH-UA-Full-Version-List",
    )
    parts: list[str] = []
    for k in keys:
        v = headers.get(k) or headers.get(k.lower())
        if v:
            parts.append(f"{k}={v.strip()}")
    return "|".join(parts)


def _happ_style_tail(ua: str) -> str:
    """
    Happ часто шлёт: Happ/4.9.0/ios/2605051735629 — хвост часто уникален на установку.
    """
    m = re.search(r"/(ios|android)/(\d{8,})", ua, re.I)
    if m:
        return f"|{m.group(1).lower()}:{m.group(2)}"
    return ""


def compute_subscription_device_fingerprint(
    user_agent: str,
    *,
    client_hint_headers: Mapping[str, str] | None = None,
) -> str:
    """
    Стабильный короткий идентификатор клиента для лимита устройств.
    IP намеренно не входит в строку (смена выхода VPN не создаёт «новое устройство»).
    """
    ua = _normalize_user_agent(user_agent)
    tail = _happ_style_tail(ua)
    hints = _extract_client_hints(client_hint_headers)
    raw = f"ua:{ua}{tail}"
    if hints:
        raw += f"|hints:{hints}"
    # MD5 hex = 32 chars; совпадает с SQL backfill в init_db (digest не нужен).
    return hashlib.md5(raw.encode("utf-8", errors="replace")).hexdigest()
