#!/usr/bin/env python3
"""
Importiert ausgewählte Bsale-Produkte als Shopify-DRAFTs.

Namenskonvention:
  bsale_*   = Daten / IDs aus Bsale
  shopify_* = Daten / IDs / Antworten aus Shopify
  BSALE_*   = Bsale-Konfiguration
  SHOPIFY_* = Shopify-Konfiguration

Sicherheitsstandard:
  - Ohne --import werden keine Produkte erstellt.
  - Bestehende Shopify-Produkte werden nicht verändert.
  - Neue Produkte werden immer mit status=DRAFT angelegt.
  - Produkt-IDs können mit --product-ids 123,456 eingeschränkt werden.
  - Stock wird nur aus dem mit --office-id ausgewählten Bsale-Office gelesen.
  - Lagerbestand wird standardmäßig nur als Metafield gespeichert.
  - Optional kann Bestand in Shopify gesetzt werden, wenn SHOPIFY_LOCATION_ID
    gesetzt ist und --sync-inventory verwendet wird.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin

import requests

BSALE_API_BASE = os.getenv("BSALE_API_BASE", "https://api.bsale.io/v1").rstrip("/")
BSALE_ACCESS_TOKEN = os.getenv("BSALE_ACCESS_TOKEN", "").strip()
BSALE_PRICE_LIST_ID = os.getenv("BSALE_PRICE_LIST_ID", "").strip()

SHOPIFY_SHOP = os.getenv("SHOPIFY_SHOP", "").strip().replace("https://", "").rstrip("/")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "").strip()
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-10").strip()
SHOPIFY_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID", "").strip()

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "bsale-shopify-draft-import/1.0"})


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).strip()


def relation_id(record: dict[str, Any], relation: str) -> str:
    return as_text((record.get(relation) or {}).get("id"))


def money(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            return None
        return f"{result:.2f}"
    except (InvalidOperation, ValueError):
        return None


class BsaleClient:
    def __init__(self, bsale_access_token: str):
        if not bsale_access_token:
            raise RuntimeError("BSALE_ACCESS_TOKEN fehlt.")
        self.headers = {"access_token": bsale_access_token, "Accept": "application/json"}

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        bsale_url = path_or_url if path_or_url.startswith("http") else f"{BSALE_API_BASE}/{path_or_url.lstrip('/')}"
        bsale_response = HTTP.get(bsale_url, headers=self.headers, params=params, timeout=45)
        if bsale_response.status_code >= 400:
            raise RuntimeError(f"Bsale HTTP {bsale_response.status_code} für {bsale_url}: {bsale_response.text[:800]}")
        return bsale_response.json()

    def list_all(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        bsale_items: list[dict[str, Any]] = []
        bsale_offset = 0
        bsale_params = dict(params or {})
        bsale_params["limit"] = 50
        while True:
            bsale_params["offset"] = bsale_offset
            bsale_page = self.get(path, bsale_params)
            bsale_page_items = bsale_page.get("items", [])
            if not isinstance(bsale_page_items, list):
                raise RuntimeError(f"Unerwartetes Bsale-Listenformat für {path}.")
            bsale_items.extend(x for x in bsale_page_items if isinstance(x, dict))
            bsale_count = int(bsale_page.get("count", len(bsale_items)))
            if not bsale_page_items or len(bsale_items) >= bsale_count or len(bsale_page_items) < 50:
                break
            bsale_offset += len(bsale_page_items)
        return bsale_items

    def products(self, bsale_product_ids: set[str] | None) -> list[dict[str, Any]]:
        if bsale_product_ids:
            bsale_products = []
            for bsale_product_id in sorted(bsale_product_ids):
                bsale_products.append(self.get(f"products/{bsale_product_id}.json"))
            return bsale_products
        return self.list_all("products.json")

    def variants_for_product(self, bsale_product_id: str) -> list[dict[str, Any]]:
        return self.list_all(f"products/{bsale_product_id}/variants.json")

    def stock_for_office(self, bsale_variant_id: str, bsale_office_id: str) -> dict[str, Any] | None:
        bsale_stock_rows = self.list_all(
            "stocks.json",
            {"variantid": bsale_variant_id, "officeid": bsale_office_id},
        )
        for bsale_stock in bsale_stock_rows:
            if relation_id(bsale_stock, "variant") == bsale_variant_id and relation_id(bsale_stock, "office") == bsale_office_id:
                return bsale_stock
        return bsale_stock_rows[0] if bsale_stock_rows else None

    def variant_price(self, bsale_variant: dict[str, Any], bsale_price_list_id: str) -> str | None:
        # Price-list IDs in Bsale are not prices themselves. Follow the variant's
        # prices link, if available, and select the configured list ID.
        bsale_prices_link = bsale_variant.get("prices")
        if isinstance(bsale_prices_link, dict):
            bsale_prices_url = as_text(bsale_prices_link.get("href"))
            if bsale_prices_url:
                bsale_price_data = self.get(bsale_prices_url)
                bsale_price_items = bsale_price_data.get("items", [])
                if isinstance(bsale_price_items, list):
                    for bsale_price_item in bsale_price_items:
                        bsale_item_list_id = relation_id(bsale_price_item, "priceList")
                        if not bsale_item_list_id:
                            bsale_item_list_id = as_text(bsale_price_item.get("priceListId") or bsale_price_item.get("price_list_id"))
                        if not bsale_price_list_id or bsale_item_list_id == bsale_price_list_id:
                            for bsale_price_key in ("price", "amount", "value", "salePrice", "finalPrice"):
                                bsale_variant_price = money(bsale_price_item.get(bsale_price_key))
                                if bsale_variant_price is not None:
                                    return bsale_variant_price
        # Some Bsale endpoints expose a direct price field.
        for bsale_price_key in ("price", "salePrice", "finalPrice"):
            bsale_variant_price = money(bsale_variant.get(bsale_price_key))
            if bsale_variant_price is not None:
                return bsale_variant_price
        return None


class ShopifyAdmin:
    def __init__(self):
        if not SHOPIFY_SHOP:
            raise RuntimeError("SHOPIFY_SHOP fehlt (z. B. mein-shop.myshopify.com).")
        if SHOPIFY_ACCESS_TOKEN:
            shopify_token = SHOPIFY_ACCESS_TOKEN
        else:
            if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
                raise RuntimeError("SHOPIFY_ACCESS_TOKEN oder SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET setzen.")
            shopify_token_response = HTTP.post(
                f"https://{SHOPIFY_SHOP}/admin/oauth/access_token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": SHOPIFY_CLIENT_ID,
                    "client_secret": SHOPIFY_CLIENT_SECRET,
                },
                timeout=30,
            )
            if shopify_token_response.status_code >= 400:
                raise RuntimeError(f"Shopify Token-Fehler {shopify_token_response.status_code}: {shopify_token_response.text[:800]}")
            shopify_token = shopify_token_response.json().get("access_token", "")
            if not shopify_token:
                raise RuntimeError("Shopify hat keinen access_token zurückgegeben.")
        self.headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": shopify_token}
        self.url = f"https://{SHOPIFY_SHOP}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        shopify_response = HTTP.post(
            self.url, headers=self.headers,
            json={"query": query, "variables": variables or {}}, timeout=60,
        )
        if shopify_response.status_code >= 400:
            raise RuntimeError(f"Shopify GraphQL HTTP {shopify_response.status_code}: {shopify_response.text[:1200]}")
        shopify_payload = shopify_response.json()
        if shopify_payload.get("errors"):
            raise RuntimeError(f"Shopify GraphQL errors: {json.dumps(shopify_payload['errors'], ensure_ascii=False)[:1500]}")
        return shopify_payload.get("data", {})

    def existing_skus(self) -> set[str]:
        shopify_skus: set[str] = set()
        shopify_cursor = None
        shopify_query = """
        query ExistingSkus($after: String) {
          products(first: 100, after: $after) {
            pageInfo { hasNextPage endCursor }
            nodes { variants(first: 100) { nodes { sku } } }
          }
        }"""
        while True:
            shopify_data = self.graphql(shopify_query, {"after": shopify_cursor})
            shopify_products = shopify_data["products"]
            for shopify_product in shopify_products["nodes"]:
                for shopify_variant in shopify_product["variants"]["nodes"]:
                    shopify_sku = as_text(shopify_variant.get("sku"))
                    if shopify_sku:
                        shopify_skus.add(shopify_sku)
            shopify_page_info = shopify_products["pageInfo"]
            if not shopify_page_info["hasNextPage"]:
                break
            shopify_cursor = shopify_page_info["endCursor"]
        return shopify_skus

    def create_draft(
        self,
        bsale_product: dict[str, Any],
        bsale_variants: list[dict[str, Any]],
        bsale_prices: dict[str, str | None],
        bsale_stocks: dict[str, dict[str, Any] | None],
        bsale_office_id: str,
        shopify_sync_inventory: bool,
    ) -> dict[str, Any]:
        if not bsale_variants:
            raise ValueError("Produkt ohne Varianten kann nicht importiert werden.")
        shopify_title = as_text(bsale_product.get("name")) or as_text(bsale_product.get("description")) or f"Bsale Produkt {bsale_product.get('id')}"
        shopify_description = as_text(bsale_product.get("description"))
        shopify_options: list[str] = []
        for bsale_variant in bsale_variants:
            shopify_option = as_text(bsale_variant.get("description")) or as_text(bsale_variant.get("code")) or str(bsale_variant.get("id"))
            if shopify_option in shopify_options:
                shopify_option = f"{shopify_option} ({bsale_variant.get('id')})"
            shopify_options.append(shopify_option[:255])

        shopify_product_input = {
            "title": shopify_title[:255],
            "descriptionHtml": "<p>" + (shopify_description.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")) + "</p>" if shopify_description else "",
            "vendor": "Bsale",
            "status": "DRAFT",
            "tags": ["bsale", f"bsale-product-{bsale_product.get('id')}"],
            "productOptions": [{
                "name": "Bsale variant",
                "values": [{"name": shopify_option} for shopify_option in shopify_options],
            }],
            "metafields": [
                {"namespace": "bsale", "key": "product_id", "type": "single_line_text_field", "value": str(bsale_product.get("id", ""))},
                {"namespace": "bsale", "key": "office_id", "type": "single_line_text_field", "value": bsale_office_id},
                {"namespace": "bsale", "key": "product_data", "type": "json", "value": json.dumps(bsale_product, ensure_ascii=False, separators=(",", ":"))[:60000]},
                {"namespace": "bsale", "key": "variants_data", "type": "json", "value": json.dumps(bsale_variants, ensure_ascii=False, separators=(",", ":"))[:60000]},
                {"namespace": "bsale", "key": "stock_data", "type": "json", "value": json.dumps({str(k): v for k, v in bsale_stocks.items()}, ensure_ascii=False, separators=(",", ":"))[:60000]},
            ],
        }
        shopify_create_mutation = """
        mutation CreateBsaleDraft($product: ProductCreateInput!) {
          productCreate(product: $product) {
            product { id title status variants(first: 1) { nodes { id } } }
            userErrors { field message }
          }
        }"""
        shopify_create_response = self.graphql(shopify_create_mutation, {"product": shopify_product_input})["productCreate"]
        if shopify_create_response.get("userErrors"):
            raise RuntimeError(f"productCreate Fehler: {json.dumps(shopify_create_response['userErrors'], ensure_ascii=False)}")
        shopify_product = shopify_create_response.get("product")
        if not shopify_product:
            raise RuntimeError("Shopify hat kein Produkt zurückgegeben.")
        shopify_initial_variant = shopify_product["variants"]["nodes"][0]

        shopify_variant_inputs = []
        for bsale_index, bsale_variant in enumerate(bsale_variants):
            shopify_variant_price = bsale_prices.get(str(bsale_variant.get("id")))
            if shopify_variant_price is None:
                raise RuntimeError(
                    f"Kein Preis für Bsale-Variante {bsale_variant.get('id')}. "
                    "Bitte BSALE_PRICE_LIST_ID prüfen bzw. Preis-Endpunkt im Bsale-Konto kontrollieren."
                )
            shopify_variant_input: dict[str, Any] = {
                "price": shopify_variant_price,
                "inventoryItem": {
                    "sku": as_text(bsale_variant.get("code")) or f"bsale-{bsale_variant.get('id')}",
                    "tracked": bool(shopify_sync_inventory),
                },
            }
            bsale_barcode = as_text(bsale_variant.get("barCode"))
            if bsale_barcode:
                shopify_variant_input["barcode"] = bsale_barcode
            if bsale_index == 0:
                shopify_variant_input["id"] = shopify_initial_variant["id"]
            else:
                shopify_variant_input["optionValues"] = [{
                    "optionName": "Bsale variant", "name": shopify_options[bsale_index]
                }]
            shopify_variant_inputs.append(shopify_variant_input)

        shopify_variant_mutation = """
        mutation SetBsaleVariants($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants { id sku price inventoryItem { id tracked } }
            userErrors { field message }
          }
        }"""
        # Shopify bulkUpdate only updates existing variants; create any remaining variants separately.
        shopify_first_variant_response = self.graphql(shopify_variant_mutation, {
            "productId": shopify_product["id"], "variants": [shopify_variant_inputs[0]]
        })["productVariantsBulkUpdate"]
        if shopify_first_variant_response.get("userErrors"):
            raise RuntimeError(f"Shopify Varianten-Fehler: {json.dumps(shopify_first_variant_response['userErrors'], ensure_ascii=False)}")

        if len(shopify_variant_inputs) > 1:
            shopify_bulk_create_mutation = """
            mutation AddBsaleVariants($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
              productVariantsBulkCreate(productId: $productId, variants: $variants) {
                productVariants { id sku price inventoryItem { id tracked } }
                userErrors { field message }
              }
            }"""
            shopify_create_variants_response = self.graphql(shopify_bulk_create_mutation, {
                "productId": shopify_product["id"], "variants": shopify_variant_inputs[1:]
            })["productVariantsBulkCreate"]
            if shopify_create_variants_response.get("userErrors"):
                raise RuntimeError(f"Shopify Varianten-Erstellung fehlgeschlagen: {json.dumps(shopify_create_variants_response['userErrors'], ensure_ascii=False)}")
            shopify_all_variant_results = shopify_first_variant_response.get("productVariants", []) + shopify_create_variants_response.get("productVariants", [])
        else:
            shopify_all_variant_results = shopify_first_variant_response.get("productVariants", [])

        if shopify_sync_inventory:
            if not SHOPIFY_LOCATION_ID:
                raise RuntimeError("--sync-inventory erfordert SHOPIFY_LOCATION_ID.")
            for bsale_variant, shopify_variant in zip(bsale_variants, shopify_all_variant_results):
                bsale_stock = bsale_stocks.get(str(bsale_variant.get("id")))
                if bsale_stock is None:
                    log(f"WARNUNG: Kein Stock für Bsale-Variante {bsale_variant.get('id')} in Office {bsale_office_id}; Bestand wird nicht gesetzt.")
                    continue
                shopify_inventory_item_id = (shopify_variant.get("inventoryItem") or {}).get("id")
                if not shopify_inventory_item_id:
                    continue
                shopify_quantity = bsale_stock.get("quantityAvailable", bsale_stock.get("quantity"))
                if shopify_quantity is None:
                    continue
                shopify_quantity = int(Decimal(str(shopify_quantity)))
                shopify_inventory_mutation = """
                mutation SetInventory($input: InventorySetQuantitiesInput!) {
                  inventorySetQuantities(input: $input) {
                    inventoryAdjustmentGroup { id }
                    userErrors { field message }
                  }
                }"""
                shopify_inventory_response = self.graphql(shopify_inventory_mutation, {
                    "input": {
                        "name": "available",
                        "reason": "correction",
                        "referenceDocumentUri": f"bsale://office/{bsale_office_id}/variant/{bsale_variant.get('id')}",
                        "quantities": [{
                            "inventoryItemId": shopify_inventory_item_id,
                            "locationId": SHOPIFY_LOCATION_ID,
                            "quantity": shopify_quantity,
                        }],
                    }
                })["inventorySetQuantities"]
                if shopify_inventory_response.get("userErrors"):
                    raise RuntimeError(f"Shopify Bestand konnte nicht gesetzt werden: {json.dumps(shopify_inventory_response['userErrors'], ensure_ascii=False)}")

        return {"id": shopify_product["id"], "title": shopify_product["title"], "status": shopify_product["status"]}


def parse_ids(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {item.strip() for item in value.split(",") if item.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Bsale-Produkte als Shopify-DRAFT importieren.")
    parser.add_argument("--product-ids", help="Kommagetrennte Bsale-Produkt-IDs, z. B. 123,456. Ohne Angabe alle Produkte.")
    parser.add_argument("--office-id", required=True, help="Bsale Office-/Sucursal-ID, aus der Stock gelesen wird.")
    parser.add_argument("--import", dest="do_import", action="store_true", help="Import tatsächlich ausführen; ohne Flag nur Vorschau.")
    parser.add_argument("--sync-inventory", action="store_true", help="Stock aus ausgewähltem Bsale-Office in Shopify-Bestand schreiben. Erfordert --import und SHOPIFY_LOCATION_ID.")
    parser.add_argument("--allow-missing-prices", action="store_true", help="Drafts trotz fehlender Preise mit 0.00 anlegen (nur für Tests).")
    parser.add_argument("--report", default="bsale_shopify_import_report.csv", help="Pfad der CSV-Vorschau/des Importberichts.")
    args = parser.parse_args()

    if not args.office_id.strip():
        parser.error("--office-id darf nicht leer sein.")
    if args.sync_inventory and not args.do_import:
        parser.error("--sync-inventory darf nur zusammen mit --import verwendet werden.")

    bsale_client = BsaleClient(BSALE_ACCESS_TOKEN)
    bsale_product_ids = parse_ids(args.product_ids)
    bsale_products = bsale_client.products(bsale_product_ids)
    log(f"Bsale-Produkte geladen: {len(bsale_products)}")
    shopify_admin = ShopifyAdmin()
    shopify_existing_skus = shopify_admin.existing_skus()

    shopify_report_rows = []
    shopify_created_count = 0
    for bsale_product in bsale_products:
        bsale_product_id = as_text(bsale_product.get("id"))
        if bsale_product_ids and bsale_product_id not in bsale_product_ids:
            continue
        bsale_variants = bsale_client.variants_for_product(bsale_product_id)
        if not bsale_variants:
            shopify_report_rows.append({"bsale_product_id": bsale_product_id, "title": as_text(bsale_product.get("name")), "status": "SKIP_NO_VARIANTS", "notes": "Keine Varianten in Bsale."})
            continue

        bsale_prices: dict[str, str | None] = {}
        bsale_stocks: dict[str, dict[str, Any] | None] = {}
        bsale_skus = []
        for bsale_variant in bsale_variants:
            bsale_variant_id = as_text(bsale_variant.get("id"))
            bsale_prices[bsale_variant_id] = bsale_client.variant_price(bsale_variant, BSALE_PRICE_LIST_ID)
            bsale_stocks[bsale_variant_id] = bsale_client.stock_for_office(bsale_variant_id, args.office_id)
            bsale_sku = as_text(bsale_variant.get("code"))
            if bsale_sku:
                bsale_skus.append(bsale_sku)

        shopify_existing_match = next((sku for sku in bsale_skus if sku in shopify_existing_skus), None)
        if shopify_existing_match:
            shopify_report_rows.append({
                "bsale_product_id": bsale_product_id, "title": as_text(bsale_product.get("name")),
                "variant_ids": ";".join(as_text(v.get("id")) for v in bsale_variants),
                "skus": ";".join(bsale_skus), "office_id": args.office_id,
                "status": "SKIP_EXISTS", "notes": f"SKU {shopify_existing_match} existiert bereits in Shopify; nicht verändert.",
            })
            continue

        shopify_missing_price_ids = [variant_id for variant_id, price in bsale_prices.items() if price is None]
        shopify_missing_stock_ids = [variant_id for variant_id, stock in bsale_stocks.items() if stock is None]
        shopify_notes = []
        if shopify_missing_price_ids:
            shopify_notes.append("Preis fehlt für Varianten: " + ",".join(shopify_missing_price_ids))
        if shopify_missing_stock_ids:
            shopify_notes.append("Kein Stock-Datensatz im ausgewählten Office für Varianten: " + ",".join(shopify_missing_stock_ids))

        shopify_status = "PREVIEW_ONLY"
        if args.do_import:
            if shopify_missing_price_ids and not args.allow_missing_prices:
                shopify_status = "SKIP_NO_PRICE"
                shopify_notes.append("Import gestoppt; für Testzwecke --allow-missing-prices verwenden.")
            else:
                if shopify_missing_price_ids:
                    bsale_prices = {k: (v if v is not None else "0.00") for k, v in bsale_prices.items()}
                shopify_create_result = shopify_admin.create_draft(
                    bsale_product, bsale_variants, bsale_prices, bsale_stocks, args.office_id, args.sync_inventory
                )
                shopify_created_count += 1
                shopify_status = "CREATED_DRAFT"
                shopify_notes.append("Als DRAFT angelegt.")
                if args.sync_inventory:
                    shopify_notes.append("Shopify-Bestand aus ausgewähltem Bsale-Office gesetzt.")

        shopify_report_rows.append({
            "bsale_product_id": bsale_product_id,
            "title": as_text(bsale_product.get("name")),
            "variant_ids": ";".join(as_text(v.get("id")) for v in bsale_variants),
            "skus": ";".join(bsale_skus),
            "office_id": args.office_id,
            "prices_missing": ";".join(shopify_missing_price_ids),
            "stock_missing": ";".join(shopify_missing_stock_ids),
            "status": shopify_status,
            "shopify_product_id": shopify_create_result.get("id", "") if args.do_import and shopify_status == "CREATED_DRAFT" else "",
            "notes": " ".join(shopify_notes) or "OK",
        })

    import csv
    shopify_report_path = os.path.abspath(args.report)
    with open(shopify_report_path, "w", newline="", encoding="utf-8-sig") as shopify_report_file:
        shopify_fieldnames = ["bsale_product_id", "title", "variant_ids", "skus", "office_id", "prices_missing", "stock_missing", "status", "shopify_product_id", "notes"]
        shopify_writer = csv.DictWriter(shopify_report_file, fieldnames=shopify_fieldnames, extrasaction="ignore")
        shopify_writer.writeheader()
        shopify_writer.writerows(shopify_report_rows)
    log(f"Bericht: {shopify_report_path}")
    log(f"Neue Draft-Produkte: {shopify_created_count}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        log(f"FEHLER: {error}")
        raise SystemExit(1)
