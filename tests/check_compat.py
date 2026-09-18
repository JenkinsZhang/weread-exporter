"""兼容性校验：横向阅读模式入口、游客无头模式、页面无 JS 异常

    python tests/check_compat.py [book_id]

不指定 book_id 时自动使用 cache 下唯一一本已缓存的书。待检查的章节按官方字数
从 meta.json 里自动挑选，脚本本身不包含任何书的内容。

横向模式那一项会临时把账号的阅读模式切成双栏，结束时还原为竖向。
游客那一项使用独立的临时 profile，也不会恢复已登录的 cookie。
"""

import asyncio
import json
import logging
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from weread_exporter.__main__ import patch_windows

if sys.platform == "win32":
    patch_windows()

from weread_exporter import utils, webpage
from weread_exporter.export import WeReadExporter

logging.basicConfig(level=logging.INFO, format="[%(levelname)s]%(message)s")

CACHE_DIR = "cache"
GUEST_PROFILE = os.path.join(CACHE_DIR, "test-guest-profile")
# 微信读书自己的报错，与钩子无关：点阅读模式按钮时它会去调微信 JSBridge，
# 桌面浏览器里没有这个 bridge。已验证开关钩子都不会额外产生 JS 异常
BENIGN_ERRORS = ("JSBridge is not ready",)

results = []


def page_errors(errors):
    return [e for e in errors if not any(b in e for b in BENIGN_ERRORS)]


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


def longest_chapter(meta):
    return max(meta["chapters"], key=lambda c: c.get("words") or 0)


def preview_candidates(meta):
    """试读章节通常在书的前面，按顺序挑几章有正文的备选"""
    out = []
    for chapter in meta["chapters"][:6]:
        if (chapter.get("words") or 0) >= 300:
            out.append(chapter)
    return out[:3]


async def check_horizontal_entry(book, meta):
    """账号处于横向双栏模式时，导出仍然要能自动切回竖向并采全"""
    print("\n=== 横向模式入口 ===")
    chapter = longest_chapter(meta)
    page = webpage.WeReadWebPage(book, webcache_path=CACHE_DIR)
    errors = []
    await page.launch(headless=False)
    p = page._page
    p.on("pageerror", lambda e: errors.append(str(e).splitlines()[0][:120]))
    try:
        await p.goto(page._get_chapter_url(chapter["id"]), wait_until="domcontentloaded")
        await p.wait_for_selector(
            "button.readerFooter_button, button.renderTarget_pager_button", timeout=60000
        )
        await asyncio.sleep(2)
        if not await p.locator("button.renderTarget_pager_button").count():
            toggle = p.locator("button.readerControls_item.isNormalReader")
            if not await toggle.count():
                record("切到横向模式", False, "找不到切换按钮")
                return
            await toggle.first.click()
            await p.wait_for_selector("button.renderTarget_pager_button", timeout=30000)
            await asyncio.sleep(1.5)
        record("账号已处于横向模式", True)

        # 正常流程：goto_chapter 应当识别横向模式、切回竖向并重新加载
        await page.goto_chapter(chapter["id"])
        pager = await p.locator("button.renderTarget_pager_button").count()
        record("已自动切回竖向模式", pager == 0, "仍有 %d 个翻页按钮" % pager)
        markdown = await page.get_markdown()
        chars = WeReadExporter._content_chars(markdown)
        gaps = await p.evaluate("wrExtractor.findGaps()")
        words = int(chapter.get("words") or 0)
        record(
            "横向入口下正文采全",
            words == 0 or chars >= words * 0.8,
            "%d 字 / 官方 %d 字" % (chars, words),
        )
        record("横向入口下无正文空洞", not gaps, "剩余 %s" % gaps[:3] if gaps else "")
        real = page_errors(errors)
        record("页面无 JS 异常", not real, "%s" % real[:3])
    finally:
        await page.close()


async def check_guest_headless(book, meta):
    """游客 + 无头模式下的试读章节"""
    print("\n=== 游客无头模式 ===")
    candidates = preview_candidates(meta)
    if not candidates:
        print("  SKIP 找不到合适的试读备选章节")
        return
    if os.path.isdir(GUEST_PROFILE):
        shutil.rmtree(GUEST_PROFILE, ignore_errors=True)
    page = webpage.WeReadWebPage(
        book, profile_dir=GUEST_PROFILE, webcache_path=CACHE_DIR
    )
    errors = []

    async def auto_yes(_prompt):
        return "Y"

    page._ainput = auto_yes  # 无头模式会提示确认，测试里自动同意
    # 指向一个不存在的备份文件，避免把已登录的 cookie 恢复进临时 profile
    page._storage_state_path = os.path.join(GUEST_PROFILE, "no-storage-state.json")
    try:
        await page.launch(headless=True, allow_guest=True)
        p = page._page
        p.on("pageerror", lambda e: errors.append(str(e).splitlines()[0][:120]))
        for chapter in candidates:
            try:
                await page.goto_chapter(chapter["id"])
                markdown = await page.get_markdown()
            except utils.LoginRequiredError:
                continue
            chars = WeReadExporter._content_chars(markdown)
            gaps = await p.evaluate("wrExtractor.findGaps()")
            words = int(chapter.get("words") or 0)
            record(
                "游客无头模式采到试读正文",
                words == 0 or chars >= words * 0.8,
                "%d 字 / 官方 %d 字" % (chars, words),
            )
            record("游客无头模式无正文空洞", not gaps, "剩余 %s" % gaps[:3] if gaps else "")
            real = page_errors(errors)
            record("游客模式页面无 JS 异常", not real, "%s" % real[:3])
            return
        print("  SKIP 这本书前几章都需要登录，游客模式无法校验")
    finally:
        await page.close()
        shutil.rmtree(GUEST_PROFILE, ignore_errors=True)


async def main():
    book = detect_book()
    if not book:
        return 1
    meta_path = os.path.join(CACHE_DIR, book, "meta.json")
    if not os.path.isfile(meta_path):
        print("找不到 %s" % meta_path)
        return 1
    with open(meta_path, encoding="utf-8") as fp:
        meta = json.load(fp)

    await check_horizontal_entry(book, meta)
    await check_guest_headless(book, meta)

    failed = [name for name, ok, _ in results if not ok]
    print("\n=== 汇总: %d 项检查，%d 项失败 ===" % (len(results), len(failed)))
    for name in failed:
        print("  FAIL %s" % name)
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
