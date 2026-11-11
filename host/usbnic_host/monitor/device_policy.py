"""下游设备统一走代理：不再按设备区分策略。

总代理开关打开时，所有下游设备（本机地址除外）的当前 IP 都会进入 nft 集合
和 mihomo 的 `usbnic-proxy` provider；关闭时撤表并清空，全部直连。
这里保留 `assignments()` / `table()` 两个入口，但策略恒为 proxy，不读写
任何策略文件，也不提供修改接口，避免出现"存储值说走代理、实际却直连"的
假状态。

IP 只是 ARP / DHCP 租约给出的瞬时映射，集合由吞吐采样线程周期刷新。
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..util import ip_sort_key, normalize_mac

POLICY_PROXY = "proxy"
POLICY_DIRECT = "direct"


def load(config: Config) -> dict[str, Any]:
    """固定返回"全部走代理"，忽略任何历史策略文件。"""
    return {"default": POLICY_PROXY, "devices": {}}


def resolve(config: Config, mac: str, data: dict[str, Any] | None = None) -> str:
    return POLICY_PROXY


def assignments(
    config: Config, items: list[dict[str, Any]] | None = None
) -> tuple[list[str], list[str]]:
    """返回（走代理的当前 IP, 直连的当前 IP），用于生成 nft 集合。"""
    if items is None:
        from . import device_stats

        items = device_stats.device_table(config)
    proxy_ips: list[str] = []
    for item in items:
        ip = str(item.get("ip") or "")
        mac = normalize_mac(str(item.get("mac") or ""))
        if not ip or not mac:
            continue
        # 本机地址（USB 网口 / TUN / VPN）永远不进下游集合，主机走自己的 TUN 开关
        if item.get("is_local"):
            continue
        proxy_ips.append(ip)
    return sorted(set(proxy_ips), key=ip_sort_key), []


def table(config: Config, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """设备列表（叠加真实生效状态），只读。"""
    if items is None:
        from . import device_stats

        items = device_stats.device_table(config)
    active = _tproxy_active()
    rows: list[dict[str, Any]] = []
    for item in items:
        ip = str(item.get("ip") or "")
        mac = normalize_mac(str(item.get("mac") or ""))
        if not ip or not mac:
            continue
        hostname = str(item.get("hostname") or "")
        rows.append(
            {
                "ip": ip,
                "mac": mac,
                "hostname": hostname,
                "name": config.device_aliases.get(mac) or hostname,
                "policy": POLICY_PROXY,
                # 没装 TPROXY 表时所有设备实际都是直连
                "effective": POLICY_PROXY if active else POLICY_DIRECT,
                "online": True,
            }
        )
    rows.sort(key=lambda row: (ip_sort_key(row["ip"] or "255.255.255.255"), row["mac"]))
    return {"ok": True, "default": POLICY_PROXY, "active": active, "items": rows}


def _tproxy_active() -> bool:
    from . import proxy

    return proxy.tproxy_installed()
