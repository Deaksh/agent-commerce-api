import httpx, re, logging
from playwright.async_api import async_playwright
from .utils import is_block_page

log = logging.getLogger("agent-commerce")

async def fetch_via_playwright(url: str) -> str | None:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=45000)
            html = await page.content()
            await browser.close()
            return html
    except Exception as e:
        log.warning(f"Playwright fetch failed: {e}")
        return None

async def fetch_via_httpx(url: str, headers: dict) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                return r.text
    except Exception as e:
        log.error(f"httpx fetch failed: {e}")
    return None
