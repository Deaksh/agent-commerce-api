from bs4 import BeautifulSoup
import json, re, logging
from .utils import clean_price

log = logging.getLogger("agent-commerce")

def extract_product_info(html: str, url: str) -> dict:
    soup = BeautifulSoup(html or "", "lxml")
    product = {"name": None, "price": None, "currency": None, "availability": None}
    # your existing parsing logic (Amazon, Flipkart, Myntra, fallback) goes here
    return product
