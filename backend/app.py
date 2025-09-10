# app.py
import os
import json
import logging
import re
import asyncio
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import httpx

# logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent-commerce")

app = FastAPI(title="Agent-Optimized Commerce API", version="0.3.1")

# optional proxy key (set in Render env)
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")  # e.g. from ScraperAPI, set on Render dashboard
SCRAPER_API_ENDPOINT = "http://api.scraperapi.com"

# Models
class AuditRequest(BaseModel):
    url: str


class AuditResponse(BaseModel):
    url: str
    score: float
    recommendations: list[str]
    product_info: dict


# ---------- helpers ----------
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.google.com/",
}


def clean_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.strip()
    # remove currency symbols and non-digit chars except dot
    t = t.replace("₹", "").replace("Rs.", "").replace("MRP", "").replace("/-", "")
    t = re.sub(r"[^\d.]", "", t)
    return t or None


def is_block_page(url: str, html: Optional[str]) -> bool:
    if not html:
        return True
    t = html.lower()
    if "site maintenance" in t or "service unavailable" in t:
        return True
    if "captcha" in t or "automated access" in t or "bot check" in t or "access denied" in t:
        return True
    # amazon-specific checks
    if "amazon." in url and ("to discuss automated access to amazon data" in t or "enter the characters you see below" in t):
        return True
    return False


# ---------- fetching layer ----------
async def fetch_via_playwright(url: str) -> Optional[str]:
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
            user_agent="Mozilla/5.0 ...",
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            java_script_enabled=True)
            await context.add_init_script("""Object.defineProperty(navigator, 'webdriver', {get: () => undefined})""")
            
            page = await context.new_page()

            # Use networkidle to force React hydration
            await page.goto(url, wait_until="networkidle", timeout=60000)

            # Simulate human scroll to trigger lazy scripts
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(2000)

            if "myntra." in url:
                try:
                    await page.wait_for_selector("#__NEXT_DATA__", timeout=20000)
                except Exception:
                    log.info("Playwright: __NEXT_DATA__ not found on Myntra, fallback to DOM")

                try:
                    await page.wait_for_selector("h1.pdp-title, h1.pdp-name, span.pdp-price", timeout=20000)
                except Exception:
                    log.info("Playwright: Myntra fallback selectors also missing")

            html = await page.content()
            await browser.close()
            log.info("Fetched page with Playwright ✅")
            return html
    except Exception as e:
        log.warning(f"Playwright fetch failed: {e}")
        return None



async def fetch_via_proxy(url: str) -> Optional[str]:
    """Use ScraperAPI (or any similar service) as a fallback for Render blocking."""
    if not SCRAPER_API_KEY:
        return None
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": url,
        "country_code": "in",
        "render": "true",  # ask the provider to render JS if supported
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(SCRAPER_API_ENDPOINT, params=params)
            if r.status_code == 200:
                log.info("Fetched page via proxy (ScraperAPI)")
                return r.text
            log.warning(f"Proxy returned status {r.status_code}")
            return None
    except Exception as e:
        log.error("Proxy fetch failed: %s", e)
        return None


async def fetch_via_httpx(url: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=BROWSER_HEADERS)
            if r.status_code == 200:
                log.info("Fetched page via httpx")
                return r.text
            log.warning("httpx returned %s", r.status_code)
            return None
    except Exception as e:
        log.error("httpx fetch failed: %s", e)
        return None


async def fetch_page(url: str) -> Optional[str]:
    """
    Fetch order:
      1) Playwright (rendered)
      2) If block or missing Myntra-specific data -> Proxy (if configured)
      3) httpx fallback
    """
    # 1) Playwright
    html = await fetch_via_playwright(url)
    if html and not is_block_page(url, html):
        return html

    # 2) Proxy if configured
    if SCRAPER_API_KEY:
        proxy_html = await fetch_via_proxy(url)
        if proxy_html and not is_block_page(url, proxy_html):
            return proxy_html

    # 3) httpx fallback
    http_html = await fetch_via_httpx(url)
    if http_html and not is_block_page(url, http_html):
        return http_html

    # last return whatever we have (even if blocked) so caller can log and decide
    return html or proxy_html or http_html


# ---------- JSON helpers to extract Myntra product data ----------
def find_in_obj(obj, key_name):
    """Recursive search for key_name in nested dict/list; returns first value found or None."""
    if isinstance(obj, dict):
        if key_name in obj:
            return obj[key_name]
        for v in obj.values():
            res = find_in_obj(v, key_name)
            if res is not None:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = find_in_obj(item, key_name)
            if res is not None:
                return res
    return None


def find_product_dict(obj):
    """
    Try to find a plausible product dict inside arbitrary JSON.
    Heuristics: dict containing 'name' and some 'price'/'mrp'/'discount' keys.
    """
    if isinstance(obj, dict):
        keys = obj.keys()
        # direct product
        if "name" in obj and ("price" in obj or "priceData" in obj or "mrp" in obj or "finalPrice" in obj):
            return obj
        for v in obj.values():
            res = find_product_dict(v)
            if res:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = find_product_dict(item)
            if res:
                return res
    return None


