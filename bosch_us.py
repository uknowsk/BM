"""Bosch US adapter (catalog.py contract): refrigerators, washers/dryers, cooking.

Data source: bosch-home.com/us/en is a Next.js app. Category pages and product pages are server-rendered and embed
the data as flight chunks (`self.__next_f.push([1,"<json string>"])`):
  * category page: `"productList":{"items":[...],"total":N,"page":P}`, 12 items per page, `?pageNumber=N` (1-based);
    items carry productCode, urlPath, productName, price.amount.
  * product page: `"specifications":[{"name":"General",...}]`, `"technicalDocuments":[...]`, `"pricing":{...}`,
    `"highlights":[...]`, `"title":{"valueClass","headline"}`.
HTML is fetched with fetch() from inside a real browser page (same-origin); PDFs go through common.download_pdf
(media3.bsh-group.com, a subdomain of an allowlisted host).
"""
import json
import math
import os
import re
import sys
import time
from contextlib import contextmanager
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

import common
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Bosch"
BASE = "https://www.bosch-home.com/us/en"
PAGE_HOSTS = ("bosch-home.com",)
PDF_HOSTS = ("bosch-home.com", "bsh-group.com")
DELAY_S = 1.0
MAX_PAGES = 15
CONSENT_DECLINE, CONSENT_ACCEPT = "Decline all", "Accept all"

# sub key -> (major key, category pages whose listings make up the sub group). Keys Bosch US does not sell
# (side_by_side, top_freezer, compact, top_load, laundry_center, gas_oven) are deliberately absent.
# Assumptions: sco = speed ovens + combination (oven + microwave) wall ovens; gas_oven = gas and dual-fuel ranges
# (no gas wall oven exists); radiant = electric ranges + electric cooktops; induction = induction ranges + cooktops;
# electric_oven = wall ovens without microwave/speed function; microwave = built-in + drawer (OTR has its own key).
# Cooking category pages overlap (a speed oven is listed under wall ovens), so classify_cooking() - one rule shared
# by discover() and scrape() - decides the sub key of every listed product from its URL path + name.
SUB_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "french_door": ("refrigerator", ("refrigerators/french-door",)),
    "bottom_freezer": ("refrigerator", ("refrigerators/bottom-freezer",)),
    "built_in": ("refrigerator", ("refrigerators/bottom-freezer/built-in", "refrigerators/single-door/built-in")),
    "front_load": ("washer", ("washers-and-dryers/washing-machines",)),
    "dryer": ("washer", ("washers-and-dryers/tumble-dryers",)),
    "microwave": ("cooking", ("cooking-baking/microwaves/built-in-microwaves",
                              "cooking-baking/microwaves/drawer-microwaves")),
    "otr": ("cooking", ("cooking-baking/microwaves/over-the-range-microwaves",)),
    "sco": ("cooking", ("cooking-baking/wall-ovens", "cooking-baking/microwaves/built-in-microwaves")),
    "gas_oven": ("cooking", ("cooking-baking/ranges",)),
    "electric_oven": ("cooking", ("cooking-baking/wall-ovens",)),
    "induction": ("cooking", ("cooking-baking/induction-electric-cooktops/induction-cooktops",
                              "cooking-baking/ranges")),
    "radiant": ("cooking", ("cooking-baking/induction-electric-cooktops/electric-cooktops",
                            "cooking-baking/ranges")),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)

ROOT_MAJOR = {"refrigerators": "refrigerator", "washers-and-dryers": "washer", "cooking-baking": "cooking"}
# product-URL prefix (after /product/) -> sub key, for scrape(); first match wins
URL_SUBS = (("washers-and-dryers/tumble-dryers", "dryer"), ("washers-and-dryers/", "front_load"))
_NOT_A_PRODUCT = re.compile(r"accessor|cleaning-and-care|spare", re.I)
_CODE = re.compile(r"^[A-Za-z0-9._-]{3,40}$")

DOC_TYPES = {"energy-label": "EnergyGuide", "product-specification": "SpecSheet",
             "user-manuals": "Manual", "installation-instruction": "Installation"}


# ---------------------------------------------------------------- browser

