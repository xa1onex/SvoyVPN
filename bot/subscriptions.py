import logging
import json
import os
import secrets
from datetime import datetime, time as dt_time, timedelta, date
from typing import Any, Optional, List, Dict
import asyncio

from .database import generate_subscription_token, get_connection
from .plans import (
    FREE_SUBSCRIPTION_END,
    FREE_TIER_ID,
    PAID_TIER_IDS,
    get_tier_bypass_gb,
    get_tier_max_devices,
    is_sentinel_subscription_end,
    is_subscription_active,
    subscription_end_date,
)
from .traffic import (
    apply_subscription_anchor_on_payment,
    ensure_bypass_period,
    is_fast_section_header,
    is_navigation_header_server,
    navigation_header_vless_line,
)
from .remnawave_client import RemnawaveClient, build_remnawave_client, is_remnawave_server
from .xui_client import XUIClient

logger = logging.getLogger(__name__)

# Не держим пул БД во время HTTP к панелям; не более 2 параллельных провижинов
_KEY_PROVISION_SEM = asyncio.Semaphore(2)

# Ключ без даты истечения не считается валидным для платной подписки.
SQL_VPN_KEY_NOT_EXPIRED = (
    "(k.expires_at IS NOT NULL AND DATE(k.expires_at) >= CURRENT_DATE)"
)

_NAV_PLACEHOLDER_IDS = (
    "00000000000000000000000000000001",
    "00000000000000000000000000000002",
)


def subscription_end_warning_days() -> int:
    """За сколько дней до конца показывать дату окончания в UI."""
    try:
        return max(0, int(os.getenv("SVOYVPN_SUB_END_WARN_DAYS", "7")))
    except ValueError:
        return 7


def should_show_subscription_end_date(
    subscription_end: datetime | date | str | None,
    *,
    has_recurring_card: bool,
) -> bool:
    """
    Дата в UI — если автоплатёж отключён и срок скоро истекает.
    При активной карте дату не показываем (продление автоматическое).
    """
    if has_recurring_card or not subscription_end:
        return False
    from .plans import is_sentinel_subscription_end, subscription_end_date

    if is_sentinel_subscription_end(subscription_end):
        return False
    end = subscription_end_date(subscription_end)
    if end is None:
        return False
    return (end - datetime.now().date()).days <= subscription_end_warning_days()


def format_subscription_status_label(end_date: datetime) -> str:
    """Короткий статус для UI/бота с акцентом на сегодня/завтра."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    end_date_only = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
    days_remaining = (end_date_only - today).days
    end_date_str = end_date.strftime("%d.%m.%Y")

    if days_remaining == 0:
        return f"активен СЕГОДНЯ ({end_date_str})"
    if days_remaining == 1:
        return f"активен ЗАВТРА ({end_date_str})"
    return f"активен до {end_date_str}"


async def get_subscription_end(user_id: int) -> datetime | None:
    """Получить дату окончания подписки пользователя."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT subscription_end FROM users WHERE user_id = $1", user_id
        )
        return row["subscription_end"] if row else None


async def set_new_subscription(user_id: int, months: int, conn=None) -> None:
    """Новая подписка: установить дату окончания от текущей даты."""
    days = months * 30
    if conn is None:
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE users
                SET
                    pay_subscribed = TRUE,
                    subscription_end = CURRENT_DATE + ($2 || ' days')::INTERVAL
                WHERE user_id = $1
                """,
                user_id,
                str(days),
            )
            # Сбрасываем уведомления при покупке новой подписки
            await conn.execute('DELETE FROM subscription_reminders WHERE user_id = $1', user_id)
            await apply_subscription_anchor_on_payment(conn, user_id)
    else:
        await conn.execute(
            """
            UPDATE users
            SET
                pay_subscribed = TRUE,
                subscription_end = CURRENT_DATE + ($2 || ' days')::INTERVAL
            WHERE user_id = $1
            """,
            user_id,
            str(days),
        )
        await conn.execute('DELETE FROM subscription_reminders WHERE user_id = $1', user_id)
        await apply_subscription_anchor_on_payment(conn, user_id)


async def set_new_subscription_days(user_id: int, days: int, conn=None) -> None:
    """Новая подписка на конкретное количество дней (для trial)."""
    if conn is None:
        async with get_connection() as _conn:
            await _conn.execute(
                """
                UPDATE users
                SET
                    pay_subscribed = TRUE,
                    subscription_end = CURRENT_DATE + ($2 || ' days')::INTERVAL
                WHERE user_id = $1
                """,
                user_id,
                str(days),
            )
            await _conn.execute('DELETE FROM subscription_reminders WHERE user_id = $1', user_id)
            await apply_subscription_anchor_on_payment(_conn, user_id)
    else:
        await conn.execute(
            """
            UPDATE users
            SET
                pay_subscribed = TRUE,
                subscription_end = CURRENT_DATE + ($2 || ' days')::INTERVAL
            WHERE user_id = $1
            """,
            user_id,
            str(days),
        )
        await conn.execute('DELETE FROM subscription_reminders WHERE user_id = $1', user_id)
        await apply_subscription_anchor_on_payment(conn, user_id)


async def extend_subscription(user_id: int, months: int, conn=None) -> None:
    """Продлить существующую подписку от текущей даты окончания."""
    if conn is None:
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE users
                SET
                    subscription_end = subscription_end + ($2 || ' months')::INTERVAL
                WHERE user_id = $1
                """,
                user_id,
                str(months),
            )
            # Сбрасываем уведомления при продлении
            await conn.execute('DELETE FROM subscription_reminders WHERE user_id = $1', user_id)
            await apply_subscription_anchor_on_payment(conn, user_id)
    else:
        await conn.execute(
            """
            UPDATE users
            SET
                subscription_end = subscription_end + ($2 || ' months')::INTERVAL
            WHERE user_id = $1
            """,
            user_id,
            str(months),
        )
        await conn.execute('DELETE FROM subscription_reminders WHERE user_id = $1', user_id)
        await apply_subscription_anchor_on_payment(conn, user_id)


_KEY_PROVISION_SEM = asyncio.Semaphore(3)


