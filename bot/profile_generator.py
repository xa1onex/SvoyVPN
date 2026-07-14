"""
Генератор Xray JSON профилей для Happ.

Подписка Happ — это массив JSON-профилей, упакованный в один crypt5 на стороне
webhook (см. build_happ_bundle_json_for_keys). Вложенные «как Clash/Mihomo»
selector → urltest внутри одной подписки недоступны, пока клиент не поддерживает
эти форматы; визуально близкий UX даёт порядок: быстрый узел → автовыбор
(burstObservatory + leastLoad) → отдельные страны.

Каждый элемент массива после расшифровки в Happ = отдельная строка в UI.

Подзаголовок в Happ: официально задаётся ``meta.serverDescription`` (до 30 символов).
В расширенной документации Happ указано, что **подпись под названием сервера
(serverDescription) доступна только при зарегистрированном Provider ID**
(HTTP-заголовок ``providerid`` на ответе подписки). Без него клиент часто
игнорирует ``meta`` и показывает технический ярлык («VLESS», «VLESS | JSON»).
Если ``SVOYVPN_HAPP_PROVIDER_ID`` не задан, краткое описание добавляется в
``remarks`` (одна строка с разделителем « · »), чтобы текст был виден в списке.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote

from .routing_rules import (
    RU_BYPASS_DOMAINS,
    YOUTUBE_RF_OUTBOUND_TAG,
    prepend_youtube_rf_rules,
    youtube_rf_enabled,
)
from .happ_catalog import autoselect_presentation, presentation_for_server
from .static_servers import youtube_adfree_link

# https://www.happ.su/happ/dev-docs/app-management — serverDescription max 30 chars
HAPP_META_SERVER_DESCRIPTION_MAX_LEN = 30


def clamp_happ_server_description(text: str, *, default: str = "Стабильное подключение") -> str:
    """Обрезка под лимит Happ для meta.serverDescription (иначе субтитр не подставляется)."""
    s = (text or "").strip() or (default or "").strip() or "SvoyVPN"
    max_len = HAPP_META_SERVER_DESCRIPTION_MAX_LEN
    if len(s) <= max_len:
        return s
    cut = s[:max_len].rstrip()
    return cut if cut else s[:max_len]


def _happ_provider_id_configured() -> bool:
    """Happ показывает meta.serverDescription в подписке в основном при providerid."""
    return bool(os.getenv("SVOYVPN_HAPP_PROVIDER_ID", "").strip())


# Макс. длина одной строки названия в списке (запас; точный лимит Happ не задокументирован).
_REMARKS_WITH_SUBTITLE_MAX_LEN = 56


def _merge_remarks_subtitle(remarks: str, subtitle: str) -> str:
    """Вшить подпись в название, когда Happ не применяет meta без Provider ID."""
    r = (remarks or "").strip()
    s = (subtitle or "").strip()
    if not s:
        return r
    sep = " · "
    if s.lower() in r.lower():
        return r
    combo = f"{r}{sep}{s}"
    if len(combo) <= _REMARKS_WITH_SUBTITLE_MAX_LEN:
        return combo
    max_r = max(4, _REMARKS_WITH_SUBTITLE_MAX_LEN - len(sep) - len(s))
    r2 = r[:max_r].rstrip()
    return f"{r2}{sep}{s}"[:_REMARKS_WITH_SUBTITLE_MAX_LEN]


def _happ_row_remarks(remarks: str, subtitle_plain: str) -> str:
    if _happ_provider_id_configured():
        return (remarks or "").strip()
    return _merge_remarks_subtitle(remarks, subtitle_plain)


# ---------------------------------------------------------------------------
# VLESS link parser
# ---------------------------------------------------------------------------

def parse_vless_link(link: str) -> dict[str, Any] | None:
    if not link or not link.startswith("vless://"):
        return None
    try:
        fragment = ""
        if "#" in link:
            link_part, fragment = link.rsplit("#", 1)
            fragment = unquote(fragment)
        else:
            link_part = link

        without_scheme = link_part[len("vless://"):]
        uuid_part, rest = without_scheme.split("@", 1)

        if "/?" in rest:
            addr_port, query_str = rest.split("/?", 1)
        elif "?" in rest:
            addr_port, query_str = rest.split("?", 1)
        else:
            addr_port, query_str = rest, ""

        address, port_str = (addr_port.rsplit(":", 1) if ":" in addr_port else (addr_port, "443"))
        params = parse_qs(query_str, keep_blank_values=True)

        extra_raw = (params.get("extra") or [""])[0]
        extra: dict[str, Any] | None = None
        if extra_raw:
            try:
                extra = json.loads(unquote(extra_raw))
            except Exception:
                extra = None

        alpn_raw = (params.get("alpn") or [""])[0]
        alpn = [p.strip() for p in alpn_raw.split(",") if p.strip()] if alpn_raw else []

        net_type = params.get("type", ["tcp"])[0]
        default_mode = "auto" if net_type == "xhttp" else "gun"
        return {
            "protocol": "vless",
            "uuid": uuid_part,
            "address": address,
            "port": int(port_str),
            "remark": fragment.split("?", 1)[0] if fragment else "",
            "security": params.get("security", ["none"])[0],
            "type": net_type,
            "flow": params.get("flow", [""])[0],
            "sni": params.get("sni", [""])[0],
            "host": params.get("host", [""])[0],
            "path": params.get("path", [""])[0],
            "pbk": params.get("pbk", [""])[0],
            "sid": params.get("sid", [""])[0],
            "fp": params.get("fp", ["chrome"])[0],
            "spx": params.get("spx", [""])[0],
            "serviceName": params.get("serviceName", [None])[0],
            "mode": params.get("mode", [default_mode])[0],
            "extra": extra,
            "alpn": alpn,
        }
    except Exception:
        return None


def parse_hysteria2_link(link: str) -> dict[str, Any] | None:
    """Parse hysteria2:// / hy2:// URI used by Happ native + JSON profiles."""
    if not link:
        return None
    raw = link.strip()
    scheme = None
    for prefix in ("hysteria2://", "hy2://"):
        if raw.startswith(prefix):
            scheme = prefix
            break
    if not scheme:
        return None
    try:
        fragment = ""
        if "#" in raw:
            link_part, fragment = raw.rsplit("#", 1)
            fragment = unquote(fragment)
        else:
            link_part = raw

        without_scheme = link_part[len(scheme):]
        auth, rest = without_scheme.split("@", 1)

        if "/?" in rest:
            addr_port, query_str = rest.split("/?", 1)
        elif "?" in rest:
            addr_port, query_str = rest.split("?", 1)
        else:
            addr_port, query_str = rest, ""

        address, port_str = (addr_port.rsplit(":", 1) if ":" in addr_port else (addr_port, "443"))
        params = parse_qs(query_str, keep_blank_values=True)
        alpn_raw = (params.get("alpn") or ["h3"])[0]
        alpn = [p.strip() for p in alpn_raw.split(",") if p.strip()] or ["h3"]
        remark = fragment.split("?", 1)[0] if fragment else ""
        return {
            "protocol": "hysteria2",
            "auth": unquote(auth),
            "address": address,
            "port": int(port_str),
            "remark": remark,
            "sni": (params.get("sni") or [address])[0] or address,
            "fp": (params.get("fp") or ["chrome"])[0] or "chrome",
            "alpn": alpn,
            "congestion": (params.get("congestion") or ["bbr"])[0] or "bbr",
            "insecure": (params.get("insecure") or ["0"])[0] in ("1", "true", "True"),
        }
    except Exception:
        return None


