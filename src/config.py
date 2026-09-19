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
    width: int = 1280
    height: int = 720
    fps: int = 30
    mirror: bool = True
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
    # Opacity of the loop ghosted over the live feed in the preview only.
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


PRESETS: dict[str, dict] = {
    "perfect": {
        "link": {"enabled": False, "quality": 100.0, "drift": 0.0, "stall_rate": 0.0},
    },
    "slightly-off": {
        "link": {
            "enabled": True,
            "quality": 78.0,
            "drift": 6.0,
            "stall_rate": 1.5,
            "stall_min": 0.2,
            "stall_max": 0.7,
            "latency": 0.15,
            "desync": 0.08,
        },
    },
    "bad-wifi": {
        "link": {
            "enabled": True,
            "quality": 48.0,
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
            "quality": 22.0,
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
            "drift": 8.0,
            "stall_rate": 22.0,
            "stall_min": 1.5,
            "stall_max": 6.0,
            "latency": 1.2,
            "desync": 0.6,
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
