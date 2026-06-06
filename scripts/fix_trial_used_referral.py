#!/usr/bin/env python3
"""
Сброс ложного trial_used у пользователей без реальной оплаты «Plus за 1₽».

Раньше при регистрации по рефералке выставлялся trial_used=TRUE без платежа.

Запуск из корня проекта:
  venv/bin/python scripts/fix_trial_used_referral.py          # dry-run
  venv/bin/python scripts/fix_trial_used_referral.py --apply  # записать в БД
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from bot.database import get_connection
from bot.trial_usage import has_completed_trial_payment, sync_trial_used_flag


async def main(apply: bool) -> None:
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, username, first_name, invited_by, trial_used
            FROM users
            WHERE trial_used = TRUE
            ORDER BY user_id
            """
        )

        to_reset: list[dict] = []
        keep: list[dict] = []

        for r in rows:
            uid = int(r["user_id"])
            if await has_completed_trial_payment(conn, uid):
                keep.append(dict(r))
            else:
                to_reset.append(dict(r))

    print(f"trial_used=TRUE всего: {len(rows)}")
    print(f"  с реальной оплатой 1₽ (оставляем): {len(keep)}")
    print(f"  без оплаты триала (сброс): {len(to_reset)}")

    if to_reset:
        print("\nСброс trial_used → FALSE (первые 30):")
        for u in to_reset[:30]:
            inv = f" invited_by={u['invited_by']}" if u["invited_by"] else ""
            print(
                f"  {u['user_id']} @{u['username'] or '—'}{inv}"
            )
        if len(to_reset) > 30:
            print(f"  … и ещё {len(to_reset) - 30}")

    if not apply:
        print("\nDry-run. Для записи: --apply")
        return

    updated = 0
    async with get_connection() as conn:
        for u in to_reset:
            uid = int(u["user_id"])
            await sync_trial_used_flag(conn, uid)
            updated += 1

    print(f"\nОбновлено пользователей: {updated}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Записать изменения в БД",
    )
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
