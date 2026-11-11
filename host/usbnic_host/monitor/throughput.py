"""网口层实时吞吐采样。

主机在做网关时，家里所有流量都会经过上行网口，因此直接读
/sys 暴露的网卡收发计数即可得到总吞吐（含 NAT、代理转发、本地互传）。
采样在进程内完成，前端只读内存环形缓冲，不会每个标签页各采一份。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

import psutil

from ..config import Config
from ..monitor.collector import Monitor

WINDOW_SECONDS = 60
SAMPLE_INTERVAL = 1.0
# 页面停止请求后，再采样这么久就暂停（默认不后台常驻）
DEMAND_GRACE = 5.0


def _is_virtual(name: str) -> bool:
    lowered = (name or "").lower()
    return lowered.startswith(
        ("lo", "tun", "utun", "meta", "clash", "tailscale", "docker", "veth", "br-")
    )


class ThroughputMonitor:
    def __init__(self, config: Config, monitor: Monitor | None = None) -> None:
        self.config = config
        self.monitor = monitor
        self._lock = threading.RLock()
        self._rings: dict[str, deque[dict[str, float]]] = {}
        self._baseline: dict[str, tuple[float, int, int]] = {}
        self._last_demand = 0.0
        self._keep = False
        self._devices: list[dict[str, float]] = []
        self._device_prev: tuple[float, dict[str, tuple[int, int]]] | None = None
        self._device_retry = 0.0
        self._policy_retry = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="throughput-monitor", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_keep(self, enabled: bool) -> None:
        with self._lock:
            self._keep = bool(enabled)
            self._last_demand = time.monotonic()
        if not enabled:
            self._baseline.clear()

    def snapshot(self, iface: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._last_demand = time.monotonic()
            counters = psutil.net_io_counters(pernic=True)
            stats = psutil.net_if_stats()
            names = [name for name in counters if name != "lo"]
            chosen = self._pick_iface(iface, names)
            ring = self._rings.get(chosen) or deque()
            samples = list(ring)[-WINDOW_SECONDS:]
            keep = self._keep
        values_rx = [point["rx"] for point in samples]
        values_tx = [point["tx"] for point in samples]
        current = samples[-1] if samples else {"rx": 0.0, "tx": 0.0}
        avg = {
            "rx": round(sum(values_rx) / len(values_rx), 2) if values_rx else 0.0,
            "tx": round(sum(values_tx) / len(values_tx), 2) if values_tx else 0.0,
        }
        peak = {
            "rx": round(max(values_rx), 2) if values_rx else 0.0,
            "tx": round(max(values_tx), 2) if values_tx else 0.0,
        }
        interfaces = []
        for name in names:
            item = stats.get(name)
            interfaces.append(
                {
                    "name": name,
                    "up": bool(item.isup) if item else False,
                    "default": name == chosen,
                    "virtual": _is_virtual(name),
                }
            )
        interfaces.sort(
            key=lambda item: (
                not item["default"],
                item["virtual"],
                not item["up"],
                item["name"],
            )
        )
        return {
            "ok": True,
            "iface": chosen,
            "interfaces": interfaces,
            "window_s": WINDOW_SECONDS,
            "active": bool(samples) and time.time() - samples[-1]["t"] < SAMPLE_INTERVAL * 3,
            "keep": keep,
            "current": {
                "rx": round(float(current.get("rx", 0.0)), 2),
                "tx": round(float(current.get("tx", 0.0)), 2),
            },
            "avg": avg,
            "peak": peak,
            "samples": samples,
            "note": "网口层吞吐（NIC），包含 NAT、代理转发、本地互传的全部流量，不是单个设备的速度",
        }

    def _pick_iface(self, requested: str | None, names: list[str]) -> str:
        if requested and requested in names:
            return requested
        default = ""
        if self.monitor is not None:
            try:
                default = str((self.monitor.snapshot().get("uplink") or {}).get("dev") or "")
            except Exception:  # noqa: BLE001
                default = ""
        if default in names:
            return default
        if "enp49s0" in names:
            return "enp49s0"
        for name in names:
            if not _is_virtual(name):
                return name
        return names[0] if names else "enp49s0"

    def _loop(self) -> None:
        while not self._stop.wait(SAMPLE_INTERVAL):
            now = time.monotonic()
            # 统计表只是只读计数器，与代理开关、页面是否打开无关，定期自愈
            self._ensure_device_table(now)
            # DHCP 换 IP 后及时把新地址同步进 TPROXY 集合；表不存在时是空操作
            self._refresh_proxy_sets(now)
            with self._lock:
                active = self._keep or (now - self._last_demand <= DEMAND_GRACE)
            if not active:
                # 暂停后恢复时重新建立基线，避免把空档期的流量算进 1 秒速率
                if self._baseline:
                    self._baseline.clear()
                continue
            counters = psutil.net_io_counters(pernic=True)
            with self._lock:
                for name, io in counters.items():
                    if name == "lo":
                        continue
                    previous = self._baseline.get(name)
                    self._baseline[name] = (now, io.bytes_recv, io.bytes_sent)
                    if previous is None:
                        continue
                    elapsed = now - previous[0]
                    if elapsed <= 0:
                        continue
                    rx = max(0, io.bytes_recv - previous[1]) * 8 / elapsed / 1e6
                    tx = max(0, io.bytes_sent - previous[2]) * 8 / elapsed / 1e6
                    ring = self._rings.setdefault(
                        name, deque(maxlen=WINDOW_SECONDS + 5)
                    )
                    ring.append(
                        {"t": round(time.time(), 3), "rx": round(rx, 2), "tx": round(tx, 2)}
                    )
                for name in list(self._rings):
                    if name not in counters:
                        self._rings.pop(name, None)
                        self._baseline.pop(name, None)
            self._sample_devices(now)

    def _ensure_device_table(self, now: float) -> None:
        """每 30 秒确认一次计数表存在；与代理、页面打开与否无关。"""
        from . import device_stats

        if now < self._device_retry:
            return
        self._device_retry = now + 30.0
        try:
            device_stats.ensure_if_missing(self.config)
        except Exception:  # noqa: BLE001
            pass

    def _refresh_proxy_sets(self, now: float) -> None:
        """每 10 秒把 ARP/租约里的新 IP 同步进 nft 集合（只读，无表时直接返回）。"""
        from . import proxy

        if now < self._policy_retry:
            return
        self._policy_retry = now + 10.0
        try:
            proxy.refresh_proxy_set(self.config)
        except Exception:  # noqa: BLE001
            pass

    def _sample_devices(self, now: float) -> None:
        """每秒读一次 nft 计数器，按设备做差算速率（只读，不影响转发）。"""
        from . import device_stats

        try:
            items = device_stats.device_table(self.config)
        except Exception:  # noqa: BLE001
            # 单次读取失败不能拖死采样线程，下一轮继续
            return
        if not items:
            return
        previous = self._device_prev
        if previous is not None:
            elapsed = now - previous[0]
            if 0 < elapsed <= 10.0:
                for item in items:
                    old = previous[1].get(str(item["ip"]))
                    if old is None:
                        continue
                    item["rx_rate"] = max(0, item["rx_bytes"] - old[0]) * 8 / elapsed / 1e6
                    item["tx_rate"] = max(0, item["tx_bytes"] - old[1]) * 8 / elapsed / 1e6
        for item in items:
            item.setdefault("rx_rate", 0.0)
            item.setdefault("tx_rate", 0.0)
        with self._lock:
            self._devices = items
            self._device_prev = (
                now,
                {str(item["ip"]): (item["rx_bytes"], item["tx_bytes"]) for item in items},
            )

    def device_snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._last_demand = time.monotonic()
            if not self._baseline:
                # 页面刚打开，先建立基线，下一秒开始有速率
                self._device_prev = None
            items = [dict(item) for item in self._devices]
        try:
            ifaces = (self.monitor.snapshot().get("interfaces") or []) if self.monitor else []
        except Exception:  # noqa: BLE001
            ifaces = []
        local_map: dict[str, dict[str, Any]] = {}
        for iface in ifaces:
            for cidr in list(iface.get("ipv4") or []) + list(iface.get("ipv6") or []):
                local_map[str(cidr).split("/", 1)[0]] = iface
        from .sources import get_local_addresses

        for ip, name in get_local_addresses().items():
            local_map.setdefault(ip, {"name": name, "type": "vpn" if name == "Meta" else ""})
        from .devices import _local_label

        kept = []
        for item in items:
            ip = str(item.get("ip") or "")
            iface = local_map.get(ip)
            if iface is not None or ip.startswith("198.18."):
                item["is_local"] = True
                item["name"] = _local_label(iface, ip)
            elif ":" in ip and not item.get("mac"):
                item["name"] = "仅 IPv6，无法识别厂商"
                item["name_note"] = "该地址没有 MAC，无法查厂商或主机名"
            elif item.get("mac") and not item.get("hostname"):
                from .sources import is_local_mac

                if is_local_mac(item["mac"]):
                    item["name"] = "设备（随机 MAC）"
                    item["mac_random"] = True
                    item["name_note"] = "MAC 未在 IEEE 注册，且没有可用的主机名"
            if not (self.config.hide_local_addresses and item.get("is_local")):
                kept.append(item)
        items = kept
        items.sort(key=lambda item: (item.get("rx_rate", 0) + item.get("tx_rate", 0)), reverse=True)
        for item in items:
            item["rx_rate"] = round(float(item.get("rx_rate") or 0.0), 2)
            item["tx_rate"] = round(float(item.get("tx_rate") or 0.0), 2)
        return {
            "ok": True,
            "ts": round(time.time(), 3),
            "count": len(items),
            "items": items,
            "note": "按源/目的 IP 统计的 LAN 侧转发速率，累计量来自 nftables 计数器",
        }
