"""The loop pedal.

Hold a key, the camera keeps going out live while the ring buffer fills. Let go
and the recording plays on repeat. Press the other key and it dissolves back to
the live feed.

Two seams have to be hidden, and they are different problems. The loop's own
wrap-around is fixed once, when the loop is built, by blending the tail into the
head. The cut between live and loop is fixed every time it happens, by running a
dissolve over the transition.

Frames are held JPEG-encoded. At 720p that is roughly 2 MB per second of
recording, against about 80 MB raw.
"""

from __future__ import annotations

import threading
from collections import deque
from enum import Enum

import cv2
import numpy as np


class State(str, Enum):
    LIVE = "live"
    REC = "rec"
    LOOP = "loop"


class Looper:
    def __init__(self, fps: int = 30, quality: int = 90) -> None:
        self.fps = max(1, fps)
        self.quality = quality
        self._lock = threading.Lock()
        self._state = State.LIVE
        self._buffer: deque[bytes] = deque()
        self._loop: list[bytes] = []
        self._cursor = 0
        # Dissolve between live and loop: frames remaining, and which way.
        self._fade_left = 0
        self._fade_total = 0
        self._fade_to_loop = True
        self._last_live: np.ndarray | None = None

    # -- state ---------------------------------------------------------

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state.value,
                "recorded": len(self._buffer),
                "loop_frames": len(self._loop),
                "seconds": round(len(self._buffer) / self.fps, 2),
            }

    # -- controls ------------------------------------------------------

    def start_record(self, max_seconds: float) -> None:
        with self._lock:
            if self._state is State.REC:
                return
            self._buffer = deque(maxlen=max(1, int(max_seconds * self.fps)))
            self._state = State.REC

    def stop_record(self, min_seconds: float, crossfade: float) -> bool:
        """Build the loop and switch to it. False if too short to be one."""
        with self._lock:
            if self._state is not State.REC:
                return False
            frames = list(self._buffer)
            if len(frames) < max(2, int(min_seconds * self.fps)):
                self._state = State.LIVE
                return False
            self._loop = _seal(frames, int(crossfade * self.fps), self.quality)
            # Start a little before the seam, so the first thing seen is the
            # wrap-around already in progress rather than a clean first frame.
            self._cursor = max(0, len(self._loop) - int(crossfade * self.fps))
            self._state = State.LOOP
            self._begin_fade(int(crossfade * self.fps), to_loop=True)
            return True

    def go_live(self, crossfade: float) -> None:
        with self._lock:
            if self._state is State.LIVE:
                return
            if self._state is State.REC:
                self._state = State.LIVE
                return
            self._begin_fade(int(crossfade * self.fps), to_loop=False)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._loop = []
            self._cursor = 0
            self._fade_left = 0
            self._state = State.LIVE

    def _begin_fade(self, frames: int, to_loop: bool) -> None:
        self._fade_total = max(0, frames)
        self._fade_left = self._fade_total
        self._fade_to_loop = to_loop

    # -- per frame -----------------------------------------------------

    def process(self, live: np.ndarray) -> np.ndarray:
        """Feed the live frame in, get whatever should go out."""
        with self._lock:
            self._last_live = live

            if self._state is State.REC:
                self._buffer.append(_encode(live, self.quality))
                return live

            if self._state is State.LIVE and self._fade_left <= 0:
                return live

            loop_frame = self._advance_loop()
            if loop_frame is None:
                self._state = State.LIVE
                return live

            if self._fade_left > 0:
                self._fade_left -= 1
                done = 1.0 - (self._fade_left / max(1, self._fade_total))
                if self._fade_to_loop:
                    out = _blend(live, loop_frame, done)
                else:
                    out = _blend(loop_frame, live, done)
                    if self._fade_left <= 0:
                        self._state = State.LIVE
                return out

            return loop_frame

    def preview(self, out_frame: np.ndarray, overlay_alpha: float) -> np.ndarray:
        """What the operator sees: the loop ghosted over the live feed.

        Lets you line yourself up with the loop before dropping back to live, so
        the return does not jump. Preview only, never goes to the virtual camera.
        """
        with self._lock:
            if self._state is not State.LOOP or self._last_live is None:
                return out_frame
            if overlay_alpha <= 0.0:
                return out_frame
            return _blend(self._last_live, out_frame, overlay_alpha)

    def _advance_loop(self) -> np.ndarray | None:
        if not self._loop:
            return None
        raw = self._loop[self._cursor % len(self._loop)]
        self._cursor = (self._cursor + 1) % len(self._loop)
        return _decode(raw)


# -- frame helpers ------------------------------------------------------


def _encode(frame: np.ndarray, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


def _decode(raw: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)


def _blend(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    if b.shape != a.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]))
    return cv2.addWeighted(a, 1.0 - t, b, t, 0.0)


def _seal(frames: list[bytes], fade: int, quality: int) -> list[bytes]:
    """Blend the tail of the recording into its head so the wrap is invisible.

    The faded region is dropped from the end rather than kept, otherwise the
    loop would play the same moment twice: once blended, once clean.
    """
    fade = max(0, min(fade, len(frames) // 3))
    if fade == 0:
        return frames

    head = [_decode(f) for f in frames[:fade]]
    tail = [_decode(f) for f in frames[-fade:]]

    sealed: list[bytes] = []
    for i in range(fade):
        t = (i + 1) / (fade + 1)
        sealed.append(_encode(_blend(tail[i], head[i], t), quality))

    return sealed + frames[fade:-fade]
