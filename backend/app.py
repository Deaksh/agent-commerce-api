# app.py
import os
import re
import json
import logging
import asyncio
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

app = FastAPI(title="Agent-Optimized Commerce API", version="0.3.0")
logging.basicConfig(level=logging.INFO)

# --- Config ---
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")  # IMPORTANT: set this in Render
PROXY_ENDPOINT = "http://api.scraperapi.com"
PROXY_DOMAINS = ("amazon.", "myntra.")  # always try proxy first for these
FALLBACK_PROXY_FOR_ANY = False          # set True if you want to proxy everything when key exists

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.google.com/",
}

# --- Simple helpers ---
def is_block_page(url: str, html: str) -> bool:
    text = (html or "").lower()
    if "amazon." in url:
        # Common Amazon bot/blocked pages
        if (
            "to discuss automated access to amazon data" in text
            or "robot check" in text
            or "enter the characters you see below" in text
            or "503 - service unavailable error" in text
        ):
            return True
    if "myntra." in url:
        if "site maintenance" in text or "maintenance" in text:
            return True
    # Generic Captcha / Access denied
    return any(t in text for t in ["captcha", "access denied"])

def should_use_proxy_first(url: str) -> bool:
    if not SCRAPER_API_KEY:
        return False
    return FALLBACK_PROXY_FOR_ANY or any(d in url for d in PROXY_DOMAINS)

# --- Fetchers ---
async def fetch_via_proxy(url: str) -> Optional[str]:
    """Scrape through ScraperAPI (or similar) when set."""
    if not SCRAPER_API_KEY:
        return None
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": url,
        "country_code": "in",
        "render": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(PROXY_ENDPOINT, params=params)
            if r.status_code == 200:
                logging.info("Fetched via proxy.")
                return r.text
            logging.warning(f"Proxy returned status {r.status_code}")
            return None
    except Exception as e:
        logging.error(f"Proxy fetch failed: {e}")
        return None

