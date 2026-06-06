"""Execute VPN backend tools (real DB / service calls)."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from datetime import datetime
from typing import Any, Callable, Awaitable

from bot.balance import credit_balance, debit_balance, get_balance
from bot.database import (
    count_active_devices,
    get_connection,
    get_user_subscription_ips,
    set_announcement_text,
)
from bot.plans import (
    FREE_TIER_ID,
    PAID_TIER_IDS,
    TIERS,
    get_bypass_packs,
    get_tier_bypass_gb,
    get_tier_max_devices,
    get_tier_plans,
)
from bot.subscriptions import (
    create_or_activate_keys_for_all_servers,
    extend_subscription,
    get_subscription_status,
    grant_free_tier,
    set_new_subscription_days,
    sync_user_keys,
)
from bot.traffic import (
    apply_subscription_anchor_on_payment,
    get_traffic_settings,
    user_bypass_traffic_snapshot,
    user_traffic_snapshot,
)

from .definitions import (
    READ_TOOL_NAMES,
    TOOL_DEFINITIONS_READONLY,
    TOOL_DEFINITIONS_STAFF,
    WRITE_TOOL_NAMES,
)

logger = logging.getLogger(__name__)

_main_bot = None


async def _get_main_bot():
    global _main_bot
    if _main_bot is None:
        from aiogram import Bot
        from support_bot.config import load_support_config

        cfg = load_support_config()
        _main_bot = Bot(token=cfg.main_bot_token)
    return _main_bot


def _json(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


async def _user_snapshot(user_id: int) -> dict[str, Any]:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT subscription_end, pay_subscribed, subscription_tier, blacklisted,
                   traffic_limit_gb, traffic_bonus_gb,
                   bypass_traffic_limit_gb, bypass_bonus_gb, device_limit, trial_used
            FROM users WHERE user_id = $1
            """,
            user_id,
        )
    if not row:
        return {"error": "user not found"}
    end = row["subscription_end"]
    return {
        "user_id": user_id,
        "subscription_end": end.isoformat() if end else None,
        "pay_subscribed": row["pay_subscribed"],
        "subscription_tier": row["subscription_tier"],
        "blacklisted": row["blacklisted"],
        "device_limit": row["device_limit"],
        "trial_used": row["trial_used"],
        "traffic_limit_gb": row["traffic_limit_gb"],
        "traffic_bonus_gb": row["traffic_bonus_gb"],
        "bypass_traffic_limit_gb": row["bypass_traffic_limit_gb"],
        "bypass_bonus_gb": row["bypass_bonus_gb"],
        "bypass_total_gb": int(row["bypass_traffic_limit_gb"] or 0) + int(row["bypass_bonus_gb"] or 0),
    }


async def _ok_write(action: str, user_id: int | None = None, **extra: Any) -> dict:
    out: dict[str, Any] = {"ok": True, "action": action, **extra}
    if user_id is not None:
        out["after"] = await _user_snapshot(user_id)
    return out


# ---------- Read handlers ----------


async def _get_user_profile(args: dict) -> dict:
    uid = int(args["user_id"])
    async with get_connection() as conn:
        user = await conn.fetchrow(
            """
            SELECT user_id, username, first_name, registration_date, last_activity,
                   pay_subscribed, subscription_end, subscription_tier, invited_by,
                   referral_count, balance, trial_used, utm_source, blacklisted,
                   device_limit, traffic_limit_gb, traffic_bonus_gb,
                   bypass_traffic_limit_gb, bypass_bonus_gb,
                   yookassa_recurring_payment_method_id IS NOT NULL AS has_autopay,
                   esim_beta_access, pending_downgrade_tier
            FROM users WHERE user_id = $1
            """,
            uid,
        )
        if not user:
            return {"error": f"Пользователь {uid} не найден"}
        payments = await conn.fetch(
            "SELECT amount, currency, timestamp, status, plan_id FROM payments "
            "WHERE user_id = $1 ORDER BY timestamp DESC LIMIT 5",
            uid,
        )
        keys_count = await conn.fetchval(
            "SELECT COUNT(*) FROM vpn_keys WHERE user_id = $1 AND is_active = TRUE", uid
        )
        bal = await get_balance(conn, uid)
        traffic = await user_traffic_snapshot(conn, uid, sync_from_panels=False)
        bypass = await user_bypass_traffic_snapshot(conn, uid)
        dev_count, dev_lim = await count_active_devices(conn, uid, hours=6)

    status = await get_subscription_status(uid)
    end = user["subscription_end"]
    return {
        "user_id": uid,
        "username": user["username"],
        "first_name": user["first_name"],
        "subscription_status": status,
        "subscription_end": end.isoformat() if end else None,
        "subscription_tier": user["subscription_tier"],
        "referral_balance_cents": bal,
        "trial_used": user["trial_used"],
        "blacklisted": user["blacklisted"],
        "device_limit": user["device_limit"],
        "active_devices_6h": dev_count,
        "has_autopay": user["has_autopay"],
        "esim_beta_access": user["esim_beta_access"],
        "bypass_total_gb": int(user["bypass_traffic_limit_gb"] or 0) + int(user["bypass_bonus_gb"] or 0),
        "traffic": traffic,
        "bypass_traffic": bypass,
        "active_vpn_keys": keys_count,
        "recent_payments": [dict(p) for p in payments],
    }


