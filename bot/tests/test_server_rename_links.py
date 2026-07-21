"""Server rename / light link refresh."""

from bot.happ_subscription import _row_remark
from bot.happ_text_notice import vless_link_title_only
from bot.profile_generator import generate_happ_configs_list, parse_vless_link
from bot.subscriptions import _merge_link_update_fields, _replace_proxy_link_remark


def test_replace_proxy_link_remark_keeps_base():
    link = (
        "vless://a827b786-c5ca-47fc-afa9-636f6272ba72@1.2.3.4:443"
        "?type=tcp&security=reality&pbk=x&sid=01&sni=a.com#Old%20Name"
    )
    out = _replace_proxy_link_remark(link, "🇩🇪 Germany")
    assert out.startswith("vless://a827b786-c5ca-47fc-afa9-636f6272ba72@1.2.3.4:443")
    assert out.endswith("#%F0%9F%87%A9%F0%9F%87%AA%20Germany") or "Germany" in out
    p = parse_vless_link(out)
    assert p["remark"] == "🇩🇪 Germany"


def test_vless_link_title_only_hy2():
    link = "hysteria2://auth@hysteria2.example:443/?sni=x&alpn=h3#old"
    out = vless_link_title_only(link, title="🇷🇺 YouTube")
    assert out.startswith("hysteria2://auth@hysteria2.example:443/")
    assert "YouTube" in out


def test_happ_prefers_db_server_name_over_link_remark():
    vless = (
        "vless://a1b2c3d4-e5f6-7890-abcd-ef1234567890@1.2.3.4:443"
        "?type=tcp&security=reality&pbk=abc&sid=01&sni=example.com&fp=chrome#OLD"
    )
    configs = generate_happ_configs_list(
        [vless],
        ["🇩🇪 NEW Germany"],
        server_is_bypass=[False],
    )
    singles = [c for c in configs if c.get("remarks") and "NEW" in str(c.get("remarks"))]
    assert singles
    assert any("NEW Germany" in str(c.get("remarks")) for c in configs)


def test_row_remark_prefers_server_name():
    assert (
        _row_remark(
            {
                "server_name": "🇩🇪 NEW",
                "vless_link": "vless://u@1.2.3.4:443?type=tcp&security=none#OLD",
            }
        )
        == "🇩🇪 NEW"
    )


def test_merge_link_update_fields_prefers_connection():
    assert _merge_link_update_fields("name", "ip") == "ip"
    assert _merge_link_update_fields("display_order", "name") == "name"
    assert _merge_link_update_fields(None, "port") == "port"
