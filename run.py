"""Entry point.

    python run.py                      # browser UI on http://127.0.0.1:8720
    python run.py --host 0.0.0.0       # reachable from a phone on the LAN
    python run.py --check              # probe devices and exit
    python run.py --no-audio           # video chain only
"""

from __future__ import annotations

import argparse
import logging
import sys

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

    controller = Controller(settings)
    controller.start()

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
    finally:
        controller.stop()
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
