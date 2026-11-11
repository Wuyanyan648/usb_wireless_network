from __future__ import annotations

import ipaddress
import re
import time
from typing import Any

from ..config import Config
from ..util import ip_sort_key, normalize_mac
from .sources import (
    get_local_addresses,
    infer_vendor,
    is_local_mac,
    lookup_vendor,
    resolve_hostname,
    short_vendor,
)

HEXNAME_RE = re.compile(r"^[0-9a-f]{20,40}$", re.IGNORECASE)


def _local_label(iface: dict[str, Any] | None, ip: str) -> str:
    if ip.startswith("198.18."):
        return "本机（TUN 网关）"
    name = str((iface or {}).get("name") or "")
    kind = str((iface or {}).get("type") or "")
    if kind == "vpn":
        return "本机（VPN 网口）"
    if name.startswith("wl"):
        return "本机（无线网口）"
    if name.startswith(("en", "eth")):
        return "本机（USB 网口）"
    return "本机"


def _iface_for_ip(interfaces: list[dict[str, Any]], ip: str) -> str:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    best: tuple[int, str] | None = None
    for iface in interfaces:
        for cidr in iface.get("ipv4", []):
            try:
                network = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                continue
            if address not in network:
                continue
            if best is None or network.prefixlen > best[0]:
                best = (network.prefixlen, iface["name"])
    return best[1] if best else ""


def _device_type(iface_name: str, interfaces: dict[str, dict[str, Any]]) -> str:
    iface = interfaces.get(iface_name)
    if not iface:
        return "unknown"
    if iface.get("type") == "ethernet":
        return "wired"
    if iface.get("type") == "wifi":
        return "wifi-ap" if iface.get("wifi_mode") == "AP" else "upstream"
    if iface.get("type") == "vpn":
        return "vpn"
    return "unknown"


def _new_device(key: str, mac: str, ip: str, iface: str) -> dict[str, Any]:
    return {
        "key": key,
        "mac": mac,
        "ip": ip,
        "hostname": "",
        "name": "",
        "vendor": "",
        "type": "unknown",
        "interface": iface,
        "online": False,
        "last_seen": 0.0,
        "first_seen": 0.0,
        "sources": [],
        "signal_dbm": None,
        "signal_avg_dbm": None,
        "rx_rate_mbps": None,
        "tx_rate_mbps": None,
        "rx_bytes": None,
        "tx_bytes": None,
        "byte_source": "",
        "connected_seconds": None,
        "inactive_ms": None,
        "proxy_chains": [],
        "proxy_connections": 0,
        "proxy_upload": 0,
        "proxy_download": 0,
        "arp_state": "",
        "lease_expiry": 0,
        "is_router": False,
        "lease_only": True,
    }


def _add_source(device: dict[str, Any], source: str) -> None:
    if source not in device["sources"]:
        device["sources"].append(source)


