from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .util import normalize_mac

# 设备级代理策略只有两种：走代理、直连。默认直连，保证装表之外的行为不变。
POLICIES = ("proxy", "direct")


def _policy(value: Any, fallback: str = "direct") -> str:
    text = str(value or "").strip().lower()
    return text if text in POLICIES else fallback


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
    # Clash Verge 的 root 服务，用于代理开启时自动拉起内核
    clash_service_api: str = "http://127.0.0.1:33211"
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
    # 默认在设备表里保留本机接口地址并标注"本机"；置 true 则从列表过滤掉
    hide_local_addresses: bool = False
    # 三种网络模式使用的 NetworkManager 连接名与下游网关参数
    uplink_connection: str = "usbnic-uplink"
    gateway_connection: str = "usbnic-gateway"
    hotspot_connection: str = "usbnic-hotspot"
    gateway_iface: str = ""
    lan_cidr: str = "10.10.10.0/24"
    hotspot_iface: str = ""
    hotspot_cidr: str = "192.168.50.0/24"
    # 热点密码文件：仅 root 可读，明文不进入仓库
    hotspot_password_file: str = "/etc/usbnic/hotspot.pass"
    # 第四种模式「移动路由器」：有线扩展口 + 5G/2.4G 双 AP
    router_lan_connection: str = "usbnic-router-lan"
    router_5g_connection: str = "usbnic-router-5g"
    router_24g_connection: str = "usbnic-router-24g"
    router_lan_iface: str = ""  # 留空则沿用 gateway_iface
    router_lan_cidr: str = "10.10.10.0/24"
    # 留空表示按能力自动挑一块能起 5G AP 的网卡
    router_5g_iface: str = ""
    router_5g_cidr: str = "10.20.0.0/24"
    router_5g_ssid: str = "Laptop-5G"
    router_5g_channel: int = 36
    router_24g_iface: str = "wlx6c1ff7f46d6d"
    router_24g_cidr: str = "10.30.0.0/24"
    router_24g_ssid: str = "Laptop-2.4G"
    router_24g_channel: int = 6
    # 默认关闭：上游华为 AP 的 2.4G 信道不可调，同频会互相压制
    router_24g_enabled: bool = False
    # 默认「有线整段直连、无线段走代理」；可按网段/单 IP 追加例外
    router_wired_direct: bool = True
    router_direct_cidrs: list[str] = field(default_factory=list)
    router_ap_probe_seconds: float = 30.0
    # 家庭网关开机判定：上游默认网关必须是这个地址（留空则只看网卡载波）
    home_gateway: str = ""
    # 宽带测速：多线程直连国内镜像，上传端点需能接收 POST
    speedtest_downloads: list[str] = field(
        default_factory=lambda: [
            "https://mirrors.aliyun.com/ubuntu/ls-lR.gz",
            "https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ls-lR.gz",
            "https://mirrors.cloud.tencent.com/ubuntu/ls-lR.gz",
        ]
    )
    speedtest_upload_url: str = "https://speed.cloudflare.com/__up"
    speedtest_dns: list[str] = field(
        default_factory=lambda: ["223.5.5.5", "119.29.29.29"]
    )
    speedtest_latency_target: str = "mirrors.aliyun.com"
    speedtest_duration: float = 6.0
    speedtest_max_mb: int = 350
    clash_tproxy_port: int = 7896
    clash_redir_port: int = 7895
    # mihomo 的 rule-provider 目录；usbnicd 在这里维护 usbnic-proxy.yaml，
    # 让被 TPROXY 接住的下游设备排在 GEOIP,CN 之前全走代理。留空表示不启用。
    clash_ruleset_dir: str = ""
    # TPROXY 永不接管的源地址：走有线直连、正在大流量下载的设备（如 Switch 2）
    tproxy_exclude_ips: list[str] = field(default_factory=list)
    # 装表后的观察窗口（秒）：窗口内命中不增长且下游有流量就自动撤表；0 表示不校验
    tproxy_verify_seconds: float = 5.0
    # 设备级代理策略：全局默认（direct 保持现有行为），可按 MAC 单独覆盖
    tproxy_default_policy: str = "proxy"
    # host.yaml 里的初始策略；运行期以 data_dir 下的 JSON 文件为准
    device_policies: dict[str, str] = field(default_factory=dict)
    device_policies_file: str = "device_policies.json"
    # ---------- 使用时长 / 系统健康（并入 usbnicd，不单独起服务） ----------
    usage_enabled: bool = True
    # 活动窗口查询间隔（秒）：每次只查一次 _NET_ACTIVE_WINDOW
    usage_sample_interval: float = 4.0
    # XScreenSaver 空闲超过这个秒数视为离开，不再计时
    usage_idle_seconds: float = 300.0
    # 默认只记录应用名；打开后才把窗口标题写进 app_titles
    usage_record_window_title: bool = False
    usage_retention_days: int = 90
    # 系统健康采样间隔（秒）与保留天数
    system_health_interval: float = 30.0
    system_health_retention_days: int = 7
    # 留空则自动从 who / /run/user/<uid>/gdm/Xauthority 发现
    usage_x11_display: str = ""
    usage_x11_authority: str = ""

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
            clash_service_api=str(
                raw.get("clash_service_api", "http://127.0.0.1:33211")
            ).rstrip("/"),
            clash_secret=str(raw.get("clash_secret", "") or ""),
            clash_ruleset_dir=str(raw.get("clash_ruleset_dir", "") or ""),
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
            hide_local_addresses=bool(raw.get("hide_local_addresses", False)),
            uplink_connection=str(raw.get("uplink_connection", "usbnic-uplink")),
            gateway_connection=str(raw.get("gateway_connection", "usbnic-gateway")),
            hotspot_connection=str(raw.get("hotspot_connection", "usbnic-hotspot")),
            gateway_iface=str(raw.get("gateway_iface", "") or ""),
            lan_cidr=str(raw.get("lan_cidr", "10.10.10.0/24")),
            hotspot_iface=str(raw.get("hotspot_iface", "") or ""),
            hotspot_cidr=str(raw.get("hotspot_cidr", "192.168.50.0/24")),
            hotspot_password_file=str(
                raw.get("hotspot_password_file", "/etc/usbnic/hotspot.pass")
                or "/etc/usbnic/hotspot.pass"
            ),
            router_lan_connection=str(
                raw.get("router_lan_connection", "usbnic-router-lan")
                or "usbnic-router-lan"
            ),
            router_5g_connection=str(
                raw.get("router_5g_connection", "usbnic-router-5g")
                or "usbnic-router-5g"
            ),
            router_24g_connection=str(
                raw.get("router_24g_connection", "usbnic-router-24g")
                or "usbnic-router-24g"
            ),
            router_lan_iface=str(raw.get("router_lan_iface", "") or ""),
            router_lan_cidr=str(raw.get("router_lan_cidr", "10.10.10.0/24")),
            router_5g_iface=str(raw.get("router_5g_iface", "wlp0s20f3") or ""),
            router_5g_cidr=str(raw.get("router_5g_cidr", "10.20.0.0/24")),
            router_5g_ssid=str(raw.get("router_5g_ssid", "Laptop-5G") or "Laptop-5G"),
            router_5g_channel=int(raw.get("router_5g_channel", 36)),
            router_24g_iface=str(
                raw.get("router_24g_iface", "wlx6c1ff7f46d6d") or ""
            ),
            router_24g_cidr=str(raw.get("router_24g_cidr", "10.30.0.0/24")),
            router_24g_ssid=str(
                raw.get("router_24g_ssid", "Laptop-2.4G") or "Laptop-2.4G"
            ),
            router_24g_channel=int(raw.get("router_24g_channel", 6)),
            router_24g_enabled=bool(raw.get("router_24g_enabled", False)),
            router_wired_direct=bool(raw.get("router_wired_direct", True)),
            router_direct_cidrs=[
                str(item).strip()
                for item in (raw.get("router_direct_cidrs") or [])
                if str(item).strip()
            ],
            router_ap_probe_seconds=float(raw.get("router_ap_probe_seconds", 30.0)),
            home_gateway=str(raw.get("home_gateway", "") or ""),
            speedtest_downloads=[
                str(item)
                for item in (
                    raw.get("speedtest_downloads")
                    or [
                        "https://mirrors.aliyun.com/ubuntu/ls-lR.gz",
                        "https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ls-lR.gz",
                        "https://mirrors.cloud.tencent.com/ubuntu/ls-lR.gz",
                    ]
                )
                if str(item).strip()
            ],
            speedtest_upload_url=str(
                raw.get("speedtest_upload_url", "https://speed.cloudflare.com/__up")
                or "https://speed.cloudflare.com/__up"
            ),
            speedtest_dns=[
                str(item)
                for item in (raw.get("speedtest_dns") or ["223.5.5.5", "119.29.29.29"])
                if str(item).strip()
            ],
            speedtest_latency_target=str(
                raw.get("speedtest_latency_target", "mirrors.aliyun.com")
                or "mirrors.aliyun.com"
            ),
            speedtest_duration=float(raw.get("speedtest_duration", 6.0)),
            speedtest_max_mb=int(raw.get("speedtest_max_mb", 350)),
            clash_tproxy_port=int(raw.get("clash_tproxy_port", 7896)),
            clash_redir_port=int(raw.get("clash_redir_port", 7895)),
            tproxy_exclude_ips=[
                str(item).strip()
                for item in (raw.get("tproxy_exclude_ips") or [])
                if str(item).strip()
            ],
            tproxy_verify_seconds=float(raw.get("tproxy_verify_seconds", 5.0)),
            tproxy_default_policy=_policy(raw.get("tproxy_default_policy", "proxy")),
            device_policies={
                mac: _policy(policy)
                for mac, policy in (
                    (normalize_mac(str(key)), value)
                    for key, value in (raw.get("device_policies") or {}).items()
                )
                if mac
            },
            device_policies_file=str(
                raw.get("device_policies_file", "device_policies.json")
                or "device_policies.json"
            ),
            usage_enabled=bool(raw.get("usage_enabled", True)),
            usage_sample_interval=float(raw.get("usage_sample_interval", 4.0)),
            usage_idle_seconds=float(raw.get("usage_idle_seconds", 300.0)),
            usage_record_window_title=bool(raw.get("usage_record_window_title", False)),
            usage_retention_days=int(raw.get("usage_retention_days", 90)),
            system_health_interval=float(raw.get("system_health_interval", 30.0)),
            system_health_retention_days=int(
                raw.get("system_health_retention_days", 7)
            ),
            usage_x11_display=str(raw.get("usage_x11_display", "") or ""),
            usage_x11_authority=str(raw.get("usage_x11_authority", "") or ""),
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
            "clash_service_api": self.clash_service_api,
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
            "hotspot_password_file": self.hotspot_password_file,
            "router_lan_connection": self.router_lan_connection,
            "router_5g_connection": self.router_5g_connection,
            "router_24g_connection": self.router_24g_connection,
            "router_lan_iface": self.router_lan_iface,
            "router_lan_cidr": self.router_lan_cidr,
            "router_5g_iface": self.router_5g_iface,
            "router_5g_cidr": self.router_5g_cidr,
            "router_5g_ssid": self.router_5g_ssid,
            "router_5g_channel": self.router_5g_channel,
            "router_24g_iface": self.router_24g_iface,
            "router_24g_cidr": self.router_24g_cidr,
            "router_24g_ssid": self.router_24g_ssid,
            "router_24g_channel": self.router_24g_channel,
            "router_24g_enabled": self.router_24g_enabled,
            "router_wired_direct": self.router_wired_direct,
            "router_direct_cidrs": list(self.router_direct_cidrs),
            "router_ap_probe_seconds": self.router_ap_probe_seconds,
            "home_gateway": self.home_gateway,
            "clash_tproxy_port": self.clash_tproxy_port,
            "clash_redir_port": self.clash_redir_port,
            "tproxy_exclude_ips": list(self.tproxy_exclude_ips),
            "tproxy_verify_seconds": self.tproxy_verify_seconds,
            "tproxy_default_policy": self.tproxy_default_policy,
            "usage_enabled": self.usage_enabled,
            "usage_sample_interval": self.usage_sample_interval,
            "usage_idle_seconds": self.usage_idle_seconds,
            "usage_record_window_title": self.usage_record_window_title,
            "usage_retention_days": self.usage_retention_days,
            "system_health_interval": self.system_health_interval,
            "system_health_retention_days": self.system_health_retention_days,
        }
