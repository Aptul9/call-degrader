"""Settings objects and presets.

Everything the UI can change lives here. One dataclass per chain plus the
shared link quality settings that drive both. Values are plain floats so the
whole thing serialises to JSON without a converter.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field, replace


@dataclass(frozen=True)
class LinkSettings:
    """The simulated line. Drives the video and the audio chain together."""

    enabled: bool = False
    # Steady-state quality, 0 = unusable, 100 = perfect.
    quality: float = 100.0
    # Quality can never rise above this, however far the walk wanders. A line
    # that is genuinely bad recovers to tolerable, not to flawless, and a
    # recovery that touches 100 reads as the problem having gone away.
    ceiling: float = 100.0
    # Nor below this. Keeps a constantly-poor line poor instead of letting it
    # bottom out into what looks like a dropped connection.
    floor: float = 0.0
    # How far quality wanders around the set point, in points.
    drift: float = 8.0
    # Stalls per minute and how long one lasts, in seconds.
    stall_rate: float = 3.0
    stall_min: float = 0.4
    stall_max: float = 2.5
    # Seconds of extra one-way delay applied on top of the effects.
    latency: float = 0.0
    # Positive pushes audio behind video, negative pulls it ahead. Seconds.
    desync: float = 0.0
    seed: int = 0


@dataclass(frozen=True)
class VideoSettings:
    enabled: bool = True
    # "camera" or "pattern". The pattern is a generated moving image, so the
    # effects can be judged with the lens covered, and so the chain can be
    # tested on a machine where the camera is dark or in use.
    source: str = "camera"
    camera: int = 0
    # Which capture backend `camera` is an index into. MSMF and DirectShow
    # enumerate in different orders on Windows, so an index is only meaningful
    # alongside the backend it came from. The picker lists DirectShow devices,
    # because that is the only backend whose devices can be named, and pins
    # this to "dshow" when you choose one. "auto" keeps the old behaviour of
    # trying each backend in turn.
    backend: str = "auto"
    width: int = 1280
    height: int = 720
    fps: int = 30
    mirror: bool = True
    # Degrade brightness only and put the original colour back afterwards.
    # A starved codec really does wreck chroma, but the result is a picture
    # whose colours crawl, which looks like a fault in the camera rather than
    # in the line, and is unpleasant to watch. On, the feed still blurs, blocks,
    # drops and smears; it just keeps its palette.
    keep_colours: bool = True
    # Each weight scales how hard that effect reacts to falling quality.
    # 0 disables the effect, 1 is the tuned default.
    drop_weight: float = 1.0
    blockiness_weight: float = 1.0
    resolution_weight: float = 1.0
    banding_weight: float = 1.0
    smear_weight: float = 1.0
    tearing_weight: float = 0.6
    jitter_weight: float = 1.0


@dataclass(frozen=True)
class AudioSettings:
    enabled: bool = True
    input_device: str | None = None
    output_device: str = "CABLE Input"
    samplerate: int = 48000
    blocksize: int = 480  # 10 ms at 48 kHz
    # Monitor the processed signal on the speakers. Feedback risk on a laptop
    # mic, off by default.
    monitor: bool = False
    dropout_weight: float = 1.0
    stutter_weight: float = 1.0
    bitcrush_weight: float = 1.0
    warble_weight: float = 1.0
    metallic_weight: float = 0.8
    jitter_weight: float = 1.0


@dataclass(frozen=True)
class PedalSettings:
    """The loop pedal. Independent of the link simulation."""

    enabled: bool = True
    record_key: str = "alt_r"
    live_key: str = "ctrl_r"
    max_seconds: float = 30.0
    min_seconds: float = 1.0
    crossfade: float = 0.5
    # "bounce" plays to the end then walks back to the start, so there is no
    # join to hide: the frame before the turn and the frame after it are
    # neighbours in the recording. "crossfade" wraps end to start and dissolves
    # over the join, which still has to travel from the last pose back to the
    # first, so it reads as a reset with a fade over it. Bounce costs
    # direction: half the cycle runs backwards, invisible on idle movement,
    # obvious on anything directional.
    loop_mode: str = "bounce"
    # Ghost the live camera under the playing loop, in the preview only. It
    # helps you line yourself back up before going live, and it is also two
    # copies of you moving out of step, which is unpleasant to sit in front of
    # for any length of time. Off means the preview shows exactly what the call
    # is getting.
    ghost: bool = True
    # Opacity of that ghost when it is on.
    overlay: float = 0.5
    # Freeze the audio too while the video loops, so a moving mouth on a loop
    # is not betrayed by live speech.
    mute_on_loop: bool = True


@dataclass(frozen=True)
class Settings:
    link: LinkSettings = field(default_factory=LinkSettings)
    video: VideoSettings = field(default_factory=VideoSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    pedal: PedalSettings = field(default_factory=PedalSettings)

    def to_dict(self) -> dict:
        return asdict(self)


# Every preset sets every field it cares about, ceiling and floor included.
# A preset that leaves one out inherits whatever the previous preset left
# behind, so switching from a capped line to `perfect` would silently keep the
# cap and the line would never look clean again.
PRESETS: dict[str, dict] = {
    "perfect": {
        "link": {
            "enabled": False,
            "quality": 100.0,
            "ceiling": 100.0,
            "floor": 0.0,
            "drift": 0.0,
            "stall_rate": 0.0,
            "latency": 0.0,
            "desync": 0.0,
        },
    },
    "slightly-off": {
        "link": {
            "enabled": True,
            "quality": 70.0,
            "ceiling": 84.0,
            "floor": 48.0,
            "drift": 6.0,
            "stall_rate": 1.5,
            "stall_min": 0.2,
            "stall_max": 0.7,
            "latency": 0.15,
            "desync": 0.08,
        },
    },
    # Bad the whole way through. A narrow band and almost no drift, so it never
    # improves and never collapses either: the line is simply poor, constantly.
    "always-rough": {
        "link": {
            "enabled": True,
            "quality": 40.0,
            "ceiling": 50.0,
            "floor": 30.0,
            "drift": 3.0,
            "stall_rate": 2.0,
            "stall_min": 0.3,
            "stall_max": 1.0,
            "latency": 0.4,
            "desync": 0.15,
        },
    },
    # Wanders a lot and does recover, but only as far as tolerable. The ceiling
    # is what stops it reading as fixed.
    "never-perfect": {
        "link": {
            "enabled": True,
            "quality": 48.0,
            "ceiling": 64.0,
            "floor": 20.0,
            "drift": 16.0,
            "stall_rate": 5.0,
            "stall_min": 0.3,
            "stall_max": 1.6,
            "latency": 0.3,
            "desync": 0.15,
        },
    },
    "bad-wifi": {
        "link": {
            "enabled": True,
            "quality": 42.0,
            "ceiling": 60.0,
            "floor": 18.0,
            "drift": 14.0,
            "stall_rate": 6.0,
            "stall_min": 0.4,
            "stall_max": 1.8,
            "latency": 0.35,
            "desync": 0.18,
        },
    },
    "train-tunnel": {
        "link": {
            "enabled": True,
            "quality": 25.0,
            "ceiling": 40.0,
            "floor": 8.0,
            "drift": 18.0,
            "stall_rate": 14.0,
            "stall_min": 0.8,
            "stall_max": 4.0,
            "latency": 0.7,
            "desync": 0.35,
        },
    },
    "about-to-drop": {
        "link": {
            "enabled": True,
            "quality": 8.0,
            "ceiling": 22.0,
            "floor": 0.0,
            "drift": 8.0,
            "stall_rate": 22.0,
            "stall_min": 1.5,
            "stall_max": 6.0,
            "latency": 1.2,
            "desync": 0.6,
        },
    },
}


# Audio presets carry a line setting as well as the weights, and that is on
# purpose. The weights only ever scale a reaction to a falling line: with the
# line off, or sitting at perfect, every one of them multiplies zero. A preset
# that set weights alone would do nothing audible and look broken.
AUDIO_PRESETS: dict[str, dict] = {
    "clean voice": {
        "link": {"enabled": False},
        "audio": {
            "dropout_weight": 1.0, "stutter_weight": 1.0, "bitcrush_weight": 1.0,
            "warble_weight": 1.0, "metallic_weight": 0.8,
        },
    },
    "choppy": {
        "link": {
            "enabled": True, "quality": 40.0, "ceiling": 55.0, "floor": 22.0,
            "drift": 10.0, "stall_rate": 8.0, "stall_min": 0.2, "stall_max": 0.8,
        },
        "audio": {
            "dropout_weight": 1.3, "stutter_weight": 1.5, "bitcrush_weight": 0.4,
            "warble_weight": 0.3, "metallic_weight": 0.2,
        },
    },
    "robot": {
        "link": {
            "enabled": True, "quality": 26.0, "ceiling": 38.0, "floor": 12.0,
            "drift": 8.0, "stall_rate": 4.0, "stall_min": 0.2, "stall_max": 0.6,
        },
        "audio": {
            "dropout_weight": 0.8, "stutter_weight": 2.0, "bitcrush_weight": 1.6,
            "warble_weight": 0.5, "metallic_weight": 1.4,
        },
    },
    "underwater": {
        "link": {
            "enabled": True, "quality": 22.0, "ceiling": 34.0, "floor": 10.0,
            "drift": 12.0, "stall_rate": 3.0, "stall_min": 0.3, "stall_max": 1.0,
        },
        "audio": {
            "dropout_weight": 0.5, "stutter_weight": 0.6, "bitcrush_weight": 2.0,
            "warble_weight": 2.0, "metallic_weight": 1.6,
        },
    },
    "barely there": {
        "link": {
            "enabled": True, "quality": 8.0, "ceiling": 22.0, "floor": 0.0,
            "drift": 8.0, "stall_rate": 20.0, "stall_min": 1.0, "stall_max": 4.0,
        },
        "audio": {
            "dropout_weight": 2.0, "stutter_weight": 1.8, "bitcrush_weight": 1.5,
            "warble_weight": 1.2, "metallic_weight": 1.0,
        },
    },
}


_SECTIONS = {
    "link": LinkSettings,
    "video": VideoSettings,
    "audio": AudioSettings,
    "pedal": PedalSettings,
}


class SettingsStore:
    """Holds the current Settings. Readers get an immutable snapshot.

    Writers take the lock. The dataclasses are frozen, so a reader that grabbed
    a snapshot keeps a consistent view even while the UI is changing values.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._lock = threading.Lock()
        self._settings = settings or Settings()
        self._version = 0

    def get(self) -> Settings:
        with self._lock:
            return self._settings

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def patch(self, changes: dict) -> Settings:
        """Apply a partial {section: {field: value}} update."""
        with self._lock:
            current = self._settings
            updates = {}
            for section, values in changes.items():
                cls = _SECTIONS.get(section)
                if cls is None or not isinstance(values, dict):
                    continue
                known = {k: v for k, v in values.items() if k in cls.__dataclass_fields__}
                if known:
                    updates[section] = replace(getattr(current, section), **known)
            if updates:
                self._settings = replace(current, **updates)
                self._version += 1
            return self._settings

    def apply_preset(self, name: str) -> Settings:
        preset = PRESETS.get(name)
        if preset is None:
            raise KeyError(name)
        return self.patch(preset)

    def apply_audio_preset(self, name: str) -> Settings:
        preset = AUDIO_PRESETS.get(name)
        if preset is None:
            raise KeyError(name)
        return self.patch(preset)
