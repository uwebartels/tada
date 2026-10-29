"""Konfiguration: Bsale- und Shopify-Variablen bewusst getrennt."""
import os
import requests

BSALE_API_BASE = os.getenv("BSALE_API_BASE", "https://api.bsale.io/v1").rstrip("/")
BSALE_ACCESS_TOKEN = os.getenv("BSALE_ACCESS_TOKEN", "").strip()
BSALE_PRICE_LIST_ID = os.getenv("BSALE_PRICE_LIST_ID", "").strip()
BSALE_PRICE_LIST_NAME = os.getenv("BSALE_PRICE_LIST_NAME", "").strip()

SHOPIFY_SHOP = os.getenv("SHOPIFY_SHOP", "").strip().replace("https://", "").rstrip("/")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "").strip()
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-10").strip()
SHOPIFY_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID", "").strip()

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "bsale-shopify-draft-import/1.0"})
