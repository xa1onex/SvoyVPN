"""
Plain-text подписка для Happ (native nodes): vless:// с #title?serverDescription=…

Порядок:
  заголовок «Быстрые» → автовыбор → быстрый узел → страны → bypass (если лимит ок)
  → заголовок «🆓 обход» → (инструкция при лимите | 📊 ЛИМИТ GiB) → ТГ при лимите.
"""

from __future__ import annotations

from typing import Any, Mapping

from .happ_catalog import autoselect_presentation, presentation_for_server
from .happ_proxy_api import happ_vless_body_prefix
from .happ_text_notice import vless_link_title_only, vless_link_with_happ_caption
from .profile_generator import (
    _fast_server_index,
    happ_bypass_limit_notice_lines,
    parse_proxy_link,
)
from .traffic import (
    is_fast_section_header,
    is_free_header_server,
    is_navigation_header_server,
    navigation_header_vless_line,
    subscription_relay_hint_vless,
    subscription_row_is_bypass,
    traffic_remaining_vless,
)


def _row_remark(row: Mapping[str, Any]) -> str:
    """Название в UI: сначала servers.name, иначе # из ссылки."""
    sname = str(row.get("server_name") or "").strip()
    if sname:
        return sname
    link = str(row.get("vless_link") or "")
    parsed = parse_proxy_link(link)
    if parsed and parsed.get("remark"):
        return str(parsed["remark"])
    return ""


def build_happ_plain_subscription_body(
    keys: list[Mapping[str, Any]],
    *,
    is_active: bool,
    bypass_exceeded: bool = False,
    used_bytes: int = 0,
    limit_bytes: int = 0,
    bot_username: str = "SvoyVPN_robot",
    cta_name: str | None = None,
    site_url: str | None = None,
    tg_relay_line: str | None = None,
    tg_relay_server_id: int | None = None,
) -> str:
    """Текст подписки Happ."""
    parts: list[str] = []
    prefix = happ_vless_body_prefix().strip()
    if prefix:
        parts.extend(prefix.splitlines())

    if not is_active:
        hint = subscription_relay_hint_vless(cta_name, site_url)
        for k in keys:
            link = k.get("vless_link")
            if link:
                parts.append(str(link))
        if hint:
            parts.append(hint)
        return "\n".join(p for p in parts if p)

    def _not_tg_relay(row: Mapping[str, Any]) -> bool:
        if tg_relay_server_id is None:
            return True
        return int(row.get("server_id") or 0) != int(tg_relay_server_id)

    all_rows = [dict(k) for k in keys if k.get("vless_link") and _not_tg_relay(k)]
    regular_rows = [
        r
        for r in all_rows
        if not subscription_row_is_bypass(r.get("server_name"), r.get("is_bypass"))
    ]
    bypass_rows = [
        r
        for r in all_rows
        if subscription_row_is_bypass(r.get("server_name"), r.get("is_bypass"))
    ]

    if not regular_rows and not bypass_rows:
        if bypass_exceeded and limit_bytes > 0:
            parts.extend(happ_bypass_limit_notice_lines(used_bytes, limit_bytes, bot_username))
        if tg_relay_line and bypass_exceeded:
            parts.append(tg_relay_line)
        return "\n".join(p for p in parts if p)

    fast_nav_rows = [
        r for r in regular_rows if is_fast_section_header(r.get("server_name"))
    ]
    main_rows = [
        r
        for r in regular_rows
        if not is_navigation_header_server(r.get("server_name"))
    ]
    header_rows = [
        r for r in regular_rows if is_free_header_server(r.get("server_name"))
    ]

    for row in fast_nav_rows:
        sname = str(row.get("server_name") or _row_remark(row))
        parts.append(navigation_header_vless_line(sname))

    if main_rows:
        display_names = [_row_remark(r) for r in main_rows]
        idx_fast = _fast_server_index(display_names)
        fast_row = main_rows[idx_fast]
        auto_title, auto_sub = autoselect_presentation()

        parts.append(
            vless_link_with_happ_caption(
                str(fast_row["vless_link"]),
                title=auto_title,
                subtitle=auto_sub,
            )
        )
        fr, fd = presentation_for_server(
            remark=_row_remark(fast_row),
            server_name=str(fast_row.get("server_name") or ""),
            is_bypass=False,
            is_tg_relay=bool(fast_row.get("is_tg_relay")),
        )
        parts.append(
            vless_link_with_happ_caption(
                str(fast_row["vless_link"]),
                remark=_row_remark(fast_row),
                server_name=str(fast_row.get("server_name") or ""),
                is_bypass=False,
                is_tg_relay=bool(fast_row.get("is_tg_relay")),
                title=fr,
                subtitle=fd,
            )
        )

        for i, row in enumerate(main_rows):
            if i == idx_fast:
                continue
            sname = str(row.get("server_name") or "")
            parts.append(
                vless_link_with_happ_caption(
                    str(row["vless_link"]),
                    remark=_row_remark(row),
                    server_name=sname,
                    is_bypass=False,
                    is_tg_relay=bool(row.get("is_tg_relay")),
                )
            )

    if not bypass_exceeded:
        for row in bypass_rows:
            sname = str(row.get("server_name") or "")
            parts.append(
                vless_link_with_happ_caption(
                    str(row["vless_link"]),
                    remark=_row_remark(row),
                    server_name=sname,
                    is_bypass=True,
                    is_tg_relay=bool(row.get("is_tg_relay")),
                )
            )

    limit_notices_added = False
    for row in header_rows:
        sname = str(row.get("server_name") or _row_remark(row))
        parts.append(navigation_header_vless_line(sname))
        if bypass_exceeded and limit_bytes > 0:
            parts.extend(happ_bypass_limit_notice_lines(used_bytes, limit_bytes, bot_username))
            limit_notices_added = True
        elif limit_bytes > 0:
            parts.append(traffic_remaining_vless(used_bytes, limit_bytes))

    if bypass_exceeded and limit_bytes > 0 and not limit_notices_added:
        parts.extend(happ_bypass_limit_notice_lines(used_bytes, limit_bytes, bot_username))
    elif limit_bytes > 0 and not limit_notices_added:
        parts.append(traffic_remaining_vless(used_bytes, limit_bytes))

    if tg_relay_line and bypass_exceeded:
        from .happ_catalog import tg_relay_presentation

        tr, _ = tg_relay_presentation()
        parts.append(vless_link_title_only(tg_relay_line, title=tr))

    return "\n".join(p for p in parts if p)
