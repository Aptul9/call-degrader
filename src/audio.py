"""Audio chain: microphone in, degradation, virtual cable out.

The call application never sees the real microphone. It is pointed at
`CABLE Output`, and this process is what writes into `CABLE Input` at the other
end of that cable, or into whatever that endpoint is called today: see
`CABLE_OUTPUT_NAMES`.

Input and output are two different devices with two different clocks, so they
get two streams and a queue between them rather than one duplex stream. The
degradation runs in the input callback, where the block already is; it is a few
hundred microseconds of numpy on 480 samples and does not justify a third hop.

Device selection started from what was proven on this machine in
ever-spammer/calls/bridge.py: MME before WDM-KS, because some indices resolve
onto WDM-KS and fail with 'Unanticipated host error -9999', and never more than
two channels open, which keeps the 16-channel VB-CABLE variants out. Both
directions now lead with WASAPI, on measurement rather than inheritance: see
`API_ORDER`.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading

import numpy as np
import sounddevice as sd

from .audiofx import AudioDegrader, JitterBuffer
from .config import SettingsStore

# Seed used when a render is asked for and the link carries no seed of its own.
# Only the offline render uses it. The live chain stays unseeded, because a call
# that repeats the same glitch in the same place is not a call.
RENDER_SEED = 20260919

log = logging.getLogger(__name__)


# Host API preference, measured on this machine against VB-CABLE rather than
# reasoned about. A 440 Hz tone was written into every CABLE Input variant and
# read back from every CABLE Output variant:
#
#   CABLE Input MME        -> CABLE Output DirectSound   peak 0.300   carries
#   anything               -> CABLE Output MME           peak 3.05e-05, silence
#   anything               -> CABLE Output WASAPI        PaErrorCode -9999
#   CABLE Input DirectSound or WASAPI -> CABLE Output DirectSound   segfault
#
# So the channel count is not what decides it: the 16-channel MME playback
# device is the one that works, and the 2-channel WASAPI one is not. WDM-KS is
# last everywhere, which matches what ever-spammer/calls/bridge.py found.
#
# Output leads with WASAPI since 2026-09-21, which reverses part of the table
# above. That table measured whether a tone survived the cable, and it does on
# both; what it never measured was the delay. Writing into `CABLE Input`
# negotiates 22 ms on WASAPI against 180 ms on MME, and those 158 ms sat under
# every call the tool has been used on.
#
# The segfault recorded above did not reproduce: a WASAPI stream into `CABLE
# Input` while a separate process read `CABLE Output` on DirectSound came back
# at peak 0.3001 of a 0.3 tone, the whole signal and no crash. It is still a
# walk rather than a swap, because `_start_stream` falls through to the next
# host API view of the same endpoint when one will not start.
#
# Input leads with WASAPI too since 2026-09-26, for the cadence rather than
# the delay. The same microphone measures 20 ms on MME against 22 ms on
# WASAPI, which is nothing, but MME hands its 10 ms blocks over in pairs, two
# back to back every 20 ms (callback spacing spread 10 ms), where WASAPI
# delivers one every 10 ms (spread 1 ms). The pairs are what the handover
# queue had to stand deep for. MME stays right behind it, where Stereo Mix and
# the other real capture devices were proven, and `_start_stream` falls
# through to it when a WASAPI view will not start. Reading back from CABLE
# Output is the one case that needs DirectSound, and only the cable test does
# that; it passes its own order rather than skewing the default for every
# microphone.
API_ORDER = {
    "output": ("WASAPI", "MME", "DirectSound", "WDM-KS"),
    "input": ("WASAPI", "MME", "DirectSound", "WDM-KS"),
}

# Blocks the handover queue may keep standing once the output side has taken
# its own. One covers the jitter of two evenly clocked streams; whatever the
# queue holds above that for a whole window is delay the call pays for
# nothing, and `_settle` takes it back out a block at a time.
QUEUE_TARGET = 1
# Output callbacks per decision, half a second of 10 ms blocks. Long enough
# that a burst is not mistaken for a surplus, short enough that the backlog a
# stalled machine leaves behind drains in a few seconds.
QUEUE_WINDOW = 50

# Reading the far end of VB-CABLE: MME delivers silence, WASAPI will not open.
CABLE_CAPTURE_APIS = ("DirectSound", "MME", "WASAPI", "WDM-KS")

# Playback endpoint names VB-CABLE has been seen presenting on this machine,
# in the order they get tried. `CABLE Input` is the documented one, the one the
# README names and the one every routing guide tells people to pick, so it
# stays first.
#
# The rest are there because an endpoint name is not a property of the driver,
# it is a property of the device instance. The VB-CABLE installer was run twice
# on 2026-09-13 and left two root-enumerated instances of the same 3.3.1.7
# driver. After the unclean shutdown of 2026-09-21 06:41 only one of them came
# back: the other went to Code 10 CM_PROB_FAILED_START and took `CABLE Input`
# and `CABLE In 16ch` with it, both to DeviceState 4, where PortAudio stops
# listing them. The survivor was presenting `CABLE In 16 Ch` and a generic
# localised `Altoparlanti`, and a 440 Hz tone written into either of those came
# back off `CABLE Output` at peak 0.5002, so falling through to them is
# measured rather than hoped for.
CABLE_OUTPUT_NAMES = ("CABLE Input", "CABLE In 16 Ch", "CABLE In 16ch")

# Last resort, and the reason the generic name above is not in the list: the
# adapter name sits on every endpoint the driver owns however the endpoint
# itself ended up called, so this catches a rename nothing here has seen. MME
# leads API_ORDER, which keeps it off the WDM-KS `VB-Audio Point` views of the
# same cable.
CABLE_ADAPTER = "VB-Audio"


def find_devices(match: str, kind: str, apis: tuple[str, ...] | None = None) -> list[tuple[int, dict]]:
    """Every device whose name contains `match`, best host API first.

    Ties break on the host API order above, then on the smallest channel count.
    Pass `apis` to force a different order for a device that needs it.

    The whole ranking rather than the winner, because a device that resolves
    is not a device that opens: one host API view of an endpoint can refuse
    where the next one works, and only having the rest of the list makes that
    recoverable.
    """
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    order = apis or API_ORDER[kind]
    devices = sd.query_devices()
    host_apis = sd.query_hostapis()

    hits = [
        (i, d)
        for i, d in enumerate(devices)
        if match.lower() in d["name"].lower() and d[key] > 0
    ]
    if not hits:
        raise RuntimeError(f"no {kind} device whose name contains {match!r}")

    def rank(hit) -> tuple[int, int]:
        name = host_apis[hit[1]["hostapi"]]["name"]
        position = next((n for n, api in enumerate(order) if api in name), len(order))
        return position, hit[1][key]

    hits.sort(key=rank)
    return hits


def find_device(match: str, kind: str, apis: tuple[str, ...] | None = None) -> tuple[int, dict]:
    """Index of the device whose name contains `match`."""
    return find_devices(match, kind, apis)[0]


def cable_output_order(match: str | None = None) -> list[str]:
    """Name fragments tried, in order, when looking for the playback side."""
    order = [match] if match else []
    order += [n for n in CABLE_OUTPUT_NAMES if n != match]
    if CABLE_ADAPTER != match:
        order.append(CABLE_ADAPTER)
    return order


def find_cable_output(match: str | None = None) -> tuple[int, dict]:
    """Playback device the degraded audio gets written into.

    `match` is tried first and wins outright when it hits, so an explicit
    `--cable` still decides. What follows is for a machine whose VB-CABLE
    endpoints are no longer called what they were called yesterday: writing
    into the cable under a different endpoint name beats not writing at all.

    Quiet on purpose. Preflight calls this on every `/api/state`, so a warning
    here is a warning per poll for the length of a call. `_open` says it once
    instead, where it happens once per chain start.
    """
    return find_cable_outputs(match)[0]


def find_cable_outputs(match: str | None = None) -> list[tuple[int, dict]]:
    """Every playback endpoint the degraded audio could be written into.

    Ordered by name first, so an explicit `--cable` still decides, then by
    host API within each name. The chain walks this until one starts, which is
    what lets WASAPI lead without being a requirement: a view that will not
    open falls through to the MME view of the same endpoint instead of taking
    the audio down with it.
    """
    found: list[tuple[int, dict]] = []
    seen: set[int] = set()
    missed: list[str] = []
    for fragment in cable_output_order(match):
        try:
            hits = find_devices(fragment, "output")
        except RuntimeError:
            missed.append(fragment)
            continue
        for index, device in hits:
            if index not in seen:
                seen.add(index)
                found.append((index, device))
    if not found:
        raise RuntimeError(
            "no VB-CABLE playback device, nothing matches "
            + ", ".join(repr(m) for m in missed)
        )
    if missed:
        log.debug(
            "no playback device matches %s, writing into [%s] %s instead",
            ", ".join(repr(m) for m in missed), found[0][0], found[0][1]["name"].strip(),
        )
    return found


def host_api_settings(device: dict):
    """Let WASAPI convert the sample rate rather than refuse it.

    Shared mode only runs at the endpoint's own mix rate. Asked for anything
    else the stream fails to start and the walk falls through to MME, which is
    the slow host API the order above exists to avoid.
    """
    name = sd.query_hostapis(device["hostapi"])["name"]
    return sd.WasapiSettings(auto_convert=True) if "WASAPI" in name else None


def list_devices() -> dict:
    """Everything the UI needs to populate the two device pickers."""
    apis = sd.query_hostapis()
    inputs, outputs = [], []
    for i, d in enumerate(sd.query_devices()):
        entry = {
            "index": i,
            "name": d["name"].strip(),
            "api": apis[d["hostapi"]]["name"],
            "samplerate": int(d["default_samplerate"]),
        }
        if d["max_input_channels"] > 0:
            inputs.append({**entry, "channels": d["max_input_channels"]})
        if d["max_output_channels"] > 0:
            outputs.append({**entry, "channels": d["max_output_channels"]})
    return {"inputs": inputs, "outputs": outputs}


class AudioPipeline:
    def __init__(self, settings_store, link, pedal_state=None) -> None:
        self._store = settings_store
        self._link = link
        # Callable returning the looper state, so audio can be held while the
        # video loops. A moving mouth on a loop with live speech over it is the
        # single most obvious tell there is.
        self._pedal_state = pedal_state or (lambda: "live")

        self._degrader: AudioDegrader | None = None
        self._jitter: JitterBuffer | None = None
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)
        # Standing depth the handover is allowed to reach. Recomputed at open
        # against the real blocksize; this is the 480-at-48k answer.
        self._ceiling = 8

        # Output-side state for keeping the queue at its target and for
        # covering an empty one without a click. Only `_on_output` touches it.
        self._low: int | None = None
        self._window = 0
        self._block_ms = 10.0
        self._last_out: np.ndarray | None = None
        self._starved = False

        self._paused = False
        self._in_stream: sd.InputStream | None = None
        self._out_stream: sd.OutputStream | None = None
        self._lock = threading.Lock()

        # A/B capture. Both sides are taken inside the input callback, where the
        # raw block and the processed one are both already in hand. Recording
        # the result off CABLE Output instead would mean opening a third device
        # and would put the two recordings on different clocks.
        self._tap_lock = threading.Lock()
        self._tap_raw: list[np.ndarray] = []
        self._tap_out: list[np.ndarray] = []
        self._tap_left = 0  # blocks still to capture, 0 when idle
        self._tap_ready = False

        self._status = {
            "running": False,
            "paused": False,
            "input": None,
            "output": None,
            "samplerate": None,
            "latency_ms": None,
            "level_in": 0.0,
            "level_out": 0.0,
            "underruns": 0,
            # Delay standing in the handover queue, and blocks taken out of it
            # to bring it back to target.
            "queue_ms": 0.0,
            "trimmed": 0,
            "error": None,
        }

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        with self._lock:
            # Guard on the output stream, not the input one. Paused never opens
            # an input, so an input-based guard would let a second start()
            # through and leave the first output stream running unreferenced.
            if self._out_stream is not None:
                return
            cfg = self._store.get().audio
            try:
                self._open(cfg)
            except Exception as exc:
                log.warning("audio chain did not start: %s", exc)
                self._set(running=False, error=str(exc))
                self._close()

    def stop(self) -> None:
        with self._lock:
            self._close()
            self._set(running=False, level_in=0.0, level_out=0.0)

    def restart(self) -> None:
        self.stop()
        self.start()

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    # -- internals -----------------------------------------------------

    def _open(self, cfg) -> None:
        # Read once at open time. A change to it restarts the chain, so the
        # callbacks never have to reach for the settings store.
        self._paused = cfg.paused

        rate = cfg.samplerate
        self._degrader = AudioDegrader(samplerate=rate)
        self._jitter = JitterBuffer(blocksize=cfg.blocksize)
        self._ceiling = max(4, int(0.08 * rate / max(1, cfg.blocksize)))
        with self._queue.mutex:
            self._queue.queue.clear()
        self._low, self._window = None, 0
        self._block_ms = 1000.0 * cfg.blocksize / rate
        self._last_out, self._starved = None, False

        out_idx, out_dev, self._out_stream = self._start_stream(
            "output",
            find_cable_outputs(cfg.output_device),
            lambda index, device: sd.OutputStream(
                samplerate=rate,
                blocksize=cfg.blocksize,
                channels=min(2, device["max_output_channels"]),
                dtype="float32",
                device=index,
                latency=cfg.latency,
                extra_settings=host_api_settings(device),
                callback=self._on_output,
            ),
        )
        if cfg.output_device.lower() not in out_dev["name"].lower():
            log.warning(
                "no playback device matches %r, writing into [%s] %s instead",
                cfg.output_device, out_idx, out_dev["name"].strip(),
            )

        # Paused means the microphone is never opened, which is the whole
        # point: nothing is holding it and the indicator goes out.
        in_idx, in_dev = None, None
        if not cfg.paused:
            in_idx, in_dev, self._in_stream = self._start_stream(
                "input",
                self._input_candidates(cfg),
                lambda index, device: sd.InputStream(
                    samplerate=rate,
                    blocksize=cfg.blocksize,
                    channels=1,
                    dtype="float32",
                    device=index,
                    latency=cfg.latency,
                    extra_settings=host_api_settings(device),
                    callback=self._on_input,
                ),
            )

        self._set(
            running=True,
            paused=cfg.paused,
            input="paused" if cfg.paused else f"[{in_idx}] {in_dev['name'].strip()}",
            output=f"[{out_idx}] {out_dev['name'].strip()}",
            samplerate=rate,
            latency_ms=self._negotiated_ms(),
            error=None,
        )
        log.info(
            "audio %s -> %s at %s Hz, %s ms in the drivers",
            self._status["input"], self._status["output"], rate, self._status["latency_ms"],
        )

    def _input_candidates(self, cfg) -> list[tuple[int, dict]]:
        """Microphone views to try, in order.

        A device asked for by name is ranked by host API like anything else.
        The default one is not matched by name, because another host API view
        of a similar name can be a different device altogether. Each host API
        reports its own view of the system default instead, and those are the
        only handles certain to be the microphone the system means, so they
        lead, in `API_ORDER`, and the views sharing the name follow as
        fallbacks.
        """
        if cfg.input_device:
            return find_devices(cfg.input_device, "input")
        apis = sd.query_hostapis()
        leads: list[tuple[int, dict]] = []
        for wanted in API_ORDER["input"]:
            for api in apis:
                index = api.get("default_input_device", -1)
                if wanted in api["name"] and index >= 0 and all(index != i for i, _ in leads):
                    leads.append((index, sd.query_devices(index)))
        if not leads:
            index = sd.default.device[0]
            leads = [(index, sd.query_devices(index))]
        taken = {i for i, _ in leads}
        stem = leads[0][1]["name"].split("(")[0].strip()
        rest: list[tuple[int, dict]] = []
        if stem:
            try:
                rest = [c for c in find_devices(stem, "input") if c[0] not in taken]
            except RuntimeError:
                rest = []
        return [*leads, *rest]

    def _start_stream(self, kind: str, candidates, build):
        """Open and start the first candidate that will take it.

        A device that resolves is not a device that opens. One host API view
        of an endpoint can come back -9999 where the next view of the same
        endpoint runs, and it does it at start rather than at open, so both
        happen here and a failure moves down the list.
        """
        errors: list[str] = []
        for index, device in candidates:
            name = device["name"].strip()
            stream = None
            try:
                stream = build(index, device)
                stream.start()
            except Exception as exc:
                if stream is not None:
                    try:
                        stream.close()
                    except Exception as closing:
                        log.debug("%s [%s] close after a failed start: %s", kind, index, closing)
                errors.append(f"[{index}] {name}: {exc}")
                continue
            if errors:
                log.warning(
                    "%s fell through to [%s] %s after %s", kind, index, name, "; ".join(errors)
                )
            return index, device, stream
        raise RuntimeError(
            f"no {kind} device would start, tried " + ("; ".join(errors) or "nothing")
        )

    def _negotiated_ms(self) -> float:
        """What the drivers gave, both directions, in ms.

        Read off the open streams rather than off the hint that was asked for,
        so it is the buffer that exists. It is the floor under every delay the
        link adds on top, and the number to look at when the far end says
        there is a lag.
        """
        total = 0.0
        for stream in (self._in_stream, self._out_stream):
            if stream is not None:
                total += float(stream.latency)
        return round(total * 1000.0, 1)

    def _close(self) -> None:
        for stream in (self._in_stream, self._out_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception as exc:
                    log.debug("stream close: %s", exc)
        self._in_stream = None
        self._out_stream = None

    def _on_input(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("input status: %s", status)
        settings = self._store.get()
        block = indata[:, 0].copy()
        level_in = float(np.sqrt(np.mean(block * block)))

        if settings.pedal.mute_on_loop and self._pedal_state() == "loop":
            block = np.zeros_like(block)

        snap = self._link.get()
        out = self._degrader.apply(block, snap, settings.audio)

        # Link latency plus any positive desync, expressed in whole blocks.
        extra = snap.latency + max(0.0, snap.desync)
        depth = int(extra * settings.audio.samplerate / max(1, settings.audio.blocksize))
        out = self._jitter.push_pop(out, depth)

        # Two devices, two clocks, and the input one can be the faster. The
        # queue then grows a block at a time and never gives one back, so a
        # call that started in sync ends a second behind with nothing on
        # screen saying why. Measured at 2 to 3 blocks over a minute on this
        # machine, so this is a ceiling rather than a fix for something seen.
        # The oldest block goes, not the newest: the far end hears a join
        # either way, and only this way does the delay come back down.
        while self._queue.qsize() >= self._ceiling:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._queue.put_nowait(out)
        except queue.Full:
            pass

        self._tap(block, out)
        self._set(level_in=round(level_in, 4), level_out=round(float(np.abs(out).max()), 4))

    def _tap(self, raw: np.ndarray, processed: np.ndarray) -> None:
        """Collect one block of each side while a test recording is running."""
        with self._tap_lock:
            if self._tap_left <= 0:
                return
            self._tap_raw.append(raw.copy())
            self._tap_out.append(processed.copy())
            self._tap_left -= 1
            if self._tap_left == 0:
                self._tap_ready = True

    # -- A/B recording -------------------------------------------------

    # Hard ceiling on a held recording, so a button that never comes back up
    # cannot grow the buffers without limit.
    MAX_CAPTURE_SECONDS = 120.0

    def start_capture(self, seconds: float | None = None) -> dict:
        """Begin capturing both sides. `seconds` None means hold until stopped."""
        cfg = self._store.get().audio
        limit = seconds if seconds is not None else self.MAX_CAPTURE_SECONDS
        blocks = max(1, int(limit * cfg.samplerate / max(1, cfg.blocksize)))
        with self._tap_lock:
            self._tap_raw, self._tap_out = [], []
            self._tap_left = blocks
            self._tap_ready = False
        return {"blocks": blocks, "seconds": round(blocks * cfg.blocksize / cfg.samplerate, 2)}

    def stop_capture(self) -> dict:
        """End a held recording and keep whatever was captured."""
        with self._tap_lock:
            self._tap_left = 0
            self._tap_ready = len(self._tap_raw) > 0
            return self._capture_report()

    def capture_status(self) -> dict:
        with self._tap_lock:
            return self._capture_report()

    def _capture_report(self) -> dict:
        cfg = self._store.get().audio
        raw_peak = max((float(np.abs(b).max()) for b in self._tap_raw), default=0.0)
        out_peak = max((float(np.abs(b).max()) for b in self._tap_out), default=0.0)
        return {
            "recording": self._tap_left > 0,
            "ready": self._tap_ready,
            "captured": len(self._tap_raw),
            "seconds": round(len(self._tap_raw) * cfg.blocksize / cfg.samplerate, 2),
            # What the microphone actually gave. Two recordings of near-silence
            # sound identical however hard the chain worked on one of them, so
            # this is the first thing to check when the two players seem the
            # same.
            "peak_in": round(raw_peak, 4),
            "peak_out": round(out_peak, 4),
        }

    def capture_audio(self, side: str) -> tuple[np.ndarray, int] | None:
        """The finished recording. `side` is "before" or "after".

        "after" is rendered from the raw side against whatever the settings say
        right now, rather than handed back as it was processed during the
        recording. That is the point: record yourself once, then move the
        sliders and press play again. Re-recording every time you nudge a
        weight makes tuning by ear impossible.
        """
        cfg = self._store.get()
        with self._tap_lock:
            if not self._tap_ready or not self._tap_raw:
                return None
            raw = list(self._tap_raw)

        if side == "before":
            return np.concatenate(raw), cfg.audio.samplerate
        return self._render(raw, cfg), cfg.audio.samplerate

    def _render(self, raw: list[np.ndarray], cfg) -> np.ndarray:
        """Run the stored blocks back through a fresh chain.

        The line is replayed from its own simulator rather than sampled live,
        and both the simulator and the degrader are seeded, so pressing play
        twice on the same recording gives the same result and a comparison
        between two settings is a comparison of the settings.

        Seeding is not decoration. Unseeded, the stall draw and the packet-loss
        draw are fresh every render, and two renders of one preset differed by
        more than two presets differ from each other: `barely there` measured
        0.034, 0.060 and 0.049 RMS on three passes over the same take. Tuning
        by ear against that is tuning against noise.
        """
        from dataclasses import replace

        from .state import LinkSimulator

        seed = cfg.link.seed or RENDER_SEED
        link_cfg = replace(cfg.link, seed=seed)
        store = SettingsStore(replace(cfg, link=link_cfg))
        sim = LinkSimulator(store)
        degrader = AudioDegrader(samplerate=cfg.audio.samplerate, seed=seed)
        jitter = JitterBuffer(blocksize=cfg.audio.blocksize)

        step = cfg.audio.blocksize / max(1, cfg.audio.samplerate)
        now = 0.0
        out_blocks = []
        for block in raw:
            now += step
            snap = sim._advance(link_cfg, step, now)
            processed = degrader.apply(block, snap, cfg.audio)
            extra = snap.latency + max(0.0, snap.desync)
            depth = int(extra * cfg.audio.samplerate / max(1, cfg.audio.blocksize))
            out_blocks.append(jitter.push_pop(processed, depth))
        return np.concatenate(out_blocks) if out_blocks else np.zeros(1, dtype=np.float32)

    def _on_output(self, outdata, frames, time_info, status) -> None:
        if status:
            log.debug("output status: %s", status)
        if self._paused:
            # Nothing is feeding the queue, and an empty queue is normally a
            # fault worth counting. Here it is the requested state.
            outdata.fill(0.0)
            self._set(level_out=0.0)
            return
        try:
            block = self._queue.get_nowait()
        except queue.Empty:
            self._status["underruns"] += 1
            self._conceal(outdata, frames)
            return
        block = self._settle(block)
        if self._starved:
            # The first block after a gap fades in, or the restart clicks.
            block = block * np.linspace(0.0, 1.0, len(block), dtype=np.float32)
            self._starved = False
        self._last_out = block
        _write(outdata, frames, block)

    def _settle(self, block: np.ndarray) -> np.ndarray:
        """Take the queue back down to its target once it has stood above it.

        The queue has to absorb bursts: two devices on two clocks, and an MME
        microphone hands its blocks over in pairs. What it must not keep is a
        standing surplus, and nothing ever took one back: wherever the queue
        parked at start set the delay for the rest of the call, anywhere from
        10 to 80 ms on top of the drivers, 60 to 70 ms in one measured run.
        The lowest depth over a window is the part of the queue no burst
        needed. While that stays above the target, one block comes out per
        window, crossfaded into the next so the join does not click.
        """
        depth = self._queue.qsize()
        self._low = depth if self._low is None else min(self._low, depth)
        self._window += 1
        self._set(queue_ms=round(depth * self._block_ms, 1))
        if self._window < QUEUE_WINDOW:
            return block
        surplus = self._low > QUEUE_TARGET
        self._low, self._window = None, 0
        if not surplus:
            return block
        try:
            nxt = self._queue.get_nowait()
        except queue.Empty:
            return block
        self._status["trimmed"] += 1
        n = min(len(block), len(nxt))
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        return block[:n] * (1.0 - ramp) + nxt[:n] * ramp

    def _conceal(self, outdata, frames: int) -> None:
        """An empty queue fades the last block out instead of cutting to zero.

        A cut from speech straight to digital silence is a click, and this
        machine stalls every process for up to 130 ms at a time, which empties
        the queue however deep it is kept. Silence follows the fade until the
        queue refills, and the first block back fades in.
        """
        last, self._last_out = self._last_out, None
        self._starved = True
        if last is None:
            outdata.fill(0.0)
            return
        _write(outdata, frames, last * np.linspace(1.0, 0.0, len(last), dtype=np.float32))

    def _set(self, **fields) -> None:
        self._status.update(fields)


def _write(outdata, frames: int, block: np.ndarray) -> None:
    """One mono block into however many channels the stream has."""
    if len(block) < frames:
        block = np.pad(block, (0, frames - len(block)))
    outdata[:] = block[:frames, None]


# -- default microphone routing ----------------------------------------

_PS_CANDIDATES = (
    "pwsh",
    r"C:\Program Files\PowerShell\7\pwsh.exe",
    "powershell",
)


def set_default_microphone(match: str) -> str:
    """Point the system default microphone at `match`.

    Needs the AudioDeviceCmdlets module, and it is installed under the module
    path of whichever host installed it, which here is PowerShell 7. Calling
    plain `powershell` finds nothing and reports success anyway, so every
    candidate is probed for the module before it is used.

    Optional. Most call applications let you pick the microphone in their own
    settings, which does the same job without touching the system default.
    """
    script = (
        f"$d = Get-AudioDevice -List | Where-Object {{ $_.Type -eq 'Recording' "
        f"-and $_.Name -match '{match}' }} | Select-Object -First 1; "
        "if ($d) { Set-AudioDevice -ID $d.ID | Out-Null; "
        "(Get-AudioDevice -Recording).Name } else { 'NOT-FOUND' }"
    )
    errors = []
    for exe in _PS_CANDIDATES:
        try:
            probe = subprocess.run(
                [exe, "-NoProfile", "-Command",
                 "if (Get-Module -ListAvailable AudioDeviceCmdlets) { 'yes' } else { 'no' }"],
                capture_output=True, text=True, timeout=30,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{exe}: {exc}")
            continue
        if probe.returncode != 0 or probe.stdout.strip() != "yes":
            errors.append(f"{exe}: module absent")
            continue
        result = subprocess.run(
            [exe, "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=30
        )
        name = result.stdout.strip()
        if name and name != "NOT-FOUND":
            return name
        raise RuntimeError(f"no recording device matching {match!r}")
    raise RuntimeError("AudioDeviceCmdlets not reachable: " + "; ".join(errors))
