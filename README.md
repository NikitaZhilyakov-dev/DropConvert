# DropConvert

A drag & drop file converter for macOS and Windows. Drop files (or a folder) into the window, click a format, and the result appears next to the original. The original is never changed and existing files are never overwritten: if a name is taken, " (1)" is appended.

| What | From | To |
|---|---|---|
| Images | PNG, JPG, WEBP, HEIC, AVIF, GIF, BMP, TIFF, ICO, TGA, PSD | PNG, JPG, WEBP, HEIC, AVIF, GIF, BMP, TIFF, ICO, PDF |
| Video | MP4, MOV, MKV, WEBM, AVI, WMV, FLV, MPG, 3GP, TS | MP4, MOV, MKV, WEBM, AVI, GIF, plus MP3, WAV, M4A (extract audio) |
| Audio | MP3, WAV, FLAC, OGG, M4A, AAC, WMA, OPUS, AIFF, AMR | MP3, WAV, FLAC, OGG, M4A, OPUS |
| Documents | Word (DOC, DOCX, RTF, ODT), Excel (XLS, XLSX, ODS), PowerPoint (PPT, PPTX, ODP) | Single PDF, PDF by pages |
| PDF | PDF | Pages… (chosen pages as one PDF) |

Documents are converted by an office suite installed on the computer, since only it lays them out exactly: Microsoft Office if it's there (Word, Excel or PowerPoint, matching the file), otherwise [LibreOffice](https://www.libreoffice.org). **Single PDF** is the whole document in one file; for Excel, every sheet of the workbook. Excel sheets are printed with "Fit All Columns on One Page", so a wide table isn't cut into column strips; a sheet whose author already chose a "Fit to" scaling keeps it.

**PDF by pages** makes a folder next to the original with a separate PDF for each part:

| From | Splits into | Example |
|---|---|---|
| Word | pages | `report (pages)/report - 01.pdf` |
| PowerPoint | slides | `deck (slides)/deck - 01.pdf` |
| Excel | sheets | `book (sheets)/book - 1 Sales.pdf` |

A long Excel sheet stays one file, however many pages it takes. Hidden and empty sheets are skipped. Characters a file name can't contain (`"` `<` `>` `|` and others) become `_`.

**Pages…** saves only the pages you list as one PDF next to the original, e.g. `report (pages 1-3, 5).pdf`. After picking the sector a small field opens at the cursor: type a range such as `1-3, 5, 8-` and press Enter (Esc cancels). `8-` runs to the last page, `-3` starts from the first; pages go in the order typed, repeats are dropped. With a single PDF the field shows its page count and won't accept a page that doesn't exist; with several PDFs the same range applies to each, and a file too short for it fails on its own. Bookmarks to the kept pages and the document info are preserved. This works without an office suite.

The office app is started in the background and quit afterwards if DropConvert launched it; files open in it aren't touched. On macOS the first conversion asks for permission to control Word / Excel / PowerPoint (System Settings → Privacy & Security → Automation).

## Working from the menu bar

DropConvert lives in the menu bar (macOS) or the tray (Windows) and works without a window:

1. In Finder / File Explorer, hold **⌘** (macOS) or **Alt** (Windows) and right-click a file. If several files are selected, the menu opens for the whole selection.
2. A wheel with formats that fit these files opens at the cursor; the regular context menu doesn't appear. If the selection mixes file types (say, images and video), pick the type first.
3. Click the sector with the format you want. From the keyboard: arrows and Enter, digits 1–9, or Esc to close. Clicking the center of the wheel opens the files in the DropConvert window.
4. The wheel collapses into a progress ring at the cursor: it shows the percentage and ends with a checkmark (or "!" if something failed). DropConvert shows no pop-up notifications: the icon's menu has "Show … in Finder" (File Explorer) for the last result and the text of the last error.

**Wheel while dragging.** Start dragging a file (from Finder, File Explorer or the desktop) and, without releasing the button, press **Tab**. The wheel opens under the cursor; drop the file on the sector you want. If you release outside the wheel or press Esc, the wheel closes and the file stays where it was. When dragging files of different types, hold them over a type sector ("Images", "Video") and it springs open into formats.

**Tab cycles wheels.** If the files include images, there is a "Tools" wheel besides "Convert". Tab opens the next wheel, Shift+Tab the previous one; this works both while dragging and in the ⌘ / Alt + right-click wheel. The current wheel is highlighted in the strip below it.

Image tools keep the format and put the result alongside, e.g. `photo (compressed).jpg`:

| Tool | What it does |
|---|---|
| Compress | re-encodes JPG, WEBP, HEIC, AVIF at lower quality; turns PNG into a 256-color palette. If the file doesn't get smaller, no copy is made |
| Strip metadata | removes EXIF, location and the like; EXIF rotation is applied to the image. A plain JPEG is saved without re-encoding |
| Trim | cuts transparent or solid-color borders |
| Crop | opens a preview to choose the area, see below |
| Rotate | 90° clockwise |
| B&W | grayscale |
| Fill | replaces transparency with white |

All tools except Strip keep EXIF (capture date, camera). Animated GIF and WEBP aren't supported by the tools yet.

**Crop** opens a window with the image as it will come out (EXIF rotation applied) and a frame on top. Drag the frame's corners or edges to resize it, drag inside to move it, or drag outside to draw a new one. The buttons below lock the proportions (Free, Original, 1:1, 4:3, 3:2, 16:9); ⇄ swaps portrait and landscape. Enter or Save keeps the crop, Esc or Cancel closes the window. With several images they come one by one: Save queues the crop and shows the next image, Skip moves on, Cancel stops (crops already saved still happen). The chosen ratio carries over to the next image. The copy is `photo (cropped).jpg`; a JPEG is re-encoded at quality 92.

Opening DropConvert doesn't show a window: it starts quietly in the menu bar (tray). The window opens from the icon's menu (on Windows, also by clicking the icon), from the center of the wheel, or by dropping files on the app icon. Launch at login can be turned on from the icon's menu. Closing the window doesn't quit the app; quit from the icon's menu.

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
DROPCONVERT_OFFICE_TESTS=1 .venv/bin/python -m pytest tests/test_office.py   # launches the real Office / LibreOffice
```

## Layout

```
main.py                    entry point
dropconvert/formats.py     supported formats
dropconvert/converters.py  conversion (Pillow + ffmpeg) and image tools
dropconvert/office.py      documents to PDF via Microsoft Office (AppleScript / COM) or LibreOffice
dropconvert/pdf.py         page ranges ("1-3, 5, 8-") and saving chosen PDF pages (pypdf)
dropconvert/app.py         drag & drop window (PySide6)
dropconvert/agent.py       menu bar icon, what the wheel shows (pages, tools), single instance
dropconvert/wheel.py       the wheel (painting, animation, mouse, keyboard, drops), the progress ring, the page range field
dropconvert/editor.py      the crop editor: preview, crop frame, ratio presets
dropconvert/trigger.py     intercepting ⌘ / Alt + right-click and Tab while dragging (CGEventTap / WH_*_LL hooks)
dropconvert/system.py      Finder / File Explorer selection, launch at login, Dock
DropConvert.spec           PyInstaller build config
```

To add a tool, add it to `TOOLS` in `formats.py`, to `TOOL_SUFFIXES` and `_apply_image_tool` in `converters.py`, and its labels to `_TOOLS` in `agent.py`.

To add a format, add it to `TARGETS` in `formats.py` and set its options in `_PIL_FORMATS` (images) or `FFMPEG_ARGS` (video and audio) in `converters.py`. A new document type goes into `KINDS` in `office.py`.
