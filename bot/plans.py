"""
Модуль для работы с планами подписки.

Новая система тарифов: Lite / Standard / Pro с bypass-лимитами.
Legacy-планы сохранены для обратной совместимости.
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime
from typing import Any, Dict

from .database import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier definitions (new system)
# ---------------------------------------------------------------------------

TIERS = {
    "lite": {
        "name": "Lite",
        "bypass_gb": 30,
        "max_devices": 3,
        "priority": "normal",
        "features": ["30 ГБ bypass", "Безлимит обычного VPN", "До 3 устройств"],
    },
    "standard": {
        "name": "Standard",
        "bypass_gb": 100,
        "max_devices": 5,
        "priority": "high",
        "features": [
            "100 ГБ bypass",
            "Приоритет bypass",
            "Безлимит обычного VPN",
            "До 5 устройств",
            "Автопродление по карте (ЮKassa)",
        ],
    },
    "pro": {
        "name": "Pro",
        "bypass_gb": 300,
        "max_devices": 10,
        "priority": "highest",
        "features": [
            "300 ГБ bypass",
            "Высокий приоритет bypass",
            "Безлимит обычного VPN",
            "До 10 устройств",
        ],
    },
}

# Base prices for tiers (price_rub in kopecks, price_stars = face value)
TIER_PLANS_BASE: Dict[str, Dict[str, Any]] = {
    # Lite
    "lite_1m": {
        "tier": "lite",
        "title": "Lite · 1 месяц",
        "duration": 1,
        "bypass_gb": 30,
        "max_devices": 3,
        "price_rub": 9900,      # 99₽
        "price_stars": 99,
    },
    # Standard (ЮKassa: сохранение карты для автопродления — см. recurring)
    "standard_1m": {
        "tier": "standard",
        "title": "Standard · 1 месяц",
        "duration": 1,
        "bypass_gb": 100,
        "max_devices": 5,
        "price_rub": 19900,     # 199₽
        "price_stars": 199,
    },
    # Pro
    "pro_1m": {
        "tier": "pro",
        "title": "Pro · 1 месяц",
        "duration": 1,
        "bypass_gb": 300,
        "max_devices": 10,
        "price_rub": 39900,     # 399₽
        "price_stars": 399,
    },
}

# Bypass GB pack defaults (also stored in bypass_pack_products table)
BYPASS_PACKS_DEFAULT = [
    {"gb": 10, "price_rub": 5900, "price_stars": 59},
    {"gb": 30, "price_rub": 12900, "price_stars": 129},
    {"gb": 100, "price_rub": 29900, "price_stars": 299},
    {"gb": 300, "price_rub": 69900, "price_stars": 699},
]

# Legacy fair-use limit (hidden, for old subscribers)
LEGACY_FAIR_USE_GB = 500

# Tier priority order for upgrades
TIER_ORDER = ["lite", "standard", "pro"]


def tier_plan_uses_yookassa_autopay_binding(plan_id: str) -> bool:
    """Первый платёж Standard через ЮKassa с save_payment_method для автоплатежей."""
    return plan_id == "standard_1m"

# ---------------------------------------------------------------------------
# Legacy plans (kept for backward compat with old subscriptions)
# ---------------------------------------------------------------------------

SUBSCRIPTION_PLANS_BASE = {
    "1_month": {
        "title": "1 месяц",
        "duration": 1,
        "traffic_gb": 100,
        "price_rub": 19900,
        "price_stars": 199,
        "new_user": True,
    },
    "3_months": {
        "title": "3 месяца",
        "duration": 3,
        "traffic_gb": 300,
        "price_rub": 49900,
        "price_stars": 499,
        "new_user": True,
    },
    "6_months": {
        "title": "6 месяцев",
        "duration": 6,
        "traffic_gb": 600,
        "price_rub": 89900,
        "price_stars": 899,
        "new_user": True,
    },
    "12_months": {
        "title": "12 месяцев",
        "duration": 12,
        "traffic_gb": 1200,
        "price_rub": 149900,
        "price_stars": 1499,
        "new_user": True,
    },
}

RENEWAL_PLANS_BASE = {
    "1_month_renew": {
        "title": "1 месяц 🔥",
        "duration": 1,
        "traffic_gb": 100,
        "price_rub": 14900,
        "price_stars": 149,
        "new_user": False,
    },
    "3_months_renew": {
        "title": "3 месяца 🔥",
        "duration": 3,
        "traffic_gb": 300,
        "price_rub": 39900,
        "price_stars": 399,
        "new_user": False,
    },
    "6_months_renew": {
        "title": "6 месяцев 🔥",
        "duration": 6,
        "traffic_gb": 600,
        "price_rub": 74900,
        "price_stars": 749,
        "new_user": False,
    },
    "12_months_renew": {
        "title": "12 месяцев 🔥",
        "duration": 12,
        "traffic_gb": 1200,
        "price_rub": 119900,
        "price_stars": 1199,
        "new_user": False,
    },
}

PAYMENT_METHODS = {
    "stars": {"title": "Telegram Stars", "currency": "XTR"},
    "yookassa": {"title": "ЮKassa", "currency": "RUB"},
    "cryptopay": {"title": "Crypto Pay", "currency": "RUB"},
}

DEFAULT_MAX_DEVICES = 5
DEFAULT_EXTRA_RUB_KOPECKS = 1000
DEFAULT_EXTRA_STARS = 10


# ---------------------------------------------------------------------------
# Tier plan retrieval (with dynamic price overrides)
# ---------------------------------------------------------------------------

async def get_tier_plans() -> Dict[str, Dict[str, Any]]:
    """Get tier plans with dynamic price overrides from tier_price_settings."""
    plans = copy.deepcopy(TIER_PLANS_BASE)
    try:
        async with get_connection() as conn:
            table_exists = await conn.fetchval('''
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'tier_price_settings'
                )
            ''')
            if table_exists:
                rows = await conn.fetch(
                    'SELECT tier, duration_months, price_rub, price_stars FROM tier_price_settings'
                )
                for row in rows:
                    plan_key = f"{row['tier']}_{row['duration_months']}m"
                    if plan_key in plans:
                        if row['price_rub'] is not None:
                            plans[plan_key]['price_rub'] = int(row['price_rub'])
                        if row['price_stars'] is not None:
                            plans[plan_key]['price_stars'] = int(row['price_stars'])
    except Exception as e:
        logger.warning(f"Error loading tier_price_settings: {e}, using defaults")
    return plans


async def get_tier_plans_for_tier(tier: str) -> Dict[str, Dict[str, Any]]:
    """Get plans filtered by specific tier."""
    all_plans = await get_tier_plans()
    return {k: v for k, v in all_plans.items() if v.get("tier") == tier}


async def get_bypass_packs() -> list[Dict[str, Any]]:
    """Get bypass GB packs from DB."""
    try:
        async with get_connection() as conn:
            rows = await conn.fetch('''
                SELECT id, title, gb_amount, price_rub, price_stars
                FROM bypass_pack_products
                WHERE is_active = TRUE
                ORDER BY gb_amount ASC, display_order ASC
            ''')
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_tier_bypass_gb(tier: str) -> int:
    """Monthly bypass GB for a given tier."""
    t = TIERS.get(tier)
    return t["bypass_gb"] if t else 0


def get_tier_max_devices(tier: str) -> int:
    """Max devices for a given tier."""
    t = TIERS.get(tier)
    return t["max_devices"] if t else 1


def get_tier_priority(tier: str) -> str:
    """Traffic priority for a given tier."""
    t = TIERS.get(tier)
    return t["priority"] if t else "normal"


# ---------------------------------------------------------------------------
# Upgrade logic
# ---------------------------------------------------------------------------

def can_upgrade(current_tier: str, target_tier: str) -> bool:
    """Check if upgrade from current to target tier is valid."""
    if current_tier not in TIER_ORDER or target_tier not in TIER_ORDER:
        return False
    return TIER_ORDER.index(target_tier) > TIER_ORDER.index(current_tier)


async def calculate_upgrade_price(
    user_id: int, target_tier: str, target_duration: int
) -> Dict[str, Any]:
    """
    Calculate upgrade cost: new tier price - already paid amount.
    Returns {'price_rub': int, 'price_stars': int, 'valid': bool, 'reason': str}.
    """
    plans = await get_tier_plans()
    target_key = f"{target_tier}_{target_duration}m"
    if target_key not in plans:
        return {"valid": False, "reason": "Целевой тариф не найден", "price_rub": 0, "price_stars": 0}

    target_plan = plans[target_key]

    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT subscription_tier, tier_price_paid, tier_duration_months,
                   subscription_end, pay_subscribed
            FROM users WHERE user_id = $1
            """,
            user_id,
        )
    if not row or not row["pay_subscribed"]:
        return {"valid": False, "reason": "Нет активной подписки", "price_rub": 0, "price_stars": 0}

    current_tier = row["subscription_tier"] or "legacy"
    if current_tier == "legacy":
        return {"valid": False, "reason": "Legacy подписки не поддерживают апгрейд через эту систему", "price_rub": 0, "price_stars": 0}

    if not can_upgrade(current_tier, target_tier):
        return {"valid": False, "reason": "Невозможно понизить тариф", "price_rub": 0, "price_stars": 0}

    paid = int(row["tier_price_paid"] or 0)
    diff_rub = max(0, target_plan["price_rub"] - paid)
    diff_stars = max(0, target_plan["price_stars"] - int(paid / 100))

    return {
        "valid": True,
        "reason": "",
        "price_rub": diff_rub,
        "price_stars": diff_stars,
        "target_plan": target_plan,
        "current_tier": current_tier,
    }


