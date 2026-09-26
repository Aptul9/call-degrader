"""Wires the pieces together and owns their lifecycle.

Nothing here does work of its own. It exists so the web server, the hotkeys and
a future native window all drive the same object rather than each reaching into
the pipelines.
"""

from __future__ import annotations

import logging

from .audio import AudioPipeline, list_devices, set_default_microphone
from .config import (
    AUDIO_PRESETS,
    PRESETS,
    Settings,
    SettingsStore,
    match_audio_preset,
    match_preset,
)
from .hotkeys import Hotkeys
from . import preflight
from .state import LinkSimulator
from .video import VideoPipeline, list_cameras

log = logging.getLogger(__name__)


class Controller:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = SettingsStore(settings)
        self.link = LinkSimulator(self.settings)
        self.video = VideoPipeline(self.settings, self.link)
        self.audio = AudioPipeline(
            self.settings, self.link, pedal_state=lambda: self.video.looper.state.value
        )
        self.hotkeys = Hotkeys(self.settings, self)

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        self.link.start()
        if self.settings.get().video.enabled:
            self.video.start()
        if self.settings.get().audio.enabled:
            self.audio.start()
        self.hotkeys.start()

    def stop(self) -> None:
        self.hotkeys.stop()
        self.audio.stop()
        self.video.stop()
        self.link.stop()

    # -- pedal ---------------------------------------------------------

    def record_start(self) -> None:
        pedal = self.settings.get().pedal
        self.video.looper.start_record(pedal.max_seconds)

    def record_stop(self) -> bool:
        pedal = self.settings.get().pedal
        return self.video.looper.stop_record(
            pedal.min_seconds, pedal.crossfade, pedal.loop_mode
        )

    def go_live(self) -> None:
        self.video.looper.go_live(self.settings.get().pedal.crossfade)

    def clear_loop(self) -> None:
        self.video.looper.clear()

    # -- preview -------------------------------------------------------

    def preview_visible(self, visible: bool) -> None:
        """The window showing the preview went out of sight, or came back."""
        self.video.pause_preview(not visible)

    # -- settings ------------------------------------------------------

    def patch(self, changes: dict) -> Settings:
        before = self.settings.get()
        after = self.settings.patch(changes)
        self._reconcile(before, after)
        return after

    def apply_preset(self, name: str) -> Settings:
        before = self.settings.get()
        after = self.settings.apply_preset(name)
        self._reconcile(before, after)
        return after

    def apply_audio_preset(self, name: str) -> Settings:
        before = self.settings.get()
        after = self.settings.apply_audio_preset(name)
        self._reconcile(before, after)
        return after

    def _reconcile(self, before: Settings, after: Settings) -> None:
        """Restart whichever chain had a setting changed that it reads once."""
        if before.video.enabled != after.video.enabled:
            self.video.start() if after.video.enabled else self.video.stop()
        elif after.video.enabled and _differs(
            before.video, after.video,
            ("source", "camera", "backend", "paused", "width", "height", "fps"),
        ):
            self.video.stop()
            self.video.start()

        if before.audio.enabled != after.audio.enabled:
            self.audio.start() if after.audio.enabled else self.audio.stop()
        elif after.audio.enabled and _differs(
            before.audio, after.audio,
            ("input_device", "output_device", "samplerate", "blocksize", "paused",
             "latency"),
        ):
            self.audio.restart()

    # -- reporting -----------------------------------------------------

    def status(self) -> dict:
        snap = self.link.get()
        return {
            "video": self.video.status(),
            "audio": self.audio.status(),
            "link": {
                "enabled": snap.enabled,
                "quality": round(snap.quality, 1),
                "severity": round(snap.severity, 3),
                "stalled": snap.stalled,
            },
            "hotkeys": {"error": self.hotkeys.error},
        }

    def settings_reply(self, settings: Settings | None = None) -> dict:
        """Settings, plus which presets those values currently amount to.

        Every write answers with this, so the highlight in the UI is read off
        the values rather than remembered from the click that produced them.
        A reload, or a slider that walks the settings off a preset, then both
        land on the truth without the client keeping its own tally.
        """
        current = settings or self.settings.get()
        return {
            "settings": current.to_dict(),
            "preset": match_preset(current),
            "audio_preset": match_audio_preset(current),
        }

    def full_state(self) -> dict:
        return {
            **self.settings_reply(),
            "status": self.status(),
            "presets": list(PRESETS),
            "audio_presets": list(AUDIO_PRESETS),
            # What is missing on this machine, so the page can say so instead
            # of just failing quietly.
            #
            # Notes are left out on purpose. They are things that used to
            # matter and no longer do on a current machine, so on a working
            # one they would put a banner on screen at every start that is
            # always there and never actionable. They still go to the log,
            # which is where someone looks when something is actually wrong.
            "preflight": [f.to_dict() for f in preflight.run(
                want_video=self.settings.get().video.enabled,
                want_audio=self.settings.get().audio.enabled,
            ) if f.level != "note"],
        }

    # -- passthroughs --------------------------------------------------

    @staticmethod
    def devices() -> dict:
        return list_devices()

    @staticmethod
    def cameras() -> list[dict]:
        return list_cameras()

    @staticmethod
    def route_microphone(match: str = "CABLE Output") -> str:
        return set_default_microphone(match)


def _differs(a, b, fields) -> bool:
    return any(getattr(a, f) != getattr(b, f) for f in fields)