def build_devices(
    interfaces: list[dict[str, Any]],
    leases: list[dict[str, Any]],
    neigh: list[dict[str, Any]],
    stations: list[dict[str, Any]],
    clash: dict[str, Any],
    config: Config,
    now_ts: float,
) -> dict[str, dict[str, Any]]:
    interfaces_by_name = {iface["name"]: iface for iface in interfaces}
    local_ips: set[str] = set()
    for iface in interfaces:
        for cidr in iface.get("ipv4", []):
            local_ips.add(cidr.split("/", 1)[0])

    devices: dict[str, dict[str, Any]] = {}
    chain_sets: dict[str, set[str]] = {}

    def ensure(mac: str, ip: str, iface: str) -> dict[str, Any]:
        key = mac if mac else f"ip:{ip}"
        device = devices.get(key)
        if device is None:
            device = _new_device(key, mac, ip, iface)
            devices[key] = device
            chain_sets[key] = set()
        else:
            if ip and not device.get("ip"):
                device["ip"] = ip
            if iface and not device.get("interface"):
                device["interface"] = iface
        return device

    # 1) DHCP 租约：给出 IP / 主机名，但不代表在线
    for lease in leases:
        mac = normalize_mac(lease.get("mac"))
        ip = str(lease.get("ip") or "")
        if not mac or not ip:
            continue
        iface = _iface_for_ip(interfaces, ip)
        device = ensure(mac, ip, iface)
        device["lease_only"] = False
        device["lease_expiry"] = int(lease.get("expiry") or 0)
        if lease.get("hostname"):
            device["hostname"] = str(lease["hostname"])
        _add_source(device, "dhcp")

    # 2) AP 关联：信号、速率、字节数、在线时长
    for station in stations:
        mac = normalize_mac(station.get("mac"))
        if not mac:
            continue
        device = ensure(mac, "", station.get("iface") or "")
        device["type"] = "wifi-ap"
        device["online"] = True
        device["last_seen"] = now_ts
        device["lease_only"] = False
        device["signal_dbm"] = station.get("signal_dbm")
        device["signal_avg_dbm"] = station.get("signal_avg_dbm")
        device["rx_rate_mbps"] = station.get("rx_rate_mbps")
        device["tx_rate_mbps"] = station.get("tx_rate_mbps")
        device["rx_bytes"] = station.get("rx_bytes")
        device["tx_bytes"] = station.get("tx_bytes")
        device["byte_source"] = "iw"
        device["connected_seconds"] = station.get("connected_seconds")
        device["inactive_ms"] = station.get("inactive_ms")
        _add_source(device, "iw")

    # 3) ARP / NDP 邻居：判断在线，补齐 IP
    for entry in neigh:
        mac = normalize_mac(entry.get("mac"))
        ip = str(entry.get("ip") or "")
        if not mac or not ip:
            continue
        iface = str(entry.get("dev") or "")
        device = ensure(mac, ip, iface)
        device["ip"] = ip
        device["interface"] = iface
        device["arp_state"] = str(entry.get("state") or "")
        device["is_router"] = bool(entry.get("router"))
        device["online"] = True
        device["last_seen"] = now_ts
        device["lease_only"] = False
        _add_source(device, "arp")
        if not device.get("type") or device["type"] == "unknown":
            device["type"] = _device_type(iface, interfaces_by_name)

    # 4) Clash 连接：按源 IP 关联设备，展示代理链路
    by_ip: dict[str, dict[str, Any]] = {}
    for device in devices.values():
        if device.get("ip"):
            by_ip[device["ip"]] = device

    if clash.get("ok"):
        for connection in clash.get("connections", []):
            source_ip = str(connection.get("source_ip") or "")
            if not source_ip or source_ip in local_ips:
                continue
            device = by_ip.get(source_ip)
            if device is None:
                device = ensure("", source_ip, "")
                device["type"] = "unknown"
                by_ip[source_ip] = device
            device["online"] = True
            device["last_seen"] = now_ts
            device["lease_only"] = False
            device["proxy_connections"] = int(device.get("proxy_connections") or 0) + 1
            device["proxy_upload"] = int(device.get("proxy_upload") or 0) + int(
                connection.get("upload") or 0
            )
            device["proxy_download"] = int(device.get("proxy_download") or 0) + int(
                connection.get("download") or 0
            )
            chains = connection.get("chains") or []
            if isinstance(chains, list):
                chain_sets[device["key"]].update(str(chain) for chain in chains if chain)
            _add_source(device, "clash")
            if device.get("byte_source") != "iw":
                device["rx_bytes"] = int(device.get("rx_bytes") or 0) + int(
                    connection.get("download") or 0
                )
                device["tx_bytes"] = int(device.get("tx_bytes") or 0) + int(
                    connection.get("upload") or 0
                )
                device["byte_source"] = "clash"

    # 4.5) 没有 DHCP 主机名时，尝试 mDNS / NetBIOS 补名称（带缓存，每轮最多 3 个）
    probes = 0
    for device in devices.values():
        if probes >= 3:
            break
        if device.get("hostname") or not device.get("online"):
            continue
        ip = device.get("ip") or ""
        if not ip or ":" in ip or ip in local_ips:
            continue
        name, source = resolve_hostname(ip)
        if name:
            device["hostname"] = name
            device["hostname_source"] = source
            probes += 1

    local_ifaces: dict[str, dict[str, Any]] = {}
    for iface in interfaces:
        for cidr in list(iface.get("ipv4") or []) + list(iface.get("ipv6") or []):
            local_ifaces[str(cidr).split("/", 1)[0]] = iface
    # 补齐 lo / Meta 等未出现在监控接口列表里的本机地址
    for ip, name in get_local_addresses().items():
        local_ifaces.setdefault(ip, {"name": name, "type": "vpn" if name == "Meta" else ""})

    # 5) 名称、厂商、代理链路
    for key, device in devices.items():
        mac = device.get("mac") or ""
        ip = device.get("ip") or ""
        vendor_full = lookup_vendor(mac, config.oui_file) if mac else ""
        local_iface = local_ifaces.get(ip) if ip else None
        device["is_local"] = bool(local_iface) or ip.startswith("198.18.")
        mac_random = bool(mac) and is_local_mac(mac)
        device["mac_random"] = mac_random

        hostname = (device.get("hostname") or "").strip()
        # 随机 MAC 查不到 OUI：先按主机名推断厂商（Apple/Android），再退到"随机 MAC"
        vendor_source = ""
        if not vendor_full and mac_random:
            vendor_full, vendor_source = infer_vendor(hostname)
        vendor_short = short_vendor(vendor_full) if vendor_full else ""
        device["vendor"] = vendor_short or ("随机 MAC（私有地址）" if mac_random else "厂商未收录")
        device["vendor_source"] = vendor_source
        device["mac_note"] = "随机 MAC（私有地址）" if mac_random else ""

        alias = config.device_aliases.get(mac.lower()) if mac else ""
        source = ""
        if device["is_local"]:
            name = _local_label(local_iface, ip)
            source = "local"
        elif alias:
            name, source = alias, "alias"
            device["alias"] = alias
        elif "nintendo" in vendor_full.lower():
            name, source = "Nintendo Switch", "vendor"
        elif hostname and HEXNAME_RE.match(hostname):
            name, source = hostname, "self"
            device["name_note"] = "设备自报名称（随机串）"
        elif hostname and hostname != "*":
            name, source = hostname, "hostname"
        elif device.get("is_router"):
            name, source = "路由器 / 网关", "router"
        elif not mac and ":" in ip:
            name, source = "仅 IPv6，无法识别厂商", "ipv6"
            device["name_note"] = "该地址没有 MAC，无法查厂商或主机名"
        elif vendor_short:
            name, source = f"{vendor_short} 设备", "vendor"
        elif mac_random:
            name, source = "设备（随机 MAC）", "mac"
            device["name_note"] = "MAC 未在 IEEE 注册，且没有可用的主机名"
        else:
            name, source = "设备（MAC 未收录）", "unknown"
        device["name"] = name
        device["name_source"] = source

        device["proxy_chains"] = sorted(chain_sets.get(key, set()))
        if not device.get("type"):
            device["type"] = "unknown"
        if not device.get("interface"):
            device["interface"] = _iface_for_ip(interfaces, device.get("ip") or "")

    if config.hide_local_addresses:
        devices = {key: dev for key, dev in devices.items() if not dev.get("is_local")}

    return devices


