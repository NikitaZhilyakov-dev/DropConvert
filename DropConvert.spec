# Сборка: pyinstaller DropConvert.spec --noconfirm
# На macOS получится dist/DropConvert.app, на Windows — dist/DropConvert/DropConvert.exe
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
            # Живём в строке меню; значок в Dock появляется, только пока открыто окно
            "LSUIElement": True,
            # Текст запроса доступа к Finder, чтобы узнать выделенные файлы
            "NSAppleEventsUsageDescription": "DropConvert узнаёт у Finder, какие файлы выделены, "
            "чтобы показать меню конвертации.",
            # Разрешает бросать файлы на иконку приложения в Dock
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
