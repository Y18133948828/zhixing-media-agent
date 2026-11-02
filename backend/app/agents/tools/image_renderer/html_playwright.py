import asyncio
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright

from app.services.storage import Storage


class CardRenderer:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.env = Environment(
            loader=FileSystemLoader(Path(__file__).parent / "templates"),
            autoescape=select_autoescape(["html"]),
        )

    async def render(
        self, task_id: str, version: int, pack: dict, theme: str, mode: str, page_numbers=None
    ):
        # psycopg uses Selector on Windows, while browser subprocesses need Proactor.
        # A dedicated thread/loop allows both in the same local FastAPI process.
        return await asyncio.to_thread(
            self._in_thread, task_id, version, pack, theme, mode, page_numbers
        )

    def _in_thread(self, *args):
        factory = asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
        with asyncio.Runner(loop_factory=factory) as runner:
            return runner.run(self._render(*args))

    async def _render(
        self, task_id: str, version: int, pack: dict, theme: str, mode: str, page_numbers=None
    ):
        files = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(
                    viewport={"width": 1242, "height": 1656}, device_scale_factor=1
                )
                await page.route("**/*", lambda route: route.abort())
                for number, slide in enumerate(pack["slides"], 1):
                    if page_numbers is not None and number not in page_numbers:
                        continue
                    html = self.env.get_template("card.html").render(
                        slide=slide, number=number, total=len(pack["slides"]), theme=theme, mode=mode
                    )
                    await page.set_content(html, wait_until="load")
                    await page.evaluate("document.fonts.ready")
                    # Shrink a little if necessary; fail visibly instead of silently clipping text.
                    for size in (48, 44, 40, 36):
                        await page.locator(".content").evaluate(
                            "(el, size) => el.style.fontSize = size + 'px'", size
                        )
                        overflow = await page.locator(".content").evaluate(
                            "el => el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth"
                        )
                        if not overflow:
                            break
                    if overflow:
                        raise ValueError(f"第 {number} 页内容过多，请精简卡片后重试")
                    png = await page.screenshot(type="png")
                    prefix = f"{task_id}/v{version}/{theme}"
                    files.append(
                        self.storage.write(
                            f"{prefix}/card-{number:02}.png", png, "xhs_image", "image/png"
                        )
                    )
            finally:
                await browser.close()
        return files
