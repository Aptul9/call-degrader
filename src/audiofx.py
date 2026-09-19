"""Audio degradation.

Same contract as the video side: severity in, weighted effects out. Everything
works on one float32 block at a time, mono, typically 10 ms.

The one that does the heavy lifting is the stutter. Real voice codecs conceal a
lost packet by repeating the last one they received, and that repetition is the
sound everybody recognises as a bad line. Plain silence reads as a muted
microphone instead.

Every gain change is ramped across the block. An abrupt one clicks, and a click
sounds like broken software rather than a broken connection.
"""

from __future__ import annotations

import random

import numpy as np


def _curve(severity: float, weight: float, knee: float = 0.35) -> float:
    """Map severity to effect strength.

    Squared, not linear. This is a camera feed going down a video call: the
    picture is meant to look like a connection having a hard time, not like a
    fault. A linear ramp meant the middle of the scale was already destroying
    the image, so every preset short of the worst one looked the same and
    looked wrong. Squaring leaves the top of the range where it was and pulls
    the middle right down: at severity 0.55 the strength drops from 0.36 to
    0.13, while at 0.95 it barely moves, 0.93 to 0.86.
    """
    if weight <= 0.0 or severity <= knee:
        return 0.0
    ramp = (severity - knee) / (1.0 - knee)
    return min(1.0, ramp * ramp * weight)


