from weread_exporter.webpage import WeReadWebPage


import asyncio
import json
import logging
import os
import re
import sys
import time
from typing import Dict, List, Optional, Any, Tuple

import bs4
import markdown

from ebooklib import epub
from weasyprint import HTML, CSS

from . import utils

if sys.version_info >= (3, 8):
    from typing import TYPE_CHECKING
else:
    from typing_extensions import TYPE_CHECKING

if TYPE_CHECKING:
    from .webpage import WeReadWebPage

current_path = os.path.dirname(os.path.abspath(__file__))


class WeReadExporter(object):
    # 完整性校验以正文的纵向覆盖为准（见 hook.js 的 findGaps）。官方字数只作兜底：
    # 它的统计口径和网页版渲染出来的正文并不一致，实测完整章节在 0.88~1.00 之间，
    # 低于 min_content_ratio 只记录一行日志，低于 retry_content_ratio 才重采
    min_content_ratio: float = 0.75
    retry_content_ratio: float = 0.5
    max_extract_attempts: int = 3
    # 官方字数太少的章节（书名页、章名页）比例波动大，不做字数校验
    min_check_words: int = 30

    def __init__(self, page: WeReadWebPage, save_dir: str) -> None:
        self._page: WeReadWebPage = page
        self._save_dir: str = save_dir
        if not os.path.isdir(save_dir):
            os.makedirs(save_dir)
        self._meta_path: str = os.path.join(self._save_dir, "meta.json")
        self._chapter_dir: str = os.path.join(self._save_dir, "chapters")
        self._image_dir: str = os.path.join(self._save_dir, "images")
        if not os.path.isdir(self._image_dir):
            os.mkdir(self._image_dir)
        self._cover_image_path: str = os.path.join(self._save_dir, "cover.jpg")
        self._meta_data: Dict[str, Any] = {}
        self._current_chapter: int = 0

    async def get_book_title(self) -> str:
        meta_data = await self._load_meta_data()
        return meta_data["title"]

    def _make_chapter_path(self, index: int, chapter_id: str) -> str:
        return os.path.join(self._chapter_dir, "%d-%s.md" % (index + 1, chapter_id))

    async def _load_meta_data(self) -> Dict[str, Any]:
        if self._meta_data:
            return self._meta_data

        if not os.path.isfile(self._meta_path):
            self._meta_data = await self._page.get_book_info()
            with open(self._meta_path, "w") as fp:
                fp.write(json.dumps(self._meta_data))
        else:
            with open(self._meta_path) as fp:
                text = fp.read()
                if text:
                    self._meta_data = json.loads(text)
        return self._meta_data

    async def merge_markdown(self, save_path: str) -> None:
        meta_data = await self._load_meta_data()
        with open(save_path, "w") as fp:
            for index, chapter in enumerate(meta_data["chapters"]):
                file_path = self._make_chapter_path(index, chapter["id"])
                if not os.path.isfile(file_path):
                    raise RuntimeError("File %s not exist" % file_path)
                with open(file_path) as fd:
                    fp.write(fd.read() + "\n")

    async def pre_process_markdown(self) -> None:
        meta_data: Dict[str, Any] = await self._load_meta_data()
        for index, chapter in enumerate(meta_data["chapters"]):
            chapter_path = self._make_chapter_path(index, chapter["id"])
            if not os.path.isfile(chapter_path):
                logging.warning(
                    "[%s] File %s not exist" % (self.__class__.__name__, chapter_path)
                )
                continue
            with open(chapter_path, "rb") as fp:
                text = fp.read().decode()

            output = ""
            code_mode = False
            blank_line = False
            for line in text.split("\n"):
                if line == "```":
                    if not code_mode:
                        output += "\n%s\n" % line
                    else:
                        output += "%s\n" % line
                    code_mode = not code_mode
                elif code_mode:
                    output += line + "\n"
                elif line == "":
                    blank_line = True
                elif blank_line:
                    output += "\n\n%s" % line
                    blank_line = False
                else:
                    output += line
            output += "\n"
            pos = 0
            while pos >= 0:
                pos = output.find("](https://", pos)
                if pos < 0:
                    break
                pos1: int = output.find(")", pos)
                url = output[pos + 2 : pos1]
                logging.info("[%s] Replace image %s" % (self.__class__.__name__, url))
                try:
                    data = await utils.fetch(url)
                except:
                    logging.exception(
                        "[%s] Fetch image data of %s failed"
                        % (self.__class__.__name__, url)
                    )
                    pos += 10
                else:
                    image_name = utils.md5(url) + ".jpg"
                    with open(os.path.join(self._image_dir, image_name), "wb") as fp:
                        fp.write(data)
                    output = output[: pos + 2] + "images/" + image_name + output[pos1:]
            if not os.path.isfile(chapter_path + ".bak"):
                os.rename(chapter_path, chapter_path + ".bak")
            with open(chapter_path, "wb") as fp:
                fp.write(output.encode())

    async def markdown_to_txt(self, save_path: str) -> None:
        meta_data = await self._load_meta_data()
        for index, chapter in enumerate(meta_data["chapters"]):
            chapter_path = self._make_chapter_path(index, chapter["id"])
            raw_html = self._markdown_to_html(chapter_path, wrap=False)
            soup = bs4.BeautifulSoup(raw_html, features="html.parser")
            with open(save_path, "a+") as fp:
                fp.write(soup.text + "\n\n")

    def _markdown_to_html(self, path_or_text: str, wrap: bool = True) -> str:
        if os.path.isfile(path_or_text):
            with open(path_or_text, "rb") as fp:
                markdown_text = fp.read().decode()
        else:
            markdown_text = path_or_text
        html = markdown.markdown(
            markdown_text,
            extensions=[
                "markdown.extensions.fenced_code",
                "markdown.extensions.attr_list",
            ],
        )
        html += '<div class="page-break"></div>'
        if wrap:
            html = (
                '<html><head><link rel="stylesheet" href="style.css"></head><body>%s</body></html>'
                % html
            )
        return html

    async def markdown_to_pdf(
        self,
        save_path: str,
        extra_css: Optional[str] = None,
        image_format: str = "jpg",
        dump_html: bool = False,
    ) -> None:
        meta_data = await self._load_meta_data()
        raw_html: str = '<img src="cover.jpg" style="width: 100%;">\n'
        for index, chapter in enumerate(meta_data["chapters"]):
            chapter_path: str = self._make_chapter_path(index, chapter["id"])
            raw_html += self._markdown_to_html(chapter_path, wrap=False)
        raw_html = raw_html.replace(
            "<pre><code>", "<pre><code>\n"
        )  # Fix unexpected indent
        if image_format == "png":
            soup = bs4.BeautifulSoup(raw_html, features="html.parser")
            for img in soup.find_all("img"):
                src: str = os.path.join(self._save_dir, img.attrs["src"])
                if not src.endswith(".png"):
                    png_path: str = src[:-3] + "png"
                    utils.save_to_png(src, png_path)
                    img.attrs["src"] = img.attrs["src"][:-3] + "png"

            raw_html = soup.prettify()

        if dump_html:
            html_path = os.path.join(self._save_dir, "output.html")
            with open(html_path, "w") as fp:
                fp.write(raw_html)  # pyright: ignore[reportUnusedCallResult]
        html = HTML(string=raw_html, base_url=self._save_dir)
        css: List[CSS] = []
        css_path = os.path.join(current_path, "style.css")
        with open(css_path) as fp:
            raw_css = fp.read()
            if extra_css:
                raw_css += "\n" + extra_css
            css.append(CSS(string=raw_css))

        # Generate PDF
        html.write_pdf(
            save_path, stylesheets=css
        )  # pyright: ignore[reportUnknownMemberType, reportUnusedCallResult]

    async def markdown_to_epub(
        self, save_path: str, extra_css: Optional[str] = None
    ) -> None:
        meta_data = await self._load_meta_data()
        book = epub.EpubBook()
        book.set_identifier("id123456")
        book.set_title(meta_data["title"])
        book.set_language("zh-cn")
        book.add_author(meta_data["author"])
        # add cover image
        with open(self._cover_image_path, "rb") as fp:
            image_data = fp.read()
            book.set_cover("cover.jpg", image_data)
        # define CSS style
        css_path = os.path.join(current_path, "epub.css")
        with open(css_path) as fp:
            style = fp.read()
        if extra_css:
            style += "\n" + extra_css
        default_css = epub.EpubItem(
            uid="style_default",
            file_name="style/default.css",
            media_type="text/css",
            content=style,
        )

        # add CSS file
        book.add_item(default_css)
        chapters = []
        toc = []
        section: Optional[tuple] = None
        for index, chapter in enumerate(meta_data["chapters"]):
            chapter_path = self._make_chapter_path(index, chapter["id"])
            xhtml_name = "chap_%.4d.xhtml" % (index + 1)
            chap = epub.EpubHtml(
                title=chapter["title"], file_name=xhtml_name, lang="hr"
            )
            html = self._markdown_to_html(chapter_path)
            chap.content = html.replace("code>", "epub-code>")
            chap.add_item(default_css)
            # add chapter
            book.add_item(chap)
            chapters.append(chap)

            if section:
                if chapter["level"] > 1:
                    section[1].append(chap)
                else:
                    toc.append(section)
                    section = None

            if not section:
                if chapter["anchors"]:
                    section = (epub.Section(chapter["title"], xhtml_name), [])
                    for i, it in enumerate(chapter["anchors"]):
                        # add anchor point
                        chap.content = chap.content.replace(
                            ">%s<" % it["title"].replace(" ", ""),
                            ' id="t%d">%s<' % ((i + 1), it["title"]),
                        )
                        section[1].append(
                            epub.Link(
                                "%s#t%d" % (xhtml_name, (i + 1)),
                                it["title"],
                                str(chapter["id"]),
                            )
                        )
                elif chapter["level"] > 1:
                    section = (toc.pop(-1), [])
                    section[1].append(
                        epub.Link(xhtml_name, chapter["title"], str(chapter["id"]))
                    )
                else:
                    toc.append(
                        epub.Link(xhtml_name, chapter["title"], str(chapter["id"]))
                    )

        for it in os.listdir(self._image_dir):
            with open(os.path.join(self._image_dir, it), "rb") as fp:
                content = fp.read()
                image = epub.EpubItem(
                    file_name="images/" + it,
                    media_type="image/jpeg",
                    content=content,
                )
                book.add_item(image)

        book.toc = toc
        # add default NCX and Nav file
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        book.spine = ["nav", *chapters]
        # write to the file
        epub.write_epub(save_path, book, {})

    async def epub_to_mobi(self, epub_path: str, save_path: str) -> None:
        kindlegen_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "bin", sys.platform, "kindlegen"
        )
        if not os.path.isfile(kindlegen_path):
            raise RuntimeError("File %s not exist" % kindlegen_path)
        if sys.platform != "win32":
            os.chmod(kindlegen_path, 0o755)
        cmdline = [
            kindlegen_path,
            os.path.abspath(epub_path),
            "-o",
            os.path.basename(save_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmdline, cwd=os.path.dirname(save_path)
        )
        await proc.wait()

    async def save_cover_image(self) -> None:
        meta_data = await self._load_meta_data()
        cover_url = meta_data["cover"].replace("/s_", "/t9_")
        data = await utils.fetch(cover_url)
        with open(self._cover_image_path, "wb") as fp:
            fp.write(data)

    @staticmethod
    def _content_chars(markdown_text: str) -> int:
        """统计正文字符数，去掉图片链接与 markdown 标记，用于和官方字数比对"""
        text = re.sub(r"!\[\]\([^)]*\)", "", markdown_text)
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        text = re.sub(r"</?sup>|`", "", text)
        text = re.sub(r"^#{1,6}\s*|^-{3,}\s*$", "", text, flags=re.M)
        return len(re.findall(r"\S", text))

    @staticmethod
    def _random_rest_point(rest_every: Tuple[float, float]) -> int:
        """返回再导出多少章后休息一次，0 表示不休息"""
        if rest_every[1] <= 0:
            return 0
        return max(1, int(round(utils.random_seconds(rest_every))))

    async def export_markdown(
        self,
        timeout: int = 60,
        interval: Tuple[float, float] = (30, 30),
        rest_every: Tuple[float, float] = (0, 0),
        rest_interval: Tuple[float, float] = (0, 0),
    ) -> None:
        if not os.path.isdir(self._chapter_dir):
            os.makedirs(self._chapter_dir)
        meta_data = await self._load_meta_data()
        if not os.path.isfile(self._cover_image_path):
            await self.save_cover_image()

        exported = 0
        incomplete: List[Tuple[str, int, int]] = []
        rest_point = self._random_rest_point(rest_every)
        for index, chapter in enumerate(meta_data["chapters"]):
            logging.info(
                "[%s] Check chapter %s/%s"
                % (self.__class__.__name__, chapter["id"], chapter["title"])
            )

            file_path = self._make_chapter_path(index, chapter["id"])
            if os.path.isfile(file_path) and os.path.getsize(file_path) > 3:
                continue
            logging.info(
                "[%s] File %s not exist" % (self.__class__.__name__, file_path)
            )

            # 两层随机等待：章节之间在区间内随机，每隔随机若干章再休息更久一次，
            # 避免固定节奏。只在真正需要加载的章节之前等待，跳过的章节和最后一章之后不等
            if exported > 0:
                if rest_point and exported >= rest_point and rest_interval[1] > 0:
                    seconds = utils.random_seconds(rest_interval)
                    logging.info(
                        "[%s] Take a rest for %.0fs after %d chapters"
                        % (self.__class__.__name__, seconds, exported)
                    )
                    rest_point = exported + self._random_rest_point(rest_every)
                else:
                    seconds = utils.random_seconds(interval)
                    logging.info(
                        "[%s] Wait %.0fs before loading next chapter"
                        % (self.__class__.__name__, seconds)
                    )
                await asyncio.sleep(seconds)

            time0 = 0
            for _ in range(3):
                time0 = time.time()
                try:
                    await asyncio.wait_for(
                        self._page.goto_chapter(
                            chapter["id"],
                            timeout=timeout,
                        ),
                        timeout=timeout + 60,
                    )  # avoid browser hangs
                except asyncio.TimeoutError:
                    logging.warning(
                        "[%s] Load chapter %s timeout %ds"
                        % (
                            self.__class__.__name__,
                            chapter["title"],
                            time.time() - time0,
                        )
                    )
                    raise utils.LoadChapterFailedError()
                except utils.LoginRequiredError:
                    # 章节需要登录才能阅读，扫码登录后重试当前章节。
                    # 等待扫码耗时较长，不能放在上面的 wait_for 超时保护之内
                    logging.info(
                        "[%s] Login required for chapter %s"
                        % (self.__class__.__name__, chapter["title"])
                    )
                    await self._page.login()
                except (KeyboardInterrupt, utils.BreakExportingError) as ex:
                    raise ex
                except:
                    logging.exception(
                        "[%s] Go to chapter %s failed"
                        % (self.__class__.__name__, chapter["title"])
                    )
                else:
                    break
            else:
                raise utils.LoadChapterFailedError(
                    "Load chapter %s failed" % chapter["title"]
                )

            markdown_content, chars, complete = await self._extract_chapter(
                chapter, timeout
            )
            if not complete:
                incomplete.append(
                    (chapter["title"], chars, int(chapter.get("words") or 0))
                )
            logging.info(
                "[%s] Export chapter %s to %s"
                % (self.__class__.__name__, chapter["title"], file_path)
            )
            with open(file_path, "wb") as fp:
                fp.write(markdown_content.encode("utf-8", errors="replace"))
            exported += 1

        if incomplete:
            logging.warning(
                "[%s] %d chapter(s) may be incomplete, delete their files under %s "
                "and run again to retry:"
                % (self.__class__.__name__, len(incomplete), self._chapter_dir)
            )
            for title, got, want in incomplete:
                logging.warning(
                    "[%s]   %s: %d chars / %d words"
                    % (self.__class__.__name__, title, got, want)
                )

    async def _extract_chapter(
        self, chapter: Dict[str, Any], timeout: int
    ) -> Tuple[str, int, bool]:
        """取回当前章节正文并校验完整性，不完整就重新加载重采

        正文里有一部分元素随视口虚拟化，偶发漏采重新加载即可补齐。判断依据以
        正文的纵向覆盖为准：钩子会检查内容容器里有没有既没有文字也没有图片、
        代码块可以解释的空白区间，有空洞才说明真的漏了内容。
        """
        markdown_content: str = await self._page.get_markdown()
        chars = self._content_chars(markdown_content)
        gaps: List[List[int]] = self._page.last_content_gaps
        words = int(chapter.get("words") or 0)
        checked = words >= self.min_check_words

        def too_short(value: int) -> bool:
            return checked and value < words * self.retry_content_ratio

        attempt = 1
        while (gaps or too_short(chars)) and attempt < self.max_extract_attempts:
            attempt += 1
            logging.warning(
                "[%s] Chapter %s looks incomplete (%d chars, %d gap(s)), retry %d/%d"
                % (
                    self.__class__.__name__,
                    chapter["title"],
                    chars,
                    len(gaps),
                    attempt,
                    self.max_extract_attempts,
                )
            )
            await asyncio.sleep(utils.random_seconds((3.0, 8.0)))
            try:
                await self._page.goto_chapter(chapter["id"], timeout=timeout)
            except utils.LoginRequiredError:
                # 重采途中登录态过期，扫码后再继续
                logging.info(
                    "[%s] Login required while retrying chapter %s"
                    % (self.__class__.__name__, chapter["title"])
                )
                await self._page.login()
                await self._page.goto_chapter(chapter["id"], timeout=timeout)
            retried: str = await self._page.get_markdown()
            retried_chars = self._content_chars(retried)
            retried_gaps: List[List[int]] = self._page.last_content_gaps
            if len(retried_gaps) < len(gaps) or (
                len(retried_gaps) == len(gaps) and retried_chars > chars
            ):
                markdown_content, chars, gaps = retried, retried_chars, retried_gaps

        ratio = chars / words if words else 0
        if gaps:
            logging.warning(
                "[%s] Chapter %s still has %d content gap(s) after %d attempt(s): %s"
                % (
                    self.__class__.__name__,
                    chapter["title"],
                    len(gaps),
                    attempt,
                    gaps[:3],
                )
            )
            return markdown_content, chars, False
        if checked and chars < words * self.min_content_ratio:
            # 覆盖完整但字数偏少：官方字数包含网页版不渲染的内容，例如纸书版权页
            logging.info(
                "[%s] Chapter %s coverage is complete, %d chars vs %d official words (%.2f)"
                % (self.__class__.__name__, chapter["title"], chars, words, ratio)
            )
        else:
            logging.info(
                "[%s] Chapter %s content check passed: %d chars / %d words (%.2f)"
                % (self.__class__.__name__, chapter["title"], chars, words, ratio)
            )
        return markdown_content, chars, True
