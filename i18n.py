"""Многоязычность BookSender.

Каталоги переводов — locales/<код>.json (плоский словарь ключ -> строка).
Добавление нового языка = новый JSON-файл, код менять не нужно.
Специальный ключ "_name" — самоназвание языка для селектора.
Подстановки — в фигурных скобках: t("key", name=...).

Автоопределение системного языка: переменные окружения LC_*/LANG,
на macOS — AppleLanguages, затем locale. Если языка нет среди каталогов —
откат на английский (FALLBACK), затем на первый доступный.
"""
from __future__ import annotations

import json
import locale
import os
import re
import subprocess
import sys
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
FALLBACK = "en"


def available_languages() -> dict:
    """{код: самоназвание} по файлам locales/*.json (без перебора в коде)."""
    langs = {}
    if LOCALES_DIR.is_dir():
        for p in sorted(LOCALES_DIR.glob("*.json")):
            code = p.stem
            try:
                data = json.loads(p.read_text("utf-8"))
                langs[code] = data.get("_name", code)
            except Exception:
                langs[code] = code
    return langs


def detect_system_language() -> str:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        v = os.environ.get(var)
        if v and v not in ("C", "POSIX"):
            code = re.split(r"[._@-]", v)[0].lower()
            if code:
                return code
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleLanguages"],
                capture_output=True, text=True, timeout=5).stdout
            m = re.search(r'"([A-Za-z]{2,3}(?:-[A-Za-z0-9]+)?)"', out)
            if m:
                return m.group(1).split("-")[0].lower()
        except Exception:
            pass
    try:
        loc = locale.getdefaultlocale()[0]
        if loc:
            return loc.split("_")[0].lower()
    except Exception:
        pass
    return FALLBACK


class I18N:
    def __init__(self, lang: str = "auto"):
        self._catalogs = {}
        self.lang = FALLBACK
        self.set_language(lang)

    def _load(self, code: str) -> dict:
        if code not in self._catalogs:
            p = LOCALES_DIR / f"{code}.json"
            try:
                self._catalogs[code] = json.loads(p.read_text("utf-8"))
            except Exception:
                self._catalogs[code] = {}
        return self._catalogs[code]

    def set_language(self, lang: str):
        avail = available_languages()
        if not avail:
            self.lang = FALLBACK
            return
        if lang == "auto" or lang not in avail:
            sys_lang = detect_system_language()
            lang = sys_lang if sys_lang in avail else FALLBACK
            if lang not in avail:
                lang = next(iter(avail))
        self.lang = lang
        self._load(lang)
        if FALLBACK in avail:
            self._load(FALLBACK)

    def t(self, key: str, **kw) -> str:
        s = self._load(self.lang).get(key)
        if s is None:
            s = self._load(FALLBACK).get(key, key)
        if kw:
            try:
                return s.format(**kw)
            except Exception:
                return s
        return s


i18n = I18N("auto")


def t(key: str, **kw) -> str:
    return i18n.t(key, **kw)


def set_language(lang: str):
    i18n.set_language(lang)


def current_language() -> str:
    return i18n.lang
