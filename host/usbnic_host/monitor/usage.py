"""前台应用使用时长采集。

GNOME on X11 下通过 `_NET_ACTIVE_WINDOW` 取当前窗口，再用窗口的
`_NET_WM_PID` / `WM_CLASS` 反查进程。为了不把刚优化下去的 CPU 拉回来：

* 每 4 秒只调用一次 `xprop -root _NET_ACTIVE_WINDOW`；
* 窗口 ID 变化时才补一次 `xprop -id ...`，并按窗口 ID 缓存；
* 空闲时间用 XScreenSaver 扩展（ctypes，无 xprintidle 依赖）；
* 锁屏状态走 logind，15 秒才查一次。

默认只记录应用名，不记录窗口标题；`usage_record_window_title` 打开后才
把标题写入 `app_titles`。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import pwd
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from ..config import Config
from ..exec import run, which
from .storage import Storage

logger = logging.getLogger("usbnic.usage")

_FLUSH_INTERVAL = 15.0
_PRUNE_INTERVAL = 6 * 3600.0
_DISCOVERY_RETRY = 60.0
_LOCK_RETRY = 15.0
_WINDOW_CACHE_LIMIT = 64
_MAX_INTERVAL = 60.0

# 常见进程名 / WM_CLASS → 易读应用名。未命中的保留原名。
_APP_ALIASES = {
    "firefox": "Firefox",
    "firefox-esr": "Firefox",
    "firefox-bin": "Firefox",
    "chrome": "Chrome",
    "google-chrome": "Chrome",
    "google-chrome-stable": "Chrome",
    "chromium": "Chromium",
    "chromium-browser": "Chromium",
    "microsoft-edge": "Edge",
    "msedge": "Edge",
    "code": "VS Code",
    "code-insiders": "VS Code Insiders",
    "trae": "Trae",
    "trae-cn": "Trae",
    "gnome-terminal-server": "终端",
    "gnome-terminal": "终端",
    "kgx": "终端",
    "alacritty": "Alacritty",
    "kitty": "Kitty",
    "wezterm": "WezTerm",
    "konsole": "Konsole",
    "nautilus": "文件",
    "org.gnome.nautilus": "文件",
    "evince": "文档查看器",
    "okular": "Okular",
    "soffice.bin": "LibreOffice",
    "libreoffice": "LibreOffice",
    "thunderbird": "Thunderbird",
    "telegram-desktop": "Telegram",
    "wechat": "微信",
    "weixin": "微信",
    "linuxqq": "QQ",
    "qq": "QQ",
    "feishu": "飞书",
    "lark": "飞书",
    "dingtalk": "钉钉",
    "slack": "Slack",
    "discord": "Discord",
    "obsidian": "Obsidian",
    "notion-app": "Notion",
    "spotify": "Spotify",
    "vlc": "VLC",
    "mpv": "mpv",
    "gimp": "GIMP",
    "inkscape": "Inkscape",
    "blender": "Blender",
    "idea": "IntelliJ IDEA",
    "pycharm": "PyCharm",
    "goland": "GoLand",
    "clion": "CLion",
    "webstorm": "WebStorm",
    "steam": "Steam",
    "lutris": "Lutris",
}

# 这些进程只是运行时外壳，真正名字优先用 WM_CLASS
_GENERIC_PROCESSES = {
    "python",
    "python3",
    "python3.10",
    "python3.11",
    "python3.12",
    "java",
    "node",
    "electron",
    "bwrap",
    "wine",
    "wine64",
}

# 桌面壳层 / 锁屏 / 系统组件，不计入前台使用
_SKIP_APPS = {
    "gnome-shell",
    "gnome-session-binary",
    "gdm",
    "gdm3",
    "xorg",
    "xwayland",
    "systemd",
    "dbus-daemon",
    "xdg-desktop-portal",
    "xdg-desktop-portal-gnome",
}


def local_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime(
        "%Y-%m-%d"
    )


def _next_midnight(ts: float) -> float:
    moment = datetime.fromtimestamp(ts)
    return (moment.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


def _lookup_alias(name: str) -> str:
    return _APP_ALIASES.get(name.strip().lower(), "")


def _read_comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def parse_active_window(output: str) -> str:
    match = re.search(r"0x[0-9a-fA-F]+", output or "")
    return match.group(0) if match else ""


def parse_pid(output: str) -> int:
    match = re.search(r"_NET_WM_PID\s*=\s*(\d+)", output or "")
    return int(match.group(1)) if match else 0


def parse_wm_class(output: str) -> list[str]:
    match = re.search(r"WM_CLASS\s*=\s*(.*)", output or "")
    if not match:
        return []
    return [item for item in re.findall(r'"([^"]*)"', match.group(1)) if item]


def parse_title(output: str) -> str:
    match = re.search(r'_NET_WM_NAME\s*=\s*"(.*)"\s*$', output or "", re.MULTILINE)
    return match.group(1).strip() if match else ""


def resolve_app_name(pid: int, window_class: list[str]) -> str:
    """进程名优先，其次 WM_CLASS；通用解释器让位给 WM_CLASS。"""
    class_name = window_class[-1].strip() if window_class else ""
    class_first = window_class[0].strip() if window_class else ""
    comm = _read_comm(pid) if pid else ""

    if comm.lower() in _GENERIC_PROCESSES and (class_name or class_first):
        raw = class_name or class_first
    else:
        raw = comm or class_name or class_first
    if not raw:
        return ""
    alias = _lookup_alias(raw) or _lookup_alias(class_name) or _lookup_alias(class_first)
    name = alias or raw
    if name.strip().lower() in _SKIP_APPS:
        return ""
    return name.strip()[:64]


class X11Session:
    """自动发现登录会话的 X11 凭据，并提供活动窗口 / 空闲时间。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.display = config.usage_x11_display.strip()
        self.authority = config.usage_x11_authority.strip()
        self.available = False
        self.error = "尚未连接 X11 会话"
        self._retry_at = 0.0
        self._error_logged = False
        self._idle = _IdleProbe()

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.display:
            env["DISPLAY"] = self.display
        if self.authority:
            env["XAUTHORITY"] = self.authority
        else:
            env.pop("XAUTHORITY", None)
        return env

    def ensure(self, now: float) -> bool:
        if self.available:
            return True
        if now < self._retry_at:
            return False
        self._retry_at = now + _DISCOVERY_RETRY
        candidates = self._candidates()
        if not candidates:
            self.error = (
                "未找到可用的 X11 会话（who 中没有 :N 显示号，"
                "或当前是 Wayland / 无图形登录）"
            )
            self._log_once()
            return False
        for display, authority in candidates:
            env = dict(os.environ)
            env["DISPLAY"] = display
            if authority:
                env["XAUTHORITY"] = authority
            else:
                env.pop("XAUTHORITY", None)
            rc, out, err = run(
                ["xprop", "-root", "-notype", "_NET_ACTIVE_WINDOW"],
                timeout=2.0,
                env=env,
            )
            if rc == 0 and "_NET_ACTIVE_WINDOW" in out:
                self.display = display
                self.authority = authority
                self.available = True
                self.error = ""
                os.environ["DISPLAY"] = display
                if authority:
                    os.environ["XAUTHORITY"] = authority
                self._idle.open(display)
                logger.info(
                    "使用时长采集已连接 X11: DISPLAY=%s XAUTHORITY=%s",
                    display,
                    authority or "(继承环境)",
                )
                return True
            self.error = (err or out or "xprop 无法读取 _NET_ACTIVE_WINDOW").strip()
        self._log_once()
        return False

    def _log_once(self) -> None:
        if not self._error_logged:
            self._error_logged = True
            logger.warning("使用时长采集暂不可用: %s", self.error)

    def _candidates(self) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        if self.display:
            if self.authority:
                found.append((self.display, self.authority))
            else:
                found.extend(
                    (self.display, auth) for auth in self._authority_paths(os.getuid())
                )
                found.append((self.display, ""))
            return found

        env_display = os.environ.get("DISPLAY", "").strip()
        if env_display:
            found.append((env_display, os.environ.get("XAUTHORITY", "").strip()))

        rc, out, _err = run(["who"], timeout=2.0)
        if rc == 0:
            seen: set[str] = set()
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 2 or not parts[1].startswith(":"):
                    continue
                user, display = parts[0], parts[1]
                if display in seen:
                    continue
                seen.add(display)
                try:
                    entry = pwd.getpwnam(user)
                except KeyError:
                    continue
                for auth in self._authority_paths(entry.pw_uid, entry.pw_dir):
                    found.append((display, auth))

        if self.authority:
            found.insert(0, (os.environ.get("DISPLAY", ":0"), self.authority))
        return found

    @staticmethod
    def _authority_paths(uid: int, home: str | None = None) -> list[str]:
        paths = [
            f"/run/user/{uid}/gdm/Xauthority",
            f"/run/user/{uid}/.Xauthority",
        ]
        if home:
            paths.append(os.path.join(home, ".Xauthority"))
        return [path for path in paths if os.path.isfile(path)]

    def active_window(self) -> str:
        rc, out, err = run(
            ["xprop", "-root", "-notype", "_NET_ACTIVE_WINDOW"],
            timeout=2.0,
            env=self.env(),
        )
        if rc != 0:
            self.available = False
            self.error = (err or out or "xprop 读取失败").strip()
            self._retry_at = 0.0
            return ""
        return parse_active_window(out)

    def window_info(self, window: str) -> dict[str, Any]:
        props = ["_NET_WM_PID", "WM_CLASS"]
        if self.config.usage_record_window_title:
            props.append("_NET_WM_NAME")
        rc, out, err = run(
            ["xprop", "-id", window, "-notype", *props],
            timeout=2.0,
            env=self.env(),
        )
        if rc != 0:
            return {}
        return {
            "pid": parse_pid(out),
            "class": parse_wm_class(out),
            "title": parse_title(out),
            "error": (err or "").strip(),
        }

    def idle_seconds(self) -> float | None:
        return self._idle.idle_seconds()


