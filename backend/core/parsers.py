from bs4 import BeautifulSoup
import json, re, logging
from .utils import clean_price

log = logging.getLogger("agent-commerce")

def extract_product_info(html: str, url: str) -> dict:
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
