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

BASE_DIR = Path(__file__).resolve().parent
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-10")
SHOP = os.getenv("SHOPIFY_SHOP", "").strip().replace("https://", "").rstrip("/")
CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()
IMPORT_ENABLED = os.getenv("IMPORT_ENABLED", "false").lower() == "true"
ALLOW_INCOMPLETE_EXPORT = os.getenv("ALLOW_INCOMPLETE_EXPORT", "false").lower() == "true"
ALLOW_ZERO_PRICE_DRAFTS = os.getenv("ALLOW_ZERO_PRICE_DRAFTS", "false").lower() == "true"
PRICE_LIST_ID = os.getenv("BSALE_PRICE_LIST_ID", "").strip()

FILES = {
    "products": "products.json",
    "variants": "variants.json",
    "offices": "offices.json",
    "price_lists": "price_lists.json",
    "stocks": "stocks.json",
}
REPORT_PATH = BASE_DIR / "bsale_shopify_comparison.csv"
LOG_PATH = BASE_DIR / "bsale_shopify_sync.log"

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


def load_bsale() -> dict[str, dict[str, Any]]:
    result = {}
    for key, filename in FILES.items():
        result[key] = load_json_file(BASE_DIR / filename)
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
    bsale_products = data["products"]
    variants = data["variants"]
    stocks = data["stocks"]
    offices = data["offices"]
    price_lists = data["price_lists"]
    errors: list[str] = []

    product_ids = {str(k) for k in bsale_products}
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
    for stock in stocks.values():
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
        f"Other reference data: {len(offices)} offices, {len(price_lists)} price lists, "
        f"{len(stocks)} stock records."
    )
    return dict(variants_by_product), errors


