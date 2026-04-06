import logging
import json
from datetime import datetime, time as dt_time, timedelta
from typing import Optional, List, Dict
import asyncio

from .database import get_connection
from .xui_client import XUIClient

logger = logging.getLogger(__name__)


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
                SELECT id, name, ip, username, password, inbound_id, base_url, is_active
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
                SELECT id, name, ip, username, password, inbound_id, base_url
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

                    client = XUIClient(
                        base_url=server["base_url"],
                        username=server["username"],
                        password=server["password"],
                        inbound_id=server["inbound_id"]
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
                                server['name'],
                                key_id,
                            )
                    
                    await client.close()
                    
                except Exception as e:
                    logger.error(f"Failed to create/reactivate key for server {server['name']}: {e}")

    except Exception as e:
        logger.error(f"Error creating keys for user {user_id}: {e}")


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
            
            # Получаем все активные ключи
            keys = await conn.fetch('''
                SELECT k.id, k.vless_client_id, s.base_url, s.username, s.password, s.inbound_id
                FROM vpn_keys k
                JOIN servers s ON k.server_id = s.id
                WHERE k.user_id = $1 AND k.is_active = TRUE
            ''', user_id)
            
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


async def handle_expired_subscriptions(bot=None):
    """
    Обрабатывает истекшие подписки: деактивирует ключи и уведомляет пользователя
    """
    import pytz
    
    logger.info("Checking for expired subscriptions...")
    
    try:
        now_moscow = datetime.now(pytz.timezone("Europe/Moscow")).date()
        async with get_connection() as conn:
            expired_users = await conn.fetch('''
                SELECT user_id, subscription_end
                FROM users
                WHERE pay_subscribed = TRUE 
                  AND subscription_end IS NOT NULL
                  AND DATE(subscription_end) < $1
            ''', now_moscow)
            
            if not expired_users:
                logger.info("No expired subscriptions found")
                return
            
            processed_count = 0
            notified_count = 0
            
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            from aiogram.types import InlineKeyboardButton
            from .plans import get_subscription_plans, format_price_both
            
            plans = await get_subscription_plans()

            for user in expired_users:
                user_id = user['user_id']
                subscription_end = user['subscription_end']
                
                try:
                    # Деактивируем ключи
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
                    
                    # Сбрасываем уведомления
                    await conn.execute('DELETE FROM subscription_reminders WHERE user_id = $1', user_id)
                    
                    processed_count += 1
                    
                    # Уведомление пользователю
                    if bot:
                        try:
                            if isinstance(subscription_end, str):
                                end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                            else:
                                end_date = subscription_end
                            end_date_str = end_date.strftime("%d.%m.%Y")
                        except:
                            end_date_str = "недавно"
                        
                        builder = InlineKeyboardBuilder()
                        
                        for plan_id, plan_data in plans.items():
                            builder.button(
                                text=f"{plan_data['title']} - {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                                callback_data=f"plan:{plan_id}"
                            )
                        builder.adjust(1)
                        
                        builder.row(
                            InlineKeyboardButton(text="💎 Все тарифы", callback_data="open_premium"),
                            InlineKeyboardButton(text="🎁 Бесплатно", callback_data="open_invite")
                        )
                        
                        await bot.send_message(
                            user_id,
                            f"⏰ <b>Ваша подписка истекла</b>\n\n"
                            f"📅 Дата окончания: <i>{end_date_str}</i>\n\n"
                            f"💳 Доступ к VPN временно ограничен.\nЧтобы вернуть доступ, выберите тариф и продолжите пользоваться быстрым и безопасным интернетом! 👇",
                            reply_markup=builder.as_markup(),
                            parse_mode="HTML"
                        )
                        notified_count += 1
                        await asyncio.sleep(0.05)

                except Exception as e:
                    logger.error(f"Error processing expired subscription for user {user_id}: {e}")
            
            logger.info(f"Expired subscriptions processed: {processed_count}, notifications sent: {notified_count}")
            
    except Exception as e:
        logger.error(f"Error in handle_expired_subscriptions: {e}", exc_info=True)


