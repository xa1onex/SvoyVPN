"""
Временная утилита: полное удаление пользователя из БД и панелей X-UI (для тестов).
"""
from __future__ import annotations

import logging

from .database import get_connection
from .xui_client import XUIClient

logger = logging.getLogger(__name__)

# Навигационные заглушки подписки — не существуют на X-UI.
_NAV_PLACEHOLDER_IDS = frozenset({
    "00000000000000000000000000000001",
    "00000000000000000000000000000002",
})

_USER_CHILD_TABLES: tuple[tuple[str, str], ...] = (
    ("vpn_keys", "user_id"),
    ("payments", "user_id"),
    ("subscription_reminders", "user_id"),
    ("subscription_usage_logs", "user_id"),
    ("user_subscription_ips", "user_id"),
    ("miniapp_usage_logs", "user_id"),
    ("utm_visits", "user_id"),
    ("user_notifications", "user_id"),
    ("user_device_fingerprints", "user_id"),
    ("user_balances", "user_id"),
    ("balance_transactions", "user_id"),
    ("balance_withdrawal_requests", "user_id"),
    ("managers", "user_id"),
    ("esim_orders", "user_id"),
    ("esim_beta_waitlist", "user_id"),
    ("esim_beta_requests", "user_id"),
    ("bypass_traffic_notifications", "user_id"),
)


async def _delete_xui_clients(user_id: int) -> tuple[int, int]:
    """Удаляет клиентов на панелях X-UI. Возвращает (успешно, ошибок)."""
    ok, err = 0, 0
    async with get_connection() as conn:
        keys = await conn.fetch(
            """
            SELECT k.vless_client_id, s.base_url, s.username, s.password, s.inbound_id
            FROM vpn_keys k
            JOIN servers s ON k.server_id = s.id
            WHERE k.user_id = $1
            """,
            user_id,
        )

    by_server: dict[tuple, list[str]] = {}
    for key in keys:
        cid = str(key["vless_client_id"] or "").strip()
        if not cid or cid.replace("-", "") in _NAV_PLACEHOLDER_IDS:
            continue
        srv = (
            key["base_url"],
            key["username"],
            key["password"],
            key["inbound_id"],
        )
        by_server.setdefault(srv, []).append(cid)

    for (base_url, username, password, inbound_id), client_ids in by_server.items():
        client = XUIClient(
            base_url=base_url,
            username=username,
            password=password,
            inbound_id=inbound_id,
        )
        try:
            for cid in client_ids:
                try:
                    await client.delete_client(cid)
                    ok += 1
                except Exception as e:
                    logger.warning(
                        "test_delete_user: X-UI delete failed user=%s client=%s: %s",
                        user_id,
                        cid,
                        e,
                    )
                    err += 1
        finally:
            await client.close()

    return ok, err


async def _revert_inviter_referral(conn, user_id: int, invited_by: int | None) -> None:
    if not invited_by:
        return
    await conn.execute(
        """
        UPDATE users SET
            referral_count = GREATEST(COALESCE(referral_count, 0) - 1, 0),
            referral_discount_percent = GREATEST(COALESCE(referral_discount_percent, 0) - 5, 0),
            referral_bonus_bypass_percent = GREATEST(COALESCE(referral_bonus_bypass_percent, 0) - 5, 0)
        WHERE user_id = $1
        """,
        invited_by,
    )
    await conn.execute(
        "UPDATE users SET invited_by = NULL WHERE invited_by = $1",
        user_id,
    )


async def purge_user_completely(user_id: int) -> dict:
    """
    Полностью удаляет пользователя и все связанные записи.
    Возвращает сводку {found, deleted_tables, xui_ok, xui_errors}.
    """
    async with get_connection() as conn:
        user = await conn.fetchrow(
            "SELECT user_id, invited_by FROM users WHERE user_id = $1",
            user_id,
        )
        if not user:
            return {"found": False}

    xui_ok, xui_err = await _delete_xui_clients(user_id)

    deleted: dict[str, int] = {}
    async with get_connection() as conn:
        async with conn.transaction():
            await _revert_inviter_referral(conn, user_id, user["invited_by"])

            for table, col in _USER_CHILD_TABLES:
                try:
                    tag = await conn.execute(
                        f"DELETE FROM {table} WHERE {col} = $1",
                        user_id,
                    )
                    deleted[table] = int(tag.split()[-1]) if tag else 0
                except Exception as e:
                    logger.warning("test_delete_user: skip %s: %s", table, e)

            for ref_table, ref_sql in (
                (
                    "referral_invite_rewards",
                    "DELETE FROM referral_invite_rewards "
                    "WHERE invited_user_id = $1 OR inviter_id = $1",
                ),
                (
                    "referral_backfill_compensations",
                    "DELETE FROM referral_backfill_compensations "
                    "WHERE invited_user_id = $1 OR inviter_id = $1",
                ),
                (
                    "referral_purchase_rewards",
                    "DELETE FROM referral_purchase_rewards "
                    "WHERE payer_user_id = $1 OR referrer_id = $1",
                ),
                (
                    "referral_tg_gift_claims",
                    "DELETE FROM referral_tg_gift_claims WHERE referrer_id = $1",
                ),
            ):
                try:
                    tag = await conn.execute(ref_sql, user_id)
                    deleted[ref_table] = int(tag.split()[-1]) if tag else 0
                except Exception as e:
                    logger.warning("test_delete_user: skip %s: %s", ref_table, e)

            try:
                tag = await conn.execute(
                    "DELETE FROM app_accounts WHERE user_id = $1",
                    user_id,
                )
                deleted["app_accounts"] = int(tag.split()[-1]) if tag else 0
            except Exception as e:
                logger.warning("test_delete_user: app_accounts: %s", e)

            tag = await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
            deleted["users"] = int(tag.split()[-1]) if tag else 0

    logger.info(
        "test_delete_user: purged user_id=%s tables=%s xui_ok=%s xui_err=%s",
        user_id,
        deleted,
        xui_ok,
        xui_err,
    )
    return {
        "found": True,
        "deleted": deleted,
        "xui_ok": xui_ok,
        "xui_errors": xui_err,
    }
