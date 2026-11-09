from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..config import Config


def _nmcli(*args: str) -> str:
    """执行 nmcli 并返回去掉首尾空白的输出；失败时返回空串。"""
    try:
        result = subprocess.run(
            ["nmcli", *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _connection_value(config: Config, field: str) -> str:
    return _nmcli("-g", field, "connection", "show", config.hotspot_connection)


def _read_password_file(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return ""


def _write_password_file(path: Path, password: str) -> bool:
    """仅 root 可写；以 0600 原子地落盘，避免明文进入仓库。"""
    if os.geteuid() != 0:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(password + "\n")
        os.chmod(path, 0o600)
    except OSError:
        return False
    return True


def _password_from_nm(config: Config) -> str:
    return _nmcli(
        "-s",
        "-g",
        "802-11-wireless-security.psk",
        "connection",
        "show",
        config.hotspot_connection,
    )


def hotspot_info(config: Config) -> dict:
    """读取热点连接的安全参数与密码。

    密码优先来自 root-only 的本地文件；文件缺失且以 root 运行时，
    会从 NetworkManager 配置中补写一次，之后不再依赖 nmcli 取 secret。
    """
    path = Path(config.hotspot_password_file).expanduser()
    ssid = _connection_value(config, "802-11-wireless.ssid")
    key_mgmt = _connection_value(config, "802-11-wireless-security.key-mgmt")
    pmf = _connection_value(config, "802-11-wireless-security.pmf")
    proto = _connection_value(config, "802-11-wireless-security.proto")
    pairwise = _connection_value(config, "802-11-wireless-security.pairwise")
    group = _connection_value(config, "802-11-wireless-security.group")
    iface = _connection_value(config, "connection.interface-name")
    autoconnect = _connection_value(config, "connection.autoconnect")
    state = _nmcli("-g", "GENERAL.STATE", "device", "show", iface) if iface else ""
    active = state.split()[0] == "100" if state else False

    password = _read_password_file(path)
    source = "file" if password else ""
    if not password:
        password = _password_from_nm(config)
        if password:
            source = "nmcli"
            if _write_password_file(path, password):
                source = "file"

    return {
        "available": bool(ssid or key_mgmt or password),
        "connection": config.hotspot_connection,
        "interface": iface,
        "ssid": ssid,
        "active": active,
        "state": state,
        "autoconnect": autoconnect == "yes",
        "security": {
            "key_mgmt": key_mgmt,
            "pmf": pmf,
            "proto": proto,
            "pairwise": pairwise,
            "group": group,
        },
        "password": password,
        "password_source": source,
        "password_file": str(path),
        "password_file_exists": path.is_file(),
    }
