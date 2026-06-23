"""
Сборка vless:// из streamSettings inbound (без подмены transport на tcp).
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote


def _parse_json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def canonical_uuid(value: str) -> str:
    """Привести UUID к каноничному 8-4-4-4-12. Без дефисов (32 hex) клиенты вроде
    Happ на macOS отвергают подписку. Xray трактует обе формы одинаково, поэтому
    добавление дефисов не ломает соответствие клиент↔сервер."""
    if not value:
        return value
    raw = str(value).strip()
    hex_only = raw.replace("-", "")
    if len(hex_only) == 32:
        try:
            int(hex_only, 16)
        except ValueError:
            return raw
        return f"{hex_only[0:8]}-{hex_only[8:12]}-{hex_only[12:16]}-{hex_only[16:20]}-{hex_only[20:32]}"
    return raw


def extract_reality_params(stream_settings: dict[str, Any]) -> dict[str, str]:
    """Параметры Reality из streamSettings inbound."""
    reality_settings = stream_settings.get("realitySettings") or {}
    pbk = ""
    sid = ""
    sni = "google.com"
    fp = "chrome"

    if not reality_settings:
        return {"pbk": pbk, "sid": sid, "sni": sni, "fp": fp}

    settings = _parse_json_field(reality_settings.get("settings", {}))
    if not isinstance(settings, dict):
        settings = {}

    pbk = settings.get("publicKey", "") or ""

    sid = reality_settings.get("shortId", "") or ""
    if not sid:
        short_ids = reality_settings.get("shortIds", []) or settings.get("shortIds", [])
        short_ids = _parse_json_field(short_ids)
        if isinstance(short_ids, list) and short_ids:
            sid = str(short_ids[0])
        elif isinstance(short_ids, str) and short_ids:
            sid = short_ids

    sni_list = _parse_json_field(reality_settings.get("serverNames", []))
    if isinstance(sni_list, list) and sni_list:
        sni = str(sni_list[0])
    elif isinstance(sni_list, str) and sni_list:
        sni = sni_list
    sni = str(sni or "").strip().rstrip(":")

    fingerprints = settings.get("fingerprints", []) or reality_settings.get("fingerprints", [])
    fingerprints = _parse_json_field(fingerprints)
    if isinstance(fingerprints, list) and fingerprints:
        fp = str(fingerprints[0])
    elif isinstance(fingerprints, str) and fingerprints:
        fp = fingerprints

    return {"pbk": pbk, "sid": sid, "sni": sni, "fp": fp}


def resolve_listen_ip(
    *,
    chosen_inbound: dict[str, Any],
    public_ip: str | None,
    base_url: str,
) -> str:
    listen_ip = public_ip
    if not listen_ip:
        listen_ip = chosen_inbound.get("listen") or ""
        if not listen_ip or listen_ip in ("0.0.0.0", "127.0.0.1", "localhost"):
            url_part = base_url.split("//")[-1].split("/")[0]
            listen_ip = url_part.split(":")[0]
    return listen_ip or "127.0.0.1"


def client_flow_for_network(network: str) -> str:
    """Flow только для tcp+reality (vision); grpc/ws — пусто."""
    return "xtls-rprx-vision" if network == "tcp" else ""


def build_vless_link(
    *,
    client_uuid: str,
    listen_ip: str,
    port: int | str,
    stream_settings: dict[str, Any],
    display_name: str,
) -> str:
    network = (stream_settings.get("network") or "tcp").lower()
    security = (stream_settings.get("security") or "none").lower()

    params: list[str] = [f"type={network}", "encryption=none"]
    if security and security != "none":
        params.append(f"security={security}")

    if security == "reality":
        rp = extract_reality_params(stream_settings)
        if rp["pbk"]:
            params.append(f"pbk={rp['pbk']}")
        params.append(f"fp={rp['fp']}")
        params.append(f"sni={rp['sni']}")
        params.append(f"sid={rp['sid'] or '3d'}")
        if network == "tcp":
            params.append("spx=%2F")

    if network == "grpc":
        grpc_settings = stream_settings.get("grpcSettings") or {}
        service_name = grpc_settings.get("serviceName")
        if service_name is not None:
            params.append(f"serviceName={quote(str(service_name), safe='')}")
        if grpc_settings.get("multiMode"):
            params.append("mode=multi")
        else:
            params.append("mode=gun")

    flow = client_flow_for_network(network)
    if flow:
        params.append(f"flow={flow}")

    query = "&".join(params)
    name = quote(display_name or "VPN", safe="")
    return f"vless://{canonical_uuid(client_uuid)}@{listen_ip}:{port}/?{query}#{name}"
