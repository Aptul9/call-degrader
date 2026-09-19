"""System tray icon, for the two things you reach for mid-call.

The window is where the tool is configured. The tray is where it is operated:
during a call you want to cut the camera or the microphone without hunting for
a window behind the call client, and that is all this offers.

It runs on its own thread. pywebview owns the main one, because the webview is
COM and its message pump belongs to whichever thread created it, and pystray
wants a message pump of its own. Two pumps, two threads, neither blocking the
other.

Absent or broken, the app still runs: `start()` reports whether it came up and
nothing else depends on it.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class Tray:
    """A tray icon over a running controller.

    `on_show` and `on_quit` come from whoever owns the window, because this
    has no business knowing what a window is.
    """

    def __init__(self, controller, icon: Path | None,
                 on_show=None, on_quit=None) -> None:
        self._controller = controller
        self._icon_path = icon
        self._on_show = on_show or (lambda: None)
        self._on_quit = on_quit or (lambda: None)
        self._icon = None
        self._thread: threading.Thread | None = None

    # -- state the menu reads and writes --------------------------------

    def _paused(self, section: str) -> bool:
        return bool(getattr(self._controller.settings.get(), section).paused)

    def _toggle(self, section: str) -> None:
        want = not self._paused(section)
        try:
            self._controller.patch({section: {"paused": want}})
        except Exception as exc:
            # A tray click must never take the app down with it.
            log.warning("tray could not toggle %s: %s", section, exc)
        if self._icon is not None:
            self._icon.update_menu()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """True if the icon is up. False is not an error, just no tray."""
        try:
            import pystray
            from PIL import Image
        except ImportError as exc:
            log.info("no tray icon (%s)", exc)
            return False

        if not self._icon_path or not self._icon_path.exists():
            log.info("no tray icon: %s is missing", self._icon_path)
            return False

        try:
            image = Image.open(self._icon_path)
            image.load()
        except Exception as exc:
            log.warning("no tray icon, %s would not open: %s", self._icon_path, exc)
            return False

        # `checked` is re-read every time the menu opens, so the ticks follow
        # the settings even when they are changed from the window instead.
        menu = pystray.Menu(
            pystray.MenuItem("open call-degrader", self._show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("pause camera", lambda *_: self._toggle("video"),
                             checked=lambda _: self._paused("video")),
            pystray.MenuItem("pause microphone", lambda *_: self._toggle("audio"),
                             checked=lambda _: self._paused("audio")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("quit", self._quit),
        )
        self._icon = pystray.Icon("call-degrader", image, "call-degrader", menu)

        def run() -> None:
            try:
                self._icon.run()
            except Exception as exc:
                log.warning("tray icon stopped: %s", exc)

        self._thread = threading.Thread(target=run, name="tray", daemon=True)
        self._thread.start()
        log.info("tray icon up, from %s", self._icon_path)
        return True

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception as exc:
                log.debug("tray stop: %s", exc)
            self._icon = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # -- menu actions ----------------------------------------------------

    def _show(self, *_args) -> None:
        try:
            self._on_show()
        except Exception as exc:
            log.warning("tray could not show the window: %s", exc)

    def _quit(self, *_args) -> None:
        # Stop the icon first. Tearing the window down from this thread ends
        # webview.start() on the main one, and a tray still drawing itself
        # while that happens leaves a dead icon in the tray until something
        # hovers over it.
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception as exc:
                log.debug("tray stop on quit: %s", exc)
        try:
            self._on_quit()
        except Exception as exc:
            log.warning("tray could not close the window: %s", exc)
