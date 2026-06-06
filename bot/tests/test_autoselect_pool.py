"""Тесты пула автовыбора и generate_xray_profile."""

from bot.autoselect_pool import (
    AUTO_BALANCER_TOLERANCE,
    AUTO_OBSERVATORY_URL,
    build_autoselect_layout,
    collect_autoselect_candidates,
)
from bot.profile_generator import generate_xray_profile


def _link(addr: str, *, sni: str = "", net: str = "grpc", sec: str = "reality") -> str:
    q = f"type={net}&security={sec}&sni={sni or addr}&fp=chrome&pbk=pk&sid=ab"
    if net == "grpc":
        q += "&serviceName=svc"
    return f"vless://uuid-0000-0000-0000-000000000001@{addr}:443?{q}#test"


def test_collect_dedupes_address():
    links = [
        _link("1.1.1.1", sni="nl.example.com"),
        _link("1.1.1.1", sni="nl2.example.com"),
        _link("2.2.2.2", sni="de.example.com"),
    ]
    cands = collect_autoselect_candidates(links, ["a", "b", "c"], server_is_bypass=[False, True, False])
    assert len(cands) == 2


def test_layout_diversity_pool():
    links = [_link(f"10.0.0.{i}", sni=f"cdn{i}.terem.live" if i % 3 == 0 else f"srv{i}.hetzner.cloud") for i in range(12)]
    cands = collect_autoselect_candidates(links)
    layout = build_autoselect_layout(cands, smart_min=8, smart_max=15)
    assert len(layout.smart) >= 8


def test_generate_xray_profile_structure():
    links = [_link(f"10.0.0.{i}") for i in range(1, 10)]
    prof = generate_xray_profile(links)
    tags = [o["tag"] for o in prof["outbounds"] if o.get("protocol") == "vless"]
    assert tags[0] == "proxy"
    assert tags[1] == "smart"
    assert "burstObservatory" in prof
    assert prof["burstObservatory"]["pingConfig"]["destination"] == AUTO_OBSERVATORY_URL
    assert prof["burstObservatory"]["pingConfig"]["destination"].startswith("http://")
    bal = prof["routing"]["balancers"][0]
    assert bal["fallbackTag"] == "proxy"
    assert bal["strategy"]["settings"]["tolerance"] == AUTO_BALANCER_TOLERANCE
    assert sum(1 for t in tags if t.startswith("smart")) == 8
