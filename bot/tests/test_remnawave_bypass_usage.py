from datetime import date

from bot.traffic_worker import _sum_node_usage_by_period


def test_node_usage_is_assigned_to_the_matching_user_period_only():
    records = [
        {"userUuid": "a-a", "date": "2026-07-14T00:00:00.000Z", "total": 100},
        {"userUuid": "a-a", "date": "2026-07-15T00:00:00.000Z", "total": 200},
        {"userUuid": "b-b", "date": "2026-07-15T00:00:00.000Z", "total": 300},
        # Outside both periods: it must not leak into bypass accounting.
        {"userUuid": "a-a", "date": "2026-07-13T00:00:00.000Z", "total": 999},
    ]

    usage = _sum_node_usage_by_period(
        records,
        {
            1: (date(2026, 7, 14), date(2026, 7, 16)),
            2: (date(2026, 7, 15), date(2026, 7, 16)),
        },
        {"aa": 1, "bb": 2},
    )

    assert usage == {1: 300, 2: 300}


def test_unknown_uuid_is_not_billed_to_any_user():
    usage = _sum_node_usage_by_period(
        [{"userUuid": "unknown", "date": "2026-07-15", "total": 123}],
        {1: (date(2026, 7, 14), date(2026, 7, 16))},
        {"known": 1},
    )

    assert usage == {}