async def send_upcoming_subscription_reminders(bot, config):
    """
    Автоматическая рассылка напоминаний о скором окончании подписки.
    За 3 дня (с предложением скидки) и за 1 день.
    """
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    from .plans import get_user_tariffs, format_price_both
    
    import pytz

    logger.info("Checking for upcoming subscription reminders...")
    
    try:
        now_moscow = datetime.now(pytz.timezone("Europe/Moscow")).date()
        target_3d = now_moscow + timedelta(days=3)
        target_1d = now_moscow + timedelta(days=1)

        async with get_connection() as conn:
            # 1. Находим тех, у кого осталось ровно 3 дня и ЕЩЕ НЕ БЫЛО напоминания '3_days'
            users_3d = await conn.fetch('''
                SELECT u.user_id, u.subscription_end, u.first_name
                FROM users u
                LEFT JOIN subscription_reminders r ON u.user_id = r.user_id AND r.reminder_type = '3_days'
                WHERE u.pay_subscribed = TRUE
                  AND u.subscription_end IS NOT NULL
                  AND DATE(u.subscription_end) = $1
                  AND r.id IS NULL
                  AND u.blacklisted = FALSE
            ''', target_3d)
            
            # 2. Находим тех, у кого осталось ровно 1 день и ЕЩЕ НЕ БЫЛО напоминания '1_day'
            users_1d = await conn.fetch('''
                SELECT u.user_id, u.subscription_end, u.first_name
                FROM users u
                LEFT JOIN subscription_reminders r ON u.user_id = r.user_id AND r.reminder_type = '1_day'
                WHERE u.pay_subscribed = TRUE
                  AND u.subscription_end IS NOT NULL
                  AND DATE(u.subscription_end) = $1
                  AND r.id IS NULL
                  AND u.blacklisted = FALSE
            ''', target_1d)
            
            # Напоминания за 3 дня
            for user in users_3d:
                user_id = user['user_id']
                sub_end = user['subscription_end']
                end_date_str = sub_end.strftime("%d.%m.%Y")
                current_tariffs, _, show_discount = await get_user_tariffs(user_id)
                
                builder = InlineKeyboardBuilder()
                for plan_id, plan_data in current_tariffs.items():
                    builder.button(
                        text=f"{plan_data['title']} - {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                        callback_data=f"plan:{plan_id}"
                    )
                builder.adjust(1)
                
                builder.row(
                    InlineKeyboardButton(text="💎 Все тарифы", callback_data="open_premium"),
                    InlineKeyboardButton(text="🎁 Бесплатно", callback_data="open_invite")
                )

                if show_discount:
                    text = (
                        f"🎁 <b>{user['first_name'] or 'Пользователь'}, у нас для вас подарок!</b>\n\n"
                        f"Ваша подписка заканчивается через <b>3 дня</b> ({end_date_str}).\n\n"
                        f"🔥 <b>Успейте продлить её сейчас со скидкой!</b>\n"
                        f"При продлении до истечения срока действуют специальные цены. Не упустите выгоду! 🎁\n\n"
                        f"Выберите тариф для продления:"
                    )
                else:
                    text = (
                        f"⏰ <b>{user['first_name'] or 'Пользователь'}, подписка скоро закончится</b>\n\n"
                        f"Ваша подписка заканчивается через <b>3 дня</b> ({end_date_str}).\n\n"
                        f"Продлите подписку заранее, чтобы сохранить доступ к VPN без перерыва.\n\n"
                        f"Выберите тариф для продления:"
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
                current_tariffs, _, show_discount = await get_user_tariffs(user_id)
                
                builder = InlineKeyboardBuilder()
                for plan_id, plan_data in current_tariffs.items():
                    builder.button(
                        text=f"{plan_data['title']} - {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                        callback_data=f"plan:{plan_id}"
                    )
                builder.adjust(1)
                
                builder.row(
                    InlineKeyboardButton(text="💎 Все тарифы", callback_data="open_premium"),
                    InlineKeyboardButton(text="🎁 Бесплатно", callback_data="open_invite")
                )

                if show_discount:
                    text = (
                        f"⏰ <b>Внимание! Подписка почти закончилась</b>\n\n"
                        f"Ваша подписка на VPN истекает <b>ЗАВТРА</b> ({end_date_str}).\n\n"
                        f"Чтобы интернет не отключился в самый подходящий момент, рекомендуем продлить её прямо сейчас по выгодной цене! 🚀"
                    )
                else:
                    text = (
                        f"⏰ <b>Внимание! Подписка почти закончилась</b>\n\n"
                        f"Ваша подписка на VPN истекает <b>ЗАВТРА</b> ({end_date_str}).\n\n"
                        f"Чтобы интернет не отключился, продлите подписку прямо сейчас."
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
            if not server or not server['is_active']:
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
            
            port = chosen.get("port") or "443"
            stream_settings = json.loads(chosen.get("streamSettings", "{}") or "{}")
            reality_settings = stream_settings.get("realitySettings") or {}
            
            pbk = reality_settings.get("settings", {}).get("publicKey", "")
            sid = reality_settings.get("shortId", "")
            sni = (reality_settings.get("serverNames", []) or ["google.com"])[0]
            fp = "chrome"
            
            listen_ip = server["ip"] or server["base_url"].split("//")[-1].split("/")[0].split(":")[0]
            
            for key in keys:
                link = f"vless://{key['vless_client_id']}@{listen_ip}:{port}/?type=tcp&encryption=none&security=reality&pbk={pbk}&fp={fp}&sni={sni}&sid={sid or '3d'}&spx=%2F&flow=xtls-rprx-vision#{server['name']}"
                await conn.execute("UPDATE vpn_keys SET vless_link = $1 WHERE id = $2", link, key['id'])
                
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
