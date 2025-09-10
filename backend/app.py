import os
import json
import logging
from typing import Optional
from fastapi import FastAPI, Request
from pydantic import BaseModel
import httpx
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent-commerce")

app = FastAPI()

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "")
SCRAPER_API_URL = "http://api.scraperapi.com"

class AuditRequest(BaseModel):
    url: str

async def fetch_via_scraperapi(url: str) -> Optional[str]:
    try:
        params = {
            "api_key": SCRAPER_API_KEY,
            "url": url,
            "country_code": "in",
            "render": "true",
            "device_type": "desktop",
            "keep_headers": "true"
        }
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.get(SCRAPER_API_URL, params=params)
            if r.status_code == 200:
                log.info("Fetched page via proxy (ScraperAPI)")
                return r.text
            else:
                log.warning(f"ScraperAPI returned {r.status_code}")
                return None
    except Exception as e:
        log.error(f"Proxy fetch failed: {e}")
        return None

async def fetch_via_playwright(url: str) -> Optional[str]:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/122.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await page.wait_for_timeout(3000)

            # Marketplace-specific waits
            if "amazon." in url:
                await page.wait_for_selector("#productTitle", timeout=15000)
            elif "flipkart." in url:
                await page.wait_for_selector("span.B_NuCI", timeout=15000)
            elif "myntra." in url:
                try:
                    await page.wait_for_selector("h1.pdp-title", timeout=15000)
                except Exception:
                    log.info("Myntra: fallback, forcing extra wait")
                    await page.wait_for_timeout(5000)

            html = await page.content()
            await browser.close()
            log.info("Fetched page with Playwright ✅")
            return html
    except Exception as e:
        log.error(f"Playwright fetch failed: {e}")
        return None

async def fetch_via_httpx(url: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url)
            if r.status_code == 200 and "captcha" not in r.text.lower():
                log.info("Fetched page via httpx")
                return r.text
            else:
                log.warning(f"httpx status {r.status_code} or block page detected")
                return None
    except Exception as e:
        log.error(f"httpx fetch failed: {e}")
        return None

async def fetch_html(url: str) -> Optional[str]:
    log.info(f"Audit requested for: {url}")

    html = await fetch_via_scraperapi(url)
    if html: return html

    html = await fetch_via_playwright(url)
    if html: return html

    html = await fetch_via_httpx(url)
    if html: return html

    return None

@app.post("/audit")
async def audit(req: AuditRequest):
    url = req.url
    html = await fetch_html(url)
    if not html:
        return {"url": url, "error": "Failed to fetch page"}

    # Marketplace-specific parsing
    product_info = {"name": None, "price": None, "currency": None, "availability": None}

    if "amazon." in url:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        product_info["name"] = soup.select_one("#productTitle").get_text(strip=True) if soup.select_one("#productTitle") else None
        product_info["price"] = soup.select_one("#priceblock_ourprice,#priceblock_dealprice")
        if product_info["price"]:
            product_info["price"] = product_info["price"].get_text(strip=True)

    elif "flipkart." in url:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        product_info["name"] = soup.select_one("span.B_NuCI").get_text(strip=True) if soup.select_one("span.B_NuCI") else None
        price_el = soup.select_one("div._30jeq3")
        if price_el:
            product_info["price"] = price_el.get_text(strip=True).replace("₹", "").replace(",", "")
            product_info["currency"] = "INR"

    elif "myntra." in url:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        product_info["name"] = soup.select_one("h1.pdp-title").get_text(strip=True) if soup.select_one("h1.pdp-title") else None
        price_el = soup.select_one("span.pdp-price") or soup.select_one("div.pdp-price")
        if price_el:
            product_info["price"] = price_el.get_text(strip=True).replace("₹", "").replace(",", "")
            product_info["currency"] = "INR"

    return {
        "url": url,
        "product_info": product_info
    }
