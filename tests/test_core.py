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


def test_quality_never_rises_above_the_ceiling():
    """The point of the ceiling: a bad line recovers to tolerable, not to fine.

    Without it the walk eventually touches 100 and the far end reads the
    problem as having gone away.
    """
    store = SettingsStore(Settings(link=LinkSettings(
        enabled=True, quality=60.0, ceiling=65.0, floor=20.0,
        drift=30.0, stall_rate=0.0, seed=3,
    )))
    sim = LinkSimulator(store)
    cfg = store.get().link
    levels = [sim._advance(cfg, 0.01, i * 0.01).quality for i in range(1, 4000)]
    assert max(levels) <= 65.0 + 1e-6, f"ceiling breached: {max(levels)}"
    assert min(levels) >= 20.0 - 1e-6, f"floor breached: {min(levels)}"
    assert max(levels) > 60.0, "a wide drift should still reach up towards the ceiling"


def test_a_set_point_outside_the_band_is_pulled_inside_it():
    store = SettingsStore(Settings(link=LinkSettings(
        enabled=True, quality=95.0, ceiling=40.0, floor=10.0, drift=0.0, stall_rate=0.0,
    )))
    sim = LinkSimulator(store)
    snap = sim._advance(store.get().link, 0.01, 1.0)
    assert snap.quality == 40.0, "a set point above the ceiling settles on the ceiling"


def test_every_preset_sets_ceiling_and_floor():
    """A preset that omits them inherits the previous one's cap.

    Switching from a capped line back to `perfect` would otherwise keep the cap
    and the feed would never look clean again.
    """
    from src.config import PRESETS

    for name, preset in PRESETS.items():
        link = preset.get("link", {})
        assert "ceiling" in link, f"preset {name!r} does not set ceiling"
        assert "floor" in link, f"preset {name!r} does not set floor"
        assert link["floor"] <= link["ceiling"], f"preset {name!r} has floor above ceiling"


def test_the_only_uncapped_preset_is_the_clean_one():
    from src.config import PRESETS

    uncapped = [n for n, p in PRESETS.items() if p["link"]["ceiling"] >= 100.0]
    assert uncapped == ["perfect"], f"these presets can still reach perfect: {uncapped}"


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


def test_keeping_colours_changes_the_palette_and_nothing_else():
    """The switch has to change the colour and nothing else.

    Banding is the effect that motivates it: measured on a gradient it is both
    the largest hue shift of the three and the only one that raises edge
    energy, because posterisation turns a smooth ramp into hard steps.
    """
    from dataclasses import replace

    import cv2

    h, w = 120, 200
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    src = np.stack([
        (110 + 70 * np.sin(xx / 30)).clip(0, 255),
        (135 + 60 * np.cos(yy / 22)).clip(0, 255),
        (175 + 55 * np.sin((xx + yy) / 35)).clip(0, 255),
    ], axis=-1).astype(np.uint8)

    def hue_shift(a, b):
        ha = cv2.cvtColor(a, cv2.COLOR_BGR2HSV).astype(np.int16)[..., 0]
        hb = cv2.cvtColor(b, cv2.COLOR_BGR2HSV).astype(np.int16)[..., 0]
        return float(np.minimum(np.abs(ha - hb), 180 - np.abs(ha - hb)).mean())

    def luma(f):
        return cv2.cvtColor(f, cv2.COLOR_BGR2YCrCb)[..., 0]

    hard = snapshot(severity=0.95, quality=5.0)
    drifting = replace(Settings().video, keep_colours=False, drop_weight=0.0, tearing_weight=0.0)
    kept = replace(drifting, keep_colours=True)

    a = VideoDegrader(seed=2)
    a.apply(src, snapshot(), drifting)
    loose = a.apply(src, hard, drifting)

    b = VideoDegrader(seed=2)
    b.apply(src, snapshot(), kept)
    held = b.apply(src, hard, kept)

    assert hue_shift(src, held) < hue_shift(src, loose) / 3, "colours must stay put"

    # The switch must be chroma-only: every bit of blur, blocking and
    # posterisation the chain produced has to survive it untouched. Comparing
    # luminance is the exact statement of that; comparing edge energy is not,
    # because on a smooth source these effects *add* edges rather than remove
    # them (2.28 to 100.76 on this gradient).
    # Within one quantisation step: the BGR/YCrCb round trip rounds, and
    # measured on this frame no pixel moves by more than 1 level in 255.
    drift = np.abs(luma(held).astype(np.int16) - luma(loose).astype(np.int16))
    assert drift.max() <= 1, f"keeping colours moved brightness by {drift.max()} levels"


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


