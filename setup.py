#!/usr/bin/env python3
"""Установка BookSender с нуля — кроссплатформенно (macOS / Windows / Linux).

Создаёт локальное окружение .venv, ставит Pillow и PyMuPDF (обработка картинок
и PDF). Дополнительно с флагом --xtc: клонирует XTC-конвертер и ставит его
зависимости (нужны Node.js 18+ и git).

Запуск:  python setup.py [--xtc]
После установки запускайте приложение через start.command (macOS/Linux)
или start.bat (Windows) — они сами используют .venv.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
VENDOR = HERE / "vendor" / "epub-to-xtc-converter"
CONVERTER_REPO = "https://github.com/bigbag/epub-to-xtc-converter"
FONT_URL = ("https://github.com/google/fonts/raw/main/"
            "ofl/ptserif/PT_Serif-Web-Regular.ttf")
FONT_NAME = "PTSerif-Regular.ttf"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def pick_base_python() -> str:
    """Базовый Python для .venv — желательно с tkinter (для GUI).
    На macOS/Linux python3 без tkinter — обычное дело, поэтому перебираем
    кандидатов и проверяем импорт."""
    cands = [sys.executable]
    for name in ("python3", "python3.13", "python3.12", "python3.11", "python"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    if sys.platform != "win32":
        cands.append("/usr/bin/python3")
    seen, ordered = set(), []
    for c in cands:
        rp = str(Path(c).resolve())
        if rp not in seen:
            seen.add(rp)
            ordered.append(c)
    for c in ordered:
        try:
            r = subprocess.run([c, "-c", "import tkinter"], capture_output=True)
            if r.returncode == 0:
                print(f"✓ базовый Python с tkinter: {c}")
                return c
        except OSError:
            continue
    print("⚠ ни один Python не имеет tkinter — GUI не запустится из .venv;")
    print("  CLI будет работать, а GUI возьмёт другой Python (см. start.*).")
    return sys.executable


def make_venv():
    if venv_python().exists():
        print("✓ .venv уже существует")
        return
    base = pick_base_python()
    run([base, "-m", "venv", str(VENV)])
    print("✓ создано окружение .venv")


def install_deps():
    py = str(venv_python())
    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "pillow", "pymupdf"])
    print("✓ Pillow (картинки) и PyMuPDF (PDF) установлены")


def download_font():
    """Свободный шрифт PT Serif (OFL) с кириллицей для XTC-рендера.
    Используется только конвертером XTC; если шрифта нет, берётся
    системный (см. FONT_CANDIDATES)."""
    fonts = HERE / "fonts"
    fonts.mkdir(exist_ok=True)
    font = fonts / FONT_NAME
    if font.exists():
        print("✓ шрифт PT Serif уже есть")
        return
    print("Скачиваю свободный шрифт PT Serif (OFL)…")
    urllib.request.urlretrieve(FONT_URL, font)
    print("✓ шрифт сохранён в fonts/")


def setup_xtc():
    if not shutil.which("node") or not shutil.which("npm"):
        print("⚠ Node.js 18+ и npm не найдены — XTC-конвертер пропущен.")
        print("  Установите Node (https://nodejs.org) и запустите ещё раз:")
        print(f"  {venv_python()} {Path(__file__).name} --xtc")
        return False
    if not VENDOR.exists():
        if not shutil.which("git"):
            print("⚠ git не найден — XTC-конвертер пропущен.")
            return False
        run(["git", "clone", "--depth", "1", CONVERTER_REPO, str(VENDOR)])
    run(["npm", "install", "--no-audit", "--no-fund"], cwd=str(VENDOR / "cli"))
    print("✓ XTC-конвертер установлен")
    return True


def main():
    ap = argparse.ArgumentParser(description="Установка BookSender с нуля")
    ap.add_argument("--xtc", action="store_true",
                    help="также установить XTC-конвертер (Node.js 18+, git)")
    args = ap.parse_args()

    make_venv()
    install_deps()
    download_font()

    if args.xtc:
        if not setup_xtc():
            print("⚠ XTC-конвертер не установлен")
        else:
            (HERE / "fonts").mkdir(exist_ok=True)
            download_font()

    py = str(venv_python())
    print("\n=== Готово ===")
    print("Запуск:")
    if os.name == "nt":
        print(f"  start.bat   или: {py} book_sender.py")
    else:
        print(f"  ./start.command   или: {py} book_sender.py")
    if not args.xtc:
        print("Для поддержки XTC/XTCH дополнительно: python setup.py --xtc")


if __name__ == "__main__":
    main()
