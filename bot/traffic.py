"""
Месячный лимит трафика (ГБ): период по «дню подписки», синхронизация с X-UI, пакеты ГБ.
"""
from __future__ import annotations

import calendar
import json
import logging
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

BYTES_PER_GB = 1024**3


def _safe_int(v: object) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def is_free_server_label(label: object) -> bool:
    """Признак «лимитируемого» сервера: пометка 🆓 или [free]/free в названии."""
    s = str(label or "")
    if "🆓" in s:
        return True
    low = s.lower()
    return "[free]" in low or " free " in f" {low} "


def is_free_header_server(label: object) -> bool:
    """Сервер-заголовок секции 🆓 (содержит «обход белых списков»)."""
    s = str(label or "").lower()
    return "🆓" in str(label or "") and "обход" in s and "белых" in s


def is_fast_section_header(label: object) -> bool:
    """Заголовок секции «🚀 Быстрые сервера 👇» — не подключать."""
    s = str(label or "")
    return "👇" in s and "быстр" in s.lower()


def is_navigation_header_server(label: object) -> bool:
    """Строки-разделители в списке Happ (не пинговать, не использовать в автовыборе)."""
    return is_fast_section_header(label) or is_free_header_server(label)


def navigation_header_vless_line(
    server_name: str,
    *,
    uuid: str = "00000000-0000-0000-0000-000000000000",
) -> str:
    """Нерабочий vless:// для заголовка секции (как лимит/уведомления)."""
    from .happ_catalog import presentation_for_server
    from .happ_text_notice import happ_text_notice_vless_uri

    name = (server_name or "Сервер").strip()
    title, subtitle = presentation_for_server(
        remark=name,
        server_name=name,
        is_bypass=False,
    )
    return happ_text_notice_vless_uri(title=title, subtitle=subtitle, uuid=uuid)


def subscription_row_is_bypass(server_name: object, is_bypass: object) -> bool:
    """Bypass-узел: флаг с панели или 🆓 в названии (кроме заголовка секции)."""
    if is_free_header_server(server_name):
        return False
    if bool(is_bypass):
        return True
    return is_free_server_label(server_name)


def subscription_vless_line(link: str, server_name: str) -> str:
    """Рабочая ссылка или заглушка для навигационного заголовка."""
    if is_navigation_header_server(server_name):
        return navigation_header_vless_line(server_name)
    return link


def _norm_xui_identity(raw: object) -> str:
    """UUID с панели и в БД могут отличаться дефисами — сравниваем в одном виде."""
    return str(raw or "").strip().lower().replace("-", "")


