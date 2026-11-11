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
            with self._lock:
                active = self._keep or (time.monotonic() - self._last_demand <= DEMAND_GRACE)
            if not active:
                # 暂停后恢复时重新建立基线，避免把空档期的流量算进 1 秒速率
                if self._baseline:
                    self._baseline.clear()
                continue
            now = time.monotonic()
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