class AudioDegrader:
    """Stateful: concealment, the comb delay line and the ramp all carry over."""

    # Longest comb delay, in samples at 48 kHz. Sized once, sliced per call.
    MAX_DELAY = 2048

    def __init__(self, samplerate: int = 48000, seed: int | None = None) -> None:
        self.samplerate = samplerate
        self._rng = random.Random(seed)
        self._conceal: np.ndarray | None = None  # last block actually received
        self._repeats = 0
        self._delay = np.zeros(self.MAX_DELAY, dtype=np.float32)
        self._gain = 1.0
        self._phase = 0.0

    def reset(self) -> None:
        self._conceal = None
        self._repeats = 0
        self._delay[:] = 0.0
        self._gain = 1.0
        self._phase = 0.0

    def apply(self, block: np.ndarray, link, audio_cfg) -> np.ndarray:
        """Return a degraded copy of `block`, float32 mono in [-1, 1]."""
        block = np.asarray(block, dtype=np.float32).reshape(-1)

        if not link.enabled:
            self._conceal = block
            self._repeats = 0
            return self._ramp_to(block, 1.0)

        sev = link.severity

        if link.stalled:
            return self._stalled(block, link, audio_cfg)

        if self._lost(sev, audio_cfg.stutter_weight):
            out = self._concealed(len(block), audio_cfg)
        else:
            self._conceal = block
            self._repeats = 0
            out = block

        out = self._bitcrush(out, sev, audio_cfg.bitcrush_weight)
        out = self._warble(out, sev, audio_cfg.warble_weight)
        out = self._metallic(out, sev, audio_cfg.metallic_weight)
        out = self._ramp_to(out, 1.0)
        return np.clip(out, -1.0, 1.0, out=out)

    # -- individual effects --------------------------------------------

    def _lost(self, sev: float, weight: float) -> bool:
        strength = _curve(sev, weight, knee=0.2)
        if strength <= 0.0:
            return False
        return self._rng.random() < strength * 0.3

    def _concealed(self, n: int, audio_cfg) -> np.ndarray:
        """Repeat the last received block, quieter each time it repeats.

        A codec that keeps repeating forever sounds like a stuck record, so the
        repeat fades out over roughly a dozen blocks and lands on silence.
        """
        if self._conceal is None or len(self._conceal) != n:
            return np.zeros(n, dtype=np.float32)
        self._repeats += 1
        fade = max(0.0, 1.0 - self._repeats / 12.0)
        out = self._conceal * np.float32(fade)
        if self._repeats % 2 == 0:
            # Alternate the polarity so a long run buzzes instead of humming a
            # clean tone at the block rate.
            out = -out
        return out

    def _bitcrush(self, block: np.ndarray, sev: float, weight: float) -> np.ndarray:
        strength = _curve(sev, weight, knee=0.3)
        if strength <= 0.0:
            return block
        bits = max(5.0, 16.0 - strength * 9.0)
        steps = np.float32(2.0 ** (bits - 1))
        crushed = np.round(block * steps) / steps

        # Sample and hold, which is what a dropped sample rate really sounds
        # like. Down to a sixth of the rate at the worst.
        hold = int(1 + round(strength * 2))
        if hold > 1:
            n = len(crushed)
            trimmed = (n // hold) * hold
            if trimmed:
                held = np.repeat(crushed[:trimmed:hold], hold)
                crushed = np.concatenate([held, crushed[trimmed:]])
        return crushed.astype(np.float32)

    def _warble(self, block: np.ndarray, sev: float, weight: float) -> np.ndarray:
        """Slow pitch wobble from a jitter buffer resampling to keep up."""
        strength = _curve(sev, weight, knee=0.4)
        if strength <= 0.0:
            return block
        n = len(block)
        depth = strength * 0.025  # up to 2.5 percent rate error
        rate = 2.0 * np.pi * 3.5 / self.samplerate  # 3.5 Hz wobble
        idx = np.arange(n, dtype=np.float32)
        offset = np.sin(self._phase + idx * rate).astype(np.float32) * (depth * n * 0.5)
        self._phase = float((self._phase + n * rate) % (2.0 * np.pi))
        src = np.clip(idx + offset, 0, n - 1)
        return np.interp(src, idx, block).astype(np.float32)

    def _metallic(self, block: np.ndarray, sev: float, weight: float) -> np.ndarray:
        """Feed-forward comb. Short delay, so it rings rather than echoes."""
        strength = _curve(sev, weight, knee=0.5)
        if strength <= 0.0:
            self._delay[:] = 0.0
            return block
        n = len(block)
        delay = min(self.MAX_DELAY - 1, max(16, int(self.samplerate * 0.004)))
        history = np.concatenate([self._delay[-delay:], block])
        self._delay = history[-self.MAX_DELAY :].astype(np.float32)
        return (block + history[:n] * np.float32(strength * 0.3)).astype(np.float32)

    def _stalled(self, block: np.ndarray, link, audio_cfg) -> np.ndarray:
        """Nothing is getting through. Conceal briefly, then fall silent."""
        strength = _curve(1.0, audio_cfg.dropout_weight, knee=0.0)
        if strength <= 0.0:
            return self._ramp_to(block, 1.0)
        # First ~150 ms of a stall still produces concealment noise, after that
        # the far end gives up and the line goes quiet.
        if link.stall_elapsed < 0.15:
            out = self._concealed(len(block), audio_cfg)
            return self._ramp_to(out, 1.0 - strength * 0.4)

        # Fade the last thing that was audible down to nothing. Handing the
        # ramp a block of zeros would drop the gain on silence and leave the
        # real cut at the previous block boundary, which is a click.
        if self._gain > 1e-4:
            tail = block if self._conceal is None or len(self._conceal) != len(block) else self._conceal
            return self._ramp_to(tail, 0.0)
        return np.zeros(len(block), dtype=np.float32)

    def _ramp_to(self, block: np.ndarray, target: float) -> np.ndarray:
        """Linear gain ramp across the block, so gain changes never click."""
        n = len(block)
        if n == 0:
            return block
        if abs(self._gain - target) < 1e-4:
            self._gain = target
            return block if target == 1.0 else block * np.float32(target)
        ramp = np.linspace(self._gain, target, n, dtype=np.float32)
        self._gain = target
        return (block * ramp).astype(np.float32)


class JitterBuffer:
    """Variable extra delay, in whole blocks.

    Latency and audio/video desync are both delays, so both come from here.
    Depth changes are applied by padding or dropping, which is what a real
    jitter buffer does when it resizes.
    """

    def __init__(self, blocksize: int, max_blocks: int = 400) -> None:
        self.blocksize = blocksize
        self.max_blocks = max_blocks
        self._queue: list[np.ndarray] = []

    def reset(self) -> None:
        self._queue.clear()

    def push_pop(self, block: np.ndarray, depth_blocks: int) -> np.ndarray:
        depth = max(0, min(self.max_blocks, int(depth_blocks)))
        self._queue.append(block)
        while len(self._queue) > depth + 1:
            out = self._queue.pop(0)
            if len(self._queue) <= depth + 1:
                return out
        if len(self._queue) <= depth:
            return np.zeros(self.blocksize, dtype=np.float32)
        return self._queue.pop(0)