async def _search_user(args: dict) -> dict:
    q = str(args["query"]).strip().lstrip("@")
    async with get_connection() as conn:
        if q.isdigit():
            row = await conn.fetchrow(
                "SELECT user_id, username, first_name FROM users WHERE user_id = $1", int(q)
            )
            return {"matches": [dict(row)] if row else [], "message": "Не найден" if not row else None}
        rows = await conn.fetch(
            "SELECT user_id, username, first_name FROM users WHERE LOWER(username)=LOWER($1) LIMIT 5", q
        )
        if not rows:
            rows = await conn.fetch(
                "SELECT user_id, username, first_name FROM users "
                "WHERE username ILIKE $1 OR first_name ILIKE $1 LIMIT 5",
                f"%{q}%",
            )
        return {"matches": [dict(r) for r in rows]}


async def _get_user_activity(args: dict) -> dict:
    uid, limit = int(args["user_id"]), min(max(int(args.get("limit", 15)), 1), 30)
    async with get_connection() as conn:
        vpn = await conn.fetch(
            "SELECT user_agent, ip_address, timestamp FROM subscription_usage_logs "
            "WHERE user_id=$1 ORDER BY timestamp DESC LIMIT $2", uid, limit
        )
        app = await conn.fetch(
            "SELECT action, timestamp FROM miniapp_usage_logs "
            "WHERE user_id=$1 ORDER BY timestamp DESC LIMIT $2", uid, limit
        )
        ips = await get_user_subscription_ips(conn, uid, limit=10)
    return {
        "vpn_sub_requests": [dict(r) for r in vpn],
        "miniapp_actions": [dict(r) for r in app],
        "subscription_ips": [dict(r) for r in ips],
    }


async def _get_user_payments(args: dict) -> dict:
    uid = int(args["user_id"])
    limit = min(max(int(args.get("limit", 15)), 1), 30)
    async with get_connection() as conn:
        rows = await conn.fetch(
            "SELECT id, amount, currency, timestamp, status, plan_id, yookassa_payment_id "
            "FROM payments WHERE user_id=$1 ORDER BY timestamp DESC LIMIT $2",
            uid, limit,
        )
    return {"payments": [dict(r) for r in rows]}


