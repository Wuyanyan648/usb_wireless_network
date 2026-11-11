from __future__ import annotations

import glob
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..config import Config
from ..exec import run

CONFIG_RELATIVE = ".local/share/io.github.clash-verge-rev.clash-verge-rev"


def _request(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 3.0,
) -> dict[str, Any]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            parsed: Any = None
            if body:
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = body
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "data": parsed,
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "data": None, "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "status": 0, "data": None, "error": str(exc)}


def _clash_headers(config: Config) -> dict[str, str]:
    if config.clash_secret:
        return {"Authorization": f"Bearer {config.clash_secret}"}
    return {}


def clash_running(config: Config) -> bool:
    result = _request(
        f"{config.clash_api}/version",
        headers=_clash_headers(config),
        timeout=1.5,
    )
    return bool(result["ok"])


def _core_pids() -> list[int]:
    """列出本机正在运行的 verge-mihomo 进程，用于避免重复拉起内核。"""
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if b"verge-mihomo" in cmdline:
            pids.append(int(entry.name))
    return pids


def _service_available(config: Config) -> bool:
    result = _request(f"{config.clash_service_api}/version", timeout=1.5)
    return bool(result["ok"])


def _discover_body() -> dict[str, Any]:
    candidates = sorted(
        glob.glob(f"/home/*/{CONFIG_RELATIVE}/clash-verge.yaml")
    )
    candidates.append(str(Path.home() / CONFIG_RELATIVE / "clash-verge.yaml"))
    bin_path = shutil.which("verge-mihomo") or "/usr/bin/verge-mihomo"
    for path in candidates:
        config_file = Path(path)
        if not config_file.is_file():
            continue
        config_dir = config_file.parent
        return {
            "core_type": "verge-mihomo",
            "bin_path": bin_path,
            "config_dir": str(config_dir),
            "config_file": str(config_file),
            "log_file": str(config_dir / "logs" / "service" / "usbnic-start.log"),
        }
    return {}


def normalize_body(data: Any) -> dict[str, Any]:
    """校验 Clash Verge 服务返回的启动参数是否完整。"""
    if not isinstance(data, dict):
        return {}
    if not data.get("bin_path") or not data.get("config_file"):
        return {}
    return data


def _body_from_service(config: Config) -> dict[str, Any]:
    result = _request(f"{config.clash_service_api}/get_clash", timeout=2.0)
    if not result["ok"] or not isinstance(result["data"], dict):
        return {}
    return normalize_body(result["data"].get("data"))


def _start_direct(body: dict[str, Any]) -> tuple[bool, str]:
    bin_path = str(body.get("bin_path") or "")
    config_dir = str(body.get("config_dir") or "")
    config_file = str(body.get("config_file") or "")
    log_file = str(body.get("log_file") or "")
    if not bin_path or not config_file:
        return False, "缺少 Clash 可执行文件或配置路径"
    if not log_file:
        log_file = str(Path(config_dir) / "logs" / "service" / "usbnic-start.log")
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "ab") as log:
            subprocess.Popen(
                [bin_path, "-d", config_dir, "-f", config_file],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
    except OSError as exc:
        return False, str(exc)
    return True, ""


def ensure_clash(config: Config) -> dict[str, Any]:
    """确保 Clash 内核可用：已运行则复用，未运行则通过 Clash Verge 服务启动。"""
    if clash_running(config):
        return {"ok": True, "started": False, "error": "", "steps": []}

    steps: list[dict[str, Any]] = []

    # 已有内核进程但 API 还没就绪：等它起来，绝不再拉第二个实例
    # （曾出现同参数双进程并存，统计口径错乱）
    existing = _core_pids()
    if existing:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if clash_running(config):
                steps.append(
                    {
                        "name": "reuse-existing",
                        "ok": True,
                        "detail": f"复用已运行内核 pid={existing}",
                    }
                )
                return {"ok": True, "started": False, "error": "", "steps": steps}
            time.sleep(0.5)
        return {
            "ok": False,
            "started": False,
            "error": f"检测到内核进程 {existing} 但控制口无响应，请检查后手动处理",
            "steps": steps,
        }

    if not _service_available(config):
        rc, _out, err = run(
            ["systemctl", "start", "clash-verge-service"], timeout=15.0
        )
        steps.append(
            {
                "name": "clash-verge-service",
                "ok": rc == 0,
                "detail": "" if rc == 0 else err,
            }
        )
        for _ in range(10):
            if _service_available(config):
                break
            time.sleep(0.3)

    body = _body_from_service(config) or _discover_body()
    if not body:
        return {
            "ok": False,
            "started": False,
            "error": "未找到 Clash 配置，请先手动打开一次 Clash Verge",
            "steps": steps,
        }

    if _service_available(config):
        result = _request(
            f"{config.clash_service_api}/start_clash",
            method="POST",
            payload=body,
            timeout=40.0,
        )
        error = ""
        if not result["ok"]:
            error = str(result.get("error") or "启动请求失败")
        elif isinstance(result["data"], dict) and result["data"].get("code") not in (0, None):
            error = str(result["data"].get("msg") or "启动失败")
        steps.append(
            {"name": "clash-start", "ok": not error, "detail": error}
        )
        if error:
            return {"ok": False, "started": False, "error": error, "steps": steps}
    else:
        ok, error = _start_direct(body)
        steps.append({"name": "clash-start", "ok": ok, "detail": error})
        if not ok:
            return {"ok": False, "started": False, "error": error, "steps": steps}

    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        if clash_running(config):
            return {"ok": True, "started": True, "error": "", "steps": steps}
        time.sleep(0.5)

    return {
        "ok": False,
        "started": False,
        "error": "Clash 启动超时，请查看服务日志",
        "steps": steps,
    }
