"""Статичные узлы без панели (общий URI для всех пользователей)."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote


YOUTUBE_ADFREE_NAME = "🇷🇺 YouTube без рекламы"
YOUTUBE_ADFREE_HOST = "xdoublegroup.online"
YOUTUBE_ADFREE_PORT = 443
YOUTUBE_ADFREE_AUTH = "12753a53-40aa-4b41-b77c-4b9c8e5d289a"


def is_static_server(server: Mapping[str, Any] | None) -> bool:
    if not server:
        return False
    return (server.get("panel_type") or "").strip().lower() == "static"


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
    sn = (sni or host or "").strip() or host
    fragment = quote((remark or "").strip() or host, safe="")
    return (
        f"hysteria2://{auth}@{host}:{int(port)}/"
        f"?sni={quote(sn, safe='')}&alpn={quote(alpn, safe='')}&fp={quote(fingerprint, safe='')}"
        f"#{fragment}"
    )


def youtube_adfree_link(remark: str = YOUTUBE_ADFREE_NAME) -> str:
    return build_hysteria2_link(
        auth=YOUTUBE_ADFREE_AUTH,
        host=YOUTUBE_ADFREE_HOST,
        port=YOUTUBE_ADFREE_PORT,
        sni=YOUTUBE_ADFREE_HOST,
        remark=remark,
    )


def static_link_for_server(server: Mapping[str, Any]) -> str:
    """URI из base_url (полный) или сборка из ip/password/port."""
    base = str(server.get("base_url") or "").strip()
    if base.startswith(("hysteria2://", "hy2://")):
        return base
    auth = str(server.get("password") or "").strip()
    host = str(server.get("ip") or "").strip()
    port = int(server.get("port") or 443)
    name = str(server.get("name") or "").strip()
    if auth and host:
        return build_hysteria2_link(auth=auth, host=host, port=port, sni=host, remark=name)
    return base
