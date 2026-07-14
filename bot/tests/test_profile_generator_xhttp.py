"""xhttp + TLS (Remnawave bypass) в Happ JSON."""

from bot.profile_generator import (
    _adapt_xhttp_extra,
    _build_single_server_config,
    _build_stream,
    parse_vless_link,
)

_SAMPLE = (
    "vless://a827b786-c5ca-47fc-afa9-636f6272ba72@shop.xdoublegroup.online:443"
    "?encryption=none&type=xhttp&path=%2Fapi%2Fcart%2Fsync&host=shop.xdoublegroup.online"
    "&mode=auto&extra=%7B%22path%22%3A%22%2Fapi%2Fcart%2Fsync%22%2C%22seqKey%22%3A%22chunk%22%2C"
    "%22sessionKey%22%3A%22visitor_id%22%2C%22sessionPlacement%22%3A%22cookie%22%2C"
    "%22uplinkHTTPMethod%22%3A%22HEAD%22%2C%22uplinkDataPlacement%22%3A%22body%22%2C"
    "%22uplinkChunkSize%22%3A0%7D"
    "&security=tls&sni=shop.xdoublegroup.online&fp=chrome&alpn=h2%2Chttp%2F1.1"
    "#Poland%201"
)


def test_parse_xhttp_remnawave_link():
    p = parse_vless_link(_SAMPLE)
    assert p is not None
    assert p["type"] == "xhttp"
    assert p["security"] == "tls"
    assert p["path"] == "/api/cart/sync"
    assert p["host"] == "shop.xdoublegroup.online"
    assert p["mode"] == "auto"
    assert isinstance(p["extra"], dict)
    assert p["extra"].get("sessionKey") == "visitor_id"
    assert p["alpn"] == ["h2", "http/1.1"]


def test_adapt_xhttp_extra_adds_session_id_fields_for_xray_266():
    ex = _adapt_xhttp_extra(
        {
            "path": "/api/cart/sync",
            "sessionKey": "visitor_id",
            "sessionPlacement": "cookie",
            "uplinkChunkSize": 0,
            "uplinkDataPlacement": "body",
        },
        "/api/cart/sync",
    )
    assert ex["sessionIDKey"] == "visitor_id"
    assert ex["sessionIDPlacement"] == "cookie"
    assert ex["sessionIDLength"] == "16-32"
    assert len(ex["sessionIDTable"]) >= 62
    assert ex["sessionKey"] == "visitor_id"
    assert ex["uplinkChunkSize"] == 0
    assert "xmux" not in ex


def test_build_stream_xhttp_includes_session_id_extra():
    p = parse_vless_link(_SAMPLE)
    ss = _build_stream(p)
    assert ss["network"] == "xhttp"
    xhttp = ss["xhttpSettings"]
    extra = xhttp["extra"]
    assert extra["sessionIDKey"] == "visitor_id"
    assert extra["sessionIDPlacement"] == "cookie"
    tls = ss["tlsSettings"]
    assert tls["serverName"] == "shop.xdoublegroup.online"
    assert "allowInsecure" not in tls


def test_xhttp_single_server_uses_minimal_happ_layout(monkeypatch):
    monkeypatch.setenv("SVOYVPN_YOUTUBE_RF_ENABLED", "1")
    import bot.profile_generator as pg
    pg._youtube_rf_outbound_cache = None
    p = parse_vless_link(_SAMPLE)
    cfg = _build_single_server_config(p, remarks="PL", description="")
    assert cfg["dns"]["queryStrategy"] == "UseIP"
    assert cfg["dns"]["servers"] == ["1.1.1.1", "1.0.0.1"]
    assert cfg["inbounds"][0]["sniffing"]["routeOnly"] is False
    assert "burstObservatory" not in cfg
    assert "policy" not in cfg
    rules = cfg["routing"]["rules"]
    assert rules[0]["outboundTag"] == "youtube-rf"
    assert "geosite:youtube" in rules[0]["domain"]
    tags = [o.get("tag") for o in cfg["outbounds"]]
    assert tags == ["proxy", "youtube-rf", "direct", "block"]
