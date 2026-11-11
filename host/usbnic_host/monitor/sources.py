from __future__ import annotations

import glob
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..config import Config
from ..exec import run_json, run_text
from ..util import normalize_mac

DHCP_GLOBS = (
    "/var/lib/NetworkManager/dnsmasq-*.leases",
    "/var/lib/misc/dnsmasq.leases",
)

OUI_RE = re.compile(
    r"^([0-9A-Fa-f]{2})-([0-9A-Fa-f]{2})-([0-9A-Fa-f]{2})\s+\(hex\)\s+(.+?)\s*$"
)

_OUI_CACHE: dict[str, dict[str, str]] = {}

# 本机 OUI 库可能落后于新设备，这里补少量已知前缀。
OUI_OVERRIDES = {
    "4044f7": "Nintendo Co.,Ltd",
}


def parse_dhcp_leases(paths: list[str]) -> list[dict[str, Any]]:
    leases: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            expiry_raw, mac_raw, ip = parts[0], parts[1], parts[2]
            mac = normalize_mac(mac_raw)
            if not mac:
                continue
            key = (mac, ip)
            if key in seen:
                continue
            seen.add(key)
            hostname = parts[3] if len(parts) >= 4 else ""
            if hostname in ("*", "-"):
                hostname = ""
            try:
                expiry = int(expiry_raw)
            except ValueError:
                expiry = 0
            leases.append(
                {
                    "mac": mac,
                    "ip": ip,
                    "hostname": hostname,
                    "expiry": expiry,
                    "source": path,
                }
            )
    return leases


def get_dhcp_leases() -> list[dict[str, Any]]:
    paths: list[str] = []
    for pattern in DHCP_GLOBS:
        paths.extend(glob.glob(pattern))
    return parse_dhcp_leases(sorted(set(paths)))


def get_neigh() -> list[dict[str, Any]]:
    data = run_json(["ip", "-j", "neigh", "show"], timeout=1.5)
    if not isinstance(data, list):
        return []
    result: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ip = str(item.get("dst") or "")
        mac = normalize_mac(item.get("lladdr"))
        dev = str(item.get("dev") or "")
        state_list = item.get("state") or []
        state = state_list[0] if isinstance(state_list, list) and state_list else ""
        if not ip or not mac or not dev:
            continue
        if ":" in ip:
            continue
        if state in ("FAILED", "INCOMPLETE"):
            continue
        if ip.startswith("224.") or ip.startswith("239.") or ip == "255.255.255.255":
            continue
        result.append(
            {
                "ip": ip,
                "mac": mac,
                "dev": dev,
                "state": state,
                "router": bool(item.get("router")),
            }
        )
    return result


def parse_iw_station_dump(text: str, iface: str = "") -> list[dict[str, Any]]:
    stations: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def finish() -> None:
        nonlocal current
        if current and current.get("mac"):
            stations.append(current)
        current = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("Station "):
            finish()
            parts = line.split()
            current = {
                "mac": normalize_mac(parts[1]) if len(parts) > 1 else "",
                "iface": iface,
                "signal_dbm": None,
                "signal_avg_dbm": None,
                "rx_bytes": None,
                "tx_bytes": None,
                "rx_rate_mbps": None,
                "tx_rate_mbps": None,
                "connected_seconds": None,
                "inactive_ms": None,
            }
            continue
        if current is None:
            continue
        if line.startswith("signal:"):
            values = line.split()
            if len(values) >= 2:
                current["signal_dbm"] = _to_int(values[1])
        elif line.startswith("signal avg:"):
            values = line.split()
            if len(values) >= 3:
                current["signal_avg_dbm"] = _to_int(values[2])
        elif line.startswith("rx bytes:"):
            current["rx_bytes"] = _to_int(line.split(":", 1)[1].strip())
        elif line.startswith("tx bytes:"):
            current["tx_bytes"] = _to_int(line.split(":", 1)[1].strip())
        elif line.startswith("rx bitrate:"):
            current["rx_rate_mbps"] = _to_float(line.split(":", 1)[1].strip())
        elif line.startswith("tx bitrate:"):
            current["tx_rate_mbps"] = _to_float(line.split(":", 1)[1].strip())
        elif line.startswith("connected time:"):
            current["connected_seconds"] = _to_int(line.split(":", 1)[1].strip())
        elif line.startswith("inactive time:"):
            current["inactive_ms"] = _to_int(line.split(":", 1)[1].strip())
    finish()
    return stations


