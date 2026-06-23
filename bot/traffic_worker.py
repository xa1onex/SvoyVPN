"""
Фоновый воркер учёта трафика.

Модель:
- По каждому активному серверу раз в цикл делаем ОДИН login + ОДИН запрос inbound list.
- Для каждого клиента inbound берём lifetime download и пишем в vpn_keys.traffic_lifetime_bytes.
- traffic_period_baseline_bytes фиксируется при старте биллингового периода (= lifetime на тот момент).
  При первом проходе воркера по ключу baseline = текущий lifetime на панели,
  чтобы в период не попадал старый накопленный трафик.
- После прохода по всем серверам одной SQL-аггрегацией пересчитываем
  users.bypass_traffic_used_bytes (servers.is_bypass = TRUE, включая Remnawave 🆓).
- Remnawave: один запрос к панели на все RW-хосты; при агрегации MAX, не SUM
  (один пользователь = один UUID на всех RW-серверах).

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
    compute_billing_period,
    ensure_bypass_period,
    ensure_traffic_anchor_and_period,
)
from .xui_client import XUIClient

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = int(os.getenv("TRAFFIC_SYNC_INTERVAL_SEC", "60"))
_sync_cycle_lock = asyncio.Lock()


async def _collect_remnawave_usage() -> dict[str, int] | None:
    """
    Трафик всех пользователей с Remnawave Panel (один запрос на все RW-серверы).
    None — панель недоступна.
    """
    from .config import load_config
    from .remnawave_client import build_remnawave_client

    config = load_config()
    if not config.remnawave.enabled:
        return {}

    client = build_remnawave_client(config)
    try:
        raw = await client.fetch_all_users_traffic()
        return {
            _norm_xui_identity(uuid): int(bytes_val)
            for uuid, bytes_val in raw.items()
            if _norm_xui_identity(uuid)
        }
    except Exception as e:
        logger.warning("traffic worker: Remnawave traffic fetch failed: %s", e)
        return None
    finally:
        await client.close()


async def _collect_server_usage(server: dict[str, Any]) -> dict[str, int] | None:
    """
    Вернёт {norm_uuid: lifetime_bytes} по ВСЕМ клиентам указанного inbound_id.
    None — если панель недоступна (не обновляем ключи этого сервера).
  Remnawave обрабатывается отдельно в _collect_remnawave_usage().
    """
    if (server.get("panel_type") or "3x-ui") == "remnawave":
        return {}

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
            up = int(cs.get("up") or 0)
            stats_by_email[email] = up + down

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

            # Если панель сбросила счётчик — не опускаем baseline: иначе расход
            # в боте «обнуляется», хотя лимит уже был потрачен.
            if new_lifetime < old_lifetime:
                logger.info(
                    "traffic worker: panel counter dropped key=%s %s -> %s (baseline kept)",
                    int(r["id"]), old_lifetime, new_lifetime,
                )

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


async def _align_stale_traffic_periods() -> None:
    """Выравнивает устаревшие traffic_period_* без сброса baseline (воркер, не /sub)."""
    from datetime import date

    today = date.today()
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, traffic_anchor_day, traffic_period_start, traffic_period_end_excl
            FROM users
            WHERE traffic_anchor_day IS NOT NULL
              AND traffic_period_start IS NOT NULL
            """
        )
        for row in rows:
            try:
                start, end_excl = compute_billing_period(today, int(row["traffic_anchor_day"]))
                stored = row["traffic_period_start"]
                stored_end = row["traffic_period_end_excl"]
                if hasattr(stored, "date"):
                    stored = stored.date()
                if hasattr(stored_end, "date"):
                    stored_end = stored_end.date()
                if stored == start:
                    continue
                if stored_end is not None and today >= stored_end:
                    await ensure_traffic_anchor_and_period(conn, int(row["user_id"]))
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
                        int(row["user_id"]),
                    )
            except Exception as e:
                logger.warning(
                    "traffic worker: align traffic period failed uid=%s: %s",
                    row["user_id"], e,
                )


