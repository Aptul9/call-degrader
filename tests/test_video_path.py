"""Proof that the video chain does what it claims, against the real camera.

Frames are pulled from the running app's preview stream, which is the same
image the virtual camera is fed, and measured. Run it against an app already
started with `python run.py`.

    python tests/test_video_path.py
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

BOUNDARY = b"--frame"


def grab(count: int, timeout: float = 20.0) -> list[np.ndarray]:
    """Pull `count` JPEG parts off the MJPEG stream and decode them."""
    frames: list[np.ndarray] = []
    buf = b""
    deadline = time.time() + timeout
    with requests.get(f"{API}/preview.mjpg", stream=True, timeout=timeout) as res:
        for chunk in res.iter_content(chunk_size=16384):
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2)
                if start < 0 or end < 0:
                    break
                img = cv2.imdecode(np.frombuffer(buf[start : end + 2], np.uint8), cv2.IMREAD_COLOR)
                buf = buf[end + 2 :]
                if img is not None:
                    frames.append(img)
            if len(frames) >= count or time.time() > deadline:
                break
    return frames[:count]


# A dropped frame is the previous output shown again with a small decay
# applied, so its difference from its predecessor is roughly the decay and
# nothing else. Two genuinely different frames of the scrolling pattern differ
# by tens. Anything under this is a repeat.
HELD_THRESHOLD = 2.0


def held_share(frames: list[np.ndarray]) -> float:
    """Share of consecutive pairs that are all but identical, i.e. held."""
    if len(frames) < 2:
        return 0.0
    held = 0
    for a, b in zip(frames, frames[1:]):
        if a.shape != b.shape:
            continue
        held += int(float(np.mean(cv2.absdiff(a, b))) < HELD_THRESHOLD)
    return held / (len(frames) - 1)


# Dropped frames are NOT asserted on here. The preview stream skips on purpose,
# sleeping 1/30 s between parts while the pipeline also runs at 30 fps, so a run
# of held frames can be sampled as a single one and the share measured off this
# stream swings between 0.03 and 0.33 for identical settings. That assertion
# lives in test_virtual_camera.py, which reads a real capture device.


def detail(frame: np.ndarray) -> float:
    """Edge energy. Compression and resolution loss both flatten it."""
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(grey, cv2.CV_64F).var())


def main() -> int:
    video = require_running()
    if video is None:
        return 1
    print(f"\ncamera {video['camera']} on {video['backend']}, {video['fps']} fps")
    print(f"virtual camera: {video['virtual_camera'] or 'not available'}")

    if video["backend"] != "pattern":
        live = grab(8)
        live_detail = float(np.median([detail(f) for f in live])) if live else 0.0
        note = "" if live_detail > 40 else "   (dark or covered lens, effects measured on the pattern)"
        print(f"real camera detail {live_detail:.1f}{note}")

    # The effects are measured against the generated pattern, not the camera. A
    # covered lens or a dark room gives an almost constant frame, and against
    # that a dropped frame and a delivered one are indistinguishable.
    with source("pattern"):
        return measure()


def measure() -> int:
    print()
    preset("original")
    clean = grab(40)
    clean_held = held_share(clean)
    clean_detail = float(np.median([detail(f) for f in clean]))
    print(f"  clean line     frames {len(clean):>3}   held {clean_held:6.3f}   "
          f"detail {clean_detail:8.1f}")

    # Explicit, not a preset. train-tunnel is stochastic: Poisson stalls on top
    # of a random walk, measured over a 1.3 s window. It lands on a good moment
    # often enough that the assertion failed roughly one run in three, with
    # detail coming back at 1995 against the usual 150.
    patch({"link": {"enabled": True, "quality": 4.0, "ceiling": 12.0, "floor": 0.0,
                    "drift": 3.0, "stall_rate": 120.0, "stall_min": 0.4, "stall_max": 0.9,
                    "latency": 0.0, "desync": 0.0}})
    wait_link_below(20.0)
    bad = grab(60)
    bad_held = held_share(bad)
    bad_detail = float(np.median([detail(f) for f in bad]))
    print(f"  degraded line  frames {len(bad):>3}   held {bad_held:6.3f}   "
          f"detail {bad_detail:8.1f}")

    preset("original")

    # -- the pedal, on the live chain ---------------------------------
    pedal("clear")
    pedal("record-start")
    time.sleep(2.0)
    recorded = pedal("record-stop")
    time.sleep(0.5)
    looping = require_running()["pedal"]
    loop_frames = grab(30)
    pedal("live")
    time.sleep(0.8)
    after = require_running()["pedal"]
    print(f"\n  pedal: recorded {recorded['pedal']['recorded']} frames, "
          f"loop {looping['loop_frames']} frames, state while looping {looping['state']!r}, "
          f"after going live {after['state']!r}")

    print()
    checks = [
        ("preview delivers frames", len(clean) >= 30),
        ("a clean line does not hold frames", clean_held < 0.25),
        ("degradation flattens detail", bad_detail < clean_detail * 0.9),
        ("holding the pedal records", recorded["pedal"]["recorded"] > 30),
        ("release builds a loop", recorded["result"] is True and looping["loop_frames"] > 0),
        ("the loop plays", looping["state"] == "loop" and len(loop_frames) >= 20),
        ("going live ends the loop", after["state"] == "live"),
    ]
    failures = 0
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        failures += 0 if ok else 1
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
