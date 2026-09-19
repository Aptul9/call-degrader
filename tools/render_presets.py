"""Render one speech take through the audio chain at every audio preset.

Reuses the exact code path the browser "re-apply" button uses: the stored take
is split into blocks and run through a fresh LinkSimulator + AudioDegrader +
JitterBuffer, same as AudioPipeline._render.

Writes one wav per preset plus a measurement table, so the by-ear pass has
something to compare against and the numbers say which weight to move.
"""

from __future__ import annotations

import json
import sys
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.audio import RENDER_SEED  # noqa: E402
from src.audiofx import AudioDegrader, JitterBuffer  # noqa: E402
from src.config import AUDIO_PRESETS, Settings, SettingsStore  # noqa: E402
from src.state import LinkSimulator  # noqa: E402


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as src:
        rate = src.getframerate()
        channels = src.getnchannels()
        width = src.getsampwidth()
        raw = src.readframes(src.getnframes())
    if width != 2:
        raise SystemExit(f"expected 16-bit wav, got {width * 8}-bit")
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm.tobytes())


def render(blocks: list[np.ndarray], cfg: Settings) -> np.ndarray:
    """Byte-for-byte the logic in AudioPipeline._render."""
    seed = cfg.link.seed or RENDER_SEED
    link_cfg = replace(cfg.link, seed=seed)
    store = SettingsStore(replace(cfg, link=link_cfg))
    sim = LinkSimulator(store)
    degrader = AudioDegrader(samplerate=cfg.audio.samplerate, seed=seed)
    jitter = JitterBuffer(blocksize=cfg.audio.blocksize)

    step = cfg.audio.blocksize / max(1, cfg.audio.samplerate)
    now = 0.0
    out_blocks = []
    for block in blocks:
        now += step
        snap = sim._advance(link_cfg, step, now)
        processed = degrader.apply(block, snap, cfg.audio)
        extra = snap.latency + max(0.0, snap.desync)
        depth = int(extra * cfg.audio.samplerate / max(1, cfg.audio.blocksize))
        out_blocks.append(jitter.push_pop(processed, depth))
    return np.concatenate(out_blocks) if out_blocks else np.zeros(1, dtype=np.float32)


def to_blocks(data: np.ndarray, blocksize: int) -> list[np.ndarray]:
    usable = (len(data) // blocksize) * blocksize
    return [data[i : i + blocksize].copy() for i in range(0, usable, blocksize)]


def measure(clean: np.ndarray, dirty: np.ndarray, rate: int, blocksize: int) -> dict:
    """Numbers that track what the ear is being asked to judge."""
    n = min(len(clean), len(dirty))
    a, b = clean[:n], dirty[:n]

    # Per-block level, on the blocks that carried speech in the original.
    ca = a.reshape(-1, blocksize) if n % blocksize == 0 else a[: (n // blocksize) * blocksize].reshape(-1, blocksize)
    cb = b[: ca.shape[0] * blocksize].reshape(-1, blocksize)
    voiced = np.sqrt((ca**2).mean(axis=1)) > 0.02

    # Share of originally-voiced blocks that came out effectively silent.
    silent = float((np.sqrt((cb[voiced] ** 2).mean(axis=1)) < 0.002).mean()) if voiced.any() else 0.0

    # Level held against the original, on voiced blocks only.
    keep = float(
        np.sqrt((cb[voiced] ** 2).mean()) / max(1e-9, np.sqrt((ca[voiced] ** 2).mean()))
    ) if voiced.any() else 0.0

    # High-frequency energy share. Bitcrush and comb both raise it, which is
    # what "metallic" and "harsh" mean in a number.
    def hf_share(x: np.ndarray) -> float:
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        freqs = np.fft.rfftfreq(len(x), 1.0 / rate)
        total = float((spec**2).sum())
        return float((spec[freqs > 4000] ** 2).sum() / max(1e-9, total))

    # Waveform correlation on voiced blocks: how much of the original shape
    # survives. 1.0 is untouched, 0 is unrelated.
    va, vb = ca[voiced].reshape(-1), cb[voiced].reshape(-1)
    corr = float(np.corrcoef(va, vb)[0, 1]) if len(va) > 1 else 0.0

    return {
        "silent_share": round(silent, 3),
        "level_kept": round(keep, 3),
        "hf_before": round(hf_share(a), 4),
        "hf_after": round(hf_share(b), 4),
        "waveform_corr": round(corr, 3),
        "peak": round(float(np.abs(b).max()), 3),
        "seconds": round(n / rate, 2),
    }


def main() -> None:
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    data, rate = read_wav(Path(sys.argv[1]))

    base = Settings()
    blocksize = base.audio.blocksize
    if rate != base.audio.samplerate:
        raise SystemExit(f"take is {rate} Hz, chain runs at {base.audio.samplerate} Hz")

    blocks = to_blocks(data, blocksize)
    clean = np.concatenate(blocks)
    write_wav(out_dir / "00-original.wav", clean, rate)

    rows = {}
    for index, name in enumerate(AUDIO_PRESETS, start=1):
        store = SettingsStore(Settings())
        cfg = store.apply_audio_preset(name)
        dirty = render(blocks, cfg)
        slug = name.replace(" ", "-")
        write_wav(out_dir / f"{index:02d}-{slug}.wav", dirty, rate)
        rows[name] = measure(clean, dirty, rate, blocksize)

    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
