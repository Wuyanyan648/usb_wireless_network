"""下游设备强制走代理：用 mihomo rule-provider 维护"按源 IP 全走代理"的清单。

mihomo 的 `RULE-SET` 默认按**目标**匹配，这里利用 classical provider 里的
`SRC-IP-CIDR,<ip>/32` 行按**源地址**匹配：规则链最前面的
`RULE-SET,usbnic-proxy,PROXY` 命中的正是被 TPROXY 接住的下游设备，
排在 `GEOIP,CN,DIRECT` 之前，因此下游不会再被国内规则拦成直连。

内容由 usbnicd 写入 `clash_ruleset_dir/usbnic-proxy.yaml`，再调用
`PUT /providers/rules/usbnic-proxy` 热更新；不重载 mihomo（重载会重绑
DNS 端口）。provider 内容清空时规则自然失效，不影响主机自身流量。
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..config import Config

PROVIDER_NAME = "usbnic-proxy"
PROVIDER_FILE = "usbnic-proxy.yaml"


def provider_path(config: Config) -> Path | None:
    directory = str(config.clash_ruleset_dir or "").strip()
    if not directory:
        return None
    return Path(directory).expanduser() / PROVIDER_FILE


def payload_lines(ips: list[str]) -> list[str]:
    """把设备当前 IP 转成 classical provider 的规则行。"""
    lines: set[str] = set()
    for item in ips:
        text = str(item or "").split("/")[0].strip()
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        if address.version != 4:
            continue
        lines.add(f"SRC-IP-CIDR,{address}/32")
    return sorted(lines)


def render(ips: list[str]) -> str:
    lines = payload_lines(ips)
    if not lines:
        return "payload: []\n"
    body = "\n".join(f"  - {line}" for line in lines)
    return f"payload:\n{body}\n"


def _headers(config: Config) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if config.clash_secret:
        headers["Authorization"] = f"Bearer {config.clash_secret}"
    return headers


def reload(config: Config) -> dict[str, Any]:
    """让 mihomo 重新读取 provider 文件；PUT 不行时退回 GET（旧版本 API）。"""
    url = f"{config.clash_api}/providers/rules/{PROVIDER_NAME}"
    errors: list[str] = []
    for method in ("PUT", "GET"):
        data = b"{}" if method == "PUT" else None
        request = urllib.request.Request(
            url, data=data, headers=_headers(config), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=config.http_timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                try:
                    body = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    body = {}
                count = body.get("ruleCount", body.get("rule_count"))
                return {
                    "ok": 200 <= response.status < 300,
                    "detail": "",
                    "rules": int(count or 0),
                }
        except urllib.error.HTTPError as exc:
            errors.append(f"{method} HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            errors.append(f"{method} {exc}")
    return {"ok": False, "detail": "；".join(errors), "rules": 0}


def sync(config: Config, ips: list[str]) -> dict[str, Any]:
    """把"走代理的设备 IP"写进 provider 并热更新；内容没变就不打扰 mihomo。"""
    path = provider_path(config)
    if path is None:
        return {"ok": True, "skipped": True, "detail": "未配置 clash_ruleset_dir"}
    content = render(ips)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != content:
            path.write_text(content, encoding="utf-8")
        elif current.strip() != "payload: []":
            return {"ok": True, "skipped": True, "detail": "清单未变化"}
    except OSError as exc:
        return {"ok": False, "skipped": False, "detail": f"写入 {path} 失败：{exc}"}
    result = reload(config)
    result["skipped"] = False
    result["path"] = str(path)
    return result
