import os
import asyncio
import logging
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent-commerce")

app = FastAPI()

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "your_scraperapi_key")
SCRAPER_API_URL = "http://api.scraperapi.com"

# -----------------------------
# Playwright Fetch Function
# -----------------------------
async def playwright_fetch(url: str) -> dict:
    """
    Fetches a product page with Playwright (Amazon, Flipkart, Myntra supported).
    Returns dict with product title + raw HTML.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = await browser.new_page()

        # Rotate UA
        await page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/119.0.0.0 Safari/537.36"
        })

        try:
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")

            # Scroll for lazy load
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, 800)")
                await asyncio.sleep(2)

            # Site-specific selectors
            if "amazon." in url:
                selectors = ["#productTitle"]
            elif "flipkart.com" in url:
                selectors = ["span.B_NuCI", "span._35KyD6", "span._2J4LW6"]
            elif "myntra.com" in url:
                selectors = ["h1.pdp-title", "h1.pdp-name", "div.pdp-name"]
            else:
                selectors = ["title"]

            product_title = None
            for selector in selectors:
                try:
                    await page.wait_for_selector(selector, timeout=10000)
                    element = await page.query_selector(selector)
                    if element:
                        product_title = (await element.inner_text()).strip()
                        logger.info(f"✅ Extracted title: {product_title}")
                        break
                except Exception:
                    continue

            if not product_title:
                logger.warning("⚠️ No product title found. Returning raw HTML.")

            html = await page.content()
            await browser.close()
            return {"title": product_title, "html": html}

        except Exception as e:
            await browser.close()
            logger.error(f"❌ Playwright fetch failed: {e}")
            return {"title": None, "html": ""}


# -----------------------------
# Proxy Fetch Function
# -----------------------------
async def proxy_fetch(url: str) -> dict:
    """
    Fetch a page via ScraperAPI proxy.
    """
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            params = {
                "api_key": SCRAPER_API_KEY,
                "url": url,
                "country_code": "in",
                "device_type": "desktop",
                "keep_headers": "true",
                "render": "true"
            }
            resp = await client.get(SCRAPER_API_URL, params=params)
            if resp.status_code == 200:
                logger.info(f"Proxy fetch status: 200 for {url}")
                return {"title": None, "html": resp.text}
            else:
                logger.error(f"Proxy fetch failed: {resp.status_code}")
                return {"title": None, "html": ""}
    except Exception as e:
        logger.error(f"Proxy fetch failed: {e}")
        return {"title": None, "html": ""}


# -----------------------------
# Audit Endpoint
# -----------------------------
@app.post("/audit")
async def audit(request: Request):
    data = await request.json()
    url = data.get("url")

    if not url:
        return JSONResponse({"error": "Missing URL"}, status_code=400)

    logger.info(f"Audit requested for: {url}")

    # 1. Try proxy fetch first
    result = await proxy_fetch(url)

    # 2. If proxy fails → fallback to Playwright
    if not result["html"]:
        logger.warning(f"{url}: proxy missing or blocked, trying Playwright after backoff")
        result = await playwright_fetch(url)

    return JSONResponse(result)


# -----------------------------
# Root
# -----------------------------
@app.get("/")
async def root():
    return {"message": "Agent Commerce API running"}
