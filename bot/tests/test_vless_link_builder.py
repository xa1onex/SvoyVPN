from bot.vless_link_builder import build_vless_link, client_flow_for_network


def test_grpc_reality_link():
    stream = {
        "network": "grpc",
        "security": "reality",
        "grpcSettings": {"serviceName": "mygrpc", "multiMode": False},
        "realitySettings": {
            "settings": {"publicKey": "pk123", "fingerprints": ["chrome"]},
            "serverNames": ["cdn.example.com"],
            "shortIds": ["abcd"],
        },
    }
    link = build_vless_link(
        client_uuid="uuid-1",
        listen_ip="1.2.3.4",
        port=443,
        stream_settings=stream,
        display_name="DE grpc",
    )
    assert "type=grpc" in link
    assert "type=tcp" not in link
    assert "security=reality" in link
    assert "pbk=pk123" in link
    assert "serviceName=mygrpc" in link
    assert "mode=gun" in link
    assert "flow=" not in link


def test_tcp_reality_vision_link():
    stream = {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
            "settings": {"publicKey": "pk"},
            "serverNames": ["google.com"],
            "shortId": "3d",
        },
    }
    link = build_vless_link(
        client_uuid="uuid-2",
        listen_ip="5.6.7.8",
        port=8443,
        stream_settings=stream,
        display_name="tcp node",
    )
    assert "type=tcp" in link
    assert "flow=xtls-rprx-vision" in link
    assert "spx=%2F" in link


def test_client_flow_for_network():
    assert client_flow_for_network("tcp") == "xtls-rprx-vision"
    assert client_flow_for_network("grpc") == ""


def test_reality_sni_strips_trailing_colon():
    from bot.vless_link_builder import extract_reality_params

    params = extract_reality_params(
        {"realitySettings": {"serverNames": ["www.yandex.ru:"], "settings": {"publicKey": "pk"}}}
    )
    assert params["sni"] == "www.yandex.ru"
