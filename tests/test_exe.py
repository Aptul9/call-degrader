"""Prove a built bundle is the app, not just an exe that starts.

Run on its own, after `pyinstaller call-degrader.spec`. It is not in
`run_all.py`: it launches its own copy of the server on a spare port, so it
cannot share a machine with the suites that drive an already-running app.

    python tests/test_exe.py

What this catches that a successful build does not: PyInstaller drops data
files and dynamically imported modules without failing. The usual shape is a
server that answers on `/` and 404s on the stylesheet, or an audio chain that
never opens because the PortAudio DLL was left behind. Neither shows up until
someone tries to take a call with it.

Cold start is measured because it is the number that decides one file against
one folder, and the answer is not obvious: the single file unpacks itself
every launch and gets no faster on the second run.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [
    ("one folder", ROOT / "dist" / "call-degrader" / "call-degrader.exe", 8741),
    ("one file", ROOT / "dist" / "call-degrader-portable.exe", 8742),
]

# Generous: the single file unpacks 66 MB before it runs a line of Python, and
# a cold machine under load is slower than this one.
BOOT_TIMEOUT = 120.0


def _get(base: str, path: str, timeout: float = 5.0):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return r.status, r.read()


def _boot(exe: Path, port: int):
    """Start it and wait for the first 200. Returns (process, seconds)."""
    started = time.monotonic()
    proc = subprocess.Popen(
        # Frozen, the default is a native window. The suite drives HTTP,
        # so it asks for the browser mode rather than popping a window
        # up on whoever is running the tests.
        [str(exe), "--port", str(port), "--no-window"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    base = f"http://127.0.0.1:{port}"
    while time.monotonic() - started < BOOT_TIMEOUT:
        if proc.poll() is not None:
            raise AssertionError(f"{exe.name} exited before serving:\n{proc.stdout.read()}")
        try:
            _get(base, "/", timeout=1.5)
            return proc, time.monotonic() - started
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.25)
    proc.kill()
    raise AssertionError(f"{exe.name} never answered within {BOOT_TIMEOUT:.0f}s")


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the whole tree, not the process that was launched.

    A onefile bundle is a bootloader: it unpacks itself and runs the real
    application as a child. `terminate()` kills the bootloader and the child
    keeps the port, the camera and the virtual camera. Three of them were left
    running that way, and the next test then measured a 0.01 s boot because it
    had connected to the previous one.
    """
    if proc.poll() is None:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, check=False,
        )
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    # The virtual camera is single-producer. Releasing it is not instant, and
    # the next bundle cannot open it until the driver has let go.
    time.sleep(1.5)


def _settled(base: str, timeout: float = 20.0) -> dict:
    """State once both chains have opened, or the last state if they never do.

    Answering on `/` only means uvicorn is up. The camera is opened on its own
    thread and takes a second or two, so reading the state straight after the
    first 200 catches the chain mid-start and reports a bundle fault that is
    really a race in the test. Found exactly that way: the same bundle passed
    when a few extra HTTP calls happened to delay the read.
    """
    deadline = time.monotonic() + timeout
    state = {}
    while time.monotonic() < deadline:
        _, body = _get(base, "/api/state")
        state = json.loads(body)
        video, audio = state["status"]["video"], state["status"]["audio"]
        settled = video.get("running") and audio.get("running")
        failed = video.get("error") or audio.get("error")
        if settled or failed:
            return state
        time.sleep(0.3)
    return state


def check_bundle(label: str, exe: Path, port: int) -> list[str]:
    failures: list[str] = []

    def check(what: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} {what}{('  ' + detail) if detail else ''}")
        if not ok:
            failures.append(f"{label}: {what}")

    proc, seconds = _boot(exe, port)
    base = f"http://127.0.0.1:{port}"
    size = (sum(f.stat().st_size for f in exe.parent.rglob("*") if f.is_file())
            if label == "one folder" else exe.stat().st_size)
    print(f"  {size / 1024 / 1024:.1f} MB, first HTTP 200 after {seconds:.2f}s")

    try:
        status, body = _get(base, "/")
        check("index.html serves", status == 200 and b"call-degrader" in body)

        # The data files are what a bundle drops, and it drops them quietly.
        status, body = _get(base, "/static/app.js")
        check("app.js bundled", status == 200 and b"abRefreshSoon" in body, f"{len(body)}B")
        status, body = _get(base, "/static/style.css")
        check("style.css bundled", status == 200 and b"ab-play" in body, f"{len(body)}B")

        state = _settled(base)
        video, audio = state["status"]["video"], state["status"]["audio"]

        check("video chain running", bool(video.get("running")))
        check("virtual camera open", bool(video.get("virtual_camera")),
              str(video.get("virtual_camera")))
        # If the PortAudio DLL did not come along, this is where it shows.
        check("audio chain running", bool(audio.get("running")), str(audio.get("output")))
        check("no chain errors", not video.get("error") and not audio.get("error"),
              f"{video.get('error')} / {audio.get('error')}")
        check("preflight ran", isinstance(state.get("preflight"), list))
        blockers = [f for f in (state.get("preflight") or []) if f["level"] == "blocker"]
        check("no preflight blockers", not blockers, str([b["what"][:40] for b in blockers]))
    finally:
        _kill_tree(proc)
    return failures


