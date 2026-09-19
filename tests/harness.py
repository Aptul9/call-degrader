"""Shared helpers for the tests that drive a running app.

These tests all change the same app: the source, the presets, the pedal. Left
as they were written, each one hands the next a different app than it expected,
and the failure shows up in whichever test happens to run second. Every one of
them therefore sets up what it needs and puts back what it found.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

import requests

API = "http://127.0.0.1:8720"


def state() -> dict:
    return requests.get(f"{API}/api/state", timeout=5).json()


def status() -> dict:
    return state()["status"]


def patch(changes: dict) -> None:
    requests.post(f"{API}/api/settings", json=changes, timeout=5).raise_for_status()


def preset(name: str, settle: float = 0.8) -> None:
    requests.post(f"{API}/api/preset/{name}", timeout=5).raise_for_status()
    time.sleep(settle)


def pedal(action: str) -> dict:
    res = requests.post(f"{API}/api/pedal/{action}", timeout=5)
    res.raise_for_status()
    return res.json()


@contextmanager
def source(kind: str):
    """Run the block with the video source set to `kind`, then put it back.

    A source change restarts the video chain, so it needs time to settle before
    anything is measured and again after it is handed back.
    """
    original = state()["settings"]["video"]["source"]
    changed = original != kind
    if changed:
        patch({"video": {"source": kind}})
        time.sleep(1.5)
    try:
        yield
    finally:
        preset("perfect", settle=0.2)
        if changed:
            patch({"video": {"source": original}})
            time.sleep(1.5)


def wait_ready(timeout: float = 20.0) -> bool:
    """Block until the video chain is up and producing frames.

    A source change restarts the chain, so a test started immediately after
    another one finished can find it mid-restart and measure a dead pipeline.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            video = status()["video"]
            if video["running"] and video["fps"] > 1.0:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def wait_audio_ready(timeout: float = 20.0) -> bool:
    """Block until the audio chain is up and its underrun count has settled.

    The output stream starts before the input has produced anything, so the
    first few blocks always underrun. A suite that starts measuring during that
    window reads a chain that is not yet steady.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            audio = status()["audio"]
            if audio["running"]:
                if last is not None and audio["underruns"] == last:
                    return True
                last = audio["underruns"]
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def wait_link_below(quality: float, timeout: float = 10.0) -> bool:
    """Block until the reported line quality is at or under `quality`.

    Applying a preset does not move the line instantly. The level is an
    Ornstein-Uhlenbeck walk, so it travels to the new set point rather than
    jumping, and a measurement started too early catches it on the way down.
    Measured from perfect to train-tunnel, it takes about 0.3 s.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            link = status()["link"]
            if link["enabled"] and link["quality"] <= quality:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.2)
    return False


def require_running() -> dict | None:
    """Return the video status, or None after printing why the test cannot run."""
    video = status()["video"]
    if not video["running"]:
        print("video chain is not running:", video["error"])
        return None
    return video
