"""Video degradation.

Every effect takes the link severity, 0 for a perfect line and 1 for an
unusable one, and a weight from the settings that scales how hard it reacts.
Weight 0 turns the effect off.

The effects that matter most are the two that reproduce what a real codec does
when it runs out of bitrate: a JPEG round trip at low quality, which produces
genuine DCT blocking rather than a mosaic, and block displacement during a
stall, which is what makes a frozen picture smear instead of simply hold.
"""

from __future__ import annotations

import random

import cv2
import numpy as np

BLOCK = 16  # macroblock edge in pixels, matches what H.264 would use


def _curve(severity: float, weight: float, knee: float = 0.35) -> float:
    """Map severity to effect strength.

    Nothing happens while the line is good, then it ramps. Without the knee
    every effect is faintly on all the time, which reads as a bad camera rather
    than a bad connection.
    """
    if weight <= 0.0 or severity <= knee:
        return 0.0
    return min(1.0, ((severity - knee) / (1.0 - knee)) * weight)


class VideoDegrader:
    """Stateful because smear, drop and tearing all need the previous frame."""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._last: np.ndarray | None = None
        self._offsets: np.ndarray | None = None
        self._offset_shape: tuple[int, int] | None = None
        # The picture the stall froze on. Every tick of the smear is applied to
        # this, never to the previous smeared result.
        self._frozen: np.ndarray | None = None

    def reset(self) -> None:
        self._last = None
        self._offsets = None
        self._frozen = None

    def apply(self, frame: np.ndarray, link, video_cfg) -> np.ndarray:
        """Return the degraded frame. `frame` is BGR uint8 and is not mutated."""
        if not link.enabled:
            self._last = frame
            return frame

        sev = link.severity

        if link.stalled:
            out = self._stall(frame, video_cfg)
            self._last = out
            return out

        # Out of the stall: drop the frozen reference and let the offsets relax
        # so the next stall starts from a clean picture.
        if self._frozen is not None:
            self._frozen = None
            self._offsets = None

        if self._drops(sev, video_cfg.drop_weight) and self._last is not None:
            # A dropped frame is the previous one shown again, not a black one.
            self._last = _decay(self._last)
            return self._last

        out = frame
        out = self._resolution(out, sev, video_cfg.resolution_weight)
        out = self._blockiness(out, sev, video_cfg.blockiness_weight)
        out = self._banding(out, sev, video_cfg.banding_weight)
        out = self._tearing(out, sev, video_cfg.tearing_weight)

        self._last = out
        return out

    # -- individual effects --------------------------------------------

    def _drops(self, sev: float, weight: float) -> bool:
        strength = _curve(sev, weight, knee=0.2)
        if strength <= 0.0:
            return False
        # Up to 8 frames in 10 lost at the bottom of the scale.
        return self._rng.random() < strength * 0.8

    def _resolution(self, frame: np.ndarray, sev: float, weight: float) -> np.ndarray:
        strength = _curve(sev, weight, knee=0.3)
        if strength <= 0.0:
            return frame
        h, w = frame.shape[:2]
        # Down to a sixth of the linear resolution at the worst.
        factor = 1.0 - strength * 0.84
        sw, sh = max(32, int(w * factor)), max(18, int(h * factor))
        small = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_AREA)
        return cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)

    def _blockiness(self, frame: np.ndarray, sev: float, weight: float) -> np.ndarray:
        strength = _curve(sev, weight, knee=0.15)
        if strength <= 0.0:
            return frame
        quality = int(round(92 - strength * 90))  # 92 down to 2
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), max(1, quality)])
        if not ok:
            return frame
        decoded = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        return frame if decoded is None else decoded

    def _banding(self, frame: np.ndarray, sev: float, weight: float) -> np.ndarray:
        strength = _curve(sev, weight, knee=0.45)
        if strength <= 0.0:
            return frame
        bits = int(round(8 - strength * 5))  # 8 bits down to 3
        if bits >= 8:
            return frame
        step = 1 << (8 - bits)
        # A lookup table rather than masking: it re-centres each band, so
        # quantising does not also darken the picture, and it cannot overflow
        # the way uint8 arithmetic on the frame itself can.
        table = np.arange(256, dtype=np.uint16)
        table = np.clip((table // step) * step + step // 2, 0, 255).astype(np.uint8)
        return cv2.LUT(frame, table)

    def _tearing(self, frame: np.ndarray, sev: float, weight: float) -> np.ndarray:
        strength = _curve(sev, weight, knee=0.55)
        if strength <= 0.0 or self._last is None:
            return frame
        if self._rng.random() > strength * 0.4:
            return frame
        if self._last.shape != frame.shape:
            return frame
        h = frame.shape[0]
        seam = self._rng.randint(h // 8, h - h // 8)
        out = frame.copy()
        out[seam:] = self._last[seam:]
        return out

    def _stall(self, frame: np.ndarray, video_cfg) -> np.ndarray:
        """A frozen picture that drags, which is what a real stall looks like.

        The picture the stall landed on is kept, and the accumulated block
        offsets are applied to *that* every tick. Applying them to the previous
        smeared output instead compounds the resampling, and a fifth of a
        second of freeze is already unrecognisable, which no real call does.
        """
        if self._frozen is None:
            self._frozen = self._last if self._last is not None else frame
            self._offsets = None
        strength = _curve(1.0, video_cfg.smear_weight, knee=0.0)
        if strength <= 0.0:
            return self._frozen
        return self._smear(self._frozen, strength)

    def _smear(self, frame: np.ndarray, strength: float) -> np.ndarray:
        h, w = frame.shape[:2]
        bh, bw = max(1, h // BLOCK), max(1, w // BLOCK)

        if self._offsets is None or self._offset_shape != (bh, bw):
            self._offsets = np.zeros((bh, bw, 2), dtype=np.float32)
            self._offset_shape = (bh, bw)

        # Each stall tick nudges the block offsets a little further, so the
        # drag grows over the length of the freeze rather than jumping. The
        # offsets are a displacement from the frozen frame, not a step on top
        # of the last one, so they can accumulate without destroying the image.
        kick = np.random.default_rng(self._rng.getrandbits(32)).normal(
            0.0, 1.1 * strength, size=(bh, bw, 2)
        )
        self._offsets = np.clip(
            self._offsets * 0.97 + kick, -BLOCK * 2.0, BLOCK * 2.0
        ).astype(np.float32)

        full = cv2.resize(self._offsets, (w, h), interpolation=cv2.INTER_NEAREST)
        grid_x, grid_y = np.meshgrid(
            np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32)
        )
        map_x = np.clip(grid_x + full[..., 0], 0, w - 1)
        map_y = np.clip(grid_y + full[..., 1], 0, h - 1)
        return cv2.remap(
            frame, map_x, map_y, interpolation=cv2.INTER_NEAREST, borderMode=cv2.BORDER_REPLICATE
        )


def _decay(frame: np.ndarray) -> np.ndarray:
    """A held frame loses a touch of contrast each time it is repeated.

    Barely visible on one repeat, clearly wrong after a dozen, which is the
    tell that the picture is stuck rather than the subject being still.
    """
    return cv2.convertScaleAbs(frame, alpha=0.995, beta=0.6)


def overlay(base: np.ndarray, top: np.ndarray, alpha: float) -> np.ndarray:
    """Ghost one frame over another. Preview only."""
    if top.shape != base.shape:
        top = cv2.resize(top, (base.shape[1], base.shape[0]))
    return cv2.addWeighted(base, 1.0 - alpha, top, alpha, 0.0)
