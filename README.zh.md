[Русский](README.md) · [English](README.en.md) · **中文**

# BookSender — 将文件夹中的图书发送到 Xteink X4 Pro（CrossPoint）

把图书（fb2、pdf、docx、epub——下面列表中的任何格式）放进一个文件夹，
按一个按钮——应用就会把全部图书转换为 EPUB，自动在网络上找到阅读器，
并把图书上传到它的书库。

支持 **macOS、Windows 和 Linux**。

## 安装

| 需要提前安装 | 用途 |
|---|---|
| **Python 3.9+**（带 tkinter） | 应用本身 |
| **pandoc** | 将图书转换为 EPUB |

| 首次启动时自动安装 | |
|---|---|
| Pillow | 图片处理 |
| PyMuPDF | 读取 PDF |
| PT Serif | XTC 渲染用字体 |

- **Windows：** 双击 `start.bat`
- **macOS / Linux：** 双击 `start.command`（或运行 `./start.command`）

首次启动时，启动器会自动创建环境并安装依赖——无需手动安装任何东西。
pandoc 会提示自动安装（通过你操作系统的包管理器）；若拒绝，请参阅
[ 安装说明](https://pandoc.org/installing.html)。

可选：XTC 转换器（图书秒开，见下文）：`python setup.py --xtc`
（需要 Node.js 18+ 和 git）。

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

应用会**先转换所有图书**，之后才连接阅读器。如果阅读器已关机或不在
File Transfer 模式——它会**等待并每 5 秒重试搜索**，直到阅读器出现
（或你按下「停止」）。IP 变了也没关系——会按新地址找到它。也可以在
转换开始之后再开启 File Transfer。

书籍文件夹会被持续监控：**新增和更改的文件会自动出现在列表中**
（置顶显示，最新在前）——无需手动点击「扫描文件夹」。

也可以只发送列表中的部分文件：按住 Cmd/Shift 点击选择需要的行，
然后按「Convert & Send」——只会处理选中的行；未选择时发送全部。

重复运行是安全的：已上传的图书会被跳过（包括已按作者归类的）。
「覆盖」模式会替换旧副本。

### CLI（用于自动化）

```bash
python book_sender.py --find                          # 在网络中查找阅读器
python book_sender.py --cli --folder ~/books --host 192.168.1.50
python book_sender.py --cli --folder ~/books --replace   # 自动搜索 + 覆盖
python book_sender.py --cli --folder ~/books --wait-timeout 300  # 最多等待 5 分钟
python book_sender.py --cli --folder ~/books --flat   # 不按作者分文件夹
python book_sender.py --cli --folder ~/books --lang en   # 消息语言
python book_sender.py --cli --folder ~/books --ascii-symbols   # 箭头 -> ASCII
```

不带 `--wait-timeout`（或设为 `0`）时，CLI 会无限等待设备出现；
中断方式为 Ctrl+C。

## 支持的格式

| 输入 | 处理方式 |
|---|---|
| `.fb2` | → EPUB；保留图片和封面，文件名取自元数据中的「作者 - 书名」 |
| `.fb2.zip`、`.fbz`、`.zip` | 解压并转换其中每个文件 |
| `.7z`、`.rar` | 同上，通过外部 `7z` 处理 |
| `.epub` | 针对屏幕优化（见下文） |
| `.xtc`、`.xtch` | 阅读器原生格式——秒开，无需索引 |
| `.docx`、`.odt`、`.rtf`、`.html`、`.md`、`.txt` | → EPUB（文档中的图片会一并保留） |
| `.pdf` | **自动处理：** 有文本 → 带图片的 EPUB；扫描版和复杂版式（分栏、表格、幻灯片）→ 按页渲染为 XTC |
| `.mobi`、`.azw3` | 通过 Calibre 转换（如已安装）；否则跳过 |
| `.djvu` | → XTC 页面图片（ddjvu 渲染；需要 [DjVuLibre](https://djvu.sourceforge.net)：macOS `brew install djvulibre`，Linux `sudo apt install djvulibre-bin`，Windows — DjVuLibre 安装程序） |
| 其他（`.cbr` 等） | 跳过并提示 |

## 对图书做的处理

- **按作者文件夹归类**（默认开启；`--flat` 关闭）：
  图书存入 `/Books/作者/`，与阅读器上插件整理的结果一致。
  无作者的图书留在根目录。
- **跳过已上传内容**：会检查 `/Books` 及其所有子文件夹，
  重复运行不会产生重复文件。「覆盖」模式会替换旧副本
  （即使位于其他文件夹）。
- **优化 EPUB**（默认开启；`--no-optimize` 关闭）：
  - 保护性 CSS，避免图书内容超出 480×800 屏幕；
  - 移除内嵌字体（阅读器不会使用它们——最多节省 2 MB）；
  - 图片 → **灰度**，缩放适配 **800×480** 屏幕，重压缩；
  - **结构修复**：若 `mimetype` 或 `META-INF/container.xml` 缺失或损坏，会自动恢复；
  - **结构校验**：损坏的 EPUB 不会被发送；
  - 可选 — **少见符号替换**：阅读器字体中缺失的箭头和对勾（`→` `⇒` `✓`）
    会替换为 ASCII（`->` `=>` `v`）——勾选「替换少见符号」或使用
    `--ascii-symbols`。另一种方法是使用 **DejaVu Serif** 字体：应用会在连接时
    **自动将其上传到阅读器**（如果尚未安装）；只需在阅读器的字体设置中选择它。
- cp1251 编码的 FB2 会自动重编码为 UTF-8。

## 秒开：XTC 与 XTCH

这是阅读器的原生格式：页面已预先渲染，图书打开无需等待索引。
代价是版式固定：字体和字号在转换时选定，在设备上无法更改。

| | **XTC** | **XTCH** |
|---|---|---|
| 像素 | 1 位：黑/白 | 2 位：4 级灰度 |
| 半色调 | 抖动 | 更平滑，更适合图片 |
| 页面大小 | ~47 KB | ~94 KB |

- **XTC**——适合文字书（文件体积小一半）。
- **XTCH**——适合带图片的书（灰阶更平滑）。
- 字号以 pt 为单位，与阅读器一致（12/14/16/18）；仅在选择了
  XTC/XTCH 时显示该选项。字号越大，页数和文件体积越大
  （每页是约 48 KB 的未压缩位图；14pt 下一部小说 ≈ 130 MB）。

启用方式：点击列表中的「格式」列（EPUB → XTC → XTCH），或选中若干行
后从面板应用格式。需要转换器：运行 `python setup.py --xtc`
（或双击 `setup_xtc.command` / `setup_xtc.bat`）。现成的 `.xtc`
文件直接放入图书文件夹即可。

## 界面语言

Русский、English、中文。按系统自动检测；也可以通过「语言」下拉框选择
（实时切换，图书列表保持不变），或使用 CLI 参数 `--lang ru|en|zh`。
选择会被记住。

## 局限与建议

- **PDF** 是薄弱环节：重排会丢失版式，扫描版需要 OCR
  （或使用 XTC 模式——页面原样保留）。对技术书籍而言，
  X4 Pro 本就不是理想的屏幕。
- 阅读器服务器无密码——请只在可信网络中使用。
- 如果搜索不到设备：请确认 File Transfer 已开启、网络互通
  （访客 Wi-Fi 会隔离客户端），并手动输入屏幕上显示的 IP。
- 设置（文件夹、地址、模式、语言）会在多次运行之间保留。

## 许可证

MIT — 见 [LICENSE](LICENSE)。
