# Bsale → Shopify Draft-Import

Das Skript liest Produkte, Varianten, Preise und Stock direkt über die Bsale-API und erstellt neue Produkte in Shopify mit Status **DRAFT**.

## Funktionen

- Produktfilter per Bsale-ID: `--product-ids 123,456`
- Stock nur aus einem bestimmten Bsale-Office: `--office-id 2`
- Preis aus einer optional festgelegten Bsale-Preisliste: `BSALE_PRICE_LIST_ID`
- bestehende Shopify-Produkte werden bei SKU-Treffer übersprungen und nicht überschrieben
- Vorschau-Modus standardmäßig; Schreiben erst mit `--import`
- optionaler Bestandssync nach Shopify mit `--sync-inventory` und Shopify Location ID
- CSV-Bericht mit Status, fehlenden Preisen und fehlenden Stock-Datensätzen
- Variablen klar getrennt: `bsale_*` = Bsale; `shopify_*` = Shopify

## 1. Installation

Python 3.10+:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests
```

## 2. Zugangsdaten

### Bsale

Bsale API verwendet den Header `access_token`. Beispiel für die API-Basis Chile:

```bash
export BSALE_API_BASE="https://api.bsale.cl/v1"
export BSALE_ACCESS_TOKEN="DEIN_BSALE_TOKEN"
```

Andere Länder/Umgebungen können eine andere API-Domain benötigen; `BSALE_API_BASE` entsprechend der Bsale-Dokumentation für dein Konto setzen.

### Shopify

Für eine Shopify Custom App mit Client-Credentials:

```bash
export SHOPIFY_SHOP="dein-shop.myshopify.com"
export SHOPIFY_CLIENT_ID="DEINE_CLIENT_ID"
export SHOPIFY_CLIENT_SECRET="DEIN_CLIENT_SECRET"
export SHOPIFY_API_VERSION="2026-10"
```

Alternativ, falls du bereits einen Admin API Access Token hast:

```bash
export SHOPIFY_ACCESS_TOKEN="shpat_..."
```

Die App benötigt mindestens `read_products` und `write_products`. Für Bestandssynchronisierung zusätzlich die erforderlichen Inventory-Scopes und Zugriff auf die Ziel-Location.

### Preisliste

```bash
export BSALE_PRICE_LIST_ID="6"
```

Wenn nicht gesetzt, versucht das Skript den Preis aus dem Preis-Endpunkt der Variante bzw. direkten Variant-Feldern zu lesen. Bitte zuerst im Vorschau-Modus prüfen, ob Preise korrekt erkannt werden.

## 3. Vorschau ohne Shopify-Änderungen

Nur bestimmte Bsale-Produkte:

```bash
python bsale_shopify_import.py --product-ids 123,456 --office-id 2
```

Alle Produkte:

```bash
python bsale_shopify_import.py --office-id 2
```

Die CSV `bsale_shopify_import_report.csv` zeigt unter anderem fehlende Preise und fehlende Stock-Datensätze.

## 4. Draft-Produkte erstellen

```bash
python bsale_shopify_import.py --product-ids 123,456 --office-id 2 --import
```

Produkte werden als `DRAFT` angelegt. Vorhandene Shopify-SKUs werden übersprungen.

## 5. Optional: Bestand aus dem ausgewählten Bsale-Office nach Shopify schreiben

Dafür muss die passende Shopify Location-ID bekannt sein, z. B. `gid://shopify/Location/123456789`.

```bash
export SHOPIFY_LOCATION_ID="gid://shopify/Location/123456789"
python bsale_shopify_import.py --product-ids 123,456 --office-id 2 --import --sync-inventory
```

Ohne `--sync-inventory` wird kein Shopify-Lagerbestand geändert; Office-ID, Varianten und die aus diesem Office gelesenen Stock-Datensätze werden als Bsale-Metafields am Produkt gespeichert. Das Skript rundet Mengen bei der Übertragung auf ganze Einheiten ab, da Shopify-Bestände üblicherweise ganzzahlig sind. Für Produkte mit Dezimalbestand bitte die Übertragung vorher fachlich prüfen.

## 6. Fehlende Preise

Das Skript stoppt den Import eines Produkts, wenn mindestens einer seiner Varianten kein Preis zugeordnet werden kann. Für einen Test-Import mit 0.00:

```bash
python bsale_shopify_import.py --product-ids 123 --office-id 2 --import --allow-missing-prices
```

Nur in einem Development Store verwenden.

## Hinweise

- Bsale-Preisdaten können je API-Version/Preislisten-Konfiguration anders strukturiert sein. Wenn Preise in der CSV fehlen, zuerst die Bsale-API-Antwort des `prices`-Endpunkts und `BSALE_PRICE_LIST_ID` prüfen.
- Shopify benötigt für Varianten eindeutige Optionswerte. Wenn Bsale keine Attributwerte liefert, verwendet das Skript `Bsale variant` und als Wert die Variantenbeschreibung, SKU oder Bsale-Varianten-ID.
- Die Bestandsübertragung ist optional und verwendet ausschließlich das gewählte Bsale-Office.
- Vor echtem Import immer zuerst die Vorschau ausführen und im Shopify Development Store testen.

## Offizielle Dokumentation

- Bsale Getting Started / Authentifizierung: https://docs.bsale.dev/get-started/
- Bsale Produkte und Varianten: https://docs.bsale.dev/productos-y-servicios/
- Bsale Stock: https://docs.bsale.dev/PE/stocks/
- Shopify Admin GraphQL API: https://shopify.dev/docs/api/admin-graphql
- Shopify `productCreate`: https://shopify.dev/docs/api/admin-graphql/latest/mutations/productcreate
- Shopify `productVariantsBulkCreate`: https://shopify.dev/docs/api/admin-graphql/latest/mutations/productVariantsBulkCreate
- Shopify `inventorySetQuantities`: https://shopify.dev/docs/api/admin-graphql/latest/mutations/inventorySetQuantities
