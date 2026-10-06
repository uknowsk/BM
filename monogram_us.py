"""Monogram US adapter (https://www.monogram.com; brand 'Monogram', the GE Appliances luxury line).

monogram.com is a Salesforce Commerce (LWR/B2B) storefront rendered client-side. Its own front-end calls the
anonymous guest Commerce API, which is used here directly with plain `requests` (no browser, no login):
  GET <API>/search/products?categoryId=<id>&page=<n>&pageSize=200   -> product ids of a category (+ category name)
  GET <API>/products?ids=<up to 20 ids>&fields=<explicit list>      -> product fields (we request only customer-facing
                                                                      fields; the API would also return internal cost data,
                                                                      which is never requested or stored)
Per product the spec table is JSON in BWC_ProductSpecAndDetails__c ({"Spec": {SECTION: {Label: value|[values]}}}),
the PDFs in BWC_Documents__c (salsify, products-salsify.geappliances.com) and the hero image in BWC_Main_Image__c.
Parsing is delegated to ge_us.parse_product (same GE data model) via a synthetic productObj; the classification,
brand and sub keys are this module's. Only 'Active', non-accessory, listed products are returned.
robots.txt allows everything (User-agent: *, Allow: /). Requests are spaced >= 1 s.
Scope (coordinator decision): cooking only. Supported: sco (5-in-1 / Advantium), electric_oven (single/double/steam/
hearth wall ovens), microwave, induction (cooktops + induction pro ranges), gas_oven (pro gas / dual-fuel ranges),
gas_cooktop (gas cooktops/rangetops). Not supported: otr, radiant (no products), warming drawers, hoods; refrigerators
and laundry are intentionally not implemented.
"""
import json
import re
import sys
import time
from urllib.parse import urlparse

import requests

import common
import ge_us as ge
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Monogram"
COUNTRY = "us"
REGION = "na"
CURRENCY = "USD"
BASE = "https://www.monogram.com"
HOSTS = ("monogram.com",)
WEBSTORE = "0ZEKe000000XZAMOA4"
API = f"{BASE}/webruntime/api/services/data/v67.0/commerce/webstores/{WEBSTORE}"
QS = {"language": "en-US", "asGuest": "true", "htmlEncode": "false"}
DELAY_S = 1.0
ID_LIMIT = 20  # products?ids= limit of the API

# customer-facing fields only (never the internal cost/margin fields the object also has)
FIELDS = ["Name", "StockKeepingUnit", "Status__c", "Is_Accessory__c", "BWC_PLPIsVisible__c", "MGM_PLPIsVisible__c",
          "BWC_Brand__c", "UMRP__c", "BWC_Product_Type__c", "BWC_Configuration__c", "BWC_Product_Category__c",
          "BWC_Product_Marketing_Description__c", "BWC_Color__c", "BWC_WiFi_Connect__c"]
DETAIL_FIELDS = FIELDS + ["BWC_ProductSpecAndDetails__c", "BWC_Documents__c", "BWC_Main_Image__c", "BWC_Benefit_Copy__c",
                          "BWC_Claims_and_certifications__c"]

# category name -> id (verified against the name returned by the API on every use)
CATEGORIES = {
    "Professional Ranges": "0ZGKe000000XZC3OAO",
    "Cooktops & Rangetops": "0ZGKe000000XZBzOAO", "Wall Ovens": "0ZGKe000000XZC8OAO", "5-in-1 Ovens": "0ZGKe000000XZC4OAO",
    "Microwaves": "0ZGKe000000XZCDOA4",
}
# sub key -> (major, [category names to scan]); classify() decides the sub key of each product.
SUB_SOURCES: dict[str, tuple[str, list[str]]] = {
    "sco": ("cooking", ["5-in-1 Ovens", "Microwaves"]),
    "electric_oven": ("cooking", ["Wall Ovens"]),
    "microwave": ("cooking", ["Microwaves"]),
    "induction": ("cooking", ["Cooktops & Rangetops", "Professional Ranges"]),
    "gas_oven": ("cooking", ["Professional Ranges"]),
    "gas_cooktop": ("cooking", ["Cooktops & Rangetops"]),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)


class MonogramError(RuntimeError):
    """The Monogram API did not have the expected structure (site changed or blocked)."""


# ---------------------------------------------------------------- HTTP
_last_call = 0.0


def _check_url(url: str) -> None:
    u = urlparse(url)
    h = (u.hostname or "").lower()
    if u.scheme != "https" or not any(h == s or h.endswith("." + s) for s in HOSTS):
        raise MonogramError(f"unexpected url {url[:120]}")


def _get(path: str, params: dict) -> dict:
    """GET <API><path> as a guest, >= DELAY_S after the previous call; https + monogram.com only, no redirects."""
    global _last_call
    wait = DELAY_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()
    r = requests.get(API + path, params={**QS, **params}, headers={"User-Agent": common.UA}, timeout=30, allow_redirects=False)
    _check_url(r.url)
    if 300 <= r.status_code < 400:
        raise MonogramError(f"unexpected redirect (HTTP {r.status_code}) for {path}")
    if r.status_code == 404:
        raise MonogramError(f"HTTP 404 for {path}")
    if r.status_code != 200:
        raise MonogramError(f"bad status {r.status_code} for {path}: {r.text[:120]}")
    try:
        return r.json()
    except ValueError as e:
        raise MonogramError(f"non-JSON response for {path}") from e


