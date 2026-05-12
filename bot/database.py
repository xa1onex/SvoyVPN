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
                renewal_used BOOLEAN DEFAULT FALSE,
                device_limit INTEGER DEFAULT 5
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

            if 'device_limit' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN device_limit INTEGER DEFAULT 5")
                logging.info("Added device_limit column to users table")

            if 'traffic_anchor_day' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN traffic_anchor_day INTEGER")
                logging.info("Added traffic_anchor_day column to users table")
            if 'traffic_period_start' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN traffic_period_start DATE")
                logging.info("Added traffic_period_start column to users table")
            if 'traffic_period_end_excl' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN traffic_period_end_excl DATE")
                logging.info("Added traffic_period_end_excl column to users table")
            if 'traffic_used_bytes' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN traffic_used_bytes BIGINT DEFAULT 0")
                logging.info("Added traffic_used_bytes column to users table")
            if 'traffic_bonus_gb' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN traffic_bonus_gb INTEGER DEFAULT 0")
                logging.info("Added traffic_bonus_gb column to users table")
            if 'traffic_limit_gb' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN traffic_limit_gb INTEGER")
                logging.info("Added traffic_limit_gb column to users table")
            if 'traffic_last_sync_at' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN traffic_last_sync_at TIMESTAMP")
                logging.info("Added traffic_last_sync_at column to users table")
            if 'traffic_period_base_bytes' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN traffic_period_base_bytes BIGINT")
                logging.info("Added traffic_period_base_bytes column to users table")
            
            if 'balance' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0")
                logging.info("Added balance column to users table")
            
            if 'trial_used' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN trial_used BOOLEAN DEFAULT FALSE")
                logging.info("Added trial_used column to users table")
            
            if 'utm_source' not in existing_columns:
                await conn.execute("ALTER TABLE users ADD COLUMN utm_source TEXT")
                logging.info("Added utm_source column to users table")

            if 'esim_beta_access' not in existing_columns:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN esim_beta_access BOOLEAN DEFAULT FALSE"
                )
                logging.info("Added esim_beta_access column to users table")

            # Subscription tier system
            if 'subscription_tier' not in existing_columns:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN subscription_tier TEXT DEFAULT 'legacy'"
                )
                logging.info("Added subscription_tier column to users table")
            if 'bypass_traffic_used_bytes' not in existing_columns:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN bypass_traffic_used_bytes BIGINT DEFAULT 0"
                )
                logging.info("Added bypass_traffic_used_bytes column to users table")
            if 'bypass_traffic_limit_gb' not in existing_columns:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN bypass_traffic_limit_gb INTEGER"
                )
                logging.info("Added bypass_traffic_limit_gb column to users table")
            if 'bypass_period_start' not in existing_columns:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN bypass_period_start DATE"
                )
                logging.info("Added bypass_period_start column to users table")
            if 'bypass_period_end_excl' not in existing_columns:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN bypass_period_end_excl DATE"
                )
                logging.info("Added bypass_period_end_excl column to users table")
            if 'bypass_bonus_gb' not in existing_columns:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN bypass_bonus_gb INTEGER DEFAULT 0"
                )
                logging.info("Added bypass_bonus_gb column to users table")
            if 'bypass_last_sync_at' not in existing_columns:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN bypass_last_sync_at TIMESTAMP"
                )
                logging.info("Added bypass_last_sync_at column to users table")
            if 'tier_purchased_at' not in existing_columns:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN tier_purchased_at TIMESTAMP"
                )
                logging.info("Added tier_purchased_at column to users table")
            if 'tier_duration_months' not in existing_columns:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN tier_duration_months INTEGER"
                )
                logging.info("Added tier_duration_months column to users table")
            if 'tier_price_paid' not in existing_columns:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN tier_price_paid INTEGER DEFAULT 0"
                )
                logging.info("Added tier_price_paid column to users table")
            if 'yookassa_recurring_payment_method_id' not in existing_columns:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN yookassa_recurring_payment_method_id TEXT"
                )
                logging.info(
                    "Added yookassa_recurring_payment_method_id column to users table"
                )
            if 'cancel_retention_used' not in existing_columns:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN cancel_retention_used BOOLEAN DEFAULT FALSE"
                )
                logging.info("Added cancel_retention_used column to users table")
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

            if 'exclude_from_subscription' not in srv_existing:
                await conn.execute(
                    """
                    ALTER TABLE servers
                    ADD COLUMN exclude_from_subscription BOOLEAN NOT NULL DEFAULT FALSE
                    """
                )
                logging.info("Added exclude_from_subscription column to servers table")

            if 'is_bypass' not in srv_existing:
                await conn.execute(
                    "ALTER TABLE servers ADD COLUMN is_bypass BOOLEAN NOT NULL DEFAULT FALSE"
                )
                logging.info("Added is_bypass column to servers table")
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

        try:
            vk_columns = await conn.fetch(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'vpn_keys'
                """
            )
            vk_existing = {row['column_name'] for row in vk_columns}
            if 'traffic_lifetime_bytes' not in vk_existing:
                await conn.execute(
                    "ALTER TABLE vpn_keys ADD COLUMN traffic_lifetime_bytes BIGINT NOT NULL DEFAULT 0"
                )
                logging.info("Added traffic_lifetime_bytes column to vpn_keys table")
            if 'traffic_period_baseline_bytes' not in vk_existing:
                await conn.execute(
                    "ALTER TABLE vpn_keys ADD COLUMN traffic_period_baseline_bytes BIGINT"
                )
                logging.info("Added traffic_period_baseline_bytes column to vpn_keys table")
            if 'traffic_last_sync_at' not in vk_existing:
                await conn.execute(
                    "ALTER TABLE vpn_keys ADD COLUMN traffic_last_sync_at TIMESTAMP"
                )
                logging.info("Added traffic_last_sync_at column to vpn_keys table")
        except Exception as e:
            logging.warning(f"Could not migrate vpn_keys traffic columns: {e}")

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

        # Таблица для настроек лимитов устройств
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_limit_settings (
                id SERIAL PRIMARY KEY,
                max_devices INTEGER NOT NULL DEFAULT 5,
                extra_price_rub INTEGER NOT NULL DEFAULT 1000,
                extra_price_stars INTEGER NOT NULL DEFAULT 10,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        try:
            dl_count = await conn.fetchval('SELECT COUNT(*) FROM device_limit_settings')
            if dl_count == 0:
                await conn.execute(
                    '''
                    INSERT INTO device_limit_settings (max_devices, extra_price_rub, extra_price_stars, updated_at)
                    VALUES (5, 1000, 10, CURRENT_TIMESTAMP)
                    '''
                )
        except Exception as e:
            logging.warning(f"Could not initialize device_limit_settings: {e}")

        # Активные устройства пользователя по обращениям к /sub
        try:
            await conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS user_device_fingerprints (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    user_agent TEXT,
                    ip_address TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, fingerprint),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                '''
            )
            await conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_user_device_fp_user_seen ON user_device_fingerprints(user_id, last_seen)'
            )
        except Exception as e:
            logging.warning(f"Could not create user_device_fingerprints table: {e}")

        # Месячный лимит трафика (ГБ) — глобальные настройки
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS traffic_settings (
                    id SERIAL PRIMARY KEY,
                    default_monthly_gb INTEGER NOT NULL DEFAULT 50,
                    panel_sync_min_seconds INTEGER NOT NULL DEFAULT 240,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            tc = await conn.fetchval("SELECT COUNT(*) FROM traffic_settings")
            if tc == 0:
                await conn.execute(
                    """
                    INSERT INTO traffic_settings (default_monthly_gb, panel_sync_min_seconds, updated_at)
                    VALUES (50, 240, CURRENT_TIMESTAMP)
                    """
                )
            ts_cols = await conn.fetch(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'traffic_settings'
                """
            )
            ts_names = {r["column_name"] for r in ts_cols}
            if "tg_relay_server_id" not in ts_names:
                await conn.execute(
                    """
                    ALTER TABLE traffic_settings
                    ADD COLUMN tg_relay_server_id INTEGER REFERENCES servers(id)
                    """
                )
                logging.info("Added tg_relay_server_id to traffic_settings")
        except Exception as e:
            logging.warning(f"Could not create traffic_settings: {e}")

        # Пакеты дополнительного трафика (ГБ) — хранятся в БД, редактируются в админке
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gb_pack_products (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                gb_amount INTEGER NOT NULL,
                price_rub INTEGER NOT NULL DEFAULT 0,
                price_stars INTEGER NOT NULL DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                display_order INTEGER DEFAULT 100,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        try:
            gbp_cols = await conn.fetch(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'gb_pack_products'
                """
            )
            gbp_existing = {r["column_name"] for r in gbp_cols}
            if "updated_at" not in gbp_existing:
                await conn.execute(
                    "ALTER TABLE gb_pack_products ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                )
                logging.info("Added updated_at column to gb_pack_products")
            if "display_order" not in gbp_existing:
                await conn.execute(
                    "ALTER TABLE gb_pack_products ADD COLUMN display_order INTEGER DEFAULT 100"
                )
                logging.info("Added display_order column to gb_pack_products")
            if "is_active" not in gbp_existing:
                await conn.execute(
                    "ALTER TABLE gb_pack_products ADD COLUMN is_active BOOLEAN DEFAULT TRUE"
                )
                logging.info("Added is_active column to gb_pack_products")
        except Exception as e:
            logging.warning(f"Could not migrate gb_pack_products columns: {e}")
        
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

        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS esim_beta_requests (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    email TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'rejected')),
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    resolved_at TIMESTAMPTZ,
                    resolved_by BIGINT
                )
                """
            )
            await conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS esim_beta_one_pending_per_user
                ON esim_beta_requests (user_id) WHERE (status = 'pending')
                """
            )
        except Exception as e:
            logging.warning(f"Could not create esim_beta_requests table: {e}")

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
            # Отпечаток клиента /sub (User-Agent + Client Hints), без сырого IP
            try:
                col = await conn.fetchval(
                    """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'subscription_usage_logs'
                      AND column_name = 'device_fingerprint'
                    """
                )
                if not col:
                    await conn.execute(
                        "ALTER TABLE subscription_usage_logs ADD COLUMN device_fingerprint VARCHAR(64)"
                    )
                await conn.execute(
                    """
                    UPDATE subscription_usage_logs
                    SET device_fingerprint = md5(
                        'ua:' || regexp_replace(lower(trim(coalesce(user_agent, ''))), '\\s+', ' ', 'g')
                    )
                    WHERE device_fingerprint IS NULL
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sub_usage_user_fp ON subscription_usage_logs(user_id, device_fingerprint)"
                )
            except Exception as e:
                logging.warning(f"Could not migrate subscription_usage_logs.device_fingerprint: {e}")
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

        # Bypass traffic notifications (20%, 10%, 0%)
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS bypass_traffic_notifications (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    notification_type TEXT NOT NULL,
                    bypass_period_start DATE NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, notification_type, bypass_period_start),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
        except Exception as e:
            logging.warning(f"Could not create bypass_traffic_notifications table: {e}")

        # Tier pricing (admin-managed prices for Lite/Standard/Pro tiers)
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS tier_price_settings (
                    id SERIAL PRIMARY KEY,
                    tier TEXT NOT NULL,
                    duration_months INTEGER NOT NULL,
                    price_rub INTEGER NOT NULL,
                    price_stars INTEGER NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tier, duration_months)
                )
            ''')
        except Exception as e:
            logging.warning(f"Could not create tier_price_settings table: {e}")

        # Bypass pack products (admin-managed bypass GB packs)
        try:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS bypass_pack_products (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    gb_amount INTEGER NOT NULL,
                    price_rub INTEGER NOT NULL DEFAULT 0,
                    price_stars INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    display_order INTEGER DEFAULT 100,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            bp_count = await conn.fetchval('SELECT COUNT(*) FROM bypass_pack_products')
            if bp_count == 0:
                await conn.execute('''
                    INSERT INTO bypass_pack_products (title, gb_amount, price_rub, price_stars, display_order)
                    VALUES
                        ('10 ГБ bypass', 10, 5900, 59, 10),
                        ('30 ГБ bypass', 30, 12900, 129, 20),
                        ('100 ГБ bypass', 100, 29900, 299, 30),
                        ('300 ГБ bypass', 300, 69900, 699, 40)
                ''')
        except Exception as e:
            logging.warning(f"Could not create bypass_pack_products table: {e}")

async def count_active_devices(conn, user_id: int, hours: int = 6) -> tuple[int, int]:
    """
    Count distinct client fingerprints for /sub in the last N hours (not raw IP).
    Returns (active_device_count, device_limit).
    """
    row = await conn.fetchrow(
        """
        SELECT
            (SELECT COUNT(DISTINCT COALESCE(
                device_fingerprint,
                md5(
                    'ua:' || regexp_replace(
                        lower(trim(coalesce(user_agent, ''))), '\\s+', ' ', 'g'
                    )
                )
            ))
             FROM subscription_usage_logs
             WHERE user_id = $1
               AND timestamp >= NOW() - ($2 || ' hours')::interval
            ) AS device_count,
            COALESCE(u.device_limit, 5) AS device_limit
        FROM users u
        WHERE u.user_id = $1
        """,
        user_id, str(hours),
    )
    if not row:
        return 0, 5
    return int(row["device_count"]), int(row["device_limit"])

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


async def log_subscription_usage(
    user_id: int,
    user_agent: str,
    ip_address: str,
    device_fingerprint: str,
) -> None:
    """Логирует запрос подписки пользователя (с отпечатком клиента, без привязки к IP в лимите)."""
    try:
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO subscription_usage_logs
                (user_id, user_agent, ip_address, device_fingerprint, timestamp)
                VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                """,
                user_id,
                user_agent,
                ip_address,
                device_fingerprint,
            )
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

    merged_traffic_anchor = tgt.get("traffic_anchor_day") or src.get("traffic_anchor_day")
    merged_traffic_bonus = int(src.get("traffic_bonus_gb") or 0) + int(tgt.get("traffic_bonus_gb") or 0)
    merged_traffic_used = max(
        int(src.get("traffic_used_bytes") or 0),
        int(tgt.get("traffic_used_bytes") or 0),
    )
    lim_t = tgt.get("traffic_limit_gb")
    lim_s = src.get("traffic_limit_gb")
    merged_traffic_limit = lim_t if lim_t is not None else lim_s

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
            last_activity = COALESCE(last_activity, CURRENT_TIMESTAMP),
            traffic_anchor_day = $9,
            traffic_period_start = NULL,
            traffic_period_end_excl = NULL,
            traffic_used_bytes = $10,
            traffic_bonus_gb = $11,
            traffic_limit_gb = $12,
            traffic_last_sync_at = NULL
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
        merged_traffic_anchor,
        merged_traffic_used,
        merged_traffic_bonus,
        merged_traffic_limit,
    )

    await conn.execute("DELETE FROM users WHERE user_id = $1", source_user_id)
    logging.info("Merged user %s into %s", source_user_id, target_user_id)

