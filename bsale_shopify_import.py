#!/usr/bin/env python3
"""Startpunkt; Implementierung und Klassen liegen in separaten Modulen."""
from common import configure_logging, log
from importer import main

if __name__ == "__main__":
    configure_logging()
    try:
        raise SystemExit(main())
    except Exception as error:
        log(f"Unerwarteter Fehler: {error}", "ERROR")
        raise SystemExit(1)
