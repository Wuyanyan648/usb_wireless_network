from __future__ import annotations

import ipaddress
import re
import socket
import time
from pathlib import Path
from typing import Any

MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}$")


def now() -> float:
    return time.time()


def read_text(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def normalize_mac(value: str | None) -> str:
    if not value:
        return ""
    mac = value.strip().lower().replace("-", ":")
    return mac if MAC_RE.match(mac) else ""


def is_ipv4(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def ip_sort_key(value: str) -> tuple[int, str]:
    try:
        addr = ipaddress.ip_address(value)
        return int(addr), value
    except ValueError:
        return 1 << 64, value


def parse_ping(output: str) -> dict[str, Any]:
    """解析 ping -c1 的输出，返回成功、延迟和丢包率。"""
    result: dict[str, Any] = {
        "ok": False,
        "rtt_ms": None,
        "loss_pct": None,
    }
    loss_match = re.search(r"(\d+(?:\.\d+)?)%\s*packet loss", output)
    if loss_match:
        result["loss_pct"] = float(loss_match.group(1))
    rtt_match = re.search(r"time[=<]\s*([\d.]+)\s*ms", output)
    if rtt_match:
        result["rtt_ms"] = float(rtt_match.group(1))
    if "1 received" in output or (result["loss_pct"] is not None and result["loss_pct"] < 100):
        result["ok"] = True
    if rtt_match:
        result["ok"] = True
    return result


def uptime_seconds() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def load_average() -> list[float]:
    try:
        return [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]]
    except (OSError, ValueError):
        return []


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def safe_div(value: float, divisor: float) -> float:
    return value / divisor if divisor else 0.0
