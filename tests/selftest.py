#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Самотест BookSender без реального устройства.

Создаёт учебные книги (FB2 с картинками и обложкой, FB2 в cp1251, zip с
именами cp866, 7z, html с картинкой, txt, epub, pdf-заглушку), поднимает
локальный mock-сервер CrossPoint (/api/status, /api/files, /mkdir, /upload)
и прогоняет весь конвейер: скан -> конвертация -> загрузка -> повторный
пропуск -> перезапись -> CLI.

Запуск: python3.13 tests/selftest.py
"""
from __future__ import annotations

import base64
import io
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import book_sender as bs  # noqa: E402

PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG = base64.b64decode(PNG_B64)
FIXTURE_META = {}

FB2_TEMPLATE = """<?xml version="1.0" encoding="{enc}"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
 <description>
  <title-info>
   <genre>sf</genre>
   <author><first-name>Иван</first-name><last-name>Тестов</last-name></author>
   <book-title>{title}</book-title>
   <lang>ru</lang>
   <coverpage><image l:href="#cover.png"/></coverpage>
  </title-info>
  <document-info>
   <author><nickname>selftest</nickname></author>
   <program-used>selftest</program-used>
   <date>2026-11-14</date>
   <id>{bid}</id>
   <version>1.0</version>
  </document-info>
 </description>
 <body>
  <title><p>{title}</p></title>
  <section>
   <p>Первый абзац с картинкой:</p>
   <p><image l:href="#pic.png"/></p>
   <p>Второй абзац.</p>
  </section>
 </body>
 <binary id="cover.png" content-type="image/png">{png}</binary>
 <binary id="pic.png" content-type="image/png">{png}</binary>
