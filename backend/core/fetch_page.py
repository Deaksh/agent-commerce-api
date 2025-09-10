from .fetchers import fetch_via_playwright, fetch_via_proxy, fetch_via_httpx
from .utils import is_block_page

async def fetch_page(url: str, headers: dict) -> str | None:
    # 1) Playwright
    html = await fetch_via_playwright(url)
    if html and not is_block_page(url, html):
        return html

    # 2) Proxy (ScraperAPI)
    proxy_html = await fetch_via_proxy(url)
    if proxy_html and not is_block_page(url, proxy_html):
        return proxy_html

    # 3) httpx fallback
    http_html = await fetch_via_httpx(url, headers)
    if http_html and not is_block_page(url, http_html):
        return http_html

    return html or proxy_html or http_html
