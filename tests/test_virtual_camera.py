"""Proof that frames actually leave the tool and arrive at another application.

Everything else measures the app's own preview, which only shows that the chain
ran. This opens the virtual camera from a separate process, the same way Zoom or
Teams would, and measures what comes back out of the driver.

Run it against any running app; it switches the source to the generated
pattern for its measurements and puts back whatever it found.

    python run.py
    python tests/test_virtual_camera.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    API, patch, pedal, preset, require_running, source, wait_link_below,
)

HELD_THRESHOLD = 2.0


def find_virtual_camera() -> tuple[int, str]:
    """DirectShow index of the virtual camera, by name rather than by guess."""
    from pygrabber.dshow_graph import FilterGraph

    names = FilterGraph().get_input_devices()
    for i, name in enumerate(names):
        if "obs virtual camera" in name.lower() or "unitycapture" in name.lower():
            return i, name
    raise RuntimeError(f"no virtual camera among {names}")


def capture(index: int, count: int, warmup: int = 10,
            size: tuple[int, int] | None = None) -> list[np.ndarray]:
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise RuntimeError(f"could not open capture device {index}")
    if size is not None:
        # The consumer negotiates the format, not the sender. OpenCV's
        # DirectShow capture asks for 640x480 unless told otherwise, so without
        # this the frames come back downscaled and it looks as though the tool
        # is sending the wrong size.
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
    try:
        for _ in range(warmup):  # the driver hands out stale frames at first
            cap.read()
        frames = []
        while len(frames) < count:
            ok, frame = cap.read()
            if ok and frame is not None:
                frames.append(frame)
        return frames
    finally:
        cap.release()


def held_share(frames: list[np.ndarray]) -> float:
    if len(frames) < 2:
        return 0.0
    held = sum(
        int(float(np.mean(cv2.absdiff(a, b))) < HELD_THRESHOLD)
        for a, b in zip(frames, frames[1:])
        if a.shape == b.shape
    )
    return held / (len(frames) - 1)


def min_self_distance(frames: list[np.ndarray], gap: int = 15) -> float:
    """Closest match between any two frames at least `gap` apart.

    Near zero means the feed revisited a moment it had already shown, which is
    what a loop does and a live feed does not.

    Comparing only against the first frame does not work across loop styles. A
    crossfade loop wraps, so frame 0 comes round every ~45 frames; a bounce
    turns around instead, giving a cycle of 2*(n-1) frames, and inside a
    60-frame window frame 0 never returns even though the loop is plainly
    repeating itself. Asking whether *any* moment recurs covers both.

    Frames are shrunk first: this is quadratic in the frame count, and full
    720p comparisons take longer than the capture did.
    """
    small = [cv2.resize(f, (160, 90)) for f in frames]
    best = float("inf")
    for i, a in enumerate(small):
        for b in small[i + gap:]:
            best = min(best, float(np.mean(cv2.absdiff(a, b))))
    return best


def detail(frame: np.ndarray) -> float:
    return float(cv2.Laplacian(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


def main() -> int:
    video = require_running()
    if video is None:
        return 1
    if not video["virtual_camera"]:
        print("the app has no virtual camera:", video["error"])
        return 1

    index, name = find_virtual_camera()
    with source("pattern"):
        return measure(index, name, video)


def measure(index: int, name: str, video: dict) -> int:
    print(f"\napp is sending to: {video['virtual_camera']}")
    print(f"reading back from: [{index}] {name}\n")

    preset("original")
    clean = capture(index, 40, size=(1280, 720))
    clean_held = held_share(clean)
    clean_detail = float(np.median([detail(f) for f in clean]))
    h, w = clean[0].shape[:2]
    black = float(np.median([f.mean() for f in clean]))
    print(f"  clean line     {w}x{h}  held {clean_held:6.3f}  detail {clean_detail:8.1f}  "
          f"mean level {black:5.1f}")

    # Explicit settings, not a preset. train-tunnel stalls about 14 times a
    # minute, so whether a stall lands inside a 40-frame window is a coin toss
    # and the test would fail on the dice rather than on the code.
    # Held frames are counted over three seconds, not one. How many stalls fall
    # inside the window is a dice roll, and over 40 frames it came up short
    # about one run in six even with the line pinned this low.
    patch({"link": {"enabled": True, "quality": 4.0, "drift": 3.0,
                    "stall_rate": 120.0, "stall_min": 0.4, "stall_max": 0.9,
                    "latency": 0.0, "desync": 0.0}})
    wait_link_below(20.0)
    bad = capture(index, 90, size=(1280, 720))
    bad_held = held_share(bad)
    bad_detail = float(np.median([detail(f) for f in bad]))
    print(f"  degraded line  {w}x{h}  held {bad_held:6.3f}  detail {bad_detail:8.1f}")

    preset("original")

    # -- the pedal, judged from the consumer side ---------------------
    # The window has to cover a whole loop cycle, and a bounce cycle is twice
    # the recording: a 36-frame clip turns at each end, so it repeats every
    # 2*(36-1) = 70 frames. Capturing 60 of a 116-frame cycle can miss both
    # turns entirely and report that a plainly looping feed never repeats.
    # 1.2 s recorded, 90 captured, covers a full cycle whatever the phase.
    #
    # The pattern scrolls 9 px a frame and takes about 142 frames to come back
    # round, so a live feed still never repeats inside the same window.
    pedal("clear")
    live_repeat = min_self_distance(capture(index, 90, size=(1280, 720)))

    pedal("record-start")
    time.sleep(1.2)
    pedal("record-stop")
    time.sleep(1.0)
    loop_repeat = min_self_distance(capture(index, 90, size=(1280, 720)))
    pedal("live")

    print(f"\n  pedal seen from the far end: live closest repeat {live_repeat:7.2f}, "
          f"looping closest repeat {loop_repeat:7.2f}")

    print()
    checks = [
        ("the virtual camera opens from another process", len(clean) == 40),
        ("it hands out the resolution the consumer asks for", (w, h) == (1280, 720)),
        ("it is our feed, not an empty OBS scene", black > 20.0 and clean_detail > 500),
        ("a clean line arrives moving, not frozen", clean_held < 0.25),
        # Held frames are NOT asserted on, for the same reason they are not in
        # test_video_path: the consumer negotiates its own frame rate, so a run
        # of held frames can be sampled as one and the share swings run to run.
        # It is printed because it is informative, and judged on nothing.
        # Edge detail is the stable measure, and the gap is a factor of 25.
        ("degradation reaches the far end", bad_detail < clean_detail * 0.5),
        ("a live feed never repeats itself at the far end", live_repeat > 10.0),
        ("a loop does repeat itself at the far end", loop_repeat < live_repeat / 3),
    ]
    failures = 0
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        failures += 0 if ok else 1
    print(f"\n{failures} failure(s)")

    out = Path(__file__).resolve().parent.parent / "docs"
    out.mkdir(exist_ok=True)
    cv2.imwrite(str(out / "virtualcam-clean.jpg"), clean[0])
    cv2.imwrite(str(out / "virtualcam-degraded.jpg"), min(bad, key=detail))
    print(f"sample frames written to {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
