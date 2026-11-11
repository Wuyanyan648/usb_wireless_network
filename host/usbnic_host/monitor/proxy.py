from __future__ import annotations

import ipaddress
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..config import Config
from ..exec import run
from ..util import read_text
from .clash import ensure_clash
from . import device_policy, rule_provider

# nftables 表名与 TPROXY 策略路由表号，统一前缀便于精确清理
NFT_TABLE = "usbnic"
TPROXY_FWMARK = "1"
TPROXY_ROUTE_TABLE = "100"
# 显式指定优先级：必须排在 mihomo TUN 的 9000/9001/9002 规则之前，
# 否则 mark=1 的包会先被 TUN 的 table 2022 抢走，tproxy 监听永远收不到。
TPROXY_RULE_PREF = "100"
# TPROXY 透明套接字必须绑 IPv4：mihomo 默认 bind-address=*，会起双栈监听，
# 而双栈 socket 缺 IPV6_TRANSPARENT，IPv4 的 tproxy 查找会失败（mihomo#3098）。
TPROXY_BIND_ADDRESS = "0.0.0.0"
DEFAULT_BIND_ADDRESS = "*"
# src_valid_mark=1 时转发包会做反向路径校验，TPROXY 标记过的包被判 martian
# 静默丢弃；装表期间临时置 0，撤表后恢复原值。
SRC_VALID_MARK_PATH = Path("/proc/sys/net/ipv4/conf/all/src_valid_mark")
RUNTIME_STATE_NAME = "tproxy_runtime.json"
PROXY_STATE_NAME = "proxy.state"
PROXY_NET_SET = "proxy_nets"
DIRECT_NET_SET = "direct_nets"


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


