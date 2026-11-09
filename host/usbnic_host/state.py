"""记录「上次成功应用的网络模式」，供守护进程重启后重放。

背景：`usbnicd` 被 stop → start（门户按钮 / systemctl）后，进程内存里的模式选择
就没了。NetworkManager 的 `ipv4.method shared` 连接通常还活着，所以 `mode` 看起来
还是 gateway；但「转发 sysctl + 代理开关」这些由守护进程做的事不会自动补回来。
这里把用户最后一次**成功**的选择落盘，启动时按它重放一次。

原则：
- 只记录用户主动发起的切换（`/api/mode` 成功、代理开关成功），不猜；
- 没有记录文件时什么都不做（首次安装默认 client，不主动推模式）；
- `client` 是默认态，启动时不重放（避免每次都去 nmcli down/up 折腾）；
- 写入用临时文件 + `os.replace`，掉电不会留下半个 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

STATE_NAME = "last_mode.json"
VALID_MODES = ("client", "hotspot", "gateway")


def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / STATE_NAME


def load(data_dir: Path) -> dict | None:
    """读上次状态；文件缺失 / 损坏 / 模式非法时返回 None。"""
    try:
        raw = json.loads(state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    mode = raw.get("mode")
    if mode not in VALID_MODES:
        return None
    return {
        "mode": mode,
        "proxy": bool(raw.get("proxy", False)),
        # 代理是用户手动拨的，还是模式默认值：只有手动拨过的才在重启后恢复
        "proxy_explicit": bool(raw.get("proxy_explicit", False)),
        "saved_at": str(raw.get("saved_at") or ""),
    }


def save(data_dir: Path, mode: str, proxy: bool, explicit: bool = False) -> dict | None:
    """原子写入；调用方保证只在成功时调用。"""
    if mode not in VALID_MODES:
        return None
    data = {
        "mode": mode,
        "proxy": bool(proxy),
        "proxy_explicit": bool(explicit),
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = state_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".last_mode.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=1)
                handle.write("\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return None
    return data
