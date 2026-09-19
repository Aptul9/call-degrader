# call-degrader

A webcam loop pedal and a simulated bad connection, on one virtual camera and one virtual microphone. Hold a key to record a few seconds of yourself, let go and it loops as your camera. Independently, put the call on a line that drops frames, crushes the bitrate, freezes, stutters the audio and pulls it out of sync with the picture.

Works with anything that reads a webcam and a microphone, desktop clients included: Zoom, Teams, Meet, Discord, Slack, OBS.

## Why it exists

The pieces exist separately. `haxybaxy/video-pedal` does the loop and nothing else. Zoom Escaper does audio sabotage in a browser tab and has not moved since 2021. FreezeCam and Bad Connection Simulator are browser extensions, so they cover web calls and not desktop clients. OBS with `obs-distort-filter` and a VST covers most of the degradation, but has no loop and no single control surface. Nothing covers the union, and nothing coordinates the audio and the video off one line state, which is what separates a convincing bad connection from two independently broken streams.

## Requirements

- Windows, Python 3.11 or newer. Tested on Windows 11 and Python 3.14.6.
- [VB-CABLE](https://vb-audio.com/Cable/) for the virtual microphone.
- OBS Studio installed for the virtual camera. It is never opened; the installer registers the driver that `pyvirtualcam` writes into. [Unity Capture](https://github.com/schellingb/UnityCapture) works as a smaller alternative, around 3 MB against 150 MB.

Without the camera driver the tool still runs and the audio chain still works. The video shows in the preview and goes nowhere else, and the status bar says so.

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

```bash
.venv/Scripts/python.exe tests/test_core.py          # no hardware needed
.venv/Scripts/python.exe tests/test_video_path.py    # against a running app
.venv/Scripts/python.exe tests/test_cable_path.py    # against a running app started with --mic "Stereo Mix"
```

`test_core.py` covers the link, the effects and the looper with no camera and no audio device. The other two measure the running chains: the video one pulls frames off the preview stream and measures held frames and edge detail, the cable one plays a tone on the speakers and records it back off `CABLE Output`, which is the device the call application would be using.

The video test switches the source to the generated pattern for its measurements. A covered lens or a dark room produces an almost constant frame, and against that a dropped frame and a delivered one are indistinguishable.

## Traps

**Keep OBS closed.** If OBS is open with its own virtual camera started, it owns the device and pushes its scene out instead.

**VB-CABLE device variants are not interchangeable.** Measured on this machine by writing a 440 Hz tone into each variant and reading it back: `CABLE Input` on MME carries, `CABLE Output` on MME returns one 16-bit LSB of dither and nothing else, `CABLE Output` on WASAPI refuses to open with `PaErrorCode -9999`, and several other pairings segfault PortAudio outright. The channel count is not the discriminator: the working playback device is the 16-channel MME one. The order used is in `src/audio.py`.

**`sd.play` and `sd.rec` share one module-global stream.** Calling them from two threads makes the second tear down the first one's stream while its callback is still running, which segfaults rather than raising. Anything that plays and records at once needs explicit `OutputStream` and `InputStream` objects.

**The camera thread carries its own stop event.** One shared event gets cleared by the next `start()` before the previous thread has noticed it was asked to stop, and that thread then runs forever, still holding the camera and still writing preview frames over the new one's.

**`video-pedal` has no licence file**, so it is all rights reserved. Nothing here is copied from it. The loop pedal is a ring buffer, a weighted blend and a three-state machine, written from the description.
