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

from .routing_rules import RU_BYPASS_DOMAINS
from .happ_catalog import autoselect_presentation, presentation_for_server

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

        return {
            "uuid": uuid_part,
            "address": address,
            "port": int(port_str),
            "remark": fragment,
            "security": params.get("security", ["none"])[0],
            "type": params.get("type", ["tcp"])[0],
            "flow": params.get("flow", [""])[0],
            "sni": params.get("sni", [""])[0],
            "pbk": params.get("pbk", [""])[0],
            "sid": params.get("sid", [""])[0],
            "fp": params.get("fp", ["chrome"])[0],
            "spx": params.get("spx", [""])[0],
            "serviceName": params.get("serviceName", [None])[0],
        }
    except Exception:
        return None


def _is_fake_link(parsed: dict[str, Any]) -> bool:
    if parsed["address"] == "0.0.0.0":
        return True
    uuid_clean = parsed["uuid"].replace("-", "")
    return len(set(uuid_clean)) <= 2


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
        ss["tlsSettings"] = {
            "serverName": parsed.get("sni", ""),
            "fingerprint": parsed.get("fp", "chrome"),
            "allowInsecure": False,
        }

    if net == "tcp":
        ss["tcpSettings"] = {}
    elif net == "grpc":
        ss["grpcSettings"] = {"authority": "", "mode": False, "serviceName": parsed.get("serviceName") or None}
    elif net == "ws":
        ss["wsSettings"] = {"path": parsed.get("spx") or "/", "headers": {}}

    return ss


def _build_vless_outbound(parsed: dict[str, Any], tag: str) -> dict[str, Any]:
    return {
        "protocol": "vless",
        "settings": {"vnext": [{"address": parsed["address"], "port": parsed["port"],
                                 "users": [{"encryption": "none", "flow": parsed.get("flow", ""), "id": parsed["uuid"]}]}]},
        "streamSettings": _build_stream(parsed),
        "tag": tag,
    }


# ---------------------------------------------------------------------------
# Single-server config (one entry in Happ UI)
# ---------------------------------------------------------------------------

def _build_single_server_config(parsed: dict[str, Any], remarks: str, description: str = "") -> dict[str, Any]:
    """Xray JSON config for ONE server — appears as one entry in Happ."""
    rules = list(_BASE_RULES)
    ui_description = clamp_happ_server_description(description)
    final_remarks = _happ_row_remarks(remarks, ui_description)
    return {
        "dns": _DNS,
        "inbounds": _INBOUNDS,
        "log": {"loglevel": "warning"},
        "outbounds": [
            _build_vless_outbound(parsed, "proxy"),
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "block"},
        ],
        "policy": _POLICY,
        "routing": {
            "domainMatcher": "hybrid",
            "domainStrategy": "IPIfNonMatch",
            "rules": rules,
        },
        "stats": {},
        "remarks": final_remarks,
        "meta": {"serverDescription": ui_description},
    }


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

    rules = list(_BASE_RULES)
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


def happ_bypass_limit_notice_configs(used_bytes: int, limit_bytes: int, bot_username: str) -> list[dict[str, Any]]:
    """
    Две записи в конец подписки Happ: лимит исчерпан + CTA на бота.
    Текст: «⚠️ Лимит исчерпан(11.2/10 ГБ)» и «📈 Увеличить @…».
    """
    used_gb = used_bytes / (1024**3)
    lim_gb = limit_bytes / (1024**3)
    lim_s = f"{lim_gb:.0f}" if abs(lim_gb - round(lim_gb)) < 1e-6 else f"{lim_gb:.1f}"
    r1 = f"⚠️ Лимит исчерпан({used_gb:.1f}/{lim_s} ГБ)"
    handle = (bot_username or "SvoyVPN_robot").lstrip("@")
    r2 = f"📈 Увеличить @{handle}"
    dummy = _dummy_notice_parsed()
    return [
        _build_single_server_config(dummy, remarks=r1, description="Лимит трафика исчерпан"),
        _build_single_server_config(dummy, remarks=r2, description="Открой Telegram для увеличения лимита"),
    ]


def generate_happ_configs_list(
    vless_links: list[str],
    server_names: list[str] | None = None,
    *,
    server_is_bypass: list[bool] | None = None,
) -> list[dict[str, Any]]:
    """
    Список Xray JSON для Happ:
      1) «Быстрые сервера» (один outbound)
      2) Автовыбор (balancer по всем серверам)
      3) Остальные серверы по одному
    """
    if server_names is None:
        server_names = [""] * len(vless_links)
    if server_is_bypass is None:
        server_is_bypass = [False] * len(vless_links)

    real_links: list[tuple[str, dict[str, Any], str, bool, str]] = []
    display_names: list[str] = []
    for i, link in enumerate(vless_links):
        parsed = parse_vless_link(link)
        if not parsed or _is_fake_link(parsed):
            continue
        sname = server_names[i] if i < len(server_names) else ""
        is_bp = bool(server_is_bypass[i]) if i < len(server_is_bypass) else False
        remark = parsed.get("remark") or sname or f"Server {i+1}"
        real_links.append((link, parsed, remark, is_bp, sname))
        display_names.append(remark)

    if not real_links:
        return []

    idx_fast = _fast_server_index(display_names)
    fast = real_links[idx_fast]
    tail = [rl for i, rl in enumerate(real_links) if i != idx_fast]

    all_vless = [rl[0] for rl in real_links]
    fr, fd = presentation_for_server(remark=fast[2], server_name=fast[4], is_bypass=fast[3])

    configs: list[dict[str, Any]] = [
        _build_single_server_config(fast[1], remarks=fr, description=fd),
        generate_xray_profile(all_vless),
    ]
    for _link, parsed, remark, is_bp, sname in tail:
        tr, td = presentation_for_server(remark=remark, server_name=sname, is_bypass=is_bp)
        configs.append(_build_single_server_config(parsed, remarks=tr, description=td))

    return configs


def build_happ_bundle_json_for_keys(
    keys: list[Mapping[str, Any]],
    *,
    bypass_exceeded: bool = False,
    used_bytes: int = 0,
    limit_bytes: int = 0,
    bot_username: str = "SvoyVPN_robot",
) -> str:
    """Единый JSON-массив профилей Happ (для crypt5). keys — vless_link, server_name, is_bypass."""
    rows = [k for k in keys if k.get("vless_link")]
    if bypass_exceeded:
        rows = [k for k in rows if not k.get("is_bypass")]
    vless_links = [str(k["vless_link"]) for k in rows]
    server_names = [str(k.get("server_name") or "") for k in rows]
    server_is_bypass = [bool(k.get("is_bypass")) for k in rows]
    configs = generate_happ_configs_list(
        vless_links, server_names, server_is_bypass=server_is_bypass
    )
    if bypass_exceeded and limit_bytes > 0:
        configs.extend(
            happ_bypass_limit_notice_configs(used_bytes, limit_bytes, bot_username)
        )
    return json.dumps(configs, ensure_ascii=False)


def generate_xray_profile_json(
    vless_links: list[str],
    server_names: list[str] | None = None,
    **kwargs: Any,
) -> str:
    return json.dumps(generate_xray_profile(vless_links, server_names, **kwargs), ensure_ascii=False, indent=2)