def _category_product_ids(name: str) -> list[str]:
    cid, ids, page = CATEGORIES[name], [], 0
    while True:
        d = _get("/search/products", {"categoryId": cid, "page": page, "pageSize": 200, "fields": "Name"})
        got = ((d.get("categories") or {}).get("category") or {}).get("name")
        if got != name:
            raise MonogramError(f"category {cid} is {got!r}, expected {name!r} (site changed?)")
        pp = d.get("productsPage") or {}
        batch = [p["id"] for p in pp.get("products") or [] if p.get("id")]
        ids += batch
        if not batch or len(ids) >= int(pp.get("total") or 0):
            return list(dict.fromkeys(ids))
        page += 1


def _products(ids: list[str], fields: list[str]) -> list[dict]:
    d = _get("/products", {"ids": ",".join(ids), "fields": ",".join(fields)})
    prods = d.get("products") if isinstance(d, dict) else None
    if not isinstance(prods, list):
        raise MonogramError("products response has no 'products' list (structure changed?)")
    return [p for p in prods if isinstance(p, dict) and isinstance(p.get("fields"), dict)]


def _list_price(fields: dict) -> float | None:
    """List price = UMRP__c (matches the PDP's 'STARTING AT' price). The pricing API is not used: for products without
    UMRP it returns unlisted internal-looking values (e.g. 226 for a 2.2 cu ft microwave), so no price is better."""
    try:
        v = float(fields.get("UMRP__c"))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


# ---------------------------------------------------------------- classification (fields only: shared by discover and scrape)
_EXCLUDE = re.compile(r"wine|beverage|clear ice|ice press|warming|hood|ventilation|dishwasher|accessor", re.I)


def _f(fields: dict, key: str) -> str:
    return ge._clean(fields.get(key) or "")


def classify(fields: dict) -> str | None:
    """Sub key of a product from its fields, or None (accessory, wine, warming drawer, unsupported kind...)."""
    name, typ = _f(fields, "BWC_Product_Marketing_Description__c") or _f(fields, "Name"), _f(fields, "BWC_Product_Type__c")
    cats, text = _f(fields, "BWC_Product_Category__c"), f"{name} {typ}"
    if fields.get("Is_Accessory__c") not in (None, "No") or _EXCLUDE.search(text):
        return None
    sub = None
    if ">Cooking" in cats:
        t = text.lower()
        if re.search(r"advantium|five.in.one|speedcook", t):
            sub = "sco"
        elif "microwave" in t:
            sub = "microwave"
        elif "induction" in t:
            sub = "induction"
        elif re.search(r"rangetop|cooktop", t):
            sub = "gas_cooktop" if re.search(r"\bgas\b", t) else None
        elif "professional range" in t:
            sub = "gas_oven" if re.search(r"gas|dual.fuel", t) else None
        elif re.search(r"wall oven|steam oven|hearth oven|single oven|double oven", t):
            sub = "electric_oven"
    return sub if sub in SUPPORTED_SUBCATEGORIES else None


def _listed(fields: dict) -> bool:
    if fields.get("Status__c") != "Active" or "monogram" not in (fields.get("BWC_Brand__c") or "monogram").lower():
        return False
    return "true" in (str(fields.get("BWC_PLPIsVisible__c")).lower(), str(fields.get("MGM_PLPIsVisible__c")).lower())


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower()) or "product"


def _url(prod: dict) -> str:
    return f"{BASE}/product/{_slug(_f(prod['fields'], 'Name') or _f(prod['fields'], 'StockKeepingUnit'))}/{prod['id']}"


def _attrs(fields: dict) -> dict:
    out = {}
    if _f(fields, "BWC_Color__c"):
        out["finish"] = _f(fields, "BWC_Color__c")
    wifi = _f(fields, "BWC_WiFi_Connect__c")
    if wifi:
        out["wifi"] = not re.match(r"(none|no|not)\b", wifi, re.I)
    return out


# ---------------------------------------------------------------- discover
def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"Monogram US adapter does not support sub category {subcategory!r}")
    major, cat_names = SUB_SOURCES[subcategory]
    found: dict[str, Candidate] = {}
    seen: set[str] = set()
    for cat in cat_names:
        todo = [i for i in _category_product_ids(cat) if i not in seen]
        for i in range(0, len(todo), ID_LIMIT):
            if len(found) >= limit:
                return list(found.values())[:limit]
            chunk = todo[i:i + ID_LIMIT]
            seen.update(chunk)
            hits = [p for p in _products(chunk, FIELDS)
                    if _listed(p["fields"]) and classify(p["fields"]) == subcategory]
            for p in hits:
                f = p["fields"]
                sku = _f(f, "StockKeepingUnit").upper()
                if not sku or sku in found:
                    continue
                name = _f(f, "BWC_Product_Marketing_Description__c") or f"Monogram {sku}"
                found[sku] = Candidate(brand=BRAND, model_number=sku, name=name, url=_url(p),
                                       price_usd=_list_price(f), category=major, subcategory=subcategory,
                                       region=REGION, country=COUNTRY, currency=CURRENCY, attrs=_attrs(f))
    return list(found.values())[:limit]


