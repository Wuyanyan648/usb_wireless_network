from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from .config import Config
from .monitor.proxy import (
    clash_patch,
    cleanup_tproxy,
    clear_proxy_mark,
    proxy_mark,
    tproxy_active,
)

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "host.yaml"


def cleanup(config: Config, disable_proxy: bool) -> dict[str, Any]:
    """清理上位机创建的 nftables/策略路由；可选关闭它开启的透明代理。"""
    mark = proxy_mark(config)
    cleanup_tproxy(config)
    result: dict[str, Any] = {
        "rules_clean": not tproxy_active(),
        "proxy_mark": mark,
        "proxy_disabled": None,
        "proxy_error": "",
    }

    if disable_proxy:
        if mark:
            patch = clash_patch(config, {"tun": {"enable": False}, "tproxy-port": 0})
            result["proxy_disabled"] = bool(patch["ok"])
            result["proxy_error"] = str(patch.get("error") or "")
            if patch["ok"]:
                clear_proxy_mark(config)
        else:
            # 没有标记说明不是上位机开启的代理，只回收 TPROXY 端口
            clash_patch(config, {"tproxy-port": 0})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理 usbnic 上位机网络资源")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    parser.add_argument(
        "--proxy",
        action="store_true",
        help="同时关闭上位机开启的 TUN / TPROXY",
    )
    args = parser.parse_args(argv)

    if os.geteuid() != 0:
        print("需要 root 才能清理 nftables / 策略路由，请用 sudo 或 pkexec 运行", file=sys.stderr)
        return 1

    config = Config.load(args.config)
    result = cleanup(config, args.proxy)

    if result["rules_clean"]:
        print("nftables / 策略路由: 已清理")
    else:
        print("nftables / 策略路由: 清理失败", file=sys.stderr)

    if result["proxy_disabled"] is True:
        print(f"透明代理: 已关闭（{result['proxy_mark']}）")
    elif result["proxy_disabled"] is False:
        print(
            f"透明代理: 关闭失败 {result['proxy_error']}".rstrip(),
            file=sys.stderr,
        )
    elif result["proxy_mark"]:
        print(f"透明代理: 保持开启（{result['proxy_mark']}）")
    elif args.proxy:
        print("透明代理: 未由上位机开启，保持原状")

    return 0 if result["rules_clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
