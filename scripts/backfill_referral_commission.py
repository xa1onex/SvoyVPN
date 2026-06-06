#!/usr/bin/env python3
"""Доначисление пропущенной реферальной комиссии по старым оплатам."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.referral_commission import (
    backfill_missing_referral_commissions,
    rebalance_referral_commissions_for_new_rates,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill / rebalance referral commissions")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only count payments that would be credited",
    )
    parser.add_argument(
        "--rebalance-rates",
        action="store_true",
        help="Top up balances to match current tier rates (20-40%%) on all past payments",
    )
    args = parser.parse_args()
    if args.rebalance_rates:
        stats = await rebalance_referral_commissions_for_new_rates(dry_run=args.dry_run)
    else:
        stats = await backfill_missing_referral_commissions(dry_run=args.dry_run)
    print(stats)


if __name__ == "__main__":
    asyncio.run(main())
