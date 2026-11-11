from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Config
from ..exec import run
from ..util import read_text
from ..monitor.proxy import proxy_mark, set_proxy
from .interfaces import invalidate_nm_cache
from .router import apply_router, teardown_router

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
    "router": {
        "label": "移动路由器",
        "desc": "有线扩展口 + 5G AP（2.4G 默认关闭，避免与上游 AP 同频）；有线直连、无线走代理（手动切换）",
    },
}

# 每种模式开始时是否自动开启透明代理
MODE_PROXY = {"client": False, "hotspot": True, "gateway": True, "router": True}


def detect_mode(config: Config, interfaces: list[dict[str, Any]]) -> str:
    connections = {iface.get("connection") for iface in interfaces}
    if (
        config.router_lan_connection in connections
        or config.router_5g_connection in connections
        or config.router_24g_connection in connections
    ):
        return "router"
    if config.gateway_connection in connections:
        return "gateway"
    if config.hotspot_connection in connections:
        return "hotspot"
    return "client"


def _active_connections() -> set[str]:
    rc, out, _err = run(
        ["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"],
        timeout=5.0,
    )
    if rc != 0:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def _mode_from_active(config: Config, active: set[str]) -> str:
    if (
        config.router_lan_connection in active
        or config.router_5g_connection in active
        or config.router_24g_connection in active
    ):
        return "router"
    if config.gateway_connection in active:
        return "gateway"
    if config.hotspot_connection in active:
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
    config: Config,
    mode: str,
    proxy: bool | None = None,
    _fallback: bool = False,
) -> dict[str, Any]:
    """应用网络模式。

    proxy 为 None 时按模式默认值决定是否开启代理；为 False 时可显式保持关闭
    （开机自动进入家庭网关时使用），此时 Clash 未运行也不视为失败。

    路由器模式任何一步失败都会逆序回退，并自动恢复到切换前的模式；回退过程
    用 `_fallback` 防止递归。
    """
    if mode not in MODES:
        return {"ok": False, "mode": mode, "error": "未知模式", "steps": []}

    steps: list[dict[str, Any]] = []
    degraded: list[str] = []
    if mode == "client":
        steps.append(_autoconnect(config.gateway_connection, False))
        steps.append(_autoconnect(config.hotspot_connection, False))
        steps.extend(teardown_router(config))
        steps.append(_down(config.gateway_connection))
        steps.append(_down(config.hotspot_connection))
        steps.append(_ensure_uplink(config))
    elif mode == "hotspot":
        steps.append(_autoconnect(config.gateway_connection, False))
        steps.append(_autoconnect(config.hotspot_connection, True))
        steps.append(_down(config.router_24g_connection))
        steps.append(_down(config.router_5g_connection))
        steps.append(_down(config.router_lan_connection))
        steps.append(_down(config.gateway_connection))
        steps.append(_up(config.hotspot_connection))
    elif mode == "router":
        previous = _mode_from_active(config, _active_connections())
        steps.append(_autoconnect(config.gateway_connection, False))
        steps.append(_autoconnect(config.hotspot_connection, False))
        steps.append(_autoconnect(config.router_lan_connection, False))
        router_result = apply_router(config)
        steps.extend(router_result["steps"])
        degraded = list(router_result.get("degraded") or [])
        if not router_result["ok"]:
            failed = next(
                (step for step in steps if not step["ok"]), {"name": "router", "detail": ""}
            )
            steps.extend(teardown_router(config))
            fallback_proxy = bool(proxy_mark(config))
            fallback_steps: list[dict[str, Any]] = []
            if previous != "router" and not _fallback:
                fallback = apply_mode(
                    config, previous, proxy=fallback_proxy, _fallback=True
                )
                fallback_steps = fallback.get("steps", [])
            # 回退过程中连接又变了一轮，缓存必须作废
            invalidate_nm_cache()
            return {
                "ok": False,
                "mode": mode,
                "error": (
                    f"路由器模式在「{failed['name']}」失败：{failed['detail']}；"
                    f"已回退到 {previous}"
                ),
                "steps": steps + fallback_steps,
                "fallback": previous,
                "degraded": degraded,
                "proxy": {"ok": True, "steps": []},
            }
    else:
        steps.append(_autoconnect(config.gateway_connection, True))
        steps.append(_autoconnect(config.hotspot_connection, False))
        steps.append(_down(config.router_24g_connection))
        steps.append(_down(config.router_5g_connection))
        steps.append(_down(config.router_lan_connection))
        steps.append(_down(config.hotspot_connection))
        steps.append(_enable_forward(config))
        steps.append(_up(config.gateway_connection))

    # 连接状态刚被改过，让接口采集立刻重新读 NM，而不是等缓存过期
    invalidate_nm_cache()

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

    # 无线 AP 起不来属于「降级」而非模式失败（仍有线扩展口可用）
    degradable = {config.router_5g_connection, config.router_24g_connection}
    ok = all(
        step["ok"] or step["name"] in degradable for step in steps
    )
    return {
        "ok": ok,
        "mode": mode,
        "steps": steps,
        "proxy": proxy_result,
        "degraded": degraded,
    }


def reinforce_mode(
    config: Config, mode: str, proxy: bool | None = None
) -> dict[str, Any]:
    """连接已处于目标模式时使用：只补守护进程侧状态，不重启 NM 连接。

    NetworkManager 的连接会自己保持（`ipv4.method shared` 等），守护进程重启后
    真正缺的只有 sysctl 转发和代理开关。这里避免 `nmcli down/up` 把正在服务的
    网关短暂掐断。
    """
    steps: list[dict[str, Any]] = []
    if mode in ("gateway", "router"):
        steps.append(_enable_forward(config))
    proxy_enable = MODE_PROXY[mode] if proxy is None else proxy
    proxy_result = set_proxy(config, proxy_enable, mode)
    proxy_ok = bool(proxy_result.get("ok"))
    steps.append(
        _step(
            "proxy",
            proxy_ok or proxy is False,
            ""
            if proxy_ok
            else ("Clash 未运行，跳过关闭代理" if proxy is False else str(proxy_result.get("error") or "代理切换失败")),
        )
    )
    return {
        "ok": all(step["ok"] for step in steps),
        "mode": mode,
        "steps": steps,
        "proxy": proxy_result,
    }
