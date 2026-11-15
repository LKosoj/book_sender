[Русский](README.md) · **English** · [中文](README.zh.md)

# BookSender — books from a folder to Xteink X4 Pro (CrossPoint)

Drop your books (fb2, pdf, docx, epub — anything from the list below) into a
folder, press one button — the app converts everything to EPUB, finds the
reader on the network by itself, and uploads the books into its library.

Runs on **macOS, Windows and Linux**.

## Installation

| Needs to be installed beforehand | Why |
|---|---|
| **Python 3.9+** (with tkinter) | the app itself |
| **pandoc** | converting books to EPUB |

| Installed automatically on first launch | |
|---|---|
| Pillow | image processing |
| PyMuPDF | reading PDF |
| PT Serif | font for XTC rendering |

- **Windows:** double-click `start.bat`
- **macOS / Linux:** double-click `start.command` (or `./start.command`)

On first launch, the launcher creates the environment and installs the
dependencies by itself — nothing needs to be installed manually. Installing
pandoc is offered automatically (through your OS's package manager); if you
decline — [instructions](https://pandoc.org/installing.html).

Optional — the XTC converter (instant book opening, see below):
`python setup.py --xtc` (needs Node.js 18+ and git).

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

The app **converts all books first** and only then connects to the reader.
If the reader is off or not in File Transfer mode, it **waits and retries the
search every 5 seconds** until the reader appears (or until you press "Stop").
If the IP has changed, it will find the reader at the new address. You can
enable File Transfer even after the conversion has started.

The book folder is monitored continuously: **new and changed files appear
in the list automatically** (at the top of the list, most recent first) —
no need to press "Scan folder".

You can also send only part of the list: select the rows you want
(Cmd/Shift+click) and press "Convert & Send" — only those will be processed.
With nothing selected, everything is sent.

Re-running is safe: already-uploaded books are skipped (including those sorted
into author folders). Overwrite mode replaces old copies.

### CLI (for automation)

```bash
python book_sender.py --find                          # find the reader on the network
python book_sender.py --cli --folder ~/books --host 192.168.1.50
python book_sender.py --cli --folder ~/books --replace   # with auto-search, overwrite
python book_sender.py --cli --folder ~/books --wait-timeout 300  # wait up to 5 min
python book_sender.py --cli --folder ~/books --flat   # no author folders
python book_sender.py --cli --folder ~/books --lang en   # message language
python book_sender.py --cli --folder ~/books --ascii-symbols   # arrows -> ASCII
```

Without `--wait-timeout` (or with `0`) the CLI waits for the device to appear
forever; interrupt with Ctrl+C.

## Supported formats

| Input | What happens |
|---|---|
| `.fb2` | → EPUB; images and the cover are preserved, file name = "Author - Title" from the metadata |
| `.fb2.zip`, `.fbz`, `.zip` | unpacking and conversion of each file inside |
| `.7z`, `.rar` | the same, via the external `7z` |
| `.epub` | optimized for the screen (see below) |
| `.xtc`, `.xtch` | the reader's native format — opens instantly, without indexing |
| `.docx`, `.odt`, `.rtf`, `.html`, `.md`, `.txt` | → EPUB (images from the document are carried over) |
| `.pdf` | **automatic:** with text → EPUB with images; scans and complex layout (columns, tables, slides) → XTC page by page |
| `.mobi`, `.azw3` | via Calibre if installed; otherwise skipped |
| `.djvu` | → XTC page images (ddjvu rendering; needs [DjVuLibre](https://djvu.sourceforge.net): macOS `brew install djvulibre`, Linux `sudo apt install djvulibre-bin`, Windows — DjVuLibre installer) |
| other (`.cbr`, …) | skipped with a message |

## What it does with the books

- **Author folders** (enabled by default; `--flat` to disable): the book goes
  into `/Books/Author/`, just like when sorted by the plugin on the reader.
  Books without an author stay in the root.
- **Skipping what is already uploaded**: `/Books` is checked together with all
  subfolders, so a re-run never duplicates anything. Overwrite mode replaces
  old copies (even ones in a different folder).
- **EPUB optimization** (enabled by default; `--no-optimize` to disable):
  - protective CSS so the book does not overflow the 480×800 screen;
  - removal of embedded fonts (the reader does not use them — saves up to 2 MB);
  - images → **grayscale**, fitted to the **800×480** screen, recompressed;
  - **structure repair**: `mimetype` and `META-INF/container.xml` are restored
    if missing or broken;
  - **structure validation**: broken EPUBs are not sent;
  - optionally — **rare-symbol substitution**: arrows and check marks missing
    from reader fonts (`→` `⇒` `✓`) are replaced with ASCII (`->` `=>` `v`) —
    the "Replace rare symbols" checkbox or `--ascii-symbols`. Alternatively,
    fonts with these symbols: the app **uploads them to the reader automatically**
    on connect and when the interface language changes — **Gentium Book Plus**
    (Russian), **Charis SIL** (English), **Noto Serif SC / 思源宋体** (Chinese).
    Just select the font in Settings > Reader > Font Family.
- FB2 files in cp1251 encoding are automatically re-encoded to UTF-8.

## Instant opening: XTC and XTCH

This is the reader's native format: the pages are already rendered and the
book opens instantly, without indexing. The downside is fixed layout: font and
size are chosen at conversion time and cannot be changed on the device.

| | **XTC** | **XTCH** |
|---|---|---|
| Pixel | 1 bit: black/white | 2 bits: 4 gray levels |
| Halftones | dithering | smoother, better for images |
| Page size | ~47 KB | ~94 KB |

- **XTC** — for text books (the file is half the size).
- **XTCH** — for books with images (smoother gray levels).
- Font size — in pt, like on the reader (12/14/16/18); the field appears when
  XTC/XTCH is selected. The larger the font, the more pages and the bigger the
  file (a page is an uncompressed bitmap of ~48 KB; a novel at 14pt ≈ 130 MB).

To enable: click the "Format" column in the list (EPUB → XTC → XTCH), or
select rows and apply a format from the panel. The converter is required:
`python setup.py --xtc` (or double-click `setup_xtc.command` / `setup_xtc.bat`).
Ready-made `.xtc` files can simply be dropped into the books folder.

## Interface languages

Russian, English, 中文. Detected automatically from the system; you can pick it
with the "Language" combo box (switching is live, the book list is preserved)
or with the CLI flag `--lang ru|en|zh`. The choice is remembered.

## Limitations and tips

- **PDF** is the weak spot: reflow loses the layout, and scans need OCR
  (or use XTC mode — pages as they are). For technical literature the X4 Pro
  is not the best screen anyway.
- The reader's server has no password — use a trusted network only.
- If the search does not find the device: check that File Transfer is on, the
  network is shared (guest Wi-Fi isolates clients), and enter the IP from the
  screen manually.
- Settings (folder, address, mode, language) are saved between runs.

## License

MIT — see [LICENSE](LICENSE).
