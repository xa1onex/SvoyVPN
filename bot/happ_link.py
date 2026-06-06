"""
Happ universal import: HTTPS /happy-link/{token} → 302 Location: happ://crypt5/…

Один endpoint для iOS, Android, Windows, macOS (без /apple|/android/…).
Шифрование — официальный API Happ (crypto.happ.su), оборачивает HTTPS /sub/{token}
(JSON bundle при providerid в query). Нельзя оборачивать /profile/…/crypt5 — двойной
crypt5 даёт ошибку 39.

Open-домены (open.*, open2.*) — только фронт для редиректа; bundle URL на основном SUBSCRIPTION_BASE_URL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import NamedTuple
from urllib.parse import quote

from .happ_proxy_api import happ_subscription_query_suffix

logger = logging.getLogger(__name__)

_DEFAULT_SUBSCRIPTION_BASE = "https://xdoublegroup.online"
_DEFAULT_CRYPT5_CACHE_TTL_SEC = 900
_MAX_CRYPT5_CACHE_ENTRIES = 4096


class _Crypt5CacheEntry(NamedTuple):
    link: str
    expires_at: float


_crypt5_cache: dict[str, _Crypt5CacheEntry] = {}
_crypt5_cache_lock = asyncio.Lock()


def should_prefer_happ_add_deep_link(user_agent: str) -> bool:
    """
    В десктопном браузере (macOS/Windows) клиент Happ часто сообщает «URL подписки невалидная»
    для ``happ://crypt5/…``, тогда как ``happ://add/https://…/sub/TOKEN?providerid=…`` с телом JSON
    импортируется нормально.

    На iPhone/iPad/Android оставляем ``crypt5`` через crypto.happ.su — там это стабильнее.
    """
    u = (user_agent or "").strip().lower()
    if not u:
        return False
    if any(x in u for x in ("iphone", "ipad", "ipod")):
        return False
    if "android" in u:
        return False
    return "macintosh" in u or "mac os x" in u or "windows nt" in u


def happ_add_deeplink_for_sub_url(subscription_https_url: str) -> str:
    """``happ://add/`` + URL-encoded HTTPS подписки (plain query ``providerid=`` в строке)."""
    s = (subscription_https_url or "").strip()
    return f"happ://add/{quote(s, safe='')}"


def parse_happ_open_base_urls() -> list[str]:
    """
    Домены для публичных happy-link (антиблок, geo, резерв).
    SVOYVPN_HAPP_OPEN_BASE_URLS=https://open.a.com,https://open2.a.com
    или SVOYVPN_HAPP_OPEN_BASE_URL + SVOYVPN_HAPP_OPEN2_BASE_URL.
    """
    multi = (os.getenv("SVOYVPN_HAPP_OPEN_BASE_URLS") or "").strip()
    if multi:
        return [u.strip().rstrip("/") for u in multi.split(",") if u.strip()]

    urls: list[str] = []
    for key in (
        "SVOYVPN_HAPP_OPEN_BASE_URL",
        "SVOYVPN_HAPP_OPEN2_BASE_URL",
        "HAPP_OPEN_BASE_URL",
        "HAPP_OPEN2_BASE_URL",
    ):
        v = (os.getenv(key) or "").strip().rstrip("/")
        if v and v not in urls:
            urls.append(v)
    return urls


