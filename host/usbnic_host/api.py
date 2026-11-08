from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Config
from .monitor.collector import Monitor

WEB_DIR = Path(__file__).parent / "web"


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
            "mode": snapshot.get("mode", "monitor"),
            "readonly": True,
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

    return app
