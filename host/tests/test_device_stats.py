from usbnic_host.config import Config
from usbnic_host.monitor.device_stats import (
    _parse_element,
    stat_networks,
    stat_ruleset,
)


def test_stat_ruleset_counts_both_directions() -> None:
    config = Config(lan_cidr="10.10.10.0/24")
    ruleset = stat_ruleset(config)
    # 上行在 prerouting、下行在 postrouting，两个 hook 都只 update 不 accept/drop
    assert "hook prerouting priority -160" in ruleset
    assert "hook postrouting priority 0" in ruleset
    assert "update @lan_tx { ip saddr }" in ruleset
    assert "update @lan_rx { ip daddr }" in ruleset
    assert "counter" in ruleset
    assert "accept" not in ruleset.replace("policy accept", "")
    assert "10.10.10.0/24" in ruleset


def test_stat_networks_deduplicates() -> None:
    config = Config(lan_cidr="10.10.10.0/24", router_lan_cidr="10.10.10.0/24")
    networks = stat_networks(config)
    assert networks.count("10.10.10.0/24") == 1
    assert "10.20.0.0/24" in networks and "10.30.0.0/24" in networks


def test_parse_element_both_shapes() -> None:
    wrapped = {
        "elem": {"val": "10.10.10.48", "counter": {"packets": 3, "bytes": 4096}}
    }
    flat = {"val": "10.10.10.49", "counter": {"packets": 1, "bytes": 100}}
    assert _parse_element(wrapped)[0] == "10.10.10.48"
    assert _parse_element(wrapped)[1]["bytes"] == 4096
    assert _parse_element(flat)[1]["bytes"] == 100
    assert _parse_element(None) == ("", {})
