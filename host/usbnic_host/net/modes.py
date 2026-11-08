from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Config
from ..exec import run
from ..util import read_text
from ..monitor.proxy import set_proxy

MODES: dict[str, dict[str, str]] = {
    "client": {
        "label": "默认客户端",
        "desc": "有线优先 / WiFi 兜底，不做 NAT",
    },
    "hotspot": {
        "label": "USB WiFi AP",
        "desc": "板载 WiFi 上网 + USB 开热点；本机与下游都走代理",
    },
    "gateway": {
        "label": "家庭网关",
        "desc": "光猫 → 有线 → USB 共享；本机与下游都走代理",
    },
}

# 每种模式开始时是否自动开启透明代理
MODE_PROXY = {"client": False, "hotspot": True, "gateway": True}


def detect_mode(config: Config, interfaces: list[dict[str, Any]]) -> str:
    connections = {iface.get("connection") for iface in interfaces}
    if config.gateway_connection in connections:
        return "gateway"
    if config.hotspot_connection in connections:
        return "hotspot"
    return "client"


def mode_info(config: Config, interfaces: list[dict[str, Any]]) -> dict[str, Any]:
    current = detect_mode(config, interfaces)
    return {
        "current": current,
        "label": MODES[current]["label"],
        "items": [
            {
                "key": key,
                "label": spec["label"],
                "desc": spec["desc"],
                "active": key == current,
                "proxy": MODE_PROXY[key],
            }
            for key, spec in MODES.items()
        ],
    }


def _step(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def _down(name: str) -> dict[str, Any]:
    rc, out, err = run(["nmcli", "connection", "down", name], timeout=15.0)
    if rc == 0:
        return _step(name, True, "已断开")
    text = f"{out}\n{err}".lower()
    if "not active" in text or "not an active" in text:
        return _step(name, True, "本来未启用")
    return _step(name, False, (err or out).strip())


def _up(name: str) -> dict[str, Any]:
    rc, out, err = run(
        ["nmcli", "--wait", "20", "connection", "up", name], timeout=25.0
    )
    if rc == 0:
        return _step(name, True, "已启用")
    return _step(name, False, (err or out).strip())


def _carrier(iface: str) -> bool:
    if not iface:
        return False
    return read_text(Path("/sys/class/net") / iface / "carrier") == "1"


def _ensure_uplink(config: Config) -> dict[str, Any]:
    iface = ""
    for name in config.uplink_candidates:
        if _carrier(name):
            iface = name
            break
    if not iface:
        return _step(config.uplink_connection, True, "未检测到网线，使用 WiFi 兜底")
    return _up(config.uplink_connection)


def _autoconnect(name: str, enable: bool) -> dict[str, Any]:
    """同步 NM 自动连接，避免离开家后网关/热点被自动拉起。"""
    value = "yes" if enable else "no"
    rc, out, err = run(
        ["nmcli", "connection", "modify", name, "connection.autoconnect", value],
        timeout=5.0,
    )
    return _step(
        f"{name}-autoconnect",
        rc == 0,
        ("" if rc == 0 else (err or out).strip()) or f"autoconnect={value}",
    )


def _enable_forward(config: Config) -> dict[str, Any]:
    rc, _out, err = run(
        ["sysctl", "-w", "net.ipv4.ip_forward=1"], timeout=3.0
    )
    return _step("ip_forward", rc == 0, err.strip() if rc != 0 else "已开启 IPv4 转发")


def apply_mode(
    config: Config, mode: str, proxy: bool | None = None
) -> dict[str, Any]:
    """应用网络模式。

    proxy 为 None 时按模式默认值决定是否开启代理；为 False 时可显式保持关闭
    （开机自动进入家庭网关时使用），此时 Clash 未运行也不视为失败。
    """
    if mode not in MODES:
        return {"ok": False, "mode": mode, "error": "未知模式", "steps": []}

    steps: list[dict[str, Any]] = []
    if mode == "client":
        steps.append(_autoconnect(config.gateway_connection, False))
        steps.append(_autoconnect(config.hotspot_connection, False))
        steps.append(_down(config.gateway_connection))
        steps.append(_down(config.hotspot_connection))
        steps.append(_ensure_uplink(config))
    elif mode == "hotspot":
        steps.append(_autoconnect(config.gateway_connection, False))
        steps.append(_autoconnect(config.hotspot_connection, True))
        steps.append(_down(config.gateway_connection))
        steps.append(_up(config.hotspot_connection))
    else:
        steps.append(_autoconnect(config.gateway_connection, True))
        steps.append(_autoconnect(config.hotspot_connection, False))
        steps.append(_down(config.hotspot_connection))
        steps.append(_enable_forward(config))
        steps.append(_up(config.gateway_connection))

    proxy_enable = MODE_PROXY[mode] if proxy is None else proxy
    proxy_result = set_proxy(config, proxy_enable, mode)
    proxy_ok = bool(proxy_result.get("ok"))
    if proxy is False:
        # 开机默认不开代理；Clash 还没启动导致关闭失败不算模式失败
        steps.append(
            _step(
                "proxy",
                True,
                "" if proxy_ok else "Clash 未运行，跳过关闭代理",
            )
        )
    else:
        steps.append(
            _step(
                "proxy",
                proxy_ok,
                "" if proxy_ok else str(proxy_result.get("error") or "代理切换失败"),
            )
        )

    ok = all(step["ok"] for step in steps)
    return {"ok": ok, "mode": mode, "steps": steps, "proxy": proxy_result}
