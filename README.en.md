[Русский](README.md) · **English** · [中文](README.zh.md)

# BookSender — books from a folder to Xteink X4 Pro (CrossPoint)

A Python + Tkinter app: it scans a folder of books, converts everything to
**EPUB with images** via pandoc, finds the reader on the network by itself,
and then uploads the books into its library (the `/Books` folder on the SD card).

Runs on **macOS, Windows and Linux**. The core is the Python standard library
only; Pillow and PyMuPDF (images and PDF) are installed by the setup script
into a local `.venv` environment and never touch the system Python.

## Install from scratch

### 1. What needs to be installed first

| Component | Why | macOS | Windows | Linux |
|---|---|---|---|---|
| **Python 3.9+** with tkinter | the app itself | python.org or `brew install python` (tkinter included) | [python.org](https://python.org) — check **tcl/tk and IDLE** during installation | `sudo apt install python3 python3-tk` |
| **pandoc** | fb2/docx/odt/rtf/html/md/txt → EPUB | `brew install pandoc` | [pandoc.org/installing](https://pandoc.org/installing.html) or `choco install pandoc` | `sudo apt install pandoc` |
| Node.js 18+ and git | only for XTC/XTCH (optional) | `brew install node git` | [nodejs.org](https://nodejs.org) + [git-scm.com](https://git-scm.com) | `sudo apt install nodejs npm git` |
| 7z | `.7z`/`.rar` archives (optional; Python handles zip itself) | `brew install sevenzip` | [7-zip.org](https://www.7-zip.org) | `sudo apt install p7zip-full` |
| Calibre | only mobi/azw3 (optional; PDF doesn't need it) | `brew install --cask calibre` | [calibre-ebook.com](https://calibre-ebook.com) | `sudo apt install calibre` |

### 2. Installing the app

```bash
git clone <your-repo-url> book-sender
cd book-sender
python setup.py            # creates .venv, installs Pillow and PyMuPDF
python setup.py --xtc      # optional: XTC converter (needs Node.js and git)
```

`setup.py` picks a Python with tkinter for the environment on its own,
downloads the free PT Serif font (OFL, Cyrillic) for XTC rendering, and with
`--xtc` clones
[epub-to-xtc-converter](https://github.com/bigbag/epub-to-xtc-converter)
into `vendor/` and installs its dependencies. Everything stays local, inside
the project folder.

### 3. Running

- **Windows:** double-click `start.bat`
- **macOS / Linux:** double-click `start.command` (or `./start.command`)
- From a terminal: `python book_sender.py` (or `.venv/bin/python book_sender.py`,
  on Windows `.venv\Scripts\python.exe book_sender.py`)

On **first run the launchers install everything automatically**: they create
`.venv`, install Pillow and PyMuPDF, download the font, and offer to install
pandoc (via the OS package manager). The only prerequisite is **Python 3.9+**.
Running `setup.py` separately is optional — it runs automatically; you can run it
manually to rebuild the environment or with `--xtc`.

## Preparing the reader (the only manual step)

The CrossPoint web server runs **only in File Transfer mode**:

1. On the reader: **Home → File Transfer → Join Network** (reader and computer
   on the same 2.4 GHz Wi-Fi network). Alternative: **Create Hotspot** — then
   connect the computer to the `CrossPoint-Reader` network, address `192.168.4.1`.
2. The screen will show an IP/QR — you can enter the address in the app manually,
   but the search usually finds the device by itself.
3. After uploading, turn File Transfer off.

## Usage

Pick the books folder → **Scan folder** → **Find**
(or leave the address empty — the search runs automatically) →
**Convert and send**.

How it works: the app **converts all books first** and only then connects to
the reader. If the reader is off or not in File Transfer mode, it **waits and
retries the search every 5 seconds** until the reader appears (or until you
press "Stop"). If the reader's IP changed via DHCP, the app will find it at the
new address. You can enable File Transfer on the reader even after the
conversion has started.

### CLI (no GUI, for automation)

```bash
python book_sender.py --find                          # find the reader on the network
python book_sender.py --cli --folder ~/books --host 192.168.1.50
python book_sender.py --cli --folder ~/books --replace   # with auto-search, overwrite
python book_sender.py --cli --folder ~/books --wait-timeout 300  # wait up to 5 min
python book_sender.py --cli --folder ~/books --flat   # no author folders
python book_sender.py --cli --folder ~/books --lang en   # message language
```

Without `--wait-timeout` (or with `0`) the CLI waits for the device to appear
forever; interrupt with Ctrl+C.

## Supported formats

| Input | What happens |
|---|---|
| `.fb2` | pandoc → EPUB; images and the cover are preserved, file name = "Author - Title" from the metadata |
| `.fb2.zip`, `.fbz`, `.zip` | unpacking (`.fbz` is how Flibusta serves a zip with fb2, including cp866 file names), each file inside is converted |
| `.7z`, `.rar` | the same, via the external `7z` |
| `.epub` | copied as is (broken EPUBs are fixed by the built-in EPUB Optimizer on the reader) |
| `.xtc`, `.xtch` | native reader formats (pre-rendered pages) — copied as is, open without indexing |
| `.docx`, `.odt`, `.rtf`, `.html`, `.md`, `.txt` | pandoc → EPUB (relative images in HTML and md are picked up; base64 works in md too) |
| `.pdf` | **Fully automatic.** PDF with text → EPUB with images (text and images are extracted with PyMuPDF and reflowed for the screen, no Calibre needed). Complex layout (2+ text columns, tables/diagrams — detected by the amount of vector graphics, landscape slides) and PDFs without text (scans) → XTC by rendering pages with white-margin cropping and dithering. Manually picking XTC/XTCH in the "Format" column forces page rendering |
| `.mobi`, `.azw3` | Calibre `ebook-convert` if installed; otherwise skipped with a message |
| other (`.djvu`, `.cbr`, …) | skipped with a message |

Re-running is safe: the app recursively checks `/Books` **together with all
nested folders** and skips already-uploaded files (including those sorted into
author folders by the organize-by-author plugin). Overwrite mode overwrites
atomically; if the old copy sits in a different folder, it is deleted.

**Author folders** (enabled by default; GUI checkbox / `--flat` in the CLI to
disable): a book is uploaded straight into the author's folder, for example
`/Books/Author Name/Author Name - Book Title.epub`. The author is read from the
OPF of the finished EPUB exactly the way the
[organize-by-author](https://github.com/itsthisjustin/sd-plugins) plugin does
it (file-as → text of the first dc:creator, the same name sanitization,
60-character limit), so the folders match the ones created by the plugin.
Books without an author stay in the root.

FB2 files in **cp1251** are automatically re-encoded to UTF-8 before conversion
(otherwise pandoc turns the text into mojibake).

**EPUB optimization before sending** (enabled by default; GUI checkbox,
`--no-optimize` in the CLI). The built-in CrossPoint optimizer is a manual tool
(on the device and in the web File Manager) — it does NOT run on upload.
BookSender performs its basic steps in advance, in pure Python:

- protective CSS into every XHTML (like the firmware optimizer:
  `img{max-width:100%}`, hyphenation, fixed table layout — so the book does not
  overflow the screen);
- removal of embedded fonts: files, manifest entries, `@font-face` in CSS
  (including data: URIs), `encryption.xml` entries — CrossPoint does not use
  them, it is 100 KB–2 MB of dead weight per book;
- stripping the `width`/`height` attributes from `<img>`.

Image processing is done through Pillow (installed by the setup script into
`.venv`; without Pillow, images are left as they are):

- **grayscale keeping all 256 levels** (not 1-bit black-and-white! the screen
  does not need color, but halftones matter);
- fitting into the **800×480** screen without upscaling (small decorations
  <20px are left untouched);
- transparency → white background;
- recompression: baseline JPEG 85% (e-ink does not understand progressive
  JPEG), PNG optimize.

CSS that is harmful to e-ink (`float`, `position: fixed/absolute`,
`display: flex/grid`) is also cleaned out of files and inline styles, and HTML
comments are removed — same as in the optimizers of
[bigbag/epub-to-xtc-converter](https://github.com/bigbag/epub-to-xtc-converter)
and the firmware. The heavy operations of the web optimizer (splitting images
for page turns, rotation, automatic margin cropping) are not ported — they are
specific to the device's page-turn mode.

## How it works inside

- Search: the official CrossPoint UDP discovery (`hello` → port 8134), then
  mDNS `crosspoint.local`, then a /24 subnet scan checking `GET /api/status`.
- Upload: `POST /upload?path=/Books` (multipart); the folder is created via
  `POST /mkdir`; the list of existing files comes from `GET /api/files`.
  Uploading a book marks the library index — the book appears the next time
  Library is opened on the device.
- Conversion: `pandoc -f fb2 -t epub --epub-cover-image …`; the cover is
  extracted from the FB2 `<coverpage>`. File names are sanitized for FAT32.

Protocol: [docs/webserver-endpoints.md](https://github.com/crosspoint-reader/crosspoint-reader/blob/develop/docs/webserver-endpoints.md).

## Indexing on the reader: can it be done in advance?

CrossPoint builds two caches:

- the **library index** — rebuilt when Library is opened after file changes;
- the **book cache** `/.crosspoint/epub_<hash>/`: `book.bin` (metadata, spine,
  TOC) and `section.bin` (line breaks, word positions, pages).

The book cache **cannot be reliably pre-built on a computer**: it is computed
by the reader's renderer with its fonts and settings (font, size, margins,
spacing — all of that goes into the cache key), and the format version changes
often (`section.bin` is already at v74; the
[history](https://github.com/crosspoint-reader/crosspoint-reader/blob/develop/docs/file-formats.md)
has dozens of rebuilds). Any settings change or firmware update resets the
cache. On top of that, the cache is cleared on **every upload/move/rename** of
a book — after a mass re-upload the reader will index the books on first open;
that is normal and happens only once.

If you need books to open **instantly, without indexing**, convert them to
**XTC/XTCH** in advance: the reader's native format with already rendered
pages, a page index, and a table of contents. Tool:
[bigbag/epub-to-xtc-converter](https://github.com/bigbag/epub-to-xtc-converter)
([web version](https://liashkov.site/epub-to-xtc-converter/), Node CLI).
Trade-off: fixed layout — the font and size are baked in at conversion time and
cannot be changed on the device. Put ready `.xtc`/`.xtch` files into the books
folder — BookSender will send them as is.

It also works the other way around: pick the **XTC/XTCH** format for individual
books in BookSender (click the "Format" column in the list: EPUB → XTC (B&W) →
XTCH (grayscale pages), or select rows and apply a format from the panel) —
the app will convert the optimized EPUB to XTC by itself. The converter is
required: `python setup.py --xtc` (or double-click `setup_xtc.command` /
`setup_xtc.bat`).

The XTC font size is chosen the same way as in the firmware — **in pt
(12/14/16/18)** via the "XTC font (pt)" combo box, which appears only when
XTC/XTCH is selected (CLI: `--xtc-font-pt`, default 14). Internally, pt are
converted to screen pixels (14pt ≈ 42px). Note: the larger the font, the more
pages and the bigger the file (each page is an uncompressed bitmap of ~48 KB;
measurements on an average-length novel: 14pt → ~130 MB, EPUB → ~400 KB).
BookSender has a local patch for a converter bug built in (upstream applies
the font settings AFTER loading the book, so the size was ignored and pages
broke); the patch is applied automatically.

## Interface languages

Russian, English, 中文. The language is detected automatically from the system
(LC_ALL/LANG, AppleLanguages on macOS); you can also pick it explicitly with
the "Language" combo box in the app window (switching is live, the file list is
preserved) or with the CLI flag `--lang ru|en|zh` (`--lang auto` — follow the
system). The choice is remembered in the settings.

This is real multilingual support, not a hack: translations live in
`locales/<code>.json` (a flat key → string dictionary; `_name` is the
self-name). **Adding a new language = a new JSON file** — it appears in the
selector on its own, no code changes needed. A missing key falls back to
English, then to the key itself. Substitutions are `{name}`, `{ip}`, etc.

## Limitations and tips

- **PDF** is the weak spot: conversion to EPUB loses the layout, and scans
  need OCR. For technical literature the X4 Pro is not the best screen anyway.
- The reader's server has no password — use a trusted network only.
- If the search does not find the device: check that File Transfer is on, the
  network is shared (guest Wi-Fi isolates clients), and enter the IP from the
  screen manually.
- Settings (folder, address, mode, language) are saved in
  `book_sender_settings.json`.

## Self-test (no device needed)

```bash
python tests/selftest.py
```

The test creates sample FB2/ZIP/7Z/PDF files, starts a local mock CrossPoint
server, and runs the whole pipeline: images inside the EPUB, cp1251/cp866,
PDF routing, XTC selection, multilingual support, and that the upload actually
happened.
