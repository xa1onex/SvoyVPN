"""
Lazy Migration — перенос пользователей из старой базы при первом /start.

Подключается к старой БД (vpn_bot) и переносит данные в текущую БД.
Для удаления: удалить этот файл и убрать вызов migrate_user() из start.py.

Структура старой базы (ожидаемая):
    users:      user_id, username, subscription_end, tariff, created_at, ...
    vpn_keys:   user_id, vless_link, vless_client_id, server_id, key_name, ...
"""

import logging
import os
import secrets
from datetime import datetime

import asyncpg

logger = logging.getLogger(__name__)

# ──────────────── Подключение к старой БД ────────────────

_old_pool: asyncpg.Pool | None = None

OLD_DB_CONFIG = {
    "host": os.getenv("OLD_DB_HOST", "localhost"),
    "port": int(os.getenv("OLD_DB_PORT", "5432")),
    "database": os.getenv("OLD_DB_NAME", "vpn_bot"),
    "user": os.getenv("OLD_DB_USER", "postgres"),
    "password": os.getenv("OLD_DB_PASSWORD", "d_s090407"),
}


async def _get_old_pool() -> asyncpg.Pool:
    """Возвращает (создаёт при необходимости) пул к старой БД."""
    global _old_pool
    if _old_pool is None:
        try:
            _old_pool = await asyncpg.create_pool(
                **OLD_DB_CONFIG,
                min_size=1,
                max_size=5,
                command_timeout=10,
            )
            logger.info("Old DB pool created (%s:%s/%s)",
                        OLD_DB_CONFIG["host"],
                        OLD_DB_CONFIG["port"],
                        OLD_DB_CONFIG["database"])
        except Exception as e:
            logger.error("Failed to create old DB pool: %s", e)
            raise
    return _old_pool


async def close_old_pool():
    """Закрыть пул к старой БД (вызывать при shutdown)."""
    global _old_pool
    if _old_pool:
        await _old_pool.close()
        _old_pool = None
        logger.info("Old DB pool closed")


# ──────────────── Основная функция миграции ────────────────

async def migrate_user(user_id: int, new_conn) -> dict | None:
    """
    Lazy-миграция одного пользователя из старой базы в новую.

    Вызывается из handle_start ПЕРЕД вставкой нового пользователя.
    Возвращает dict с данными мигрированного пользователя или None.

    Параметры:
        user_id   — Telegram ID пользователя
        new_conn  — активное asyncpg-соединение к НОВОЙ базе

    Алгоритм:
        1. Ищет пользователя в старой базе.
        2. Если не найден → None.
        3. Если найден → переносит в новую базу, помечает migrated = TRUE.
        4. Также переносит VPN-ключи (если есть).
    """
    try:
        old_pool = await _get_old_pool()
    except Exception:
        logger.warning("Old DB unavailable, skipping migration for %s", user_id)
        return None

    try:
        async with old_pool.acquire() as old_conn:
            # ── 1. Ищем пользователя в старой базе ──
            old_user = await old_conn.fetchrow(
                "SELECT * FROM users WHERE user_id = $1", user_id
            )
            if not old_user:
                return None

            # ── 2. Проверяем, не был ли уже мигрирован ──
            migrated = old_user.get('migrated', False)
            if migrated:
                logger.info("User %s already marked as migrated in old DB", user_id)
                return None

            # ── 3. Собираем данные для переноса ──
            from .database import generate_subscription_token

            username = old_user.get('username')
            first_name = old_user.get('first_name', 'Пользователь')
            subscription_end = old_user.get('subscription_end')
            registration_date = old_user.get('registration_date') or old_user.get('created_at')
            referral_code = secrets.token_hex(4)
            sub_token = generate_subscription_token()

            # Определяем статус подписки
            pay_subscribed = False
            if subscription_end:
                end_date = subscription_end
                if isinstance(end_date, str):
                    end_date = datetime.strptime(end_date.split()[0], "%Y-%m-%d")
                if hasattr(end_date, 'date'):
                    is_active = end_date.date() >= datetime.now().date()
                else:
                    is_active = end_date >= datetime.now().date()
                pay_subscribed = is_active

            # ── 4. Вставляем в новую базу ──
            await new_conn.execute('''
                INSERT INTO users (
                    user_id, username, first_name, registration_date, last_activity,
                    referral_code, pay_subscribed, subscription_end, subscription_token,
                    utm_source
                ) VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP, $5, $6, $7, $8, $9)
                ON CONFLICT (user_id) DO NOTHING
            ''',
                user_id,
                username,
                first_name,
                registration_date or datetime.now(),
                referral_code,
                pay_subscribed,
                subscription_end,
                sub_token,
                'migration',
            )

            # ── 5. Переносим VPN ключи (если есть) ──
            keys_migrated = 0
            try:
                old_keys = await old_conn.fetch(
                    "SELECT * FROM vpn_keys WHERE user_id = $1", user_id
                )
                for key in old_keys:
                    try:
                        await new_conn.execute('''
                            INSERT INTO vpn_keys (
                                user_id, server_id, vless_client_id, vless_link,
                                key_name, created_at, expires_at, is_active
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                            ON CONFLICT DO NOTHING
                        ''',
                            user_id,
                            key.get('server_id', 1),
                            key.get('vless_client_id', ''),
                            key.get('vless_link', ''),
                            key.get('key_name', f'migrated_{user_id}'),
                            key.get('created_at', datetime.now()),
                            key.get('expires_at'),
                            key.get('is_active', True),
                        )
                        keys_migrated += 1
                    except Exception as e:
                        logger.warning("Failed to migrate VPN key for %s: %s", user_id, e)
            except Exception as e:
                logger.warning("Could not fetch VPN keys from old DB for %s: %s", user_id, e)

            # ── 6. Помечаем как мигрированного в старой базе ──
            try:
                await old_conn.execute(
                    "UPDATE users SET migrated = TRUE WHERE user_id = $1", user_id
                )
            except Exception as e:
                # Если колонки migrated нет — создаём и повторяем
                logger.warning("Could not set migrated flag, trying to add column: %s", e)
                try:
                    await old_conn.execute(
                        "ALTER TABLE users ADD COLUMN IF NOT EXISTS migrated BOOLEAN DEFAULT FALSE"
                    )
                    await old_conn.execute(
                        "UPDATE users SET migrated = TRUE WHERE user_id = $1", user_id
                    )
                except Exception as e2:
                    logger.error("Failed to set migrated flag even after adding column: %s", e2)

            logger.info(
                "✅ Migrated user %s (sub_end=%s, keys=%d)",
                user_id, subscription_end, keys_migrated
            )

            return {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "subscription_end": subscription_end,
                "pay_subscribed": pay_subscribed,
                "keys_migrated": keys_migrated,
            }

    except Exception as e:
        logger.error("Migration error for user %s: %s", user_id, e)
        return None
