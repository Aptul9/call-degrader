"""What has to be on the machine before any of this works.

Two drivers carry the whole tool and neither ships with it. Without them the
app still starts, the picture still moves in the preview, and nothing says why
the call cannot see it. That silence is the thing this module exists to break:
every finding names what is missing and the exact command that fixes it.

Checks are read-only on purpose. The virtual camera is tested by looking for
its CLSID registration rather than by opening it, because opening a device the
video chain is about to claim is a good way to invent a contention bug that
only happens at startup.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

# Registered by OBS's virtualcam-install.bat, in both the 64-bit and the
# 32-bit view. pyvirtualcam writes into this filter; there is no fallback.
VIRTUAL_CAMERA_CLSID = "{A3FCE0F5-3493-419F-958A-ABA1250EC20B}"

_MF_PLATFORM = r"SOFTWARE\Microsoft\Windows Media Foundation\Platform"
_MF_PLATFORM_32 = r"SOFTWARE\WOW6432Node\Microsoft\Windows Media Foundation\Platform"


@dataclass(frozen=True)
class Finding:
    """One thing that is wrong, and what to type to make it right."""

    # "blocker" stops a call working, "warning" narrows what works, "note" is
    # something that used to matter and may still on an older build. A note is
    # not an alarm: it prints quietly and never colours the banner.
    level: str
    what: str
    fix: str

    def to_dict(self) -> dict:
        return {"level": self.level, "what": self.what, "fix": self.fix}


def _registry_has_key(root, path: str) -> bool:
    import winreg

    try:
        with winreg.OpenKey(root, path):
            return True
    except OSError:
        return False


def _registry_value(root, path: str, name: str):
    import winreg

    try:
        with winreg.OpenKey(root, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _virtual_camera() -> list[Finding]:
    import winreg

    # Checked in both views. Registering only the 64-bit one leaves 32-bit
    # clients, which several call apps still are, unable to see the device.
    views = [
        ("64-bit", winreg.KEY_WOW64_64KEY),
        ("32-bit", winreg.KEY_WOW64_32KEY),
    ]
    missing = []
    for label, flag in views:
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                rf"SOFTWARE\Classes\CLSID\{VIRTUAL_CAMERA_CLSID}",
                0,
                winreg.KEY_READ | flag,
            ):
                pass
        except OSError:
            missing.append(label)

    if not missing:
        return []

    fix = (
        "install OBS Studio, then from an elevated prompt run\n"
        r"    %USERPROFILE%\scoop\apps\obs-studio\current\data\obs-plugins\win-dshow\virtualcam-install.bat"
        "\n    (or use the official OBS installer, which does it in one step)"
    )
    if len(missing) == 1:
        return [Finding(
            "warning",
            f"the virtual camera is registered for 64-bit clients but not {missing[0]} ones",
            fix,
        )]
    return [Finding(
        "blocker",
        "no virtual camera: the OBS DirectShow filter is not registered, so no call app can see the video",
        fix,
    )]


def _cable() -> list[Finding]:
    try:
        import sounddevice as sd
    except Exception as exc:
        return [Finding("blocker", f"sounddevice will not import: {exc}",
                        "reinstall the requirements into the venv")]

    try:
        devices = sd.query_devices()
    except Exception as exc:
        return [Finding("blocker", f"no audio devices could be listed: {exc}",
                        "check that the Windows audio service is running")]

    names = [d["name"].lower() for d in devices]
    fix = ("install VB-CABLE from https://vb-audio.com/Cable/ , run the installer as "
           "administrator, then reboot")

    out = []
    # What this process writes into.
    if not any("cable input" in n for n in names):
        out.append(Finding(
            "blocker",
            "no CABLE Input: there is nowhere to write the degraded audio, so the call hears nothing",
            fix,
        ))
    # What the call application is told to listen to.
    if not any("cable output" in n for n in names):
        out.append(Finding(
            "blocker",
            "no CABLE Output: the call application has no virtual microphone to select",
            fix,
        ))
    return out


def _camera() -> list[Finding]:
    from .video import list_cameras

    try:
        if list_cameras():
            return []
    except Exception as exc:
        return [Finding("warning", f"the camera list could not be read: {exc}",
                        "run with --pattern to use a generated source instead")]
    return [Finding(
        "warning",
        "no camera found",
        "plug one in, or run with --pattern to use a generated source instead",
    )]


def _teams_frame_server() -> list[Finding]:
    import winreg

    on = all(
        _registry_value(winreg.HKEY_LOCAL_MACHINE, path, "EnableFrameServerMode") == 1
        for path in (_MF_PLATFORM, _MF_PLATFORM_32)
    )
    if on:
        return []
    # Stated as a fact until 2026-09-19, when it turned out not to be one.
    # MSTeams 26225.1806.5074.1452 lists the virtual camera with both values
    # absent, so whatever it needed before, it does not need this now. The
    # check stays because older builds did, and because the fix costs nothing
    # to write down; it is a note rather than a warning because on a working
    # machine an amber banner every start is just noise.
    return [Finding(
        "note",
        "frame server mode is off; older Teams desktop builds needed it to list the virtual camera, "
        "current ones do not. Only act on this if Teams has no camera to pick",
        'from an elevated prompt, then reboot:\n'
        '    reg add "HKLM\\SOFTWARE\\Microsoft\\Windows Media Foundation\\Platform" '
        '/v EnableFrameServerMode /t REG_DWORD /d 1 /f\n'
        '    reg add "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows Media Foundation\\Platform" '
        '/v EnableFrameServerMode /t REG_DWORD /d 1 /f\n'
        "    (Teams in a browser tab works without this)",
    )]


def run(want_video: bool = True, want_audio: bool = True) -> list[Finding]:
    """Everything wrong with this machine, worst first.

    A chain that was turned off at the command line is not checked, so
    `--no-video` does not complain about a driver it will never touch.
    """
    if not sys.platform.startswith("win"):
        return [Finding("warning", f"{sys.platform} is untested, this was built for Windows", "")]

    findings: list[Finding] = []
    if want_video:
        findings += _virtual_camera()
        findings += _camera()
        findings += _teams_frame_server()
    if want_audio:
        findings += _cable()

    order = {"blocker": 0, "warning": 1, "note": 2}
    return sorted(findings, key=lambda f: order.get(f.level, 3))


def report(findings: list[Finding]) -> str:
    """The startup block. Plain text, because it goes to a terminal."""
    if not findings:
        return "preflight: virtual camera, cable and camera all present"

    lines = []
    blockers = [f for f in findings if f.level == "blocker"]
    warnings = [f for f in findings if f.level == "warning"]
    if blockers:
        lines.append(f"preflight: {len(blockers)} thing(s) will stop this working")
    elif warnings:
        lines.append("preflight: everything needed for a call is present, with warnings")
    else:
        lines.append("preflight: virtual camera, cable and camera all present")

    for f in findings:
        mark = {"blocker": "!!", "warning": " -"}.get(f.level, " .")
        lines.append(f"  {mark} {f.what}")
        for line in f.fix.splitlines():
            if line.strip():
                lines.append(f"       {line}")
    return "\n".join(lines)
