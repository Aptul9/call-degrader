# call-degrader

A webcam loop pedal and a simulated bad connection, on one virtual camera and one virtual microphone. Hold a key to record a few seconds of yourself, let go and it loops as your camera. Independently, put the call on a line that drops frames, crushes the bitrate, freezes, stutters the audio and pulls it out of sync with the picture.

Works with anything that reads a webcam and a microphone, desktop clients included: Zoom, Teams, Meet, Discord, Slack, OBS.

## Why it exists

The pieces exist separately. `haxybaxy/video-pedal` does the loop and nothing else. Zoom Escaper does audio sabotage in a browser tab and has not moved since 2021. FreezeCam and Bad Connection Simulator are browser extensions, so they cover web calls and not desktop clients. OBS with `obs-distort-filter` and a VST covers most of the degradation, but has no loop and no single control surface. Nothing covers the union, and nothing coordinates the audio and the video off one line state, which is what separates a convincing bad connection from two independently broken streams.

## Requirements

- Windows, Python 3.11 or newer. Tested on Windows 11 and Python 3.14.6.
- [VB-CABLE](https://vb-audio.com/Cable/) for the virtual microphone.
- OBS Studio for the virtual camera. It is never opened; what is needed is the DirectShow filter its package registers, which `pyvirtualcam` writes into.

Without the camera driver the tool still runs and the audio chain still works. The video shows in the preview and goes nowhere else, and the status bar says so.

### Installing the camera driver

```
scoop install obs-studio
```

Then, from an elevated prompt, once:

```
%USERPROFILE%\scoop\apps\obs-studio\current\data\obs-plugins\win-dshow\virtualcam-install.bat
```

That registers CLSID `{A3FCE0F5-3493-419F-958A-ABA1250EC20B}` under `HKLM\SOFTWARE\Classes\CLSID`, in both the 32-bit and the 64-bit view. `virtualcam-uninstall.bat` in the same folder reverses it. The official OBS installer does the same thing in one step; the scoop route keeps OBS itself in the user directory.

### Other backends

`pyvirtualcam` supports OBS and [Unity Capture](https://github.com/schellingb/UnityCapture) on Windows, OBS on macOS (OBS 30 or later on macOS 13 and up), and v4l2loopback on Linux. Unity Capture has not been pushed since May 2023 and carries 32 open issues.

Two maintained alternatives exist, neither of which `pyvirtualcam` drives, so either would mean writing the frame feed here: [softcam](https://github.com/tshino/softcam), MIT, Windows only, a DLL with `scCreateCamera` and `scSendFrame` reachable from `ctypes`, and [akvirtualcamera](https://github.com/webcamoid/akvirtualcamera), GPLv3, Windows and macOS, fed by piping raw frames to `AkVCamManager` on stdin.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe run.py --check
```

`--check` lists the cameras, the audio devices and whether the virtual camera is reachable. Run it first; it answers most of what goes wrong later.

## Running

```bash
.venv/Scripts/python.exe run.py
```

The UI is at `http://127.0.0.1:8720`.

In the call application, pick the virtual camera as the camera and `CABLE Output` as the microphone. The button in the UI sets the system default microphone instead, for applications that do not offer a picker; it needs the `AudioDeviceCmdlets` PowerShell module.

| flag | default | what it does |
|---|---|---|
| `--host` | `127.0.0.1` | `0.0.0.0` makes the UI reachable from a phone on the same network |
| `--port` | `8720` | |
| `--camera` | `0` | camera index, as listed by `--check` |
| `--size` | `1280x720` | |
| `--fps` | `30` | |
| `--mic` | system default | input device, matched on a fragment of its name |
| `--cable` | `CABLE Input` | output device, matched on a fragment of its name |
| `--pattern` | | generated test pattern instead of the camera |
| `--no-audio`, `--no-video` | | run one chain only |
| `--check` | | list devices and exit |

## The pedal

Hold right Alt to record, release to loop, press right Ctrl to go back to live. The buttons in the UI do the same thing.

Recording is hold-to-record, not press-to-start. The live feed keeps going out for as long as the key is down and the switch happens on release, so the moment you reach for the key is not inside the loop. Both seams are dissolved: the loop's own wrap-around is blended when the loop is built, and the cut between live and loop is crossfaded each time it happens.

The preview ghosts the loop over the live camera at half opacity while a loop plays, so you can line yourself up before dropping back to live. That overlay never reaches the virtual camera.

Frames are held JPEG-encoded, about 2 MB per second of recording at 720p against about 80 MB raw.

## The line

One simulated link drives both chains. Its quality wanders around the set point as an Ornstein-Uhlenbeck walk, and stalls arrive on top as a Poisson process. Both chains read the same snapshot on the same clock, which is the part that matters: a freeze that does not line up with a dropout reads as two broken things rather than one bad connection.

Presets: `perfect`, `slightly-off`, `bad-wifi`, `train-tunnel`, `about-to-drop`. Every effect also has its own weight, and a weight of zero turns that effect off.

**Video.** Dropped frames, held and decayed rather than blacked. Resolution collapse. JPEG round trip at falling quality, which produces real DCT blocking instead of a mosaic. Colour banding. Tearing between two frames. During a stall, the frozen picture is dragged block by block, so it smears the way a decoder does when it runs out of reference data.

**Audio.** Packet loss concealed by repeating the last block, which is the stutter everybody recognises; plain silence reads as a muted microphone instead. Bit depth and sample rate crush. Pitch warble from a jitter buffer resampling to keep up. A short comb filter for the metallic ring. Dropouts during a stall, after a brief window of concealment. Every gain change is ramped across the block, because an abrupt one clicks and a click sounds like broken software rather than a broken line.

**Both.** Extra latency, and an audio-against-video desync knob in either direction.

## Tests

Start the app in one terminal, then in another:

```bash
.venv/Scripts/python.exe tests/run_all.py
```

Or one at a time:

```bash
.venv/Scripts/python.exe tests/test_core.py            # no hardware needed
.venv/Scripts/python.exe tests/test_video_path.py      # the chain, via the preview stream
.venv/Scripts/python.exe tests/test_virtual_camera.py  # reads the virtual camera back
.venv/Scripts/python.exe tests/test_cable_path.py      # needs the app on --mic "Stereo Mix"
```

`test_core.py` covers the link, the effects and the looper with no camera and no audio device.

`test_virtual_camera.py` is the one that proves the tool reaches another application: it opens the virtual camera from a separate process the way a call client would, and checks the resolution, that the picture is the feed rather than an empty OBS scene, that degradation arrives, and that a loop repeats at the far end while a live feed does not.

`test_cable_path.py` plays a tone on the speakers, lets the app pick it up through Stereo Mix, and records it back off `CABLE Output`, which is the device the call application would be using as its microphone.

Each test that drives the app sets up what it needs and puts back what it found, so they can run in any order. The integration tests switch the source to the generated pattern for their measurements; a covered lens or a dark room produces an almost constant frame, and against that a dropped frame and a delivered one are indistinguishable.

## Traps

**Keep OBS closed.** If OBS is open with its own virtual camera started, it owns the device and pushes its scene out instead.

**The consumer negotiates the capture format, not the sender.** OpenCV's DirectShow capture asks for 640x480 unless told otherwise, so reading the virtual camera back without setting `CAP_PROP_FRAME_WIDTH` and `CAP_PROP_FRAME_HEIGHT` returns downscaled frames and looks as though the tool is sending the wrong size.

**Dropped frames cannot be counted off the preview stream.** It skips on purpose, sleeping 1/30 s between parts while the pipeline also runs at 30 fps, so a run of held frames can be sampled as a single one. The measured share swings between 0.03 and 0.33 for identical settings. Count them at a real capture device instead.

**Randomised presets make flaky assertions.** `train-tunnel` stalls about 14 times a minute, so over a two-second window whether a stall lands at all is a coin toss. Tests that need a stall set the link explicitly rather than reaching for a preset.

**VB-CABLE device variants are not interchangeable.** Measured on this machine by writing a 440 Hz tone into each variant and reading it back: `CABLE Input` on MME carries, `CABLE Output` on MME returns one 16-bit LSB of dither and nothing else, `CABLE Output` on WASAPI refuses to open with `PaErrorCode -9999`, and several other pairings segfault PortAudio outright. The channel count is not the discriminator: the working playback device is the 16-channel MME one. The order used is in `src/audio.py`.

**`sd.play` and `sd.rec` share one module-global stream.** Calling them from two threads makes the second tear down the first one's stream while its callback is still running, which segfaults rather than raising. Anything that plays and records at once needs explicit `OutputStream` and `InputStream` objects.

**The camera thread carries its own stop event.** One shared event gets cleared by the next `start()` before the previous thread has noticed it was asked to stop, and that thread then runs forever, still holding the camera and still writing preview frames over the new one's.

**`video-pedal` has no licence file**, so it is all rights reserved. Nothing here is copied from it. The loop pedal is a ring buffer, a weighted blend and a three-state machine, written from the description.
