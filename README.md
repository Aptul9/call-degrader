# call-degrader

Two things on one virtual camera and one virtual microphone, for a video call.

**A loop pedal.** Hold a key, record a few seconds of yourself, let go, and it plays on repeat as your camera. Press another key to go back to live.

**A bad connection.** Put the call on a line that drops frames, softens the picture, freezes, stutters the audio and pulls it out of sync. One simulated line drives the video and the audio together, which is what makes it read as a connection struggling rather than as two separate faults.

Works with anything that reads a webcam and a microphone, desktop clients included: Zoom, Teams, Meet, Discord, Slack, OBS.

## What you need to install

| | why | notes |
|---|---|---|
| Windows 11, Python 3.11+ | | tested on Python 3.14.6 |
| [VB-CABLE](https://vb-audio.com/Cable/) | the virtual microphone | run the installer as administrator, then reboot |
| OBS Studio | the virtual camera | never opened; only its DirectShow filter is used |

### The virtual camera

`pyvirtualcam` has no camera of its own on Windows. It writes into the driver that the OBS package registers.

```
scoop install obs-studio
```

Then once, from an **elevated** prompt:

```
%USERPROFILE%\scoop\apps\obs-studio\current\data\obs-plugins\win-dshow\virtualcam-install.bat
```

That registers CLSID `{A3FCE0F5-3493-419F-958A-ABA1250EC20B}` under `HKLM\SOFTWARE\Classes\CLSID`, in both the 64-bit and the 32-bit view. `virtualcam-uninstall.bat` beside it reverses this. The official OBS installer does the same job in one step; the scoop route keeps OBS itself inside your user directory.

Without this the tool still runs and the audio still works. The video appears in the preview and goes nowhere else, and the status bar says so.

### Microsoft Teams, only if it shows you no camera

Try Teams first. On MSTeams 26225.1806.5074.1452 both the virtual camera and the virtual microphone appear in the device list with none of this done, checked 2026-09-19.

Older builds did need it. The new Teams reads the Windows Media Foundation frame server, and the OBS virtual camera is a DirectShow filter, which stays invisible to that path until frame-server mode is switched on. If your Teams has no camera to pick, this is the fix. From an elevated prompt, **then reboot**:

```
reg add "HKLM\SOFTWARE\Microsoft\Windows Media Foundation\Platform" /v EnableFrameServerMode /t REG_DWORD /d 1 /f
reg add "HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows Media Foundation\Platform" /v EnableFrameServerMode /t REG_DWORD /d 1 /f
```

Teams in a browser tab works without any of this, because Chromium enumerates DirectShow devices directly.

## Setup

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe run.py --check
```

`--check` lists the cameras, the audio devices, and whether the virtual camera is reachable. Run it first: it answers most of what goes wrong later.

Every normal start also runs a preflight before opening anything, and prints what is missing with the command that fixes it. The same list appears as a banner at the top of the interface. It exists because neither driver ships with this tool, and without them the app still starts and the preview still moves, so the only symptom is a call that cannot see or hear you.

## Running

```
.venv/Scripts/python.exe run.py
```

The interface is at `http://127.0.0.1:8720`.

In the call application, pick **OBS Virtual Camera** as the camera and **CABLE Output** as the microphone. The button under `audio routing` sets the system default microphone instead, for applications with no picker of their own; it needs the `AudioDeviceCmdlets` PowerShell module.

| flag | default | what it does |
|---|---|---|
| `--host` | `127.0.0.1` | `0.0.0.0` reaches the interface from a phone on the same network |
| `--port` | `8720` | |
| `--camera` | `0` | camera index, as listed by `--check` |
| `--size` | `1280x720` | |
| `--fps` | `30` | |
| `--mic` | system default | input device, matched on part of its name |
| `--cable` | `CABLE Input` | output device, matched on part of its name |
| `--pattern` | | a generated test pattern instead of the camera |
| `--no-audio`, `--no-video` | | run one chain only |
| `--check` | | list devices and exit |

## Using it

Two tabs. **video** has the preview, the pedal and the line presets. **audio** has the recorder and its own presets. The line itself sits under both, because it drives both.

**Nothing happens until the line is on.** Every effect weight scales a reaction to a falling line, so with the line off they all multiply zero: the sliders move and the picture does not change. Tick **bad line on**, or click a preset. The panels say so when it is off.

### The pedal

Hold right Alt to record, release to loop, press right Ctrl to go back to live. The buttons do the same. The hotkeys work while another window has focus, which is the point.

Recording is hold-to-record, not press-to-start: the live feed keeps going out while the key is down and the switch happens on release, so the moment you reach for the key is not inside the loop.

**Loop style.** `bounce` plays to the end and walks back, so every step lands on an adjacent recorded frame and there is no join at all. `crossfade` wraps round and dissolves over the join, which still has to travel from your last pose back to your first, so it reads as a reset with a fade over it. Bounce costs direction: half the cycle runs backwards, invisible on idle movement, obvious on anything with a clear direction.

### Testing the audio

Hold **hold to record** and talk, release, and one player appears with both takes on it: your microphone, and what the call hears.

The processed side is rendered from the stored take, not captured during the recording, and it is re-rendered whenever anything that reaches the audio chain moves. Click a preset, drag a weight, change the line, and the call side rebuilds itself from the same words. Nothing is recorded twice.

Both takes play at once through their own gain, so the switch between them crossfades in 8 ms instead of restarting. Flipping sides lands on the same syllable, which is the only way to hear what an effect did to a particular consonant. `space` plays, `a` flips, dragging the wave moves the playhead, and `loop` repeats a phrase while you turn a weight down.

The wave draws both: the microphone as a dim silhouette, the call side over the top. Anything the line ate shows up as the front shape not being there.

The render is seeded, so the same take with the same settings renders identically every time. Unseeded it was not, and two renders of one preset varied more than two presets varied from each other, which made tuning by ear a comparison of random draws.

If the two sound identical, check the `mic peak` the panel reports. Two recordings of near-silence sound the same however hard the chain worked on one of them.

### How hard it hits

The response is squared, not linear: a call having a hard time, not a fault. Measured against a clean frame, the presets land at roughly 100%, 36%, 38%, 31% and 19% of the original edge detail, from `slightly-off` down to `about-to-drop`.

Every effect also has its own weight, and a weight of zero turns it off.

**Video.** Dropped frames, held and decayed rather than blacked. Resolution loss. A JPEG round trip, which gives real DCT blocking rather than a mosaic. Colour banding. Tearing. During a stall the frozen picture is dragged block by block, so it smears the way a decoder does when it runs out of reference data.

**Audio.** Packet loss concealed by repeating the last block, which is the stutter everyone recognises; plain silence reads as a muted microphone instead. Bit depth and rate crush. Pitch warble. A short comb filter for the metallic ring. Dropouts during a stall, after a brief window of concealment.

**Keep colours** is on by default: the chain degrades brightness and puts the original colour back. A starved codec really does wreck chroma, but the result is a picture whose colours crawl, which looks like a broken camera rather than a bad line.

## Building an exe

```
.venv/Scripts/python.exe -m pip install pyinstaller
.venv/Scripts/python.exe -m PyInstaller --noconfirm --clean call-degrader.spec
```

One spec, two targets, because the trade between them is real:

| | size | first HTTP 200 | |
|---|---|---|---|
| `dist/call-degrader/` | 164.2 MB, 139 files | 1.06 s | a folder |
| `dist/call-degrader-portable.exe` | 66.1 MB | 4.11 s | one file |

The single file is a bootloader: it unpacks the whole bundle to a temp directory before running a line of Python, every launch. It does not get faster on the second run, measured at 4.10 s cold and 4.08 s warm.

The console window is kept on purpose. It carries the preflight report, and without a tray icon it is how the app is stopped.

Neither build removes VB-CABLE or the OBS registration. Those are installed separately whatever the exe looks like.

```
.venv/Scripts/python.exe tests/test_exe.py
```

checks whatever is in `dist/`, and says so rather than failing when nothing is built yet. A build that succeeds is not a build that works: PyInstaller drops data files and dynamically imported modules without any error, and the usual result is a server that answers on `/` and 404s on the stylesheet, or an audio chain that never opens because the PortAudio DLL stayed behind.

## Tests

Start the app, then in another terminal:

```
.venv/Scripts/python.exe tests/run_all.py
```

`test_core` needs no hardware. The others measure the running chains: one through the preview stream, one by opening the virtual camera from a separate process the way a call client would, and two by playing a tone on the speakers and recording it back off the cable.

The two tone-based suites need the app started with `--mic "Stereo Mix"` and **audible speakers**. With the speakers muted they say so and skip, rather than failing as though the product were broken.

## Traps

**Keep OBS closed.** If OBS is open with its own virtual camera started, it owns the device and pushes its scene instead.

**A onefile bundle is a bootloader, so killing it kills the wrong process.** It unpacks itself and runs the real application as a child. `terminate()` on the launcher leaves that child holding the port, the camera and the virtual camera, and the only symptom is the next run connecting to the previous one: three were left behind that way and the test after them reported a 0.01 s cold start. Kill the tree, `taskkill /F /T /PID`.

**The three installs are once per machine, but only two of them stay put.** VB-CABLE survives until it is uninstalled. The camera registration points at `%USERPROFILE%\scoop\apps\obs-studio\current\data\obs-plugins\win-dshow\obs-virtualcam-module64.dll`, and `current` is a scoop junction, so an OBS update keeps working and `scoop uninstall obs-studio` leaves the CLSID registered against a file that is gone: the device still lists in every picker and fails to open. The Teams `EnableFrameServerMode` pair was found absent on the development machine after having been set earlier, and Teams went on listing both devices anyway, so current builds do not depend on it. The preflight reports all three on every start, the Teams one quietly as a note.

**VB-CABLE device variants are not interchangeable, and the channel count is not what decides it.** Measured by writing a 440 Hz tone into each variant and reading it back: `CABLE Input` on MME carries; `CABLE Output` on MME returns one 16-bit LSB of dither; `CABLE Output` on WASAPI will not open (`PaErrorCode -9999`); several other pairings segfault PortAudio. The working playback device is the 16-channel MME one. The order used is in `src/audio.py`.

**`sd.play` and `sd.rec` share one module-global stream.** Called from two threads, the second tears down the first one's stream while its callback is still running, and it segfaults rather than raising. Anything that plays and records at once needs explicit `OutputStream` and `InputStream` objects.

**MSMF and DirectShow do not share a camera index space.** An index is only meaningful alongside the backend it came from. The picker lists DirectShow devices and pins the backend when you choose one.

**A worker thread must own its stop event.** Sharing one with its next incarnation means `start()` clears it before the old thread has noticed, and the old thread never exits, still holding the camera.

**Dropped frames cannot be counted off a stream the consumer paces itself.** Both the preview and the virtual camera skip, so a run of held frames gets sampled as one. Edge detail measures the same thing without the noise.

**`haxybaxy/video-pedal` has no licence file**, so it is all rights reserved. Nothing here is copied from it. The pedal is a ring buffer, a weighted blend and a three-state machine, written from the description.
