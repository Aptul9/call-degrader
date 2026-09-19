"""Entry point.

    python run.py                      # browser UI on http://127.0.0.1:8720
    python run.py --window             # native window instead (the default in a build)
    python run.py --host 0.0.0.0       # reachable from a phone on the LAN
    python run.py --check              # probe devices and exit
    python run.py --no-audio           # video chain only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from src import preflight
from src.app import Controller
from src.config import AudioSettings, Settings, VideoSettings

log = logging.getLogger("call-degrader")


LOG_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "call-degrader"


def _setup_output(verbose: bool) -> Path | None:
    """Logging, plus somewhere for output to go when there is no console.

    A windowed build has no console, and PyInstaller then leaves `sys.stdout`
    and `sys.stderr` as None. A bare `print()` raises `AttributeError` there
    and the process dies before the window opens, with nothing on screen to
    say why. So when there is no stderr everything goes to a file, which is
    then the only record a failed start leaves behind.
    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

    handlers: list[logging.Handler] = []
    logfile: Path | None = None
    if sys.stderr is None or getattr(sys, "frozen", False):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            logfile = LOG_DIR / "call-degrader.log"
            # Append, never truncate. Opening "w" while another instance holds
            # the file is a sharing violation on Windows, and the handler here
            # then quietly fell back to no file at all: the second run left no
            # trace and the first run's stale log sat there looking like its
            # output. Two instances back to back is the normal case, because
            # that is what the test suite does.
            if logfile.exists() and logfile.stat().st_size > 1_000_000:
                logfile.unlink()
            handlers.append(logging.FileHandler(logfile, mode="a", encoding="utf-8"))
        except OSError:
            logfile = None  # read-only install directory, carry on without it
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))

    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S", handlers=handlers)

    if logfile:
        # One file, many runs. Without a banner carrying the pid there is no
        # way to tell where one start ends and the next begins.
        log.info("%s", "=" * 62)
        log.info("call-degrader pid %s starting", os.getpid())
    return logfile


