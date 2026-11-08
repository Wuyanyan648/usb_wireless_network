from __future__ import annotations

from usbnic_host.config import Config
from usbnic_host.monitor.proxy import tproxy_ruleset
from usbnic_host.net.modes import MODE_PROXY, detect_mode, mode_info


def _iface(name: str, connection: str = "") -> dict:
    return {"name": name, "connection": connection}


def test_detect_mode() -> None:
    config = Config()
    assert detect_mode(config, [_iface("enx", "usbnic-gateway")]) == "gateway"
    assert detect_mode(config, [_iface("wlx", "usbnic-hotspot")]) == "hotspot"
    assert detect_mode(config, [_iface("enp49s0", "usbnic-uplink")]) == "client"
    assert detect_mode(config, []) == "client"


def test_mode_info_marks_active() -> None:
    config = Config()
    info = mode_info(config, [_iface("wlx", "usbnic-hotspot")])
    assert info["current"] == "hotspot"
    active = [item for item in info["items"] if item["active"]]
    assert len(active) == 1
    assert active[0]["key"] == "hotspot"


def test_tproxy_ruleset_only_hijacks_lan_forwarding() -> None:
    config = Config(
        gateway_iface="enx-test",
        lan_cidr="10.10.10.0/24",
        clash_tproxy_port=7896,
    )
    ruleset = tproxy_ruleset(config)
    assert 'iifname != "enx-test" return' in ruleset
    assert "ip saddr != 10.10.10.0/24 return" in ruleset
    assert "tproxy to :7896" in ruleset
    # 下游 LAN 自身与上游私网不能进代理，避免设备互访被劫持
    assert "10.10.10.0/24" in ruleset.split("ip daddr")[1]
    assert "192.168.0.0/16" in ruleset


def test_tproxy_ruleset_for_hotspot() -> None:
    config = Config(clash_tproxy_port=7896)
    ruleset = tproxy_ruleset(config, "wlx-test", "192.168.50.0/24")
    assert 'iifname != "wlx-test" return' in ruleset
    assert "ip saddr != 192.168.50.0/24 return" in ruleset


def test_hotspot_mode_enables_proxy() -> None:
    assert MODE_PROXY["hotspot"] is True
    assert MODE_PROXY["gateway"] is True
    assert MODE_PROXY["client"] is False