def _modes() -> list[bool]:
    """Headless flags to try, in order. FRIDGE_BROWSER_MODE (auto | headless | visible) is read on every call."""
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return [True]
    if mode == "visible":
        return [False]
    return [True, False]


def _host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _check_final_url(url: str) -> None:
    if urlparse(url).scheme != "https" or not _host_in(url, PAGE_HOSTS):
        raise ValueError(f"unexpected host after navigation: {url[:120]}")


def _consent_visible(page) -> bool:
    try:
        return page.get_by_role("button", name=CONSENT_ACCEPT).first.is_visible(timeout=500)
    except PlaywrightError:
        return False


def _dismiss_consent(page) -> None:
    """Decline non-essential cookies first; accept only if the dialog is still there afterwards."""
    for label in (CONSENT_DECLINE, CONSENT_ACCEPT):
        if label == CONSENT_ACCEPT and not _consent_visible(page):
            return
        try:
            page.get_by_role("button", name=label).first.click(timeout=2500)
            page.wait_for_timeout(300)
        except PlaywrightTimeoutError:
            if label == CONSENT_DECLINE:
                return  # no banner


class _Session:
    """Browser page on bosch-home.com; fetches same-origin HTML with the page's cookies."""

    def __init__(self, page):
        self.page = page

    def html(self, url: str) -> str:
        _check_final_url(url)
        status, body = self.page.evaluate("async u=>{const r=await fetch(u);return [r.status,await r.text()]}", url)
        if status != 200:
            raise RuntimeError(f"Bosch fetch {url[:100]} -> HTTP {status}")
        return body


def _close_quietly(browser) -> None:
    """Close a browser in an error path; a failing close must not abort the mode-fallback loop."""
    if browser is None:
        return
    try:
        browser.close()
    except PlaywrightError as e:
        print(f"bosch_us: browser.close() failed: {e}", file=sys.stderr)


