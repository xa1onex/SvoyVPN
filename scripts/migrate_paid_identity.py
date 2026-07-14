"""
Этап A миграции на раздельные Remnawave-identity (bypass vs paid).

Для каждого активного ключа на платных/быстрых Remnawave-хостах (43/44/45,
is_bypass=FALSE) пересоздаёт ссылку через ОТДЕЛЬНЫЙ "paid"-идентити
(username tg_{id}_paid, squad = Default-Squad с gRPC).

Ничего не ломает: старая (bypass) identity пользователя ещё не трогается,
её squad не меняется на этом этапе — просто рядом появляется вторая, новая
ссылка для gRPC-хостов.

Запуск: python3 -m scripts.migrate_paid_identity
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from bot.database import get_connection, get_pool
from bot.subscriptions import ensure_user_keys_for_server_ids

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_paid_identity")

PAID_SERVER_IDS = [43, 44, 45]


async def main() -> None:
    await get_pool()
    try:
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT k.user_id, k.server_id
                FROM vpn_keys k
                WHERE k.server_id = ANY($1::int[]) AND k.is_active = TRUE
                ORDER BY k.user_id
                """,
                PAID_SERVER_IDS,
            )

        by_user: dict[int, list[int]] = {}
        for r in rows:
            by_user.setdefault(int(r["user_id"]), []).append(int(r["server_id"]))

        total = len(by_user)
        logger.info("Users to migrate to paid identity: %d", total)

        sem = asyncio.Semaphore(3)
        done = 0
        errors: list[tuple[int, str]] = []

        async def _one(uid: int, server_ids: list[int]) -> None:
            nonlocal done
            async with sem:
                try:
                    await ensure_user_keys_for_server_ids(uid, server_ids)
                except Exception as e:  # noqa: BLE001
                    errors.append((uid, str(e)))
                    logger.error("Failed for user %s servers %s: %s", uid, server_ids, e)
                finally:
                    done += 1
                    if done % 50 == 0:
                        logger.info("Progress: %d/%d", done, total)

        await asyncio.gather(*[_one(uid, sids) for uid, sids in by_user.items()])

        logger.info("Done. %d/%d succeeded, %d errors", total - len(errors), total, len(errors))
        for uid, err in errors[:30]:
            logger.error("  user %s: %s", uid, err)
    finally:
        pool = await get_pool()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