def parse_proxy_link(link: str) -> dict[str, Any] | None:
    return parse_vless_link(link) or parse_hysteria2_link(link)


def _is_fake_link(parsed: dict[str, Any]) -> bool:
    if parsed.get("protocol") == "hysteria2":
        return parsed.get("address") in ("0.0.0.0", "127.0.0.1")
    if parsed["address"] == "0.0.0.0":
        return True
    uuid_clean = str(parsed.get("uuid") or "").replace("-", "")
    return bool(uuid_clean) and len(set(uuid_clean)) <= 2


# ---------------------------------------------------------------------------
# Shared Xray building blocks
# ---------------------------------------------------------------------------

_DNS = {
    "hosts": {
        "cloudflare-dns.com": ["1.1.1.1", "1.0.0.1"],
        "dns.google": ["8.8.8.8", "8.8.4.4"],
        "dns.quad9.net": ["9.9.9.9", "149.112.112.112"],
        "one.one.one.one": ["1.1.1.1", "1.0.0.1"],
    },
    "queryStrategy": "UseIPv4",
    "servers": [
        "https://dns.google/dns-query",
        "https://cloudflare-dns.com/dns-query",
        "https://dns.quad9.net/dns-query",
        "8.8.8.8", "1.1.1.1",
    ],
}

_INBOUNDS = [
    {
        "listen": "127.0.0.1", "port": 10808, "protocol": "socks",
        "settings": {"auth": "noauth", "udp": True, "userLevel": 8},
        "sniffing": {"destOverride": ["http", "tls", "quic"], "enabled": True},
        "tag": "socks",
    },
    {
        "listen": "127.0.0.1", "port": 10809, "protocol": "http",
        "settings": {"userLevel": 8},
        "sniffing": {"destOverride": ["http", "tls", "quic"], "enabled": True},
        "tag": "http",
    },
]

_POLICY = {"system": {"statsOutboundDownlink": True, "statsOutboundUplink": True}}

