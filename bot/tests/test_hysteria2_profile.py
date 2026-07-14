"""Tests for YouTube ad-free static exit → Happ profile."""

from bot.profile_generator import (
    _build_single_server_config,
    generate_happ_configs_list,
    generate_xray_profile,
    parse_vless_link,
)
from bot.static_servers import (
    YOUTUBE_ADFREE_HOST,
    YOUTUBE_ADFREE_UUID,
    youtube_adfree_link,
)


def test_parse_youtube_vless_link():
    p = parse_vless_link(youtube_adfree_link())
    assert p is not None
    assert p["protocol"] == "vless"
    assert p["address"] == YOUTUBE_ADFREE_HOST
    assert p["port"] == 443
    assert p["uuid"] == YOUTUBE_ADFREE_UUID
    assert p["security"] == "tls"
    assert p["flow"] == "xtls-rprx-vision"
    assert p["sni"] == "j5orbbxwns.medved.app"
    assert p["fp"] == "firefox"


def test_youtube_single_config_uses_minimal_routing():
    p = parse_vless_link(youtube_adfree_link())
    cfg = _build_single_server_config(
        p, remarks="🇷🇺 Россия | YouTube без рекламы", description="для RU сервисов и YouTube"
    )
    proxy = cfg["outbounds"][0]
    assert proxy["protocol"] == "vless"
    assert proxy["settings"]["vnext"][0]["address"] == YOUTUBE_ADFREE_HOST
    assert proxy["settings"]["vnext"][0]["users"][0]["flow"] == "xtls-rprx-vision"
    assert proxy["streamSettings"]["security"] == "tls"
    assert proxy["streamSettings"]["tlsSettings"]["serverName"] == "j5orbbxwns.medved.app"
    assert proxy["streamSettings"]["tlsSettings"]["fingerprint"] == "firefox"
    # Не уводить RU/YouTube в direct
    assert cfg["routing"]["rules"] == [
        {"outboundTag": "direct", "protocol": ["bittorrent"], "type": "field"}
    ]
    assert "youtube-rf" not in [o.get("tag") for o in cfg["outbounds"]]


def test_youtube_not_in_autoselect_pool(monkeypatch):
    monkeypatch.setenv("SVOYVPN_YOUTUBE_RF_ENABLED", "1")
    import bot.profile_generator as pg
    pg._youtube_rf_outbound_cache = None
    vless = (
        "vless://a1b2c3d4-e5f6-7890-abcd-ef1234567890@1.2.3.4:443"
        "?type=tcp&security=reality&pbk=abc&sid=01&sni=example.com&fp=chrome#DE"
    )
    configs = generate_happ_configs_list(
        [vless, youtube_adfree_link()],
        ["🇩🇪 Germany", "🇷🇺 Россия | YouTube без рекламы"],
        server_is_bypass=[False, False],
    )
    yt = [
        c for c in configs
        if c.get("outbounds")
        and c["outbounds"][0].get("protocol") == "vless"
        and c["outbounds"][0].get("settings", {}).get("vnext", [{}])[0].get("address") == YOUTUBE_ADFREE_HOST
        and c["outbounds"][0].get("tag") == "proxy"
    ]
    assert len(yt) == 1
    # автовыбор не должен держать youtube exit как proxy/smart
    auto = next(c for c in configs if "Автовыбор" in str(c.get("remarks") or "") or "💫" in str(c.get("remarks") or ""))
    # youtube-rf outbound в автовыборе ожидаем; proxy/smart — только обычные ноды
    smart_addrs = []
    for o in auto.get("outbounds", []):
        if o.get("protocol") == "vless" and o.get("tag") != "youtube-rf":
            smart_addrs.append(o["settings"]["vnext"][0]["address"])
    assert YOUTUBE_ADFREE_HOST not in smart_addrs
    assert any(o.get("tag") == "youtube-rf" for o in auto.get("outbounds", []))


def test_vless_routes_youtube_through_rf_when_enabled(monkeypatch):
    monkeypatch.setenv("SVOYVPN_YOUTUBE_RF_ENABLED", "1")
    import bot.profile_generator as pg
    pg._youtube_rf_outbound_cache = None
    vless = (
        "vless://a1b2c3d4-e5f6-7890-abcd-ef1234567890@1.2.3.4:443"
        "?type=tcp&security=reality&pbk=abc&sid=01&sni=example.com&fp=chrome#DE"
    )
    cfg = _build_single_server_config(parse_vless_link(vless), remarks="DE", description="")
    rf = next(o for o in cfg["outbounds"] if o.get("tag") == "youtube-rf")
    assert rf["protocol"] == "vless"
    assert rf["settings"]["vnext"][0]["address"] == YOUTUBE_ADFREE_HOST
    assert cfg["routing"]["rules"][0]["outboundTag"] == "youtube-rf"


def test_vless_does_not_route_youtube_when_rf_disabled(monkeypatch):
    monkeypatch.setenv("SVOYVPN_YOUTUBE_RF_ENABLED", "0")
    import bot.profile_generator as pg
    pg._youtube_rf_outbound_cache = None
    vless = (
        "vless://a1b2c3d4-e5f6-7890-abcd-ef1234567890@1.2.3.4:443"
        "?type=tcp&security=reality&pbk=abc&sid=01&sni=example.com&fp=chrome#DE"
    )
    cfg = _build_single_server_config(parse_vless_link(vless), remarks="DE", description="")
    assert "youtube-rf" not in [o.get("tag") for o in cfg["outbounds"]]
