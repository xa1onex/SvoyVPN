"""Тесты sentinel-даты подписки (Free 2099, покупка Plus)."""

from datetime import date, datetime

from bot.plans import (
    FREE_SUBSCRIPTION_END,
    FREE_TIER_ID,
    format_subscription_end_for_display,
    is_sentinel_subscription_end,
    should_reset_subscription_period_on_purchase,
)


def test_free_end_is_sentinel():
    assert is_sentinel_subscription_end(FREE_SUBSCRIPTION_END)
    assert is_sentinel_subscription_end(datetime(2099, 12, 31))
    assert is_sentinel_subscription_end(date(2100, 1, 31))


def test_real_paid_end_not_sentinel():
    assert not is_sentinel_subscription_end(date(2026, 6, 15))
    assert not is_sentinel_subscription_end(datetime(2027, 1, 1))


def test_purchase_from_free_resets_period():
    assert should_reset_subscription_period_on_purchase(
        pay_subscribed=True,
        subscription_end=FREE_SUBSCRIPTION_END,
        subscription_tier=FREE_TIER_ID,
    )


def test_purchase_extends_real_paid_period():
    end = date(2026, 8, 1)
    assert not should_reset_subscription_period_on_purchase(
        pay_subscribed=True,
        subscription_end=end,
        subscription_tier="plus",
    )


def test_purchase_after_sentinel_paid_resets():
    assert should_reset_subscription_period_on_purchase(
        pay_subscribed=True,
        subscription_end=date(2100, 1, 31),
        subscription_tier="plus",
    )


def test_format_display_hides_sentinel():
    assert format_subscription_end_for_display(FREE_SUBSCRIPTION_END) is None
    assert format_subscription_end_for_display(date(2026, 3, 1)) == "01.03.2026"
