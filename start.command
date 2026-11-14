#!/bin/bash
# Лаунчер BookSender для macOS / Linux.
# Сначала пробует локальное окружение .venv (Pillow/PyMuPDF из setup.py),
# затем любой Python с tkinter.
cd "$(dirname "$0")"
if [ -x ".venv/bin/python" ] && ".venv/bin/python" -c "import tkinter" >/dev/null 2>&1; then
  exec ".venv/bin/python" book_sender.py "$@"
fi
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c "import tkinter" >/dev/null 2>&1; then
    exec "$py" book_sender.py "$@"
  fi
done
echo "Не найден Python с tkinter."
echo "Установите Python 3.9+ (tkinter входит в python.org-дистрибутив;"
echo "на Linux: sudo apt install python3-tk) и выполните: python3 setup.py"
echo "Нажмите любую клавишу для выхода…"
read -n 1 -s
