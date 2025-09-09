# app.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import json
import asyncio

app = FastAPI(title="Agent-Optimized Commerce API", version="0.1.0")

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
        "version": "0.1.0",
        "supported_sites": ["Amazon", "Flipkart", "Myntra (beta)", "Generic marketplaces"],
    }


# ---------- CORE FUNCTIONS ---------- #
async def fetch_page_playwright(url: str) -> str:
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
            page = await browser.new_page(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ))
            await page.goto(url, timeout=60000, wait_until="networkidle")
            await page.wait_for_selector("title", timeout=10000)
            content = await page.content()
            await browser.close()
            return content
    except PlaywrightTimeoutError:
        raise HTTPException(status_code=504, detail="Timeout loading page")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Playwright error: {str(e)}")


def extract_product_info(html: str, url: str) -> dict:
    """Extract product info from HTML, site-specific + generic fallback"""
    soup = BeautifulSoup(html, "lxml")
    product = {"name": None, "price": None, "currency": None, "availability": None}

    # 1️⃣ JSON-LD schema.org Product
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

    # 2️⃣ OpenGraph & meta tags
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

    # 3️⃣ Site-specific tweaks
    if "amazon." in url:
        name_tag = soup.select_one("#productTitle")
        price_tag = soup.select_one(
            "#priceblock_ourprice, #priceblock_dealprice, span.a-price span.a-offscreen"
        )
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
            "price": price_tag.get_text(strip=True).replace("₹", "").replace(",", "") if price_tag else None,
            "currency": "INR" if price_tag else None,
            "availability": avail_tag.get_text(strip=True) if avail_tag else None,
        })
        return product

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

    if "myntra." in url:
        name_tag = soup.select_one("h1.pdp-title")
        price_tag = soup.select_one("span.pdp-price, span.pdp-discount-price")
        avail_tag = soup.find("div", string=lambda t: t and "out of stock" in t.lower())
        product.update({
            "name": name_tag.get_text(strip=True) if name_tag else None,
            "price": price_tag.get_text(strip=True).replace("₹", "").replace(",", "") if price_tag else None,
            "currency": "INR" if price_tag else None,
            "availability": "Out of stock" if avail_tag else "In stock",
        })
        return product

    # 4️⃣ Fallback: title + heuristics
    title = soup.find("title")
    product.update({
        "name": title.get_text(strip=True) if title else None,
        "availability": "In stock",
    })
    return product


def audit_product(product: dict) -> tuple[float, list[str]]:
    """Compute score and recommendations"""
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


# ---------- MAIN AUDIT ENDPOINT ---------- #
@app.post("/audit", response_model=AuditResponse)
async def audit_store(request: AuditRequest):
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
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")
