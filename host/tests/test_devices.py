from __future__ import annotations

from usbnic_host.config import Config
from usbnic_host.monitor.devices import DeviceTracker, build_devices

OUI_FILE = "/usr/share/ieee-data/oui.txt"


def test_build_devices_merges_sources() -> None:
    interfaces = [
        {
            "name": "enx-test",
            "type": "ethernet",
            "ipv4": ["10.10.10.1/24"],
            "wifi_mode": None,
        },
        {
            "name": "wlx-ap",
            "type": "wifi",
            "ipv4": ["10.42.0.1/24"],
            "wifi_mode": "AP",
        },
    ]
    switch_mac = "98:e8:fa:00:00:01"
    phone_mac = "11:22:33:44:55:66"
    leases = [
        {
            "mac": switch_mac,
            "ip": "10.10.10.50",
            "hostname": "switch",
            "expiry": 1900000000,
        },
        {
            "mac": phone_mac,
            "ip": "10.10.10.51",
            "hostname": "",
            "expiry": 1900000000,
        },
    ]
    neigh = [
        {
            "ip": "10.10.10.50",
            "mac": switch_mac,
            "dev": "enx-test",
            "state": "REACHABLE",
            "router": False,
        },
        {
            "ip": "10.10.10.51",
            "mac": phone_mac,
            "dev": "wlx-ap",
            "state": "REACHABLE",
            "router": False,
        },
    ]
    stations = [
        {
            "mac": phone_mac,
            "iface": "wlx-ap",
            "signal_dbm": -50,
            "signal_avg_dbm": -48,
            "rx_bytes": 1000,
            "tx_bytes": 2000,
            "rx_rate_mbps": 300.0,
            "tx_rate_mbps": 200.0,
            "connected_seconds": 60,
            "inactive_ms": 5,
        }
    ]
    clash = {
        "ok": True,
        "connections": [
            {
                "source_ip": "10.10.10.50",
                "chains": ["HK", "Proxy"],
                "upload": 100,
                "download": 200,
            },
            {
                "source_ip": "10.10.10.51",
                "chains": ["JP"],
                "upload": 50,
                "download": 80,
            },
        ],
    }

    devices = build_devices(
        interfaces,
        leases,
        neigh,
        stations,
        clash,
        Config(oui_file=OUI_FILE),
        now_ts=1000.0,
    )

    switch = next(d for d in devices.values() if d["mac"] == switch_mac)
    assert switch["name"] == "Nintendo Switch"
    assert switch["type"] == "wired"
    assert switch["online"] is True
    assert switch["interface"] == "enx-test"
    assert sorted(switch["proxy_chains"]) == ["HK", "Proxy"]
    assert switch["proxy_connections"] == 1
    assert switch["byte_source"] == "clash"

    phone = next(d for d in devices.values() if d["mac"] == phone_mac)
    assert phone["type"] == "wifi-ap"
    assert phone["online"] is True
    assert phone["signal_dbm"] == -50
    assert phone["rx_rate_mbps"] == 300.0
    assert phone["byte_source"] == "iw"
    assert sorted(phone["proxy_chains"]) == ["JP"]


def test_device_tracker_keeps_offline_briefly() -> None:
    tracker = DeviceTracker(offline_after=180)
    current = {
        "aa:bb:cc:dd:ee:ff": {
            "key": "aa:bb:cc:dd:ee:ff",
            "online": True,
            "last_seen": 100.0,
            "first_seen": 100.0,
            "ip": "10.10.10.20",
        }
    }
    items = tracker.update(current, 100.0)
    assert len(items) == 1
    assert items[0]["online"] is True

    items = tracker.update({}, 110.0)
    assert len(items) == 1
    assert items[0]["online"] is False

    items = tracker.update({}, 300.0)
    assert items == []
