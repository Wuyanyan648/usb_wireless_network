from __future__ import annotations

from pathlib import Path

from usbnic_host.config import Config
from usbnic_host.monitor import device_policy
from usbnic_host.monitor.proxy import tproxy_ruleset


def _config(tmp_path: Path) -> Config:
    return Config(
        gateway_iface="enx-test",
        lan_cidr="10.10.10.0/24",
        clash_tproxy_port=7896,
        data_dir=tmp_path,
        tproxy_default_policy="direct",
    )


def test_policy_defaults_to_direct(tmp_path: Path) -> None:
    config = _config(tmp_path)
    data = device_policy.load(config)
    assert data["default"] == "direct"
    assert data["devices"] == {}
    assert device_policy.resolve(config, "aa:bb:cc:dd:ee:ff") == "direct"


def test_policy_roundtrip_and_override(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert device_policy.set_device(config, "AA-BB-CC-DD-EE-FF", "proxy")["ok"]
    assert device_policy.resolve(config, "aa:bb:cc:dd:ee:ff") == "proxy"
    assert device_policy.resolve(config, "aa:bb:cc:dd:ee:00") == "direct"

    assert device_policy.set_default(config, "proxy")["ok"]
    assert device_policy.resolve(config, "aa:bb:cc:dd:ee:00") == "proxy"
    # 单独设置为直连的设备仍保持直连
    assert device_policy.resolve(config, "aa:bb:cc:dd:ee:ff") == "proxy"
    device_policy.set_device(config, "aa:bb:cc:dd:ee:ff", "direct")
    assert device_policy.resolve(config, "aa:bb:cc:dd:ee:ff") == "direct"

    # 非法值不落盘
    assert not device_policy.set_device(config, "bad", "proxy")["ok"]
    assert not device_policy.set_default(config, "auto")["ok"]


def test_assignments_split_proxy_and_direct_ips(tmp_path: Path) -> None:
    config = _config(tmp_path)
    device_policy.set_device(config, "aa:bb:cc:dd:ee:01", "proxy")
    items = [
        {"ip": "10.10.10.101", "mac": "aa:bb:cc:dd:ee:01"},
        {"ip": "10.10.10.110", "mac": "aa:bb:cc:dd:ee:02"},
        {"ip": "10.10.10.111", "mac": ""},
    ]
    proxy_ips, direct_ips = device_policy.assignments(config, items)
    assert proxy_ips == ["10.10.10.101"]
    # 没有 MAC 的条目无法按设备判定，按直连处理
    assert direct_ips == ["10.10.10.110"]


def test_ruleset_orders_direct_before_tproxy() -> None:
    config = Config(
        gateway_iface="enx-test",
        lan_cidr="10.10.10.0/24",
        clash_tproxy_port=7896,
    )
    ruleset = tproxy_ruleset(
        config, proxy_nets=["10.10.10.101"], direct_nets=["10.10.10.110"]
    )
    assert "set proxy_nets" in ruleset and "set direct_nets" in ruleset
    assert "ip saddr @direct_nets return" in ruleset
    assert "ip saddr != @proxy_nets return" in ruleset
    assert ruleset.index("ip saddr @direct_nets return") < ruleset.index(
        "ip saddr != @proxy_nets return"
    )
    assert ruleset.index("ip saddr != @proxy_nets return") < ruleset.index(
        "tproxy to :7896"
    )


def test_ruleset_skips_when_no_device_selected() -> None:
    config = Config(gateway_iface="enx-test", clash_tproxy_port=7896)
    assert tproxy_ruleset(config, proxy_nets=[], direct_nets=["10.10.10.110"]) == ""
