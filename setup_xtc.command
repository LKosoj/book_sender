#!/bin/bash
# Обёртка установки XTC-конвертера (macOS / Linux).
# Сама установка кроссплатформенная: setup.py --xtc
cd "$(dirname "$0")"
PY=".venv/bin/python"; [ -x "$PY" ] || PY=python3
"$PY" setup.py --xtc
echo "Нажмите любую клавишу для выхода…"
read -n 1 -s