def extract_price_from_product_dict(pdict: Dict[str, Any]) -> Optional[str]:
    """Try many common price fields used by Myntra"""
    if not isinstance(pdict, dict):
        return None
    candidates = []
    # Common nested structures
    for k in ("discountedPrice", "discounted", "finalPrice", "sellingPrice", "price", "mrp"):
        v = pdict.get(k)
        if v:
            candidates.append(v)
    # Some structures keep price inside nested dict "price": {"discounted": ...}
    price_node = pdict.get("price")
    if isinstance(price_node, dict):
        for k in ("discounted", "final", "sellingPrice", "value"):
            v = price_node.get(k)
            if v:
                candidates.append(v)
    # If candidates contain objects, try to extract numbers
    for c in candidates:
        if isinstance(c, (int, float)):
            return str(c)
        if isinstance(c, str):
            cp = clean_price(c)
            if cp:
                return cp
        if isinstance(c, dict):
            # deep search for numeric
            for inner in c.values():
                if isinstance(inner, (int, float)):
                    return str(inner)
                if isinstance(inner, str):
                    cp = clean_price(inner)
                    if cp:
                        return cp
    # fallback: try to find any digits in JSON dump of dict
    dumped = json.dumps(pdict)
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", dumped)
    if m:
        return m.group(1)
    return None


# ---------- parser layer ----------
def extract_product_info(html: str, url: str) -> Dict[str, Optional[str]]:
    """Extract product info from HTML (amazon/flipkart preserved), Myntra improved with __NEXT_DATA__ parsing"""
    soup = BeautifulSoup(html or "", "lxml")
    product = {"name": None, "price": None, "currency": None, "availability": None}

    # 1) JSON-LD first (works for some pages)
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                data = next((d for d in data if isinstance(d, dict) and d.get("@type") == "Product"), None)
            if isinstance(data, dict) and data.get("@type") == "Product":
                offers = data.get("offers", {}) or {}
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                product.update({
                    "name": data.get("name"),
                    "price": offers.get("price"),
                    "currency": offers.get("priceCurrency"),
                    "availability": offers.get("availability", "In stock"),
                })
                return product
        except Exception:
            continue

    # 2) OpenGraph/meta fallback
    meta_map = {
        "name": ["og:title", "twitter:title"],
        "price": ["product:price:amount", "og:price:amount"],
        "currency": ["product:price:currency", "og:price:currency"],
        "availability": ["product:availability", "og:availability"],
    }
    for key, props in meta_map.items():
        for prop in props:
            tag = soup.find("meta", {"property": prop}) or soup.find("meta", {"name": prop})
            if tag and tag.get("content"):
                product[key] = tag["content"]
                break
    if any(product.values()):
        return product

    # 3) site-specific parsing
    if "amazon." in url:
        name_tag = soup.select_one("#productTitle, span#title, h1 span")
        price_tag = soup.select_one("#priceblock_ourprice, #priceblock_dealprice, span.a-price span.a-offscreen")
        if not price_tag:
            price_whole = soup.select_one("span.a-price-whole")
            price_symbol = soup.select_one("span.a-price-symbol")
            if price_whole:
                price_value = price_whole.get_text(strip=True)
                symbol = price_symbol.get_text(strip=True) if price_symbol else "₹"
                price_tag = type("obj", (object,), {"text": f"{symbol}{price_value}"})
        avail_tag = soup.select_one("#availability span, #availability .a-color-success")
        product.update({
            "name": name_tag.get_text(strip=True) if name_tag else None,
            "price": clean_price(price_tag.get_text(strip=True)) if price_tag else None,
            "currency": "INR" if price_tag else None,
            "availability": avail_tag.get_text(strip=True) if avail_tag else None,
        })
        return product

    if "flipkart." in url:
        name_tag = soup.select_one("span.B_NuCI")
        price_tag = soup.select_one("div._30jeq3._16Jk6d, div._30jeq3")
        avail_tag = soup.select_one("div._16FRp0, div._2jcMA_, div._2jcMA_-NpjcY")
        product.update({
            "name": name_tag.get_text(strip=True) if name_tag else None,
            "price": clean_price(price_tag.get_text(strip=True)) if price_tag else None,
            "currency": "INR" if price_tag else None,
            "availability": avail_tag.get_text(strip=True) if avail_tag else None,
        })
        return product

    # Myntra: prefer parsing __NEXT_DATA__ JSON (Next.js)
    if "myntra." in url:
        # 1) try __NEXT_DATA__ script tag
        script_tag = soup.find("script", {"id": "__NEXT_DATA__"})
        data_obj = None
        if script_tag and script_tag.string:
            try:
                raw = script_tag.string
                data_obj = json.loads(raw)
            except Exception as e:
                log.warning("Failed to parse __NEXT_DATA__ JSON: %s", e)

        # 2) if not found, scan other scripts for product JSON fragments
        if not data_obj:
            for s in soup.find_all("script"):
                txt = s.string or s.get_text() or ""
                if '{"props"' in txt and '"pageProps"' in txt:
                    try:
                        # try to extract JSON substring safely
                        obj = json.loads(txt)
                        data_obj = obj
                        break
                    except Exception:
                        # attempt to find JSON braces substring (best-effort)
                        m = re.search(r"(\{.*\"pageProps\".*\})", txt, flags=re.S)
                        if m:
                            try:
                                data_obj = json.loads(m.group(1))
                                break
                            except Exception:
                                continue

        # 3) If we found a JSON data_obj, find product dict
        product_data = None
        if data_obj:
            # common path: props.pageProps.product
            pp = data_obj.get("props", {}).get("pageProps", {})
            product_data = pp.get("product") or pp.get("pdp") or find_in_obj(pp, "product")
            if not product_data:
                # fallback: recursive search for product-like dict
                product_data = find_product_dict(data_obj)

        # 4) extract fields from product_data if available
        if product_data:
            name = product_data.get("name") or product_data.get("displayName") or product_data.get("productName")
            price = extract_price_from_product_dict(product_data)
            currency = None
            # check price node for currency if present
            if isinstance(product_data.get("price"), dict):
                currency = product_data.get("price").get("currency") or product_data.get("price").get("currencyCode")
            # fallback heuristics
            if not currency and price:
                currency = "INR"
            availability = None
            # many Myntra structures have inStock / isInStock flags
            if isinstance(product_data.get("inStock"), bool):
                availability = "In stock" if product_data.get("inStock") else "Out of stock"
            elif isinstance(product_data.get("stock"), dict):
                availability = "In stock" if product_data.get("stock").get("available", True) else "Out of stock"

            product.update({
                "name": name,
                "price": clean_price(price) if price else None,
                "currency": currency,
                "availability": availability,
            })
            # if we got a name or price, return result
            if product.get("name") or product.get("price"):
                return product

        # 5) Fallback to DOM selectors (if JSON fails)
        name_tag = soup.select_one("h1.pdp-title, h1.pdp-name")
        price_tag = (
            soup.select_one("span.pdp-price strong")
            or soup.select_one("span.pdp-discount-price")
            or soup.select_one("span.pdp-offers-offerPrice")
            or soup.select_one("span.pdp-price")
        )
        avail_btn = soup.select_one("button.pdp-add-to-bag")
        oos_btn = soup.select_one("button.pdp-out-of-stock")
        oos_text = soup.find(string=lambda t: t and "out of stock" in t.lower())

        clean_price_val = None
        if price_tag:
            clean_price_val = clean_price(price_tag.get_text(strip=True))

        product.update({
            "name": name_tag.get_text(strip=True) if name_tag else None,
            "price": clean_price_val,
            "currency": "INR" if clean_price_val else None,
            "availability": ("Out of stock" if (oos_btn or oos_text) else ("In stock" if avail_btn else None))
        })
        return product

    # 4) Generic fallback: title + heuristics
    title = soup.find("title")
    product.update({
        "name": title.get_text(strip=True) if title else None,
        "availability": "In stock"
    })
    return product


