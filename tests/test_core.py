"""Core logic tests. No camera, no audio device, no virtual cable.

Everything that decides what the far end sees or hears is exercised here, so a
regression shows up without having to set a call up.

    python -m pytest tests -q
    python tests/test_core.py        # same checks, no pytest needed
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.audiofx import AudioDegrader, JitterBuffer  # noqa: E402
from src.config import LinkSettings, Settings, SettingsStore  # noqa: E402
from src.looper import Looper, State  # noqa: E402
from src.state import IDLE, LinkSimulator, LinkSnapshot  # noqa: E402
from src.videofx import VideoDegrader  # noqa: E402


def frame(seed: int = 0, w: int = 320, h: int = 180) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, size=(h, w, 3), dtype=np.uint8)


def snapshot(**kw) -> LinkSnapshot:
    base = dict(
        quality=100.0, severity=0.0, stalled=False, stall_elapsed=0.0,
        latency=0.0, desync=0.0, enabled=True, at=0.0,
    )
    base.update(kw)
    return LinkSnapshot(**base)


# -- settings -----------------------------------------------------------


def test_patch_is_partial_and_ignores_unknown_fields():
    store = SettingsStore()
    store.patch({"link": {"quality": 40.0, "nonsense": 1}, "bogus": {"x": 1}})
    s = store.get()
    assert s.link.quality == 40.0
    assert s.link.drift == LinkSettings().drift, "untouched fields must survive a patch"
    assert not hasattr(s.link, "nonsense")


def test_preset_changes_only_its_own_section():
    store = SettingsStore()
    store.patch({"video": {"fps": 60}})
    store.apply_preset("bad-wifi")
    s = store.get()
    assert s.link.enabled and s.link.quality < 60
    assert s.video.fps == 60, "a link preset must not reach into the video settings"


# -- the link -----------------------------------------------------------


def test_link_is_inert_while_disabled():
    store = SettingsStore()
    sim = LinkSimulator(store)
    snap = sim._advance(store.get().link, 0.01, 1.0)
    assert snap.quality == 100.0 and not snap.stalled and snap.severity == 0.0


def test_link_stalls_and_wanders_when_enabled():
    store = SettingsStore(Settings(link=LinkSettings(
        enabled=True, quality=50.0, drift=10.0, stall_rate=60.0,
        stall_min=0.05, stall_max=0.1, seed=7,
    )))
    sim = LinkSimulator(store)
    cfg = store.get().link

    seen_stall = False
    levels = []
    now = 0.0
    for _ in range(600):
        now += 0.01
        snap = sim._advance(cfg, 0.01, now)
        seen_stall |= snap.stalled
        if not snap.stalled:
            levels.append(snap.quality)

    assert seen_stall, "60 stalls a minute over 6 s must produce at least one"
    assert np.std(levels) > 0.5, "quality must wander, not sit on the set point"
    assert 0.0 <= min(levels) and max(levels) <= 100.0


def test_link_is_reproducible_for_a_given_seed():
    def run():
        store = SettingsStore(Settings(link=LinkSettings(
            enabled=True, quality=40.0, drift=12.0, stall_rate=30.0, seed=99)))
        sim = LinkSimulator(store)
        cfg = store.get().link
        return [round(sim._advance(cfg, 0.01, i * 0.01).quality, 6) for i in range(1, 200)]

    assert run() == run()


def test_link_thread_publishes_snapshots():
    store = SettingsStore(Settings(link=LinkSettings(enabled=True, quality=30.0, drift=20.0)))
    sim = LinkSimulator(store, tick=0.005)
    sim.start()
    try:
        time.sleep(0.2)
        snap = sim.get()
        assert snap is not IDLE and snap.enabled
    finally:
        sim.stop()


# -- video --------------------------------------------------------------


def test_clean_link_passes_the_frame_through_untouched():
    deg = VideoDegrader(seed=1)
    src = frame(1)
    out = deg.apply(src, snapshot(enabled=False), Settings().video)
    assert np.array_equal(out, src)


def test_degradation_changes_the_picture():
    deg = VideoDegrader(seed=1)
    cfg = Settings().video
    src = frame(2)
    deg.apply(src, snapshot(severity=0.0, quality=100.0), cfg)  # prime the previous frame
    out = deg.apply(src, snapshot(severity=0.95, quality=5.0), cfg)
    assert out.shape == src.shape
    assert not np.array_equal(out, src)


def test_stall_smears_rather_than_holding_a_clean_frame():
    deg = VideoDegrader(seed=3)
    cfg = Settings().video
    src = frame(4)
    deg.apply(src, snapshot(), cfg)
    stalled = deg.apply(frame(5), snapshot(stalled=True, severity=1.0, quality=0.0), cfg)
    assert stalled.shape == src.shape
    assert not np.array_equal(stalled, frame(5)), "a stall must not show the new frame"


def test_every_video_effect_runs_on_its_own():
    """Each effect called directly, because the chain hides most of them.

    At high severity the dropped-frame check fires first and returns the held
    frame, so a crash in banding, tearing or resolution never showed up in a
    whole-chain test. That is exactly how the banding call shipped broken.
    """
    cfg = Settings().video
    src = frame(11)
    for name in ("_resolution", "_blockiness", "_banding", "_tearing"):
        deg = VideoDegrader(seed=5)
        deg.apply(src, snapshot(), cfg)  # give _tearing a previous frame
        out = getattr(deg, name)(src, 1.0, 1.0)
        assert out is not None, f"{name} returned nothing"
        assert out.shape == src.shape, f"{name} changed the frame shape"
        assert out.dtype == src.dtype, f"{name} changed the frame dtype"


def test_banding_quantises_without_darkening():
    deg = VideoDegrader(seed=5)
    flat = np.full((16, 16, 3), 200, dtype=np.uint8)
    out = deg._banding(flat, 1.0, 1.0)
    assert len(np.unique(out)) <= 2, "banding must collapse levels"
    assert abs(int(out.mean()) - 200) < 32, "banding must not pull the picture dark"


def test_effect_weights_of_zero_disable_their_effect():
    from dataclasses import replace

    cfg = replace(
        Settings().video,
        drop_weight=0.0, blockiness_weight=0.0, resolution_weight=0.0,
        banding_weight=0.0, tearing_weight=0.0,
    )
    deg = VideoDegrader(seed=9)
    src = frame(6)
    deg.apply(src, snapshot(), cfg)
    out = deg.apply(src, snapshot(severity=1.0, quality=0.0), cfg)
    assert np.array_equal(out, src), "every weight at zero must be a passthrough"


# -- audio --------------------------------------------------------------


def test_clean_link_passes_audio_through():
    deg = AudioDegrader()
    block = np.sin(np.linspace(0, 20, 480)).astype(np.float32)
    out = deg.apply(block, snapshot(enabled=False), Settings().audio)
    assert np.allclose(out, block, atol=1e-6)


def test_a_settled_stall_produces_silence():
    deg = AudioDegrader()
    cfg = Settings().audio
    block = np.sin(np.linspace(0, 20, 480)).astype(np.float32)
    deg.apply(block, snapshot(), cfg)
    out = block
    for _ in range(6):  # past the 150 ms concealment window, plus the gain ramp
        out = deg.apply(block, snapshot(stalled=True, severity=1.0, stall_elapsed=0.4), cfg)
    assert np.abs(out).max() < 1e-4


def test_concealment_repeats_the_last_block_then_fades():
    deg = AudioDegrader()
    block = np.ones(480, dtype=np.float32) * 0.5
    deg._conceal = block
    first = deg._concealed(480, Settings().audio)
    assert np.abs(first).max() > 0.3, "the first concealed block is near full level"
    for _ in range(20):
        last = deg._concealed(480, Settings().audio)
    assert np.abs(last).max() == 0.0, "concealment must give up rather than loop forever"


def test_output_never_clips():
    deg = AudioDegrader()
    cfg = Settings().audio
    loud = np.ones(480, dtype=np.float32)
    for _ in range(50):
        out = deg.apply(loud, snapshot(severity=0.9, quality=10.0), cfg)
        assert out.max() <= 1.0 and out.min() >= -1.0


def test_gain_ramp_avoids_a_step_at_the_block_edge():
    deg = AudioDegrader()
    cfg = Settings().audio
    block = np.ones(480, dtype=np.float32) * 0.5
    deg.apply(block, snapshot(), cfg)
    out = deg.apply(block, snapshot(stalled=True, severity=1.0, stall_elapsed=0.4), cfg)
    assert abs(float(out[0])) > abs(float(out[-1])), "the gain change must be spread, not stepped"


def test_jitter_buffer_delays_by_the_requested_depth():
    buf = JitterBuffer(blocksize=4)
    blocks = [np.full(4, i, dtype=np.float32) for i in range(10)]
    out = [buf.push_pop(b, 3) for b in blocks]
    assert float(out[0][0]) == 0.0, "the first pops are the padding the buffer filled with"
    assert float(out[-1][0]) == 6.0, "a depth of 3 must lag the input by 3 blocks"


# -- looper -------------------------------------------------------------


def test_recording_too_short_refuses_to_become_a_loop():
    lp = Looper(fps=30)
    lp.start_record(max_seconds=5)
    for i in range(5):
        lp.process(frame(i, 64, 36))
    assert lp.stop_record(min_seconds=1.0, crossfade=0.2) is False
    assert lp.state is State.LIVE


def test_a_loop_is_built_sealed_and_played_back():
    lp = Looper(fps=30)
    lp.start_record(max_seconds=5)
    for i in range(60):
        lp.process(frame(i, 64, 36))
    assert lp.stop_record(min_seconds=1.0, crossfade=0.2) is True
    assert lp.state is State.LOOP

    fade = int(0.2 * 30)
    assert lp.status()["loop_frames"] == 60 - fade, "the faded tail is folded in, not kept"

    live = frame(999, 64, 36)
    seen = [lp.process(live) for _ in range(90)]
    assert all(f is not None and f.shape == live.shape for f in seen)


def test_ring_buffer_keeps_the_newest_frames_only():
    lp = Looper(fps=10)
    lp.start_record(max_seconds=1.0)  # ten frames
    for i in range(40):
        lp.process(frame(i, 32, 18))
    assert lp.status()["recorded"] == 10


def test_going_live_dissolves_and_then_settles_on_live():
    lp = Looper(fps=30)
    lp.start_record(max_seconds=5)
    for i in range(60):
        lp.process(frame(i, 64, 36))
    lp.stop_record(min_seconds=1.0, crossfade=0.2)

    live = frame(1234, 64, 36)
    for _ in range(30):
        lp.process(live)
    lp.go_live(crossfade=0.2)
    for _ in range(int(0.2 * 30) + 2):
        out = lp.process(live)
    assert lp.state is State.LIVE
    assert np.array_equal(out, live), "once the dissolve ends the live frame goes out untouched"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL {name}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ERR  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
