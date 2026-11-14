[Русский](README.md) · [English](README.en.md) · **中文**

# BookSender — 将文件夹中的图书发送到 Xteink X4 Pro（CrossPoint）

基于 Python + Tkinter 的应用：扫描图书文件夹，通过 pandoc 将全部图书转换为
**带图片的 EPUB**，并自动在网络上查找阅读器，然后把图书上传到它的书库
（SD 卡上的 `/Books` 文件夹）。

支持 **macOS、Windows 和 Linux**。核心仅依赖 Python 标准库；Pillow 和
PyMuPDF（图片与 PDF 处理）由安装程序装入本地 `.venv` 环境，不会动系统 Python。

## 从零开始安装

### 1. 需要提前安装的组件

| 组件 | 用途 | macOS | Windows | Linux |
|---|---|---|---|---|
| **Python 3.9+**（带 tkinter） | 应用本身 | python.org 或 `brew install python`（已含 tkinter） | [python.org](https://python.org)——安装时勾选 **tcl/tk and IDLE** | `sudo apt install python3 python3-tk` |
| **pandoc** | fb2/docx/odt/rtf/html/md/txt → EPUB | `brew install pandoc` | [pandoc.org/installing](https://pandoc.org/installing.html) 或 `choco install pandoc` | `sudo apt install pandoc` |
| Node.js 18+ 和 git | 仅 XTC/XTCH 需要（可选） | `brew install node git` | [nodejs.org](https://nodejs.org) + [git-scm.com](https://git-scm.com) | `sudo apt install nodejs npm git` |
| 7z | `.7z`/`.rar` 压缩包（可选；zip 由 Python 自行处理） | `brew install sevenzip` | [7-zip.org](https://www.7-zip.org) | `sudo apt install p7zip-full` |
| Calibre | 仅 mobi/azw3（可选；PDF 不需要它） | `brew install --cask calibre` | [calibre-ebook.com](https://calibre-ebook.com) | `sudo apt install calibre` |

### 2. 安装应用

```bash
git clone <你的仓库URL> book-sender
cd book-sender
python setup.py            # 创建 .venv，安装 Pillow 和 PyMuPDF
python setup.py --xtc      # 可选：XTC 转换器（需要 Node.js 和 git）
```

`setup.py` 会自动为环境选择带 tkinter 的 Python，下载用于 XTC 渲染的
免费字体 PT Serif（OFL，含西里尔字母），并在 `--xtc` 时把
[epub-to-xtc-converter](https://github.com/bigbag/epub-to-xtc-converter)
克隆到 `vendor/` 并安装其依赖。全部都在项目文件夹内本地完成。

### 3. 启动

- **Windows：** 双击 `start.bat`
- **macOS / Linux：** 双击 `start.command`（或运行 `./start.command`）
- 从终端：`python book_sender.py`（或 `.venv/bin/python book_sender.py`，
  Windows 上为 `.venv\Scripts\python.exe book_sender.py`）

启动器会自动使用 `.venv`（如存在）。

## 准备阅读器（唯一的手动步骤）

CrossPoint 的 Web 服务器**仅在文件传输模式下工作**：

1. 在阅读器上：**Home → File Transfer → Join Network**（阅读器与电脑处于
   同一 2.4 GHz Wi-Fi 网络）。另一种方式：**Create Hotspot**——此时把电脑
   连接到 `CrossPoint-Reader` 网络，地址 `192.168.4.1`。
2. 屏幕上会显示 IP/QR——可以在应用中手动输入该地址，但通常自动搜索
   就能找到设备。
3. 上传完成后请关闭 File Transfer。

## 使用

选择图书文件夹 → **扫描文件夹** → **查找**
（或将地址留空——会自动搜索）→ **转换并发送**。

工作流程：应用**先转换所有图书**，之后才连接阅读器。如果阅读器已关机
或不在 File Transfer 模式——它会**等待并每 5 秒重试搜索**，直到阅读器出现
（或你按下「停止」）。如果阅读器的 IP 因 DHCP 而变化，应用会按新地址
找到它。也可以在转换开始之后，再在阅读器上开启 File Transfer。

### CLI（无 GUI，用于自动化）

```bash
python book_sender.py --find                          # 在网络中查找阅读器
python book_sender.py --cli --folder ~/books --host 192.168.1.50
python book_sender.py --cli --folder ~/books --replace   # 自动搜索 + 覆盖
python book_sender.py --cli --folder ~/books --wait-timeout 300  # 最多等待 5 分钟
python book_sender.py --cli --folder ~/books --flat   # 不按作者分文件夹
python book_sender.py --cli --folder ~/books --lang en   # 消息语言
```

不带 `--wait-timeout`（或设为 `0`）时，CLI 会无限等待设备出现；
中断方式为 Ctrl+C。

## 支持的格式

| 输入 | 处理方式 |
|---|---|
| `.fb2` | pandoc → EPUB；保留图片和封面，文件名取自元数据中的「作者 - 书名」 |
| `.fb2.zip`、`.fbz`、`.zip` | 解压（`.fbz` 是 Flibusta 提供 fb2 zip 包的方式；包括 cp866 编码的文件名），并转换其中每个文件 |
| `.7z`、`.rar` | 同上，通过外部 `7z` 处理 |
| `.epub` | 原样复制（损坏的 EPUB 可用阅读器内置的 EPUB Optimizer 修复） |
| `.xtc`、`.xtch` | 阅读器原生格式（预渲染页面）——原样复制，无需索引即可打开 |
| `.docx`、`.odt`、`.rtf`、`.html`、`.md`、`.txt` | pandoc → EPUB（HTML 和 md 中的相对路径图片会被读取；md 中也支持 base64） |
| `.pdf` | **全自动处理。** 有文本层的 PDF → 带图片的 EPUB（文本与图像由 PyMuPDF 提取，并按屏幕重新排版，无需 Calibre）。复杂版式（2 栏以上文本、表格/示意图——按矢量图形数量判断、横向幻灯片页）以及无文本层的 PDF（扫描版）→ 逐页渲染为 XTC，带白边裁剪与抖动处理。在「格式」列手动选择 XTC/XTCH 会强制启用逐页渲染 |
| `.mobi`、`.azw3` | 由 Calibre `ebook-convert` 转换（如已安装）；否则跳过并提示 |
| 其他（`.djvu`、`.cbr` 等） | 跳过并提示 |

重复运行是安全的：应用会递归检查 `/Books` **及其所有子文件夹**，
并跳过已上传的文件（包括已被 organize-by-author 插件按作者归类的文件）。
「覆盖」模式以原子方式执行覆盖；若旧副本位于其他文件夹，则会将其删除。

**按作者归类**（默认开启；GUI 中为勾选框，CLI 中用 `--flat` 关闭）：
图书直接上传到作者文件夹，例如
`/Books/作者姓名/作者姓名 - 书名.epub`。作者从最终 EPUB 的 OPF
中读取，方式与 [organize-by-author](https://github.com/itsthisjustin/sd-plugins)
插件完全一致（file-as → 第一个 dc:creator 的文本、相同的名称清理规则、
60 字符限制），因此生成的文件夹与插件创建的完全相同。无作者的图书
留在根目录。

**cp1251** 编码的 FB2 在转换前会自动重编码为 UTF-8
（否则 pandoc 会把文本变成乱码）。

**发送前优化 EPUB**（默认开启；GUI 中为勾选框，CLI 中为 `--no-optimize`）。
CrossPoint 内置优化器是手动工具（位于设备上和网页 File Manager 中），
上传时**不会**自动运行。BookSender 用纯 Python 提前完成它的基础步骤：

- 向每个 XHTML 注入保护性 CSS（与固件优化器相同：`img{max-width:100%}`、
  换行、固定表格——避免图书内容超出屏幕）；
- 移除内嵌字体：字体文件、manifest 条目、CSS 中的 `@font-face`
  （包括 data: URI）、`encryption.xml` 中的条目——CrossPoint 不会使用它们，
  这相当于每本书携带 100 KB–2 MB 的无用负担；
- 移除 `<img>` 的 `width`/`height` 属性。

图片处理通过 Pillow 完成（由安装程序装入 `.venv`；
没有 Pillow 时图片保持原样）：

- **转灰度并保留全部 256 级**（不是 1-bit 黑白！屏幕不需要颜色，
  但中间调很重要）；
- 缩放适配 **800×480** 屏幕且不放大（<20px 的小装饰不处理）；
- 透明 → 白色背景；
- 重压缩：baseline JPEG 85%（e-ink 不支持 progressive JPEG）、PNG optimize。

同时清除对 e-ink 不友好的 CSS（`float`、`position: fixed/absolute`、
`display: flex/grid`），涵盖文件内样式与内联样式，并删除 HTML 注释——
与 [bigbag/epub-to-xtc-converter](https://github.com/bigbag/epub-to-xtc-converter)
及固件的优化器一致。网页优化器的重量级操作（为翻页切割图片、旋转、
自动裁边）未做移植——它们专属于设备的翻页模式。

## 内部工作原理

- 搜索：先使用 CrossPoint 官方 UDP 搜索（`hello` → 端口 8134），
  再尝试 mDNS `crosspoint.local`，最后扫描 /24 子网并验证 `GET /api/status`。
- 上传：`POST /upload?path=/Books`（multipart），文件夹通过
  `POST /mkdir` 创建，已有文件列表通过 `GET /api/files` 获取。
  上传图书会标记书库索引——下次在设备上打开 Library 时图书就会出现。
- 转换：`pandoc -f fb2 -t epub --epub-cover-image …`；封面从 FB2 的
  `<coverpage>` 提取。文件名按 FAT32 规则清理。

协议文档：[docs/webserver-endpoints.md](https://github.com/crosspoint-reader/crosspoint-reader/blob/develop/docs/webserver-endpoints.md)。

## 阅读器上的索引：能否提前完成？

CrossPoint 会构建两个缓存：

- **书库索引**——文件变更后，打开 Library 时重新构建；
- **图书缓存** `/.crosspoint/epub_<hash>/`：`book.bin`（元数据、spine、TOC）
  和 `section.bin`（换行、单词位置、页面）。

图书缓存**无法在电脑上可靠地提前构建**：它由阅读器渲染器结合自身
字体与设置计算（字体、字号、页边距、行距——都是缓存键的一部分），
且格式版本经常变化（`section.bin` 已是 v74，
[变更历史](https://github.com/crosspoint-reader/crosspoint-reader/blob/develop/docs/file-formats.md)
中有数十次重构）。任何设置变更或固件更新都会使缓存失效。
此外，**每次上传/移动/重命名**图书都会清除缓存——批量重新上传后，
阅读器会在首次打开时对图书建立索引，这是正常现象，且只发生一次。

如果希望图书**无需索引、立即打开**，请提前将其转换为 **XTC/XTCH**：
阅读器原生格式，页面已预渲染，带页面索引和目录。工具：
[bigbag/epub-to-xtc-converter](https://github.com/bigbag/epub-to-xtc-converter)
（[网页版](https://liashkov.site/epub-to-xtc-converter/)，Node CLI）。
代价：版式固定——字体和字号在转换时「固化」，在设备上无法更改。
把现成的 `.xtc`/`.xtch` 放入图书文件夹即可——BookSender 会原样发送。

也可以反过来：在 BookSender 中为个别图书选择 **XTC/XTCH** 格式
（点击列表中的「格式」列：EPUB → XTC (黑白) → XTCH (灰阶页面)，
或选中若干行后从面板应用格式）——应用会自动把优化后的 EPUB 转换为 XTC。
需要转换器：运行 `python setup.py --xtc`
（或双击 `setup_xtc.command` / `setup_xtc.bat`）。

XTC 字号的选择方式与固件一致——**以 pt 为单位（12/14/16/18）**，
通过「XTC 字号（pt）」下拉框选择，该选项仅在选择了 XTC/XTCH 时出现
（CLI：`--xtc-font-pt`，默认 14）。内部会把 pt 换算为屏幕像素（14pt ≈ 42px）。
注意：字号越大，页数和文件体积越大（每页是约 48 KB 的未压缩位图；
以中等篇幅小说实测：14pt → 约 130 MB，EPUB → 约 400 KB）。BookSender
内置了针对转换器 bug 的本地补丁（上游在加载图书**之后**才应用字体设置，
导致字号被忽略、分页错乱）；补丁会自动应用。

## 界面语言

Русский、English、中文。语言按系统设置自动检测（LC_ALL/LANG，
macOS 上为 AppleLanguages）；也可以在应用窗口中通过「语言」下拉框
显式选择（实时切换，文件列表保持不变），或使用 CLI 参数
`--lang ru|en|zh`（`--lang auto` 表示跟随系统）。选择会保存在设置中。

这是真正的多语言支持，而不是硬编码的逐一遍历：翻译存放在
`locales/<代码>.json`（扁平的 键 → 字符串 字典，`_name` 为语言自称）。
**添加新语言 = 新增一个 JSON 文件**——它会自动出现在语言选择器中，
无需修改代码。缺失的键先回退到英语，再回退到键本身。
占位符形如 `{name}`、`{ip}` 等。

## 局限与建议

- **PDF** 是薄弱环节：转换为 EPUB 会丢失版式，扫描版需要 OCR。
  对技术书籍而言，X4 Pro 本就不是理想的屏幕。
- 阅读器服务器无密码——请只在可信网络中使用。
- 如果搜索不到设备：请确认 File Transfer 已开启、网络互通
  （访客 Wi-Fi 会隔离客户端），并手动输入屏幕上显示的 IP。
- 设置（文件夹、地址、模式、语言）保存在 `book_sender_settings.json` 中。

## 自测（无需设备）

```bash
python tests/selftest.py
```

测试会生成示例 FB2/ZIP/7Z/PDF，启动本地 mock CrossPoint 服务器，
并跑完整个流程：EPUB 内图片、cp1251/cp866、PDF 路由、XTC 选择、
多语言支持，以及上传是否成功。
