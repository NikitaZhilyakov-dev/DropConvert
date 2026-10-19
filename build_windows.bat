@echo off
rem Сборка DropConvert.exe на Windows. Нужен Python 3.10+ (python.org).
cd /d "%~dp0"
if not exist .venv py -3 -m venv .venv
.venv\Scripts\pip install -q -r requirements-dev.txt || exit /b 1
.venv\Scripts\pyinstaller DropConvert.spec --noconfirm || exit /b 1
echo Готово: dist\DropConvert\DropConvert.exe
