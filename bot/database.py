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
        
        # Добавляем колонки, если их нет (для существующих БД)
        # PostgreSQL не поддерживает IF NOT EXISTS для ALTER TABLE ADD COLUMN, поэтому используем проверку
        try:
            columns_result = await conn.fetch("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'users'
            """)
            existing_columns = {row['column_name'] for row in columns_result}
            
            if 'referral_code' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN referral_code TEXT")
                logging.info("Added referral_code column to users table")
            
            if 'referral_count' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN referral_count INTEGER DEFAULT 0")
                logging.info("Added referral_count column to users table")
            
            if 'invited_by' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN invited_by BIGINT")
                logging.info("Added invited_by column to users table")
            
            if 'renewal_used' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN renewal_used BOOLEAN DEFAULT FALSE")
                logging.info("Added renewal_used column to users table")
            
            if 'balance' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0")
                logging.info("Added balance column to users table")
            
            if 'trial_used' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN trial_used BOOLEAN DEFAULT FALSE")
                logging.info("Added trial_used column to users table")
            
            if 'utm_source' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN utm_source TEXT")
                logging.info("Added utm_source column to users table")
        except Exception as e:
            logging.warning(f"Could not add columns to users table: {e}")
        
        # Добавляем уникальный индекс для referral_code (после добавления колонки)
        try:
            await conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS users_referral_code_uix
                ON users(referral_code)
                WHERE referral_code IS NOT NULL
                """
            )
        except Exception as e:
            logging.warning(f"Could not create referral_code index: {e}")

        # уникальный токен подписки
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS users_subscription_token_uix
            ON users(subscription_token)
            """
        )

        
        # announcements (объявления для пользователей)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS announcements (
                id SERIAL PRIMARY KEY,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        
        # Если таблица пустая, добавляем дефолтное объявление
        try:
            count = await conn.fetchval('SELECT COUNT(*) FROM announcements')
            if count == 0:
                default_text = ""
                await conn.execute(
                    'INSERT INTO announcements (text, updated_at) VALUES ($1, CURRENT_TIMESTAMP)',
                    default_text
                )
        except Exception as e:
            logging.warning(f"Could not initialize announcements: {e}")

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
                display_order INTEGER DEFAULT 100,
                is_system BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP
            )
            """
        )
        
        # Миграция: добавляем display_order в servers, если его нет
        try:
            srv_columns = await conn.fetch("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'servers'
            """)
            srv_existing = {row['column_name'] for row in srv_columns}
            
            if 'display_order' not in srv_existing:
                await conn.execute("ALTER TABLE servers ADD COLUMN display_order INTEGER DEFAULT 100")
                logging.info("Added display_order column to servers table")
            
            if 'is_system' not in srv_existing:
                await conn.execute("ALTER TABLE servers ADD COLUMN is_system BOOLEAN DEFAULT FALSE")
                logging.info("Added is_system column to servers table")
        except Exception as e:
            logging.warning(f"Could not migrate servers table (display_order/is_system): {e}")

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
        
        # Таблица для динамических цен
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_settings (
                id SERIAL PRIMARY KEY,
                plan_id TEXT UNIQUE NOT NULL,
                price_rub INTEGER NOT NULL,
                price_stars INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        
        # Таблица для настроек реферальной системы
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_settings (
                id SERIAL PRIMARY KEY,
                inviter_bonus_days INTEGER DEFAULT 5,
                invited_bonus_days INTEGER DEFAULT 3,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        
        # Инициализация настроек реферальной системы, если таблица пустая
        try:
            referral_count = await conn.fetchval('SELECT COUNT(*) FROM referral_settings')
            if referral_count == 0:
                await conn.execute('''
                    INSERT INTO referral_settings (inviter_bonus_days, invited_bonus_days, updated_at)
                    VALUES (5, 3, CURRENT_TIMESTAMP)
                ''')
        except Exception as e:
            logging.warning(f"Could not initialize referral_settings: {e}")
        
        # Таблица для настроек скидок
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discount_settings (
                id SERIAL PRIMARY KEY,
                days_threshold INTEGER DEFAULT 3,
                enable_for_all BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        
        # Инициализация настроек скидок, если таблица пустая
        try:
            discount_count = await conn.fetchval('SELECT COUNT(*) FROM discount_settings')
            if discount_count == 0:
                await conn.execute('''
                    INSERT INTO discount_settings (days_threshold, enable_for_all, updated_at)
                    VALUES (3, FALSE, CURRENT_TIMESTAMP)
                ''')
        except Exception as e:
            logging.warning(f"Could not initialize discount_settings: {e}")
        
        # Таблица для настроек пробного периода
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trial_settings (
                id SERIAL PRIMARY KEY,
                days INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        
        # Инициализация настроек пробного периода, если таблица пустая
        try:
            trial_count = await conn.fetchval('SELECT COUNT(*) FROM trial_settings')
            if trial_count == 0:
                await conn.execute('''
                    INSERT INTO trial_settings (days, updated_at)
                    VALUES (0, CURRENT_TIMESTAMP)
                ''')
        except Exception as e:
            logging.warning(f"Could not initialize trial_settings: {e}")
        
        # Таблица для менеджеров (техподдержка)
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS managers (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    support_link TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
            ''')
        except Exception as e:
            logging.warning(f"Could not create managers table: {e}")
        
        # Таблица для балансов пользователей
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_balances (
                    user_id BIGINT PRIMARY KEY,
                    balance INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
            ''')
        except Exception as e:
            logging.warning(f"Could not create user_balances table: {e}")
        
        # Таблица для отслеживания отправленных уведомлений о подписке
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS subscription_reminders (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    reminder_type TEXT NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
            ''')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_sub_rem_user_type ON subscription_reminders(user_id, reminder_type)')
        except Exception as e:
            logging.warning(f"Could not create subscription_reminders table: {e}")
        
        # Таблица для приложений устройств
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS device_apps (
                    id SERIAL PRIMARY KEY,
                    device_type TEXT NOT NULL,
                    app_name TEXT NOT NULL,
                    app_url TEXT NOT NULL,
                    display_order INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
        except Exception as e:
            logging.warning(f"Could not create device_apps table: {e}")
        
        # Таблица для фото инструкций устройств
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS device_instruction_photos (
                    id SERIAL PRIMARY KEY,
                    device_type TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Миграция: переименовываем photo_file_id в file_id, если существует
            try:
                photos_columns = await conn.fetch("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = 'device_instruction_photos'
                """)
                photos_existing = {row['column_name'] for row in photos_columns}
                
                if 'photo_file_id' in photos_existing and 'file_id' not in photos_existing:
                    await conn.execute('ALTER TABLE device_instruction_photos RENAME COLUMN photo_file_id TO file_id')
                    logging.info("Renamed photo_file_id to file_id in device_instruction_photos table")
            except Exception as e:
                logging.warning(f"Could not migrate device_instruction_photos table: {e}")
        except Exception as e:
            logging.warning(f"Could not create device_instruction_photos table: {e}")

        # eSIM заказы (SvoyVPN + eSIM Access)
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS esim_orders (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id),
                    transaction_id TEXT UNIQUE NOT NULL,
                    package_code TEXT NOT NULL,
                    location_code TEXT,
                    price_kopecks INTEGER NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'test',
                    batch_order_no TEXT,
                    status TEXT NOT NULL DEFAULT 'completed',
                    delivery_json JSONB,
                    provider_raw JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')
            await conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_esim_orders_user ON esim_orders(user_id, created_at DESC)'
            )
        except Exception as e:
            logging.warning(f"Could not create esim_orders table: {e}")

        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS esim_webhook_events (
                    id BIGSERIAL PRIMARY KEY,
                    received_at TIMESTAMPTZ DEFAULT NOW(),
                    notify_type TEXT,
                    payload JSONB NOT NULL
                )
            ''')
        except Exception as e:
            logging.warning(f"Could not create esim_webhook_events table: {e}")

        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS esim_beta_waitlist (
                    user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                    email TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
        except Exception as e:
            logging.warning(f"Could not create esim_beta_waitlist table: {e}")

        # news
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS news (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    image_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
        except Exception as e:
            logging.warning(f"Could not create news table: {e}")

        # UTM campaigns
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS utm_campaigns (
                    id SERIAL PRIMARY KEY,
                    tag TEXT UNIQUE NOT NULL,
                    description TEXT DEFAULT '',
                    bonus_days INTEGER DEFAULT 0,
                    bonus_trial_days INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
        except Exception as e:
            logging.warning(f"Could not create utm_campaigns table: {e}")

        # UTM visits
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS utm_visits (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    utm_tag TEXT NOT NULL,
                    is_new_user BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
        except Exception as e:
            logging.warning(f"Could not create utm_visits table: {e}")

        # Subscription usage logs
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS subscription_usage_logs (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    user_agent TEXT,
                    ip_address TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_sub_usage_user_id ON subscription_usage_logs(user_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_sub_usage_timestamp ON subscription_usage_logs(timestamp)')
        except Exception as e:
            logging.warning(f"Could not create subscription_usage_logs table: {e}")

        # Mini-app usage logs
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS miniapp_usage_logs (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    action TEXT DEFAULT 'open',
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_miniapp_usage_user_id ON miniapp_usage_logs(user_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_miniapp_usage_timestamp ON miniapp_usage_logs(timestamp)')
        except Exception as e:
            logging.warning(f"Could not create miniapp_usage_logs table: {e}")

        # Добавляем колонку payment_source в payments (если нет)
        try:
            res = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'payments' AND column_name = 'payment_source'")
            if not res:
                await conn.execute("ALTER TABLE payments ADD COLUMN payment_source TEXT DEFAULT 'bot'")
        except Exception as e:
             logging.warning(f"Could not add payment_source to payments: {e}")

async def log_subscription_usage(user_id: int, user_agent: str, ip_address: str):
    """Логирует обращение к subscription endpoint"""
    try:
        async with get_connection() as conn:
            await conn.execute(
                'INSERT INTO subscription_usage_logs (user_id, user_agent, ip_address) VALUES ($1, $2, $3)',
                user_id, user_agent, ip_address
            )
    except Exception as e:
        logging.error(f"Error logging subscription usage: {e}")

async def log_miniapp_usage(user_id: int, action: str = 'open'):
    """Логирует активность в Mini App"""
    try:
        async with get_connection() as conn:
            await conn.execute(
                'INSERT INTO miniapp_usage_logs (user_id, action) VALUES ($1, $2)',
                user_id, action
            )
    except Exception as e:
        logging.error(f"Error logging miniapp usage: {e}")




def generate_subscription_token() -> str:
    """
    Генерирует уникальный токен подписки (Hex).
    Hex-строка (0-9, a-f) гарантированно не содержит спецсимволов и стабильна в браузерах.
    """
    return secrets.token_hex(12)


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


async def get_support_link() -> str:
    """Получить ссылку на техподдержку"""
    async with get_connection() as conn:
        manager = await conn.fetchrow('SELECT support_link FROM managers WHERE is_active = TRUE AND support_link IS NOT NULL LIMIT 1')
        if manager and manager['support_link']:
            return manager['support_link']
    return ""


async def get_announcement_text() -> str:
    """Получить текст объявления"""
    async with get_connection() as conn:
        row = await conn.fetchrow('SELECT text FROM announcements ORDER BY id DESC LIMIT 1')
        return row['text'] if row and row.get('text') else ""


async def set_announcement_text(text: str) -> None:
    """Установить текст объявления"""
    async with get_connection() as conn:
        await conn.execute('''
            INSERT INTO announcements (text, updated_at)
            VALUES ($1, CURRENT_TIMESTAMP)
        ''', text)


async def get_device_instruction_photos(device_type: str) -> list:
    """Получить список file_id фото инструкций для устройства"""
    async with get_connection() as conn:
        rows = await conn.fetch('''
            SELECT file_id FROM device_instruction_photos
            WHERE device_type = $1
            ORDER BY id
        ''', device_type)
        return [row['file_id'] for row in rows]


async def get_device_instruction_photos_list(device_type: str) -> list:
    """Получить список фото инструкций с ID для управления"""
    async with get_connection() as conn:
        return await conn.fetch('''
            SELECT id, file_id FROM device_instruction_photos
            WHERE device_type = $1
            ORDER BY id
        ''', device_type)


async def add_device_instruction_photo(device_type: str, file_id: str) -> None:
    """Добавить фото инструкции для устройства"""
    async with get_connection() as conn:
        await conn.execute('''
            INSERT INTO device_instruction_photos (device_type, file_id, created_at)
            VALUES ($1, $2, CURRENT_TIMESTAMP)
        ''', device_type, file_id)


async def delete_device_instruction_photo(photo_id: int) -> None:
    """Удалить фото инструкции"""
    async with get_connection() as conn:
        await conn.execute('DELETE FROM device_instruction_photos WHERE id = $1', photo_id)


async def log_subscription_usage(user_id: int, user_agent: str, ip_address: str) -> None:
    """Логирует запрос подписки пользователя"""
    try:
        async with get_connection() as conn:
            await conn.execute('''
                INSERT INTO subscription_usage_logs (user_id, user_agent, ip_address, timestamp)
                VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
            ''', user_id, user_agent, ip_address)
    except Exception as e:
        logging.error(f"Error logging subscription usage: {e}")


def _as_dt(v):
    """Привести значение из БД к datetime для сравнения."""
    if v is None:
        return None
    if hasattr(v, "timestamp"):
        return v
    return v


async def merge_vpn_user_into(conn, source_user_id: int, target_user_id: int) -> None:
    """
    Переносит данные source_user_id → target_user_id, строка source удаляется.
    Используется при объединении email-аккаунта (отрицательный user_id) с Telegram.
    """
    if source_user_id == target_user_id:
        return

    src = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", source_user_id)
    tgt = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", target_user_id)
    if not src or not tgt:
        raise ValueError("merge_vpn_user_into: user not found")

    se_s = _as_dt(src["subscription_end"])
    se_t = _as_dt(tgt["subscription_end"])
    merged_end = None
    for c in (se_s, se_t):
        if c is None:
            continue
        if merged_end is None or c > merged_end:
            merged_end = c

    merged_pay = bool(src["pay_subscribed"]) or bool(tgt["pay_subscribed"])
    trial_used = bool(src["trial_used"]) or bool(tgt["trial_used"])
    balance = (src["balance"] or 0) + (tgt["balance"] or 0)
    ref_count = (src["referral_count"] or 0) + (tgt["referral_count"] or 0)
    renewal_used = bool(src.get("renewal_used")) or bool(tgt.get("renewal_used"))

    tok_s = src.get("subscription_token")
    tok_t = tgt.get("subscription_token")
    merged_token = tok_t or tok_s

    # vpn_keys: убрать дубликаты по server_id
    await conn.execute(
        """
        DELETE FROM vpn_keys vk1
        USING vpn_keys vk2
        WHERE vk1.user_id = $1 AND vk2.user_id = $2 AND vk1.server_id = vk2.server_id
        """,
        source_user_id,
        target_user_id,
    )
    await conn.execute(
        "UPDATE vpn_keys SET user_id = $2 WHERE user_id = $1",
        source_user_id,
        target_user_id,
    )

    for table in ("payments", "subscription_reminders", "subscription_usage_logs", "miniapp_usage_logs"):
        try:
            await conn.execute(
                f"UPDATE {table} SET user_id = $2 WHERE user_id = $1",
                source_user_id,
                target_user_id,
            )
        except Exception as e:
            logging.warning(f"merge: skip {table}: {e}")

    try:
        await conn.execute(
            "UPDATE utm_visits SET user_id = $2 WHERE user_id = $1",
            source_user_id,
            target_user_id,
        )
    except Exception as e:
        logging.warning(f"merge: skip utm_visits: {e}")

    await conn.execute(
        "UPDATE users SET invited_by = $2 WHERE invited_by = $1",
        source_user_id,
        target_user_id,
    )

    try:
        await conn.execute("DELETE FROM managers WHERE user_id = $1", source_user_id)
    except Exception as e:
        logging.warning(f"merge: managers: {e}")

    try:
        ub_s = await conn.fetchval("SELECT balance FROM user_balances WHERE user_id = $1", source_user_id) or 0
        ub_t = await conn.fetchval("SELECT balance FROM user_balances WHERE user_id = $1", target_user_id) or 0
        await conn.execute("DELETE FROM user_balances WHERE user_id = $1", source_user_id)
        ubs = int(ub_s) + int(ub_t)
        if ubs > 0:
            await conn.execute(
                """
                INSERT INTO user_balances (user_id, balance, updated_at)
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE SET
                    balance = EXCLUDED.balance,
                    updated_at = CURRENT_TIMESTAMP
                """,
                target_user_id,
                ubs,
            )
    except Exception as e:
        logging.warning(f"merge: user_balances: {e}")

    await conn.execute(
        "UPDATE app_accounts SET user_id = $2 WHERE user_id = $1",
        source_user_id,
        target_user_id,
    )

    await conn.execute(
        """
        UPDATE users SET
            pay_subscribed = $1,
            subscription_end = $2,
            trial_used = $3,
            balance = $4,
            referral_count = $5,
            renewal_used = $6,
            subscription_token = COALESCE($7, subscription_token),
            last_activity = COALESCE(last_activity, CURRENT_TIMESTAMP)
        WHERE user_id = $8
        """,
        merged_pay,
        merged_end,
        trial_used,
        balance,
        ref_count,
        renewal_used,
        merged_token,
        target_user_id,
    )

    await conn.execute("DELETE FROM users WHERE user_id = $1", source_user_id)
    logging.info("Merged user %s into %s", source_user_id, target_user_id)

