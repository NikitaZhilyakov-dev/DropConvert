@echo off
rem Builds DropConvert.exe on Windows. Requires Python 3.10+ (python.org).
cd /d "%~dp0"
if not exist .venv py -3 -m venv .venv
.venv\Scripts\pip install -q -r requirements-dev.txt || exit /b 1
.venv\Scripts\pyinstaller DropConvert.spec --noconfirm || exit /b 1
echo Done: dist\DropConvert\DropConvert.exe
