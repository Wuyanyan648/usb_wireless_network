from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Config:
    """上位机运行配置：采集参数与三种网络模式使用的连接名。"""

    bind: str = "127.0.0.1"
    port: int = 8787
    poll_interval: float = 2.0
    health_interval: float = 5.0
    history_minutes: int = 60
    data_dir: Path = Path("data")
    clash_api: str = "http://127.0.0.1:9097"
    clash_secret: str = ""
    oui_file: str = "/usr/share/ieee-data/oui.txt"
    uplink_candidates: list[str] = field(default_factory=list)
    exclude_interfaces: list[str] = field(
        default_factory=lambda: ["lo", "tailscale0"]
    )
    public_targets: list[str] = field(
        default_factory=lambda: ["223.5.5.5", "1.1.1.1"]
    )
    dns_target: str = "www.baidu.com"
    health_timeout: float = 1.2
    http_timeout: float = 1.5
    log_level: str = "info"
    device_aliases: dict[str, str] = field(default_factory=dict)
    # 三种网络模式使用的 NetworkManager 连接名与下游网关参数
    uplink_connection: str = "usbnic-uplink"
    gateway_connection: str = "usbnic-gateway"
    hotspot_connection: str = "usbnic-hotspot"
    gateway_iface: str = ""
    lan_cidr: str = "10.10.10.0/24"
    hotspot_iface: str = ""
    hotspot_cidr: str = "192.168.50.0/24"
    clash_tproxy_port: int = 7896
    clash_redir_port: int = 7895

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        config_path = Path(path).expanduser().resolve()
        raw: dict[str, Any] = {}
        if config_path.is_file():
            with config_path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}

        data_dir = Path(raw.get("data_dir", "data")).expanduser()
        if not data_dir.is_absolute():
            data_dir = (config_path.parent / data_dir).resolve()

        cfg = cls(
            bind=str(raw.get("bind", "127.0.0.1")),
            port=int(raw.get("port", 8787)),
            poll_interval=float(raw.get("poll_interval", 2.0)),
            health_interval=float(raw.get("health_interval", 5.0)),
            history_minutes=int(raw.get("history_minutes", 60)),
            data_dir=data_dir,
            clash_api=str(raw.get("clash_api", "http://127.0.0.1:9097")).rstrip("/"),
            clash_secret=str(raw.get("clash_secret", "") or ""),
            oui_file=str(raw.get("oui_file", "/usr/share/ieee-data/oui.txt")),
            uplink_candidates=list(raw.get("uplink_candidates", []) or []),
            exclude_interfaces=list(
                raw.get("exclude_interfaces", ["lo", "tailscale0"]) or []
            ),
            public_targets=list(
                raw.get("public_targets", ["223.5.5.5", "1.1.1.1"]) or []
            ),
            dns_target=str(raw.get("dns_target", "www.baidu.com")),
            health_timeout=float(raw.get("health_timeout", 1.2)),
            http_timeout=float(raw.get("http_timeout", 1.5)),
            log_level=str(raw.get("log_level", "info")),
            device_aliases={
                str(key).lower().replace("-", ":"): str(value)
                for key, value in (raw.get("device_aliases") or {}).items()
            },
            uplink_connection=str(raw.get("uplink_connection", "usbnic-uplink")),
            gateway_connection=str(raw.get("gateway_connection", "usbnic-gateway")),
            hotspot_connection=str(raw.get("hotspot_connection", "usbnic-hotspot")),
            gateway_iface=str(raw.get("gateway_iface", "") or ""),
            lan_cidr=str(raw.get("lan_cidr", "10.10.10.0/24")),
            hotspot_iface=str(raw.get("hotspot_iface", "") or ""),
            hotspot_cidr=str(raw.get("hotspot_cidr", "192.168.50.0/24")),
            clash_tproxy_port=int(raw.get("clash_tproxy_port", 7896)),
            clash_redir_port=int(raw.get("clash_redir_port", 7895)),
        )
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        return cfg

    def public_dict(self) -> dict[str, Any]:
        """返回可安全展示给前端的配置，不包含 secret。"""
        return {
            "bind": self.bind,
            "port": self.port,
            "poll_interval": self.poll_interval,
            "health_interval": self.health_interval,
            "history_minutes": self.history_minutes,
            "data_dir": str(self.data_dir),
            "clash_api": self.clash_api,
            "uplink_candidates": list(self.uplink_candidates),
            "exclude_interfaces": list(self.exclude_interfaces),
            "public_targets": list(self.public_targets),
            "dns_target": self.dns_target,
            "uplink_connection": self.uplink_connection,
            "gateway_connection": self.gateway_connection,
            "hotspot_connection": self.hotspot_connection,
            "gateway_iface": self.gateway_iface,
            "lan_cidr": self.lan_cidr,
            "hotspot_iface": self.hotspot_iface,
            "hotspot_cidr": self.hotspot_cidr,
            "clash_tproxy_port": self.clash_tproxy_port,
            "clash_redir_port": self.clash_redir_port,
        }
