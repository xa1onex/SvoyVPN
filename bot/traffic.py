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


async def get_traffic_settings(conn) -> dict[str, int]:
    row = await conn.fetchrow(
        """
        SELECT default_monthly_gb, panel_sync_min_seconds
        FROM traffic_settings
        ORDER BY id DESC
        LIMIT 1
        """
    )
    if not row:
        return {"default_monthly_gb": 50, "panel_sync_min_seconds": 240}
    return {
        "default_monthly_gb": int(row["default_monthly_gb"] or 50),
        "panel_sync_min_seconds": int(row["panel_sync_min_seconds"] or 240),
    }


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
    if hasattr(stored, "date"):
        stored = stored.date()
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
        # Смена биллингового периода: фиксируем baseline на всех активных
        # ключах пользователя как текущий lifetime — с этого момента
        # трафик периода начинается с нуля.
        await conn.execute(
            """
            UPDATE vpn_keys
            SET traffic_period_baseline_bytes = traffic_lifetime_bytes
            WHERE user_id = $1 AND is_active = TRUE
            """,
            user_id,
        )
        # Сброс только расхода периода. Докупленные ГБ (traffic_bonus_gb) НЕ обнуляем здесь:
        # они «живут» до конца активной подписки и сгорают при её окончании
        # (см. handle_expired_subscriptions). Иначе пользователь теряет оплаченный
        # пакет, если купил в конце расчётного месяца.
        await conn.execute(
            """
            UPDATE users SET
                traffic_period_start = $1,
                traffic_period_end_excl = $2,
                traffic_used_bytes = 0,
                traffic_last_sync_at = NULL
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


def blocked_traffic_vless(used_bytes: int, limit_bytes: int, bot_username: str | None = None) -> str:
    """Две «пустые» строки подписки: факт лимита и подсказка открыть бота для докупки."""
    from urllib.parse import quote

    handle = (bot_username or "SvoyVPN_robot").strip().lstrip("@")
    used_g = used_bytes / BYTES_PER_GB
    lim_g = limit_bytes / BYTES_PER_GB
    name1 = quote(f"Лимит трафика ({used_g:.1f}/{lim_g:.0f} ГБ)", safe="")
    name2 = quote(
        f"Увеличить лимит: @{handle} → Подписка → «Увеличить лимит»",
        safe="",
    )
    fake = (
        "vless://00000000-0000-0000-0000-000000000000@0.0.0.0:1"
        "?type=tcp&security=none&flow=none#"
    )
    return f"{fake}{name1}\n{fake}{name2}"


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
        "baseLimitGb": base_lim,
        "defaultMonthlyGb": default_gb,
        "trafficExceeded": exceeded,
        "trafficEnforced": enforced,
    }
