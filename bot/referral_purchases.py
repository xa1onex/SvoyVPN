"""
Реферальные бонусы за оплаты приглашённых: % дней Plus + подарок TG за каждую N-ю годовую Plus.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot

from .database import get_connection
from .plans import SUBSCRIPTION_PLANS_BASE, TIER_PLANS_BASE
from .referral_commission import is_commissionable_payment
from .referral_rewards import _add_subscription_days
from .subscriptions import create_or_activate_keys_for_all_servers

logger = logging.getLogger(__name__)

DEFAULT_PURCHASE_BONUS_PERCENT = 10
DEFAULT_YEARLY_GIFT_EVERY_N = 3
TG_GIFT_RUB_RANGE = "800–1500₽"


async def get_purchase_bonus_percent() -> int:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT purchase_bonus_percent
            FROM referral_settings
            ORDER BY id DESC
            LIMIT 1
            """
        )
    if not row or row["purchase_bonus_percent"] is None:
        return DEFAULT_PURCHASE_BONUS_PERCENT
    return max(0, min(100, int(row["purchase_bonus_percent"])))


async def get_yearly_gift_every_n() -> int:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT yearly_gift_every_n
            FROM referral_settings
            ORDER BY id DESC
            LIMIT 1
            """
        )
    if not row or row["yearly_gift_every_n"] is None:
        return DEFAULT_YEARLY_GIFT_EVERY_N
    return max(1, int(row["yearly_gift_every_n"]))


def _purchase_base_days(plan_type: str | None, plan_id: str | None) -> int:
    """База для расчёта % дней: длительность подписки или эквивалент месяца для пакетов."""
    pid = (plan_id or "").strip()
    pt = (plan_type or "").strip().lower()
    if pid in TIER_PLANS_BASE:
        months = int(TIER_PLANS_BASE[pid].get("duration") or 1)
        return months * 30
    if pid in SUBSCRIPTION_PLANS_BASE:
        months = int(SUBSCRIPTION_PLANS_BASE[pid].get("duration") or 1)
        return months * 30
    if pt in ("bypass_pack", "gb_pack") or pid.startswith(("bypass_pack", "gb_pack")):
        return 30
    return 30


def _is_plus_yearly_purchase(plan_type: str | None, plan_id: str | None) -> bool:
    pid = (plan_id or "").strip()
    if pid == "plus_12m":
        return True
    if pid in TIER_PLANS_BASE:
        plan = TIER_PLANS_BASE[pid]
        if plan.get("tier") == "plus":
            return int(plan.get("duration") or 0) >= 12
    return False


def _product_label(plan_type: str | None, plan_id: str | None) -> str:
    pid = (plan_id or "").strip()
    if pid in TIER_PLANS_BASE:
        return str(TIER_PLANS_BASE[pid].get("title") or pid)
    if pid in SUBSCRIPTION_PLANS_BASE:
        return str(SUBSCRIPTION_PLANS_BASE[pid].get("title") or pid)
    if pid.startswith("bypass_pack:"):
        return "Bypass-пакет"
    if pid.startswith("gb_pack"):
        return "Пакет ГБ"
    return (plan_type or plan_id or "оплата").strip() or "оплата"


async def get_referrer_purchase_stats(referrer_id: int) -> dict[str, Any]:
    """Статистика для экрана «Подарок»."""
    every_n = await get_yearly_gift_every_n()
    bonus_percent = await get_purchase_bonus_percent()

    async with get_connection() as conn:
        yearly_count = int(
            await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM referral_purchase_rewards
                WHERE referrer_id = $1 AND is_yearly_plus = TRUE
                """,
                referrer_id,
            )
            or 0
        )
        pending_gifts = int(
            await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM referral_tg_gift_claims
                WHERE referrer_id = $1 AND status = 'pending'
                """,
                referrer_id,
            )
            or 0
        )
        fulfilled_gifts = int(
            await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM referral_tg_gift_claims
                WHERE referrer_id = $1 AND status = 'fulfilled'
                """,
                referrer_id,
            )
            or 0
        )
        reg_friends = int(
            await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM referral_invite_rewards
                WHERE inviter_id = $1 AND inviter_reward_at IS NOT NULL
                """,
                referrer_id,
            )
            or 0
        )
        total_purchase_days = int(
            await conn.fetchval(
                """
                SELECT COALESCE(SUM(reward_days), 0)
                FROM referral_purchase_rewards
                WHERE referrer_id = $1
                """,
                referrer_id,
            )
            or 0
        )

    mod = yearly_count % every_n
    until_gift = every_n if mod == 0 else every_n - mod

    from .referral_rewards import get_referral_bonus_days

    reg_bonus_days = await get_referral_bonus_days()
    total_earned_days = reg_friends * reg_bonus_days + total_purchase_days

    return {
        "purchase_bonus_percent": bonus_percent,
        "yearly_gift_every_n": every_n,
        "yearly_plus_purchases": yearly_count,
        "until_tg_gift": until_gift,
        "pending_tg_gifts": pending_gifts,
        "fulfilled_tg_gifts": fulfilled_gifts,
        "total_purchase_reward_days": total_purchase_days,
        "total_earned_days": total_earned_days,
    }


async def apply_referral_purchase_reward(
    bot: Bot | None,
    *,
    payer_user_id: int,
    payment_db_id: int,
    plan_type: str | None,
    plan_id: str | None,
    amount_cents: int,
    is_trial: bool = False,
) -> int:
    """
    Начисляет пригласившему % дней Plus за оплату друга.
    Каждая N-я годовая Plus — запись на TG-подарок (админ связывается вручную).
    """
    if not is_commissionable_payment(
        amount_cents, plan_type, plan_id, is_trial=is_trial
    ):
        return 0

    async with get_connection() as conn:
        already = await conn.fetchval(
            "SELECT 1 FROM referral_purchase_rewards WHERE payment_id = $1",
            payment_db_id,
        )
        if already:
            return 0

        referrer_id = await conn.fetchval(
            "SELECT invited_by FROM users WHERE user_id = $1",
            payer_user_id,
        )
        if not referrer_id:
            return 0

        bonus_percent = await get_purchase_bonus_percent()
        if bonus_percent <= 0:
            return 0

        base_days = _purchase_base_days(plan_type, plan_id)
        reward_days = max(1, int(base_days * bonus_percent / 100))
        is_yearly = _is_plus_yearly_purchase(plan_type, plan_id)
        product_label = _product_label(plan_type, plan_id)

        tg_gift_created = False
        milestone_no = 0

        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO referral_purchase_rewards (
                    payment_id, referrer_id, payer_user_id,
                    reward_days, base_days, bonus_percent,
                    is_yearly_plus, product_label
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                payment_db_id,
                referrer_id,
                payer_user_id,
                reward_days,
                base_days,
                bonus_percent,
                is_yearly,
                product_label,
            )
            await _add_subscription_days(conn, referrer_id, reward_days)

            if is_yearly:
                every_n = await get_yearly_gift_every_n()
                yearly_count = int(
                    await conn.fetchval(
                        """
                        SELECT COUNT(*)
                        FROM referral_purchase_rewards
                        WHERE referrer_id = $1 AND is_yearly_plus = TRUE
                        """,
                        referrer_id,
                    )
                    or 0
                )
                if yearly_count > 0 and yearly_count % every_n == 0:
                    milestone_no = yearly_count // every_n
                    row = await conn.fetchrow(
                        """
                        INSERT INTO referral_tg_gift_claims (
                            referrer_id, payment_id, milestone_no, status
                        ) VALUES ($1, $2, $3, 'pending')
                        ON CONFLICT (payment_id) DO NOTHING
                        RETURNING id
                        """,
                        referrer_id,
                        payment_db_id,
                        milestone_no,
                    )
                    tg_gift_created = row is not None

    asyncio.create_task(create_or_activate_keys_for_all_servers(referrer_id))

    if bot:
        try:
            await bot.send_message(
                referrer_id,
                f"🎁 <b>Друг оплатил {product_label}!</b>\n\n"
                f"Вам начислено <b>+{reward_days} дн.</b> SvoyVPN Plus "
                f"({bonus_percent}% от {base_days} дн.).",
                parse_mode="HTML",
            )
            if tg_gift_created:
                await bot.send_message(
                    referrer_id,
                    f"🎉 <b>Подарок Telegram!</b>\n\n"
                    f"За {yearly_count}-ю годовую Plus от друга вам полагается подарок "
                    f"на <b>{TG_GIFT_RUB_RANGE}</b>.\n\n"
                    "Мы свяжемся с вами в Telegram для отправки. "
                    "Дни Plus уже начислены автоматически.",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.debug("referral purchase notify referrer=%s: %s", referrer_id, e)

        if tg_gift_created:
            try:
                async with get_connection() as conn:
                    payer = await conn.fetchrow(
                        "SELECT first_name, username FROM users WHERE user_id = $1",
                        payer_user_id,
                    )
                payer_name = (payer["first_name"] or "Друг") if payer else "Друг"
                payer_un = payer["username"] if payer else None
                who = f"@{payer_un}" if payer_un else payer_name
                from .config import load_config

                config = load_config()
                admin_text = (
                    f"🎁 <b>Реферальный TG-подарок</b>\n\n"
                    f"Кому: <code>{referrer_id}</code>\n"
                    f"Друг: {who} (<code>{payer_user_id}</code>)\n"
                    f"Покупка: {product_label}\n"
                    f"Подарок #{milestone_no}\n\n"
                    "Проверьте и свяжитесь с пользователем."
                )
                for admin_id in config.bot.admin_ids:
                    try:
                        await bot.send_message(admin_id, admin_text, parse_mode="HTML")
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("referral tg gift admin notify: %s", e)

    logger.info(
        "referral purchase reward referrer=%s payer=%s payment=%s days=%s yearly=%s gift=%s",
        referrer_id,
        payer_user_id,
        payment_db_id,
        reward_days,
        is_yearly,
        tg_gift_created,
    )
    return reward_days


async def referral_reward_after_payment(
    bot: Bot | None,
    *,
    payer_user_id: int,
    plan_type: str | None,
    plan_id: str | None,
    amount_cents: int,
    is_trial: bool = False,
    yookassa_payment_id: str | None = None,
    telegram_payment_charge_id: str | None = None,
    payment_db_id: int | None = None,
) -> None:
    """Найти payment.id и начислить бонус пригласившему."""
    pid = payment_db_id
    if not pid:
        async with get_connection() as conn:
            if yookassa_payment_id:
                pid = await conn.fetchval(
                    "SELECT id FROM payments WHERE yookassa_payment_id = $1",
                    yookassa_payment_id,
                )
            elif telegram_payment_charge_id:
                pid = await conn.fetchval(
                    "SELECT id FROM payments WHERE telegram_payment_charge_id = $1",
                    telegram_payment_charge_id,
                )
    if not pid:
        return
    try:
        await apply_referral_purchase_reward(
            bot,
            payer_user_id=payer_user_id,
            payment_db_id=int(pid),
            plan_type=plan_type,
            plan_id=plan_id,
            amount_cents=amount_cents,
            is_trial=is_trial,
        )
    except Exception as e:
        logger.error(
            "referral_reward_after_payment payer=%s payment=%s: %s",
            payer_user_id,
            pid,
            e,
            exc_info=True,
        )
