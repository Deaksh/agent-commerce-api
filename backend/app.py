import os
import asyncio
import random
import logging
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
import httpx
from playwright.async_api import async_playwright

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "your_scraper_api_key_here")
SCRAPER_API_URL = "http://api.scraperapi.com"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.1 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent-commerce")

app = FastAPI()


# -------------------------------------------------------------------
# Utilities
# -------------------------------------------------------------------
async def proxy_get(url: str, client: httpx.AsyncClient, render: bool = False):
    """Fetch page via ScraperAPI."""
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": url,
        "country_code": "in",
        "device_type": "desktop",
        "keep_headers": "true",
    }
    if render:
        params["render"] = "true"

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-IN,en;q=0.9",
    }

    resp = await client.get(SCRAPER_API_URL, params=params, headers=headers, timeout=30)
    logger.info(f"Fetched page via proxy (ScraperAPI): {resp.status_code}")
    resp.raise_for_status()
    return resp.text


async def playwright_get(url: str, playwright, selector: str = None):
    """Fetch page via Playwright with optional wait_for_selector."""
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page(user_agent=random.choice(USER_AGENTS))
    await page.goto(url, wait_until="networkidle")

    if selector:
        try:
            await page.wait_for_selector(selector, timeout=10000)
        except Exception:
            logger.warning(f"Selector {selector} not found for {url}")

    content = await page.content()
    await browser.close()
    logger.info(f"Fetched page with Playwright ✅ {url}")
    return content


def is_blocked(html: str) -> bool:
    """Detect block pages by keywords."""
    block_signals = ["captcha", "blocked", "access denied", "too many requests", "server error"]
    return any(sig in html.lower() for sig in block_signals)


# -------------------------------------------------------------------
# Site-specific fetchers
# -------------------------------------------------------------------
async def fetch_myntra(url: str, client, playwright):
    # Playwright first (React hydration needed)
    html = await playwright_get(url, playwright, selector="h1.pdp-title")
    if not is_blocked(html) and "__NEXT_DATA__" in html:
        return html

    # ScraperAPI fallback
    html = await proxy_get(url, client, render=True)
    if is_blocked(html):
        raise Exception("Myntra blocked request")
    return html


async def fetch_flipkart(url: str, client, playwright):
    # ScraperAPI first (cheaper than Playwright)
    html = await proxy_get(url, client, render=True)
    if not is_blocked(html):
        return html

    # Backoff + Playwright fallback
    logger.warning("Flipkart blocked via proxy, retrying with Playwright...")
    await asyncio.sleep(random.randint(3, 6))
    html = await playwright_get(url, playwright, selector="span.B_NuCI")
    if is_blocked(html):
        raise Exception("Flipkart blocked request")
    return html


async def fetch_generic(url: str, client, playwright):
    # Generic sites → try proxy first
    html = await proxy_get(url, client, render=True)
    if not is_blocked(html):
        return html

    # fallback to Playwright
    html = await playwright_get(url, playwright)
    if is_blocked(html):
        raise Exception("Generic fetch blocked")
    return html


# -------------------------------------------------------------------
# Unified fetch_page (site-aware)
# -------------------------------------------------------------------
async def fetch_page(url: str):
    async with httpx.AsyncClient() as client:
        async with async_playwright() as playwright:
            if "myntra.com" in url:
                return await fetch_myntra(url, client, playwright)
            elif "flipkart.com" in url:
                return await fetch_flipkart(url, client, playwright)
            else:
                return await fetch_generic(url, client, playwright)


# -------------------------------------------------------------------
# FastAPI endpoint
# -------------------------------------------------------------------
@app.get("/audit")
async def audit(url: str = Query(..., description="Product URL to audit")):
    try:
        logger.info(f"Audit requested for: {url}")
        html = await fetch_page(url)
        return JSONResponse({"status": "success", "url": url, "length": len(html)})
    except Exception as e:
        logger.error(f"Audit failed for {url}: {e}")
        return JSONResponse({"status": "error", "url": url, "error": str(e)})
