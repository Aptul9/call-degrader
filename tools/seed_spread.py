"""How different are two runs of the same preset on the same words?

The A/B player renders with a fixed seed so two presses are comparable. The
live chain feeding the call is unseeded and has been walking for minutes. Both
are the same code, so the question is how much of what you hear is the preset
and how much is which draw you happened to get.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from render_presets import measure, read_wav, to_blocks  # noqa: E402
from src.audiofx import AudioDegrader, JitterBuffer  # noqa: E402
from src.config import Settings, SettingsStore  # noqa: E402
from src.state import LinkSimulator  # noqa: E402

SEEDS = [20260919, 7, 101, 555, 9001, 31337, 42, 12345]


def render_with(blocks, cfg, seed, warm_seconds=0.0):
    """One render. `warm_seconds` runs the link before the audio starts.

    That is the difference between the player and the call: the player builds
    a fresh simulator per press, the live one has been running.
    """
    link = replace(cfg.link, seed=seed)
    sim = LinkSimulator(SettingsStore(replace(cfg, link=link)))
    deg = AudioDegrader(samplerate=cfg.audio.samplerate, seed=seed)
    jit = JitterBuffer(blocksize=cfg.audio.blocksize)

    step = cfg.audio.blocksize / cfg.audio.samplerate
    now = 0.0
    for _ in range(int(warm_seconds / step)):
        now += step
        sim._advance(link, step, now)

    out = []
    for block in blocks:
        now += step
        snap = sim._advance(link, step, now)
        processed = deg.apply(block, snap, cfg.audio)
        extra = snap.latency + max(0.0, snap.desync)
        depth = int(extra * cfg.audio.samplerate / max(1, cfg.audio.blocksize))
        out.append(jit.push_pop(processed, depth))
    return np.concatenate(out)


data, rate = read_wav(Path(sys.argv[1]))
base = Settings()
blocks = to_blocks(data, base.audio.blocksize)
clean = np.concatenate(blocks)

for name in ("choppy", "robot", "underwater", "barely there"):
    cfg = SettingsStore(Settings()).apply_audio_preset(name)
    rows = []
    for seed in SEEDS:
        dirty = render_with(blocks, cfg, seed)
        m = measure(clean, dirty, rate, base.audio.blocksize)
        rows.append((m["waveform_corr"], m["silent_share"], m["level_kept"]))

    corr = [r[0] for r in rows]
    silent = [r[1] for r in rows]
    level = [r[2] for r in rows]
    print(f"\n{name}   across {len(SEEDS)} draws of the same words")
    print(f"  waveform kept  {min(corr):.3f} .. {max(corr):.3f}   "
          f"spread {max(corr) - min(corr):.3f}")
    print(f"  silence        {min(silent):.3f} .. {max(silent):.3f}   "
          f"spread {max(silent) - min(silent):.3f}")
    print(f"  level          {min(level):.3f} .. {max(level):.3f}")

    # And what the warm-up alone changes, holding the seed fixed.
    cold = measure(clean, render_with(blocks, cfg, SEEDS[0], 0.0), rate, base.audio.blocksize)
    warm = measure(clean, render_with(blocks, cfg, SEEDS[0], 60.0), rate, base.audio.blocksize)
    print(f"  fresh simulator vs one running 60 s, same seed: "
          f"corr {cold['waveform_corr']:.3f} against {warm['waveform_corr']:.3f}, "
          f"silence {cold['silent_share']:.3f} against {warm['silent_share']:.3f}")
