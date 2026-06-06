"""
Реферальная комиссия на баланс отключена (подарок — только дни Plus).
Оставлены заглушки и утилиты для backfill-скриптов.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from aiogram import Bot

from .balance import credit_balance
from .database import get_connection
from .referral_tiers import (
    msk_year_month_sql,
    rate_for_volume_cents,
)

logger = logging.getLogger(__name__)

_COMMISSIONABLE_PAYMENT_SQL = """
    p.status = 'completed'
    AND COALESCE(p.plan_type, '') NOT IN ('balance')
    AND COALESCE(p.plan_id, '') NOT IN ('balance_topup')
    AND COALESCE(p.plan_id, '') NOT LIKE 'balance%'
    AND p.amount >= 100
"""

# plan_type / plan_id, не дающие комиссию и не входящие в оборот
_EXCLUDED_PLAN_TYPES = frozenset({"balance"})
# referral_invited (1₽) — комиссия начисляется отдельно в referral_rewards
_EXCLUDED_PLAN_IDS = frozenset({"balance_topup"})


def is_commissionable_payment(
    amount_cents: int,
    plan_type: str | None,
    plan_id: str | None,
    *,
    is_trial: bool = False,
) -> bool:
    if is_trial or amount_cents < 100:
        return False
    if (plan_type or "") in _EXCLUDED_PLAN_TYPES:
        return False
    if (plan_id or "") in _EXCLUDED_PLAN_IDS:
        return False
    if (plan_id or "").startswith("balance"):
        return False
    return True


def payment_key_for_row(
    *,
    yookassa_payment_id: str | None = None,
    telegram_payment_charge_id: str | None = None,
    payment_id: int | None = None,
) -> str:
    if yookassa_payment_id:
        return f"pay:yk:{yookassa_payment_id}"
    if telegram_payment_charge_id:
        return f"pay:tg:{telegram_payment_charge_id}"
    if payment_id is not None:
        return f"pay:id:{payment_id}"
    return ""


async def apply_referral_commission(
    bot: Bot | None,
    payer_user_id: int,
    amount_cents: int,
    product_label: str,
    *,
    plan_type: str | None = None,
    plan_id: str | None = None,
    is_trial: bool = False,
    payment_key: str | None = None,
) -> None:
    if amount_cents <= 0:
        return
    try:
        await process_referral_commission(
            payer_user_id,
            amount_cents,
            product_label,
            plan_type=plan_type,
            plan_id=plan_id,
            is_trial=is_trial,
            payment_key=payment_key,
            bot=bot,
        )
    except Exception as e:
        logger.error(
            "referral commission error payer=%s: %s",
            payer_user_id,
            e,
            exc_info=True,
        )


async def referral_volume_cents(
    conn,
    referrer_id: int,
    year: int | None = None,
    month: int | None = None,
    *,
    include_payment_cents: int = 0,
) -> int:
    """Оборот оплат приглашённых (копейки). Без year/month — за всё время."""
    ym_sql = ""
    params: list = [referrer_id]
    if year is not None and month is not None:
        ym_sql = f"AND {msk_year_month_sql('p.timestamp', 2, 3)}"
        params.extend([year, month])
    vol = await conn.fetchval(
        f"""
        SELECT COALESCE(SUM(p.amount), 0)
        FROM payments p
        INNER JOIN users u ON u.user_id = p.user_id
        WHERE u.invited_by = $1
          AND {_COMMISSIONABLE_PAYMENT_SQL}
          {ym_sql}
        """,
        *params,
    )
    return int(vol or 0) + max(0, include_payment_cents)


async def referral_earned_cents(
    conn,
    referrer_id: int,
    year: int | None = None,
    month: int | None = None,
) -> int:
    """Сумма реферальных начислений. Без year/month — за всё время."""
    ym_sql = ""
    params: list = [referrer_id]
    if year is not None and month is not None:
        ym_sql = f"AND {msk_year_month_sql('created_at', 2, 3)}"
        params.extend([year, month])
    earned = await conn.fetchval(
        f"""
        SELECT COALESCE(SUM(amount), 0)
        FROM balance_transactions
        WHERE user_id = $1
          AND type = 'referral_bonus'
          AND amount > 0
          {ym_sql}
        """,
        *params,
    )
    return int(earned or 0)


async def referral_earned_cents_month(
    conn, referrer_id: int, year: int, month: int
) -> int:
    return await referral_earned_cents(conn, referrer_id, year, month)


async def count_paying_referrals(
    conn,
    referrer_id: int,
    year: int | None = None,
    month: int | None = None,
) -> int:
    """Число приглашённых с хотя бы одной оплатой. Без year/month — за всё время."""
    ym_sql = ""
    params: list = [referrer_id]
    if year is not None and month is not None:
        ym_sql = f"AND {msk_year_month_sql('p.timestamp', 2, 3)}"
        params.extend([year, month])
    return int(
        await conn.fetchval(
            f"""
            SELECT COUNT(DISTINCT p.user_id)
            FROM payments p
            INNER JOIN users u ON u.user_id = p.user_id
            WHERE u.invited_by = $1
              AND {_COMMISSIONABLE_PAYMENT_SQL}
              {ym_sql}
            """,
            *params,
        )
        or 0
    )


async def count_paying_referrals_month(
    conn, referrer_id: int, year: int, month: int
) -> int:
    return await count_paying_referrals(conn, referrer_id, year, month)


async def _commission_already_paid(conn, payment_key: str) -> bool:
    if not payment_key:
        return False
    row = await conn.fetchval(
        """
        SELECT 1 FROM balance_transactions
        WHERE type = 'referral_bonus'
          AND description LIKE $1
        LIMIT 1
        """,
        f"%{payment_key}%",
    )
    return row is not None


async def _commission_paid_cents(conn, payment_key: str) -> int:
    if not payment_key:
        return 0
    paid = await conn.fetchval(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM balance_transactions
        WHERE type = 'referral_bonus'
          AND amount > 0
          AND description LIKE $1
        """,
        f"%{payment_key}%",
    )
    return int(paid or 0)


