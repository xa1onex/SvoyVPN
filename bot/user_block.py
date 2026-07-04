"""
Блокировка пользователей (п. 5.6, 8.6 оферты).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .database import get_connection
from .plans import FREE_TIER_ID
from .subscriptions import revoke_all_vpn_access
from .custom_emojis import E, e, lbl, btn, emoji_button, raw

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 60.0
_blacklist_cache: dict[int, tuple[bool, float]] = {}


def invalidate_blacklist_cache(user_id: int) -> None:
    _blacklist_cache.pop(user_id, None)


async def is_user_blacklisted(user_id: int) -> bool:
    entry = _blacklist_cache.get(user_id)
    if entry is not None:
        value, ts = entry
        if time.monotonic() - ts <= _CACHE_TTL_SEC:
            return value
        _blacklist_cache.pop(user_id, None)

    async with get_connection() as conn:
        blocked = bool(
            await conn.fetchval(
                "SELECT blacklisted FROM users WHERE user_id = $1",
                user_id,
            )
        )
    _blacklist_cache[user_id] = (blocked, time.monotonic())
    return blocked

OFFER_URL = (
    "https://telegra.ph/PUBLICHNAYA-OFERTA-I-POLITIKA-KONFIDENCIALNOSTI-04-20"
)

BLOCKED_USER_MESSAGE = (
    f"{E.blocked} <b>Доступ к сервису ограничен</b>\n\n"
    "Аккаунт заблокирован за нарушение "
    f'<a href="{OFFER_URL}">оферты</a> '
    "(в т.ч. п. 8.6 — злоупотребление партнёрской программой).\n\n"
    "По вопросам блокировки обратитесь в техническую поддержку через раздел «Помощь»."
)


async def block_user(
    user_id: int,
    *,
    reason: str,
    admin_id: int | None = None,
    revert_referral_fraud: bool = False,
) -> dict[str, Any]:
    """
    Заблокировать пользователя: отключить VPN, обнулить бонусы партнёрки при fraud.
    """
    result: dict[str, Any] = {
        "user_id": user_id,
        "blocked": False,
        "keys_revoked": 0,
        "referrals_blocked": 0,
        "referral_rewards_removed": 0,
    }

    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT user_id, blacklisted, referral_count
            FROM users WHERE user_id = $1
            """,
            user_id,
        )
        if not row:
            result["error"] = "not_found"
            return result
        if row["blacklisted"]:
            result["error"] = "already_blocked"
            return result

        async with conn.transaction():
            keys_revoked = await revoke_all_vpn_access(conn, user_id)
            result["keys_revoked"] = keys_revoked

            if revert_referral_fraud:
                invited_rows = await conn.fetch(
                    """
                    SELECT invited_user_id
                    FROM referral_invite_rewards
                    WHERE inviter_id = $1
                    """,
                    user_id,
                )
                invited_ids = [int(r["invited_user_id"]) for r in invited_rows]
                if not invited_ids:
                    invited_rows = await conn.fetch(
                        "SELECT user_id FROM users WHERE invited_by = $1",
                        user_id,
                    )
                    invited_ids = [int(r["user_id"]) for r in invited_rows]

                for invited_id in invited_ids:
                    if invited_id == user_id:
                        continue
                    sub_revoked = await revoke_all_vpn_access(conn, invited_id)
                    await conn.execute(
                        """
                        UPDATE users SET
                            blacklisted = TRUE,
                            blacklist_reason = $2,
                            blacklisted_at = NOW(),
                            pay_subscribed = FALSE,
                            subscription_end = CURRENT_DATE - INTERVAL '1 day',
                            subscription_tier = $3,
                            balance = 0,
                            yookassa_recurring_payment_method_id = NULL
                        WHERE user_id = $1 AND blacklisted = FALSE
                        """,
                        invited_id,
                        f"Связан с заблокированным аккаунтом {user_id} (п. 8.6)",
                        FREE_TIER_ID,
                    )
                    result["referrals_blocked"] += 1
                    result.setdefault("referral_keys_revoked", 0)
                    result["referral_keys_revoked"] += sub_revoked
                    invalidate_blacklist_cache(invited_id)

                tag = await conn.execute(
                    "DELETE FROM referral_invite_rewards WHERE inviter_id = $1",
                    user_id,
                )
                result["referral_rewards_removed"] = (
                    int(tag.split()[-1]) if tag and tag != "DELETE 0" else 0
                )
                await conn.execute(
                    """
                    UPDATE users SET
                        referral_count = 0,
                        referral_discount_percent = 0,
                        referral_bonus_bypass_percent = 0
                    WHERE user_id = $1
                    """,
                    user_id,
                )

            await conn.execute(
                """
                UPDATE users SET
                    blacklisted = TRUE,
                    blacklist_reason = $2,
                    blacklisted_at = NOW(),
                    pay_subscribed = FALSE,
                    subscription_end = CURRENT_DATE - INTERVAL '1 day',
                    subscription_tier = $3,
                    balance = 0,
                    yookassa_recurring_payment_method_id = NULL,
                    referral_count = CASE WHEN $4 THEN 0 ELSE referral_count END,
                    referral_discount_percent = CASE WHEN $4 THEN 0 ELSE referral_discount_percent END,
                    referral_bonus_bypass_percent = CASE WHEN $4 THEN 0 ELSE referral_bonus_bypass_percent END
                WHERE user_id = $1
                """,
                user_id,
                reason[:500],
                FREE_TIER_ID,
                revert_referral_fraud,
            )
            result["blocked"] = True

    invalidate_blacklist_cache(user_id)
    logger.warning(
        "user blocked user_id=%s admin_id=%s fraud_referrals=%s keys=%s referrals_blocked=%s",
        user_id,
        admin_id,
        revert_referral_fraud,
        result["keys_revoked"],
        result["referrals_blocked"],
    )
    return result


async def unblock_user(
    user_id: int,
    *,
    admin_id: int | None = None,
) -> bool:
    async with get_connection() as conn:
        tag = await conn.execute(
            """
            UPDATE users SET
                blacklisted = FALSE,
                blacklist_reason = NULL,
                blacklisted_at = NULL
            WHERE user_id = $1 AND blacklisted = TRUE
            """,
            user_id,
        )
        ok = tag.endswith("1")
    if ok:
        invalidate_blacklist_cache(user_id)
        logger.info("user unblocked user_id=%s admin_id=%s", user_id, admin_id)
    return ok
