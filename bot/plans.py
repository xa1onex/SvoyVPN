"""
Модуль для работы с планами подписки.

Тарифы: Free (бесплатный) и Plus (платный).
Legacy-тарифы lite/standard/pro сохранены только для исторических записей в БД.
"""
from __future__ import annotations

import copy
import logging
from datetime import date, datetime
from typing import Any, Dict

from .database import get_connection
from .custom_emojis import E, e, lbl, btn, emoji_button, raw

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier definitions (new system)
# ---------------------------------------------------------------------------

FREE_TIER_ID = "free"
PAID_TIER_IDS = ("plus",)
# Исторические тарифы (lite/standard/pro) — только для legacy-записей в БД.
LEGACY_TIER_IDS = ("lite", "standard", "pro")
# Все платные ID включая legacy — для SQL-фильтров исторических данных.
ALL_PAID_TIER_IDS = PAID_TIER_IDS + LEGACY_TIER_IDS

FREE_SUBSCRIPTION_END = date(2099, 12, 31)
# Даты >= порога — «бессрочный» sentinel (Free), не продлеваем при покупке тарифа.
SENTINEL_SUBSCRIPTION_END_THRESHOLD = date(2090, 1, 1)

TIERS = {
    "free": {
        "name": "Free",
        "bypass_gb": 5,
        "max_devices": 1,
        "priority": "normal",
        "features": [
            "5 ГБ bypass в месяц",
            "Один безлимитный сервер VPN",
            "1 устройство",
        ],
    },
    "plus": {
        "name": "Plus",
        "bypass_gb": 50,
        "max_devices": 999,  # безлимит устройств
        "priority": "high",
        "features": [
            "50 ГБ bypass в месяц",
            "YouTube / TikTok / AI работают",
            "Подключение за 30 сек",
            "Безлимит устройств",
        ],
    },
    # Legacy-тарифы: отображение для старых записей в БД
    "lite": {
        "name": "Plus",  # показываем как Plus в UI
        "bypass_gb": 50,
        "max_devices": 999,
        "priority": "high",
        "features": ["50 ГБ bypass в месяц", "YouTube / TikTok / AI работают", "Безлимит устройств"],
    },
    "standard": {
        "name": "Plus",
        "bypass_gb": 50,
        "max_devices": 999,
        "priority": "high",
        "features": ["50 ГБ bypass в месяц", "YouTube / TikTok / AI работают", "Безлимит устройств"],
    },
    "pro": {
        "name": "Plus",
        "bypass_gb": 50,
        "max_devices": 999,
        "priority": "high",
        "features": ["50 ГБ bypass в месяц", "YouTube / TikTok / AI работают", "Безлимит устройств"],
    },
}

# Цены тарифов (price_rub в копейках, price_stars — номинал)
TIER_PLANS_BASE: Dict[str, Dict[str, Any]] = {
    # Plus — 1 месяц (30 дней)
    "plus_1m": {
        "tier": "plus",
        "title": "Plus · 1 месяц",
        "duration": 1,
        "bypass_gb": 50,
        "max_devices": 999,
        "price_rub": 14900,     # 149₽/мес
        "price_stars": 149,
    },
    # Plus — 12 месяцев (365 дней, ~83₽/мес)
    "plus_12m": {
        "tier": "plus",
        "title": "Plus · 12 месяцев",
        "duration": 12,
        "bypass_gb": 50,
        "max_devices": 999,
        "price_rub": 99900,     # 999₽/год
        "price_stars": 999,
    },
}

# Bypass GB pack defaults (also stored in bypass_pack_products table)
BYPASS_PACKS_DEFAULT = [
    {"gb": 10, "price_rub": 5900, "price_stars": 59},
    {"gb": 30, "price_rub": 12900, "price_stars": 129},
    {"gb": 100, "price_rub": 29900, "price_stars": 299},
    {"gb": 300, "price_rub": 69900, "price_stars": 699},
]

# Порядок тарифов (один платный тариф)
TIER_ORDER = ["plus"]

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
        "title": f"1 месяц {E.hot}",
        "duration": 1,
        "traffic_gb": 100,
        "price_rub": 14900,
        "price_stars": 149,
        "new_user": False,
    },
    "3_months_renew": {
        "title": f"3 месяца {E.hot}",
        "duration": 3,
        "traffic_gb": 300,
        "price_rub": 39900,
        "price_stars": 399,
        "new_user": False,
    },
    "6_months_renew": {
        "title": f"6 месяцев {E.hot}",
        "duration": 6,
        "traffic_gb": 600,
        "price_rub": 74900,
        "price_stars": 749,
        "new_user": False,
    },
    "12_months_renew": {
        "title": f"12 месяцев {E.hot}",
        "duration": 12,
        "traffic_gb": 1200,
        "price_rub": 119900,
        "price_stars": 1199,
        "new_user": False,
    },
}