def _client_norm_ids_from_record(c: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k in ("id", "clientId", "password"):
        n = _norm_xui_identity(c.get(k))
        if n and n not in out:
            out.append(n)
    return out


def last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def clamp_anchor_day(year: int, month: int, want: int) -> int:
    return min(max(int(want), 1), last_day_of_month(year, month))


def day_in_month(year: int, month: int, want_day: int) -> date:
    d = clamp_anchor_day(year, month, want_day)
    return date(year, month, d)


def _as_date(val: object) -> date | None:
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if hasattr(val, "date"):
        return val.date()  # type: ignore[union-attr]
    return None


def split_bypass_consumption(
    used_bytes: int,
    base_gb: int,
    pack_remaining_bytes: int,
    pack_purchased_bytes: int | None = None,
    referral_gb: int = 0,
) -> dict[str, int | float]:
    """
    Расход bypass для отображения.
    Остаток пакета хранится в байтах, поэтому небольшой расход больше не
    уничтожает целый гигабайт.
    """
    used = max(int(used_bytes), 0)
    pack_remaining = max(int(pack_remaining_bytes), 0)
    pack_purchased = max(
        int(pack_purchased_bytes if pack_purchased_bytes is not None else pack_remaining),
        0,
    )
    pack_used_bytes = max(0, pack_purchased - pack_remaining)
    referral_bytes = max(int(referral_gb), 0) * BYTES_PER_GB
    base_limit_bytes = max(int(base_gb), 0) * BYTES_PER_GB

    remaining_after_pack = max(0, used - pack_used_bytes)
    referral_used = min(remaining_after_pack, referral_bytes)
    base_used = max(0, remaining_after_pack - referral_used)

    referral_remaining_bytes = max(0, referral_bytes - referral_used)
    base_remaining_bytes = max(0, base_limit_bytes - base_used)

    return {
        "packUsedBytes": pack_used_bytes,
        "referralUsedBytes": referral_used,
        "baseUsedBytes": base_used,
        "packRemainingBytes": pack_remaining,
        "referralRemainingBytes": referral_remaining_bytes,
        "baseRemainingBytes": base_remaining_bytes,
        "packRemainingGb": round(pack_remaining / BYTES_PER_GB, 3),
        "baseUsedGb": round(base_used / BYTES_PER_GB, 3),
    }


def apply_bypass_usage_delta(
    old_used_bytes: int,
    new_used_bytes: int,
    pack_remaining_bytes: int,
) -> int:
    """
    Списать прирост трафика сначала с остатка пакета.
    Возвращает точный остаток пакета в байтах.
    """
    old_used = max(int(old_used_bytes), 0)
    new_used = max(int(new_used_bytes), 0)
    if new_used <= old_used:
        return max(int(pack_remaining_bytes), 0)
    delta = new_used - old_used
    remaining = max(int(pack_remaining_bytes), 0)
    return max(remaining - delta, 0)


def compute_pack_carryover_bytes(pack_remaining_bytes: int) -> int:
    """Точный неиспользованный остаток пакета для переноса на следующий месяц."""
    return max(int(pack_remaining_bytes), 0)


def compute_billing_period(today: date, anchor_day: int) -> tuple[date, date]:
    """
    Период [period_start, period_end_excl) — сброс в день anchor_day каждого месяца.
    Если в месяце нет такого числа — последний день месяца.
    """
    ad = min(max(int(anchor_day), 1), 31)
    y, m = today.year, today.month
    anchor_this = day_in_month(y, m, ad)
    if today >= anchor_this:
        start = anchor_this
    else:
        if m == 1:
            py, pm = y - 1, 12
        else:
            py, pm = y, m - 1
        start = day_in_month(py, pm, ad)

    if start.month == 12:
        ny, nm = start.year + 1, 1
    else:
        ny, nm = start.year, start.month + 1
    end_excl = day_in_month(ny, nm, ad)
    return start, end_excl


def _client_usage_bytes(c: dict[str, Any]) -> int:
    stats = c.get("stats")
    if isinstance(stats, str):
        try:
            stats = json.loads(stats)
        except (json.JSONDecodeError, TypeError):
            stats = {}
    if not isinstance(stats, dict):
        stats = {}

    def pick_up_down(d: dict[str, Any]) -> tuple[int, int]:
        up = _safe_int(d.get("up")) if d.get("up") is not None else _safe_int(d.get("upload"))
        down = _safe_int(d.get("down")) if d.get("down") is not None else _safe_int(d.get("download"))
        return up, down

    up, down = pick_up_down(c)
    if not up and not down:
        up2, down2 = pick_up_down(stats)
        up, down = up or up2, down or down2
    if up or down:
        return up + down

    tot = _safe_int(c.get("total")) if c.get("total") is not None else _safe_int(stats.get("total"))
    return tot


async def get_traffic_settings(conn) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT default_monthly_gb, panel_sync_min_seconds, tg_relay_server_id
        FROM traffic_settings
        ORDER BY id DESC
        LIMIT 1
        """
    )
    if not row:
        return {
            "default_monthly_gb": 50,
            "panel_sync_min_seconds": 240,
            "tg_relay_server_id": None,
        }
    return {
        "default_monthly_gb": int(row["default_monthly_gb"] or 50),
        "panel_sync_min_seconds": int(row["panel_sync_min_seconds"] or 240),
        "tg_relay_server_id": int(row["tg_relay_server_id"])
        if row.get("tg_relay_server_id") is not None
        else None,
    }


def vless_set_fragment_display_name(vless_link: str, display: str) -> str:
    """Имя узла в клиенте — фрагмент после # в vless://…"""
    from urllib.parse import quote

    enc = quote(display, safe="")
    s = vless_link.strip()
    if "#" in s:
        return s.rsplit("#", 1)[0] + "#" + enc
    return s + "#" + enc


async def get_tg_relay_server_id(conn) -> int | None:
    row = await conn.fetchrow(
        """
        SELECT tg_relay_server_id
        FROM traffic_settings
        ORDER BY id DESC
        LIMIT 1
        """
    )
    if not row or row["tg_relay_server_id"] is None:
        return None
    return int(row["tg_relay_server_id"])


async def get_user_tg_relay_vless_line(conn, user_id: int) -> str | None:
    """
    Один рабочий vless для режима «лимит трафика»: сервер из traffic_settings.tg_relay_server_id,
    отображаемое имя «‼️ ТГ БЕЗЛИМИТ ‼️». TG-only настраивается на inbound в панели.
    """
    sid = await get_tg_relay_server_id(conn)
    if sid is None:
        return None
    key = await conn.fetchrow(
        """
        SELECT k.vless_link
        FROM vpn_keys k
        WHERE k.user_id = $1 AND k.server_id = $2 AND k.is_active = TRUE
          AND (k.expires_at IS NULL OR DATE(k.expires_at) >= CURRENT_DATE)
        LIMIT 1
        """,
        user_id,
        sid,
    )
    if not key or not key.get("vless_link"):
        return None
    from .happ_catalog import tg_relay_presentation
    from .happ_text_notice import vless_link_with_happ_caption

    title, subtitle = tg_relay_presentation()
    return vless_link_with_happ_caption(
        str(key["vless_link"]),
        title=title,
        subtitle=subtitle,
        is_tg_relay=True,
    )


async def ensure_user_tg_relay_vless_line(conn, user_id: int) -> str | None:
    """Строка «‼️ ТГ БЕЗЛИМИТ ‼️»; при отсутствии ключа — создаёт и повторяет."""
    line = await get_user_tg_relay_vless_line(conn, user_id)
    if line:
        return line
    sid = await get_tg_relay_server_id(conn)
    if sid is None:
        return None
    from .subscriptions import ensure_user_keys_for_server_ids

    try:
        await ensure_user_keys_for_server_ids(user_id, [sid])
    except Exception:
        return None
    return await get_user_tg_relay_vless_line(conn, user_id)


async def ensure_traffic_anchor_and_period(conn, user_id: int) -> None:
    """Выставить день якоря (первый раз) и при смене календарного периода сбросить учёт."""
    row = await conn.fetchrow(
        """
        SELECT traffic_anchor_day, traffic_period_start, traffic_period_end_excl,
               traffic_used_bytes, traffic_bonus_gb, pay_subscribed, subscription_end
        FROM users WHERE user_id = $1
        """,
        user_id,
    )
    if not row:
        return

    anchor = row["traffic_anchor_day"]
    today = datetime.now().date()
    if anchor is None:
        # Для старых активных пользователей, созданных до внедрения трафика:
        # пытаемся восстановить "день оплаты" из последнего completed платежа.
        is_active = bool(row.get("pay_subscribed")) and row.get("subscription_end") is not None
        if is_active:
            inferred_day = None
            last_paid_ts = await conn.fetchval(
                """
                SELECT timestamp
                FROM payments
                WHERE user_id = $1
                  AND status = 'completed'
                  AND COALESCE(plan_type, '') NOT IN ('gb_pack', 'esim')
                  AND COALESCE(plan_id, '') NOT LIKE 'gb_pack:%'
                  AND COALESCE(plan_id, '') NOT LIKE 'esim:%'
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                user_id,
            )
            if last_paid_ts is not None:
                inferred_day = int(last_paid_ts.day if hasattr(last_paid_ts, "day") else today.day)
            else:
                # Фолбэк: якорим по дню окончания текущей подписки.
                sub_end = row.get("subscription_end")
                if sub_end is not None and hasattr(sub_end, "day"):
                    inferred_day = int(sub_end.day)
                else:
                    inferred_day = int(today.day)

            anchor = min(max(int(inferred_day), 1), 31)
            await conn.execute(
                "UPDATE users SET traffic_anchor_day = $1 WHERE user_id = $2",
                anchor,
                user_id,
            )
        else:
            # Пока нет «дня подписки» (первая оплата / триал) — период и сброс не ведём.
            return

    start, end_excl = compute_billing_period(today, int(anchor))
    stored = row["traffic_period_start"]
    stored_end = row["traffic_period_end_excl"]
    if hasattr(stored, "date"):
        stored = stored.date()
    if hasattr(stored_end, "date"):
        stored_end = stored_end.date()

    if stored is None:
        await conn.execute(
            """
            UPDATE users SET
                traffic_period_start = $1,
                traffic_period_end_excl = $2
            WHERE user_id = $3
            """,
            start,
            end_excl,
            user_id,
        )
    elif stored != start:
        # Сброс только если биллинговый период реально истёк.
        # Иначе просто выравниваем даты (bypass-период мог обновиться воркером раньше).
        period_expired = stored_end is not None and today >= stored_end
        if period_expired:
            await conn.execute(
                """
                UPDATE vpn_keys
                SET traffic_period_baseline_bytes = traffic_lifetime_bytes
                WHERE user_id = $1 AND is_active = TRUE
                """,
                user_id,
            )
            await conn.execute(
                """
                UPDATE users SET
                    traffic_period_start = $1,
                    traffic_period_end_excl = $2,
                    traffic_used_bytes = 0,
                    traffic_bonus_gb = 0,
                    traffic_last_sync_at = NULL
                WHERE user_id = $3
                """,
                start,
                end_excl,
                user_id,
            )
        else:
            await conn.execute(
                """
                UPDATE users SET
                    traffic_period_start = $1,
                    traffic_period_end_excl = $2
                WHERE user_id = $3
                """,
                start,
                end_excl,
                user_id,
            )


async def user_traffic_allowance_bytes(conn, user_id: int) -> tuple[int, int, int]:
    """
    (limit_bytes, bonus_gb, default_gb) — лимит = персональный или дефолт из настроек + бонус пакетов.
    """
    settings = await get_traffic_settings(conn)
    default_gb = int(settings["default_monthly_gb"])
    u = await conn.fetchrow(
        """
        SELECT traffic_limit_gb, traffic_bonus_gb
        FROM users WHERE user_id = $1
        """,
        user_id,
    )
    limit_gb = u["traffic_limit_gb"] if u and u["traffic_limit_gb"] is not None else default_gb
    bonus_gb = int(u["traffic_bonus_gb"] or 0) if u else 0
    total_gb = int(limit_gb) + bonus_gb
    return total_gb * BYTES_PER_GB, bonus_gb, default_gb


async def sync_user_traffic_bytes_from_panels(conn, user_id: int, min_interval_sec: int) -> int:
    """
    Совместимость: актуальный учёт ведёт фоновой воркер (bot/traffic_worker.py),
    который пишет в vpn_keys.traffic_lifetime_bytes и агрегирует
    users.traffic_used_bytes. Здесь просто возвращаем текущее значение из БД.
    """
    _ = min_interval_sec  # не используется, оставлен для обратной совместимости
    row = await conn.fetchrow(
        "SELECT traffic_used_bytes FROM users WHERE user_id = $1",
        user_id,
    )
    if not row:
        return 0
    return int(row["traffic_used_bytes"] or 0)


def subscription_relay_hint_vless(
    bot_username: str | None = None,
    public_site_url: str | None = None,
) -> str:
    """
    Информационная строка подписки (не рабочий узел): «безлимит» для Telegram и сайта,
    чтобы пользователь мог зайти в бота / на сайт и продлить подписку или докупить трафик.
    """
    from urllib.parse import quote, urlparse

    handle = (bot_username or "SvoyVPN_robot").strip().lstrip("@")
    site_host = ""
    if public_site_url:
        u = (public_site_url or "").strip()
        if u and not u.startswith("http"):
            u = "https://" + u.lstrip("/")
        try:
            site_host = (urlparse(u).netloc or "").strip()
            if not site_host:
                site_host = u.replace("https://", "").replace("http://", "").split("/")[0].strip()
        except Exception:
            site_host = ""
    if not site_host:
        site_host = "сайт"
    # Отдельный UUID от «лимит»-строк, чтобы в клиенте выглядело как отдельный пункт.
    name = quote(
        f"Не подключать · TG+{site_host} открываются без VPN · @{handle}",
        safe="",
    )
    fake = (
        "vless://11111111-1111-1111-1111-111111111111@0.0.0.0:1"
        "?type=tcp&security=none&flow=none#"
    )
    return f"{fake}{name}"


def blocked_traffic_vless(
    used_bytes: int,
    limit_bytes: int,
    bot_username: str | None = None,
    public_site_url: str | None = None,
) -> str:
    """Две информационные строки: лимит и как докупить трафик (без лишних «узлов»)."""
    from urllib.parse import quote

    handle = (bot_username or "SvoyVPN_robot").strip().lstrip("@")
    used_g = used_bytes / BYTES_PER_GB
    lim_g = limit_bytes / BYTES_PER_GB
    name1 = quote(f"Лимит трафика ({used_g:.1f}/{lim_g:.0f} ГБ)", safe="")
    name2 = quote(
        f"Увеличить: @{handle} → Подписка → «Увеличить лимит»",
        safe="",
    )
    fake = (
        "vless://00000000-0000-0000-0000-000000000000@0.0.0.0:1"
        "?type=tcp&security=none&flow=none#"
    )
    return f"{fake}{name1}\n{fake}{name2}"


def traffic_remaining_vless(used_bytes: int, limit_bytes: int) -> str:
    """
    Системная строка для клиентов из /sub:
    «X.XX / Y GiB» как отдельный нерабочий VLESS-пункт.
    """
    from urllib.parse import quote

    used_gb = int(used_bytes) / BYTES_PER_GB
    limit_gb = int(limit_bytes) / BYTES_PER_GB
    name = quote(f"📊 ЛИМИТ: {used_gb:.2f} / {limit_gb:.0f} GiB", safe="")
    fake = (
        "vless://22222222-2222-2222-2222-222222222222@0.0.0.0:1"
        "?type=tcp&security=none&flow=none#"
    )
    return f"{fake}{name}"


async def apply_subscription_anchor_on_payment(conn, user_id: int) -> None:
    await conn.execute(
        """
        UPDATE users
        SET traffic_anchor_day = COALESCE(traffic_anchor_day, EXTRACT(DAY FROM CURRENT_DATE)::int)
        WHERE user_id = $1
        """,
        user_id,
    )


def _d(val) -> str | None:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


async def user_traffic_snapshot(conn, user_id: int, *, sync_from_panels: bool) -> dict[str, Any]:
    """
    Состояние месячного трафика для API и /sub.
    При sync_from_panels=True обновляет traffic_used_bytes с панелей (с троттлингом).
    """
    settings = await get_traffic_settings(conn)
    await ensure_traffic_anchor_and_period(conn, user_id)
    if sync_from_panels:
        await sync_user_traffic_bytes_from_panels(
            conn, user_id, int(settings["panel_sync_min_seconds"])
        )

    row = await conn.fetchrow(
        """
        SELECT traffic_anchor_day, traffic_period_start, traffic_period_end_excl,
               traffic_used_bytes, traffic_limit_gb, traffic_bonus_gb
        FROM users WHERE user_id = $1
        """,
        user_id,
    )
    default_gb = int(settings["default_monthly_gb"])
    if not row:
        return {
            "hasAnchor": False,
            "trafficAnchorDay": None,
            "periodStart": None,
            "periodEndExclusive": None,
            "usedBytes": 0,
            "limitBytes": default_gb * BYTES_PER_GB,
            "usedGb": 0.0,
            "limitGb": float(default_gb),
            "bonusGb": 0,
            "defaultMonthlyGb": default_gb,
            "trafficExceeded": False,
            "trafficEnforced": False,
        }

    limit_bytes, bonus_gb, _def = await user_traffic_allowance_bytes(conn, user_id)
    used = int(row["traffic_used_bytes"] or 0)
    anchor = row["traffic_anchor_day"]
    base_lim = row["traffic_limit_gb"]
    base_lim = int(base_lim) if base_lim is not None else default_gb

    enforced = anchor is not None
    exceeded = enforced and limit_bytes > 0 and used >= limit_bytes

    bonus_bytes = bonus_gb * BYTES_PER_GB
    bonus_remaining_gb = max(bonus_gb - round(used / BYTES_PER_GB, 3), 0)
    base_used_bytes = max(used - bonus_bytes, 0)

    return {
        "hasAnchor": enforced,
        "trafficAnchorDay": int(anchor) if anchor is not None else None,
        "periodStart": _d(row["traffic_period_start"]),
        "periodEndExclusive": _d(row["traffic_period_end_excl"]),
        "usedBytes": used,
        "limitBytes": limit_bytes,
        "usedGb": round(used / BYTES_PER_GB, 3),
        "limitGb": round(limit_bytes / BYTES_PER_GB, 3),
        "bonusGb": bonus_gb,
        "bonusRemainingGb": round(bonus_remaining_gb, 3),
        "baseUsedGb": round(base_used_bytes / BYTES_PER_GB, 3),
        "baseLimitGb": base_lim,
        "defaultMonthlyGb": default_gb,
        "trafficExceeded": exceeded,
        "trafficEnforced": enforced,
    }


# ---------------------------------------------------------------------------
# Bypass traffic system (new tier-based)
# ---------------------------------------------------------------------------

def is_bypass_server(label: object) -> bool:
    """Identify bypass servers by name marker or DB flag."""
    s = str(label or "")
    if "🔓" in s:
        return True
    low = s.lower()
    return "[bypass]" in low or " bypass " in f" {low} "


async def _bypass_allowance_parts(conn, user_id: int) -> tuple[int, int, int]:
    """(base_gb, pack_remaining_bytes, referral_gb)."""
    row = await conn.fetchrow(
        """
        SELECT subscription_tier, bypass_traffic_limit_gb,
               COALESCE(
                   bypass_bonus_bytes,
                   COALESCE(bypass_bonus_gb, 0)::bigint * $2
               ) AS pack_bytes,
               COALESCE(referral_bonus_bypass_percent, 0) AS referral_bypass_pct
        FROM users WHERE user_id = $1
        """,
        user_id,
        BYTES_PER_GB,
    )
    if not row:
        return 0, 0, 0

    from .plans import FREE_TIER_ID, get_tier_bypass_gb

    tier = row["subscription_tier"] or FREE_TIER_ID
    base_gb = int(row["bypass_traffic_limit_gb"] or get_tier_bypass_gb(tier))
    pack_bytes = int(row["pack_bytes"] or 0)
    referral_pct = int(row["referral_bypass_pct"] or 0)
    referral_gb = int(base_gb * referral_pct / 100) if referral_pct > 0 else 0
    return base_gb, pack_bytes, referral_gb


async def ensure_bypass_period(conn, user_id: int) -> None:
    """
    Месячный bypass-период по traffic_anchor_day.
    Сброс только когда today >= bypass_period_end_excl (не при каждом расхождении дат).
    При сбросе: месячная база восстанавливается, неиспользованные пакеты переносятся.
    """
    row = await conn.fetchrow(
        """
        SELECT traffic_anchor_day, bypass_period_start, bypass_period_end_excl,
               bypass_traffic_used_bytes,
               COALESCE(
                   bypass_bonus_bytes,
                   COALESCE(bypass_bonus_gb, 0)::bigint * $2
               ) AS pack_bytes
        FROM users WHERE user_id = $1
        """,
        user_id,
        BYTES_PER_GB,
    )
    if not row:
        return

    anchor = row["traffic_anchor_day"]
    if anchor is None:
        return

    today = date.today()
    start, end_excl = compute_billing_period(today, int(anchor))
    stored_start = _as_date(row["bypass_period_start"])
    stored_end = _as_date(row["bypass_period_end_excl"])
    pack_remaining_bytes = int(row["pack_bytes"] or 0)

    if stored_start is None:
        await conn.execute(
            """
            UPDATE users SET
                bypass_period_start = $1,
                bypass_period_end_excl = $2
            WHERE user_id = $3
            """,
            start, end_excl, user_id,
        )
        return

    period_expired = stored_end is not None and today >= stored_end

    if period_expired and start != stored_start:
        carry_pack_bytes = compute_pack_carryover_bytes(pack_remaining_bytes)
        carry_pack_gb = (
            carry_pack_bytes + BYTES_PER_GB - 1
        ) // BYTES_PER_GB
        await conn.execute(
            """
            UPDATE vpn_keys
            SET traffic_period_baseline_bytes = traffic_lifetime_bytes
            WHERE user_id = $1 AND is_active = TRUE
              AND server_id IN (SELECT id FROM servers WHERE is_bypass = TRUE)
            """,
            user_id,
        )
        await conn.execute(
            """
            UPDATE users SET
                bypass_period_start = $1,
                bypass_period_end_excl = $2,
                bypass_traffic_used_bytes = 0,
                bypass_bonus_bytes = $3,
                bypass_pack_purchased_bytes = $3,
                bypass_bonus_gb = $4,
                bypass_pack_purchased_gb = $4,
                bypass_meter_baseline_bytes = 0,
                bypass_meter_period_start = $1,
                bypass_last_sync_at = NULL
            WHERE user_id = $5
            """,
            start, end_excl, carry_pack_bytes, carry_pack_gb, user_id,
        )
        logger.info(
            "bypass period rollover uid=%s carry_pack_bytes=%s period=%s..%s",
            user_id, carry_pack_bytes, start, end_excl,
        )
    elif stored_end != end_excl or stored_start != start:
        # Внутри периода или просроченный end без смены месяца — только выравниваем даты.
        await conn.execute(
            """
            UPDATE users SET
                bypass_period_start = $1,
                bypass_period_end_excl = $2
            WHERE user_id = $3
            """,
            start, end_excl, user_id,
        )


async def user_bypass_allowance_bytes(conn, user_id: int) -> tuple[int, int]:
    """(limit_bytes, pack_purchased_bytes), включая точный перенос."""
    base_gb, pack_remaining_bytes, referral_gb = await _bypass_allowance_parts(conn, user_id)
    row = await conn.fetchrow(
        """
        SELECT COALESCE(
                   bypass_pack_purchased_bytes,
                   COALESCE(bypass_pack_purchased_gb, 0)::bigint * $2
               ) AS pack_purchased_bytes
        FROM users WHERE user_id = $1
        """,
        user_id,
        BYTES_PER_GB,
    )
    pack_purchased_bytes = (
        int(row["pack_purchased_bytes"] or 0) if row else pack_remaining_bytes
    )
    total_bytes = (
        (base_gb + referral_gb) * BYTES_PER_GB
        + pack_purchased_bytes
    )
    return total_bytes, pack_purchased_bytes


async def user_bypass_traffic_snapshot(conn, user_id: int) -> dict[str, Any]:
    """Snapshot of bypass traffic usage for the current period."""
    await ensure_bypass_period(conn, user_id)

    row = await conn.fetchrow(
        """
        SELECT subscription_tier, traffic_anchor_day,
               bypass_period_start, bypass_period_end_excl,
               bypass_traffic_used_bytes, bypass_traffic_limit_gb,
               COALESCE(
                   bypass_bonus_bytes,
                   COALESCE(bypass_bonus_gb, 0)::bigint * $2
               )
                   AS pack_remaining_bytes
        FROM users WHERE user_id = $1
        """,
        user_id,
        BYTES_PER_GB,
    )
    if not row:
        return {
            "tier": "none",
            "bypassUsedBytes": 0,
            "bypassLimitBytes": 0,
            "bypassUsedGb": 0.0,
            "bypassLimitGb": 0.0,
            "bypassBonusGb": 0,
            "bypassExceeded": False,
            "periodStart": None,
            "periodEndExclusive": None,
        }

    from .plans import FREE_TIER_ID, get_tier_bypass_gb

    tier = row["subscription_tier"] or FREE_TIER_ID
    limit_bytes, pack_purchased_bytes = await user_bypass_allowance_bytes(conn, user_id)
    used = int(row["bypass_traffic_used_bytes"] or 0)
    pack_remaining_bytes = int(row["pack_remaining_bytes"] or 0)

    base_gb, _, referral_gb = await _bypass_allowance_parts(conn, user_id)
    if base_gb == 0:
        base_gb = get_tier_bypass_gb(tier)

    split = split_bypass_consumption(
        used, base_gb, pack_remaining_bytes, pack_purchased_bytes, referral_gb
    )
    exceeded = limit_bytes > 0 and used >= limit_bytes

    remaining_gb = max(0, (limit_bytes - used)) / BYTES_PER_GB
    percent_used = (used / limit_bytes * 100) if limit_bytes > 0 else 0

    return {
        "tier": tier,
        "bypassUsedBytes": used,
        "bypassLimitBytes": limit_bytes,
        "bypassUsedGb": round(used / BYTES_PER_GB, 2),
        "bypassLimitGb": round(limit_bytes / BYTES_PER_GB, 2),
        "bypassRemainingGb": round(remaining_gb, 2),
        "bypassBonusGb": round(pack_purchased_bytes / BYTES_PER_GB, 3),
        "bypassPackGb": round(pack_purchased_bytes / BYTES_PER_GB, 3),
        "bypassPackRemainingGb": split["packRemainingGb"],
        "bypassReferralGb": referral_gb,
        "bypassBaseGb": base_gb,
        "bypassBaseUsedGb": split["baseUsedGb"],
        "bypassExceeded": exceeded,
        "bypassPercentUsed": round(percent_used, 1),
        "periodStart": _d(row["bypass_period_start"]),
        "periodEndExclusive": _d(row["bypass_period_end_excl"]),
    }