async def _sync_remnawave_key_for_user(
    conn,
    client: RemnawaveClient,
    *,
    user_id: int,
    server_id: int,
    host_remark: str,
    subscription_end,
) -> bool:
    """Создать или обновить один Remnawave-ключ в vpn_keys."""
    expire_at = RemnawaveClient.parse_expiry_datetime(subscription_end)
    expires_at = expire_at.date()

    rw_user = await client.ensure_user(
        telegram_id=user_id,
        expire_at=expire_at,
    )
    short_uuid = rw_user.get("shortUuid")
    vless_uuid = rw_user.get("vlessUuid")
    rw_user_uuid = rw_user.get("uuid")
    if not short_uuid or not vless_uuid:
        return False

    vless_link = await client.get_vless_link_for_host_remark(short_uuid, host_remark)
    if not vless_link:
        logger.warning(
            "Remnawave link not found for user %s host %s",
            user_id,
            host_remark,
        )
        return False

    await conn.execute(
        """
        UPDATE users
        SET remnawave_user_uuid = $1
        WHERE user_id = $2
          AND (remnawave_user_uuid IS NULL OR remnawave_user_uuid = $1)
        """,
        rw_user_uuid,
        user_id,
    )

    existing = await conn.fetchrow(
        """
        SELECT id FROM vpn_keys
        WHERE user_id = $1 AND server_id = $2
        ORDER BY id DESC
        LIMIT 1
        """,
        user_id,
        server_id,
    )
    if existing:
        await conn.execute(
            """
            UPDATE vpn_keys
            SET vless_client_id = $1, vless_link = $2, key_name = $3,
                expires_at = $4, is_active = TRUE
            WHERE id = $5
            """,
            vless_uuid,
            vless_link,
            host_remark,
            expires_at,
            existing["id"],
        )
        return True

    key_id = await conn.fetchval(
        """
        INSERT INTO vpn_keys (user_id, server_id, vless_client_id, vless_link,
                              key_name, expires_at, is_active)
        VALUES ($1, $2, $3, $4, $5, $6, TRUE)
        ON CONFLICT (user_id, server_id) WHERE is_active = TRUE DO NOTHING
        RETURNING id
        """,
        user_id,
        server_id,
        vless_uuid,
        vless_link,
        host_remark,
        expires_at,
    )
    if key_id:
        return True

    await conn.execute(
        """
        UPDATE vpn_keys
        SET vless_client_id = $1, vless_link = $2, key_name = $3,
            expires_at = $4, is_active = TRUE
        WHERE user_id = $5 AND server_id = $6
        """,
        vless_uuid,
        vless_link,
        host_remark,
        expires_at,
        user_id,
        server_id,
    )
    return True


async def _create_remnawave_keys_for_server(server_id: int, server: dict) -> None:
    """Создать/обновить ключи Remnawave для всех активных пользователей на новом хосте."""
    from .config import load_config
    from .traffic import is_free_server_label

    config = load_config()
    client = build_remnawave_client(config)
    host_remark = str(server["name"])

    try:
        async with get_connection() as conn:
            paid_users = await conn.fetch(
                """
                SELECT user_id, subscription_end
                FROM users
                WHERE pay_subscribed = TRUE
                  AND subscription_end IS NOT NULL
                  AND DATE(subscription_end) >= CURRENT_DATE
                  AND COALESCE(subscription_tier, 'free') <> 'free'
                  AND DATE(subscription_end) < DATE '2090-01-01'
                """
            )
            active_users = list(paid_users)

            if is_free_server_label(host_remark):
                free_users = await conn.fetch(
                    """
                    SELECT user_id, subscription_end
                    FROM users
                    WHERE pay_subscribed = TRUE
                      AND subscription_end IS NOT NULL
                      AND DATE(subscription_end) >= CURRENT_DATE
                      AND COALESCE(subscription_tier, 'free') = 'free'
                    """
                )
                seen = {int(u["user_id"]) for u in active_users}
                for row in free_users:
                    uid = int(row["user_id"])
                    if uid not in seen:
                        active_users.append(row)
                        seen.add(uid)

            if not active_users:
                logger.info("No active users for Remnawave server %s", server_id)
                return

            logger.info(
                "Syncing Remnawave keys for %d users on host %s (server %s)",
                len(active_users),
                host_remark,
                server_id,
            )

            for user_row in active_users:
                user_id = int(user_row["user_id"])
                try:
                    await _sync_remnawave_key_for_user(
                        conn,
                        client,
                        user_id=user_id,
                        server_id=server_id,
                        host_remark=host_remark,
                        subscription_end=user_row["subscription_end"],
                    )
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logger.error(
                        "Remnawave key sync failed for user %s server %s: %s",
                        user_id,
                        server_id,
                        e,
                    )
    finally:
        await client.close()


