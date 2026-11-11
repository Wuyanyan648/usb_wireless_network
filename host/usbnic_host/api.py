from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .config import Config
from .monitor.clash_proxies import (
    proxy_overview,
    restore_auto,
    select_proxy,
    test_delay,
)
from .monitor.collector import Monitor
from .monitor.hotspot import hotspot_info
from .monitor.proxy import (
    clash_get,
    enable_tproxy,
    host_proxy_pref,
    refresh_proxy_set,
    set_host_proxy,
    set_proxy,
    tproxy_installed,
)
from .monitor import device_policy
from .monitor.throughput import ThroughputMonitor
from .net.modes import MODES, MODE_PROXY, apply_mode
from .net.router import invalidate_router_status, probe_5g_ap, router_status
from .net.speedtest import SpeedTestManager
from . import state

WEB_DIR = Path(__file__).parent / "web"


class ProxyToggleRequest(BaseModel):
    enable: bool


class HostProxyRequest(BaseModel):
    enable: bool


class ProxySelectRequest(BaseModel):
    group: str
    name: str


class ProxyAutoRequest(BaseModel):
    group: str


class ProxyDelayRequest(BaseModel):
    target: str


class ModeRequest(BaseModel):
    mode: str
    # None: 按模式默认值；false: 切换模式但不开启代理（开机场景）
    proxy: bool | None = None


class KeepRequest(BaseModel):
    enabled: bool = False


class DevicePolicyRequest(BaseModel):
    mac: str
    policy: str


class DefaultPolicyRequest(BaseModel):
    policy: str


# 这些模式下下游流量需要 TPROXY；client 模式只代理本机，不装表
TPROXY_MODES = ("gateway", "router", "hotspot", "tun")


def _install_tproxy_if_needed(config: Config, monitor: Monitor) -> dict:
    """设备改成走代理后按需装表，避免"设了策略却什么都没发生"。"""
    if tproxy_installed():
        return {"ok": True, "installed": True, "detail": "已在接管中", "steps": []}
    # 用持久记录的模式判断，不用采集器快照（有约 2 秒延迟，刚切模式时会误判）
    saved = state.load(config.data_dir) or {}
    mode = str(saved.get("mode") or monitor.snapshot().get("mode") or "client")
    if mode not in TPROXY_MODES:
        return {"ok": False, "installed": False, "detail": f"{mode} 模式不接管下游", "steps": []}
    tun = bool((clash_get(config, "/configs").get("tun") or {}).get("enable"))
    if not tun:
        return {
            "ok": False,
            "installed": False,
            "detail": "代理未开启，先打开代理再设置",
            "steps": [],
        }
    result = enable_tproxy(config, mode=mode)
    return {
        "ok": bool(result.get("ok")),
        "installed": bool(result.get("ok")),
        "detail": str(result.get("detail") or result.get("error") or ""),
        "steps": result.get("steps") or [],
    }


def _install_error(install: dict) -> str:
    """把装表结果里失败的那一步拼进提示，便于页面直接显示。"""
    detail = str(install.get("detail") or "装表失败")
    for step in install.get("steps") or []:
        if step.get("ok"):
            continue
        name = str(step.get("name") or "step")
        extra = str(step.get("detail") or step.get("error") or "").strip()
        return f"{detail}（{name}: {extra}）" if extra else f"{detail}（{name}）"
    return detail


