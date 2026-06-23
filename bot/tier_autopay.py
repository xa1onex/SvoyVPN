"""
Автопродление тарифа Plus через ЮKassa по сохранённой карте.
Поддерживает ежемесячное (plus_1m) и годовое (plus_12m) продление.
https://yookassa.ru/developers/payment-acceptance/scenario-extensions/recurring-payments/basics
"""
from __future__ import annotations

import logging
from typing import Optional

from asyncpg.exceptions import UniqueViolationError
from aiogram import Bot

from .config import AppConfig
from .database import get_connection
from .plans import ALL_PAID_TIER_IDS, TIER_PLANS_BASE, get_tier_plans, TIERS
from .yookassa_client import YooKassaClient

logger = logging.getLogger(__name__)


async def run_yookassa_autopay_renewals(config: AppConfig, bot: Optional[Bot] = None) -> None:
    """За день до окончания подписки создаём платёж по сохранённому payment_method_id.

    Работает только для Plus (plus_1m / plus_12m).
    """
    if not config.yookassa.enabled:
        return

    yk = YooKassaClient(config.yookassa)
    plans = await get_tier_plans()

    async with get_connection() as conn:
        tier_ids_sql = ", ".join(f"'{t}'" for t in ALL_PAID_TIER_IDS)
        rows = await conn.fetch(
            f"""
            SELECT u.user_id, u.subscription_tier, u.subscription_end,
                   u.yookassa_recurring_payment_method_id,
                   u.tier_duration_months
            FROM users u
            WHERE u.subscription_tier IN ({tier_ids_sql})
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
        duration_months = int(row.get("tier_duration_months") or 1)

        # Legacy-тарифы (lite/standard/pro) продлеваем как plus; только 1m или 12m
        from .plans import LEGACY_TIER_IDS
        effective_tier = "plus" if tier in LEGACY_TIER_IDS else tier
        if effective_tier != "plus":
            logger.warning("autopay: skip non-plus tier=%s user=%s", tier, user_id)
            continue

        plan_id = "plus_12m" if duration_months >= 12 else "plus_1m"
        plan = plans.get(plan_id)
        if not plan:
            logger.warning("autopay: plan %s not found for user=%s", plan_id, user_id)
            continue

        price_cents = plan["price_rub"]

        # ── Попытка оплаты с баланса ────────────────────────────────────────
        paid_from_balance = await _try_pay_from_balance(
            user_id, plan_id, plan, price_cents, effective_tier, bot
        )
        if paid_from_balance:
            logger.info("autopay: paid from balance user=%s plan=%s", user_id, plan_id)
            continue
        # ────────────────────────────────────────────────────────────────────

        amount_rub = price_cents / 100.0

        end_key = sub_end.strftime("%Y-%m-%d") if sub_end else "na"
        idem = f"autopay-{user_id}-{plan_id}-{end_key}"

        metadata = {
            "user_id": str(user_id),
            "plan_id": plan_id,
            "method_id": "yookassa",
            "product_type": "tier",
            "payment_source": "yookassa_autopay",
        }
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


async def _try_pay_from_balance(
    user_id: int,
    plan_id: str,
    plan: dict,
    price_cents: int,
    effective_tier: str,
    bot: Optional[Bot],
) -> bool:
    """
    Если у пользователя достаточно баланса — списываем и продлеваем подписку без ЮKassa.
    Возвращает True если оплата с баланса прошла успешно.
    """
    from .balance import get_balance, debit_balance
    from .subscriptions import set_new_subscription_days
    from .traffic import apply_subscription_anchor_on_payment, ensure_bypass_period
    from .subscriptions import create_or_activate_keys_for_all_servers

    try:
        async with get_connection() as conn:
            balance = await get_balance(conn, user_id)
            if balance < price_cents:
                return False

            success, new_balance = await debit_balance(
                conn,
                user_id,
                price_cents,
                "subscription_payment",
                f"Оплата подписки {plan.get('title', plan_id)} с баланса",
            )
            if not success:
                return False

            # Продлеваем на количество дней в зависимости от плана
            plan_duration_months = plan.get("duration", 1)
            renewal_days = plan_duration_months * 30
            await set_new_subscription_days(user_id, renewal_days, conn)

            tier_info = TIERS.get(effective_tier, {})
            await conn.execute(
                """
                UPDATE users SET
                    subscription_tier = $1,
                    bypass_traffic_limit_gb = $2,
                    device_limit = $3,
                    tier_duration_months = $4,
                    tier_price_paid = $5,
                    tier_purchased_at = NOW(),
                    bypass_traffic_used_bytes = CASE
                        WHEN bypass_period_start IS NULL THEN 0
                        ELSE bypass_traffic_used_bytes
                    END,
                    pending_downgrade_tier = NULL
                WHERE user_id = $6
                """,
                effective_tier,
                tier_info.get("bypass_gb", plan.get("bypass_gb")),
                tier_info.get("max_devices", plan.get("max_devices")),
                plan_duration_months,
                price_cents,
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO payments
                (user_id, amount, currency, plan_id, plan_type, status, payment_source)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                user_id, price_cents, "RUB", plan_id, "tier",
                "completed", "balance",
            )
            await apply_subscription_anchor_on_payment(conn, user_id)
            await ensure_bypass_period(conn, user_id)
            from .autopay_grace import clear_autopay_grace
            from .subscriptions import clear_subscription_expiry_reminders

            await clear_autopay_grace(conn, user_id)
            await clear_subscription_expiry_reminders(conn, user_id)

        await create_or_activate_keys_for_all_servers(user_id)

        # Тихое автопродление с баланса — без уведомления пользователю
        logger.info(
            "autopay balance: user=%s plan=%s charged=%s new_balance=%s",
            user_id,
            plan_id,
            price_cents,
            new_balance,
        )

        return True

    except Exception as e:
        logger.error("_try_pay_from_balance error user=%s: %s", user_id, e, exc_info=True)
        return False
