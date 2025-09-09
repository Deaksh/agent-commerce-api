import asyncio
from playwright.async_api import async_playwright


async def fetch_page(url: str) -> str:
    """Fetch HTML of a page using Playwright."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, timeout=60000)
        content = await page.content()
        await browser.close()
        return content


# Run standalone for testing
if __name__ == "__main__":
    test_url = "https://demo.vercel.store/product/t-shirt"
    html = asyncio.run(fetch_page(test_url))
    print(html[:500])
