from __future__ import annotations

from usbnic_host.boot import detect_home_gateway
from usbnic_host.config import Config
from usbnic_host.monitor.clash import normalize_body
from usbnic_host.monitor.proxy import router_targets, tproxy_ruleset
from usbnic_host.net.modes import MODE_PROXY, detect_mode, mode_info


def _iface(name: str, connection: str = "") -> dict:
    return {"name": name, "connection": connection}


def test_detect_mode() -> None:
    config = Config()
    assert detect_mode(config, [_iface("enx", "usbnic-gateway")]) == "gateway"
    assert detect_mode(config, [_iface("wlx", "usbnic-hotspot")]) == "hotspot"
    assert detect_mode(config, [_iface("enp49s0", "usbnic-uplink")]) == "client"
    assert detect_mode(config, []) == "client"
    assert detect_mode(config, [_iface("enx", "usbnic-router-lan")]) == "router"
    assert detect_mode(config, [_iface("wlp", "usbnic-router-5g")]) == "router"


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
    assert 'iifname != { "enx-test" } return' in ruleset
    assert "ip saddr != @lan_nets return" in ruleset
    assert "10.10.10.0/24" in ruleset
    assert "tproxy to :7896" in ruleset
    # counter 用于判断规则是否真的命中，而不是只看表存在
    assert "counter accept" in ruleset
    # 私网/上游不能进代理，避免设备互访被劫持
    assert "192.168.0.0/16" in ruleset
    # fake-IP 必须被 TPROXY 送回 Clash，不能排除 198.18/16
    assert "198.18.0.0/16" not in ruleset


def test_tproxy_ruleset_excludes_direct_devices() -> None:
    config = Config(
        gateway_iface="enx-test",
        lan_cidr="10.10.10.0/24",
        clash_tproxy_port=7896,
        tproxy_exclude_ips=["10.10.10.110"],
    )
    ruleset = tproxy_ruleset(config)
    # 排除规则必须排在 tproxy 之前，才能保证直连设备不被劫持
    assert ruleset.index("ip saddr { 10.10.10.110 } return") < ruleset.index(
        "tproxy to :7896"
    )
    # 先计数再 tproxy，用于区分"匹配到"和"被接住"
    assert ruleset.index("meta l4proto { tcp, udp } counter") < ruleset.index(
        "tproxy to :7896"
    )


def test_tproxy_ruleset_for_hotspot() -> None:
    config = Config(clash_tproxy_port=7896)
    ruleset = tproxy_ruleset(config, "wlx-test", "192.168.50.0/24")
    assert 'iifname != { "wlx-test" } return' in ruleset
    assert "192.168.50.0/24" in ruleset


def test_tproxy_ruleset_covers_router_interfaces() -> None:
    config = Config(
        gateway_iface="enx-test",
        router_lan_cidr="10.10.10.0/24",
        router_5g_iface="wlp-test",
        router_5g_cidr="10.20.0.0/24",
        router_24g_iface="wlx-test",
        router_24g_cidr="10.30.0.0/24",
        router_wired_direct=True,
    )
    # 2.4G AP 默认关闭：不进入 TPROXY 接口/网段集合
    ifaces, cidrs, bypass = router_targets(config)
    assert ifaces == ["enx-test", "wlp-test"]
    assert cidrs == ["10.10.10.0/24", "10.20.0.0/24"]
    assert "10.10.10.0/24" in bypass

    ruleset = tproxy_ruleset(config, ifaces=ifaces, cidrs=cidrs, bypass=bypass)
    assert '"enx-test"' in ruleset and '"wlp-test"' in ruleset
    assert '"wlx-test"' not in ruleset
    assert "ip saddr { 10.10.10.0/24 } return" in ruleset

    # 打开开关后覆盖 2.4G 段
    enabled = Config(
        gateway_iface="enx-test",
        router_lan_cidr="10.10.10.0/24",
        router_5g_iface="wlp-test",
        router_5g_cidr="10.20.0.0/24",
        router_24g_iface="wlx-test",
        router_24g_cidr="10.30.0.0/24",
        router_24g_enabled=True,
    )
    ifaces2, cidrs2, _ = router_targets(enabled)
    assert ifaces2 == ["enx-test", "wlp-test", "wlx-test"]
    assert cidrs2 == ["10.10.10.0/24", "10.20.0.0/24", "10.30.0.0/24"]


def test_hotspot_mode_enables_proxy() -> None:
    assert MODE_PROXY["hotspot"] is True
    assert MODE_PROXY["gateway"] is True
    assert MODE_PROXY["router"] is True
    assert MODE_PROXY["client"] is False


def test_detect_home_gateway_matches_wired_topology() -> None:
    config = Config(
        gateway_iface="enx-test",
        uplink_candidates=["enp-test", "wlp-test"],
        home_gateway="192.168.1.1",
    )
    exists = lambda name: name in {"enx-test", "enp-test", "wlp-test"}  # noqa: E731
    carrier = lambda name: name in {"enx-test", "enp-test"}  # noqa: E731
    routes = [{"dev": "enp-test", "gateway": "192.168.1.1", "metric": 10}]

    ok, details = detect_home_gateway(
        config, exists=exists, carrier=carrier, routes_getter=lambda: routes
    )
    assert ok is True
    assert "enx-test" in " ".join(details)


def test_detect_home_gateway_rejects_other_network() -> None:
    config = Config(
        gateway_iface="enx-test",
        uplink_candidates=["enp-test"],
        home_gateway="192.168.1.1",
    )
    exists = lambda name: True  # noqa: E731
    carrier = lambda name: True  # noqa: E731
    routes = [{"dev": "enp-test", "gateway": "10.0.0.1", "metric": 10}]

    ok, _details = detect_home_gateway(
        config, exists=exists, carrier=carrier, routes_getter=lambda: routes
    )
    assert ok is False


def test_detect_home_gateway_requires_downstream_carrier() -> None:
    config = Config(
        gateway_iface="enx-test",
        uplink_candidates=["enp-test"],
        home_gateway="192.168.1.1",
    )
    exists = lambda name: True  # noqa: E731
    carrier = lambda name: name != "enx-test"  # noqa: E731

    ok, _details = detect_home_gateway(
        config,
        exists=exists,
        carrier=carrier,
        routes_getter=lambda: [{"dev": "enp-test", "gateway": "192.168.1.1"}],
    )
    assert ok is False


def test_normalize_clash_start_body() -> None:
    body = {
        "bin_path": "/usr/bin/verge-mihomo",
        "config_dir": "/tmp/clash",
        "config_file": "/tmp/clash/config.yaml",
        "log_file": "/tmp/clash/log",
    }
    assert normalize_body(body) == body
    assert normalize_body({}) == {}
    assert normalize_body({"bin_path": "/usr/bin/verge-mihomo"}) == {}
    assert normalize_body(None) == {}
