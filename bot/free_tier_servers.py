"""
Один VPN-узел и один bypass-узел для тарифа Free (закрепляются при выдаче/обновлении).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

from .plans import FREE_TIER_ID
from .traffic import (
    is_free_header_server,
    is_free_server_label,
    is_navigation_header_server,
)

logger = logging.getLogger(__name__)

_GRPC_REALITY = re.compile(r"grpc.*reality|reality.*grpc", re.I)
_GRPC = re.compile(r"\bgrpc\b", re.I)
_REALITY = re.compile(r"\breality\b", re.I)


def _name_transport_score(name: str) -> int:
    """Чем выше — тем «быстрее» узел по эвристике (как autoselect_pool)."""
    low = (name or "").lower()
    if _GRPC_REALITY.search(low):
        return 100
    if _GRPC.search(low):
        return 75
    if _REALITY.search(low):
        return 65
    if "tls" in low:
        return 45
    return 20


async def get_system_server_ids_for_subscription(conn) -> set[int]:
    """Активные системные узлы: в MiniApp скрыты, в подписке доступны всем тарифам."""
    rows = await conn.fetch(
        """
        SELECT id FROM servers
        WHERE is_active = TRUE
          AND COALESCE(is_system, FALSE) = TRUE
          AND COALESCE(exclude_from_subscription, FALSE) = FALSE
        """
    )
    return {int(r["id"]) for r in rows}


def _pick_from_server_rows(rows, *, is_bypass: bool) -> int | None:
    """Выбор лучшего сервера из уже загруженных строк."""
    if is_bypass:
        candidates = [
            r
            for r in rows
            if not is_navigation_header_server(r["name"])
            and (
                r.get("is_bypass")
                or (
                    is_free_server_label(r["name"])
                    and not is_free_header_server(r["name"])
                    and (r.get("panel_type") or "3x-ui") != "remnawave"
                )
            )
        ]
    else:
        candidates = [
            r
            for r in rows
            if not is_navigation_header_server(r["name"])
            and not r.get("is_bypass")
            and (
                not is_free_server_label(r["name"])
                or (
                    (r.get("panel_type") or "3x-ui") == "remnawave"
                    and not is_free_header_server(r["name"])
                )
            )
        ]
    if not candidates:
        return None
    min_order = min(int(r["display_order"] or 100) for r in candidates)
    tier = [r for r in candidates if int(r["display_order"] or 100) == min_order]
    best = max(tier, key=lambda r: _name_transport_score(str(r["name"] or "")))
    return int(best["id"])


async def pick_best_server_id(conn, *, is_bypass: bool) -> int | None:
    """Самый приоритетный активный сервер: display_order, затем транспорт в имени."""
    rows = await conn.fetch(
        """
        SELECT id, name, display_order, is_bypass, panel_type
        FROM servers
        WHERE is_active = TRUE
          AND COALESCE(exclude_from_subscription, FALSE) = FALSE
          AND COALESCE(is_system, FALSE) = FALSE
        ORDER BY display_order ASC NULLS LAST, id ASC
        """
    )
    return _pick_from_server_rows(rows, is_bypass=is_bypass)


async def get_free_tier_header_server_ids(conn) -> set[int]:
    """Заголовки секции 🆓 обход — в подписке Free, без отдельного ключа на панели."""
    rows = await conn.fetch(
        """
        SELECT id, name FROM servers
        WHERE is_active = TRUE
          AND COALESCE(exclude_from_subscription, FALSE) = FALSE
        """
    )
    return {
        int(r["id"])
        for r in rows
        if is_free_header_server(r["name"])
    }


async def _server_id_active(conn, server_id: int | None) -> bool:
    if server_id is None:
        return False
    return bool(
        await conn.fetchval(
            """
            SELECT is_active FROM servers
            WHERE id = $1
              AND COALESCE(exclude_from_subscription, FALSE) = FALSE
            """,
            int(server_id),
        )
    )


async def assign_free_tier_servers(conn, user_id: int) -> tuple[int | None, int | None]:
    """Пересчитать и закрепить VPN/bypass серверы для Free."""
    vpn_id = await pick_best_server_id(conn, is_bypass=False)
    bypass_id = await pick_best_server_id(conn, is_bypass=True)
    await conn.execute(
        """
        UPDATE users SET
            free_vpn_server_id = $2,
            free_bypass_server_id = $3,
            free_servers_assigned_at = NOW()
        WHERE user_id = $1
        """,
        user_id,
        vpn_id,
        bypass_id,
    )
    logger.info(
        "Free tier servers assigned user=%s vpn=%s bypass=%s",
        user_id,
        vpn_id,
        bypass_id,
    )
    return vpn_id, bypass_id


async def get_free_tier_allowed_server_ids(conn, user_id: int) -> set[int]:
    """Набор server_id для подписки Free: закреплённая пара + системные узлы."""
    row = await conn.fetchrow(
        """
        SELECT subscription_tier, free_vpn_server_id, free_bypass_server_id
        FROM users WHERE user_id = $1
        """,
        user_id,
    )
    if not row or (row["subscription_tier"] or "") != FREE_TIER_ID:
        return set()

    vpn_id = row["free_vpn_server_id"]
    bypass_id = row["free_bypass_server_id"]
    need_assign = (
        vpn_id is None
        or bypass_id is None
        or not await _server_id_active(conn, vpn_id)
        or not await _server_id_active(conn, bypass_id)
    )
    if need_assign:
        vpn_id, bypass_id = await assign_free_tier_servers(conn, user_id)

    allowed: set[int] = set()
    if vpn_id is not None:
        allowed.add(int(vpn_id))
    if bypass_id is not None:
        allowed.add(int(bypass_id))
    allowed.update(await get_system_server_ids_for_subscription(conn))
    allowed.update(await get_free_tier_header_server_ids(conn))
    return allowed


def filter_subscription_keys(
    keys: list[Mapping[str, Any] | Any],
    allowed_ids: set[int] | None,
) -> list[Any]:
    """Отфильтровать ключи по набору разрешённых server_id.
    None → без фильтрации (возвращаем все).
    set() → пусто (ни один сервер не разрешён).
    """
    if allowed_ids is None:
        return list(keys)
    return [k for k in keys if int(k.get("server_id") or 0) in allowed_ids]


async def deactivate_free_tier_extra_keys(
    conn, user_id: int, allowed_ids: set[int], *, tg_relay_server_id: int | None = None
) -> None:
    """Отключить ключи на серверах вне закреплённой пары (кроме TG relay)."""
    if not allowed_ids:
        return
    ids = list(allowed_ids)
    await conn.execute(
        """
        UPDATE vpn_keys
        SET is_active = FALSE
        WHERE user_id = $1
          AND is_active = TRUE
          AND server_id <> ALL($2::bigint[])
          AND (
              $3::bigint IS NULL
              OR server_id IS DISTINCT FROM $3::bigint
          )
        """,
        user_id,
        ids,
        tg_relay_server_id,
    )


async def migrate_free_tier_server_assignments(*, batch_size: int = 500) -> int:
    """Назначить серверы существующим Free без free_vpn_server_id."""
    from .database import get_connection

    updated = 0
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id FROM users
            WHERE subscription_tier = $1
              AND free_vpn_server_id IS NULL
              AND free_bypass_server_id IS NULL
            LIMIT $2
            """,
            FREE_TIER_ID,
            batch_size,
        )
        relay_sid = await conn.fetchval(
            """
            SELECT tg_relay_server_id
            FROM traffic_settings
            ORDER BY id DESC
            LIMIT 1
            """
        )
        for row in rows:
            uid = int(row["user_id"])
            await assign_free_tier_servers(conn, uid)
            allowed = await get_free_tier_allowed_server_ids(conn, uid)
            await deactivate_free_tier_extra_keys(
                conn, uid, allowed, tg_relay_server_id=relay_sid
            )
            updated += 1
    if updated:
        logger.info("migrate_free_tier_server_assignments: %s users", updated)
    return updated