async def create_keys_for_specific_server(server_id: int) -> None:
    """
    Создать ключи для конкретного сервера всем активным пользователям.
    Используется при добавлении нового сервера.
    """
    try:
        async with get_connection() as conn:
            # Получаем информацию о сервере
            server = await conn.fetchrow(
                """
                SELECT id, name, ip, username, password, inbound_id, base_url, is_active,
                       panel_type, remnawave_host_uuid, remnawave_node_uuid
                FROM servers
                WHERE id = $1
                """,
                server_id,
            )
            relay_sid = await conn.fetchval(
                """
                SELECT tg_relay_server_id FROM traffic_settings ORDER BY id DESC LIMIT 1
                """
            )
            if not server or (
                not server["is_active"] and server_id != relay_sid
            ):
                logger.warning(f"Server {server_id} not found or not active")
                return

            if is_navigation_header_server(server["name"]):
                nav_users = await conn.fetch(
                    """
                    SELECT user_id, subscription_end FROM users
                    WHERE pay_subscribed = TRUE
                      AND subscription_end IS NOT NULL
                      AND DATE(subscription_end) >= CURRENT_DATE
                      AND COALESCE(subscription_tier, 'free') <> 'free'
                    """
                )
                for ur in nav_users:
                    uid = int(ur["user_id"])
                    se = ur["subscription_end"]
                    exp = se.date() if isinstance(se, datetime) else se
                    await upsert_navigation_header_key(
                        conn,
                        user_id=uid,
                        server_id=server_id,
                        server_name=str(server["name"]),
                        expires_at=exp,
                    )
                logger.info(
                    "Navigation header stubs for server %s: %d users",
                    server["name"],
                    len(nav_users),
                )
                return

            if is_remnawave_server(server):
                await _create_remnawave_keys_for_server(server_id, server)
                return

            # Только платные пользователи (Free управляет своим набором серверов)
            active_users = await conn.fetch('''
                SELECT user_id, subscription_end, pay_subscribed
                FROM users
                WHERE pay_subscribed = TRUE
                  AND subscription_end IS NOT NULL
                  AND DATE(subscription_end) >= CURRENT_DATE
                  AND COALESCE(subscription_tier, 'free') <> 'free'
                  AND DATE(subscription_end) < DATE '2090-01-01'
            ''')
            
            if not active_users:
                logger.info(f"No active users for server {server_id}")
                return
            
            # Создаем ОДИН клиент на весь процесс
            client = XUIClient(
                base_url=server["base_url"],
                username=server["username"],
                password=server["password"],
                inbound_id=server["inbound_id"]
            )
            await client.ensure_login()

            logger.info(f"Creating keys for {len(active_users)} active users on server {server['name']} (ID: {server_id})")
            
            for user_row in active_users:
                user_id = user_row['user_id']
                subscription_end = user_row['subscription_end']
                
                try:
                    # Парсим дату окончания
                    if isinstance(subscription_end, str):
                        if " " in subscription_end:
                            end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                        else:
                            end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
                    else:
                        end_date = subscription_end
                    
                    end_dt = datetime.combine(end_date.date(), dt_time(23, 59, 59))
                    expiry_ms = int(end_dt.timestamp() * 1000)
                    expires_at = end_date.date() if isinstance(end_date, datetime) else end_date
                    
                    # Проверяем, есть ли уже ключ для этого сервера
                    existing = await conn.fetchrow(
                        """
                        SELECT id, vless_client_id, is_active
                        FROM vpn_keys
                        WHERE user_id = $1 AND server_id = $2
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        user_id,
                        server_id,
                    )
                    
                    if existing:
                        # Ключ есть - активируем и продлеваем
                        await client.update_client_expiry(existing["vless_client_id"], expiry_ms)
                        
                        await conn.execute(
                            """
                            UPDATE vpn_keys
                            SET is_active = TRUE, expires_at = $1
                            WHERE id = $2
                            """,
                            expires_at,
                            existing["id"],
                        )
                    else:
                        # Ключа нет - создаём новый
                        result = await client.add_vless_client(
                            telegram_user_id=user_id,
                            display_name=server["name"],
                            traffic_gb=None,
                            expiry_time_unix_ms=expiry_ms,
                            public_ip=server.get("ip")
                        )
                        
                        if not result.get("id") or not result.get("link"):
                            continue
                        
                        key_id = await conn.fetchval(
                            """
                            INSERT INTO vpn_keys (user_id, server_id, vless_client_id, vless_link,
                                                  key_name, expires_at, is_active)
                            VALUES ($1, $2, $3, $4, $5, $6, TRUE)
                            ON CONFLICT (user_id, server_id) WHERE is_active = TRUE DO NOTHING
                            RETURNING id
                            """,
                            user_id,
                            server_id,
                            result["id"],
                            result["link"],
                            None,
                            expires_at,
                        )
                        if key_id:
                            await conn.execute(
                                """
                                UPDATE vpn_keys
                                SET key_name = $1
                                WHERE id = $2
                                """,
                                server['name'],
                                key_id,
                            )
                    
                    await asyncio.sleep(0.05)

                except Exception as e:
                    logger.error(f"Failed to create key for user {user_id} on server {server['name']}: {e}")
            
            await client.close()
            
    except Exception as e:
        logger.error(f"Error creating keys for server {server_id}: {e}", exc_info=True)


async def create_or_activate_keys_for_all_servers(user_id: int) -> None:
    """
    Создать или активировать ключи для всех активных серверов.
    Если ключ уже есть - активирует и продлевает, если нет - создаёт новый.
    """
    async with _KEY_PROVISION_SEM:
        try:
            await _create_or_activate_keys_for_all_servers_impl(user_id)
        except Exception as e:
            logger.error(f"Error creating keys for user {user_id}: {e}")


async def _create_or_activate_keys_for_all_servers_impl(user_id: int) -> None:
    is_free = False
    allowed: set[int] | None = None
    expires_at = None
    expiry_ms = 0
    relay_sid = None
    servers = []

    async with get_connection() as conn:
        user_row = await conn.fetchrow(
            """
            SELECT subscription_end, pay_subscribed, subscription_tier
            FROM users
            WHERE user_id = $1
              AND pay_subscribed = TRUE
              AND subscription_end IS NOT NULL
            """,
            user_id,
        )
        if not user_row:
            return

        tier = (user_row["subscription_tier"] or FREE_TIER_ID).strip() or FREE_TIER_ID
        is_free = tier == FREE_TIER_ID

        if is_free:
            from .free_tier_servers import (
                assign_free_tier_servers,
                get_free_tier_allowed_server_ids,
            )

            await assign_free_tier_servers(conn, user_id)
            allowed = await get_free_tier_allowed_server_ids(conn, user_id)
            expires_at = FREE_SUBSCRIPTION_END
            end_dt = datetime.combine(FREE_SUBSCRIPTION_END, dt_time(23, 59, 59))
            expiry_ms = int(end_dt.timestamp() * 1000)
        else:
            subscription_end = user_row["subscription_end"]
            if isinstance(subscription_end, str):
                if " " in subscription_end:
                    end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                else:
                    end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
            else:
                end_date = subscription_end

            end_dt = datetime.combine(end_date.date(), dt_time(23, 59, 59))
            expiry_ms = int(end_dt.timestamp() * 1000)
            expires_at = end_date.date() if isinstance(end_date, datetime) else end_date

        relay_sid = await conn.fetchval(
            """
            SELECT tg_relay_server_id
            FROM traffic_settings
            ORDER BY id DESC
            LIMIT 1
            """
        )
        servers = await conn.fetch(
            """
            SELECT id, name, ip, username, password, inbound_id, base_url, panel_type
            FROM servers
            WHERE is_active = TRUE
               OR id IS NOT DISTINCT FROM $1::bigint
            """,
            relay_sid,
        )

        await conn.execute(
            """
            UPDATE vpn_keys
            SET is_active = FALSE
            WHERE user_id = $1
              AND is_active = TRUE
              AND server_id IN (
                  SELECT id FROM servers WHERE is_active = FALSE
              )
              AND server_id IS DISTINCT FROM (
                  SELECT tg_relay_server_id
                  FROM traffic_settings
                  ORDER BY id DESC
                  LIMIT 1
              )
            """,
            user_id,
        )

        existing_by_server: dict[int, Any] = {}
        for row in await conn.fetch(
            """
            SELECT server_id, id, vless_client_id, is_active
            FROM vpn_keys
            WHERE user_id = $1
            ORDER BY id DESC
            """,
            user_id,
        ):
            sid = row["server_id"]
            if sid not in existing_by_server:
                existing_by_server[sid] = row

    if not servers:
        return

    for server in servers:
        server_id = server["id"]
        try:
            if is_navigation_header_server(server["name"]):
                if is_free and allowed is not None and int(server_id) not in allowed:
                    continue
                async with get_connection() as conn:
                    await upsert_navigation_header_key(
                        conn,
                        user_id=user_id,
                        server_id=int(server_id),
                        server_name=str(server["name"]),
                        expires_at=expires_at,
                    )
                continue

            if is_free and allowed is not None and int(server_id) not in allowed:
                continue

            if is_remnawave_server(server):
                from .config import load_config

                rw_client = build_remnawave_client(load_config())
                try:
                    async with get_connection() as conn:
                        sub_end = (
                            FREE_SUBSCRIPTION_END
                            if is_free
                            else expires_at
                        )
                        await _sync_remnawave_key_for_user(
                            conn,
                            rw_client,
                            user_id=user_id,
                            server_id=int(server_id),
                            host_remark=str(server["name"]),
                            subscription_end=sub_end,
                        )
                finally:
                    await rw_client.close()
                continue

            existing = existing_by_server.get(server_id)

            client = XUIClient(
                base_url=server["base_url"],
                username=server["username"],
                password=server["password"],
                inbound_id=server["inbound_id"],
            )
            try:
                if existing:
                    await client.update_client_expiry(existing["vless_client_id"], expiry_ms)
                    async with get_connection() as conn:
                        await conn.execute(
                            """
                            UPDATE vpn_keys
                            SET is_active = TRUE, expires_at = $1
                            WHERE id = $2
                            """,
                            expires_at,
                            existing["id"],
                        )
                else:
                    result = await client.add_vless_client(
                        telegram_user_id=user_id,
                        display_name=server["name"],
                        traffic_gb=None,
                        expiry_time_unix_ms=expiry_ms,
                        public_ip=server.get("ip"),
                    )
                    if not result.get("id") or not result.get("link"):
                        continue

                    async with get_connection() as conn:
                        key_id = await conn.fetchval(
                            """
                            INSERT INTO vpn_keys (user_id, server_id, vless_client_id, vless_link,
                                                  key_name, expires_at, is_active)
                            VALUES ($1, $2, $3, $4, $5, $6, TRUE)
                            ON CONFLICT (user_id, server_id) WHERE is_active = TRUE DO NOTHING
                            RETURNING id
                            """,
                            user_id,
                            server_id,
                            result["id"],
                            result["link"],
                            None,
                            expires_at,
                        )
                        if key_id:
                            await conn.execute(
                                """
                                UPDATE vpn_keys
                                SET key_name = $1
                                WHERE id = $2
                                """,
                                server["name"],
                                key_id,
                            )
            finally:
                await client.close()

        except Exception as e:
            logger.error(f"Failed to create/reactivate key for server {server['name']}: {e}")

    if is_free and allowed:
        from .free_tier_servers import deactivate_free_tier_extra_keys

        async with get_connection() as conn:
            await deactivate_free_tier_extra_keys(
                conn, user_id, allowed, tg_relay_server_id=relay_sid
            )


async def ensure_user_keys_for_server_ids(user_id: int, server_ids: List[int]) -> None:
    """
    Создать/активировать ключи пользователя только на перечисленных серверах.
    Используется из GET /sub/{token}, чтобы не дергать все панели XUI подряд
    (клиенты вроде Happ обрывают запрос по таймауту).
    """
    from .config import load_config

    if not server_ids:
        return
    try:
        async with get_connection() as conn:
            user_row = await conn.fetchrow(
                """
                SELECT subscription_end, pay_subscribed
                FROM users
                WHERE user_id = $1
                  AND pay_subscribed = TRUE
                  AND subscription_end IS NOT NULL
                """,
                user_id,
            )
            if not user_row:
                return

            subscription_end = user_row["subscription_end"]
            if isinstance(subscription_end, str):
                if " " in subscription_end:
                    end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                else:
                    end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
            else:
                end_date = subscription_end

            end_dt = datetime.combine(end_date.date(), dt_time(23, 59, 59))
            expiry_ms = int(end_dt.timestamp() * 1000)
            expires_at = end_date.date() if isinstance(end_date, datetime) else end_date

            relay_sid = await conn.fetchval(
                """
                SELECT tg_relay_server_id FROM traffic_settings ORDER BY id DESC LIMIT 1
                """
            )
            servers = await conn.fetch(
                """
                SELECT id, name, ip, username, password, inbound_id, base_url, panel_type
                FROM servers
                WHERE id = ANY($1::int[])
                  AND COALESCE(is_system, FALSE) = FALSE
                  AND (is_active = TRUE OR id IS NOT DISTINCT FROM $2::bigint)
                """,
                server_ids,
                relay_sid,
            )
            if not servers:
                return

            for server in servers:
                server_id = server["id"]
                try:
                    if is_remnawave_server(server):
                        rw_client = build_remnawave_client(load_config())
                        try:
                            await _sync_remnawave_key_for_user(
                                conn,
                                rw_client,
                                user_id=user_id,
                                server_id=int(server_id),
                                host_remark=str(server["name"]),
                                subscription_end=subscription_end,
                            )
                        finally:
                            await rw_client.close()
                        continue

                    existing = await conn.fetchrow(
                        """
                        SELECT id, vless_client_id, is_active
                        FROM vpn_keys
                        WHERE user_id = $1 AND server_id = $2
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        user_id,
                        server_id,
                    )

                    client = XUIClient(
                        base_url=server["base_url"],
                        username=server["username"],
                        password=server["password"],
                        inbound_id=server["inbound_id"],
                    )

                    if existing:
                        try:
                            await client.update_client_expiry(existing["vless_client_id"], expiry_ms)
                            await conn.execute(
                                """
                                UPDATE vpn_keys
                                SET is_active = TRUE, expires_at = $1
                                WHERE id = $2
                                """,
                                expires_at,
                                existing["id"],
                            )
                        except Exception as upd_err:
                            # Клиент пропал на панели (inbound пересоздан / shortId сменён) —
                            # вместо вечной ошибки пересоздаём клиента и чиним строку ключа.
                            logger.warning(
                                f"Stale key for user {user_id} on server {server_id} "
                                f"({existing['vless_client_id']}): {upd_err}; recreating"
                            )
                            result = await client.add_vless_client(
                                telegram_user_id=user_id,
                                display_name=server["name"],
                                traffic_gb=None,
                                expiry_time_unix_ms=expiry_ms,
                                public_ip=server.get("ip"),
                            )
                            if result.get("id") and result.get("link"):
                                await conn.execute(
                                    """
                                    UPDATE vpn_keys
                                    SET vless_client_id = $1, vless_link = $2,
                                        key_name = $3, expires_at = $4, is_active = TRUE
                                    WHERE id = $5
                                    """,
                                    result["id"],
                                    result["link"],
                                    server["name"],
                                    expires_at,
                                    existing["id"],
                                )
                    else:
                        result = await client.add_vless_client(
                            telegram_user_id=user_id,
                            display_name=server["name"],
                            traffic_gb=None,
                            expiry_time_unix_ms=expiry_ms,
                            public_ip=server.get("ip"),
                        )

                        if not result.get("id") or not result.get("link"):
                            await client.close()
                            continue

                        key_id = await conn.fetchval(
                            """
                            INSERT INTO vpn_keys (user_id, server_id, vless_client_id, vless_link,
                                                  key_name, expires_at, is_active)
                            VALUES ($1, $2, $3, $4, $5, $6, TRUE)
                            ON CONFLICT (user_id, server_id) WHERE is_active = TRUE DO NOTHING
                            RETURNING id
                            """,
                            user_id,
                            server_id,
                            result["id"],
                            result["link"],
                            None,
                            expires_at,
                        )
                        if key_id:
                            await conn.execute(
                                """
                                UPDATE vpn_keys
                                SET key_name = $1
                                WHERE id = $2
                                """,
                                server["name"],
                                key_id,
                            )

                    await client.close()

                except Exception as e:
                    logger.error(
                        f"Failed to create/reactivate key for user {user_id} on server {server['name']}: {e}"
                    )

    except Exception as e:
        logger.error(f"Error ensuring keys for user {user_id} on server_ids={server_ids}: {e}")


