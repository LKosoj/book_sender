#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BookSender — массовая конвертация книг в EPUB и отправка на читалку
Xteink X4 Pro с прошивкой CrossPoint (режим File Transfer).

Что делает:
  1. Сканирует папку с книгами (fb2, fb2.zip, fbz, zip/7z/rar, epub, docx, odt,
     rtf, html, md, txt; pdf/mobi/azw3 — если установлен Calibre).
  2. СРАЗУ конвертирует всё в EPUB через pandoc (картинки и обложки сохраняются).
  3. Ждёт появления читалки в сети, повторяя поиск каждые 5 секунд
     (UDP-поиск CrossPoint, mDNS crosspoint.local, сканирование подсети),
     и загружает книги по HTTP в папку /Books. Ждёт бесконечно, пока не
     нажмут «Стоп» (CLI: --wait-timeout).

Зависимости: только стандартная библиотека Python 3.9+ и внешний pandoc.
Опционально: Calibre (ebook-convert) для PDF, 7z для архивов .7z/.rar.

Запуск GUI:   python book_sender.py
Запуск CLI:   python book_sender.py --cli --folder ~/Books --host 192.168.1.50
Поиск:        python book_sender.py --find

Протокол устройства: docs/webserver-endpoints.md репозитория
https://github.com/crosspoint-reader/crosspoint-reader
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
import posixpath
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import error, parse, request

from i18n import t, set_language, current_language, available_languages

APP_NAME = "BookSender"
# Путь к настройкам: можно переопределить переменной окружения
# BOOKSENDER_SETTINGS (используется тестами, чтобы не трогать ваши настройки)
_settings_env = os.environ.get("BOOKSENDER_SETTINGS")
SETTINGS_FILE = (Path(_settings_env) if _settings_env
                 else Path(__file__).resolve().with_name("book_sender_settings.json"))
VENDOR_CLI = (Path(__file__).resolve().parent / "vendor"
              / "epub-to-xtc-converter" / "cli")
XTC_SETTINGS = Path(__file__).resolve().with_name("xtc_settings.json")
def _font_candidates() -> list:
    """Шрифты с кириллицей для XTC-рендера: сначала свободные из fonts/
    (PT Serif, OFL), затем системные — отдельно для каждой ОС."""
    here = Path(__file__).resolve().parent
    cands = sorted((here / "fonts").glob("*.ttf"))
    if sys.platform == "darwin":
        cands += [Path("/System/Library/Fonts/Supplemental/Georgia.ttf"),
                  Path("/System/Library/Fonts/Supplemental/Times New Roman.ttf"),
                  Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")]
    elif sys.platform == "win32":
        fdir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        cands += [fdir / "georgia.ttf", fdir / "times.ttf", fdir / "arial.ttf"]
    else:
        cands += [Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),
                  Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"),
                  Path("/usr/share/fonts/TTF/DejaVuSerif.ttf")]
    return cands


FONT_CANDIDATES = _font_candidates()

# Форматы, которые читает pandoc (расширение -> входной формат pandoc)
PANDOC_FORMATS = {
    ".fb2": "fb2",
    ".docx": "docx",
    ".odt": "odt",
    ".rtf": "rtf",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "markdown",
}
# Форматы, которые умеет только Calibre (ebook-convert)
CALIBRE_EXTS = {".pdf", ".mobi", ".azw", ".azw3"}
# .fbz — zip с fb2 под другим именем (так отдаёт Flibusta)
ARCHIVE_EXTS = {".zip", ".fbz", ".7z", ".rar"}
EPUB_EXT = ".epub"
# Нативные форматы читалки: отдаём как есть, без конвертации
NATIVE_EXTS = {".epub", ".xtc", ".xtch"}
# Понятные подписи форматов в интерфейсе (зависят от языка)
def format_labels() -> dict:
    return {"epub": t("fmt.epub"), "xtc": t("fmt.xtc"), "xtch": t("fmt.xtch")}


def label_to_format(label: str) -> str:
    return {v: k for k, v in format_labels().items()}.get(label, "epub")


def selected_subset(files, formats, iids, selected):
    """Возвращает (files, formats, iids) только для выделенных строк;
    если ничего не выделено — все. Сохраняет порядок выделения."""
    if not selected:
        return list(files), list(formats), list(iids)
    idxs = [iids.index(r) for r in selected if r in iids]
    return ([files[i] for i in idxs],
            [formats[i] if i < len(formats) else "epub" for i in idxs],
            [iids[i] for i in idxs])
# Размеры шрифта как в прошивке (встроенный Noto Sans: 12/14/16/18 pt)
XTC_FONT_PT_SIZES = (12, 14, 16, 18)
SCREEN_PPI = 217  # 4.3", 800×480 -> px = pt × 217/72

FB2_NS = {
    "fb": "http://www.gribuser.ru/xml/fictionbook/2.0",
    "l": "http://www.w3.org/1999/xlink",
}
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

DISCOVERY_PORT = 8134  # UDP-поиск CrossPoint: "hello" -> "crosspoint (on <host>);81"


class Cancelled(Exception):
    """Пользователь нажал «Стоп»."""


class ToolError(Exception):
    """Ошибка внешнего инструмента (pandoc / calibre / 7z)."""


class Skipped(Exception):
    """Файл пропущен: формат не поддерживается или нет опционального инструмента."""


class DeviceError(Exception):
    """Ошибка общения с читалкой."""


class FileExistsError_(DeviceError):
    """Файл уже есть на устройстве (без overwrite)."""


# ---------------------------------------------------------------- инструменты

def find_tool(name: str, extra_paths=()) -> str | None:
    p = shutil.which(name)
    if p:
        return p
    for cand in extra_paths:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _calibre_paths() -> list:
    if sys.platform == "darwin":
        return ["/Applications/calibre.app/Contents/MacOS/ebook-convert"]
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        return [str(Path(pf) / "Calibre2" / "ebook-convert.exe"),
                str(Path(pf86) / "Calibre2" / "ebook-convert.exe")]
    return ["/usr/bin/ebook-convert", "/opt/calibre/ebook-convert"]


def _7z_paths() -> list:
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        return [str(Path(pf) / "7-Zip" / "7z.exe"),
                str(Path(pf86) / "7-Zip" / "7z.exe")]
    if sys.platform == "darwin":
        return ["/opt/homebrew/bin/7z", "/usr/local/bin/7z"]
    return ["/usr/bin/7z", "/usr/local/bin/7z"]


