"""Video chain: camera in, loop pedal, degradation, virtual camera out.

One thread owns the camera and the whole chain. The preview is a JPEG snapshot
this thread leaves behind for the web server to pick up, so a slow or absent
browser can never stall the feed going to the call.

The virtual camera is optional on purpose. Without the OBS driver registered
there is nowhere to send frames, and refusing to start would make the tool
untestable on a machine that has not been set up yet. It runs preview-only
instead and says so.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import cv2
import numpy as np

from .looper import Looper, State
from .videofx import VideoDegrader, overlay

log = logging.getLogger(__name__)

# Backends worth trying on Windows, in order. MSMF is the modern one and is
# what most integrated cameras answer on; DSHOW is the fallback for the rest.
_BACKENDS = [
    ("MSMF", getattr(cv2, "CAP_MSMF", 0)),
    ("DSHOW", getattr(cv2, "CAP_DSHOW", 0)),
    ("ANY", getattr(cv2, "CAP_ANY", 0)),
]


class VideoPipeline:
    def __init__(self, settings_store, link) -> None:
        self._store = settings_store
        self._link = link
        # One event per run, not one shared event. A shared one gets cleared by
        # the next start() before the previous thread has noticed it was asked
        # to stop, and that thread then runs forever, still holding the camera
        # and still writing preview frames over the new one's.
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._life = threading.Lock()

        self.looper = Looper()
        self._degrader = VideoDegrader()
        self._delay: deque[np.ndarray] = deque()

        self._preview_lock = threading.Lock()
        self._preview: bytes | None = None
        self._preview_seq = 0

        self._status_lock = threading.Lock()
        self._status = {
            "running": False,
            "camera": None,
            "backend": None,
            "virtual_camera": None,
            "fps": 0.0,
            "error": None,
        }

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        with self._life:
            if self._thread is not None and self._thread.is_alive():
                return
            stop = threading.Event()
            self._stop = stop
            self._thread = threading.Thread(
                target=self._run, args=(stop,), name="video", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._life:
            stop, thread = self._stop, self._thread
            self._stop, self._thread = None, None
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join(timeout=6.0)
            if thread.is_alive():
                # It is blocked in a camera read that never returned. Say so:
                # the next start() will open a second camera handle and the
                # only symptom otherwise is a preview that flickers between two
                # sources.
                log.error("video thread did not stop, camera read is stuck")

    def status(self) -> dict:
        with self._status_lock:
            out = dict(self._status)
        out["pedal"] = self.looper.status()
        return out

    def preview_jpeg(self) -> bytes | None:
        with self._preview_lock:
            return self._preview

    # -- the chain -----------------------------------------------------

    def _run(self, stop: threading.Event) -> None:
        cfg = self._store.get().video
        if cfg.source == "pattern":
            cap, backend = _TestPattern(cfg), "pattern"
        else:
            cap, backend = _open_camera(cfg)
        if cap is None:
            self._set_status(running=False, error=f"no camera at index {cfg.camera}")
            return

        self.looper.fps = cfg.fps
        cam = self._open_virtual_camera(cfg)
        self._set_status(
            running=True,
            camera=cfg.camera,
            backend=backend,
            virtual_camera=(cam.device if cam else None),
            error=None,
        )

        period = 1.0 / max(1, cfg.fps)
        next_due = time.monotonic()
        ticks, window_start = 0, time.monotonic()

        try:
            while not stop.is_set():
                settings = self._store.get()
                cfg = settings.video

                ok, frame = cap.read()
                if not ok or frame is None:
                    time.sleep(0.01)
                    continue
                if cfg.mirror:
                    frame = cv2.flip(frame, 1)

                snap = self._link.get()
                out = self.looper.process(frame)
                out = self._degrader.apply(out, snap, cfg)
                out = self._delayed(out, snap, cfg)

                if cam is not None:
                    try:
                        cam.send(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))
                    except Exception as exc:  # the driver can vanish mid-run
                        log.warning("virtual camera send failed: %s", exc)
                        cam = None
                        self._set_status(virtual_camera=None, error=f"virtual camera lost: {exc}")

                self._publish_preview(out, settings)

                ticks += 1
                now = time.monotonic()
                if now - window_start >= 1.0:
                    self._set_status(fps=round(ticks / (now - window_start), 1))
                    ticks, window_start = 0, now

                next_due += period
                sleep = next_due - time.monotonic()
                if sleep > 0:
                    stop.wait(sleep)
                else:
                    next_due = time.monotonic()  # fell behind, do not spiral
        except Exception as exc:
            # Without this the thread dies, the status still reads error: None,
            # and the only sign is that the preview stopped moving. The
            # traceback goes to the log and the reason goes to the UI.
            log.exception("video chain stopped: %s", exc)
            self._set_status(error=f"{type(exc).__name__}: {exc}")
        finally:
            cap.release()
            if cam is not None:
                cam.close()
            self._set_status(running=False, fps=0.0)

    def _delayed(self, frame: np.ndarray, snap, cfg) -> np.ndarray:
        """Hold frames back for the link latency, plus any negative desync."""
        extra = snap.latency + max(0.0, -snap.desync)
        depth = int(extra * max(1, cfg.fps))
        if depth <= 0:
            self._delay.clear()
            return frame
        self._delay.append(frame)
        while len(self._delay) > depth:
            return self._delay.popleft()
        return self._delay[0]

    def _publish_preview(self, out: np.ndarray, settings) -> None:
        shown = out
        if self.looper.state is State.LOOP and settings.pedal.ghost:
            shown = self.looper.preview(out, settings.pedal.overlay)
        shown = _annotate(shown, self.looper.state, self._link.get())
        ok, buf = cv2.imencode(".jpg", shown, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if not ok:
            return
        with self._preview_lock:
            self._preview = buf.tobytes()
            self._preview_seq += 1

    def _open_virtual_camera(self, cfg):
        try:
            import pyvirtualcam
        except Exception as exc:
            self._set_status(error=f"pyvirtualcam missing: {exc}")
            return None
        try:
            return pyvirtualcam.Camera(
                width=cfg.width, height=cfg.height, fps=cfg.fps, fmt=pyvirtualcam.PixelFormat.RGB
            )
        except Exception as exc:
            log.warning("virtual camera unavailable, preview only: %s", exc)
            self._set_status(error=f"virtual camera unavailable: {exc}")
            return None

    def _set_status(self, **fields) -> None:
        with self._status_lock:
            self._status.update(fields)


# -- helpers ------------------------------------------------------------


class _TestPattern:
    """Stands in for a camera. Same read/set/release surface as VideoCapture.

    The frame has to have real detail and real motion, otherwise the effects
    have nothing to destroy: a flat image stays flat through a JPEG crush, and
    a still one cannot show a dropped frame. Hence the noise field, the colour
    bars and the block that moves every frame.
    """

    def __init__(self, cfg) -> None:
        self.w, self.h = cfg.width, cfg.height
        self._n = 0
        rng = np.random.default_rng(1234)
        self._noise = rng.integers(0, 255, size=(self.h, self.w, 3), dtype=np.uint8)
        self._bars = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        colours = [(255, 255, 255), (0, 255, 255), (255, 255, 0), (0, 255, 0),
                   (255, 0, 255), (0, 0, 255), (255, 0, 0), (32, 32, 32)]
        band = max(1, self.w // len(colours))
        for i, colour in enumerate(colours):
            self._bars[:, i * band : (i + 1) * band] = colour

    def isOpened(self) -> bool:  # noqa: N802 - mirrors cv2.VideoCapture
        return True

    def set(self, *_args) -> bool:
        return True

    def release(self) -> None:
        return None

    def read(self):  # noqa: D401 - mirrors cv2.VideoCapture
        self._n += 1
        frame = cv2.addWeighted(self._bars, 0.75, self._noise, 0.25, 0)
        # The whole image scrolls, it is not just a block crossing a still
        # background. A local mover changes about one percent of the pixels, so
        # two consecutive live frames average out as nearly identical and
        # become indistinguishable from a dropped one.
        frame = np.roll(frame, (self._n * 9) % self.w, axis=1)
        y = int(self.h * 0.5) - 60
        cv2.rectangle(frame, (40, y), (160, y + 120), (0, 0, 0), -1)
        cv2.putText(frame, str(self._n), (48, y + 70),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 3, cv2.LINE_AA)
        return True, frame


def list_cameras() -> list[dict]:
    """Cameras that can be picked, by name, with the virtual one excluded.

    Offering our own output as an input would let someone feed the tool back
    into itself, which produces a recursive picture and holds the device open
    against the write we are about to make.
    """
    try:
        from pygrabber.dshow_graph import FilterGraph

        names = FilterGraph().get_input_devices()
    except Exception as exc:
        log.warning("could not enumerate cameras: %s", exc)
        return []

    out = []
    for index, name in enumerate(names):
        if any(bad in name.lower() for bad in ("obs virtual camera", "unitycapture")):
            continue
        out.append({"index": index, "name": name})
    return out


def _open_camera(cfg):
    wanted = (cfg.backend or "auto").lower()
    backends = _BACKENDS if wanted == "auto" else [
        (n, a) for n, a in _BACKENDS if n.lower() == wanted
    ]
    if not backends:
        log.warning("unknown capture backend %r, falling back to auto", cfg.backend)
        backends = _BACKENDS

    for name, api in backends:
        cap = cv2.VideoCapture(cfg.camera, api)
        if not cap.isOpened():
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        cap.set(cv2.CAP_PROP_FPS, cfg.fps)
        ok, frame = cap.read()
        if ok and frame is not None:
            log.info("camera %s open on %s at %sx%s", cfg.camera, name, *frame.shape[1::-1])
            return cap, name
        cap.release()
    return None, None


def _annotate(frame: np.ndarray, state: State, snap) -> np.ndarray:
    """Status line for the preview. Never reaches the virtual camera."""
    out = frame.copy()
    label = state.value.upper()
    colour = {"live": (120, 220, 120), "rec": (80, 80, 255), "loop": (255, 200, 90)}[state.value]
    cv2.putText(out, label, (16, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.8, colour, 2, cv2.LINE_AA)
    if snap.enabled:
        text = "STALL" if snap.stalled else f"link {snap.quality:.0f}"
        cv2.putText(
            out,
            text,
            (16, 64),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (80, 80, 255) if snap.stalled else (200, 200, 200),
            2,
            cv2.LINE_AA,
        )
    return out
