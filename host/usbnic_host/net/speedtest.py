"""宽带测速：后台任务 + 直连约束。

设计要点：
- 测速线程把 socket 绑定到实际上行网卡（SO_BINDTODEVICE，失败则绑源 IP），
  因此流量不会进入 Clash TUN / 7897 代理；
- DNS 也走绑定网卡的直连查询，避免 Clash 的 fake-IP 把域名解析到 198.18.0.0/16；
- 下载使用国内镜像的多线程 Range 请求，上传使用可接收 POST 的端点；
- 采样线程每 0.25s 记录一次速率，HTTP 状态接口只读内存，不阻塞服务。
"""

from __future__ import annotations

import copy
import json
import os
import random
import socket
import ssl
import statistics
import struct
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psutil

from ..config import Config
from ..monitor.collector import Monitor
from ..net.interfaces import get_default_routes

HISTORY_NAME = "speedtest_history.json"
HISTORY_LIMIT = 50
SAMPLE_INTERVAL = 0.25
DOWNLOAD_THREADS_PER_HOST = 2
UPLOAD_THREADS = 4
DOWNLOAD_CHUNK = 8 * 1024 * 1024
UPLOAD_REQUEST_BYTES = 4 * 1024 * 1024
UPLOAD_BLOCK = 256 * 1024
# 低于这个流量视为「接口无流量」，而不是 0 Mbps 成功
MIN_BYTES = 64 * 1024


class SpeedTestError(RuntimeError):
    """带明确原因的测速失败。"""


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".speedtest.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_virtual(name: str) -> bool:
    lowered = (name or "").lower()
    return lowered.startswith(
        ("lo", "tun", "utun", "meta", "clash", "tailscale", "docker", "veth", "br-")
    )


def _bind_socket(sock: socket.socket, dev: str, source_ip: str) -> str:
    """把 socket 固定到物理出口，返回实际使用的绑定方式。"""
    if dev:
        try:
            sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_BINDTODEVICE, (dev + "\0").encode()
            )
            return f"device:{dev}"
        except OSError:
            pass
    if source_ip:
        try:
            sock.bind((source_ip, 0))
            return f"source:{source_ip}"
        except OSError:
            pass
    raise SpeedTestError(
        f"无法绑定出口网卡 {dev or source_ip or '未知'}，为免测到代理速度已中止"
    )


def _parse_dns_answers(data: bytes) -> list[str]:
    if len(data) < 12:
        return []
    ancount = struct.unpack("!H", data[6:8])[0]
    offset = 12
    while offset < len(data) and data[offset]:
        offset += data[offset] + 1
    offset += 5  # 根标签 + QTYPE + QCLASS
    ips: list[str] = []
    for _ in range(ancount):
        if offset >= len(data):
            break
        if data[offset] & 0xC0 == 0xC0:
            offset += 2
        else:
            while offset < len(data) and data[offset]:
                offset += data[offset] + 1
            offset += 1
        if offset + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        if rtype == 1 and rdlen == 4 and offset + 4 <= len(data):
            ips.append(socket.inet_ntoa(data[offset : offset + 4]))
        offset += rdlen
    return ips


