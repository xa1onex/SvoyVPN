"""
Разовый бэкофилл: создать VPN-ключи активным пользователям, у которых нет
ни одной реальной (несистемной) ноды. Чинит ситуацию, когда юзер видит в Happ
только заголовки. Безопасно: идемпотентно, последовательно, с прогрессом.
"""
import asyncio
import logging

from dotenv import load_dotenv

load_dotenv("/root/SvoyVPN/.env")

from bot.database import get_connection
from bot.subscriptions import ensure_user_keys_for_server_ids
from bot.free_tier_servers import get_free_tier_allowed_server_ids
from bot.plans import FREE_TIER_ID

logging.basicConfig(level=logging.WARNING, format="[%(asctime)s] %(levelname)s: %(message)s")
log = logging.getLogger("backfill")


async def main() -> None:
    async with get_connection() as conn:
        users = await conn.fetch(
            """
            SELECT u.user_id, u.subscription_tier AS tier
            FROM users u
            WHERE u.blacklisted = FALSE AND u.pay_subscribed = TRUE
              AND (u.subscription_end IS NULL OR u.subscription_end >= CURRENT_DATE)
              AND NOT EXISTS (
                SELECT 1 FROM vpn_keys k JOIN servers s ON s.id = k.server_id
                WHERE k.user_id = u.user_id AND k.is_active
                  AND s.is_active AND COALESCE(s.is_system, FALSE) = FALSE
                  AND COALESCE(s.exclude_from_subscription, FALSE) = FALSE
                  AND (k.expires_at IS NULL OR DATE(k.expires_at) >= CURRENT_DATE)
              )
            ORDER BY u.last_activity DESC NULLS LAST
            """
        )
        paid_server_ids = [
            int(r["id"])
            for r in await conn.fetch(
                """
                SELECT id FROM servers
                WHERE is_active = TRUE
                  AND COALESCE(is_system, FALSE) = FALSE
                  AND COALESCE(exclude_from_subscription, FALSE) = FALSE
                """
            )
        ]

    total = len(users)
    print(f"START backfill: {total} users", flush=True)
    ok = 0
    fail = 0
    for i, u in enumerate(users, 1):
        uid = int(u["user_id"])
        tier = (u["tier"] or FREE_TIER_ID).strip() or FREE_TIER_ID
        try:
            if tier == FREE_TIER_ID:
                async with get_connection() as conn:
                    allowed = await get_free_tier_allowed_server_ids(conn, uid)
                target = [int(x) for x in allowed] or paid_server_ids
            else:
                target = paid_server_ids
            await ensure_user_keys_for_server_ids(uid, target)
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            log.warning("user %s failed: %s", uid, e)
        if i % 25 == 0 or i == total:
            print(f"PROGRESS {i}/{total} ok={ok} fail={fail}", flush=True)
        await asyncio.sleep(0.15)

    # final recount
    async with get_connection() as conn:
        remaining = await conn.fetchval(
            """
            SELECT COUNT(*) FROM users u
            WHERE u.blacklisted = FALSE AND u.pay_subscribed = TRUE
              AND (u.subscription_end IS NULL OR u.subscription_end >= CURRENT_DATE)
              AND NOT EXISTS (
                SELECT 1 FROM vpn_keys k JOIN servers s ON s.id = k.server_id
                WHERE k.user_id = u.user_id AND k.is_active
                  AND s.is_active AND COALESCE(s.is_system, FALSE) = FALSE
                  AND COALESCE(s.exclude_from_subscription, FALSE) = FALSE
                  AND (k.expires_at IS NULL OR DATE(k.expires_at) >= CURRENT_DATE)
              )
            """
        )
    print(f"DONE ok={ok} fail={fail} remaining_without_nodes={remaining}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
