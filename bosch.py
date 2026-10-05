"""Bosch B36CT80SNS scraper. Product data lives in the Next.js flight payload embedded in the page HTML."""
import json, re
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import common
from schema import ProductRecord, DocumentRecord, RawSpec

BRAND, MODEL = "Bosch", "B36CT80SNS"
URL = ("https://www.bosch-home.com/us/en/product/refrigerators/fridge-freezers/"
       "freestanding-fridge-freezers-with-freezer-at-bottom/B36CT80SNS")
DOC_TYPES = {"user-manuals": "Manual", "product-specification": "SpecSheet",
             "installation-instruction": "Installation", "energy-label": "EnergyGuide"}


def _fetch_payload() -> str:
    with sync_playwright() as p:
        browser = common.launch_browser(p)
        try:
            page = browser.new_page(user_agent=common.UA)
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            try:
                page.get_by_role("button", name="Decline all").click(timeout=5000)
            except PlaywrightTimeoutError:
                pass  # banner absent
            html = page.content()
        finally:
            browser.close()
    # Next.js flight data: each self.__next_f.push([1,"<json string>"]) chunk is a JSON string literal
    chunks = re.findall(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', html)
    if not chunks:
        raise ValueError("Bosch page has no self.__next_f flight payload chunks")
    return "".join(json.loads(c) for c in chunks)


def _json_after(text: str, marker: str, open_char: str = "["):
    i = text.find(marker)
    if i < 0:
        raise ValueError(f"Bosch payload marker not found: {marker!r}")
    return json.JSONDecoder().raw_decode(text, text.index(open_char, i))[0]


def _frac(s: str) -> float:
    """'35 5/8' -> 35.625"""
    total = 0.0
    for part in s.split():
        if "/" in part:
            a, b = part.split("/"); total += int(a) / int(b)
        else:
            total += float(part)
    return total


def _num(pattern: str, text: str) -> float | None:
    return common.num(pattern, text, re.I | re.S)


def scrape() -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    u = _fetch_payload()
    sections = _json_after(u, '"specifications":[{"name":"General"')
    raw, spec = [], {}
    for sec in sections:
        for s in sec["specifications"]:
            val = s["value"]["text"].strip()
            if val.startswith("specifications.translatedBoolean."):
                val = val.rsplit(".", 1)[1].capitalize()
            val = " ".join(val.split()) + (f" {s['unit']}" if s["unit"] else "")
            spec[s["key"]] = val
            raw.append(RawSpec(brand=BRAND, model_number=MODEL, source="web", section=sec["name"],
                               key=s["name"]["text"].strip(), value=val))

    title = re.search(r'"title":\{"valueClass":"([^"]*)","headline":"([^"]*)"', u)
    pricing = _json_after(u, '"pricing":', "{") if '"pricing":{' in u else {}
    highlights = _json_after(u, '"highlights":[')
    yes = lambda k: True if spec.get(k, "").lower().startswith("yes") else None
    dims = re.findall(r"\d+(?: \d+/\d+)?", spec.get("DIM_US", ""))  # H x W x D
    hc = spec.get("HOMECONNECT_TYPE")

    # documents: unique PDFs only; duplicate doc types get a suffix so files don't collide
    docs, seen, counts = [], set(), {}
    for d in _json_after(u, '"technicalDocuments":['):
        url = urljoin(URL, d["url"])
        if not url.lower().endswith(".pdf") or url in seen:
            continue
        seen.add(url)
        dtype = DOC_TYPES.get(d["titleKey"], "Other")
        counts[dtype] = counts.get(dtype, 0) + 1
        suffix = str(counts[dtype]) if counts[dtype] > 1 else ""
        if dtype == "Other":
            suffix = d["titleKey"] + suffix
        rec = common.download_pdf(BRAND, MODEL, dtype + suffix, url)
        if rec:
            docs.append(rec.model_copy(update={"doc_type": dtype}))

    # PDF enrichment: spec sheet (electrical, water dispenser) + Energy Guide (kWh/yr)
    # newest = first listed; later sheets are older revisions with different layouts
    text = lambda t: next((common.pdf_text(common.ROOT / r.local_path) for r in docs if r.doc_type == t), "")
    sheet, guide = text("SpecSheet"), text("EnergyGuide")
    kwh = (_num(r"([\d.]+)\s*kWh\s*\nEstimated Yearly Electricity", guide)
           or _num(r"Energy consumption[^\d]*([\d.]+)", sheet))
    dispenser = re.search(r"Internal water dispenser\s*\n\s*(Yes|No)", sheet)
    volts = re.search(r"Volts[^\n]*\n\s*(\d+)\s*V", sheet)
    product = ProductRecord(
        brand=BRAND, model_number=MODEL,
        product_name=f"{title.group(1)} {re.sub(r'(\d+)_IN\b', r'\1 in', title.group(2))}".strip() if title else MODEL,
        door_style="French Door Bottom Mount" if title and "French Door" in title.group(2) else None,
        finish_color=spec.get("COL_MAIN"), product_url=URL,
        price_usd=float(pricing["amount"]) if pricing.get("amount") is not None else None,
        capacity_total_cuft=_num(r"([\d.]+) Cu Ft", spec.get("CAP_GROSS_CUBIC_FEET", "")),
        capacity_fridge_cuft=_num(r"([\d.]+) Cu Ft", spec.get("CAP_REFR_GROSS_US", "")),
        capacity_freezer_cuft=_num(r"([\d.]+) Cu Ft", spec.get("CAP_FREEZ_GROSS_US", "")),
        height_in=_frac(dims[0]) if len(dims) == 3 else None,
        width_in=_frac(dims[1]) if len(dims) == 3 else None,
        depth_in=_frac(dims[2]) if len(dims) == 3 else None,
        weight_lb=(_num(r"Net weight[^\n]*\n\s*([\d.]+)\s*lbs", sheet)
                   or _num(r"([\d.]+) lbs", spec.get("WEIGHT_GROSS_US", ""))),
        voltage_v=volts.group(1) if volts else None,
        amps=_num(r"Current[^\n]*\n\s*([\d.]+)\s*A", sheet),
        frequency_hz=_num(r"Frequency[^\n]*\n\s*([\d.]+)\s*Hz", sheet),
        energy_kwh_year=kwh,
        water_dispenser=dispenser.group(1) == "Yes" if dispenser else None,
        energy_star=yes("ENERGY_STAR_QUALIFIED"), ice_maker=yes("ICE_MAKER"),
        wifi_supported=yes("HOMECONNECTABLE"),
        wifi_evidence=f"Home Connect: Yes; features: {hc}" if yes("HOMECONNECTABLE") else None,
        pod_features=[h["headline"]["text"] for h in highlights],
    )
    return product, docs, raw
