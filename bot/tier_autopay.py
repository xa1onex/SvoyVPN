"""
Автопродление Standard через ЮKassa по сохранённой карте.
https://yookassa.ru/developers/payments/recurring-payments
"""
from __future__ import annotations

import logging

from asyncpg.exceptions import UniqueViolationError

from .config import AppConfig
from .database import get_connection
from .plans import get_tier_plans
from .yookassa_client import YooKassaClient

logger = logging.getLogger(__name__)

STANDARD_PLAN_ID = "standard_1m"


async def run_standard_yookassa_autopay_renewals(config: AppConfig) -> None:
    """За день до окончания подписки создаём платёж по сохранённому payment_method_id."""
    if not config.yookassa.enabled:
        return

    yk = YooKassaClient(config.yookassa)
    plans = await get_tier_plans()
    if STANDARD_PLAN_ID not in plans:
        logger.error("autopay: plan %s not in tier plans", STANDARD_PLAN_ID)
        return
    plan = plans[STANDARD_PLAN_ID]
    amount_rub = plan["price_rub"] / 100.0

    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id, u.subscription_end, u.yookassa_recurring_payment_method_id
            FROM users u
            WHERE u.subscription_tier = 'standard'
              AND u.pay_subscribed = TRUE
              AND u.subscription_end IS NOT NULL
              AND DATE(u.subscription_end) = CURRENT_DATE + 1
              AND u.yookassa_recurring_payment_method_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM payments p
                  WHERE p.user_id = u.user_id
                    AND p.plan_id = $1
                    AND p.plan_type = 'tier'
                    AND p.status = 'pending'
                    AND p.created_at > NOW() - INTERVAL '3 days'
              )
            """,
            STANDARD_PLAN_ID,
        )

    for row in rows:
        user_id = row["user_id"]
        pm_id = row["yookassa_recurring_payment_method_id"]
        sub_end = row["subscription_end"]
        end_key = sub_end.strftime("%Y-%m-%d") if sub_end else "na"
        idem = f"autopay-{user_id}-{end_key}"

        metadata = {
            "user_id": str(user_id),
            "plan_id": STANDARD_PLAN_ID,
            "method_id": "yookassa",
            "product_type": "tier",
            "payment_source": "yookassa_autopay",
        }
        try:
            payment = yk.create_recurring_payment(
                amount=amount_rub,
                description="VPN Standard — продление (автоплатёж)",
                payment_method_id=pm_id,
                metadata=metadata,
                idempotency_key=idem,
            )
        except Exception as e:
            logger.error("autopay: YooKassa create failed user=%s: %s", user_id, e, exc_info=True)
            continue

        try:
            async with get_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO payments
                    (user_id, amount, currency, plan_id, plan_type, status,
                     yookassa_payment_id, payment_source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    user_id,
                    plan["price_rub"],
                    "RUB",
                    STANDARD_PLAN_ID,
                    "tier",
                    "pending",
                    payment["id"],
                    "yookassa_autopay",
                )
        except UniqueViolationError:
            logger.info(
                "autopay: duplicate payment row for user=%s (idem ok)", user_id
            )
            continue
        except Exception as e:
            logger.error("autopay: DB insert failed user=%s: %s", user_id, e, exc_info=True)
            continue

        logger.info("autopay: created payment %s user=%s", payment.get("id"), user_id)
