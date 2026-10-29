"""Bsale API-Client und Bsale-spezifische Preis-/Bestandslogik."""
import re
from typing import Any
from config import BSALE_API_BASE, HTTP
from common import as_text, log, money, relation_id

class BsaleClient:
    def __init__(self, bsale_access_token: str, bsale_price_list_id: str = "", bsale_price_list_name: str = ""):
        if not bsale_access_token:
            raise RuntimeError("BSALE_ACCESS_TOKEN fehlt.")
        if bsale_price_list_id and bsale_price_list_name:
            raise RuntimeError("Bitte entweder BSALE_PRICE_LIST_ID oder BSALE_PRICE_LIST_NAME verwenden, nicht beides.")
        self.headers = {"access_token": bsale_access_token, "Accept": "application/json"}
        self._active_price_lists_cache: list[dict[str, Any]] | None = None
        self.bsale_price_list_id = bsale_price_list_id.strip()
        self.bsale_price_list_name = bsale_price_list_name.strip()
        self._resolved_price_list: dict[str, Any] | None = None

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        bsale_url = path_or_url if path_or_url.startswith("http") else f"{BSALE_API_BASE}/{path_or_url.lstrip('/')}"
        log(f"Bsale API GET {bsale_url}; Parameter={params or {}}", "DEBUG")
        bsale_response = HTTP.get(bsale_url, headers=self.headers, params=params, timeout=45)
        if bsale_response.status_code >= 400:
            raise RuntimeError(f"Bsale HTTP {bsale_response.status_code} für {bsale_url}: {bsale_response.text[:800]}")
        bsale_payload = bsale_response.json()
        log(f"Bsale API Antwort: HTTP {bsale_response.status_code} für {bsale_url}", "DEBUG")
        return bsale_payload

    def list_all(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        bsale_items: list[dict[str, Any]] = []
        bsale_offset = 0
        bsale_params = dict(params or {})
        bsale_params["limit"] = 50
        while True:
            bsale_params["offset"] = bsale_offset
            bsale_page = self.get(path, bsale_params)
            bsale_page_items = bsale_page.get("items", [])
            if not isinstance(bsale_page_items, list):
                raise RuntimeError(f"Unerwartetes Bsale-Listenformat für {path}.")
            bsale_items.extend(x for x in bsale_page_items if isinstance(x, dict))
            bsale_count = int(bsale_page.get("count", len(bsale_items)))
            if not bsale_page_items or len(bsale_items) >= bsale_count or len(bsale_page_items) < 50:
                break
            bsale_offset += len(bsale_page_items)
        return bsale_items

    def products(self, bsale_product_ids: set[str] | None) -> list[dict[str, Any]]:
        if bsale_product_ids:
            bsale_products = []
            for bsale_product_id in sorted(bsale_product_ids):
                bsale_products.append(self.get(f"products/{bsale_product_id}.json"))
            return bsale_products
        return self.list_all("products.json")

    def variants_for_product(self, bsale_product_id: str) -> list[dict[str, Any]]:
        return self.list_all(f"products/{bsale_product_id}/variants.json")

    def stock_for_office(self, bsale_variant_id: str, bsale_office_id: str) -> dict[str, Any] | None:
        bsale_stock_rows = self.list_all(
            "stocks.json",
            {"variantid": bsale_variant_id, "officeid": bsale_office_id},
        )
        for bsale_stock in bsale_stock_rows:
            if relation_id(bsale_stock, "variant") == bsale_variant_id and relation_id(bsale_stock, "office") == bsale_office_id:
                return bsale_stock
        return bsale_stock_rows[0] if bsale_stock_rows else None

    def _price_from_detail(self, bsale_price_detail: dict[str, Any]) -> str | None:
        # Bsale detail objects normally expose variantValue and
        # variantValueWithTaxes. Use the tax-included value for a retail Shopify
        # price when available; otherwise fall back to the net value.
        for bsale_price_key in (
            "variantValueWithTaxes", "finalPrice", "salePrice",
            "variantValue", "price", "amount", "value",
        ):
            bsale_variant_price = money(bsale_price_detail.get(bsale_price_key))
            if bsale_variant_price is not None:
                return bsale_variant_price
        return None

    def resolve_price_list(self) -> dict[str, Any] | None:
        """Löst die explizit ausgewählte Bsale-Preisliste anhand ID oder Name auf."""
        if not self.bsale_price_list_id and not self.bsale_price_list_name:
            return None
        if self._resolved_price_list is not None:
            return self._resolved_price_list
        bsale_lists = self.list_all("price_lists.json")
        if self.bsale_price_list_id:
            matches = [item for item in bsale_lists if as_text(item.get("id")) == self.bsale_price_list_id]
        else:
            matches = [item for item in bsale_lists if as_text(item.get("name")).casefold() == self.bsale_price_list_name.casefold()]
        if not matches:
            wanted = self.bsale_price_list_id or self.bsale_price_list_name
            raise RuntimeError(f"Bsale-Preisliste nicht gefunden: {wanted!r}. Prüfe ID oder exakten Namen.")
        if len(matches) > 1:
            raise RuntimeError(f"Mehrere Bsale-Preislisten mit dem Namen {self.bsale_price_list_name!r}. Bitte BSALE_PRICE_LIST_ID verwenden.")
        self._resolved_price_list = matches[0]
        log(f"Ausgewählte Bsale-Preisliste: {self._resolved_price_list.get('name')} (ID {self._resolved_price_list.get('id')})")
        return self._resolved_price_list

    def variant_price(self, bsale_variant: dict[str, Any]) -> str | None:
        """Liest den Preis der Variante aus der ausgewählten Bsale-Preisliste.

        Bei Auswahl über ID oder Name wird ausschließlich diese Liste verwendet.
        Ohne Auswahl bleibt der frühere dynamische Modus erhalten: erst eine ID aus
        prices.href, sonst aktive Listen durchsuchen und bei Mehrdeutigkeit abbrechen.
        """
        bsale_variant_id = as_text(bsale_variant.get("id"))
        bsale_selected_list = self.resolve_price_list()
        if bsale_selected_list:
            log(f"Ermittle Preis für Variante {bsale_variant_id} aus Preisliste {bsale_selected_list.get('name')} (ID {bsale_selected_list.get('id')}).", "DEBUG")
            bsale_selected_list_id = as_text(bsale_selected_list.get("id"))
            bsale_price_data = self.get(
                f"price_lists/{bsale_selected_list_id}/details.json",
                {"variantid": bsale_variant_id},
            )
            bsale_price_items = bsale_price_data.get("items", [])
            if isinstance(bsale_price_items, list):
                for bsale_price_item in bsale_price_items:
                    if relation_id(bsale_price_item, "variant") == bsale_variant_id or as_text(bsale_price_item.get("variantId")) == bsale_variant_id:
                        return self._price_from_detail(bsale_price_item)
            return None

        bsale_prices_link = bsale_variant.get("prices")
        bsale_prices_url = as_text(bsale_prices_link.get("href")) if isinstance(bsale_prices_link, dict) else ""
        bsale_match = re.search(r"/price_lists/(\d+)/details(?:/|\.json)", bsale_prices_url) if bsale_prices_url else None
        bsale_referenced_list_id = bsale_match.group(1) if bsale_match else ""
        if bsale_referenced_list_id:
            bsale_price_data = self.get(f"price_lists/{bsale_referenced_list_id}/details.json", {"variantid": bsale_variant_id})
            for bsale_price_item in bsale_price_data.get("items", []):
                if relation_id(bsale_price_item, "variant") == bsale_variant_id or as_text(bsale_price_item.get("variantId")) == bsale_variant_id:
                    return self._price_from_detail(bsale_price_item)
            return None

        if self._active_price_lists_cache is None:
            self._active_price_lists_cache = self.list_all("price_lists.json", {"state": 0})
        bsale_candidates: list[tuple[str, str, str]] = []
        for bsale_price_list in self._active_price_lists_cache:
            bsale_list_id = as_text(bsale_price_list.get("id"))
            if not bsale_list_id: continue
            bsale_detail_data = self.get(f"price_lists/{bsale_list_id}/details.json", {"variantid": bsale_variant_id})
            for bsale_price_item in bsale_detail_data.get("items", []):
                bsale_item_variant_id = relation_id(bsale_price_item, "variant") or as_text(bsale_price_item.get("variantId"))
                if bsale_item_variant_id == bsale_variant_id:
                    bsale_item_price = self._price_from_detail(bsale_price_item)
                    if bsale_item_price is not None:
                        bsale_candidates.append((bsale_list_id, as_text(bsale_price_list.get("name")), bsale_item_price))
        bsale_candidates = list(dict.fromkeys(bsale_candidates))
        if len(bsale_candidates) == 1: return bsale_candidates[0][2]
        if len(bsale_candidates) > 1:
            bsale_summary = ", ".join(f"{lid} ({name or 'ohne Namen'}): {price}" for lid, name, price in bsale_candidates)
            log(f"Mehrere aktive Bsale-Preislisten für Variante {bsale_variant_id}: {bsale_summary}. Preis bleibt leer.", "WARN")
            return None
        for bsale_price_key in ("finalPrice", "salePrice", "price"):
            bsale_variant_price = money(bsale_variant.get(bsale_price_key))
            if bsale_variant_price is not None: return bsale_variant_price
        return None
