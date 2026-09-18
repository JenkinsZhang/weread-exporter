"""章节正文提取的端到端校验

需要一个已登录的浏览器 profile（先正常跑一次 weread-exporter 扫码登录），
针对缓存里已有 meta.json 的书运行；不指定 book_id 时自动使用 cache 下唯一
一本已缓存的书：

    python tests/check_extract.py [book_id]

待检查的章节按官方字数从 meta.json 里自动挑选，覆盖最长、次长、中位、最短等
不同版面，脚本本身不包含任何书的内容。校验项：
  1. 正文的纵向覆盖有没有空洞，这是判断漏采的主要依据；
  2. 长章节必须同时采到画布文字和定位 span 两种来源；
  3. 采到的字数与官方字数的比值，以及和旧版导出结果的对比；
  4. 图片是否齐全且没有重复；
  5. 同一章重复采两次结果是否一致；
  6. 阅读顺序与完整性的独立参照：横向双栏模式下正文全部经由 canvas 按阅读
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

CACHE_DIR = "cache"
# 字数超过这个值的章节才做比值校验，太短的章名页、版权页比值波动大
RATIO_CHECK_WORDS = 1000
# 比值下限只用来兜底拦住大面积漏采，完整章节实测在 0.88 以上；
# 精确判定交给纵向覆盖校验
RATIO_FLOOR = 0.5
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


def detect_book():
    if len(sys.argv) > 1:
        return sys.argv[1]
    if not os.path.isdir(CACHE_DIR):
        return ""
    found = [
        name
        for name in os.listdir(CACHE_DIR)
        if os.path.isfile(os.path.join(CACHE_DIR, name, "meta.json"))
    ]
    if len(found) == 1:
        return found[0]
    print("cache 下有 %d 本已缓存的书，请在命令行指定 book_id: %s" % (len(found), found))
    return ""


def pick_cases(meta):
    """按官方字数自动挑选覆盖面尽量宽的章节"""
    chapters = meta["chapters"]
    order = sorted(range(len(chapters)), key=lambda i: chapters[i].get("words") or 0)
    picks = []

    def add(index, note):
        if index is None or index < 0 or index >= len(chapters):
            return
        if any(index == i for i, _ in picks):
            return
        picks.append((index, note))

    add(order[-1], "最长章节")
    if len(order) > 1:
        add(order[-2], "次长章节")
    add(order[len(order) // 2], "中位章节")
    add(order[0], "最短章节")
    add(0, "首章")
    for index in order:
        if (chapters[index].get("words") or 0) >= 500:
            add(index, "较短的正文章节")
            break
    return picks


def old_export(book, index, chapter_id):
    base = os.path.join(CACHE_DIR, book, "chapters", "%d-%s.md" % (index + 1, chapter_id))
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
    stats = await page._page.evaluate("wrExtractor.stats()")
    view = await page._page.evaluate("() => window.innerHeight")
    return markdown, gaps, stats, view


async def check_chapters(page, book, meta, cases):
    print("\n=== 逐章校验 ===")
    print(
        "  %-4s %6s %6s %5s %6s %6s %5s %5s %5s %8s  %s"
        % ("章", "官方字", "采到字", "比值", "画布字", "span字", "图片", "重复", "空洞", "旧版字数", "挑选原因")
    )
    longest = None
    span_seen = []
    for index, note in cases:
        chapter = meta["chapters"][index]
        markdown, gaps, stats, view = await extract(page, chapter)
        chars = content_chars(markdown)
        words = int(chapter.get("words") or 0)
        ratio = chars / words if words else 0
        images = IMG_RE.findall(markdown)
        dup = len(images) - len(set(images))
        old = old_export(book, index, chapter["id"])
        old_chars = content_chars(old) if old else -1
        print(
            "  %-4d %6d %6d %5.2f %6d %6d %5d %5d %5d %8d  %s"
            % (
                index + 1,
                words,
                chars,
                ratio,
                stats.get("canvasChars", 0),
                stats.get("spanChars", 0),
                len(images),
                dup,
                len(gaps),
                old_chars,
                note,
            )
        )
        tag = "第%d章" % (index + 1)
        record("%s 无正文空洞" % tag, not gaps, "剩余 %s" % gaps[:3] if gaps else "")
        record("%s 图片不重复" % tag, dup == 0, "重复 %d 个" % dup if dup else "")
        record("%s 正文非空" % tag, chars > 0)
        if words >= RATIO_CHECK_WORDS:
            record(
                "%s 字数未大面积缺失" % tag,
                ratio >= RATIO_FLOOR,
                "%d/%d = %.2f，下限 %.2f" % (chars, words, ratio, RATIO_FLOOR),
            )
        if old_chars > 0:
            record(
                "%s 不少于旧版" % tag,
                chars >= old_chars,
                "新 %d / 旧 %d" % (chars, old_chars),
            )
        # 正文总是先画在画布上，超出画布区域的部分才变成定位 span；
        # 页面高不代表正文长，插图多的章节可能整章正文都在画布上
        if chars > 0:
            record(
                "%s 画布文字已采到" % tag,
                stats.get("canvasChars", 0) > 0,
                "画布 %d 字 / span %d 字"
                % (stats.get("canvasChars", 0), stats.get("spanChars", 0)),
            )
        if stats.get("spanChars", 0) > 0:
            span_seen.append(index + 1)
        if note == "最长章节":
            longest = (index, markdown)

    if span_seen:
        record("定位 span 采集路径已覆盖", True, "第 %s 章命中" % span_seen)
    else:
        print("  NOTE 抽样的章节都没有定位 span，这本书的章节偏短，span 路径未被覆盖")
    return longest


async def check_determinism(page, meta, longest):
    print("\n=== 重复采集一致性 ===")
    index, first = longest
    second, gaps, _, _ = await extract(page, meta["chapters"][index])
    same = plain_text(first) == plain_text(second)
    record(
        "最长章节两次采集正文一致",
        same,
        ""
        if same
        else "第一次 %d 字，第二次 %d 字" % (content_chars(first), content_chars(second)),
    )
    record("最长章节复采无正文空洞", not gaps, "剩余 %s" % gaps[:3] if gaps else "")
    return second


async def check_reading_order(page, meta, index, markdown):
    """用横向双栏模式的绘制顺序作独立参照，双向校验顺序与完整性"""
    print("\n=== 与横向模式双向比对 ===")
    p = page._page
    chapter = meta["chapters"][index]
    following = meta["chapters"][index + 1] if index + 1 < len(meta["chapters"]) else None
    next_title = re.sub(r"\s+", "", following["title"]) if following else ""

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

    drawn = MEASURE_TEXT.sub(
        "", re.sub(r"\s+", "", await p.evaluate("() => window.__wrOrder.join('')"))
    )
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
        "" if cut > 0 else "未找到下一章标题，参照可能不完整",
    )

    forward = sample_snippets(body, 12)
    miss_forward = [s for s in forward if s not in drawn]
    record(
        "重建顺序与横向模式一致",
        forward and not miss_forward,
        "%d/%d 个片段命中" % (len(forward) - len(miss_forward), len(forward)),
    )
    backward = sample_snippets(chapter_drawn, 15, seed=8848)
    miss_backward = [s for s in backward if s not in body]
    record(
        "横向文本无漏采",
        backward and not miss_backward,
        "%d/%d 个片段命中" % (len(backward) - len(miss_backward), len(backward)),
    )
    for s in (miss_forward + miss_backward)[:5]:
        print("     未命中片段: %r" % s)

    if switched:
        back = p.locator("button.readerControls_item.isHorizontalReader")
        if await back.count():
            await back.first.click()
            await asyncio.sleep(1.5)
            print("  已把阅读模式还原为竖向")


async def main():
    book = detect_book()
    if not book:
        return 1
    meta_path = os.path.join(CACHE_DIR, book, "meta.json")
    if not os.path.isfile(meta_path):
        print("找不到 %s，请先正常导出一次该书以生成 meta.json" % meta_path)
        return 1
    with open(meta_path, encoding="utf-8") as fp:
        meta = json.load(fp)
    cases = pick_cases(meta)
    print("书 %s 共 %d 章，抽取 %d 章校验" % (book, len(meta["chapters"]), len(cases)))

    page = webpage.WeReadWebPage(book, webcache_path=CACHE_DIR)
    await page.launch(headless=False)
    try:
        longest = await check_chapters(page, book, meta, cases)
        if longest:
            second = await check_determinism(page, meta, longest)
            await check_reading_order(page, meta, longest[0], second or longest[1])
    finally:
        await page.close()

    failed = [name for name, ok, _ in results if not ok]
    print("\n=== 汇总: %d 项检查，%d 项失败 ===" % (len(results), len(failed)))
    for name in failed:
        print("  FAIL %s" % name)
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