def test_camera_list_never_offers_the_virtual_camera():
    """Offering our own output as an input is a feedback loop.

    It also holds the device open against the write the tool is about to make.
    """
    from src.video import list_cameras

    names = [c["name"].lower() for c in list_cameras()]
    assert not [n for n in names if "obs virtual camera" in n or "unitycapture" in n]


def test_recording_too_short_refuses_to_become_a_loop():
    lp = Looper(fps=30)
    lp.start_record(max_seconds=5)
    for i in range(5):
        lp.process(frame(i, 64, 36))
    assert lp.stop_record(min_seconds=1.0, crossfade=0.2) is False
    assert lp.state is State.LIVE


def test_a_loop_is_built_sealed_and_played_back():
    """Crossfade mode specifically: bounce does no sealing, it has no join."""
    lp = Looper(fps=30)
    lp.start_record(max_seconds=5)
    for i in range(60):
        lp.process(frame(i, 64, 36))
    assert lp.stop_record(min_seconds=1.0, crossfade=0.2, mode="crossfade") is True
    assert lp.state is State.LOOP

    fade = int(0.2 * 30)
    assert lp.status()["loop_frames"] == 60 - fade, "the faded tail is folded in, not kept"

    live = frame(999, 64, 36)
    seen = [lp.process(live) for _ in range(90)]
    assert all(f is not None and f.shape == live.shape for f in seen)


def _loop_sequence(mode: str, count: int, recorded: int = 12) -> list[int]:
    """Play a loop back and report which recorded frame each output is.

    Each recorded frame is a flat image of a unique brightness, so the frame
    that came out can be identified by reading one pixel.
    """
    lp = Looper(fps=10, quality=100)
    lp.start_record(max_seconds=10)
    for i in range(recorded):
        lp.process(np.full((16, 16, 3), (i + 1) * 10, dtype=np.uint8))
    assert lp.stop_record(min_seconds=0.1, crossfade=0.0, mode=mode) is True

    live = np.zeros((16, 16, 3), dtype=np.uint8)
    seen = []
    for _ in range(count):
        out = lp.process(live)
        seen.append(int(round(float(out[8, 8, 0]) / 10)) - 1)
    return seen


def test_a_bounce_loop_never_jumps():
    """The complaint that motivated bounce: a wrapping loop resets.

    Every step of a bounce is to an adjacent recorded frame, including across
    the two turns, so there is no join to hide. A wrapping loop has one step
    that leaps the whole recording, which is the reset that is visible however
    much it is dissolved.
    """
    seen = _loop_sequence("bounce", 40)
    steps = [abs(b - a) for a, b in zip(seen, seen[1:])]
    assert set(steps) == {1}, f"bounce must only ever step one frame, got {sorted(set(steps))}"


def test_a_bounce_loop_turns_around_at_both_ends():
    seen = _loop_sequence("bounce", 40, recorded=6)
    # 0 1 2 3 4 5 4 3 2 1 0 1 ... two turns, no frame repeated at the turn
    assert seen[:12] == [0, 1, 2, 3, 4, 5, 4, 3, 2, 1, 0, 1], seen[:12]
    assert max(seen) == 5 and min(seen) == 0


def test_a_crossfade_loop_does_jump_and_that_is_the_difference():
    seen = _loop_sequence("crossfade", 40)
    steps = [abs(b - a) for a, b in zip(seen, seen[1:])]
    assert max(steps) > 1, "a wrapping loop has to leap back to the start somewhere"


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
    lp.stop_record(min_seconds=1.0, crossfade=0.2, mode="crossfade")

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