# ---------------------------------------------------------------------------
# Legacy plan functions (backward compat)
# ---------------------------------------------------------------------------

async def get_subscription_plans() -> Dict[str, Dict[str, Any]]:
    """Получить планы подписки с динамическими ценами из БД"""
    plans = copy.deepcopy(SUBSCRIPTION_PLANS_BASE)
    try:
        async with get_connection() as conn:
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
                        if setting['price_rub'] is not None:
                            plans[plan_id]['price_rub'] = int(setting['price_rub'])
                        if setting['price_stars'] is not None:
                            plans[plan_id]['price_stars'] = int(setting['price_stars'])
    except Exception as e:
        logger.warning(f"Error loading price_settings: {e}, using default prices")
    return plans


async def get_renewal_plans() -> Dict[str, Dict[str, Any]]:
    """Получить планы продления с динамическими ценами из БД"""
    plans = copy.deepcopy(RENEWAL_PLANS_BASE)
    try:
        async with get_connection() as conn:
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
                        if setting['price_rub'] is not None:
                            plans[plan_id]['price_rub'] = int(setting['price_rub'])
                        if setting['price_stars'] is not None:
                            plans[plan_id]['price_stars'] = int(setting['price_stars'])
    except Exception as e:
        logger.warning(f"Error loading price_settings: {e}, using default prices")
    return plans