def get_wifi_stations(iface: str) -> list[dict[str, Any]]:
    text = run_text(["iw", "dev", iface, "station", "dump"], timeout=1.5)
    return parse_iw_station_dump(text, iface)


def _to_int(value: str) -> int | None:
    try:
        return int(float(str(value).split()[0]))
    except (TypeError, ValueError, IndexError):
        return None


def _to_float(value: str) -> float | None:
    try:
        return float(str(value).split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def _clean_ip(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("::ffff:"):
        return text[7:]
    return text


def get_clash(config: Config) -> dict[str, Any]:
    url = f"{config.clash_api}/connections"
    headers = {"Accept": "application/json"}
    if config.clash_secret:
        headers["Authorization"] = f"Bearer {config.clash_secret}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=config.http_timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "error": str(exc),
            "connections": [],
            "download_total": 0,
            "upload_total": 0,
            "memory": 0,
        }

    raw_connections = payload.get("connections") or []
    connections: list[dict[str, Any]] = []
    if isinstance(raw_connections, list):
        for item in raw_connections:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") or {}
            connections.append(
                {
                    "id": item.get("id") or "",
                    "source_ip": _clean_ip(metadata.get("sourceIP")),
                    "source_port": metadata.get("sourcePort") or "",
                    "destination_ip": metadata.get("destinationIP") or "",
                    "destination_port": metadata.get("destinationPort") or "",
                    "host": metadata.get("host") or "",
                    "network": metadata.get("network") or "",
                    "type": metadata.get("type") or "",
                    "rule": item.get("rule") or "",
                    "rule_payload": item.get("rulePayload") or "",
                    "chains": item.get("chains") or [],
                    "upload": int(item.get("upload") or 0),
                    "download": int(item.get("download") or 0),
                    "start": item.get("start") or "",
                }
            )

    return {
        "ok": True,
        "error": "",
        "connections": connections,
        "download_total": int(payload.get("downloadTotal") or 0),
        "upload_total": int(payload.get("uploadTotal") or 0),
        "memory": int(payload.get("memory") or 0),
    }


def load_oui(path: str) -> dict[str, str]:
    cached = _OUI_CACHE.get(path)
    if cached is not None:
        return cached
    table: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                match = OUI_RE.match(line)
                if not match:
                    continue
                prefix = f"{match.group(1)}{match.group(2)}{match.group(3)}".lower()
                table[prefix] = match.group(4).strip()
    except OSError:
        pass
    _OUI_CACHE[path] = table
    return table


def lookup_vendor(mac: str, oui_file: str) -> str:
    normalized = normalize_mac(mac)
    if not normalized:
        return ""
    prefix = normalized.replace(":", "")[:6]
    if prefix in OUI_OVERRIDES:
        return OUI_OVERRIDES[prefix]
    table = load_oui(oui_file)
    if not table:
        # 配置路径不存在时退回常见的离线 OUI 库，不联网
        for candidate in (
            "/usr/share/ieee-data/oui.txt",
            "/usr/share/misc/oui.txt",
            "/var/lib/ieee-data/oui.txt",
        ):
            if candidate != oui_file:
                table = load_oui(candidate)
                if table:
                    break
    return table.get(prefix, "")


def is_local_mac(mac: str) -> bool:
    """本地管理地址（随机/私有 MAC）：首字节 bit1 = 1。"""
    normalized = normalize_mac(mac)
    if not normalized:
        return False
    try:
        return bool(int(normalized.split(":", 1)[0], 16) & 0x02)
    except ValueError:
        return False


APPLE_HOST_RE = re.compile(
    r"iphone|ipad|ipod|macbook|imac|mac-?mini|mac-?pro|apple-?tv|homepod|airpods|apple",
    re.IGNORECASE,
)
ANDROID_HOST_RE = re.compile(
    r"android|galaxy|sm-[a-z0-9]|redmi|pixel|oneplus|honor|huawei-|mi-?[0-9]",
    re.IGNORECASE,
)

_HOSTNAME_CACHE: dict[str, tuple[float, str, str]] = {}


def infer_vendor(hostname: str) -> tuple[str, str]:
    """随机 MAC 查不到 OUI 时，用主机名推断厂商。返回 (厂商, 来源)。"""
    name = (hostname or "").strip()
    if not name:
        return "", ""
    if APPLE_HOST_RE.search(name):
        return "Apple Inc.", "hostname"
    if ANDROID_HOST_RE.search(name):
        return "Android", "hostname"
    return "", ""


def _mdns_name(ip: str, timeout: float) -> str:
    """优先 avahi-resolve，其次 avahi 的 D-Bus ResolveAddress；未安装则跳过。"""
    import shutil
    import subprocess

    binary = shutil.which("avahi-resolve")
    if binary:
        try:
            out = subprocess.run([binary, "-a", ip], capture_output=True, text=True,
                                 timeout=timeout).stdout.strip()
            if out:
                return out.split()[-1]
        except (OSError, subprocess.SubprocessError):
            pass
        return ""
    if shutil.which("dbus-send"):
        try:
            out = subprocess.run(
                ["dbus-send", "--system", "--print-reply",
                 "--dest=org.freedesktop.Avahi", "/",
                 "org.freedesktop.Avahi.Server.ResolveAddress",
                 "int32:-1", "int32:0", "string:" + ip, "uint32:0"],
                capture_output=True, text=True, timeout=timeout).stdout
            for line in out.splitlines():
                if 'string "' in line:
                    value = line.split('string "', 1)[1].rstrip('"')
                    if value and value != ip:
                        return value
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def _netbios_name(ip: str, timeout: float) -> str:
    """nmblookup 存在时查 NetBIOS 名称（samba-common）；没有就跳过。"""
    import shutil
    import subprocess

    binary = shutil.which("nmblookup")
    if not binary:
        return ""
    try:
        out = subprocess.run([binary, "-A", ip], capture_output=True, text=True,
                             timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        if "<00>" in line and "GROUP" not in line.upper():
            return line.split("<00>", 1)[0].strip()
    return ""


def get_local_addresses() -> dict[str, str]:
    """返回本机全部接口地址 {ip: ifname}，包含 lo / Meta / 隧道口。

    设备表用它与下游设备区分，避免把本机网关地址当成陌生设备。
    """
    try:
        proc = subprocess.run(["ip", "-j", "addr", "show"], capture_output=True,
                              text=True, timeout=3)
        links = json.loads(proc.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    result = {}
    for link in links:
        name = link.get("ifname") or ""
        for info in link.get("addr_info") or []:
            local = info.get("local")
            if local and name:
                result[local] = name
    return result


def resolve_hostname(ip: str, timeout: float = 0.8) -> tuple[str, str]:
    """mDNS → NetBIOS 的主机名补充；结果缓存 5 分钟，失败缓存 1 分钟。"""
    if not ip or ":" in ip:
        return "", ""            # 只对 IPv4 做主动探测，IPv6 交给 NDP/租约
    now = time.time()
    cached = _HOSTNAME_CACHE.get(ip)
    if cached and now < cached[0]:
        return cached[1], cached[2]
    name = ""
    source = ""
    mdns = _mdns_name(ip, timeout)
    if mdns:
        name, source = mdns, "mdns"
    else:
        nb = _netbios_name(ip, timeout)
        if nb:
            name, source = nb, "netbios"
    ttl = 300.0 if name else 60.0
    _HOSTNAME_CACHE[ip] = (now + ttl, name, source)
    return name, source


def short_vendor(org: str) -> str:
    if not org:
        return "未知厂商"
    lower = org.lower()
    known = {
        "nintendo": "Nintendo",
        "apple": "Apple",
        "samsung": "Samsung",
        "huawei": "Huawei",
        "xiaomi": "Xiaomi",
        "intel": "Intel",
        "realtek": "Realtek",
        "tp-link": "TP-Link",
        "tenda": "Tenda",
        "mercury": "Mercury",
        "netgear": "Netgear",
        "sony": "Sony",
        "microsoft": "Microsoft",
    }
    for key, name in known.items():
        if key in lower:
            return name
    return org[:42]
