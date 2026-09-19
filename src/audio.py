"""Audio chain: microphone in, degradation, virtual cable out.

The call application never sees the real microphone. It is pointed at
`CABLE Output`, and this process is what writes into `CABLE Input` at the other
end of that cable.

Input and output are two different devices with two different clocks, so they
get two streams and a queue between them rather than one duplex stream. The
degradation runs in the input callback, where the block already is; it is a few
hundred microseconds of numpy on 480 samples and does not justify a third hop.

Device selection follows what was already proven on this machine in
ever-spammer/calls/bridge.py: prefer the MME host API, because some indices
resolve onto WDM-KS and fail with 'Unanticipated host error -9999', and never
open more than two channels, which keeps the 16-channel VB-CABLE variants out.
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
# Input stays MME-first because that is what was proven for real capture
# devices, Stereo Mix included. Reading back from CABLE Output is the one case
# that needs DirectSound, and only the cable test does that; it passes its own
# order rather than skewing the default for every microphone.
API_ORDER = {
    "output": ("MME", "DirectSound", "WASAPI", "WDM-KS"),
    "input": ("MME", "DirectSound", "WASAPI", "WDM-KS"),
}

# Reading the far end of VB-CABLE: MME delivers silence, WASAPI will not open.
CABLE_CAPTURE_APIS = ("DirectSound", "MME", "WASAPI", "WDM-KS")


def find_device(match: str, kind: str, apis: tuple[str, ...] | None = None) -> tuple[int, dict]:
    """Index of the device whose name contains `match`.

    Ties break on the host API order above, then on the smallest channel count.
    Pass `apis` to force a different order for a device that needs it.
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
    return hits[0]


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
            "input": None,
            "output": None,
            "samplerate": None,
            "level_in": 0.0,
            "level_out": 0.0,
            "underruns": 0,
            "error": None,
        }

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._in_stream is not None:
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
        if cfg.input_device:
            in_idx, in_dev = find_device(cfg.input_device, "input")
        else:
            in_idx = sd.default.device[0]
            in_dev = sd.query_devices(in_idx)
        out_idx, out_dev = find_device(cfg.output_device, "output")

        rate = cfg.samplerate
        self._degrader = AudioDegrader(samplerate=rate)
        self._jitter = JitterBuffer(blocksize=cfg.blocksize)
        with self._queue.mutex:
            self._queue.queue.clear()

        self._out_stream = sd.OutputStream(
            samplerate=rate,
            blocksize=cfg.blocksize,
            channels=min(2, out_dev["max_output_channels"]),
            dtype="float32",
            device=out_idx,
            callback=self._on_output,
        )
        self._in_stream = sd.InputStream(
            samplerate=rate,
            blocksize=cfg.blocksize,
            channels=1,
            dtype="float32",
            device=in_idx,
            callback=self._on_input,
        )
        self._out_stream.start()
        self._in_stream.start()

        self._set(
            running=True,
            input=f"[{in_idx}] {in_dev['name'].strip()}",
            output=f"[{out_idx}] {out_dev['name'].strip()}",
            samplerate=rate,
            error=None,
        )
        log.info("audio %s -> %s at %s Hz", self._status["input"], self._status["output"], rate)

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

        try:
            self._queue.put_nowait(out)
        except queue.Full:
            # Output side is behind. Dropping the newest block is better than
            # growing a queue that turns into unbounded latency.
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
        so pressing play twice on the same recording gives the same result and
        a comparison between two settings is a comparison of the settings.
        """
        from .state import LinkSimulator

        store = SettingsStore(cfg)
        sim = LinkSimulator(store)
        degrader = AudioDegrader(samplerate=cfg.audio.samplerate)
        jitter = JitterBuffer(blocksize=cfg.audio.blocksize)

        step = cfg.audio.blocksize / max(1, cfg.audio.samplerate)
        now = 0.0
        out_blocks = []
        for block in raw:
            now += step
            snap = sim._advance(cfg.link, step, now)
            processed = degrader.apply(block, snap, cfg.audio)
            extra = snap.latency + max(0.0, snap.desync)
            depth = int(extra * cfg.audio.samplerate / max(1, cfg.audio.blocksize))
            out_blocks.append(jitter.push_pop(processed, depth))
        return np.concatenate(out_blocks) if out_blocks else np.zeros(1, dtype=np.float32)

    def _on_output(self, outdata, frames, time_info, status) -> None:
        if status:
            log.debug("output status: %s", status)
        try:
            block = self._queue.get_nowait()
        except queue.Empty:
            outdata.fill(0.0)
            self._status["underruns"] += 1
            return
        if len(block) < frames:
            block = np.pad(block, (0, frames - len(block)))
        outdata[:] = block[:frames, None]

    def _set(self, **fields) -> None:
        self._status.update(fields)


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
