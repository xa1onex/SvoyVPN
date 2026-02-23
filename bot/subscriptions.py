from __future__ import annotations

from datetime import datetime, time as dt_time

from .database import get_connection
from .xui_client import XUIClient


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


async def create_keys_for_specific_server(server_id: int) -> None:
    """
    Создать ключи для конкретного сервера всем активным пользователям.
    Используется при добавлении нового сервера.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        async with get_connection() as conn:
            # Получаем информацию о сервере
            server = await conn.fetchrow(
                """
                SELECT id, name, username, password, inbound_id, base_url, is_active
                FROM servers
                WHERE id = $1
                """,
                server_id,
            )
            if not server or not server['is_active']:
                logger.warning(f"Server {server_id} not found or not active")
                return
            
            # Получаем всех активных пользователей
            active_users = await conn.fetch('''
                SELECT user_id, subscription_end, pay_subscribed
                FROM users
                WHERE pay_subscribed = TRUE 
                  AND subscription_end IS NOT NULL
                  AND DATE(subscription_end) >= CURRENT_DATE
            ''')
            
            if not active_users:
                logger.info(f"No active users for server {server_id}")
                return
            
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
                        client = XUIClient(
                            base_url=server["base_url"],
                            username=server["username"],
                            password=server["password"],
                            inbound_id=server["inbound_id"]
                        )
                        
                        client.update_client_expiry(existing["vless_client_id"], expiry_ms)
                        
                        await conn.execute(
                            """
                            UPDATE vpn_keys
                            SET is_active = TRUE, expires_at = $1
                            WHERE id = $2
                            """,
                            expires_at,
                            existing["id"],
                        )
                        logger.debug(f"Reactivated key {existing['id']} for user {user_id} on server {server['name']}")
                    else:
                        # Ключа нет - создаём новый
                        client = XUIClient(
                            base_url=server["base_url"],
                            username=server["username"],
                            password=server["password"],
                            inbound_id=server["inbound_id"]
                        )
                        
                        result = client.add_vless_client(
                            telegram_user_id=user_id,
                            display_name=server["name"],
                            traffic_gb=None,
                            expiry_time_unix_ms=expiry_ms,
                        )
                        
                        if not result.get("id") or not result.get("link"):
                            logger.warning(f"Failed to create key for user {user_id} on server {server['name']}: invalid response")
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
                                f"{server['name']} #{key_id}",
                                key_id,
                            )
                            logger.debug(f"Created key {key_id} for user {user_id} on server {server['name']}")
                            
                except Exception as e:
                    logger.error(f"Failed to create key for user {user_id} on server {server['name']}: {e}")
                    
    except Exception as e:
        logger.error(f"Error creating keys for server {server_id}: {e}", exc_info=True)


async def create_or_activate_keys_for_all_servers(user_id: int) -> None:
    """
    Создать или активировать ключи для всех активных серверов.
    Если ключ уже есть - активирует и продлевает, если нет - создаёт новый.
    """
    import logging
    logger = logging.getLogger(__name__)
    
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

            servers = await conn.fetch(
                """
                SELECT id, name, username, password, inbound_id, base_url
                FROM servers
                WHERE is_active = TRUE
                """
            )
            
            # Деактивируем ключи для неактивных серверов
            await conn.execute('''
                UPDATE vpn_keys
                SET is_active = FALSE
                WHERE user_id = $1
                  AND is_active = TRUE
                  AND server_id IN (
                      SELECT id FROM servers WHERE is_active = FALSE
                  )
            ''', user_id)
            
            if not servers:
                return

            for server in servers:
                server_id = server["id"]
                try:
                    # Проверяем существующий ключ
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
                        client = XUIClient(
                            base_url=server["base_url"],
                            username=server["username"],
                            password=server["password"],
                            inbound_id=server["inbound_id"]
                        )
                        
                        client.update_client_expiry(existing["vless_client_id"], expiry_ms)
                        
                        await conn.execute(
                            """
                            UPDATE vpn_keys
                            SET is_active = TRUE, expires_at = $1
                            WHERE id = $2
                            """,
                            expires_at,
                            existing["id"],
                        )
                        logger.info(f"Reactivated key {existing['id']} for user {user_id} on server {server['name']}")
                    else:
                        # Ключа нет - создаём новый
                        client = XUIClient(
                            base_url=server["base_url"],
                            username=server["username"],
                            password=server["password"],
                            inbound_id=server["inbound_id"]
                        )
                        
                        result = client.add_vless_client(
                            telegram_user_id=user_id,
                            display_name=server["name"],
                            traffic_gb=None,
                            expiry_time_unix_ms=expiry_ms,
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
                                f"{server['name']} #{key_id}",
                                key_id,
                            )
                            logger.info(f"Created key {key_id} for user {user_id} on server {server['name']}")
                            
                except Exception as e:
                    logger.error(f"Failed to create/reactivate key for server {server['name']}: {e}")
                    
    except Exception as e:
        logger.error(f"Error creating keys for user {user_id}: {e}")


async def sync_user_keys(user_id: int) -> None:
    """Синхронизирует ключи пользователя с датой окончания подписки (продлевает)"""
    import logging
    logger = logging.getLogger(__name__)
    
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
            
            # Получаем все активные ключи
            keys = await conn.fetch('''
                SELECT k.id, k.vless_client_id, s.base_url, s.username, s.password, s.inbound_id
                FROM vpn_keys k
                JOIN servers s ON k.server_id = s.id
                WHERE k.user_id = $1 AND k.is_active = TRUE
            ''', user_id)
            
            for key in keys:
                try:
                    client = XUIClient(
                        base_url=key['base_url'],
                        username=key['username'],
                        password=key['password'],
                        inbound_id=key['inbound_id']
                    )
                    
                    client.update_client_expiry(
                        client_id=key['vless_client_id'],
                        expiry_time_unix_ms=expiry_time_unix_ms
                    )
                    
                    await conn.execute('''
                        UPDATE vpn_keys SET expires_at = $1 WHERE id = $2
                    ''', expires_at, key['id'])
                    
                except Exception as e:
                    logger.error(f"Failed to sync key {key['id']}: {e}")
                    
    except Exception as e:
        logger.error(f"Error syncing keys for user {user_id}: {e}")


async def handle_expired_subscriptions(bot=None):
    """
    Обрабатывает истекшие подписки: деактивирует ключи (НЕ удаляет)
    """
    import logging
    logger = logging.getLogger(__name__)
    
    logger.info("Checking for expired subscriptions...")
    
    try:
        async with get_connection() as conn:
            expired_users = await conn.fetch('''
                SELECT user_id, subscription_end
                FROM users
                WHERE pay_subscribed = TRUE 
                  AND subscription_end IS NOT NULL
                  AND DATE(subscription_end) < CURRENT_DATE
            ''')
            
            if not expired_users:
                logger.info("No expired subscriptions found")
                return
            
            processed_count = 0
            notified_count = 0
            
            for user in expired_users:
                user_id = user['user_id']
                subscription_end = user['subscription_end']
                
                try:
                    # ✅ ИСПРАВЛЕНО: Деактивируем ключи (НЕ удаляем!)
                    await conn.execute('''
                        UPDATE vpn_keys
                        SET is_active = FALSE
                        WHERE user_id = $1 AND is_active = TRUE
                    ''', user_id)
                    
                    # Обновляем статус подписки
                    await conn.execute('''
                        UPDATE users 
                        SET pay_subscribed = FALSE, renewal_used = FALSE
                        WHERE user_id = $1
                    ''', user_id)
                    
                    processed_count += 1
                    
                    # Уведомление пользователю
                    if bot:
                        try:
                            if isinstance(subscription_end, str):
                                if ' ' in subscription_end:
                                    end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                                else:
                                    end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
                            else:
                                end_date = subscription_end
                            end_date_str = end_date.strftime("%d.%m.%Y")
                        except:
                            end_date_str = "недавно"
                        
                        from aiogram.utils.keyboard import InlineKeyboardBuilder
                        from aiogram.types import InlineKeyboardButton
                        
                        builder = InlineKeyboardBuilder()
                        builder.row(InlineKeyboardButton(text="💳 Подписка", callback_data="open_premium"))
                        
                        await bot.send_message(
                            user_id,
                            f"⏰ <b>Ваша подписка истекла</b>\n\n"
                            f"📅 Дата окончания: <i>{end_date_str}</i>\n\n"
                            f"💳 Чтобы вернуть доступ, необходимо купить подписку.",
                            reply_markup=builder.as_markup(),
                            parse_mode="HTML"
                        )
                        notified_count += 1
                except Exception as e:
                    logger.error(f"Error processing expired subscription for user {user_id}: {e}")
            
            logger.info(f"Expired subscriptions processed: {processed_count}, notifications sent: {notified_count}")
            
    except Exception as e:
        logger.error(f"Error in handle_expired_subscriptions: {e}", exc_info=True)


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
                try:
                    subscription_end = user_data['subscription_end']
                    if isinstance(subscription_end, str):
                        if ' ' in subscription_end:
                            end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                        else:
                            end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
                    else:
                        end_date = subscription_end
                    
                    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                    end_date_only = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
                    
                    if end_date_only >= today:
                        return f"активен до {end_date.strftime('%d.%m.%Y')}"
                except Exception as e:
                    import logging
                    logging.error(f"Error parsing subscription date: {e}")
                    return "неактивен"
    except Exception as e:
        import logging
        logging.error(f"Error in get_subscription_status: {e}")
    return "неактивен"


async def get_user_subscription_url(user_id: int, config=None) -> str:
    """Получает URL подписки пользователя"""
    import logging
    from .database import ensure_subscription_token
    
    logger = logging.getLogger(__name__)
    
    token = await ensure_subscription_token(user_id)
    
    # Пробуем получить домен из конфига или переменных окружения
    if config and hasattr(config, 'subscription_base_url') and config.subscription_base_url:
        base_url = config.subscription_base_url
    else:
        import os
        base_url = (
            os.getenv("SUBSCRIPTION_BASE_URL") or 
            os.getenv("PUBLIC_BASE_URL") or 
            os.getenv("WEBHOOK_BASE_URL") or 
            ""
        )
        base_url = base_url.rstrip("/")
    
    if not base_url:
        logger.error("SUBSCRIPTION_BASE_URL or PUBLIC_BASE_URL not set in .env!")
        logger.error("Please set SUBSCRIPTION_BASE_URL=https://your-domain.com in .env file")
        # Возвращаем placeholder, чтобы пользователь видел проблему
        return f"https://your-domain.com/sub/{token}"
    
    # Убеждаемся, что URL не содержит localhost или 127.0.0.1
    if "localhost" in base_url.lower() or "127.0.0.1" in base_url:
        logger.error(f"Subscription URL contains localhost/127.0.0.1: {base_url}")
        logger.error("Please set SUBSCRIPTION_BASE_URL to your public domain (e.g., https://your-domain.com)")
        return f"https://your-domain.com/sub/{token}"
    
    full_url = f"{base_url}/sub/{token}"
    logger.debug(f"Generated subscription URL for user {user_id}: {full_url}")
    return full_url

