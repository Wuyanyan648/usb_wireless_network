from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..config import Config


def set_tun(config: Config, enable: bool) -> dict[str, Any]:
    """通过 Clash external-controller 动态开关 TUN。"""
    payload: dict[str, Any] = {"tun": {"enable": bool(enable)}}
    if enable:
        payload = {
            "tun": {
                "enable": True,
                "stack": "mixed",
                "auto-route": True,
                "auto-detect-interface": True,
                "dns-hijack": ["any:53"],
            }
        }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.clash_secret:
        headers["Authorization"] = f"Bearer {config.clash_secret}"
    request = urllib.request.Request(
        f"{config.clash_api}/configs",
        data=body,
        headers=headers,
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.http_timeout) as response:
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "status": 0, "error": str(exc)}
