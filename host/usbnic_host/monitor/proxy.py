from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..config import Config
from ..exec import run
from .clash import ensure_clash

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


def set_tun(
    config: Config, enable: bool, keep_tproxy: bool = False
) -> dict[str, Any]:
    """通过 Clash external-controller 动态开关 TUN（用于本机代理）。

    keep_tproxy=True 时不动已安装的 TPROXY 表：否则开启 TUN 会顺手把下游
    劫持规则删掉，导致流量改走 TUN、源地址被改写成 198.18.0.1。
    """
    if enable:
        if not keep_tproxy:
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


def _ensure_device_stats(config: Config) -> None:
    """设备速率统计与代理解耦：切换代理后确认计数表仍在。

    `usbnic_stat` 只是 prerouting/postrouting 上的只读计数器，和 TUN/TPROXY
    没有任何关系；但外部清理（cleanup.py、手工 nft flush）可能顺手把它删掉。
    这里在每次代理开关后补一次幂等检查，避免"关代理后设备速率归零"。
    """
    from . import device_stats

    device_stats.ensure_if_missing(config)


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
        _ensure_device_stats(config)
        return {"ok": result["ok"], "error": result["error"], "steps": [result]}
    # 内核没运行就自动拉起；已运行则直接复用
    ensured = ensure_clash(config)
    if not ensured.get("ok"):
        return {
            "ok": False,
            "error": str(ensured.get("error") or "Clash 启动失败"),
            "steps": ensured.get("steps", []),
        }
    if mode == "hotspot":
        result = enable_tproxy(config, mode="hotspot")
    elif mode == "gateway":
        result = enable_tproxy(config, mode="gateway")
    elif mode == "router":
        result = enable_tproxy(config, mode="router")
    else:
        result = set_tun(config, True)
    if result.get("ok"):
        mark_proxy(
            config,
            "tproxy" if mode in ("hotspot", "gateway", "router") else "tun",
        )
    _ensure_device_stats(config)
    return result


# 私网 / 环回 / 组播不进代理：既保证设备互访与上游管理页可用，
# 也避免把本机回程流量再次劫持。注意不要排除 198.18.0.0/16——LAN 客户端
# 从 dnsmasq 拿到的 fake-IP 必须被 TPROXY 送回 Clash 才能映射回域名。
BYPASS_NETWORKS = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "255.255.255.255",
]


def router_targets(config: Config) -> tuple[list[str], list[str], list[str]]:
    """返回路由器模式的（下游接口, 需要代理的网段, 直连网段）。"""
    ifaces = [
        iface
        for iface in (
            config.router_lan_iface or config.gateway_iface,
            config.router_5g_iface,
        )
        if iface
    ]
    cidrs = [
        cidr
        for cidr in (
            config.router_lan_cidr,
            config.router_5g_cidr,
        )
        if cidr
    ]
    if config.router_24g_enabled and config.router_24g_iface:
        ifaces.append(config.router_24g_iface)
        cidrs.append(config.router_24g_cidr)
    bypass = list(config.router_direct_cidrs)
    if config.router_wired_direct:
        bypass.append(config.router_lan_cidr)
    return ifaces, cidrs, bypass