class DeviceTracker:
    """保留短暂离线设备，避免设备在列表中闪烁。"""

    def __init__(self, offline_after: float = 180.0) -> None:
        self.offline_after = offline_after
        self._previous: dict[str, dict[str, Any]] = {}

    def update(
        self, current: dict[str, dict[str, Any]], now_ts: float | None = None
    ) -> list[dict[str, Any]]:
        now_ts = now_ts or time.time()
        merged: dict[str, dict[str, Any]] = {}

        for key, device in current.items():
            previous = self._previous.get(key)
            if previous:
                device["first_seen"] = previous.get("first_seen") or now_ts
                if not device.get("last_seen"):
                    device["last_seen"] = previous.get("last_seen") or 0
            else:
                device["first_seen"] = device.get("first_seen") or now_ts
            self._previous[key] = device
            merged[key] = device

        for key, previous in list(self._previous.items()):
            if key in merged:
                continue
            last_seen = previous.get("last_seen") or previous.get("first_seen") or now_ts
            if now_ts - last_seen <= self.offline_after:
                offline = dict(previous)
                offline["online"] = False
                offline["proxy_connections"] = 0
                offline["proxy_chains"] = []
                merged[key] = offline
                self._previous[key] = offline
            else:
                self._previous.pop(key, None)

        items = list(merged.values())
        items.sort(
            key=lambda item: (
                0 if item.get("online") else 1,
                ip_sort_key(item.get("ip") or "255.255.255.255"),
                item.get("key") or "",
            )
        )
        return items
