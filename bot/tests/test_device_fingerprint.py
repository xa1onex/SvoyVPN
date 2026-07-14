from bot.device_fingerprint import (
    analyze_subscription_client,
    compute_subscription_device_fingerprint,
    format_device_display_name,
    happ_ios_has_broken_xhttp,
    is_countable_subscription_client,
    parse_happ_install_key,
    resolve_device_model_name,
)


def test_happ_install_key_ignores_app_version():
    a = parse_happ_install_key("Happ/4.10.2/ios/2605221355512")
    b = parse_happ_install_key("Happ/4.11.0/ios/2605221355512")
    assert a == b == ("ios", "2605221355512")


def test_happ_ios_broken_xhttp_version_gate():
    assert happ_ios_has_broken_xhttp("Happ/4.14.0/ios/2607031625695")
    assert not happ_ios_has_broken_xhttp("Happ/4.7.0/ios/2603181558630")
    assert not happ_ios_has_broken_xhttp("Happ/4.6.0/macos catalyst/2603181558630")


def test_hwid_fingerprint_stable():
    fp1 = compute_subscription_device_fingerprint(
        "Happ/4.10.2/ios/2605221355512",
        device_hwid="abc123",
    )
    fp2 = compute_subscription_device_fingerprint(
        "Happ/4.11.0/ios/9999999999999",
        device_hwid="abc123",
    )
    assert fp1 == fp2


def test_resolve_iphone_model_identifier():
    assert resolve_device_model_name("iPhone16,1", "iOS") == "iPhone 15 Pro"
    assert resolve_device_model_name("iPhone 15 Pro", "iOS") == "iPhone 15 Pro"


def test_analyze_subscription_client_with_happ_headers():
    info = analyze_subscription_client(
        {
            "User-Agent": "Happ/4.10.2/ios/2605221355512",
            "X-Device-Model": "iPhone16,1",
            "X-Device-Os": "iOS",
            "X-Ver-Os": "18.4",
            "X-Hwid": "test-hwid-001",
        }
    )
    assert info.display_name == "iPhone 15 Pro"
    assert info.device_hwid == "test-hwid-001"


def test_format_device_display_name_from_stored_model():
    assert format_device_display_name(
        "Happ/4.10.2/ios/1",
        device_model="MacBook Air (M2)",
    ) == "MacBook Air (M2)"


def test_non_vpn_clients_not_countable():
    assert not is_countable_subscription_client(
        "Mozilla/5.0 (Linux; Android 16; K) AppleWebKit/537.36 Chrome/148.0"
    )
    assert is_countable_subscription_client("Happ/4.10.2/ios/2605221355512")
