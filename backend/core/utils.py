import re
from bs4 import BeautifulSoup

def default_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/115.0 Safari/537.36"
        )
    }

def is_block_page(url: str, html: str) -> bool:
    """Detect common block/captcha pages"""
    if not html:
        return True
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text().lower()
    blocked_patterns = [
        "access denied",
        "captcha",
        "robot",
        "unusual traffic",
    ]
    return any(p in text for p in blocked_patterns)
