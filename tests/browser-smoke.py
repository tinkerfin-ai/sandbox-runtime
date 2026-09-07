"""Verify preinstalled Chromium with an offline page and the asynchronous API."""

import asyncio
import json
import os
import shutil
import struct
from importlib.metadata import version

from playwright.async_api import async_playwright


async def main() -> None:
    assert version("playwright") == "1.62.0"
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/sandbox-runtime/browsers"
    for command in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        assert shutil.which(command) is None
    results = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 640, "height": 480})
            await page.set_content(
                "<html><head><title>Runtime browser</title></head>"
                "<body><h1>Browser ready</h1><button onclick=\"this.textContent='clicked'\">"
                "Run</button></body></html>"
            )
            assert await page.title() == "Runtime browser"
            await page.get_by_role("button", name="Run").click()
            assert await page.get_by_role("button").inner_text() == "clicked"
            screenshot = await page.screenshot(type="png")
            assert screenshot[:8] == b"\x89PNG\r\n\x1a\n"
            assert struct.unpack(">II", screenshot[16:24]) == (640, 480)
            assert len(screenshot) > 1000
            results.append(
                {
                    "channel": "headless-shell",
                    "version": browser.version,
                    "png_bytes": len(screenshot),
                }
            )
        finally:
            await browser.close()
    print(json.dumps({"playwright": version("playwright"), "browsers": results}))


asyncio.run(main())
