"""Run every test in order and report once.

The three integration tests drive the same running app. They each set up what
they need and put back what they found, so the order here is for readability
rather than for correctness.

    python run.py                    # in one terminal
    python tests/run_all.py          # in another

`test_cable_path` needs the app reading Stereo Mix, so it is skipped with a
note rather than failed when the app is on a microphone.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SUITES = ["test_core", "test_video_path", "test_virtual_camera",
          "test_cable_path", "test_audio_ab"]


AUDIO_SUITES = {"test_cable_path", "test_audio_ab"}


def ready(name: str) -> bool:
    from harness import wait_audio_ready, wait_ready

    if not wait_ready():
        return False
    # The audio suites measure a live stream, so they also wait for the
    # underrun count to stop moving, which is how the chain says it has settled.
    return wait_audio_ready() if name in AUDIO_SUITES else True


def main() -> int:
    failed: list[str] = []
    skipped: list[str] = []

    for name in SUITES:
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
        if name != "test_core" and not ready(name):
            print("the chain never settled, skipping")
            failed.append(name)
            continue
        result = subprocess.run(
            [sys.executable, str(HERE / f"{name}.py")],
            capture_output=True, text=True,
        )
        output = result.stdout.strip()
        print(output or result.stderr.strip())

        if "restart with --mic" in output or 'restart with --mic' in output:
            skipped.append(name)
        elif result.returncode != 0:
            failed.append(name)

    print(f"\n{'=' * 60}")
    for name in SUITES:
        mark = "SKIP" if name in skipped else "FAIL" if name in failed else "ok  "
        print(f"  {mark}  {name}")
    print(f"\n{len(failed)} suite(s) failed, {len(skipped)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
