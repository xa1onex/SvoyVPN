"""Tests for Hysteria2 static server → Happ profile."""

from bot.profile_generator import (
    _build_single_server_config,
    generate_happ_configs_list,
    generate_xray_profile,
    parse_hysteria2_link,
    parse_vless_link,
)
from bot.static_servers import (
    YOUTUBE_ADFREE_AUTH,
    YOUTUBE_ADFREE_HOST,
    youtube_adfree_link,
)


def test_parse_youtube_hy2_link():
    link = youtube_adfree_link()
    p = parse_hysteria2_link(link)
    assert p is not None
    assert p["protocol"] == "hysteria2"
    assert p["address"] == YOUTUBE_ADFREE_HOST
    assert p["port"] == 443
    assert p["auth"] == YOUTUBE_ADFREE_AUTH
    assert p["alpn"] == ["h3"]
    assert "YouTube" in p["remark"]


def test_hy2_single_config_matches_happ_shape():
    p = parse_hysteria2_link(youtube_adfree_link())
    cfg = _build_single_server_config(
        p, remarks="🇷🇺 YouTube без рекламы", description="YouTube без рекламы"
    )
    proxy = cfg["outbounds"][0]
    assert proxy["protocol"] == "hysteria"
    assert proxy["settings"]["address"] == YOUTUBE_ADFREE_HOST
    ss = proxy["streamSettings"]
    assert ss["hysteriaSettings"]["auth"] == YOUTUBE_ADFREE_AUTH
    assert cfg["routing"]["rules"] == [
        {"outboundTag": "direct", "protocol": ["bittorrent"], "type": "field"}
    ]


def test_hy2_not_in_autoselect_pool(monkeypatch):
    monkeypatch.setenv("SVOYVPN_YOUTUBE_RF_ENABLED", "1")
    import bot.profile_generator as pg
    pg._youtube_rf_outbound_cache = None
    vless = (
        "vless://a1b2c3d4-e5f6-7890-abcd-ef1234567890@1.2.3.4:443"
        "?type=tcp&security=reality&pbk=abc&sid=01&sni=example.com&fp=chrome#DE"
    )
    configs = generate_happ_configs_list(
        [vless, youtube_adfree_link()],
        ["🇩🇪 Germany", "🇷🇺 YouTube без рекламы"],
        server_is_bypass=[False, False],
    )
    hy2_cfgs = [
        c for c in configs
        if c.get("outbounds") and c["outbounds"][0].get("protocol") == "hysteria"
        and c["outbounds"][0].get("tag") == "proxy"
    ]
    assert len(hy2_cfgs) == 1
    for c in configs:
        if any(o.get("protocol") == "vless" for o in c.get("outbounds", [])):
            assert "youtube-rf" in [o.get("tag") for o in c["outbounds"]]


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
    assert rf["settings"]["address"] == YOUTUBE_ADFREE_HOST
    assert cfg["routing"]["rules"][0]["outboundTag"] == "youtube-rf"
