"""
Учёт пробного периода «Plus за 1₽».

trial_used = TRUE только после успешной оплаты триала (1₽),
не при регистрации по реферальной ссылке.
"""
from __future__ import annotations

import asyncpg

# Пробный платёж: 1₽, plan plus_1m (отдельно от полной цены 149₽+).
TRIAL_PAYMENT_AMOUNT_KOPECKS = 100
TRIAL_PAYMENT_PLAN_ID = "plus_1m"


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
