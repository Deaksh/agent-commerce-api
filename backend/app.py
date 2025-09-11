# app.py (patched)
import os
import json
import logging
import re
import asyncio
import hashlib
import secrets
import calendar
import datetime
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from pydantic import BaseModel, HttpUrl
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import httpx
import redis.asyncio as redis

# ---------- logging ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent-commerce")

# ---------- app ----------
app = FastAPI(title="Agent-Optimized Commerce API", version="0.4.0")

# ---------- env/config ----------
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")  # optional ScraperAPI key
SCRAPER_API_ENDPOINT = os.getenv("SCRAPER_API_ENDPOINT", "http://api.scraperapi.com")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "changeme_admin_token")  # protect admin endpoints

# initialize redis client (async)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# ---------- Pydantic models (LLM-friendly) ----------
class ProductInfo(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    price_raw: Optional[str] = None
    currency: Optional[str] = None
    availability: Optional[str] = None
    images: List[HttpUrl] = []
    description: Optional[str] = None
    brand: Optional[str] = None
    sku: Optional[str] = None
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    raw_html_excerpt: Optional[str] = None


class AuditResponse(BaseModel):
    url: HttpUrl
    product_info: ProductInfo
    score: float
    recommendations: List[str]
    fetched_via: Optional[str] = None     # "playwright"|"proxy"|"httpx"
    fetched_at: Optional[str] = None      # ISO timestamp


class AuditRequest(BaseModel):
    url: HttpUrl


# ---------- your existing helpers (minorly adapted) ----------
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
    if "amazon." in url and ("to discuss automated access to amazon data" in t or "enter the characters you see below" in t):
        return True
    return False


# ----------- fetching layer (unchanged logic + slight instrumentation) -----------
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
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768},
                java_script_enabled=True,
                locale="en-US",
            )

            # make bot detection harder
            await context.add_init_script(
                """Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"""
            )

            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=45000)

            # scroll + wait for lazy loaded content
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await page.wait_for_timeout(5000)

            if "myntra." in url:
                try:
                    await page.wait_for_selector("#__NEXT_DATA__", timeout=30000)
                    log.info("Playwright: __NEXT_DATA__ found on Myntra ✅")
                except Exception:
                    log.warning("Playwright: __NEXT_DATA__ not found, fallback to DOM")
                for _ in range(3):
                    await page.evaluate("window.scrollBy(0, 1000)")
                    await page.wait_for_timeout(2000)
                try:
                    await page.wait_for_selector("h1.pdp-title, h1.pdp-name", timeout=15000)
                except Exception:
                    log.warning("Playwright: Myntra product title not found in DOM fallback")

            html = await page.content()
            await browser.close()
            log.info("Fetched page with Playwright ✅")
            return html

    except Exception as e:
        log.warning(f"Playwright fetch failed: {e}")
        return None


async def fetch_via_proxy(url: str) -> Optional[str]:
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
    html = await fetch_via_playwright(url)
    if html and not is_block_page(url, html):
        return html

    if SCRAPER_API_KEY:
        proxy_html = await fetch_via_proxy(url)
        if proxy_html and not is_block_page(url, proxy_html):
            return proxy_html

    http_html = await fetch_via_httpx(url)
    if http_html and not is_block_page(url, http_html):
        return http_html

    return html or proxy_html or http_html


# ---------- JSON helpers / parser (copied from your file) ----------
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
        keys = obj.keys()
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


def extract_product_info(html: str, url: str) -> Dict[str, Optional[str]]:
    # NOTE: This is your existing parser code copied mostly verbatim for brevity.
    # It returns a simple dict with keys name, price, currency, availability.
    soup = BeautifulSoup(html or "", "lxml")
    product = {"name": None, "price": None, "currency": None, "availability": None}

    # JSON-LD
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

    # meta
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

    # amazon
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

    # flipkart
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

    # myntra - trying JSON and DOM fallbacks (kept as is)
    if "myntra." in url:
        script_tag = soup.find("script", {"id": "__NEXT_DATA__"})
        data_obj = None
        if script_tag and script_tag.string:
            try:
                raw = script_tag.string
                data_obj = json.loads(raw)
            except Exception as e:
                log.warning("Failed to parse __NEXT_DATA__ JSON: %s", e)
        if not data_obj:
            for s in soup.find_all("script"):
                txt = s.string or s.get_text() or ""
                if '{"props"' in txt and '"pageProps"' in txt:
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
            product_data = (
                pp.get("product")
                or pp.get("pdp")
                or pp.get("initialData", {}).get("product")
                or find_in_obj(pp, "product")
                or find_in_obj(pp, "productDetails")
                or find_in_obj(pp, "style")
            )
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

    title = soup.find("title")
    product.update({
        "name": title.get_text(strip=True) if title else None,
        "availability": "In stock"
    })
    return product


# ---------- audit scoring (keeps your logic) ----------
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


# ---------- Redis caching / helpers ----------
CACHE_TTL = int(os.getenv("CACHE_TTL", 3600))  # seconds
STALE_TTL = int(os.getenv("STALE_TTL", 600))


def cache_key_for_url(url: str) -> str:
    return "cache:" + hashlib.sha256(url.encode()).hexdigest()


async def get_cached_audit(url: str) -> Optional[dict]:
    ck = cache_key_for_url(url)
    data = await redis_client.get(ck)
    if data:
        try:
            return json.loads(data)
        except Exception:
            return None
    return None


async def set_cached_audit(url: str, payload: dict):
    ck = cache_key_for_url(url)
    await redis_client.set(ck, json.dumps(payload), ex=CACHE_TTL)


# ---------- API key management (Redis-backed) ----------
def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


