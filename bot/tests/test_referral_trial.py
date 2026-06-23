from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from bot.trial_usage import (
    should_show_trial_in_main_menu,
    user_eligible_for_trial_offer,
    user_has_active_paid_subscription,
    user_has_referral_trial_source,
    user_show_referral_trial_offer,
)


def _conn(*, fetchrow=None, fetchval=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetchval = AsyncMock(side_effect=fetchval or [])
    return conn


@pytest.mark.asyncio
async def test_referral_source_by_invited_by():
    conn = _conn(fetchrow={"invited_by": 42, "utm_source": None}, fetchval=[])
    assert await user_has_referral_trial_source(conn, 1) is True


@pytest.mark.asyncio
async def test_referral_source_by_utm():
    conn = _conn(fetchrow={"invited_by": None, "utm_source": "boss_privetka2"}, fetchval=[])
    assert await user_has_referral_trial_source(conn, 1) is True


@pytest.mark.asyncio
async def test_no_referral_source_direct_user():
    conn = _conn(fetchrow={"invited_by": None, "utm_source": None}, fetchval=[None])
    assert await user_has_referral_trial_source(conn, 1) is False


@pytest.mark.asyncio
async def test_show_trial_only_with_referral(monkeypatch):
    async def fake_eligible(conn, user_id):
        return True

    monkeypatch.setattr("bot.trial_usage.user_eligible_for_trial_offer", fake_eligible)

    conn_ref = _conn(fetchrow={"invited_by": 1, "utm_source": None}, fetchval=[])
    assert await user_show_referral_trial_offer(conn_ref, 5) is True

    conn_direct = _conn(fetchrow={"invited_by": None, "utm_source": None}, fetchval=[None])
    assert await user_show_referral_trial_offer(conn_direct, 5) is False


@pytest.mark.asyncio
async def test_main_menu_hidden_during_active_plus(monkeypatch):
    async def fake_show(conn, user_id):
        return True

    monkeypatch.setattr("bot.trial_usage.user_show_referral_trial_offer", fake_show)

    conn = _conn(
        fetchrow={
            "subscription_tier": "plus",
            "pay_subscribed": True,
            "subscription_end": datetime.utcnow() + timedelta(days=14),
        }
    )
    assert await user_has_active_paid_subscription(conn, 1) is True
    assert await should_show_trial_in_main_menu(conn, 1) is False


@pytest.mark.asyncio
async def test_main_menu_shown_after_plus_expired(monkeypatch):
    async def fake_show(conn, user_id):
        return True

    monkeypatch.setattr("bot.trial_usage.user_show_referral_trial_offer", fake_show)

    conn = _conn(
        fetchrow={
            "subscription_tier": "plus",
            "pay_subscribed": True,
            "subscription_end": datetime(2020, 1, 1),
        }
    )
    assert await user_has_active_paid_subscription(conn, 1) is False
    assert await should_show_trial_in_main_menu(conn, 1) is True


@pytest.mark.asyncio
async def test_eligible_without_referral_still_false_for_show(monkeypatch):
    async def fake_trial_days(conn):
        return 7

    monkeypatch.setattr("bot.trial_usage.get_trial_days", fake_trial_days)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "subscription_tier": "free",
            "pay_subscribed": True,
            "subscription_end": datetime(2099, 12, 31),
        }
    )
    conn.fetchval = AsyncMock(return_value=None)

    assert await user_eligible_for_trial_offer(conn, 1) is True

    conn2 = _conn(fetchrow={"invited_by": None, "utm_source": None}, fetchval=[None])
    assert await user_show_referral_trial_offer(conn2, 1) is False