async def _get_user_vpn_keys(args: dict) -> dict:
    uid = int(args["user_id"])
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT vk.id, vk.server_id, s.name AS server_name, vk.is_active,
                   vk.expires_at, s.is_bypass
            FROM vpn_keys vk
            JOIN servers s ON s.id = vk.server_id
            WHERE vk.user_id = $1
            ORDER BY vk.is_active DESC, s.name
            """,
            uid,
        )
    return {"keys": [dict(r) for r in rows]}


async def _get_user_balance(args: dict) -> dict:
    uid = int(args["user_id"])
    async with get_connection() as conn:
        bal = await get_balance(conn, uid)
        txs = await conn.fetch(
            "SELECT amount, type, description, created_at FROM balance_transactions "
            "WHERE user_id=$1 ORDER BY created_at DESC LIMIT 15",
            uid,
        )
    return {"balance_cents": bal, "balance_rub": round(bal / 100, 2), "transactions": [dict(t) for t in txs]}


async def _get_user_devices(args: dict) -> dict:
    uid = int(args["user_id"])
    async with get_connection() as conn:
        count, limit = await count_active_devices(conn, uid, hours=6)
        fps = await conn.fetch(
            "SELECT fingerprint, last_seen FROM user_device_fingerprints "
            "WHERE user_id=$1 ORDER BY last_seen DESC LIMIT 10",
            uid,
        )
    return {"active_count": count, "device_limit": limit, "fingerprints": [dict(f) for f in fps]}


async def _get_service_overview(args: dict) -> dict:
    async with get_connection() as conn:
        return {
            "total_users": await conn.fetchval("SELECT COUNT(*) FROM users"),
            "active_subscriptions": await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE pay_subscribed AND subscription_end >= NOW()"
            ),
            "payments_today": await conn.fetchval(
                "SELECT COUNT(*) FROM payments WHERE status='completed' AND timestamp >= CURRENT_DATE"
            ),
            "open_support_tickets": await conn.fetchval(
                "SELECT COUNT(*) FROM support_tickets WHERE status != 'closed'"
            ),
            "servers": [
                dict(r)
                for r in await conn.fetch(
                    "SELECT id, name, is_active, is_bypass FROM servers ORDER BY id"
                )
            ],
        }


async def _get_admin_stats(args: dict) -> dict:
    async with get_connection() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users")
        active = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE pay_subscribed AND subscription_end >= NOW()"
        )
        revenue = await conn.fetchval(
            "SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='completed' AND currency='RUB'"
        )
        keys = await conn.fetchval("SELECT COUNT(*) FROM vpn_keys WHERE is_active")
        tiers = await conn.fetch(
            """
            SELECT COALESCE(NULLIF(subscription_tier,''),'free') AS tier, COUNT(*) AS cnt
            FROM users GROUP BY 1
            """
        )
        pending_wd = await conn.fetchval(
            "SELECT COUNT(*) FROM balance_withdrawal_requests WHERE status='pending'"
        )
    return {
        "users_total": total,
        "subscriptions_active": active,
        "revenue_rub_kopecks": revenue,
        "active_vpn_keys": keys,
        "tier_distribution": {r["tier"]: r["cnt"] for r in tiers},
        "pending_withdrawals": pending_wd,
    }


async def _get_system_logs(args: dict) -> dict:
    from support_bot.config import load_support_config

    cfg = load_support_config()
    lines = min(max(int(args.get("lines", cfg.journal_lines)), 10), 150)
    unit = args.get("unit") or cfg.journal_unit
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        logs = proc.stdout[-12000:] if proc.returncode == 0 else proc.stderr
    except Exception as e:
        logs = str(e)
    return {"logs": logs}


async def _list_servers(args: dict) -> dict:
    active_only = bool(args.get("active_only", False))
    async with get_connection() as conn:
        q = "SELECT id, name, ip, is_active, is_bypass, exclude_from_subscription, display_order FROM servers"
        if active_only:
            q += " WHERE is_active = TRUE"
        q += " ORDER BY display_order NULLS LAST, id"
        rows = await conn.fetch(q)
    return {"servers": [dict(r) for r in rows]}


async def _get_server_detail(args: dict) -> dict:
    sid = int(args["server_id"])
    async with get_connection() as conn:
        s = await conn.fetchrow("SELECT * FROM servers WHERE id=$1", sid)
        if not s:
            return {"error": "server not found"}
        key_stats = await conn.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE is_active) AS active_keys, COUNT(*) AS total_keys "
            "FROM vpn_keys WHERE server_id=$1",
            sid,
        )
    return {"server": dict(s), "keys": dict(key_stats)}


async def _get_traffic_settings_tool(args: dict) -> dict:
    async with get_connection() as conn:
        return dict(await get_traffic_settings(conn))


async def _get_pricing_catalog(args: dict) -> dict:
    tier_plans = await get_tier_plans()
    async with get_connection() as conn:
        legacy = await conn.fetch("SELECT plan_id, price_rub, price_stars FROM price_settings")
    return {"tier_plans": tier_plans, "legacy_prices": [dict(r) for r in legacy], "tiers": TIERS}


async def _get_bypass_packs(args: dict) -> dict:
    return {"packs": await get_bypass_packs()}


async def _get_gb_traffic_packs(args: dict) -> dict:
    async with get_connection() as conn:
        rows = await conn.fetch(
            "SELECT id, title, gb_amount, price_rub, price_stars, is_active "
            "FROM gb_pack_products ORDER BY display_order, gb_amount"
        )
    return {"packs": [dict(r) for r in rows]}


async def _get_referral_settings(args: dict) -> dict:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT inviter_bonus_days, invited_bonus_days FROM referral_settings ORDER BY id DESC LIMIT 1"
        )
    return dict(row) if row else {}


async def _get_trial_settings(args: dict) -> dict:
    async with get_connection() as conn:
        row = await conn.fetchrow("SELECT days FROM trial_settings ORDER BY id DESC LIMIT 1")
    return {"days": row["days"] if row else 0}


async def _get_discount_settings(args: dict) -> dict:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT days_threshold, enable_for_all FROM discount_settings ORDER BY id DESC LIMIT 1"
        )
    return dict(row) if row else {}


async def _get_announcement(args: dict) -> dict:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT text, updated_at FROM announcements ORDER BY id DESC LIMIT 1"
        )
    return dict(row) if row else {"text": None}


async def _list_support_managers(args: dict) -> dict:
    async with get_connection() as conn:
        rows = await conn.fetch(
            "SELECT user_id, support_link, is_active FROM managers ORDER BY is_active DESC"
        )
    return {"managers": [dict(r) for r in rows]}


async def _list_utm_campaigns(args: dict) -> dict:
    limit = min(max(int(args.get("limit", 20)), 1), 50)
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"SELECT tag, description, bonus_days, is_active, created_at FROM utm_campaigns "
            f"ORDER BY created_at DESC LIMIT {limit}"
        )
    return {"campaigns": [dict(r) for r in rows]}


async def _list_pending_withdrawals(args: dict) -> dict:
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT w.id, w.user_id, w.amount_cents, w.status, w.created_at,
                   u.username, u.first_name
            FROM balance_withdrawal_requests w
            LEFT JOIN users u ON u.user_id = w.user_id
            WHERE w.status = 'pending'
            ORDER BY w.created_at
            """
        )
    return {"requests": [dict(r) for r in rows]}


