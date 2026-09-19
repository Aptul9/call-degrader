# PyInstaller build. Two targets from one analysis:
#
#     pyinstaller call-degrader.spec
#
# produces dist/call-degrader/ (a folder, starts fast) and
# dist/call-degrader.exe (one file, unpacks itself to a temp directory on
# every launch, so it starts slower the heavier the bundle is).
#
# The console is kept on purpose. It carries the preflight report, which is
# the first thing anyone needs when a call cannot see the camera, and it is
# how the app is stopped without a tray icon.

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("ui", "ui")]
binaries = []
hiddenimports = []

# pyvirtualcam ships its native backend as package data, which no import
# points at. sounddevice is deliberately absent from this list: it is a
# module rather than a package, so collect_all skips it with a warning, and
# PyInstaller's own hook brings the PortAudio DLL along anyway.
# pywebview reaches WebView2 through pythonnet, which loads its CLR bridge
# at runtime rather than importing it, so none of it is reachable by
# following imports.
for package in ("pyvirtualcam", "webview", "clr_loader", "pythonnet"):
    p_datas, p_binaries, p_hidden = collect_all(package)
    datas += p_datas
    binaries += p_binaries
    hiddenimports += p_hidden

# uvicorn picks its event loop, its HTTP parser and its websocket
# implementation by importing a string at runtime, so nothing static points
# at these and the analysis never sees them.
hiddenimports += collect_submodules("uvicorn")

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Qt arrives through opencv and is never used: no imshow, no namedWindow,
    # nothing. Left in it is tens of megabytes of bundle for nothing.
    excludes=["tkinter", "matplotlib", "PySide6", "PyQt5", "PyQt6", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# -- folder build ------------------------------------------------------

exe_dir = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="call-degrader",
    console=True,
    disable_windowed_traceback=False,
    upx=False,
)

COLLECT(
    exe_dir,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="call-degrader",
)

# -- single file -------------------------------------------------------

exe_one = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="call-degrader-portable",
    console=True,
    disable_windowed_traceback=False,
    upx=False,
)
