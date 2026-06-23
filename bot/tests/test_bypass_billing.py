"""Bypass billing: pack-first consumption and monthly rollover carryover."""
from datetime import date

import pytest

from bot.traffic import (
    BYTES_PER_GB,
    apply_bypass_usage_delta,
    compute_billing_period,
    compute_pack_carryover_gb,
    split_bypass_consumption,
)


def test_compute_billing_period_stable_within_month():
    anchor = 8
    start_may, end_may = compute_billing_period(date(2026, 5, 15), anchor)
    start_may2, end_may2 = compute_billing_period(date(2026, 5, 28), anchor)
    assert start_may == date(2026, 5, 8)
    assert start_may2 == date(2026, 5, 8)
    assert end_may == end_may2 == date(2026, 6, 8)


def test_compute_billing_period_advances_on_anchor_day():
    start, end = compute_billing_period(date(2026, 6, 8), 8)
    assert start == date(2026, 6, 8)
    assert end == date(2026, 7, 8)


def test_pack_delta_consumed_before_base():
    """После покупки 10 ГБ новый трафик списывается с пакета."""
    pack_remaining = 10
    pack_remaining = apply_bypass_usage_delta(20 * BYTES_PER_GB, 25 * BYTES_PER_GB, pack_remaining)
    assert pack_remaining == 5


def test_user_scenario_remaining_before_renewal():
    """50 base, +10 pack, 20 base used then +5 → 35 GB left (5 pack + 30 base)."""
    pack_remaining = apply_bypass_usage_delta(20 * BYTES_PER_GB, 25 * BYTES_PER_GB, 10)
    split = split_bypass_consumption(
        25 * BYTES_PER_GB, 50, pack_remaining, pack_purchased_gb=10
    )
    pack_left = split["packRemainingBytes"] / BYTES_PER_GB
    base_left = split["baseRemainingBytes"] / BYTES_PER_GB
    assert pack_left == 5.0
    assert base_left == 30.0
    assert pack_left + base_left == 35.0


def test_user_scenario_carryover_after_renewal():
    """После продления: 50 base + 5 carry pack = 55."""
    carry = compute_pack_carryover_gb(5)
    assert carry == 5
    assert 50 + carry == 55


def test_user_scenario_second_month_exactly_base():
    """55 лимит, потратили 35 → 20 осталось. Перенос 0 → ровно 50."""
    pack_remaining = apply_bypass_usage_delta(0, 35 * BYTES_PER_GB, 5)
    assert pack_remaining == 0
    carry = compute_pack_carryover_gb(pack_remaining)
    assert carry == 0
    split = split_bypass_consumption(
        35 * BYTES_PER_GB, 50, pack_remaining, pack_purchased_gb=5
    )
    assert split["baseRemainingBytes"] == 20 * BYTES_PER_GB


@pytest.mark.parametrize(
    "today,anchor,expected_start",
    [
        (date(2026, 3, 1), 31, date(2026, 2, 28)),
        (date(2026, 3, 31), 31, date(2026, 3, 31)),
        (date(2026, 4, 1), 31, date(2026, 3, 31)),
    ],
)
def test_anchor_day_31_february(today, anchor, expected_start):
    start, _ = compute_billing_period(today, anchor)
    assert start == expected_start
