"""通过 Clash external-controller 读取与切换代理组。

只使用 Clash 自己的 REST API，不读写它的配置文件：
  GET    /proxies                 读取所有代理与策略组
  PUT    /proxies/{group}         选择节点（自动组会固定到该节点）
  DELETE /proxies/{group}         清除自动组的固定节点，恢复自动选择
  GET    /group/{group}/delay     测试整组延迟
  GET    /proxies/{node}/delay    测试单节点延迟
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from ..config import Config
from .clash import _clash_headers, _request

# 策略组类型：Selector 需要手动选择，其余类型由 Clash 自动决策
GROUP_TYPES = {
    "Selector": "手动选择",
    "URLTest": "自动测速",
    "Fallback": "故障转移",
    "LoadBalance": "负载均衡",
    "Relay": "链式代理",
}
AUTO_TYPES = {"URLTest", "Fallback", "LoadBalance"}
SELECTABLE_TYPES = {"Selector", "URLTest", "Fallback"}
# GLOBAL 是 Clash 的全局出口，和配置里的策略组混在一起容易误操作
EXCLUDED_GROUPS = {"GLOBAL"}
MAX_CHAIN = 8
DELAY_TEST_URL = "http://www.gstatic.com/generate_204"


def _delay(node: dict[str, Any]) -> int | None:
    """取最近一次延迟测试结果；0 表示超时，None 表示尚未测试。"""
    history = node.get("history")
    if not isinstance(history, list) or not history:
        return None
    last = history[-1]
    if not isinstance(last, dict):
        return None
    try:
        return int(last.get("delay"))
    except (TypeError, ValueError):
        return None


def _alive(node: dict[str, Any]) -> bool:
    # 部分内置代理没有 alive 字段，视为可用
    return bool(node.get("alive", True))


def _resolve(
    proxies: dict[str, Any], name: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """沿 now 字段一路解析到最终节点，返回链路与出口节点。"""
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = name
    for _ in range(MAX_CHAIN):
        node = proxies.get(current)
        if not isinstance(node, dict) or current in seen:
            break
        seen.add(current)
        chain.append(
            {
                "name": current,
                "type": str(node.get("type") or ""),
                "alive": _alive(node),
                "fixed": str(node.get("fixed") or ""),
            }
        )
        if not node.get("all"):
            break
        # 自动组被手动固定时以 fixed 为准，避免 now 尚未刷新导致出口显示错误
        if node.get("fixed") and str(node.get("type") or "") in AUTO_TYPES:
            nxt = str(node.get("fixed"))
        else:
            nxt = str(node.get("now") or "")
        if not nxt or nxt not in proxies:
            break
        current = nxt
    exit_node = proxies.get(chain[-1]["name"]) if chain else {}
    return chain, exit_node if isinstance(exit_node, dict) else {}


def _auto_active(chain: list[dict[str, Any]]) -> bool:
    """链路里存在未被手动固定的自动组时，说明当前由 Clash 自动决策。"""
    return any(
        item["type"] in AUTO_TYPES and not item.get("fixed") for item in chain[:-1]
    )


def _option(proxies: dict[str, Any], name: str, selected: str) -> dict[str, Any]:
    node = proxies.get(name) or {}
    is_group = bool(node.get("all"))
    if is_group:
        chain, exit_node = _resolve(proxies, name)
    else:
        chain = [
            {
                "name": name,
                "type": str(node.get("type") or ""),
                "alive": _alive(node),
                "fixed": str(node.get("fixed") or ""),
            }
        ]
        exit_node = node
    exit_name = chain[-1]["name"] if chain else name
    return {
        "name": name,
        "type": str(node.get("type") or ""),
        "alive": _alive(node),
        "is_group": is_group,
        "selected": bool(selected) and name == selected,
        # 组选项展示它最终落到哪个节点，方便识别「自动」背后的真实出口
        "delay": _delay(exit_node) if is_group else _delay(node),
        "exit": exit_name,
        "chain": [item["name"] for item in chain],
    }


def build_groups(proxies: dict[str, Any]) -> list[dict[str, Any]]:
    """把 Clash /proxies 的原始数据整理成前端可直接渲染的策略组列表。"""
    groups: list[dict[str, Any]] = []
    for name, item in proxies.items():
        if not isinstance(item, dict) or not item.get("all"):
            continue
        if name in EXCLUDED_GROUPS:
            continue
        group_type = str(item.get("type") or "")
        fixed = str(item.get("fixed") or "")
        chain, exit_node = _resolve(proxies, name)
        selected = fixed if group_type in AUTO_TYPES else str(item.get("now") or "")
        options = [
            _option(proxies, str(member), selected)
            for member in item.get("all") or []
            if str(member) in proxies
        ]
        groups.append(
            {
                "name": name,
                "type": group_type,
                "type_label": GROUP_TYPES.get(group_type, group_type or "策略组"),
                "now": str(item.get("now") or ""),
                "fixed": fixed,
                "auto": _auto_active(chain),
                "auto_supported": group_type in AUTO_TYPES,
                "selectable": group_type in SELECTABLE_TYPES,
                "alive": _alive(item),
                "chain": [entry["name"] for entry in chain],
                "exit": chain[-1]["name"] if chain else "",
                "exit_type": str(exit_node.get("type") or ""),
                "delay": _delay(exit_node),
                "options": options,
            }
        )
    return groups


def _load(config: Config) -> tuple[dict[str, Any], str]:
    result = _request(
        f"{config.clash_api}/proxies",
        headers=_clash_headers(config),
        timeout=config.http_timeout,
    )
    if not result["ok"]:
        return {}, result["error"] or "Clash API 不可用"
    data = result["data"]
    proxies = data.get("proxies") if isinstance(data, dict) else None
    if not isinstance(proxies, dict):
        return {}, "Clash 返回的代理列表格式异常"
    return proxies, ""


def _clash_mode(config: Config) -> str:
    result = _request(
        f"{config.clash_api}/configs",
        headers=_clash_headers(config),
        timeout=1.5,
    )
    if not result["ok"] or not isinstance(result["data"], dict):
        return ""
    return str(result["data"].get("mode") or "")


def proxy_overview(config: Config) -> dict[str, Any]:
    proxies, error = _load(config)
    if error:
        return {
            "ok": False,
            "error": error,
            "mode": "",
            "groups": [],
            "updated_at": time.time(),
        }
    return {
        "ok": True,
        "error": "",
        "mode": _clash_mode(config),
        "groups": build_groups(proxies),
        "updated_at": time.time(),
    }


def _q(value: str) -> str:
    return quote(value, safe="")


def select_proxy(config: Config, group: str, name: str) -> dict[str, Any]:
    proxies, error = _load(config)
    if error:
        return {"ok": False, "error": error, "group": group, "name": name}
    item = proxies.get(group)
    if not isinstance(item, dict) or not item.get("all"):
        return {
            "ok": False,
            "error": f"代理组不存在: {group}",
            "group": group,
            "name": name,
        }
    if name not in [str(member) for member in item.get("all") or []]:
        return {
            "ok": False,
            "error": f"{group} 中没有节点: {name}",
            "group": group,
            "name": name,
        }
    result = _request(
        f"{config.clash_api}/proxies/{_q(group)}",
        method="PUT",
        payload={"name": name},
        headers=_clash_headers(config),
        timeout=config.http_timeout,
    )
    ok = bool(result["ok"])
    return {
        "ok": ok,
        "error": "" if ok else result["error"] or f"HTTP {result['status']}",
        "group": group,
        "name": name,
    }


def restore_auto(config: Config, group: str) -> dict[str, Any]:
    """清除 URLTest / Fallback 的手动固定，恢复自动决策。"""
    proxies, error = _load(config)
    if error:
        return {"ok": False, "error": error, "group": group}
    item = proxies.get(group)
    if not isinstance(item, dict) or not item.get("all"):
        return {"ok": False, "error": f"代理组不存在: {group}", "group": group}
    if str(item.get("type") or "") not in AUTO_TYPES:
        return {"ok": False, "error": f"{group} 不是自动组", "group": group}
    result = _request(
        f"{config.clash_api}/proxies/{_q(group)}",
        method="DELETE",
        headers=_clash_headers(config),
        timeout=config.http_timeout,
    )
    ok = bool(result["ok"])
    return {
        "ok": ok,
        "error": "" if ok else result["error"] or f"HTTP {result['status']}",
        "group": group,
    }


def test_delay(
    config: Config, target: str, timeout_ms: int = 3000
) -> dict[str, Any]:
    proxies, error = _load(config)
    if error:
        return {"ok": False, "error": error, "target": target}
    item = proxies.get(target)
    if not isinstance(item, dict):
        return {"ok": False, "error": f"节点或代理组不存在: {target}", "target": target}
    if item.get("all"):
        path = f"/group/{_q(target)}/delay"
    else:
        path = f"/proxies/{_q(target)}/delay"
    url = (
        f"{config.clash_api}{path}?url={quote(DELAY_TEST_URL, safe='')}"
        f"&timeout={int(timeout_ms)}"
    )
    result = _request(url, headers=_clash_headers(config), timeout=20.0)
    # 部分节点超时时 Clash 会返回 408，但其余节点的结果已经写入 history
    error = ""
    if not result["ok"] and result["status"] != 408:
        error = result["error"] or f"HTTP {result['status']}"
    return {"ok": not error, "error": error, "target": target}
