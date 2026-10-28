
import os
import sys
import re
import requests
import json
import requests
import logging
from lib import *



BSALE_URL = "https://api.bsale.io/v1"
os.environ["BSALE_ACCESS_TOKEN"] = "tutokendeacceso"
limit = 50

logger = logging.getLogger()

def bsale_get(type, params=None):
    logger.debug('calling bsale_get(type='+type+',params='+json.dumps(params)+')')

    # preparation: filter for id
    path_id=""
    if "id" in params:
        path=type+"/"+str(params.pop("id"))+".json"
    else:
        path=type+".json"

    try:
        r = requests.get(f"{BSALE_URL}/{path}", params=params,
                         headers={"access_token": env("BSALE_ACCESS_TOKEN")}, timeout=60)
    except Exception as e:
        getExceptionMessage(["message"])
        sys.exit(1)

    r.raise_for_status()
    return r.json()

def bsale_get_objects(type, params=None):
    """Retrieves all objects or a single object referenced by id"""
    logger.info('calling bsale_get_objects(type='+type+', params='+json.dumps(params)+')')
    items, offset = {}, 0
    while True:
        data = bsale_get(type, params | {"limit": limit, "offset": offset, "expand": "[details,product_taxes]"})
        if "count" in data:
            logstring="retrieved "+str(len(data["items"])+offset)+"/"+str(data["count"])+" records."

            if logging.getLogger().getEffectiveLevel() == logging.INFO:
                if offset > 0:
                    sys.stdout.write('\b'*lenlogstring)
                sys.stdout.write(logstring)
                if not data.get("next"): sys.stdout.write('\n')
            else:
                logger.debug(logstring)
            lenlogstring=len(logstring)

        # retrieving one item via id does not return the usual structure with items[]
        # so we copy the element into the items list for easier handling
        if "items" not in data:
            data["items"] = [data.copy()]

        # collect them
        for item in data["items"]:
            if item["id"] in items:
                logger.error('found duplicate with id:' + str(item["id"]))
            items[item["id"]] = item
        offset += limit

        if not data.get("next"):
            break

    with open(type+".json", "w") as f:
        json.dump(items, f, indent=4)

    return items

# originally from cline
def bsale_stock_by_sku():
    """Return {sku: {name, price?, quantity}} summing stock over all offices."""
    logger.debug('calling bsale_stock_by_sku()')
    items, offset = {}, 0
    name_cache = {}
    while True:
        data = bsale_get("/stocks.json", {"limit": limit, "offset": offset, "expand": "[variant]"})
        # json.dump(data, sys.stdout, indent=4)
        # sys.exit(0)
        for item in data.get("items", []):
            v = item.get("variant") or {}
            sku = (v.get("code") or "").strip()
            logstring="sku="+sku

            variantsku = (v.get("code") or "").strip()
            if variantsku: logstring=logstring+", variant.sku="+variantsku

            if "href" in v: logstring=logstring+", variant.href="+v["href"]
            if not sku:
                logger.info(logstring)
                continue
            entry = items.get(sku)
            if entry is None:
                title = v.get("description") or sku
                logstring=logstring+", title="+title
                href = (v.get("product") or {}).get("href")
                if href:
                    logstring=logstring+",href="+href
                    if href not in name_cache:
                        name_cache[href] = bsale_get(href.replace(BSALE_URL, "")).get("name")
                    pname = name_cache[href]
                    if pname:
                        title = pname if title in ("", "Default Title") or title == pname else f"{pname} {title}"
                entry = items[sku] = {"title": title, "quantity": 0}
            entry["quantity"] += int(item.get("quantity") or 0)
            logstring=logstring+", quantity="+str(entry["quantity"])
            logger.info(logstring)
            json.dump(item, sys.stdout, indent=4)
            href = v["href"]
            url=re.sub("https://api.bsale.io/v1","",href)
            logger.debug("href: "+href)
            logger.debug("url: "+url)

            # for key in v:
            #     if isinstance(v[key], dict):
            #         for subkey in v[key]:
            #             if isinstance(subkey, str):
            #                 if subkey == "href":
            #                     subdata = bsale_get(re.sub("https://api.bsale.io/v1","",v[key][subkey]), {"limit": 50, "offset": offset, "expand": "[variant]"})
            #                     v["key"] = v[key] | subdata

            sys.exit(0)
        if not data.get("next"):
            break
        offset += 50
    return items
