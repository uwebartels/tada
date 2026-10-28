#!/usr/bin/env python3 -u
"""Bsale -> Shopify product sync. SKU (Bsale variant code) is the identifier.

Commands:
  create-drafts   Create products found in Bsale (with stock) but missing in Shopify, as drafts.
  publish-drafts  Set every Shopify draft product to active.
Use --dry-run to preview without writing.

Environment variables:
  BSALE_ACCESS_TOKEN, SHOPIFY_STORE (e.g. my-store.myshopify.com),
  SHOPIFY_ACCESS_TOKEN, SHOPIFY_API_VERSION (default 2024-10),
  SHOPIFY_LOCATION_ID (optional; default: first location, used to set stock)
"""
import argparse
import os
import sys
import time
import re
import requests
import logging
import json

loglevel=logging.INFO
logger = logging.getLogger()
logger.setLevel(loglevel)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(loglevel)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

from lib import *
from bsale import *

def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"Missing environment variable {name}")
    return v




# -------------------------------------------------------------- Shopify
class Shopify:
    def __init__(self):
        self.base = f"https://{env('SHOPIFY_STORE')}/admin/api/{env('SHOPIFY_API_VERSION', '2024-10')}"
        self.s = requests.Session()
        self.s.headers["X-Shopify-Access-Token"] = env("SHOPIFY_ACCESS_TOKEN")

    def req(self, method, url, **kw):
        if not url.startswith("http"):
            url = self.base + url
        while True:
            r = self.s.request(method, url, timeout=60, **kw)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2)))
                continue
            r.raise_for_status()
            return r

    def paged(self, path, key, params):
        url = path
        while url:
            r = self.req("GET", url, params=params)
            yield from r.json()[key]
            params = None  # next link already carries the query
            url = r.links.get("next", {}).get("url")

    def products(self, **params):
        return self.paged("/products.json", "products", {"limit": 250, **params})

    def existing_skus(self):
        skus = set()
        for p in self.products(fields="id,variants"):
            skus.update((v.get("sku") or "").strip() for v in p["variants"])
        skus.discard("")
        return skus

    def location_id(self):
        loc = os.environ.get("SHOPIFY_LOCATION_ID")
        if loc:
            return int(loc)
        return self.req("GET", "/locations.json").json()["locations"][0]["id"]

    def create_draft(self, sku, title, quantity, location_id):
        body = {"product": {"title": title, "status": "draft", "variants": [
            {"sku": sku, "inventory_management": "shopify"}]}}
        p = self.req("POST", "/products.json", json=body).json()["product"]
        inv = p["variants"][0]["inventory_item_id"]
        self.req("POST", "/inventory_levels/set.json", json={
            "location_id": location_id, "inventory_item_id": inv, "available": max(quantity, 0)})
        return p["id"]

    def publish(self, product_id):
        self.req("PUT", f"/products/{product_id}.json",
                 json={"product": {"id": product_id, "status": "active"}})


# ------------------------------------------------------------- commands
def create_drafts(dry_run, include_zero):
    shop = Shopify()
    stock = bsale_stock_by_sku()
    existing = shop.existing_skus()
    new = {k: v for k, v in stock.items()
           if k not in existing and (include_zero or v["quantity"] > 0)}
    print(f"Bsale SKUs: {len(stock)} | Shopify SKUs: {len(existing)} | to create: {len(new)}")
    loc = None if dry_run or not new else shop.location_id()
    for sku, d in sorted(new.items()):
        if dry_run:
            print(f"[dry-run] would create draft {sku} '{d['title']}' qty={d['quantity']}")
            continue
        try:
            pid = shop.create_draft(sku, d["title"], d["quantity"], loc)
            print(f"created draft {sku} (product {pid})")
        except requests.HTTPError as e:
            print(f"ERROR {sku}: {e} {e.response.text[:200]}", file=sys.stderr)


def publish_drafts(dry_run):
    shop = Shopify()
    drafts = list(shop.products(status="draft", fields="id,title"))
    print(f"draft products: {len(drafts)}")
    for p in drafts:
        if dry_run:
            print(f"[dry-run] would activate {p['id']} '{p['title']}'")
            continue
        try:
            shop.publish(p["id"])
            print(f"activated {p['id']} '{p['title']}'")
        except requests.HTTPError as e:
            print(f"ERROR {p['id']}: {e}", file=sys.stderr)


def main():
    logger.info('calling main()')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["read-catalog","create-drafts", "publish-drafts"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-zero-stock", action="store_true",
                    help="also create products whose Bsale stock is 0")
    a = ap.parse_args()
    if a.command == "create-drafts":
        create_drafts(a.dry_run, a.include_zero_stock)
    elif a.command == "publish-drafts":
        publish_drafts(a.dry_run)
    elif a.command == "read-catalog":
        # data = bsale_get("/products/24223.json")
        # json.dump(data, sys.stdout, indent=4)
        # sys.exit(0)

        # examples
        #bsale_get_objects("offices",{"name": "La Reina"})
        #bsale_get_objects("stocks",{"officeid": "1"})
        #bsale_get_objects("offices",{"id": "1"})
        bsale_get_objects("variants",{"expand": "[prices,discounts,costs,attribute_values]"})

        sys.exit(0)

        bsale_get_objects("coins")
        bsale_get_objects("product_types")
        bsale_get_objects("price_lists")
        bsale_get_objects("products",{"expand": "[product_taxes]"})
        bsale_get_objects("variants")
        bsale_get_objects("offices")
        # retrieve stock from only one office to reduce mass data download
        bsale_get_objects("stocks",{"officeid": "1"})


if __name__ == "__main__":
    main()