async def sync_user_keys(user_id: int) -> None:
    """Синхронизирует ключи пользователя с датой окончания подписки (продлевает)"""
    try:
        async with get_connection() as conn:
            user_data = await conn.fetchrow('''
                SELECT subscription_end FROM users
                WHERE user_id = $1 
                  AND pay_subscribed = TRUE 
                  AND subscription_end IS NOT NULL
            ''', user_id)
            
            if not user_data:
                return
            
            subscription_end = user_data['subscription_end']
            if isinstance(subscription_end, str):
                end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
            else:
                end_date = subscription_end
            
            end_datetime = datetime.combine(end_date.date(), dt_time(23, 59, 59))
            expiry_time_unix_ms = int(end_datetime.timestamp() * 1000)
            expires_at = end_date.date() if isinstance(end_date, datetime) else end_date
            
            # Получаем все активные ключи (без навигационных заглушек)
            keys = await conn.fetch('''
                SELECT k.id, k.vless_client_id, s.base_url, s.username, s.password, s.inbound_id
                FROM vpn_keys k
                JOIN servers s ON k.server_id = s.id
                WHERE k.user_id = $1 AND k.is_active = TRUE
                  AND REPLACE(k.vless_client_id, '-', '') <> ALL($2::text[])
            ''', user_id, list(_NAV_PLACEHOLDER_IDS))

            await conn.execute(
                "UPDATE vpn_keys SET expires_at = $1 WHERE user_id = $2 AND is_active = TRUE "
                "AND REPLACE(vless_client_id, '-', '') = ANY($3::text[])",
                expires_at, user_id, list(_NAV_PLACEHOLDER_IDS),
            )

            for key in keys:
                client = XUIClient(
                    base_url=key['base_url'],
                    username=key['username'],
                    password=key['password'],
                    inbound_id=key['inbound_id']
                )
                try:
                    await client.update_client_expiry(
                        client_id=key['vless_client_id'],
                        expiry_time_unix_ms=expiry_time_unix_ms
                    )
                    
                    await conn.execute('''
                        UPDATE vpn_keys SET expires_at = $1 WHERE id = $2
                    ''', expires_at, key['id'])
                    
                except Exception as e:
                    logger.error(f"Failed to sync key {key['id']}: {e}")
                finally:
                    await client.close()
                    
    except Exception as e:
        logger.error(f"Error syncing keys for user {user_id}: {e}")


