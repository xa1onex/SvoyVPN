"""Тесты выбора серверов для тарифа Free."""

from bot.free_tier_servers import (
    _name_transport_score,
    _pick_from_server_rows,
    filter_subscription_keys,
)
from bot.traffic import subscription_row_is_bypass


def test_name_transport_score_prefers_grpc_reality():
    assert _name_transport_score("NL grpc reality") > _name_transport_score("DE tcp")
    assert _name_transport_score("grpc") > _name_transport_score("plain")


def test_filter_subscription_keys():
    keys = [
        {"server_id": 1, "vless_link": "a"},
        {"server_id": 2, "vless_link": "b"},
        {"server_id": 3, "vless_link": "c"},
    ]
    assert len(filter_subscription_keys(keys, {1, 3})) == 2
    assert len(filter_subscription_keys(keys, None)) == 3
    assert filter_subscription_keys(keys, set()) == []


def test_filter_subscription_keys_includes_system_servers():
    """Free: 2 закреплённых + системные (например id 99)."""
    keys = [
        {"server_id": 10, "vless_link": "vpn"},
        {"server_id": 20, "vless_link": "bypass"},
        {"server_id": 99, "vless_link": "sys"},
        {"server_id": 50, "vless_link": "other"},
    ]
    allowed = {10, 20, 99}
    filtered = filter_subscription_keys(keys, allowed)
    assert {k["server_id"] for k in filtered} == {10, 20, 99}


def test_pick_bypass_falls_back_to_free_emoji_without_db_flag():
    rows = [
        {"id": 1, "name": "🚀 Быстрые сервера 👇", "display_order": 1, "is_bypass": False},
        {"id": 2, "name": "🆓 - обход белых списков", "display_order": 2, "is_bypass": True},
        {"id": 3, "name": "🆓 NL bypass", "display_order": 3, "is_bypass": False},
        {"id": 4, "name": "DE vpn", "display_order": 4, "is_bypass": False},
    ]
    assert _pick_from_server_rows(rows, is_bypass=True) == 3
    assert _pick_from_server_rows(rows, is_bypass=False) == 4


def test_subscription_row_is_bypass():
    assert subscription_row_is_bypass("🆓 NL", False) is True
    assert subscription_row_is_bypass("🆓 обход белых", False) is False
    assert subscription_row_is_bypass("DE", True) is True
