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
    # Whether the two delays above are applied at all. Every other part of a
    # bad line survives being switched off and still reads as the same line;
    # delay does not. The presets carry 0.15 s to 1.2 s of it, and on top of
    # the transport the far end is answering a second late, which stops being
    # a bad connection and starts being a conversation nobody can hold. Off
    # keeps the stalls, the dropouts and the artefacts and leaves the timing
    # where it was.
    add_delay: bool = True
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
    # Release the physical camera and feed the call a dark card instead. The
    # camera light goes out, which is the whole point, and the virtual camera
    # stays up so a call in progress does not see its device disappear.
    # Independent of `source`: pausing and then unpausing comes back to
    # whatever source was selected.
    paused: bool = False
    width: int = 1280
    height: int = 720
    fps: int = 30
    # Mirrors the preview and nothing else. A self-view that is not a mirror
    # is disorienting to sit in front of, and it is what every call
    # application shows you. What goes down the wire is never flipped: the far
    # end is looking at you rather than at your reflection, so a mirrored feed
    # arrives with writing backwards and pointing right arriving as pointing
    # left.
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
    # Preference, not a requirement. `find_cable_output` tries this first and
    # falls through to the other VB-CABLE playback endpoint names when the
    # machine is not presenting this one.
    output_device: str = "CABLE Input"
    samplerate: int = 48000
    blocksize: int = 480  # 10 ms at 48 kHz
    # PortAudio latency hint, passed to both streams. sounddevice leaves this
    # at "high" and nothing here used to override it, which on the MME view of
    # the cable negotiated 180 ms on the playback side alone. Measured on this
    # machine at 48 kHz, in ms:
    #
    #   playback  WASAPI  22 / 22 / 22      capture  MME          30 / 20 / 30
    #             MME    180 / 100 / 180             DirectSound  10 / 10 / 10
    #             DirectSound 240 / 120 / 240        WASAPI       22 / 22 / 22
    #
    # read as default / low / high. A float here is taken as seconds and is a
    # request rather than a promise.
    latency: str | float = "low"
    # Release the microphone and write silence to the cable instead. Same
    # bargain as the video pause: the input device is closed so the privacy
    # indicator goes out, and the output stream stays open so a call in
    # progress keeps seeing a live microphone rather than a device that died.
    paused: bool = False
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
# behind, so switching from a capped line to `original` would silently keep the
# cap and the line would never look clean again.
PRESETS: dict[str, dict] = {
    "original": {
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
    "original": {
        "link": {"enabled": False},
        "audio": {
            "dropout_weight": 1.0, "stutter_weight": 1.0, "bitcrush_weight": 1.0,
            "warble_weight": 1.0, "metallic_weight": 0.8,
        },
    },
    # Gaps, and a voice that is intact between them. The stall rate carries
    # this one: with it at 8 a minute no stall landed inside speech over a
    # 13 s take, so the preset named for chopping measured 0.000 silence.
    "choppy": {
        "link": {
            "enabled": True, "quality": 40.0, "ceiling": 55.0, "floor": 22.0,
            "drift": 10.0, "stall_rate": 16.0, "stall_min": 0.2, "stall_max": 0.7,
        },
        "audio": {
            "dropout_weight": 1.4, "stutter_weight": 1.6, "bitcrush_weight": 0.4,
            "warble_weight": 0.3, "metallic_weight": 0.2,
        },
    },
    # Dull and swimming, which is the opposite of robot: warble and crush
    # lead, and the comb is nearly off because a metallic ring is the one
    # thing underwater should not have.
    #
    # `bitcrush_weight` cannot separate the two things it does. Dropping
    # bit depth adds harmonics and brightens; the sample-and-hold under it
    # is a low-pass and dulls. One knob drives both, so neither this nor
    # `robot` can be pushed to a high-frequency signature without moving
    # the other one with it: measured 1.04x and 0.96x of the clean take
    # against a target of clearly under and clearly over. Splitting them
    # into two weights is the fix, and it is not a tuning change.
    "underwater": {
        "link": {
            "enabled": True, "quality": 18.0, "ceiling": 30.0, "floor": 8.0,
            "drift": 12.0, "stall_rate": 4.0, "stall_min": 0.3, "stall_max": 1.0,
        },
        "audio": {
            "dropout_weight": 0.6, "stutter_weight": 0.8, "bitcrush_weight": 2.0,
            "warble_weight": 2.0, "metallic_weight": 0.4,
        },
    },
    # Machine-voiced but still intelligible, so the comb leads and the
    # bitcrush follows. Its sample-and-hold is a low-pass in disguise: at 1.6
    # it took more top off than the comb put back and the preset named for a
    # metallic ring measured below the clean take for high-frequency share.
    #
    # Sits after `underwater` because it is the harsher of the two, which the
    # names do not tell you: measured on one take, underwater keeps 0.786 of
    # the waveform and robot 0.715. The row is read left to right as a ladder,
    # so it is ordered like one.
    "robot": {
        "link": {
            "enabled": True, "quality": 26.0, "ceiling": 38.0, "floor": 12.0,
            "drift": 8.0, "stall_rate": 4.0, "stall_min": 0.2, "stall_max": 0.6,
        },
        "audio": {
            "dropout_weight": 0.8, "stutter_weight": 2.0, "bitcrush_weight": 1.0,
            "warble_weight": 0.5, "metallic_weight": 2.0,
        },
    },
    # Between `robot` and `barely there`, and it exists because there was
    # nothing there. Robot leaves the voice whole and barely there takes about
    # 39 percent of a phrase away, which is too much to hold a conversation
    # over. This interrupts without swallowing sentences: 0.156 silent on
    # average, 0.246 on the worst of twelve draws.
    "half there": {
        "link": {
            "enabled": True, "quality": 20.0, "ceiling": 36.0, "floor": 10.0,
            "drift": 10.0, "stall_rate": 24.0, "stall_min": 0.25, "stall_max": 0.9,
        },
        "audio": {
            "dropout_weight": 1.4, "stutter_weight": 1.5, "bitcrush_weight": 1.2,
            "warble_weight": 0.9, "metallic_weight": 0.8,
        },
    },
    # Stalls come often and short rather than rarely and long, and that is
    # about variance, not severity. At 20 a minute with stalls of 1 to 4
    # seconds, one draw of a 13 s take measured 0.187 of it silent and another
    # 0.773: the same preset on the same words was either mostly audible or
    # almost entirely gone, and which one you got was luck. That is why the
    # A/B player and the call sound like different settings. At 40 a minute
    # with 0.4 to 1.6 the average interruption is about the same, 0.405
    # against 0.472, and the spread across draws halves, 0.269 against 0.586.
    "barely there": {
        "link": {
            "enabled": True, "quality": 8.0, "ceiling": 22.0, "floor": 0.0,
            "drift": 8.0, "stall_rate": 40.0, "stall_min": 0.4, "stall_max": 1.6,
        },
        "audio": {
            "dropout_weight": 2.0, "stutter_weight": 1.8, "bitcrush_weight": 1.5,
            "warble_weight": 1.2, "metallic_weight": 1.0,
        },
    },
}


def _matches(settings: "Settings", preset: dict) -> bool:
    for section, values in preset.items():
        current = getattr(settings, section, None)
        if current is None:
            return False
        for field, want in values.items():
            got = getattr(current, field, None)
            if isinstance(want, bool) or isinstance(got, bool):
                if bool(got) != bool(want):
                    return False
            elif isinstance(want, (int, float)) and isinstance(got, (int, float)):
                if abs(float(got) - float(want)) > 1e-6:
                    return False
            elif got != want:
                return False
    return True


def match_preset(settings: "Settings") -> str | None:
    """Which line preset these settings are, if any.

    Nothing stores the name: a preset is shorthand for a set of field values
    and nothing else. Deriving it back is what survives a page reload, where
    the browser has forgotten what was clicked and the server never knew. It
    also drops the highlight the moment a slider moves the settings off the
    preset, which the old client-side guess got wrong in both directions.
    """
    return next((name for name, p in PRESETS.items() if _matches(settings, p)), None)


def match_audio_preset(settings: "Settings") -> str | None:
    return next((name for name, p in AUDIO_PRESETS.items() if _matches(settings, p)), None)


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
