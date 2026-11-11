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
    )


def test_policy_is_always_proxy(tmp_path: Path) -> None:
    """不再有逐设备策略：任何 MAC 都固定走代理，也不读写策略文件。"""
    config = _config(tmp_path)
    (tmp_path / "device_policies.json").write_text(
        '{"default": "direct", "devices": {"aa:bb:cc:dd:ee:ff": "direct"}}',
        encoding="utf-8",
    )
    data = device_policy.load(config)
    assert data == {"default": "proxy", "devices": {}}
    assert device_policy.resolve(config, "aa:bb:cc:dd:ee:ff") == "proxy"


def test_assignments_takes_all_downstream_ips(tmp_path: Path) -> None:
    config = _config(tmp_path)
    items = [
        {"ip": "10.10.10.101", "mac": "aa:bb:cc:dd:ee:01"},
        {"ip": "10.10.10.110", "mac": "aa:bb:cc:dd:ee:02"},
        {"ip": "10.10.10.111", "mac": ""},
        {"ip": "10.10.10.1", "mac": "aa:bb:cc:dd:ee:99", "is_local": True},
    ]
    proxy_ips, direct_ips = device_policy.assignments(config, items)
    # 没有 MAC 的条目无法按设备判定；is_local 的本机地址不进下游集合
    assert proxy_ips == ["10.10.10.101", "10.10.10.110"]
    assert direct_ips == []


def test_table_reports_direct_when_tproxy_off(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(device_policy, "_tproxy_active", lambda: False)
    items = [{"ip": "10.10.10.101", "mac": "aa:bb:cc:dd:ee:01"}]
    table = device_policy.table(config, items)
    assert table["default"] == "proxy"
    # 没有装表时 effective 必须是 direct，页面才不会假装"已走代理"
    assert table["active"] is False
    assert table["items"][0]["policy"] == "proxy"
    assert table["items"][0]["effective"] == "direct"


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