async def clear_subscription_expiry_reminders(conn, user_id: int) -> None:
    await conn.execute(
        "DELETE FROM subscription_reminders WHERE user_id = $1",
        user_id,
    )


async def handle_expired_subscriptions(bot=None):
    """
    Истекшие платные подписки: отсрочка для автоплатежа или перевод на Free.
    """
    logger.info("Checking for expired subscriptions...")
    try:
        from .autopay_grace import try_start_grace_for_expired_autopay_user

        repaired = 0
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, subscription_end
                FROM users
                WHERE blacklisted = FALSE
                  AND COALESCE(subscription_tier, '') != $1
                  AND pay_subscribed = TRUE
                  AND subscription_end IS NOT NULL
                  AND DATE(subscription_end) < CURRENT_DATE
                LIMIT 500
                """,
                FREE_TIER_ID,
            )

        for row in rows:
            uid = int(row["user_id"])
            try:
                async with get_connection() as conn:
                    if await try_start_grace_for_expired_autopay_user(
                        conn, uid, row["subscription_end"], bot
                    ):
                        continue
                    await grant_free_tier(conn, uid)
                await create_or_activate_keys_for_all_servers(uid)
                repaired += 1
            except Exception as e:
                logger.error("handle_expired user %s: %s", uid, e)

        if repaired:
            logger.info("Expired subscriptions → Free: %s users", repaired)
        else:
            logger.info("No expired subscriptions to downgrade")
    except Exception as e:
        logger.error("Error in handle_expired_subscriptions: %s", e, exc_info=True)


async def send_upcoming_subscription_reminders(bot, config):
    """
    Напоминания о скором окончании подписки (только без привязанной карты).
    За 3 дня и за 1 день.
    """
    from .plans import build_expiry_reminder_markup

    import pytz

    logger.info("Checking for upcoming subscription reminders...")

    try:
        now_moscow = datetime.now(pytz.timezone("Europe/Moscow")).date()
        target_3d = now_moscow + timedelta(days=3)
        target_1d = now_moscow + timedelta(days=1)

        async with get_connection() as conn:
            users_3d = await conn.fetch(
                """
                SELECT u.user_id, u.subscription_end, u.first_name
                FROM users u
                LEFT JOIN subscription_reminders r
                    ON u.user_id = r.user_id AND r.reminder_type = '3_days'
                WHERE u.pay_subscribed = TRUE
                  AND u.subscription_end IS NOT NULL
                  AND DATE(u.subscription_end) = $1
                  AND r.id IS NULL
                  AND u.blacklisted = FALSE
                  AND u.yookassa_recurring_payment_method_id IS NULL
                """,
                target_3d,
            )

            users_1d = await conn.fetch(
                """
                SELECT u.user_id, u.subscription_end, u.first_name
                FROM users u
                LEFT JOIN subscription_reminders r
                    ON u.user_id = r.user_id AND r.reminder_type = '1_day'
                WHERE u.pay_subscribed = TRUE
                  AND u.subscription_end IS NOT NULL
                  AND DATE(u.subscription_end) = $1
                  AND r.id IS NULL
                  AND u.blacklisted = FALSE
                  AND u.yookassa_recurring_payment_method_id IS NULL
                """,
                target_1d,
            )

            for user in users_3d:
                user_id = user['user_id']
                sub_end = user['subscription_end']
                end_date_str = sub_end.strftime("%d.%m.%Y")
                builder, _ = await build_expiry_reminder_markup(user_id)
                
                text = (
                    f"⏰ <b>{user['first_name'] or 'Пользователь'}, подписка скоро закончится</b>\n\n"
                    f"Plus заканчивается через <b>3 дня</b> ({end_date_str}).\n\n"
                    f"Продлите заранее, чтобы VPN работал без перерыва.\n\n"
                    f"Доступны тарифы <b>Plus на месяц</b> и <b>Plus на год</b>:"
                )
                
                try:
                    await bot.send_message(user_id, text, reply_markup=builder.as_markup(), parse_mode="HTML")
                    await conn.execute('INSERT INTO subscription_reminders (user_id, reminder_type) VALUES ($1, $2)', user_id, '3_days')
                    logger.info(f"Sent 3-day reminder to user {user_id}")
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logger.error(f"Failed to send 3-day reminder to {user_id}: {e}")

            # Напоминания за 1 день
            for user in users_1d:
                user_id = user['user_id']
                sub_end = user['subscription_end']
                end_date_str = sub_end.strftime("%d.%m.%Y")
                builder, _ = await build_expiry_reminder_markup(user_id)

                text = (
                    f"⏰ <b>Внимание! Подписка почти закончилась</b>\n\n"
                    f"Plus истекает <b>завтра</b> ({end_date_str}).\n\n"
                    f"Продлите сейчас — <b>Plus на месяц</b> или <b>Plus на год</b>:"
                )
                
                try:
                    await bot.send_message(user_id, text, reply_markup=builder.as_markup(), parse_mode="HTML")
                    await conn.execute('INSERT INTO subscription_reminders (user_id, reminder_type) VALUES ($1, $2)', user_id, '1_day')
                    logger.info(f"Sent 1-day reminder to user {user_id}")
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logger.error(f"Failed to send 1-day reminder to {user_id}: {e}")
                    
    except Exception as e:
        logger.error(f"Error in send_upcoming_subscription_reminders: {e}", exc_info=True)


async def get_subscription_status(user_id: int) -> str:
    """Получает статус подписки пользователя"""
    try:
        async with get_connection() as conn:
            user_data = await conn.fetchrow('''
                SELECT subscription_end, pay_subscribed 
                FROM users 
                WHERE user_id = $1
            ''', user_id)

            if user_data and user_data['pay_subscribed'] and user_data['subscription_end']:
                subscription_end = user_data['subscription_end']
                if isinstance(subscription_end, str):
                    end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                else:
                    end_date = subscription_end
                
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                if end_date.replace(hour=0, minute=0, second=0, microsecond=0) >= today:
                    return format_subscription_status_label(end_date)
    except Exception as e:
        logger.error(f"Error in get_subscription_status: {e}")
    return "неактивен"


async def get_subscription_status_display(user_id: int) -> str:
    """Текст статуса для главного меню /start."""
    try:
        from .plans import (
            ALL_PAID_TIER_IDS,
            FREE_TIER_ID,
            TIERS,
            format_subscription_end_for_display,
            is_sentinel_subscription_end,
            is_subscription_active,
        )

        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT subscription_tier, pay_subscribed, subscription_end,
                       yookassa_recurring_payment_method_id IS NOT NULL AS has_card
                FROM users WHERE user_id = $1
                """,
                user_id,
            )

        if not row or not is_subscription_active(row["pay_subscribed"], row["subscription_end"]):
            return "VPN не подключен"

        tier = (row["subscription_tier"] or FREE_TIER_ID).strip() or FREE_TIER_ID
        has_card = bool(row["has_card"])
        sub_end = row["subscription_end"]

        if tier == FREE_TIER_ID or is_sentinel_subscription_end(sub_end):
            return "<b>VPN Free</b> подключен"

        if tier in ALL_PAID_TIER_IDS:
            tier_name = TIERS.get(tier, TIERS.get("plus", {})).get("name", "Plus")
            end_str = format_subscription_end_for_display(sub_end)
            label = f"<b>VPN {tier_name}</b> подключен"
            # С привязанной картой (автооплата) — без даты; без карты — до окончания периода
            if has_card or not end_str:
                return label
            return f"{label} до {end_str}"

        return "<b>VPN</b> подключен"
    except Exception as e:
        logger.error("get_subscription_status_display: %s", e)
        return "VPN не подключен"