async def _get_global_activity_logs(args: dict) -> dict:
    limit = min(max(int(args.get("limit", 25)), 1), 50)
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT user_id, user_agent AS detail, timestamp, 'vpn' AS type
            FROM subscription_usage_logs
            UNION ALL
            SELECT user_id, action AS detail, timestamp, 'miniapp' AS type
            FROM miniapp_usage_logs
            ORDER BY timestamp DESC LIMIT {limit}
            """
        )
    return {"logs": [dict(r) for r in rows]}


async def _list_esim_beta_requests(args: dict) -> dict:
    st = (args.get("status") or "pending").lower()
    async with get_connection() as conn:
        if st == "all":
            rows = await conn.fetch(
                "SELECT id, user_id, email, status, created_at FROM esim_beta_requests "
                "ORDER BY created_at DESC LIMIT 30"
            )
        else:
            rows = await conn.fetch(
                "SELECT id, user_id, email, status, created_at FROM esim_beta_requests "
                "WHERE status=$1 ORDER BY created_at DESC LIMIT 30",
                st,
            )
    return {"requests": [dict(r) for r in rows]}


# ---------- Write handlers ----------


async def _write_user_subscription_days(uid: int, days: int, *, from_today: bool) -> None:
    if from_today:
        await set_new_subscription_days(uid, days)
        return
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE users SET pay_subscribed = TRUE,
                subscription_end = GREATEST(COALESCE(subscription_end, NOW()), NOW())
                    + ($2 || ' days')::INTERVAL
            WHERE user_id = $1
            """,
            uid,
            str(days),
        )
        await conn.execute("DELETE FROM subscription_reminders WHERE user_id=$1", uid)
        await apply_subscription_anchor_on_payment(conn, uid)


