"""Support tickets schema and CRUD."""

from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from typing import Any

from bot.database import get_connection

logger = logging.getLogger(__name__)


class TicketStatus(str, Enum):
    OPEN = "open"
    AWAITING_HUMAN = "awaiting_human"
    CLOSED = "closed"


class MessageRole(str, Enum):
    USER = "user"
    AI = "ai"
    STAFF = "staff"
    SYSTEM = "system"


async def init_support_tables() -> None:
    async with get_connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS support_tickets (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                human_requested BOOLEAN DEFAULT FALSE,
                assigned_staff_id BIGINT,
                rating SMALLINT,
                rating_comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS support_messages (
                id SERIAL PRIMARY KEY,
                ticket_id INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
                sender_role TEXT NOT NULL,
                sender_id BIGINT,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_support_tickets_user
            ON support_tickets(user_id, status)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_support_messages_ticket
            ON support_messages(ticket_id, created_at)
            """
        )
    logger.info("Support tables initialized")


async def create_ticket(user_id: int) -> int:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO support_tickets (user_id, status)
            VALUES ($1, $2)
            RETURNING id
            """,
            user_id,
            TicketStatus.OPEN.value,
        )
        return int(row["id"])


async def get_ticket(ticket_id: int) -> dict[str, Any] | None:
    async with get_connection() as conn:
        row = await conn.fetchrow("SELECT * FROM support_tickets WHERE id = $1", ticket_id)
        return dict(row) if row else None


async def get_user_open_ticket(user_id: int) -> dict[str, Any] | None:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM support_tickets
            WHERE user_id = $1 AND status != $2
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            user_id,
            TicketStatus.CLOSED.value,
        )
        return dict(row) if row else None


async def list_user_tickets(user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, status, human_requested, rating, created_at, closed_at
            FROM support_tickets
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
        return [dict(r) for r in rows]


async def list_open_tickets_for_staff(limit: int = 30) -> list[dict[str, Any]]:
    """Все открытые тикеты для оператора (очередь)."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT t.*, u.username, u.first_name
            FROM support_tickets t
            LEFT JOIN users u ON u.user_id = t.user_id
            WHERE t.status != $1
            ORDER BY
                CASE WHEN t.human_requested THEN 0 ELSE 1 END,
                t.updated_at DESC
            LIMIT $2
            """,
            TicketStatus.CLOSED.value,
            limit,
        )
        return [dict(r) for r in rows]


async def list_awaiting_human_tickets(limit: int = 30) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT t.*, u.username, u.first_name
            FROM support_tickets t
            LEFT JOIN users u ON u.user_id = t.user_id
            WHERE t.status = $1 OR t.human_requested = TRUE AND t.status != $2
            ORDER BY t.updated_at DESC
            LIMIT $3
            """,
            TicketStatus.AWAITING_HUMAN.value,
            TicketStatus.CLOSED.value,
            limit,
        )
        return [dict(r) for r in rows]


async def update_ticket_status(
    ticket_id: int,
    status: TicketStatus,
    *,
    human_requested: bool | None = None,
    assigned_staff_id: int | None = None,
) -> None:
    async with get_connection() as conn:
        sets = ["status = $2", "updated_at = CURRENT_TIMESTAMP"]
        args: list[Any] = [ticket_id, status.value]
        idx = 3
        if human_requested is not None:
            sets.append(f"human_requested = ${idx}")
            args.append(human_requested)
            idx += 1
        if assigned_staff_id is not None:
            sets.append(f"assigned_staff_id = ${idx}")
            args.append(assigned_staff_id)
            idx += 1
        if status == TicketStatus.CLOSED:
            sets.append("closed_at = CURRENT_TIMESTAMP")
        await conn.execute(
            f"UPDATE support_tickets SET {', '.join(sets)} WHERE id = $1",
            *args,
        )


async def save_rating(ticket_id: int, rating: int, comment: str | None = None) -> None:
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE support_tickets
            SET rating = $2, rating_comment = $3, updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
            """,
            ticket_id,
            rating,
            comment,
        )


async def add_message(
    ticket_id: int,
    role: MessageRole,
    text: str,
    sender_id: int | None = None,
) -> int:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO support_messages (ticket_id, sender_role, sender_id, text)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            ticket_id,
            role.value,
            sender_id,
            text,
        )
        await conn.execute(
            "UPDATE support_tickets SET updated_at = CURRENT_TIMESTAMP WHERE id = $1",
            ticket_id,
        )
        return int(row["id"])


async def get_ticket_messages(ticket_id: int, limit: int = 40) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT sender_role, sender_id, text, created_at
            FROM support_messages
            WHERE ticket_id = $1
            ORDER BY created_at ASC
            LIMIT $2
            """,
            ticket_id,
            limit,
        )
        return [dict(r) for r in rows]


def format_ticket_status(status: str) -> str:
    mapping = {
        TicketStatus.OPEN.value: "🟢 Открыт",
        TicketStatus.AWAITING_HUMAN.value: "🙋 Ожидает оператора",
        TicketStatus.CLOSED.value: "✅ Закрыт",
    }
    return mapping.get(status, status)