async def _refresh_bypass_periods() -> None:
    """
    Устанавливает bypass-период для новых пользователей и сбрасывает
    счётчик для тех, у кого истёк период (anchor day пройден).
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
                await ensure_bypass_period(conn, row["user_id"])
            except Exception as e:
                logger.warning(
                    "traffic worker: bypass period refresh failed uid=%s: %s",
                    row["user_id"], e,
                )


async def _aggregate_bypass_traffic() -> None:
    """Пересчитывает users.bypass_traffic_used_bytes; пакет тратится первым (по дельте)."""
    from .traffic import apply_bypass_usage_delta

    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id,
                   COALESCE(u.bypass_traffic_used_bytes, 0) AS old_used,
                   COALESCE(u.bypass_bonus_gb, 0) AS pack_remaining_gb,
                   COALESCE(agg.used, 0) AS new_used
            FROM users u
            JOIN (
                SELECT user_id,
                       COALESCE(SUM(used) FILTER (WHERE panel_type <> 'remnawave'), 0)
                       + COALESCE(MAX(used) FILTER (WHERE panel_type = 'remnawave'), 0)
                       AS used
                FROM (
                    SELECT k.user_id,
                           s.panel_type,
                           GREATEST(
                               k.traffic_lifetime_bytes
                               - COALESCE(k.traffic_period_baseline_bytes, 0),
                               0
                           ) AS used
                    FROM vpn_keys k
                    JOIN servers s ON s.id = k.server_id
                    WHERE k.is_active = TRUE
                      AND s.is_bypass = TRUE
                ) key_delta
                GROUP BY user_id
            ) agg ON u.user_id = agg.user_id
            """
        )
        for row in rows:
            old_used = int(row["old_used"] or 0)
            new_used = int(row["new_used"] or 0)
            if new_used < old_used:
                # Панель могла сбросить счётчик — не откатываем списание пакета.
                new_used = old_used
            pack_remaining = apply_bypass_usage_delta(
                old_used, new_used, int(row["pack_remaining_gb"] or 0)
            )
            await conn.execute(
                """
                UPDATE users
                SET bypass_traffic_used_bytes = $1,
                    bypass_bonus_gb = $2,
                    bypass_last_sync_at = NOW()
                WHERE user_id = $3
                """,
                new_used,
                pack_remaining,
                int(row["user_id"]),
            )


async def run_sync_once() -> dict[str, int]:
    """Один полный проход синхронизации. Возвращает статистику."""
    if _sync_cycle_lock.locked():
        logger.warning("traffic worker: previous cycle still running, skip")
        return {
            "servers_total": 0,
            "servers_ok": 0,
            "servers_failed": 0,
            "keys_updated": 0,
            "skipped": 1,
        }
    async with _sync_cycle_lock:
        return await _run_sync_once_impl()


async def _run_sync_once_impl() -> dict[str, int]:
    """Внутренняя реализация полного прохода синхронизации."""
    async with get_connection() as conn:
        servers = await conn.fetch(
            """
            SELECT id, name, base_url, username, password, inbound_id, panel_type
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

    rw_usage = await _collect_remnawave_usage()
    if rw_usage is None:
        servers_failed += 1
        logger.warning("traffic worker: Remnawave panel unavailable, RW keys not updated")
    else:
        rw_server_ids = [
            int(s["id"])
            for s in servers
            if (s.get("panel_type") or "3x-ui") == "remnawave"
        ]
        if rw_server_ids:
            servers_ok += 1
            for sid in rw_server_ids:
                if not rw_usage:
                    continue
                try:
                    keys_updated += await _apply_server_usage(sid, rw_usage)
                except Exception as e:
                    logger.warning(
                        "traffic worker: apply Remnawave usage failed for server %s: %s",
                        sid, e,
                    )

    try:
        await _align_stale_traffic_periods()
    except Exception as e:
        logger.warning("traffic worker: align traffic periods failed: %s", e)

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
