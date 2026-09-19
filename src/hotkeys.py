"""Global hotkeys for the pedal.

The point of a pedal is that it works while the call window has focus, so these
are system-wide hooks rather than key handlers on the UI. The UI has buttons for
the same actions, and those are what to use if the hook cannot be installed.

Record is hold-to-record: the live feed keeps going out for as long as the key
is down, and the switch to the loop happens on release. Pressing to start and
pressing again to stop would put the moment you reach for the key inside the
recording, which is exactly the frame you do not want looping.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class Hotkeys:
    def __init__(self, settings_store, controller) -> None:
        self._store = settings_store
        self._controller = controller
        self._listener = None
        self._held = False
        self._lock = threading.Lock()
        self.error: str | None = None

    def start(self) -> None:
        try:
            from pynput import keyboard
        except Exception as exc:
            self.error = f"pynput unavailable: {exc}"
            log.warning(self.error)
            return
        try:
            self._listener = keyboard.Listener(on_press=self._press, on_release=self._release)
            self._listener.start()
        except Exception as exc:
            self.error = f"hotkey hook failed: {exc}"
            log.warning(self.error)
            self._listener = None

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    # -- handlers ------------------------------------------------------

    def _press(self, key) -> None:
        pedal = self._store.get().pedal
        if not pedal.enabled:
            return
        name = _name(key)
        if name is None:
            return
        if name == pedal.record_key:
            with self._lock:
                if self._held:
                    return  # key repeat, not a second press
                self._held = True
            self._safe(self._controller.record_start)
        elif name == pedal.live_key:
            self._safe(self._controller.go_live)

    def _release(self, key) -> None:
        pedal = self._store.get().pedal
        if not pedal.enabled:
            return
        if _name(key) != pedal.record_key:
            return
        with self._lock:
            if not self._held:
                return
            self._held = False
        self._safe(self._controller.record_stop)

    def _safe(self, fn) -> None:
        # An exception raised inside a pynput callback kills the listener
        # silently, and the first sign is that the pedal stopped working.
        try:
            fn()
        except Exception as exc:
            log.exception("hotkey action failed: %s", exc)


def _name(key) -> str | None:
    """pynput key to the name used in the settings, e.g. 'alt_r', 'f9', 'a'."""
    name = getattr(key, "name", None)
    if name:
        return name
    char = getattr(key, "char", None)
    return char.lower() if char else None
