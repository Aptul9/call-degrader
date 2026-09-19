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

from harness import API, patch, preset, wait_link_below  # noqa: E402
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


def tone_reaches_the_chain(before: np.ndarray) -> bool:
    """Whether the injected tone actually got in.

    Both audio suites drive the chain by playing a tone on the speakers and
    letting Stereo Mix pick it up. Muted speakers, or a volume at zero, means
    nothing reaches the chain and every measurement reads as total silence.
    That is not the product failing, and reporting it as a failure sends
    whoever reads it hunting for a bug that is not there.
    """
    return float(np.abs(before).max()) > 0.01


def cannot_measure() -> int:
    print("\n  the tone never reached the chain: the speakers are muted, the volume")
    print("  is at zero, or Stereo Mix is disabled. Nothing about the product can")
    print("  be measured this way until playback is audible.")
    print("\nskipped: restart with --mic \"Stereo Mix\" and audible speakers")
    return 0


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


def check_hold_to_record(speaker: int) -> list[tuple[str, bool]]:
    """The held gesture: start with no duration, stop when the button comes up."""
    started = requests.post(f"{API}/api/audio-test/start", json={}, timeout=5).json()
    tone = threading.Thread(target=play_tone, args=(speaker, 1.5))
    tone.start()
    time.sleep(1.2)
    during = requests.get(f"{API}/api/audio-test/status", timeout=5).json()
    done = requests.post(f"{API}/api/audio-test/stop", timeout=5).json()
    tone.join()

    before, _ = fetch_wav("before")
    after, _ = fetch_wav("after")
    print(f"  held         {done['seconds']:.2f}s captured, mic peak {done['peak_in']:.4f}, "
          f"out peak {done['peak_out']:.4f}")
    return [
        ("a held start needs no duration", started.get("ok") is True),
        ("it reports whether the line is on", "link_on" in started),
        ("it is recording while held", during["recording"] is True),
        ("stopping ends it", done["recording"] is False and done["ready"] is True),
        ("it keeps roughly what was held for", 0.8 < done["seconds"] < 2.0),
        ("it reports the level the microphone gave", done["peak_in"] > 0.02),
        ("both sides come back", len(before) > 0 and len(after) > 0),
    ]


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
    if not tone_reaches_the_chain(clean_before):
        return cannot_measure()

    patch({"link": {"enabled": True, "quality": 4.0, "drift": 3.0,
                    "stall_rate": 90.0, "stall_min": 0.8, "stall_max": 1.2,
                    "latency": 0.0, "desync": 0.0}})
    wait_link_below(20.0)
    bad_before, bad_after, _ = record_round(speaker)
    print(f"  bad line     before peak {np.abs(bad_before).max():6.4f} "
          f"silent {silence_share(bad_before):5.3f}   "
          f"after peak {np.abs(bad_after).max():6.4f} silent {silence_share(bad_after):5.3f}")
    held_checks = check_hold_to_record(speaker)
    preset("perfect")

    expected = int(SECONDS * rate)
    print()
    checks = [
        ("the WAV is the length that was asked for", abs(len(clean_before) - expected) < rate * 0.2),
        ("both sides come back", len(clean_after) == len(clean_before)),
        ("before is the microphone, and it is not silent", np.abs(clean_before).max() > 0.02),
        ("a clean line leaves after untouched",
         abs(silence_share(clean_after) - silence_share(clean_before)) < 0.05),
        # Judged on level, not on silence share. The capture starts a moment
        # before the tone and ends after it, and how much of that lead-in lands
        # inside the window shifts every run: measured across runs, the before
        # side's silence share moved between 0.000 and 0.080 with nothing
        # changing in the code. Peak level is what actually answers the
        # question, and it holds steady at about 0.22 either way.
        ("the line never touches the before side",
         abs(float(np.abs(bad_before).max()) - float(np.abs(clean_before).max())) < 0.05),
        ("nor does it change how loud the before side is",
         abs(float(np.sqrt(np.mean(bad_before ** 2)))
             - float(np.sqrt(np.mean(clean_before ** 2)))) < 0.03),
        ("after carries the degradation", silence_share(bad_after) > silence_share(bad_before) + 0.3),
        *held_checks,
    ]
    failures = 0
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        failures += 0 if ok else 1
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
