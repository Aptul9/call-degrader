"""Local web server: static UI, control API, preview stream.

Bound to 127.0.0.1 by default. Pass --host 0.0.0.0 to drive it from a phone on
the same network while the call runs on the laptop, which is the one thing a
browser UI gives you that a native window does not.

The preview is MJPEG rather than frames over the socket. An <img> tag decodes
it without a line of JavaScript, and a viewer that stops reading only stalls its
own response, never the chain feeding the call.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .app import Controller

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
BOUNDARY = "frame"


def create_app(controller: Controller) -> FastAPI:
    app = FastAPI(title="call-degrader", docs_url=None, redoc_url=None)
    app.state.controller = controller

    @app.on_event("shutdown")
    def _shutdown() -> None:
        controller.stop()

    # -- UI ------------------------------------------------------------

    # The UI is edited while the tool is running, and a browser that keeps its
    # cached copy shows an old page with no sign that it is doing so. A new
    # control simply does not appear and it looks like the feature was never
    # built. Nothing here is worth caching: it is all served from localhost.
    class NoCacheStatic(StaticFiles):
        def file_response(self, *args, **kwargs):
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            return response

    app.mount("/static", NoCacheStatic(directory=UI_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(
            UI_DIR / "index.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    # -- state ---------------------------------------------------------

    @app.get("/api/state")
    def state():
        return controller.full_state()

    @app.get("/api/devices")
    def devices():
        return controller.devices()

    @app.get("/api/cameras")
    def cameras():
        return {"cameras": controller.cameras()}

    @app.post("/api/settings")
    async def settings(payload: dict):
        return {"settings": controller.patch(payload).to_dict()}

    @app.post("/api/preset/{name}")
    def preset(name: str):
        try:
            return {"settings": controller.apply_preset(name).to_dict()}
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no preset {name!r}")

    # -- pedal ---------------------------------------------------------

    @app.post("/api/pedal/{action}")
    def pedal(action: str):
        actions = {
            "record-start": controller.record_start,
            "record-stop": controller.record_stop,
            "live": controller.go_live,
            "clear": controller.clear_loop,
        }
        fn = actions.get(action)
        if fn is None:
            raise HTTPException(status_code=404, detail=f"no pedal action {action!r}")
        result = fn()
        return {"ok": True, "result": result, "pedal": controller.video.looper.status()}

    # -- microphone routing --------------------------------------------

    @app.post("/api/route-microphone")
    def route_microphone(payload: dict | None = None):
        match = (payload or {}).get("match", "CABLE Output")
        try:
            return {"ok": True, "device": controller.route_microphone(match)}
        except Exception as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    # -- preview -------------------------------------------------------

    @app.get("/preview.mjpg")
    def preview():
        return StreamingResponse(
            _frames(controller),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={"Cache-Control": "no-store"},
        )

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        try:
            while True:
                await socket.send_json(controller.status())
                await asyncio.sleep(0.2)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.debug("websocket closed: %s", exc)

    return app


async def _frames(controller: Controller):
    """MJPEG parts, at most one per preview refresh."""
    last = None
    while True:
        jpeg = controller.video.preview_jpeg()
        if jpeg is not None and jpeg is not last:
            last = jpeg
            yield (
                f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                + jpeg
                + b"\r\n"
            )
            await asyncio.sleep(1 / 30)
        else:
            await asyncio.sleep(0.02)
