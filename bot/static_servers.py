"""Статичные узлы без панели (общий URI для всех пользователей)."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote


YOUTUBE_ADFREE_NAME = "🇷🇺 Россия | YouTube без рекламы"
YOUTUBE_ADFREE_HOST = "104.171.131.174"
YOUTUBE_ADFREE_LEGACY_HOSTS = (
    "104.171.131.174",
    "ru-1-67.cdnquanta.com",
)
YOUTUBE_ADFREE_PORT = 443
YOUTUBE_ADFREE_UUID = "a215f8d3-fc76-49a9-8762-010483ba94fe"
YOUTUBE_ADFREE_SNI = "unnftuqcfa.medved.app"
YOUTUBE_ADFREE_FP = "firefox"
YOUTUBE_ADFREE_FLOW = "xtls-rprx-vision"
YOUTUBE_ADFREE_ALPN = "h2,http/1.1"
YOUTUBE_ADFREE_DESCRIPTION = "для RU сервисов и YouTube"

# backward-compat (старый Reality/gRPC exit)
YOUTUBE_ADFREE_LEGACY_HOST = "ru-1-67.cdnquanta.com"
YOUTUBE_ADFREE_PBK = ""
YOUTUBE_ADFREE_SID = ""

# backward-compat alias
YOUTUBE_ADFREE_AUTH = YOUTUBE_ADFREE_UUID


def is_static_server(server: Mapping[str, Any] | None) -> bool:
    if not server:
        return False
    return (server.get("panel_type") or "").strip().lower() == "static"


def is_youtube_adfree_host(address: object) -> bool:
    host = str(address or "").strip()
    return host in YOUTUBE_ADFREE_LEGACY_HOSTS or host == YOUTUBE_ADFREE_HOST


def youtube_adfree_link(remark: str = YOUTUBE_ADFREE_NAME) -> str:
    """VLESS+TCP+TLS+Vision exit (RU / YouTube без рекламы)."""
    fragment = quote((remark or "").strip() or YOUTUBE_ADFREE_NAME, safe="")
    return (
        f"vless://{YOUTUBE_ADFREE_UUID}@{YOUTUBE_ADFREE_HOST}:{YOUTUBE_ADFREE_PORT}"
        f"?encryption=none"
        f"&flow={quote(YOUTUBE_ADFREE_FLOW, safe='')}"
        f"&type=tcp&security=tls"
        f"&sni={quote(YOUTUBE_ADFREE_SNI, safe='')}"
        f"&fp={quote(YOUTUBE_ADFREE_FP, safe='')}"
        f"&alpn={quote(YOUTUBE_ADFREE_ALPN, safe='')}"
        f"#{fragment}"
    )


def build_hysteria2_link(
    *,
    auth: str,
    host: str,
    port: int = 443,
    sni: str | None = None,
    alpn: str = "h3",
    fingerprint: str = "chrome",
    remark: str = "",
) -> str:
    """Legacy HY2 helper (не используется для YouTube exit)."""
    sn = (sni or host or "").strip() or host
    fragment = quote((remark or "").strip() or host, safe="")
    return (
        f"hysteria2://{auth}@{host}:{int(port)}/"
        f"?sni={quote(sn, safe='')}&alpn={quote(alpn, safe='')}&fp={quote(fingerprint, safe='')}"
        f"#{fragment}"
    )


def static_link_for_server(server: Mapping[str, Any]) -> str:
    """URI из base_url (полный) или сборка youtube VLESS."""
    base = str(server.get("base_url") or "").strip()
    if base.startswith(("vless://", "hysteria2://", "hy2://")):
        return base
    host = str(server.get("ip") or "").strip()
    name = str(server.get("name") or "").strip()
    if is_youtube_adfree_host(host) or "youtube" in name.lower() or "ютуб" in name.lower():
        return youtube_adfree_link(remark=name or YOUTUBE_ADFREE_NAME)
    return base