def _windows_of(pid: int) -> list[int]:
    """Visible top-level windows belonging to a process, by title."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if "call-degrader" in buf.value:
                    found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    return found


def _close_window(hwnd: int) -> None:
    """What the X button sends. Anything gentler skips the handler."""
    import ctypes

    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE


def _tray_in_log() -> str:
    """What the log says about the tray, for the run that is still going.

    A frozen build writes to %LOCALAPPDATA%, which is the only channel the
    tray has: it publishes no HTTP and the window says nothing about it.
    """
    import os

    log = Path(os.environ.get("LOCALAPPDATA", "")) / "call-degrader" / "call-degrader.log"
    if not log.exists():
        return ""
    for line in reversed(log.read_text(encoding="utf-8", errors="replace").splitlines()):
        if "tray icon up" in line:
            return line.split("src.tray:")[-1].strip()
        if "no tray icon" in line:
            return ""
    return ""


def check_window(exe: Path, port: int) -> list[str]:
    """Start it the way a double-click does, and see whether it stays up.

    Everything else here passes --no-window, so nothing else exercises the
    path a user actually takes. That gap hid a crash: the window icon was a
    png, System.Drawing.Icon rejected it on a .NET thread, and the process
    died at about eight seconds with no window, no traceback and no Python
    exception to catch. Every headless check passed on that same binary.

    A window appears for a few seconds while this runs. That is the test.
    """
    failures: list[str] = []
    print("\nwindowed, as a double-click would: (a window will appear briefly)")
    proc = subprocess.Popen([str(exe), "--port", str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    base = f"http://127.0.0.1:{port}"
    try:
        # Well past where the icon crash landed, and past a slow camera open.
        deadline = time.monotonic() + 25
        serving = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                _get(base, "/", timeout=1.5)
                serving = True
                break
            except (urllib.error.URLError, OSError, TimeoutError):
                time.sleep(0.3)

        alive = proc.poll() is None
        print(f"  {'ok  ' if alive else 'FAIL'} still running"
              f"{'' if alive else f' (exit {proc.returncode})'}")
        if not alive:
            failures.append("windowed: the process died")
        print(f"  {'ok  ' if serving else 'FAIL'} serving")
        if not serving:
            failures.append("windowed: never served")

        if alive:
            # The crash took about eight seconds, so outliving the first
            # request is not enough to call it up.
            time.sleep(8)
            held = proc.poll() is None
            print(f"  {'ok  ' if held else 'FAIL'} still running 8s later"
                  f"{'' if held else f' (exit {proc.returncode})'}")
            if not held:
                failures.append("windowed: died after starting")

            # The tray is the only part of a build with no HTTP surface, so
            # the log is the only thing that can say whether it came up.
            tray = _tray_in_log()
            print(f"  {'ok  ' if tray else 'FAIL'} tray icon up  {tray or 'not in the log'}")
            if not tray:
                failures.append("windowed: no tray icon")

            # Closing has to hide, not quit, so the chains keep feeding a call
            # while the window is out of the way. Only a real WM_CLOSE goes
            # through the handler that decides which of the two happens.
            hwnds = _windows_of(proc.pid)
            print(f"  {'ok  ' if hwnds else 'FAIL'} window found  {len(hwnds)}")
            if not hwnds:
                failures.append("windowed: no window to close")
            else:
                _close_window(hwnds[0])
                time.sleep(6)
                survived = proc.poll() is None
                print(f"  {'ok  ' if survived else 'FAIL'} survived the close"
                      f"{'' if survived else f' (exit {proc.returncode})'}")
                if not survived:
                    failures.append("windowed: closing quit instead of hiding")
                else:
                    try:
                        _get(base, "/", timeout=3)
                        still = True
                    except (urllib.error.URLError, OSError, TimeoutError):
                        still = False
                    print(f"  {'ok  ' if still else 'FAIL'} still serving once hidden")
                    if not still:
                        failures.append("windowed: stopped serving when hidden")

                    hidden = not _windows_of(proc.pid)
                    print(f"  {'ok  ' if hidden else 'FAIL'} window hidden")
                    if not hidden:
                        failures.append("windowed: the window is still on screen")
    finally:
        _kill_tree(proc)
    return failures


def main() -> int:
    present = [(label, exe, port) for label, exe, port in TARGETS if exe.exists()]
    if not present:
        # Same contract as the tone suites: say why, do not fail as though the
        # product were broken.
        print("nothing built yet, skipping. Run: pyinstaller call-degrader.spec")
        return 0

    failures: list[str] = []
    for label, exe, port in present:
        print(f"\n{label}: {exe.relative_to(ROOT)}")
        failures += check_bundle(label, exe, port)

    # Once, on the folder build. The windowed path is the same code in both
    # and the single file only wraps it, so running it twice buys nothing but
    # another window in someone's face.
    failures += check_window(present[0][1], 8743)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
