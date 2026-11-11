"""系统健康采样：CPU、内存、swap、磁盘可用空间与温度。

低频（默认 30 秒）写入 host.db，随 usbnicd 一起启停。温度优先读
coretemp 的 Package id 0，没有 CPU 传感器时退回 nvme / 其它可用传感器。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import psutil

from ..config import Config
from .storage import Storage

logger = logging.getLogger("usbnic.health")

_PRUNE_INTERVAL = 6 * 3600.0


class SystemHealthMonitor:
    def __init__(self, config: Config, storage: Storage) -> None:
        self.config = config
        self.storage = storage
        self._interval = max(5.0, float(config.system_health_interval))
        self._lock = threading.RLock()
        self._latest: dict[str, Any] = {}
        self._last_prune = 0.0
        self._started = False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="system-health", daemon=True
        )

    def start(self) -> None:
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._started:
            self._thread.join(timeout=3.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._sample()
            except Exception as exc:  # noqa: BLE001 - 采集线程必须存活
                logger.warning("系统健康采样失败: %s", exc)
            elapsed = time.monotonic() - started
            self._stop.wait(max(1.0, self._interval - elapsed))

    def _sample(self) -> None:
        ts = time.time()
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        try:
            disk = psutil.disk_usage(str(self.config.data_dir))
            disk_pct = float(disk.percent)
            disk_free = int(disk.free)
            disk_total = int(disk.total)
        except OSError:
            disk_pct = 0.0
            disk_free = 0
            disk_total = 0
        temp_c, temp_source = self.temperature()
        sample = {
            "ts": ts,
            # 0.2s 阻塞采样：只在健康线程里，每 30 秒一次，比跨线程基线更可靠
            "cpu_pct": round(float(psutil.cpu_percent(interval=0.2)), 1),
            "mem_pct": round(float(memory.percent), 1),
            "mem_used": int(memory.used),
            "mem_total": int(memory.total),
            "swap_pct": round(float(swap.percent), 1),
            "disk_pct": round(disk_pct, 1),
            "disk_free": disk_free,
            "disk_total": disk_total,
            "temp_c": temp_c,
            "temp_source": temp_source,
        }
        self.storage.add_health_sample(sample)
        with self._lock:
            self._latest = sample
        if ts - self._last_prune >= _PRUNE_INTERVAL:
            self._last_prune = ts
            try:
                cutoff = ts - max(1, self.config.system_health_retention_days) * 86400
                self.storage.prune_health(cutoff)
            except Exception as exc:  # noqa: BLE001
                logger.warning("清理健康曲线历史失败: %s", exc)

    @staticmethod
    def temperature() -> tuple[float | None, str]:
        try:
            groups = psutil.sensors_temperatures() or {}
        except Exception:  # noqa: BLE001 - 某些内核/容器没有传感器
            return None, ""

        for group in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            entries = groups.get(group) or []
            for preferred in ("Package id 0", "Tctl", "Tdie"):
                for entry in entries:
                    if entry.label == preferred and entry.current:
                        return round(float(entry.current), 1), f"{group}:{preferred}"
            values = [entry.current for entry in entries if entry.current]
            if values:
                return round(max(values), 1), group

        for entry in groups.get("nvme") or []:
            if entry.label == "Composite" and entry.current:
                return round(float(entry.current), 1), "nvme:Composite"
        values = [
            entry.current
            for entries in groups.values()
            for entry in entries
            if entry.current
        ]
        if values:
            return round(max(values), 1), "sensors:max"
        return None, ""

    def snapshot(self, hours: float = 6.0, max_points: int = 240) -> dict[str, Any]:
        hours = max(0.5, min(float(hours), 24.0 * 30))
        max_points = max(30, min(int(max_points), 600))
        now = time.time()
        points = self.storage.health_series(now - hours * 3600, max_points)
        with self._lock:
            latest = dict(self._latest) if self._latest else None
        if latest is None:
            latest = self.storage.health_latest()
        return {
            "ok": True,
            "hours": hours,
            "interval": self._interval,
            "disk_path": str(self.config.data_dir),
            "latest": latest,
            "points": points,
            "retention_days": self.config.system_health_retention_days,
            "note": "CPU / 内存 / swap 为百分比；磁盘可用空间取数据目录所在分区；温度优先 CPU 封装",
        }
