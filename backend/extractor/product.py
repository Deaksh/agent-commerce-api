from bs4 import BeautifulSoup
import json


def extract_product(html: str) -> dict:
    """Extract minimal product data from JSON-LD or meta tags."""
    soup = BeautifulSoup(html, "lxml")

    # Look for JSON-LD
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict) and data.get("@type") == "Product":
                return {
                    "name": data.get("name"),
                    "price": data.get("offers", {}).get("price"),
                    "currency": data.get("offers", {}).get("priceCurrency"),
                    "availability": data.get("offers", {}).get("availability"),
                }
        except Exception:
            continue

    # fallback: parse <meta> tags
    title = soup.find("meta", {"property": "og:title"})
    price = soup.find("meta", {"property": "product:price:amount"})
    currency = soup.find("meta", {"property": "product:price:currency"})

    return {
        "name": title["content"] if title else None,
        "price": price["content"] if price else None,
        "currency": currency["content"] if currency else None,
        "availability": None,
    }
