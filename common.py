"""Gemeinsame Hilfsfunktionen und zentrale Logging-Konfiguration."""
import json
import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Any

LOGGER_NAME = "bsale_shopify_import"
logger = logging.getLogger(LOGGER_NAME)

def configure_logging(level: str | None = None) -> None:
    """Konfiguriert Konsolen-Logging. Unterstützt DEBUG, INFO, WARN/WARNING, ERROR."""
    selected = (level or os.getenv("LOG_LEVEL", "INFO")).strip().upper()
    if selected == "WARN":
        selected = "WARNING"
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if selected not in valid_levels:
        raise ValueError(f"Ungültiges Loglevel {selected!r}. Erlaubt: DEBUG, INFO, WARN, ERROR.")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, selected))
    logger.propagate = False

def log(message: str) -> None:
    """Kompatibilitätshelfer: bisherige Meldungen erscheinen auf INFO."""
    logger.info(message)

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
