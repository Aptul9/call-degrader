# PyInstaller build:
#
#     pyinstaller call-degrader.spec
#
# produces dist/call-degrader.exe, one file. It replaced the folder build in
# 0.1.3: a folder unzipped from a download carries the Mark of the Web on
# every file, and .NET Framework refuses to load Python.Runtime.dll from it,
# so pywebview had no window to open. Onefile unpacks to a temp directory at
# every launch, and files the app writes itself carry no mark.
#
# The bootloader runs the real application as a child. Until 0.1.1 that meant
# ending the visible process left the child serving; run.py now has the child
# exit with its parent (_die_with_bootloader).
#
# No console. The window is what the app is now, closing it is what stops it,
# and the preflight report reaches the UI as a banner. Nothing is lost by
# hiding it because run.py writes the same output to a log file whenever there
# is no stderr to write to, which is exactly the windowed case.

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("ui", "ui"), ("assets/icon.ico", "assets"), ("assets/icon.png", "assets")]
binaries = []
hiddenimports = []

# pyvirtualcam ships its native backend as package data, which no import
# points at. sounddevice is deliberately absent from this list: it is a
# module rather than a package, so collect_all skips it with a warning, and
# PyInstaller's own hook brings the PortAudio DLL along anyway.
# pywebview reaches WebView2 through pythonnet, which loads its CLR bridge
# at runtime rather than importing it, so none of it is reachable by
# following imports.
for package in ("pyvirtualcam", "webview", "clr_loader", "pythonnet", "pystray"):
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

EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="call-degrader",
    console=False,
    disable_windowed_traceback=False,
    icon="assets/icon.ico",
    upx=False,
)
