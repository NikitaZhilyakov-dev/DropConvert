#!/bin/bash
# Сборка DropConvert.app на macOS. Нужен Python 3.10+.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
[ -d .venv ] || "$PYTHON" -m venv .venv
.venv/bin/pip install -q -r requirements-dev.txt
.venv/bin/pyinstaller DropConvert.spec --noconfirm
echo "Готово: dist/DropConvert.app"