def resolve_ipv4(host: str, dev: str, source_ip: str, servers: list[str]) -> list[str]:
    """绑定出口做直连 DNS，绕开 Clash 的 fake-IP。"""
    for server in servers:
        query = struct.pack("!HHHHHH", random.randint(0, 65535), 0x0100, 1, 0, 0, 0)
        for part in host.split("."):
            query += bytes([len(part)]) + part.encode()
        query += b"\x00" + struct.pack("!HH", 1, 1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(2.5)
            _bind_socket(sock, dev, source_ip)
            sock.sendto(query, (server, 53))
            data, _addr = sock.recvfrom(4096)
            ips = _parse_dns_answers(data)
            if ips:
                return ips
        except (OSError, SpeedTestError):
            continue
        finally:
            sock.close()
    raise SpeedTestError(f"直连 DNS 解析失败: {host}")


def _connect_tls(
    host: str,
    port: int,
    dev: str,
    source_ip: str,
    servers: list[str],
    timeout: float = 5.0,
) -> tuple[ssl.SSLSocket, str, str]:
    ips = resolve_ipv4(host, dev, source_ip, servers)
    errors: list[str] = []
    for ip in ips[:3]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            mode = _bind_socket(sock, dev, source_ip)
            sock.connect((ip, port))
            context = ssl.create_default_context()
            tls = context.wrap_socket(sock, server_hostname=host)
            tls.settimeout(timeout)
            return tls, ip, mode
        except (OSError, ssl.SSLError) as exc:
            errors.append(f"{ip}: {exc}")
            try:
                sock.close()
            except OSError:
                pass
    detail = "；".join(errors[-2:]) or "无可用地址"
    raise SpeedTestError(f"{host} 连接失败（{detail}）")


def _read_headers(fp: Any) -> tuple[int, dict[str, str]]:
    line = fp.readline(4096)
    if not line:
        raise SpeedTestError("端点提前断开（没有响应）")
    parts = line.decode("latin-1", errors="replace").split()
    if len(parts) < 2 or not parts[1].isdigit():
        raise SpeedTestError(f"端点响应异常: {line[:80]!r}")
    status = int(parts[1])
    headers: dict[str, str] = {}
    while True:
        raw = fp.readline(4096)
        if raw in (b"\r\n", b"\n", b""):
            break
        key, _, value = raw.decode("latin-1", errors="replace").partition(":")
        headers[key.strip().lower()] = value.strip()
    return status, headers


def _http_download_once(
    host: str,
    path: str,
    start: int,
    length: int,
    dev: str,
    source_ip: str,
    servers: list[str],
    stop: threading.Event,
    counters: dict[str, int],
    counter_lock: threading.Lock,
) -> None:
    tls, _ip, mode = _connect_tls(host, 443, dev, source_ip, servers)
    try:
        counters["bind_mode"] = mode  # 仅用于结果展示
        end = start + length - 1
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "User-Agent: usbnic-speedtest/1.0\r\n"
            "Accept: */*\r\n"
            f"Range: bytes={start}-{end}\r\n"
            "Connection: close\r\n\r\n"
        )
        tls.sendall(request.encode("latin-1"))
        fp = tls.makefile("rb")
        status, _headers = _read_headers(fp)
        if status not in (200, 206):
            raise SpeedTestError(f"{host} 返回 HTTP {status}")
        while not stop.is_set():
            tls.settimeout(1.0)
            try:
                chunk = fp.read(64 * 1024)
            except socket.timeout:
                if stop.is_set():
                    break
                continue
            if not chunk:
                break
            with counter_lock:
                counters["download"] += len(chunk)
                if not counters.get("download_first_byte_at"):
                    counters["download_first_byte_at"] = time.monotonic()
    finally:
        try:
            tls.close()
        except OSError:
            pass


def _download_worker(
    host: str,
    path: str,
    size: int,
    dev: str,
    source_ip: str,
    servers: list[str],
    stop: threading.Event,
    counters: dict[str, int],
    counter_lock: threading.Lock,
    errors: list[str],
    errors_lock: threading.Lock,
) -> None:
    offset = random.randrange(0, max(1, size - DOWNLOAD_CHUNK))
    failures = 0
    while not stop.is_set():
        try:
            _http_download_once(
                host,
                path,
                offset,
                DOWNLOAD_CHUNK,
                dev,
                source_ip,
                servers,
                stop,
                counters,
                counter_lock,
            )
            failures = 0
            offset += DOWNLOAD_CHUNK
            if offset >= size:
                offset = 0
        except (OSError, SpeedTestError, ssl.SSLError) as exc:
            failures += 1
            with errors_lock:
                errors.append(f"{host}: {exc}")
            if failures >= 2:
                return
            time.sleep(0.2)


