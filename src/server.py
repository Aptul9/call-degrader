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
import io
import logging
import sys
import wave
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .app import Controller

log = logging.getLogger(__name__)

def _assets_dir() -> Path:
    """Where icon.ico lives. Same bundle question as the ui folder."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "assets"
    return Path(__file__).resolve().parent.parent / "assets"


def _ui_dir() -> Path:
    """Where index.html, app.js and style.css actually are.

    Frozen by PyInstaller the source tree does not exist: the bundle is
    unpacked to a temp directory and `sys._MEIPASS` points at it. Deriving the
    path from `__file__` happens to work there too, but only by accident of
    how the modules are laid out, and it breaks the moment the bundle is
    arranged differently.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "ui"
    return Path(__file__).resolve().parent.parent / "ui"


UI_DIR = _ui_dir()
ASSETS_DIR = _assets_dir()
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

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        # The browser asks for this whether or not it is offered, and a 404 in
        # the console of a dev UI is noise someone eventually chases.
        icon = ASSETS_DIR / "icon.ico"
        if not icon.exists():
            raise HTTPException(status_code=404, detail="no icon")
        return FileResponse(icon, media_type="image/x-icon")

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
        return controller.settings_reply(controller.patch(payload))

    @app.post("/api/preset/{name}")
    def preset(name: str):
        try:
            return controller.settings_reply(controller.apply_preset(name))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no preset {name!r}")

    @app.post("/api/audio-preset/{name}")
    def audio_preset(name: str):
        try:
            return controller.settings_reply(controller.apply_audio_preset(name))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no audio preset {name!r}")

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

    # -- audio A/B ------------------------------------------------------

    @app.post("/api/audio-test/start")
    def audio_test_start(payload: dict | None = None):
        raw = (payload or {}).get("seconds")
        # No duration means hold-to-record: capture until the button comes up.
        seconds = None if raw is None else max(1.0, min(30.0, float(raw)))
        state = controller.audio.status()
        if not state["running"]:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "the audio chain is not running"})
        # Recording a released microphone gives two identical silent takes,
        # which is the exact confusion the peak readout exists to prevent.
        if state.get("paused"):
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "the microphone is paused, nothing to record"})
        started = controller.audio.start_capture(seconds)
        return {"ok": True, "link_on": controller.settings.get().link.enabled, **started}

    @app.post("/api/audio-test/stop")
    def audio_test_stop():
        return {"ok": True, **controller.audio.stop_capture()}

    @app.get("/api/audio-test/status")
    def audio_test_status():
        return controller.audio.capture_status()

    @app.get("/api/audio-test/{side}.wav")
    def audio_test_wav(side: str):
        if side not in ("before", "after"):
            raise HTTPException(status_code=404, detail="side must be before or after")
        got = controller.audio.capture_audio(side)
        if got is None:
            raise HTTPException(status_code=409, detail="no finished recording")
        samples, rate = got
        return Response(
            content=_wav_bytes(samples, rate),
            media_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )

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


def _wav_bytes(samples: np.ndarray, rate: int) -> bytes:
    """Mono 16-bit WAV, which every browser plays without a codec question.

    Clipped rather than normalised: the whole point is to hear what the far end
    hears, and normalising would quietly undo the level changes the degradation
    made.
    """
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm.tobytes())
    return buffer.getvalue()


async def _frames(controller: Controller):
    """MJPEG parts, at most one per preview refresh.

    Counted as a viewer for as long as the response is open, because the video
    thread only encodes a preview while something is reading one.
    """
    with controller.video.viewer():
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
