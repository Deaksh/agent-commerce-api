def audit_product(product: dict) -> dict:
    """Simple audit scoring."""
    checks = {
        "structured_data": bool(product.get("name")),
        "price_with_currency": bool(product.get("price") and product.get("currency")),
        "availability_present": bool(product.get("availability")),
    }

    score = sum(1 for c in checks.values() if c) / len(checks) * 100

    return {
        "score": round(score, 2),
        "checks": checks,
    }
