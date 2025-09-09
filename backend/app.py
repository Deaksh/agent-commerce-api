from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import json
import asyncio

app = FastAPI()


@app.get("/")
def read_root():
    return {"message": "Agent-Optimized Commerce API is running 🚀"}


class AuditRequest(BaseModel):
    url: str


class AuditResponse(BaseModel):
    url: str
    score: float
    recommendations: list[str]
    product_info: dict


async def fetch_page_playwright(url: str) -> str:
    """Fetches rendered HTML using Playwright for JS-heavy pages (Render optimized)"""
    try:
        async with async_playwright() as p:
            browser = None
            try:
                # Prefer Firefox for Myntra, else Chromium
                if "myntra." in url:
                    try:
                        browser = await p.firefox.launch(headless=True)
                    except Exception:
                        browser = await p.chromium.launch(headless=True)
                else:
                    browser = await p.chromium.launch(headless=True)

                page = await browser.new_page()
                await page.goto(url, timeout=60000, wait_until="networkidle")
                await asyncio.sleep(3)  # let JS settle
                content = await page.content()
                await browser.close()
                return content
            finally:
                if browser:
                    await browser.close()
    except PlaywrightTimeoutError:
        raise HTTPException(status_code=504, detail="Timeout loading page")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Playwright error: {e}")


def extract_product_info(html: str, url: str) -> dict:
    """Extract product info from HTML, generic for all marketplaces"""
    soup = BeautifulSoup(html, "lxml")
    product = {"name": None, "price": None, "currency": None, "availability": None}

    # JSON-LD schema
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

    # OpenGraph/meta
    meta_map = {
        "name": ["og:title", "twitter:title"],
        "price": ["product:price:amount", "og:price:amount"],
        "currency": ["product:price:currency", "og:price:currency"],
        "availability": ["product:availability", "og:availability"]
    }
    for key, props in meta_map.items():
        for prop in props:
            tag = soup.find("meta", {"property": prop}) or soup.find("meta", {"name": prop})
            if tag and tag.get("content"):
                product[key] = tag["content"]
                break

    if any(product.values()):
        return product

    # Amazon
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
            "availability": avail_tag.get_text(strip=True) if avail_tag else None
        })
        return product

    # Flipkart
    if "flipkart." in url:
        name_tag = soup.select_one("span.B_NuCI")
        price_tag = soup.select_one("div._30jeq3._16Jk6d")
        avail_tag = soup.select_one("div._16FRp0")
        product.update({
            "name": name_tag.get_text(strip=True) if name_tag else None,
            "price": price_tag.get_text(strip=True).replace("₹", "").replace(",", "") if price_tag else None,
            "currency": "INR" if price_tag else None,
            "availability": avail_tag.get_text(strip=True) if avail_tag else "In stock"
        })
        return product

    # Myntra
    if "myntra." in url:
        name_tag = soup.select_one("h1.pdp-title, h1.pdp-name")
        price_tag = (
            soup.select_one("span.pdp-price strong") or
            soup.select_one("span.pdp-discount-price") or
            soup.select_one("span.pdp-price")
        )
        avail_tag = soup.find("div", string=lambda t: t and "out of stock" in t.lower())

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
            "availability": "Out of stock" if avail_tag else "In stock"
        })
        return product

    title = soup.find("title")
    product.update({
        "name": title.get_text(strip=True) if title else None,
        "availability": "In stock"
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
            "product_info": product
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")
