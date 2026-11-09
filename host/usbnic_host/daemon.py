from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

import uvicorn

from . import __version__, state
from .api import create_app
from .boot import detect_home_gateway
from .config import Config
from .monitor.collector import Monitor
from .net.modes import apply_mode, reinforce_mode

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "host.yaml"


def replay_last_mode(
    config: Config, logger: logging.Logger, monitor: Monitor | None = None
) -> None:
    """把用户上次成功应用的网络模式重放一次（stop → start 后不用人工点）。

    只重放**落盘的选择**，没有记录或记录是 `client`（默认态）时不动网络。
    代理开关只有被用户手动拨过才恢复，否则按开机约定保持关闭。

    如果 NetworkManager 里连接已经处于目标模式（重启守护进程时很常见），只补齐
    sysctl 转发与代理，不做 `nmcli down/up`，避免把正在上网的网关短暂掐断。
    """
    saved = state.load(config.data_dir)
    if not saved:
        logger.info("没有上次模式记录，保持默认 client")
        return
    if saved["mode"] == "client":
        logger.info("上次模式是 client（默认态），启动不重放")
        return

    time.sleep(3.0)  # 等 NetworkManager / 网卡就绪
    proxy = saved["proxy"] if saved.get("proxy_explicit") else False
    current = ""
    if monitor is not None:
        try:
            current = str(monitor.snapshot().get("mode") or "")
        except Exception:  # noqa: BLE001 - 取不到就当未知，走完整重放
            current = ""
    if current == saved["mode"]:
        result = reinforce_mode(config, saved["mode"], proxy)
        how = "连接已是该模式，只补转发/代理"
    else:
        if saved["mode"] == "gateway":
            # 不在家（下游没载波 / 上行不是有线 / 默认网关不对）时不强行拉起网关
            matched, reasons = detect_home_gateway(config)
            if not matched:
                logger.info(
                    "上次模式是 gateway，但当前硬件不满足家庭网关拓扑（%s），本次不重放",
                    "；".join(reasons),
                )
                return
        result = apply_mode(config, saved["mode"], proxy=proxy)
        how = "重新应用模式"
    failed = [s for s in result.get("steps", []) if not s.get("ok")]
    if result.get("ok"):
        logger.info(
            "已重放上次模式 %s（代理%s，记录于 %s；%s）",
            saved["mode"],
            "开" if proxy else "关",
            saved.get("saved_at") or "未知",
            how,
        )
    else:
        logger.warning(
            "重放上次模式 %s 有 %d 步失败: %s",
            saved["mode"],
            len(failed),
            "；".join("%s: %s" % (s.get("name"), s.get("detail") or "") for s in failed),
        )


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

    # 后台重放上次模式：不阻塞 HTTP 服务起来，也不在没记录时碰网络
    threading.Thread(
        target=replay_last_mode,
        args=(config, logger, monitor),
        name="mode-replay",
        daemon=True,
    ).start()

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