# ---------- scoring ----------
def audit_product(product: Dict[str, Optional[str]]) -> (float, list):
    checks = {
        "structured_data": bool(product.get("name")),
        "price_with_currency": bool(product.get("price") and product.get("currency")),
        "availability_present": bool(product.get("availability")),
    }
    score = sum(1 for v in checks.values() if v) / len(checks) * 100
    recommendations = []
    if not checks["structured_data"]:
        recommendations.append("Add structured product name in JSON-LD or HTML metadata.")
    if not checks["price_with_currency"]:
        recommendations.append("Add price in machine-readable format.")
        recommendations.append("Include product currency clearly.")
    if not checks["availability_present"]:
        recommendations.append("Specify availability status clearly.")
    if score == 100:
        recommendations.append("Store is agent-ready ✅")
    return round(score, 2), recommendations


# ---------- endpoint ----------
@app.post("/audit", response_model=AuditResponse)
async def audit_store(request: AuditRequest):
    url = request.url
    log.info("Audit requested for: %s", url)

    html = await fetch_page(url)
    if not html:
        raise HTTPException(status_code=502, detail="Failed to fetch target page")

    # if we detect a block page, surface an explicit error so you can see it in render logs
    if is_block_page(url, html):
        log.warning("Detected block page for %s", url)
        # if we have proxy key, try proxy once more
        if SCRAPER_API_KEY:
            proxy_html = await fetch_via_proxy(url)
            if proxy_html and not is_block_page(url, proxy_html):
                html = proxy_html
            else:
                raise HTTPException(status_code=403, detail="Target site returned bot-block page from this environment")
        else:
            raise HTTPException(status_code=403, detail="Target site returned bot-block page from this environment")

    product = extract_product_info(html, url)
    score, recommendations = audit_product(product)
    return {
        "url": url,
        "score": score,
        "recommendations": recommendations,
        "product_info": product,
    }