def _connect(p, url: str):
    """Return (browser, page) on url; headless first, visible fallback (auto mode)."""
    _check_final_url(url)
    last_err: Exception | None = None
    modes = _modes()
    for headless in modes:
        browser = None
        try:
            browser = common.launch_browser(p, headless=headless)
            page = browser.new_context(user_agent=common.UA).new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            _check_final_url(page.url)
            try:  # slow render is not "blocked": wait for real content before judging
                page.wait_for_function("document.body && document.body.innerText.length >= 200", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            _dismiss_consent(page)
            if common.looks_blocked(resp.status if resp else None, page.inner_text("body")):  # visible text only
                raise RuntimeError(f"blocked (status {resp.status if resp else None})")
            return browser, page
        except (PlaywrightTimeoutError, PlaywrightError, RuntimeError, ValueError) as e:
            last_err = e
            _close_quietly(browser)
        except BaseException:
            _close_quietly(browser)
            raise
    raise RuntimeError(f"Bosch page unreachable (modes tried: {modes}): {last_err}")


@contextmanager
def _session(url: str):
    with sync_playwright() as p:
        browser, page = _connect(p, url)
        try:
            yield _Session(page)
        finally:
            browser.close()


# ---------------------------------------------------------------- flight payload

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


def flight_text(html: str) -> str:
    """Concatenate the JSON-string payload of every self.__next_f.push([1,"..."]) chunk."""
    chunks = _PUSH.findall(html)
    if not chunks:
        raise ValueError("Bosch page has no self.__next_f flight payload chunks")
    return "".join(json.loads(c) for c in chunks)


def _json_after(text: str, marker: str, open_char: str = "["):
    """Decode the JSON value that starts at the first `open_char` after `marker`; raise if marker is missing."""
    i = text.find(marker)
    if i < 0:
        raise ValueError(f"Bosch payload marker not found: {marker!r}")
    return json.JSONDecoder().raw_decode(text, text.index(open_char, i))[0]


def _title(flight: str) -> tuple[str, str]:
    """(valueClass, headline) of the product title object, JSON-decoded (handles escaped quotes)."""
    m = re.search(r'"title":(?=\{"valueClass":)', flight)
    if not m:
        raise ValueError("Bosch title marker not found")
    obj = json.JSONDecoder().raw_decode(flight, m.end())[0]
    return str(obj.get("valueClass") or ""), str(obj.get("headline") or "")


def _clean_name(parts: list[str]) -> str:
    return re.sub(r"(\d+)_IN\b", r"\1 in", " ".join(p.strip() for p in parts if p and p.strip()))


# ---------------------------------------------------------------- discover

def parse_listing(html: str, root: str) -> tuple[list[dict], int]:
    """(raw productList items, total) of one category page. Raises if the payload shape is unrecognised."""
    pl = _json_after(flight_text(html), '"productList":{"items":[', "{")
    if not isinstance(pl.get("items"), list) or not isinstance(pl.get("total"), int):
        raise ValueError("Bosch productList structure unrecognised")
    return pl["items"], pl["total"]


def item_to_candidate(item: dict, major: str, sub: str, root: str) -> Candidate | None:
    """Candidate for a listing item, or None if invalid or (refrigerators) classified under another sub key."""
    code, path = item.get("productCode"), item.get("urlPath") or ""
    if not code or not _CODE.match(code) or not path.startswith(f"/product/{root}/") or _NOT_A_PRODUCT.search(path):
        return None
    headline = _clean_name(item.get("productName") or [])
    if root == "refrigerators" and classify_fridge(path, headline) != sub:
        return None
    if root == "cooking-baking" and classify_cooking(path, headline) != sub:
        return None
    url = urljoin(BASE + "/", path.lstrip("/"))
    if not _host_in(url, PAGE_HOSTS):
        print(f"dropped off-domain candidate {code}: {url[:100]}", file=sys.stderr)
        return None
    amount = (item.get("price") or {}).get("amount")
    return Candidate(brand=BRAND, model_number=code, name=_clean_name(item.get("productName") or []) or code,
                     url=url, price_usd=float(amount) if amount else None, category=major, subcategory=sub)


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUB_SOURCES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Bosch US")
    major, pages = SUB_SOURCES[subcategory]
    found: dict[str, Candidate] = {}
    with _session(f"{BASE}/category/{pages[0]}") as s:
        for cat in pages:
            root = cat.split("/")[0]
            seen, page_no, total, kept_before = 0, 1, 1, len(found)
            while seen < total and page_no <= MAX_PAGES and len(found) < limit:
                url = f"{BASE}/category/{cat}" + (f"?pageNumber={page_no}" if page_no > 1 else "")
                items, total = parse_listing(s.html(url), root)
                if not items:
                    break
                seen += len(items)
                for it in items:
                    c = item_to_candidate(it, major, subcategory, root)
                    if c:
                        found.setdefault(c.model_number, c)
                page_no += 1
                if seen < total:
                    time.sleep(DELAY_S)
            print(f"bosch_us: discover({subcategory}) {cat}: {seen} listed, {len(found) - kept_before} kept "
                  f"(rest invalid or classified under another sub key)", file=sys.stderr)
            if len(found) >= limit:
                break
            time.sleep(DELAY_S)
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape

def model_from_url(url: str) -> tuple[str, str]:
    """(model, root) from https://www.bosch-home.com/us/en/product/<root>/.../<MODEL>; ValueError otherwise."""
    u = urlparse(url)
    parts = [x for x in u.path.split("/") if x]
    if (u.scheme != "https" or not _host_in(url, PAGE_HOSTS) or parts[:3] != ["us", "en", "product"]
            or len(parts) < 5 or not _CODE.match(parts[-1]) or parts[3] not in ROOT_MAJOR):
        raise ValueError(f"not a supported Bosch US product URL: {url}")
    return parts[-1], parts[3]


def _frac(s: str) -> float:
    """'35 5/8' -> 35.625"""
    total = 0.0
    for part in s.split():
        if "/" in part:
            a, b = part.split("/")
            total += int(a) / int(b)
        else:
            total += float(part)
    return total


def _num(pattern: str, text: str) -> float | None:
    return common.num(pattern, text, re.I)


def _flatten(sections: list[dict]) -> list[tuple[str, dict, str]]:
    """(section name, row, display value) per spec row; booleans normalised, unit appended."""
    out = []
    for sec in sections:
        for r in sec["specifications"]:
            val = " ".join(r["value"]["text"].split())
            if val.startswith("specifications.translatedBoolean."):
                val = val.rsplit(".", 1)[1].capitalize()
            if not val:
                continue
            out.append((sec["name"], r, val + (f" {r['unit']}" if r.get("unit") else "")))
    return out


def _noise(t) -> str:
    """Strip registered/trademark marks; line breaks inside a value become ' | '."""
    lines = [" ".join(l.replace("®", "").replace("™", "").split()) for l in str(t).splitlines()]
    return " | ".join(l for l in lines if l)


def full_spec_table(sections: list[dict]) -> dict[str, str]:
    """EVERY Specs & Details row: 'Section > Label' -> full value (+ unit), multi-line values joined with ' | '."""
    out: dict[str, str] = {}
    for sec in sections:
        for r in sec["specifications"]:
            val = _noise(r["value"]["text"])
            if val.startswith("specifications.translatedBoolean."):
                val = val.rsplit(".", 1)[1].capitalize()
            if not val:
                continue
            val += f" {r['unit']}" if r.get("unit") else ""
            key = f"{_noise(sec['name'])} > {_noise(r['name']['text'])}"
            out[key] = f"{out[key]} | {val}" if key in out else val
    return out


IMAGE_HOSTS = ("bsh-group.com", "bosch-home.com")


def main_image_url(flight: str) -> str | None:
    """Hero image: first entry of the page's schema.org `productJsonLd.image` list (absolute https on the BSH CDN)."""
    m = re.search(r'"productJsonLd":(?=\{)', flight)
    if not m:
        return None
    try:
        img = json.JSONDecoder().raw_decode(flight, m.end())[0].get("image")
    except ValueError:
        return None
    for raw in img if isinstance(img, list) else [img]:
        if isinstance(raw, str) and raw.strip():
            url = urljoin("https://www.bosch-home.com/us/", raw.strip())
            return url if urlparse(url).scheme == "https" and _host_in(url, IMAGE_HOSTS) else None
    return None


def _bool(v: str | None) -> bool | None:
    if not v:
        return None
    return True if v.lower().startswith("yes") else False if v.lower().startswith("no") else None


def classify_fridge(path: str, headline: str) -> str | None:
    """Refrigerator sub key from the product's URL path + headline. Single source of truth for discover() and
    scrape(); rules are mutually exclusive by precedence: built-in, then French door, then bottom freezer."""
    h = (headline or "").lower().replace("built in", "built-in")
    if "built-in" in h or "/built-in/" in path.lower():
        return "built_in"
    if "french door" in h:
        return "french_door"
    return "bottom_freezer" if "bottom" in h else None


def classify_cooking(path: str, headline: str) -> str | None:
    """Cooking sub key from the product's URL path + headline. Single source of truth for discover() and
    scrape(); mutually exclusive by precedence: otr, sco, microwave, induction, gas_oven, radiant, electric_oven."""
    rel = path.split("/product/", 1)[-1].lower()
    h = (headline or "").lower()
    if rel.startswith("cooking-baking/microwaves/over-the-range"):
        return "otr"
    if rel.startswith(("cooking-baking/wall-ovens/", "cooking-baking/microwaves/")) and (
            "/speed-ovens/" in rel or re.search(r"speed oven|combination oven|microwave combination", h)):
        return "sco"
    if rel.startswith("cooking-baking/microwaves/"):
        return "microwave"
    if "/induction-ranges/" in rel or rel.startswith("cooking-baking/induction-electric-cooktops/induction-cooktops"):
        return "induction"
    if rel.startswith("cooking-baking/ranges/"):
        if "/gas-ranges/" in rel or "/dual-fuel-ranges/" in rel or re.search(r"(?<![a-z])gas(?![a-z])|dual fuel", h):
            return "gas_oven"
        return "radiant" if "electric" in rel or "electric" in h else None
    if rel.startswith("cooking-baking/induction-electric-cooktops/electric-cooktops"):
        return "radiant"
    return "electric_oven" if rel.startswith("cooking-baking/wall-ovens/") else None


def _sub_from_page(root: str, path: str, headline: str) -> str | None:
    if root == "refrigerators":
        return classify_fridge(path, headline)
    if root == "cooking-baking":
        return classify_cooking(path, headline)
    rel = path.split("/product/", 1)[-1]
    return next((sub for prefix, sub in URL_SUBS if rel.startswith(prefix)), None)


# keys folded into typed ProductRecord fields (kept out of extra_specs)
_CONSUMED = {"COL_MAIN", "ENERGY_STAR_QUALIFIED", "DIM_US", "WEIGHT_NET_US", "WEIGHT_GROSS_US", "VOLTAGE",
             "ENERGY_ANNUAL_US", "HOMECONNECTABLE", "HOMECONNECT_TYPE"}
_FRIDGE_CONSUMED = {"CAP_GROSS_CUBIC_FEET", "CAP_REFR_GROSS_US", "CAP_FREEZ_GROSS_US", "ICE_MAKER"}


def parse_product(model: str, url: str, root: str, flight: str) -> tuple[ProductRecord, list[RawSpec]]:
    """Product record (web data only, no PDF enrichment) and the full spec table, from a PDP flight payload."""
    sections = _json_after(flight, '"specifications":[{"name"')
    pricing = _json_after(flight, '"pricing":', "{")
    highlights = _json_after(flight, '"highlights":[')
    try:
        title_class, headline = _title(flight)
    except ValueError:
        raise ValueError(f"Bosch title marker not found for {model}") from None
    rows = _flatten(sections)
    spec = {}
    for _sec, r, val in rows:
        spec.setdefault(r["key"], val)
    major = ROOT_MAJOR[root]
    fridge = major == "refrigerator"

    dims = re.findall(r"\d+(?: \d+/\d+)?", " ".join(spec.get("DIM_US", "").split()))
    dim_label = next((r["name"]["text"] for _s, r, _v in rows if r["key"] == "DIM_US"), "")
    hwd = len(dims) == 3 and "HxWxD" in dim_label.replace(" ", "")
    net, gross = _num(r"([\d.]+)", spec.get("WEIGHT_NET_US", "")), _num(r"([\d.]+)", spec.get("WEIGHT_GROSS_US", ""))
    hc, hc_type = spec.get("HOMECONNECTABLE"), spec.get("HOMECONNECT_TYPE")
    energy = spec.get("ENERGY_ANNUAL_US", "")

    extra = {}
    for _sec, r, val in rows:
        if r["key"] in _CONSUMED or (fridge and r["key"] in _FRIDGE_CONSUMED):
            continue
        extra.setdefault(r["name"]["text"].strip(), val)
    if net is None and gross is not None:
        extra["Weight basis"] = "gross (net weight not listed on web page)"

    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=_clean_name([title_class, headline]) or model,
        category=major, subcategory=_sub_from_page(root, urlparse(url).path, headline),
        product_url=url, finish_color=spec.get("COL_MAIN"),
        price_usd=float(pricing["amount"]) if pricing.get("amount") is not None else None,
        height_in=_frac(dims[0]) if hwd else None, width_in=_frac(dims[1]) if hwd else None,
        depth_in=_frac(dims[2]) if hwd else None,
        weight_lb=net if net is not None else gross,
        voltage_v=spec["VOLTAGE"].removesuffix(" V") if "VOLTAGE" in spec else None,
        energy_kwh_year=_num(r"([\d.,]+)", energy) if "kwh" in energy.lower() else None,
        energy_star=_bool(spec.get("ENERGY_STAR_QUALIFIED")),
        wifi_supported=_bool(hc),
        wifi_evidence=(f"Home Connect: {hc}" + (f"; features: {hc_type}" if hc_type else "")) if hc else None,
        pod_features=[h["headline"]["text"] for h in highlights
                      if isinstance(h.get("headline"), dict) and h["headline"].get("text")],
        extra_specs={**full_spec_table(sections), **extra},  # full sectioned table + legacy flat labels
        image_url=main_image_url(flight),
    )
    if fridge:
        record = record.model_copy(update=dict(
            door_style=(re.match(r"(.*?)\s+Refrigerator\b", headline) or [None, None])[1],
            capacity_total_cuft=_num(r"([\d.]+)", spec.get("CAP_GROSS_CUBIC_FEET", "")),
            capacity_fridge_cuft=_num(r"([\d.]+)", spec.get("CAP_REFR_GROSS_US", "")),
            capacity_freezer_cuft=_num(r"([\d.]+)", spec.get("CAP_FREEZ_GROSS_US", "")),
            ice_maker=_bool(spec.get("ICE_MAKER"))))
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=sec, key=r["name"]["text"].strip(), value=val)
           for sec, r, val in rows]
    return record, raw


