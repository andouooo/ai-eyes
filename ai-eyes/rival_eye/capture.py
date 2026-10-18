"""Playwright 截图：http(s) 与本地 file:// 统一处理，固定视口保证红框坐标稳定。"""
import os
import sys
from pathlib import Path

from .config import VIEWPORT


def _default_browsers_dir() -> str:
    # 打包后 playwright 会去解包临时目录找浏览器，这里显式钉回用户缓存目录
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
        return os.path.join(local, "ms-playwright")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/ms-playwright")
    return os.path.expanduser("~/.cache/ms-playwright")


os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _default_browsers_dir())


def shot(url: str, out_png: Path) -> str:
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)
        page.goto(url, wait_until="networkidle", timeout=30_000)
        page.screenshot(path=str(out_png))
        browser.close()
    return str(out_png)
