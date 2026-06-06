"""
Отпечаток «устройства» для /sub без опоры на сырой IP.

Happ при запросе подписки шлёт заголовки:
  X-Device-Model, X-Device-Os, X-Ver-Os, X-Hwid
— по ним показываем «iPhone 15 Pro», «MacBook Air», а не «iPhone / iPad».
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping

from .apple_device_models import APPLE_MODEL_NAMES

_HAPP_INSTALL = re.compile(r"^happ/[\d.]+/([^/]+)/(\d{8,})", re.I)

_NON_DEVICE_UA_PREFIXES = (
    "mozilla/",
    "telegram",
    "vkshare",
    "python-",
    "curl/",
    "go-http",
    "axios/",
    "java/",
    "okhttp/",
    "dart/",
)

_COUNTABLE_VPN_PREFIXES = (
    "happ/",
    "hiddify",
    "v2raytun/",
    "streisand",
    "shadowrocket/",
    "clash-verge",
    "clashmeta",
    "sing-box",
)

_HUMAN_MODEL_RE = re.compile(
    r"^(iPhone|iPad|MacBook|Mac mini|Mac Studio|iMac|Apple Watch|AirPods)\b",
    re.I,
)


def _normalize_user_agent(ua: str) -> str:
    s = (ua or "").strip()
    if not s:
        return "unknown"
    return " ".join(s.split()).lower()


def _header_get(headers: Mapping[str, str] | None, name: str) -> str:
    if not headers:
        return ""
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return (value or "").strip()
    return ""


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
        v = _header_get(headers, k)
        if v:
            parts.append(f"{k}={v}")
    return "|".join(parts)


def parse_happ_device_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Заголовки Happ: модель, ОС, HWID."""
    if not headers:
        return {}
    hwid = _header_get(headers, "X-Hwid") or _header_get(headers, "X-HWID")
    return {
        "hwid": hwid,
        "model": _header_get(headers, "X-Device-Model"),
        "os": _header_get(headers, "X-Device-Os"),
        "os_version": _header_get(headers, "X-Ver-Os"),
    }


def parse_happ_install_key(user_agent: str) -> tuple[str, str] | None:
    ua = _normalize_user_agent(user_agent)
    m = _HAPP_INSTALL.match(ua)
    if not m:
        return None
    platform = m.group(1).lower().replace(" ", "_")
    return platform, m.group(2)


def is_happ_client_user_agent(user_agent: str) -> bool:
    return _normalize_user_agent(user_agent).startswith("happ/")


def is_countable_subscription_client(user_agent: str) -> bool:
    ua = _normalize_user_agent(user_agent)
    if not ua or ua == "unknown":
        return False
    if ua.startswith(_NON_DEVICE_UA_PREFIXES):
        return False
    if any(token in ua for token in ("bot", "crawler", "spider", "preview")):
        return False
    if ua.startswith(_COUNTABLE_VPN_PREFIXES):
        return True
    if "like clashmeta" in ua or "clashmeta" in ua:
        return True
    return False


def resolve_device_model_name(raw_model: str, device_os: str = "") -> str:
    """
    X-Device-Model → «iPhone 15 Pro».
    Поддержка Apple-идентификаторов (iPhone16,1) и уже человекочитаемых строк.
    """
    raw = (raw_model or "").strip().strip('"')
    if not raw:
        return ""

    if raw in APPLE_MODEL_NAMES:
        return APPLE_MODEL_NAMES[raw]

    if _HUMAN_MODEL_RE.match(raw):
        return raw

    os_l = (device_os or "").strip().lower()
    if raw.startswith(("iPhone", "iPad", "iPod", "Mac", "MacBook")):
        mapped = APPLE_MODEL_NAMES.get(raw)
        if mapped:
            return mapped
        if "," in raw:
            family = raw.split(",", 1)[0]
            if family in ("iPhone", "iPad"):
                return family

    if os_l == "android" and raw:
        return raw

    if os_l in ("ios", "ipados") and raw.startswith("iPhone"):
        return APPLE_MODEL_NAMES.get(raw, raw.split(",")[0])

    if os_l in ("macos", "mac os", "mac os x") and raw.startswith("Mac"):
        return APPLE_MODEL_NAMES.get(raw, raw.replace("MacBookPro", "MacBook Pro"))

    return raw


def _fingerprint_raw(
    user_agent: str,
    *,
    device_hwid: str = "",
    client_hint_headers: Mapping[str, str] | None = None,
) -> str:
    hwid = (device_hwid or "").strip()
    if hwid:
        return f"hwid:{hwid.lower()}"

    ua = _normalize_user_agent(user_agent)
    happ = parse_happ_install_key(ua)
    if happ:
        platform, install_id = happ
        return f"happ:{platform}:{install_id}"

    hints = _extract_client_hints(client_hint_headers)
    hint_model = _header_get(client_hint_headers, "Sec-CH-UA-Model").strip('"')
    if hint_model:
        return f"model:{hint_model.lower()}|ua:{ua}"
    if hints:
        return f"ua:{ua}|hints:{hints}"
    return f"ua:{ua}"