async def get_user_tariffs(user_id: int) -> tuple[Dict[str, Dict[str, Any]], bool, bool]:
    """
    Возвращает актуальный список планов, флаг is_renew, и флаг show_discount.
    Это единый метод для получения правильных цен для пользователя во всей экосистеме.
    """
    is_renew = False
    days_remaining = 0

    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT pay_subscribed, subscription_end FROM users WHERE user_id = $1", user_id
        )
        if row and row['pay_subscribed'] and row['subscription_end']:
            end_date = row['subscription_end']
            if isinstance(end_date, str):
                end_date = datetime.strptime(end_date.split()[0], "%Y-%m-%d").date()
            elif hasattr(end_date, 'date'):
                end_date = end_date.date()
            if end_date >= datetime.now().date():
                is_renew = True
                days_remaining = (end_date - datetime.now().date()).days

    show_discount = False
    async with get_connection() as conn:
        discount_settings = await conn.fetchrow(
            'SELECT days_threshold, enable_for_all FROM discount_settings ORDER BY id DESC LIMIT 1'
        )
        if not discount_settings:
            show_discount = (days_remaining <= 3) if is_renew else False
        else:
            if discount_settings['enable_for_all']:
                show_discount = True
            else:
                threshold = discount_settings['days_threshold']
                if not threshold or threshold <= 0:
                    show_discount = False
                else:
                    show_discount = (days_remaining <= threshold) if is_renew else False

    regular_plans = await get_subscription_plans()
    renewal_plans = await get_renewal_plans()

    if is_renew:
        active_plans = copy.deepcopy(renewal_plans)
        if not show_discount:
            for plan_id, plan_data in active_plans.items():
                base_id = plan_id.replace('_renew', '')
                base_plan = regular_plans.get(base_id, {})
                plan_data['price_rub'] = base_plan.get('price_rub', plan_data['price_rub'])
                plan_data['price_stars'] = base_plan.get('price_stars', plan_data['price_stars'])
                plan_data['title'] = plan_data['title'].replace(' 🔥', '')
    else:
        active_plans = copy.deepcopy(regular_plans)
        if show_discount:
            for plan_id, plan_data in active_plans.items():
                renew_id = f"{plan_id}_renew"
                renew_plan = renewal_plans.get(renew_id, {})
                plan_data['price_rub'] = renew_plan.get('price_rub', plan_data['price_rub'])
                plan_data['price_stars'] = renew_plan.get('price_stars', plan_data['price_stars'])
                if '🔥' not in plan_data['title']:
                    plan_data['title'] = f"{plan_data['title']} 🔥"

    return active_plans, is_renew, show_discount


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_price_rub(price_cents: int) -> str:
    """Форматирует цену в рублях"""
    return f"{price_cents // 100}₽"