def clash_get(config: Config, path: str) -> dict[str, Any]:
    """只读取 mihomo 控制口，失败时返回空 dict，不抛异常。"""
    headers: dict[str, str] = {}
    if config.clash_secret:
        headers["Authorization"] = f"Bearer {config.clash_secret}"
    request = urllib.request.Request(f"{config.clash_api}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=config.http_timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {}


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


def host_proxy_pref(config: Config) -> bool:
    """主机（TUN）代理的持久选择；总开关只控制生效，不改这个值。"""
    path = Path(config.data_dir) / "host_proxy.json"
    try:
        return bool(json.loads(path.read_text()).get("enabled", True))
    except (OSError, ValueError, AttributeError):
        return True


def save_host_proxy_pref(config: Config, enabled: bool) -> None:
    path = Path(config.data_dir) / "host_proxy.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"enabled": bool(enabled)}))
    except OSError:
        pass


def set_host_proxy(config: Config, enabled: bool) -> dict[str, Any]:
    """只切 TUN，不动 TPROXY 表与设备策略（主机与下游互不依赖）。"""
    result = clash_patch(config, {"tun": {"enable": bool(enabled)}})
    if result["ok"]:
        save_host_proxy_pref(config, enabled)
    return result


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
        mark_proxy(config, "tproxy" if result.get("kind") == "tproxy" else "tun")
        # 总开关重新打开时按主机开关的持久选择恢复，而不是一律开 TUN
        if not host_proxy_pref(config):
            clash_patch(config, {"tun": {"enable": False}})
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
    exclude: list[str] | None = None,
    proxy_nets: list[str] | None = None,
    direct_nets: list[str] | None = None,
) -> str:
    """生成劫持下游 LAN 转发的 nftables 规则（TCP + UDP，保留真实源 IP）。

    支持一次覆盖多个下游接口（有线扩展口 + 5G AP + 2.4G AP）。`counter`
    让 `tproxy_status()` 能判断规则是否真的命中，而不只是"表存在"。
    `exclude` 里的源地址（默认 `tproxy_exclude_ips`）永不接管，用于保护
    走有线直连、正在大流量下载的设备。

    `proxy_nets` 为设备级策略选中的 IP 白名单（None 表示不限）；`direct_nets`
    是显式直连设备集合，必须排在白名单判断之前。两个集合都留在表里，之后
    由 `refresh_proxy_set()` 按 ARP/租约变化增量刷新，避免重建整张表。
    """
    ifaces = ifaces if ifaces is not None else ([iface] if iface else [config.gateway_iface])
    cidrs = cidrs if cidrs is not None else ([cidr] if cidr else [config.lan_cidr])
    ifaces = [item for item in ifaces if item]
    cidrs = [item for item in cidrs if item]
    bypass = [item for item in (bypass or []) if item]
    if not ifaces or not cidrs:
        return ""
    proxy_nets = None if proxy_nets is None else [item for item in proxy_nets if item]
    direct_nets = [item for item in (direct_nets or []) if item]
    # 白名单为空时没有设备需要代理，装表只会白跑一趟
    if proxy_nets is not None and not proxy_nets:
        return ""
    nft_ifaces = ", ".join(f'"{item}"' for item in ifaces)
    lan_elements = ", ".join(cidrs)
    daddr = ", ".join(BYPASS_NETWORKS)
    bypass_rule = (
        f"        ip saddr {{ {', '.join(bypass)} }} return\n" if bypass else ""
    )
    exclude = [item for item in (exclude or config.tproxy_exclude_ips) if item]
    exclude_rule = (
        f"        ip saddr {{ {', '.join(exclude)} }} return\n" if exclude else ""
    )
    direct_rule = (
        f"        ip saddr @{DIRECT_NET_SET} return\n" if direct_nets else ""
    )
    proxy_rule = (
        f"        ip saddr != @{PROXY_NET_SET} return\n" if proxy_nets is not None else ""
    )
    direct_elements = (
        f"        elements = {{ {', '.join(direct_nets)} }}\n" if direct_nets else ""
    )
    proxy_elements = (
        f"        elements = {{ {', '.join(proxy_nets)} }}\n" if proxy_nets else ""
    )
    return (
        f"table ip {NFT_TABLE} {{\n"
        "    set lan_nets {\n"
        "        type ipv4_addr\n"
        "        flags interval\n"
        f"        elements = {{ {lan_elements} }}\n"
        "    }\n"
        f"    set {PROXY_NET_SET} {{\n"
        "        type ipv4_addr\n"
        "        flags interval\n"
        "        size 1024\n"
        f"{proxy_elements}"
        "    }\n"
        f"    set {DIRECT_NET_SET} {{\n"
        "        type ipv4_addr\n"
        "        flags interval\n"
        "        size 1024\n"
        f"{direct_elements}"
        "    }\n"
        "    chain prerouting {\n"
        "        type filter hook prerouting priority mangle; policy accept;\n"
        f"        iifname != {{ {nft_ifaces} }} return\n"
        "        ip saddr != @lan_nets return\n"
        f"        ip daddr {{ {daddr} }} return\n"
        f"{bypass_rule}"
        f"{exclude_rule}"
        # 直连集合优先于白名单与 tproxy：显式直连的设备永不被劫持
        f"{direct_rule}"
        f"{proxy_rule}"
        # 先统计"匹配到规则"的包，再统计"成功交给 tproxy"的包。
        # 两者不相等时，说明命中了但套接字没接住（下游会黑洞），据此自动回滚。
        "        meta l4proto { tcp, udp } counter\n"
        f"        meta l4proto {{ tcp, udp }} tproxy to :{config.clash_tproxy_port} "
        "meta mark set 1 counter accept\n"
        "    }\n"
        "}\n"
    )


def _delete_table() -> None:
    run(["nft", "delete", "table", "ip", NFT_TABLE], timeout=3.0)


def _runtime_path(config: Config) -> Path:
    return config.data_dir / RUNTIME_STATE_NAME


