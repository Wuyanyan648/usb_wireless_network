from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..config import Config
from ..exec import run
from ..net.interfaces import get_interface_gateway
from ..util import parse_ping


class HealthChecker:
    """对默认路由和候选上行做主动探测。"""

    def __init__(self, config: Config) -> None:
        self.config = config

    def check_all(
        self, interfaces: list[dict[str, Any]], routes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        route_by_dev: dict[str, dict[str, Any]] = {}
        for route in routes:
            route_by_dev.setdefault(route["dev"], route)
        candidates = [
            iface
            for iface in interfaces
            if iface.get("type") in ("ethernet", "wifi")
            and not iface.get("excluded")
            and iface.get("ipv4")
            and (
                iface.get("is_default")
                or iface["name"] in self.config.uplink_candidates
            )
        ]
        if not candidates:
            return {"checked_at": time.time(), "interfaces": {}, "error": "没有可探测的上行接口"}

        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
            futures = {
                pool.submit(self._check_one, iface, route_by_dev.get(iface["name"])): iface
                for iface in candidates
            }
            for future, iface in futures.items():
                try:
                    results[iface["name"]] = future.result()
                except Exception as exc:  # noqa: BLE001 - 采集器不能因单个接口失败而退出
                    results[iface["name"]] = {
                        "iface": iface["name"],
                        "error": str(exc),
                        "gateway": "",
                        "gateway_ok": False,
                        "gateway_rtt_ms": None,
                        "internet_ok": False,
                        "internet_target": "",
                        "internet_rtt_ms": None,
                        "dns_ok": False,
                        "dns_ms": None,
                        "score": 0,
                        "checked_at": time.time(),
                    }
        return {"checked_at": time.time(), "interfaces": results, "error": ""}

    def _check_one(
        self, iface: dict[str, Any], route: dict[str, Any] | None
    ) -> dict[str, Any]:
        name = iface["name"]
        gateway = (route or {}).get("gateway") or iface.get("default_gateway") or ""
        if not gateway:
            gateway = get_interface_gateway(name)
        is_default = bool(route)

        gateway_result = self._ping(name, gateway) if gateway else {
            "ok": False,
            "rtt_ms": None,
            "loss_pct": None,
        }
        internet_result = {
            "ok": None,
            "rtt_ms": None,
            "target": "",
            "method": "",
        }
        dns_result = {"ok": None, "ms": None}

        if is_default:
            for target in self.config.public_targets:
                result = self._ping(name, target)
                if result["ok"]:
                    internet_result = {
                        "ok": True,
                        "rtt_ms": result["rtt_ms"],
                        "target": target,
                        "method": "icmp",
                    }
                    break
            else:
                fallback = self._http_probe(name)
                if fallback["ok"]:
                    internet_result = {
                        "ok": True,
                        "rtt_ms": fallback["rtt_ms"],
                        "target": fallback["target"],
                        "method": "http",
                    }
                else:
                    internet_result = {
                        "ok": False,
                        "rtt_ms": None,
                        "target": self.config.public_targets[0]
                        if self.config.public_targets
                        else "",
                        "method": "",
                    }
            dns_result = self._dns()

        score = 0
        if gateway_result["ok"]:
            score += 40
        if internet_result["ok"] is True:
            score += 40
        if dns_result["ok"] is True:
            score += 20
        if not is_default:
            score = 40 if gateway_result["ok"] else 0

        return {
            "iface": name,
            "gateway": gateway,
            "gateway_ok": bool(gateway_result["ok"]),
            "gateway_rtt_ms": gateway_result["rtt_ms"],
            "internet_ok": internet_result["ok"],
            "internet_target": internet_result["target"],
            "internet_rtt_ms": internet_result["rtt_ms"],
            "internet_method": internet_result["method"],
            "dns_ok": dns_result["ok"],
            "dns_ms": dns_result["ms"],
            "score": score,
            "checked_at": time.time(),
            "error": "",
        }

    def _ping(self, iface: str, target: str) -> dict[str, Any]:
        timeout = max(1, int(round(self.config.health_timeout)))
        rc, out, err = run(
            ["ping", "-n", "-c", "1", "-W", str(timeout), "-I", iface, target],
            timeout=timeout + 1.0,
        )
        parsed = parse_ping(out or err)
        return parsed

    def _http_probe(self, iface: str) -> dict[str, Any]:
        """部分路由器会丢弃 ICMP，用 HTTP 204 探测补足。"""
        timeout = max(1.0, self.config.http_timeout)
        urls = [
            "http://connect.rom.miui.com/generate_204",
            "http://www.baidu.com",
        ]
        for url in urls:
            rc, out, _err = run(
                [
                    "curl",
                    "--interface",
                    iface,
                    "-sS",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code} %{time_total}",
                    "--max-time",
                    str(timeout),
                    url,
                ],
                timeout=timeout + 1.0,
            )
            if rc != 0 or not out.strip():
                continue
            parts = out.split()
            if not parts:
                continue
            code = parts[0]
            try:
                elapsed_ms = round(float(parts[1]) * 1000, 1) if len(parts) > 1 else None
            except ValueError:
                elapsed_ms = None
            if code.isdigit() and 200 <= int(code) < 400:
                return {"ok": True, "rtt_ms": elapsed_ms, "target": url}
        return {"ok": False, "rtt_ms": None, "target": urls[0]}

    def _dns(self) -> dict[str, Any]:
        started = time.monotonic()
        rc, out, _err = run(
            ["getent", "hosts", self.config.dns_target],
            timeout=max(1.0, self.config.health_timeout + 1.0),
        )
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        if rc == 0 and out.strip():
            return {"ok": True, "ms": elapsed_ms}
        return {"ok": False, "ms": elapsed_ms}
