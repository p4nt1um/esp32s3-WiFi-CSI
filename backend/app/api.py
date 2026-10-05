"""REST API。"""
from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .config import settings
from .db import Database
from .mqtt_client import MqttBridge


def create_app() -> FastAPI:
    app = FastAPI(title="CSI Home Sensing Backend", version="0.1.0")
    db = Database(settings.db_path)
    bridge = MqttBridge(db)
    app.state.db = db
    app.state.mqtt = bridge

    @app.on_event("startup")
    def _start():
        bridge.start()

    @app.on_event("shutdown")
    def _stop():
        bridge.stop()
        db.close()

    @app.get("/api/health")
    def health():
        return {"status": "ok", "broker_connected": bridge.connected,
                "db": {"breath": db.count("breath"), "stat": db.count("stat")},
                "now": int(time.time())}

    @app.get("/api/devices")
    def devices():
        seen = {d["device"]: d for d in bridge.device_status()}
        out = []
        for device in db.devices():
            latest = db.latest_breath(device) or {}
            s = seen.get(device, {})
            out.append({"device": device,
                        "last_msg_ms": s.get("last_msg_ms"),
                        "last_bpm": latest.get("bpm"),
                        "last_valid": latest.get("valid"),
                        "last_ts": latest.get("ts")})
        return out

    @app.get("/api/breath/latest")
    def breath_latest(device: str = Query(..., min_length=1)):
        row = db.latest_breath(device)
        if not row:
            raise HTTPException(404, f"no data for device {device}")
        return row

    @app.get("/api/breath/history")
    def breath_history(device: str = Query(..., min_length=1),
                       hours: float = Query(1.0, gt=0, le=24 * 30),
                       limit: int = Query(2000, gt=0),
                       valid_only: bool = False):
        return db.history(device, hours, min(limit, settings.history_limit_max), valid_only)

    @app.get("/api/amp/waterfall")
    def amp_waterfall(device: str = Query(..., min_length=1),
                      seconds: float = Query(30.0, gt=0, le=60)):
        eng = bridge._engines.get(device)
        if not eng:
            raise HTTPException(404, f"no amp data for {device}")
        return eng.snapshot(seconds)

    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(__file__.replace("api.py", "static/index.html"), encoding="utf-8") as f:
            return HTMLResponse(f.read())

    return app
