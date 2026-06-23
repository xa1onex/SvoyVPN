from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from bot.trial_usage import (
    TRIAL_PAYMENT_AMOUNT_KOPECKS,
    TRIAL_PAYMENT_PLAN_ID,
    TRIAL_RETRY_LAPSE_DAYS,
    can_retry_trial_after_lapse,
    user_eligible_for_trial_offer,
)


def _conn(fetchrow=None, fetchval=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetchval = AsyncMock(side_effect=fetchval or [])
    return conn


@pytest.mark.asyncio
async def test_recent_trial_payment_not_eligible(monkeypatch):
    async def fake_trial_days(conn):
        return 7

    monkeypatch.setattr("bot.trial_usage.get_trial_days", fake_trial_days)

    paid_at = datetime.utcnow() - timedelta(days=2)
    calls = {"n": 0}

    async def fetchval(sql, *args):
        calls["n"] += 1
        if "trial_settings" in sql or "days" in sql:
            return 7
        if "EXISTS" in sql and TRIAL_PAYMENT_PLAN_ID in args:
            return True
        if "MAX(timestamp)" in sql and TRIAL_PAYMENT_AMOUNT_KOPECKS in args:
            return paid_at
        return None

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "subscription_tier": "free",
            "pay_subscribed": True,
            "subscription_end": datetime(2099, 12, 31),
            "yookassa_recurring_payment_method_id": None,
            "last_plus_ended_at": None,
        }
    )
    conn.fetchval = AsyncMock(side_effect=fetchval)

    assert await user_eligible_for_trial_offer(conn, 1) is False
    assert await can_retry_trial_after_lapse(conn, 1) is False


@pytest.mark.asyncio
async def test_lapsed_trial_eligible_for_retry(monkeypatch):
    async def fake_trial_days(conn):
        return 7

    monkeypatch.setattr("bot.trial_usage.get_trial_days", fake_trial_days)

    ended = datetime.utcnow() - timedelta(days=TRIAL_RETRY_LAPSE_DAYS + 5)

    async def fetchval(sql, *args):
        if "EXISTS" in sql:
            return True
        if "MAX(timestamp)" in sql and TRIAL_PAYMENT_AMOUNT_KOPECKS in args:
            return ended - timedelta(days=30)
        return None

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "subscription_tier": "free",
            "pay_subscribed": True,
            "subscription_end": datetime(2099, 12, 31),
            "yookassa_recurring_payment_method_id": None,
            "last_plus_ended_at": ended,
        }
    )
    conn.fetchval = AsyncMock(side_effect=fetchval)

    assert await can_retry_trial_after_lapse(conn, 2) is True
    assert await user_eligible_for_trial_offer(conn, 2) is True
