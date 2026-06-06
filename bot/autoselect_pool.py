"""
Пул узлов и разметка outbounds для профиля «Автовыбор» (leastLoad + burstObservatory).

Цели: 8–15 smart-* outbounds, diversity по IP/SNI/провайдеру/транспорту,
отдельные fallback proxy-fast / proxy-stable / proxy-cdn, tolerance 0.1.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Any


def _parse_vless(link: str) -> dict[str, Any] | None:
    from .profile_generator import parse_vless_link

    return parse_vless_link(link)


def _is_fake(parsed: dict[str, Any]) -> bool:
    from .profile_generator import _is_fake_link

    return _is_fake_link(parsed)

# --- tunables (env) ---
AUTO_SMART_MIN = max(1, int(os.getenv("SVOYVPN_AUTO_SMART_MIN", "8")))
AUTO_SMART_MAX = max(AUTO_SMART_MIN, int(os.getenv("SVOYVPN_AUTO_SMART_MAX", "15")))
AUTO_BALANCER_TOLERANCE = float(os.getenv("SVOYVPN_AUTO_BALANCER_TOLERANCE", "0.1"))
# Xray/Happ шлют HTTP HEAD/GET на URL; рабочий дефолт — cloudflare (http, не https).
AUTO_OBSERVATORY_URL = (
    os.getenv("SVOYVPN_OBSERVATORY_URL", "http://cp.cloudflare.com/generate_204").strip()
    or "http://cp.cloudflare.com/generate_204"
)

_PROVIDER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("hetzner", re.compile(r"hetzner|hx\d+", re.I)),
    ("ovh", re.compile(r"\bovh\b|kimsufi|soyoustart", re.I)),
    ("yandex", re.compile(r"yandex|yandexcloud|\byc\b", re.I)),
    ("aeza", re.compile(r"aeza|pq\.host", re.I)),
    ("timeweb", re.compile(r"timeweb|twcloud", re.I)),
    ("selectel", re.compile(r"selectel", re.I)),
    ("cdn", re.compile(r"cdn\d*|terem\.live|\.live\b|cloudfront|fastly", re.I)),
]

_CDN_HOST_RE = re.compile(
    r"(cdn\d*|terem\.live|\.live\b|cloudfront|akamai|fastly|edgekey)",
    re.I,
)


@dataclass
class AutoselectCandidate:
    link: str
    parsed: dict[str, Any]
    remark: str
    server_name: str
    is_bypass: bool
    address: str
    sni: str
    provider: str
    is_cdn: bool
    is_hostname: bool
    transport: str  # grpc_reality | grpc | reality | tcp_tls | other
    score: int = 0


@dataclass
class AutoselectLayout:
    """Результат разборки пула для generate_xray_profile."""

    smart: list[AutoselectCandidate] = field(default_factory=list)
    proxy_fast: AutoselectCandidate | None = None
    proxy_stable: AutoselectCandidate | None = None
    proxy_cdn: AutoselectCandidate | None = None
    smart_tags: list[str] = field(default_factory=list)
    fallback_tag: str = "proxy-stable"
    balancer_selector: list[str] = field(default_factory=lambda: ["smart"])


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address((host or "").strip())
        return True
    except ValueError:
        return False


def _host_key(parsed: dict[str, Any]) -> str:
    sni = (parsed.get("sni") or "").strip().lower()
    addr = (parsed.get("address") or "").strip().lower()
    if sni and not _is_ip(sni):
        return sni
    if addr and not _is_ip(addr):
        return addr
    return addr or sni or ""


def _detect_provider(blob: str) -> str:
    for name, pat in _PROVIDER_PATTERNS:
        if pat.search(blob):
            return name
    return "other"


def _classify_transport(parsed: dict[str, Any]) -> str:
    net = (parsed.get("type") or "tcp").lower()
    sec = (parsed.get("security") or "none").lower()
    if net == "grpc" and sec == "reality":
        return "grpc_reality"
    if net == "grpc":
        return "grpc"
    if sec == "reality":
        return "reality"
    if sec == "tls":
        return "tcp_tls"
    return "other"


def _transport_score(transport: str) -> int:
    return {
        "grpc_reality": 100,
        "grpc": 75,
        "reality": 65,
        "tcp_tls": 45,
        "other": 20,
    }.get(transport, 10)


def _is_cdn_candidate(parsed: dict[str, Any], server_name: str, remark: str) -> bool:
    blob = " ".join(
        [
            parsed.get("address") or "",
            parsed.get("sni") or "",
            server_name or "",
            remark or "",
        ]
    )
    if _CDN_HOST_RE.search(blob):
        return True
    addr = parsed.get("address") or ""
    sni = parsed.get("sni") or ""
    return (bool(addr) and not _is_ip(addr)) or (bool(sni) and not _is_ip(sni))


def _build_candidate(
    link: str,
    *,
    server_name: str = "",
    is_bypass: bool = False,
) -> AutoselectCandidate | None:
    parsed = _parse_vless(link)
    if not parsed or _is_fake(parsed):
        return None
    remark = (parsed.get("remark") or server_name or "").strip()
    addr = (parsed.get("address") or "").strip()
    sni = (parsed.get("sni") or "").strip()
    blob = f"{server_name} {remark} {addr} {sni}"
    transport = _classify_transport(parsed)
    is_cdn = _is_cdn_candidate(parsed, server_name, remark)
    is_hostname = bool(addr) and not _is_ip(addr)
    provider = _detect_provider(blob)
    if is_cdn and provider == "other":
        provider = "cdn"

    score = _transport_score(transport)
    if is_cdn:
        score += 35
    if is_hostname:
        score += 15
    if provider in ("hetzner", "ovh", "yandex", "selectel"):
        score += 10

    return AutoselectCandidate(
        link=link,
        parsed=parsed,
        remark=remark,
        server_name=server_name,
        is_bypass=is_bypass,
        address=addr,
        sni=sni,
        provider=provider,
        is_cdn=is_cdn,
        is_hostname=is_hostname,
        transport=transport,
        score=score,
    )


def collect_autoselect_candidates(
    vless_links: list[str],
    server_names: list[str] | None = None,
    *,
    server_is_bypass: list[bool] | None = None,
) -> list[AutoselectCandidate]:
    if server_names is None:
        server_names = [""] * len(vless_links)
    if server_is_bypass is None:
        server_is_bypass = [False] * len(vless_links)

    out: list[AutoselectCandidate] = []
    seen_addr: set[str] = set()
    for i, link in enumerate(vless_links):
        if not link:
            continue
        sname = server_names[i] if i < len(server_names) else ""
        is_bp = bool(server_is_bypass[i]) if i < len(server_is_bypass) else False
        c = _build_candidate(link, server_name=sname, is_bypass=is_bp)
        if not c:
            continue
        # один ключ на адрес в пуле кандидатов (лучший score)
        key = c.address.lower()
        if key in seen_addr:
            existing = next(x for x in out if x.address.lower() == key)
            if c.score > existing.score:
                out.remove(existing)
            else:
                continue
        seen_addr.add(key)
        out.append(c)
    out.sort(key=lambda x: x.score, reverse=True)
    return out


def _pick_best(
    pool: list[AutoselectCandidate],
    *,
    predicate,
    used: set[int],
) -> AutoselectCandidate | None:
    for i, c in enumerate(pool):
        if i in used or not predicate(c):
            continue
        used.add(i)
        return c
    return None


def _diversity_key(c: AutoselectCandidate) -> tuple[str, str, str]:
    return (c.address.lower(), c.provider, _host_key(c.parsed))


def build_autoselect_layout(
    candidates: list[AutoselectCandidate],
    *,
    smart_min: int = AUTO_SMART_MIN,
    smart_max: int = AUTO_SMART_MAX,
) -> AutoselectLayout:
    layout = AutoselectLayout()
    if not candidates:
        return layout

    pool = list(candidates)
    used: set[int] = set()
    effective_min = min(smart_min, len(pool)) if pool else 0

    layout.proxy_fast = _pick_best(
        pool,
        predicate=lambda c: c.transport == "grpc_reality" and not c.is_cdn,
        used=used,
    ) or _pick_best(pool, predicate=lambda c: c.transport in ("grpc_reality", "grpc"), used=used)

    layout.proxy_stable = _pick_best(
        pool,
        predicate=lambda c: c.provider in ("hetzner", "ovh", "yandex", "selectel", "aeza")
        and c.transport in ("grpc_reality", "reality", "grpc", "tcp_tls"),
        used=used,
    ) or _pick_best(pool, predicate=lambda c: not c.is_cdn, used=used)

    layout.proxy_cdn = _pick_best(
        pool,
        predicate=lambda c: c.is_cdn or c.is_hostname,
        used=used,
    ) or _pick_best(pool, predicate=lambda c: True, used=used)

    # smart pool: greedy diversity
    smart: list[AutoselectCandidate] = []
    seen_div: set[tuple[str, str, str]] = set()
    seen_providers: set[str] = set()

    def _try_add(idx: int) -> bool:
        if idx in used or len(smart) >= smart_max:
            return False
        c = pool[idx]
        dk = _diversity_key(c)
        if dk in seen_div and len(smart) >= effective_min:
            return False
        used.add(idx)
        smart.append(c)
        seen_div.add(dk)
        seen_providers.add(c.provider)
        return True

    # сначала лучшие по score с diversity
    for i, c in enumerate(pool):
        if len(smart) >= smart_max:
            break
        if i in used:
            continue
        dk = _diversity_key(c)
        if dk not in seen_div or c.provider not in seen_providers:
            _try_add(i)

    # добрать до effective_min любыми оставшимися
    for i, c in enumerate(pool):
        if len(smart) >= smart_max:
            break
        if i not in used:
            _try_add(i)

    # если узлов мало — добираем уникальные из pool (без дублей адреса в smart)
    if len(smart) < effective_min and pool:
        for c in pool:
            if len(smart) >= effective_min:
                break
            if c not in smart:
                smart.append(c)

    layout.smart = smart[:smart_max]
    layout.smart_tags = ["smart"] + [f"smart-{n}" for n in range(2, len(layout.smart) + 1)]
    # Happ/Xray ожидают fallbackTag «proxy» (как в рабочем конфиге до рефакторинга).
    layout.fallback_tag = "proxy"
    # selector «smart» — prefix match в Xray (smart, smart-2, …)
    layout.balancer_selector = ["smart"]
    return layout
