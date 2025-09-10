# app.py
import os
import re
import json
import logging
import asyncio
from typing import Optional, Dict, Any, Tuple, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import httpx

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent-commerce")

# ---------- App ----------
app = FastAPI(title="Agent-Optimized Commerce API", version="0.5.0")

# ---------- Config ----------
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")  # optional, set on Render for proxy use
SCRAPER_API_ENDPOINT = os.getenv("SCRAPER_API_ENDPOINT", "http://api.scraperapi.com")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.google.com/",
}

# ---------- Models ----------
class AuditRequest(BaseModel):
    url: str


class AuditResponse(BaseModel):
    url: str
    score: float
    recommendations: List[str]
    product_info: dict


# ---------- Utilities ----------
def clean_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.strip()
    t = t.replace("₹", "").replace("Rs.", "").replace("MRP", "").replace("/-", "")
    t = re.sub(r"[^\d.]", "", t)
    return t or None


def is_block_page(url: str, html: Optional[str]) -> bool:
    if not html:
        return True
    t = html.lower()
    signals = [
        "site maintenance", "service unavailable", "captcha", "automated access",
        "bot check", "access denied", "too many requests", "server error", "enter the characters you see below"
    ]
    for s in signals:
        if s in t:
            return True
    # quick domain-specific hints
    if "amazon." in url and ("to discuss automated access to amazon data" in t or "enter the characters you see below" in t):
        return True
    if "flipkart." in url and ("error" in t and "access" in t):
        return True
    return False