_BASE_RULES: list[dict[str, Any]] = [
    {"outboundTag": "block", "protocol": ["bittorrent"], "type": "field"},
    {
        "domain": RU_BYPASS_DOMAINS + [
            "geosite:private",
            "geosite:category-gov-ru",
            "regexp:.*\\.xn--p1ai$",
        ],
        "outboundTag": "direct",
        "type": "field",
    },
    {"ip": ["geoip:private", "geoip:ru"], "outboundTag": "direct", "type": "field"},
]

# Xray 26.6.22+ (#6258): sessionKey/sessionPlacement → sessionID* (без fallback).
# Remnawave vless:// extra ещё отдаёт старые имена — Happ 4.14 / 26.6 их игнорирует.
_SESSION_ID_TABLE_DEFAULT = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)

_DNS_XHTTP = {
    "queryStrategy": "UseIP",
    "servers": ["1.1.1.1", "1.0.0.1"],
}

_INBOUNDS_XHTTP = [
    {
        "listen": "127.0.0.1",
        "port": 10808,
        "protocol": "socks",
        "settings": {"auth": "noauth", "udp": True},
        "sniffing": {
            "destOverride": ["http", "tls", "quic"],
            "enabled": True,
            "routeOnly": False,
        },
        "tag": "socks",
    },
    {
        "listen": "127.0.0.1",
        "port": 10809,
        "protocol": "http",
        "settings": {"allowTransparent": False},
        "sniffing": {
            "destOverride": ["http", "tls", "quic"],
            "enabled": True,
            "routeOnly": False,
        },
        "tag": "http",
    },
]


def _adapt_xhttp_extra(extra: dict[str, Any] | None, path: str) -> dict[str, Any]:
    """
    Привести xhttp extra к формату, который понимает Xray 26.6+ (как у рабочих подписок).
    Старые sessionKey/sessionPlacement дублируем в sessionID* + sessionIDLength/Table.
    """
    merged = dict(extra) if isinstance(extra, dict) else {}
    merged.setdefault("path", path or "/")

    session_key = merged.get("sessionIDKey") or merged.get("sessionKey")
    session_placement = merged.get("sessionIDPlacement") or merged.get("sessionPlacement")
    if session_key:
        merged.setdefault("sessionIDKey", session_key)
        merged.setdefault("sessionKey", session_key)
    if session_placement:
        merged.setdefault("sessionIDPlacement", session_placement)
        merged.setdefault("sessionPlacement", session_placement)
    if session_key and session_placement:
        merged.setdefault("sessionIDLength", "16-32")
        merged.setdefault("sessionIDTable", _SESSION_ID_TABLE_DEFAULT)

    return merged


def _build_stream(parsed: dict[str, Any]) -> dict[str, Any]:
    security = parsed.get("security", "none")
    net = parsed.get("type", "tcp")
    ss: dict[str, Any] = {"network": net, "security": security}

    if security == "reality":
        ss["realitySettings"] = {
            "fingerprint": parsed.get("fp", "chrome"),
            "publicKey": parsed.get("pbk", ""),
            "serverName": parsed.get("sni", ""),
            "shortId": parsed.get("sid", ""),
            "spiderX": parsed.get("spx") or "/",
        }
    elif security == "tls":
        tls: dict[str, Any] = {
            "serverName": parsed.get("sni") or parsed.get("host") or "",
            "fingerprint": parsed.get("fp", "chrome"),
        }
        if parsed.get("alpn"):
            tls["alpn"] = parsed["alpn"]
        ss["tlsSettings"] = tls

    if net == "tcp":
        ss["tcpSettings"] = {}
    elif net == "grpc":
        ss["grpcSettings"] = {
            "authority": "",
            "multiMode": parsed.get("mode") == "multi",
            "serviceName": parsed.get("serviceName") or "",
        }
    elif net == "ws":
        ss["wsSettings"] = {"path": parsed.get("spx") or "/", "headers": {}}
    elif net == "xhttp":
        xhttp: dict[str, Any] = {
            "path": parsed.get("path") or "/",
            "host": parsed.get("host") or parsed.get("sni") or parsed.get("address") or "",
            "mode": parsed.get("mode") or "auto",
        }
        extra = parsed.get("extra")
        if isinstance(extra, dict) and extra:
            xhttp["extra"] = _adapt_xhttp_extra(extra, xhttp["path"])
        elif xhttp.get("path"):
            xhttp["extra"] = _adapt_xhttp_extra(None, xhttp["path"])
        ss["xhttpSettings"] = xhttp

    return ss


def _build_vless_outbound(parsed: dict[str, Any], tag: str) -> dict[str, Any]:
    return {
        "protocol": "vless",
        "settings": {"vnext": [{"address": parsed["address"], "port": parsed["port"],
                                 "users": [{"encryption": "none", "flow": parsed.get("flow", ""), "id": parsed["uuid"]}]}]},
        "streamSettings": _build_stream(parsed),
        "tag": tag,
    }


_youtube_rf_outbound_cache: dict[str, Any] | None = None


