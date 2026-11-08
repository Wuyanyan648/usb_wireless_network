from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

from . import __version__
from .api import create_app
from .config import Config
from .monitor.collector import Monitor

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "host.yaml"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="usbnic 上位机服务")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="配置文件路径，默认 host/config/host.yaml",
    )
    parser.add_argument("--host", default=None, help="监听地址，覆盖配置文件")
    parser.add_argument("--port", type=int, default=None, help="监听端口，覆盖配置文件")
    parser.add_argument("--data-dir", default=None, help="数据目录，覆盖配置文件")
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="日志级别",
    )
    parser.add_argument("--version", action="version", version=f"usbnic-host {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = Config.load(args.config)
    if args.host:
        config.bind = args.host
    if args.port:
        config.port = args.port
    if args.data_dir:
        config.data_dir = Path(args.data_dir).expanduser().resolve()
        config.data_dir.mkdir(parents=True, exist_ok=True)
    if args.log_level:
        config.log_level = args.log_level

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("usbnic.host")

    if os.geteuid() != 0:
        logger.warning(
            "以非 root 运行：DHCP 租约、部分 iw 关联信息可能不可见；生产环境请用 systemd 以 root 启动"
        )

    monitor = Monitor(config)
    monitor.start()
    app = create_app(monitor, config)

    logger.info("usbnic 上位机 %s 启动: http://%s:%s", __version__, config.bind, config.port)
    logger.info("支持网络模式切换与透明代理控制")
    logger.info("数据目录: %s", config.data_dir)

    try:
        uvicorn.run(
            app,
            host=config.bind,
            port=config.port,
            log_level=config.log_level,
            access_log=False,
        )
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        logger.info("usbnic 上位机已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
