#!/usr/bin/env python3
"""Startpunkt; Implementierung und Klassen liegen in separaten Modulen."""
from common import configure_logging, logger, log
from importer import main
import os

if __name__ == "__main__":
    # Fallback-Konfiguration, falls bereits beim Programmstart ein Fehler auftritt.
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    try:
        raise SystemExit(main())
    except Exception as error:
        logger.exception("Import abgebrochen: %s", error)
        raise SystemExit(1)
