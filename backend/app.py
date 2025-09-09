import os
import logging
import httpx
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

app = FastAPI()
logging.basicConfig(level=logging.INFO)

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/118.0.0.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.google.com/"
}


def should_use_proxy(url: str) -> bool:
    return any(domain in url for domain in ["amazon.", "myntra.", "flipkart."])


async def fetch_page(url: str) -> str | None:
    # --- Try ScraperAPI (proxy) first if configured ---
    if should_use_proxy(url) and SCRAPER_API_KEY:
        try:
            proxy_url = "http://api.scraperapi.com"
            params = {
                "api_key": SCRAPER_API_KEY,
                "url": url,
                "country_code": "in",
                "render": "true"
            }
            resp = httpx.get(proxy_url, params=params, timeout=30)
            if resp.status_code == 200:
                logging.info("Fetched via ScraperAPI")
                return resp.text
            logging.error(f"ScraperAPI failed: {resp.status_code}")
        except Exception as e:
            logging.error(f"Proxy fetch failed: {e}")

    # --- Try Playwright ---
    html = await fetch_with_playwright(url)
    if html:
        return html

    # --- Fallback to raw httpx ---
    return fetch_with_httpx(url)


async def fetch_with_playwright(url: str) -> str | None:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(user_agent=BROWSER_HEADERS["User-Agent"])
            page = await context.new_page()
            await page.goto(url, timeout=30000)
            html = await page.content()
            await browser.close()
            return html
    except Exception as e:
        logging.error(f"Playwright failed: {e}")
        return None


def fetch_with_httpx(url: str) -> str | None:
    try:
        resp = httpx.get(url, headers=BROWSER_HEADERS, timeout=30)
        if resp.status_code == 200:
            return resp.text
        logging.error(f"httpx failed with {resp.status_code}")
        return None
    except Exception as e:
        logging.error(f"httpx fetch failed: {e}")
        return None


def extract_product_info(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    name, price, currency, availability = None, None, None, None

    # --- AMAZON ---
    if "amazon." in url:
        title_tag = soup.find("span", id="productTitle")
        price_tag = soup.find("span", class_="a-price-whole")
        currency_tag = soup.find("span", class_="a-price-symbol")
        availability_tag = soup.find("span", id="availability")

        if title_tag:
            name = title_tag.get_text(strip=True)
        if price_tag:
            price = price_tag.get_text(strip=True).replace(",", "")
        if currency_tag:
            currency = currency_tag.get_text(strip=True)
        if availability_tag:
            availability = availability_tag.get_text(strip=True)

    # --- FLIPKART ---
    elif "flipkart." in url:
        title_tag = soup.find("span", class_="B_NuCI")
        price_tag = soup.find("div", class_="_30jeq3")
        availability_tag = soup.find("div", class_="_16FRp0")

        if title_tag:
            name = title_tag.get_text(strip=True)
        if price_tag:
            price = price_tag.get_text(strip=True).replace("₹", "").replace(",", "")
            currency = "INR"
        if availability_tag:
            availability = availability_tag.get_text(strip=True)

    # --- MYNTRA ---
    elif "myntra." in url:
        title_tag = soup.find("h1", class_="pdp-title")
        subtitle_tag = soup.find("h1", class_="pdp-name")
        price_tag = soup.find("span", class_="pdp-price")
        availability = "In stock"

        if title_tag and subtitle_tag:
            name = f"{title_tag.get_text(strip=True)} {subtitle_tag.get_text(strip=True)}"
        elif title_tag:
            name = title_tag.get_text(strip=True)
        if price_tag:
            price = price_tag.get_text(strip=True).replace("₹", "").replace(",", "")
            currency = "INR"

    # --- FALLBACK ---
    if not name:
        title = soup.find("title")
        if title:
            name = title.get_text(strip=True)

    return {
        "url": url,
        "product_info": {
            "name": name,
            "price": price,
            "currency": currency,
            "availability": availability,
        },
    }


@app.post("/audit")
async def audit(request: Request):
    body = await request.json()
    url = body.get("url")
    if not url:
        return JSONResponse({"error": "URL is required"}, status_code=400)

    html = await fetch_page(url)
    if not html:
        return JSONResponse({"error": "Failed to fetch page"}, status_code=500)

    data = extract_product_info(html, url)

    # Scoring logic
    score = 0
    recs = []
    info = data["product_info"]

    if info["name"]:
        score += 25
    else:
        recs.append("Add structured product name in JSON-LD or HTML metadata.")

    if info["price"]:
        score += 25
    else:
        recs.append("Add price in machine-readable format.")

    if info["currency"]:
        score += 25
    else:
        recs.append("Include product currency clearly.")

    if info["availability"]:
        score += 25
    else:
        recs.append("Specify availability status clearly.")

    data["score"] = score
    data["recommendations"] = recs if recs else ["Store is agent-ready ✅"]

    return JSONResponse(data)