async def create_api_key_record(plan: str = "free", quota: int = 1000) -> str:
    """
    Create an API key record in Redis and return the plaintext key (show once).
    """
    key = secrets.token_urlsafe(32)
    h = hash_key(key)
    data = {
        "plan": plan,
        "created_at": datetime.datetime.utcnow().isoformat(),
        "quota": str(quota),
        "disabled": "0",
    }
    await redis_client.hset(f"apikey:{h}", mapping=data)
    return key


async def get_api_record_from_hash(h: str) -> Optional[dict]:
    k = f"apikey:{h}"
    exists = await redis_client.exists(k)
    if not exists:
        return None
    rec = await redis_client.hgetall(k)
    return rec



async def get_api_record(
    x_api_key: str = Header(None, alias="x-api-key"),
    x_rapidapi_key: str = Header(None, alias="X-RapidAPI-Key")
):
    # Case 1: Direct customer key (your Redis)
    if x_api_key:
        h = hash_key(x_api_key)
        rec = await get_api_record_from_hash(h)
        if not rec:
            raise HTTPException(status_code=401, detail="Invalid API key (direct)")
        if rec.get("disabled") == "1":
            raise HTTPException(status_code=403, detail="API key disabled")
        rec["key_hash"] = h
        return rec

    # Case 2: RapidAPI user (skip Redis, trust RapidAPI gateway)
    if x_rapidapi_key:
        return {"plan": "rapidapi", "quota": "handled_by_rapidapi"}

    # If neither header is present
    raise HTTPException(status_code=401, detail="Missing API key")


async def seconds_until_month_end():
    now = datetime.datetime.utcnow()
    last_day = calendar.monthrange(now.year, now.month)[1]
    end = datetime.datetime(now.year, now.month, last_day, 23, 59, 59)
    return int((end - now).total_seconds()) + 1


async def enforce_quota(api_record: dict = Depends(get_api_record)):
    if api_record.get("plan") == "rapidapi":
        # RapidAPI handles quota & billing
        return {"usage": "handled_by_rapidapi", "quota": "handled_by_rapidapi", "plan": "rapidapi"}
    
    # Normal Redis quota check for direct customers
    key_hash = api_record["key_hash"]
    quota = int(api_record.get("quota", 1000))
    month_key = f"usage:{key_hash}:{datetime.datetime.utcnow().strftime('%Y%m')}"
    current = await redis_client.incr(month_key)
    if current == 1:
        await redis_client.expire(month_key, await seconds_until_month_end())
    if current > quota:
        raise HTTPException(status_code=429, detail="Monthly quota exceeded")
    return {"usage": current, "quota": quota, "plan": api_record.get("plan")}


# ---------- admin endpoints ----------
def check_admin_token(x_admin_token: str = Header(None, alias="x-admin-token")):
    if not x_admin_token or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    return True


@app.post("/admin/create_key")
async def admin_create_key(plan: str = "dev", quota: int = 1000, ok: bool = Depends(check_admin_token)):
    key = await create_api_key_record(plan=plan, quota=quota)
    # return plaintext key (show once)
    return {"api_key": key, "plan": plan, "quota": quota}


# ---------- main audit endpoint (protected) ----------
@app.post("/audit", response_model=AuditResponse)
async def audit_store(
    request: AuditRequest,
    api_record: dict = Depends(get_api_record),
    _usage: dict = Depends(enforce_quota),
):
    url = str(request.url)
    log.info("Audit requested for: %s (by plan=%s)", url, api_record.get("plan"))

    # 1) check cache
    cached = await get_cached_audit(url)
    if cached:
        log.info("Returning cached audit result")
        return cached

    # Special case: Myntra via proxy (better reliability)
    html = None
    fetched_via = None
    if "myntra." in url:
        if not SCRAPER_API_KEY:
            raise HTTPException(status_code=403, detail="Myntra requires proxy but no SCRAPER_API_KEY is set")
        log.info("Myntra detected -> forcing proxy fetch")
        html = await fetch_via_proxy(url)
        fetched_via = "proxy"
    else:
        # default fetch order
        html = await fetch_page(url)
        fetched_via = "playwright" if html else None

    if not html:
        raise HTTPException(status_code=502, detail="Failed to fetch target page")

    # Try parse even if block markers exist (we already tried to force proxy for myntra)
    product = extract_product_info(html, url)
    # If no useful product info, try proxy fallback (if available and not already proxy)
    if (not product.get("name") and not product.get("price")) and SCRAPER_API_KEY and fetched_via != "proxy":
        log.info("No product data found; trying proxy fallback")
        proxy_html = await fetch_via_proxy(url)
        if proxy_html:
            product = extract_product_info(proxy_html, url)
            fetched_via = "proxy"

    if not product.get("name") and not product.get("price"):
        log.warning("No product info could be extracted for %s", url)
        raise HTTPException(status_code=422, detail="Could not extract product info from target page")

    score, recommendations = audit_product(product)

    # normalize into structured ProductInfo (map fields we have)
    pinfo = ProductInfo(
        name=product.get("name"),
        price=float(product.get("price")) if product.get("price") else None,
        price_raw=str(product.get("price")) if product.get("price") else None,
        currency=product.get("currency"),
        availability=product.get("availability"),
        raw_html_excerpt=(html[:200] if html else None),
    )

    response_payload = {
        "url": url,
        "product_info": pinfo.dict(),
        "score": score,
        "recommendations": recommendations,
        "fetched_via": fetched_via,
        "fetched_at": datetime.datetime.utcnow().isoformat(),
    }

    # cache successful response
    await set_cached_audit(url, response_payload)

    return response_payload


# ---------- health endpoints ----------
@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/")
async def read_root():
    return {"message": "Agent-Optimized Commerce API (ready)"}
