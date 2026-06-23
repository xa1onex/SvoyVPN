"""
Реферальная программа: пригласил друга — вам n дней подписки и ему n дней.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from .database import get_connection
from .plans import get_tier_bypass_gb, get_tier_max_devices, is_sentinel_subscription_end
from .subscriptions import (
    apply_subscription_anchor_on_payment,
    create_or_activate_keys_for_all_servers,
    set_new_subscription_days,
)
from .traffic import ensure_bypass_period

logger = logging.getLogger(__name__)

REFERRAL_GIFT_TIER = "plus"


async def get_referral_bonus_days() -> int:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT inviter_bonus_days, invited_bonus_days
            FROM referral_settings
            ORDER BY id DESC
            LIMIT 1
            """
        )
    if not row:
        return 7
    inv = int(row["inviter_bonus_days"] or 0)
    if inv > 0:
        return inv
    return int(row["invited_bonus_days"] or 7)


async def get_referral_bonus_settings() -> tuple[int, int]:
    n = await get_referral_bonus_days()
    return n, n


async def _add_subscription_days(conn, user_id: int, days: int) -> None:
    row = await conn.fetchrow(
        "SELECT subscription_end FROM users WHERE user_id = $1", user_id
    )
    if not row:
        return

    bypass_gb = get_tier_bypass_gb(REFERRAL_GIFT_TIER)
    device_limit = get_tier_max_devices(REFERRAL_GIFT_TIER)

    if is_sentinel_subscription_end(row["subscription_end"]):
        await set_new_subscription_days(user_id, days, conn)
    else:
        await conn.execute(
            """
            UPDATE users
            SET
                subscription_end = CASE
                    WHEN subscription_end IS NULL
                         OR DATE(subscription_end) < CURRENT_DATE
                    THEN CURRENT_DATE + ($2 || ' days')::INTERVAL
                    ELSE DATE(subscription_end) + ($2 || ' days')::INTERVAL
                END,
                pay_subscribed = TRUE
            WHERE user_id = $1
            """,
            user_id,
            str(days),
        )

    await conn.execute(
        """
        UPDATE users SET
            subscription_tier = $2,
            bypass_traffic_limit_gb = $3,
            device_limit = $4
        WHERE user_id = $1
        """,
        user_id,
        REFERRAL_GIFT_TIER,
        bypass_gb,
        device_limit,
    )
    await apply_subscription_anchor_on_payment(conn, user_id)
    await ensure_bypass_period(conn, user_id)


async def grant_plus_bonus_days(conn, user_id: int, days: int) -> None:
    """Подарок Plus на days дней (UTM-акции и прочие промо)."""
    await _add_subscription_days(conn, user_id, days)


async def grant_referral_bonuses(
    bot: Bot | None,
    invited_user_id: int,
    inviter_id: int,
) -> None:
    bonus_days = await get_referral_bonus_days()

    async with get_connection() as conn:
        redeemed = await conn.fetchval(
            """
            SELECT invited_reward_at FROM referral_invite_rewards
            WHERE invited_user_id = $1 AND invited_reward_at IS NOT NULL
            """,
            invited_user_id,
        )
        if redeemed:
            return

        async with conn.transaction():
            await _add_subscription_days(conn, invited_user_id, bonus_days)
            await _add_subscription_days(conn, inviter_id, bonus_days)
            await conn.execute(
                """
                INSERT INTO referral_invite_rewards (
                    invited_user_id, inviter_id, invited_reward_at, inviter_reward_at
                ) VALUES ($1, $2, NOW(), NOW())
                ON CONFLICT (invited_user_id) DO UPDATE SET
                    inviter_id = EXCLUDED.inviter_id,
                    invited_reward_at = COALESCE(
                        referral_invite_rewards.invited_reward_at, NOW()
                    ),
                    inviter_reward_at = COALESCE(
                        referral_invite_rewards.inviter_reward_at, NOW()
                    )
                """,
                invited_user_id,
                inviter_id,
            )

    await create_or_activate_keys_for_all_servers(invited_user_id)
    asyncio.create_task(create_or_activate_keys_for_all_servers(inviter_id))

    if bot:
        try:
            await bot.send_message(
                inviter_id,
                f"🎁 <b>Друг зарегистрировался!</b>\n\n"
                f"Вам начислено <b>{bonus_days} дн.</b> SvoyVPN Plus.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.debug("referral bonus notify: %s", e)

    logger.info(
        "referral_bonuses granted invited=%s inviter=%s days=%s",
        invited_user_id,
        inviter_id,
        bonus_days,
    )
