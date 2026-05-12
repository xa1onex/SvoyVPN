"""
Автопродление тарифов (Lite/Standard/Pro) через ЮKassa по сохранённой карте.
https://yookassa.ru/developers/payment-acceptance/scenario-extensions/recurring-payments/basics
"""
from __future__ import annotations

import logging

from asyncpg.exceptions import UniqueViolationError

from .config import AppConfig
from .database import get_connection
from .plans import TIER_PLANS_BASE, get_tier_plans
from .yookassa_client import YooKassaClient

logger = logging.getLogger(__name__)


async def run_yookassa_autopay_renewals(config: AppConfig) -> None:
    """За день до окончания подписки создаём платёж по сохранённому payment_method_id.

    Работает для всех tier-планов (lite_1m, standard_1m, pro_1m).
    """
    if not config.yookassa.enabled:
        return

    yk = YooKassaClient(config.yookassa)
    plans = await get_tier_plans()

    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id, u.subscription_tier, u.subscription_end,
                   u.yookassa_recurring_payment_method_id,
                   u.pending_downgrade_tier,
                   COALESCE(u.referral_discount_percent, 0) as referral_discount_percent
            FROM users u
            WHERE u.subscription_tier IN ('lite', 'standard', 'pro')
              AND u.pay_subscribed = TRUE
              AND u.subscription_end IS NOT NULL
              AND DATE(u.subscription_end) = CURRENT_DATE + 1
              AND u.yookassa_recurring_payment_method_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM payments p
                  WHERE p.user_id = u.user_id
                    AND p.plan_type = 'tier'
                    AND p.status = 'pending'
                    AND p.created_at > NOW() - INTERVAL '3 days'
              )
            """
        )

    for row in rows:
        user_id = row["user_id"]
        tier = row["subscription_tier"]
        pm_id = row["yookassa_recurring_payment_method_id"]
        sub_end = row["subscription_end"]
        pending_downgrade = row.get("pending_downgrade_tier")
        referral_discount = row.get("referral_discount_percent") or 0

        # If downgrade is scheduled, use the new (lower) tier for billing
        effective_tier = pending_downgrade if pending_downgrade else tier
        plan_id = f"{effective_tier}_1m"
        plan = plans.get(plan_id)
        if not plan:
            logger.warning("autopay: plan %s not found for user=%s", plan_id, user_id)
            continue

        amount_rub = plan["price_rub"] / 100.0
        # Apply referral discount (one-time per cycle, then reset)
        if referral_discount > 0:
            amount_rub = round(amount_rub * (100 - referral_discount) / 100, 2)
            if amount_rub < 1.0:
                amount_rub = 1.0

        end_key = sub_end.strftime("%Y-%m-%d") if sub_end else "na"
        idem = f"autopay-{user_id}-{plan_id}-{end_key}"

        metadata = {
            "user_id": str(user_id),
            "plan_id": plan_id,
            "method_id": "yookassa",
            "product_type": "tier",
            "payment_source": "yookassa_autopay",
        }
        if pending_downgrade:
            metadata["downgrade_from"] = tier
        if referral_discount > 0:
            metadata["referral_discount"] = str(referral_discount)
        try:
            payment = yk.create_recurring_payment(
                amount=amount_rub,
                description=f"VPN {plan['title']} — продление (автоплатёж)",
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
                    plan_id,
                    "tier",
                    "pending",
                    payment["id"],
                    "yookassa_autopay",
                )
        except UniqueViolationError:
            logger.info("autopay: duplicate payment row for user=%s (idem ok)", user_id)
            continue
        except Exception as e:
            logger.error("autopay: DB insert failed user=%s: %s", user_id, e, exc_info=True)
            continue

        logger.info("autopay: created payment %s user=%s plan=%s", payment.get("id"), user_id, plan_id)
