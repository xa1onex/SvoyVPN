"""
Официальное шифрование ссылок Happ (native crypt5).

Документация: https://www.happ.su/happ/dev-docs/crypto-link
  POST https://crypto.happ.su/api-v2.php
  JSON: {"url": "https://…"}
  Ответ: {"encrypted_link": "happ://crypt5/…"}

Это не «наш» AES-GCM по JSON: ключи и формат внутри приложения Happ.
Чтобы подписка открывалась в protected mode, нужно отдавать именно
ссылку с crypto.happ.su, которая оборачивает URL подписки (например /sub/…).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

_DEFAULT_API = "https://crypto.happ.su/api-v2.php"


async def official_crypt5_link_for_url(subscription_url: str) -> str:
    """
    Возвращает полную строку happ://crypt5/… от официального API Happ.

    :param subscription_url: HTTPS URL подписки (обычно …/sub/{token})
    """
    api = os.getenv("SVOYVPN_HAPP_OFFICIAL_CRYPTO_URL", _DEFAULT_API).strip()
    timeout = aiohttp.ClientTimeout(total=20)
    payload: dict[str, Any] = {"url": subscription_url}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            api,
            json=payload,
            headers={"Content-Type": "application/json"},
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Happ crypto API HTTP {resp.status}: {text[:500]}")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {}

    if isinstance(data, dict) and data.get("encrypted_link"):
        link = str(data["encrypted_link"]).strip()
        if not link.startswith("happ://crypt5/"):
            raise RuntimeError(f"Unexpected encrypted_link format: {link[:80]}…")
        return link

    raise RuntimeError(f"Happ crypto API: no encrypted_link in response: {text[:500]}")
