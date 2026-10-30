"""Shopify Admin API-Klasse."""
import json
from decimal import Decimal
from typing import Any
from config import (HTTP, SHOPIFY_SHOP, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SHOPIFY_ACCESS_TOKEN, SHOPIFY_API_VERSION, SHOPIFY_LOCATION_ID)
from common import as_text, logger, log

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
        logger.debug("Shopify GraphQL Request gestartet")
        shopify_response = HTTP.post(
            self.url, headers=self.headers,
            json={"query": query, "variables": variables or {}}, timeout=60,
        )
        if shopify_response.status_code >= 400:
            raise RuntimeError(f"Shopify GraphQL HTTP {shopify_response.status_code}: {shopify_response.text[:1200]}")
        shopify_payload = shopify_response.json()
        if shopify_payload.get("errors"):
            raise RuntimeError(f"Shopify GraphQL errors: {json.dumps(shopify_payload['errors'], ensure_ascii=False)[:1500]}")
        logger.debug("Shopify GraphQL Antwort erfolgreich (HTTP %s)", shopify_response.status_code)
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
        logger.info("Erstelle Shopify-Draft für Bsale-Produkt %s", bsale_product.get("id"))
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
                    "Bitte Preislisten-Referenz der Variante bzw. die Bsale-Preisdetails prüfen."
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
                    logger.warning("Kein Stock für Bsale-Variante %s in Office %s; Bestand wird nicht gesetzt.", bsale_variant.get("id"), bsale_office_id)
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