# Актуальные планы продажи: только Plus на месяц и на год.
ACTIVE_TIER_PLAN_IDS = frozenset({"plus_1m", "plus_12m"})

# Старые subscription-планы (1/3/6/12 мес.) — только для истории в payments.
LEGACY_SUBSCRIPTION_PLAN_IDS = frozenset(
    list(SUBSCRIPTION_PLANS_BASE.keys()) + list(RENEWAL_PLANS_BASE.keys())
)


def is_active_tier_plan(plan_id: str | None) -> bool:
    return bool(plan_id) and plan_id in ACTIVE_TIER_PLAN_IDS


def is_legacy_subscription_plan(plan_id: str | None) -> bool:
    """Старые планы подписки (3/6 мес. и т.д.) — больше не продаются."""
    if not plan_id:
        return False
    if plan_id in LEGACY_SUBSCRIPTION_PLAN_IDS:
        return True
    base = plan_id.replace("_renew", "")
    if base in SUBSCRIPTION_PLANS_BASE:
        return True
    for legacy_tier in LEGACY_TIER_IDS:
        if plan_id.startswith(f"{legacy_tier}_"):
            return True
    return False


def tier_plan_uses_yookassa_autopay_binding(plan_id: str) -> bool:
    """Tier-план с привязкой карты для автосписания."""
    return plan_id in ACTIVE_TIER_PLAN_IDS

PAYMENT_METHODS = {
    "stars": {"title": "Telegram Stars", "currency": "XTR"},
    "yookassa": {"title": "ЮKassa", "currency": "RUB"},
    "cryptopay": {"title": "Crypto Pay", "currency": "RUB"},
}

