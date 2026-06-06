"""
Учёт пробного периода «Plus за 1₽».

trial_used = TRUE только после успешной оплаты триала (1₽),
не при нажатии кнопки и не при регистрации по реферальной ссылке.
"""
from __future__ import annotations

import asyncpg

# Пробный платёж: 1₽, plan plus_1m (отдельно от полной цены 149₽+).
TRIAL_PAYMENT_AMOUNT_KOPECKS = 100
TRIAL_PAYMENT_PLAN_ID = "plus_1m"


async def get_trial_days(conn: asyncpg.Connection) -> int:
    row = await conn.fetchrow(
        "SELECT days FROM trial_settings ORDER BY id DESC LIMIT 1"
    )
    try:
        return max(0, int(row["days"])) if row and row["days"] is not None else 0
    except (TypeError, ValueError):
        return 0


async def has_completed_trial_payment(
    conn: asyncpg.Connection,
    user_id: int,
) -> bool:
    """Был ли у пользователя успешный платёж пробного периода (1₽)."""
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM payments
                WHERE user_id = $1
                  AND status = 'completed'
                  AND currency = 'RUB'
                  AND plan_id = $2
                  AND amount = $3
            )
            """,
            user_id,
            TRIAL_PAYMENT_PLAN_ID,
            TRIAL_PAYMENT_AMOUNT_KOPECKS,
        )
    )


async def user_eligible_for_trial_offer(conn: asyncpg.Connection, user_id: int) -> bool:
    """
    Можно ли показать/активировать «Plus за 1₽».

    Free с датой 2099 (sentinel) — eligible.
    Активный платный Plus — нет.
    Уже оплативший триал 1₽ — нет.
    """
    if await has_completed_trial_payment(conn, user_id):
        return False
    if await get_trial_days(conn) <= 0:
        return False

    from .plans import (
        ALL_PAID_TIER_IDS,
        FREE_TIER_ID,
        is_sentinel_subscription_end,
        is_subscription_active,
    )

    row = await conn.fetchrow(
        """
        SELECT subscription_tier, pay_subscribed, subscription_end
        FROM users WHERE user_id = $1
        """,
        user_id,
    )
    if not row:
        return True

    tier = (row["subscription_tier"] or FREE_TIER_ID).strip() or FREE_TIER_ID
    if tier not in ALL_PAID_TIER_IDS:
        return True
    if not is_subscription_active(row["pay_subscribed"], row["subscription_end"]):
        return True
    if is_sentinel_subscription_end(row["subscription_end"]):
        return True
    return False


async def sync_trial_used_flag(conn: asyncpg.Connection, user_id: int) -> bool:
    """
    Приводит users.trial_used в соответствие с фактом оплаты триала.
    Возвращает новое значение trial_used.
    """
    used = await has_completed_trial_payment(conn, user_id)
    await conn.execute(
        "UPDATE users SET trial_used = $1 WHERE user_id = $2",
        used,
        user_id,
    )
    return used
