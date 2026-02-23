"""
Модуль для работы с планами подписки
"""
from typing import Dict, Any
from .database import get_connection

# Базовые планы подписки
SUBSCRIPTION_PLANS_BASE = {
    "1_month": {
        "title": "1 месяц",
        "duration": 1,
        "traffic_gb": 100,
        "price_rub": 19900,  # 199₽
        "price_stars": 199,
        "new_user": True
    },
    "3_months": {
        "title": "3 месяца",
        "duration": 3,
        "traffic_gb": 300,
        "price_rub": 49900,  # 499₽
        "price_stars": 499,
        "new_user": True
    },
    "6_months": {
        "title": "6 месяцев",
        "duration": 6,
        "traffic_gb": 600,
        "price_rub": 89900,  # 899₽
        "price_stars": 899,
        "new_user": True
    },
    "12_months": {
        "title": "12 месяцев",
        "duration": 12,
        "traffic_gb": 1200,
        "price_rub": 149900,  # 1499₽
        "price_stars": 1499,
        "new_user": True
    }
}

RENEWAL_PLANS_BASE = {
    "1_month_renew": {
        "title": "1 месяц 🔥",
        "duration": 1,
        "traffic_gb": 100,
        "price_rub": 14900,  # 149₽
        "price_stars": 149,
        "new_user": False
    },
    "3_months_renew": {
        "title": "3 месяца 🔥",
        "duration": 3,
        "traffic_gb": 300,
        "price_rub": 39900,  # 399₽
        "price_stars": 399,
        "new_user": False
    },
    "6_months_renew": {
        "title": "6 месяцев 🔥",
        "duration": 6,
        "traffic_gb": 600,
        "price_rub": 74900,  # 749₽
        "price_stars": 749,
        "new_user": False
    },
    "12_months_renew": {
        "title": "12 месяцев 🔥",
        "duration": 12,
        "traffic_gb": 1200,
        "price_rub": 119900,  # 1199₽
        "price_stars": 1199,
        "new_user": False
    }
}

# Методы оплаты
PAYMENT_METHODS = {
    "stars": {
        "title": "Telegram Stars",
        "currency": "XTR"
    },
    "yookassa": {
        "title": "ЮKassa",
        "currency": "RUB"
    }
}


async def get_subscription_plans() -> Dict[str, Dict[str, Any]]:
    """Получить планы подписки с динамическими ценами из БД"""
    plans = SUBSCRIPTION_PLANS_BASE.copy()
    try:
        async with get_connection() as conn:
            # Проверяем существование таблицы
            table_exists = await conn.fetchval('''
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'price_settings'
                )
            ''')
            if table_exists:
                price_settings = await conn.fetch('SELECT plan_id, price_rub, price_stars FROM price_settings')
                for setting in price_settings:
                    plan_id = setting['plan_id']
                    if plan_id in plans:
                        plans[plan_id]['price_rub'] = int(setting['price_rub']) if setting['price_rub'] is not None else plans[plan_id]['price_rub']
                        plans[plan_id]['price_stars'] = int(setting['price_stars']) if setting['price_stars'] is not None else plans[plan_id]['price_stars']
    except Exception as e:
        import logging
        logging.warning(f"Error loading price_settings: {e}, using default prices")
    return plans


async def get_renewal_plans() -> Dict[str, Dict[str, Any]]:
    """Получить планы продления с динамическими ценами из БД"""
    plans = RENEWAL_PLANS_BASE.copy()
    try:
        async with get_connection() as conn:
            # Проверяем существование таблицы
            table_exists = await conn.fetchval('''
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'price_settings'
                )
            ''')
            if table_exists:
                price_settings = await conn.fetch('SELECT plan_id, price_rub, price_stars FROM price_settings')
                for setting in price_settings:
                    plan_id = setting['plan_id']
                    if plan_id in plans:
                        plans[plan_id]['price_rub'] = int(setting['price_rub']) if setting['price_rub'] is not None else plans[plan_id]['price_rub']
                        plans[plan_id]['price_stars'] = int(setting['price_stars']) if setting['price_stars'] is not None else plans[plan_id]['price_stars']
    except Exception as e:
        import logging
        logging.warning(f"Error loading price_settings: {e}, using default prices")
    return plans


def format_price_rub(price_cents: int) -> str:
    """Форматирует цену в рублях"""
    return f"{price_cents // 100}₽"


def format_price_stars(price_stars: int) -> str:
    """Форматирует цену в звездах"""
    return f"{price_stars}⭐"


def format_price_both(price_rub: int, price_stars: int) -> str:
    """Форматирует цену в рублях и звездах"""
    return f"{format_price_rub(price_rub)} | {format_price_stars(price_stars)}"
