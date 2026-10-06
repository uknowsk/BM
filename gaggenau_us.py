"""Gaggenau US adapter (catalog.py contract): cooking only (cooktops, ovens). Also the shared Gaggenau core for gaggenau_de.

gaggenau.com/<cc>/<lang> is the same BSH Next.js platform as thermador.com (core in thermador_us.py), but a pure
marketing site: product pages are `/<cc>/<lang>/mkt-product/<path>/<MODEL>`, there is NO price, and the "specifications"
payload is one unlabeled bullet list (value text only, no labels). Category listings are client-rendered/mixed with
accessories, so discover() reads the robots-listed `/<cc>/sitemap.xml` (one request) and classifies products from the
URL path + model prefix. Robots.txt (checked 2026-10-06) disallows only /manual/, */search/*, */comparison/*, graphql
and ajax paths, none of which is requested. Facts (weight, capacity, rating, energy ...) are parsed out of the bullets;
the bullets themselves are kept verbatim as RawSpec rows and joined into one extra_specs value.
"""
import re
import sys
from urllib.parse import urlparse

import units
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec
from thermador_us import (Site, clean_name, download_docs, flight_text, host_in, json_after, main_image_url, noise,
                          session, spec_rows, title_of)

BRAND = "Gaggenau"
COUNTRY = "us"
REGION = "na"
CURRENCY = "USD"
SITE = Site(BRAND, "gaggenau_us", "https://www.gaggenau.com/us/en", ("gaggenau.com",),
            ("gaggenau.com", "bsh-group.com"), ("bsh-group.com", "gaggenau.com"), "Decline all", "Accept all")

# Gaggenau US sells no gas ovens/ranges, over-the-range microwaves or radiant cooktops (cooking only: the
# coordinator limited this round to the cooking sub keys). Assumptions: sco = combi-microwave ovens (BM/GM);
# microwave = plain microwave ovens (MW); electric_oven = ovens + combi-steam ovens (no microwave function).
SUPPORTED_SUBCATEGORIES = {"microwave", "sco", "gas_cooktop", "electric_oven", "induction"}
COOKTOP_TOPS = ("cooktops", "kochfelder")
OVEN_TOPS = ("ovens", "backoefen")
_MODEL = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
_SUB_LABEL = {"gas_cooktop": "Gas cooktop", "induction": "Induction cooktop", "radiant": "Ceramic cooktop",
              "sco": "Combi-microwave oven", "electric_oven": "Oven", "microwave": "Microwave oven"}


def classify(path: str, name: str = "") -> str | None:
    """Sub key from a /mkt-product/ path (+ headline). The model prefix wins over the category path because Gaggenau's
    own category paths are sometimes stale. Shared by discover() and scrape() of the US and DE adapters.
    None = not one of our groups (refrigerators, ventilation, dishwashers, grills/teppanyaki, drawers, accessories)."""
    rel = path.split("/mkt-product/", 1)[-1].strip("/").lower()
    parts = rel.split("/")
    top, model = parts[0], parts[-1].upper()
    prefix = re.match(r"[A-Z]+", model)
    prefix = prefix.group(0) if prefix else ""
    if top in COOKTOP_TOPS or (len(parts) == 1 and prefix in ("CG", "VG", "CI", "CX", "VI", "CV", "CE")):
        if prefix in ("CG", "VG"):
            return "gas_cooktop"
        if prefix in ("CI", "CX", "VI", "CV"):
            return "induction"
        return "radiant" if prefix == "CE" else None
    if top in OVEN_TOPS or (len(parts) == 1 and prefix in ("BO", "BS", "BM", "GO", "GS", "GM", "MW", "EB")):
        if re.search(r"warming|waerme|vacuum|vakuum", rel) or prefix in ("BV", "GV", "BW", "GW"):
            return None
        if prefix == "MW" or "mikrowellenoefen" in rel:
            return "microwave"
        if prefix in ("BM", "GM") or re.search(r"combimicrowave|mikrowellen-backoefen|combi-microwave", rel):
            return "sco"
        return "electric_oven"
    return None


def candidate_name(path: str, sub: str) -> str:
    model = path.rstrip("/").rsplit("/", 1)[-1]
    steam = re.search(r"steam|dampf", path.lower())
    label = "Combi-steam oven" if sub == "electric_oven" and steam else _SUB_LABEL.get(sub, sub)
    return f"{label} {model}"


def sitemap_urls(s) -> list[str]:
    """All https product URLs of this market in the (robots-listed) sitemap, in sitemap order, deduplicated."""
    country = s.site.base.split("/")[3]
    xml = s.text(f"https://www.gaggenau.com/{country}/sitemap.xml")
    seen, out = set(), []
    for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
        if u.startswith(s.site.base + "/mkt-product/") and u not in seen and host_in(u, s.site.page_hosts):
            seen.add(u)
            out.append(u)
    return out


def candidates_from_urls(urls: list[str], site: Site, sub: str, major: str, limit: int, region: str, country: str,
                         currency: str) -> list[Candidate]:
    out: dict[str, Candidate] = {}
    for u in urls:
        model = u.rsplit("/", 1)[-1]
        if not _MODEL.match(model) or model in out or classify(u, "") != sub:
            continue
        name = candidate_name(u, sub)
        out[model] = Candidate(brand=BRAND, model_number=model, name=name, url=u, category=major, subcategory=sub,
                               region=region, country=country, currency=currency, price_usd=None, price_local=None)
        if len(out) >= limit:
            break
    return list(out.values())


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Gaggenau US")
    top = "cooktops" if subcategory in ("gas_cooktop", "induction") else "ovens"
    with session(SITE, f"{SITE.base}/mkt-category/{top}") as s:
        urls = sitemap_urls(s)
    print(f"gaggenau_us: discover({subcategory}): {len(urls)} product urls in sitemap", file=sys.stderr)
    return candidates_from_urls(urls, SITE, subcategory, "cooking", limit, REGION, COUNTRY, CURRENCY)


