"""The simulated link.

One object, ticked by one thread, read by both the video and the audio chain.
That sharing is the whole point: a freeze that does not line up with a dropout
reads as two broken things rather than one bad connection.

The quality figure is an Ornstein-Uhlenbeck walk around the set point, so it
wanders and pulls back instead of jumping. Stalls arrive as a Poisson process
on top and force both chains to the floor for their duration.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass

from .config import LinkSettings

# Quality below this counts as a hard stall for the chains that ask.
STALL_FLOOR = 3.0


@dataclass(frozen=True)
class LinkSnapshot:
    """What the chains read. Immutable, so a reader never sees a half-update."""

    quality: float  # 0 unusable .. 100 perfect
    severity: float  # 0 perfect .. 1 unusable, the number effects scale on
    stalled: bool
    stall_elapsed: float  # seconds into the current stall, 0 when not stalled
    latency: float  # seconds of extra delay asked for
    desync: float  # seconds audio should lag video, may be negative
    enabled: bool
    at: float  # monotonic timestamp of the tick

    @property
    def usable(self) -> bool:
        return not self.stalled and self.quality > STALL_FLOOR


IDLE = LinkSnapshot(
    quality=100.0,
    severity=0.0,
    stalled=False,
    stall_elapsed=0.0,
    latency=0.0,
    desync=0.0,
    enabled=False,
    at=0.0,
)


class LinkSimulator:
    """Ticks the link state on its own thread and publishes a snapshot."""

    TICK = 1.0 / 100.0  # 100 Hz, finer than a 30 fps frame or a 10 ms block
    # Pull strength of the walk back towards the set point, per second.
    THETA = 0.9

    def __init__(self, settings_store, tick: float | None = None) -> None:
        self._store = settings_store
        self._tick = tick or self.TICK
        self._snapshot = IDLE
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._level = 100.0
        self._stall_until = 0.0
        self._stall_started = 0.0
        self._rng = random.Random()
        self._seeded_with: int | None = None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- reading -------------------------------------------------------

    def get(self) -> LinkSnapshot:
        with self._lock:
            return self._snapshot

    # -- internals -----------------------------------------------------

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            dt = min(now - last, 0.25)  # a scheduler hiccup must not teleport the walk
            last = now
            snap = self._advance(self._store.get().link, dt, now)
            with self._lock:
                self._snapshot = snap
            self._stop.wait(self._tick)

    def _advance(self, cfg: LinkSettings, dt: float, now: float) -> LinkSnapshot:
        if not cfg.enabled:
            self._level = 100.0
            self._stall_until = 0.0
            return LinkSnapshot(
                quality=100.0,
                severity=0.0,
                stalled=False,
                stall_elapsed=0.0,
                latency=0.0,
                desync=0.0,
                enabled=False,
                at=now,
            )

        if cfg.seed and cfg.seed != self._seeded_with:
            self._rng.seed(cfg.seed)
            self._seeded_with = cfg.seed

        self._level = self._walk(self._level, cfg.quality, cfg.drift, dt)
        self._maybe_stall(cfg, dt, now)

        stalled = now < self._stall_until
        if stalled:
            quality = 0.0
            stall_elapsed = now - self._stall_started
        else:
            quality = self._level
            stall_elapsed = 0.0

        return LinkSnapshot(
            quality=quality,
            severity=1.0 - (quality / 100.0),
            stalled=stalled,
            stall_elapsed=stall_elapsed,
            latency=max(0.0, cfg.latency),
            desync=cfg.desync,
            enabled=True,
            at=now,
        )

    def _walk(self, level: float, setpoint: float, drift: float, dt: float) -> float:
        if drift <= 0.0:
            return setpoint
        pull = self.THETA * (setpoint - level) * dt
        kick = drift * math.sqrt(dt) * self._rng.gauss(0.0, 1.0)
        return _clamp(level + pull + kick, 0.0, 100.0)

    def _maybe_stall(self, cfg: LinkSettings, dt: float, now: float) -> None:
        if now < self._stall_until or cfg.stall_rate <= 0.0:
            return
        # Poisson arrivals: rate is given per minute, dt is in seconds.
        if self._rng.random() >= (cfg.stall_rate / 60.0) * dt:
            return
        lo = max(0.0, cfg.stall_min)
        hi = max(lo, cfg.stall_max)
        self._stall_started = now
        self._stall_until = now + self._rng.uniform(lo, hi)


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value
