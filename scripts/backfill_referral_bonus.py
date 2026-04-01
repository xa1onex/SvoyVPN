import argparse
import asyncio
import os
from datetime import datetime
from typing import Iterable

import asyncpg


def parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in raw.split(","):
        v = part.strip()
        if not v:
            continue
        if not v.isdigit():
            raise ValueError(f"Invalid user id: {v}")
        ids.append(int(v))
    return ids


def load_ids_from_file(path: str) -> list[int]:
    ids: list[int] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            if not raw.isdigit():
                raise ValueError(f"Invalid user id in file: {raw}")
            ids.append(int(raw))
    return ids


async def connect_db() -> asyncpg.Connection:
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return await asyncpg.connect(dsn=dsn)
    return await asyncpg.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "vpn_db"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


async def ensure_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS referral_backfill_compensations (
            invited_user_id BIGINT PRIMARY KEY,
            inviter_id BIGINT NOT NULL,
            bonus_days INTEGER NOT NULL,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


async def process_one(
    conn: asyncpg.Connection,
    invited_user_id: int,
    bonus_days: int,
    dry_run: bool,
    note: str,
) -> tuple[str, str]:
    invited = await conn.fetchrow(
        """
        SELECT user_id, invited_by
        FROM users
        WHERE user_id = $1
        """,
        invited_user_id,
    )
    if not invited:
        return ("skip", f"{invited_user_id}: invited user not found")
    if not invited["invited_by"]:
        return ("skip", f"{invited_user_id}: invited_by is NULL (not a referral user)")

    inviter_id = int(invited["invited_by"])

    already = await conn.fetchrow(
        """
        SELECT invited_user_id, inviter_id, bonus_days, created_at
        FROM referral_backfill_compensations
        WHERE invited_user_id = $1
        """,
        invited_user_id,
    )
    if already:
        return (
            "skip",
            (
                f"{invited_user_id}: already compensated "
                f"(inviter={already['inviter_id']}, days={already['bonus_days']}, at={already['created_at']})"
            ),
        )

    inviter = await conn.fetchrow(
        """
        SELECT user_id, subscription_end
        FROM users
        WHERE user_id = $1
        """,
        inviter_id,
    )
    if not inviter:
        return ("skip", f"{invited_user_id}: inviter {inviter_id} not found")

    if dry_run:
        return (
            "plan",
            f"{invited_user_id}: would add +{bonus_days} days to inviter {inviter_id}",
        )

    async with conn.transaction():
        updated = await conn.fetchrow(
            """
            UPDATE users
            SET
                subscription_end = CASE
                    WHEN subscription_end IS NULL OR subscription_end < CURRENT_DATE
                    THEN CURRENT_DATE + ($2 || ' days')::INTERVAL
                    ELSE subscription_end + ($2 || ' days')::INTERVAL
                END,
                pay_subscribed = TRUE
            WHERE user_id = $1
            RETURNING subscription_end
            """,
            inviter_id,
            str(bonus_days),
        )

        await conn.execute(
            """
            INSERT INTO referral_backfill_compensations (
                invited_user_id, inviter_id, bonus_days, note
            ) VALUES ($1, $2, $3, $4)
            """,
            invited_user_id,
            inviter_id,
            bonus_days,
            note,
        )

    new_end = updated["subscription_end"].strftime("%d.%m.%Y") if updated and updated["subscription_end"] else "—"
    return ("done", f"{invited_user_id}: compensated inviter {inviter_id}, new_end={new_end}")


async def run(ids: Iterable[int], bonus_days: int, dry_run: bool, note: str) -> None:
    ids = list(ids)
    conn = await connect_db()
    try:
        await ensure_table(conn)
        print(
            f"Start backfill: users={len(ids)}, bonus_days={bonus_days}, "
            f"dry_run={'yes' if dry_run else 'no'}, note={note!r}"
        )
    finally:
        await conn.close()
    conn = await connect_db()
    try:
        await ensure_table(conn)
        planned = 0
        done = 0
        skipped = 0
        for invited_user_id in ids:
            status, msg = await process_one(conn, invited_user_id, bonus_days, dry_run, note)
            print(msg)
            if status == "plan":
                planned += 1
            elif status == "done":
                done += 1
            else:
                skipped += 1

        print(
            f"Summary: planned={planned}, done={done}, skipped={skipped}, "
            f"finished_at={datetime.now().isoformat(timespec='seconds')}"
        )
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Safe referral bonus backfill. "
            "Compensates inviters for specific invited users with idempotency protection."
        )
    )
    parser.add_argument(
        "--invited-user-ids",
        type=str,
        default="",
        help="Comma-separated invited user IDs to compensate (example: 123,456,789)",
    )
    parser.add_argument(
        "--input-file",
        type=str,
        default="",
        help="Path to file with invited user IDs (one per line)",
    )
    parser.add_argument(
        "--bonus-days",
        type=int,
        required=True,
        help="How many days to add to inviter for each invited user in this run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only; do not write anything",
    )
    parser.add_argument(
        "--note",
        type=str,
        default="manual_referral_backfill",
        help="Audit note saved with compensation record",
    )
    args = parser.parse_args()

    if args.bonus_days <= 0:
        raise ValueError("--bonus-days must be > 0")

    ids: list[int] = []
    if args.invited_user_ids:
        ids.extend(parse_ids(args.invited_user_ids))
    if args.input_file:
        ids.extend(load_ids_from_file(args.input_file))
    ids = sorted(set(ids))
    if not ids:
        raise ValueError("Provide IDs via --invited-user-ids and/or --input-file")

    asyncio.run(run(ids=ids, bonus_days=args.bonus_days, dry_run=args.dry_run, note=args.note))


if __name__ == "__main__":
    main()