async def get_subscription_navigation_header_rows(conn) -> list[dict[str, Any]]:
    """
    Строки-разделители «Информация» (🚀 Быстрые / 🆓 обход) для подписки.
    Доступны всем активным тарифам, не требуют клиента на X-UI.
    """
    from .free_tier_servers import get_system_server_ids_for_subscription

    sys_ids = await get_system_server_ids_for_subscription(conn)
    if not sys_ids:
        return []
    rows = await conn.fetch(
        """
        SELECT id AS sid, name AS server_name, display_order
        FROM servers
        WHERE id = ANY($1::bigint[])
          AND is_active = TRUE
          AND COALESCE(exclude_from_subscription, FALSE) = FALSE
        ORDER BY display_order ASC NULLS LAST, id ASC
        """,
        list(sys_ids),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        sname = str(r["server_name"] or "")
        if not is_navigation_header_server(sname):
            continue
        sid = int(r["sid"])
        out.append(
            {
                "vless_link": navigation_header_vless_line(sname),
                "server_id": sid,
                "display_order": int(r["display_order"] or 100),
                "sid": sid,
                "server_name": sname,
                "is_bypass": False,
            }
        )
    return out


def merge_subscription_keys_with_navigation_headers(
    keys: list[Any], header_rows: list[dict[str, Any]]
) -> list[Any]:
    """Добавить заголовки секций, если их ещё нет в списке ключей."""
    if not header_rows:
        return keys
    present = {int(k.get("server_id") or 0) for k in keys}
    merged = list(keys)
    for row in header_rows:
        if int(row["server_id"]) not in present:
            merged.append(row)
    return sorted(
        merged, key=lambda x: (x.get("display_order", 100), x.get("sid", 0))
    )


async def upsert_navigation_header_key(
    conn,
    *,
    user_id: int,
    server_id: int,
    server_name: str,
    expires_at: date,
) -> None:
    """Заглушка vless для строки-разделителя (без панели X-UI)."""
    fake = navigation_header_vless_line(str(server_name))
    placeholder_id = (
        "00000000-0000-0000-0000-000000000001"
        if is_fast_section_header(server_name)
        else "00000000-0000-0000-0000-000000000002"
    )
    existing = await conn.fetchrow(
        """
        SELECT id FROM vpn_keys
        WHERE user_id = $1 AND server_id = $2
        ORDER BY id DESC
        LIMIT 1
        """,
        user_id,
        server_id,
    )
    if existing:
        await conn.execute(
            """
            UPDATE vpn_keys
            SET is_active = TRUE,
                vless_link = $1,
                expires_at = $2,
                key_name = $3
            WHERE id = $4
            """,
            fake,
            expires_at,
            server_name,
            existing["id"],
        )
        return
    await conn.execute(
        """
        INSERT INTO vpn_keys (
            user_id, server_id, vless_client_id, vless_link,
            key_name, expires_at, is_active
        )
        VALUES ($1, $2, $3, $4, $5, $6, TRUE)
        """,
        user_id,
        server_id,
        placeholder_id,
        fake,
        server_name,
        expires_at,
    )


async def fetch_subscription_keys(
    conn, user_id: int
) -> tuple[list[Any], str, bool, int]:
    """
    Ключи для /sub и Happ.

    Returns:
        keys, subscription_tier, calendar_active, expire_timestamp
    """
    from .free_tier_servers import (
        filter_subscription_keys,
        get_free_tier_allowed_server_ids,
    )

    user_row = await conn.fetchrow(
        """
        SELECT pay_subscribed, subscription_end, subscription_tier
        FROM users WHERE user_id = $1
        """,
        user_id,
    )
    if not user_row:
        return [], FREE_TIER_ID, False, 0

    tier = (user_row["subscription_tier"] or FREE_TIER_ID).strip() or FREE_TIER_ID
    calendar_active = is_subscription_active(
        user_row["pay_subscribed"], user_row["subscription_end"]
    )

    expire_ts = 0
    sub_end = user_row.get("subscription_end")
    if sub_end and calendar_active:
        try:
            end_d = subscription_end_date(sub_end)
            if end_d:
                expire_ts = int(
                    datetime.combine(end_d, dt_time(23, 59, 59)).timestamp()
                )
        except Exception:
            pass

    if not calendar_active and tier != FREE_TIER_ID:
        return [], tier, False, 1

    if tier == FREE_TIER_ID:
        keys_data = await conn.fetch(
            """
            SELECT DISTINCT ON (k.server_id)
                k.vless_link, k.server_id, s.display_order, s.id AS sid,
                s.name AS server_name, s.is_bypass
            FROM vpn_keys k
            INNER JOIN servers s ON k.server_id = s.id
            WHERE k.user_id = $1
              AND k.is_active = TRUE
              AND s.is_active = TRUE
              AND COALESCE(s.exclude_from_subscription, FALSE) = FALSE
              AND (
                  k.expires_at IS NULL
                  OR DATE(k.expires_at) >= CURRENT_DATE
              )
            ORDER BY k.server_id, k.id ASC
            """,
            user_id,
        )
        keys = sorted(
            keys_data, key=lambda x: (x.get("display_order", 100), x.get("sid", 0))
        )
        allowed = await get_free_tier_allowed_server_ids(conn, user_id)
        keys = filter_subscription_keys(keys, allowed)
        nav_headers = await get_subscription_navigation_header_rows(conn)
        keys = merge_subscription_keys_with_navigation_headers(keys, nav_headers)
        if not expire_ts and calendar_active:
            expire_ts = int(
                datetime.combine(FREE_SUBSCRIPTION_END, dt_time(23, 59, 59)).timestamp()
            )
        return keys, tier, calendar_active, expire_ts

    if not calendar_active:
        return [], tier, False, 1

    keys_data = await conn.fetch(
        f"""
        SELECT DISTINCT ON (k.server_id)
            k.vless_link, k.server_id, s.display_order, s.id AS sid,
            s.name AS server_name, s.is_bypass
        FROM vpn_keys k
        INNER JOIN servers s ON k.server_id = s.id
        WHERE k.user_id = $1
          AND k.is_active = TRUE
          AND s.is_active = TRUE
          AND COALESCE(s.exclude_from_subscription, FALSE) = FALSE
          AND {SQL_VPN_KEY_NOT_EXPIRED}
        ORDER BY k.server_id, k.id ASC
        """,
        user_id,
    )
    keys = sorted(keys_data, key=lambda x: (x.get("display_order", 100), x.get("sid", 0)))
    nav_headers = await get_subscription_navigation_header_rows(conn)
    keys = merge_subscription_keys_with_navigation_headers(keys, nav_headers)
    return keys, tier, calendar_active, expire_ts


async def finalize_free_tier_access(conn, user_id: int) -> None:
    """
    После перехода на Free: отключить платные ключи в БД, продлить free-ключи до 2099.
    """
    from .free_tier_servers import (
        assign_free_tier_servers,
        deactivate_free_tier_extra_keys,
        get_free_tier_allowed_server_ids,
    )

    await assign_free_tier_servers(conn, user_id)
    allowed = await get_free_tier_allowed_server_ids(conn, user_id)
    relay_sid = await conn.fetchval(
        """
        SELECT tg_relay_server_id FROM traffic_settings ORDER BY id DESC LIMIT 1
        """
    )

    revoked_rows = []
    if allowed:
        revoked_rows = await conn.fetch(
            """
            UPDATE vpn_keys
            SET is_active = FALSE,
                expires_at = CURRENT_DATE - INTERVAL '1 day'
            WHERE user_id = $1
              AND is_active = TRUE
              AND server_id <> ALL($2::bigint[])
              AND (
                  $3::bigint IS NULL
                  OR server_id IS DISTINCT FROM $3::bigint
              )
            RETURNING id, vless_client_id, server_id
            """,
            user_id,
            list(allowed),
            relay_sid,
        )
        await conn.execute(
            """
            UPDATE vpn_keys
            SET is_active = TRUE, expires_at = $2
            WHERE user_id = $1
              AND server_id = ANY($3::bigint[])
            """,
            user_id,
            FREE_SUBSCRIPTION_END,
            list(allowed),
        )
        await deactivate_free_tier_extra_keys(
            conn, user_id, allowed, tg_relay_server_id=relay_sid
        )
    else:
        revoked_rows = await conn.fetch(
            """
            UPDATE vpn_keys
            SET is_active = FALSE,
                expires_at = CURRENT_DATE - INTERVAL '1 day'
            WHERE user_id = $1 AND is_active = TRUE
            RETURNING id, vless_client_id, server_id
            """,
            user_id,
        )

    if revoked_rows:
        asyncio.create_task(_revoke_keys_on_panels(list(revoked_rows)))


async def revoke_all_vpn_access(conn, user_id: int) -> int:
    """Отключить все VPN-ключи пользователя в БД и на панелях."""
    revoked_rows = await conn.fetch(
        """
        UPDATE vpn_keys
        SET is_active = FALSE,
            expires_at = CURRENT_DATE - INTERVAL '1 day'
        WHERE user_id = $1 AND is_active = TRUE
        RETURNING id, vless_client_id, server_id
        """,
        user_id,
    )
    if revoked_rows:
        asyncio.create_task(_revoke_keys_on_panels(list(revoked_rows)))
    return len(revoked_rows)


async def _revoke_keys_on_panels(rows: list) -> None:
    """Срок клиента на X-UI в прошлое — платные узлы перестают работать."""
    past_ms = 1_000
    for row in rows:
        cid = row.get("vless_client_id")
        sid = row.get("server_id")
        if not cid or not sid:
            continue
        if str(cid).replace("-", "") in _NAV_PLACEHOLDER_IDS:
            continue
        try:
            async with get_connection() as conn:
                server = await conn.fetchrow(
                    """
                    SELECT base_url, username, password, inbound_id
                    FROM servers WHERE id = $1
                    """,
                    sid,
                )
            if not server:
                continue
            client = XUIClient(
                base_url=server["base_url"],
                username=server["username"],
                password=server["password"],
                inbound_id=server["inbound_id"],
            )
            await client.update_client_expiry(str(cid), past_ms)
            await client.close()
        except Exception as e:
            logger.warning(
                "revoke panel client user key server %s: %s",
                sid,
                e,
            )


async def repair_expired_subscriptions_access(*, limit: int = 500, bot=None) -> int:
    """Пользователи с прошедшей датой подписки — отсрочка автоплатежа или перевод на Free."""
    from .autopay_grace import try_start_grace_for_expired_autopay_user

    repaired = 0
    try:
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, subscription_end FROM users
                WHERE blacklisted = FALSE
                  AND COALESCE(subscription_tier, '') != $1
                  AND pay_subscribed = TRUE
                  AND subscription_end IS NOT NULL
                  AND DATE(subscription_end) < CURRENT_DATE
                LIMIT $2
                """,
                FREE_TIER_ID,
                limit,
            )
        for row in rows:
            uid = int(row["user_id"])
            try:
                async with get_connection() as conn:
                    if await try_start_grace_for_expired_autopay_user(
                        conn, uid, row["subscription_end"], bot
                    ):
                        continue
                    await grant_free_tier(conn, uid)
                await create_or_activate_keys_for_all_servers(uid)
                repaired += 1
            except Exception as e:
                logger.error("repair_expired access user %s: %s", uid, e)

        async with get_connection() as conn:
            free_stale = await conn.fetch(
                """
                SELECT u.user_id
                FROM users u
                WHERE u.subscription_tier = $1
                  AND u.blacklisted = FALSE
                  AND EXISTS (
                      SELECT 1 FROM vpn_keys k
                      INNER JOIN servers s ON s.id = k.server_id
                      WHERE k.user_id = u.user_id
                        AND k.is_active = TRUE
                        AND COALESCE(s.is_bypass, FALSE) = FALSE
                        AND k.server_id IS DISTINCT FROM u.free_vpn_server_id
                        AND k.server_id IS DISTINCT FROM u.free_bypass_server_id
                        AND REPLACE(k.vless_client_id, '-', '') <> ALL($2::text[])
                  )
                LIMIT $3
                """,
                FREE_TIER_ID,
                list(_NAV_PLACEHOLDER_IDS),
                limit,
            )
        for row in free_stale:
            uid = int(row["user_id"])
            try:
                async with get_connection() as conn:
                    await finalize_free_tier_access(conn, uid)
                repaired += 1
            except Exception as e:
                logger.error("repair_free stale keys user %s: %s", uid, e)
    except Exception as e:
        logger.error("repair_expired_subscriptions_access: %s", e, exc_info=True)
    if repaired:
        logger.info("repair_expired_subscriptions_access: %s users", repaired)
    return repaired


async def ensure_user_has_subscription(
    user_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
    provision_keys: bool = False,
) -> str:
    """
    Гарантирует запись в users и активную подписку (минимум Free).
    Возвращает subscription_tier.
    """
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT subscription_tier, pay_subscribed, subscription_end
            FROM users WHERE user_id = $1
            """,
            user_id,
        )
        if not row:
            token = generate_subscription_token()
            ref = secrets.token_hex(4)
            await conn.execute(
                """
                INSERT INTO users (
                    user_id, username, first_name, registration_date, last_activity,
                    referral_code, pay_subscribed, subscription_end, subscription_token,
                    subscription_tier, bypass_traffic_limit_gb, device_limit
                ) VALUES (
                    $1, $2, $3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                    $4, TRUE, $5, $6, $7, $8, $9
                )
                """,
                user_id,
                username,
                first_name or "Пользователь",
                ref,
                FREE_SUBSCRIPTION_END,
                token,
                FREE_TIER_ID,
                get_tier_bypass_gb(FREE_TIER_ID),
                get_tier_max_devices(FREE_TIER_ID),
            )
            await ensure_bypass_period(conn, user_id)
            tier = FREE_TIER_ID
        elif is_subscription_active(row["pay_subscribed"], row["subscription_end"]):
            tier = row["subscription_tier"] or FREE_TIER_ID
        else:
            await grant_free_tier(conn, user_id)
            tier = FREE_TIER_ID

    if provision_keys:
        asyncio.create_task(create_or_activate_keys_for_all_servers(user_id))
    return tier


