from __future__ import annotations

from pathlib import Path

import pytest

from usbnic_host.monitor.sources import (
    lookup_vendor,
    parse_dhcp_leases,
    parse_iw_station_dump,
    short_vendor,
)
from usbnic_host.util import parse_ping

OUI_FILE = "/usr/share/ieee-data/oui.txt"

STATION_SAMPLE = """Station cc:a2:60:a2:94:71 (on wlp0s20f3)
	inactive time:	8 ms
	rx bytes:	58240364
	rx packets:	144892
	tx bytes:	230581433
	tx packets:	84636
	signal:  	-59 [-63, -59] dBm
	signal avg:	-56 dBm
	tx bitrate:	104.0 MBit/s MCS 13
	rx bitrate:	52.0 MBit/s MCS 11
	connected time:	1234 seconds
"""


def test_parse_iw_station_dump() -> None:
    stations = parse_iw_station_dump(STATION_SAMPLE, "wlp0s20f3")
    assert len(stations) == 1
    station = stations[0]
    assert station["mac"] == "cc:a2:60:a2:94:71"
    assert station["iface"] == "wlp0s20f3"
    assert station["signal_dbm"] == -59
    assert station["signal_avg_dbm"] == -56
    assert station["rx_bytes"] == 58240364
    assert station["tx_bytes"] == 230581433
    assert station["rx_rate_mbps"] == 52.0
    assert station["tx_rate_mbps"] == 104.0
    assert station["connected_seconds"] == 1234
    assert station["inactive_ms"] == 8


def test_parse_dhcp_leases(tmp_path: Path) -> None:
    lease = tmp_path / "dnsmasq-test.leases"
    lease.write_text(
        "1750000000 aa:bb:cc:dd:ee:ff 10.10.10.50 switch 01:aa:bb:cc:dd:ee:ff\n"
        "1750000000 11:22:33:44:55:66 10.10.10.51 * 01:11:22:33:44:55:66\n",
        encoding="utf-8",
    )
    leases = parse_dhcp_leases([str(lease)])
    assert len(leases) == 2
    assert leases[0]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert leases[0]["ip"] == "10.10.10.50"
    assert leases[0]["hostname"] == "switch"
    assert leases[1]["hostname"] == ""


@pytest.mark.skipif(not Path(OUI_FILE).exists(), reason="IEEE OUI 数据库不存在")
def test_nintendo_oui() -> None:
    vendor = lookup_vendor("98:e8:fa:00:00:01", OUI_FILE)
    assert "Nintendo" in vendor
    assert short_vendor(vendor) == "Nintendo"


def test_parse_ping() -> None:
    output = (
        "PING 223.5.5.5 (223.5.5.5) 56(84) bytes of data.\n"
        "64 bytes from 223.5.5.5: icmp_seq=1 ttl=117 time=12.3 ms\n\n"
        "--- 223.5.5.5 ping statistics ---\n"
        "1 packets transmitted, 1 received, 0% packet loss, time 0ms\n"
    )
    result = parse_ping(output)
    assert result["ok"] is True
    assert result["rtt_ms"] == 12.3
    assert result["loss_pct"] == 0.0
