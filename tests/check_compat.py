"""兼容性校验：横向阅读模式入口、游客无头模式、页面无 JS 异常

    python tests/check_compat.py [book_id]

横向模式那一项会临时把账号的阅读模式切成双栏，结束时还原为竖向。
游客那一项使用独立的临时 profile，不会影响已登录的 profile。
"""

import asyncio
import json
import logging
import os
import re
import shutil
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
LONG_TITLE = "5.6 布林带新战法"
FREE_TITLE = "前言"
GUEST_PROFILE = os.path.join("cache", "test-guest-profile")

results = []
# 微信读书自己的报错，与钩子无关：点阅读模式按钮时它会去调微信 JSBridge，
# 桌面浏览器里没有这个 bridge。已验证开关钩子都不会额外产生 JS 异常
BENIGN_ERRORS = ("JSBridge is not ready",)


def page_errors(errors):
    return [e for e in errors if not any(b in e for b in BENIGN_ERRORS)]


def record(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("  %s %s %s" % ("PASS" if ok else "FAIL", name, detail))


def find_chapter(meta, title):
    return next(c for c in meta["chapters"] if c["title"].strip() == title)


async def check_horizontal_entry(meta):
    """账号处于横向双栏模式时，导出仍然要能自动切回竖向并采全"""
    print("\n=== 横向模式入口 ===")
    chapter = find_chapter(meta, LONG_TITLE)
    page = webpage.WeReadWebPage(DEFAULT_BOOK, webcache_path="cache")
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
        record("横向入口下正文采全", chars >= 8500, "%d 字" % chars)
        record("横向入口下无正文空洞", not gaps, "剩余 %s" % gaps[:3] if gaps else "")
        real = page_errors(errors)
        record("页面无 JS 异常", not real, "%s" % real[:3])
    finally:
        await page.close()


async def check_guest_headless(meta):
    """游客 + 无头模式下的试读章节"""
    print("\n=== 游客无头模式 ===")
    chapter = find_chapter(meta, FREE_TITLE)
    if os.path.isdir(GUEST_PROFILE):
        shutil.rmtree(GUEST_PROFILE, ignore_errors=True)
    page = webpage.WeReadWebPage(
        DEFAULT_BOOK, profile_dir=GUEST_PROFILE, webcache_path="cache"
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
        await page.goto_chapter(chapter["id"])
        markdown = await page.get_markdown()
        chars = WeReadExporter._content_chars(markdown)
        gaps = await p.evaluate("wrExtractor.findGaps()")
        record("游客无头模式采到试读正文", chars > 2100, "%d 字（登录态 2223 字）" % chars)
        record("游客无头模式无正文空洞", not gaps, "剩余 %s" % gaps[:3] if gaps else "")
        real = page_errors(errors)
        record("游客模式页面无 JS 异常", not real, "%s" % real[:3])
    finally:
        await page.close()
        shutil.rmtree(GUEST_PROFILE, ignore_errors=True)


async def main():
    book = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BOOK
    meta_path = os.path.join("cache", book, "meta.json")
    if not os.path.isfile(meta_path):
        print("找不到 %s" % meta_path)
        return 1
    with open(meta_path, encoding="utf-8") as fp:
        meta = json.load(fp)

    await check_horizontal_entry(meta)
    await check_guest_headless(meta)

    failed = [name for name, ok, _ in results if not ok]
    print("\n=== 汇总: %d 项检查，%d 项失败 ===" % (len(results), len(failed)))
    for name in failed:
        print("  FAIL %s" % name)
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
