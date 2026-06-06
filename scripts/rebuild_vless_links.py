#!/usr/bin/env python3
"""Пересобрать vless_link для всех активных серверов (после фикса Reality-параметров)."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Корень проекта в PYTHONPATH
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from bot.database import get_connection  # noqa: E402
from bot.subscriptions import (  # noqa: E402
    ensure_navigation_header_fake_links_for_server,
    update_vless_links_for_server,
)
from bot.traffic import is_navigation_header_server  # noqa: E402


async def main(server_ids: list[int] | None) -> None:
    async with get_connection() as conn:
        if server_ids:
            rows = await conn.fetch(
                "SELECT id, name FROM servers WHERE id = ANY($1::int[]) ORDER BY id",
                server_ids,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, name FROM servers WHERE is_active = TRUE ORDER BY display_order, id"
            )

    if not rows:
        print("No servers found.")
        return

    print(f"Rebuilding links for {len(rows)} server(s)...")
    for row in rows:
        sid = int(row["id"])
        name = str(row["name"])
        if is_navigation_header_server(name):
            await ensure_navigation_header_fake_links_for_server(sid)
            print(f"  NAV (fake) #{sid} {name}")
        else:
            await update_vless_links_for_server(sid)
            print(f"  OK #{sid} {name}")
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "server_ids",
        nargs="*",
        type=int,
        help="ID серверов (по умолчанию — все активные)",
    )
    args = parser.parse_args()
    ids = args.server_ids or None
    asyncio.run(main(ids))
