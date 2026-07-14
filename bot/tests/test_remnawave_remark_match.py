from bot.remnawave_client import RemnawaveClient


def test_remarks_match_strips_new_suffix():
    assert RemnawaveClient.remarks_match(
        "🇵🇱 🆓 Poland 1 | 🎉 NEW",
        "🇵🇱 🆓 Poland 1",
    )
    assert RemnawaveClient.normalize_host_remark("🇩🇪 🆓 Germany 1 | 🎉 NEW") == "🇩🇪 🆓 Germany 1"
    assert not RemnawaveClient.remarks_match("🇵🇱 🆓 Poland 1", "🇩🇪 🆓 Germany 1")
