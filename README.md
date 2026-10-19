# DropConvert

A drag & drop file converter for macOS and Windows. Drop files (or a folder) into the window, click a format, and the result appears next to the original. The original is never changed and existing files are never overwritten: if a name is taken, " (1)" is appended.

| What | From | To |
|---|---|---|
| Images | PNG, JPG, WEBP, HEIC, AVIF, GIF, BMP, TIFF, ICO, TGA, PSD | PNG, JPG, WEBP, HEIC, AVIF, GIF, BMP, TIFF, ICO, PDF |
| Video | MP4, MOV, MKV, WEBM, AVI, WMV, FLV, MPG, 3GP, TS | MP4, MOV, MKV, WEBM, AVI, GIF, plus MP3, WAV, M4A (extract audio) |
| Audio | MP3, WAV, FLAC, OGG, M4A, AAC, WMA, OPUS, AIFF, AMR | MP3, WAV, FLAC, OGG, M4A, OPUS |

## Working from the menu bar

DropConvert lives in the menu bar (macOS) or the tray (Windows) and works without a window:

1. In Finder / File Explorer, hold **⌘** (macOS) or **Alt** (Windows) and right-click a file. If several files are selected, the menu opens for the whole selection.
2. A wheel with formats that fit these files opens at the cursor; the regular context menu doesn't appear. If the selection mixes file types (say, images and video), pick the type first.
3. Click the sector with the format you want. From the keyboard: arrows and Enter, digits 1–9, or Esc to close. Clicking the center of the wheel opens the files in the DropConvert window.
4. The wheel collapses into a progress ring at the cursor: it shows the percentage and ends with a checkmark (or "!" if something failed). A notification also arrives; clicking it shows the result.

**Wheel while dragging.** Start dragging a file (from Finder, File Explorer or the desktop) and, without releasing the button, press **Tab**. The wheel opens under the cursor; drop the file on the sector you want. If you release outside the wheel or press Esc, the wheel closes and the file stays where it was. When dragging files of different types, hold them over a type sector ("Images", "Video") and it springs open into formats.

**Tab cycles wheels.** If the files include images, there is a "Tools" wheel besides "Convert". Tab opens the next wheel, Shift+Tab the previous one; this works both while dragging and in the ⌘ / Alt + right-click wheel. The current wheel is highlighted in the strip below it.

Image tools keep the format and put the result alongside, e.g. `photo (compressed).jpg`:

| Tool | What it does |
|---|---|
| Compress | re-encodes JPG, WEBP, HEIC, AVIF at lower quality; turns PNG into a 256-color palette. If the file doesn't get smaller, no copy is made |
| Strip metadata | removes EXIF, location and the like; EXIF rotation is applied to the image. A plain JPEG is saved without re-encoding |
| Trim | cuts transparent or solid-color borders |
| Shrink | halves each side |
| Rotate | 90° clockwise |
| B&W | grayscale |
| Fill | replaces transparency with white |

All tools except Strip keep EXIF (capture date, camera). Animated GIF and WEBP aren't supported by the tools yet.

Launch at login can be turned on from the icon's menu. Closing the window doesn't quit the app; quit from the icon's menu.

macOS asks for two permissions:
- **Accessibility**, to intercept ⌘ + right-click and Tab while dragging. The prompt appears on first launch; enable DropConvert in System Settings → Privacy & Security → Accessibility. No restart needed. The app isn't signed, so after every rebuild macOS treats it as a new app: turn the switch off and on again.
- **Finder automation**, to learn which files are selected. The prompt appears on the first click.

While the left mouse button is held and the mouse has moved, Tab doesn't reach applications: DropConvert takes it.

Windows limitations: the selection comes from the active File Explorer window, files on the desktop aren't supported yet; if the File Explorer window has several tabs, the first one is used.

Also:
- you can drop files on the Dock icon (macOS) or on the `.exe` / a shortcut (Windows);
- shows conversion progress for video and audio;
- double-clicking a row shows the file in Finder or File Explorer;
- the "Stay on top" toggle keeps the window above others, handy for dragging from the desktop.

## Running from source

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # Windows: .venv\Scripts\pip ...
.venv/bin/python main.py
```

No separate ffmpeg install is needed: a binary for your platform ships in the `imageio-ffmpeg` package.

## Building

PyInstaller can't cross-compile, so each version is built on its own OS:

- **macOS:** `./build_mac.sh` → `dist/DropConvert.app`
- **Windows:** `build_windows.bat` → `dist\DropConvert\` folder with `DropConvert.exe`

Unsigned, macOS warns about an unidentified developer on first launch (open it via right-click → Open), and Windows shows SmartScreen. Distribution needs an Apple Developer ID with notarization and a Windows code signing certificate.

## Tests

```bash
.venv/bin/python -m pytest
```

## Layout

```
main.py                    entry point
dropconvert/formats.py     supported formats
dropconvert/converters.py  conversion (Pillow + ffmpeg) and image tools
dropconvert/app.py         drag & drop window (PySide6)
dropconvert/agent.py       menu bar icon, what the wheel shows (pages, tools), single instance
dropconvert/wheel.py       the wheel (painting, animation, mouse, keyboard, drops) and the progress ring
dropconvert/trigger.py     intercepting ⌘ / Alt + right-click and Tab while dragging (CGEventTap / WH_*_LL hooks)
dropconvert/system.py      Finder / File Explorer selection, launch at login, Dock
DropConvert.spec           PyInstaller build config
```

To add a tool, add it to `TOOLS` in `formats.py`, to `TOOL_SUFFIXES` and `_apply_image_tool` in `converters.py`, and its labels to `_TOOLS` in `agent.py`.

To add a format, add it to `TARGETS` in `formats.py` and set its options in `_PIL_FORMATS` (images) or `FFMPEG_ARGS` (video and audio) in `converters.py`.
