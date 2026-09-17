"""
WebRead WebPage

基于 Playwright 驱动 Chrome 打开微信读书网页版：维护登录态、导航阅读页、
并从注入的 Canvas 钩子中取回章节 markdown。
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import (
    BrowserContext,
    ConsoleMessage,
    Error as PlaywrightError,
    Page,
    Playwright,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from . import utils


HOOK_SCRIPT_PATH: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "hook.js"
)

DETECT_HEADLESS_SCRIPT = """
const webdriver = navigator.webdriver === true;
const chromeObj = typeof window.chrome !== "undefined";
const pluginCount = navigator.plugins.length;
const languageCount = navigator.languages ? navigator.languages.length : 0;
const headlessUA = /HeadlessChrome/.test(navigator.userAgent);
const zeroOuterSize = (window.outerWidth === 0 && window.outerHeight === 0);
webdriver || !chromeObj || pluginCount === 0 || languageCount === 0  || headlessUA || zeroOuterSize;
"""


class HorizontalReaderError(RuntimeError):
    """阅读页处于横向翻页模式，无法按章导出"""


class WeReadWebPage(object):
    """WebRead WebPage"""

    root_url: str = "https://weread.qq.com"
    window_size: Tuple[int, int] = (1920, 1080)
    # 埋点/日志上报接口：直接返回成功，避免真实上报，也减少无关请求
    telemetry_url_patterns: Tuple[str, ...] = (
        "**/hera/**",
        "**/sentry/**",
        "**/river/single**",
    )
    # 书籍详情页与阅读页上可能出现的登录入口
    login_button_selectors: Tuple[str, ...] = (
        "button.navBar_link_Login",
        "div.readerTopBar_right button.actionItem",
        "button.readerFooter_button",
        "button:has-text('登录')",
    )

    def __init__(
        self,
        book_id: str,
        profile_dir: Optional[str] = None,
        webcache_path: Optional[str] = None,
        debug: bool = False,
    ) -> None:
        self._book_id: str = book_id
        self._debug: bool = debug
        self._webcache_path: str = webcache_path or "cache"
        if not os.path.isdir(self._webcache_path):
            os.makedirs(self._webcache_path)
        # 固定的浏览器 profile 目录：登录态（包括服务端轮换的 wr_skey）由浏览器
        # 自己保存和续期，多次运行之间不会丢失
        self._profile_dir: str = os.path.abspath(
            profile_dir or os.path.join(self._webcache_path, "chrome-profile")
        )
        # 登录成功后额外备份一份 cookie，profile 损坏或被删除时可以恢复
        self._storage_state_path: str = os.path.join(
            self._webcache_path, "storage_state.json"
        )
        self._console_log_path: str = os.path.join(
            self._webcache_path, book_id, "console.log"
        )
        self._home_url: str = "%s/web/bookDetail/%s" % (
            self.__class__.root_url,
            book_id,
        )
        self._chapter_root_url: str = self.__class__.root_url + "/web/reader/"
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._console_log: Optional[Any] = None
        self._headless: bool = False
        self._user_name: str = ""
        self._url: str = ""

    @property
    def user_name(self) -> str:
        """当前登录用户名，未登录为空串"""
        return self._user_name

    async def get_book_info(self) -> Dict[str, Any]:
        html = (await utils.fetch(self._home_url)).decode()
        pos1 = html.find("window.__INITIAL_STATE__")
        if pos1 <= 0:
            raise RuntimeError("Unexpected html: %s" % html)
        pos1 = html.find("=", pos1)
        pos2 = html.find("};", pos1)
        data = html[pos1 + 1 : pos2 + 1].strip()
        data = json.loads(data)
        book_info: Dict[str, Any] = {}
        book_info["title"] = data["reader"]["bookInfo"]["title"]
        book_info["author"] = data["reader"]["bookInfo"]["author"]
        book_info["cover"] = data["reader"]["bookInfo"]["cover"]
        book_info["intro"] = data["reader"]["bookInfo"]["intro"]
        book_info["chapters"] = []
        for chapter in data["reader"]["chapterInfos"]:
            chap = {
                "id": chapter["chapterUid"],
                "title": chapter["title"],
                "level": chapter["level"],
                "words": chapter["wordCount"],
                "anchors": [],
            }
            if chapter["anchors"]:
                for it in chapter["anchors"]:
                    chap["anchors"].append({"title": it["title"], "level": it["level"]})
            book_info["chapters"].append(chap)
        return book_info

    async def check_valid(self) -> bool:
        html = await utils.fetch(self._home_url)
        if b'"soldout":1' in html:
            return False
        return True

    async def launch(
        self,
        headless: bool = False,
        force_login: bool = False,
        allow_guest: bool = False,
        proxy_server: Optional[str] = None,
    ) -> None:
        logging.info("[%s] Launch url %s" % (self.__class__.__name__, self._home_url))
        self._headless = headless
        if not os.path.isdir(self._profile_dir):
            os.makedirs(self._profile_dir)
        logging.info(
            "[%s] Use profile dir %s" % (self.__class__.__name__, self._profile_dir)
        )

        launch_kwargs: Dict[str, Any] = {
            "user_data_dir": self._profile_dir,
            "headless": headless,
            "no_viewport": True,
            "args": [
                "--window-size=%d,%d" % self.__class__.window_size,
                "--disable-blink-features=AutomationControlled",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        if proxy_server:
            launch_kwargs["proxy"] = {"server": proxy_server}

        self._playwright = await async_playwright().start()
        try:
            # 优先使用本机安装的 Chrome，无需额外下载浏览器
            self._context = await self._playwright.chromium.launch_persistent_context(
                channel="chrome", **launch_kwargs
            )
        except PlaywrightError as ex:
            logging.warning(
                "[%s] Launch system chrome failed: %s, fallback to bundled chromium"
                % (self.__class__.__name__, str(ex).splitlines()[0])
            )
            try:
                self._context = (
                    await self._playwright.chromium.launch_persistent_context(
                        **launch_kwargs
                    )
                )
            except PlaywrightError:
                await self.close()
                raise utils.ChromeNotInstalledError(
                    "Chrome not found. Please install Google Chrome, "
                    "or run `playwright install chromium` to download a browser."
                )

        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )
        await self._context.add_init_script(script=self._build_hook_script())
        for pattern in self.__class__.telemetry_url_patterns:
            await self._context.route(pattern, self._handle_telemetry)
        await self._add_reader_mode_cookie()

        if self._debug:
            # hook.js 每章会产生上万条 console 输出，只在调试时才收集
            log_dir = os.path.dirname(self._console_log_path)
            if not os.path.isdir(log_dir):
                os.makedirs(log_dir)
            self._console_log = open(self._console_log_path, "a", encoding="utf-8")
            self._page.on("console", self._handle_console)

        await self._restore_storage_state()
        await self._page.goto(self._home_url, wait_until="domcontentloaded")

        detect_headless_result = await self._page.evaluate(DETECT_HEADLESS_SCRIPT)
        if detect_headless_result:
            key = await self._ainput(
                "浏览器检测到Headless模式，继续执行可能导致帐号被封禁，是否继续执行？Y/n\n"
            )
            if key.strip() != "Y":
                raise utils.BreakExportingError()

        await self._ensure_login(force_login, allow_guest)

    async def close(self) -> None:
        if self._console_log:
            self._console_log.close()
            self._console_log = None
        if self._context:
            try:
                await self._context.close()
            except PlaywrightError as ex:
                logging.warning(
                    "[%s] Close browser failed: %s" % (self.__class__.__name__, ex)
                )
        if self._playwright:
            await self._playwright.stop()
        self._context = self._page = self._playwright = None

    async def _ensure_login(self, force_login: bool, allow_guest: bool) -> None:
        user_name = await self.get_login_user()
        if user_name and not force_login:
            self._user_name = user_name
            logging.info(
                "[%s] Current login user is %s" % (self.__class__.__name__, user_name)
            )
            print("当前登录用户：%s" % user_name, flush=True)
            return
        if force_login:
            logging.info(
                "[%s] Force login, clear current login state" % self.__class__.__name__
            )
            await self._clear_login()
        elif allow_guest:
            logging.warning(
                "[%s] Not logged in, export as guest (only preview chapters available)"
                % self.__class__.__name__
            )
            print("当前未登录，将以游客身份导出（只能导出试读章节）", flush=True)
            return
        await self.login()

    async def login(self, timeout: int = 300) -> str:
        """打开登录二维码并等待用户扫码，成功后返回用户名"""
        if self._headless:
            raise utils.LoginRequiredError(
                "未登录：headless 模式下无法扫码，请先不带 --headless 运行一次完成登录"
            )
        if await self._open_login_dialog():
            print("请在浏览器弹出的二维码窗口中用微信扫码登录，登录成功后会自动继续...", flush=True)
        else:
            print("未能自动打开登录窗口，请在浏览器中手动点击「登录」并扫码，登录成功后会自动继续...", flush=True)
        time0 = time.time()
        while time.time() - time0 < timeout:
            await asyncio.sleep(2)
            user_name = await self.get_login_user()
            if not user_name:
                continue
            self._user_name = user_name
            logging.info(
                "[%s] Login success, user is %s" % (self.__class__.__name__, user_name)
            )
            print("登录成功：%s" % user_name, flush=True)
            await self._save_storage_state()
            return user_name
        raise utils.LoginRequiredError("登录超时（%d 秒）" % timeout)

    async def get_login_user(self) -> str:
        """校验浏览器中的登录态，返回用户名，未登录返回空串"""
        cookies = await self._context.cookies(self.__class__.root_url)
        vid = next((it["value"] for it in cookies if it["name"] == "wr_vid"), "")
        if not vid:
            return ""
        url = "%s/web/user?userVid=%s" % (self.__class__.root_url, vid)
        for attempt in range(2):
            try:
                # context.request 与浏览器共享 cookie，不需要手工拼 Cookie 头
                rsp = await self._context.request.get(
                    url, headers={"Referer": self.__class__.root_url}
                )
                rsp_data = await rsp.json()
            except (PlaywrightError, ValueError) as ex:
                logging.warning(
                    "[%s] Get user info failed: %s" % (self.__class__.__name__, ex)
                )
                return ""
            err_code = rsp_data.get("errCode")
            if err_code == -2012 and attempt == 0:
                # 登录态需要续期，页面脚本会用 wr_rt 刷新 wr_skey，刷新页面后重试
                await self._page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(2)
                continue
            if err_code:
                logging.warning(
                    "[%s] Login state invalid: %s" % (self.__class__.__name__, rsp_data)
                )
                return ""
            return rsp_data.get("name") or "Anonymous"
        return ""

    def _login_dialog_opened(self) -> bool:
        # 登录弹窗会把微信扫码页以 iframe 形式嵌入
        return any("open.weixin.qq.com" in frame.url for frame in self._page.frames)

    async def _wait_for_login_dialog(self, timeout: float = 5) -> bool:
        time0 = time.time()
        while time.time() - time0 < timeout:
            if self._login_dialog_opened():
                return True
            await asyncio.sleep(0.5)
        return False

    async def _open_login_dialog(self, retries: int = 3) -> bool:
        """点击登录入口并确认二维码窗口已经出现"""
        for _ in range(retries):
            if self._login_dialog_opened():
                return True
            if not await self._click_login_button():
                await asyncio.sleep(1)
                continue
            if await self._wait_for_login_dialog():
                return True
        return self._login_dialog_opened()

    async def _click_login_button(self) -> bool:
        for selector in self.__class__.login_button_selectors:
            locator = self._page.locator(selector)
            try:
                count = await locator.count()
            except PlaywrightError:
                continue
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not await item.is_visible():
                        continue
                    text = (await item.inner_text()).strip()
                    if "登录" not in text:
                        continue
                    # 弹窗遮罩可能拦住按钮，缩短超时避免长时间卡住
                    await item.click(timeout=5000)
                except PlaywrightError as ex:
                    logging.debug(
                        "[%s] Click %s failed: %s"
                        % (self.__class__.__name__, selector, ex)
                    )
                    continue
                return True
        return False

    async def _clear_login(self) -> None:
        await self._context.clear_cookies()
        await self._add_reader_mode_cookie()
        if os.path.isfile(self._storage_state_path):
            os.remove(self._storage_state_path)
        await self._page.reload(wait_until="domcontentloaded")

    async def _add_reader_mode_cookie(self) -> None:
        # 强制阅读页使用非横向翻页模式，hook.js 的排版还原依赖该模式下的绘制方式
        await self._context.add_cookies(
            [
                {
                    "name": "wr_useHorizonReader",
                    "value": "0",
                    "domain": "weread.qq.com",
                    "path": "/",
                }
            ]
        )

    async def _save_storage_state(self) -> None:
        await self._context.storage_state(path=self._storage_state_path)
        logging.info(
            "[%s] Login state saved to %s"
            % (self.__class__.__name__, self._storage_state_path)
        )

    async def _restore_storage_state(self) -> None:
        """profile 中没有登录态但存在备份时，从备份恢复 cookie"""
        if not os.path.isfile(self._storage_state_path):
            return
        cookies = await self._context.cookies(self.__class__.root_url)
        if any(it["name"] == "wr_vid" for it in cookies):
            return
        try:
            with open(self._storage_state_path, encoding="utf-8") as fp:
                state = json.load(fp)
        except (OSError, ValueError) as ex:
            logging.warning(
                "[%s] Load %s failed: %s"
                % (self.__class__.__name__, self._storage_state_path, ex)
            )
            return
        saved: List[Dict[str, Any]] = [
            it
            for it in state.get("cookies", [])
            if "weread.qq.com" in it.get("domain", "")
        ]
        if not saved:
            return
        await self._context.add_cookies(saved)
        logging.info(
            "[%s] Restore %d cookies from %s"
            % (self.__class__.__name__, len(saved), self._storage_state_path)
        )

    def _build_hook_script(self) -> str:
        with open(HOOK_SCRIPT_PATH, encoding="utf-8") as fp:
            hook_script = fp.read()
        # 只在阅读页启用 canvas 钩子，详情页和登录二维码 iframe 不受影响；
        # __wereadDebug 控制 hook.js 是否输出逐次 canvas 调用的调试日志
        return (
            "window.__wereadDebug = %s;\n"
            "if (location.pathname.startsWith('/web/reader/')) {\n%s\n}\n"
        ) % ("true" if self._debug else "false", hook_script)

    async def _handle_telemetry(self, route: Route) -> None:
        request = route.request
        if request.method == "POST" and request.post_data:
            logging.debug(
                "[%s] %s %s %s"
                % (self.__class__.__name__, request.method, request.url, request.post_data)
            )
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*",
        }
        body = '{"err_code":0,"msg":"suc"}' if "/river/single" in request.url else ""
        try:
            await route.fulfill(status=200, headers=headers, body=body)
        except PlaywrightError:
            # 页面已跳转或关闭，请求不再需要响应
            pass

    def _handle_console(self, message: ConsoleMessage) -> None:
        # hook.js 会输出大量 canvas 调用日志，只落盘不打到终端
        if not self._console_log:
            return
        try:
            self._console_log.write("[%s] %s\n" % (self._url, message.text))
        except (OSError, ValueError):
            pass

    async def _ainput(self, prompt: str) -> str:
        # input() 会阻塞事件循环，放到线程池中执行
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, input, prompt)

    async def get_html(self) -> str:
        return await self._page.evaluate("document.documentElement.outerHTML;")

    async def screenshot(self, save_path: str) -> None:
        await self._page.screenshot(path=save_path)

    async def _dump_page_state(self) -> None:
        html_path = os.path.join(self._webcache_path, "webpage.html")
        with open(html_path, "w", encoding="utf-8") as fp:
            fp.write(await self.get_html())
        logging.info(
            "[%s] Current html saved to %s" % (self.__class__.__name__, html_path)
        )
        screenshot_path = os.path.join(self._webcache_path, "screenshot.jpg")
        await self.screenshot(screenshot_path)
        logging.info(
            "[%s] Current screenshot saved to %s"
            % (self.__class__.__name__, screenshot_path)
        )

    async def get_markdown(self) -> str:
        # 由浏览器侧判断渲染完成，条件满足时立即返回；超时则按现有内容继续
        try:
            await self._page.wait_for_function(
                "() => window.canvasContextHandler && canvasContextHandler.data.complete",
                timeout=10 * 1000,
                polling=100,
            )
        except PlaywrightTimeoutError:
            logging.info(
                "[%s] Wait for canvas complete timeout" % self.__class__.__name__
            )
        script = "canvasContextHandler.data.markdown;"
        result = await self._page.evaluate(script)
        if not result:
            await self._page.evaluate("canvasContextHandler.updateMarkdown();")
            result = await self._page.evaluate(script)
            if not result:
                raise RuntimeError("Wait for creating markdown timeout")
        return result

    async def _check_next_page(self) -> None:
        while True:
            try:
                # 竖向滚动模式渲染完成后出现「下一章」按钮；横向翻页模式则是「上一页/下一页」
                await self._page.wait_for_selector(
                    "button.readerFooter_button, button.renderTarget_pager_button",
                    timeout=60 * 1000,
                )
            except PlaywrightTimeoutError:
                logging.info("[%s] load selector timeout " % self.__class__.__name__)
                await self._dump_page_state()
                break
            if await self._page.locator("button.renderTarget_pager_button").count():
                raise HorizontalReaderError()
            button = self._page.locator("button.readerFooter_button").first
            result = (await button.inner_text()).strip()
            if result == "下一页":
                logging.info("[%s] Go to next page" % self.__class__.__name__)
                await self._page.evaluate(
                    r"canvasContextHandler.data.markdown += '\n\n';"
                )
                await button.click()
                await asyncio.sleep(1)
            elif result == "下一章":
                break
            elif result.startswith("登录"):
                raise utils.LoginRequiredError("Login required to read this chapter")
            else:
                raise NotImplementedError(result)

    def _get_chapter_url(self, chapter_id: str) -> str:
        return "%s%sk%s" % (
            self._chapter_root_url,
            self._book_id,
            utils.wr_hash(str(chapter_id)),
        )

    async def _switch_to_vertical_reader(self) -> None:
        """从横向翻页模式切换到竖向滚动模式

        登录用户的阅读模式偏好保存在账号侧，wr_useHorizonReader cookie 无法强制。
        横向模式下一屏画布会包含跨章节的内容，无法按章导出，必须切回竖向模式。
        """
        logging.info(
            "[%s] Horizontal reader detected, switch to vertical reader"
            % self.__class__.__name__
        )
        toggle = self._page.locator("button.readerControls_item.isHorizontalReader")
        if not await toggle.count():
            raise RuntimeError(
                "Horizontal reader detected, but the mode toggle button is missing"
            )
        await toggle.first.click(timeout=5000)
        await self._page.wait_for_selector("button.readerFooter_button", timeout=30 * 1000)

    async def goto_chapter(self, chapter_id: str, timeout: int = 120) -> None:
        logging.info("[%s] Go to chapter %s" % (self.__class__.__name__, chapter_id))
        self._url = self._get_chapter_url(chapter_id)
        await self._page.goto(
            self._url, timeout=1000 * timeout, wait_until="domcontentloaded"
        )
        # 需要登录时抛出 LoginRequiredError，由调用方在超时保护之外处理扫码登录
        try:
            await self._check_next_page()
        except HorizontalReaderError:
            await self._switch_to_vertical_reader()
            # 切换前画布上已经画过横向模式的内容，重新加载让钩子从头记录本章
            await self._page.goto(
                self._url, timeout=1000 * timeout, wait_until="domcontentloaded"
            )
            try:
                await self._check_next_page()
            except HorizontalReaderError:
                raise RuntimeError(
                    "Reader is still in horizontal mode after switching, "
                    "please switch to vertical mode manually in the browser"
                )

    async def clear_cache(self) -> None:
        await self._page.evaluate("canvasContextHandler.clearCanvasCache();")
