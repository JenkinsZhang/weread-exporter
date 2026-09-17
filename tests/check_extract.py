"""章节正文提取的端到端校验

需要一个已登录的浏览器 profile（先正常跑一次 weread-exporter 扫码登录），
针对缓存里已有 meta.json 的书运行：

    python tests/check_extract.py [book_id]

校验内容：
  1. 正文的纵向覆盖有没有空洞，这是判断漏采的主要依据；
  2. 采到的字数与官方字数的比值，以及和旧版导出结果的对比；
  3. 图片是否齐全且没有重复；
  4. 同一章重复采两次结果是否一致；
  5. 阅读顺序与完整性的独立参照：横向双栏模式下正文全部经由 canvas 按阅读
     顺序绘制，双向抽样比对——竖向重建的片段都能在横向文本里找到（顺序正确），
     横向文本里本章范围内的片段也都能在竖向重建里找到（没有漏字）。
"""

import asyncio
import json
import logging
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from weread_exporter.__main__ import patch_windows

if sys.platform == "win32":
    patch_windows()

from weread_exporter import webpage
from weread_exporter.export import WeReadExporter

logging.basicConfig(level=logging.INFO, format="[%(levelname)s]%(message)s")

DEFAULT_BOOK = "5fd32370813abb566g0162bd"
# 覆盖各种版面。min_ratio 是采到字数与官方字数比值的下限：
# 版权信息的官方字数含纸书版权页（出版社、ISBN 等），网页版只渲染书名作者几行，
# 属于已核实的口径差异，因此不设下限；章名页字数太少同样不做比值校验
CASES = [
    {"title": "版权信息", "min_ratio": 0.0, "note": "官方字数含纸书版权页"},
    {"title": "前言", "min_ratio": 0.95, "note": "纯 canvas 渲染"},
    {"title": "第一章 利弗莫尔的启发", "min_ratio": 0.0, "note": "章名页，仅图片"},
    {"title": "3.1 跳空定义", "min_ratio": 0.95, "note": "含内嵌图片"},
    {"title": "4.4 控制点的意义", "min_ratio": 0.85, "note": "图表密集"},
    {"title": "5.6 布林带新战法", "min_ratio": 0.85, "note": "最长章节，canvas+span"},
]
LONG_TITLE = "5.6 布林带新战法"
# 跨越 canvas 与 span 分界的关键词，缺任何一个都说明有一侧没采到
BOUNDARY_KEYWORDS = ["布林带的新战法", "金陵体育", "规律四", "兆易创新"]
# 页面测量字体宽度用的哑元文本，不是正文
MEASURE_TEXT = re.compile(r"[Aa]bcdefghijklmnopqrstuvwxyz[^一-鿿]*")

IMG_RE = re.compile(r"!\[\]\(([^)]*)\)")
ORDER_HOOK = (
    "(() => { window.__wrOrder = [];"
    " const P = CanvasRenderingContext2D.prototype; const f = P.fillText;"
    " P.fillText = function (t) { window.__wrOrder.push(String(t)); return f.apply(this, arguments); };"
    " })();"
)

results = []


def content_chars(text):
    return WeReadExporter._content_chars(text)


def plain_text(markdown_text):
    text = IMG_RE.sub("", markdown_text)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"</?sup>|`", "", text)
    text = re.sub(r"^#{1,6}\s*|^-{3,}\s*$", "", text, flags=re.M)
    return re.sub(r"\s+", "", text)


