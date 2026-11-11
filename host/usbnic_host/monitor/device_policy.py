"""设备级代理策略：MAC 是设备身份，IP 只是 ARP / DHCP 租约给出的瞬时映射。

策略只有 `proxy`（下游流量交给 TPROXY）和 `direct`（普通 NAT）两种，另有
一个全局默认（默认 `proxy`：总开关打开后所有下游设备默认走代理，用户显式
点成 direct 的设备才直连）。运行期设置写入 `data_dir/device_policies.json`；
host.yaml 里的 `device_policies` 只作为文件缺失时的初始值。

装表时只把"当前策略为 proxy"的设备的 IP 放进 nft 集合，因此 DHCP 换 IP
不会把策略带错；集合由吞吐采样线程按 ARP/租约变化周期刷新。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import Config
from ..util import ip_sort_key, normalize_mac

POLICY_PROXY = "proxy"
POLICY_DIRECT = "direct"
POLICIES = (POLICY_PROXY, POLICY_DIRECT)


def _path(config: Config) -> Path:
    name = config.device_policies_file or "device_policies.json"
    path = Path(name).expanduser()
    return path if path.is_absolute() else config.data_dir / path


def _clean(value: Any, fallback: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in POLICIES else fallback


def load(config: Config) -> dict[str, Any]:
    """读策略文件；文件缺失或损坏时回落到 host.yaml 的初始值。"""
    raw: dict[str, Any] = {}
    try:
        loaded = json.loads(_path(config).read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    except (OSError, json.JSONDecodeError):
        raw = {}
    default = _clean(raw.get("default"), _clean(config.tproxy_default_policy, POLICY_PROXY))
    devices = dict(config.device_policies)
    for mac, policy in (raw.get("devices") or {}).items():
        key = normalize_mac(str(mac))
        if key:
            devices[key] = _clean(policy, default)
    return {"default": default, "devices": devices}


def save(config: Config, data: dict[str, Any]) -> None:
    try:
        _path(config).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def default_policy(config: Config) -> str:
    return str(load(config)["default"])


def set_default(config: Config, policy: str) -> dict[str, Any]:
    text = str(policy or "").strip().lower()
    if text not in POLICIES:
        return {"ok": False, "error": f"未知策略: {policy}"}
    data = load(config)
    data["default"] = text
    save(config, data)
    return {"ok": True, "error": "", "default": text}


def set_device(config: Config, mac: str, policy: str) -> dict[str, Any]:
    key = normalize_mac(mac)
    if not key:
        return {"ok": False, "error": "无效的 MAC 地址"}
    text = str(policy or "").strip().lower()
    if text not in POLICIES:
        return {"ok": False, "error": f"未知策略: {policy}"}
    data = load(config)
    data["devices"][key] = text
    save(config, data)
    return {"ok": True, "error": "", "mac": key, "policy": text}


def resolve(config: Config, mac: str, data: dict[str, Any] | None = None) -> str:
    policy_data = data or load(config)
    key = normalize_mac(mac)
    if not key:
        return str(policy_data["default"])
    return str(policy_data["devices"].get(key, policy_data["default"]))


def assignments(
    config: Config, items: list[dict[str, Any]] | None = None
) -> tuple[list[str], list[str]]:
    """返回（走代理的当前 IP, 直连的当前 IP），用于生成 nft 集合。"""
    data = load(config)
    if items is None:
        from . import device_stats

        items = device_stats.device_table(config)
    proxy_ips: list[str] = []
    direct_ips: list[str] = []
    for item in items:
        ip = str(item.get("ip") or "")
        mac = normalize_mac(str(item.get("mac") or ""))
        if not ip or not mac:
            continue
        # 本机地址（USB 网口 / TUN / VPN）永远不进下游集合，主机走自己的 TUN 开关
        if item.get("is_local"):
            continue
        target = proxy_ips if resolve(config, mac, data) == POLICY_PROXY else direct_ips
        target.append(ip)
    return (
        sorted(set(proxy_ips), key=ip_sort_key),
        sorted(set(direct_ips), key=ip_sort_key),
    )


def table(config: Config, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """页面用的设备策略列表：在现有设备明细上叠加策略与生效状态。"""
    data = load(config)
    if items is None:
        from . import device_stats

        items = device_stats.device_table(config)
    active = _tproxy_active()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        ip = str(item.get("ip") or "")
        mac = normalize_mac(str(item.get("mac") or ""))
        if not ip or not mac:
            continue
        seen.add(mac)
        rows.append(_row(config, data, mac, ip, str(item.get("hostname") or ""), active))
    # 离线但设置过策略的设备也要出现，否则开关会"消失"
    for mac, policy in data["devices"].items():
        if mac in seen:
            continue
        rows.append(
            {
                "ip": "",
                "mac": mac,
                "hostname": "",
                "name": config.device_aliases.get(mac, ""),
                "policy": policy,
                "effective": policy if active else POLICY_DIRECT,
                "online": False,
            }
        )
    rows.sort(key=lambda row: (ip_sort_key(row["ip"] or "255.255.255.255"), row["mac"]))
    return {"ok": True, "default": data["default"], "active": active, "items": rows}


def _row(
    config: Config,
    data: dict[str, Any],
    mac: str,
    ip: str,
    hostname: str,
    active: bool,
) -> dict[str, Any]:
    policy = str(data["devices"].get(mac, data["default"]))
    return {
        "ip": ip,
        "mac": mac,
        "hostname": hostname,
        "name": config.device_aliases.get(mac) or hostname,
        "policy": policy,
        # 没装 TPROXY 表时所有设备实际都是直连，避免页面显示"已走代理"
        "effective": policy if active else POLICY_DIRECT,
        "online": True,
    }


def _tproxy_active() -> bool:
    from . import proxy

    return proxy.tproxy_installed()
