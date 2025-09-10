import os
import httpx
import logging
from playwright.async_api import async_playwright

log = logging.getLogger("agent-commerce")

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")
SCRAPER_API_ENDPOINT = "http://api.scraperapi.com"

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

async def fetch_via_proxy(url: str) -> str | None:
    """Fetch using ScraperAPI (requires SCRAPER_API_KEY in env)."""
    if not SCRAPER_API_KEY:
        log.warning("SCRAPER_API_KEY not set, skipping proxy")
        return None

    params = {
        "api_key": SCRAPER_API_KEY,
        "url": url,
        "country_code": "in",
        "render": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(SCRAPER_API_ENDPOINT, params=params)
            if r.status_code == 200:
                log.info("Fetched via ScraperAPI proxy")
                return r.text
            log.warning(f"Proxy returned {r.status_code}")
    except Exception as e:
        log.error(f"Proxy fetch failed: {e}")
    return None

async def fetch_via_httpx(url: str, headers: dict) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                log.info("Fetched via httpx")
                return r.text
    except Exception as e:
        log.error(f"httpx fetch failed: {e}")
    return None