def record(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("  %s %s %s" % ("PASS" if ok else "FAIL", name, detail))


def old_export(book, index, chapter_id):
    base = os.path.join("cache", book, "chapters", "%d-%s.md" % (index, chapter_id))
    for path in (base + ".bak", base):
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fp:
                return fp.read()
    return ""


def sample_snippets(text, count, size=20, seed=20260917):
    random.seed(seed)
    out = []
    if len(text) < size + 4:
        return out
    for _ in range(count):
        start = random.randint(0, len(text) - size)
        out.append(text[start : start + size])
    return out


async def extract(page, chapter):
    await page.goto_chapter(chapter["id"])
    markdown = await page.get_markdown()
    gaps = await page._page.evaluate("wrExtractor.findGaps()")
    return markdown, gaps


def find_case(meta, title):
    return next(
        (i + 1, c)
        for i, c in enumerate(meta["chapters"])
        if c["title"].strip() == title
    )


async def check_chapters(page, book, meta):
    print("\n=== 逐章校验 ===")
    print(
        "  %-22s %6s %6s %5s %5s %5s %5s %8s  %s"
        % ("章节", "官方字", "采到字", "比值", "图片", "重复", "空洞", "旧版字数", "说明")
    )
    long_markdown = ""
    for case in CASES:
        title = case["title"]
        index, chapter = find_case(meta, title)
        markdown, gaps = await extract(page, chapter)
        chars = content_chars(markdown)
        words = int(chapter.get("words") or 0)
        ratio = chars / words if words else 0
        images = IMG_RE.findall(markdown)
        dup = len(images) - len(set(images))
        old = old_export(book, index, chapter["id"])
        old_chars = content_chars(old) if old else -1
        print(
            "  %-22s %6d %6d %5.2f %5d %5d %5d %8d  %s"
            % (title[:22], words, chars, ratio, len(images), dup, len(gaps), old_chars, case["note"])
        )
        if title == LONG_TITLE:
            long_markdown = markdown

        record("%s 无正文空洞" % title, not gaps, "剩余 %s" % gaps[:3] if gaps else "")
        record("%s 图片不重复" % title, dup == 0, "重复 %d 个" % dup if dup else "")
        record("%s 正文非空" % title, chars > 0)
        if case["min_ratio"] > 0:
            record(
                "%s 字数达标" % title,
                ratio >= case["min_ratio"],
                "%d/%d = %.2f，下限 %.2f" % (chars, words, ratio, case["min_ratio"]),
            )
        if old_chars > 0:
            record(
                "%s 不少于旧版" % title,
                chars >= old_chars,
                "新 %d / 旧 %d" % (chars, old_chars),
            )
    return long_markdown


async def check_determinism(page, meta, first):
    print("\n=== 重复采集一致性 ===")
    _, chapter = find_case(meta, LONG_TITLE)
    second, gaps = await extract(page, chapter)
    same = plain_text(first) == plain_text(second)
    record(
        "长章节两次采集正文一致",
        same,
        ""
        if same
        else "第一次 %d 字，第二次 %d 字" % (content_chars(first), content_chars(second)),
    )
    record("长章节复采无正文空洞", not gaps, "剩余 %s" % gaps[:3] if gaps else "")
    return second


async def check_reading_order(page, meta, markdown):
    """用横向双栏模式的绘制顺序作独立参照，双向校验顺序与完整性"""
    print("\n=== 与横向模式双向比对 ===")
    p = page._page
    index, chapter = find_case(meta, LONG_TITLE)
    next_title = re.sub(r"\s+", "", meta["chapters"][index]["title"])

    await p.add_init_script(ORDER_HOOK)
    await p.goto(page._get_chapter_url(chapter["id"]), wait_until="domcontentloaded")
    await p.wait_for_selector(
        "button.readerFooter_button, button.renderTarget_pager_button", timeout=60000
    )
    await asyncio.sleep(2)

    switched = False
    if not await p.locator("button.renderTarget_pager_button").count():
        toggle = p.locator("button.readerControls_item.isNormalReader")
        if not await toggle.count():
            record("切换到横向模式", False, "找不到切换按钮，跳过比对")
            return
        await toggle.first.click()
        await p.wait_for_selector("button.renderTarget_pager_button", timeout=30000)
        await asyncio.sleep(1.5)
        await p.goto(page._get_chapter_url(chapter["id"]), wait_until="domcontentloaded")
        await p.wait_for_selector("button.renderTarget_pager_button", timeout=60000)
        await asyncio.sleep(2.5)
        switched = True

    body = plain_text(markdown)
    for _ in range(80):
        drawn = re.sub(r"\s+", "", await p.evaluate("() => window.__wrOrder.join('')"))
        if next_title and next_title[:8] in drawn:
            break
        if len(drawn) >= len(body) * 1.25:
            break
        nxt = p.locator("button.renderTarget_pager_button", has_text="下一页")
        if not await nxt.count() or await nxt.first.is_disabled():
            break
        await nxt.first.click()
        await asyncio.sleep(1.1)

    drawn = re.sub(r"\s+", "", await p.evaluate("() => window.__wrOrder.join('')"))
    drawn = MEASURE_TEXT.sub("", drawn)
    # 横向翻页会越过章节边界，按下一章标题截断，只留本章内容
    cut = drawn.find(next_title[:8]) if next_title else -1
    chapter_drawn = drawn[:cut] if cut > 0 else drawn
    print(
        "  横向模式绘制 %d 字，截到下一章标题前 %d 字；竖向重建 %d 字"
        % (len(drawn), len(chapter_drawn), len(body))
    )
    record(
        "横向参照已覆盖整章",
        cut > 0,
        "" if cut > 0 else "未找到下一章标题 %r，参照可能不完整" % next_title[:8],
    )

    forward = sample_snippets(body, 12)
    hit = [s for s in forward if s in drawn]
    record(
        "重建顺序与横向模式一致",
        forward and len(hit) == len(forward),
        "%d/%d 个片段命中" % (len(hit), len(forward)),
    )
    for s in forward:
        if s not in drawn:
            print("     竖向片段未在横向文本中找到: %r" % s)

    backward = sample_snippets(chapter_drawn, 15, seed=8848)
    miss = [s for s in backward if s not in body]
    record(
        "横向文本无漏采",
        backward and not miss,
        "%d/%d 个片段命中" % (len(backward) - len(miss), len(backward)),
    )
    for s in miss[:5]:
        print("     横向片段未在竖向重建中找到: %r" % s)

    for kw in BOUNDARY_KEYWORDS:
        record("关键词 %s 已采到" % kw, kw in body)

    if switched:
        back = p.locator("button.readerControls_item.isHorizontalReader")
        if await back.count():
            await back.first.click()
            await asyncio.sleep(1.5)
            print("  已把阅读模式还原为竖向")


async def main():
    book = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BOOK
    meta_path = os.path.join("cache", book, "meta.json")
    if not os.path.isfile(meta_path):
        print("找不到 %s，请先正常导出一次该书以生成 meta.json" % meta_path)
        return 1
    with open(meta_path, encoding="utf-8") as fp:
        meta = json.load(fp)

    page = webpage.WeReadWebPage(book, webcache_path="cache")
    await page.launch(headless=False)
    try:
        long_markdown = await check_chapters(page, book, meta)
        second = await check_determinism(page, meta, long_markdown)
        await check_reading_order(page, meta, second or long_markdown)
    finally:
        await page.close()

    failed = [name for name, ok, _ in results if not ok]
    print("\n=== 汇总: %d 项检查，%d 项失败 ===" % (len(results), len(failed)))
    for name in failed:
        print("  FAIL %s" % name)
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