# Подписка Plus — только карта / crypto (без Stars).
SUBSCRIPTION_PAYMENT_METHODS = {
    k: v for k, v in PAYMENT_METHODS.items() if k != "stars"
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
    return {k: v for k, v in plans.items() if k in ACTIVE_TIER_PLAN_IDS}


async def get_tier_plans_for_tier(tier: str) -> Dict[str, Dict[str, Any]]:
    """Get plans filtered by specific tier."""
    all_plans = await get_tier_plans()
    return {k: v for k, v in all_plans.items() if v.get("tier") == tier}


async def get_plus_per_month_from_yearly() -> int:
    """₽/мес при оплате Plus на год (годовая цена из БД ÷ 12, как в экране тарифов)."""
    plans = await get_tier_plans()
    plan = plans.get("plus_12m") or TIER_PLANS_BASE["plus_12m"]
    return int(plan["price_rub"]) // 1200


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


def is_paid_tier(tier: str | None) -> bool:
    """Возвращает True для активного платного тарифа (plus) и legacy-тарифов."""
    return tier in ALL_PAID_TIER_IDS


def subscription_end_date(subscription_end: object) -> date | None:
    if subscription_end is None:
        return None
    if isinstance(subscription_end, datetime):
        return subscription_end.date()
    if isinstance(subscription_end, date):
        return subscription_end
    if isinstance(subscription_end, str):
        return datetime.strptime(subscription_end.split()[0], "%Y-%m-%d").date()
    return None


def is_subscription_active(
    pay_subscribed: bool | None,
    subscription_end: object,
) -> bool:
    if not pay_subscribed or subscription_end is None:
        return False
    end = subscription_end_date(subscription_end)
    if end is None:
        return False
    return end >= datetime.now().date()


def is_sentinel_subscription_end(subscription_end: object) -> bool:
    """Бессрочная/служебная дата (Free до 2099), не реальный срок платной подписки."""
    end = subscription_end_date(subscription_end)
    if end is None:
        return False
    return end >= SENTINEL_SUBSCRIPTION_END_THRESHOLD


def should_reset_subscription_period_on_purchase(
    *,
    pay_subscribed: bool | None,
    subscription_end: object,
    subscription_tier: str | None,
) -> bool:
    """
    Новая платная подписка считается от сегодня, если:
    - нет активной подписки;
    - тариф Free;
    - дата окончания — sentinel (2099 и т.п.).
    Иначе — продление от текущей даты окончания.
    """
    if not pay_subscribed or subscription_end is None:
        return True
    tier = (subscription_tier or "").strip() or FREE_TIER_ID
    if tier == FREE_TIER_ID:
        return True
    if is_sentinel_subscription_end(subscription_end):
        return True
    end = subscription_end_date(subscription_end)
    if end is None:
        return True
    return end < datetime.now().date()


def format_subscription_end_for_display(subscription_end: object) -> str | None:
    """Дата для UI; sentinel не показываем как «до 2099/2100»."""
    if is_sentinel_subscription_end(subscription_end):
        return None
    end = subscription_end_date(subscription_end)
    if end is None:
        return None
    return end.strftime("%d.%m.%Y")


# ---------------------------------------------------------------------------
# Upgrade logic (упрощено: один платный тариф Plus, upgrade не нужен)
# ---------------------------------------------------------------------------

def can_upgrade(current_tier: str, target_tier: str) -> bool:
    """Переход с Free на Plus разрешён; нет многоуровневых апгрейдов."""
    if current_tier == FREE_TIER_ID and target_tier in TIER_ORDER:
        return True
    return False


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
    Актуальные планы для UI и уведомлений: только plus_1m и plus_12m.
    Возвращает (plans, is_renew, show_discount).
    """
    is_renew = False
    days_remaining = 0

    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT pay_subscribed, subscription_end FROM users WHERE user_id = $1", user_id
        )
        if row and row["pay_subscribed"] and row["subscription_end"]:
            end_date = row["subscription_end"]
            if isinstance(end_date, str):
                end_date = datetime.strptime(end_date.split()[0], "%Y-%m-%d").date()
            elif hasattr(end_date, "date"):
                end_date = end_date.date()
            if end_date >= datetime.now().date():
                is_renew = True
                days_remaining = (end_date - datetime.now().date()).days

    show_discount = False
    async with get_connection() as conn:
        discount_settings = await conn.fetchrow(
            "SELECT days_threshold, enable_for_all FROM discount_settings ORDER BY id DESC LIMIT 1"
        )
        if discount_settings and discount_settings["enable_for_all"]:
            show_discount = True
        elif is_renew and discount_settings:
            threshold = discount_settings["days_threshold"]
            if threshold and threshold > 0:
                show_discount = days_remaining <= threshold

    all_plans = await get_tier_plans()
    active_plans = {
        pid: copy.deepcopy(pdata)
        for pid, pdata in all_plans.items()
        if pid in ACTIVE_TIER_PLAN_IDS
    }
    return active_plans, is_renew, show_discount


async def build_expiry_reminder_markup(user_id: int):
    """Клавиатура напоминаний: Plus 1 мес / 12 мес по актуальным ценам."""
    from aiogram.types import InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT subscription_tier, tier_duration_months FROM users WHERE user_id = $1",
            user_id,
        )

    duration = int((row["tier_duration_months"] if row else None) or 1)
    plans = await get_tier_plans()
    builder = InlineKeyboardBuilder()

    preferred = "plus_12m" if duration >= 12 else "plus_1m"
    for plan_id in (preferred, "plus_1m" if preferred != "plus_1m" else "plus_12m"):
        plan = plans.get(plan_id)
        if not plan:
            continue
        builder.row(
            btn("{plan['title']} — {format_price_rub(plan['price_rub'])}", "card",
                callback_data=f"tier_pay:{plan_id}",
            )
        )

    builder.row(
        btn("Все тарифы", "plus", callback_data="open_tiers"),
        btn("Подарок", "gift", callback_data="open_invite"),
    )
    return builder, "Plus"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_price_rub(price_cents: int) -> str:
    """Форматирует цену в рублях"""
    return f"{price_cents // 100}₽"


def format_price_stars(price_stars: int) -> str:
    """Форматирует цену в звездах"""
    return f"{price_stars}{E.star}"


def format_price_both(price_rub: int, price_stars: int) -> str:
    """Форматирует цену в рублях и звездах"""
    return f"{format_price_rub(price_rub)} | {format_price_stars(price_stars)}"


def _strikethrough_plain(text: str) -> str:
    """Зачёркивание для текста кнопок (Telegram не поддерживает HTML в кнопках)."""
    return "".join(c + "\u0336" for c in text)


def format_tier_monthly_price_html(
    price_cents: int, base_cents: int | None = None
) -> str:
    """HTML: 199₽/мес или <s>199₽</s> <b>149₽</b>/мес"""
    rub = price_cents // 100
    if base_cents and base_cents > price_cents:
        return f"<s>{base_cents // 100}₽</s> <b>{rub}₽</b>/мес"
    return f"<b>{rub}₽</b>/мес"


def format_tier_monthly_price_button(
    price_cents: int, base_cents: int | None = None
) -> str:
    """Подпись цены для inline-кнопки."""
    rub = price_cents // 100
    if base_cents and base_cents > price_cents:
        old = _strikethrough_plain(f"{base_cents // 100}₽")
        return f"{old} {rub}₽/мес"
    return f"{rub}₽/мес"


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
