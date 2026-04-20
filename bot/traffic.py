"""
Месячный лимит трафика (ГБ): период по «дню подписки», синхронизация с X-UI, пакеты ГБ.
"""
from __future__ import annotations

import calendar
import json
import logging
from datetime import date, datetime
from typing import Any

from .xui_client import XUIClient

logger = logging.getLogger(__name__)

BYTES_PER_GB = 1024**3

# После смены биллингового периода: первый sync с панелей должен записать baseline (lifetime) в base_bytes.
TRAFFIC_BASELINE_PENDING_SNAPSHOT = -1


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
        await conn.execute(
            """
            UPDATE users SET
                traffic_period_start = $1,
                traffic_period_end_excl = $2,
                traffic_used_bytes = 0,
                traffic_bonus_gb = 0,
                traffic_last_sync_at = NULL,
                traffic_period_base_bytes = $4
            WHERE user_id = $3
            """,
            start,
            end_excl,
            user_id,
            TRAFFIC_BASELINE_PENDING_SNAPSHOT,
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
    Суммирует up+down по всем активным ключам пользователя на панелях X-UI.
    Обновляет traffic_used_bytes не чаще min_interval_sec.
    """
    urow = await conn.fetchrow(
        "SELECT traffic_used_bytes, traffic_last_sync_at, traffic_period_base_bytes FROM users WHERE user_id = $1",
        user_id,
    )
    if not urow:
        return 0
    last = urow["traffic_last_sync_at"]
    if last:
        delta = datetime.utcnow() - last.replace(tzinfo=None) if last.tzinfo else datetime.utcnow() - last
        if delta.total_seconds() < min_interval_sec:
            return int(urow["traffic_used_bytes"] or 0)

    keys = await conn.fetch(
        """
        SELECT DISTINCT k.server_id, k.vless_client_id, s.base_url, s.username, s.password,
               s.inbound_id
        FROM vpn_keys k
        INNER JOIN servers s ON s.id = k.server_id
        WHERE k.user_id = $1 AND k.is_active = TRUE
          AND s.is_active = TRUE
        """,
        user_id,
    )
    if not keys:
        await conn.execute(
            "UPDATE users SET traffic_used_bytes = 0, traffic_last_sync_at = NOW() WHERE user_id = $1",
            user_id,
        )
        return 0

    by_server: dict[int, list[str]] = {}
    server_cfg: dict[int, dict[str, Any]] = {}
    for r in keys:
        sid = int(r["server_id"])
        server_cfg[sid] = dict(r)
        by_server.setdefault(sid, []).append(r["vless_client_id"])

    total = 0
    for sid, client_ids in by_server.items():
        cfg = server_cfg[sid]
        try:
            client = XUIClient(
                base_url=cfg["base_url"],
                username=cfg.get("username"),
                password=cfg.get("password"),
                api_token=None,
                inbound_id=int(cfg["inbound_id"]),
            )
            await client.ensure_login()
            resp = await client._client.get("panel/api/inbounds/list")
            data = resp.json()
            inbounds = data.get("obj") or []
            chosen = next((i for i in inbounds if int(i.get("id", 0)) == int(cfg["inbound_id"])), None)
            if not chosen:
                await client.close()
                continue
            settings_str = chosen.get("settings", "{}")
            try:
                settings = json.loads(settings_str) if isinstance(settings_str, str) else settings_str
            except Exception:
                settings = {}
            clients = settings.get("clients") or []
            if not isinstance(clients, list):
                clients = []
            want = {_norm_xui_identity(cid) for cid in client_ids if _norm_xui_identity(cid)}
            matched_here = 0
            for c in clients:
                if not isinstance(c, dict):
                    continue
                for nk in _client_norm_ids_from_record(c):
                    if nk in want:
                        total += _client_usage_bytes(c)
                        matched_here += 1
                        break
            if not matched_here and want and clients:
                logger.info(
                    "traffic sync server %s user %s: inbound matched 0 clients (db_ids=%s inbound_count=%s)",
                    sid,
                    user_id,
                    list(want)[:3],
                    len(clients),
                )
            await client.close()
        except Exception as e:
            logger.warning("traffic sync server %s user %s: %s", sid, user_id, e)

    base_raw = urow.get("traffic_period_base_bytes")
    prev_used = max(0, int(urow.get("traffic_used_bytes") or 0))

    if base_raw == TRAFFIC_BASELINE_PENDING_SNAPSHOT:
        # Новый биллинговый период: фиксируем «ноль периода» как текущий lifetime total с панелей.
        base_bytes = int(total)
        used_period = 0
    elif base_raw is None:
        # Первый учёт / старые строки: при prev_used=0 весь total считаем расходом периода (base=0).
        if prev_used > 0:
            base_bytes = max(0, int(total) - prev_used)
        else:
            base_bytes = 0
        used_period = max(0, int(total) - int(base_bytes))
    else:
        base_bytes = int(base_raw)
        used_period = max(0, int(total) - int(base_bytes))

    await conn.execute(
        """
        UPDATE users
        SET traffic_used_bytes = $1,
            traffic_last_sync_at = NOW(),
            traffic_period_base_bytes = $2
        WHERE user_id = $3
        """,
        used_period,
        int(base_bytes),
        user_id,
    )
    return int(used_period)


def blocked_traffic_vless(used_bytes: int, limit_bytes: int) -> str:
    from urllib.parse import quote

    used_g = used_bytes / BYTES_PER_GB
    lim_g = limit_bytes / BYTES_PER_GB
    name = quote(f"Лимит трафика ({used_g:.1f}/{lim_g:.0f} ГБ)", safe="")
    return (
        "vless://00000000-0000-0000-0000-000000000000@0.0.0.0:1"
        f"?type=tcp&security=none&flow=none#{name}"
    )


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
