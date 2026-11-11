"""按设备统计 LAN 侧转发流量（只计数，不影响转发路径）。

做法：在 prerouting(-160) 统计"设备发出"，在 postrouting(0) 统计"发往设备"，
两个 hook 都只执行 `update @set`，不 accept/drop，因此不改变任何转发、代理
或分流行为。计数器挂在 nftables 动态集合的 per-element counter 上。

放在 prerouting 而不是 forward，是因为 TPROXY 会在 prerouting 把下行流量
截走到本地，forward 链根本看不到；下行放在 postrouting，能同时覆盖 TPROXY
转发的包和 Clash 本机生成的包。
"""

from __future__ import annotations

import glob
import ipaddress
import json
from pathlib import Path
from typing import Any

from ..config import Config
from ..exec import run

STAT_TABLE = "usbnic_stat"
LEASE_GLOBS = (
    "/var/lib/NetworkManager/dnsmasq-*.leases",
    "/var/lib/misc/dnsmasq.leases",
)


def stat_networks(config: Config) -> list[str]:
    """所有可能出现的下游网段（与当前模式无关，未使用的不会命中）。"""
    cidrs = [
        config.lan_cidr,
        config.hotspot_cidr,
        config.router_lan_cidr,
        config.router_5g_cidr,
        config.router_24g_cidr,
    ]
    seen: list[str] = []
    for cidr in cidrs:
        if cidr and cidr not in seen:
            seen.append(cidr)
    return seen


def stat_ruleset(config: Config) -> str:
    networks = ", ".join(stat_networks(config))
    return (
        f"table ip {STAT_TABLE} {{\n"
        "    set lan_nets {\n"
        "        type ipv4_addr\n"
        "        flags interval\n"
        f"        elements = {{ {networks} }}\n"
        "    }\n"
        "    set lan_tx {\n"
        "        type ipv4_addr\n"
        "        size 1024\n"
        "        flags dynamic\n"
        "        counter\n"
        "    }\n"
        "    set lan_rx {\n"
        "        type ipv4_addr\n"
        "        size 1024\n"
        "        flags dynamic\n"
        "        counter\n"
        "    }\n"
        "    chain pre {\n"
        "        type filter hook prerouting priority -160; policy accept;\n"
        "        ip saddr @lan_nets ip daddr != @lan_nets "
        "update @lan_tx { ip saddr }\n"
        "    }\n"
        "    chain post {\n"
        "        type filter hook postrouting priority 0; policy accept;\n"
        "        ip daddr @lan_nets ip saddr != @lan_nets "
        "update @lan_rx { ip daddr }\n"
        "    }\n"
        "}\n"
    )


def ensure(config: Config) -> dict[str, Any]:
    """幂等安装统计表；已存在则先删后建（清空旧计数器）。"""
    # 先尝试删除，避免同名表导致 add 失败
    run(["nft", "delete", "table", "ip", STAT_TABLE], timeout=3.0)
    rc, _out, err = run(
        ["nft", "-f", "-"], timeout=5.0, input_text=stat_ruleset(config)
    )
    return {"ok": rc == 0, "error": "" if rc == 0 else err.strip()}


def ensure_if_missing(config: Config) -> None:
    rc, _out, _err = run(["nft", "list", "table", "ip", STAT_TABLE], timeout=2.0)
    if rc != 0:
        ensure(config)


def remove() -> None:
    run(["nft", "delete", "table", "ip", STAT_TABLE], timeout=3.0)


def read_counters() -> dict[str, dict[str, int]]:
    """返回 {ip: {"tx": bytes, "rx": bytes}}；表不存在时返回空。"""
    rc, out, _err = run(["nft", "-j", "list", "table", "ip", STAT_TABLE], timeout=3.0)
    if rc != 0:
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    result: dict[str, dict[str, int]] = {}
    for item in data.get("nftables", []):
        element = item.get("set")
        if not element or element.get("name") not in ("lan_tx", "lan_rx"):
            continue
        direction = "tx" if element["name"] == "lan_tx" else "rx"
        for entry in element.get("elem") or []:
            address, counter = _parse_element(entry)
            if not address:
                continue
            bucket = result.setdefault(address, {"tx": 0, "rx": 0})
            bucket[direction] += int(counter.get("bytes") or 0)
    return result


def _parse_element(entry: Any) -> tuple[str, dict[str, Any]]:
    """兼容 nft -j 的两种元素形态：{"elem": {...}} 与 {"val": ..., "counter": ...}。"""
    payload = entry.get("elem") if isinstance(entry, dict) else None
    if isinstance(payload, dict):
        return str(payload.get("val") or ""), dict(payload.get("counter") or {})
    if isinstance(entry, dict):
        return str(entry.get("val") or ""), dict(entry.get("counter") or {})
    return "", {}


def _lan_devices(config: Config) -> set[str]:
    return {config.gateway_iface, config.router_lan_iface, config.hotspot_iface,
            config.router_5g_iface, config.router_24g_iface}


def _networks(config: Config) -> list[ipaddress.IPv4Network]:
    nets: list[ipaddress.IPv4Network] = []
    for cidr in stat_networks(config):
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return nets


def _in_lan(address: str, networks: list[ipaddress.IPv4Network]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def _neighbors(config: Config) -> dict[str, str]:
    """从 ARP/邻居表取 IP → MAC。"""
    rc, out, _err = run(["ip", "-j", "neigh"], timeout=3.0)
    result: dict[str, str] = {}
    if rc != 0:
        return result
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return result
    devices = _lan_devices(config)
    networks = _networks(config)
    for row in rows:
        ip = str(row.get("dst") or "")
        mac = str(row.get("lladdr") or "")
        dev = str(row.get("dev") or "")
        if not ip or not mac or dev not in devices:
            continue
        if not _in_lan(ip, networks):
            continue
        result[ip] = mac.lower()
    return result


def _leases() -> dict[str, dict[str, str]]:
    """读 dnsmasq 租约（root 可读）：IP → {mac, hostname}。"""
    result: dict[str, dict[str, str]] = {}
    for pattern in LEASE_GLOBS:
        for path in glob.glob(pattern):
            try:
                lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                parts = line.split()
                if len(parts) < 4:
                    continue
                ip, mac, host = parts[2], parts[1], parts[3]
                if host == "*":
                    host = ""
                result[ip] = {"mac": mac.lower(), "hostname": host}
    return result


def device_table(config: Config) -> list[dict[str, Any]]:
    """合并计数器、ARP 与 DHCP 租约，返回设备明细（不含速率）。"""
    counters = read_counters()
    neighbors = _neighbors(config)
    leases = _leases()
    networks = _networks(config)
    addresses = set(counters) | set(neighbors) | {
        ip for ip in leases if _in_lan(ip, networks)
    }
    items: list[dict[str, Any]] = []
    for ip in addresses:
        if not _in_lan(ip, networks):
            continue
        lease = leases.get(ip, {})
        items.append(
            {
                "ip": ip,
                "mac": neighbors.get(ip) or lease.get("mac", ""),
                "hostname": lease.get("hostname", ""),
                "tx_bytes": counters.get(ip, {}).get("tx", 0),
                "rx_bytes": counters.get(ip, {}).get("rx", 0),
            }
        )
    return items
