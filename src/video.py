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
from contextlib import contextmanager

import cv2
import numpy as np

from .looper import Looper, State
from .videofx import VideoDegrader, overlay

log = logging.getLogger(__name__)

# OpenCV splits every call across all eight cores by default and its pool
# spins between calls, which on 720p frames buys nothing worth the price.
# Measured on the camera path with nothing else changed: the whole app at
# 28.1 percent of a core on the default pool, 15.9 on two threads and 14.2 on
# one. The effects are the calls that do use the extra threads, 6.7 to 9.5 ms
# a frame on eight, 7.8 to 11.2 on two, 11.6 to 14.0 on one, so two it is.
cv2.setNumThreads(2)

# Backends worth trying on Windows, in order. MSMF is the modern one and is
# what most integrated cameras answer on; DSHOW is the fallback for the rest.
_BACKENDS = [
    ("MSMF", getattr(cv2, "CAP_MSMF", 0)),
    ("DSHOW", getattr(cv2, "CAP_DSHOW", 0)),
    ("ANY", getattr(cv2, "CAP_ANY", 0)),
]

# The preview is a self-view for whoever is at the keyboard, not what the call
# gets, so it is encoded smaller and less often than the feed. At full size on
# every frame it took 7.9 ms of the 33 ms budget on the thread feeding the
# call, measured at 720p, and it took it with nothing watching: a window
# hidden to the tray kept its stream open and WebView2 kept decoding it, 29
# percent of a core for a picture nobody could see.
PREVIEW_WIDTH = 640
PREVIEW_FPS = 15


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
        self._viewers = 0
        self._preview_paused = False
        self._preview_due = 0.0

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
        with self._preview_lock:
            out["preview_viewers"] = self._viewers
        return out

    def preview_jpeg(self) -> bytes | None:
        with self._preview_lock:
            return self._preview

    @contextmanager
    def viewer(self):
        """Keep the preview encoded for as long as the block runs."""
        with self._preview_lock:
            self._viewers += 1
        try:
            yield
        finally:
            with self._preview_lock:
                self._viewers -= 1

    def pause_preview(self, paused: bool) -> None:
        """Stop encoding while the window showing the preview is out of sight.

        The viewer count cannot tell: a window hidden to the tray keeps its
        stream open, and to WebView2 the page is still visible.
        """
        with self._preview_lock:
            self._preview_paused = paused

    # -- the chain -----------------------------------------------------

    def _run(self, stop: threading.Event) -> None:
        cfg = self._store.get().video
        if cfg.paused:
            cap, backend = _Paused(cfg), "paused"
        elif cfg.source == "pattern":
            cap, backend = _TestPattern(cfg), "pattern"
        else:
            cap, backend = _open_camera(cfg)
        if cap is None:
            self._set_status(running=False, error=f"no camera at index {cfg.camera}")
            return

        self.looper.fps = cfg.fps
        cam, to_camera = self._open_virtual_camera(cfg)
        self._set_status(
            running=True,
            camera=cfg.camera,
            backend=backend,
            virtual_camera=(cam.device if cam else None),
            error=None,
        )

        # Mirroring is for a lens pointed at you. A generated frame has no
        # handedness, so flipping one only reverses the text written on it,
        # and the preview flip further down skips it.
        generated = backend in ("paused", "pattern")

        # A camera paces the loop by itself: read() blocks until the next frame
        # is there. A timer of our own on top of it was a second clock, and it
        # cost twice. When the two drifted apart the loop waited on both, a 21
        # ms sleep followed by a 29 ms wait for the frame the sleep had just
        # missed going out as a 62 ms gap. When the loop ran late, frames queued
        # behind the timer and stayed queued: 57 to 110 ms old when read, run to
        # run, against 43 ms with the camera setting the pace. What the camera
        # does not do is hand frames over evenly, so they go out on its own
        # clock instead, see `_Playout`. Only a generated source needs a timer,
        # and it gets time.sleep, because Event.wait rounds up to the 15.6 ms
        # system tick unless something in the process has raised the timer
        # resolution.
        playout = None if generated else _Playout()
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
                arrived = time.perf_counter()
                snap = self._link.get()
                out = self.looper.process(frame)
                out = self._degrader.apply(out, snap, cfg)
                out = self._delayed(out, snap, cfg)

                if playout is not None:
                    stamp = cap.get(cv2.CAP_PROP_POS_MSEC)
                    if stamp > 0:
                        # Bounded, so a camera whose clock jumps costs one frame
                        # of waiting rather than a frozen feed.
                        wait = playout.due(stamp / 1000.0, arrived) - time.perf_counter()
                        if wait > 0:
                            time.sleep(min(wait, period))

                if cam is not None:
                    try:
                        cam.send(cv2.cvtColor(out, to_camera))
                    except Exception as exc:  # the driver can vanish mid-run
                        log.warning("virtual camera send failed: %s", exc)
                        cam = None
                        self._set_status(virtual_camera=None, error=f"virtual camera lost: {exc}")

                if self._preview_wanted():
                    self._publish_preview(out, settings, generated)

                ticks += 1
                now = time.monotonic()
                if now - window_start >= 1.0:
                    self._set_status(fps=round(ticks / (now - window_start), 1))
                    ticks, window_start = 0, now

                if generated:
                    next_due += period
                    sleep = next_due - time.monotonic()
                    if sleep > 0:
                        time.sleep(sleep)
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

    def _preview_wanted(self) -> bool:
        """True when something is watching and the next preview frame is due."""
        now = time.monotonic()
        with self._preview_lock:
            if self._viewers <= 0 or self._preview_paused or now < self._preview_due:
                return False
        # A little under the interval, so a 30 fps chain lands on every other
        # frame instead of beating against the preview rate.
        self._preview_due = now + 0.8 / PREVIEW_FPS
        return True

    def _publish_preview(self, out: np.ndarray, settings, generated: bool) -> None:
        shown = out
        if self.looper.state is State.LOOP and settings.pedal.ghost:
            shown = self.looper.preview(out, settings.pedal.overlay)
        h, w = shown.shape[:2]
        if w > PREVIEW_WIDTH:
            shown = cv2.resize(
                shown, (PREVIEW_WIDTH, round(h * PREVIEW_WIDTH / w)), interpolation=cv2.INTER_AREA
            )
        # Before the annotation, or the status line comes out back to front.
        if settings.video.mirror and not generated:
            shown = cv2.flip(shown, 1)
        shown = _annotate(shown, self.looper.state, self._link.get())
        ok, buf = cv2.imencode(".jpg", shown, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if not ok:
            return
        with self._preview_lock:
            self._preview = buf.tobytes()
            self._preview_seq += 1

    def _open_virtual_camera(self, cfg):
        """The virtual camera and the conversion that feeds it, or (None, None)."""
        try:
            import pyvirtualcam
        except Exception as exc:
            self._set_status(error=f"pyvirtualcam missing: {exc}")
            return None, None
        fmt, code = _camera_pixels(cfg.width, cfg.height)
        try:
            cam = pyvirtualcam.Camera(
                width=cfg.width, height=cfg.height, fps=cfg.fps,
                fmt=getattr(pyvirtualcam.PixelFormat, fmt),
            )
        except Exception as exc:
            log.warning("virtual camera unavailable, preview only: %s", exc)
            self._set_status(error=f"virtual camera unavailable: {exc}")
            return None, None
        return cam, code

    def _set_status(self, **fields) -> None:
        with self._status_lock:
            self._status.update(fields)


# -- helpers ------------------------------------------------------------


def _camera_pixels(width: int, height: int) -> tuple[str, int]:
    """The pyvirtualcam pixel format to open with, and the cv2 conversion to it.

    I420 rather than RGB. pyvirtualcam turns whatever it is handed into the
    NV12 the OBS driver takes, and it does that holding the GIL: 5.5 ms a
    frame from RGB, measured at 720p, during which neither audio callback can
    run. OpenCV's I420 conversion releases the GIL and leaves pyvirtualcam a
    copy of the planes, 0.27 ms. I420 needs both sides even, so an odd size
    keeps RGB rather than refusing to start.
    """
    if width % 2 == 0 and height % 2 == 0:
        return "I420", cv2.COLOR_BGR2YUV_I420
    return "RGB", cv2.COLOR_BGR2RGB


class _Playout:
    """When a camera frame goes out: on the camera's clock, a steady delay behind it.

    The camera stamps its frames exactly 33.3 ms apart and hands them over
    anything from 16 to 49 ms apart (spacing p50 32, p95 49 ms, measured on
    MSMF). Sent the moment they arrived, that unevenness went straight to the
    far end, 6 to 16 gaps over 70 ms in 90 s where a steadier feed had 0 to
    5. So each frame waits until its capture time plus the transit that 95 in
    100 of the last two seconds of frames made within. That is the delay the
    call pays, 60 ms measured on every run against 60 to 112 on the timer
    this replaced, and the far end got 0 to 4 such gaps. A frame later than
    that goes at once, so a backlog drains instead of settling in.

    The stamp is `CAP_PROP_POS_MSEC`, which MSMF fills with the capture time.
    Its clock does not have to be ours: the offset between the two rides
    inside the transit.
    """

    WINDOW = 60  # frames, two seconds at 30 fps

    def __init__(self) -> None:
        self._transit: deque[float] = deque(maxlen=self.WINDOW)

    def due(self, stamp: float, arrived: float) -> float:
        """perf_counter time the frame stamped `stamp` should go out at."""
        self._transit.append(arrived - stamp)
        ranked = sorted(self._transit)
        return stamp + ranked[int(0.95 * (len(ranked) - 1))]


class _Paused:
    """What the call sees while the lens is released.

    The point of pausing is the camera light going out, so the physical device
    is never opened at all. The virtual camera stays up and keeps being fed,
    because a call already in progress reads a device that stops delivering as
    a camera that broke rather than as one that was turned off, and some
    clients then drop it for the rest of the call.

    A dark card, not black: a pure black feed is what a covered lens and a
    dead driver both look like.
    """

    def __init__(self, cfg) -> None:
        self.w, self.h = cfg.width, cfg.height
        self._frame = np.full((self.h, self.w, 3), 18, dtype=np.uint8)
        cv2.rectangle(self._frame, (0, 0), (self.w - 1, self.h - 1), (46, 40, 34), 2)
        text = "camera paused"
        scale = max(0.8, self.w / 900.0)
        size, _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        cv2.putText(
            self._frame, text,
            ((self.w - size[0]) // 2, (self.h + size[1]) // 2),
            cv2.FONT_HERSHEY_SIMPLEX, scale, (150, 160, 175), 2, cv2.LINE_AA,
        )

    def isOpened(self) -> bool:  # noqa: N802 - mirrors cv2.VideoCapture
        return True

    def set(self, *_args) -> bool:
        return True

    def release(self) -> None:
        return None

    def read(self):  # noqa: D401 - mirrors cv2.VideoCapture
        return True, self._frame.copy()


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
    # Laid out for a 1280-wide frame and scaled with it, with a floor so the
    # smaller preview keeps a line that can still be read.
    k = max(0.6, out.shape[1] / 1280.0)
    weight = max(1, round(2 * k))
    label = state.value.upper()
    colour = {"live": (120, 220, 120), "rec": (80, 80, 255), "loop": (255, 200, 90)}[state.value]
    cv2.putText(out, label, (round(16 * k), round(34 * k)), cv2.FONT_HERSHEY_SIMPLEX,
                0.8 * k, colour, weight, cv2.LINE_AA)
    if snap.enabled:
        text = "STALL" if snap.stalled else f"link {snap.quality:.0f}"
        cv2.putText(
            out,
            text,
            (round(16 * k), round(64 * k)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6 * k,
            (80, 80, 255) if snap.stalled else (200, 200, 200),
            weight,
            cv2.LINE_AA,
        )
    return out
