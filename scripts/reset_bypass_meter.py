"""
Одноразовый переход с глобального UUID-счётчика Remnawave на выделенную ноду.

Запускать при остановленном svoyvpn после отключения обычных inbound на bypass-ноде.
Старый загрязнённый расход обнуляется, а купленные пакеты восстанавливаются полностью.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv("/root/SvoyVPN/.env")

from bot.database import get_connection, get_pool, init_db
from bot.traffic import BYTES_PER_GB
from bot.traffic_worker import (
    _collect_remnawave_usage,
    _refresh_bypass_periods,
    _sum_node_usage_by_period,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reset_bypass_meter")


async def main() -> None:
    await init_db()
    await _refresh_bypass_periods()

    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, bypass_period_start, bypass_period_end_excl
            FROM users
            WHERE bypass_period_start IS NOT NULL
              AND bypass_period_end_excl IS NOT NULL
            """
        )
        node_rows = await conn.fetch(
            """
            SELECT DISTINCT remnawave_node_uuid
            FROM servers
            WHERE is_bypass = TRUE
              AND panel_type = 'remnawave'
              AND remnawave_node_uuid IS NOT NULL
            """
        )
        key_rows = await conn.fetch(
            """
            SELECT k.user_id, k.vless_client_id
            FROM vpn_keys k
            JOIN servers s ON s.id = k.server_id
            WHERE k.is_active = TRUE
              AND s.is_active = TRUE
              AND s.is_bypass = TRUE
              AND s.panel_type = 'remnawave'
            """
        )

    periods = {
        int(row["user_id"]): (
            row["bypass_period_start"],
            row["bypass_period_end_excl"],
        )
        for row in rows
    }
    today = date.today()
    earliest = min((period[0] for period in periods.values()), default=today - timedelta(days=35))
    result = await _collect_remnawave_usage(
        bypass_node_uuids={str(row["remnawave_node_uuid"]) for row in node_rows},
        period_start=earliest,
        period_end_exclusive=today + timedelta(days=1),
    )
    if result is None:
        raise RuntimeError("Remnawave usage is unavailable; migration aborted")
    node_records, remnawave_to_vless = result
    user_ids_by_vless_uuid = {
        str(row["vless_client_id"]).lower().replace("-", ""): int(row["user_id"])
        for row in key_rows
        if row["vless_client_id"]
    }
    user_ids_by_remnawave_uuid = {
        remnawave_uuid: user_ids_by_vless_uuid[vless_uuid]
        for remnawave_uuid, vless_uuid in remnawave_to_vless.items()
        if vless_uuid in user_ids_by_vless_uuid
    }
    raw_totals = _sum_node_usage_by_period(
        node_records, periods, user_ids_by_remnawave_uuid
    )

    async with get_connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE vpn_keys k
                SET traffic_period_baseline_bytes = k.traffic_lifetime_bytes
                FROM servers s
                WHERE k.server_id = s.id
                  AND s.is_bypass = TRUE
                  AND s.panel_type <> 'remnawave'
                  AND k.is_active = TRUE
                """
            )
            for user_id, period in periods.items():
                source_baseline = int(raw_totals.get(user_id, 0))
                await conn.execute(
                    """
                    UPDATE users
                    SET bypass_traffic_used_bytes = 0,
                        bypass_meter_baseline_bytes = $1,
                        bypass_meter_period_start = $2,
                        bypass_bonus_bytes = COALESCE(
                            bypass_pack_purchased_bytes,
                            COALESCE(bypass_pack_purchased_gb, 0)::bigint * $4
                        ),
                        bypass_bonus_gb = CEIL(
                            COALESCE(
                                bypass_pack_purchased_bytes,
                                COALESCE(bypass_pack_purchased_gb, 0)::bigint * $4
                            )::numeric / $4
                        )::integer,
                        bypass_last_sync_at = NOW()
                    WHERE user_id = $3
                    """,
                    source_baseline,
                    period[0],
                    user_id,
                    BYTES_PER_GB,
                )

    logger.info(
        "Bypass meter reset for %s users; %s node-history rows anchored",
        len(periods),
        len(node_records),
    )
    pool = await get_pool()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
