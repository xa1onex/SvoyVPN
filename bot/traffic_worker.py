"""
Фоновый воркер учёта трафика.

Модель:
- По каждому активному серверу раз в цикл делаем ОДИН login + ОДИН запрос inbound list.
- Для каждого клиента inbound берём lifetime download и пишем в vpn_keys.traffic_lifetime_bytes.
- traffic_period_baseline_bytes фиксируется при старте биллингового периода (= lifetime на тот момент).
  При первом проходе воркера по ключу baseline = текущий lifetime на панели,
  чтобы в период не попадал старый накопленный трафик.
- После прохода по всем серверам одной SQL-аггрегацией пересчитываем
  users.bypass_traffic_used_bytes (только servers.is_bypass = TRUE).

На сбой отдельной панели реагируем мягко: значения lifetime прошлого прохода сохраняются,
никакой ключ не «обнуляется» из-за таймаута.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from .database import get_connection
from .traffic import (
    _client_norm_ids_from_record,
    _client_usage_bytes,
    _norm_xui_identity,
    ensure_bypass_period,
)
from .xui_client import XUIClient

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = int(os.getenv("TRAFFIC_SYNC_INTERVAL_SEC", "60"))


async def _collect_server_usage(server: dict[str, Any]) -> dict[str, int] | None:
    """
    Вернёт {norm_uuid: lifetime_bytes} по ВСЕМ клиентам указанного inbound_id.
    None — если панель недоступна (не обновляем ключи этого сервера).
    """
    inbound_id = int(server["inbound_id"])
    client = XUIClient(
        base_url=server["base_url"],
        username=server.get("username"),
        password=server.get("password"),
        api_token=None,
        inbound_id=inbound_id,
    )
    try:
        await client.ensure_login()
        resp = await client._client.get("panel/api/inbounds/list")
        data = resp.json()
        inbounds = data.get("obj") or []
    except Exception as e:
        logger.warning(
            "traffic worker: server %s (%s) login/list failed: %s",
            server["id"], server.get("name"), e,
        )
        try:
            await client.close()
        except Exception:
            pass
        return None

    try:
        await client.close()
    except Exception:
        pass

    inbound = next((i for i in inbounds if int(i.get("id", 0)) == inbound_id), None)
    if not inbound:
        logger.warning(
            "traffic worker: server %s has no inbound with id=%s",
            server["id"], inbound_id,
        )
        return {}

    settings_raw = inbound.get("settings", "{}")
    try:
        settings = json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw
    except Exception:
        settings = {}
    clients = settings.get("clients") or []
    if not isinstance(clients, list):
        clients = []

    client_stats = inbound.get("clientStats") or []
    stats_by_email: dict[str, int] = {}
    if isinstance(client_stats, list):
        for cs in client_stats:
            if not isinstance(cs, dict):
                continue
            email = str(cs.get("email") or "").strip()
            if not email:
                continue
            down = int(cs.get("down") or 0)
            stats_by_email[email] = down

    usage: dict[str, int] = {}
    for c in clients:
        if not isinstance(c, dict):
            continue
        total = _client_usage_bytes(c)
        if not total:
            email = str(c.get("email") or "").strip()
            if email and email in stats_by_email:
                total = stats_by_email[email]
        for norm in _client_norm_ids_from_record(c):
            if norm:
                prev = usage.get(norm, 0)
                if total > prev:
                    usage[norm] = total
    return usage


async def _apply_server_usage(server_id: int, usage: dict[str, int]) -> int:
    """Обновляет vpn_keys для данного сервера по полученной карте uuid→lifetime."""
    updated = 0
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, vless_client_id, traffic_lifetime_bytes,
                   traffic_period_baseline_bytes
            FROM vpn_keys
            WHERE server_id = $1 AND is_active = TRUE
            """,
            server_id,
        )
        for r in rows:
            norm = _norm_xui_identity(r["vless_client_id"])
            if not norm or norm not in usage:
                continue
            new_lifetime = int(usage[norm])
            old_lifetime = int(r["traffic_lifetime_bytes"] or 0)
            baseline = r["traffic_period_baseline_bytes"]

            if baseline is None:
                # Первый sync нового ключа: якорим на текущем lifetime,
                # чтобы старый накопленный трафик не попал в текущий период.
                baseline_val = new_lifetime
            else:
                baseline_val = int(baseline)

            # Если панель ресетнулась (lifetime упал) — подтягиваем baseline вниз,
            # чтобы used_period не становился отрицательным и не прыгал вверх.
            if new_lifetime < old_lifetime:
                baseline_val = min(baseline_val, new_lifetime)

            await conn.execute(
                """
                UPDATE vpn_keys
                SET traffic_lifetime_bytes = $1,
                    traffic_period_baseline_bytes = $2,
                    traffic_last_sync_at = NOW()
                WHERE id = $3
                """,
                new_lifetime,
                baseline_val,
                int(r["id"]),
            )
            updated += 1
    return updated


