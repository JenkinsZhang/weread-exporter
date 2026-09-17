# weread-exporter — 微信读书导出工具

[中文](#中文说明) | [English](#english)

基于 [drunkdream/weread-exporter](https://github.com/drunkdream/weread-exporter) 二次开发。原项目依赖的 pyppeteer 与新版 Chrome 已不兼容，本项目将浏览器层迁移到 [Playwright](https://playwright.dev/python/)，并加入了扫码登录、持久化登录态和随机化的抓取节奏。

Based on [drunkdream/weread-exporter](https://github.com/drunkdream/weread-exporter). The upstream project relies on pyppeteer, which no longer works with current Chrome. This fork moves the browser layer to [Playwright](https://playwright.dev/python/) and adds QR-code login, persistent login state and randomized crawling pace.

---

## 中文说明

### 实现原理

微信读书网页版的正文不是普通 DOM 文本，而且同一章里有两种渲染方式：

- 章节开头若干屏画在 Canvas 上，逐字调用 `fillText`；
- 其余正文是逐字绝对定位的 `<span>`，DOM 顺序被打乱，只有 CSS `transform` 上的坐标可用，并且随视口虚拟化，滚出视野就会被移除。

本工具用 Playwright 驱动本机 Chrome 打开阅读页，注入脚本同时采集这两类内容：接管 `fillText` 记录画布文字，一边逐屏滚动一边采集页面上的定位 `span`。两者的坐标都归一到同一个内容容器坐标系，再按 (y, x) 排序还原阅读顺序，图片、代码块、分隔线按同一套坐标插回正文之间，最后生成 Markdown 并转换成 epub / pdf / txt；mobi 格式由 kindlegen 从 epub 转换而来。

采完一章后还会校验正文的纵向覆盖：如果某段区间既没有文字也没有图片可以解释这片空白，就说明有内容没采到，工具会回滚到那个位置补采，必要时重新加载整章重采。

### 与原项目的差异

- 浏览器驱动从 pyppeteer 换成 Playwright，优先使用本机已安装的 Google Chrome。
- 长章节不再缺内容：原项目只采集 Canvas 上的文字，而微信读书现在把长章节的大部分正文改成了定位 `span`，导致长章节只能导出开头。实测一章 9832 字的内容，原方案只拿到约 2400 字。
- 图片不再重复：原项目会把同一张图既按位置插入又在章末追加一遍。
- 每章导出后按坐标覆盖校验完整性，发现空洞会自动补采并在日志里给出结论，导出结束时汇总仍不完整的章节。
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

默认节奏下每章约等待 30 秒，一本 60 章的书大约需要 40 到 50 分钟。缩短 `--load-interval` 可以加快速度，但请求过于频繁可能触发风控，请自行权衡。等待之外，每章还要逐屏滚完才能采全正文，短章节 1 到 2 秒，最长的万字章节约 12 秒，日志里会打印实际耗时。

已导出的章节缓存在 `cache/<book_id>/chapters/`，重新运行时会跳过。想整本重新导出，需要先清空这个目录，并把 `output/` 下同名的 epub / pdf 移走，否则会被当成已完成而跳过。`cache/<book_id>/images/` 可以保留，图片按 URL 哈希命名，能直接复用。

### 测试

两个脚本需要先完成一次扫码登录，并且缓存里已有对应书的 `meta.json`：

```bash
# 正文提取：覆盖空洞、字数比值、图片去重、重复采集一致性，
# 并用横向双栏模式的绘制顺序作独立参照双向比对阅读顺序与完整性
python tests/check_extract.py [book_id]

# 兼容性：账号处于横向双栏模式时的入口、游客无头模式、页面无 JS 异常
python tests/check_compat.py [book_id]
```

两个脚本都会打印逐项 PASS / FAIL 与汇总，进程退出码非零表示有失败项。

### 隐私与缓存

`cache/` 目录保存浏览器 profile、登录态备份（`storage_state.json`）和抓取的书籍内容，`output/` 保存导出的文件。这些目录已在 `.gitignore` 中忽略，**请勿分享 `cache/chrome-profile` 或 `storage_state.json`**，它们等同于你的微信读书登录凭据。

### 致谢

本项目基于 [drunkdream/weread-exporter](https://github.com/drunkdream/weread-exporter)，Canvas 钩子与 Markdown 还原、epub / pdf 生成等核心逻辑均来自原项目。

### 免责声明

本工具仅作技术研究之用，请勿用于商业或违法用途。使用本工具导出的内容请仅供个人阅读，由此产生的侵权或其他问题，本工具不承担任何责任。

---

## English

### How it works

WeRead's web reader does not render book text as ordinary DOM text, and a single chapter uses two different rendering paths:

- the first few screens are painted onto a Canvas, one `fillText` call per character;
- the rest of the text consists of absolutely positioned per-character `<span>` elements whose DOM order is shuffled, whose only usable coordinates live in the CSS `transform`, and which are virtualized away once they scroll out of view.

This tool drives your local Chrome with Playwright and captures both: it wraps `fillText` to record canvas text, and sweeps the chapter screen by screen to harvest the positioned spans. Both sources are normalized into the same content-container coordinate space, sorted by (y, x) to recover the reading order, with images, code blocks and rules re-inserted by the same coordinates. The result is Markdown, converted to epub / pdf / txt; mobi is produced from epub with kindlegen.

After each chapter the vertical coverage is verified: any stretch explained by neither text nor an image means content was missed, so the tool scrolls back to repair it and, if needed, reloads the whole chapter.

### What's different from upstream

- Browser automation moved from pyppeteer to Playwright, using the locally installed Google Chrome by default.
- Long chapters are no longer truncated. Upstream only captures canvas text, but WeRead now renders most of a long chapter as positioned spans, so only the beginning was exported. On a 9832-word chapter the old approach captured about 2400 characters.
- Images are no longer duplicated. Upstream inserted each image by position and appended it again at the end of the chapter.
- Every chapter is checked for coverage gaps, repaired automatically, and any chapter that still looks incomplete is listed when the export finishes.
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

With the default pace each chapter waits about 30 seconds, so a 60-chapter book takes roughly 40 to 50 minutes. Lowering `--load-interval` speeds things up, but very frequent requests may trigger WeRead's rate limiting; choose your own trade-off. On top of the waiting, each chapter is swept screen by screen to capture all of its text: 1 to 2 seconds for short chapters and about 12 seconds for the longest ten-thousand-character one. The log prints the actual time.

Exported chapters are cached under `cache/<book_id>/chapters/` and skipped on later runs. To re-export a whole book, empty that directory and move the matching epub / pdf out of `output/`, otherwise both are treated as already done. `cache/<book_id>/images/` can stay, since images are named by URL hash and reused.

### Tests

Both scripts need a completed QR login and an existing `meta.json` for the book in the cache:

```bash
# Content extraction: coverage gaps, character-count ratio, image de-duplication,
# repeated-run consistency, plus a two-way comparison against the reading order
# drawn by the horizontal two-column mode as an independent reference
python tests/check_extract.py [book_id]

# Compatibility: entering with the account in horizontal mode, guest headless mode,
# and no page-side JS errors
python tests/check_compat.py [book_id]
```

Each script prints per-item PASS / FAIL plus a summary and exits non-zero on failure.

### Privacy and cache

`cache/` holds the browser profile, a login-state backup (`storage_state.json`) and the crawled book content; `output/` holds the exported files. Both are git-ignored. **Never share `cache/chrome-profile` or `storage_state.json`** — they are equivalent to your WeRead login credentials.

### Credits

This project is based on [drunkdream/weread-exporter](https://github.com/drunkdream/weread-exporter). The Canvas hook, Markdown reconstruction and epub / pdf generation all originate from the upstream project.

### Disclaimer

This tool is for technical research only. Do not use it for commercial or illegal purposes. Exported content is for personal reading only; the authors accept no liability for copyright infringement or other issues arising from its use.