def get_access_token() -> str:
    if not (SHOP and CLIENT_ID and CLIENT_SECRET):
        raise RuntimeError(
            "Set SHOPIFY_SHOP, SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET first."
        )
    url = f"https://{SHOP}/admin/oauth/access_token"
    response = session.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
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
        self.url = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
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
        by_sku: dict[str, dict] = {}
        by_barcode: dict[str, dict] = {}
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
        cursor = None
        while True:
            data = self.graphql(query, {"after": cursor})
            connection = data["products"]
            for product in connection["nodes"]:
                for variant in product["variants"]["nodes"]:
                    record = {"product": product, "variant": variant}
                    sku = clean_sku(variant.get("sku"))
                    barcode = clean_sku(variant.get("barcode"))
                    if sku:
                        by_sku.setdefault(sku, record)
                    if barcode:
                        by_barcode.setdefault(barcode, record)
            page = connection["pageInfo"]
            if not page["hasNextPage"]:
                break
            cursor = page["endCursor"]
        log(f"Shopify lookup loaded {len(by_sku)} distinct SKUs and {len(by_barcode)} distinct barcodes.")
        return by_sku, by_barcode

    def create_draft(self, product: dict[str, Any], variants: list[dict[str, Any]],
                     stock_records: list[dict[str, Any]], office_map: dict[str, Any],
                     price_lists: dict[str, Any]) -> dict[str, Any]:
        """Create a draft product and preserve source JSON in product metafields."""
        if not variants:
            raise ValueError("Cannot create product without linked Bsale variants.")

        title = (
            text_value(product.get("name"))
            or text_value(product.get("description"))
            or text_value(variants[0].get("description"))
            or f"Bsale product {product.get('id', '')}"
        )
        description = text_value(product.get("description"))
        if description:
            description_html = "<p>" + (
                description.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "<br>")
            ) + "</p>"
        else:
            description_html = ""

        # Bsale export does not contain option attribute values. To avoid inventing size/color
        # options, variants are represented with a single option named "Bsale variant".
        # Variant label is description, then code, then Bsale ID.
        labels = []
        seen_labels = set()
        for v in variants:
            label = text_value(v.get("description")) or clean_sku(v.get("code")) or str(v["_bsale_variant_id"])
            if label in seen_labels:
                label = f"{label} ({v['_bsale_variant_id']})"
            seen_labels.add(label)
            labels.append(label)

        # Create product in DRAFT status. Shopify productCreate creates its initial variant.
        create_mutation = """
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
        product_input: dict[str, Any] = {
            "title": title[:255],
            "status": "DRAFT",
            "vendor": "Bsale",
            "descriptionHtml": description_html,
            "tags": ["bsale", f"bsale-product-{product.get('id', '')}"],
            "productOptions": [{
                "name": "Bsale variant",
                "values": [{"name": label[:255]} for label in labels],
            }],
            "metafields": [
                {
                    "namespace": "bsale",
                    "key": "product_id",
                    "type": "single_line_text_field",
                    "value": str(product.get("id", "")),
                },
                {
                    "namespace": "bsale",
                    "key": "product_data",
                    "type": "json",
                    "value": json_compact(product),
                },
                {
                    "namespace": "bsale",
                    "key": "variants_data",
                    "type": "json",
                    "value": json_compact(variants),
                },
                {
                    "namespace": "bsale",
                    "key": "stock_data",
                    "type": "json",
                    "value": json_compact(stock_records),
                },
                {
                    "namespace": "bsale",
                    "key": "offices_data",
                    "type": "json",
                    "value": json_compact(office_map),
                },
                {
                    "namespace": "bsale",
                    "key": "price_lists_data",
                    "type": "json",
                    "value": json_compact(price_lists),
                },
            ],
        }
        # Shopify metafields have size limits; fail clearly rather than silently losing source data.
        for mf in product_input["metafields"]:
            if len(mf["value"].encode("utf-8")) > 60_000:
                raise ValueError(f"Metafield {mf['key']} exceeds safe 60 KB size limit; split data into smaller metafields.")

        result = self.graphql(create_mutation, {"product": product_input})["productCreate"]
        if result["userErrors"]:
            raise RuntimeError(f"productCreate userErrors: {json_compact(result['userErrors'])}")
        created = result["product"]
        if not created:
            raise RuntimeError("Shopify did not return a created product.")

        initial = created["variants"]["nodes"][0]
        # Set the first variant's SKU/barcode and price. If price isn't present in the exports,
        # zero-price import is only allowed with ALLOW_ZERO_PRICE_DRAFTS=true.
        first = variants[0]
        price = get_variant_price(first, price_lists)
        if price is None:
            if not ALLOW_ZERO_PRICE_DRAFTS:
                raise RuntimeError(
                    "Price unavailable. Import blocked. Export price-list detail records or set "
                    "ALLOW_ZERO_PRICE_DRAFTS=true to create a DRAFT with price 0.00."
                )
            price = "0.00"

        update_first = """
        mutation UpdateInitialVariant($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants { id sku barcode price }
            userErrors { field message }
          }
        }
        """
        first_input: dict[str, Any] = {
            "id": initial["id"],
            "price": price,
            "inventoryItem": {"sku": clean_sku(first.get("code")) or None},
        }
        if clean_sku(first.get("barCode")):
            first_input["barcode"] = clean_sku(first.get("barCode"))
        updated = self.graphql(
            update_first,
            {"productId": created["id"], "variants": [first_input]},
        )["productVariantsBulkUpdate"]
        if updated["userErrors"]:
            raise RuntimeError(f"Could not update initial variant: {json_compact(updated['userErrors'])}")

        # Create remaining variants with the same single Bsale variant option.
        if len(variants) > 1:
            rest_inputs = []
            for v, label in zip(variants[1:], labels[1:]):
                variant_price = get_variant_price(v, price_lists)
                if variant_price is None:
                    if not ALLOW_ZERO_PRICE_DRAFTS:
                        raise RuntimeError(
                            f"Price unavailable for Bsale variant {v['_bsale_variant_id']}. "
                            "Import blocked. Export price-list details or explicitly allow zero-price drafts."
                        )
                    variant_price = "0.00"
                item = {
                    "price": variant_price,
                    "optionValues": [{"optionName": "Bsale variant", "name": label[:255]}],
                    "inventoryItem": {"sku": clean_sku(v.get("code")) or None},
                }
                if clean_sku(v.get("barCode")):
                    item["barcode"] = clean_sku(v.get("barCode"))
                rest_inputs.append(item)

            bulk_create = """
            mutation AddBsaleVariants($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
              productVariantsBulkCreate(productId: $productId, variants: $variants) {
                productVariants { id sku barcode price }
                userErrors { field message }
              }
            }
            """
            created_variants = self.graphql(
                bulk_create,
                {"productId": created["id"], "variants": rest_inputs},
            )["productVariantsBulkCreate"]
            if created_variants["userErrors"]:
                raise RuntimeError(f"Could not create variants: {json_compact(created_variants['userErrors'])}")

        return {"id": created["id"], "title": created["title"], "status": created["status"]}


def get_variant_price(variant: dict[str, Any], price_lists: dict[str, Any]) -> str | None:
    """
    This snapshot's variants contain only a URL under 'prices', not price values.
    This function intentionally returns None unless a future enriched export includes a
    numeric price or a `price` field. Do not infer price from a price-list header.
    """
    if variant.get("price") is not None:
        try:
            return f"{float(variant['price']):.2f}"
        except (TypeError, ValueError):
            pass
    prices = variant.get("prices")
    if isinstance(prices, dict):
        for key in ("price", "amount", "value"):
            if prices.get(key) is not None:
                try:
                    return f"{float(prices[key]):.2f}"
                except (TypeError, ValueError):
                    pass
    # price_lists.json has names/IDs but not variant-specific price values.
    return None


def make_stock_indexes(data: dict[str, dict[str, Any]]) -> tuple[dict[str, list[dict]], dict[str, Any]]:
    stocks_by_variant: dict[str, list[dict]] = defaultdict(list)
    for stock_id, stock in data["stocks"].items():
        variant_id = text_value((stock.get("variant") or {}).get("id"))
        office_id = text_value((stock.get("office") or {}).get("id"))
        row = dict(stock)
        row["_bsale_stock_id"] = str(stock_id)
        row["_office_id"] = office_id
        stocks_by_variant[variant_id].append(row)
    return stocks_by_variant, data["offices"]


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
    data = load_bsale()
    variants_by_product, validation_errors = validate_exports(data)
    stocks_by_variant, office_map = make_stock_indexes(data)

    # If credentials are configured, compare against Shopify even in compare-only mode.
    # IMPORT_ENABLED controls creation only.
    if SHOP and CLIENT_ID and CLIENT_SECRET:
        token = get_access_token()
        shopify = ShopifyAdmin(token)
        by_sku, by_barcode = shopify.all_variants()
    else:
        shopify = None
        by_sku, by_barcode = {}, {}
        log("Shopify credentials not set: Bsale-only report; Shopify comparison skipped.")
    if not IMPORT_ENABLED:
        log("COMPARE-ONLY MODE: no Shopify products will be created or changed.")

    rows: list[dict[str, Any]] = []
    bsale_products = data["products"]
    variants = data["variants"]

    # Count parent mismatches. By default, do not import from an incomplete joined dataset.
    total_variants = len(variants)
    linked_variant_count = sum(len(v) for v in variants_by_product.values())
    missing_ratio = (total_variants - linked_variant_count) / max(total_variants, 1)
    if IMPORT_ENABLED and missing_ratio > 0.05 and not ALLOW_INCOMPLETE_EXPORT:
        raise RuntimeError(
            f"Import stopped for safety: {missing_ratio:.1%} of variants point to products absent "
            "from bsale_products.json. Re-export complete/paginated Bsale data. Only set "
            "ALLOW_INCOMPLETE_EXPORT=true if you have checked the relationships."
        )

    log("prepare import")
    imported = 0
    for product_id, product in products.items():
        product_variants = variants_by_product.get(str(product_id), [])
        title = (
            text_value(product.get("name"))
            or text_value(product.get("description"))
            or (text_value(product_variants[0].get("description")) if product_variants else "")
            or f"Bsale product {product_id}"
        )
        sku_list = [clean_sku(v.get("code")) for v in product_variants if clean_sku(v.get("code"))]
        barcode_list = [clean_sku(v.get("barCode")) for v in product_variants if clean_sku(v.get("barCode"))]
        existing = None
        matched_by = ""
        for v in product_variants:
            sku = clean_sku(v.get("code"))
            barcode = clean_sku(v.get("barCode"))
            if sku and sku in by_sku:
                existing = by_sku[sku]
                matched_by = f"SKU {sku}"
                break
            if barcode and barcode in by_barcode:
                existing = by_barcode[barcode]
                matched_by = f"barcode {barcode}"
                break

        if not product_variants:
            status = "SKIP_NO_LINKED_VARIANTS"
            notes = "No exported variant points to this product ID; re-export complete Bsale variants."
        elif existing:
            status = "EXISTS"
            notes = f"Already found in Shopify by {matched_by}; left unchanged."
        else:
            status = "NEW_CANDIDATE"
            notes = "Not found by SKU/barcode. Product remains a candidate until export integrity is confirmed."
            if IMPORT_ENABLED:
                has_price = all(get_variant_price(v, data["price_lists"]) is not None for v in product_variants)
                if not has_price and not ALLOW_ZERO_PRICE_DRAFTS:
                    status = "SKIP_NO_PRICE"
                    notes = "No variant prices in export; export price-list details or explicitly allow 0.00 draft prices."
                elif missing_ratio > 0.05 and not ALLOW_INCOMPLETE_EXPORT:
                    status = "BLOCKED_INCOMPLETE_EXPORT"
                    notes = "Import blocked because product/variant export relationships are incomplete."
                else:
                    stock_records = []
                    for v in product_variants:
                        stock_records.extend(stocks_by_variant.get(str(v["_bsale_variant_id"]), []))
                    # Keep the raw data attached, but do not modify inventory levels.
                    result = shopify.create_draft(
                        product, product_variants, stock_records, office_map, data["price_lists"]
                    )
                    imported += 1
                    status = "CREATED_DRAFT"
                    notes = "Created as DRAFT; inventory was not changed."

        rows.append({
            "bsale_product_id": str(product_id),
            "bsale_product_title": title,
            "bsale_variant_ids": ";".join(v["_bsale_variant_id"] for v in product_variants),
            "skus": ";".join(sku_list),
            "barcodes": ";".join(barcode_list),
            "status": status,
            "shopify_product_id": (existing or {}).get("product", {}).get("id", ""),
            "shopify_product_title": (existing or {}).get("product", {}).get("title", ""),
            "notes": notes,
        })

    log("write_report")
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
    if not IMPORT_ENABLED:
        log("To create products, first fix export completeness, verify prices, then set IMPORT_ENABLED=true.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"FATAL: {exc}")
        raise SystemExit(1)