def subscription_bundle_base_url(config=None) -> str:
    """База для URL профиля внутри crypt5 (основной subscription-домен)."""
    if config is not None:
        base = getattr(config, "subscription_base_url", None)
        if base:
            return str(base).rstrip("/")
    return (
        (os.getenv("SUBSCRIPTION_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
        or _DEFAULT_SUBSCRIPTION_BASE
    )


def primary_happ_import_base_url(config=None) -> str:
    """Первый open-домен для кнопки «Подключить», иначе subscription base."""
    open_urls = parse_happ_open_base_urls()
    if open_urls:
        return open_urls[0]
    return subscription_bundle_base_url(config)


def happ_import_base_urls(config=None) -> list[str]:
    """Все HTTPS-базы для happy-link (open + fallback на subscription)."""
    open_urls = parse_happ_open_base_urls()
    sub = subscription_bundle_base_url(config)
    if not open_urls:
        return [sub]
    seen: set[str] = set()
    out: list[str] = []
    for u in open_urls + [sub]:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def happy_link_https_url(import_base_url: str, subscription_token: str) -> str:
    """Публичный HTTPS URL для импорта в Happ (копирование / кнопка в боте)."""
    base = (import_base_url or "").rstrip("/")
    token = (subscription_token or "").strip()
    return f"{base}/happy-link/{token}"


def happy_link_https_urls(config=None, subscription_token: str = "") -> list[str]:
    """Те же happy-link на всех open-доменах (+ основной, если не в списке)."""
    token = (subscription_token or "").strip()
    return [happy_link_https_url(base, token) for base in happ_import_base_urls(config)]


def happ_subscription_https_url(bundle_base_url: str, subscription_token: str) -> str:
    """
    URL подписки для обёртки crypto.happ.su.
    providerid в query → JSON Xray-bundle (как при User-Agent Happ/), не plain vless.
    """
    base = (bundle_base_url or "").rstrip("/")
    token = (subscription_token or "").strip()
    return f"{base}/sub/{token}{happ_subscription_query_suffix()}"


def plain_sub_https_url(base_url: str, subscription_token: str) -> str:
    """Plain vless /sub — fallback если API crypt5 недоступен."""
    base = (base_url or "").rstrip("/")
    token = (subscription_token or "").strip()
    return f"{base}/sub/{token}{happ_subscription_query_suffix()}"


def _crypt5_cache_ttl_sec() -> int:
    try:
        return max(60, int(os.getenv("SVOYVPN_HAPP_CRYPT5_CACHE_TTL_SEC", str(_DEFAULT_CRYPT5_CACHE_TTL_SEC))))
    except ValueError:
        return _DEFAULT_CRYPT5_CACHE_TTL_SEC


def invalidate_happ_crypt5_cache(subscription_token: str) -> None:
    """Сброс кэша deeplink для токена (опционально при смене ключей)."""
    token = (subscription_token or "").strip()
    if token:
        _crypt5_cache.pop(f"sub:{token}", None)
        _crypt5_cache.pop(token, None)  # legacy key


async def happ_import_deeplink(bundle_base_url: str, subscription_token: str) -> str:
    """
    happ://crypt5/… через crypto.happ.su (оборачивает /sub/{token}?providerid=…).
    При сбое API — happ://add/… + тот же /sub URL.
    """
    sub_url = happ_subscription_https_url(bundle_base_url, subscription_token)
    try:
        from .happ_official_crypto import official_crypt5_link_for_url

        link = await official_crypt5_link_for_url(sub_url)
        logger.debug(
            "happ_import_deeplink: official crypt5 wraps %s token=%s…",
            sub_url[:64],
            subscription_token[:8],
        )
        return link
    except Exception as e:
        logger.warning(
            "happ_import_deeplink: official API failed (%s), fallback happ://add/ token=%s…",
            e,
            subscription_token[:8],
        )
        return f"happ://add/{quote(sub_url, safe='')}"


async def cached_happ_import_deeplink(bundle_base_url: str, subscription_token: str) -> str:
    """Кэшированный happ://crypt5/… (меньше нагрузки на crypto.happ.su)."""
    token = (subscription_token or "").strip()
    if not token:
        raise ValueError("empty subscription token")

    ttl = _crypt5_cache_ttl_sec()
    now = time.monotonic()
    cache_key = f"sub:{token}"
    async with _crypt5_cache_lock:
        entry = _crypt5_cache.get(cache_key)
        if entry and entry.expires_at > now:
            return entry.link

    link = await happ_import_deeplink(bundle_base_url, token)

    async with _crypt5_cache_lock:
        if len(_crypt5_cache) >= _MAX_CRYPT5_CACHE_ENTRIES:
            expired = [k for k, v in _crypt5_cache.items() if v.expires_at <= now]
            for k in expired:
                _crypt5_cache.pop(k, None)
            if len(_crypt5_cache) >= _MAX_CRYPT5_CACHE_ENTRIES:
                oldest = min(_crypt5_cache, key=lambda k: _crypt5_cache[k].expires_at)
                _crypt5_cache.pop(oldest, None)
        _crypt5_cache[cache_key] = _Crypt5CacheEntry(link=link, expires_at=now + ttl)

    return link
