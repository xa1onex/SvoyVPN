"""Tests for Hysteria2 static server → Happ profile."""

from bot.profile_generator import (
    _build_single_server_config,
    generate_happ_configs_list,
    parse_hysteria2_link,
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
        if c.get("outbounds") and c["outbounds"][0].get("protocol") == "hysteria"
    ]
    assert len(hy2_cfgs) == 1
    vless_profiles = [
        c
        for c in configs
        if any(o.get("protocol") == "vless" for o in c.get("outbounds", []))
    ]
    assert vless_profiles
    assert all(
        o.get("protocol") != "hysteria"
        for c in vless_profiles
        for o in c.get("outbounds", [])
    )
    assert len(configs) >= 3  # auto + DE + YouTube