def _probe_size(host: str, path: str, dev: str, source_ip: str, servers: list[str]) -> int:
    """用 HEAD 取文件大小；取不到时按 38MB 处理。"""
    tls, _ip, _mode = _connect_tls(host, 443, dev, source_ip, servers)
    try:
        request = (
            f"HEAD {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "User-Agent: usbnic-speedtest/1.0\r\n"
            "Connection: close\r\n\r\n"
        )
        tls.sendall(request.encode("latin-1"))
        fp = tls.makefile("rb")
        status, headers = _read_headers(fp)
        if status == 200 and headers.get("content-length", "").isdigit():
            return int(headers["content-length"])
    finally:
        try:
            tls.close()
        except OSError:
            pass
    return 38 * 1024 * 1024


def _upload_worker(
    host: str,
    path: str,
    dev: str,
    source_ip: str,
    servers: list[str],
    stop: threading.Event,
    counters: dict[str, int],
    counter_lock: threading.Lock,
    errors: list[str],
    errors_lock: threading.Lock,
) -> None:
    block = b"\0" * UPLOAD_BLOCK
    failures = 0
    while not stop.is_set():
        try:
            tls, _ip, _mode = _connect_tls(host, 443, dev, source_ip, servers)
        except (OSError, SpeedTestError, ssl.SSLError) as exc:
            failures += 1
            with errors_lock:
                errors.append(f"{host}: {exc}")
            if failures >= 2:
                return
            time.sleep(0.2)
            continue
        try:
            header = (
                f"POST {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "User-Agent: usbnic-speedtest/1.0\r\n"
                "Content-Type: application/octet-stream\r\n"
                f"Content-Length: {UPLOAD_REQUEST_BYTES}\r\n"
                "Connection: close\r\n\r\n"
            )
            tls.sendall(header.encode("latin-1"))
            sent = 0
            while sent < UPLOAD_REQUEST_BYTES and not stop.is_set():
                tls.sendall(block)
                sent += len(block)
                with counter_lock:
                    counters["upload"] += len(block)
                    if not counters.get("upload_first_byte_at"):
                        counters["upload_first_byte_at"] = time.monotonic()
            failures = 0
            fp = tls.makefile("rb")
            status, _headers = _read_headers(fp)
            if status >= 400:
                with errors_lock:
                    errors.append(f"{host} 上传返回 HTTP {status}")
            fp.close()
        except (OSError, SpeedTestError, ssl.SSLError) as exc:
            failures += 1
            with errors_lock:
                errors.append(f"{host}: {exc}")
        finally:
            try:
                tls.close()
            except OSError:
                pass
        if failures >= 2:
            return


def measure_latency(
    target: str,
    dev: str,
    source_ip: str,
    servers: list[str],
    count: int = 12,
) -> tuple[float, float, str]:
    ips = resolve_ipv4(target, dev, source_ip, servers)
    ip = ips[0]
    samples: list[float] = []
    for _ in range(count):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(2.0)
            _bind_socket(sock, dev, source_ip)
            started = time.perf_counter()
            sock.connect((ip, 443))
            samples.append((time.perf_counter() - started) * 1000.0)
        except (OSError, SpeedTestError):
            pass
        finally:
            sock.close()
        time.sleep(0.12)
    if len(samples) < 2:
        raise SpeedTestError(f"延迟探测失败: {target} 无法建立 TCP 连接")
    # 第一次包含 DNS / 握手预热，丢掉
    values = samples[1:] if len(samples) > 4 else samples
    avg = statistics.fmean(values)
    jitter = statistics.pstdev(values)
    return round(avg, 1), round(jitter, 1), ip


