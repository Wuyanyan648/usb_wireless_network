from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..config import Config
from ..exec import run

# nftables 表名与 TPROXY 策略路由表号，统一前缀便于精确清理
NFT_TABLE = "usbnic"
TPROXY_FWMARK = "1"
TPROXY_ROUTE_TABLE = "100"
PROXY_STATE_NAME = "proxy.state"


def _state_path(config: Config) -> Path:
    return config.data_dir / PROXY_STATE_NAME


def mark_proxy(config: Config, kind: str) -> None:
    """记录代理由上位机开启，停止服务时据此决定是否需要关闭。"""
    try:
        _state_path(config).write_text(kind, encoding="utf-8")
    except OSError:
        pass


def clear_proxy_mark(config: Config) -> None:
    try:
        _state_path(config).unlink()
    except OSError:
        pass


def proxy_mark(config: Config) -> str:
    try:
        return _state_path(config).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def clash_patch(config: Config, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.clash_secret:
        headers["Authorization"] = f"Bearer {config.clash_secret}"
    request = urllib.request.Request(
        f"{config.clash_api}/configs",
        data=body,
        headers=headers,
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.http_timeout) as response:
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "status": 0, "error": str(exc)}


def set_tun(config: Config, enable: bool) -> dict[str, Any]:
    """通过 Clash external-controller 动态开关 TUN（用于非网关模式的本机代理）。"""
    if enable:
        cleanup_tproxy(config, clear_port=True)
        payload: dict[str, Any] = {
            "tun": {
                "enable": True,
                "stack": "mixed",
                "auto-route": True,
                "auto-detect-interface": True,
                "dns-hijack": ["any:53"],
            }
        }
    else:
        payload = {"tun": {"enable": False}}
    result = clash_patch(config, payload)
    return {"ok": result["ok"], "error": result["error"], "steps": [result]}


def set_proxy(config: Config, enable: bool, mode: str) -> dict[str, Any]:
    """按当前模式选择透明代理方式。

    家庭网关 / USB WiFi AP 模式使用 TPROXY 接管下游转发流量，让 Clash 看到
    设备真实源 IP；同时开启 TUN 代理本机流量。TPROXY 在 prerouting 阶段
    就截获下游包，不会被 TUN 的路由抢走，因此两者可以共存。
    默认客户端模式只有本机流量，直接使用 TUN。
    """
    if not enable:
        # 关闭时两种接管方式都要清理，避免规则残留
        cleanup_tproxy(config, clear_port=True)
        result = clash_patch(config, {"tun": {"enable": False}})
        if result["ok"]:
            clear_proxy_mark(config)
        return {"ok": result["ok"], "error": result["error"], "steps": [result]}
    if mode == "hotspot":
        result = enable_tproxy(config, config.hotspot_iface, config.hotspot_cidr)
    elif mode == "gateway":
        result = enable_tproxy(config, config.gateway_iface, config.lan_cidr)
    else:
        result = set_tun(config, True)
    if result.get("ok"):
        mark_proxy(config, "tproxy" if mode in ("hotspot", "gateway") else "tun")
    return result


def _bypass_networks(cidr: str) -> list[str]:
    networks = [
        cidr,
        "127.0.0.0/8",
        # 上游光猫/路由器所在网段，保持直连便于管理
        "192.168.0.0/16",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255",
    ]
    seen: list[str] = []
    for item in networks:
        if item and item not in seen:
            seen.append(item)
    return seen


def tproxy_ruleset(
    config: Config, iface: str | None = None, cidr: str | None = None
) -> str:
    """生成只劫持下游 LAN 转发的 nftables 规则（TCP + UDP，保留源 IP）。"""
    iface = iface or config.gateway_iface
    cidr = cidr or config.lan_cidr
    bypass = ", ".join(_bypass_networks(cidr))
    return (
        f"table ip {NFT_TABLE} {{\n"
        "    chain prerouting {\n"
        "        type filter hook prerouting priority mangle; policy accept;\n"
        f'        iifname != "{iface}" return\n'
        f"        ip saddr != {cidr} return\n"
        f"        ip daddr {{ {bypass} }} return\n"
        f"        meta l4proto {{ tcp, udp }} tproxy to :{config.clash_tproxy_port} "
        "meta mark set 1 accept\n"
        "    }\n"
        "}\n"
    )


def _delete_table() -> None:
    run(["nft", "delete", "table", "ip", NFT_TABLE], timeout=3.0)


def cleanup_tproxy(config: Config, clear_port: bool = False) -> None:
    _delete_table()
    run(
        ["ip", "rule", "del", "fwmark", TPROXY_FWMARK, "lookup", TPROXY_ROUTE_TABLE],
        timeout=3.0,
    )
    run(["ip", "route", "flush", "table", TPROXY_ROUTE_TABLE], timeout=3.0)
    if clear_port:
        clash_patch(config, {"tproxy-port": 0})


def enable_tproxy(
    config: Config, iface: str | None = None, cidr: str | None = None
) -> dict[str, Any]:
    iface = iface or config.gateway_iface
    cidr = cidr or config.lan_cidr
    if not iface:
        return {"ok": False, "error": "未配置下游接口", "steps": []}
    steps: list[dict[str, Any]] = []

    # 1) 开启 TUN 代理本机流量，同时打开 TPROXY 端口。
    #    下游转发包会在 prerouting 被 TPROXY 截获，不受 TUN 路由影响。
    clash = clash_patch(
        config,
        {
            "tun": {
                "enable": True,
                "stack": "mixed",
                "auto-route": True,
                "auto-detect-interface": True,
                "dns-hijack": ["any:53"],
            },
            "tproxy-port": config.clash_tproxy_port,
        },
    )
    steps.append({"name": "clash", **clash})
    if not clash["ok"]:
        return {"ok": False, "error": clash["error"], "steps": steps}

    # 2) 安装 nftables 劫持规则
    _delete_table()
    rc, _out, err = run(
        ["nft", "-f", "-"],
        timeout=5.0,
        input_text=tproxy_ruleset(config, iface, cidr),
    )
    steps.append(
        {
            "name": "nftables",
            "ok": rc == 0,
            "error": err.strip() if rc != 0 else "",
        }
    )
    if rc != 0:
        return {"ok": False, "error": err.strip(), "steps": steps}

    # 3) 策略路由：把 mark=1 的包交给本地 tproxy 监听
    run(
        ["ip", "rule", "del", "fwmark", TPROXY_FWMARK, "lookup", TPROXY_ROUTE_TABLE],
        timeout=3.0,
    )
    rc, _out, err = run(
        ["ip", "rule", "add", "fwmark", TPROXY_FWMARK, "lookup", TPROXY_ROUTE_TABLE],
        timeout=3.0,
    )
    steps.append(
        {"name": "ip-rule", "ok": rc == 0, "error": err.strip() if rc != 0 else ""}
    )
    if rc != 0:
        return {"ok": False, "error": err.strip(), "steps": steps}

    rc, _out, err = run(
        [
            "ip",
            "route",
            "replace",
            "local",
            "0.0.0.0/0",
            "dev",
            "lo",
            "table",
            TPROXY_ROUTE_TABLE,
        ],
        timeout=3.0,
    )
    steps.append(
        {"name": "ip-route", "ok": rc == 0, "error": err.strip() if rc != 0 else ""}
    )
    return {"ok": rc == 0, "error": err.strip() if rc != 0 else "", "steps": steps}


def tproxy_active() -> bool:
    rc, _out, _err = run(["nft", "list", "table", "ip", NFT_TABLE], timeout=2.0)
    return rc == 0