async def _exec_write(name: str, args: dict) -> dict:
    uid = int(args["user_id"]) if "user_id" in args else None

    if name == "extend_user_subscription":
        months = min(max(int(args["months"]), 1), 12)
        await extend_subscription(int(args["user_id"]), months)
        return await _ok_write(f"+{months} мес.", int(args["user_id"]))

    if name == "extend_user_subscription_days":
        days = min(max(int(args["days"]), 1), 365)
        await _write_user_subscription_days(int(args["user_id"]), days, from_today=False)
        return await _ok_write(f"+{days} дн.", int(args["user_id"]))

    if name == "grant_subscription_days":
        days = min(max(int(args["days"]), 1), 365)
        await _write_user_subscription_days(int(args["user_id"]), days, from_today=True)
        return await _ok_write(f"подписка {days} дн. с сегодня", int(args["user_id"]))

    if name == "set_subscription_tier":
        tier = str(args["tier"]).strip().lower()
        if tier not in (*PAID_TIER_IDS, FREE_TIER_ID, "legacy"):
            return {"error": f"Неверный tier: {tier}"}
        uid = int(args["user_id"])
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE users SET subscription_tier=$2,
                    bypass_traffic_limit_gb=$3, device_limit=$4
                WHERE user_id=$1
                """,
                uid,
                tier,
                get_tier_bypass_gb(tier),
                get_tier_max_devices(tier),
            )
        return await _ok_write(f"tier={tier}", uid)

    if name == "grant_free_tier":
        uid = int(args["user_id"])
        async with get_connection() as conn:
            await grant_free_tier(conn, uid)
        return await _ok_write("free tier", uid)

    if name == "set_device_limit":
        uid = int(args["user_id"])
        lim = min(max(int(args["device_limit"]), 1), 20)
        async with get_connection() as conn:
            await conn.execute("UPDATE users SET device_limit=$2 WHERE user_id=$1", uid, lim)
        return await _ok_write(f"device_limit={lim}", uid)

    if name == "reset_user_trial":
        uid = int(args["user_id"])
        async with get_connection() as conn:
            await conn.execute("UPDATE users SET trial_used=FALSE WHERE user_id=$1", uid)
        return await _ok_write("trial_used=false", uid)

    if name == "sync_user_vpn_keys":
        await sync_user_keys(int(args["user_id"]))
        return await _ok_write("ключи синхронизированы", int(args["user_id"]))

    if name == "provision_user_keys":
        await create_or_activate_keys_for_all_servers(int(args["user_id"]))
        return await _ok_write("ключи созданы на серверах", int(args["user_id"]))

    if name == "set_user_blacklist":
        uid = int(args["user_id"])
        bl = bool(args["blacklisted"])
        async with get_connection() as conn:
            await conn.execute("UPDATE users SET blacklisted=$2 WHERE user_id=$1", uid, bl)
        return await _ok_write(f"blacklisted={bl}", uid)

    if name == "set_bypass_traffic_limit_gb":
        uid, gb = int(args["user_id"]), min(max(int(args["limit_gb"]), 0), 10000)
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE users SET bypass_traffic_limit_gb=$2 WHERE user_id=$1", uid, gb
            )
        return await _ok_write(f"bypass_limit={gb}GB", uid)

    if name == "add_bypass_traffic_bonus_gb":
        uid, gb = int(args["user_id"]), min(max(int(args["bonus_gb"]), 0), 10000)
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE users SET bypass_bonus_gb=COALESCE(bypass_bonus_gb,0)+$2 WHERE user_id=$1",
                uid, gb,
            )
        return await _ok_write(f"+{gb}GB bypass bonus", uid)

    if name == "set_monthly_traffic_limit_gb":
        uid, gb = int(args["user_id"]), min(max(int(args["limit_gb"]), 0), 10000)
        async with get_connection() as conn:
            await conn.execute("UPDATE users SET traffic_limit_gb=$2 WHERE user_id=$1", uid, gb)
        return await _ok_write(f"monthly_limit={gb}GB", uid)

    if name == "add_monthly_traffic_bonus_gb":
        uid, gb = int(args["user_id"]), min(max(int(args["bonus_gb"]), 0), 10000)
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE users SET traffic_bonus_gb=COALESCE(traffic_bonus_gb,0)+$2 WHERE user_id=$1",
                uid, gb,
            )
        return await _ok_write(f"+{gb}GB monthly bonus", uid)

    if name == "adjust_user_balance":
        uid = int(args["user_id"])
        cents = int(args["amount_cents"])
        reason = str(args.get("reason") or "support_bot")
        async with get_connection() as conn:
            if cents > 0:
                new_bal = await credit_balance(conn, uid, cents, "admin_credit", reason)
            elif cents < 0:
                ok, new_bal = await debit_balance(conn, uid, -cents, "admin_debit", reason)
                if not ok:
                    return {"error": "Недостаточно средств на балансе"}
            else:
                new_bal = await get_balance(conn, uid)
        return {"ok": True, "balance_cents": new_bal, "action": f"balance {cents:+d} коп."}

    if name == "approve_withdrawal":
        rid = int(args["request_id"])
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT id, status FROM balance_withdrawal_requests WHERE id=$1", rid
            )
            if not row:
                return {"error": "Заявка не найдена"}
            if row["status"] != "pending":
                return {"error": f"Статус: {row['status']}"}
            await conn.execute(
                "UPDATE balance_withdrawal_requests SET status='approved' WHERE id=$1", rid
            )
        return {"ok": True, "action": f"withdrawal #{rid} approved"}

    if name == "reject_withdrawal":
        rid = int(args["request_id"])
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT id, user_id, amount_cents, status FROM balance_withdrawal_requests WHERE id=$1",
                rid,
            )
            if not row or row["status"] != "pending":
                return {"error": "Заявка не найдена или уже обработана"}
            await credit_balance(
                conn, row["user_id"], row["amount_cents"], "admin_credit", "withdrawal rejected"
            )
            await conn.execute(
                "UPDATE balance_withdrawal_requests SET status='rejected' WHERE id=$1", rid
            )
        return {"ok": True, "action": f"withdrawal #{rid} rejected, refunded"}

    if name == "toggle_server_active":
        sid, active = int(args["server_id"]), bool(args["active"])
        async with get_connection() as conn:
            await conn.execute("UPDATE servers SET is_active=$2 WHERE id=$1", sid, active)
            if not active:
                await conn.execute(
                    "UPDATE vpn_keys SET is_active=FALSE WHERE server_id=$1 AND is_active=TRUE", sid
                )
        return {"ok": True, "server_id": sid, "is_active": active}

    if name == "toggle_server_bypass":
        sid = int(args["server_id"])
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE servers SET is_bypass=$2 WHERE id=$1", sid, bool(args["is_bypass"])
            )
        return {"ok": True, "server_id": sid, "is_bypass": args["is_bypass"]}

    if name == "toggle_server_exclude_subscription":
        sid = int(args["server_id"])
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE servers SET exclude_from_subscription=$2 WHERE id=$1",
                sid, bool(args["exclude"]),
            )
        return {"ok": True, "server_id": sid, "exclude": args["exclude"]}

    if name == "set_global_traffic_default_gb":
        gb = min(max(int(args["default_gb"]), 1), 10000)
        async with get_connection() as conn:
            row = await conn.fetchrow("SELECT id FROM traffic_settings ORDER BY id LIMIT 1")
            if row:
                await conn.execute(
                    "UPDATE traffic_settings SET default_monthly_gb=$1, updated_at=NOW() WHERE id=$2",
                    gb, row["id"],
                )
            else:
                await conn.execute(
                    "INSERT INTO traffic_settings (default_monthly_gb, panel_sync_min_seconds) VALUES ($1, 300)",
                    gb,
                )
        return {"ok": True, "default_monthly_gb": gb}

    if name == "set_trial_days":
        days = min(max(int(args["days"]), 0), 90)
        async with get_connection() as conn:
            ex = await conn.fetchrow("SELECT id FROM trial_settings ORDER BY id DESC LIMIT 1")
            if ex:
                await conn.execute(
                    "UPDATE trial_settings SET days=$1, updated_at=NOW() WHERE id=$2", days, ex["id"]
                )
            else:
                await conn.execute("INSERT INTO trial_settings (days) VALUES ($1)", days)
        return {"ok": True, "trial_days": days}

    if name == "set_referral_bonus_days":
        inv = min(max(int(args["inviter_days"]), 0), 365)
        invited = min(max(int(args["invited_days"]), 0), 365)
        async with get_connection() as conn:
            ex = await conn.fetchrow("SELECT id FROM referral_settings ORDER BY id DESC LIMIT 1")
            if ex:
                await conn.execute(
                    "UPDATE referral_settings SET inviter_bonus_days=$1, invited_bonus_days=$2 WHERE id=$3",
                    inv, invited, ex["id"],
                )
            else:
                await conn.execute(
                    "INSERT INTO referral_settings (inviter_bonus_days, invited_bonus_days) VALUES ($1,$2)",
                    inv, invited,
                )
        return {"ok": True, "inviter_days": inv, "invited_days": invited}

    if name == "set_tier_price":
        tier = str(args["tier"]).lower()
        months = int(args["duration_months"])
        async with get_connection() as conn:
            rub = args.get("price_rub")
            stars = args.get("price_stars")
            if rub is not None:
                await conn.execute(
                    """
                    INSERT INTO tier_price_settings (tier, duration_months, price_rub, updated_at)
                    VALUES ($1,$2,$3,NOW())
                    ON CONFLICT (tier, duration_months) DO UPDATE SET price_rub=$3, updated_at=NOW()
                    """,
                    tier, months, int(rub),
                )
            if stars is not None:
                await conn.execute(
                    """
                    INSERT INTO tier_price_settings (tier, duration_months, price_stars, updated_at)
                    VALUES ($1,$2,$3,NOW())
                    ON CONFLICT (tier, duration_months) DO UPDATE SET price_stars=$3, updated_at=NOW()
                    """,
                    tier, months, int(stars),
                )
        return {"ok": True, "tier": tier, "months": months}

    if name == "set_legacy_plan_price":
        plan_id = str(args["plan_id"])
        async with get_connection() as conn:
            if args.get("price_rub") is not None:
                await conn.execute(
                    """
                    INSERT INTO price_settings (plan_id, price_rub, updated_at)
                    VALUES ($1,$2,NOW())
                    ON CONFLICT (plan_id) DO UPDATE SET price_rub=$2, updated_at=NOW()
                    """,
                    plan_id, int(args["price_rub"]),
                )
            if args.get("price_stars") is not None:
                await conn.execute(
                    """
                    INSERT INTO price_settings (plan_id, price_stars, updated_at)
                    VALUES ($1,$2,NOW())
                    ON CONFLICT (plan_id) DO UPDATE SET price_stars=$2, updated_at=NOW()
                    """,
                    plan_id, int(args["price_stars"]),
                )
        return {"ok": True, "plan_id": plan_id}

    if name == "create_utm_campaign":
        tag = str(args["tag"]).strip().lower()[:64]
        desc = str(args.get("description") or "")
        bonus = int(args.get("bonus_days") or 0)
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO utm_campaigns (tag, description, bonus_days, is_active)
                VALUES ($1,$2,$3,TRUE)
                ON CONFLICT (tag) DO UPDATE SET description=$2, bonus_days=$3, is_active=TRUE
                """,
                tag, desc, bonus,
            )
        return {"ok": True, "tag": tag}

    if name == "toggle_utm_campaign":
        tag = str(args["tag"])
        active = bool(args["active"])
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE utm_campaigns SET is_active=$2 WHERE tag=$1", tag, active
            )
        return {"ok": True, "tag": tag, "active": active}

    if name == "upsert_support_manager":
        mid = int(args["user_id"])
        link = str(args["support_link"]).strip()
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO managers (user_id, support_link, is_active)
                VALUES ($1,$2,TRUE)
                ON CONFLICT (user_id) DO UPDATE SET support_link=$2, is_active=TRUE
                """,
                mid, link,
            )
        return {"ok": True, "manager_id": mid}

    if name == "deactivate_support_manager":
        mid = int(args["user_id"])
        async with get_connection() as conn:
            await conn.execute("UPDATE managers SET is_active=FALSE WHERE user_id=$1", mid)
        return {"ok": True, "manager_id": mid}

    if name == "set_announcement_text":
        text = str(args["text"])
        await set_announcement_text(text)
        return {"ok": True, "length": len(text)}

    if name == "resolve_esim_beta":
        rid = int(args["request_id"])
        approve = bool(args["approve"])
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT id, user_id, email, status FROM esim_beta_requests WHERE id=$1", rid
            )
            if not row:
                return {"error": "Заявка не найдена"}
            if row["status"] != "pending":
                return {"error": "Уже обработана"}
            st = "approved" if approve else "rejected"
            await conn.execute(
                """
                UPDATE esim_beta_requests
                SET status=$1, resolved_at=NOW() WHERE id=$2
                """,
                st, rid,
            )
            if approve:
                await conn.execute(
                    "UPDATE users SET esim_beta_access=TRUE WHERE user_id=$1", row["user_id"]
                )
                email = (row["email"] or "").strip().lower()
                if email:
                    await conn.execute(
                        """
                        INSERT INTO esim_beta_waitlist (user_id, email)
                        VALUES ($1,$2)
                        ON CONFLICT (user_id) DO UPDATE SET email=EXCLUDED.email
                        """,
                        row["user_id"], email,
                    )
        return {"ok": True, "request_id": rid, "status": st, "note": "Письмо юзеру — через основной /admin"}

    return {"error": f"unknown write tool: {name}"}


async def _get_notification_button_catalog(_args: dict) -> dict:
    from support_bot.user_notifications import list_button_catalog

    return list_button_catalog()


async def _get_user_payment_context_tool(args: dict) -> dict:
    from support_bot.promo_offers import get_user_payment_context

    return await get_user_payment_context(int(args["user_id"]))


READ_HANDLERS: dict[str, Callable[..., Awaitable[dict]]] = {
    "get_user_profile": _get_user_profile,
    "search_user": _search_user,
    "get_user_activity": _get_user_activity,
    "get_user_payments": _get_user_payments,
    "get_user_vpn_keys": _get_user_vpn_keys,
    "get_user_balance": _get_user_balance,
    "get_user_devices": _get_user_devices,
    "get_service_overview": _get_service_overview,
    "get_admin_stats": _get_admin_stats,
    "get_system_logs": _get_system_logs,
    "list_servers": _list_servers,
    "get_server_detail": _get_server_detail,
    "get_traffic_settings": _get_traffic_settings_tool,
    "get_pricing_catalog": _get_pricing_catalog,
    "get_bypass_packs": _get_bypass_packs,
    "get_gb_traffic_packs": _get_gb_traffic_packs,
    "get_referral_settings": _get_referral_settings,
    "get_trial_settings": _get_trial_settings,
    "get_discount_settings": _get_discount_settings,
    "get_announcement": _get_announcement,
    "list_support_managers": _list_support_managers,
    "list_utm_campaigns": _list_utm_campaigns,
    "list_pending_withdrawals": _list_pending_withdrawals,
    "get_global_activity_logs": _get_global_activity_logs,
    "list_esim_beta_requests": _list_esim_beta_requests,
    "get_notification_button_catalog": _get_notification_button_catalog,
    "get_user_payment_context": _get_user_payment_context_tool,
}


async def _send_user_notification_tool(args: dict) -> dict:
    from support_bot.user_notifications import send_user_notification

    bot = await _get_main_bot()
    return await send_user_notification(
        bot,
        int(args["user_id"]),
        str(args["text"]),
        buttons=args.get("buttons") or [],
    )


async def _create_personal_discount_offer_tool(args: dict) -> dict:
    from support_bot.promo_offers import create_personal_promo_offer

    return await create_personal_promo_offer(
        user_id=int(args["user_id"]),
        discount_percent=int(args["discount_percent"]),
        plan_id=args.get("plan_id"),
        tier=args.get("tier"),
        button_text=args.get("button_text"),
        note=args.get("note"),
        valid_hours=int(args.get("valid_hours", 72)),
    )


async def _send_discount_notification_tool(args: dict) -> dict:
    from support_bot.promo_offers import create_personal_promo_offer
    from support_bot.user_notifications import send_user_notification

    uid = int(args["user_id"])
    offer = await create_personal_promo_offer(
        user_id=uid,
        discount_percent=int(args["discount_percent"]),
        plan_id=args.get("plan_id"),
        tier=args.get("tier"),
        button_text=args.get("button_text"),
    )
    if offer.get("error"):
        return offer

    buttons = [
        {
            "type": "personal_promo",
            "offer_id": offer["offer_id"],
            "text": offer["button_text"],
        },
    ]
    buttons.extend(args.get("extra_buttons") or [])

    text = str(args["text"])
    ctx = offer.get("payment_context") or {}
    if ctx.get("recurring_warning"):
        text += f"\n\n<i>{ctx['recurring_warning']}</i>"

    bot = await _get_main_bot()
    sent = await send_user_notification(bot, uid, text, buttons=buttons)
    return {"offer": offer, "notification": sent}


NOTIFY_WRITE_TOOLS = frozenset({
    "send_user_notification",
    "create_personal_discount_offer",
    "send_discount_notification",
})


async def execute_vpn_tool(name: str, arguments: dict[str, Any], *, staff: bool) -> str:
    try:
        if name in NOTIFY_WRITE_TOOLS:
            if not staff:
                return _json({"error": "Недостаточно прав"})
            if name == "send_user_notification":
                result = await _send_user_notification_tool(arguments)
            elif name == "create_personal_discount_offer":
                result = await _create_personal_discount_offer_tool(arguments)
            else:
                result = await _send_discount_notification_tool(arguments)
        elif name in WRITE_TOOL_NAMES:
            if not staff:
                return _json({"error": "Недостаточно прав"})
            result = await _exec_write(name, arguments)
        elif name in READ_HANDLERS:
            result = await READ_HANDLERS[name](arguments)
        else:
            result = {"error": f"Неизвестный инструмент: {name}"}
    except Exception as e:
        logger.exception("vpn_tool %s failed", name)
        result = {"error": str(e)}
    return _json(result)
