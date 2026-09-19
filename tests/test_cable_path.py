"""End-to-end proof that audio really reaches the far end of the virtual cable.

Nothing here is mocked. A tone is played on the speakers, the running app picks
it up through Stereo Mix, degrades it and writes it to CABLE Input, and this
script records CABLE Output, which is the device a call application would be
using as its microphone. What comes back is measured, not assumed.

Run it against an app already started with:

    python run.py --mic "Stereo Mix"
    python tests/test_cable_path.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.audio import CABLE_CAPTURE_APIS, find_device  # noqa: E402

API = "http://127.0.0.1:8720"
TONE_HZ = 440.0
RATE = 48000
SECONDS = 7.0


# sd.play and sd.rec are convenience wrappers over one module-global stream.
# Calling them from two threads makes the second tear down the first one's
# stream while its callback is still running, which segfaults rather than
# raising. Explicit stream objects have no shared state and are what a test
# that plays and records at the same time has to use.


def play_tone(device: int, seconds: float) -> None:
    t = np.arange(int(RATE * seconds), dtype=np.float32) / RATE
    tone = (0.25 * np.sin(2 * np.pi * TONE_HZ * t)).astype(np.float32)
    frames = np.column_stack([tone, tone])
    with sd.OutputStream(samplerate=RATE, channels=2, dtype="float32", device=device) as out:
        out.write(frames)


def record(device: int, seconds: float) -> np.ndarray:
    # Two channels, never one. The DirectSound CABLE Output device declares 16
    # and opening it mono is one of the configurations that segfaults PortAudio.
    chunks: list[np.ndarray] = []

    def on_block(indata, frames, time_info, status):
        chunks.append(indata[:, 0].copy())

    with sd.InputStream(samplerate=RATE, channels=2, dtype="float32",
                        device=device, callback=on_block):
        time.sleep(seconds)
    return np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)


def steady(signal: np.ndarray) -> np.ndarray:
    """The middle 60 percent, which is inside the tone on every run.

    The recorder is started before the tone and stopped after it, so the ends
    are silence by construction and would otherwise be counted as dropouts.
    """
    n = len(signal)
    return signal[int(n * 0.2) : int(n * 0.8)] if n > 10 else signal


def dominant_hz(signal: np.ndarray) -> float:
    """Loudest frequency present. A band share needs a threshold pulled from
    nowhere; asking which frequency won does not."""
    if signal.size < 64 or not np.any(signal):
        return 0.0
    spectrum = np.abs(np.fft.rfft(signal * np.hanning(len(signal))))
    freqs = np.fft.rfftfreq(len(signal), 1 / RATE)
    return float(freqs[int(np.argmax(spectrum))])


def silence_share(signal: np.ndarray, block: int = 480) -> float:
    """Share of 10 ms blocks that are effectively silent."""
    n = (len(signal) // block) * block
    if n == 0:
        return 1.0
    blocks = signal[:n].reshape(-1, block)
    peaks = np.abs(blocks).max(axis=1)
    return float(np.mean(peaks < 0.005))


def measure(label: str, speaker: int, cable_out: int) -> tuple[float, float, float]:
    captured: list[np.ndarray] = []
    rec = threading.Thread(target=lambda: captured.append(record(cable_out, SECONDS)))
    rec.start()
    time.sleep(0.2)  # let the recorder settle before the tone starts
    play_tone(speaker, SECONDS - 0.4)
    rec.join()

    signal = steady(captured[0])
    peak = float(np.abs(signal).max())
    hz = dominant_hz(signal)
    quiet = silence_share(signal)
    print(f"  {label:<14} peak {peak:6.4f}   loudest {hz:7.1f} Hz   silent blocks {quiet:6.3f}")
    return peak, hz, quiet


def main() -> int:
    status = requests.get(f"{API}/api/state", timeout=5).json()["status"]
    if not status["audio"]["running"]:
        print("audio chain is not running:", status["audio"]["error"])
        return 1
    if "stereo mix" not in status["audio"]["input"].lower():
        print(f"input is {status['audio']['input']}, restart with --mic \"Stereo Mix\"")
        return 1

    speaker, _ = find_device("Speakers", "output")
    cable_out, cable_dev = find_device("CABLE Output", "input", apis=CABLE_CAPTURE_APIS)

    print(f"\nplaying {TONE_HZ:.0f} Hz on device {speaker}, "
          f"recording device {cable_out} ({cable_dev['name'].strip()})\n")

    requests.post(f"{API}/api/preset/perfect", timeout=5).raise_for_status()
    time.sleep(0.5)
    clean_peak, clean_hz, clean_quiet = measure("clean line", speaker, cable_out)
    if clean_peak < 0.01:
        # Nothing got in. Muted speakers, volume at zero, or Stereo Mix
        # disabled: every measurement below would read as silence and be
        # reported as the product failing, which it is not.
        print("\n  the tone never reached the cable: the speakers are muted, the volume")
        print("  is at zero, or Stereo Mix is disabled.")
        print("\nskipped: nothing measurable until playback is audible")
        requests.post(f"{API}/api/preset/perfect", timeout=5)
        return 0

    # Not a preset. train-tunnel stalls about 14 times a minute, so over a
    # window this short whether a stall lands at all is a coin toss and the
    # test fails on the dice rather than on the code. These numbers guarantee
    # a stall duty cycle over half, and silence only begins 150 ms into a
    # stall, so the share that comes back silent settles around 0.5.
    requests.post(f"{API}/api/settings", timeout=5, json={"link": {
        "enabled": True, "quality": 4.0, "drift": 3.0,
        "stall_rate": 90.0, "stall_min": 0.8, "stall_max": 1.2,
        "latency": 0.0, "desync": 0.0,
    }}).raise_for_status()
    time.sleep(0.5)
    bad_peak, _bad_hz, bad_quiet = measure("stalling line", speaker, cable_out)

    requests.post(f"{API}/api/preset/perfect", timeout=5).raise_for_status()

    print()
    checks = [
        ("tone reaches the far end of the cable", clean_peak > 0.02),
        ("what arrives is the tone that was sent", abs(clean_hz - TONE_HZ) < 20),
        ("a clean line stays open", clean_quiet < 0.05),
        ("degradation cuts the line up", bad_quiet > clean_quiet + 0.3),
        ("degradation does not simply mute", bad_peak > 0.005),
    ]
    failures = 0
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        failures += 0 if ok else 1
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
