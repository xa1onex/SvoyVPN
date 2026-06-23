"""
Учёт пробного периода «Plus за 1₽».

trial_used синхронизируется с фактом оплаты 1₽, но после долгого простоя
без карты предложение можно показать снова (can_retry_trial_after_lapse).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import asyncpg

# Пробный платёж: 1₽, plan plus_1m.
TRIAL_PAYMENT_AMOUNT_KOPECKS = 100
TRIAL_PAYMENT_PLAN_ID = "plus_1m"
TRIAL_RETRY_LAPSE_DAYS = 30


async def user_has_referral_trial_source(conn: asyncpg.Connection, user_id: int) -> bool:
    """
    Пользователь пришёл по реферальной или UTM-ссылке (в т.ч. старые визиты в utm_visits).
    """
    row = await conn.fetchrow(
        "SELECT invited_by, utm_source FROM users WHERE user_id = $1",
        user_id,
    )
    if row:
        if row.get("invited_by"):
            return True
        if str(row.get("utm_source") or "").strip():
            return True
    has_utm = await conn.fetchval(
        "SELECT 1 FROM utm_visits WHERE user_id = $1 LIMIT 1",
        user_id,
    )
    return bool(has_utm)


async def user_show_referral_trial_offer(conn: asyncpg.Connection, user_id: int) -> bool:
    """Plus за 1₽ — только реферал/UTM и пока триал не исчерпан."""
    if not await user_has_referral_trial_source(conn, user_id):
        return False
    return await user_eligible_for_trial_offer(conn, user_id)


async def user_has_active_paid_subscription(conn: asyncpg.Connection, user_id: int) -> bool:
    """Активная платная подписка (Plus и legacy) с реальной датой окончания."""
    from .plans import (
        ALL_PAID_TIER_IDS,
        FREE_TIER_ID,
        is_sentinel_subscription_end,
        is_subscription_active,
    )

    row = await conn.fetchrow(
        """
        SELECT subscription_tier, pay_subscribed, subscription_end
        FROM users WHERE user_id = $1
        """,
        user_id,
    )
    if not row:
        return False
    tier = (row["subscription_tier"] or FREE_TIER_ID).strip() or FREE_TIER_ID
    if tier not in ALL_PAID_TIER_IDS:
        return False
    sub_end = row["subscription_end"]
    if is_sentinel_subscription_end(sub_end):
        return False
    return is_subscription_active(row["pay_subscribed"], sub_end)


async def should_show_trial_in_main_menu(conn: asyncpg.Connection, user_id: int) -> bool:
    """
    Кнопка «Plus за 1₽» в главном меню: реферал/UTM, триал доступен,
    но нет активной платной подписки (подарок/UTM/Plus).
    """
    if await user_has_active_paid_subscription(conn, user_id):
        return False
    return await user_show_referral_trial_offer(conn, user_id)


async def get_trial_days(conn: asyncpg.Connection) -> int:
    row = await conn.fetchrow(
        "SELECT days FROM trial_settings ORDER BY id DESC LIMIT 1"
    )
    try:
        return max(0, int(row["days"])) if row and row["days"] is not None else 0
    except (TypeError, ValueError):
        return 0


async def has_completed_trial_payment(
    conn: asyncpg.Connection,
    user_id: int,
) -> bool:
    """Был ли у пользователя успешный платёж пробного периода (1₽)."""
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM payments
                WHERE user_id = $1
                  AND status = 'completed'
                  AND currency = 'RUB'
                  AND plan_id = $2
                  AND amount = $3
            )
            """,
            user_id,
            TRIAL_PAYMENT_PLAN_ID,
            TRIAL_PAYMENT_AMOUNT_KOPECKS,
        )
    )


def _as_utc_naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


