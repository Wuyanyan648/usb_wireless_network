from __future__ import annotations

import ipaddress
import json
import secrets
import string
import threading
import time
from pathlib import Path
from typing import Any

from ..config import Config
from ..exec import run
from . import wifi_caps

STATE_NAME = "router.state"
AP_TYPES = ("wlan", "wifi")

# router_status 的缓存：每次探测要跑 1 次 nmcli + 2~3 次 iw + rfkill，约 60ms 子进程 CPU，
# 而 profile 是否存在、网卡能力、rfkill 这些信息十几秒内不会变。
STATUS_TTL = 15.0
_status_cache: dict[str, Any] = {"ts": 0.0, "payload": None}
_status_lock = threading.Lock()


def invalidate_router_status() -> None:
    """模式/探测改动过 NM 连接后调用，让下一次读取立刻重新探测。"""
    with _status_lock:
        _status_cache["ts"] = 0.0
        _status_cache["payload"] = None


def _step(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def _nmcli(*args: str, timeout: float = 15.0) -> tuple[int, str, str]:
    return run(["nmcli", *args], timeout=timeout)


def _gateway_address(cidr: str) -> str:
    """把 10.20.0.0/24 转成网关地址 10.20.0.1/24。"""
    network = ipaddress.ip_network(cidr, strict=False)
    return f"{network.network_address + 1}/{network.prefixlen}"


def _connection_names() -> set[str]:
    """一次 nmcli 取全部 profile 名，供只读状态里多处判断复用。"""
    rc, out, _err = _nmcli("-t", "-f", "NAME", "connection", "show", timeout=10.0)
    if rc != 0:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def _connection_exists(name: str) -> bool:
    return name in _connection_names()


def _down(name: str) -> dict[str, Any]:
    rc, out, err = _nmcli("connection", "down", name, timeout=15.0)
    if rc == 0:
        return _step(name, True, "已断开")
    text = f"{out}\n{err}".lower()
    if "not active" in text or "not an active" in text:
        return _step(name, True, "本来未启用")
    return _step(name, False, (err or out).strip())


def _state_path(config: Config) -> Path:
    return Path(config.data_dir) / STATE_NAME


def _load_state(config: Config) -> dict[str, Any]:
    try:
        return json.loads(_state_path(config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(config: Config, data: dict[str, Any]) -> None:
    try:
        _state_path(config).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def _strong_password() -> str:
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghijkmnopqrstuvwxyz"
    digit = "23456789"
    symbol = "!@#%^&*_=+:,.?/"
    alphabet = upper + lower + digit + symbol
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(24))
        classes = sum(
            (
                any(ch in upper for ch in password),
                any(ch in lower for ch in password),
                any(ch in digit for ch in password),
                any(ch in symbol for ch in password),
            )
        )
        if classes >= 3:
            return password


def ensure_password(config: Config) -> tuple[str, dict[str, Any]]:
    """复用热点密码文件；缺失时生成 24 位强密码并以 0600 落盘。"""
    path = Path(config.hotspot_password_file)
    try:
        password = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        password = ""
    if len(password) >= 20:
        return password, _step("password", True, "复用现有热点密码")
    password = _strong_password()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(password + "\n", encoding="utf-8")
        path.chmod(0o600)
    except OSError as exc:
        return "", _step("password", False, str(exc))
    return password, _step("password", True, "已生成 24 位强密码")


def _rfkill_rows() -> list[dict[str, str]]:
    rc, out, _err = run(["rfkill", "--output", "ID,TYPE,SOFT"], timeout=3.0)
    if rc != 0:
        return []
    rows: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        rows.append({"id": parts[0], "type": parts[1], "soft": parts[2]})
    return rows


def _radio_wifi_enabled() -> bool:
    rc, out, _err = _nmcli("radio", "wifi", timeout=5.0)
    return rc == 0 and out.strip().lower() == "enabled"


def unblock_wifi(config: Config) -> list[str]:
    """解除无线软锁（含 NM 的 wifi 开关），并记录原状态以便退出时恢复。"""
    ids = [
        row["id"]
        for row in _rfkill_rows()
        if row["type"] in AP_TYPES and row["soft"] == "blocked"
    ]
    if not ids:
        return []
    for rfkill_id in ids:
        run(["rfkill", "unblock", rfkill_id], timeout=3.0)
    state = _load_state(config)
    # rfkill 解开了，但 NM 自己的 wifi 开关可能仍是 off，设备会一直 unavailable
    if not _radio_wifi_enabled():
        rc, _out, _err = _nmcli("radio", "wifi", "on", timeout=8.0)
        state["radio_was_off"] = rc == 0
    state["rfkill"] = ids
    _save_state(config, state)
    return ids


def restore_rfkill(config: Config) -> dict[str, Any]:
    state = _load_state(config)
    ids = [str(item) for item in state.get("rfkill") or []]
    for rfkill_id in ids:
        run(["rfkill", "block", rfkill_id], timeout=3.0)
    changed = bool(ids)
    if state.get("radio_was_off"):
        _nmcli("radio", "wifi", "off", timeout=8.0)
        state["radio_was_off"] = False
        changed = True
    if ids:
        state["rfkill"] = []
    if changed:
        _save_state(config, state)
    return _step(
        "rfkill-restore",
        True,
        f"已恢复软锁 {','.join(ids)}" if ids else "无需恢复",
    )


def _ap_active(iface: str) -> bool:
    rc, out, _err = run(["iw", "dev", iface, "info"], timeout=3.0)
    return rc == 0 and "type AP" in out


def ensure_lan(config: Config) -> dict[str, Any]:
    name = config.router_lan_connection
    iface = config.router_lan_iface or config.gateway_iface
    if not iface:
        return _step(name, False, "未配置有线扩展口")
    if not _connection_exists(name):
        rc, out, err = _nmcli(
            "connection", "add", "type", "ethernet", "ifname", iface,
            "con-name", name, timeout=20.0,
        )
        if rc != 0:
            return _step(name, False, (err or out).strip())
    address = _gateway_address(config.router_lan_cidr)
    rc, out, err = _nmcli(
        "connection", "modify", name,
        "connection.autoconnect", "no",
        "connection.interface-name", iface,
        "ipv4.method", "shared",
        "ipv4.addresses", address,
        "ipv6.method", "ignore",
        timeout=15.0,
    )
    return _step(name, rc == 0, ("" if rc == 0 else (err or out).strip()) or address)


def ensure_ap(
    config: Config,
    *,
    name: str,
    iface: str,
    ssid: str,
    band: str,
    channel: int,
    cidr: str,
    password: str,
) -> dict[str, Any]:
    """创建/更新一个 AP profile（SAE + PMF required + 仅 CCMP）。"""
    if not iface:
        return _step(name, False, "未配置无线网卡")
    if not _connection_exists(name):
        rc, out, err = _nmcli(
            "connection", "add", "type", "wifi", "ifname", iface,
            "con-name", name, "ssid", ssid, timeout=20.0,
        )
        if rc != 0:
            return _step(name, False, (err or out).strip())
    address = _gateway_address(cidr)
    rc, out, err = _nmcli(
        "connection", "modify", name,
        "connection.autoconnect", "no",
        "connection.interface-name", iface,
        "802-11-wireless.ssid", ssid,
        "802-11-wireless.mode", "ap",
        "802-11-wireless.band", band,
        "802-11-wireless.channel", str(channel),
        "802-11-wireless-security.key-mgmt", "sae",
        "802-11-wireless-security.pmf", "3",
        "802-11-wireless-security.psk", password,
        "802-11-wireless-security.proto", "rsn",
        "802-11-wireless-security.pairwise", "ccmp",
        "802-11-wireless-security.group", "ccmp",
        "802-11-wireless-security.wps-method", "1",
        "802-11-wireless.ap-isolation", "no",
        "ipv4.method", "shared",
        "ipv4.addresses", address,
        "ipv6.method", "ignore",
        "wifi.cloned-mac-address", "permanent",
        timeout=15.0,
    )
    return _step(name, rc == 0, ("" if rc == 0 else (err or out).strip()) or f"{ssid} ch{channel}")


def _device_state(iface: str) -> str:
    rc, out, _err = _nmcli(
        "-t", "-f", "DEVICE,STATE", "device", "status", timeout=5.0
    )
    if rc != 0:
        return ""
    for line in out.splitlines():
        parts = line.split(":")
        if parts and parts[0] == iface:
            return parts[-1]
    return ""


def _wait_wifi_ready(iface: str, timeout: float = 20.0) -> bool:
    """rfkill 解锁后 NM 需要几秒才会把网卡从 unavailable 拉回 disconnected。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _device_state(iface)
        if state in ("disconnected", "connected"):
            return True
        time.sleep(1.0)
    return False


def _activate(
    name: str, iface: str, timeout: float, expect_ap: bool = True
) -> dict[str, Any]:
    """拉起连接；无线接口额外确认已进入 AP 模式。"""
    if expect_ap and not _wait_wifi_ready(iface):
        return _step(name, False, "无线网卡未就绪（仍是 unavailable）")
    rc, out, err = _nmcli(
        "--wait", str(int(timeout)), "connection", "up", name, timeout=timeout + 10.0
    )
    if rc != 0:
        return _step(name, False, (err or out).strip())
    if not expect_ap:
        return _step(name, True, "已启用")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ap_active(iface):
            return _step(name, True, "AP 已启动")
        time.sleep(1.0)
    return _step(name, False, "NM 已连接，但未进入 AP 模式（可降级为仅有线）")


def apply_router(config: Config) -> dict[str, Any]:
    """建立路由器模式：有线扩展口 + 5G AP + 2.4G AP。

    返回 ok=False 表示"有线扩展口"这一步失败，需要整体回退；无线失败只记降级。
    """
    steps: list[dict[str, Any]] = []
    degraded: list[str] = []
    lan_iface = config.router_lan_iface or config.gateway_iface
    if not lan_iface:
        return {
            "ok": False,
            "steps": [_step("router", False, "未配置有线扩展口")],
            "degraded": [],
            "error": "未配置有线扩展口",
        }

    password, password_step = ensure_password(config)
    steps.append(password_step)
    if not password:
        return {
            "ok": False,
            "steps": steps,
            "degraded": [],
            "error": password_step["detail"],
        }

    unblocked = unblock_wifi(config)
    if unblocked:
        # rfkill 解锁后 NM 需要一点时间把设备从 unavailable 拉回
        time.sleep(1.5)
    steps.append(
        _step(
            "rfkill",
            True,
            f"已解锁射频 {','.join(unblocked)}" if unblocked else "无线未被软锁",
        )
    )

    steps.append(ensure_lan(config))
    # 起 AP 前先判定能不能在目标信道发射，避免白等 20 秒超时
    target_5g = wifi_caps.resolve(config.router_5g_iface, config.router_5g_channel)
    target_24g = (
        wifi_caps.resolve(config.router_24g_iface, config.router_24g_channel)
        if config.router_24g_enabled
        else {"iface": "", "capability": {}}
    )
    if target_5g["iface"]:
        steps.append(
            ensure_ap(
                config,
                name=config.router_5g_connection,
                iface=target_5g["iface"],
                ssid=config.router_5g_ssid,
                band="a",
                channel=config.router_5g_channel,
                cidr=config.router_5g_cidr,
                password=password,
            )
        )
    else:
        steps.append(
            _step(
                config.router_5g_connection,
                False,
                wifi_caps.describe(target_5g["capability"]),
            )
        )
        degraded.append("5g")
    if config.router_24g_enabled:
        if target_24g["iface"]:
            steps.append(
                ensure_ap(
                    config,
                    name=config.router_24g_connection,
                    iface=target_24g["iface"],
                    ssid=config.router_24g_ssid,
                    band="bg",
                    channel=config.router_24g_channel,
                    cidr=config.router_24g_cidr,
                    password=password,
                )
            )
        else:
            steps.append(
                _step(
                    config.router_24g_connection,
                    False,
                    wifi_caps.describe(target_24g["capability"]),
                )
            )
            degraded.append("24g")

    steps.append(_down(config.gateway_connection))
    steps.append(_down(config.hotspot_connection))

    lan_step = _activate(
        config.router_lan_connection, lan_iface, 20.0, expect_ap=False
    )
    lan_step["name"] = config.router_lan_connection
    steps.append(lan_step)
    if not lan_step["ok"]:
        return {
            "ok": False,
            "steps": steps,
            "degraded": degraded,
            "error": f"有线扩展口启动失败：{lan_step['detail']}",
        }

    if target_5g["iface"]:
        ap5 = _activate(config.router_5g_connection, target_5g["iface"], 20.0)
        steps.append(ap5)
        if not ap5["ok"]:
            degraded.append("5g")

    if not config.router_24g_enabled:
        # 上游华为 AP 的 2.4G 信道不可调，同频 AP 只会互相压制，默认关闭
        steps.append(
            _step(
                config.router_24g_connection,
                True,
                "默认关闭（避免与上游 AP 的 2.4G 同频干扰）",
            )
        )
    elif target_24g["iface"]:
        ap24 = _activate(
            config.router_24g_connection, target_24g["iface"], 20.0
        )
        steps.append(ap24)
        if not ap24["ok"]:
            degraded.append("24g")
    else:
        steps.append(
            _step(
                config.router_24g_connection,
                False,
                wifi_caps.describe(target_24g["capability"]),
            )
        )
        degraded.append("24g")

    rc, _out, err = run(["sysctl", "-w", "net.ipv4.ip_forward=1"], timeout=3.0)
    steps.append(
        _step(
            "ip_forward",
            rc == 0,
            err.strip() if rc != 0 else "已开启 IPv4 转发",
        )
    )
    return {
        "ok": rc == 0,
        "steps": steps,
        "degraded": degraded,
        "error": "" if rc == 0 else err.strip(),
    }


def probe_5g_ap(config: Config, seconds: float | None = None) -> dict[str, Any]:
    """最小验证：只把 AX211 起成 AP 若干秒，然后断开并恢复 rfkill。

    不碰 gateway/hotspot 连接，也不改当前网络模式；用于确认 iwlwifi 是否
    支持 NM 的 AP 模式。失败时返回 ok=False，调用方据此降级。
    """
    seconds = seconds or config.router_ap_probe_seconds
    target = wifi_caps.resolve(config.router_5g_iface, config.router_5g_channel)
    iface = target["iface"]
    cap = target["capability"]
    if not iface:
        detail = wifi_caps.describe(cap)
        return {
            "ok": False,
            "ap_active": False,
            "iface": "",
            "channel": config.router_5g_channel,
            "reason": detail,
            "steps": [_step("ap-capable", False, detail)],
        }
    steps: list[dict[str, Any]] = []
    password, password_step = ensure_password(config)
    steps.append(password_step)
    if not password:
        return {"ok": False, "ap_active": False, "steps": steps}

    unblocked = unblock_wifi(config)
    if unblocked:
        time.sleep(1.5)
    steps.append(
        _step(
            "rfkill",
            True,
            f"已解锁射频 {','.join(unblocked)}" if unblocked else "无线未被软锁",
        )
    )
    steps.append(
        ensure_ap(
            config,
            name=config.router_5g_connection,
            iface=iface,
            ssid=config.router_5g_ssid,
            band="a",
            channel=config.router_5g_channel,
            cidr=config.router_5g_cidr,
            password=password,
        )
    )
    steps.append(_step("ap-capable", True, wifi_caps.describe(cap)))

    ap_active = False
    if cap.get("available"):
        result = _activate(config.router_5g_connection, iface, seconds)
        steps.append(result)
        ap_active = bool(result["ok"])
        _down(config.router_5g_connection)
    steps.append(restore_rfkill(config))
    return {
        "ok": ap_active,
        "ap_active": ap_active,
        "iface": iface,
        "ssid": config.router_5g_ssid,
        "channel": config.router_5g_channel,
        "steps": steps,
    }


def teardown_router(config: Config) -> list[dict[str, Any]]:
    """按逆序撤销：先断 AP，再断有线，最后恢复射频软锁。"""
    steps = [
        _down(config.router_24g_connection),
        _down(config.router_5g_connection),
        _down(config.router_lan_connection),
        restore_rfkill(config),
    ]
    return steps


def router_status(config: Config, ttl: float = STATUS_TTL) -> dict[str, Any]:
    """给 API 用的只读状态：profile、AP 模式、软锁、直连列表。

    页面会周期性读取，这里按 ttl 缓存；模式切换后由调用方 invalidate。
    """
    now = time.monotonic()
    with _status_lock:
        payload = _status_cache["payload"]
        if payload is not None and now - _status_cache["ts"] < ttl:
            return payload
    payload = _collect_router_status(config)
    with _status_lock:
        _status_cache["ts"] = now
        _status_cache["payload"] = payload
    return payload


def _collect_router_status(config: Config) -> dict[str, Any]:
    names = _connection_names()
    rows = _rfkill_rows()
    auto_5g = wifi_caps.resolve(config.router_5g_iface, config.router_5g_channel)
    cap_5g = auto_5g["capability"]
    cap_24g = wifi_caps.capability(config.router_24g_iface, config.router_24g_channel)
    return {
        "lan": {
            "connection": config.router_lan_connection,
            "iface": config.router_lan_iface or config.gateway_iface,
            "cidr": config.router_lan_cidr,
            "exists": config.router_lan_connection in names,
        },
        "ap_5g": {
            "connection": config.router_5g_connection,
            "iface": config.router_5g_iface or auto_5g["iface"],
            "auto_iface": not bool(config.router_5g_iface),
            "ssid": config.router_5g_ssid,
            "channel": config.router_5g_channel,
            "cidr": config.router_5g_cidr,
            "exists": config.router_5g_connection in names,
            "available": cap_5g["available"],
            "reason": "" if cap_5g["available"] else wifi_caps.describe(cap_5g),
            "capability": cap_5g,
            "active": bool(cap_5g["iface"])
            and _ap_active(cap_5g["iface"]),
        },
        "ap_24g": {
            "connection": config.router_24g_connection,
            "iface": config.router_24g_iface,
            "ssid": config.router_24g_ssid,
            "channel": config.router_24g_channel,
            "cidr": config.router_24g_cidr,
            "enabled": config.router_24g_enabled,
            "note": "默认关闭，避免与上游 AP 的 2.4G 同频干扰",
            "exists": config.router_24g_connection in names,
            "available": cap_24g["available"],
            "reason": "" if cap_24g["available"] else wifi_caps.describe(cap_24g),
            "capability": cap_24g,
            "active": bool(config.router_24g_iface)
            and _ap_active(config.router_24g_iface),
        },
        "rfkill_blocked": [
            row["id"] for row in rows if row["type"] in AP_TYPES and row["soft"] == "blocked"
        ],
        "wired_direct": config.router_wired_direct,
        "direct_cidrs": list(config.router_direct_cidrs),
    }