async def migrate_inactive_users_to_free(
    *, batch_size: int = 300, provision_keys: bool = False
) -> int:
    """Перевести пользователей без активной подписки на Free (только БД)."""
    migrated = 0
    try:
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id FROM users
                WHERE blacklisted = FALSE
                  AND (
                    pay_subscribed = FALSE
                    OR subscription_end IS NULL
                    OR DATE(subscription_end) < CURRENT_DATE
                  )
                LIMIT $1
                """,
                batch_size,
            )
        for row in rows:
            uid = row["user_id"]
            async with get_connection() as conn:
                await grant_free_tier(conn, uid)
            migrated += 1
            if provision_keys:
                asyncio.create_task(create_or_activate_keys_for_all_servers(uid))
        if migrated:
            logger.info("migrate_inactive_users_to_free: %s users", migrated)
    except Exception as e:
        logger.error("migrate_inactive_users_to_free: %s", e, exc_info=True)
    return migrated


async def grant_free_tier(conn, user_id: int) -> None:
    """Перевести пользователя на тариф Free (сохраняем дату окончания Plus для триала)."""
    from .plans import (
        ALL_PAID_TIER_IDS,
        FREE_SUBSCRIPTION_END,
        FREE_TIER_ID,
        get_tier_bypass_gb,
        get_tier_max_devices,
        is_sentinel_subscription_end,
    )
    from .free_tier_servers import assign_free_tier_servers

    old = await conn.fetchrow(
        """
        SELECT subscription_tier, subscription_end
        FROM users WHERE user_id = $1
        """,
        user_id,
    )
    last_plus_ended_at = None
    if old:
        old_tier = (old["subscription_tier"] or "").strip()
        old_end = old["subscription_end"]
        if old_tier in ALL_PAID_TIER_IDS:
            if old_end and not is_sentinel_subscription_end(old_end):
                last_plus_ended_at = old_end
            else:
                last_plus_ended_at = datetime.utcnow()

    await conn.execute(
        """
        UPDATE users SET
            subscription_tier = $2,
            pay_subscribed = TRUE,
            subscription_end = $3,
            bypass_traffic_limit_gb = $4,
            device_limit = $5,
            bypass_traffic_used_bytes = 0,
            yookassa_recurring_payment_method_id = NULL,
            pending_downgrade_tier = NULL,
            referral_discount_percent = 0,
            tier_duration_months = NULL,
            tier_price_paid = NULL,
            tier_purchased_at = NULL,
            renewal_used = FALSE,
            last_plus_ended_at = COALESCE($6, last_plus_ended_at)
        WHERE user_id = $1
        """,
        user_id,
        FREE_TIER_ID,
        FREE_SUBSCRIPTION_END,
        get_tier_bypass_gb(FREE_TIER_ID),
        get_tier_max_devices(FREE_TIER_ID),
        last_plus_ended_at,
    )
    await assign_free_tier_servers(conn, user_id)
    await finalize_free_tier_access(conn, user_id)


async def update_vless_links_for_server(server_id: int) -> None:
    """
    Обновляет VLESS ссылки для всех пользователей при редактировании сервера.
    """
    try:
        async with get_connection() as conn:
            server = await conn.fetchrow(
                "SELECT id, name, ip, port, protocol, username, password, inbound_id, base_url, is_active FROM servers WHERE id = $1",
                server_id,
            )
            relay_sid = await conn.fetchval(
                "SELECT tg_relay_server_id FROM traffic_settings ORDER BY id DESC LIMIT 1"
            )
            if not server or (not server["is_active"] and server_id != relay_sid):
                return
            
            keys = await conn.fetch('''
                SELECT id, user_id, vless_client_id, key_name FROM vpn_keys 
                WHERE server_id = $1 AND is_active = TRUE
            ''', server_id)
            
            if not keys:
                return
            
            client = XUIClient(
                base_url=server["base_url"],
                username=server["username"],
                password=server["password"],
                inbound_id=server["inbound_id"]
            )
            
            await client.ensure_login()
            resp = await client._client.get("panel/api/inbounds/list")
            inbounds = resp.json().get("obj", [])
            chosen = next((i for i in inbounds if i.get("id") == server["inbound_id"]), None)
            
            if not chosen:
                return
            
            from .vless_link_builder import build_vless_link, resolve_listen_ip

            port = chosen.get("port") or "443"
            stream_settings = json.loads(chosen.get("streamSettings", "{}") or "{}")
            listen_ip = server["ip"] or resolve_listen_ip(
                chosen_inbound=chosen,
                public_ip=server["ip"],
                base_url=server["base_url"],
            )

            display_name = server["name"]
            for key in keys:
                link = build_vless_link(
                    client_uuid=key["vless_client_id"],
                    listen_ip=listen_ip,
                    port=port,
                    stream_settings=stream_settings,
                    display_name=display_name,
                )
                await conn.execute(
                    """
                    UPDATE vpn_keys
                    SET vless_link = $1, key_name = $2
                    WHERE id = $3
                    """,
                    link,
                    display_name,
                    key["id"],
                )
                
            await client.close()
    except Exception as e:
        logger.error(f"Error updating links for server {server_id}: {e}")


async def get_user_subscription_url(user_id: int, config=None) -> str:
    """Получает URL подписки пользователя"""
    from .database import ensure_subscription_token
    import os
    
    token = await ensure_subscription_token(user_id)
    base_url = (config.subscription_base_url if config else None) or os.getenv("SUBSCRIPTION_BASE_URL", "")
    return f"{base_url.rstrip('/')}/sub/{token}"


async def migrate_all_vless_configs() -> None:
    """Миграция: обновление всех VLESS конфигов для удаления спецсимволов из ID (если нужно)"""
    logger.info("Migrating VLESS configurations...")
    # Здесь можно добавить логику массового обновления ссылок при необходимости
    pass
