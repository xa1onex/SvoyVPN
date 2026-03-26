"""
One-off utility to compensate users affected by referral bug.

Bug context:
- Users could be linked with invited_by=<bot_user_id> due to invite callback bug.
- As a result, real inviters did not receive bonuses.

What this script does:
1) Finds all users where invited_by == bot user id.
2) Adds bonus days to those users.
3) Clears invited_by for those users.
4) Decrements bot account referral_count by affected count.
5) Optionally sends Telegram notifications to affected users.

Run (dry-run):
    python -m bot.compensate_referral_bug

Run (apply changes + notify users):
    python -m bot.compensate_referral_bug --apply --notify
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Sequence

from aiogram import Bot

from .config import load_config
from .database import get_connection

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compensate referral bug affected users")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply database changes. Without this flag script only prints dry-run info.",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Send Telegram notifications to affected users (works with --apply).",
    )
    parser.add_argument(
        "--bonus-days",
        type=int,
        default=None,
        help="Override compensation days. By default uses invited_bonus_days from referral_settings.",
    )
    parser.add_argument(
        "--bot-user-id",
        type=int,
        default=None,
        help="Override inviter bot user id. By default uses current bot id via Bot API.",
    )
    return parser


async def get_bonus_days(default_days: int = 3) -> int:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT invited_bonus_days FROM referral_settings ORDER BY id DESC LIMIT 1"
        )
        if not row or row["invited_bonus_days"] is None:
            return default_days
        return int(row["invited_bonus_days"])


async def get_affected_users(bot_user_id: int) -> Sequence[int]:
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id
            FROM users
            WHERE invited_by = $1
            ORDER BY user_id
            """,
            bot_user_id,
        )
        return [int(r["user_id"]) for r in rows]


async def apply_compensation(bot_user_id: int, user_ids: Sequence[int], bonus_days: int) -> None:
    async with get_connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE users
                SET
                    pay_subscribed = TRUE,
                    subscription_end = CASE
                        WHEN subscription_end IS NULL OR subscription_end < CURRENT_DATE
                        THEN CURRENT_DATE + ($1 || ' days')::INTERVAL
                        ELSE subscription_end + ($1 || ' days')::INTERVAL
                    END
                WHERE user_id = ANY($2::BIGINT[])
                """,
                str(bonus_days),
                list(user_ids),
            )

            await conn.execute(
                """
                UPDATE users
                SET invited_by = NULL
                WHERE user_id = ANY($1::BIGINT[])
                """,
                list(user_ids),
            )

            await conn.execute(
                """
                UPDATE users
                SET referral_count = GREATEST(COALESCE(referral_count, 0) - $2, 0)
                WHERE user_id = $1
                """,
                bot_user_id,
                len(user_ids),
            )


async def notify_users(bot: Bot, user_ids: Sequence[int], bonus_days: int) -> tuple[int, int]:
    ok = 0
    failed = 0
    day_word = "дней"
    if bonus_days % 10 == 1 and bonus_days % 100 != 11:
        day_word = "день"
    elif bonus_days % 10 in (2, 3, 4) and bonus_days % 100 not in (12, 13, 14):
        day_word = "дня"

    text = (
        "🎁 Мы исправили ошибку в реферальной системе.\n\n"
        f"Вам автоматически начислено +{bonus_days} {day_word} подписки.\n"
        "Ничего делать не нужно."
    )

    for user_id in user_ids:
        try:
            await bot.send_message(user_id, text)
            ok += 1
        except Exception as exc:
            failed += 1
            logger.warning("Failed to notify user %s: %s", user_id, exc)
    return ok, failed


async def main() -> None:
    args = build_parser().parse_args()
    config = load_config()
    bot = Bot(token=config.bot.bot_token)

    try:
        me = await bot.get_me()
        bot_user_id = int(args.bot_user_id or me.id)
        bonus_days = int(args.bonus_days) if args.bonus_days is not None else await get_bonus_days()
        affected_user_ids = await get_affected_users(bot_user_id)

        logger.info("Bot id used as invalid inviter: %s", bot_user_id)
        logger.info("Compensation days: %s", bonus_days)
        logger.info("Affected users found: %s", len(affected_user_ids))
        if affected_user_ids:
            logger.info("Affected user ids: %s", ", ".join(str(x) for x in affected_user_ids))

        if not args.apply:
            logger.info("Dry-run mode. Use --apply to execute updates.")
            return

        if not affected_user_ids:
            logger.info("No affected users. Nothing to update.")
            return

        await apply_compensation(bot_user_id, affected_user_ids, bonus_days)
        logger.info("Database compensation applied for %s users.", len(affected_user_ids))

        if args.notify:
            sent, failed = await notify_users(bot, affected_user_ids, bonus_days)
            logger.info("Notifications sent: %s, failed: %s", sent, failed)
        else:
            logger.info("Notifications skipped. Use --notify to send messages.")

    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
