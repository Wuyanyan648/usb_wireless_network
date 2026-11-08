from __future__ import annotations

from usbnic_host.monitor.clash_proxies import build_groups


def _proxies() -> dict:
    return {
        "AUTO": {
            "type": "URLTest",
            "now": "节点1",
            "fixed": "",
            "all": ["节点1", "节点2"],
            "alive": True,
        },
        "节点1": {
            "type": "Shadowsocks",
            "alive": True,
            "history": [{"delay": 120}],
        },
        "节点2": {
            "type": "Shadowsocks",
            "alive": False,
            "history": [{"delay": 0}],
        },
        "PROXY": {
            "type": "Selector",
            "now": "AUTO",
            "all": ["AUTO", "节点1"],
            "alive": True,
        },
        "OTHER": {
            "type": "Selector",
            "now": "PROXY",
            "all": ["PROXY", "DIRECT"],
            "alive": True,
        },
        "DIRECT": {"type": "Direct", "alive": True},
        "GLOBAL": {
            "type": "Selector",
            "now": "PROXY",
            "all": ["PROXY"],
            "alive": True,
        },
    }


def _group(groups: list[dict], name: str) -> dict:
    return next(item for item in groups if item["name"] == name)


def test_build_groups_excludes_global_and_plain_nodes() -> None:
    names = [item["name"] for item in build_groups(_proxies())]
    assert names == ["AUTO", "PROXY", "OTHER"]


def test_auto_group_reports_actual_exit_without_manual_selection() -> None:
    auto = _group(build_groups(_proxies()), "AUTO")
    assert auto["type"] == "URLTest"
    assert auto["auto"] is True
    assert auto["auto_supported"] is True
    assert auto["fixed"] == ""
    assert auto["exit"] == "节点1"
    assert auto["delay"] == 120
    assert auto["chain"] == ["AUTO", "节点1"]
    # 自动组没有手动固定时，不应把当前节点标成用户选择
    assert not any(option["selected"] for option in auto["options"])
    assert auto["options"][0]["delay"] == 120


def test_selector_can_point_to_auto_group_and_resolve_chain() -> None:
    proxy = _group(build_groups(_proxies()), "PROXY")
    assert proxy["selectable"] is True
    assert proxy["auto"] is True
    assert proxy["exit"] == "节点1"
    assert proxy["chain"] == ["PROXY", "AUTO", "节点1"]
    auto_option = next(item for item in proxy["options"] if item["name"] == "AUTO")
    assert auto_option["selected"] is True
    assert auto_option["is_group"] is True
    assert auto_option["exit"] == "节点1"
    assert auto_option["delay"] == 120


def test_nested_groups_resolve_to_final_node() -> None:
    other = _group(build_groups(_proxies()), "OTHER")
    assert other["chain"] == ["OTHER", "PROXY", "AUTO", "节点1"]
    assert other["exit"] == "节点1"


def test_fixed_auto_group_is_no_longer_automatic() -> None:
    proxies = _proxies()
    proxies["AUTO"]["fixed"] = "节点2"
    auto = _group(build_groups(proxies), "AUTO")
    assert auto["auto"] is False
    assert auto["fixed"] == "节点2"
    assert auto["exit"] == "节点2"
    assert auto["delay"] == 0
    pinned = next(item for item in auto["options"] if item["name"] == "节点2")
    assert pinned["selected"] is True


def test_dead_node_marks_unavailable() -> None:
    auto = _group(build_groups(_proxies()), "AUTO")
    dead = next(item for item in auto["options"] if item["name"] == "节点2")
    assert dead["alive"] is False
    assert dead["delay"] == 0


def test_group_cycle_does_not_loop_forever() -> None:
    proxies = {
        "A": {"type": "Selector", "now": "B", "all": ["B"], "alive": True},
        "B": {"type": "Selector", "now": "A", "all": ["A"], "alive": True},
    }
    groups = build_groups(proxies)
    assert len(groups) == 2
    assert _group(groups, "A")["chain"] == ["A", "B"]