def tproxy_ruleset(
    config: Config,
    iface: str | None = None,
    cidr: str | None = None,
    *,
    ifaces: list[str] | None = None,
    cidrs: list[str] | None = None,
    bypass: list[str] | None = None,
) -> str:
    """生成劫持下游 LAN 转发的 nftables 规则（TCP + UDP，保留真实源 IP）。

    支持一次覆盖多个下游接口（有线扩展口 + 5G AP + 2.4G AP）。`counter`
    让 `tproxy_status()` 能判断规则是否真的命中，而不只是"表存在"。
    """
    ifaces = ifaces if ifaces is not None else ([iface] if iface else [config.gateway_iface])
    cidrs = cidrs if cidrs is not None else ([cidr] if cidr else [config.lan_cidr])
    ifaces = [item for item in ifaces if item]
    cidrs = [item for item in cidrs if item]
    bypass = [item for item in (bypass or []) if item]
    if not ifaces or not cidrs:
        return ""
    nft_ifaces = ", ".join(f'"{item}"' for item in ifaces)
    lan_elements = ", ".join(cidrs)
    daddr = ", ".join(BYPASS_NETWORKS)
    bypass_rule = (
        f"        ip saddr {{ {', '.join(bypass)} }} return\n" if bypass else ""
    )
    return (
        f"table ip {NFT_TABLE} {{\n"
        "    set lan_nets {\n"
        "        type ipv4_addr\n"
        "        flags interval\n"
        f"        elements = {{ {lan_elements} }}\n"
        "    }\n"
        "    chain prerouting {\n"
        "        type filter hook prerouting priority mangle; policy accept;\n"
        f"        iifname != {{ {nft_ifaces} }} return\n"
        "        ip saddr != @lan_nets return\n"
        f"        ip daddr {{ {daddr} }} return\n"
        f"{bypass_rule}"
        f"        meta l4proto {{ tcp, udp }} tproxy to :{config.clash_tproxy_port} "
        "meta mark set 1 counter accept\n"
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
    config: Config,
    iface: str | None = None,
    cidr: str | None = None,
    *,
    mode: str | None = None,
    ifaces: list[str] | None = None,
    cidrs: list[str] | None = None,
    bypass: list[str] | None = None,
) -> dict[str, Any]:
    if mode == "router":
        ifaces, cidrs, bypass = router_targets(config)
    elif mode == "hotspot":
        ifaces = [config.hotspot_iface] if config.hotspot_iface else []
        cidrs = [config.hotspot_cidr]
    elif mode == "gateway":
        ifaces = [config.gateway_iface] if config.gateway_iface else []
        cidrs = [config.lan_cidr]
    if ifaces is None:
        ifaces = [iface] if iface else [config.gateway_iface]
    if cidrs is None:
        cidrs = [cidr] if cidr else [config.lan_cidr]
    ifaces = [item for item in ifaces if item]
    if not ifaces:
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
            # TPROXY 的透明套接字必须绑 0.0.0.0；mihomo 在 allow-lan=false 时
            # 会把所有监听器绑到 127.0.0.1，导致 nft tproxy 找不到套接字。
            # 由此带来的 7897/9097 暴露由 nft usbnic_guard 表兜底。
            "allow-lan": True,
        },
    )
    steps.append({"name": "clash", **clash})
    if not clash["ok"]:
        return {"ok": False, "error": clash["error"], "steps": steps}

    # 2) 安装 nftables 劫持规则（可同时覆盖多个下游接口）
    _delete_table()
    ruleset = tproxy_ruleset(
        config, ifaces=ifaces, cidrs=cidrs, bypass=bypass
    )
    rc, _out, err = run(
        ["nft", "-f", "-"],
        timeout=5.0,
        input_text=ruleset,
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


def tproxy_status() -> dict[str, Any]:
    """读取 TPROXY 规则的真实状态：表是否存在、规则是否命中（counter）。

    只看"表存在"会把"规则装上了但没匹配任何包"误判成已接管，这正是下游
    流量被 masquerade 成 198.18.0.1 时面板仍显示正常的原因。
    """
    rc, out, err = run(["nft", "-j", "list", "table", "ip", NFT_TABLE], timeout=2.0)
    if rc != 0:
        return {
            "installed": False,
            "rule": False,
            "packets": 0,
            "bytes": 0,
            "error": (err or out).strip(),
        }
    packets = 0
    packets_bytes = 0
    seen_rule = False
    try:
        data = json.loads(out)
        for item in data.get("nftables", []):
            rule = item.get("rule")
            if not rule:
                continue
            exprs = rule.get("expr") or []
            if not any(isinstance(expr, dict) and "tproxy" in expr for expr in exprs):
                continue
            seen_rule = True
            for expr in exprs:
                if isinstance(expr, dict) and "counter" in expr:
                    counter = expr.get("counter") or {}
                    packets += int(counter.get("packets") or 0)
                    packets_bytes = int(counter.get("bytes") or 0)
        if seen_rule is False:
            return {"installed": True, "rule": False, "packets": 0, "bytes": 0, "error": ""}
        return {
            "installed": True,
            "rule": True,
            "packets": packets,
            "bytes": packets_bytes,
            "error": "",
        }
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "installed": True,
            "rule": False,
            "packets": 0,
            "bytes": 0,
            "error": str(exc),
        }


def tproxy_installed() -> bool:
    rc, _out, _err = run(["nft", "list", "table", "ip", NFT_TABLE], timeout=2.0)
    return rc == 0


def tproxy_active() -> bool:
    """只有规则存在且确实命中过流量，才算真正接管。"""
    status = tproxy_status()
    return bool(status["installed"] and status["packets"] > 0)
