"""
Одноразовый пересчёт bypass-лимита (ГБ) для всех пользователей.

После фикса раздельных Remnawave-identity старые bypass_traffic_used_bytes
могли включать трафик с обычных (не-bypass) серверов. Скрипт:
  1) синхронизирует lifetime с панелей (traffic worker);
  2) ставит baseline = lifetime на всех bypass-ключах (дельта периода = 0);
  3) обнуляет users.bypass_traffic_used_bytes;
  4) пересчитывает агрегат (как воркер).

Пакеты bypass_bonus_gb не трогаем.
"""
from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv

load_dotenv("/root/SvoyVPN/.env")

from bot.database import get_connection, get_pool
from bot.traffic_worker import _aggregate_bypass_traffic, run_sync_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("recalc_bypass_traffic")


async def main() -> None:
    await get_pool()

    async with get_connection() as conn:
        before = await conn.fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE bypass_traffic_used_bytes > 0) AS nz,
                   COALESCE(SUM(bypass_traffic_used_bytes), 0) AS total_bytes
            FROM users
            """
        )
        logger.info(
            "Before: %s users with usage, total %.2f GB",
            before["nz"],
            int(before["total_bytes"] or 0) / (1024**3),
        )

    logger.info("Step 1/4: sync lifetimes from panels...")
    stats = await run_sync_once()
    logger.info("Sync done: %s", stats)

    async with get_connection() as conn:
        keys_updated = await conn.execute(
            """
            UPDATE vpn_keys k
            SET traffic_period_baseline_bytes = k.traffic_lifetime_bytes,
                traffic_last_sync_at = COALESCE(k.traffic_last_sync_at, NOW())
            FROM servers s
            WHERE k.server_id = s.id
              AND s.is_bypass = TRUE
              AND k.is_active = TRUE
            """
        )
        users_updated = await conn.execute(
            """
            UPDATE users
            SET bypass_traffic_used_bytes = 0,
                bypass_last_sync_at = NOW()
            WHERE traffic_anchor_day IS NOT NULL
               OR user_id IN (
                   SELECT DISTINCT user_id FROM vpn_keys k
                   JOIN servers s ON s.id = k.server_id
                   WHERE k.is_active = TRUE AND s.is_bypass = TRUE
               )
            """
        )
        logger.info("Step 2/4: baselines reset (%s)", keys_updated)
        logger.info("Step 3/4: user counters zeroed (%s)", users_updated)

    logger.info("Step 4/4: aggregate bypass traffic...")
    await _aggregate_bypass_traffic()

    async with get_connection() as conn:
        after = await conn.fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE bypass_traffic_used_bytes > 0) AS nz,
                   COALESCE(SUM(bypass_traffic_used_bytes), 0) AS total_bytes,
                   COALESCE(MAX(bypass_traffic_used_bytes), 0) AS max_bytes
            FROM users
            """
        )
        logger.info(
            "After: %s users with usage, total %.4f GB, max %.4f GB",
            after["nz"],
            int(after["total_bytes"] or 0) / (1024**3),
            int(after["max_bytes"] or 0) / (1024**3),
        )

    pool = await get_pool()
    await pool.close()
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
