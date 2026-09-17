# weread-exporter — 微信读书导出工具

[中文](#中文说明) | [English](#english)

基于 [drunkdream/weread-exporter](https://github.com/drunkdream/weread-exporter) 二次开发。原项目依赖的 pyppeteer 与新版 Chrome 已不兼容，本项目将浏览器层迁移到 [Playwright](https://playwright.dev/python/)，并加入了扫码登录、持久化登录态和随机化的抓取节奏。

Based on [drunkdream/weread-exporter](https://github.com/drunkdream/weread-exporter). The upstream project relies on pyppeteer, which no longer works with current Chrome. This fork moves the browser layer to [Playwright](https://playwright.dev/python/) and adds QR-code login, persistent login state and randomized crawling pace.

---

## 中文说明

### 实现原理

微信读书网页版的正文不是 DOM 文本，而是绘制在 Canvas 上的。本工具用 Playwright 驱动本机 Chrome 打开阅读页，向页面注入一段脚本劫持 Canvas 2D 上下文，把每一次 `fillText` 调用（连同字号、颜色、坐标）还原成 Markdown，再转换成 epub / pdf / txt 格式；mobi 格式由 kindlegen 从 epub 转换而来。

### 与原项目的差异

- 浏览器驱动从 pyppeteer 换成 Playwright，优先使用本机已安装的 Google Chrome。
- 登录态保存在独立的浏览器 profile 目录（默认 `cache/chrome-profile`），多次运行之间不会丢失，不再需要手工维护 cookie。
- 首次运行自动弹出二维码，扫码后自动继续；导出中途遇到需要登录的章节同样会自动弹出二维码。
- 自动识别并切换阅读模式：登录用户的阅读页可能处于"横向翻页"模式，该模式下无法按章导出，工具会自动切回竖向滚动模式。
- 章节之间的等待时间改为两层随机（章节间随机 + 每隔若干章休息更久一次），避免固定节奏。

### 安装

需要 Python 3.9 及以上，以及本机已安装的 Google Chrome。

```bash
git clone https://github.com/JenkinsZhang/weread-exporter.git
cd weread-exporter

# 方式一：pip
pip install -e .

# 方式二：uv
uv sync
```

如果没有安装 Chrome，可以下载 Playwright 自带的 Chromium：

```bash
playwright install chromium
```

生成 PDF 依赖 cairo / pango：Windows 下所需 DLL 已随包附带；macOS 执行 `brew install cairo pango`；Linux 安装 `libcairo2-dev` 与 `libpango1.0-dev`（或对应发行版的包）。

### 使用

```bash
python -m weread_exporter -b <book_id> -o epub -o pdf
```

获取书籍 ID 的方法：在 <https://weread.qq.com/> 搜索目标书籍，进入书籍介绍页，URL 形如 `https://weread.qq.com/web/bookDetail/08232ac0720befa90825d88`，其中 `08232ac0720befa90825d88` 就是书籍 ID。

`-o` 指定输出格式，可重复，支持 `epub`、`pdf`、`mobi`、`txt`；生成的文件在 `output/` 目录。`epub` 适合手机，`pdf` 适合电脑，`mobi` 适合 Kindle（仅 Linux 支持转换）。

首次运行会弹出微信登录二维码，扫码后自动开始导出；登录态会保存在 profile 目录中，之后运行不再需要扫码。已导出的章节会缓存在 `cache/<book_id>/chapters/`，中断后重新运行会自动跳过。

### 参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `-b, --book-id` | 书籍 ID，也支持带下划线的书单 ID | 必填 |
| `-o, --output-format` | 输出格式，可重复指定 | `epub` |
| `--load-interval` | 章节之间的等待秒数，可以是固定值或 `最小-最大` 随机区间 | `15-45` |
| `--rest-every` | 每导出多少章后休息更久一次，固定值或区间，`0` 关闭 | `10-20` |
| `--rest-interval` | 长休息的秒数，固定值或区间 | `60-120` |
| `--load-timeout` | 加载章节页面的超时秒数 | `60` |
| `--force-login` | 清除已保存的登录态并重新扫码 | 关 |
| `--guest` | 不登录，以游客身份导出（只有试读章节） | 关 |
| `--profile-dir` | 保存登录态的浏览器 profile 目录 | `cache/chrome-profile` |
| `--headless` | 无界面模式；该模式下无法扫码，需先有界面地运行一次完成登录 | 关 |
| `--proxy-server` | HTTP 代理，如 `http://127.0.0.1:8888` | 无 |
| `--css-file` | 覆盖默认样式 | 无 |
| `--debug` | 把浏览器 console 输出保存到 `cache/<book_id>/console.log` | 关 |

默认节奏下每章约等待 30 秒，一本 60 章的书大约需要 40 分钟。缩短 `--load-interval` 可以加快速度，但请求过于频繁可能触发风控，请自行权衡。

### 隐私与缓存

`cache/` 目录保存浏览器 profile、登录态备份（`storage_state.json`）和抓取的书籍内容，`output/` 保存导出的文件。这些目录已在 `.gitignore` 中忽略，**请勿分享 `cache/chrome-profile` 或 `storage_state.json`**，它们等同于你的微信读书登录凭据。

### 致谢

本项目基于 [drunkdream/weread-exporter](https://github.com/drunkdream/weread-exporter)，Canvas 钩子与 Markdown 还原、epub / pdf 生成等核心逻辑均来自原项目。

### 免责声明

本工具仅作技术研究之用，请勿用于商业或违法用途。使用本工具导出的内容请仅供个人阅读，由此产生的侵权或其他问题，本工具不承担任何责任。

---

## English

### How it works

WeRead's web reader does not render book text as DOM nodes; it draws the text onto a Canvas. This tool drives your local Chrome with Playwright, injects a script that proxies the Canvas 2D context, reconstructs Markdown from every `fillText` call (together with font size, color and position), and then converts the Markdown to epub / pdf / txt. The mobi format is produced from epub with kindlegen.

### What's different from upstream

- Browser automation moved from pyppeteer to Playwright, using the locally installed Google Chrome by default.
- Login state lives in a dedicated browser profile (`cache/chrome-profile` by default) and survives across runs; no manual cookie handling.
- The first run opens a WeChat QR code automatically and continues once you have scanned it. Chapters that require login mid-export trigger the QR code as well.
- Reader mode is detected and fixed automatically: logged-in accounts may open the reader in "horizontal paging" mode, which cannot be exported chapter by chapter, so the tool switches it back to vertical scrolling.
- Waiting between chapters is randomized on two levels (random gap between chapters plus a longer rest every few chapters) to avoid a fixed rhythm.

### Installation

Requires Python 3.9+ and Google Chrome installed locally.

```bash
git clone https://github.com/JenkinsZhang/weread-exporter.git
cd weread-exporter

# Option 1: pip
pip install -e .

# Option 2: uv
uv sync
```

Without Chrome, you can download Playwright's bundled Chromium instead:

```bash
playwright install chromium
```

PDF output needs cairo / pango: the required DLLs are bundled for Windows; on macOS run `brew install cairo pango`; on Linux install `libcairo2-dev` and `libpango1.0-dev` (or your distribution's equivalents).

### Usage

```bash
python -m weread_exporter -b <book_id> -o epub -o pdf
```

To find a book ID, search for the book on <https://weread.qq.com/> and open its detail page. The URL looks like `https://weread.qq.com/web/bookDetail/08232ac0720befa90825d88`, and `08232ac0720befa90825d88` is the book ID.

`-o` selects output formats and may be repeated: `epub`, `pdf`, `mobi`, `txt`. Files are written to `output/`. `epub` suits phones, `pdf` suits desktops, `mobi` suits Kindle (conversion is Linux only).

The first run shows a WeChat login QR code; scan it and the export starts automatically. The login state is stored in the profile directory, so later runs need no scanning. Exported chapters are cached under `cache/<book_id>/chapters/`, and an interrupted run resumes where it stopped.

### Options

| Option | Description | Default |
|---|---|---|
| `-b, --book-id` | Book ID; book-list IDs containing an underscore are also accepted | required |
| `-o, --output-format` | Output format, repeatable | `epub` |
| `--load-interval` | Seconds to wait between chapters, a fixed number or a `min-max` random range | `15-45` |
| `--rest-every` | Take a longer rest after every N exported chapters, number or range, `0` to disable | `10-20` |
| `--rest-interval` | Length of the longer rest in seconds, number or range | `60-120` |
| `--load-timeout` | Timeout in seconds for loading a chapter page | `60` |
| `--force-login` | Clear the saved login state and scan the QR code again | off |
| `--guest` | Export without logging in (free preview chapters only) | off |
| `--profile-dir` | Browser profile directory that keeps the login state | `cache/chrome-profile` |
| `--headless` | Headless mode; QR scanning is impossible here, so log in once in a normal run first | off |
| `--proxy-server` | HTTP proxy, e.g. `http://127.0.0.1:8888` | none |
| `--css-file` | Override the default stylesheet | none |
| `--debug` | Save browser console output to `cache/<book_id>/console.log` | off |

With the default pace each chapter waits about 30 seconds, so a 60-chapter book takes roughly 40 minutes. Lowering `--load-interval` speeds things up, but very frequent requests may trigger WeRead's rate limiting; choose your own trade-off.

### Privacy and cache

`cache/` holds the browser profile, a login-state backup (`storage_state.json`) and the crawled book content; `output/` holds the exported files. Both are git-ignored. **Never share `cache/chrome-profile` or `storage_state.json`** — they are equivalent to your WeRead login credentials.

### Credits

This project is based on [drunkdream/weread-exporter](https://github.com/drunkdream/weread-exporter). The Canvas hook, Markdown reconstruction and epub / pdf generation all originate from the upstream project.

### Disclaimer

This tool is for technical research only. Do not use it for commercial or illegal purposes. Exported content is for personal reading only; the authors accept no liability for copyright infringement or other issues arising from its use.