class _IdleProbe:
    """优先用 libXss 的 XScreenSaverInfo，没有库时退回 xprintidle。"""

    def __init__(self) -> None:
        self._x11: Any = None
        self._xss: Any = None
        self._display: Any = None
        self._root = 0
        self._info: Any = None
        self._use_binary = False
        self.error = ""
        self._lock = threading.Lock()

    def open(self, display: str) -> None:
        try:
            x11_name = ctypes.util.find_library("X11") or "libX11.so.6"
            xss_name = ctypes.util.find_library("Xss") or "libXss.so.1"
            self._x11 = ctypes.CDLL(x11_name)
            self._xss = ctypes.CDLL(xss_name)
        except OSError as exc:
            self.error = f"libX11/libXss 不可用: {exc}"
            self._use_binary = which("xprintidle") is not None
            return

        try:
            self._x11.XOpenDisplay.restype = ctypes.c_void_p
            self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            self._x11.XDefaultRootWindow.restype = ctypes.c_ulong
            self._x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            self._xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XScreenSaverInfo)
            self._xss.XScreenSaverQueryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(_XScreenSaverInfo),
            ]
            handle = self._x11.XOpenDisplay(display.encode())
            if not handle:
                raise OSError("XOpenDisplay 返回空")
            self._display = ctypes.c_void_p(handle)
            self._root = self._x11.XDefaultRootWindow(self._display)
            self._info = self._xss.XScreenSaverAllocInfo()
            self.error = ""
        except Exception as exc:  # noqa: BLE001 - 空闲探测失败不影响主采集
            self.error = f"XScreenSaver 初始化失败: {exc}"
            self._display = None
            self._use_binary = which("xprintidle") is not None

    def idle_seconds(self) -> float | None:
        with self._lock:
            if self._display and self._xss and self._info:
                try:
                    ok = self._xss.XScreenSaverQueryInfo(
                        self._display, self._root, self._info
                    )
                    if ok:
                        return float(self._info.contents.idle) / 1000.0
                except Exception as exc:  # noqa: BLE001
                    self.error = str(exc)
                    return None
            if self._use_binary:
                rc, out, _err = run(["xprintidle"], timeout=1.0)
                if rc == 0 and out.strip().isdigit():
                    return float(out.strip()) / 1000.0
            return None