def _youtube_rf_outbound() -> dict[str, Any]:
    """RU HY2 exit для YouTube без рекламы (встроен во все VLESS-профили)."""
    global _youtube_rf_outbound_cache
    if _youtube_rf_outbound_cache is not None:
        return _youtube_rf_outbound_cache
    parsed = parse_hysteria2_link(youtube_adfree_link())
    if not parsed:
        raise RuntimeError("YouTube RF HY2 link is invalid")
    _youtube_rf_outbound_cache = _build_hysteria2_outbound(parsed, YOUTUBE_RF_OUTBOUND_TAG)
    return _youtube_rf_outbound_cache


def _append_youtube_rf_outbound(outbounds: list[dict[str, Any]]) -> None:
    """Вставить HY2 outbound перед direct/block (только если RF включён)."""
    if not youtube_rf_enabled():
        return
    insert_at = max(len(outbounds) - 2, 1)
    outbounds.insert(insert_at, _youtube_rf_outbound())


def _build_hysteria2_outbound(parsed: dict[str, Any], tag: str = "proxy") -> dict[str, Any]:
    """Happ/Xray hysteria2 outbound (matches working Happ export)."""
    alpn = parsed.get("alpn") or ["h3"]
    if isinstance(alpn, str):
        alpn = [p.strip() for p in alpn.split(",") if p.strip()] or ["h3"]
    return {
        "protocol": "hysteria",
        "settings": {
            "address": parsed["address"],
            "port": int(parsed["port"]),
            "version": 2,
        },
        "streamSettings": {
            "finalmask": {
                "quicParams": {
                    "congestion": parsed.get("congestion") or "bbr",
                    "debug": False,
                }
            },
            "hysteriaSettings": {
                "auth": parsed["auth"],
                "version": 2,
            },
            "network": "hysteria",
            "security": "tls",
            "tlsSettings": {
                "alpn": alpn,
                "fingerprint": parsed.get("fp") or "chrome",
                "serverName": parsed.get("sni") or parsed["address"],
            },
        },
        "tag": tag,
    }


# ---------------------------------------------------------------------------
# Single-server config (one entry in Happ UI)
# ---------------------------------------------------------------------------

_MINIMAL_ROUTING_RULES: list[dict[str, Any]] = [
    {"outboundTag": "direct", "protocol": ["bittorrent"], "type": "field"},
]


def _build_single_server_config(parsed: dict[str, Any], remarks: str, description: str = "") -> dict[str, Any]:
    """Xray JSON config for ONE server — appears as one entry in Happ."""
    ui_description = clamp_happ_server_description(description)
    final_remarks = _happ_row_remarks(remarks, ui_description)
    proto = parsed.get("protocol") or "vless"
    is_xhttp = (parsed.get("type") or "tcp") == "xhttp"
    if proto == "hysteria2":
        # YouTube/special nodes: весь трафик в proxy (иначе geoip:ru уведёт YouTube в direct).
        outbound = _build_hysteria2_outbound(parsed, "proxy")
        rules = list(_MINIMAL_ROUTING_RULES)
        dns = _DNS
        inbounds = _INBOUNDS
    elif is_xhttp:
        outbound = _build_vless_outbound(parsed, "proxy")
        rules = list(_MINIMAL_ROUTING_RULES)
        dns = _DNS_XHTTP
        inbounds = _INBOUNDS_XHTTP
    else:
        outbound = _build_vless_outbound(parsed, "proxy")
        rules = list(_BASE_RULES)
        dns = _DNS
        inbounds = _INBOUNDS
    outbounds: list[dict[str, Any]] = [
        outbound,
        {"protocol": "freedom", "tag": "direct"},
        {"protocol": "blackhole", "tag": "block"},
    ]
    if proto != "hysteria2":
        _append_youtube_rf_outbound(outbounds)
        rules = prepend_youtube_rf_rules(rules)

    cfg: dict[str, Any] = {
        "dns": dns,
        "inbounds": inbounds,
        "log": {"loglevel": "warning"},
        "outbounds": outbounds,
        "routing": {
            "domainMatcher": "hybrid",
            "domainStrategy": "IPIfNonMatch",
            "rules": rules,
        },
        "remarks": final_remarks,
        "meta": {"serverDescription": ui_description},
    }
    if not is_xhttp:
        cfg["policy"] = _POLICY
        cfg["stats"] = {}
    return cfg


# ---------------------------------------------------------------------------
# Auto-select config (balancer + observatory)
# ---------------------------------------------------------------------------

