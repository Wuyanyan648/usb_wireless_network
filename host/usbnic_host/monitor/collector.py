from __future__ import annotations

import copy
import os
import threading
import time
from typing import Any

from .. import __version__
from ..config import Config
from ..net.interfaces import get_default_routes, get_interfaces
from ..util import hostname, load_average, uptime_seconds
from .devices import DeviceTracker, build_devices
from .health import HealthChecker
from .sources import get_clash, get_dhcp_leases, get_neigh, get_wifi_stations
from .storage import Storage


class Monitor:
    """后台采集器：Phase 1 只读，不修改任何网络配置。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.lock = threading.RLock()
        self._latest: dict[str, Any] = {"ready": False, "ts": time.time()}
        self._prev_counters: dict[str, tuple[float, int, int]] = {}
        self._health: dict[str, Any] = {"checked_at": 0, "interfaces": {}, "error": ""}
        self._health_ts = 0.0
        self._device_state: dict[str, bool] = {}
        self._last_uplink = ""
        self._last_proxy_active: bool | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="usbnic-monitor", daemon=True)
        self.device_tracker = DeviceTracker(offline_after=180.0)
        self.health_checker = HealthChecker(config)
        self.storage = Storage(config)

    def start(self) -> None:
        self.storage.init()
        self.storage.add_event("info", "监控服务启动（只读模式）")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)
        self.storage.close()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self._latest)

    def history(self, iface: str | None = None, minutes: int = 30) -> dict[str, Any]:
        if not iface:
            with self.lock:
                iface = (self._latest.get("uplink") or {}).get("dev") or ""
        return self.storage.history(iface or None, minutes)

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.storage.events(limit)

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._collect()
            except Exception as exc:  # noqa: BLE001 - 采集线程必须存活
                self.storage.add_event("error", f"采集失败: {exc}")
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.5, self.config.poll_interval - elapsed))

    def _collect(self) -> None:
        ts = time.time()
        interfaces = get_interfaces(self.config)
        self._apply_rates(interfaces, ts)
        routes = get_default_routes()

        if ts - self._health_ts >= self.config.health_interval:
            self._health = self.health_checker.check_all(interfaces, routes)
            self._health_ts = ts

        leases = get_dhcp_leases()
        neigh = get_neigh()
        stations: list[dict[str, Any]] = []
        for iface in interfaces:
            if iface.get("wifi_mode") == "AP":
                stations.extend(get_wifi_stations(iface["name"]))

        clash = get_clash(self.config)
        current_devices = build_devices(
            interfaces, leases, neigh, stations, clash, self.config, ts
        )
        device_items = self.device_tracker.update(current_devices, ts)
        devices_summary = {
            "total": len(device_items),
            "online": sum(1 for item in device_items if item.get("online")),
            "wifi_ap": sum(1 for item in device_items if item.get("type") == "wifi-ap"),
            "wired": sum(1 for item in device_items if item.get("type") == "wired"),
            "nintendo": sum(
                1
                for item in device_items
                if "nintendo" in (item.get("vendor") or "").lower()
                or "Nintendo" in (item.get("name") or "")
            ),
            "proxied": sum(
                1 for item in device_items if int(item.get("proxy_connections") or 0) > 0
            ),
        }

        uplink = self._build_uplink(interfaces, routes, health=self._health)
        proxy = self._build_proxy(interfaces, routes, clash)
        warnings = self._build_warnings(
            interfaces, routes, uplink, proxy, leases, devices_summary
        )

        self._record_transitions(device_items, uplink, proxy)

        snapshot = {
            "ready": True,
            "version": __version__,
            "ts": ts,
            "hostname": hostname(),
            "uptime": uptime_seconds(),
            "load": load_average(),
            "mode": "monitor",
            "readonly": True,
            "uplink": uplink,
            "default_routes": routes,
            "interfaces": interfaces,
            "devices": {"summary": devices_summary, "items": device_items},
            "health": self._health,
            "proxy": proxy,
            "warnings": warnings,
        }
        with self.lock:
            self._latest = snapshot
        self.storage.record(snapshot)

    def _apply_rates(self, interfaces: list[dict[str, Any]], ts: float) -> None:
        for iface in interfaces:
            name = iface["name"]
            rx = int(iface.get("rx_bytes") or 0)
            tx = int(iface.get("tx_bytes") or 0)
            previous = self._prev_counters.get(name)
            rx_rate = 0.0
            tx_rate = 0.0
            if previous:
                dt = ts - previous[0]
                if dt > 0:
                    rx_rate = max(0.0, (rx - previous[1]) / dt)
                    tx_rate = max(0.0, (tx - previous[2]) / dt)
            self._prev_counters[name] = (ts, rx, tx)
            iface["rx_rate"] = rx_rate
            iface["tx_rate"] = tx_rate

    def _build_uplink(
        self,
        interfaces: list[dict[str, Any]],
        routes: list[dict[str, Any]],
        health: dict[str, Any],
    ) -> dict[str, Any]:
        if not routes:
            return {
                "dev": "",
                "gateway": "",
                "metric": None,
                "ipv4": "",
                "speed_mbps": None,
                "rx_rate": 0.0,
                "tx_rate": 0.0,
                "health": {},
            }
        route = routes[0]
        dev = route["dev"]
        iface = next((item for item in interfaces if item["name"] == dev), {})
        return {
            "dev": dev,
            "gateway": route.get("gateway") or "",
            "metric": route.get("metric"),
            "ipv4": (iface.get("ipv4") or [""])[0],
            "speed_mbps": iface.get("speed_mbps"),
            "rx_rate": iface.get("rx_rate") or 0.0,
            "tx_rate": iface.get("tx_rate") or 0.0,
            "health": (health.get("interfaces") or {}).get(dev, {}),
            "interface": iface,
        }

    def _build_proxy(
        self,
        interfaces: list[dict[str, Any]],
        routes: list[dict[str, Any]],
        clash: dict[str, Any],
    ) -> dict[str, Any]:
        tun = next(
            (
                iface
                for iface in interfaces
                if iface.get("up")
                and iface.get("type") == "vpn"
                and iface["name"].lower().startswith(("tun", "utun", "meta", "clash"))
            ),
            None,
        )
        tun_name = tun["name"] if tun else ""
        default_via_tun = bool(tun_name) and any(
            route.get("dev") == tun_name for route in routes
        )
        clash_ok = bool(clash.get("ok"))
        active_connections = len(clash.get("connections") or [])
        if clash_ok and default_via_tun:
            status = "active"
        elif clash_ok:
            status = "clash_only"
        elif tun_name:
            status = "tun_only"
        else:
            status = "off"
        return {
            "status": status,
            "tun": tun_name,
            "default_via_tun": default_via_tun,
            "clash_ok": clash_ok,
            "error": clash.get("error") or "",
            "active_connections": active_connections,
            "upload_total": int(clash.get("upload_total") or 0),
            "download_total": int(clash.get("download_total") or 0),
            "memory": int(clash.get("memory") or 0),
        }

    def _build_warnings(
        self,
        interfaces: list[dict[str, Any]],
        routes: list[dict[str, Any]],
        uplink: dict[str, Any],
        proxy: dict[str, Any],
        leases: list[dict[str, Any]],
        devices_summary: dict[str, Any],
    ) -> list[str]:
        warnings: list[str] = []
        if os.geteuid() != 0:
            warnings.append("当前非 root 运行，DHCP 租约和部分 iw 数据可能缺失")
        if not routes:
            warnings.append("当前没有 IPv4 默认路由，PC 自身无法上网")
        elif len(routes) > 1:
            warnings.append(
                "存在多条默认路由（"
                + ", ".join(f"{r['dev']} metric {r['metric']}" for r in routes)
                + "），可能发生上行切换"
            )
        uplink_iface = uplink.get("interface") or {}
        if uplink_iface.get("usb_id"):
            warnings.append(
                f"当前默认上行是 USB 网卡 {uplink_iface.get('name')}；模式 3 需要把它改为下游 shared 网关"
            )
        health = uplink.get("health") or {}
        if health.get("internet_ok") is False:
            warnings.append("当前默认上行的公网探测失败")
        if not leases:
            warnings.append("未读取到 DHCP 租约；模式 2/3 的客户端识别会不完整")
            if not any(i.get("wifi_mode") == "AP" for i in interfaces):
                warnings.append("当前不是 DHCP 网关，设备列表依赖 ARP 发现，可能不完整")
        ipv6_interfaces = [i["name"] for i in interfaces if i.get("ipv6")]
        if ipv6_interfaces:
            warnings.append(
                "检测到 IPv6 地址（"
                + ", ".join(ipv6_interfaces[:3])
                + "）；若下游下发 IPv6，可能绕过 Clash 代理"
            )
        if proxy.get("status") != "active":
            warnings.append("Clash TUN 未处于活动状态；下游设备暂时无法共享代理")
        if devices_summary.get("total", 0) == 0:
            warnings.append("暂未发现下游设备；接入设备后这里会显示 IP/MAC/流量")
        return warnings

    def _record_transitions(
        self,
        devices: list[dict[str, Any]],
        uplink: dict[str, Any],
        proxy: dict[str, Any],
    ) -> None:
        current_state = {
            item["key"]: bool(item.get("online")) for item in devices if item.get("key")
        }
        for key, online in current_state.items():
            previous = self._device_state.get(key)
            if previous is None and online:
                item = next((d for d in devices if d.get("key") == key), {})
                self.storage.add_event(
                    "info",
                    f"设备上线: {item.get('name') or key} ({item.get('ip') or '-'})",
                )
            elif previous is True and not online:
                item = next((d for d in devices if d.get("key") == key), {})
                self.storage.add_event(
                    "info",
                    f"设备离线: {item.get('name') or key} ({item.get('ip') or '-'})",
                )
        self._device_state = current_state

        current_uplink = uplink.get("dev") or ""
        if self._last_uplink and current_uplink != self._last_uplink:
            self.storage.add_event(
                "warning",
                f"默认上行切换: {self._last_uplink} -> {current_uplink or '无'}",
            )
        self._last_uplink = current_uplink

        current_proxy = proxy.get("status") == "active"
        if self._last_proxy_active is not None and current_proxy != self._last_proxy_active:
            self.storage.add_event(
                "info",
                "Clash TUN 已接管转发流量" if current_proxy else "Clash TUN 已停止接管",
            )
        self._last_proxy_active = current_proxy