def pick_docs(technical_documents: list[dict]) -> list[tuple[str, str]]:
    """(our doc_type, url): one https PDF per type on an allowed host, one entry per distinct URL.
    Spec sheet: the first non-pregenerated sheet (the '/specsheet/<locale>/<model>.pdf' one is regenerated
    rarely and is usually older); manuals etc.: first listed."""
    by_type: dict[str, list[str]] = {}
    for d in technical_documents:
        dtype, u = DOC_TYPES.get(d.get("titleKey")), d.get("url") or ""
        if (dtype and u.lower().endswith(".pdf") and urlparse(u).scheme == "https" and _host_in(u, PDF_HOSTS)
                and u not in by_type.setdefault(dtype, [])):
            by_type[dtype].append(u)
    out, used = [], set()
    for dtype in DOC_TYPES.values():
        urls = by_type.get(dtype) or []
        if dtype == "SpecSheet":
            urls = [u for u in urls if "/specsheet/" not in u] + [u for u in urls if "/specsheet/" in u]
        if urls and urls[0] not in used:
            used.add(urls[0])
            out.append((dtype, urls[0]))
    return out


def parse_spec_sheet(text: str) -> dict:
    """Electrical / weight / dispenser / kWh from the spec-sheet PDF text (label on one line, value on the next)."""
    t = text.replace("\r", "")
    disp = re.search(r"Internal water dispenser\s*\n\s*(Yes|No)", t)
    volts = re.search(r"Volts\s*\n\s*([\d/.\- ]+?)\s*V\b", t)
    return dict(
        voltage_v=volts.group(1).strip() if volts else None,
        amps=_num(r"\nCurrent\s*\n\s*([\d.]+)\s*A", t),
        frequency_hz=_num(r"Frequency\s*\n\s*([\d.]+)\s*Hz", t),
        weight_lb=_num(r"Net weight\s*\n\s*([\d.,]+)\s*lbs", t),
        water_dispenser=disp.group(1) == "Yes" if disp else None,
        energy_kwh_year=_num(r"Energy consumption\s*\n\s*([\d.,]+)\s*kWh", t))