class SpeedTestManager:
    def __init__(self, config: Config, monitor: Monitor | None = None) -> None:
        self.config = config
        self.monitor = monitor
        self._lock = threading.RLock()
        self._stop_sample = threading.Event()
        self._status: dict[str, Any] = {
            "running": False,
            "phase": "idle",
            "phase_label": "待测速",
            "progress": 0,
            "started_at": 0,
            "samples": [],
            "live": {"down_mbps": None, "up_mbps": None},
            "result": None,
            "error": "",
        }
        self._history: list[dict[str, Any]] = self._load_history()

    # ---------- 对外接口 ----------
    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._status["running"]:
                return {
                    "ok": False,
                    "busy": True,
                    "error": "已有测速任务在运行，请等这次结束再点",
                }
            self._counter_lock = threading.Lock()
            self._counters = {"download": 0, "upload": 0, "bind_mode": ""}
            self._status = {
                "running": True,
                "phase": "latency",
                "phase_label": "测量延迟",
                "progress": 0,
                "started_at": time.time(),
                "samples": [],
                "live": {"down_mbps": None, "up_mbps": None},
                "result": None,
                "error": "",
            }
            self._stop_sample = threading.Event()
        threading.Thread(target=self._run, name="speedtest", daemon=True).start()
        threading.Thread(
            target=self._sample_loop,
            args=(self._stop_sample,),
            name="speedtest-sample",
            daemon=True,
        ).start()
        return {"ok": True, "running": True}

    def status(self) -> dict[str, Any]:
        with self._lock:
            payload = copy.deepcopy(self._status)
            payload["recent"] = copy.deepcopy(self._history[:12])
            if not payload["running"] and payload["result"] is None and self._history:
                # 页面刷新或服务重启后，仍把最近一次结果展示出来
                payload["result"] = copy.deepcopy(self._history[0])
            return payload

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._history[:limit])

    # ---------- 内部实现 ----------
    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._status.update(fields)

    def _append_sample(self, phase: str, mbps: float, elapsed: float) -> None:
        with self._lock:
            samples = self._status["samples"]
            samples.append(
                {"t": round(elapsed, 2), "mbps": round(mbps, 1), "phase": phase}
            )
            if len(samples) > 1200:
                del samples[: len(samples) - 1200]

    def _sample_loop(self, stop: threading.Event) -> None:
        last = time.monotonic()
        last_down = 0
        last_up = 0
        while not stop.wait(SAMPLE_INTERVAL):
            counters = getattr(self, "_counters", None)
            counter_lock = getattr(self, "_counter_lock", None)
            if counters is None or counter_lock is None:
                continue
            now = time.monotonic()
            with counter_lock:
                down = counters.get("download", 0)
                up = counters.get("upload", 0)
            with self._lock:
                phase = self._status["phase"]
                started = self._status["started_at"]
            dt = now - last
            if dt <= 0:
                continue
            elapsed = time.time() - started
            if phase == "download":
                mbps = (down - last_down) * 8 / dt / 1e6
                self._append_sample("download", max(0.0, mbps), elapsed)
                self._update(live={"down_mbps": round(max(0.0, mbps), 1), "up_mbps": None})
            elif phase == "upload":
                mbps = (up - last_up) * 8 / dt / 1e6
                self._append_sample("upload", max(0.0, mbps), elapsed)
                self._update(live={"down_mbps": None, "up_mbps": round(max(0.0, mbps), 1)})
            last = now
            last_down = down
            last_up = up

    def _pick_interface(self) -> tuple[str, str]:
        snapshot: dict[str, Any] = {}
        if self.monitor is not None:
            try:
                snapshot = self.monitor.snapshot()
            except Exception:  # noqa: BLE001 - 采集未就绪时退回系统查询
                snapshot = {}
        dev = str((snapshot.get("uplink") or {}).get("dev") or "")
        if not dev or _is_virtual(dev):
            for route in get_default_routes():
                candidate = str(route.get("dev") or "")
                if candidate and not _is_virtual(candidate):
                    dev = candidate
                    break
        if not dev:
            raise SpeedTestError("没有找到可用的物理上行网卡")
        source_ip = ""
        for iface in snapshot.get("interfaces") or []:
            if iface.get("name") == dev:
                ips = iface.get("ipv4") or []
                if ips:
                    source_ip = str(ips[0]).split("/")[0]
                break
        if not source_ip:
            for addr in psutil.net_if_addrs().get(dev, []):
                if addr.family == socket.AF_INET:
                    source_ip = addr.address
                    break
        if not source_ip:
            raise SpeedTestError(f"上行网卡 {dev} 没有 IPv4 地址")
        return dev, source_ip

    def _run(self) -> None:
        started = time.monotonic()
        dev = ""
        result: dict[str, Any] | None = None
        error = ""
        try:
            dev, source_ip = self._pick_interface()
            servers = list(self.config.speedtest_dns) or ["223.5.5.5"]
            endpoints = []
            for url in self.config.speedtest_downloads:
                parts = urlsplit(url)
                if parts.hostname:
                    endpoints.append((parts.hostname, parts.path or "/"))
            if not endpoints:
                raise SpeedTestError("没有配置下载测速端点")
            upload = urlsplit(self.config.speedtest_upload_url)

            proxy: dict[str, Any] = {}
            if self.monitor is not None:
                proxy = (self.monitor.snapshot().get("proxy") or {})
            proxy_active = proxy.get("status") == "active"

            # 1) 延迟 / 抖动
            latency: float | None = None
            jitter: float | None = None
            latency_error = ""
            self._update(phase="latency", phase_label="测量延迟", progress=3)
            try:
                latency, jitter, _ip = measure_latency(
                    self.config.speedtest_latency_target, dev, source_ip, servers
                )
            except SpeedTestError as exc:
                latency_error = str(exc)
            self._update(progress=8)

            # 2) 下载
            counter_lock = self._counter_lock
            counters = self._counters
            self._update(phase="download", phase_label="下载测试", progress=10)
            stop_download = threading.Event()
            errors: list[str] = []
            errors_lock = threading.Lock()
            sizes: dict[str, int] = {}
            probe_threads: list[threading.Thread] = []
            for host, path in endpoints:
                def probe(h: str = host, p: str = path) -> None:
                    try:
                        sizes[h] = _probe_size(h, p, dev, source_ip, servers)
                    except (OSError, SpeedTestError, ssl.SSLError):
                        sizes[h] = 38 * 1024 * 1024

                thread = threading.Thread(target=probe, daemon=True)
                thread.start()
                probe_threads.append(thread)
            for thread in probe_threads:
                thread.join(timeout=3.5)
            workers: list[threading.Thread] = []
            for host, path in endpoints:
                for _ in range(DOWNLOAD_THREADS_PER_HOST):
                    thread = threading.Thread(
                        target=_download_worker,
                        args=(
                            host,
                            path,
                            sizes.get(host, 38 * 1024 * 1024),
                            dev,
                            source_ip,
                            servers,
                            stop_download,
                            counters,
                            counter_lock,
                            errors,
                            errors_lock,
                        ),
                        daemon=True,
                    )
                    thread.start()
                    workers.append(thread)
            download_deadline = time.monotonic() + self.config.speedtest_duration
            target_bytes = self.config.speedtest_max_mb * 1024 * 1024
            while time.monotonic() < download_deadline:
                with counter_lock:
                    if counters["download"] >= target_bytes:
                        break
                if all(not thread.is_alive() for thread in workers):
                    break
                time.sleep(0.1)
            stop_download.set()
            for thread in workers:
                thread.join(timeout=3.0)
            with counter_lock:
                down_bytes = counters["download"]
                first_byte = counters.get("download_first_byte_at") or 0.0
                bind_mode = counters.get("bind_mode") or ""
            active = max(0.5, time.monotonic() - first_byte) if first_byte else 0.5
            down_mbps = round(down_bytes * 8 / active / 1e6, 1)
            self._update(progress=68)
            if down_bytes < MIN_BYTES:
                detail = "；".join(errors[-3:]) or "6 秒内没有收到有效数据"
                raise SpeedTestError(f"下载失败：{dev} 无流量（{detail}）")

            # 3) 上传
            up_mbps: float | None = None
            upload_error = ""
            up_bytes = 0
            if upload.hostname:
                self._update(phase="upload", phase_label="上传测试", progress=72)
                stop_upload = threading.Event()
                upload_workers: list[threading.Thread] = []
                for _ in range(UPLOAD_THREADS):
                    thread = threading.Thread(
                        target=_upload_worker,
                        args=(
                            upload.hostname,
                            upload.path or "/",
                            dev,
                            source_ip,
                            servers,
                            stop_upload,
                            counters,
                            counter_lock,
                            errors,
                            errors_lock,
                        ),
                        daemon=True,
                    )
                    thread.start()
                    upload_workers.append(thread)
                upload_deadline = time.monotonic() + 5.0
                upload_target = 150 * 1024 * 1024
                while time.monotonic() < upload_deadline:
                    with counter_lock:
                        if counters["upload"] >= upload_target:
                            break
                    if all(not thread.is_alive() for thread in upload_workers):
                        break
                    time.sleep(0.1)
                stop_upload.set()
                for thread in upload_workers:
                    thread.join(timeout=3.0)
                with counter_lock:
                    up_bytes = counters["upload"]
                    up_first = counters.get("upload_first_byte_at") or 0.0
                up_active = max(0.5, time.monotonic() - up_first) if up_first else 0.5
                if up_bytes < MIN_BYTES:
                    upload_error = "；".join(errors[-3:]) or "上传端点没有接收数据"
                else:
                    up_mbps = round(up_bytes * 8 / up_active / 1e6, 1)
            else:
                upload_error = "未配置上传端点"

            duration = round(time.monotonic() - started, 1)
            ok = bool(up_mbps is not None and up_bytes >= MIN_BYTES)
            if not ok:
                error = f"上传失败：{upload_error or '接口无流量'}"
            result = {
                "id": int(time.time() * 1000),
                "ts": self._status["started_at"],
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "down_mbps": down_mbps,
                "up_mbps": up_mbps,
                "latency_ms": latency,
                "jitter_ms": jitter,
                "duration_s": duration,
                "iface": dev,
                "source_ip": source_ip,
                "bind_mode": bind_mode,
                "direct": bool(bind_mode),
                "proxy_active": proxy_active,
                "egress": "直连" if bind_mode else "未知",
                "download_endpoints": [host for host, _ in endpoints],
                "upload_endpoint": upload.hostname or "",
                "down_bytes": down_bytes,
                "up_bytes": up_bytes,
                "ok": ok,
                "error": error,
                "notes": latency_error,
            }
            self._record_history(result)
            self._update(
                running=False,
                phase="done" if ok else "error",
                phase_label="完成" if ok else "失败",
                progress=100,
                result=result,
                error=error,
                live={"down_mbps": down_mbps, "up_mbps": up_mbps},
            )
        except SpeedTestError as exc:
            error = str(exc)
            self._fail(error, dev)
        except Exception as exc:  # noqa: BLE001 - 后台线程必须收尾
            error = f"测速异常: {exc}"
            self._fail(error, dev)
        finally:
            self._stop_sample.set()
            if result is None and not error:
                self._fail("测速未完成（未知原因）", dev)

    def _fail(self, error: str, dev: str) -> None:
        duration = round(time.time() - self._status.get("started_at", time.time()), 1)
        self._update(
            running=False,
            phase="error",
            phase_label="失败",
            progress=100,
            error=error,
            result={
                "id": int(time.time() * 1000),
                "ts": self._status.get("started_at", time.time()),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "down_mbps": None,
                "up_mbps": None,
                "latency_ms": None,
                "jitter_ms": None,
                "duration_s": duration,
                "iface": dev,
                "direct": True,
                "ok": False,
                "error": error,
            },
            live={"down_mbps": None, "up_mbps": None},
        )

    def _history_path(self) -> Path:
        return Path(self.config.data_dir) / HISTORY_NAME

    def _load_history(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self._history_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)][:HISTORY_LIMIT]

    def _record_history(self, result: dict[str, Any]) -> None:
        with self._lock:
            self._history.insert(0, result)
            self._history = self._history[:HISTORY_LIMIT]
            payload = copy.deepcopy(self._history)
        try:
            _atomic_write(self._history_path(), payload)
        except OSError:
            pass