def create_app(monitor: Monitor, config: Config) -> FastAPI:
    app = FastAPI(
        title="usbnic 上位机",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    speedtest = SpeedTestManager(config, monitor)
    throughput = ThroughputMonitor(config, monitor)
    throughput.start()
    # 只挂计数器，不改变转发；失败不影响主功能
    from .monitor import device_stats

    device_stats.ensure(config)

    @app.middleware("http")
    async def no_store(request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/") or path.startswith("/api/"):
            # 本地控制页与接口都实时变化，避免浏览器拿旧 JS / 旧数据
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/favicon.ico")
    def favicon() -> JSONResponse:
        return JSONResponse(status_code=204, content=None)

    @app.get("/api/health")
    def health() -> dict:
        snapshot = monitor.snapshot()
        return {
            "ok": True,
            "version": __version__,
            "ready": bool(snapshot.get("ready")),
            "mode": snapshot.get("mode", "client"),
            "mode_label": snapshot.get("mode_label", ""),
            "readonly": False,
            "uptime": snapshot.get("uptime", 0),
            "server_time": time.time(),
        }

    @app.get("/api/status")
    def status() -> dict:
        return monitor.snapshot()

    @app.get("/api/hotspot")
    def hotspot() -> dict:
        return hotspot_info(config)

    @app.get("/api/devices")
    def devices() -> dict:
        snapshot = monitor.snapshot()
        return snapshot.get("devices", {"summary": {}, "items": []})

    @app.get("/api/interfaces")
    def interfaces() -> dict:
        snapshot = monitor.snapshot()
        return {"items": snapshot.get("interfaces", [])}

    @app.get("/api/history")
    def history(
        iface: str | None = Query(default=None),
        minutes: int = Query(default=30, ge=1, le=config.history_minutes),
    ) -> dict:
        return monitor.history(iface=iface, minutes=minutes)

    @app.get("/api/events")
    def events(limit: int = Query(default=100, ge=1, le=500)) -> dict:
        return {"items": monitor.events(limit=limit)}

    @app.get("/api/usage")
    def usage_today() -> dict:
        """今日前台应用使用时长排行（只读，不含窗口标题）。"""
        return monitor.usage.snapshot()

    @app.get("/api/usage/trend")
    def usage_trend(days: int = Query(default=7, ge=2, le=90)) -> dict:
        return monitor.usage.trend(days)

    @app.get("/api/system/health")
    def system_health(
        hours: float = Query(default=6.0, ge=0.5, le=720),
        points: int = Query(default=240, ge=30, le=600),
    ) -> dict:
        """CPU / 内存 / swap / 磁盘 / 温度曲线。"""
        return monitor.system_health.snapshot(hours, points)

    @app.post("/api/speedtest/start")
    def speedtest_start() -> dict:
        result = speedtest.start()
        if not result["ok"]:
            return JSONResponse(status_code=409, content=result)
        return result

    @app.get("/api/speedtest/status")
    def speedtest_status() -> dict:
        return {"ok": True, **speedtest.status()}

    @app.get("/api/speedtest/history")
    def speedtest_history(limit: int = Query(default=20, ge=1, le=50)) -> dict:
        return {"items": speedtest.history(limit=limit)}

    @app.get("/api/throughput")
    def throughput_status(iface: str | None = Query(default=None)) -> dict:
        return throughput.snapshot(iface)

    @app.post("/api/throughput/keep")
    def throughput_keep(request: KeepRequest) -> dict:
        throughput.set_keep(request.enabled)
        return {"ok": True, "keep": request.enabled}

    @app.get("/api/config")
    def public_config() -> dict:
        return config.public_dict()

    @app.post("/api/proxy/toggle")
    def proxy_toggle(body: ProxyToggleRequest) -> dict:
        mode = monitor.snapshot().get("mode", "client")
        result = set_proxy(config, body.enable, mode)
        # 代理是单独的手动选择，同步进上次模式记录，重启重放时不会被改回去
        if result["ok"]:
            saved = state.load(config.data_dir)
            if saved:
                state.save(config.data_dir, saved["mode"], body.enable, explicit=True)
            # 总开关重新打开时，按已保存的设备策略恢复 TPROXY 接管
            if body.enable:
                result["install"] = _install_tproxy_if_needed(config, monitor)
        # 等采集器完成下一次采样，避免前端拿到切换前的旧状态
        time.sleep(2.2)
        snapshot = monitor.snapshot()
        return {
            "ok": result["ok"],
            "error": result["error"],
            "proxy": snapshot.get("proxy", {}),
        }

    @app.put("/api/proxy/host")
    def proxy_host(body: HostProxyRequest) -> dict:
        """主机代理开关：只切 tun.enable，不碰 TPROXY 表与设备策略。"""
        proxy_state = monitor.snapshot().get("proxy", {})
        if not (proxy_state.get("tun") or proxy_state.get("tproxy")):
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": "总代理未开启，主机开关不生效"},
            )
        result = set_host_proxy(config, body.enable)
        time.sleep(1.2)
        return {
            "ok": result["ok"],
            "error": result["error"],
            "host_proxy": bool(body.enable) if result["ok"] else host_proxy_pref(config),
            "proxy": monitor.snapshot().get("proxy", {}),
        }

    @app.get("/api/proxies")
    def proxies() -> dict:
        return proxy_overview(config)

    @app.post("/api/proxies/select")
    def proxies_select(body: ProxySelectRequest) -> dict:
        result = select_proxy(config, body.group, body.name)
        result["overview"] = proxy_overview(config)
        return result

    @app.post("/api/proxies/auto")
    def proxies_auto(body: ProxyAutoRequest) -> dict:
        result = restore_auto(config, body.group)
        result["overview"] = proxy_overview(config)
        return result

    @app.post("/api/proxies/delay")
    def proxies_delay(body: ProxyDelayRequest) -> dict:
        result = test_delay(config, body.target)
        result["overview"] = proxy_overview(config)
        return result

    @app.post("/api/mode")
    def mode_switch(body: ModeRequest) -> dict:
        if body.mode not in MODES:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": f"未知模式: {body.mode}"},
            )
        result = apply_mode(config, body.mode, body.proxy)
        # 模式切换改了 NM 连接，只读状态的缓存立刻作废
        invalidate_router_status()
        if result["ok"]:
            # 只有成功的选择才落盘，供守护进程重启后重放
            wanted = MODE_PROXY[body.mode] if body.proxy is None else bool(body.proxy)
            state.save(config.data_dir, body.mode, wanted, explicit=body.proxy is not None)
        time.sleep(4.0 if body.mode == "router" else 2.5)
        snapshot = monitor.snapshot()
        return {
            "ok": result["ok"],
            "error": ""
            if result["ok"]
            else str(result.get("error") or "部分步骤失败"),
            "steps": result.get("steps", []),
            "fallback": result.get("fallback", ""),
            "degraded": result.get("degraded", []),
            "mode": snapshot.get("mode", body.mode),
            "mode_label": snapshot.get("mode_label", ""),
            "proxy": snapshot.get("proxy", {}),
        }

    @app.get("/api/devices/traffic")
    def devices_traffic() -> dict:
        """按设备的下行/上行实时速率与累计量（只读，来自 nftables 计数器）。"""
        return throughput.device_snapshot()

    @app.get("/api/devices/policies")
    def devices_policies() -> dict:
        """设备级策略：谁走代理、谁直连，以及当前是否已生效。"""
        data = device_policy.table(config)
        proxy_state = monitor.snapshot().get("proxy", {})
        saved = state.load(config.data_dir) or {}
        # 总开关是否开启以持久记录为准：主机 TUN 关掉、暂无设备走代理时仍是开启态
        data["total"] = bool(saved.get("proxy") or proxy_state.get("tproxy"))
        return data

    @app.put("/api/devices/policy")
    def devices_policy(body: DevicePolicyRequest) -> dict:
        result = device_policy.set_device(config, body.mac, body.policy)
        if not result["ok"]:
            return JSONResponse(status_code=400, content=result)
        # 代理已开启时，把设备改成"走代理"应当立刻装表，而不是等下一次代理开关
        if body.policy == "proxy":
            install = _install_tproxy_if_needed(config, monitor)
            result["install"] = install
            if not install.get("ok"):
                # 装表失败就把策略回滚，别让开关显示"走代理"而 effective 是直连
                device_policy.set_device(config, body.mac, "direct")
                refresh_proxy_set(config)
                return JSONResponse(
                    status_code=409,
                    content={
                        **device_policy.table(config),
                        "ok": False,
                        "error": _install_error(install),
                        "install": install,
                    },
                )
        result["refresh"] = refresh_proxy_set(config)
        result.update(device_policy.table(config))
        return result

    @app.put("/api/devices/policy/default")
    def devices_policy_default(body: DefaultPolicyRequest) -> dict:
        result = device_policy.set_default(config, body.policy)
        if not result["ok"]:
            return JSONResponse(status_code=400, content=result)
        result["refresh"] = refresh_proxy_set(config)
        result.update(device_policy.table(config))
        return result

    @app.get("/api/router/status")
    def router_info() -> dict:
        return router_status(config)

    @app.post("/api/router/probe")
    def router_probe() -> dict:
        """只读验证：AX211 起 AP 约 30 秒后自动断开，不切换模式。"""
        try:
            return probe_5g_ap(config)
        finally:
            # 探测过程中建/删过 profile 与 AP，缓存必须作废
            invalidate_router_status()

    return app