async def _refresh_bypass_periods() -> None:
    """
    Устанавливает bypass-период для новых пользователей и сбрасывает
    счётчик для тех, у кого истёк период (anchor day пройден).
    Вызывается перед агрегацией, чтобы при каждом цикле воркера
    период корректно переходил в новый месяц.
    """
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id FROM users
            WHERE traffic_anchor_day IS NOT NULL
              AND (
                bypass_period_start IS NULL
                OR bypass_period_end_excl <= CURRENT_DATE
              )
            """
        )
    for row in rows:
        try:
            async with get_connection() as conn:
                await ensure_bypass_period(conn, row["user_id"])
        except Exception as e:
            logger.warning(
                "traffic worker: bypass period refresh failed uid=%s: %s",
                row["user_id"], e,
            )


async def _aggregate_bypass_traffic() -> None:
    """Пересчитывает users.bypass_traffic_used_bytes по bypass-серверам."""
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE users u
            SET bypass_traffic_used_bytes = COALESCE(agg.used, 0),
                bypass_last_sync_at = NOW()
            FROM (
                SELECT user_id,
                       SUM(
                           GREATEST(
                               traffic_lifetime_bytes
                               - COALESCE(traffic_period_baseline_bytes, 0),
                               0
                           )
                       ) AS used
                FROM vpn_keys k
                JOIN servers s ON s.id = k.server_id
                WHERE k.is_active = TRUE
                  AND s.is_bypass = TRUE
                GROUP BY user_id
            ) agg
            WHERE u.user_id = agg.user_id
            """
        )


async def run_sync_once() -> dict[str, int]:
    """Один полный проход синхронизации. Возвращает статистику."""
    async with get_connection() as conn:
        servers = await conn.fetch(
            """
            SELECT id, name, base_url, username, password, inbound_id
            FROM servers
            WHERE is_active = TRUE
               OR id IS NOT DISTINCT FROM (
                   SELECT tg_relay_server_id
                   FROM traffic_settings
                   ORDER BY id DESC
                   LIMIT 1
               )
            """
        )

    servers_total = len(servers)
    servers_ok = 0
    servers_failed = 0
    keys_updated = 0

    for s in servers:
        srv = dict(s)
        try:
            usage = await _collect_server_usage(srv)
        except Exception as e:
            logger.warning(
                "traffic worker: unexpected error on server %s: %s", srv["id"], e
            )
            usage = None

        if usage is None:
            servers_failed += 1
            continue

        servers_ok += 1
        if usage:
            try:
                keys_updated += await _apply_server_usage(int(srv["id"]), usage)
            except Exception as e:
                logger.warning(
                    "traffic worker: apply usage failed for server %s: %s",
                    srv["id"], e,
                )

    try:
        await _refresh_bypass_periods()
    except Exception as e:
        logger.warning("traffic worker: bypass period refresh failed: %s", e)

    try:
        await _aggregate_bypass_traffic()
    except Exception as e:
        logger.warning("traffic worker: aggregate bypass traffic failed: %s", e)

    stats = {
        "servers_total": servers_total,
        "servers_ok": servers_ok,
        "servers_failed": servers_failed,
        "keys_updated": keys_updated,
    }
    logger.info(
        "traffic worker: cycle done %s ok / %s fail / %s total (keys updated=%s)",
        servers_ok, servers_failed, servers_total, keys_updated,
    )
    return stats


async def run_traffic_sync_loop(interval_sec: int | None = None) -> None:
    """Бесконечный фоновый цикл синхронизации трафика с панелей."""
    interval = int(interval_sec or DEFAULT_INTERVAL_SEC)
    if interval < 10:
        interval = 10
    logger.info("traffic worker: started, interval=%ss", interval)
    try:
        # первая синхронизация — сразу, без ожидания
        try:
            await run_sync_once()
        except Exception as e:
            logger.error("traffic worker: initial cycle failed: %s", e, exc_info=True)

        while True:
            await asyncio.sleep(interval)
            try:
                await run_sync_once()
            except Exception as e:
                logger.error("traffic worker: cycle failed: %s", e, exc_info=True)
    except asyncio.CancelledError:
        logger.info("traffic worker: stopped")
        raise
