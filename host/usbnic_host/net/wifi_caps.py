"""无线接口的 AP 能力探测（只读）。

依据 `iw phy <phy> info` 的接口模式列表与信道标志判断"能不能在目标信道起 AP"，
不发起任何关联或扫描，也不需要 root。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..exec import run

_FREQ_RE = re.compile(r"^\s*\*\s+(\d+(?:\.\d+)?)\s+MHz\s+\[(\d+)\](.*)$")
# iwlwifi 在 5GHz 起 AP 会在 wpa_supplicant 阶段失败（实测 ch36/ch149 均不通）
_DRIVERS_NO_5G_AP = {"iwlwifi"}


def wifi_interfaces() -> list[str]:
    result: list[str] = []
    for path in sorted(Path("/sys/class/net").glob("*/phy80211")):
        result.append(path.parent.name)
    return result


def _phy_of(iface: str) -> str:
    link = Path(f"/sys/class/net/{iface}/phy80211")
    try:
        return link.resolve().name
    except OSError:
        return ""


def _driver_of(iface: str) -> str:
    link = Path(f"/sys/class/net/{iface}/device/driver")
    try:
        return link.resolve().name
    except OSError:
        return ""


def _phy_info(phy: str) -> str:
    rc, out, _err = run(["iw", "phy", phy, "info"], timeout=5.0)
    return out if rc == 0 else ""


def _ap_supported(info: str) -> bool:
    in_modes = False
    for line in info.splitlines():
        stripped = line.strip()
        if stripped.startswith("Supported interface modes"):
            in_modes = True
            continue
        if in_modes:
            if stripped.startswith("*"):
                if stripped[1:].strip() == "AP":
                    return True
            elif stripped and not stripped.startswith("*"):
                in_modes = False
    return False


def _channel_flags(info: str, channel: int) -> tuple[int, list[str], bool] | None:
    for line in info.splitlines():
        match = _FREQ_RE.match(line)
        if not match:
            continue
        if int(match.group(2)) != channel:
            continue
        frequency = int(float(match.group(1)))
        tail = match.group(3).lower()
        flags = re.findall(r"\(([^)]+)\)", tail)
        blocked = any(
            "no ir" in flag or "disabled" in flag or "passive" in flag
            for flag in flags
        )
        return frequency, flags, blocked
    return None


def regdomain() -> str:
    rc, out, _err = run(["iw", "reg", "get"], timeout=3.0)
    if rc != 0:
        return ""
    for line in out.splitlines():
        if line.strip().startswith("country"):
            return line.strip()
    return ""


def capability(iface: str, channel: int) -> dict[str, Any]:
    """判断某个无线接口能否在指定信道起 AP，并给出不可用原因。"""
    iface = (iface or "").strip()
    phy = _phy_of(iface) if iface else ""
    driver = _driver_of(iface) if iface else ""
    result: dict[str, Any] = {
        "iface": iface,
        "phy": phy,
        "driver": driver,
        "channel": channel,
        "frequency": None,
        "ap_supported": False,
        "flags": [],
        "available": False,
        "reasons": [],
    }
    if not iface or not phy:
        result["reasons"].append("无线接口不存在")
        return result
    info = _phy_info(phy)
    if not info:
        result["reasons"].append("读取 iw phy 信息失败（需要 iw 工具）")
        return result

    result["ap_supported"] = _ap_supported(info)
    found = _channel_flags(info, channel)
    if found is None:
        result["reasons"].append(f"信道 {channel} 不在网卡支持列表")
    else:
        frequency, flags, blocked = found
        result["frequency"] = frequency
        result["flags"] = flags
        if blocked:
            result["reasons"].append(
                f"信道 {channel} 被标记为 no IR/disabled（监管域限制）"
            )
        if frequency >= 5000 and driver in _DRIVERS_NO_5G_AP:
            result["reasons"].append(
                f"{driver} 固件无法启动 5GHz AP（实测 ch36/ch149 均在 wpa_supplicant 阶段失败）"
            )
        if frequency >= 5000 and not blocked and "country 00" in regdomain():
            result["reasons"].append("监管域为 country 00，5GHz 被动扫描，不能主动发射")

    if not result["ap_supported"]:
        result["reasons"].append("驱动未上报 AP 模式")
    result["available"] = bool(result["ap_supported"]) and not result["reasons"]
    return result


def describe(capability: dict[str, Any]) -> str:
    if capability.get("available"):
        return f"{capability['iface']} 可在信道 {capability['channel']} 起 AP"
    iface = capability.get("iface") or "未配置"
    reasons = "；".join(capability.get("reasons") or ["能力未知"])
    return f"{iface} 不可用：{reasons}"


def resolve(preferred: str, channel: int, require_5g: bool = True) -> dict[str, Any]:
    """优先用配置指定的网卡；留空或不可用时按能力自动挑一块。"""
    preferred = (preferred or "").strip()
    if preferred:
        cap = capability(preferred, channel)
        return {"iface": preferred if cap["available"] else "", "capability": cap}
    for iface in wifi_interfaces():
        cap = capability(iface, channel)
        if cap["available"]:
            return {"iface": iface, "capability": cap}
    # 一个都不可用：把第一块无线卡的原因带回去，便于页面展示
    ifaces = wifi_interfaces()
    cap = capability(ifaces[0], channel) if ifaces else {"iface": "", "available": False,
                                                         "reasons": ["没有无线网卡"]}
    return {"iface": "", "capability": cap}