</FictionBook>
"""


def make_fb2(title, bid, enc="utf-8"):
    text = FB2_TEMPLATE.format(title=title, bid=bid, enc=enc, png=PNG_B64)
    return text.encode(enc)


# ------------------------------------------------------------- mock-устройство

class MockHandler(BaseHTTPRequestHandler):
    storage: Path = None  # задаётся в make_mock
    v2_empty_missing = False  # поведение прошивки 2.0.0: 200 [] вместо 404

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _dir(self, path):
        rel = path.lstrip("/")
        return self.storage / rel if rel else self.storage

    def do_GET(self):
        u = parse.urlparse(self.path)
        q = parse.parse_qs(u.query)
        if u.path == "/api/status":
            self._json(200, {
                "version": "1.3.0-mock", "firmwareFamily": "crosspoint",
                "device": "X4", "mode": "STA", "ip": "127.0.0.1",
            })
        elif u.path == "/api/files":
            d = self._dir(q.get("path", ["/"])[0])
            if not d.is_dir():
                if self.v2_empty_missing:
                    self._json(200, [])
                else:
                    self._send(404, b"not found")
                return
            items = []
            for p in sorted(d.iterdir()):
                items.append({
                    "name": p.name,
                    "size": p.stat().st_size if p.is_file() else 0,
                    "isDirectory": p.is_dir(),
                    "isEpub": p.suffix.lower() == ".epub",
                })
            self._json(200, items)
        elif u.path == "/":
            self._send(200, b"<html>CrossPoint mock</html>", "text/html")
        else:
            self._send(404, b"not found")

    def do_POST(self):
        u = parse.urlparse(self.path)
        q = parse.parse_qs(u.query)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if u.path == "/mkdir":
            form = parse.parse_qs(body.decode())
            parent = self._dir(form.get("path", ["/"])[0])
            name = form["name"][0]
            d = parent / name
            if d.exists():
                self._send(400, b"Folder already exists")
                return
            d.mkdir(parents=True)
            self._send(200, b"Folder created")
        elif u.path == "/upload":
            dest = self._dir(q.get("path", ["/"])[0])
            overwrite = q.get("overwrite", ["0"])[0] in ("1", "true")
            ctype = self.headers.get("Content-Type", "")
            raw = (b"Content-Type: " + ctype.encode()
                   + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            saved = None
            for part in msg.iter_parts():
                if part.get_param("name", header="content-disposition") == "file":
                    fname = part.get_filename()
                    data = part.get_payload(decode=True)
                    target = dest / fname
                    if target.exists() and not overwrite:
                        self._send(400, f"File already exists: {fname}".encode())
                        return
                    dest.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    saved = fname
            if saved:
                self._send(200, f"File uploaded successfully: {saved}".encode())
            else:
                self._send(400, b"no file part")
        elif u.path == "/delete":
            form = parse.parse_qs(body.decode())
            p = self._dir(form["path"][0])
            if p.is_file():
                p.unlink()
                self._send(200, b"All items deleted successfully")
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
                self._send(200, b"All items deleted successfully")
            else:
                self._send(500, b"cannot delete")
        else:
            self._send(404, b"not found")


def make_mock(storage: Path, port=0, v2=False):
    handler = type("H", (MockHandler,), {
        "storage": storage, "v2_empty_missing": v2})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"127.0.0.1:{srv.server_address[1]}"


# ------------------------------------------------------------------- фикстуры

def make_fixtures(d: Path, tools):
    d.mkdir(parents=True, exist_ok=True)
    (d / "img.png").write_bytes(PNG)

    (d / "book1.fb2").write_bytes(make_fb2("Проверка пера", "t1"))
    with zipfile.ZipFile(d / "book1.fb2.zip", "w") as z:
        z.writestr("book1.fb2", make_fb2("Проверка пера", "t1"))

    (d / "cp1251.fb2").write_bytes(make_fb2("Кодировка.win", "t2", "cp1251"))

    # .fbz — zip с fb2 под другим именем (Flibusta)
    with zipfile.ZipFile(d / "fbz.fb2.fbz", "w") as z:
        z.writestr("fbz.fb2", make_fb2("Формат FBZ", "tfbz"))

    # zip с именами в cp866 (без флага UTF-8)
    with zipfile.ZipFile(d / "russian.zip", "w") as z:
        for i, title in enumerate(["Первая книга", "Вторая книга"]):
            raw_name = f"Книга {i + 1}.fb2".encode("cp866").decode("cp437")
            zi = zipfile.ZipInfo(raw_name)
            z.writestr(zi, make_fb2(title, f"z{i}"))

    if tools.get("7z"):
        (d / "_7z_src").mkdir()
        (d / "_7z_src" / "seven.fb2").write_bytes(make_fb2("Семь зип", "t7"))
        subprocess.run(
            [tools["7z"], "a", str(d / "archive.7z"), "seven.fb2"],
            check=True, capture_output=True, cwd=str(d / "_7z_src"),
        )
        shutil.rmtree(d / "_7z_src")

    (d / "note.txt").write_text("Заметка\n=======\n\nТекст заметки.\n", "utf-8")
    (d / "page.html").write_text(
        '<html><head><title>Страница</title></head><body>'
        '<p>Картинка: <img src="img.png"></p></body></html>', "utf-8")

    subprocess.run(
        [tools["pandoc"], "-f", "markdown", "-t", "epub",
         "--metadata", "title=Готовый EPUB", "-o", str(d / "ready.epub")],
        input="# Глава\n\nТекст.\n".encode(), check=True, capture_output=True,
    )

    if not tools.get("calibre"):
        pass  # PDF теперь обрабатывается без Calibre: текст->EPUB, скан->XTC
    (d / "unsupported.cbr").write_bytes(b"fake comic book archive")
    (d / "native.xtc").write_bytes(b"XTC fake pre-rendered book")

    # текстовый PDF -> EPUB (перенабор); PDF без текста (скан) -> XTC страницами
    try:
        import pymupdf
        georgia = next((p for p in bs.FONT_CANDIDATES if Path(p).is_file()), None)
        if georgia is None:
            raise ImportError("нет шрифта для PDF-фикстур")
        doc = pymupdf.open()
        for t in ("Первая глава", "Вторая глава"):
            p = doc.new_page(width=595, height=842)
            p.insert_text((60, 80), t, fontsize=22,
                          fontfile=str(georgia), fontname="G")
            p.insert_text((60, 130), "Текст для проверки перенабора под экран.",
                          fontsize=12, fontfile=str(georgia), fontname="G")
        # картинка на первой странице — должна встроиться в EPUB
        from PIL import Image as PILImage
        ipng = d / "pic.png"
        PILImage.effect_noise((80, 60), 100).convert("RGB").save(ipng)
        doc[0].insert_image(pymupdf.Rect(400, 60, 520, 105), filename=str(ipng))
        doc.set_metadata({"title": "Тестовый PDF", "author": "Иван Тестов"})
        doc.save(d / "doc.pdf")
        doc.close()
        # скан: страница-картинка БЕЗ текстового слоя (настоящий скан)
        scan = pymupdf.open()
        simg = PILImage.new("L", (1240, 1754), 255)  # «скан A4» 150 dpi
        (d / ".scan").mkdir(exist_ok=True)
        (d / "_scan").mkdir(exist_ok=True)
        from PIL import ImageDraw as _ID
        dr = _ID.Draw(simg)
        dr.rectangle([200, 300, 1040, 1450], outline=150, width=3)
        for i in range(10):
            dr.line([240, 400 + i * 90, 1000, 400 + i * 90], fill=60, width=4)
        simg.save(d / ".scan" / "scanimg.png")
        p = scan.new_page(width=595, height=842)
        p.insert_image(p.rect, filename=str(d / ".scan" / "scanimg.png"))
        scan.save(d / "scan.pdf")
        scan.close()
        # двухколоночный PDF -> эвристика должна распознать сложную вёрстку
        cols = pymupdf.open()
        for _ in range(2):
            p = cols.new_page(width=595, height=842)
            for x in (60, 330):
                for i in range(8):
                    p.insert_text((x, 80 + i * 40), f"Колонка текст {i}",
                                  fontsize=11, fontfile=str(georgia), fontname="G")
        cols.set_metadata({"title": "Две колонки", "author": "Иван Тестов"})
        cols.save(d / "columns.pdf")
        cols.close()
    except ImportError:
        pass

    # «плохой» epub: встроенный шрифт, @font-face, width/height у img —
    # всё это должен вычистить оптимизатор перед отправкой
    with zipfile.ZipFile(d / "fonted.epub", "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles>'
                   '</container>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="3.0"><metadata/><manifest>'
                   '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="css" href="style.css" media-type="text/css"/>'
                   '<item id="font" href="fonts/embedded.ttf" '
                   'media-type="application/x-font-ttf"/></manifest>'
                   '<spine><itemref idref="ch1"/></spine></package>')
        z.writestr("OEBPS/style.css",
                   '@font-face { font-family: "X"; src: url(fonts/embedded.ttf); }\n'
                   'p { color: red; float: left; position: absolute; display: flex; }')
        z.writestr("OEBPS/ch1.xhtml",
                   '<html><head><title>t</title></head><body><!-- лишний комментарий -->'
                   '<p>Текст</p><p style="float:right; color:blue;">x</p>'
                   '<img src="x.png" width="500" height="300"/></body></html>')
        z.writestr("OEBPS/fonts/embedded.ttf", b"\x00\x01\x00\x00" + b"fakefont" * 100)

    # epub с БИТЫМ встроенным шрифтом (повреждённый deflate-поток):
    # оптимизатор должен выбросить шрифт, а не упасть
    bp = d / "brokenfont.epub"
    with zipfile.ZipFile(bp, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles>'
                   '</container>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="3.0"><metadata/><manifest>'
                   '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="font" href="fonts/broken.ttf" '
                   'media-type="application/x-font-ttf"/></manifest>'
                   '<spine><itemref idref="ch1"/></spine></package>')
        z.writestr("OEBPS/ch1.xhtml",
                   '<html><head><title>t</title></head><body><p>Текст</p></body></html>')
        z.writestr("OEBPS/fonts/broken.ttf", b"\x00\x01\x00\x00" + b"fakefont" * 200,
                   compress_type=zipfile.ZIP_DEFLATED)
    with open(bp, "r+b") as f:
        blob = f.read()
        idx = blob.find(b"fonts/broken.ttf")
        hdr = idx - 30
        nlen = struct.unpack("<H", blob[hdr + 26:hdr + 28])[0]
        elen = struct.unpack("<H", blob[hdr + 28:hdr + 30])[0]
        f.seek(hdr + 30 + nlen + elen + 4)
        f.write(b"\xff" * 24)

    # большая цветная картинка -> должна стать серой и вписаться в экран 800x480
    try:
        from PIL import Image
        noise = Image.effect_noise((1600, 1200), 128).convert("RGB")
        buf = io.BytesIO()
        noise.save(buf, "PNG")
        FIXTURE_META["bigimg_png_size"] = buf.tell()
        with zipfile.ZipFile(d / "bigimg.epub", "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml",
                       '<?xml version="1.0"?><container version="1.0" '
                       'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                       '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                       'media-type="application/oebps-package+xml"/></rootfiles>'
                       '</container>')
            z.writestr("OEBPS/content.opf",
                       '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                       'version="3.0"><metadata/><manifest>'
                       '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                       '<item id="img" href="pic.png" media-type="image/png"/></manifest>'
                       '<spine><itemref idref="ch1"/></spine></package>')
            z.writestr("OEBPS/ch1.xhtml",
                       '<html><head><title>t</title></head><body>'
                       '<img src="pic.png"/></body></html>')
            z.writestr("OEBPS/pic.png", buf.getvalue())
    except ImportError:
        pass

    # md/docx с картинками: файл в подпапке рядом, base64 внутри md,
    # встроенная в docx
    try:
        from PIL import Image
        (d / "pics").mkdir(exist_ok=True)
        Image.effect_noise((200, 150), 90).convert("RGB").save(d / "pics" / "pic.png")
        (d / "mdpic.md").write_text(
            "# Заметка\n\nКартинка из подпапки:\n\n![](pics/pic.png)\n", "utf-8")
        b64 = base64.b64encode((d / "pics" / "pic.png").read_bytes()).decode()
        (d / "b64pic.md").write_text(
            f"Картинка base64:\n\n![](data:image/png;base64,{b64})\n", "utf-8")
        subprocess.run([tools["pandoc"], "mdpic.md", "-o", "docxpic.docx"],
                       cwd=str(d), check=True, capture_output=True)
    except ImportError:
        pass
    # DjVu (нужен DjVuLibre: c44/djvm) — должен уйти в XTC страницами
    try:
        import shutil as _sh
        from PIL import Image as _PILImage, ImageDraw as _PILDraw
        if _sh.which("c44") and _sh.which("djvm") and tools.get("ddjvu"):
            for i in (1, 2):
                im = _PILImage.new("L", (480, 800), 255)
                _PILDraw.Draw(im).rectangle([40, 40 + i * 10, 440, 700], outline=100)
                im.save(d / f"p{i}.ppm")
                subprocess.run(["c44", f"p{i}.ppm", f"p{i}.djvu"], cwd=str(d),
                               check=True, capture_output=True)
            subprocess.run(["djvm", "-c", "scanbook.djvu", "p1.djvu", "p2.djvu"],
                           cwd=str(d), check=True, capture_output=True)
            for f in ("p1.ppm", "p2.ppm", "p1.djvu", "p2.djvu"):
                (d / f).unlink(missing_ok=True)
    except Exception:
        pass
    return d


# --------------------------------------------------------------------- тесты

def epub_images(path: Path):
    with zipfile.ZipFile(path) as z:
        return [n for n in z.namelist()
                if "/media/" in n or n.startswith("media/")
                or n.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))]


def main():
    tools = bs.detect_tools()
    assert tools["pandoc"], "нужен pandoc"
    tmp = Path(tempfile.mkdtemp(prefix="booksender_test_"))
    # размер шрифта XTC задаётся в pt (как в прошивке) и переводится в px
    s14 = json.loads(bs.xtc_settings_file(tmp, font_pt=14).read_text())
    s12 = json.loads(bs.xtc_settings_file(tmp, font_pt=12).read_text())
    assert s14["font"]["size"] == 42 and s12["font"]["size"] == 36, (s14, s12)
    print("шрифт XTC: 14pt -> 42px, 12pt -> 36px — ОК")
    # мониторинг папки: сравнение снимков
    snap = {"a.fb2": (1.0, 10), "b.epub": (2.0, 20)}
    cur = {"a.fb2": (1.5, 12), "b.epub": (2.0, 20), "c.pdf": (3.0, 30)}
    added, changed, removed = bs.snapshot_diff(snap, cur)
    assert added == ["c.pdf"] and changed == ["a.fb2"] and removed == [], \
        (added, changed, removed)
    print("мониторинг папки: snapshot_diff — ОК")
    # замена редких символов на ASCII (стрелки/галочки)
    import zipfile as _zf
    sym = tmp / "symbols.epub"
    with _zf.ZipFile(sym, "w") as z:
        z.writestr("mimetype", b"application/epub+zip")
        z.writestr("OEBPS/ch1.xhtml",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<html><body>'
                   '<p>Стрелка → и галочка ✓ и ⇒</p></body></html>'.encode("utf-8"))
    n_sym = bs.ascii_symbols_epub(sym)
    assert n_sym == 3, n_sym
    with _zf.ZipFile(sym) as z:
        content = z.read("OEBPS/ch1.xhtml").decode("utf-8")
        assert "->" in content and "=>" in content and " v " in content, content
        assert "→" not in content and "✓" not in content and "⇒" not in content
        assert z.read("mimetype") == b"application/epub+zip"
    print("замена символов -> ASCII — ОК")
    # i18n: каталоги, автоопределение, фолбэк
    locales = Path(bs.__file__).resolve().parent / "locales"
    langs = bs.available_languages()
    assert {"ru", "en", "zh"} <= set(langs), langs
    assert langs["en"] == "English" and langs["zh"] == "中文", langs
    for code in ("en", "zh"):
        catalog = json.loads((locales / f"{code}.json").read_text("utf-8"))
        bs.set_language(code)
        for key in ("gui.find", "fmt.xtc", "log.summary", "cli.folder"):
            assert bs.t(key) == catalog[key], f"{code}: {key} != {catalog[key]!r}"
    bs.set_language("auto")
    assert bs.current_language() in langs, "автоопределение должно дать известный язык"
    bs.set_language("en")
    assert bs.t("__no_such_key__") == "__no_such_key__", "фолбэк на сам ключ"
    bs.set_language("ru")
    assert bs.t("gui.find") == "Найти"
    print(f"i18n: языки {sorted(langs)}, автоопределение и фолбэк — ОК")
    ok = True
    try:
        fixtures = make_fixtures(tmp / "books", tools)
        storage = tmp / "sd"
        storage.mkdir()
        srv, host = make_mock(storage)
        print(f"mock-устройство: http://{host}")
        logs = []
        files = bs.scan_folder(fixtures)
        print(f"файлов-фикстур: {len(files)}")

        # 1) полный прогон
        sent, skipped, errors = bs.run_batch(
            files, host, "/Books", False, tools, logs.append)
        print("\n".join(logs))
        assert errors == 0, f"ошибки в первом прогоне: {errors}"
        assert (storage / "fonts" / "GentiumBookPlus" / "GentiumBookPlus-Regular.ttf").is_file(), \
            "шрифт Gentium Book Plus должен автоматически загрузиться на устройство"
        books = storage / "Books"
        names = {str(p.relative_to(books)) for p in books.rglob("*") if p.is_file()}
        print("на «SD»:", sorted(names))
        AF = "Иван Тестов"  # pandoc пишет dc:creator как first-name + last-name
        expected = {
            f"{AF}/Тестов Иван - Проверка пера.epub",  # fb2 и fb2.zip -> одно имя (пропуск дубля)
            f"{AF}/Тестов Иван - Кодировка.win.epub",
            f"{AF}/Тестов Иван - Формат FBZ.epub",
            f"{AF}/Тестов Иван - Первая книга.epub",
            f"{AF}/Тестов Иван - Вторая книга.epub",
            "note.epub", "page.epub", "ready.epub", "fonted.epub",
            "brokenfont.epub", "native.xtc",  # без автора -> корень
        }
        try:
            import PIL  # noqa: F401
            expected.add("bigimg.epub")
            expected.update({"mdpic.epub", "b64pic.epub", "docxpic.epub"})
            have_pil = True
        except ImportError:
            have_pil = False
        if tools.get("pymupdf"):
            expected.add(f"{AF}/Иван Тестов - Тестовый PDF.epub")
            expected.add("scan.xtc")
            expected.add(f"{AF}/Иван Тестов - Две колонки.xtc")
        if tools.get("ddjvu") and (fixtures / "scanbook.djvu").exists():
            expected.add("scanbook.xtc")
        if tools.get("7z"):
            expected.add(f"{AF}/Тестов Иван - Семь зип.epub")
        missing = expected - names
        assert not missing, f"не загружено: {missing}"

        # 2) картинки внутри EPUB
        for n in (f"{AF}/Тестов Иван - Проверка пера.epub",
                  f"{AF}/Тестов Иван - Первая книга.epub"):
            imgs = epub_images(books / n)
            assert imgs, f"нет картинок в {n}"
            print(f"картинки в {n}: {imgs}")
        assert epub_images(books / "page.epub"), "html-картинка потерялась"
        # cp1251 должен перекодироваться в UTF-8 без кракозябр
        with zipfile.ZipFile(books / AF / "Тестов Иван - Кодировка.win.epub") as z:
            text = " ".join(z.read(n).decode("utf-8", "replace")
                            for n in z.namelist() if n.endswith(".xhtml"))
        assert "Первый абзац" in text, "cp1251 текст должен быть корректным"
        print("cp1251 -> UTF-8 без кракозябр — ОК")

        # 2b) оптимизация: шрифт удалён, @font-face вычищен, CSS добавлен,
        # width/height у img сняты, OPF без записи о шрифте
        with zipfile.ZipFile(books / "fonted.epub") as z:
            nn = z.namelist()
            assert not any("embedded.ttf" in n for n in nn), "шрифт должен быть удалён"
            assert "@font-face" not in z.read("OEBPS/style.css").decode()
            ch = z.read("OEBPS/ch1.xhtml").decode()
            assert "max-width:100%" in ch, "защитный CSS должен быть добавлен"
            assert 'width="500"' not in ch and 'height="300"' not in ch
            assert "embedded.ttf" not in z.read("OEBPS/content.opf").decode()
            assert nn[0] == "mimetype", "mimetype должен остаться первым"
            css = z.read("OEBPS/style.css").decode()
            assert "float" not in css and "position" not in css and "flex" not in css, \
                "вредный CSS должен быть вычищен"
            assert "<!--" not in ch, "комментарии должны быть удалены"
            assert "float" not in ch, "inline-стили должны быть вычищены"
        print("оптимизация EPUB (шрифты/CSS/img/комментарии) — ОК")

        # 2b) большая цветная картинка -> серая, вписана в экран, меньше размером
        if have_pil:
            from PIL import Image
            with zipfile.ZipFile(books / "bigimg.epub") as z:
                raw = z.read("OEBPS/pic.png")
                im = Image.open(io.BytesIO(raw))
                assert im.mode == "L", f"картинка должна быть серой, а не {im.mode}"
                assert im.width <= 800 and im.height <= 480, im.size
                assert len(raw) < FIXTURE_META["bigimg_png_size"], "размер должен уменьшиться"
            print(f"картинка 1600x1200 RGB -> {im.width}x{im.height} серая, "
                  f"{FIXTURE_META['bigimg_png_size'] // 1024} -> {len(raw) // 1024} КБ — ОК")
            for n in ("mdpic.epub", "b64pic.epub", "docxpic.epub"):
                with zipfile.ZipFile(books / n) as z:
                    assert any("/media/" in x for x in z.namelist()), \
                        f"нет картинки в {n}"
            print("md/docx с картинками (подпапка, base64, встроенные) — ОК")

        # 2c) битый шрифт выброшен, книга цела
        with zipfile.ZipFile(books / "brokenfont.epub") as z:
            nn = z.namelist()
            assert not any(n.endswith(".ttf") for n in nn), "битый шрифт должен быть выброшен"
            assert "max-width:100%" in z.read("OEBPS/ch1.xhtml").decode()
            assert "broken.ttf" not in z.read("OEBPS/content.opf").decode()
        print("битый встроенный шрифт удалён без падения — ОК")

        # 2d) PDF: текст -> EPUB с перенабором (картинки встраиваются),
        # скан -> XTC страницами (с обрезкой полей)
        if tools.get("pymupdf"):
            with zipfile.ZipFile(books / AF / "Иван Тестов - Тестовый PDF.epub") as z:
                text = " ".join(z.read(n).decode("utf-8", "replace")
                                for n in z.namelist() if n.endswith(".xhtml"))
                imgs = [n for n in z.namelist()
                        if n.lower().endswith((".png", ".jpg", ".jpeg"))]
            assert "Текст для проверки перенабора" in text, "текст PDF должен попасть в EPUB"
            assert imgs, "картинка из PDF должна встроиться в EPUB"
            assert (books / "scan.xtc").read_bytes()[:4] == b"XTC\x00"
            assert (books / AF / "Иван Тестов - Две колонки.xtc").read_bytes()[:4] == b"XTC\x00", \
                "двухколоночный PDF должен автоматически уйти в XTC"
            print(f"PDF: текст -> EPUB ({len(imgs)} карт. встроено), "
                  "скан и колонки -> XTC автоматически — ОК")
        if tools.get("ddjvu") and (fixtures / "scanbook.djvu").exists():
            assert (books / "scanbook.xtc").read_bytes()[:4] == b"XTC\x00", \
                "DjVu должен уйти в XTC страницами"
            print("DjVu -> XTC страницами — ОК")

        # 3) повторный прогон — всё пропущено
        logs2 = []
        sent2, skipped2, errors2 = bs.run_batch(
            files, host, "/Books", False, tools, logs2.append)
        assert sent2 == 0 and errors2 == 0, (sent2, errors2)
        print(f"повтор: отправлено {sent2}, пропущено {skipped2} — ОК")

        # 4) режим перезаписи
        logs3 = []
        sent3, _, errors3 = bs.run_batch(
            files, host, "/Books", True, tools, logs3.append)
        assert errors3 == 0 and sent3 >= len(expected) - 1, (sent3, errors3)
        print(f"перезапись: отправлено {sent3} — ОК")

        # 4b) рекурсивный пропуск + перезапись из другой папки:
        # книга уже лежит во вложенной папке (как после плагина organize-by-author)
        nested = books / "Чужая папка"
        nested.mkdir()
        (books / "note.epub").rename(nested / "note.epub")
        logs4b = []
        s4b, k4b, e4b = bs.run_batch([fixtures / "note.txt"], host, "/Books",
                                     False, tools, logs4b.append)
        assert s4b == 0 and e4b == 0 and k4b >= 1, (s4b, k4b, e4b)
        assert (nested / "note.epub").is_file(), "существующий файл не должен трогаться"
        logs4c = []
        s4c, _, e4c = bs.run_batch([fixtures / "note.txt"], host, "/Books",
                                   True, tools, logs4c.append)
        assert s4c == 1 and e4c == 0, (s4c, e4c)
        assert not (nested / "note.epub").exists(), "старая копия должна быть удалена"
        assert (books / "note.epub").is_file()
        print("рекурсивный пропуск и перезапись из вложенной папки — ОК")

        # 5) вложенная папка создаётся сама
        logs4 = []
        one = [fixtures / "note.txt"]
        _, _, e4 = bs.run_batch(one, host, "/Books/Подпапка", False, tools, logs4.append)
        assert e4 == 0 and (storage / "Books" / "Подпапка" / "note.epub").is_file()
        print("вложенная папка /Books/Подпапка — ОК")

        # 5b) регресс: прошивка 2.0.0 отдаёт 200 [] для несуществующих папок —
        # ensure_dir обязан создать /Books, а не поверить пустому листингу
        storage2 = tmp / "sd_v2"
        storage2.mkdir()
        srv_v2, host_v2 = make_mock(storage2, v2=True)
        logs_v2 = []
        _, _, e_v2 = bs.run_batch([fixtures / "note.txt"], host_v2, "/Books",
                                  False, tools, logs_v2.append)
        assert e_v2 == 0, logs_v2
        assert (storage2 / "Books" / "note.epub").is_file(), \
            "папка /Books должна быть создана на v2-прошивке"
        srv_v2.shutdown()
        print("v2-прошивка (200 [] на несуществующую папку) — ОК")

        # 6) конвертация сразу, устройство появляется позже
        port = srv.server_address[1]
        srv.shutdown()
        srv.server_close()
        one = [fixtures / "cp1251.fb2"]
        logs5, result = [], {}

        def batch():
            try:
                result["r"] = bs.run_batch(
                    one, host, "/Late", False, tools, logs5.append, wait_timeout=60)
            except Exception as e:
                result["e"] = e

        th = threading.Thread(target=batch, daemon=True)
        th.start()
        time.sleep(2)
        assert th.is_alive(), "батч должен ждать устройство, а не завершиться"
        assert any("Подготовка завершена" in l for l in logs5), \
            "конвертация должна пройти до появления устройства"
        srv2, host2 = make_mock(storage, port)
        assert host2 == host
        th.join(timeout=40)
        assert not th.is_alive(), "батч не дождался устройства"
        assert "r" in result, f"батч упал: {result.get('e')}"
        s5, _, e5 = result["r"]
        assert s5 == 1 and e5 == 0, result["r"]
        assert (storage / "Late").is_dir() and len(list((storage / "Late").rglob("*.epub"))) == 1
        i_conv = next(i for i, l in enumerate(logs5) if "Подготовка завершена" in l)
        i_dev = next(i for i, l in enumerate(logs5) if l.startswith("Устройство:"))
        assert i_conv < i_dev, "конвертация должна быть раньше подключения"
        print("ожидание устройства: конвертация сразу, отправка после появления — ОК")

        # 7) таймаут ожидания: конвертация всё равно выполняется
        logs6 = []
        try:
            bs.run_batch(one, "127.0.0.1:1", "/Never", False, tools,
                         logs6.append, wait_timeout=2)
            raise AssertionError("ожидался DeviceError по таймауту")
        except bs.DeviceError:
            pass
        assert any("Подготовка завершена" in l for l in logs6)
        print("таймаут ожидания после конвертации — ОК")

        # 7b) XTC: выбор формата, папка автора сохраняется, фолбэк на EPUB
        stub = tmp / "xtc_stub.py"
        stub.write_text(
            "import sys\n"
            "a = sys.argv[1:]\n"
            "out = a[a.index('-o') + 1]\n"
            "fmt = a[a.index('-f') + 1]\n"
            "open(out, 'wb').write((b'XTC\\x00' if fmt == 'xtc' else b'XTCH') + b'fake')\n",
            "utf-8")
        tools_x = dict(tools)
        tools_x["node"] = sys.executable
        tools_x["xtc"] = str(stub)
        logs_x = []
        sx, _, ex = bs.run_batch([fixtures / "cp1251.fb2"], host, "/XtcBooks",
                                 False, tools_x, logs_x.append, formats=["xtc"])
        assert ex == 0 and sx == 1, (sx, ex, logs_x)
        assert (storage / "XtcBooks" / "Иван Тестов"
                / "Тестов Иван - Кодировка.win.xtc").is_file(), "XTC должен лечь в папку автора"
        stub_fail = tmp / "xtc_stub_fail.py"
        stub_fail.write_text("import sys; sys.exit(1)\n", "utf-8")
        tools_f = dict(tools_x)
        tools_f["xtc"] = str(stub_fail)
        logs_f = []
        sf, _, ef = bs.run_batch([fixtures / "note.txt"], host, "/XtcBooks",
                                 False, tools_f, logs_f.append, formats=["xtc"])
        assert ef == 0 and sf == 1, (sf, ef, logs_f)
        assert (storage / "XtcBooks" / "note.epub").is_file(), "фолбэк на EPUB"
        print("XTC: выбор формата, папка автора, фолбэк на EPUB — ОК")

        # 7c) проверка структуры EPUB: битый -> пропуск, валидный -> ок,
        #     мелкий огрех (mimetype) -> предупреждение, но отправляется
        ec = tmp / "epubcheck_src"
        ec.mkdir()
        (ec / "broken.epub").write_bytes(b"this is not a zip file")
        v = bs.validate_epub(ec / "broken.epub")
        assert v["fatal"] and v["problems"], v
        logs_b = []
        sb, _, eb = bs.run_batch([ec / "broken.epub"], host, "/EpubCheck",
                                 False, tools, logs_b.append)
        assert eb == 1 and sb == 0, (sb, eb, logs_b)
        assert not list((storage / "EpubCheck").rglob("*.epub")), "битый не должен уйти"
        assert not bs.validate_epub(fixtures / "ready.epub")["problems"]
        wrong = ec / "wrongmime.epub"
        with zipfile.ZipFile(fixtures / "ready.epub") as zin:
            with zipfile.ZipFile(wrong, "w") as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == "mimetype":
                        data = b"text/plain"
                    zout.writestr(item, data)
        vw = bs.validate_epub(wrong)
        assert vw["problems"] and not vw["fatal"], vw
        logs_w = []
        sw, _, ew = bs.run_batch([wrong], host, "/EpubCheck", False, tools, logs_w.append)
        assert ew == 0 and sw == 1, (sw, ew, logs_w)
        uploaded = storage / "EpubCheck" / "wrongmime.epub"
        with zipfile.ZipFile(uploaded) as z:
            assert z.read("mimetype").decode().strip() == "application/epub+zip", \
                "mimetype должен быть починен при обработке"
        print("проверка EPUB: битый пропущен, mimetype починен — ОК")

        # 8) CLI end-to-end
        r = subprocess.run(
            [sys.executable, str(Path(bs.__file__)), "--cli",
             "--folder", str(fixtures), "--host", host, "--dest", "/CLI"],
            capture_output=True, text=True, timeout=600,
        )
        assert r.returncode == 0, f"CLI exit {r.returncode}\n{r.stdout}\n{r.stderr}"
        assert (storage / "CLI").is_dir() and len(list((storage / "CLI").rglob("*.epub"))) >= 8
        print("CLI — ОК")

        srv2.shutdown()
        print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ ✔")
    except AssertionError as e:
        ok = False
        print(f"\nПРОВАЛ: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
