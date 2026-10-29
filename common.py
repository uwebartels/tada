"""Gemeinsame Hilfsfunktionen für Bsale- und Shopify-Module."""
import json
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Any

_LOG_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARN": logging.WARNING, "WARNING": logging.WARNING, "ERROR": logging.ERROR}

def configure_logging(level: str | None = None) -> None:
    """Initialisiert ein einheitliches Konsolen-Logging mit Zeitstempel und Level."""
    selected = (level or os.getenv("LOG_LEVEL", "INFO")).strip().upper()
    if selected not in _LOG_LEVELS:
        raise ValueError(f"Ungültiges Loglevel {selected!r}; erlaubt sind DEBUG, INFO und WARN.")
    logging.basicConfig(
        level=_LOG_LEVELS[selected],
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )

def log(message: str, level: str = "INFO") -> None:
    """Schreibt eine Meldung über das zentrale Logging; WARN und WARNING sind Aliase."""
    normalized = level.strip().upper()
    if normalized not in _LOG_LEVELS:
        normalized = "INFO"
    logging.getLogger("bsale_shopify_import").log(_LOG_LEVELS[normalized], message)

def as_text(value: Any) -> str:
    if value is None: return ""
    if isinstance(value, (dict, list)): return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).strip()

def relation_id(record: dict[str, Any], relation: str) -> str:
    return as_text((record.get(relation) or {}).get("id"))

def money(value: Any) -> str | None:
    if value is None or value == "": return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0: return None
        return f"{result:.2f}"
    except (InvalidOperation, ValueError): return None