def compute_subscription_device_fingerprint(
    user_agent: str,
    *,
    device_hwid: str = "",
    client_hint_headers: Mapping[str, str] | None = None,
) -> str:
    raw = _fingerprint_raw(
        user_agent,
        device_hwid=device_hwid,
        client_hint_headers=client_hint_headers,
    )
    return hashlib.md5(raw.encode("utf-8", errors="replace")).hexdigest()


def format_device_display_name(
    user_agent: str,
    *,
    device_model: str = "",
    device_os: str = "",
    client_hint_headers: Mapping[str, str] | None = None,
) -> str:
    """Конкретное имя устройства для UI."""
    stored = (device_model or "").strip()
    if stored:
        return stored

    happ_meta = {
        "model": _header_get(client_hint_headers, "X-Device-Model"),
        "os": _header_get(client_hint_headers, "X-Device-Os") or device_os,
    }
    resolved = resolve_device_model_name(happ_meta["model"], happ_meta["os"])
    if resolved:
        return resolved

    hint_model = _header_get(client_hint_headers, "Sec-CH-UA-Model").strip('"')
    if hint_model and hint_model.lower() not in ("unknown", "?"):
        return resolve_device_model_name(hint_model, device_os) or hint_model

    ua_raw = (user_agent or "").strip()
    ua = _normalize_user_agent(user_agent)

    if ua.startswith("hiddify"):
        if "(ios)" in ua:
            return "Hiddify (iPhone)"
        if "(android)" in ua:
            return "Hiddify (Android)"
        if "(windows)" in ua:
            return "Hiddify (Windows)"
        if "(macos)" in ua:
            return "Hiddify (Mac)"
        return "Hiddify"

    if ua.startswith("v2raytun/"):
        plat = ua_raw.split("/", 1)[1].split()[0] if "/" in ua_raw else "?"
        labels = {"ios": "iPhone", "android": "Android", "macos": "Mac", "windows": "Windows"}
        return f"v2rayTun ({labels.get(plat.lower(), plat)})"

    if "clashmeta" in ua or "like clashmeta" in ua:
        if "(ios)" in ua:
            return "Clash (iPhone)"
        if "(android)" in ua:
            return "Clash (Android)"
        if "(windows)" in ua:
            return "Clash (Windows)"
        return "Clash"

    if ua.startswith("shadowrocket"):
        return "Shadowrocket (iPhone)"

    happ = parse_happ_install_key(ua)
    if happ:
        platform, _install_id = happ
        if platform == "ios":
            return "iPhone"
        if platform in ("macos", "macos_catalyst"):
            return "Mac"
        if platform == "android":
            return "Android"
        if platform == "windows":
            return "Windows"

    return "VPN-клиент"


@dataclass(frozen=True)
class SubscriptionClientInfo:
    user_agent: str
    ip_address: str
    device_hwid: str
    device_os: str
    device_model: str
    fingerprint: str
    display_name: str


def analyze_subscription_client(
    headers: Mapping[str, str],
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> SubscriptionClientInfo:
    """Разбор запроса /sub: отпечаток и имя устройства."""
    ua = (user_agent or _header_get(headers, "User-Agent") or "Unknown").strip()
    ip = (ip_address or _header_get(headers, "X-Forwarded-For") or "").split(",")[0].strip()
    if not ip:
        ip = "Unknown"

    happ = parse_happ_device_headers(headers)
    hint_keys = (
        "Sec-CH-UA-Mobile",
        "Sec-CH-UA-Platform",
        "Sec-CH-UA-Platform-Version",
        "Sec-CH-UA-Model",
        "Sec-CH-UA-Full-Version-List",
        "X-Device-Model",
        "X-Device-Os",
        "X-Ver-Os",
        "X-Hwid",
        "X-HWID",
    )
    hint_map = {k: _header_get(headers, k) for k in hint_keys if _header_get(headers, k)}

    model_name = resolve_device_model_name(happ["model"], happ["os"])
    fp = compute_subscription_device_fingerprint(
        ua,
        device_hwid=happ["hwid"],
        client_hint_headers=hint_map or None,
    )
    display = format_device_display_name(
        ua,
        device_model=model_name,
        device_os=happ["os"],
        client_hint_headers=hint_map or None,
    )
    return SubscriptionClientInfo(
        user_agent=ua,
        ip_address=ip,
        device_hwid=happ["hwid"],
        device_os=happ["os"],
        device_model=display,
        fingerprint=fp,
        display_name=display,
    )


SUBSCRIPTION_DEVICE_COUNTABLE_SQL = """
    lower(trim(coalesce(user_agent, ''))) LIKE 'happ/%'
    OR lower(trim(coalesce(user_agent, ''))) LIKE 'hiddify%'
    OR lower(trim(coalesce(user_agent, ''))) LIKE 'v2raytun/%'
    OR lower(trim(coalesce(user_agent, ''))) LIKE 'shadowrocket%'
    OR lower(trim(coalesce(user_agent, ''))) LIKE 'streisand%'
    OR lower(trim(coalesce(user_agent, ''))) LIKE 'clash-verge%'
    OR lower(trim(coalesce(user_agent, ''))) LIKE 'sing-box%'
    OR lower(coalesce(user_agent, '')) LIKE '%clashmeta%'
"""