def _djvu_paths(name: str) -> list:
    if sys.platform == "darwin":
        return [f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"]
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        return [str(Path(pf) / "DjVuLibre" / f"{name}.exe")]
    return [f"/usr/bin/{name}"]


def detect_tools() -> dict:
    return {
        "pandoc": find_tool("pandoc"),
        "calibre": find_tool("ebook-convert", _calibre_paths()),
        "7z": find_tool("7z", _7z_paths())
        or find_tool("7za")
        or find_tool("unar"),
        "pillow": importlib.util.find_spec("PIL") is not None,
        "pymupdf": importlib.util.find_spec("pymupdf") is not None,
        "node": find_tool("node"),
        "ddjvu": find_tool("ddjvu", _djvu_paths("ddjvu")),
        "djvused": find_tool("djvused", _djvu_paths("djvused")),
        "xtc": str(VENDOR_CLI / "index.js")
        if (VENDOR_CLI / "index.js").is_file()
        and (VENDOR_CLI / "node_modules").is_dir() else None,
    }


def run_cmd(cmd, cwd=None, timeout=600, stop: threading.Event | None = None):
    """Запуск внешней программы с возможностью отмены и таймаутом."""
    try:
        p = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as e:
        raise ToolError(t("err.run_fail", cmd=cmd[0], error=e)) from e
    t0 = time.monotonic()
    while True:
        if stop is not None and stop.is_set():
            p.kill()
            p.communicate()
            raise Cancelled()
        try:
            out, err = p.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() - t0 > timeout:
                p.kill()
                p.communicate()
                raise ToolError(t("err.timeout", timeout=timeout, cmd=cmd[0]))
    if p.returncode != 0:
        msg = (err or out or b"").decode("utf-8", "replace").strip()
        raise ToolError(f"{cmd[0]}: {msg[-800:]}")
    return out, err


# ------------------------------------------------------------------- имена

def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Имя файла, безопасное для FAT32 на SD-карте читалки."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name or "book"


def unique_name(name: str, used: set) -> str:
    stem, ext = os.path.splitext(name)
    candidate = name
    i = 2
    while candidate.lower() in used:
        candidate = f"{stem} ({i}){ext}"
        i += 1
    used.add(candidate.lower())
    return candidate


def author_folder_name(author: str) -> str:
    """Имя папки автора — ТОЧНО как в плагине organize-by-author:
    запрещённые символы -> пробел, схлопнуть пробелы, обрезка до 60 символов."""
    name = re.sub(r'[/\\:*?"<>|]', " ", author or "")
    name = re.sub(r"[\x00-\x1f\x7f]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^\.+|\.+$", "", name).strip()
    return name[:60]


def epub_author(path: Path) -> str:
    """Автор из OPF готового EPUB — в том же порядке, что читает плагин
    organize-by-author: атрибут file-as -> meta property=file-as -> текст
    первого dc:creator. Пустая строка, если автора нет."""
    try:
        with zipfile.ZipFile(path) as z:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            rf = next((el for el in container.iter()
                       if el.tag.split("}")[-1] == "rootfile"), None)
            if rf is None or not rf.get("full-path"):
                return ""
            opf = ET.fromstring(z.read(rf.get("full-path")))
            creator = next((el for el in opf.iter()
                            if el.tag.split("}")[-1] == "creator"), None)
            if creator is None:
                return ""
            file_as = (creator.get("{http://www.idpf.org/2007/opf}file-as")
                       or creator.get("file-as"))
            if file_as:
                return file_as.strip()
            cid = creator.get("id")
            if cid:
                for el in opf.iter():
                    if (el.tag.split("}")[-1] == "meta"
                            and el.get("property") == "file-as"
                            and el.get("refines") == "#" + cid
                            and (el.text or "").strip()):
                        return el.text.strip()
            return (creator.text or "").strip()
    except Exception:
        return ""


def validate_epub(path: Path) -> dict:
    """Лёгкая проверка структуры EPUB (не полный epubcheck): целостность zip,
    mimetype, container.xml, OPF и непустой spine. Возвращает
    {"problems": [...], "fatal": bool}. fatal=True — книга не откроется,
    такие не отправляем; мелкие огрехи (mimetype) лечит оптимизатор читалки."""
    problems = []
    fatal = False
    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
            if bad is not None:
                problems.append(t("epub.bad_entry", name=bad))
                fatal = True
            names = z.namelist()
            low = {n.lower(): n for n in names}
            if "mimetype" not in low:
                problems.append(t("epub.no_mimetype"))
            else:
                mt = z.read(low["mimetype"]).decode("ascii", "replace").strip()
                if mt != "application/epub+zip":
                    problems.append(t("epub.bad_mimetype", mt=mt))
            cont = low.get("meta-inf/container.xml")
            opf = None
            if not cont:
                problems.append(t("epub.no_container"))
                fatal = True
            else:
                try:
                    cxml = z.read(cont).decode("utf-8", "replace")
                    m = re.search(r'full-path=["\']([^"\']+)["\']', cxml)
                    if m and m.group(1) in names:
                        opf = m.group(1)
                except Exception:
                    pass
                if opf is None:
                    opf = next((n for n in names if n.lower().endswith(".opf")), None)
                if opf is None:
                    problems.append(t("epub.no_opf"))
                    fatal = True
                else:
                    try:
                        oxml = z.read(opf).decode("utf-8", "replace").lower()
                        if "<spine" not in oxml or "itemref" not in oxml:
                            problems.append(t("epub.empty_spine"))
                            fatal = True
                    except Exception:
                        problems.append(t("epub.no_opf"))
                        fatal = True
    except zipfile.BadZipFile:
        problems.append(t("epub.not_zip"))
        fatal = True
    except Exception as e:
        problems.append(t("epub.read_error", error=e))
        fatal = True
    return {"problems": problems, "fatal": fatal}


# --------------------------------------------------------------- метаданные

def fb2_meta(path: Path) -> dict:
    """Название, автор, язык и обложка из FB2 (для имени файла и EPUB)."""
    try:
        return fb2_meta_bytes(path.read_bytes())
    except OSError:
        return {}


def fb2_meta_bytes(data: bytes) -> dict:
    """fb2_meta для уже прочитанных байт (файл или член zip-архива)."""
    meta: dict = {}
    try:
        root = ET.fromstring(data)
    except Exception:
        return meta
    ti = root.find("fb:description/fb:title-info", FB2_NS)
    if ti is None:
        return meta
    meta["title"] = (ti.findtext("fb:book-title", "", FB2_NS) or "").strip()
    authors = []
    for a in ti.findall("fb:author", FB2_NS):
        parts = [
            (a.findtext(f"fb:{k}", "", FB2_NS) or "").strip()
            for k in ("last-name", "first-name", "middle-name")
        ]
        full = " ".join(p for p in parts if p)
        if full:
            authors.append(full)
    if authors:
        meta["author"] = authors[0]
    meta["lang"] = (ti.findtext("fb:lang", "", FB2_NS) or "").strip()
    img = ti.find("fb:coverpage/fb:image", FB2_NS)
    if img is not None:
        href = img.get(XLINK_HREF) or img.get("href") or ""
        if href.startswith("#"):
            bid = href[1:]
            for b in root.findall("fb:binary", FB2_NS):
                if b.get("id") == bid and b.text:
                    ctype = (b.get("content-type") or "image/jpeg").lower()
                    ext = {
                        "image/png": ".png",
                        "image/gif": ".gif",
                        "image/jpeg": ".jpg",
                        "image/jpg": ".jpg",
                    }.get(ctype, ".jpg")
                    try:
                        meta["cover"] = base64.b64decode(b.text)
                        meta["cover_ext"] = ext
                    except Exception:
                        pass
                    break
    return meta


def zip_fb2_bytes(path: Path) -> bytes | None:
    """Первый .fb2 внутри zip/fbz (для превью; 7z/rar ради превью не читаем)."""
    try:
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
                if name.lower().endswith(".fb2") and not name.startswith("._"):
                    with z.open(info) as f:
                        return f.read()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return None
    return None


def epub_preview(path: Path) -> dict:
    """Обложка + название/автор EPUB одним чтением архива (для превью):
    {"cover": bytes, "cover_ext": str, "title": str, "author": str}."""
    out: dict = {}
    try:
        with zipfile.ZipFile(path) as z:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            rf = next((el for el in container.iter()
                       if el.tag.split("}")[-1] == "rootfile"), None)
            if rf is None or not rf.get("full-path"):
                return out
            opf_path = rf.get("full-path")
            opf = ET.fromstring(z.read(opf_path))
            base = posixpath.dirname(opf_path)
            metadata = next((el for el in opf.iter()
                             if el.tag.split("}")[-1] == "metadata"), None)
            title = author = cover_id = ""
            if metadata is not None:
                for el in metadata.iter():
                    ln = el.tag.split("}")[-1]
                    value = (el.text or "").strip()
                    if ln == "title" and not title and value:
                        title = value
                    elif ln == "creator" and not author and value:
                        author = value
                    elif (ln == "meta"
                          and (el.get("name") or "").lower() == "cover"):
                        cover_id = el.get("content", "")
            manifest = next((el for el in opf.iter()
                             if el.tag.split("}")[-1] == "manifest"), None)
            href = None
            if manifest is not None:
                items = [el for el in manifest
                         if el.tag.split("}")[-1] == "item"]
                # EPUB 3: properties="cover-image"; EPUB 2: meta name="cover"
                for item in items:
                    if ("cover-image" in (item.get("properties") or "").split()
                            or (cover_id and item.get("id") == cover_id)):
                        href = item.get("href")
                        break
                if not href:
                    # запасной случай: картинка с id вроде "cover"
                    for item in items:
                        if ("cover" in (item.get("id") or "").lower()
                                and (item.get("media-type") or "")
                                .startswith("image/")):
                            href = item.get("href")
                            break
            if href:
                member = posixpath.normpath(
                    posixpath.join(base, parse.unquote(href)))
                if not member.startswith("..") and not member.startswith("/"):
                    ext = posixpath.splitext(member)[1].lower()
                    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                        ext = ".jpg"
                    try:
                        out["cover"] = z.read(member)
                        out["cover_ext"] = ext
                    except KeyError:
                        pass
            if title:
                out["title"] = title
            if author:
                out["author"] = author
    except (OSError, zipfile.BadZipFile, ET.ParseError, StopIteration):
        return out
    return out


def pdf_cover(path: Path) -> dict:
    """Первая страница PDF как «обложка» (PNG) + название/автор из метаданных."""
    out: dict = {}
    try:
        import pymupdf
    except ImportError:
        return out
    try:
        with pymupdf.open(path) as doc:
            if doc.page_count:
                zoom = 280 / max(doc[0].rect.height, 1.0)  # высота превью ~280px
                pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
                out["cover"] = pix.tobytes("png")
                out["cover_ext"] = ".png"
            m = doc.metadata or {}
            if (m.get("title") or "").strip():
                out["title"] = m["title"].strip()
            if (m.get("author") or "").strip():
                out["author"] = m["author"].strip()
    except Exception:
        return {}
    return out


def book_cover(path: Path) -> dict:
    """Данные для превью обложки в GUI. Формат результата как у fb2_meta:
    cover/cover_ext/title/author (см. epub_preview). Пустой dict, если
    обложки нет. Никогда не бросает исключений — превью не должно ломать GUI."""
    try:
        ext = path.suffix.lower()
        if ext == ".fb2":
            return {k: v for k, v in fb2_meta(path).items() if v}
        if ext in (".zip", ".fbz"):
            data = zip_fb2_bytes(path)
            if data:
                return {k: v for k, v in fb2_meta_bytes(data).items() if v}
            return {}
        if ext == ".epub":
            return epub_preview(path)
        if ext == ".pdf":
            return pdf_cover(path)
    except Exception:
        return {}
    return {}


# -------------------------------------------------------------- конвертация

def extract_archive(path: Path, dest: Path, tools: dict, log, stop=None):
    """Распаковка zip/fbz (штатно) и 7z/rar (внешним 7z). Возвращает список файлов."""
    ext = path.suffix.lower()
    if ext in (".zip", ".fbz"):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                # Русские zip часто хранят имена в cp866 без флага UTF-8
                if not (info.flag_bits & 0x800):
                    try:
                        fixed = name.encode("cp437").decode("cp866")
                        if re.search(r"[а-яА-ЯёЁ]", fixed):
                            name = fixed
                    except (UnicodeEncodeError, UnicodeDecodeError):
                        pass
                name = name.replace("\\", "/")
                if name.startswith("__MACOSX/") or "/._" in name or name.startswith("._"):
                    continue
                target = (dest / name).resolve()
                if not str(target).startswith(str(dest.resolve()) + os.sep):
                    continue  # защита от zip-slip
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as fsrc, open(target, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
    elif ext in (".7z", ".rar"):
        if not tools.get("7z"):
            raise ToolError(t("err.7z_needed"))
        sevenz = tools["7z"]
        if Path(sevenz).name == "unar":
            run_cmd([sevenz, "-f", "-o", str(dest), str(path)], timeout=300, stop=stop)
        else:
            run_cmd([sevenz, "x", "-y", f"-o{dest}", str(path)], timeout=300, stop=stop)
    else:
        raise ToolError(t("err.unknown_archive", name=path.name))
    return sorted(p for p in dest.rglob("*") if p.is_file())


def ensure_utf8_fb2(path: Path, tmp_dir: Path) -> Path:
    """pandoc понимает FB2 только в UTF-8: cp1251 превращается в кракозябры
    и в метаданных, и в тексте. Перекодируем такие файлы во временный UTF-8."""
    data = path.read_bytes()
    m = re.match(rb'\s*<\?xml[^>]*?encoding\s*=\s*["\']([^"\']+)["\']',
                 data[:300], re.IGNORECASE)
    enc = m.group(1).decode("ascii", "replace").strip().lower() if m else None
    if enc in ("utf-8", "utf8"):
        return path
    if enc is None:
        try:
            data.decode("utf-8")
            return path
        except UnicodeDecodeError:
            enc = "cp1251"  # самая частая кодировка русских FB2 без декларации
    try:
        text = data.decode(enc)
    except (UnicodeDecodeError, LookupError):
        return path
    text = re.sub(r'(<\?xml[^>]*?encoding\s*=\s*["\'])[^"\']+(["\'])',
                  r"\1utf-8\2", text, count=1, flags=re.IGNORECASE)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / (path.stem + "_utf8.fb2")
    tmp.write_text(text, "utf-8")
    return tmp


def convert_one(book: Path, out_dir: Path, tools: dict, log, used_names: set,
                stop=None) -> Path:
    """Одна книга -> EPUB в out_dir. Возвращает путь к готовому EPUB."""
    ext = book.suffix.lower()
    out_dir.mkdir(parents=True, exist_ok=True)

    if ext in NATIVE_EXTS:
        name = unique_name(sanitize_filename(book.stem) + ext, used_names)
        out = out_dir / name
        shutil.copyfile(book, out)
        return out

    if ext in PANDOC_FORMATS:
        if not tools.get("pandoc"):
            raise ToolError(t("err.pandoc_missing"))
        fmt = PANDOC_FORMATS[ext]
        title = book.stem
        cover_path = None
        if fmt == "fb2":
            book = ensure_utf8_fb2(book, out_dir)
            meta = fb2_meta(book)
            if meta.get("title"):
                title = meta["title"]
                if meta.get("author"):
                    title = f"{meta['author']} - {title}"
            if meta.get("cover"):
                cover_path = out_dir / (sanitize_filename(book.stem) + "_cover"
                                        + meta.get("cover_ext", ".jpg"))
                cover_path.write_bytes(meta["cover"])
        name = unique_name(sanitize_filename(title) + ".epub", used_names)
        out = out_dir / name
        cmd = [tools["pandoc"], str(book), "-f", fmt, "-t", "epub", "-o", str(out)]
        if fmt == "fb2":
            if cover_path:
                cmd += ["--epub-cover-image", str(cover_path)]
        else:
            cmd += ["--metadata", f"title={title}"]
        # cwd = папка книги: pandoc подхватит относительные картинки в html
        run_cmd(cmd, cwd=str(book.parent), timeout=600, stop=stop)
        if cover_path:
            cover_path.unlink(missing_ok=True)
        if not out.is_file() or out.stat().st_size == 0:
            raise ToolError(t("err.pandoc_no_epub"))
        return out

    if ext in CALIBRE_EXTS:
        if not tools.get("calibre"):
            raise Skipped(t("err.calibre_only", ext=ext))
        name = unique_name(sanitize_filename(book.stem) + ".epub", used_names)
        out = out_dir / name
        run_cmd([tools["calibre"], str(book), str(out)], timeout=900, stop=stop)
        if not out.is_file() or out.stat().st_size == 0:
            raise ToolError(t("err.calibre_no_epub"))
        return out

    raise Skipped(t("err.unsupported", ext=ext))


def process_source(src: Path, tmp_root: Path, tools: dict, log, used_names: set,
                   stop=None, depth=0) -> list:
    """Один исходный файл (возможно архив) -> список готовых EPUB."""
    if stop is not None and stop.is_set():
        raise Cancelled()
    ext = src.suffix.lower()
    if ext in ARCHIVE_EXTS:
        if depth >= 2:
            log(t("log.nested_archive", name=src.name))
            return []
        ex_dir = tmp_root / f"ex_{uuid.uuid4().hex[:8]}"
        ex_dir.mkdir(parents=True, exist_ok=True)
        try:
            inner = extract_archive(src, ex_dir, tools, log, stop)
        except zipfile.BadZipFile as e:
            raise ToolError(t("err.bad_zip", error=e)) from e
        result = []
        for f in inner:
            if stop is not None and stop.is_set():
                raise Cancelled()
            try:
                result.extend(
                    process_source(f, tmp_root, tools, log, used_names, stop, depth + 1)
                )
            except Skipped as e:
                log(t("log.inner_skipped", name=f.name, error=e))
            except ToolError as e:
                log(f"  ⚠ {f.name}: {e}")
        return result
    out_dir = tmp_root / "epub"
    return [convert_one(src, out_dir, tools, log, used_names, stop)]


# ------------------------------------------------- оптимизация EPUB до отправки
# Порт ключевых шагов встроенного оптимизатора CrossPoint
# (src/network/html/js/epub-optimizer.js): защитный CSS, удаление встроенных
# шрифтов (прошивка их не использует), снятие width/height у img.
# Обработка картинок (оттенки серого, ≤800×480) — опционально, если установлен Pillow.

DEFENSIVE_STYLE = ('<style type="text/css">img,svg{max-width:100%;height:auto}'
                   'body{overflow-wrap:break-word}table{max-width:100%;'
                   'table-layout:fixed}pre,code{white-space:pre-wrap;'
                   'word-wrap:break-word}*{box-sizing:border-box}</style>')
FONT_EXT_RE = re.compile(r"\.(ttf|otf|woff2?|eot)$", re.I)
FONT_MEDIA_RE = re.compile(
    r"^(font/|application/font-|application/x-font-|"
    r"application/vnd\.ms-(opentype|fontobject)$)", re.I)
FONT_FACE_RE = re.compile(r"@font-face(?:\s|/\*[\s\S]*?\*/)*\{[^{}]*\}", re.I)
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
# «Вредный» для e-ink CSS — как в оптимизаторе bigbag/epub-to-xtc-converter
PROBLEM_CSS_RE = [
    re.compile(r"float\s*:\s*[^;}]+;?", re.I),
    re.compile(r"position\s*:\s*(?:fixed|absolute|relative|sticky)[^;}]*;?", re.I),
    re.compile(r"display\s*:\s*(?:flex|grid|inline-flex|inline-grid)[^;}]*;?", re.I),
]
STYLE_ATTR_RE = re.compile(r'style\s*=\s*"([^"]*)"', re.I)
SCREEN_BOX = (800, 480)  # экран X4 Pro; картинки вписываем, не увеличивая
PREVIEW_BOX = (200, 280)  # превью обложки в GUI


def strip_xml_comments(t: str):
    """Удаление <!-- --> комментариев (порт stripComments из оптимизатора
    прошивки): CDATA, <?...?> и DOCTYPE не трогаем."""
    out = []
    i = 0
    count = 0
    while i < len(t):
        nxt = t.find("<", i)
        if nxt < 0:
            out.append(t[i:])
            break
        if nxt > i:
            out.append(t[i:nxt])
        if t.startswith("<!--", nxt):
            close = t.find("-->", nxt + 4)
            i = len(t) if close < 0 else close + 3
            count += 1
            continue
        if t.startswith("<!DOCTYPE", nxt):
            gt = t.find(">", nxt)
            subset = t.find("[", nxt)
            if subset < 0 or (gt >= 0 and subset > gt):
                end = len(t) if gt < 0 else gt + 1
            else:
                close = t.find("]>", subset)
                end = len(t) if close < 0 else close + 2
            out.append(t[nxt:end])
            i = end
            continue
        skipped = False
        for opener, closer in (("<![CDATA[", "]]>"), ("<?", "?>")):
            if t.startswith(opener, nxt):
                close = t.find(closer, nxt + len(opener))
                end = len(t) if close < 0 else close + len(closer)
                out.append(t[nxt:end])
                i = end
                skipped = True
                break
        if skipped:
            continue
        out.append("<")
        i = nxt + 1
    return "".join(out), count


# Символы, которых обычно нет в шрифтах читалок -> безопасные ASCII-аналоги
SYMBOL_MAP = {
    "→": "->", "←": "<-", "↔": "<->", "↦": "->", "↣": "->",
    "⇒": "=>", "⇐": "<=", "⇔": "<=>",
    "➜": "->", "➔": "->", "➤": "->", "►": "->",
    "✓": "v", "✔": "v", "☑": "v",
    "✗": "x", "✘": "x", "☒": "x",
}


def ascii_symbols_epub(path: Path) -> int:
    """Заменяет в XHTML/HTML содержимом EPUB символы, которых обычно нет
    в шрифтах читалок (стрелки, галочки), на ASCII-аналоги.
    Возвращает число замен (0 — если менять нечего)."""
    replaced = 0
    data = {}
    with zipfile.ZipFile(path) as z:
        for item in z.infolist():
            data[item.filename] = z.read(item.filename)
    for name, blob in list(data.items()):
        if not name.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        text = None
        for enc in ("utf-8", "cp1252"):
            try:
                text = blob.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            continue
        new = "".join(SYMBOL_MAP.get(ch, ch) for ch in text)
        if new != text:
            replaced += sum(text.count(sym) for sym in SYMBOL_MAP)
            data[name] = new.encode("utf-8")
    if replaced:
        tmp = path.with_name(path.name + ".sym.tmp")
        with zipfile.ZipFile(tmp, "w") as z:
            if "mimetype" in data:  # по спецификации EPUB — первым, без сжатия
                z.writestr(zipfile.ZipInfo("mimetype"), data["mimetype"],
                           compress_type=zipfile.ZIP_STORED)
            for n, b in data.items():
                if n != "mimetype":
                    z.writestr(n, b, compress_type=zipfile.ZIP_DEFLATED)
        tmp.replace(path)
    return replaced


def optimize_epub(path: Path, optimize_images: bool = False) -> dict:
    """Оптимизирует EPUB на месте. Возвращает статистику; при любой ошибке
    оставляет исходный файл нетронутым."""
    stats = {"fonts": 0, "css": 0, "css_clean": 0, "comments": 0,
             "img_attrs": 0, "images": 0, "repairs": 0,
             "before": path.stat().st_size, "after": path.stat().st_size}
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        data = {}
        unreadable = []
        for n in names:
            try:
                data[n] = z.read(n)
            except Exception:
                unreadable.append(n)
    # Битые встроенные шрифты просто выбрасываем (прошивка их не использует).
    # Если повреждено что-то другое — оптимизацию пропускаем целиком.
    dropped_broken_fonts = False
    for n in unreadable:
        if FONT_EXT_RE.search(n):
            stats["fonts"] += 1
            dropped_broken_fonts = True
        else:
            raise ValueError(t("err.bad_epub_file", name=n))

    def dec(b: bytes) -> str:
        if b.startswith(b"\xef\xbb\xbf"):
            b = b[3:]
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return b.decode("cp1252", "replace")

    opf_name = None
    for n in names:
        if n.lower() == "meta-inf/container.xml":
            m = re.search(r'full-path=["\']([^"\']+)["\']', dec(data[n]))
            if m and m.group(1) in data:
                opf_name = m.group(1)
            break
    if opf_name is None:
        opf_name = next((n for n in names if n.lower().endswith(".opf")), None)
    opf_dir = opf_name.rsplit("/", 1)[0] if opf_name and "/" in opf_name else ""

    def resolve(href: str) -> str:
        href = parse.unquote(href)
        parts = (opf_dir + "/" + href).split("/") if opf_dir else href.split("/")
        out = []
        for p in parts:
            if p == "..":
                if out:
                    out.pop()
            elif p not in ("", "."):
                out.append(p)
        return "/".join(out)

    font_paths = {n for n in names if FONT_EXT_RE.search(n)}
    if opf_name:
        for m in re.finditer(r"<item\b[^>]*>", dec(data[opf_name])):
            tag = m.group(0)
            mt = re.search(r'media-type=["\']([^"\']+)["\']', tag)
            hf = re.search(r'href=["\']([^"\']+)["\']', tag)
            if mt and hf and FONT_MEDIA_RE.match(mt.group(1)):
                rp = resolve(hf.group(1))
                if rp in data:
                    font_paths.add(rp)

    pil = None
    if optimize_images:
        try:
            from PIL import Image
            pil = Image
        except ImportError:
            pil = None

    changed = dropped_broken_fonts
    for n in list(data.keys()):
        low = n.lower()
        if n in font_paths:
            del data[n]
            stats["fonts"] += 1
            changed = True
        elif low.endswith((".xhtml", ".html", ".htm")):
            text = dec(data[n])
            orig = text
            text, ncomments = strip_xml_comments(text)
            stats["comments"] += ncomments

            def _clean_style(m):
                s = m.group(1)
                for rx in PROBLEM_CSS_RE:
                    s2 = rx.sub("", s)
                    if s2 != s:
                        stats["css_clean"] += 1
                    s = s2
                return 'style="' + s + '"'

            text = STYLE_ATTR_RE.sub(_clean_style, text)
            if DEFENSIVE_STYLE not in text:
                m = re.search(r"</head>", text, re.I)
                if m:
                    text = text[:m.start()] + DEFENSIVE_STYLE + text[m.start():]
                else:
                    m2 = re.search(r"<body\b", text, re.I)
                    text = (text[:m2.start()] + DEFENSIVE_STYLE + text[m2.start():]
                            if m2 else DEFENSIVE_STYLE + text)
                stats["css"] += 1

            def _img(m):
                tag = m.group(0)
                new = re.sub(
                    r'\s(?:width|height)\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)',
                    "", tag, flags=re.I)
                if new != tag:
                    stats["img_attrs"] += 1
                return new

            text = IMG_TAG_RE.sub(_img, text)
            if text != orig:
                data[n] = text.encode("utf-8")
                changed = True
        elif low.endswith(".css"):
            text = dec(data[n])
            t2 = FONT_FACE_RE.sub("", text)
            for rx in PROBLEM_CSS_RE:
                t2, cnt = rx.subn("", t2)
                stats["css_clean"] += cnt
            if t2 != text:
                data[n] = t2.encode("utf-8")
                changed = True
        elif n == opf_name:
            text = dec(data[n])

            def _item(m):
                tag = m.group(0)
                hf = re.search(r'href=["\']([^"\']+)["\']', tag)
                mt = re.search(r'media-type=["\']([^"\']+)["\']', tag)
                if hf and (resolve(hf.group(1)) in font_paths
                           or (mt and FONT_MEDIA_RE.match(mt.group(1)))):
                    return ""
                return tag

            t2 = re.sub(r"<item\b[^>]*/>", _item, text)
            if t2 != text:
                data[n] = t2.encode("utf-8")
                changed = True
        elif low == "meta-inf/encryption.xml":
            text = dec(data[n])

            def _enc(m):
                block = m.group(0)
                uri = re.search(r'CipherReference[^>]*URI=["\']([^"\']+)["\']',
                                block)
                if uri and parse.unquote(uri.group(1)).lstrip("/") in font_paths:
                    return ""
                return block

            t2 = re.sub(r"<(?:\w+:)?EncryptedData\b[\s\S]*?</(?:\w+:)?EncryptedData>",
                        _enc, text)
            if t2 != text:
                if not re.search(r"<(?:\w+:)?Encrypted(?:Data|Key)\b", t2):
                    t2 = re.sub(r"(<(?:\w+:)?encryption\b[^>]*>)[\s\S]*"
                                r"(</(?:\w+:)?encryption>)", r"\1\2", t2)
                data[n] = t2.encode("utf-8")
                changed = True
        elif pil is not None and low.endswith((".jpg", ".jpeg", ".png")):
            try:
                img = pil.open(io.BytesIO(data[n]))
                if min(img.size) < 20:  # мелкий декор не трогаем
                    continue
                # прозрачность -> белый фон (на e-ink прозрачности нет)
                if img.mode in ("RGBA", "LA", "PA") or (
                        img.mode == "P" and "transparency" in img.info):
                    img = img.convert("RGBA")
                    bg = pil.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[-1])
                    img = bg
                img = img.convert("L")  # оттенки серого: все 256 уровней сохраняются
                img.thumbnail(SCREEN_BOX,
                              getattr(pil, "Resampling", pil).LANCZOS)
                buf = io.BytesIO()
                if low.endswith(".png"):
                    img.save(buf, "PNG", optimize=True)
                else:
                    # e-ink нужен baseline JPEG, не progressive
                    img.save(buf, "JPEG", quality=85, optimize=True,
                             progressive=False)
                if buf.tell() < len(data[n]):
                    data[n] = buf.getvalue()
                    stats["images"] += 1
                    changed = True
            except Exception:
                pass

    # Мелкий ремонт структуры: mimetype и container.xml.
    mt_key = next((k for k in data if k.lower() == "mimetype"), None)
    if mt_key is None:
        data = {"mimetype": b"application/epub+zip", **data}
        stats["repairs"] += 1
    else:
        if data[mt_key].strip() != b"application/epub+zip":
            data[mt_key] = b"application/epub+zip"
            stats["repairs"] += 1
        if mt_key != "mimetype":
            data["mimetype"] = data.pop(mt_key)
            mt_key = "mimetype"
        if list(data.keys())[0] != mt_key:
            data = {mt_key: data.pop(mt_key), **data}
            stats["repairs"] += 1
    if opf_name and "meta-inf/container.xml" not in {k.lower() for k in data}:
        safe_opf = opf_name.replace("&", "&amp;").replace('"', "&quot;")
        container = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<container version="1.0" '
                     'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                     f'<rootfiles><rootfile full-path="{safe_opf}" '
                     'media-type="application/oebps-package+xml"/>'
                     '</rootfiles></container>')
        data["META-INF/container.xml"] = container.encode("utf-8")
        stats["repairs"] += 1
    if stats["repairs"]:
        changed = True

    if changed:
        tmp = path.with_name(path.name + ".opt.tmp")
        with zipfile.ZipFile(tmp, "w") as z:
            if "mimetype" in data:  # по спецификации EPUB — первым, без сжатия
                z.writestr(zipfile.ZipInfo("mimetype"), data["mimetype"],
                           compress_type=zipfile.ZIP_STORED)
            for n, b in data.items():
                if n != "mimetype":
                    z.writestr(n, b, compress_type=zipfile.ZIP_DEFLATED)
        tmp.replace(path)
        stats["after"] = path.stat().st_size
    return stats