# ---------------------------------------------------------------- parse (pure; fixture-testable)
def product_id_from_url(url: str) -> str:
    u = urlparse(url)
    m = re.fullmatch(r"/product/[^/]+/([A-Za-z0-9]{15,18})/?", u.path)
    if u.scheme != "https" or not (u.hostname or "").lower().endswith("monogram.com") or not m:
        raise ValueError(f"not a Monogram product URL: {url}")
    return m.group(1)


def _spec_fields(fields: dict) -> list[dict]:
    """Synthetic GE-style custom_fields: Spec_<SECTION>_<Label>[_n], Documents_<n>_<Label>, Product_Claims_<n>."""
    out: list[dict] = []
    raw = fields.get("BWC_ProductSpecAndDetails__c")
    try:
        spec = (json.loads(raw) if raw else {}).get("Spec") or {}
    except ValueError as e:
        raise MonogramError(f"BWC_ProductSpecAndDetails__c is not JSON: {e}") from e
    for section, rows in spec.items():
        if not isinstance(rows, dict) or section.upper() == "ACCESSORIES":
            continue
        for label, val in rows.items():
            vals = val if isinstance(val, list) else [val]
            for n, v in enumerate(vals):
                if isinstance(v, (dict, list)) or v in (None, ""):
                    continue
                out.append({"name": f"Spec_{section}_{label}" + (f"_{n + 1}" if len(vals) > 1 else ""), "value": str(v)})
    try:
        docs = (json.loads(fields.get("BWC_Documents__c") or "{}") or {}).get("Documents") or {}
    except ValueError:
        docs = {}
    out += [{"name": k, "value": v} for k, v in docs.items() if k.startswith("Documents_") and isinstance(v, str)]
    claims = [c for c in (fields.get("BWC_Claims_and_certifications__c") or "").split("~") if c.strip()]
    out += [{"name": f"Product_Claims_{n + 1}", "value": c.strip()} for n, c in enumerate(claims)]
    if _f(fields, "BWC_Color__c"):
        out.append({"name": "Color", "value": _f(fields, "BWC_Color__c")})
    return out


def parse_product(url: str, fields: dict, price: float | None) -> tuple[ProductRecord, list[RawSpec], list[tuple[str, str]]]:
    sub = classify(fields)
    if not sub:
        raise ValueError(f"not a supported Monogram product ({_f(fields, 'StockKeepingUnit')}: "
                         f"{_f(fields, 'BWC_Product_Type__c')!r})")
    major = SUB_SOURCES[sub][0]
    sku = _f(fields, "StockKeepingUnit").upper()
    po = {"sku": sku, "title": _f(fields, "BWC_Product_Marketing_Description__c") or sku,
          "custom_fields": _spec_fields(fields),
          "price": {"without_tax": {"value": price}} if price else {},
          "category": [path for path in [_ge_category(sub)]],
          "warranty": fields.get("BWC_Benefit_Copy__c") or "",
          "main_image": {"data": fields.get("BWC_Main_Image__c") or ""}, "images": []}
    record, raw, links = ge.parse_product(url, po)
    record.brand, record.category, record.subcategory = BRAND, major, sub
    record.region, record.country, record.currency = REGION, COUNTRY, CURRENCY
    if record.wifi_evidence:
        record.wifi_evidence = record.wifi_evidence.replace("GE spec table", "Monogram spec table")
    return record, [r.model_copy(update={"brand": BRAND}) for r in raw], links


def _ge_category(sub: str) -> str:
    """GE-style category path so ge.parse_product can run; its own inference is overridden by parse_product above."""
    key = sub if sub in ge.SUB_SOURCES else "french_door"
    sources = ge.SUB_SOURCES[key][1]
    return next((p for p, pred in sources if pred is None), sources[0][0]).replace(">", "/")


# ---------------------------------------------------------------- scrape
def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    pid = product_id_from_url(url)
    prods = _products([pid], DETAIL_FIELDS)
    if not prods or prods[0].get("id", "")[:15] != pid[:15]:
        raise MonogramError(f"product {pid} not found")
    fields = prods[0]["fields"]
    product, raw, links = parse_product(url, fields, _list_price(fields))
    docs = []
    for dtype, link in links:
        if not ge._pdf_url_ok(link):
            print(f"skipped {dtype}: PDF url not https on an allowed host ({link[:100]})", file=sys.stderr)
            continue
        time.sleep(DELAY_S)
        d = common.download_pdf(BRAND, ge._safe_name(product.model_number), dtype, link)
        if d:
            docs.append(d)
    return product, docs, raw
