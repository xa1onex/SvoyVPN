"""
One-off utility for referral compensations.

Modes:
1) affected      - compensate users linked to bot via invited_by (old mode)
2) all-referrers - grant every inviter extra days by formula:
                   added_days = referral_count * inviter_bonus_days
                   (even if previously rewarded)

Run dry-run:
    python -m bot.compensate_referral_bug --mode all-referrers

Run apply + notify:
    python -m bot.compensate_referral_bug --mode all-referrers --apply --notify
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from aiogram import Bot

from .config import load_config
from .database import get_connection

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class GrantResult:
    user_id: int
    added_days: int
    subscription_end: datetime | None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compensate referral bonuses")
    parser.add_argument(
        "--mode",
        choices=["affected", "all-referrers"],
        default="affected",
        help="Compensation mode: 'affected' or 'all-referrers'.",
    )
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
        help="Override per-user compensation days in 'affected' mode.",
    )
    parser.add_argument(
        "--per-friend-days",
        type=int,
        default=None,
        help="Override days per friend in 'all-referrers' mode. Default = inviter_bonus_days.",
    )
    parser.add_argument(
        "--bot-user-id",
        type=int,
        default=None,
        help="Override inviter bot user id. By default uses current bot id via Bot API.",
    )
    return parser


def days_word(days: int) -> str:
    if days % 10 == 1 and days % 100 != 11:
        return "день"
    if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


async def get_bonus_days(default_days: int = 3) -> int:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT invited_bonus_days FROM referral_settings ORDER BY id DESC LIMIT 1"
        )
        if not row or row["invited_bonus_days"] is None:
            return default_days
        return int(row["invited_bonus_days"])


async def get_inviter_bonus_days(default_days: int = 5) -> int:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT inviter_bonus_days FROM referral_settings ORDER BY id DESC LIMIT 1"
        )
        if not row or row["inviter_bonus_days"] is None:
            return default_days
        return int(row["inviter_bonus_days"])


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


async def get_all_referrers_grants(bot_user_id: int, per_friend_days: int) -> list[GrantResult]:
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT
                user_id,
                (referral_count * $1)::INT AS added_days,
                subscription_end
            FROM users
            WHERE referral_count > 0
              AND user_id <> $2
            ORDER BY user_id
            """,
            per_friend_days,
            bot_user_id,
        )
        return [
            GrantResult(
                user_id=int(r["user_id"]),
                added_days=int(r["added_days"]),
                subscription_end=r["subscription_end"],
            )
            for r in rows
        ]


async def apply_all_referrers_grants(bot_user_id: int, per_friend_days: int) -> list[GrantResult]:
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            WITH targets AS (
                SELECT
                    user_id,
                    (referral_count * $1)::INT AS added_days
                FROM users
                WHERE referral_count > 0
                  AND user_id <> $2
            )
            UPDATE users u
            SET
                pay_subscribed = TRUE,
                subscription_end = CASE
                    WHEN u.subscription_end IS NULL OR u.subscription_end < CURRENT_DATE
                    THEN CURRENT_DATE + (t.added_days || ' days')::INTERVAL
                    ELSE u.subscription_end + (t.added_days || ' days')::INTERVAL
                END
            FROM targets t
            WHERE u.user_id = t.user_id
            RETURNING u.user_id, t.added_days, u.subscription_end
            """,
            per_friend_days,
            bot_user_id,
        )
        return [
            GrantResult(
                user_id=int(r["user_id"]),
                added_days=int(r["added_days"]),
                subscription_end=r["subscription_end"],
            )
            for r in rows
        ]


async def notify_affected_users(bot: Bot, user_ids: Sequence[int], bonus_days: int) -> tuple[int, int]:
    ok = 0
    failed = 0
    day = days_word(bonus_days)
    text = (
        "🎁 Мы исправили ошибку в реферальной системе.\n\n"
        f"Вам автоматически начислено +{bonus_days} {day} подписки.\n"
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


async def notify_all_referrers(bot: Bot, grants: Sequence[GrantResult]) -> tuple[int, int]:
    ok = 0
    failed = 0
    for grant in grants:
        end_str = "не определена"
        if grant.subscription_end is not None and hasattr(grant.subscription_end, "strftime"):
            end_str = grant.subscription_end.strftime("%d.%m.%Y")
        text = (
            f"🎉 Вы получили +{grant.added_days} {days_word(grant.added_days)} VPN за приглашение друга!\n"
            f"Теперь ваш VPN активен до: {end_str}"
        )
        try:
            await bot.send_message(grant.user_id, text)
            ok += 1
        except Exception as exc:
            failed += 1
            logger.warning("Failed to notify user %s: %s", grant.user_id, exc)
    return ok, failed


async def main() -> None:
    args = build_parser().parse_args()
    config = load_config()
    bot = Bot(token=config.bot.bot_token)

    try:
        me = await bot.get_me()
        bot_user_id = int(args.bot_user_id or me.id)
        logger.info("Bot id used as invalid inviter: %s", bot_user_id)

        if args.mode == "affected":
            bonus_days = int(args.bonus_days) if args.bonus_days is not None else await get_bonus_days()
            affected_user_ids = await get_affected_users(bot_user_id)
            logger.info("Mode: affected")
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
                sent, failed = await notify_affected_users(bot, affected_user_ids, bonus_days)
                logger.info("Notifications sent: %s, failed: %s", sent, failed)
            else:
                logger.info("Notifications skipped. Use --notify to send messages.")
            return

        per_friend_days = (
            int(args.per_friend_days)
            if args.per_friend_days is not None
            else await get_inviter_bonus_days()
        )
        preview = await get_all_referrers_grants(bot_user_id, per_friend_days)
        logger.info("Mode: all-referrers")
        logger.info("Per-friend days: %s", per_friend_days)
        logger.info("Inviters to reward: %s", len(preview))
        logger.info("Total days to grant: %s", sum(x.added_days for x in preview))
        if preview:
            logger.info("Preview (first 20): %s", ", ".join(f"{x.user_id}:+{x.added_days}" for x in preview[:20]))

        if not args.apply:
            logger.info("Dry-run mode. Use --apply to execute updates.")
            return
        if not preview:
            logger.info("No inviters found. Nothing to update.")
            return

        applied = await apply_all_referrers_grants(bot_user_id, per_friend_days)
        logger.info("Database grants applied for %s inviters.", len(applied))
        if args.notify:
            sent, failed = await notify_all_referrers(bot, applied)
            logger.info("Notifications sent: %s, failed: %s", sent, failed)
        else:
            logger.info("Notifications skipped. Use --notify to send messages.")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
