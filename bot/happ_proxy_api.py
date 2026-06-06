"""
Клиент API happ-proxy.com: Limited Links (install), list/delete HWID.

Документация: https://docs.happ-proxy.com/getting-started/api
(удаление HWID: GET ``/api/delete-hwid`` — см. также ru-версию docs.)

Требуется в .env:
  SVOYVPN_HAPP_AUTH_KEY — ключ 32 символа из кабинета Happ
  SVOYVPN_HAPP_PROVIDER_ID (или SVOYVPN_HAPP_PROVIDER_CODE) — ровно 8 [A-Za-z0-9]

Опционально:
  SVOYVPN_HAPP_API_BASE — по умолчанию https://happ-proxy.com
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


def happ_api_base() -> str:
    return (os.getenv("SVOYVPN_HAPP_API_BASE", "https://happ-proxy.com") or "").strip().rstrip("/")


DEFAULT_HAPP_PROVIDER_ID = "mEayGpub"


def happ_provider_code() -> str:
    """8-символьный Provider ID из кабинета Happ (по умолчанию mEayGpub)."""
    pc = (
        os.getenv("SVOYVPN_HAPP_PROVIDER_CODE")
        or os.getenv("SVOYVPN_HAPP_PROVIDER_ID")
        or DEFAULT_HAPP_PROVIDER_ID
    ).strip()
    return pc if len(pc) == 8 else DEFAULT_HAPP_PROVIDER_ID


def happ_subscription_url_fragment(*, with_install_id: bool = False) -> str:
    """
    Фрагмент URL (limited-link / installid) — ``#?providerid=…``.
    Для ``happ://add/`` используйте ``happ_subscription_query_suffix()`` без ``#``.
    """
    qs = f"providerid={happ_provider_code()}"
    if happ_hide_settings_enabled():
        qs += "&hide-settings=1"
    if with_install_id:
        return f"?{qs}"
    return f"#?{qs}"


def happ_subscription_query_suffix() -> str:
    """
    Query для HTTPS URL подписки (``happ://add/``, обёртка crypt5).

    Всегда ``providerid``; при активном режиме — и ``hide-settings=1``.
    На iPhone заголовки ответа /sub обычно подхватываются; на **macOS** при импорте
    через ``happ://add/…`` заголовки иногда теряются, поэтому дублируем
    ``hide-settings`` в строке URL (как в ``happ_subscription_url_fragment``).
    """
    qs = f"providerid={happ_provider_code()}"
    if happ_hide_settings_enabled():
        qs += "&hide-settings=1"
    return f"?{qs}"


def happ_vless_body_prefix() -> str:
    """
    providerid / hide-settings — только HTTP-заголовки ответа /sub и фрагмент/query URL подписки.
    Строки ``#providerid`` / ``#hide-settings`` **в теле** подписки перед JSON-бандлом дают ошибку 39 Happ;
    использовать только заголовки + ``?providerid=…`` (и при необходимости ``hide-settings`` в query).
    """
    return ""


def happ_auth_key() -> str:
    return (os.getenv("SVOYVPN_HAPP_AUTH_KEY", "") or "").strip()


def happ_devices_api_enabled() -> bool:
    pc = happ_provider_code()
    ak = happ_auth_key()
    return bool(pc and len(pc) == 8 and ak and len(ak) == 32)


def happ_hide_settings_enabled() -> bool:
    """
    App management: параметр hide-settings (заголовки ответа + фрагмент URL подписки).
    Отключить: ``SVOYVPN_HAPP_HIDE_SETTINGS=0`` / ``false`` / ``off`` / ``no`` / ``disabled``.
    """
    raw = (os.getenv("SVOYVPN_HAPP_HIDE_SETTINGS", "1") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "disable", "disabled")


def happ_subscription_fragment_device_params() -> str:
    """``&providerid=…&hide-settings=1`` после ``?installid=…`` в limited-link URL."""
    parts: list[str] = [f"providerid={happ_provider_code()}"]
    if happ_hide_settings_enabled():
        parts.append("hide-settings=1")
    return "&" + "&".join(parts)


async def _get_json(session: aiohttp.ClientSession, path: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"{happ_api_base()}{path}"
    timeout = aiohttp.ClientTimeout(total=25)
    async with session.get(url, params=params, timeout=timeout) as resp:
        text = await resp.text()
        try:
            data: dict[str, Any] = __import__("json").loads(text)
        except Exception:
            data = {"rc": 0, "msg": text[:200]}
        if resp.status != 200:
            logger.warning("Happ API HTTP %s %s: %s", resp.status, path, text[:300])
        return data


async def happ_add_install(*, install_limit: int, note: str | None = None) -> tuple[str | None, int | None, str]:
    """
    Создать limited link. Возвращает (install_code, id, msg).
    install_limit 1..100.
    """
    if not happ_devices_api_enabled():
        return None, None, "Happ API not configured"
    install_limit = max(1, min(100, int(install_limit)))
    params: dict[str, Any] = {
        "provider_code": happ_provider_code(),
        "auth_key": happ_auth_key(),
        "install_limit": install_limit,
    }
    if note:
        params["note"] = note[:255]
    async with aiohttp.ClientSession() as session:
        data = await _get_json(session, "/api/add-install", params)
    if int(data.get("rc") or 0) != 1:
        return None, None, str(data.get("msg") or data)
    code = str(data.get("install_code") or "").strip()
    rid = data.get("id")
    rid_i = int(rid) if rid is not None else None
    if len(code) != 12:
        return None, None, f"Bad install_code: {code!r}"
    return code, rid_i, "Ok"


async def happ_list_hwid(
    install_code: str | None = None,
    *,
    install_id: int | None = None,
) -> list[dict[str, Any]]:
    if not happ_devices_api_enabled():
        return []
    params: dict[str, Any] = {
        "provider_code": happ_provider_code(),
        "auth_key": happ_auth_key(),
    }
    if install_code and len(str(install_code).strip()) == 12:
        params["install_code"] = str(install_code).strip()
    elif install_id is not None:
        params["install_id"] = int(install_id)
    else:
        return []
    async with aiohttp.ClientSession() as session:
        data = await _get_json(session, "/api/list-hwid", params)
    if int(data.get("rc") or 0) != 1:
        logger.warning("Happ list-hwid: %s", data.get("msg"))
        return []
    raw = data.get("data")
    return list(raw) if isinstance(raw, list) else []


async def happ_delete_hwid(
    hwid: str,
    *,
    install_code: str | None = None,
    install_id: int | None = None,
) -> tuple[bool, str]:
    if not happ_devices_api_enabled() or not (hwid or "").strip():
        return False, "Bad parameters"
    params: dict[str, Any] = {
        "provider_code": happ_provider_code(),
        "auth_key": happ_auth_key(),
        "hwid": hwid.strip(),
    }
    if install_code and len(str(install_code).strip()) == 12:
        params["install_code"] = str(install_code).strip()
    elif install_id is not None:
        params["install_id"] = int(install_id)
    else:
        return False, "install_code or install_id required"
    async with aiohttp.ClientSession() as session:
        data = await _get_json(session, "/api/delete-hwid", params)
    ok = int(data.get("rc") or 0) == 1
    return ok, str(data.get("msg") or "")


async def happ_update_install_limit(*, install_id: int, install_limit: int) -> tuple[bool, str]:
    if not happ_devices_api_enabled():
        return False, "Happ API not configured"
    install_limit = max(1, min(100, int(install_limit)))
    params: dict[str, Any] = {
        "provider_code": happ_provider_code(),
        "auth_key": happ_auth_key(),
        "id": install_id,
        "install_limit": install_limit,
    }
    async with aiohttp.ClientSession() as session:
        data = await _get_json(session, "/api/update-install", params)
    ok = int(data.get("rc") or 0) == 1
    return ok, str(data.get("msg") or "")
