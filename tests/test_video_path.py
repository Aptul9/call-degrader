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

API = "http://127.0.0.1:8720"
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


def detail(frame: np.ndarray) -> float:
    """Edge energy. Compression and resolution loss both flatten it."""
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(grey, cv2.CV_64F).var())


def preset(name: str) -> None:
    requests.post(f"{API}/api/preset/{name}", timeout=5).raise_for_status()
    time.sleep(0.6)


def source(kind: str) -> None:
    """Swap the camera for the generated pattern, or back.

    The effects are measured against the pattern, not the camera. A covered
    lens or a dark room produces an almost constant frame, and against that a
    dropped frame and a delivered one are indistinguishable, so the test would
    report a failure that says nothing about the code.
    """
    requests.post(f"{API}/api/settings", json={"video": {"source": kind}}, timeout=5).raise_for_status()
    time.sleep(1.5)  # the chain restarts on a source change


def pedal(action: str) -> dict:
    res = requests.post(f"{API}/api/pedal/{action}", timeout=5)
    res.raise_for_status()
    return res.json()


def main() -> int:
    status = requests.get(f"{API}/api/state", timeout=5).json()["status"]["video"]
    if not status["running"]:
        print("video chain is not running:", status["error"])
        return 1
    print(f"\ncamera {status['camera']} on {status['backend']}, {status['fps']} fps")
    print(f"virtual camera: {status['virtual_camera'] or 'not available'}")

    live = grab(8)
    live_detail = float(np.median([detail(f) for f in live])) if live else 0.0
    print(f"real camera detail {live_detail:.1f}"
          + ("" if live_detail > 40 else "   (dark or covered lens, effects measured on the pattern)"))

    source("pattern")
    print()

    preset("perfect")
    clean = grab(40)
    clean_held = held_share(clean)
    clean_detail = float(np.median([detail(f) for f in clean]))
    print(f"  clean line     frames {len(clean):>3}   held {clean_held:6.3f}   "
          f"detail {clean_detail:8.1f}")

    preset("train-tunnel")
    bad = grab(40)
    bad_held = held_share(bad)
    bad_detail = float(np.median([detail(f) for f in bad]))
    print(f"  train tunnel   frames {len(bad):>3}   held {bad_held:6.3f}   "
          f"detail {bad_detail:8.1f}")

    preset("perfect")

    # -- the pedal, on the live chain ---------------------------------
    pedal("clear")
    pedal("record-start")
    time.sleep(2.0)
    recorded = pedal("record-stop")
    time.sleep(0.5)
    looping = requests.get(f"{API}/api/state", timeout=5).json()["status"]["video"]["pedal"]
    loop_frames = grab(30)
    pedal("live")
    time.sleep(0.8)
    after = requests.get(f"{API}/api/state", timeout=5).json()["status"]["video"]["pedal"]
    source("camera")
    print(f"\n  pedal: recorded {recorded['pedal']['recorded']} frames, "
          f"loop {looping['loop_frames']} frames, state while looping {looping['state']!r}, "
          f"after going live {after['state']!r}")

    print()
    checks = [
        ("preview delivers frames", len(clean) >= 30),
        ("a clean line does not hold frames", clean_held < 0.25),
        ("degradation holds frames", bad_held > clean_held + 0.2),
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
