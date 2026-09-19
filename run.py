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
import sys
import time

from src import preflight
from src.app import Controller
from src.config import AudioSettings, Settings, VideoSettings


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

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.check:
        return _check()

    try:
        width, height = (int(v) for v in args.size.lower().split("x"))
    except ValueError:
        print(f"bad --size {args.size!r}, expected WIDTHxHEIGHT", file=sys.stderr)
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
    print(preflight.report(findings), file=sys.stderr)

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

    print(f"\n  UI on http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}\n")
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
        print(f"no native window ({exc}), falling back to the browser", file=sys.stderr)
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
        print("the server did not come up, falling back to the browser", file=sys.stderr)

    print(f"\n  UI in a window, and at http://{host}:{args.port}\n")
    webview.create_window(
        "call-degrader",
        f"http://{host}:{args.port}",
        width=1380,
        height=900,
        min_size=(900, 620),
    )
    try:
        webview.start()
    except KeyboardInterrupt:
        pass

    server.should_exit = True
    thread.join(timeout=5.0)
    return 0


def _check() -> int:
    from src.audio import list_devices

    devices = list_devices()
    for kind in ("inputs", "outputs"):
        print(f"\n{kind}:")
        for d in devices[kind]:
            print(f"  [{d['index']:>3}] {d['name'][:44]:<44} {d['api']:<18} "
                  f"{d['channels']}ch {d['samplerate']} Hz")

    print("\ncamera:")
    import cv2

    for index in range(4):
        cap = cv2.VideoCapture(index, getattr(cv2, "CAP_MSMF", 0))
        if cap.isOpened():
            ok, frame = cap.read()
            if ok and frame is not None:
                print(f"  [{index}] {frame.shape[1]}x{frame.shape[0]}")
        cap.release()

    print("\nvirtual camera:")
    try:
        import pyvirtualcam

        with pyvirtualcam.Camera(width=640, height=360, fps=30) as cam:
            print(f"  available: {cam.device}")
    except Exception as exc:
        print(f"  unavailable: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
