"""
Утилиты для работы с балансом пользователей.
Баланс хранится в копейках (INTEGER). 100 = 1₽.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Устарело: ставка задаётся в referral_tiers.py (лестница 20–40%).
REFERRAL_BONUS_PERCENT = 20

WITHDRAWAL_MIN_CENTS = 100_000  # 1000 ₽
WITHDRAWAL_FEE_PERCENT = 10


def withdrawal_fee_cents(amount_cents: int) -> int:
    return round(amount_cents * WITHDRAWAL_FEE_PERCENT / 100)


def withdrawal_payout_cents(amount_cents: int) -> int:
    return amount_cents - withdrawal_fee_cents(amount_cents)


async def get_balance(conn, user_id: int) -> int:
    """Возвращает текущий баланс пользователя в копейках."""
    row = await conn.fetchrow(
        "SELECT balance FROM user_balances WHERE user_id = $1", user_id
    )
    return row["balance"] if row else 0


async def credit_balance(
    conn,
    user_id: int,
    amount_cents: int,
    tx_type: str,
    description: str,
    related_user_id: Optional[int] = None,
) -> int:
    """Пополняет баланс пользователя. Возвращает новый баланс."""
    new_balance = await conn.fetchval(
        """
        INSERT INTO user_balances (user_id, balance, updated_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (user_id) DO UPDATE
            SET balance = user_balances.balance + $2,
                updated_at = NOW()
        RETURNING balance
        """,
        user_id,
        amount_cents,
    )
    await conn.execute(
        """
        INSERT INTO balance_transactions
            (user_id, amount, type, description, related_user_id)
        VALUES ($1, $2, $3, $4, $5)
        """,
        user_id, amount_cents, tx_type, description, related_user_id,
    )
    return new_balance


async def debit_balance(
    conn,
    user_id: int,
    amount_cents: int,
    tx_type: str,
    description: str,
) -> tuple[bool, int]:
    """
    Списывает баланс. Возвращает (успех, новый_баланс).
    Не уходит в минус.
    """
    current = await get_balance(conn, user_id)
    if current < amount_cents:
        return False, current

    new_balance = await conn.fetchval(
        """
        UPDATE user_balances
        SET balance = balance - $2, updated_at = NOW()
        WHERE user_id = $1
        RETURNING balance
        """,
        user_id,
        amount_cents,
    )
    await conn.execute(
        """
        INSERT INTO balance_transactions
            (user_id, amount, type, description)
        VALUES ($1, $2, $3, $4)
        """,
        user_id, -amount_cents, tx_type, description,
    )
    return True, new_balance


async def accrue_referral_bonus(
    conn,
    referrer_id: int,
    referred_id: int,
    payment_amount_cents: int,
    plan_title: str,
) -> int:
    """Устаревший API — используйте referral_commission.process_referral_commission."""
    from .referral_commission import referral_volume_cents
    from .referral_tiers import rate_for_volume_cents

    volume = await referral_volume_cents(
        conn, referrer_id, include_payment_cents=payment_amount_cents
    )
    rate = rate_for_volume_cents(volume)
    bonus = max(1, round(payment_amount_cents * rate / 100))
    await credit_balance(
        conn,
        referrer_id,
        bonus,
        "referral_bonus",
        f"Комиссия {rate}% · {plan_title}",
        related_user_id=referred_id,
    )
    return bonus


async def get_transaction_history(conn, user_id: int, limit: int = 10) -> list:
    """Возвращает последние транзакции пользователя."""
    rows = await conn.fetch(
        """
        SELECT amount, type, description, related_user_id, created_at
        FROM balance_transactions
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return list(rows)