# ------------------------------------------------------- XTC/XTCH конвертация

def ensure_xtc_patched(tools: dict, log=print) -> bool:
    """Патч вендоренного конвертера (баг upstream): CREngine считает страницы
    при загрузке книги с дефолтным шрифтом, а setFontSize после загрузки
    пересчёта не запускает — итог: размер шрифта игнорируется, книга ломается.
    Правильный порядок — настройки ДО загрузки."""
    p = VENDOR_CLI / "converter.js"
    try:
        src = p.read_text("utf-8")
    except OSError:
        if tools.get("xtc"):
            log(t("log.xtc_converter_missing"))
        return False
    if "BOOKSENDER_PATCH" in src:
        return True
    old = """    // Load EPUB
    const { pageCount, info, toc } = await loadEpub(epubPath);

    if (pageCount === 0) {
        throw new Error('EPUB has no pages');
    }

    // Apply settings after loading (affects pagination)
    applySettings(settings);

    // Re-get page count after settings (pagination may change)
    const totalPages = renderer.getPageCount();"""
    new = """    // BOOKSENDER_PATCH: настройки ДО загрузки — CREngine считает страницы
    // при загрузке, setFontSize после загрузки страницы не пересчитывает.
    applySettings(settings);

    // Load EPUB
    const { pageCount, info, toc } = await loadEpub(epubPath);

    if (pageCount === 0) {
        throw new Error('EPUB has no pages');
    }

    const totalPages = renderer.getPageCount();"""
    if old not in src:
        log(t("log.patch_failed"))
        return False
    p.write_text(src.replace(old, new), "utf-8")
    log(t("log.patch_applied"))
    return True


