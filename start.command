#!/bin/bash
# Лаунчер BookSender для macOS / Linux.
# При первом запуске сам ставит зависимости (setup.py), затем запускает приложение.
cd "$(dirname "$0")"
PY=".venv/bin/python"

# Авто-установка, если окружения нет или в нём не хватает зависимостей
if [ ! -x "$PY" ] || ! "$PY" -c "import PIL, pymupdf, tkinter" >/dev/null 2>&1; then
  echo "Первый запуск: устанавливаю зависимости…"
  BASE=""
  for b in python3 python; do
    if command -v "$b" >/dev/null 2>&1; then BASE="$b"; break; fi
  done
  if [ -z "$BASE" ]; then
    echo "Не найден Python. Установите Python 3.9+ (https://python.org) и повторите."
    echo "Нажмите любую клавишу для выхода…"
    read -n 1 -s
    exit 1
  fi
  "$BASE" setup.py || { echo "Установка не удалась."; read -n 1 -s; exit 1; }
  PY=".venv/bin/python"
fi

if [ ! -x "$PY" ]; then
  echo "Не удалось создать окружение .venv."
  echo "Нажмите любую клавишу для выхода…"
  read -n 1 -s
  exit 1
fi

exec "$PY" book_sender.py "$@"
