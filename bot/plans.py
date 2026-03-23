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
    },
    "cryptopay": {
        "title": "Crypto Pay",
        "currency": "RUB"
    }
}


import copy
from datetime import datetime

async def get_subscription_plans() -> Dict[str, Dict[str, Any]]:
    """Получить планы подписки с динамическими ценами из БД"""
    plans = copy.deepcopy(SUBSCRIPTION_PLANS_BASE)
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
                        if setting['price_rub'] is not None: plans[plan_id]['price_rub'] = int(setting['price_rub'])
                        if setting['price_stars'] is not None: plans[plan_id]['price_stars'] = int(setting['price_stars'])
    except Exception as e:
        import logging
        logging.warning(f"Error loading price_settings: {e}, using default prices")
    return plans


async def get_renewal_plans() -> Dict[str, Dict[str, Any]]:
    """Получить планы продления с динамическими ценами из БД"""
    plans = copy.deepcopy(RENEWAL_PLANS_BASE)
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
                        if setting['price_rub'] is not None: plans[plan_id]['price_rub'] = int(setting['price_rub'])
                        if setting['price_stars'] is not None: plans[plan_id]['price_stars'] = int(setting['price_stars'])
    except Exception as e:
        import logging
        logging.warning(f"Error loading price_settings: {e}, using default prices")
    return plans


async def get_user_tariffs(user_id: int) -> tuple[Dict[str, Dict[str, Any]], bool, bool]:
    """
    Возвращает актуальный список планов, флаг is_renew, и флаг show_discount.
    Это единый метод для получения правильных цен для пользователя во всей экосистеме.
    """
    is_renew = False
    days_remaining = 0
    
    async with get_connection() as conn:
        row = await conn.fetchrow("SELECT pay_subscribed, subscription_end FROM users WHERE user_id = $1", user_id)
        if row and row['pay_subscribed'] and row['subscription_end']:
            end_date = row['subscription_end']
            if isinstance(end_date, str):
                end_date = datetime.strptime(end_date.split()[0], "%Y-%m-%d").date()
            elif hasattr(end_date, 'date'):
                end_date = end_date.date()
            
            if end_date >= datetime.now().date():
                is_renew = True
                days_remaining = (end_date - datetime.now().date()).days
    
    # Check if discount applies
    show_discount = False
    async with get_connection() as conn:
        discount_settings = await conn.fetchrow('SELECT days_threshold, enable_for_all FROM discount_settings ORDER BY id DESC LIMIT 1')
        if not discount_settings:
            show_discount = (days_remaining <= 3) if is_renew else False
        else:
            if discount_settings['enable_for_all']:
                show_discount = True
            else:
                threshold = discount_settings['days_threshold'] or 0
                show_discount = (days_remaining <= threshold) if is_renew else False

    regular_plans = await get_subscription_plans()
    renewal_plans = await get_renewal_plans()
    
    # Base our returned plans on the renewal status
    # But ONLY apply discounted prices if show_discount is True!
    if is_renew:
        active_plans = copy.deepcopy(renewal_plans)
        if not show_discount:
            # Overwrite with regular prices
            for plan_id, plan_data in active_plans.items():
                base_id = plan_id.replace('_renew', '')
                base_plan = regular_plans.get(base_id, {})
                plan_data['price_rub'] = base_plan.get('price_rub', plan_data['price_rub'])
                plan_data['price_stars'] = base_plan.get('price_stars', plan_data['price_stars'])
                plan_data['title'] = plan_data['title'].replace(' 🔥', '')
    else:
        # Not a renewal
        active_plans = copy.deepcopy(regular_plans)
        if show_discount:
            # They get discounted prices! (From renewal plans)
            for plan_id, plan_data in active_plans.items():
                renew_id = f"{plan_id}_renew"
                renew_plan = renewal_plans.get(renew_id, {})
                plan_data['price_rub'] = renew_plan.get('price_rub', plan_data['price_rub'])
                plan_data['price_stars'] = renew_plan.get('price_stars', plan_data['price_stars'])
                if '🔥' not in plan_data['title']:
                    plan_data['title'] = f"{plan_data['title']} 🔥"

    return active_plans, is_renew, show_discount


def format_price_rub(price_cents: int) -> str:
    """Форматирует цену в рублях"""
    return f"{price_cents // 100}₽"


def format_price_stars(price_stars: int) -> str:
    """Форматирует цену в звездах"""
    return f"{price_stars}⭐"


def format_price_both(price_rub: int, price_stars: int) -> str:
    """Форматирует цену в рублях и звездах"""
    return f"{format_price_rub(price_rub)} | {format_price_stars(price_stars)}"