async def can_retry_trial_after_lapse(
    conn: asyncpg.Connection,
    user_id: int,
    *,
    lapse_days: int = TRIAL_RETRY_LAPSE_DAYS,
) -> bool:
    """
    Можно снова предложить Plus за 1₽: прошло N дней с окончания Plus
    (триал, покупка, реферал/UTM — без разницы), карта не привязана.
    """
    from .plans import (
        ALL_PAID_TIER_IDS,
        FREE_TIER_ID,
        is_sentinel_subscription_end,
        is_subscription_active,
    )

    row = await conn.fetchrow(
        """
        SELECT subscription_tier, pay_subscribed, subscription_end,
               yookassa_recurring_payment_method_id, last_plus_ended_at
        FROM users WHERE user_id = $1
        """,
        user_id,
    )
    if not row:
        return False

    if row["yookassa_recurring_payment_method_id"]:
        return False

    tier = (row["subscription_tier"] or FREE_TIER_ID).strip() or FREE_TIER_ID
    sub_end = _as_utc_naive(row["subscription_end"])
    cutoff = datetime.utcnow() - timedelta(days=lapse_days)

    if (
        tier in ALL_PAID_TIER_IDS
        and is_subscription_active(row["pay_subscribed"], sub_end)
        and not is_sentinel_subscription_end(sub_end)
    ):
        return False

    last_ended = _as_utc_naive(row.get("last_plus_ended_at"))
    if last_ended and last_ended <= cutoff:
        return True

    if sub_end and not is_sentinel_subscription_end(sub_end) and sub_end <= cutoff:
        return True

    trial_paid_at = await conn.fetchval(
        """
        SELECT MAX(timestamp) FROM payments
        WHERE user_id = $1
          AND status = 'completed'
          AND currency = 'RUB'
          AND plan_id = $2
          AND amount = $3
        """,
        user_id,
        TRIAL_PAYMENT_PLAN_ID,
        TRIAL_PAYMENT_AMOUNT_KOPECKS,
    )
    trial_paid_at = _as_utc_naive(trial_paid_at)
    if trial_paid_at and trial_paid_at <= cutoff:
        on_free = tier == FREE_TIER_ID or is_sentinel_subscription_end(sub_end)
        expired_plus = (
            tier in ALL_PAID_TIER_IDS
            and sub_end
            and not is_sentinel_subscription_end(sub_end)
            and not is_subscription_active(row["pay_subscribed"], sub_end)
        )
        if on_free or expired_plus:
            return True

    any_paid_at = await conn.fetchval(
        """
        SELECT MAX(timestamp) FROM payments
        WHERE user_id = $1
          AND status = 'completed'
          AND currency = 'RUB'
          AND amount > $2
        """,
        user_id,
        TRIAL_PAYMENT_AMOUNT_KOPECKS,
    )
    any_paid_at = _as_utc_naive(any_paid_at)
    if any_paid_at and any_paid_at <= cutoff:
        on_free = tier == FREE_TIER_ID or is_sentinel_subscription_end(sub_end)
        if on_free:
            return True

    return False


async def user_eligible_for_trial_offer(conn: asyncpg.Connection, user_id: int) -> bool:
    """
    Можно ли показать/активировать «Plus за 1₽».
    """
    if await get_trial_days(conn) <= 0:
        return False

    if await has_completed_trial_payment(conn, user_id):
        return await can_retry_trial_after_lapse(conn, user_id)

    from .plans import (
        ALL_PAID_TIER_IDS,
        FREE_TIER_ID,
        is_sentinel_subscription_end,
        is_subscription_active,
    )

    row = await conn.fetchrow(
        """
        SELECT subscription_tier, pay_subscribed, subscription_end
        FROM users WHERE user_id = $1
        """,
        user_id,
    )
    if not row:
        return True

    tier = (row["subscription_tier"] or FREE_TIER_ID).strip() or FREE_TIER_ID
    sub_end = row["subscription_end"]
    if tier not in ALL_PAID_TIER_IDS:
        return True
    if not is_subscription_active(row["pay_subscribed"], sub_end):
        return True
    if is_sentinel_subscription_end(sub_end):
        return True
    return False


async def sync_trial_used_flag(conn: asyncpg.Connection, user_id: int) -> bool:
    """
    trial_used = оплатил 1₽ и сейчас НЕ в окне повторного предложения.
    """
    paid = await has_completed_trial_payment(conn, user_id)
    used = paid and not await can_retry_trial_after_lapse(conn, user_id)
    await conn.execute(
        "UPDATE users SET trial_used = $1 WHERE user_id = $2",
        used,
        user_id,
    )
    return used


async def trial_status_for_admin(conn: asyncpg.Connection, user_id: int) -> str:
    """Текст для карточки пользователя в админке."""
    if not await has_completed_trial_payment(conn, user_id):
        return "❌ Нет"
    if await user_eligible_for_trial_offer(conn, user_id):
        return "🔄 Доступен повторно (1₽)"
    return "✅ Использовал"


def referral_trial_offer_text(trial_days: int) -> str:
    """Текст экрана «Plus за 1₽» для реферальных/UTM пользователей."""
    return (
        f"🎁 <b>Plus за 1₽</b>\n\n"
        f"Период: <b>{trial_days} дней</b>\n"
        f"Стоимость сейчас: <b>1₽</b>\n\n"
        f"· Безлимит устройств\n"
        f"· 50 ГБ bypass-трафика в месяц\n"
        f"· Безлимит на быстрые сервера\n"
        f"· YouTube / TikTok / ChatGPT\n\n"
        f"Привяжите карту — после пробного периода подписка продлится автоматически "
        f"по актуальной цене Plus.\n\n"
        f"<i>Plus активируется после оплаты — обновите подписку в Happ (🔄) "
        f"или через бота → «🔗 Подключить VPN»</i>"
    )
