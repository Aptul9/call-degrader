# Third-party licences

`call-degrader` is GPLv2 because `pyvirtualcam` is, and there is no way to write to the virtual camera on Windows without it. Everything below is bundled into the release build.

| Package | Licence | What it does here |
|---|---|---|
| [pyvirtualcam](https://github.com/letmaik/pyvirtualcam) | **GPL-2.0** | writes frames into the OBS virtual camera |
| [pynput](https://github.com/moses-palmer/pynput) | **LGPL-3.0** | global hotkeys for the loop pedal |
| [pystray](https://github.com/moses-palmer/pystray) | **LGPL-3.0** | tray icon and its menu |
| [opencv-python](https://github.com/opencv/opencv-python) | Apache-2.0 | capture, resize, JPEG round trip, the effects |
| [numpy](https://numpy.org) | BSD-3-Clause | every per-pixel and per-sample operation |
| [sounddevice](https://python-sounddevice.readthedocs.io) | MIT | PortAudio bindings, microphone and cable |
| PortAudio | MIT | shipped inside sounddevice |
| [pywebview](https://pywebview.flowrl.com) | BSD-3-Clause | the native window |
| [pythonnet](https://pythonnet.github.io) / clr_loader | MIT | how pywebview reaches WebView2 |
| [Pillow](https://python-pillow.org) | MIT-CMU | loads the icon for the tray |
| [FastAPI](https://fastapi.tiangolo.com) | MIT | the local HTTP API |
| [uvicorn](https://www.uvicorn.org) | BSD-3-Clause | serves it |
| [starlette](https://www.starlette.io), [pydantic](https://docs.pydantic.dev), anyio, h11, click, websockets, httptools, watchfiles, PyYAML, python-dotenv, six, bottle, proxy_tools, comtypes | MIT / BSD | transitive |
| CPython | PSF-2.0 | the interpreter PyInstaller bundles |

## The conflict, stated rather than hidden

`pyvirtualcam` is GPL-2.0 and PyPI classifies it as v2, not v2-or-later. `pynput` and `pystray` are LGPL-3.0. The FSF's own compatibility table says LGPL-3.0 code cannot be combined with GPL-2.0-only code, and a single-file build of this contains all three.

Nobody is being harmed by it: everything here is copyleft, all the source is public, and no author's terms are being worked around. But it is a real incompatibility rather than a tidy one, and it is written down here instead of being left for someone else to find.

Two things would resolve it. If `pyvirtualcam` is actually v2-or-later, the whole build becomes GPL-3.0 and the conflict disappears. Otherwise, dropping `pystray` and `pynput` costs the tray icon and the global hotkeys and leaves the binary GPL-2.0 clean.

## Not bundled, and deliberately

**VB-CABLE** and the **OBS virtual camera driver** are installed separately by whoever runs this. VB-CABLE is donationware from VB-Audio with no published redistribution terms, and the OBS filter is GPLv2 owned by the OBS Project. Neither is redistributed here, which is why the README asks you to install them yourself.
