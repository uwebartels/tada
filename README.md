# Bsale → Shopify Draft-Import (modular)

## Dateien / Klassen

- `config.py`: Bsale- und Shopify-Konfiguration, HTTP-Session
- `common.py`: gemeinsame Hilfsfunktionen (`log`, `as_text`, `money`, `relation_id`)
- `bsale_client.py`: Klasse `BsaleClient` – Bsale API, Produkte, Varianten, Preise und Bestand
- `shopify_admin.py`: Klasse `ShopifyAdmin` – Shopify GraphQL, Draft-Produkte, Varianten und optional Bestand
- `importer.py`: Ablaufsteuerung, CLI-Parameter, CSV-Bericht
- `bsale_shopify_import.py`: ausführbarer Einstiegspunkt

Alle Variablen behalten die Namenskonvention: `BSALE_*` für Bsale, `SHOPIFY_*` für Shopify.

## Preisliste auswählen

Du kannst die Preisliste ausdrücklich über ID **oder** Namen festlegen. Bei Auswahl wird für jede Variante ausschließlich der Preis aus dieser Liste gelesen.

Per Kommandozeile:

```bash
python bsale_shopify_import.py --office-id 123 --price-list-id 7
python bsale_shopify_import.py --office-id 123 --price-list-name "Retail"
```

Oder als Umgebungsvariable:

```bash
export BSALE_PRICE_LIST_ID="7"
# alternativ:
export BSALE_PRICE_LIST_NAME="Retail"
```

Die Namenssuche ist ohne Beachtung der Groß-/Kleinschreibung, ansonsten muss der Name exakt übereinstimmen. Wenn mehrere Preislisten denselben Namen haben, bricht der Import mit Hinweis ab; nutze dann die ID. `--price-list-id` und `--price-list-name` dürfen nicht gleichzeitig gesetzt sein. Kommandozeilenwerte überschreiben die entsprechenden Umgebungsvariablen.

Wenn keine Preisliste angegeben wird, bleibt der dynamische Fallback-Modus aktiv: erst wird eine ID aus dem Variantenlink verwendet, sonst werden aktive Preislisten geprüft. Wenn mehrere Preise gefunden werden, wird kein beliebiger Preis ausgewählt.

## Logging / Loglevel

Das Programm schreibt Meldungen mit Zeitstempel und Loglevel in die Konsole. Unterstützt werden `DEBUG`, `INFO` und `WARN` (alternativ `WARNING`). Standard ist `INFO`.

Über die Kommandozeile:

```bash
python bsale_shopify_import.py --office-id 123 --price-list-id 7 --log-level DEBUG
python bsale_shopify_import.py --office-id 123 --price-list-id 7 --log-level INFO
python bsale_shopify_import.py --office-id 123 --price-list-id 7 --log-level WARN
```

Oder über eine Umgebungsvariable:

```bash
export LOG_LEVEL=DEBUG
```

- `DEBUG`: API-Aufrufe, Parameter und Details zur Produkt-/Variantenverarbeitung.
- `INFO`: wesentliche Schritte, ausgewählte Preisliste, Ergebnis und Berichtspfad.
- `WARN`: nur Warnungen und Fehler.

API-Zugangstoken werden nicht protokolliert. Beachte, dass DEBUG weitere API-Parameter und IDs sichtbar macht; teile Debug-Protokolle daher nur nach Prüfung.

## Voraussetzungen

```bash
python -m pip install requests
export BSALE_ACCESS_TOKEN="..."
export SHOPIFY_SHOP="dein-shop.myshopify.com"
# entweder:
export SHOPIFY_ACCESS_TOKEN="..."
# oder SHOPIFY_CLIENT_ID und SHOPIFY_CLIENT_SECRET setzen
```

## Vorschau und Import

Vorschau (Standard, erstellt nichts):

```bash
python bsale_shopify_import.py --office-id 123 --price-list-name "Retail" --product-ids 100,101
```

Tatsächlicher Import als Shopify-Drafts:

```bash
python bsale_shopify_import.py --office-id 123 --price-list-id 7 --product-ids 100,101 --import
```

Optional Bestand aus dem ausgewählten Bsale-Office synchronisieren:

```bash
export SHOPIFY_LOCATION_ID="gid://shopify/Location/123456789"
python bsale_shopify_import.py --office-id 123 --price-list-id 7 --product-ids 100,101 --import --sync-inventory
```

Ohne `--import` wird nur ein CSV-Bericht geschrieben. Bestehende Shopify-SKUs werden übersprungen; neue Produkte werden als `DRAFT` angelegt. Prüfe die Preislisten-ID und Währung in Bsale, bevor du den echten Import startest.
