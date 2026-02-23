import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime

import asyncpg
import pytz
from asyncpg.exceptions import UniqueViolationError

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Создать (при необходимости) и вернуть пул соединений PostgreSQL."""
    global _pool
    if _pool is None:
        db_url = os.getenv("DATABASE_URL")
        if db_url:
            _pool = await asyncpg.create_pool(
                dsn=db_url,
                min_size=1,
                max_size=20,
            )
        else:
            _pool = await asyncpg.create_pool(
                host=os.getenv("DB_HOST", "localhost"),
                port=int(os.getenv("DB_PORT", "5432")),
                database=os.getenv("DB_NAME", "vpn_db"),
                user=os.getenv("DB_USER", "postgres"),
                password=os.getenv("DB_PASSWORD", ""),
                min_size=1,
                max_size=20,
            )
        logging.info("PostgreSQL connection pool created")
    return _pool


@asynccontextmanager
async def get_connection():
    """Async context manager for DB access."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def init_db() -> None:
    """Инициализация схемы БД (упрощённый вариант, только нужные сущности)."""
    async with get_connection() as conn:
        # users
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP,
                pay_subscribed BOOLEAN DEFAULT FALSE,
                subscription_end TIMESTAMP,
                blacklisted BOOLEAN DEFAULT FALSE,
                subscription_token TEXT,
                referral_code TEXT,
                referral_count INTEGER DEFAULT 0,
                invited_by BIGINT,
                renewal_used BOOLEAN DEFAULT FALSE
            )
            """
        )
        
        # Добавляем уникальный индекс для referral_code
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS users_referral_code_uix
            ON users(referral_code)
            WHERE referral_code IS NOT NULL
            """
        )
        
        # Добавляем колонки, если их нет (для существующих БД)
        try:
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code TEXT")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_count INTEGER DEFAULT 0")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS invited_by BIGINT")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS renewal_used BOOLEAN DEFAULT FALSE")
        except Exception as e:
            logging.warning(f"Could not add columns to users table: {e}")

        # уникальный токен подписки
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS users_subscription_token_uix
            ON users(subscription_token)
            """
        )

        # servers
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS servers (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                ip TEXT NOT NULL,
                port INTEGER DEFAULT 54321,
                protocol TEXT DEFAULT 'https',
                username TEXT,
                password TEXT,
                inbound_id INTEGER NOT NULL,
                base_url TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP
            )
            """
        )

        # vpn_keys
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vpn_keys (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                server_id INTEGER NOT NULL,
                vless_client_id TEXT NOT NULL,
                vless_link TEXT NOT NULL,
                key_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (server_id) REFERENCES servers(id)
            )
            """
        )

        # один активный ключ на сервер для пользователя
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS vpn_keys_user_server_active_uix
            ON vpn_keys(user_id, server_id)
            WHERE is_active = TRUE
            """
        )

        # payments (упрощённо)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                amount INTEGER,
                currency TEXT,
                plan_id TEXT,
                plan_type TEXT,
                status TEXT,
                telegram_payment_charge_id TEXT,
                yookassa_payment_id TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
            """
        )

        # индексы идемпотентности
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS payments_telegram_charge_id_uix
            ON payments(telegram_payment_charge_id)
            WHERE telegram_payment_charge_id IS NOT NULL
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS payments_yookassa_payment_id_uix
            ON payments(yookassa_payment_id)
            WHERE yookassa_payment_id IS NOT NULL
            """
        )


async def check_expired_subscriptions() -> None:
    """Сбрасывает статус подписки для истёкших пользователей (мягко)."""
    current_time = datetime.now(pytz.timezone("Europe/Moscow")).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    async with get_connection() as conn:
        try:
            result = await conn.execute(
                """
                UPDATE users
                SET
                    pay_subscribed = FALSE,
                    subscription_end = NULL
                WHERE
                    pay_subscribed = TRUE
                    AND subscription_end < $1
                """,
                current_time,
            )
            if result and "UPDATE" in result:
                count = int(result.split()[-1])
                if count > 0:
                    logging.info("Disabled %s expired subscriptions", count)
        except Exception as e:
            logging.error("Error in check_expired_subscriptions: %s", e)
            raise


def generate_subscription_token() -> str:
    """
    Генерирует уникальный токен подписки (URL-safe).
    Длина ~ 32-43 символа, подходит для использования в URL.
    """
    return secrets.token_urlsafe(32)


async def ensure_subscription_token(user_id: int) -> str:
    """
    Гарантирует, что у пользователя есть subscription_token.
    Создаёт и сохраняет токен, если его нет.
    """
    async with get_connection() as conn:
        existing = await conn.fetchval(
            "SELECT subscription_token FROM users WHERE user_id = $1",
            user_id,
        )
        if existing:
            return existing

        # Пытаемся несколько раз на случай редких коллизий/гонок
        for _ in range(8):
            token = generate_subscription_token()
            try:
                await conn.execute(
                    """
                    UPDATE users
                    SET subscription_token = $1
                    WHERE user_id = $2 AND subscription_token IS NULL
                    """,
                    token,
                    user_id,
                )
            except UniqueViolationError:
                continue

            # Возвращаем фактическое значение (на случай, если его установили параллельно)
            current = await conn.fetchval(
                "SELECT subscription_token FROM users WHERE user_id = $1",
                user_id,
            )
            if current:
                return current

        raise RuntimeError("Failed to generate unique subscription token")