def xtc_settings_file(tmp_dir: Path, font_pt=None) -> Path:
    """Настройки для Node-конвертера XTC: шрифт с кириллицей, переносы ru.
    Размер задаётся в pt (как в прошивке), переводится в px экрана.
    Пользовательские правки — в xtc_settings.json рядом со скриптом."""
    base = {}
    try:
        base = json.loads(XTC_SETTINGS.read_text("utf-8"))
    except Exception:
        pass
    font = dict(base.get("font") or {})
    if font_pt:
        font["size"] = round(int(font_pt) * SCREEN_PPI / 72)
    if not font.get("path") or not os.path.isfile(font["path"]):
        font["path"] = next((str(p) for p in FONT_CANDIDATES if p.is_file()), "")
    font.setdefault("size", round(14 * SCREEN_PPI / 72))  # 14pt ≈ 42px
    font.setdefault("weight", 400)
    base["font"] = font
    base.setdefault("device", "xteink-x4")
    base.setdefault("width", 480)
    base.setdefault("height", 800)
    base.setdefault("margins", {"left": 16, "top": 16, "right": 16, "bottom": 16})
    base.setdefault("lineHeight", 120)
    base.setdefault("textAlign", "justify")
    hyph = dict(base.get("hyphenation") or {})
    hyph.setdefault("enabled", True)
    hyph.setdefault("language", "ru")
    base["hyphenation"] = hyph
    out = tmp_dir / "xtc_settings.json"
    out.write_text(json.dumps(base, ensure_ascii=False, indent=2), "utf-8")
    return out


def pdf_meta(path: Path) -> dict:
    """Название/автор из метаданных PDF."""
    try:
        import pymupdf
        with pymupdf.open(path) as doc:
            md = doc.metadata or {}
            return {"title": (md.get("title") or "").strip(),
                    "author": (md.get("author") or "").strip()}
    except Exception:
        return {}


def _page_to_frame(img, fmt: str) -> bytes:
    """Страница (PIL, оттенки серого) -> кадр RGBA под экран 480×800:
    обрезка белых полей, вписывание, дизеринг (XTC) или 4 уровня (XTCH)."""
    from PIL import Image
    W, H = 480, 800
    bbox = img.point(lambda g: 0 if g > 240 else 255).getbbox()
    if bbox:
        pad = 8
        img = img.crop((max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                        min(img.width, bbox[2] + pad),
                        min(img.height, bbox[3] + pad)))
    fit = min(W / img.width, H / img.height, 2.0)
    if abs(fit - 1.0) > 0.01:
        resample = getattr(Image, "Resampling", Image).LANCZOS
        img = img.resize((max(1, round(img.width * fit)),
                          max(1, round(img.height * fit))), resample)
    if fmt == "xtch":
        img = img.point(lambda g: 255 if g > 212 else
                        170 if g > 127 else 85 if g > 42 else 0)
    else:
        img = img.convert("1", dither=Image.FLOYDSTEINBERG).convert("L")
    canvas = Image.new("L", (W, H), 255)
    canvas.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
    return canvas.convert("RGBA").tobytes()


def _pack_frames(frames: Path, n: int, fmt: str, title: str, author: str,
                 out: Path, tools: dict, stop=None):
    """Кадры RGBA -> контейнер XTC/XTCH вендоренным Node-энкодером."""
    bridge = Path(__file__).resolve().with_name("pdf_frames_to_xtc.js")
    run_cmd([tools["node"], str(bridge), str(frames), str(n), "480", "800",
             fmt, title, author, str(out)], timeout=1800, stop=stop)
    if not out.is_file() or out.stat().st_size == 0:
        raise ToolError(t("err.encoder_no_xtc"))


def pdf_to_xtc(pdf: Path, out: Path, fmt: str, tools: dict, log, stop=None) -> Path:
    """PDF -> XTC/XTCH: постраничный рендер (PyMuPDF) -> оттенки серого/дизеринг
    (Pillow) -> упаковка вендоренным Node-энкодером. Без Calibre."""
    if not tools.get("pymupdf"):
        raise ToolError(t("err.pymupdf_needed"))
    if not tools.get("node") or not tools.get("xtc"):
        raise ToolError(t("err.xtc_not_installed"))
    import pymupdf
    from PIL import Image
    frames = out.with_name(out.name + ".frames")
    try:
        try:
            doc = pymupdf.open(pdf)
        except Exception as e:
            raise ToolError(t("err.pdf_open", error=e)) from e
        with doc:
            if doc.page_count == 0:
                raise ToolError(t("err.pdf_no_pages"))
            n = 0
            with open(frames, "wb") as f:
                for page in doc:
                    if stop is not None and stop.is_set():
                        raise Cancelled()
                    scale = min(480 / page.rect.width, 800 / page.rect.height)
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                          colorspace=pymupdf.csGRAY, alpha=False)
                    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                    f.write(_page_to_frame(img, fmt))
                    n += 1
        meta = pdf_meta(pdf)
        _pack_frames(frames, n, fmt, meta.get("title") or pdf.stem,
                     meta.get("author") or "", out, tools, stop)
    finally:
        frames.unlink(missing_ok=True)
    return out


def djvu_to_xtc(djvu: Path, out: Path, fmt: str, tools: dict, log, stop=None) -> Path:
    """DjVu -> XTC/XTCH: страницы рендерим ddjvu (DjVuLibre) и упаковываем
    тем же конвейером, что и PDF-сканы. Текстовый слой DjVu игнорируется —
    это всегда постраничные картинки."""
    if not tools.get("ddjvu"):
        raise ToolError(t("err.ddjvu_needed"))
    if not tools.get("node") or not tools.get("xtc"):
        raise ToolError(t("err.xtc_not_installed"))
    import io as _io
    from PIL import Image
    try:
        outp, _ = run_cmd([tools["djvused"], str(djvu), "-e", "n"],
                           timeout=120, stop=stop)
        pages = int(outp.decode().strip().split()[-1])
    except Cancelled:
        raise
    except Exception as e:
        raise ToolError(t("err.djvu_open", error=e)) from e
    if pages <= 0:
        raise ToolError(t("err.djvu_no_pages"))
    frames = out.with_name(out.name + ".frames")
    try:
        n = 0
        with open(frames, "wb") as f:
            for i in range(1, pages + 1):
                if stop is not None and stop.is_set():
                    raise Cancelled()
                ppm, _ = run_cmd([tools["ddjvu"], "-format=ppm", f"-page={i}",
                                  "-size=960x1600", str(djvu)],
                                 timeout=300, stop=stop)
                img = Image.open(_io.BytesIO(ppm)).convert("L")
                f.write(_page_to_frame(img, fmt))
                n += 1
        _pack_frames(frames, n, fmt, djvu.stem, "", out, tools, stop)
    finally:
        frames.unlink(missing_ok=True)
    return out


def pdf_is_complex(pdf: Path, tools: dict) -> bool:
    """Эвристика «сложная ли вёрстка»: колонки текста, много векторной
    графики (таблицы/схемы), альбомные страницы (слайды). Такие PDF лучше
    рендерить страницами (XTC), а не перенабирать."""
    if not tools.get("pymupdf"):
        return False
    try:
        import pymupdf
        with pymupdf.open(pdf) as doc:
            n = len(doc)
            if n == 0:
                return False
            column_pages = 0
            landscape = 0
            drawings = 0
            for page in doc:
                w = page.rect.width
                if w > page.rect.height * 1.1:
                    landscape += 1
                drawings += len(page.get_drawings())
                xs = sorted(b["bbox"][0] for b in page.get_text("dict")["blocks"]
                            if b.get("type") == 0)
                clusters = []
                for x in xs:
                    if clusters and x - clusters[-1][-1] < w * 0.12:
                        clusters[-1].append(x)
                    else:
                        clusters.append([x])
                if len([c for c in clusters if len(c) >= 3]) >= 2:
                    column_pages += 1
            return (column_pages / n >= 0.3 or landscape / n >= 0.5
                    or drawings / n > 40)
    except Exception:
        return False