# ---------------------------------------------------------------- scrape

def model_from_url(url: str, site: Site = SITE, prefix: str = "us/en") -> str:
    """Model number from https://www.gaggenau.com/<cc>/<lang>/mkt-product/<path>/<MODEL>; ValueError otherwise."""
    u = urlparse(url)
    parts = [x for x in u.path.split("/") if x]
    head = prefix.split("/")
    if (u.scheme != "https" or not host_in(url, site.page_hosts) or parts[:3] != head + ["mkt-product"]
            or len(parts) < 4 or ".." in parts or not _MODEL.match(parts[-1])):
        raise ValueError(f"not a supported {site.brand} product URL: {url}")
    return parts[-1]


def _first(pattern: str, text: str, flags: int = re.I) -> str | None:
    m = re.search(pattern, text, flags)
    return (m.group(1) if m.re.groups else m.group(0)) if m else None


def facts_en(bullets: list[str], headline: str) -> tuple[dict, dict]:
    """(ProductRecord field updates, extra_specs) read from the English bullet list and headline."""
    text = "\n".join(bullets)
    upd: dict = {}
    extra: dict = {}
    w = _first(r"(\d{2})\s*(?:''|\"|″)", headline)
    if w:
        upd["width_in"] = float(w)
    lbs = _first(r"weight[^\n]*?([\d.,]+)\s*lbs?", text)
    kg = _first(r"weight[^\n]*?([\d.,]+)\s*kg", text)
    if lbs:
        upd["weight_lb"] = float(lbs.replace(",", ""))
    elif kg:
        upd["weight_lb"] = round(units.kg_to_lb(float(kg.replace(",", ""))), 1)
    m = re.search(r"(\d{3}(?:\s*/\s*\d{3})?)\s*V\s*/\s*(\d{2})\s*Hz", text, re.I)
    if m:
        upd["voltage_v"] = re.sub(r"\s", "", m.group(1))
        upd["frequency_hz"] = float(m.group(2))
    amps = _first(r"total amps?:?\s*([\d.]+)\s*A", text)
    if amps:
        upd["amps"] = float(amps)
    kwh = units.parse_kwh_per_year(text)
    if kwh is not None:
        upd["energy_kwh_year"] = kwh
    if re.search(r"energy\s*star", text, re.I):
        upd["energy_star"] = True
    rating = _first(r"total rating[^\n]*", text)
    if rating:
        extra["Technical data > Total rating"] = noise(rating.strip(" ."))
    cap = _first(r"cavity capacity\s*([\d.]+)\s*cu", text)
    if cap:
        extra["Technical data > Cavity capacity (cu ft)"] = cap
    burners = _first(r"on\s*(\d+)\s*burners", text)
    if burners:
        extra["Technical data > Burners"] = burners
    mw = _first(r"microwave[^\n]*?(\d{3,4})\s*W\b", text)
    if mw:
        extra["Technical data > Microwave power (W)"] = mw
    cable = _first(r"(connecting|electrical connection) cable[^\n]*", text)
    if cable:
        extra["Technical data > Connecting cable"] = noise(re.search(r"(connecting|electrical connection) cable[^\n]*", text, re.I).group(0).strip(" ."))
    hc = re.search(r"home connect|remote control and monitoring", text, re.I)
    if hc:
        upd["wifi_supported"] = True
        upd["wifi_evidence"] = hc.group(0)
    return upd, extra


def build_record(site: Site, model: str, url: str, flight: str, facts, market: dict,
                 translate=None) -> tuple[ProductRecord, list[RawSpec]]:
    """ProductRecord + RawSpec rows from a Gaggenau product-page payload. `facts(bullets, headline)` is the language
    specific parser. `translate(list[str])` (German adapter) maps the headline and the first six bullets (the
    pod_features) to English; the full bullet list then stays in the original language under a '(original)' label
    (translating 70 long sentences per product with the local LLM is too slow)."""
    sections = json_after(flight, '"specifications":[{"name"')
    rows = spec_rows(sections)
    bullets = [r["value"] for r in rows]
    title_class, headline = title_of(flight)
    upd, extra = facts(bullets, headline)
    lead = bullets[:6]
    en = translate(lead + [headline]) if translate else lead + [headline]
    en_lead, en_head = en[:-1], en[-1]
    name = clean_name([title_class, en_head]) or model
    extra_specs = dict(extra)
    if bullets:
        label = "Additional information > Features" + (" (original)" if translate else "")
        extra_specs[label] = " | ".join(bullets)
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=name, category="cooking", subcategory=classify(url, headline),
        product_url=url, price_usd=None, price_local=None, pod_features=en_lead, extra_specs=extra_specs,
        image_url=main_image_url(site, flight), **market, **upd)
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section="Additional information",
                   key=f"Feature {i}", value=b) for i, b in enumerate(bullets, 1)]
    return record, raw


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    model = model_from_url(url)
    with session(SITE, url) as s:
        flight = flight_text(s.html(url))
    record, raw = build_record(SITE, model, url, flight, facts_en,
                               dict(region=REGION, country=COUNTRY, currency=CURRENCY))
    return record, download_docs(SITE, BRAND, model, flight), raw
