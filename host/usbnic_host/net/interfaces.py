from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import psutil

from ..config import Config
from ..exec import run, run_json, run_text
from ..util import normalize_mac, read_text

SYS_NET = Path("/sys/class/net")


def _carrier(name: str) -> bool | None:
    value = read_text(SYS_NET / name / "carrier")
    if value in ("0", "1"):
        return value == "1"
    return None


def _driver(name: str) -> str:
    link = SYS_NET / name / "device" / "driver"
    try:
        return link.resolve().name
    except OSError:
        return ""


def _usb_ids(name: str) -> str:
    base = SYS_NET / name / "device"
    try:
        real = base.resolve()
    except OSError:
        return ""
    for candidate in (real, real.parent):
        vendor = read_text(candidate / "idVendor")
        product = read_text(candidate / "idProduct")
        if vendor and product:
            return f"{vendor}:{product}"
    return ""


def _wifi_mode(name: str) -> str | None:
    text = run_text(["iw", "dev", name, "info"], timeout=1.0)
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("type "):
            return line.split(None, 1)[1].strip().lower()
    return None


def _wifi_link(name: str) -> dict[str, Any] | None:
    text = run_text(["iw", "dev", name, "link"], timeout=1.0)
    if not text or "Not connected" in text:
        return None
    info: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Connected to "):
            info["bssid"] = normalize_mac(line.split()[2])
        elif line.startswith("SSID:"):
            info["ssid"] = line.split(":", 1)[1].strip()
        elif line.startswith("signal:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    info["signal_dbm"] = int(parts[1])
                except ValueError:
                    pass
        elif line.startswith("rx bitrate:"):
            info["rx_rate_mbps"] = _parse_mbit(line)
        elif line.startswith("tx bitrate:"):
            info["tx_rate_mbps"] = _parse_mbit(line)
    return info or None


def _parse_mbit(line: str) -> float | None:
    parts = line.split()
    for index, part in enumerate(parts):
        if part.lower() == "mbit/s" and index > 0:
            try:
                return float(parts[index - 1])
            except ValueError:
                return None
    return None


def _ethtool(name: str) -> dict[str, Any]:
    text = run_text(["ethtool", name], timeout=1.0)
    if not text:
        return {}
    info: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Speed:"):
            raw = line.split(":", 1)[1].strip()
            if raw.endswith("Mb/s"):
                try:
                    info["speed_mbps"] = int(raw.split("Mb/s")[0])
                except ValueError:
                    pass
        elif line.startswith("Duplex:"):
            info["duplex"] = line.split(":", 1)[1].strip().lower()
        elif line.startswith("Link detected:"):
            info["link_detected"] = line.split(":", 1)[1].strip().lower() == "yes"
    return info


def _prefix_len(netmask: str | None, family: int) -> int:
    default = 32 if family == socket.AF_INET else 128
    if not netmask:
        return default
    try:
        if family == socket.AF_INET:
            packed = socket.inet_aton(netmask)
        else:
            packed = socket.inet_pton(socket.AF_INET6, netmask)
        return sum(bin(byte).count("1") for byte in packed)
    except OSError:
        return default


def _normalize_ip(value: str) -> str:
    return value.split("%", 1)[0]


def get_default_routes() -> list[dict[str, Any]]:
    data = run_json(["ip", "-j", "route", "show", "default"], timeout=1.5)
    if not isinstance(data, list):
        return []
    routes: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        route = {
            "dev": item.get("dev") or "",
            "gateway": item.get("gateway") or "",
            "metric": int(item.get("metric") or 0),
            "protocol": item.get("protocol") or "",
            "prefsrc": item.get("prefsrc") or "",
        }
        routes.append(route)
    routes.sort(key=lambda r: (r["metric"], r["dev"]))
    return routes


def _nm_connections() -> dict[str, str]:
    text = run_text(
        ["nmcli", "-t", "-f", "DEVICE,CONNECTION", "device", "status"],
        timeout=1.5,
    )
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            continue
        if ":" not in line:
            continue
        device, connection = line.split(":", 1)
        connection = connection.replace("\\:", ":").replace("\\\\", "\\")
        if device and connection and connection not in ("--", ""):
            result[device] = connection
    return result


def _duplex_text(value: int) -> str | None:
    if value == psutil.NIC_DUPLEX_FULL:
        return "full"
    if value == psutil.NIC_DUPLEX_HALF:
        return "half"
    return None


def _iface_type(name: str, has_device: bool) -> str:
    if name == "lo":
        return "loopback"
    if (SYS_NET / name / "phy80211").exists():
        return "wifi"
    lower = name.lower()
    if lower.startswith(("tun", "tap", "wg", "tailscale", "utun")):
        return "vpn"
    if has_device:
        return "ethernet"
    return "other"


def get_interfaces(config: Config | None = None) -> list[dict[str, Any]]:
    stats = psutil.net_if_stats()
    addresses = psutil.net_if_addrs()
    counters = psutil.net_io_counters(pernic=True)
    routes = get_default_routes()
    route_by_dev: dict[str, list[dict[str, Any]]] = {}
    for route in routes:
        route_by_dev.setdefault(route["dev"], []).append(route)
    nm_connections = _nm_connections()

    exclude = set(config.exclude_interfaces) if config else set()
    result: list[dict[str, Any]] = []

    for name in sorted(stats):
        if name == "lo" or name.startswith("p2p-"):
            continue
        stat = stats.get(name)
        if stat is None:
            continue
        addr_list = addresses.get(name, [])
        ipv4: list[str] = []
        ipv6: list[str] = []
        mac = ""
        for addr in addr_list:
            if addr.family == socket.AF_INET:
                prefix = _prefix_len(addr.netmask, socket.AF_INET)
                ipv4.append(f"{addr.address}/{prefix}")
            elif addr.family == socket.AF_INET6:
                address = _normalize_ip(addr.address)
                if address.startswith("fe80:") or address == "::1":
                    continue
                prefix = _prefix_len(addr.netmask, socket.AF_INET6)
                ipv6.append(f"{address}/{prefix}")
            elif addr.family == psutil.AF_LINK:
                mac = normalize_mac(addr.address)

        device_dir = SYS_NET / name / "device"
        driver = _driver(name)
        usb_id = _usb_ids(name)
        iface_type = _iface_type(name, device_dir.exists())
        wifi_mode = _wifi_mode(name) if iface_type == "wifi" else None
        link_info = _wifi_link(name) if wifi_mode == "managed" else None
        eth = _ethtool(name) if iface_type == "ethernet" and stat.isup else {}

        speed = stat.speed if stat.speed and stat.speed > 0 else eth.get("speed_mbps")
        duplex = _duplex_text(stat.duplex) or eth.get("duplex")
        dev_routes = route_by_dev.get(name, [])
        default_route = dev_routes[0] if dev_routes else None
        io = counters.get(name)

        result.append(
            {
                "name": name,
                "mac": mac,
                "type": iface_type,
                "driver": driver,
                "usb_id": usb_id,
                "state": read_text(SYS_NET / name / "operstate", "unknown"),
                "up": bool(stat.isup),
                "carrier": _carrier(name),
                "mtu": stat.mtu,
                "speed_mbps": speed,
                "duplex": duplex,
                "ipv4": ipv4,
                "ipv6": ipv6,
                "connection": nm_connections.get(name, ""),
                "wifi_mode": wifi_mode,
                "wifi_link": link_info,
                "is_default": bool(dev_routes),
                "default_gateway": default_route["gateway"] if default_route else "",
                "default_metric": default_route["metric"] if default_route else None,
                "default_routes": dev_routes,
                "excluded": name in exclude,
                "rx_bytes": io.bytes_recv if io else 0,
                "tx_bytes": io.bytes_sent if io else 0,
                "rx_packets": io.packets_recv if io else 0,
                "tx_packets": io.packets_sent if io else 0,
                "rx_errors": io.errin if io else 0,
                "tx_errors": io.errout if io else 0,
                "rx_dropped": io.dropin if io else 0,
                "tx_dropped": io.dropout if io else 0,
            }
        )

    return result


def get_interface_gateway(name: str) -> str:
    data = run_json(["ip", "-j", "route", "show", "dev", name], timeout=1.0)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("dst") == "default":
                return str(item.get("gateway") or "")
    return ""