async def process_referral_commission(
    payer_user_id: int,
    amount_cents: int,
    product_label: str,
    *,
    plan_type: str | None = None,
    plan_id: str | None = None,
    is_trial: bool = False,
    payment_key: str | None = None,
    bot: Bot | None = None,
) -> int:
    """Рефералка — только дни Plus при регистрации; денежная комиссия отключена."""
    return 0


async def backfill_missing_referral_commissions(*, dry_run: bool = False) -> dict[str, int]:
    """
    Доначисляет комиссию по завершённым оплатам приглашённых, где её не было
    (например, старые subscription-платежи до подключения комиссии в payments.py).
    """
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT p.id, p.user_id, p.amount, p.plan_type, p.plan_id,
                   p.yookassa_payment_id, p.telegram_payment_charge_id
            FROM payments p
            INNER JOIN users u ON u.user_id = p.user_id
            WHERE u.invited_by IS NOT NULL
              AND {_COMMISSIONABLE_PAYMENT_SQL}
            ORDER BY p.timestamp ASC NULLS LAST, p.id ASC
            """
        )

    stats = {"checked": 0, "credited": 0, "skipped": 0, "cents": 0}
    for row in rows:
        stats["checked"] += 1
        amount_cents = int(row["amount"] or 0)
        plan_type = row["plan_type"]
        plan_id = row["plan_id"]
        if not is_commissionable_payment(
            amount_cents, plan_type, plan_id, is_trial=False
        ):
            stats["skipped"] += 1
            continue

        payment_key = payment_key_for_row(
            yookassa_payment_id=row["yookassa_payment_id"],
            telegram_payment_charge_id=row["telegram_payment_charge_id"],
            payment_id=row["id"],
        )
        label = str(plan_id or plan_type or "оплата")
        if dry_run:
            async with get_connection() as conn:
                if payment_key and await _commission_already_paid(conn, payment_key):
                    stats["skipped"] += 1
                    continue
            stats["credited"] += 1
            continue

        credited = await process_referral_commission(
            int(row["user_id"]),
            amount_cents,
            label,
            plan_type=plan_type,
            plan_id=plan_id,
            payment_key=payment_key or None,
            bot=None,
        )
        if credited > 0:
            stats["credited"] += 1
            stats["cents"] += credited
        else:
            stats["skipped"] += 1

    return stats


async def rebalance_referral_commissions_for_new_rates(
    *, dry_run: bool = False
) -> dict[str, int]:
    """
    Доначисляет разницу по всем прошлым оплатам: пересчёт комиссии по текущей
    шкале (20–40%) в хронологическом порядке, минус уже выплаченное.
    """
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT p.id, p.user_id, p.amount, p.plan_type, p.plan_id,
                   p.yookassa_payment_id, p.telegram_payment_charge_id,
                   u.invited_by AS referrer_id
            FROM payments p
            INNER JOIN users u ON u.user_id = p.user_id
            WHERE u.invited_by IS NOT NULL
              AND {_COMMISSIONABLE_PAYMENT_SQL}
            ORDER BY p.timestamp ASC NULLS LAST, p.id ASC
            """
        )

    volume_by_referrer: dict[int, int] = {}
    stats = {"checked": 0, "topped_up": 0, "skipped": 0, "cents": 0}

    for row in rows:
        stats["checked"] += 1
        amount_cents = int(row["amount"] or 0)
        plan_type = row["plan_type"]
        plan_id = row["plan_id"]
        referrer_id = int(row["referrer_id"])
        payer_id = int(row["user_id"])

        if not is_commissionable_payment(
            amount_cents, plan_type, plan_id, is_trial=False
        ):
            stats["skipped"] += 1
            continue

        volume_before = volume_by_referrer.get(referrer_id, 0)
        volume_after = volume_before + amount_cents
        rate = rate_for_volume_cents(volume_after)
        expected = max(1, round(amount_cents * rate / 100))

        payment_key = payment_key_for_row(
            yookassa_payment_id=row["yookassa_payment_id"],
            telegram_payment_charge_id=row["telegram_payment_charge_id"],
            payment_id=row["id"],
        )
        label = str(plan_id or plan_type or "оплата")

        async with get_connection() as conn:
            paid = await _commission_paid_cents(conn, payment_key) if payment_key else 0
            delta = expected - paid

            if delta <= 0:
                stats["skipped"] += 1
                volume_by_referrer[referrer_id] = volume_after
                continue

            if dry_run:
                stats["topped_up"] += 1
                stats["cents"] += delta
                volume_by_referrer[referrer_id] = volume_after
                continue

            key_part = f" · {payment_key}" if payment_key else ""
            desc = (
                f"Доначисление · комиссия {rate}% · {label} · "
                f"{amount_cents / 100:.2f}₽{key_part}"
            )
            await credit_balance(
                conn,
                referrer_id,
                delta,
                "referral_bonus",
                desc,
                related_user_id=payer_id,
            )

        stats["topped_up"] += 1
        stats["cents"] += delta
        volume_by_referrer[referrer_id] = volume_after

    return stats
