#!/usr/bin/env python3
"""
Bsale -> Shopify Development Store product comparison/import.

Safety defaults:
- Dry run by default; no Shopify products are created unless IMPORT_ENABLED=true.
- Existing Shopify products are never modified.
- Import is blocked when Bsale product/variant exports are materially incomplete.
- Missing prices block import unless ALLOW_ZERO_PRICE_DRAFTS=true.
- New products are created with status DRAFT.
- Bsale source records are retained in JSON metafields where possible.
- Inventory is NOT changed; Bsale stock/office data is preserved as metadata.

Expected input files in the same directory:
  products.json, variants.json, offices.json, price_lists.json, stocks.json

Naming convention in this script:
  bsale_*   = values originating from Bsale exports/API
  shopify_* = Shopify Admin API/client/configuration/results
  *_input  = payload sent to Shopify; *_result = Shopify API response

Environment variables:
  SHOPIFY_SHOP=your-store.myshopify.com
  SHOPIFY_CLIENT_ID=...
  SHOPIFY_CLIENT_SECRET=...
  SHOPIFY_API_VERSION=2026-10
  IMPORT_ENABLED=false
  ALLOW_INCOMPLETE_EXPORT=false
  ALLOW_ZERO_PRICE_DRAFTS=false
  BSALE_PRICE_LIST_ID=       # optional: choose a price list ID after adding its details export
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-10")
SHOPIFY_SHOP_DOMAIN = os.getenv("SHOPIFY_SHOP", "").strip().replace("https://", "").rstrip("/")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "").strip()
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()
SHOPIFY_IMPORT_ENABLED = os.getenv("IMPORT_ENABLED", "false").lower() == "true"
BSALE_ALLOW_INCOMPLETE_EXPORT = os.getenv("ALLOW_INCOMPLETE_EXPORT", "false").lower() == "true"
SHOPIFY_ALLOW_ZERO_PRICE_DRAFTS = os.getenv("ALLOW_ZERO_PRICE_DRAFTS", "false").lower() == "true"
BSALE_PRICE_LIST_ID = os.getenv("BSALE_PRICE_LIST_ID", "").strip()

BSALE_EXPORT_FILES = {
    "products": "products.json",
    "variants": "variants.json",
    "offices": "offices.json",
    "price_lists": "price_lists.json",
    "stocks": "stocks.json",
}
REPORT_PATH = SCRIPT_DIR / "bsale_shopify_comparison.csv"
LOG_PATH = SCRIPT_DIR / "bsale_shopify_sync.log"

session = requests.Session()


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a JSON object keyed by record ID.")
    return data


def load_bsale_exports() -> dict[str, dict[str, Any]]:
    result = {}
    for key, filename in BSALE_EXPORT_FILES.items():
        result[key] = load_json_file(SCRIPT_DIR / filename)
        log(f"Loaded {filename}: {len(result[key])} records")
    return result


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).strip()


def clean_sku(value: Any) -> str:
    return text_value(value).strip()


def json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def validate_exports(data: dict[str, dict[str, Any]]) -> tuple[dict[str, list[dict]], list[str]]:
    bsale_products = bsale_exports["products"]
    bsale_variants = bsale_exports["variants"]
    bsale_stocks = data["stocks"]
    bsale_offices = data["offices"]
    bsale_price_lists = data["price_lists"]
    errors: list[str] = []

    product_ids = {str(k) for k in products}
    variant_ids = {str(k) for k in variants}

    variants_by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_parent = []
    for variant_id, variant in variants.items():
        parent_id = text_value((variant.get("product") or {}).get("id"))
        if parent_id in product_ids:
            variant_copy = dict(variant)
            variant_copy["_bsale_variant_id"] = str(variant_id)
            variants_by_product[parent_id].append(variant_copy)
        else:
            missing_parent.append((str(variant_id), parent_id))

    missing_stock_variant = 0
    for stock in bsale_stocks.values():
        v_id = text_value((stock.get("variant") or {}).get("id"))
        if v_id not in variant_ids:
            missing_stock_variant += 1

    log(
        f"Export linkage: {len(variants) - len(missing_parent)}/{len(variants)} "
        f"variants link to an exported product; {len(missing_parent)} parent IDs missing."
    )
    if missing_parent:
        sample = ", ".join(f"variant {v}->product {p}" for v, p in missing_parent[:8])
        errors.append(
            f"{len(missing_parent)} of {len(variants)} variants refer to product IDs absent "
            f"from products.json. Examples: {sample}"
        )
    if missing_stock_variant:
        log(f"Warning: {missing_stock_variant} stock records refer to variants absent from variants.json.")

    # The supplied price_lists.json contains list metadata, not per-variant prices.
    # Variant records contain links to price endpoints, but no actual price values.
    errors.append(
        "No per-variant price details were provided: variants.json contains only a prices URL, "
        "and price_lists.json contains price-list headers. Export the price-list details before "
        "a price-accurate import, or explicitly enable zero-price draft import."
    )

    log(
        f"Other reference data: {len(bsale_offices)} offices, {len(bsale_price_lists)} price lists, "
        f"{len(bsale_stocks)} stock records."
    )
    return dict(variants_by_product), errors


def get_access_token() -> str:
    if not (SHOPIFY_SHOP_DOMAIN and SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET):
        raise RuntimeError(
            "Set SHOPIFY_SHOP, SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET first."
        )
    url = f"https://{SHOPIFY_SHOP_DOMAIN}/admin/oauth/access_token"
    response = session.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Shopify token request failed ({response.status_code}): {response.text[:1000]}")
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in Shopify response: {payload}")
    return token


class ShopifyAdmin:
    def __init__(self, token: str):
        self.url = f"https://{SHOPIFY_SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
        self.token = token

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = session.post(
            self.url,
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": self.token,
            },
            json={"query": query, "variables": variables or {}},
            timeout=60,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Shopify GraphQL HTTP {response.status_code}: {response.text[:2000]}")
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(f"Shopify GraphQL errors: {json_compact(payload['errors'])}")
        return payload.get("data", {})

    def all_variants(self) -> tuple[dict[str, dict], dict[str, dict]]:
        """Return SKU -> Shopify product/variant and barcode -> Shopify product/variant indexes."""
        shopify_variants_by_sku: dict[str, dict] = {}
        shopify_variants_by_barcode: dict[str, dict] = {}
        query = """
        query ProductsAndVariants($after: String) {
          products(first: 100, after: $after) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              title
              status
              variants(first: 100) {
                nodes { id sku barcode title }
              }
            }
          }
        }
        """
        shopify_page_cursor = None
        while True:
            shopify_response_data = self.graphql(query, {"after": shopify_page_cursor})
            shopify_products_connection = shopify_response_data["products"]
            for shopify_product in shopify_products_connection["nodes"]:
                for shopify_variant in shopify_product["variants"]["nodes"]:
                    shopify_product_variant_record = {"product": shopify_product, "variant": shopify_variant}
                    sku = clean_sku(shopify_variant.get("sku"))
                    barcode = clean_sku(shopify_variant.get("barcode"))
                    if sku:
                        shopify_variants_by_sku.setdefault(sku, shopify_product_variant_record)
                    if barcode:
                        shopify_variants_by_barcode.setdefault(barcode, shopify_product_variant_record)
            shopify_page_info = shopify_products_connection["pageInfo"]
            if not shopify_page_info["hasNextPage"]:
                break
            shopify_page_cursor = shopify_page_info["endCursor"]
        log(f"Shopify lookup loaded {len(shopify_variants_by_sku)} distinct SKUs and {len(shopify_variants_by_barcode)} distinct barcodes.")
        return shopify_variants_by_sku, shopify_variants_by_barcode

    def create_draft(self, bsale_product: dict[str, Any], bsale_variants: list[dict[str, Any]],
                     bsale_stock_records: list[dict[str, Any]], bsale_office_map: dict[str, Any],
                     bsale_price_lists: dict[str, Any]) -> dict[str, Any]:
        """Create a draft product and preserve source JSON in product metafields."""
        if not bsale_variants:
            raise ValueError("Cannot create product without linked Bsale variants.")

        shopify_product_title = (
            text_value(bsale_product.get("name"))
            or text_value(bsale_product.get("description"))
            or text_value(bsale_variants[0].get("description"))
            or f"Bsale product {bsale_product.get('id', '')}"
        )
        bsale_product_description = text_value(bsale_product.get("description"))
        if bsale_product_description:
            shopify_description_html = "<p>" + (
                bsale_product_description.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "<br>")
            ) + "</p>"
        else:
            shopify_description_html = ""

        # Bsale export does not contain option attribute values. To avoid inventing size/color
        # options, variants are represented with a single option named "Bsale variant".
        # Variant label is description, then code, then Bsale ID.
        shopify_variant_option_labels = []
        shopify_seen_option_labels = set()
        for bsale_variant in bsale_variants:
            shopify_variant_option_label = text_value(bsale_variant.get("description")) or clean_sku(bsale_variant.get("code")) or str(bsale_variant["_bsale_variant_id"])
            if shopify_variant_option_label in shopify_seen_option_labels:
                shopify_variant_option_label = f"{shopify_variant_option_label} ({bsale_variant['_bsale_variant_id']})"
            shopify_seen_option_labels.add(shopify_variant_option_label)
            shopify_variant_option_labels.append(shopify_variant_option_label)

        # Create product in DRAFT status. Shopify productCreate creates its initial variant.
        shopify_create_mutation = """
        mutation CreateBsaleDraft($product: ProductCreateInput!) {
          productCreate(product: $product) {
            product {
              id
              title
              status
              variants(first: 1) {
                nodes { id sku barcode price selectedOptions { name value } }
              }
            }
            userErrors { field message }
          }
        }
        """
        shopify_product_input: dict[str, Any] = {
            "title": shopify_product_title[:255],
            "status": "DRAFT",
            "vendor": "Bsale",
            "descriptionHtml": shopify_description_html,
            "tags": ["bsale", f"bsale-product-{bsale_product.get('id', '')}"],
            "productOptions": [{
                "name": "Bsale variant",
                "values": [{"name": shopify_variant_option_label[:255]} for shopify_variant_option_label in shopify_variant_option_labels],
            }],
            "metafields": [
                {
                    "namespace": "bsale",
                    "key": "product_id",
                    "type": "single_line_text_field",
                    "value": str(bsale_product.get("id", "")),
                },
                {
                    "namespace": "bsale",
                    "key": "product_data",
                    "type": "json",
                    "value": json_compact(bsale_product),
                },
                {
                    "namespace": "bsale",
                    "key": "variants_data",
                    "type": "json",
                    "value": json_compact(bsale_variants),
                },
                {
                    "namespace": "bsale",
                    "key": "stock_data",
                    "type": "json",
                    "value": json_compact(bsale_stock_records),
                },
                {
                    "namespace": "bsale",
                    "key": "offices_data",
                    "type": "json",
                    "value": json_compact(bsale_office_map),
                },
                {
                    "namespace": "bsale",
                    "key": "price_lists_data",
                    "type": "json",
                    "value": json_compact(bsale_price_lists),
                },
            ],
        }
        # Shopify metafields have size limits; fail clearly rather than silently losing source data.
        for shopify_metafield in shopify_product_input["metafields"]:
            if len(shopify_metafield["value"].encode("utf-8")) > 60_000:
                raise ValueError(f"Metafield {shopify_metafield['key']} exceeds safe 60 KB size limit; split data into smaller metafields.")

        shopify_create_response = self.graphql(shopify_create_mutation, {"product": shopify_product_input})["productCreate"]
        if shopify_create_response["userErrors"]:
            raise RuntimeError(f"productCreate userErrors: {json_compact(shopify_create_response['userErrors'])}")
        shopify_created_product = shopify_create_response["product"]
        if not shopify_created_product:
            raise RuntimeError("Shopify did not return a created product.")

        shopify_initial_variant = shopify_created_product["variants"]["nodes"][0]
        # Set the first variant's SKU/barcode and price. If price isn't present in the exports,
        # zero-price import is only allowed with ALLOW_ZERO_PRICE_DRAFTS=true.
        bsale_first_variant = bsale_variants[0]
        shopify_variant_price = get_variant_price(bsale_first_variant, bsale_price_lists)
        if shopify_variant_price is None:
            if not SHOPIFY_ALLOW_ZERO_PRICE_DRAFTS:
                raise RuntimeError(
                    "Price unavailable. Import blocked. Export price-list detail records or set "
                    "ALLOW_ZERO_PRICE_DRAFTS=true to create a DRAFT with price 0.00."
                )
            shopify_variant_price = "0.00"

        shopify_update_initial_variant_mutation = """
        mutation UpdateInitialVariant($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants { id sku barcode price }
            userErrors { field message }
          }
        }
        """
        shopify_first_variant_input: dict[str, Any] = {
            "id": shopify_initial_variant["id"],
            "price": shopify_variant_price,
            "inventoryItem": {"sku": clean_sku(bsale_first_variant.get("code")) or None},
        }
        if clean_sku(bsale_first_variant.get("barCode")):
            shopify_first_variant_input["barcode"] = clean_sku(bsale_first_variant.get("barCode"))
        shopify_updated_variant_response = self.graphql(
            shopify_update_initial_variant_mutation,
            {"productId": shopify_created_product["id"], "variants": [shopify_first_variant_input]},
        )["productVariantsBulkUpdate"]
        if shopify_updated_variant_response["userErrors"]:
            raise RuntimeError(f"Could not update initial variant: {json_compact(shopify_updated_variant_response['userErrors'])}")

        # Create remaining variants with the same single Bsale variant option.
        if len(bsale_variants) > 1:
            shopify_remaining_variant_inputs = []
            for bsale_variant, shopify_variant_option_label in zip(bsale_variants[1:], shopify_variant_option_labels[1:]):
                shopify_variant_price = get_variant_price(bsale_variant, bsale_price_lists)
                if shopify_variant_price is None:
                    if not SHOPIFY_ALLOW_ZERO_PRICE_DRAFTS:
                        raise RuntimeError(
                            f"Price unavailable for Bsale variant {bsale_variant['_bsale_variant_id']}. "
                            "Import blocked. Export price-list details or explicitly allow zero-price drafts."
                        )
                    shopify_variant_price = "0.00"
                shopify_variant_input = {
                    "price": shopify_variant_price,
                    "optionValues": [{"optionName": "Bsale variant", "name": shopify_variant_option_label[:255]}],
                    "inventoryItem": {"sku": clean_sku(bsale_variant.get("code")) or None},
                }
                if clean_sku(bsale_variant.get("barCode")):
                    shopify_variant_input["barcode"] = clean_sku(bsale_variant.get("barCode"))
                shopify_remaining_variant_inputs.append(shopify_variant_input)

            shopify_bulk_create_variants_mutation = """
            mutation AddBsaleVariants($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
              productVariantsBulkCreate(productId: $productId, variants: $variants) {
                productVariants { id sku barcode price }
                userErrors { field message }
              }
            }
            """
            shopify_created_variants_response = self.graphql(
                shopify_bulk_create_variants_mutation,
                {"productId": shopify_created_product["id"], "variants": shopify_remaining_variant_inputs},
            )["productVariantsBulkCreate"]
            if shopify_created_variants_response["userErrors"]:
                raise RuntimeError(f"Could not create variants: {json_compact(shopify_created_variants_response['userErrors'])}")

        return {"id": shopify_created_product["id"], "title": shopify_created_product["title"], "status": shopify_created_product["status"]}


def get_variant_price(bsale_variant: dict[str, Any], bsale_price_lists: dict[str, Any]) -> str | None:
    """
    This snapshot's variants contain only a URL under 'prices', not price values.
    This function intentionally returns None unless a future enriched export includes a
    numeric price or a `price` field. Do not infer price from a price-list header.
    """
    if bsale_variant.get("price") is not None:
        try:
            return f"{float(bsale_variant['price']):.2f}"
        except (TypeError, ValueError):
            pass
    prices = bsale_variant.get("prices")
    if isinstance(prices, dict):
        for key in ("price", "amount", "value"):
            if prices.get(key) is not None:
                try:
                    return f"{float(prices[key]):.2f}"
                except (TypeError, ValueError):
                    pass
    # price_lists.json has names/IDs but not variant-specific price values.
    return None


def make_stock_indexes(bsale_exports: dict[str, dict[str, Any]]) -> tuple[dict[str, list[dict]], dict[str, Any]]:
    bsale_stocks_by_variant: dict[str, list[dict]] = defaultdict(list)
    for bsale_stock_id, bsale_stock in bsale_exports["stocks"].items():
        bsale_variant_id = text_value((bsale_stock.get("variant") or {}).get("id"))
        bsale_office_id = text_value((bsale_stock.get("office") or {}).get("id"))
        bsale_stock_row = dict(bsale_stock)
        bsale_stock_row["_bsale_stock_id"] = str(bsale_stock_id)
        bsale_stock_row["_office_id"] = bsale_office_id
        bsale_stocks_by_variant[bsale_variant_id].append(bsale_stock_row)
    return bsale_stocks_by_variant, bsale_exports["offices"]


def write_report(rows: list[dict[str, Any]]) -> None:
    fields = [
        "bsale_product_id", "bsale_product_title", "bsale_variant_ids", "skus",
        "barcodes", "status", "shopify_product_id", "shopify_product_title", "notes",
    ]
    with REPORT_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log(f"Comparison report written: {REPORT_PATH}")


def main() -> int:
    # Clear old log for this run.
    LOG_PATH.write_text("", encoding="utf-8")
    bsale_exports = load_bsale_exports()
    bsale_variants_by_product, validation_errors = validate_exports(bsale_exports)
    bsale_stocks_by_variant, bsale_office_map = make_stock_indexes(bsale_exports)

    # delete all variants except those with product id: 24223
    product_ids = keys(bsale_variants_by_product)
    for id in product_ids:
        del bsale_variants_by_product[id]

    # If credentials are configured, compare against Shopify even in compare-only mode.
    # SHOPIFY_IMPORT_ENABLED controls creation only.
    if SHOPIFY_SHOP_DOMAIN and SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET:
        token = get_access_token()
        shopify_admin = ShopifyAdmin(token)
        shopify_variants_by_sku, shopify_variants_by_barcode = shopify_admin.all_variants()
    else:
        shopify_admin = None
        shopify_variants_by_sku, shopify_variants_by_barcode = {}, {}
        log("Shopify credentials not set: Bsale-only report; Shopify comparison skipped.")
    if not SHOPIFY_IMPORT_ENABLED:
        log("COMPARE-ONLY MODE: no Shopify products will be created or changed.")

    rows: list[dict[str, Any]] = []
    bsale_products = bsale_exports["products"]
    bsale_variants = bsale_exports["variants"]

    # Count parent mismatches. By default, do not import from an incomplete joined dataset.
    total_variants = len(bsale_variants)
    linked_variant_count = sum(len(v) for v in bsale_variants_by_product.values())
    missing_ratio = (total_variants - linked_variant_count) / max(total_variants, 1)
    if SHOPIFY_IMPORT_ENABLED and missing_ratio > 0.05 and not BSALE_ALLOW_INCOMPLETE_EXPORT:
        raise RuntimeError(
            f"Import stopped for safety: {missing_ratio:.1%} of variants point to products absent "
            "from products.json. Re-export complete/paginated Bsale data. Only set "
            "ALLOW_INCOMPLETE_EXPORT=true if you have checked the relationships."
        )

    imported = 0
    for bsale_product_id, bsale_product in bsale_products.items():
        bsale_product_variants = bsale_variants_by_product.get(str(bsale_product_id), [])
        bsale_product_title = (
            text_value(bsale_product.get("name"))
            or text_value(bsale_product.get("description"))
            or (text_value(bsale_product_variants[0].get("description")) if bsale_product_variants else "")
            or f"Bsale product {bsale_product_id}"
        )
        bsale_sku_list = [clean_sku(v.get("code")) for v in bsale_product_variants if clean_sku(v.get("code"))]
        bsale_barcode_list = [clean_sku(v.get("barCode")) for v in bsale_product_variants if clean_sku(v.get("barCode"))]
        existing = None
        matched_by = ""
        for v in bsale_product_variants:
            sku = clean_sku(v.get("code"))
            barcode = clean_sku(v.get("barCode"))
            if sku and sku in shopify_variants_by_sku:
                existing = shopify_variants_by_sku[sku]
                matched_by = f"SKU {sku}"
                break
            if barcode and barcode in shopify_variants_by_barcode:
                existing = shopify_variants_by_barcode[barcode]
                matched_by = f"barcode {barcode}"
                break

        if not bsale_product_variants:
            status = "SKIP_NO_LINKED_VARIANTS"
            notes = "No exported variant points to this product ID; re-export complete Bsale variants."
        elif existing:
            status = "EXISTS"
            notes = f"Already found in Shopify by {matched_by}; left unchanged."
        else:
            status = "NEW_CANDIDATE"
            notes = "Not found by SKU/barcode. Product remains a candidate until export integrity is confirmed."
            if SHOPIFY_IMPORT_ENABLED:
                has_price = all(get_variant_price(v, bsale_exports["price_lists"]) is not None for v in bsale_product_variants)
                if not has_price and not SHOPIFY_ALLOW_ZERO_PRICE_DRAFTS:
                    status = "SKIP_NO_PRICE"
                    notes = "No variant prices in export; export price-list details or explicitly allow 0.00 draft prices."
                elif missing_ratio > 0.05 and not BSALE_ALLOW_INCOMPLETE_EXPORT:
                    status = "BLOCKED_INCOMPLETE_EXPORT"
                    notes = "Import blocked because product/variant export relationships are incomplete."
                else:
                    bsale_stock_records = []
                    for v in bsale_product_variants:
                        bsale_stock_records.extend(bsale_stocks_by_variant.get(str(v["_bsale_variant_id"]), []))
                    # Keep the raw data attached, but do not modify inventory levels.
                    shopify_create_result = shopify_admin.create_draft(
                        bsale_product, bsale_product_variants, bsale_stock_records, bsale_office_map, bsale_exports["price_lists"]
                    )
                    imported += 1
                    status = "CREATED_DRAFT"
                    notes = "Created as DRAFT; inventory was not changed."

        rows.append({
            "bsale_product_id": str(bsale_product_id),
            "bsale_product_title": bsale_product_title,
            "bsale_variant_ids": ";".join(v["_bsale_variant_id"] for v in bsale_product_variants),
            "skus": ";".join(bsale_sku_list),
            "barcodes": ";".join(bsale_barcode_list),
            "status": status,
            "shopify_product_id": (existing or {}).get("product", {}).get("id", ""),
            "shopify_product_title": (existing or {}).get("product", {}).get("title", ""),
            "notes": notes,
        })

    write_report(rows)
    counts = defaultdict(int)
    for row in rows:
        counts[row["status"]] += 1
    log("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    log(f"New draft products created this run: {imported}")
    if validation_errors:
        log("Validation warnings:")
        for error in validation_errors:
            log("  - " + error)
    if not SHOPIFY_IMPORT_ENABLED:
        log("To create products, first fix export completeness, verify prices, then set IMPORT_ENABLED=true.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"FATAL: {exc}")
        raise SystemExit(1)