# ---------- Fetch methods ----------
async def fetch_via_playwright(url: str, wait_selector: Optional[str] = None, timeout: int = 45000) -> Optional[str]:
    """Use Playwright to render. Includes small stealth patches and scrolling."""
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
                viewport={"width": 1366, "height": 768},
                locale="en-IN",
                extra_http_headers=BROWSER_HEADERS,
            )

            # stealth-ish
            await context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
                window.chrome = { runtime: {} };
                """
            )

            page = await context.new_page()

            # navigate
            try:
                await page.goto(url, wait_until="networkidle", timeout=timeout)
            except Exception as e:
                log.warning("Playwright goto error: %s", e)

            # scroll to trigger lazy-hydration
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            # optional wait for a selector (product title/price)
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=15000)
                except Exception:
                    log.info("Playwright: wait_selector not found within timeout: %s", wait_selector)

            html = await page.content()

            # cleanup
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

            log.info("Fetched page with Playwright")
            return html
    except Exception as e:
        log.warning("Playwright fetch failed: %s", e)
        return None


async def fetch_via_proxy(url: str, render: bool = True) -> Optional[str]:
    """Fetch via ScraperAPI (or similar). Return HTML text or None."""
    if not SCRAPER_API_KEY:
        return None
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": url,
        "country_code": "in",
        "device_type": "desktop",
        "keep_headers": "true",
    }
    if render:
        params["render"] = "true"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(SCRAPER_API_ENDPOINT, params=params, headers={"User-Agent": BROWSER_HEADERS["User-Agent"]})
            log.info("Proxy fetch status: %s for %s", r.status_code, url)
            if r.status_code == 200:
                return r.text
            else:
                log.warning("Proxy returned %s for %s", r.status_code, url)
                return None
    except Exception as e:
        log.error("Proxy fetch failed: %s", e)
        return None


async def fetch_via_httpx(url: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers=BROWSER_HEADERS)
            log.info("httpx status %s for %s", r.status_code, url)
            if r.status_code == 200:
                return r.text
            return None
    except Exception as e:
        log.error("httpx fetch failed: %s", e)
        return None


# ---------- Site-aware fetchers ----------
async def fetch_myntra(url: str) -> Optional[str]:
    """
    Myntra strategy: proxy-first (if SCRAPER_API_KEY available) to avoid Render IP blocks,
    otherwise Playwright-first -> httpx.
    """
    # 1) Proxy-first if available
    if SCRAPER_API_KEY:
        proxy_html = await fetch_via_proxy(url, render=True)
        if proxy_html and not is_block_page(url, proxy_html):
            log.info("Myntra: returning proxy HTML")
            return proxy_html
        log.info("Myntra: proxy missing or blocked, trying Playwright")

    # 2) Playwright fallback
    html = await fetch_via_playwright(url, wait_selector="h1.pdp-title")
    if html and not is_block_page(url, html):
        log.info("Myntra: Playwright returned usable HTML")
        return html

    # 3) httpx last resort
    http_html = await fetch_via_httpx(url)
    if http_html and not is_block_page(url, http_html):
        log.info("Myntra: httpx returned usable HTML")
        return http_html

    return None


async def fetch_flipkart(url: str) -> Optional[str]:
    """
    Flipkart strategy: proxy-first when available (reduces 529), else Playwright-first.
    If proxy returns blocked/529, do backoff then Playwright.
    """
    if SCRAPER_API_KEY:
        proxy_html = await fetch_via_proxy(url, render=True)
        if proxy_html and not is_block_page(url, proxy_html):
            log.info("Flipkart: returning proxy HTML")
            return proxy_html
        log.warning("Flipkart: proxy missing or blocked, backoff then Playwright")
        await asyncio.sleep(3)

    # Playwright fallback
    html = await fetch_via_playwright(url, wait_selector="span.B_NuCI")
    if html and not is_block_page(url, html):
        log.info("Flipkart: Playwright returned usable HTML")
        return html

    # httpx last resort
    http_html = await fetch_via_httpx(url)
    if http_html and not is_block_page(url, http_html):
        log.info("Flipkart: httpx returned usable HTML")
        return http_html

    return None


async def fetch_amazon(url: str) -> Optional[str]:
    """
    Amazon strategy: Playwright-first (good locally), then proxy if needed, then httpx.
    """
    html = await fetch_via_playwright(url, wait_selector="#productTitle")
    if html and not is_block_page(url, html):
        log.info("Amazon: Playwright returned usable HTML")
        return html

    if SCRAPER_API_KEY:
        proxy_html = await fetch_via_proxy(url, render=True)
        if proxy_html and not is_block_page(url, proxy_html):
            log.info("Amazon: returning proxy HTML")
            return proxy_html

    http_html = await fetch_via_httpx(url)
    if http_html and not is_block_page(url, http_html):
        log.info("Amazon: httpx returned usable HTML")
        return http_html

    return None


async def fetch_generic(url: str) -> Optional[str]:
    """
    Generic fallback for other marketplaces: Playwright then proxy then httpx.
    """
    html = await fetch_via_playwright(url)
    if html and not is_block_page(url, html):
        return html

    if SCRAPER_API_KEY:
        proxy_html = await fetch_via_proxy(url, render=True)
        if proxy_html and not is_block_page(url, proxy_html):
            return proxy_html

    http_html = await fetch_via_httpx(url)
    if http_html and not is_block_page(url, http_html):
        return http_html

    return None


async def fetch_page(url: str) -> Optional[str]:
    """Site-aware router that returns HTML or None."""
    url_lower = url.lower()
    if "myntra." in url_lower:
        return await fetch_myntra(url)
    if "flipkart." in url_lower:
        return await fetch_flipkart(url)
    if "amazon." in url_lower:
        return await fetch_amazon(url)
    return await fetch_generic(url)


# ---------- JSON helpers to extract Myntra product data ----------
def find_in_obj(obj, key_name):
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
    if isinstance(obj, dict):
        if "name" in obj and any(k in obj for k in ("price", "priceData", "mrp", "finalPrice", "discountedPrice")):
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
    if not isinstance(pdict, dict):
        return None
    candidates = []
    for k in ("discountedPrice", "discounted", "finalPrice", "sellingPrice", "price", "mrp"):
        v = pdict.get(k)
        if v:
            candidates.append(v)
    price_node = pdict.get("price")
    if isinstance(price_node, dict):
        for k in ("discounted", "final", "sellingPrice", "value"):
            v = price_node.get(k)
            if v:
                candidates.append(v)
    for c in candidates:
        if isinstance(c, (int, float)):
            return str(c)
        if isinstance(c, str):
            cp = clean_price(c)
            if cp:
                return cp
        if isinstance(c, dict):
            for inner in c.values():
                if isinstance(inner, (int, float)):
                    return str(inner)
                if isinstance(inner, str):
                    cp = clean_price(inner)
                    if cp:
                        return cp
    dumped = json.dumps(pdict)
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", dumped)
    if m:
        return m.group(1)
    return None


# ---------- Parser layer (Amazon / Flipkart / Myntra / fallback) ----------
def extract_product_info(html: str, url: str) -> Dict[str, Optional[str]]:
    soup = BeautifulSoup(html or "", "lxml")
    product = {"name": None, "price": None, "currency": None, "availability": None}

    # 1) JSON-LD
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            raw = script.string or script.get_text()
            data = json.loads(raw)
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

    # 2) OG/meta
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
    url_lower = (url or "").lower()
    if "amazon." in url_lower:
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

    if "flipkart." in url_lower:
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

    # Myntra: prefer parsing __NEXT_DATA__ JSON
    if "myntra." in url_lower:
        data_obj = None
        script_tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if script_tag:
            raw = script_tag.string or script_tag.get_text()
            try:
                data_obj = json.loads(raw)
            except Exception as e:
                log.warning("Failed to parse __NEXT_DATA__: %s", e)

        if not data_obj:
            for s in soup.find_all("script"):
                txt = (s.string or s.get_text() or "")
                if '"pageProps"' in txt and '"props"' in txt:
                    try:
                        obj = json.loads(txt)
                        data_obj = obj
                        break
                    except Exception:
                        m = re.search(r"(\{.*\"pageProps\".*\})", txt, flags=re.S)
                        if m:
                            try:
                                data_obj = json.loads(m.group(1))
                                break
                            except Exception:
                                continue

        product_data = None
        if data_obj:
            pp = data_obj.get("props", {}).get("pageProps", {})
            product_data = pp.get("product") or pp.get("pdp") or find_in_obj(pp, "product")
            if not product_data:
                product_data = find_product_dict(data_obj)

        if product_data:
            name = product_data.get("name") or product_data.get("displayName") or product_data.get("productName")
            price = extract_price_from_product_dict(product_data)
            currency = None
            if isinstance(product_data.get("price"), dict):
                currency = product_data.get("price").get("currency") or product_data.get("price").get("currencyCode")
            if not currency and price:
                currency = "INR"
            availability = None
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
            if product.get("name") or product.get("price"):
                return product

        # DOM fallback
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

    # Generic fallback: title + heuristics
    title = soup.find("title")
    product.update({
        "name": title.get_text(strip=True) if title else None,
        "availability": "In stock"
    })
    return product


# ---------- Scoring ----------
def audit_product(product: Dict[str, Optional[str]]) -> Tuple[float, List[str]]:
    checks = {
        "structured_data": bool(product.get("name")),
        "price_with_currency": bool(product.get("price") and product.get("currency")),
        "availability_present": bool(product.get("availability")),
    }
    score = sum(1 for v in checks.values() if v) / len(checks) * 100
    recommendations: List[str] = []
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


# ---------- Endpoints ----------
@app.get("/")
def read_root():
    return {"message": "Agent-Optimized Commerce API is running 🚀"}


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/debug")
def debug_info():
    return {
        "service": "Agent-Optimized Commerce API",
        "version": "0.5.0",
        "supported_sites": ["Amazon", "Flipkart", "Myntra (proxy-first)", "Generic marketplaces"],
    }


@app.post("/audit", response_model=AuditResponse)
async def audit_store(request: AuditRequest):
    url = request.url
    log.info("Audit requested for: %s", url)

    html = await fetch_page(url)
    if not html:
        raise HTTPException(status_code=502, detail="Failed to fetch target page")

    # If we detect a block page at this stage, surface it clearly so logs show what's happening.
    if is_block_page(url, html):
        log.warning("Detected block page for %s", url)
        # For Myntra, try proxy (if not already used)
        if "myntra." in url and SCRAPER_API_KEY:
            proxy_html = await fetch_via_proxy(url, render=True)
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
