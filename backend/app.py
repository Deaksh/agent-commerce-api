# app.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import json
import httpx
import logging

# ---------- LOGGING ---------- #
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Agent-Optimized Commerce API", version="0.2.1")

# ---------- MODELS ---------- #
class AuditRequest(BaseModel):
    url: str


class AuditResponse(BaseModel):
    url: str
    score: float
    recommendations: list[str]
    product_info: dict


# ---------- HEALTH ENDPOINTS ---------- #
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
        "version": "0.2.1",
        "supported_sites": ["Amazon", "Flipkart", "Myntra (improved)", "Generic marketplaces"],
    }


# ---------- CORE FUNCTIONS ---------- #
async def fetch_page_playwright(url: str) -> str:
    """Try to fetch with Playwright, fallback to httpx if blocked or fails"""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                ],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.google.com/"
                }
            )
            page = await context.new_page()
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")

            # 🔹 Extra wait for Myntra since data loads late
            if "myntra.com" in url:
                try:
                    await page.wait_for_selector("h1.pdp-title, h1.pdp-name", timeout=5000)
                except Exception:
                    logging.warning("Myntra product title not found within timeout")

            await page.wait_for_timeout(2000)
            content = await page.content()
            await browser.close()

            # detect Amazon bot-block page
            if "automated access" in content.lower() or "captcha" in content.lower():
                logging.warning("Amazon bot-block detected, falling back to httpx")
                raise Exception("Bot blocked")

            return content

    except Exception as e:
        logging.error(f"Playwright failed: {str(e)} — trying httpx")
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0.0.0 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.google.com/"
                }
                r = await client.get(url, headers=headers)
                r.raise_for_status()
                return r.text
        except Exception as http_err:
            raise HTTPException(status_code=500, detail=f"Both Playwright & httpx failed: {str(http_err)}")


def extract_product_info(html: str, url: str) -> dict:
    """Extract product info from HTML"""
    soup = BeautifulSoup(html, "lxml")
    product = {"name": None, "price": None, "currency": None, "availability": None}

    # Debug: log <title>
    title = soup.find("title")
    logging.info(f"Page title: {title.get_text(strip=True) if title else 'N/A'}")

    # 1️⃣ JSON-LD
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                data = next((d for d in data if d.get("@type") == "Product"), None)
            if isinstance(data, dict) and data.get("@type") == "Product":
                offers = data.get("offers", {})
                if isinstance(offers, list):
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

    # 2️⃣ Amazon-specific
    if "amazon." in url:
        name_tag = soup.select_one("#productTitle, span#title, h1 span")
        price_tag = soup.select_one(
            "#priceblock_ourprice, #priceblock_dealprice, span.a-price span.a-offscreen, span#price_inside_buybox"
        )
        avail_tag = soup.select_one("#availability span, #availability .a-color-success")

        product.update({
            "name": name_tag.get_text(strip=True) if name_tag else None,
            "price": price_tag.get_text(strip=True).replace("₹", "").replace(",", "") if price_tag else None,
            "currency": "INR" if price_tag else None,
            "availability": avail_tag.get_text(strip=True) if avail_tag else None,
        })
        return product

    # 3️⃣ Flipkart
    if "flipkart." in url:
        name_tag = soup.select_one("span.B_NuCI")
        price_tag = soup.select_one("div._30jeq3._16Jk6d")
        avail_tag = soup.select_one("div._16FRp0")
        product.update({
            "name": name_tag.get_text(strip=True) if name_tag else None,
            "price": price_tag.get_text(strip=True).replace("₹", "").replace(",", "") if price_tag else None,
            "currency": "INR" if price_tag else None,
            "availability": avail_tag.get_text(strip=True) if avail_tag else "In stock",
        })
        return product

    # 4️⃣ Myntra (improved fallback)
    if "myntra." in url:
        name_tag = soup.select_one("h1.pdp-title, h1.pdp-name")
        price_tag = (
            soup.select_one("span.pdp-price strong") or
            soup.select_one("span.pdp-discount-price") or
            soup.select_one("span.pdp-price")
        )
        avail_btn = soup.select_one("button.pdp-add-to-bag")
        out_of_stock_msg = soup.find(string=lambda t: t and "out of stock" in t.lower())

        # Clean up price
        clean_price = None
        if price_tag:
            clean_price = (
                price_tag.get_text(strip=True)
                .replace("₹", "")
                .replace("Rs.", "")
                .replace("MRP", "")
                .replace(",", "")
                .strip()
            )
            clean_price = clean_price.split()[0]

        product.update({
            "name": name_tag.get_text(strip=True) if name_tag else None,
            "price": clean_price,
            "currency": "INR" if clean_price else None,
            "availability": "Out of stock" if out_of_stock_msg else (
                "In stock" if avail_btn else None
            ),
        })
        return product

    # 5️⃣ Fallback: title
    product.update({
        "name": title.get_text(strip=True) if title else None,
        "availability": "In stock",
    })
    return product


def audit_product(product: dict) -> tuple[float, list[str]]:
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


# ---------- MAIN ENDPOINT ---------- #
@app.post("/audit", response_model=AuditResponse)
async def audit_store(request: AuditRequest):
    logging.info(f"Incoming request: {request.url}")
    try:
        html = await fetch_page_playwright(request.url)
        product = extract_product_info(html, request.url)
        score, recommendations = audit_product(product)
        return {
            "url": request.url,
            "score": score,
            "recommendations": recommendations,
            "product_info": product,
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        logging.error(f"Unhandled error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")