def generate_xray_profile(
    vless_links: list[str],
    server_names: list[str] | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Xray JSON автовыбора — burstObservatory + leastLoad balancer."""
    outbounds: list[dict[str, Any]] = []
    smart_tags: list[str] = []
    proxy_assigned = False

    for link in vless_links:
        parsed = parse_vless_link(link)
        if not parsed or _is_fake_link(parsed):
            continue

        if not proxy_assigned:
            tag = "proxy"
            proxy_assigned = True
        elif not smart_tags:
            tag = "smart"
        else:
            tag = f"smart-{len(smart_tags) + 1}"

        outbounds.append(_build_vless_outbound(parsed, tag))
        if tag != "proxy":
            smart_tags.append(tag)

    outbounds.append({"protocol": "freedom", "tag": "direct"})
    outbounds.append({"protocol": "blackhole", "tag": "block"})
    _append_youtube_rf_outbound(outbounds)

    rules = prepend_youtube_rf_rules(list(_BASE_RULES))
    balancers: list[dict[str, Any]] = []

    if smart_tags:
        balancers.append({
            "fallbackTag": "proxy",
            "selector": ["smart"],
            "strategy": {"settings": {"baselines": ["800ms", "10000ms"], "expected": 0, "maxRTT": "10000ms", "tolerance": 0.01}, "type": "leastLoad"},
            "tag": "AUTO_BALANCER",
        })
        rules.append({"balancerTag": "AUTO_BALANCER", "network": "tcp,udp", "type": "field"})
    else:
        rules.append({"outboundTag": "proxy", "network": "tcp,udp", "type": "field"})

    observatory: dict[str, Any] | None = None
    if smart_tags:
        observatory = {
            "pingConfig": {"destination": "http://cp.cloudflare.com/generate_204", "interval": "1m", "sampling": 2, "timeout": "3s"},
            "subjectSelector": ["smart"],
        }

    profile: dict[str, Any] = {}
    if observatory:
        profile["burstObservatory"] = observatory
    profile["dns"] = _DNS
    profile["inbounds"] = _INBOUNDS
    profile["log"] = {"loglevel": "warning"}
    profile["outbounds"] = outbounds
    profile["policy"] = _POLICY
    auto_remarks, auto_desc = autoselect_presentation()
    ui_desc = clamp_happ_server_description(auto_desc)
    profile["routing"] = {"balancers": balancers, "domainMatcher": "hybrid", "domainStrategy": "IPIfNonMatch", "rules": rules}
    profile["stats"] = {}
    profile["remarks"] = _happ_row_remarks(auto_remarks, ui_desc)
    profile["meta"] = {"serverDescription": ui_desc}
    return profile


# ---------------------------------------------------------------------------
# Generate full Happ subscription (auto-select + individual servers)
# ---------------------------------------------------------------------------

def _fast_server_index(display_names: list[str]) -> int:
    """Индекс строки «🚀 Быстрые сервера» (или первый), чтобы автовыбор шёл сразу после неё."""
    for i, raw in enumerate(display_names):
        name = (raw or "").lower()
        if "быстр" in name:
            return i
        if "🚀" in (raw or ""):
            return i
    return 0


def _navigation_header_config(server_name: str) -> dict[str, Any]:
    """Заголовок секции в Happ: 0.0.0.0 + VLESS none (виден в списке, не пингуется)."""
    from .traffic import is_fast_section_header, navigation_header_vless_line

    name = (server_name or "Сервер").strip()
    nav_uuid = (
        "00000000-0000-0000-0000-000000000001"
        if is_fast_section_header(name)
        else "00000000-0000-0000-0000-000000000002"
    )
    link = navigation_header_vless_line(name, uuid=nav_uuid)
    parsed = parse_vless_link(link)
    if not parsed:
        parsed = {
            "uuid": nav_uuid,
            "address": "0.0.0.0",
            "port": 1,
            "remark": name,
            "security": "none",
            "type": "tcp",
            "flow": "none",
            "sni": "",
            "pbk": "",
            "sid": "",
            "fp": "chrome",
            "spx": "",
            "serviceName": None,
        }
    tr, td = presentation_for_server(remark=name, server_name=name, is_bypass=False)
    cfg = _build_single_server_config(parsed, remarks=tr, description=td)
    cfg.pop("meta", None)
    return cfg


def _dummy_notice_parsed() -> dict[str, Any]:
    """Заглушка для записей-уведомлений в списке Happ (не подключается)."""
    return {
        "uuid": "a1b2c3d4-e5f6-4789-a012-3456789abcde",
        "address": "127.0.0.1",
        "port": 65534,
        "remark": "",
        "security": "none",
        "type": "tcp",
        "flow": "",
        "sni": "",
        "pbk": "",
        "sid": "",
        "fp": "chrome",
        "spx": "",
        "serviceName": None,
    }


def _build_info_notice_config(remarks: str, *, uuid: str | None = None) -> dict[str, Any]:
    """Информационная запись в Happ: только remarks, без meta.serverDescription."""
    parsed = {**_dummy_notice_parsed(), "remark": remarks.strip()}
    if uuid:
        parsed["uuid"] = uuid
    cfg = _build_single_server_config(parsed, remarks=remarks, description="")
    cfg.pop("meta", None)
    return cfg


_HAPP_NOTICE_REMARK_MAX_LEN = 32


def _happ_notice_remark(text: str) -> str:
    """Короткая строка для информационного сервера в Happ."""
    s = (text or "").strip()
    if len(s) <= _HAPP_NOTICE_REMARK_MAX_LEN:
        return s
    cut = s[:_HAPP_NOTICE_REMARK_MAX_LEN].rstrip()
    return cut if cut else s[:_HAPP_NOTICE_REMARK_MAX_LEN]


def _format_gb_short(value_bytes: int) -> str:
    gb = int(value_bytes) / (1024**3)
    if abs(gb - round(gb)) < 0.05:
        return f"{round(gb):.0f}"
    return f"{gb:.1f}".rstrip("0").rstrip(".")


def _device_limit_remark(text: str) -> str:
    return _happ_notice_remark(text)


def _device_limit_notice_items(
    device_count: int, device_limit: int, bot_username: str
) -> list[str]:
    """Краткая пошаговая инструкция — каждый пункт отдельный сервер."""
    handle = (bot_username or "SvoyVPN_robot").lstrip("@")
    lim = max(int(device_limit), 1)
    return [
        _device_limit_remark(f"⚠️ Лимит устройств исчерпан: {device_count}/{lim}"),
        _device_limit_remark("Чтобы сбросить лимит:"),
        _device_limit_remark(f"1. Открыть @{handle}"),
        _device_limit_remark("2. 📱 Устройства"),
        _device_limit_remark("3. 🔄 Сбросить ненужные сессии"),
        _device_limit_remark("4. Обновить Happ, через 🔄"),
        _device_limit_remark("⭐ Кстати, Plus без лимита"),
    ]


_DEVICE_LIMIT_NOTICE_UUIDS = tuple(
    f"44444444-4444-4444-4444-4444444444{i:02d}" for i in range(1, 12)
)


def happ_device_limit_notice_lines(
    device_count: int, device_limit: int, bot_username: str
) -> list[str]:
    """Plain-text vless-строки для Happ /sub при лимите устройств."""
    from .happ_text_notice import happ_info_notice_vless_uri

    lines: list[str] = []
    for i, title in enumerate(
        _device_limit_notice_items(device_count, device_limit, bot_username)
    ):
        lines.append(
            happ_info_notice_vless_uri(
                title=title,
                uuid=_DEVICE_LIMIT_NOTICE_UUIDS[i],
            )
        )
    return lines


def happ_device_limit_notice_configs(
    device_count: int, device_limit: int, bot_username: str
) -> list[dict[str, Any]]:
    """Happ JSON-бандл при превышении лимита устройств."""
    return [
        _build_info_notice_config(title, uuid=_DEVICE_LIMIT_NOTICE_UUIDS[i])
        for i, title in enumerate(
            _device_limit_notice_items(device_count, device_limit, bot_username)
        )
    ]


def happ_device_limit_bundle_json(
    device_count: int,
    device_limit: int,
    bot_username: str,
    *,
    tg_relay_vless_line: str | None = None,
) -> str:
    """JSON-массив для crypt5 / Happ при лимите устройств + опционально ТГ безлимит."""
    configs = happ_device_limit_notice_configs(device_count, device_limit, bot_username)
    if tg_relay_vless_line:
        parsed = parse_vless_link(tg_relay_vless_line.strip())
        if parsed and not _is_fake_link(parsed):
            from .happ_catalog import tg_relay_presentation

            tr, _ = tg_relay_presentation()
            tg_cfg = _build_single_server_config(parsed, remarks=tr, description="")
            tg_cfg.pop("meta", None)
            configs.append(tg_cfg)
    return json.dumps(configs, ensure_ascii=False)


def _bypass_limit_notice_items(
    used_bytes: int, limit_bytes: int, bot_username: str
) -> list[str]:
    """Краткая инструкция при исчерпании bypass-лимита — каждый пункт отдельный сервер."""
    handle = (bot_username or "SvoyVPN_robot").lstrip("@")
    used_s = _format_gb_short(used_bytes)
    lim_s = _format_gb_short(limit_bytes)
    return [
        _happ_notice_remark(f"⚠️ Лимит исчерпан: {used_s}/{lim_s} ГБ"),
        _happ_notice_remark("Чтобы увеличить лимит:"),
        _happ_notice_remark(f"1. Откройте @{handle}"),
        _happ_notice_remark("2. ⏫ Лимиты"),
        _happ_notice_remark("3. Выберите нужный пакет"),
        _happ_notice_remark("⭐ Кстати, Plus — 50 ГБ/мес"),
    ]


def _bypass_limit_notice_uuid(index: int) -> str:
    return f"33333333-3333-3333-3333-{index:012x}"


def happ_bypass_limit_notice_lines(
    used_bytes: int, limit_bytes: int, bot_username: str
) -> list[str]:
    """Plain-text vless-строки для Happ при исчерпании bypass-лимита."""
    from .happ_text_notice import happ_info_notice_vless_uri

    return [
        happ_info_notice_vless_uri(
            title=title,
            uuid=_bypass_limit_notice_uuid(i),
        )
        for i, title in enumerate(
            _bypass_limit_notice_items(used_bytes, limit_bytes, bot_username),
            start=1,
        )
    ]


def happ_bypass_limit_notice_configs(
    used_bytes: int, limit_bytes: int, bot_username: str
) -> list[dict[str, Any]]:
    """Happ JSON-бандл при исчерпании bypass-лимита."""
    return [
        _build_info_notice_config(title, uuid=_bypass_limit_notice_uuid(i))
        for i, title in enumerate(
            _bypass_limit_notice_items(used_bytes, limit_bytes, bot_username),
            start=1,
        )
    ]


def happ_traffic_remaining_config(used_bytes: int, limit_bytes: int) -> dict[str, Any]:
    """Строка «📊 ЛИМИТ: X / Y GiB» в JSON-подписке Happ."""
    used_gb = int(used_bytes) / (1024**3)
    limit_gb = int(limit_bytes) / (1024**3)
    title = f"📊 ЛИМИТ: {used_gb:.2f} / {limit_gb:.0f} GiB"
    dummy = {**_dummy_notice_parsed(), "uuid": "22222222-2222-2222-2222-222222222222"}
    cfg = _build_single_server_config(
        dummy, remarks=title, description="Использование трафика"
    )
    cfg.pop("meta", None)
    return cfg


def _append_bypass_traffic_status_configs(
    configs: list[dict[str, Any]],
    *,
    used_bytes: int,
    limit_bytes: int,
    bot_username: str,
    bypass_exceeded: bool,
    bypass_notices_added: bool,
    limit_status_added: bool,
) -> tuple[bool, bool]:
    """Лимит bypass: 📊 остаток или ⚠️ исчерпан + CTA."""
    if limit_bytes <= 0 or limit_status_added:
        return bypass_notices_added, limit_status_added
    if bypass_exceeded:
        if not bypass_notices_added:
            configs.extend(
                happ_bypass_limit_notice_configs(used_bytes, limit_bytes, bot_username)
            )
            bypass_notices_added = True
        return bypass_notices_added, True
    configs.append(happ_traffic_remaining_config(used_bytes, limit_bytes))
    return bypass_notices_added, True


def generate_happ_configs_list(
    vless_links: list[str],
    server_names: list[str] | None = None,
    *,
    server_is_bypass: list[bool] | None = None,
    bypass_exceeded: bool = False,
    used_bytes: int = 0,
    limit_bytes: int = 0,
    bot_username: str = "SvoyVPN_robot",
) -> list[dict[str, Any]]:
    """
    Список Xray JSON для Happ:
      1) заголовок «Быстрые сервера»
      2) Автовыбор (balancer по всем серверам)
      3) «Быстрые сервера» (один outbound)
      4) Остальные серверы по одному
      5) bypass-секция + лимит / инструкции
    """
    from .traffic import is_fast_section_header, is_free_header_server

    if server_names is None:
        server_names = [""] * len(vless_links)
    if server_is_bypass is None:
        server_is_bypass = [False] * len(vless_links)

    configs: list[dict[str, Any]] = []
    bypass_nav_names: list[str] = []
    for i, link in enumerate(vless_links):
        sname = server_names[i] if i < len(server_names) else ""
        if is_fast_section_header(sname):
            configs.append(_navigation_header_config(sname))
        elif is_free_header_server(sname):
            bypass_nav_names.append(sname)

    real_links: list[tuple[str, dict[str, Any], str, bool, str]] = []
    display_names: list[str] = []
    for i, link in enumerate(vless_links):
        parsed = parse_proxy_link(link)
        if not parsed or _is_fake_link(parsed):
            continue
        sname = server_names[i] if i < len(server_names) else ""
        if is_fast_section_header(sname) or is_free_header_server(sname):
            continue
        is_bp = bool(server_is_bypass[i]) if i < len(server_is_bypass) else False
        # Имя из БД (servers.name) важнее хвоста # в старой ссылке —
        # иначе rename в админке не виден, пока не переписаны vpn_keys.
        remark = (sname.strip() if isinstance(sname, str) and sname.strip() else None) or parsed.get("remark") or f"Server {i+1}"
        real_links.append((link, parsed, remark, is_bp, sname))
        if not is_bp and (parsed.get("protocol") or "vless") == "vless":
            display_names.append(remark)

    if not real_links:
        bypass_notices_added = False
        limit_status_added = False
        if bypass_nav_names:
            for nav_name in bypass_nav_names:
                configs.append(_navigation_header_config(nav_name))
        _append_bypass_traffic_status_configs(
            configs,
            used_bytes=used_bytes,
            limit_bytes=limit_bytes,
            bot_username=bot_username,
            bypass_exceeded=bypass_exceeded,
            bypass_notices_added=bypass_notices_added,
            limit_status_added=limit_status_added,
        )
        return configs

    regular = [(rl, p, r, bp, sn) for rl, p, r, bp, sn in real_links if not bp]
    bypass = [(rl, p, r, bp, sn) for rl, p, r, bp, sn in real_links if bp]
    regular_vless = [x for x in regular if (x[1].get("protocol") or "vless") == "vless"]
    regular_other = [x for x in regular if (x[1].get("protocol") or "vless") != "vless"]

    if regular_vless:
        idx_fast = _fast_server_index([r for _, _, r, _, _ in regular_vless])
        fast = regular_vless[idx_fast]
        tail = [rl for i, rl in enumerate(regular_vless) if i != idx_fast]
        all_vless = [rl[0] for rl in regular_vless]
        fr, fd = presentation_for_server(remark=fast[2], server_name=fast[4], is_bypass=False)
        configs.extend([
            generate_xray_profile(all_vless),
            _build_single_server_config(fast[1], remarks=fr, description=fd),
        ])
        for _link, parsed, remark, is_bp, sname in tail:
            tr, td = presentation_for_server(remark=remark, server_name=sname, is_bypass=False)
            configs.append(_build_single_server_config(parsed, remarks=tr, description=td))

    for _link, parsed, remark, is_bp, sname in regular_other:
        tr, td = presentation_for_server(remark=remark, server_name=sname, is_bypass=False)
        configs.append(_build_single_server_config(parsed, remarks=tr, description=td))

    bypass_notices_added = False
    bypass_nav_added = False
    limit_status_added = False

    def _add_bypass_section_header() -> None:
        nonlocal bypass_nav_added, bypass_notices_added, limit_status_added
        if bypass_nav_added:
            return
        for nav_name in bypass_nav_names:
            configs.append(_navigation_header_config(nav_name))
        bypass_nav_added = True
        bypass_notices_added, limit_status_added = _append_bypass_traffic_status_configs(
            configs,
            used_bytes=used_bytes,
            limit_bytes=limit_bytes,
            bot_username=bot_username,
            bypass_exceeded=bypass_exceeded,
            bypass_notices_added=bypass_notices_added,
            limit_status_added=limit_status_added,
        )

    if not bypass_exceeded:
        for _link, parsed, remark, _is_bp, sname in bypass:
            if not bypass_nav_added:
                _add_bypass_section_header()
            tr, td = presentation_for_server(remark=remark, server_name=sname, is_bypass=True)
            configs.append(_build_single_server_config(parsed, remarks=tr, description=td))
    elif bypass_nav_names:
        _add_bypass_section_header()

    if limit_bytes > 0 and not limit_status_added:
        _append_bypass_traffic_status_configs(
            configs,
            used_bytes=used_bytes,
            limit_bytes=limit_bytes,
            bot_username=bot_username,
            bypass_exceeded=bypass_exceeded,
            bypass_notices_added=bypass_notices_added,
            limit_status_added=limit_status_added,
        )

    return configs


def build_happ_bundle_json_for_keys(
    keys: list[Mapping[str, Any]],
    *,
    bypass_exceeded: bool = False,
    used_bytes: int = 0,
    limit_bytes: int = 0,
    bot_username: str = "SvoyVPN_robot",
    tg_relay_vless_line: str | None = None,
) -> str:
    """Единый JSON-массив профилей Happ (для crypt5). keys — vless_link, server_name, is_bypass."""
    from .traffic import subscription_row_is_bypass

    rows = [k for k in keys if k.get("vless_link")]
    if bypass_exceeded:
        rows = [
            k
            for k in rows
            if not subscription_row_is_bypass(k.get("server_name"), k.get("is_bypass"))
        ]
    vless_links = [str(k["vless_link"]) for k in rows]
    server_names = [str(k.get("server_name") or "") for k in rows]
    server_is_bypass = [
        subscription_row_is_bypass(k.get("server_name"), k.get("is_bypass"))
        for k in rows
    ]
    configs = generate_happ_configs_list(
        vless_links,
        server_names,
        server_is_bypass=server_is_bypass,
        bypass_exceeded=bypass_exceeded,
        used_bytes=used_bytes,
        limit_bytes=limit_bytes,
        bot_username=bot_username,
    )
    if tg_relay_vless_line and bypass_exceeded:
        parsed = parse_vless_link(tg_relay_vless_line.strip())
        if parsed and not _is_fake_link(parsed):
            from .happ_catalog import tg_relay_presentation

            tr, _ = tg_relay_presentation()
            tg_cfg = _build_single_server_config(parsed, remarks=tr, description="")
            tg_cfg.pop("meta", None)
            configs.append(tg_cfg)
    return json.dumps(configs, ensure_ascii=False)


def generate_xray_profile_json(
    vless_links: list[str],
    server_names: list[str] | None = None,
    **kwargs: Any,
) -> str:
    return json.dumps(generate_xray_profile(vless_links, server_names, **kwargs), ensure_ascii=False, indent=2)
