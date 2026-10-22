#!/usr/bin/env python3
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
import requests
import logging
import json

BSALE_URL = "https://api.bsale.io/v1"
os.environ["BSALE_ACCESS_TOKEN"] = "tutokendeacceso"

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"Missing environment variable {name}")
    return v


# ---------------------------------------------------------------- Bsale
def bsale_get(path, params=None):
    logger.info('calling bsale_get(path='+path+',params...)')
    r = requests.get(f"{BSALE_URL}{path}", params=params,
                     headers={"access_token": env("BSALE_ACCESS_TOKEN")}, timeout=60)
    r.raise_for_status()
    return r.json()


def bsale_stock_by_sku():
    """Return {sku: {name, price?, quantity}} summing stock over all offices."""
    logger.info('calling bsale_stock_by_sku()')
    items, offset = {}, 0
    name_cache = {}
    while True:
        data = bsale_get("/stocks.json", {"limit": 50, "offset": offset, "expand": "[variant]"})
        # json.dump(data, sys.stdout, indent=4)
        # sys.exit(0)
        for s in data.get("items", []):
            v = s.get("variant") or {}
            sku=str(s["id"])
            logstring="sku="+sku

            variantsku = (v.get("code") or "").strip()
            if variantsku: logstring=logstring+", variant.sku="+variantsku

            if "href" in v: logstring=logstring+", variant.href="+v["href"]
            if not sku:
                logger.info(logstring)
                continue
            entry = items.get(sku)
            if entry is None:
                title = v.get("description") or sku
                logstring=logstring+", title="+title
                href = (v.get("product") or {}).get("href")
                if href:
                    logstring=logstring+",href="+href
                    if href not in name_cache:
                        name_cache[href] = bsale_get(href.replace(BSALE_URL, "")).get("name")
                    pname = name_cache[href]
                    if pname:
                        title = pname if title in ("", "Default Title") or title == pname else f"{pname} {title}"
                entry = items[sku] = {"title": title, "quantity": 0}
            entry["quantity"] += int(s.get("quantity") or 0)
            logstring=logstring+", quantity="+str(entry["quantity"])
            logger.info(logstring)
        if not data.get("next"):
            break
        offset += 50
    return items


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
    ap.add_argument("command", choices=["read-stock","create-drafts", "publish-drafts"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-zero-stock", action="store_true",
                    help="also create products whose Bsale stock is 0")
    a = ap.parse_args()
    if a.command == "create-drafts":
        create_drafts(a.dry_run, a.include_zero_stock)
    elif a.command == "publish-drafts":
        publish_drafts(a.dry_run)
    elif a.command == "read-stock":
        items = bsale_stock_by_sku()
        logger.info(str(count(items))+" items in stock")


if __name__ == "__main__":
    main()
