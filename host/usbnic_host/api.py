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
from .monitor.proxy import set_proxy
from .monitor.throughput import ThroughputMonitor
from .net.modes import MODES, MODE_PROXY, apply_mode
from .net.speedtest import SpeedTestManager
from . import state

WEB_DIR = Path(__file__).parent / "web"


class ProxyToggleRequest(BaseModel):
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
        # 等采集器完成下一次采样，避免前端拿到切换前的旧状态
        time.sleep(2.2)
        snapshot = monitor.snapshot()
        return {
            "ok": result["ok"],
            "error": result["error"],
            "proxy": snapshot.get("proxy", {}),
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
        if result["ok"]:
            # 只有成功的选择才落盘，供守护进程重启后重放
            wanted = MODE_PROXY[body.mode] if body.proxy is None else bool(body.proxy)
            state.save(config.data_dir, body.mode, wanted, explicit=body.proxy is not None)
        time.sleep(2.5)
        snapshot = monitor.snapshot()
        return {
            "ok": result["ok"],
            "error": "" if result["ok"] else "部分步骤失败",
            "steps": result.get("steps", []),
            "mode": snapshot.get("mode", body.mode),
            "mode_label": snapshot.get("mode_label", ""),
            "proxy": snapshot.get("proxy", {}),
        }

    return app
