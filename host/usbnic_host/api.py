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
from .monitor.proxy import set_proxy
from .net.modes import MODES, apply_mode

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


def create_app(monitor: Monitor, config: Config) -> FastAPI:
    app = FastAPI(
        title="usbnic 上位机",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def no_store_api(request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
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

    @app.get("/api/config")
    def public_config() -> dict:
        return config.public_dict()

    @app.post("/api/proxy/toggle")
    def proxy_toggle(body: ProxyToggleRequest) -> dict:
        mode = monitor.snapshot().get("mode", "client")
        result = set_proxy(config, body.enable, mode)
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
