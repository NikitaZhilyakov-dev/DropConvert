# Build: pyinstaller DropConvert.spec --noconfirm
# Produces dist/DropConvert.app on macOS, dist/DropConvert/DropConvert.exe on Windows
import sys

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for package in ("imageio_ffmpeg", "pillow_heif"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["main.py"],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DropConvert",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="DropConvert")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="DropConvert.app",
        bundle_identifier="com.dropconvert.app",
        info_plist={
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
            # Lives in the menu bar; the Dock icon shows only while the window is open
            "LSUIElement": True,
            # Text of the Finder access prompt, used to learn the selected files
            "NSAppleEventsUsageDescription": "DropConvert asks Finder which files are selected "
            "to show the convert menu.",
            # Allows dropping files on the app's Dock icon
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "Media",
                    "CFBundleTypeRole": "Viewer",
                    "LSHandlerRank": "Alternate",
                    "LSItemContentTypes": [
                        "public.image", "public.movie", "public.audio",
                        "public.audiovisual-content",
                    ],
                }
            ],
        },
    )
