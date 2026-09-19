"""The in-browser A/B recorder, checked the way the browser uses it.

A tone is played on the speakers so there is something to record, the capture
is started over the API, and both WAVs are fetched and measured. "before" must
be the clean microphone and "after" must carry the degradation.

Run it against an app started with `--mic "Stereo Mix"`, so the tone reaches
the chain without needing anyone to speak.
"""

from __future__ import annotations

import io
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import API, patch, preset  # noqa: E402
from src.audio import find_device  # noqa: E402

TONE_HZ = 440.0
RATE = 48000
SECONDS = 4.0


def play_tone(device: int, seconds: float) -> None:
    t = np.arange(int(RATE * seconds), dtype=np.float32) / RATE
    tone = (0.25 * np.sin(2 * np.pi * TONE_HZ * t)).astype(np.float32)
    with sd.OutputStream(samplerate=RATE, channels=2, dtype="float32", device=device) as out:
        out.write(np.column_stack([tone, tone]))


def fetch_wav(side: str) -> tuple[np.ndarray, int]:
    res = requests.get(f"{API}/api/audio-test/{side}.wav", timeout=10)
    res.raise_for_status()
    with wave.open(io.BytesIO(res.content), "rb") as wav:
        assert wav.getnchannels() == 1, "the players expect mono"
        assert wav.getsampwidth() == 2, "the players expect 16-bit"
        rate = wav.getframerate()
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    return pcm.astype(np.float32) / 32768.0, rate


def silence_share(signal: np.ndarray, block: int = 480) -> float:
    n = (len(signal) // block) * block
    if n == 0:
        return 1.0
    peaks = np.abs(signal[:n].reshape(-1, block)).max(axis=1)
    return float(np.mean(peaks < 0.005))


def record_round(speaker: int) -> tuple[np.ndarray, np.ndarray, int]:
    started = requests.post(f"{API}/api/audio-test/start",
                            json={"seconds": SECONDS}, timeout=5).json()
    assert started.get("ok"), started

    tone = threading.Thread(target=play_tone, args=(speaker, SECONDS))
    tone.start()

    for _ in range(int(SECONDS * 10) + 40):
        if requests.get(f"{API}/api/audio-test/status", timeout=5).json()["ready"]:
            break
        time.sleep(0.1)
    tone.join()

    before, rate = fetch_wav("before")
    after, _ = fetch_wav("after")
    return before, after, rate


def main() -> int:
    if not requests.get(f"{API}/api/state", timeout=5).json()["status"]["audio"]["running"]:
        print("the audio chain is not running")
        return 1
    audio_in = requests.get(f"{API}/api/state", timeout=5).json()["status"]["audio"]["input"]
    if "stereo mix" not in audio_in.lower():
        print(f'input is {audio_in}, restart with --mic "Stereo Mix"')
        return 1

    speaker, _ = find_device("Speakers", "output")
    print(f"\nplaying {TONE_HZ:.0f} Hz on device {speaker}\n")

    preset("perfect")
    clean_before, clean_after, rate = record_round(speaker)
    print(f"  clean line   before peak {np.abs(clean_before).max():6.4f} "
          f"silent {silence_share(clean_before):5.3f}   "
          f"after peak {np.abs(clean_after).max():6.4f} silent {silence_share(clean_after):5.3f}")

    patch({"link": {"enabled": True, "quality": 4.0, "drift": 3.0,
                    "stall_rate": 90.0, "stall_min": 0.8, "stall_max": 1.2,
                    "latency": 0.0, "desync": 0.0}})
    time.sleep(0.5)
    bad_before, bad_after, _ = record_round(speaker)
    print(f"  bad line     before peak {np.abs(bad_before).max():6.4f} "
          f"silent {silence_share(bad_before):5.3f}   "
          f"after peak {np.abs(bad_after).max():6.4f} silent {silence_share(bad_after):5.3f}")
    preset("perfect")

    expected = int(SECONDS * rate)
    print()
    checks = [
        ("the WAV is the length that was asked for", abs(len(clean_before) - expected) < rate * 0.2),
        ("both sides come back", len(clean_after) == len(clean_before)),
        ("before is the microphone, and it is not silent", np.abs(clean_before).max() > 0.02),
        ("a clean line leaves after untouched",
         abs(silence_share(clean_after) - silence_share(clean_before)) < 0.05),
        # Not an absolute threshold. The capture starts a moment before the
        # tone and ends after it, so perhaps 15 percent of any recording is
        # lead-in and lead-out. What matters is that the raw side is the same
        # whether the line is clean or wrecked.
        ("the line never touches the before side",
         abs(silence_share(bad_before) - silence_share(clean_before)) < 0.08),
        ("after carries the degradation", silence_share(bad_after) > silence_share(bad_before) + 0.3),
    ]
    failures = 0
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        failures += 0 if ok else 1
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
