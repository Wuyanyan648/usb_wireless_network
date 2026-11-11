from usbnic_host.net.wifi_caps import _ap_supported, _channel_flags, describe

PHY_INFO = """
	Supported interface modes:
		 * IBSS
		 * managed
		 * AP
		 * AP/VLAN
		 * P2P-client
	Band 1:
		Frequencies:
			* 2412.0 MHz [1] (22.0 dBm)
			* 2437.0 MHz [6] (22.0 dBm)
	Band 2:
		Frequencies:
			* 5180.0 MHz [36] (22.0 dBm) (no IR)
			* 5745.0 MHz [149] (22.0 dBm)
"""


def test_ap_supported() -> None:
    assert _ap_supported(PHY_INFO) is True
    assert _ap_supported("Supported interface modes:\n\t * managed\n") is False


def test_channel_flags_detects_no_ir() -> None:
    frequency, flags, blocked = _channel_flags(PHY_INFO, 36)
    assert frequency == 5180 and blocked is True
    assert any("no ir" in flag for flag in flags)

    frequency2, _flags2, blocked2 = _channel_flags(PHY_INFO, 6)
    assert frequency2 == 2437 and blocked2 is False

    assert _channel_flags(PHY_INFO, 100) is None


def test_describe_includes_reason() -> None:
    text = describe(
        {"iface": "wlp0s20f3", "channel": 36, "available": False,
         "reasons": ["信道 36 被标记为 no IR/disabled（监管域限制）"]}
    )
    assert "不可用" in text and "no IR" in text
    assert "可" in describe({"iface": "wlx1", "channel": 36, "available": True})
