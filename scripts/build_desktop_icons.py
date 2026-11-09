"""Render the approved vector mark into multi-size Windows icons (existing deep deps)."""

import asyncio
from io import BytesIO
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
SIZES = (16, 24, 32, 48, 64, 128, 256)


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 192, "height": 192}, device_scale_factor=4)
            for state in ("running", "stopped"):
                source = ROOT / "desktop/icons" / f"{state}.svg"
                await page.set_content('<style>body{margin:0;background:transparent}</style>'
                                       + source.read_text(encoding="utf-8"))
                png = await page.locator("svg").screenshot(omit_background=True)
                with Image.open(BytesIO(png)) as rendered:
                    image = rendered.convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS)
                    image.save(source.with_suffix(".ico"), sizes=[(size, size) for size in SIZES])
                print(f"{state}: {', '.join(map(str, SIZES))} px")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