def _asset(name: str) -> Path | None:
    """An `assets/` file, wherever it ended up.

    Frozen it is under `sys._MEIPASS`, from a checkout it is beside this file.
    Returns None rather than raising: a missing icon is worth a blank taskbar
    entry, not a refusal to start.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    root = Path(bundle) if bundle else Path(__file__).resolve().parent
    path = root / "assets" / name
    return path if path.exists() else None


def _say(text: str) -> None:
    """For the things a terminal would show. Safe with no terminal."""
    for line in text.splitlines():
        log.info("%s", line)
    if sys.stderr is not None:
        print(text, file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="webcam loop pedal and bad-connection simulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8720)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--size", default="1280x720")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--mic", default=None, help="input device name fragment")
    parser.add_argument("--cable", default="CABLE Input", help="output device name fragment")
    parser.add_argument("--pattern", action="store_true",
                        help="use a generated test pattern instead of the camera")
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--window", action="store_true",
                        help="native window instead of a browser tab (default when frozen)")
    parser.add_argument("--no-window", action="store_true",
                        help="browser tab even when frozen")
    parser.add_argument("--check", action="store_true", help="list devices and exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logfile = _setup_output(args.verbose)

    if args.check:
        return _check()

    try:
        width, height = (int(v) for v in args.size.lower().split("x"))
    except ValueError:
        _say(f"bad --size {args.size!r}, expected WIDTHxHEIGHT")
        return 2

    settings = Settings(
        video=VideoSettings(
            enabled=not args.no_video,
            source="pattern" if args.pattern else "camera",
            camera=args.camera,
            width=width,
            height=height,
            fps=args.fps,
        ),
        audio=AudioSettings(
            enabled=not args.no_audio,
            input_device=args.mic,
            output_device=args.cable,
        ),
    )

    # Before anything opens a device. Both drivers this depends on are
    # installed separately, and without this the app starts, the preview
    # moves, and nothing says why the call cannot see or hear it.
    findings = preflight.run(
        want_video=not args.no_video, want_audio=not args.no_audio
    )
    _say(preflight.report(findings))
    if logfile:
        _say(f"  log: {logfile}")

    controller = Controller(settings)
    controller.start()

    # A window from the exe, a browser tab from a checkout. Running the test
    # suites should not pop a window up, and someone who built an exe did not
    # do it to go on typing an address.
    frozen = bool(getattr(sys, "frozen", False))
    windowed = args.window or (frozen and not args.no_window)

    try:
        if windowed:
            return _run_windowed(controller, args)
        return _run_headless(controller, args)
    finally:
        controller.stop()


def _run_headless(controller, args) -> int:
    import uvicorn

    from src.server import create_app

    _say(f"\n  UI on http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}\n")
    try:
        uvicorn.run(
            create_app(controller),
            host=args.host,
            port=args.port,
            log_level="warning",
        )
    except KeyboardInterrupt:
        pass
    return 0


def _run_windowed(controller, args) -> int:
    """Native window over the same server.

    The server goes on a thread because webview.start() has to own the main
    one: on Windows the webview is COM and its message pump belongs to the
    thread that created it. Closing the window returns from start(), which is
    what shuts the server down, so there is one way out rather than two.
    """
    import threading

    import uvicorn

    from src.server import create_app

    try:
        import webview
    except ImportError as exc:
        _say(f"no native window ({exc}), falling back to the browser")
        return _run_headless(controller, args)

    host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    config = uvicorn.Config(
        create_app(controller), host=args.host, port=args.port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="http", daemon=True)
    thread.start()

    # Pointing the window at a socket that is not listening yet gives a blank
    # frame and no retry, so the window waits for the server rather than the
    # other way round.
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    else:
        _say("the server did not come up, falling back to the browser")

    _say(f"\n  UI in a window, and at http://{host}:{args.port}\n")
    window = webview.create_window(
        "call-degrader",
        f"http://{host}:{args.port}",
        width=1380,
        height=900,
        min_size=(900, 620),
    )

    # The tray is where the two pause toggles are reachable from, with the
    # call client covering the screen. It is not required: start() says so and
    # the app carries on without one.
    from src.tray import Tray

    def show() -> None:
        window.show()
        window.restore()

    # The .ico for this too, not the png. Pillow reads it and hands pystray
    # the 256px frame, so one file covers the exe, the window and the tray,
    # and there is one rule to remember rather than two.
    tray = Tray(controller, _asset("icon.ico"), on_show=show, on_quit=window.destroy)
    if not tray.start():
        _say("  no tray icon, the window is the only way in")

    try:
        # The exe carries its own icon, but the window is drawn by WebView2
        # and takes this one, so without it the taskbar entry is a generic
        # blank while the file in Explorer is not.
        #
        # It has to be a .ico. pywebview hands this straight to
        # System.Drawing.Icon, which does not read PNG, and it does so on a
        # .NET dispatcher thread: the ArgumentException never becomes a Python
        # exception, it takes the whole process down with 0xE0434352 and no
        # traceback. A png here cost a build and an event-log trawl to find.
        icon = _asset("icon.ico")
        webview.start(**({"icon": str(icon)} if icon else {}))
    except KeyboardInterrupt:
        pass
    finally:
        tray.stop()

    server.should_exit = True
    thread.join(timeout=5.0)
    return 0


def _check() -> int:
    from src.audio import list_devices

    devices = list_devices()
    for kind in ("inputs", "outputs"):
        _say(f"\n{kind}:")
        for d in devices[kind]:
            _say(f"  [{d['index']:>3}] {d['name'][:44]:<44} {d['api']:<18} "
                  f"{d['channels']}ch {d['samplerate']} Hz")

    _say("\ncamera:")
    import cv2

    for index in range(4):
        cap = cv2.VideoCapture(index, getattr(cv2, "CAP_MSMF", 0))
        if cap.isOpened():
            ok, frame = cap.read()
            if ok and frame is not None:
                _say(f"  [{index}] {frame.shape[1]}x{frame.shape[0]}")
        cap.release()

    _say("\nvirtual camera:")
    try:
        import pyvirtualcam

        with pyvirtualcam.Camera(width=640, height=360, fps=30) as cam:
            _say(f"  available: {cam.device}")
    except Exception as exc:
        _say(f"  unavailable: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
