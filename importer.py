"""Ablaufsteuerung für den Bsale-zu-Shopify-Import."""
import argparse
import csv
import os
import json
from typing import Any
from config import BSALE_ACCESS_TOKEN
from common import as_text, configure_logging, logger, log
from bsale_client import BsaleClient
from shopify_admin import ShopifyAdmin

def parse_ids(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {item.strip() for item in value.split(",") if item.strip()}

def main() -> int:
    parser = argparse.ArgumentParser(description="Bsale-Produkte als Shopify-DRAFT importieren.")
    parser.add_argument("--product-ids", help="Kommagetrennte Bsale-Produkt-IDs, z. B. 123,456. Ohne Angabe alle Produkte.")
    parser.add_argument("--office-id", required=True, help="Bsale Office-/Sucursal-ID, aus der Stock gelesen wird.")
    parser.add_argument("--price-list-id", default=os.getenv("BSALE_PRICE_LIST_ID", "").strip(), help="Bsale-Preisliste über ID auswählen (alternativ BSALE_PRICE_LIST_ID).")
    parser.add_argument("--price-list-name", default=os.getenv("BSALE_PRICE_LIST_NAME", "").strip(), help="Bsale-Preisliste über exakten Namen auswählen (alternativ BSALE_PRICE_LIST_NAME).")
    parser.add_argument("--import", dest="do_import", action="store_true", help="Import tatsächlich ausführen; ohne Flag nur Vorschau.")
    parser.add_argument("--sync-inventory", action="store_true", help="Stock aus ausgewähltem Bsale-Office in Shopify-Bestand schreiben. Erfordert --import und SHOPIFY_LOCATION_ID.")
    parser.add_argument("--allow-missing-prices", action="store_true", help="Drafts trotz fehlender Preise mit 0.00 anlegen (nur für Tests).")
    parser.add_argument("--report", default="bsale_shopify_import_report.csv", help="Pfad der CSV-Vorschau/des Importberichts.")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"), choices=["DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL", "debug", "info", "warn", "warning", "error", "critical"], help="Loglevel: DEBUG, INFO, WARN, ERROR (Standard: INFO; alternativ Umgebungsvariable LOG_LEVEL).")
    args = parser.parse_args()
    configure_logging(args.log_level)
    logger.info("Bsale-Shopify-Import gestartet; Loglevel=%s", args.log_level.upper())

    if not args.office_id.strip():
        parser.error("--office-id darf nicht leer sein.")
    if args.price_list_id and args.price_list_name:
        parser.error("Bitte entweder --price-list-id oder --price-list-name verwenden, nicht beides.")
    if args.sync_inventory and not args.do_import:
        parser.error("--sync-inventory darf nur zusammen mit --import verwendet werden.")

    bsale_client = BsaleClient(BSALE_ACCESS_TOKEN, args.price_list_id, args.price_list_name)
    bsale_product_ids = parse_ids(args.product_ids)
    bsale_products = bsale_client.products(bsale_product_ids)
    logger.info("Bsale-Produkte geladen: %s", len(bsale_products))
    logger.debug("Gefilterte Bsale-Produkt-IDs: %s", sorted(bsale_product_ids) if bsale_product_ids else "alle Produkte")
    if args.price_list_id or args.price_list_name:
        bsale_client.resolve_price_list()
    shopify_admin = ShopifyAdmin()
    shopify_existing_skus = shopify_admin.existing_skus()
    print("shopify_existing_skus: ")
    print(json.dumps(dict(shopify_existing_skus), indent=4))

    shopify_report_rows = []
    shopify_created_count = 0
    for bsale_product in bsale_products:
        bsale_product_id = as_text(bsale_product.get("id"))
        if bsale_product_ids and bsale_product_id not in bsale_product_ids:
            continue
        logger.debug("Verarbeite Bsale-Produkt %s (%s)", bsale_product_id, as_text(bsale_product.get("name")))
        bsale_variants = bsale_client.variants_for_product(bsale_product_id)
        logger.debug("Varianten für Produkt %s geladen: %s", bsale_product_id, len(bsale_variants))
        if not bsale_variants:
            shopify_report_rows.append({"bsale_product_id": bsale_product_id, "title": as_text(bsale_product.get("name")), "status": "SKIP_NO_VARIANTS", "notes": "Keine Varianten in Bsale."})
            continue

        bsale_prices: dict[str, str | None] = {}
        bsale_stocks: dict[str, dict[str, Any] | None] = {}
        bsale_skus = []
        for bsale_variant in bsale_variants:
            bsale_variant_id = as_text(bsale_variant.get("id"))
            bsale_prices[bsale_variant_id] = bsale_client.variant_price(bsale_variant)
            logger.debug("Preisermittlung Variante %s: %s", bsale_variant_id, bsale_prices[bsale_variant_id] if bsale_prices[bsale_variant_id] is not None else "FEHLT")
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
                logger.warning("Import von Produkt %s übersprungen; Preise fehlen für Varianten %s", bsale_product_id, ",".join(shopify_missing_price_ids))
                shopify_notes.append("Import gestoppt; für Testzwecke --allow-missing-prices verwenden.")
            else:
                if shopify_missing_price_ids:
                    bsale_prices = {k: (v if v is not None else "0.00") for k, v in bsale_prices.items()}
                shopify_create_result = shopify_admin.create_draft(
                    bsale_product, bsale_variants, bsale_prices, bsale_stocks, args.office_id, args.sync_inventory
                )
                shopify_created_count += 1
                logger.info("Shopify-Draft erstellt: Produkt %s", bsale_product_id)
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
    logger.info("Bericht geschrieben: %s", shopify_report_path)
    logger.info("Neue Draft-Produkte: %s", shopify_created_count)
    return 0