def pdf_to_epub(pdf: Path, out_epub: Path, tools: dict, log, stop=None) -> Path:
    """Текстовый PDF -> EPUB с перенабором под экран: PyMuPDF извлекает текст
    и картинки (по блокам, порядок чтения), pandoc собирает EPUB, дальше
    CREngine верстает его под 480×800 с выбранным шрифтом и переносами.
    Для сложной вёрстки (колонки, таблицы, сканы) лучше режим «страницы»."""
    if not tools.get("pymupdf"):
        raise ToolError(t("err.pymupdf_needed"))
    if not tools.get("pandoc"):
        raise ToolError(t("err.pandoc_needed_pdf"))
    import pymupdf
    work = Path(tempfile.mkdtemp(prefix="pdfreflow_"))
    try:
        try:
            doc = pymupdf.open(pdf)
        except Exception as e:
            raise ToolError(t("err.pdf_open", error=e)) from e
        entries = []  # ("text", текст, размер шрифта) | ("img", имя файла, 0)
        with doc:
            for page in doc:
                if stop is not None and stop.is_set():
                    raise Cancelled()
                data = page.get_text("dict")
                for block in data.get("blocks", []):
                    if block.get("type") == 0:  # текстовый блок
                        lines = []
                        block_size = 0.0
                        for line in block.get("lines", []):
                            line_text = "".join(s.get("text", "")
                                                for s in line.get("spans", []))
                            if not line_text.strip():
                                continue
                            lines.append(line_text.strip())
                            block_size = max(block_size, max(
                                (s.get("size", 0.0) for s in line.get("spans", [])
                                 if s.get("text", "").strip()), default=0.0))
                        if not lines:
                            continue
                        text = " ".join(lines)
                        # склейка слов с переносом на конце строки
                        text = re.sub(r"([а-яёa-z])-\s+([а-яёa-z])",
                                      r"\1\2", text, flags=re.I)
                        entries.append(("text", text, block_size))
                    elif block.get("type") == 1:  # картинка
                        try:
                            img_bytes = block.get("image")
                            if img_bytes:
                                pix = pymupdf.Pixmap(img_bytes)
                            else:
                                pix = pymupdf.Pixmap(doc, block["xref"])
                            if pix.colorspace and pix.colorspace.n > 3:
                                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                            name = f"img{len(entries):04d}.png"
                            pix.save(str(work / name))
                            entries.append(("img", name, 0.0))
                        except Exception:
                            pass
        texts = sorted(s for kind, _, s in entries if kind == "text" and s > 0)
        if not texts:
            # картинок может быть много (скан), но без текста это не книга для
            # перенабора — такие PDF идут в XTC рендером страниц
            raise ToolError(t("err.pdf_no_text"))
        body = texts[len(texts) // 2] if texts else 10.0
        md = []
        for kind, value, size in entries:
            if kind == "img":
                md.append(f"\n![]({value})\n")
            elif size >= body * 1.3 and len(value) < 120:
                md.append(f"\n## {value}\n")  # крупный текст = заголовок
            else:
                md.append(f"\n{value}\n")
        md_path = work / "book.md"
        md_path.write_text("\n".join(md), "utf-8")
        meta = pdf_meta(pdf)
        title = meta.get("title") or pdf.stem
        author = meta.get("author") or ""
        cmd = [tools["pandoc"], "book.md", "-f", "markdown", "-t", "epub",
               "--metadata", f"title={title}", "-o", str(out_epub)]
        if author:
            cmd += ["--metadata", f"author={author}"]
        run_cmd(cmd, cwd=str(work), timeout=900, stop=stop)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not out_epub.is_file() or out_epub.stat().st_size == 0:
        raise ToolError("pandoc не создал EPUB из PDF")
    return out_epub


def convert_to_xtc(epub: Path, out: Path, fmt: str, settings: Path,
                   tools: dict, stop=None) -> Path:
    """EPUB -> XTC (1-bit) / XTCH (2-bit) через вендоренный Node-конвертер
    bigbag/epub-to-xtc-converter (рендер CREngine, как в CoolReader)."""
    if not tools.get("node") or not tools.get("xtc"):
        raise ToolError("XTC-конвертер не установлен (запустите setup_xtc.command)")
    cmd = [tools["node"], tools["xtc"], "convert", str(epub),
           "-o", str(out), "-c", str(settings), "-f", fmt]
    run_cmd(cmd, cwd=str(epub.parent), timeout=900, stop=stop)
    if not out.is_file() or out.stat().st_size == 0:
        raise ToolError("конвертер не создал XTC-файл")
    return out


# ---------------------------------------------------------------- устройство

class Device:
    """HTTP-клиент веб-сервера CrossPoint (режим File Transfer)."""

    def __init__(self, host: str):
        host = host.strip()
        if "://" not in host:
            host = "http://" + host
        self.base = host.rstrip("/")

    def _req(self, method, path, data=None, headers=None, timeout=30):
        req = request.Request(
            self.base + path, data=data, method=method, headers=headers or {}
        )
        return request.urlopen(req, timeout=timeout)

    def status(self) -> dict:
        with self._req("GET", "/api/status", timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if not isinstance(data, dict) or "version" not in data:
            raise DeviceError("ответ не похож на CrossPoint /api/status")
        return data

    def list_dir(self, path: str):
        """Список файлов папки или None, если папки нет."""
        url = "/api/files?path=" + parse.quote(path, safe="")
        try:
            with self._req("GET", url, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except error.HTTPError as e:
            if e.code == 404:
                return None
            raise DeviceError(f"/api/files: HTTP {e.code}") from e

    def ensure_dir(self, path: str):
        """Создаёт папку (и родителей при необходимости). Наличие проверяем
        по списку РОДИТЕЛЯ: прошивка 2.0.0 возвращает 200 [] для несуществующих
        папок, поэтому доверять листингу самой папки нельзя."""
        cur = ""
        for part in [p for p in path.split("/") if p]:
            parent = cur or "/"
            cur = cur + "/" + part

            def exists():
                entries = self.list_dir(parent)
                return entries is not None and any(
                    e.get("isDirectory")
                    and e.get("name", "").casefold() == part.casefold()
                    for e in entries
                )

            if exists():
                continue
            body = parse.urlencode({"name": part, "path": parent}).encode()
            try:
                self._req(
                    "POST", "/mkdir", data=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=20,
                ).close()
            except error.HTTPError as e:
                txt = e.read().decode("utf-8", "replace")
                if "exist" not in txt.lower():
                    raise DeviceError(
                        f"mkdir {cur}: HTTP {e.code} {txt[:120]}") from e
            if not exists():
                raise DeviceError(f"не удалось создать папку {cur} на устройстве")

    def list_recursive(self, path: str, stop=None) -> dict:
        """Все файлы под path, включая вложенные папки: {name.lower: полный путь}.
        Скрытые папки пропускаются, глубина ограничена."""
        found = {}
        stack = [(path, 0)]
        while stack:
            d, depth = stack.pop()
            if stop is not None and stop.is_set():
                raise Cancelled()
            for e in self.list_dir(d) or []:
                full = d.rstrip("/") + "/" + e["name"]
                if e.get("isDirectory"):
                    if depth < 8 and not e["name"].startswith("."):
                        stack.append((full, depth + 1))
                else:
                    found.setdefault(e["name"].lower(), full)
        return found

    def delete(self, path: str):
        body = parse.urlencode({"path": path}).encode()
        try:
            self._req(
                "POST", "/delete", data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=20,
            ).close()
        except error.HTTPError as e:
            raise DeviceError(f"delete {path}: HTTP {e.code}") from e

    def upload(self, file: Path, dest: str, overwrite: bool, timeout=300):
        boundary = "----BookSender" + uuid.uuid4().hex
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{file.name}"\r\n'
            f"Content-Type: application/epub+zip\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode()
        data = head + file.read_bytes() + tail
        url = "/upload?path=" + parse.quote(dest, safe="")
        if overwrite:
            url += "&overwrite=1"
        try:
            with self._req(
                "POST", url, data=data, timeout=timeout,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            ) as resp:
                text = resp.read().decode("utf-8", "replace")
        except error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code == 400 and "already exists" in body.lower():
                raise FileExistsError_(body) from e
            raise DeviceError(f"upload: HTTP {e.code} {body}") from e
        if "successfully" not in text.lower():
            raise DeviceError(f"upload: {text[:200]}")


def probe_device(host: str):
    try:
        return Device(host).status()
    except Exception:
        return None


# -------------------------------------------------------------------- поиск

def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # трафик не отправляется
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def udp_discover(timeout=1.6) -> dict:
    """Официальный поиск CrossPoint: UDP broadcast 'hello' на порт 8134."""
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.3)
    targets = {"255.255.255.255"}
    ip = local_ip()
    if ip.count(".") == 3:
        targets.add(".".join(ip.split(".")[:3]) + ".255")
    for t in targets:
        try:
            s.sendto(b"hello", (t, DISCOVERY_PORT))
        except OSError:
            pass
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            data, addr = s.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        text = data.decode("utf-8", "replace")
        if text.startswith("crosspoint"):
            found[addr[0]] = text.strip()
    s.close()
    return found


def resolve_mdns(name="crosspoint.local", timeout=3.0):
    result = []

    def _resolve():
        try:
            result.append(socket.gethostbyname(name))
        except OSError:
            pass

    th = threading.Thread(target=_resolve, daemon=True)
    th.start()
    th.join(timeout)
    return result[0] if result else None


def scan_subnet(port=80, workers=64, connect_timeout=0.3) -> list:
    """Fallback: какие хосты подсети /24 слушают порт 80."""
    ip = local_ip()
    if ip.count(".") != 3:
        return []
    prefix = ".".join(ip.split(".")[:3])
    hosts = [f"{prefix}.{i}" for i in range(1, 255)]

    def check(h):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(connect_timeout)
        try:
            return h if s.connect_ex((h, port)) == 0 else None
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return [h for h in ex.map(check, hosts) if h]


def find_devices(log=print, stop=None, verbose=True) -> list:
    """Возвращает список (ip, status_dict). Сначала UDP/mDNS, потом скан."""
    candidates = []

    def add(ip):
        if ip and ip not in candidates:
            candidates.append(ip)

    for ip in udp_discover():
        add(ip)
    add(resolve_mdns())
    add("192.168.4.1")  # режим хотспота CrossPoint-Reader

    results = []
    for ip in candidates:
        if stop is not None and stop.is_set():
            raise Cancelled()
        st = probe_device(ip)
        if st:
            results.append((ip, st))
    if results:
        return results

    if verbose:
        log("UDP/mDNS не нашли читалку — сканирую подсеть (порт 80)…")
    for ip in scan_subnet():
        if stop is not None and stop.is_set():
            raise Cancelled()
        st = probe_device(ip)
        if st:
            results.append((ip, st))
    return results


# ------------------------------------------------------------------ пайплайн

def file_sha256(path: Path, stop=None) -> str | None:
    """SHA-256 содержимого файла (потоково, 1 МБ блоками); None при ошибке чтения."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                if stop is not None and stop.is_set():
                    return None
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def find_duplicates(files, stop=None) -> dict:
    """Дубликаты по содержимому: {путь_копии: путь_оригинала}.
    Оригинал — первый файл в порядке списка. Сначала группируем по размеру:
    файлы с уникальным размером дубликатами быть не могут и не хэшируются.
    Разные форматы одной книги (fb2 и epub) содержимым не совпадают —
    это не дубликаты."""
    by_size: dict = {}
    for p in files:
        if stop is not None and stop.is_set():
            break
        try:
            by_size.setdefault(p.stat().st_size, []).append(p)
        except OSError:
            continue
    seen: dict = {}
    dups: dict = {}
    for _size, group in sorted(by_size.items()):
        if len(group) < 2:
            continue
        for p in group:
            if stop is not None and stop.is_set():
                break
            digest = file_sha256(p, stop)
            if digest is None:
                continue
            if digest in seen:
                dups[str(p)] = seen[digest]
            else:
                seen[digest] = p
    return dups


def scan_folder(folder: Path, recursive=True) -> list:
    files = []
    if recursive:
        for root, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in sorted(names):
                if n.startswith(".") or n.startswith("._"):
                    continue
                files.append(Path(root) / n)
    else:
        files = sorted(
            p for p in folder.iterdir()
            if p.is_file() and not p.name.startswith(".")
        )
    return files


def snapshot_diff(snapshot: dict, current: dict):
    """Сравнение снимков папки: (added, changed, removed) — списки ключей."""
    added = [k for k in current if k not in snapshot]
    changed = [k for k in snapshot if k in current and snapshot[k] != current[k]]
    removed = [k for k in snapshot if k not in current]
    return added, changed, removed


def describe_file(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in ARCHIVE_EXTS:
        return "архив"
    if ext in NATIVE_EXTS:
        return ext.lstrip(".")
    if ext in PANDOC_FORMATS:
        return ext.lstrip(".")
    if ext in CALIBRE_EXTS:
        return ext.lstrip(".")
    return ext.lstrip(".") or "?"


def convert_all(files, stage: Path, tools, log, stop=None, on_file=None,
                on_progress=None, formats=None, duplicates=None):
    """Фаза 1: конвертация всех файлов в stage, НЕ дожидаясь устройства.
    Возвращает (tasks, skipped, errors, page_tasks); tasks = список
    (idx_файла, путь_epub); page_tasks = [(idx, путь)] — PDF и DjVu,
    которые обрабатываются постранично (в EPUB с перенабором или в XTC).
    duplicates — карта find_duplicates: копии пропускаются до конвертации."""
    tasks = []
    page_tasks = []
    skipped = errors = 0
    total = len(files)
    for idx, src in enumerate(files):
        if stop is not None and stop.is_set():
            raise Cancelled()
        first = (duplicates or {}).get(str(src))
        if first:
            skipped += 1
            log(t("log.duplicate", name=src.name, first=Path(first).name))
            if on_file:
                on_file(idx, t("status.duplicate"), "skip")
            if on_progress:
                on_progress(idx + 1, total)
            continue
        if src.suffix.lower() in (".pdf", ".djvu"):
            # PDF: есть текст -> EPUB (перенабор), нет (скан) -> XTC страницами.
            # DjVu: всегда постранично (это картинки страниц).
            page_tasks.append((idx, src))
            if on_file:
                on_file(idx, t("status.route_pdf" if src.suffix.lower() == ".pdf"
                               else "status.route_djvu"), "work")
            if on_progress:
                on_progress(idx + 1, total)
            continue
        if on_file:
            on_file(idx, t("status.converting"), "work")
        log(f"[{idx + 1}/{total}] {src.name}")
        src_tmp = stage / f"s{idx}"
        used_names: set = set()
        try:
            epubs = process_source(src, src_tmp, tools, log, used_names, stop)
            if not epubs:
                skipped += 1
                if on_file:
                    on_file(idx, t("status.nothing"), "skip")
            else:
                tasks.extend((idx, p) for p in epubs)
                if on_file:
                    on_file(idx, t("status.ready_n", n=len(epubs)), "ready")
        except Cancelled:
            raise
        except Skipped as e:
            skipped += 1
            log(t("log.skipped", error=e))
            if on_file:
                on_file(idx, t("status.skipped", error=e)[:80], "skip")
        except (ToolError, OSError) as e:
            errors += 1
            log(f"  ✗ {e}")
            if on_file:
                on_file(idx, str(e)[:80], "err")
        if on_progress:
            on_progress(idx + 1, total)
    return tasks, skipped, errors, page_tasks


def wait_for_device(host, log, stop=None, interval=5, timeout=0, on_attempt=None):
    """Фаза 2: ждём появления читалки, повторяя поиск каждые interval секунд.
    timeout=0 — ждать бесконечно (пока не найдём или не нажмут «Стоп»).
    Если заданный host молчит, периодически делаем полный поиск — IP мог
    смениться по DHCP. Возвращает (ip, status_dict)."""
    t0 = time.monotonic()
    attempt = 0
    while True:
        if stop is not None and stop.is_set():
            raise Cancelled()
        attempt += 1
        if on_attempt:
            on_attempt(attempt)
        if host:
            st = probe_device(host)
            if st:
                return host, st
            if attempt % 3 == 0:
                found = find_devices(log=log, stop=stop, verbose=False)
                if found:
                    ip, st = found[0]
                    if ip != host:
                        log(t("log.device_other_ip", ip=ip))
                    return ip, st
            log(t("log.device_no_answer", host=host, n=attempt, interval=interval))
        else:
            found = find_devices(log=log, stop=stop, verbose=(attempt == 1))
            if found:
                return found[0]
            log(t("log.reader_not_found", n=attempt, interval=interval))
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                raise Cancelled()
            if timeout and time.monotonic() - t0 >= timeout:
                raise DeviceError(t("err.device_timeout", timeout=timeout))
            time.sleep(0.2)
        if timeout and time.monotonic() - t0 >= timeout:
            raise DeviceError(f"устройство не появилось за {timeout} с")


# Семейства шрифтов для чтения по языку интерфейса: полные стрелки/галочки,
# кириллица/латиница и настоящие жирный с курсивом.
READER_FONTS = {
    "ru": ("GentiumBookPlus", (
        "GentiumBookPlus-Regular.ttf", "GentiumBookPlus-Bold.ttf",
        "GentiumBookPlus-Italic.ttf", "GentiumBookPlus-BoldItalic.ttf")),
    "en": ("CharisSIL", (
        "CharisSIL-Regular.ttf", "CharisSIL-Bold.ttf",
        "CharisSIL-Italic.ttf", "CharisSIL-BoldItalic.ttf")),
    # Китайский: .cpfont (2-битные битмапы CrossPoint), по файлу на размер.
    # Полный CJK-набор (21606 иероглифов), шрифт Noto Serif SC / 思源宋体 (OFL).
    "zh": ("NotoSerifSC", (
        "NotoSerifSC_12.cpfont", "NotoSerifSC_14.cpfont",
        "NotoSerifSC_16.cpfont", "NotoSerifSC_18.cpfont")),
}


def ensure_reader_font(dev, log, lang=None) -> bool:
    """Загружает семейство шрифта для языка интерфейса в /fonts устройства,
    если его там нет (ru -> Gentium Book Plus, en -> Charis SIL,
    zh -> Noto Serif SC в формате .cpfont). Ничего с устройства не удаляет.
    Ошибка не блокирует отправку книг."""
    lang = lang or current_language()
    spec = READER_FONTS.get(lang)
    if spec is None:
        return False
    family, files = spec
    local_dir = Path(__file__).resolve().with_name("fonts") / family
    if not (local_dir / files[0]).is_file():
        return False
    remote_dir = "/fonts/" + family
    try:
        entries = dev.list_dir(remote_dir) or []
        names = {e.get("name", "").casefold() for e in entries}
        if files[0].casefold() in names:
            return True
        dev.ensure_dir(remote_dir)
        for fn in files:
            f = local_dir / fn
            if f.is_file():
                dev.upload(f, remote_dir, True)
        log(t("log.font_uploaded", family=family))
        return True
    except Exception as e:
        log(t("log.font_failed", error=e))
        return False


def run_batch(files, host, dest, replace_existing, tools, log, stop=None,
              on_file=None, on_progress=None, wait_timeout=0,
              on_device=None, on_waiting=None, by_author=True, optimize=True,
              formats=None, xtc_font_pt=None, ascii_symbols=False):
    """Конвертация сразу -> оптимизация -> XTC по выбору -> ожидание -> отправка.
    on_file(index, status_text, tag) — для GUI; wait_timeout=0 — ждать вечно.
    by_author: раскладывать по папкам авторов (как плагин organize-by-author).
    optimize: защитный CSS, удаление шрифтов, картинки (если есть Pillow).
    formats: список форматов по файлам ("epub"/"xtc"/"xtch"), по умолчанию epub."""
    sent = skipped = errors = 0
    stage = Path(tempfile.mkdtemp(prefix="booksender_stage_"))
    try:
        # Дубликаты по содержимому пропускаем всегда, до конвертации
        duplicates = find_duplicates(files, stop)
        if duplicates:
            log(t("log.duplicates_found", n=len(duplicates)))
        # Фаза 1: конвертируем всё сразу, устройство пока не нужно
        tasks, skipped, errors, page_tasks = convert_all(
            files, stage, tools, log, stop, on_file, on_progress, formats,
            duplicates)
        if not tasks and not page_tasks:
            log(t("log.summary_zero", skipped=skipped, errors=errors))
            return sent, skipped, errors

        # Фаза 1.5: оптимизация EPUB и конвертация в XTC/XTCH по выбору
        if optimize:
            log(t("log.optimizing", images=(
                t("log.optimize_images_yes") if tools.get("pillow")
                else t("log.optimize_images_no"))))
        settings = None
        xtc_requested = any(f in ("xtc", "xtch") for f in (formats or []))
        if xtc_requested:
            if not tools.get("xtc"):
                log(t("log.xtc_not_installed"))
            else:
                ensure_xtc_patched(tools, log)
                settings = xtc_settings_file(stage, font_pt=xtc_font_pt)
                px = json.loads(settings.read_text())["font"]["size"]
                pt = xtc_font_pt or 14
                log(t("log.xtc_font", pt=pt, px=px))
        prepared = []
        for idx, epub in tasks:
            if stop is not None and stop.is_set():
                raise Cancelled()
            author = ""
            if epub.suffix.lower() == ".epub":
                if optimize:
                    try:
                        s = optimize_epub(epub, optimize_images=bool(tools.get("pillow")))
                        if (s["fonts"] or s["css"] or s["css_clean"] or s["comments"]
                                or s["img_attrs"] or s["images"]):
                            log(t("log.optimized", name=epub.name,
                                  before=s['before'] // 1024, after=s['after'] // 1024,
                                  fonts=s['fonts'], css=s['css'],
                                  css_clean=s['css_clean'], comments=s['comments'],
                                  images=s['images'], repairs=s['repairs']))
                    except Exception as e:
                        log(t("log.optimize_skipped", name=epub.name, error=e))
                if ascii_symbols:
                    try:
                        n_sym = ascii_symbols_epub(epub)
                        if n_sym:
                            log(t("log.ascii_symbols", n=n_sym, name=epub.name))
                    except Exception as e:
                        log(f"  ⚠ {epub.name}: {e}")
                author = author_folder_name(epub_author(epub))
                v = validate_epub(epub)
                if v["problems"]:
                    joined = "; ".join(v["problems"])
                    if v["fatal"]:
                        errors += 1
                        log(t("log.epub_fatal", name=epub.name, problems=joined))
                        if on_file:
                            on_file(idx, t("status.epub_broken"), "err")
                        continue
                    log(t("log.epub_problems", name=epub.name, problems=joined))
                    if on_file:
                        on_file(idx, t("status.epub_warning"), "skip")
            fmt = formats[idx] if formats and idx < len(formats) else "epub"
            if fmt in ("xtc", "xtch") and settings is not None:
                if on_file:
                    on_file(idx, t("status.converting_to", fmt=fmt.upper()), "work")
                out = epub.with_name(epub.stem + "." + fmt)
                try:
                    convert_to_xtc(epub, out, fmt, settings, tools, stop)
                    log(t("log.xtc_done", name=epub.name, out=out.name,
                          mb=out.stat().st_size // 1024 // 1024,
                          kb=out.stat().st_size // 1024))
                    prepared.append((idx, out, author))
                    continue
                except Cancelled:
                    raise
                except ToolError as e:
                    log(t("log.xtc_fallback", error=e))
            prepared.append((idx, epub, author))
        tasks = prepared

        # Постраничные файлы: PDF и DjVu.
        # PDF: если есть текст — EPUB с картинками (перенабор под экран);
        # если текста нет (скан) — рендер страниц в XTC.
        for idx, pdf in page_tasks:
            if stop is not None and stop.is_set():
                raise Cancelled()
            if on_file:
                on_file(idx, t("status.pdf_analyze"), "work")
            chosen = formats[idx] if formats and idx < len(formats) else "epub"
            is_djvu = pdf.suffix.lower() == ".djvu"
            if is_djvu:
                title, author_raw = pdf.stem, ""
            else:
                meta = pdf_meta(pdf)
                title = meta.get("title") or pdf.stem
                author_raw = meta.get("author") or ""
            author = author_folder_name(author_raw)
            stem = sanitize_filename(
                ((author_raw + " - ") if author_raw else "") + title)
            work = stage / f"p{idx}"
            work.mkdir(parents=True, exist_ok=True)
            if is_djvu:
                # DjVu — всегда постраничные картинки -> XTC
                fmt_out = chosen if chosen in ("xtc", "xtch") else "xtc"
                out = work / (stem + "." + fmt_out)
                try:
                    djvu_to_xtc(pdf, out, fmt_out, tools, log, stop)
                    log(t("log.pdf_pages", name=pdf.name, out=out.name,
                          kb=out.stat().st_size // 1024))
                    prepared.append((idx, out, author))
                    if on_file:
                        on_file(idx, t("status.ready_fmt", fmt=fmt_out.upper()), "ready")
                except Cancelled:
                    raise
                except ToolError as e:
                    errors += 1
                    log(f"  ✗ {pdf.name}: {e}")
                    if on_file:
                        on_file(idx, str(e)[:80], "err")
                continue
            if chosen in ("xtc", "xtch"):
                # Пользователь явно выбрал XTC/XTCH: страницы как есть —
                # путь для PDF со сложной вёрсткой (колонки, таблицы)
                out = work / (stem + "." + chosen)
                try:
                    pdf_to_xtc(pdf, out, chosen, tools, log, stop)
                    log(t("log.pdf_pages", name=pdf.name, out=out.name,
                          kb=out.stat().st_size // 1024))
                    prepared.append((idx, out, author))
                    if on_file:
                        on_file(idx, t("status.ready_fmt", fmt=chosen.upper()), "ready")
                except Cancelled:
                    raise
                except ToolError as e:
                    errors += 1
                    log(f"  ✗ {pdf.name}: {e}")
                    if on_file:
                        on_file(idx, str(e)[:80], "err")
                continue
            epub_out = work / (stem + ".epub")
            try:
                if pdf_is_complex(pdf, tools):
                    raise ToolError(t("err.complex_layout"))
                pdf_to_epub(pdf, epub_out, tools, log, stop)
            except Cancelled:
                raise
            except ToolError as e:
                log(t("log.pdf_render", name=pdf.name, error=e))
                out = work / (stem + ".xtc")
                try:
                    pdf_to_xtc(pdf, out, "xtc", tools, log, stop)
                    log(t("log.pdf_pages", name=pdf.name, out=out.name,
                          kb=out.stat().st_size // 1024))
                    prepared.append((idx, out, author))
                    if on_file:
                        on_file(idx, t("status.ready_xtc"), "ready")
                except Cancelled:
                    raise
                except ToolError as e2:
                    errors += 1
                    log(f"  ✗ {pdf.name}: {e2}")
                    if on_file:
                        on_file(idx, str(e2)[:80], "err")
                continue
            if optimize:
                try:
                    optimize_epub(epub_out,
                                  optimize_images=bool(tools.get("pillow")))
                except Exception as e:
                    log(t("log.optimize_skipped", name=epub_out.name, error=e))
            if ascii_symbols:
                try:
                    ascii_symbols_epub(epub_out)
                except Exception:
                    pass
            log(t("log.pdf_reflow", name=pdf.name, out=epub_out.name))
            prepared.append((idx, epub_out, author))
            if on_file:
                on_file(idx, t("status.ready_epub"), "ready")
        tasks = prepared

        log(t("log.prep_done", n=len(tasks)))

        # Фаза 2: ждём читалку (периодический поиск, пока не появится)
        host, st = wait_for_device(host, log, stop, timeout=wait_timeout,
                                   on_attempt=on_waiting)
        log(t("log.device_info", device=st.get('device', '?'),
              family=st.get('firmwareFamily', 'crosspoint'),
              version=st.get('version', '?'), mode=st.get('mode', '?'), host=host))
        if on_device:
            on_device(host, st)

        # Фаза 3: отправка
        dev = Device(host)
        ensure_reader_font(dev, log)
        dev.ensure_dir(dest)
        existing = dev.list_recursive(dest, stop)  # name.lower -> полный путь
        log(t("log.folder_count", dest=dest, n=len(existing)))

        by_idx = {}
        for idx, path, author in tasks:
            by_idx.setdefault(idx, []).append((path, author))
        total_up = len(tasks)
        done_up = 0
        made_dirs = {dest}
        for idx, items in by_idx.items():
            file_sent = file_skipped = file_errors = 0
            for epub, author in items:
                if stop is not None and stop.is_set():
                    raise Cancelled()
                target_dir = dest
                if by_author and author:
                    target_dir = dest + "/" + author
                target_path = target_dir + "/" + epub.name
                old_path = existing.get(epub.name.lower())
                if old_path and not replace_existing:
                    log(t("log.already_on_device", path=old_path))
                    file_skipped += 1
                    done_up += 1
                    if on_progress:
                        on_progress(done_up, total_up)
                    continue
                if target_dir not in made_dirs:
                    dev.ensure_dir(target_dir)
                    made_dirs.add(target_dir)
                if old_path and old_path != target_path:
                    try:
                        dev.delete(old_path)
                        log(t("log.replace_old_removed", path=old_path))
                    except DeviceError as e:
                        log(t("log.replace_old_failed", error=e))
                if on_file:
                    on_file(idx, t("status.sending", name=epub.name), "work")
                outcome, last_err = None, None
                for attempt in (1, 2):
                    try:
                        dev.upload(epub, target_dir, overwrite=replace_existing)
                        outcome = "sent"
                        break
                    except FileExistsError_:
                        outcome = "skipped"
                        log(t("log.already_on_device", path=epub.name))
                        break
                    except (DeviceError, error.URLError, OSError) as e:
                        last_err = e
                        if attempt == 1:
                            log(t("log.retry_send", error=e))
                            time.sleep(2)
                if outcome == "sent":
                    existing[epub.name.lower()] = target_path
                    file_sent += 1
                    size_kb = epub.stat().st_size / 1024
                    log(t("log.sent", name=epub.name, dir=target_dir, kb=f"{size_kb:.0f}"))
                elif outcome == "skipped":
                    file_skipped += 1
                else:
                    file_errors += 1
                    log(f"  ✗ {epub.name}: {last_err}")
                done_up += 1
                if on_progress:
                    on_progress(done_up, total_up)
            sent += file_sent
            skipped += file_skipped
            errors += file_errors
            if on_file:
                if file_errors:
                    on_file(idx, t("status.errors_n", n=file_errors), "err")
                elif file_sent:
                    on_file(idx, t("status.sent_n", n=file_sent), "ok")
                else:
                    on_file(idx, t("status.already"), "skip")
        log(t("log.summary", sent=sent, skipped=skipped, errors=errors))
        return sent, skipped, errors
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# ---------------------------------------------------------------------- GUI

def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    tools = detect_tools()

    class App:
        def __init__(self, root):
            self.root = root
            self.q = queue.Queue()
            self.stop_event = threading.Event()
            self.worker = None
            self.files = []
            self.formats = []
            self.iids = []
            self.batch_iids = []
            self.busy = False
            self.snapshot = {}
            self.meta = {}
            self._cover_req = 0
            self._cover_cache = {}  # str(path) -> (размер, dict из book_cover)
            self._cover_photo = None
            self.settings = self.load_settings()
            set_language(self.settings.get("language", "auto"))
            root.title(t("app.title", app=APP_NAME))
            root.geometry("980x680")
            root.minsize(820, 540)
            self.build_ui()
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            root.after(100, self.poll_queue)
            if not tools["pandoc"]:
                self.log(t("log.pandoc_missing"))
            else:
                self.log(f"pandoc: {tools['pandoc']}")
            if tools["calibre"]:
                self.log(t("log.calibre_found", path=tools["calibre"]))
            else:
                self.log(t("log.calibre_missing"))
            if tools["7z"]:
                self.log(t("log.7z_found", path=tools["7z"]))
            if tools["pillow"]:
                self.log(t("log.pillow_found"))
            self.log(t("log.hint_transfer"))
            self.log(t("log.hint_format"))
            self.update_font_visibility()
            if self.folder_var.get() and Path(self.folder_var.get()).expanduser().is_dir():
                self.scan_files()
            root.after(5000, self.poll_folder)

        # ---- ui
        def build_ui(self):
            pad = {"padx": 6, "pady": 3}
            top = ttk.Frame(self.root)
            top.pack(fill="x", **pad)

            ttk.Label(top, text=t("gui.folder")).grid(row=0, column=0, sticky="w")
            self.folder_var = tk.StringVar(value=self.settings.get("folder", ""))
            ttk.Entry(top, textvariable=self.folder_var).grid(
                row=0, column=1, sticky="ew", padx=4)
            ttk.Button(top, text=t("gui.browse"), command=self.choose_folder).grid(row=0, column=2)
            self.recursive_var = tk.BooleanVar(
                value=self.settings.get("recursive", True))
            ttk.Checkbutton(top, text=t("gui.recursive"),
                            variable=self.recursive_var).grid(row=0, column=3, sticky="w")

            ttk.Label(top, text=t("gui.device")).grid(row=1, column=0, sticky="w")
            self.host_var = tk.StringVar(value=self.settings.get("host", ""))
            ttk.Entry(top, textvariable=self.host_var).grid(
                row=1, column=1, sticky="ew", padx=4)
            self.find_btn = ttk.Button(top, text=t("gui.find"), command=self.find_device)
            self.find_btn.grid(row=1, column=2)
            self.device_lbl = ttk.Label(top, text=t("gui.not_connected"), foreground="#666")
            self.device_lbl.grid(row=1, column=3, sticky="w")

            ttk.Label(top, text=t("gui.dest")).grid(row=2, column=0, sticky="w")
            self.dest_var = tk.StringVar(value=self.settings.get("dest", "/Books"))
            ttk.Entry(top, textvariable=self.dest_var).grid(
                row=2, column=1, sticky="ew", padx=4)
            self.mode_var = tk.StringVar(
                value=self.settings.get("mode", t("gui.mode_skip")))
            ttk.Combobox(top, textvariable=self.mode_var, state="readonly", width=26,
                         values=[t("gui.mode_skip"), t("gui.mode_replace")]).grid(
                row=2, column=2, columnspan=2, sticky="w")
            self.by_author_var = tk.BooleanVar(
                value=self.settings.get("by_author", True))
            ttk.Checkbutton(top, text=t("gui.by_author"),
                            variable=self.by_author_var).grid(
                row=3, column=1, columnspan=3, sticky="w")
            self.optimize_var = tk.BooleanVar(
                value=self.settings.get("optimize", True))
            ttk.Checkbutton(top, text=t("gui.optimize"),
                            variable=self.optimize_var).grid(
                row=4, column=1, columnspan=2, sticky="w")
            self.ascii_var = tk.BooleanVar(
                value=self.settings.get("ascii_symbols", False))
            ttk.Checkbutton(top, text=t("gui.ascii_symbols"),
                            variable=self.ascii_var).grid(
                row=6, column=1, columnspan=3, sticky="w")
            self.font_label = ttk.Label(top, text=t("gui.font_pt"))
            self.font_label.grid(row=4, column=3, sticky="e")
            old_px = self.settings.get("xtc_font_size")
            default_pt = int(self.settings.get(
                "xtc_font_pt", round(old_px * 72 / SCREEN_PPI) if old_px else 14))
            if default_pt not in XTC_FONT_PT_SIZES:
                default_pt = 14
            self.xtc_font_pt_var = tk.IntVar(value=default_pt)
            self.font_spin = ttk.Combobox(
                top, textvariable=self.xtc_font_pt_var, state="readonly", width=4,
                values=list(XTC_FONT_PT_SIZES))
            self.font_spin.grid(row=4, column=4, sticky="w")

            ttk.Label(top, text=t("gui.language")).grid(row=5, column=0, sticky="w")
            self._lang_by_name = {name: code
                                  for code, name in available_languages().items()}
            cur = self.settings.get("language", "auto")
            cur_label = (t("gui.lang_auto") if cur == "auto"
                         else available_languages().get(current_language(), cur))
            self.lang_var = tk.StringVar(value=cur_label)
            lang_cb = ttk.Combobox(
                top, textvariable=self.lang_var, state="readonly", width=18,
                values=[t("gui.lang_auto")] + list(self._lang_by_name.keys()))
            lang_cb.grid(row=5, column=1, sticky="w")
            lang_cb.bind("<<ComboboxSelected>>", self.change_language)
            top.columnconfigure(1, weight=1)

            btns = ttk.Frame(self.root)
            btns.pack(fill="x", **pad)
            self.scan_btn = ttk.Button(btns, text=t("gui.scan"),
                                       command=self.scan_files)
            self.scan_btn.pack(side="left")
            self.start_btn = ttk.Button(btns, text=t("gui.start"),
                                        command=self.start_batch)
            self.start_btn.pack(side="left", padx=6)
            self.stop_btn = ttk.Button(btns, text=t("gui.stop"), command=self.stop,
                                       state="disabled")
            self.stop_btn.pack(side="left")
            ttk.Label(btns, text=t("gui.format")).pack(side="left", padx=(12, 2))
            labels = format_labels()
            self.bulk_format_var = tk.StringVar(value=labels["epub"])
            ttk.Combobox(btns, textvariable=self.bulk_format_var, state="readonly",
                         width=13,
                         values=list(labels.values())).pack(side="left")
            ttk.Button(btns, text=t("gui.apply_sel"),
                       command=self.apply_bulk_format).pack(side="left", padx=2)
            self.progress = ttk.Progressbar(btns, mode="determinate")
            self.progress.pack(side="left", fill="x", expand=True, padx=10)

            table_frame = ttk.Frame(self.root)
            table_frame.pack(side="top", fill="both", expand=True, padx=6)
            cols = ("file", "fmt", "format", "status")
            self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=12)
            self.tree.heading("file", text=t("gui.col_file"))
            self.tree.heading("fmt", text=t("gui.col_type"))
            self.tree.heading("format", text=t("gui.col_format"))
            self.tree.heading("status", text=t("gui.col_status"))
            self.tree.column("file", width=400, anchor="w")
            self.tree.column("fmt", width=60, anchor="center")
            self.tree.column("format", width=110, anchor="center")
            self.tree.column("status", width=270, anchor="w")
            self.tree.bind("<Button-1>", self.on_tree_click)
            self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
            self.tree.tag_configure("ok", foreground="#0a7d32")
            self.tree.tag_configure("err", foreground="#c0392b")
            self.tree.tag_configure("skip", foreground="#888888")
            self.tree.tag_configure("work", foreground="#1a5fb4")
            self.tree.tag_configure("ready", foreground="#1a5fb4")
            vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=vsb.set)
            preview = ttk.Frame(table_frame)
            preview.pack(side="right", fill="y", padx=(6, 0))
            ttk.Label(preview, text=t("gui.preview"),
                      font=("", 11, "bold")).pack(anchor="center")
            self.cover_label = tk.Label(preview, text=t("gui.cover_none"),
                                        foreground="#888", anchor="center",
                                        width=26, height=12)
            self.cover_label.pack(padx=2, pady=2)
            self.cover_info = tk.Label(preview, text="", foreground="#555",
                                       wraplength=190, justify="center")
            self.cover_info.pack()
            vsb.pack(side="right", fill="y")
            self.tree.pack(side="left", fill="both", expand=True)

            self.log_text = tk.Text(self.root, height=10, wrap="word",
                                    font=("Menlo", 11), state="disabled")
            self.log_text.pack(fill="both", expand=False, padx=6, pady=(3, 6))

        # ---- helpers
        def log(self, text):
            self.q.put(("log", text))

        def load_settings(self):
            try:
                return json.loads(SETTINGS_FILE.read_text("utf-8"))
            except Exception:
                return {}

        def save_settings(self):
            try:
                SETTINGS_FILE.write_text(json.dumps({
                    "folder": self.folder_var.get(),
                    "host": self.host_var.get(),
                    "dest": self.dest_var.get(),
                    "mode": self.mode_var.get(),
                    "recursive": self.recursive_var.get(),
                    "by_author": self.by_author_var.get(),
                    "optimize": self.optimize_var.get(),
                    "ascii_symbols": self.ascii_var.get(),
                    "xtc_font_pt": self.xtc_font_pt_var.get(),
                    "language": self.settings.get("language", "auto"),
                }, ensure_ascii=False, indent=2), "utf-8")
            except Exception:
                pass

        def choose_folder(self):
            d = filedialog.askdirectory(title=t("gui.folder_dialog"))
            if d:
                self.folder_var.set(d)
                self.scan_files()
                self.save_settings()  # выбор папки сохраняется сразу

        def change_language(self, event=None):
            choice = self.lang_var.get()
            code = ("auto" if choice == t("gui.lang_auto")
                    else self._lang_by_name.get(choice, "auto"))
            self.settings["language"] = code
            set_language(code)
            self.save_settings()
            self.rebuild_ui()
            self._upload_font_for_language()

        def _upload_font_for_language(self):
            """При смене языка сразу пробуем загрузить шрифт нового языка,
            если известен адрес устройства. Офлайн — загрузим при отправке."""
            host = self.host_var.get().strip()
            if not host or self.busy:
                return

            def work():
                try:
                    dev = Device(host)
                    dev.status()
                    ensure_reader_font(dev, self.log)
                except Exception:
                    pass

            threading.Thread(target=work, daemon=True).start()

        def rebuild_ui(self):
            """Пересборка интерфейса при смене языка с сохранением списка файлов."""
            for child in self.root.winfo_children():
                child.destroy()
            self.build_ui()
            if self.files:
                folder = Path(self.folder_var.get()).expanduser()
                labels = format_labels()
                self.tree.delete(*self.tree.get_children())
                self.iids = []
                for i, p in enumerate(self.files):
                    try:
                        rel = str(p.relative_to(folder))
                    except ValueError:
                        rel = p.name
                    fmt = self.formats[i] if i < len(self.formats) else "epub"
                    iid = self.tree.insert("", "end", values=(
                        rel, describe_file(p), labels[fmt], "—"))
                    self.iids.append(iid)
                self.progress.configure(maximum=max(1, len(self.files)), value=0)
            self.update_font_visibility()

        def set_busy(self, busy):
            self.busy = busy
            state = "disabled" if busy else "normal"
            for b in (self.scan_btn, self.start_btn, self.find_btn):
                b.configure(state=state)
            self.stop_btn.configure(state="normal" if busy else "disabled")

        # ---- discovery
        def find_device(self):
            self.set_busy(True)
            self.log(t("log.searching"))

            def work():
                try:
                    found = find_devices(log=self.log, stop=self.stop_event)
                    if found:
                        ip, st = found[0]
                        self.q.put(("found", ip, st))
                        for ip2, st2 in found[1:]:
                            self.log(t("log.also_found", ip=ip2, version=st2.get('version')))
                    else:
                        self.log(t("log.not_found_gui"))
                except Cancelled:
                    self.log(t("log.search_stopped"))
                except Exception as e:
                    self.log(t("log.search_error", error=e))
                finally:
                    self.q.put(("busy", False))

            self.stop_event.clear()
            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()

        # ---- scan
        def scan_files(self):
            folder = Path(self.folder_var.get()).expanduser()
            if not folder.is_dir():
                messagebox.showwarning(APP_NAME, t("dlg.choose_folder"))
                return
            paths = scan_folder(folder, self.recursive_var.get())
            # базовый снимок: ничего не помечаем как новое
            self.snapshot = {}
            self.meta = {}
            self.files = []
            self.formats = []
            self.tree.delete(*self.tree.get_children())
            self._cover_req += 1
            self._show_preview({})
            self.iids = []
            labels = format_labels()
            for p in paths:
                key = str(p)
                try:
                    st = p.stat()
                    self.snapshot[key] = (st.st_mtime, st.st_size)
                except OSError:
                    pass
                self.meta[key] = {"fresh": False, "status": "—", "tag": "",
                                  "fmt": "epub"}
                self.files.append(p)
                self.formats.append("epub")
                iid = self.tree.insert("", "end", values=(
                    str(p.relative_to(folder)), describe_file(p),
                    labels["epub"], "—"))
                self.iids.append(iid)
            self.batch_iids = []
            self.progress.configure(maximum=max(1, len(self.files)), value=0)
            self.log(t("log.found_files", count=len(self.files), folder=folder))
            self.update_font_visibility()

        def poll_folder(self):
            try:
                if not self.busy:
                    self._watch_folder()
            finally:
                self.root.after(5000, self.poll_folder)

        def _watch_folder(self):
            """Мониторинг папки: новые и изменённые файлы наверху списка."""
            folder = Path(self.folder_var.get()).expanduser()
            if not folder.is_dir():
                return
            current = {}
            for p in scan_folder(folder, self.recursive_var.get()):
                try:
                    st = p.stat()
                except OSError:
                    continue
                current[str(p)] = (st.st_mtime, st.st_size)
            added, changed, removed = snapshot_diff(self.snapshot, current)
            if not (added or changed or removed):
                return
            for k in added:
                self.meta[k] = {"fresh": True, "status": t("status.new"),
                                "tag": "work", "fmt": "epub"}
            for k in changed:
                m = self.meta.get(k) or {"fmt": "epub"}
                m.update({"fresh": True, "status": t("status.changed"),
                          "tag": "work"})
                self.meta[k] = m
            for k in removed:
                self.meta.pop(k, None)
            self.snapshot = current
            self._rebuild_tree(folder)
            self.log(t("log.watch_changed", n=len(added) + len(changed)))

        def _rebuild_tree(self, folder):
            """Перестраивает список: новые/изменённые сверху (по времени),
            остальные — по алфавиту; статусы и форматы сохраняются."""
            labels = format_labels()
            fresh = [k for k, m in self.meta.items() if m["fresh"]]
            rest = [k for k, m in self.meta.items() if not m["fresh"]]
            fresh.sort(key=lambda k: self.snapshot.get(k, (0.0,))[0],
                       reverse=True)
            rest.sort()
            self.files = [Path(k) for k in fresh + rest]
            self.formats = [self.meta[k]["fmt"] for k in fresh + rest]
            self.batch_iids = []
            self.tree.delete(*self.tree.get_children())
            self._cover_req += 1
            self._show_preview({})
            self.iids = []
            for k in fresh + rest:
                p = Path(k)
                m = self.meta[k]
                try:
                    rel = str(p.relative_to(folder))
                except ValueError:
                    rel = p.name
                iid = self.tree.insert("", "end", values=(
                    rel, describe_file(p), labels[m["fmt"]], m["status"]),
                    tags=(m["tag"],) if m["tag"] else ())
                self.iids.append(iid)
            self.progress.configure(maximum=max(1, len(self.files)), value=0)
            self.update_font_visibility()

        def update_font_visibility(self):
            """Размер шрифта показываем только если выбран XTC/XTCH."""
            any_xtc = any(f in ("xtc", "xtch")
                          for f in getattr(self, "formats", []))
            if any_xtc:
                self.font_label.grid()
                self.font_spin.grid()
            else:
                self.font_label.grid_remove()
                self.font_spin.grid_remove()

        def on_tree_click(self, event):
            if self.tree.identify("region", event.x, event.y) != "cell":
                return
            if self.tree.identify_column(event.x) != "#3":
                return
            row = self.tree.identify_row(event.y)
            if not row or row not in self.iids:
                return
            idx = self.iids.index(row)
            cycle = {"epub": "xtc", "xtc": "xtch", "xtch": "epub"}
            self.formats[idx] = cycle.get(self.formats[idx], "epub")
            m = self.meta.get(str(self.files[idx]))
            if m:
                m["fmt"] = self.formats[idx]
            self.tree.set(row, "format", format_labels()[self.formats[idx]])
            self.update_font_visibility()

        def apply_bulk_format(self):
            fmt = label_to_format(self.bulk_format_var.get())
            sel = self.tree.selection()
            if not sel:
                self.log(t("log.select_rows"))
                return
            labels = format_labels()
            for row in sel:
                if row in self.iids:
                    idx = self.iids.index(row)
                    self.formats[idx] = fmt
                    m = self.meta.get(str(self.files[idx]))
                    if m:
                        m["fmt"] = fmt
                    self.tree.set(row, "format", labels[fmt])
            self.update_font_visibility()

        # ---- превью обложки выбранной книги
        def on_tree_select(self, _event=None):
            sel = self.tree.selection()
            if not sel or sel[0] not in self.iids:
                self._show_preview({})
                return
            path = self.files[self.iids.index(sel[0])]
            self._cover_req += 1
            req = self._cover_req
            key = str(path)
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            cached = self._cover_cache.get(key)
            if cached is not None and cached[0] == size:
                self._show_preview(cached[1])
                return
            self._show_preview(None)  # разбор в фоне, результат — через очередь

            def work():
                data = book_cover(path)
                self.q.put(("cover", req, key, size, data))

            threading.Thread(target=work, daemon=True).start()

        def _show_preview(self, data):
            """data=None — загрузка; {} — нет обложки; иначе dict из book_cover."""
            self._cover_photo = None
            if data is None:
                self.cover_label.configure(image="", text=t("gui.cover_loading"),
                                           width=26, height=12)
                self.cover_info.configure(text="")
                return
            info = "\n".join(p for p in (data.get("title", ""),
                                         data.get("author", "")) if p)
            photo = None
            if data.get("cover"):
                raw = data["cover"]
                ext = (data.get("cover_ext") or ".jpg").lower()
                try:
                    if tools.get("pillow"):
                        from PIL import Image as pil_image, ImageTk
                        img = pil_image.open(io.BytesIO(raw))
                        img.thumbnail(PREVIEW_BOX)
                        photo = ImageTk.PhotoImage(img)
                    elif ext in (".png", ".gif"):
                        photo = tk.PhotoImage(
                            data=base64.b64encode(raw).decode("ascii"))
                except Exception:
                    photo = None
            if photo is not None:
                self._cover_photo = photo
                self.cover_label.configure(image=photo, text="",
                                           width=PREVIEW_BOX[0] + 10,
                                           height=PREVIEW_BOX[1] + 10)
            else:
                self.cover_label.configure(image="", text=t("gui.cover_none"),
                                           width=26, height=12)
            self.cover_info.configure(text=info)

        # ---- batch
        def start_batch(self):
            if not tools["pandoc"]:
                messagebox.showerror(APP_NAME, t("dlg.need_pandoc"))
                return
            if not self.files:
                self.scan_files()
                if not self.files:
                    return
            folder = Path(self.folder_var.get()).expanduser()
            host = self.host_var.get().strip()
            dest = self.dest_var.get().strip() or "/Books"
            if not dest.startswith("/"):
                dest = "/" + dest
            replace = self.mode_var.get() == t("gui.mode_replace")
            by_author = self.by_author_var.get()
            optimize = self.optimize_var.get()
            ascii_symbols = self.ascii_var.get()
            formats = list(getattr(self, "formats", []))
            try:
                xtc_font_pt = int(self.xtc_font_pt_var.get())
            except (ValueError, tk.TclError):
                xtc_font_pt = 14
            sel = self.tree.selection()
            files, formats, iids = selected_subset(
                self.files, formats, self.iids, sel)
            self.batch_iids = iids
            if sel:
                self.log(t("log.batch_selected", n=len(files)))
            for iid in iids:
                self.tree.set(iid, "status", t("status.queued"))
            self.set_busy(True)
            self.stop_event.clear()
            self.progress.configure(maximum=len(files), value=0)

            def on_file(idx, text, tag):
                self.q.put(("row", idx, text, tag))

            def on_progress(done, total):
                self.q.put(("progress", done, total))

            def work():
                try:
                    run_batch(files, host, dest, replace, tools, self.log,
                              self.stop_event, on_file, on_progress,
                              on_device=lambda ip, st: self.q.put(("found", ip, st)),
                              on_waiting=lambda n: self.q.put(("waiting", n)),
                              by_author=by_author, optimize=optimize,
                              formats=formats, xtc_font_pt=xtc_font_pt,
                              ascii_symbols=ascii_symbols)
                    self.save_settings_quiet()
                except Cancelled:
                    self.log(t("log.stopped_user"))
                except (DeviceError, error.URLError, OSError) as e:
                    self.log(t("log.device_error", error=e))
                except Exception as e:
                    self.log(t("log.unexpected", error=e))
                finally:
                    self.q.put(("busy", False))

            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()

        def save_settings_quiet(self):
            self.q.put(("save",))

        def stop(self):
            self.stop_event.set()
            self.log(t("log.stopping"))

        # ---- queue
        def poll_queue(self):
            try:
                while True:
                    msg = self.q.get_nowait()
                    kind = msg[0]
                    if kind == "log":
                        self.log_text.configure(state="normal")
                        self.log_text.insert(
                            "end", time.strftime("[%H:%M:%S] ") + msg[1] + "\n")
                        if int(self.log_text.index("end-1c").split(".")[0]) > 2500:
                            self.log_text.delete("1.0", "500.0")
                        self.log_text.see("end")
                        self.log_text.configure(state="disabled")
                    elif kind == "row":
                        _, idx, text, tag = msg
                        batch_iids = getattr(self, "batch_iids", self.iids)
                        if idx < len(batch_iids):
                            iid = batch_iids[idx]
                            self.tree.set(iid, "status", text)
                            self.tree.item(iid, tags=(tag,))
                            self.tree.see(iid)
                            if idx < len(self.files):
                                m = self.meta.get(str(self.files[idx]))
                                if m:
                                    m["status"] = text
                                    m["tag"] = tag
                    elif kind == "progress":
                        self.progress.configure(maximum=max(1, msg[2]), value=msg[1])
                    elif kind == "cover":
                        _, req, key, size, data = msg
                        if data and data.get("cover"):
                            self._cover_cache[key] = (size, data)
                            while len(self._cover_cache) > 24:
                                self._cover_cache.pop(next(iter(self._cover_cache)))
                        if req == self._cover_req:
                            self._show_preview(data or {})
                    elif kind == "waiting":
                        self.device_lbl.configure(
                            text=t("status.waiting", n=msg[1]),
                            foreground="#b36b00")
                    elif kind == "found":
                        _, ip, st = msg
                        self.host_var.set(ip)
                        self.device_lbl.configure(
                            text=f"{st.get('device', '?')} v{st.get('version', '?')}",
                            foreground="#0a7d32")
                        self.log(t("log.device_found", ip=ip,
                                   version=st.get('version', '?'),
                                   mode=st.get('mode', '?')))
                    elif kind == "host":
                        self.host_var.set(msg[1])
                    elif kind == "busy":
                        self.set_busy(msg[1])
                    elif kind == "save":
                        self.save_settings()
            except queue.Empty:
                pass
            self.root.after(100, self.poll_queue)

        def on_close(self):
            self.save_settings()
            self.stop_event.set()
            self.root.destroy()

    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    app = App(root)
    root.mainloop()
    return app


# ---------------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(
        description=t("cli.description"))
    ap.add_argument("--cli", action="store_true", help=t("cli.cli"))
    ap.add_argument("--folder", help=t("cli.folder"))
    ap.add_argument("--host", help=t("cli.host"))
    ap.add_argument("--dest", default="/Books", help=t("cli.dest"))
    ap.add_argument("--replace", action="store_true",
                    help=t("cli.replace"))
    ap.add_argument("--no-recursive", action="store_true", help=t("cli.no_recursive"))
    ap.add_argument("--wait-timeout", type=int, default=0, metavar="SEC",
                    help=t("cli.wait_timeout"))
    ap.add_argument("--flat", action="store_true",
                    help=t("cli.flat"))
    ap.add_argument("--no-optimize", action="store_true",
                    help=t("cli.no_optimize"))
    ap.add_argument("--ascii-symbols", action="store_true",
                    help=t("cli.ascii_symbols"))
    ap.add_argument("--format", choices=["epub", "xtc", "xtch"], default="epub",
                    help=t("cli.format"))
    ap.add_argument("--xtc-font-pt", type=int, default=14, choices=[12, 14, 16, 18],
                    help=t("cli.font_pt"))
    ap.add_argument("--lang", default=None,
                    help="UI language: " + ", ".join(
                        ["auto"] + list(available_languages())))
    ap.add_argument("--find", action="store_true", help=t("cli.find"))
    args = ap.parse_args()
    if args.lang:
        set_language(args.lang)

    def log(text):
        print(text, flush=True)

    if args.find:
        found = find_devices(log=log)
        if not found:
            log(t("cli.no_devices"))
            return 1
        for ip, st in found:
            log(t("cli.device_line", ip=ip, device=st.get("device"),
                  version=st.get("version"), mode=st.get("mode")))
        return 0

    if not args.cli:
        try:
            run_gui()
        except ImportError as e:
            print(t("cli.tkinter_missing", error=e), file=sys.stderr)
            return 2
        return 0

    if not args.folder:
        ap.error(t("cli.need_folder"))
    tools = detect_tools()
    if not tools["pandoc"]:
        log(t("cli.pandoc_missing"))
        return 2
    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        log(t("cli.folder_missing", folder=folder))
        return 2
    files = scan_folder(folder, not args.no_recursive)
    if not files:
        log(t("cli.no_files"))
        return 0
    log(t("cli.files_count", n=len(files)))
    host = args.host or ""
    dest = args.dest if args.dest.startswith("/") else "/" + args.dest
    try:
        _, _, errors = run_batch(files, host, dest, args.replace, tools, log,
                                 wait_timeout=args.wait_timeout,
                                 by_author=not args.flat,
                                 optimize=not args.no_optimize,
                                 formats=[args.format] * len(files),
                                 xtc_font_pt=args.xtc_font_pt,
                                 ascii_symbols=args.ascii_symbols)
    except KeyboardInterrupt:
        log(t("cli.interrupted"))
        return 130
    except (DeviceError, error.URLError, OSError) as e:
        log(t("cli.device_error", error=e))
        return 1
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