async def fetch_via_playwright(url: str) -> Optional[str]:
    """Headless Chromium with a few stealthy tweaks."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(
                user_agent=BROWSER_HEADERS["User-Agent"],
                locale="en-IN",
                viewport={"width": 1366, "height": 768},
                extra_http_headers=BROWSER_HEADERS,
            )

            # Hide webdriver flag a bit
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            page = await context.new_page()
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")

            # Mild scroll to trigger lazy content
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

            html = await page.content()
            await browser.close()
            return html
    except Exception as e:
        logging.error(f"Playwright failed: {e}")
        return None

async def fetch_via_httpx(url: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=25, headers=BROWSER_HEADERS) as client:
            r = await client.get(url)
            if r.status_code == 200:
                return r.text
            logging.warning(f"httpx status {r.status_code} for {url}")
            return None
    except Exception as e:
        logging.error(f"httpx failed: {e}")
        return None

async def fetch_page(url: str) -> Optional[str]:
    """
    Strategy:
      1) Proxy first for Amazon/Myntra (if key present).
      2) Playwright.
      3) httpx.
      4) If any fetch returns a known block page, and proxy available, try proxy once more.
    """
    tried_proxy = False

    if should_use_proxy_first(url):
        tried_proxy = True
        html = await fetch_via_proxy(url)
        if html and not is_block_page(url, html):
            return html

    html = await fetch_via_playwright(url)
    if html and not is_block_page(url, html):
        return html

    html = await fetch_via_httpx(url)
    if html and not is_block_page(url, html):
        return html

    # Final attempt: if we have a proxy but haven't tried it after a block, do it now.
    if SCRAPER_API_KEY and not tried_proxy:
        html = await fetch_via_proxy(url)
        if html and not is_block_page(url, html):
            return html

    return html  # may be None or blocked, caller will handle

# --- Parser utilities ---
def parse_json_ld_product(soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
    """Try strict JSON-LD first; many sites include a Product block."""
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            # Some pages put multiple JSON objects or trailing commas; skip silently
            continue

        candidates = []
        if isinstance(data, list):
            candidates = [d for d in data if isinstance(d, dict)]
        elif isinstance(data, dict):
            candidates = [data]

        for d in candidates:
            if d.get("@type") == "Product":
                product = {"name": None, "price": None, "currency": None, "availability": None}
                product["name"] = d.get("name")
                offers = d.get("offers")
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                if isinstance(offers, dict):
                    product["price"] = offers.get("price")
                    product["currency"] = offers.get("priceCurrency")
                    product["availability"] = offers.get("availability") or offers.get("availabilityStarts")
                return product
    return None

def clean_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.strip()
    t = t.replace("₹", "").replace("Rs.", "").replace("MRP", "").replace("/-", "")
    t = re.sub(r"[^\d.]", "", t)  # keep digits and dot
    return t or None

# --- Extractors per-domain + generic fallbacks ---
def extract_amazon(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    name = price = currency = availability = None
    title_tag = soup.select_one("#productTitle, span#title")
    price_tag = soup.select_one("span.a-price span.a-offscreen, span.a-price-whole")
    symbol_tag = soup.select_one("span.a-price-symbol")
    avail_tag = soup.select_one("#availability span, #availability .a-color-success")

    if title_tag:
        name = title_tag.get_text(strip=True)
    if price_tag:
        # offscreen has symbol+amount; whole is amount only
        price = clean_price(price_tag.get_text())
    if symbol_tag:
        sym = symbol_tag.get_text(strip=True)
        currency = "INR" if "₹" in sym else None
    elif price:
        currency = "INR"  # heuristic for amazon.in
    if avail_tag:
        availability = avail_tag.get_text(strip=True)
    return {"name": name, "price": price, "currency": currency, "availability": availability}

def extract_flipkart(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    name = price = currency = availability = None
    name_tag = soup.select_one("span.B_NuCI")
    price_tag = soup.select_one("div._30jeq3._16Jk6d, div._30jeq3")
    avail_tag = soup.select_one("div._16FRp0")

    if name_tag:
        name = name_tag.get_text(strip=True)
    if price_tag:
        price = clean_price(price_tag.get_text())
        currency = "INR"
    if avail_tag:
        availability = avail_tag.get_text(strip=True)
    else:
        availability = "In stock"
    return {"name": name, "price": price, "currency": currency, "availability": availability}

def extract_myntra(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    name = price = currency = availability = None
    # HTML selectors (can vary)
    title_tag = soup.select_one("h1.pdp-title, h1.pdp-name")
    subtitle_tag = soup.select_one("h1.pdp-name") if not title_tag or "pdp-title" in title_tag.get("class", []) else None
    price_tag = soup.select_one("span.pdp-price, span.pdp-discount-price, span.pdp-price strong")

    if title_tag and subtitle_tag:
        name = f"{title_tag.get_text(strip=True)} {subtitle_tag.get_text(strip=True)}"
    elif title_tag:
        name = title_tag.get_text(strip=True)

    if price_tag:
        price = clean_price(price_tag.get_text())
        currency = "INR"

    # Try parsing embedded JSON/state if HTML failed to show price
    if not price:
        for script in soup.find_all("script"):
            raw = script.string or script.get_text()
            if not raw:
                continue
            # Look for common Myntra price fields
            m = re.search(r'"(?:discountedPrice|price)\"?\s*:\s*\"?([0-9,]+)', raw)
            if m:
                price = clean_price(m.group(1))
                currency = "INR"
                break

    # Myntra rarely exposes explicit availability; assume in stock if page loads
    availability = availability or "In stock"
    return {"name": name, "price": price, "currency": currency, "availability": availability}

def extract_meta_fallback(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    name = price = currency = availability = None
    # OpenGraph/Twitter meta
    og_title = soup.find("meta", {"property": "og:title"}) or soup.find("meta", {"name": "twitter:title"})
    if og_title and og_title.get("content"):
        name = og_title["content"].strip()

    og_amount = soup.find("meta", {"property": "product:price:amount"}) or soup.find("meta", {"property": "og:price:amount"})
    if og_amount and og_amount.get("content"):
        price = clean_price(og_amount["content"])

    og_currency = soup.find("meta", {"property": "product:price:currency"}) or soup.find("meta", {"property": "og:price:currency"})
    if og_currency and og_currency.get("content"):
        currency = og_currency["content"].strip().upper()

    og_avail = soup.find("meta", {"property": "product:availability"}) or soup.find("meta", {"property": "og:availability"})
    if og_avail and og_avail.get("content"):
        availability = og_avail["content"].strip()

    return {"name": name, "price": price, "currency": currency, "availability": availability}
    
def extract_product_info(html: str, url: str) -> dict:
    """Parse product info depending on site (Amazon, Flipkart, Myntra)."""
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
        availability_tag = (
            soup.find("div", class_="_16FRp0") or
            soup.find("div", class_="_2jcMA_") or
            soup.find("div", class_="_2jcMA_-NpjcY")
        )

        if title_tag:
            name = title_tag.get_text(strip=True)
        if price_tag:
            price = price_tag.get_text(strip=True).replace("₹", "").replace(",", "")
            currency = "INR"
        if availability_tag:
            availability = availability_tag.get_text(strip=True)

    # --- MYNTRA ---
    elif "myntra." in url:
        try:
            script_tag = soup.find("script", id="__NEXT_DATA__")
            if script_tag and script_tag.string:
                data = json.loads(script_tag.string)
                product = data["props"]["pageProps"]["product"]
                name = f"{product.get('brand', '')} {product.get('name', '')}".strip()
                price = str(product["price"]["discounted"])
                currency = product["price"].get("currency", "INR")
                availability = "In stock" if product.get("inStock", True) else "Out of stock"
        except Exception as e:
            logging.error(f"Myntra JSON parse failed: {e}")

    # --- DEFAULT FALLBACK ---
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


def score_product(product: Dict[str, Optional[str]]) -> Dict[str, Any]:
    score = 0
    recs = []
    if product.get("name"):
        score += 25
    else:
        recs.append("Add structured product name in JSON-LD or HTML metadata.")
    if product.get("price"):
        score += 25
    else:
        recs.append("Add price in machine-readable format.")
    if product.get("currency"):
        score += 25
    else:
        recs.append("Include product currency clearly.")
    if product.get("availability"):
        score += 25
    else:
        recs.append("Specify availability status clearly.")

    if score == 100:
        recs = ["Store is agent-ready ✅"]
    return {"score": score, "recommendations": recs}

# --- Routes ---
@app.get("/")
def root():
    return {"message": "Agent-Optimized Commerce API is running 🚀", "version": "0.3.0"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/audit")
async def audit(request: Request):
    payload = await request.json()
    url = payload.get("url")
    if not url:
        return JSONResponse({"error": "URL is required"}, status_code=400)

    html = await fetch_page(url)
    if not html:
        return JSONResponse({"error": "Failed to fetch page (blocked or unreachable)"}, status_code=502)

    # If the final HTML is clearly a block page, surface it as a 403 to make debugging obvious
    if is_block_page(url, html):
        return JSONResponse({"error": "Target site returned a bot-block page from this environment."}, status_code=403)

    product = extract_product_info(html, url)
    scored = score_product(product)

    return JSONResponse(
        {
            "url": url,
            "product_info": product,
            "score": scored["score"],
            "recommendations": scored["recommendations"],
        }
    )