class _XScreenSaverInfo(ctypes.Structure):
    _fields_ = [
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("til_or_since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong),
        ("eventMask", ctypes.c_ulong),
    ]


class UsageMonitor:
    """后台使用时长采集线程。并入 usbnicd，随 Monitor 启停。"""

    def __init__(self, config: Config, storage: Storage) -> None:
        self.config = config
        self.storage = storage
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._started = False
        self._thread = threading.Thread(
            target=self._loop, name="usage-monitor", daemon=True
        )
        self.session = X11Session(config)
        self._window_cache: dict[str, dict[str, Any]] = {}
        self._pending: dict[tuple[str, str], float] = {}
        self._pending_titles: dict[tuple[str, str, str], float] = {}
        self._last_ts = 0.0
        self._last_app = ""
        self._last_title = ""
        self._last_idle = True
        self._last_flush = 0.0
        self._last_prune = 0.0
        self._current_app = ""
        self._current_pid = 0
        self._current_since = 0.0
        self._status: dict[str, Any] = {
            "app": "",
            "pid": 0,
            "idle": True,
            "reason": "等待第一次采样",
            "at": 0.0,
        }
        self._session_id = ""
        self._locked = False
        self._lock_checked = 0.0
        self._lock_supported = True

    def start(self) -> None:
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._started:
            self._thread.join(timeout=3.0)
        try:
            self.flush()
        except Exception as exc:  # noqa: BLE001 - 退出时尽量落盘
            logger.warning("使用时长落盘失败: %s", exc)

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        now = time.time()
        if self._pending:
            entries = [
                (day, app, round(seconds, 3), now)
                for (day, app), seconds in self._pending.items()
                if seconds > 0
            ]
            if entries:
                self.storage.add_usage(entries)
            self._pending.clear()
        if self._pending_titles:
            titles = [
                (day, app, title, round(seconds, 3))
                for (day, app, title), seconds in self._pending_titles.items()
                if seconds > 0
            ]
            if titles:
                self.storage.add_usage_titles(titles)
            self._pending_titles.clear()
        self._last_flush = now

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - 采集线程必须存活
                logger.warning("使用时长采样失败: %s", exc)
                with self._lock:
                    self._status = {
                        "app": "",
                        "pid": 0,
                        "idle": True,
                        "reason": f"采样失败: {exc}",
                        "at": time.time(),
                    }
            elapsed = time.monotonic() - started
            self._stop.wait(
                max(1.0, self.config.usage_sample_interval - elapsed)
            )

    def _tick(self) -> None:
        now = time.time()
        sample = self._sample(now)

        elapsed = now - self._last_ts if self._last_ts else 0.0
        if elapsed > max(_MAX_INTERVAL, self.config.usage_sample_interval * 4):
            # 休眠 / 挂起 / 服务刚启动，不把空档算到应用头上
            elapsed = 0.0
        if elapsed > 0 and self._last_app and not self._last_idle:
            self._accumulate(self._last_ts, now, self._last_app, self._last_title)

        self._last_ts = now
        self._last_app = sample["app"]
        self._last_title = sample.get("title") or ""
        self._last_idle = bool(sample["idle"])

        with self._lock:
            self._status = {
                "app": sample["app"],
                "pid": sample["pid"],
                "idle": bool(sample["idle"]),
                "reason": sample["reason"],
                "at": now,
            }
            if sample["app"]:
                if sample["app"] != self._current_app:
                    self._current_since = now
                self._current_app = sample["app"]
                self._current_pid = int(sample["pid"] or 0)
            else:
                self._current_app = ""
                self._current_pid = 0
                self._current_since = 0.0
            if now - self._last_flush >= _FLUSH_INTERVAL:
                self._flush_locked()
            if now - self._last_prune >= _PRUNE_INTERVAL:
                self._prune_locked(now)

    def _sample(self, now: float) -> dict[str, Any]:
        if not self.session.ensure(now):
            return {
                "app": "",
                "pid": 0,
                "title": "",
                "idle": True,
                "reason": self.session.error or "X11 不可用",
            }
        if self._is_locked(now):
            return {"app": "", "pid": 0, "title": "", "idle": True, "reason": "锁屏"}

        window = self.session.active_window()
        if not window or int(window, 16) == 0:
            return {
                "app": "",
                "pid": 0,
                "title": "",
                "idle": True,
                "reason": "无活动窗口",
            }

        info = self._window_cache.get(window)
        if (
            info is None
            or (not info.get("pid") and not info.get("class"))
            or (
                self.config.usage_record_window_title
                and now - float(info.get("at") or 0.0) >= 30.0
            )
        ):
            info = self.session.window_info(window)
            info["at"] = now
            if len(self._window_cache) >= _WINDOW_CACHE_LIMIT:
                self._window_cache.clear()
            self._window_cache[window] = info

        pid = int(info.get("pid") or 0)
        app = resolve_app_name(pid, list(info.get("class") or []))
        if not app:
            return {
                "app": "",
                "pid": pid,
                "title": "",
                "idle": True,
                "reason": "桌面壳层",
            }

        idle_seconds = self.session.idle_seconds()
        if (
            idle_seconds is not None
            and idle_seconds >= max(30.0, self.config.usage_idle_seconds)
        ):
            return {
                "app": "",
                "pid": pid,
                "title": "",
                "idle": True,
                "reason": f"无输入 {int(idle_seconds)}s",
            }

        return {
            "app": app,
            "pid": pid,
            "title": str(info.get("title") or "") if self.config.usage_record_window_title else "",
            "idle": False,
            "reason": "",
        }

    def _accumulate(
        self, start_ts: float, end_ts: float, app: str, title: str
    ) -> None:
        if end_ts <= start_ts:
            return
        record_title = self.config.usage_record_window_title and bool(title)
        cursor = start_ts
        with self._lock:
            while cursor < end_ts:
                day = local_day(cursor)
                chunk_end = min(end_ts, _next_midnight(cursor))
                seconds = chunk_end - cursor
                if seconds > 0:
                    key = (day, app)
                    self._pending[key] = self._pending.get(key, 0.0) + seconds
                    if record_title:
                        title_key = (day, app, title)
                        self._pending_titles[title_key] = (
                            self._pending_titles.get(title_key, 0.0) + seconds
                        )
                cursor = chunk_end

    def _is_locked(self, now: float) -> bool:
        if not self._lock_supported:
            return False
        if now - self._lock_checked < _LOCK_RETRY:
            return self._locked
        self._lock_checked = now
        if not self._session_id:
            self._session_id = self._find_x11_session()
            if not self._session_id:
                return self._locked
        rc, out, _err = run(
            ["loginctl", "show-session", self._session_id, "-p", "LockedHint"],
            timeout=2.0,
        )
        if rc != 0:
            # 会话消失（注销 / 重启）后重新发现；loginctl 本身不可用则停用锁屏检测
            self._session_id = ""
            if rc == 127:
                self._lock_supported = False
            return self._locked
        self._locked = "LockedHint=yes" in out
        return self._locked

    @staticmethod
    def _find_x11_session() -> str:
        rc, out, _err = run(["loginctl", "list-sessions", "--no-legend"], timeout=2.0)
        if rc != 0:
            return ""
        for line in out.splitlines():
            parts = line.split()
            if not parts:
                continue
            session_id = parts[0]
            _rc, detail, _err = run(
                [
                    "loginctl",
                    "show-session",
                    session_id,
                    "-p",
                    "Type",
                    "-p",
                    "Active",
                ],
                timeout=2.0,
            )
            if "Type=x11" in detail and "Active=yes" in detail:
                return session_id
        return ""

    def _prune_locked(self, now: float) -> None:
        self._last_prune = now
        try:
            cutoff = (
                datetime.now() - timedelta(days=max(1, self.config.usage_retention_days))
            ).strftime("%Y-%m-%d")
            self.storage.prune_usage(cutoff)
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响采集
            logger.warning("清理使用时长历史失败: %s", exc)

    def snapshot(self) -> dict[str, Any]:
        day = local_day()
        self.flush()
        rows = self.storage.usage_day(day)
        total = sum(item["seconds"] for item in rows)
        items = [
            {
                "app": item["app"],
                "seconds": round(item["seconds"], 1),
                "percent": round(item["seconds"] / total * 100, 1) if total else 0.0,
            }
            for item in rows
        ]
        with self._lock:
            status = dict(self._status)
            current_app = self._current_app
            current_pid = self._current_pid
            current_since = self._current_since
        if not self.config.usage_enabled:
            return {
                "ok": True,
                "enabled": False,
                "date": day,
                "total_seconds": round(total, 1),
                "items": items,
                "current": None,
                "idle": True,
                "idle_reason": "使用时长采集未启用",
                "x11": {
                    "available": False,
                    "display": "",
                    "error": "usage_enabled=false",
                },
                "record_titles": False,
                "retention_days": self.config.usage_retention_days,
                "sample_interval": self.config.usage_sample_interval,
            }
        return {
            "ok": True,
            "enabled": True,
            "date": day,
            "total_seconds": round(total, 1),
            "items": items,
            "current": (
                {
                    "app": current_app,
                    "pid": current_pid,
                    "since": current_since,
                }
                if current_app
                else None
            ),
            "idle": bool(status.get("idle", True)),
            "idle_reason": status.get("reason") or "",
            "x11": {
                "available": bool(self.session.available),
                "display": self.session.display,
                "error": self.session.error,
                "idle_seconds": self.session.idle_seconds(),
                "locked": self._locked,
            },
            "record_titles": bool(self.config.usage_record_window_title),
            "retention_days": self.config.usage_retention_days,
            "sample_interval": self.config.usage_sample_interval,
            "idle_seconds": self.config.usage_idle_seconds,
            "note": "按前台窗口统计；默认只记录应用名，不记录窗口标题",
        }

    def trend(self, days: int = 7) -> dict[str, Any]:
        days = max(2, min(int(days), max(2, self.config.usage_retention_days)))
        self.flush()
        end = datetime.now().date()
        start = end - timedelta(days=days - 1)
        start_text = start.strftime("%Y-%m-%d")
        end_text = end.strftime("%Y-%m-%d")
        totals = {
            item["day"]: item["seconds"]
            for item in self.storage.usage_day_totals(start_text, end_text)
        }
        items = []
        for offset in range(days):
            current = start + timedelta(days=offset)
            key = current.strftime("%Y-%m-%d")
            items.append(
                {
                    "date": key,
                    "label": f"{current.month}/{current.day:02d}",
                    "seconds": round(totals.get(key, 0.0), 1),
                }
            )
        apps = self.storage.usage_app_totals(start_text, end_text, limit=8)
        total = sum(item["seconds"] for item in items)
        for item in apps:
            item["seconds"] = round(item["seconds"], 1)
            item["percent"] = round(item["seconds"] / total * 100, 1) if total else 0.0
        return {
            "ok": True,
            "enabled": bool(self.config.usage_enabled),
            "days": days,
            "start": start_text,
            "end": end_text,
            "items": items,
            "apps": apps,
            "total_seconds": round(total, 1),
        }