def format_price_stars(price_stars: int) -> str:
    """Форматирует цену в звездах"""
    return f"{price_stars}⭐"


def format_price_both(price_rub: int, price_stars: int) -> str:
    """Форматирует цену в рублях и звездах"""
    return f"{format_price_rub(price_rub)} | {format_price_stars(price_stars)}"


# ---------------------------------------------------------------------------
# Device limit helpers (legacy compat)
# ---------------------------------------------------------------------------

async def get_device_limit_settings() -> Dict[str, int]:
    """Настройки лимитов устройств и наценки за доп. устройство."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT max_devices, extra_price_rub, extra_price_stars
            FROM device_limit_settings
            ORDER BY id DESC LIMIT 1
            """
        )
        if not row:
            return {
                "max_devices": DEFAULT_MAX_DEVICES,
                "extra_price_rub": DEFAULT_EXTRA_RUB_KOPECKS,
                "extra_price_stars": DEFAULT_EXTRA_STARS,
            }
        return {
            "max_devices": int(row["max_devices"] or DEFAULT_MAX_DEVICES),
            "extra_price_rub": int(row["extra_price_rub"] or 0),
            "extra_price_stars": int(row["extra_price_stars"] or 0),
        }


def clamp_device_count(device_count: int, max_devices: int) -> int:
    """Ограничивает количество устройств в допустимых пределах."""
    try:
        dc = int(device_count)
    except (TypeError, ValueError):
        dc = 1
    if dc < 1:
        dc = 1
    if dc > max_devices:
        dc = max_devices
    return dc


def calc_total_price_with_devices(base_price: int, device_count: int, extra_per_device: int) -> int:
    """Цена по формуле: база + (N-1) * наценка."""
    return int(base_price) + max(0, int(device_count) - 1) * int(extra_per_device)
