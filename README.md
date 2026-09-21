# call-degrader

Two things on one virtual camera and one virtual microphone, for a video call.

**A loop pedal.** Hold a key, record a few seconds of yourself, let go, and it plays on repeat as your camera. Press another key to go back to live.

**A bad connection.** Put the call on a line that drops frames, softens the picture, freezes, stutters the audio and pulls it out of sync. One simulated line drives the video and the audio together, which is what makes it read as a connection struggling rather than as two separate faults.

Works with anything that reads a webcam and a microphone, desktop clients included: Zoom, Teams, Meet, Discord, Slack, OBS.

Windows only. It writes into the OBS virtual camera and VB-CABLE, and both of those are Windows drivers.

## Download

[**Latest release**](https://github.com/Aptul9/call-degrader/releases/latest) — a zip, no installer. Unzip it anywhere and run `call-degrader.exe`. It opens its own window and puts an icon in the tray; there is no console.

It is unsigned, so the first run gets *Windows protected your PC* — **More info**, then **Run anyway**.

**It will not work until you install the two drivers below.** The app starts either way and tells you which one is missing, with the command that fixes it, but a call will see nothing until they are there.

Or run it from source, which needs Python 3.11+:

```
git clone https://github.com/Aptul9/call-degrader
cd call-degrader
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe run.py
```

## What you need to install

| | why | notes |
|---|---|---|
| Windows 11, Python 3.11+ | | tested on Python 3.14.6 |
| [VB-CABLE](https://vb-audio.com/Cable/) | the virtual microphone | run the installer as administrator, then reboot. Once, not twice: see the traps |
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

A build also puts an icon in the system tray, with `open call-degrader`, `pause camera`, `pause microphone` and `quit`. The two pauses are why it is there: mid-call the window is behind the call client and those are the controls you reach for. The ticks are read fresh each time the menu opens, so they follow whatever was done in the window. Closing the window hides it and the chains keep feeding the call; `quit` on the tray is the only way out. If the tray fails to appear the close button keeps its usual meaning instead, because hiding a window with no tray behind it leaves a running process and no way back to it. The log says which of the two you got.

From a checkout that is a browser tab. A built exe opens a native window instead, over the same server: `pywebview` pointed at WebView2, which Windows 11 already has, so nothing bundles a browser. `--window` forces the window from a checkout and `--no-window` forces the tab from a build; the test suites use the latter so that running them does not put a window on your screen.

In the call application, pick **OBS Virtual Camera** as the camera and **CABLE Output** as the microphone. The button under `audio routing` sets the system default microphone instead, for applications with no picker of their own; it needs the `AudioDeviceCmdlets` PowerShell module.

| flag | default | what it does |
|---|---|---|
| `--host` | `127.0.0.1` | `0.0.0.0` reaches the interface from a phone on the same network |
| `--port` | `8720` | |
| `--camera` | `0` | camera index, as listed by `--check` |
| `--size` | `1280x720` | |
| `--fps` | `30` | |
| `--mic` | system default | input device, matched on part of its name |
| `--cable` | `CABLE Input` | output device, matched on part of its name. Tried first; if no device carries that name the other VB-CABLE playback endpoints are tried in turn |
| `--pattern` | | a generated test pattern instead of the camera |
| `--no-audio`, `--no-video` | | run one chain only |
| `--window`, `--no-window` | window when built, tab from source | override which one you get |
| `--check` | | list devices and exit |

## Using it

Two tabs. **video** has the preview, the pedal and the line presets. **audio** has the recorder and its own presets. The line itself sits under both, because it drives both.

**Nothing happens until the line is on.** Every effect weight scales a reaction to a falling line, so with the line off they all multiply zero: the sliders move and the picture does not change. Tick **bad line on**, or click a preset. The panels say so when it is off.

**Delay is the one part of a bad line a conversation cannot absorb.** The line presets carry 0.15 s to 1.2 s of one-way delay, and `train-tunnel` adds 0.35 s of audio-behind-video on top of that, which is what makes the rough end of the range impossible to talk over rather than merely rough. **delay too** turns both off and leaves everything else where it was: the stalls, the dropouts and the artefacts all carry on. The **audio** pill reports what the two drivers negotiated, which is the floor the line adds on top of.

**mirror preview** moves the preview and nothing else. It is on, because a self-view that is not a mirror is unpleasant to sit in front of and every call application shows you one. What goes down the wire is never flipped, whatever that box says: the far end is looking at you rather than at your reflection, so a mirrored feed would arrive with writing backwards and pointing right arriving as pointing left.

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

One target, `dist/call-degrader/`: 181.8 MB, one process, first HTTP 200 after 1.82 s.

A single-file target existed until 0.1.1 and was dropped. PyInstaller onefile is a bootloader that unpacks the bundle to a temp directory and runs the real application as a child, so ending the visible task leaves the child serving, still holding the camera and the cable, with nothing on screen to say so. Measured: two processes, kill the parent, the survivor still answered HTTP 200. It also cost 4.13 s to first response against 1.82 s, and never improved, because it re-extracts every launch.

There is no console window. Closing it hides the window and leaves the chains running, as above, and the preflight report reaches the UI as a banner. Everything that would have gone to a terminal is appended to:

```
%LOCALAPPDATA%\call-degrader\call-degrader.log
```

That file is the only thing a failed start leaves behind, so it is the first place to look if the icon appears in the taskbar and no window ever does. Each run writes a banner with its pid, because one file holds many runs.

The build does not remove VB-CABLE or the OBS registration. Those are installed separately whatever the exe looks like.

`assets/make_icon.py` draws the icon and writes `assets/icon.ico`. `assets/icon.svg` beside it is the reference drawing; the two are kept in step by hand. Run it after changing either.

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

**The A/B player and the call are the same chain with different dice, and over a short take the dice dominate.** The player renders with a fixed seed so two presses are comparable; the chain feeding the call is unseeded and running. Measured on one 13 s phrase, `barely there` across twelve draws came out between 0.187 and 0.773 silent: the same preset on the same words, either mostly audible or almost entirely gone. So the player tells you what a setting does on average, not what this minute of the call will sound like. Stall rate and length are what drive it, because Poisson variance on a handful of expected events is enormous and one four-second stall eats a third of a short take. `tools/seed_spread.py` measures the spread for a given preset.

**Keep OBS closed.** If OBS is open with its own virtual camera started, it owns the device and pushes its scene instead.

**The window icon must be a `.ico`, and a `.png` does not fail, it kills the process.** pywebview hands it to `System.Drawing.Icon`, which reads ICO only, on a .NET dispatcher thread. The `ArgumentException` never becomes a Python exception: the process exits with `0xE0434352`, no window, no traceback, nothing in the log past the audio chain. Every headless check passed on that build, because they all run `--no-window`. `tests/test_exe.py` now starts one windowed and waits past the eight seconds it took to die.

**Stop a bundle by killing the tree, not the process that was launched.** `terminate()` on the launched process once left three survivors holding the port, the camera and the virtual camera, and the next test then reported a 0.01 s cold start because it had connected to one of them. `taskkill /F /T /PID`. This is why the onefile target was dropped in 0.1.1: its bootloader made every stop this fragile.

**The three installs are once per machine, but only two of them stay put.** VB-CABLE survives until it is uninstalled. The camera registration points at `%USERPROFILE%\scoop\apps\obs-studio\current\data\obs-plugins\win-dshow\obs-virtualcam-module64.dll`, and `current` is a scoop junction, so an OBS update keeps working and `scoop uninstall obs-studio` leaves the CLSID registered against a file that is gone: the device still lists in every picker and fails to open. The Teams `EnableFrameServerMode` pair was found absent on the development machine after having been set earlier, and Teams went on listing both devices anyway, so current builds do not depend on it. The preflight reports all three on every start, the Teams one quietly as a note.

**VB-CABLE device variants are not interchangeable, and the channel count is not what decides it.** Measured by writing a 440 Hz tone into each variant and reading it back: `CABLE Input` carries on MME and on WASAPI; `CABLE Output` on MME returns one 16-bit LSB of dither; `CABLE Output` on WASAPI will not open (`PaErrorCode -9999`). The order used is in `src/audio.py`, and it is walked rather than fixed: a host API view that will not start falls through to the next view of the same endpoint.

**The host API decides the delay as well as whether anything comes out.** A playback stream into `CABLE Input` negotiates 22 ms on WASAPI, 100 ms on MME asking for its low buffer, 180 ms on MME asking for nothing, and 240 ms on DirectSound. sounddevice asks for nothing by default, so the first release wrote into the cable through 180 ms of buffer and nobody looked at the number. Capture is the other way round and barely matters: the same microphone measures 20 ms on MME against 22 ms on WASAPI, so it stays on the host API that was proven for real devices. The WASAPI-into-`CABLE Input` pairing recorded in this file as a segfault did not reproduce: a 0.3 tone written that way and read off `CABLE Output` on DirectSound from a second process arrived at peak 0.3001.

**Run the VB-CABLE installer once. Twice leaves two device instances, and a reboot can kill the wrong one.** An endpoint name belongs to a device instance, not to the driver. The installer was run twice on the development machine and left `ROOT\MEDIA\0000` and `ROOT\MEDIA\0001`, both the same 3.3.1.7 driver, created a second apart. After an unclean shutdown only 0001 came back: 0000 went to Code 10 `CM_PROB_FAILED_START` and took its endpoints `CABLE Input` and `CABLE In 16ch` with it, both to `DeviceState 4`, where PortAudio stops listing them. The survivor was presenting `CABLE In 16 Ch` and a generic localised name, and a tone written into either arrived at `CABLE Output` at peak 0.5002. So the cable worked and the app refused to start, because it matched one literal name. `src/audio.py` now falls through `CABLE_OUTPUT_NAMES` and then sweeps on the adapter name, and the preflight reports which endpoint it settled on rather than claiming the driver is missing. Diagnose a repeat with `Get-PnpDevice -InstanceId "ROOT\MEDIA\*"` for the problem code, and `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render\*` reading `Properties\{a45c254e-df1c-4efd-8020-67d146a850e0},2` against `DeviceState`.

**A finding that names its own fix can name the wrong one.** The preflight used to turn "no device matches `cable input`" into "install VB-CABLE, run as administrator, then reboot". On the machine above that advice was both wrong and harmful: the driver was installed and working, and a second install is what produced the duplicate instance in the first place. A check reports what it observed; only a check that established the cause may prescribe the cure.

**`sd.play` and `sd.rec` share one module-global stream.** Called from two threads, the second tears down the first one's stream while its callback is still running, and it segfaults rather than raising. Anything that plays and records at once needs explicit `OutputStream` and `InputStream` objects.

**MSMF and DirectShow do not share a camera index space.** An index is only meaningful alongside the backend it came from. The picker lists DirectShow devices and pins the backend when you choose one.

**A worker thread must own its stop event.** Sharing one with its next incarnation means `start()` clears it before the old thread has noticed, and the old thread never exits, still holding the camera.

**Dropped frames cannot be counted off a stream the consumer paces itself.** Both the preview and the virtual camera skip, so a run of held frames gets sampled as one. Edge detail measures the same thing without the noise.

**`haxybaxy/video-pedal` has no licence file**, so it is all rights reserved. Nothing here is copied from it. The pedal is a ring buffer, a weighted blend and a three-state machine, written from the description.

## Licence

GPL-2.0, because [pyvirtualcam](https://github.com/letmaik/pyvirtualcam) is and there is no other way onto the virtual camera on Windows. Full text in [LICENSE](LICENSE).

Everything bundled into the release build is listed in [THIRD-PARTY.md](THIRD-PARTY.md), including one real licence incompatibility that is written down there rather than left for someone else to find.

Neither VB-CABLE nor the OBS driver is redistributed here. You install both yourself, from their own authors, under their own terms.
