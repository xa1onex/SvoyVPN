"""Компактный payload для инвойсов Telegram (лимит длины) — eSIM: страна + код пакета."""
from __future__ import annotations

import base64
import json
import re


def encode_esim_blob(location_code: str, package_code: str) -> str:
    loc = (location_code or "").strip().upper()
    pkg = (package_code or "").strip()
    raw = json.dumps({"l": loc, "p": pkg}, separators=(",", ":"), ensure_ascii=True).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_esim_blob(blob: str) -> tuple[str, str]:
    b = (blob or "").strip()
    pad = "=" * (-len(b) % 4)
    raw = base64.urlsafe_b64decode(b + pad).decode("utf-8")
    o = json.loads(raw)
    return str(o["l"]).upper(), str(o["p"])


def parse_stars_or_yoo_esim_payload(payload: str) -> tuple[str, int, str, str] | None:
    """
    stars_esim.{uid}.{b64}.{ts}_miniapp или yoo_esim.{uid}.{b64}.{ts}_miniapp
    Возвращает (kind, user_id, location, package) или None.
    """
    if not payload:
        return None
    p = payload[:-8] if payload.endswith("_miniapp") else payload
    m = re.match(r"^(stars|yoo)_esim\.(\d+)\.(.+)\.(\d+)$", p)
    if not m:
        return None
    kind, uid_s, b64, _ts = m.groups()
    loc, pkg = decode_esim_blob(b64)
    return kind, int(uid_s), loc, pkg