def parse_energy_guide_kwh(text: str) -> float | None:
    return _num(r"([\d.,]+)\s*kWh\s*\n\s*Estimated Yearly Electricity Use", text.replace("\r", ""))


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    model, root = model_from_url(url)
    with _session(url) as s:
        flight = flight_text(s.html(url))
    record, raw = parse_product(model, url, root, flight)
    docs: list[DocumentRecord] = []
    texts: dict[str, str] = {}
    for dtype, durl in pick_docs(_json_after(flight, '"technicalDocuments":[')):
        rec = common.download_pdf(BRAND, model, dtype, durl)
        if rec:
            docs.append(rec)
            if dtype in ("SpecSheet", "EnergyGuide"):
                texts[dtype] = common.pdf_text(common.ROOT / rec.local_path)
    sheet = parse_spec_sheet(texts["SpecSheet"]) if "SpecSheet" in texts else {}
    upd = {k: v for k, v in sheet.items()
           if v is not None and getattr(record, k) is None and k in ("voltage_v", "amps", "frequency_hz")}
    if root == "refrigerators":
        if sheet.get("water_dispenser") is not None:
            upd["water_dispenser"] = sheet["water_dispenser"]
    if sheet.get("weight_lb") is not None and (record.weight_lb is None or "Weight basis" in record.extra_specs):
        upd["weight_lb"] = sheet["weight_lb"]  # net weight beats the web page's gross weight / fills a gap
    if record.energy_kwh_year is None:
        kwh = parse_energy_guide_kwh(texts.get("EnergyGuide", "")) or sheet.get("energy_kwh_year")
        if kwh is None and root == "refrigerators":
            print(f"warning: no kWh/year found for {model}", file=sys.stderr)
        upd["energy_kwh_year"] = kwh
    if "weight_lb" in upd and "Weight basis" in record.extra_specs:
        upd["extra_specs"] = {k: v for k, v in record.extra_specs.items() if k != "Weight basis"}
    return record.model_copy(update=upd), docs, raw