def _save_runtime(config: Config, state: dict[str, Any]) -> None:
    try:
        _runtime_path(config).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def _load_runtime(config: Config) -> dict[str, Any]:
    try:
        loaded = json.loads(_runtime_path(config).read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def src_valid_mark() -> str:
    """读取当前值；非 Linux 或无权限时返回空串。"""
    return read_text(SRC_VALID_MARK_PATH)


def _prepare_src_valid_mark(config: Config) -> dict[str, Any]:
    """装表前把 src_valid_mark 临时置 0，并校验没有被其他进程改回去。

    Tailscale 会把它写成 1：此后 TPROXY 标记过的转发包在反向路径校验里被
    判成 martian 静默丢弃，表现为 nft 命中数上涨但 mihomo 没有连接。
    只在装表期间改动，撤表时由 `cleanup_tproxy()` 恢复原值。
    """
    original = src_valid_mark()
    changed = original == "1"
    if changed:
        try:
            SRC_VALID_MARK_PATH.write_text("0", encoding="utf-8")
        except OSError as exc:
            return {
                "ok": False,
                "error": f"无法写 src_valid_mark: {exc}",
                "original": original,
            }
    current = src_valid_mark()
    if current != "0":
        return {
            "ok": False,
            "error": f"src_valid_mark 仍为 {current or 'unknown'}，拒绝装表",
            "original": original,
        }
    if changed:
        runtime = _load_runtime(config)
        runtime["src_valid_mark"] = original
        _save_runtime(config, runtime)
    return {"ok": True, "error": "", "original": original, "changed": changed}


def _restore_src_valid_mark(config: Config) -> None:
    runtime = _load_runtime(config)
    original = str(runtime.pop("src_valid_mark", "") or "")
    if original and src_valid_mark() != original:
        try:
            SRC_VALID_MARK_PATH.write_text(original, encoding="utf-8")
        except OSError:
            pass
    if runtime:
        _save_runtime(config, runtime)
    else:
        try:
            _runtime_path(config).unlink()
        except OSError:
            pass


def _listener_addresses(config: Config) -> dict[str, list[str]]:
    """读取 7896 的实际监听地址，用于确认透明套接字是 IPv4 可达的。"""
    port = str(config.clash_tproxy_port)
    result: dict[str, list[str]] = {"tcp": [], "udp": []}
    for proto, flag in (("tcp", "-lnt"), ("udp", "-lnu")):
        rc, out, _err = run(["ss", flag], timeout=2.0)
        if rc != 0:
            continue
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            local = parts[3]
            if local.endswith(f":{port}"):
                result[proto].append(local)
    return result


def _probe_ipv4(port: int, timeout: float = 0.6) -> bool:
    """从本机用 IPv4 连一下端口，确认双栈 `*:7896` 对 v4 是可达的。"""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_ipv4_listener(config: Config, timeout: float = 3.0) -> dict[str, Any]:
    """确认 tproxy 端口在监听，且 IPv4 连接能够落到这个监听上。

    `*:7896` 是双栈监听（IPV6_V6ONLY=0），IPv4 本来就能进入，不应因为
    没有显式绑 `0.0.0.0` 就拒绝装表；最终是否真正接管由 verify_tproxy
    的实测投递决定。
    """
    deadline = time.monotonic() + timeout
    addresses = _listener_addresses(config)
    while time.monotonic() < deadline:
        if addresses["tcp"] and _probe_ipv4(int(config.clash_tproxy_port)):
            return {"ok": True, "error": "", "tcp": addresses["tcp"], "udp": addresses["udp"]}
        time.sleep(0.25)
        addresses = _listener_addresses(config)
    return {
        "ok": False,
        "error": (
            "7896 未监听或 IPv4 无法连接（当前："
            f"{', '.join(addresses['tcp']) or '未监听'}）"
        ),
        "tcp": addresses["tcp"],
        "udp": addresses["udp"],
    }


def cleanup_tproxy(config: Config, clear_port: bool = False) -> None:
    # 先让下游规则失效：否则表撤了、provider 还留着 10.x -> PROXY 的源地址规则
    rule_provider.sync(config, [])
    _delete_table()
    # 新规则显式指定 pref；旧版本装的规则没有 pref，两条删除都执行一次
    run(["ip", "rule", "del", "pref", TPROXY_RULE_PREF], timeout=3.0)
    run(
        ["ip", "rule", "del", "fwmark", TPROXY_FWMARK, "lookup", TPROXY_ROUTE_TABLE],
        timeout=3.0,
    )
    run(["ip", "route", "flush", "table", TPROXY_ROUTE_TABLE], timeout=3.0)
    # 即使不关服务，也要把为了装表临时改过的 bind-address 收回去
    runtime = _load_runtime(config)
    original_bind = str(runtime.pop("bind_address", "") or "")
    payload: dict[str, Any] = {}
    if clear_port:
        payload["tproxy-port"] = 0
    if original_bind:
        payload["bind-address"] = original_bind
    if payload:
        clash_patch(config, payload)
    if runtime:
        _save_runtime(config, runtime)
    else:
        try:
            _runtime_path(config).unlink()
        except OSError:
            pass
    _restore_src_valid_mark(config)


def _tun_enabled(config: Config) -> bool:
    """读取当前 TUN 开关，用于回滚时还原用户原来的状态。"""
    body = clash_get(config, "/configs")
    return bool((body.get("tun") or {}).get("enable"))


def _downstream_networks(config: Config) -> list[ipaddress.IPv4Network]:
    nets: list[ipaddress.IPv4Network] = []
    for cidr in (
        config.lan_cidr,
        config.hotspot_cidr,
        config.router_lan_cidr,
        config.router_5g_cidr,
        config.router_24g_cidr,
    ):
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return nets


def connection_source(item: dict[str, Any]) -> str:
    """mihomo /connections 的源地址在 metadata.sourceIP；兼容旧的 source_ip。"""
    metadata = item.get("metadata") or {}
    return str(metadata.get("sourceIP") or item.get("source_ip") or "")


def _downstream_connections(config: Config) -> int:
    """统计 mihomo 里源地址属于下游网段的连接数。"""
    body = clash_get(config, "/connections")
    nets = _downstream_networks(config)
    count = 0
    for item in body.get("connections") or []:
        try:
            address = ipaddress.ip_address(connection_source(item))
        except ValueError:
            continue
        if any(address in network for network in nets):
            count += 1
    return count


def _rollback_tproxy(config: Config, prev_tun: bool) -> None:
    """撤表、收口监听，并把 TUN 还原到改动前的状态。"""
    cleanup_tproxy(config, clear_port=True)
    clash_patch(config, {"allow-lan": False})
    if not prev_tun:
        set_tun(config, False)


def _lan_bytes(config: Config) -> int:
    """下游统计表里的累计字节，用来判断窗口内下游到底有没有流量。"""
    from . import device_stats

    try:
        counters = device_stats.read_counters()
    except Exception:  # noqa: BLE001 - 统计失败不能拖垮回滚判断
        return 0
    return sum(int(item.get("rx", 0)) + int(item.get("tx", 0)) for item in counters.values())


def verify_tproxy(config: Config, prev_tun: bool) -> dict[str, Any]:
    """装表后的安全窗口：下游有流量却进不了 mihomo，就立刻自动撤表。

    不能只看 nft 的 counter：隔离环境实测表明，tproxy 没接住包时后面的
    counter 仍会累加，计数器无法区分"接住了"和"黑洞了"。唯一可靠的信号
    是 mihomo 的连接表里有没有下游源地址。
    """
    window = float(config.tproxy_verify_seconds or 0)
    if window <= 0:
        return {"ok": True, "detail": "未启用校验"}
    lan_before = _lan_bytes(config)
    deadline = time.monotonic() + window
    while True:
        if _downstream_connections(config) > 0:
            return {"ok": True, "detail": "mihomo 已看到下游连接"}
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    lan_delta = _lan_bytes(config) - lan_before
    if lan_delta > 256 * 1024:
        _rollback_tproxy(config, prev_tun)
        return {
            "ok": False,
            "rolled_back": True,
            "detail": (
                f"窗口内下游有 {lan_delta // 1024} KB 流量，但 mihomo 没有任何下游连接，"
                "已自动撤表回滚"
            ),
        }
    return {
        "ok": True,
        "detail": f"窗口内下游流量仅 {max(lan_delta, 0) // 1024} KB，无法判断，未回滚",
    }


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
    # 记录改动前的 TUN 开关，自动回滚时要还原成用户原来的状态
    prev_tun = _tun_enabled(config)
    # 设备级策略：只有 proxy 设备的当前 IP 进入劫持白名单
    proxy_nets, direct_nets = device_policy.assignments(config)
    if not proxy_nets:
        # 没有设备选择走代理：只开本机 TUN，下游保持普通 NAT
        result = set_tun(config, True)
        result["kind"] = "tun"
        result["detail"] = "没有设备选择走代理，跳过 TPROXY（下游保持直连）"
        return result
    # 1) 开启 TUN 代理本机流量，同时打开 TPROXY 端口。
    #    下游转发包会在 prerouting 被 TPROXY 截获，不受 TUN 路由影响。
    #    bind-address 强制 0.0.0.0：默认 `*` 会起双栈监听，IPv4 的
    #    tproxy 查找找不到带 IP_TRANSPARENT 的 v4 套接字（mihomo#3098）。
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
            "allow-lan": True,
            "bind-address": TPROXY_BIND_ADDRESS,
        },
    )
    steps.append({"name": "clash", **clash})
    if not clash["ok"]:
        return {"ok": False, "error": clash["error"], "steps": steps}
    # 记录原始 bind-address，清理时恢复；默认值是 *
    runtime = _load_runtime(config)
    runtime.setdefault("bind_address", DEFAULT_BIND_ADDRESS)
    _save_runtime(config, runtime)

    # 2) 确认 7896 是 IPv4 监听，否则装了表也接不住（会直接黑洞下游）
    listener = _wait_ipv4_listener(config)
    steps.append(
        {
            "name": "listener",
            "ok": bool(listener.get("ok")),
            "detail": "" if listener.get("ok") else str(listener.get("error") or ""),
        }
    )
    if not listener.get("ok"):
        _rollback_tproxy(config, prev_tun)
        return {"ok": False, "error": str(listener.get("error") or ""), "steps": steps}

    # 3) 临时关闭 src_valid_mark，并确认没有被 tailscaled 之类改回去
    guard = _prepare_src_valid_mark(config)
    steps.append(
        {
            "name": "src_valid_mark",
            "ok": bool(guard.get("ok")),
            "detail": (
                f"原值 {guard.get('original') or 'unknown'}"
                if guard.get("ok")
                else str(guard.get("error") or "")
            ),
        }
    )
    if not guard.get("ok"):
        _rollback_tproxy(config, prev_tun)
        return {"ok": False, "error": str(guard.get("error") or ""), "steps": steps}

    # 4) 安装 nftables 劫持规则（可同时覆盖多个下游接口）
    _delete_table()
    ruleset = tproxy_ruleset(
        config,
        ifaces=ifaces,
        cidrs=cidrs,
        bypass=bypass,
        proxy_nets=proxy_nets,
        direct_nets=direct_nets,
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
        _rollback_tproxy(config, prev_tun)
        return {"ok": False, "error": err.strip(), "steps": steps}

    # 装表后再复核一次：tailscaled 可能在装表瞬间又把值写回 1
    current_mark = src_valid_mark()
    steps.append(
        {
            "name": "src_valid_mark-after",
            "ok": current_mark == "0",
            "detail": f"装表后 {current_mark or 'unknown'}",
        }
    )
    if current_mark != "0":
        _rollback_tproxy(config, prev_tun)
        return {
            "ok": False,
            "error": f"装表后 src_valid_mark 变为 {current_mark or 'unknown'}，已撤表",
            "steps": steps,
        }

    # 5) 策略路由：把 mark=1 的包交给本地 tproxy 监听。
    #    pref 必须早于 TUN 的 9002，否则包会被 table 2022 送进 TUN。
    run(["ip", "rule", "del", "pref", TPROXY_RULE_PREF], timeout=3.0)
    run(
        ["ip", "rule", "del", "fwmark", TPROXY_FWMARK, "lookup", TPROXY_ROUTE_TABLE],
        timeout=3.0,
    )
    rc, _out, err = run(
        [
            "ip",
            "rule",
            "add",
            "pref",
            TPROXY_RULE_PREF,
            "fwmark",
            TPROXY_FWMARK,
            "lookup",
            TPROXY_ROUTE_TABLE,
        ],
        timeout=3.0,
    )
    steps.append(
        {"name": "ip-rule", "ok": rc == 0, "error": err.strip() if rc != 0 else ""}
    )
    if rc != 0:
        _rollback_tproxy(config, prev_tun)
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
    if rc != 0:
        _rollback_tproxy(config, prev_tun)
        return {"ok": False, "error": err.strip(), "steps": steps}

    # 5.5) 下游强制走代理：把这些设备的源 IP 写进 mihomo 的 usbnic-proxy
    #      provider，规则链最前面的 RULE-SET 会让它们绕过 GEOIP,CN,DIRECT。
    provider = rule_provider.sync(config, proxy_nets)
    steps.append(
        {
            "name": "rule-provider",
            "ok": bool(provider.get("ok")),
            "detail": str(provider.get("detail") or ""),
        }
    )
    if not provider.get("ok"):
        _rollback_tproxy(config, prev_tun)
        return {
            "ok": False,
            "error": f"下发 usbnic-proxy 规则失败：{provider.get('detail')}",
            "steps": steps,
        }

    # 6) 安全窗口：确认规则真的接住了下游流量，否则立刻撤表回滚
    verdict = verify_tproxy(config, prev_tun)
    steps.append(
        {
            "name": "verify",
            "ok": bool(verdict.get("ok")),
            "detail": str(verdict.get("detail") or ""),
        }
    )
    if not verdict.get("ok"):
        return {
            "ok": False,
            "error": str(verdict.get("detail") or "TPROXY 校验失败"),
            "steps": steps,
        }
    return {
        "ok": True,
        "error": "",
        "kind": "tproxy",
        "detail": f"已接管 {len(proxy_nets)} 台走代理的设备",
        "steps": steps,
    }


def refresh_proxy_set(config: Config) -> dict[str, Any]:
    """按 ARP / DHCP 租约刷新 nft 集合（DHCP 换 IP 后仍能命中正确设备）。

    表不存在时什么都不做；最后一个走代理的设备被关掉时自动撤表，避免留下
    "命中但不投递"的空转规则。
    """
    if not tproxy_installed():
        return {"ok": True, "updated": False, "proxy": 0, "direct": 0}
    proxy_ips, direct_ips = device_policy.assignments(config)
    if not proxy_ips:
        cleanup_tproxy(config, clear_port=True)
        clash_patch(config, {"allow-lan": False})
        return {"ok": True, "updated": False, "proxy": 0, "direct": len(direct_ips)}
    lines = [
        f"flush set ip {NFT_TABLE} {PROXY_NET_SET}",
        f"flush set ip {NFT_TABLE} {DIRECT_NET_SET}",
        f"add element ip {NFT_TABLE} {PROXY_NET_SET} {{ {', '.join(proxy_ips)} }}",
    ]
    if direct_ips:
        lines.append(
            f"add element ip {NFT_TABLE} {DIRECT_NET_SET} {{ {', '.join(direct_ips)} }}"
        )
    rc, _out, err = run(
        ["nft", "-f", "-"], timeout=5.0, input_text="\n".join(lines) + "\n"
    )
    if rc == 0:
        # DHCP 换 IP 后 provider 里的源地址也要跟着换，否则设备会被 CN 规则直连
        rule_provider.sync(config, proxy_ips)
    return {
        "ok": rc == 0,
        "updated": rc == 0,
        "proxy": len(proxy_ips),
        "direct": len(direct_ips),
        "error": "" if rc == 0 else err.strip(),
    }


def tproxy_status() -> dict[str, Any]:
    """读取 TPROXY 规则的真实状态：表是否存在、规则是否命中（counter）。

    只看"表存在"会把"规则装上了但没匹配任何包"误判成已接管，这正是下游
    流量被 masquerade 成 198.18.0.1 时面板仍显示正常的原因。
    """
    rc, out, err = run(["nft", "-j", "list", "table", "ip", NFT_TABLE], timeout=2.0)
    mark = src_valid_mark()
    if rc != 0:
        return {
            "installed": False,
            "rule": False,
            "packets": 0,
            "matched": 0,
            "bytes": 0,
            "src_valid_mark": mark,
            "error": (err or out).strip(),
        }
    packets = 0
    packets_bytes = 0
    matched = 0
    seen_rule = False
    try:
        data = json.loads(out)
        for item in data.get("nftables", []):
            rule = item.get("rule")
            if not rule:
                continue
            exprs = rule.get("expr") or []
            has_tproxy = any(isinstance(expr, dict) and "tproxy" in expr for expr in exprs)
            for expr in exprs:
                if not (isinstance(expr, dict) and "counter" in expr):
                    continue
                counter = expr.get("counter") or {}
                count = int(counter.get("packets") or 0)
                matched += count
                if has_tproxy:
                    packets += count
                    packets_bytes = int(counter.get("bytes") or 0)
            seen_rule = seen_rule or has_tproxy
        if seen_rule is False:
            return {"installed": True, "rule": False, "packets": 0, "matched": 0,
                    "bytes": 0, "src_valid_mark": mark, "error": ""}
        return {
            "installed": True,
            "rule": True,
            "packets": packets,
            "matched": matched,
            "bytes": packets_bytes,
            "src_valid_mark": mark,
            "error": "",
        }
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "installed": True,
            "rule": False,
            "packets": 0,
            "matched": 0,
            "bytes": 0,
            "src_valid_mark": mark,
            "error": str(exc),
        }


def tproxy_installed() -> bool:
    rc, _out, _err = run(["nft", "list", "table", "ip", NFT_TABLE], timeout=2.0)
    return rc == 0


def tproxy_active() -> bool:
    """只有规则存在且确实命中过流量，才算真正接管。"""
    status = tproxy_status()
    return bool(status["installed"] and status["packets"] > 0)
