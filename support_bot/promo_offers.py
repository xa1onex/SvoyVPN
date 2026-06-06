"""Персональные скидочные офферы (одноразовая оплата в основном боте)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from bot.database import get_connection
from bot.plans import (
    PAID_TIER_IDS,
    TIERS,
    format_price_rub,
    get_tier_plans,
    get_user_tariffs,
)


async def init_promo_tables() -> None:
    async with get_connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS support_personal_promo_offers (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                plan_id TEXT NOT NULL,
                discount_percent INTEGER NOT NULL,
                price_rub INTEGER NOT NULL,
                base_price_rub INTEGER NOT NULL,
                button_text TEXT,
                is_renewal_context BOOLEAN DEFAULT FALSE,
                has_recurring_at_create BOOLEAN DEFAULT FALSE,
                status TEXT NOT NULL DEFAULT 'pending',
                created_by BIGINT,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                used_at TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_personal_promo_user_status
            ON support_personal_promo_offers(user_id, status)
            """
        )


async def get_user_payment_context(user_id: int) -> dict[str, Any]:
    """Контекст для выбора кнопок и скидки: рекуррент, продление, тариф."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT pay_subscribed, subscription_end, subscription_tier,
                   yookassa_recurring_payment_method_id IS NOT NULL AS has_recurring,
                   trial_used, cancel_retention_used
            FROM users WHERE user_id = $1
            """,
            user_id,
        )
    if not row:
        return {"error": "user not found"}

    _, is_renew, show_discount = await get_user_tariffs(user_id)
    sub_end = row["subscription_end"]
    days_left = None
    if sub_end and row["pay_subscribed"]:
        end = sub_end.date() if hasattr(sub_end, "date") else sub_end
        days_left = (end - datetime.now().date()).days

    tier = row["subscription_tier"] or "free"
    return {
        "user_id": user_id,
        "subscription_tier": tier,
        "is_active_subscriber": bool(is_renew),
        "days_until_expiry": days_left,
        "show_global_discount": show_discount,
        "has_recurring_card": bool(row["has_recurring"]),
        "recurring_warning": (
            "У пользователя привязана карта для автопродления — списание пойдёт по полной цене тарифа "
            "в дату автоплатежа, если не отключить автопродление. Персональная скидка — только при оплате по кнопке оффера."
            if row["has_recurring"]
            else None
        ),
        "recommended_plan_id": f"{tier}_1m" if tier in PAID_TIER_IDS else "standard_1m",
        "trial_used": row["trial_used"],
    }


def _calc_discounted_price(base_kopecks: int, discount_percent: int) -> int:
    pct = min(max(discount_percent, 1), 99)
    price = int(base_kopecks * (100 - pct) / 100)
    return max(price, 100)


async def create_personal_promo_offer(
    *,
    user_id: int,
    discount_percent: int,
    plan_id: str | None = None,
    tier: str | None = None,
    button_text: str | None = None,
    note: str | None = None,
    created_by: int | None = None,
    valid_hours: int = 72,
) -> dict[str, Any]:
    ctx = await get_user_payment_context(user_id)
    if "error" in ctx:
        return ctx

    plans = await get_tier_plans()
    if plan_id:
        pid = plan_id
    elif tier:
        pid = f"{tier.strip().lower()}_1m"
    else:
        pid = ctx["recommended_plan_id"]

    if pid not in plans:
        return {"error": f"План {pid} не найден", "available_plans": list(plans.keys())[:12]}

    plan = plans[pid]
    base = int(plan["price_rub"])
    final = _calc_discounted_price(base, discount_percent)
    label = button_text or (
        f"🔥 {TIERS.get(plan['tier'], {}).get('name', plan['tier'])} "
        f"-{discount_percent}% · {format_price_rub(final)}"
    )

    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO support_personal_promo_offers (
                user_id, plan_id, discount_percent, price_rub, base_price_rub,
                button_text, is_renewal_context, has_recurring_at_create,
                created_by, note, expires_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, NOW() + ($11 || ' hours')::INTERVAL)
            RETURNING id, expires_at
            """,
            user_id,
            pid,
            discount_percent,
            final,
            base,
            label,
            ctx["is_active_subscriber"],
            ctx["has_recurring_card"],
            created_by,
            note,
            str(min(max(valid_hours, 1), 168)),
        )

    return {
        "offer_id": row["id"],
        "user_id": user_id,
        "plan_id": pid,
        "discount_percent": discount_percent,
        "base_price_rub": base,
        "price_rub": final,
        "button_text": label,
        "callback_data": f"personal_promo:{row['id']}",
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "payment_context": ctx,
    }


async def get_promo_offer(offer_id: int) -> dict[str, Any] | None:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM support_personal_promo_offers WHERE id=$1", offer_id
        )
    return dict(row) if row else None


async def mark_promo_used(offer_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE support_personal_promo_offers
            SET status='used', used_at=NOW() WHERE id=$1
            """,
            offer_id,
        )
