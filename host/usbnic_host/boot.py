from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .net.interfaces import get_default_routes
from .net.modes import apply_mode
from .util import read_text

SYS_NET = Path("/sys/class/net")
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "host.yaml"


def _exists(name: str) -> bool:
    return bool(name) and (SYS_NET / name).exists()


def _carrier(name: str) -> bool:
    return read_text(SYS_NET / name / "carrier") == "1"


def _is_wireless(name: str) -> bool:
    return (SYS_NET / name / "phy80211").exists()


def detect_home_gateway(
    config: Config,
    exists: Callable[[str], bool] = _exists,
    carrier: Callable[[str], bool] = _carrier,
    routes_getter: Callable[[], list[dict[str, Any]]] = get_default_routes,
) -> tuple[bool, list[str]]:
    """判断当前硬件是否满足家庭网关拓扑。

    条件：下游 USB 网卡存在且有载波、有有线载波的上行、默认网关是 home_gateway。
    """
    gateway_iface = config.gateway_iface
    if not gateway_iface:
        return False, ["未配置 gateway_iface"]
    if not exists(gateway_iface):
        return False, [f"未找到下游网卡 {gateway_iface}"]
    if not carrier(gateway_iface):
        return False, [f"{gateway_iface} 未连接（无载波）"]

    uplink = ""
    for name in config.uplink_candidates:
        if _is_wireless(name) or not exists(name) or not carrier(name):
            continue
        uplink = name
        break
    if not uplink:
        return False, ["没有已连接网线的有线上行"]

    routes = routes_getter()
    if config.home_gateway:
        route = next(
            (item for item in routes if item.get("gateway") == config.home_gateway),
            None,
        )
        if route is None:
            gateways = ", ".join(
                sorted({str(item.get("gateway")) for item in routes if item.get("gateway")})
            )
            return False, [
                f"默认网关不是 {config.home_gateway}（当前 {gateways or '无'}）"
            ]
        if route.get("dev") and route["dev"] != uplink:
            return False, [
                f"默认网关 {config.home_gateway} 走 {route['dev']}，不是有线上行 {uplink}"
            ]

    return True, [f"下游 {gateway_iface}", f"上行 {uplink}"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="开机检测家庭网关硬件并自动启用")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    parser.add_argument(
        "--wait",
        type=float,
        default=90.0,
        help="等待硬件就绪的最长秒数，默认 90",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只检测并打印结果，不修改网络",
    )
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    deadline = time.monotonic() + max(0.0, args.wait)
    reasons: list[str] = []
    matched = False

    while True:
        matched, reasons = detect_home_gateway(config)
        if matched or time.monotonic() >= deadline:
            break
        time.sleep(2.0)

    if not matched:
        print("未检测到家庭网关硬件连接: " + "；".join(reasons))
        return 0

    print("检测到家庭网关硬件连接: " + "；".join(reasons))
    if args.dry_run:
        print("dry-run: 不修改网络")
        return 0

    result = apply_mode(config, "gateway", proxy=False)
    for step in result.get("steps", []):
        print(f"{'OK' if step.get('ok') else 'FAIL'} {step.get('name')}: {step.get('detail') or ''}")
    print(f"家庭网关: {'已启用' if result.get('ok') else '部分步骤失败'}（代理保持关闭）")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
