"""Tests for Hysteria2 static server → Happ profile."""

import os

from bot.profile_generator import (
    _build_single_server_config,
    generate_happ_configs_list,
    generate_xray_profile,
    parse_hysteria2_link,
    parse_vless_link,
)
from bot.static_servers import youtube_adfree_link


def test_parse_youtube_hy2_link():
    link = youtube_adfree_link()
    p = parse_hysteria2_link(link)
    assert p is not None
    assert p["protocol"] == "hysteria2"
    assert p["address"] == "hysteria2.s1gyma4ka.ru"
    assert p["port"] == 443
    assert p["auth"] == "c6434293-3fd4-4409-962c-0416f0a2fe96"
    assert p["alpn"] == ["h3"]
    assert "YouTube" in p["remark"]


def test_hy2_single_config_matches_happ_shape():
    p = parse_hysteria2_link(youtube_adfree_link())
    cfg = _build_single_server_config(
        p, remarks="🇷🇺 YouTube без рекламы", description="YouTube без рекламы"
    )
    proxy = cfg["outbounds"][0]
    assert proxy["protocol"] == "hysteria"
    assert proxy["settings"]["address"] == "hysteria2.s1gyma4ka.ru"
    assert proxy["settings"]["version"] == 2
    ss = proxy["streamSettings"]
    assert ss["network"] == "hysteria"
    assert ss["hysteriaSettings"]["auth"] == "c6434293-3fd4-4409-962c-0416f0a2fe96"
    assert ss["tlsSettings"]["alpn"] == ["h3"]
    assert ss["finalmask"]["quicParams"]["congestion"] == "bbr"
    # Не должно уводить YouTube в direct через geoip:ru
    assert cfg["routing"]["rules"] == [
        {"outboundTag": "direct", "protocol": ["bittorrent"], "type": "field"}
    ]


def test_hy2_not_in_autoselect_pool():
    vless = (
        "vless://a1b2c3d4-e5f6-7890-abcd-ef1234567890@1.2.3.4:443"
        "?type=tcp&security=reality&pbk=abc&sid=01&sni=example.com&fp=chrome#DE"
    )
    hy2 = youtube_adfree_link()
    configs = generate_happ_configs_list(
        [vless, hy2],
        ["🇩🇪 Germany", "🇷🇺 YouTube без рекламы"],
        server_is_bypass=[False, False],
    )
    hy2_cfgs = [
        c
        for c in configs
        if c.get("outbounds")
        and c["outbounds"][0].get("protocol") == "hysteria"
        and c["outbounds"][0].get("tag") == "proxy"
    ]
    assert len(hy2_cfgs) == 1
    vless_profiles = [
        c
        for c in configs
        if any(o.get("protocol") == "vless" for o in c.get("outbounds", []))
    ]
    assert vless_profiles
    # RF выключен по умолчанию — в VLESS-профилях нет youtube-rf
    for c in vless_profiles:
        tags = [o.get("tag") for o in c.get("outbounds", [])]
        assert "youtube-rf" not in tags
    assert len(configs) >= 3  # auto + DE + YouTube


def test_vless_does_not_route_youtube_when_rf_disabled(monkeypatch):
    monkeypatch.delenv("SVOYVPN_YOUTUBE_RF_ENABLED", raising=False)
    vless = (
        "vless://a1b2c3d4-e5f6-7890-abcd-ef1234567890@1.2.3.4:443"
        "?type=tcp&security=reality&pbk=abc&sid=01&sni=example.com&fp=chrome#DE"
    )
    cfg = _build_single_server_config(parse_vless_link(vless), remarks="DE", description="")
    auto = generate_xray_profile([vless])
    for profile in (cfg, auto):
        tags = [o.get("tag") for o in profile["outbounds"]]
        assert "youtube-rf" not in tags
        assert all(r.get("outboundTag") != "youtube-rf" for r in profile["routing"]["rules"])


def test_vless_routes_youtube_through_rf_when_enabled(monkeypatch):
    monkeypatch.setenv("SVOYVPN_YOUTUBE_RF_ENABLED", "1")
    # reload routing flag path used at call time
    from bot import routing_rules
    assert routing_rules.youtube_rf_enabled()
    vless = (
        "vless://a1b2c3d4-e5f6-7890-abcd-ef1234567890@1.2.3.4:443"
        "?type=tcp&security=reality&pbk=abc&sid=01&sni=example.com&fp=chrome#DE"
    )
    cfg = _build_single_server_config(parse_vless_link(vless), remarks="DE", description="")
    rf = next(o for o in cfg["outbounds"] if o.get("tag") == "youtube-rf")
    assert rf["settings"]["address"] == "hysteria2.s1gyma4ka.ru"
    assert cfg["routing"]["rules"][0]["outboundTag"] == "youtube-rf"
