"""GE GFE26JYMFS scraper: product page (Playwright) + PDFs (QuickSpecs/EnergyGuide/Manual)."""
import re
from playwright.sync_api import sync_playwright
from common import ROOT, UA, download_pdf, launch_browser, num, pdf_text
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND, MODEL = "GE", "GFE26JYMFS"
URL = ("https://www.geappliances.com/appliance/GE-ENERGY-STAR-25-7-Cu-Ft-"
       "Fingerprint-Resistant-French-Door-Refrigerator-GFE26JYMFS")
# anchor-text pattern -> doc_type (first match wins; multilingual-friendly)
DOC_PATTERNS = [
    (r"energy ?guide|etiqueta energ", "EnergyGuide"),
    (r"quick ?spec|spec(?:ification)? sheet|data ?sheet|datenblatt|fiche technique", "QuickSpecs"),
    (r"warranty|garant", "Warranty"),
    (r"\bmanual\b|manuel|handbuch|use (&|and) care", "Manual"),
    (r"\binstall(?:ation)?\b", "Installation"),
]
# Specific types win over Manual/Installation when several anchors share one URL
PRIORITY = ["EnergyGuide", "QuickSpecs", "Warranty", "Manual", "Installation"]


def _classify(text: str) -> str | None:
    t = text.lower()
    return next((d for p, d in DOC_PATTERNS if re.search(p, t)), None)


def _fetch_page() -> tuple[str, str, list[tuple[str, str]]]:
    with sync_playwright() as p:
        b = launch_browser(p)
        try:
            pg = b.new_context(user_agent=UA).new_page()
            pg.goto(URL, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(6000)
            title = pg.inner_text("h1")
            text = pg.inner_text("body")
            anchors = pg.eval_on_selector_all("a[href*='.pdf']", "e=>e.map(x=>[x.innerText.trim(),x.href])")
        finally:
            b.close()
    return title, text, anchors


def _doc_urls(anchors: list[tuple[str, str]]) -> dict[str, str]:
    """url -> doc_type, deduped by URL, best-priority type across anchors sharing the URL."""
    found: dict[str, str] = {}
    for text, href in anchors:
        dt = _classify(text)
        if dt and (href not in found or PRIORITY.index(dt) < PRIORITY.index(found[href])):
            found[href] = dt
    return found


def _about_block(text: str) -> str:
    return text.split("About This Product")[-1].split("Claims & Certifications")[0]


def _features(text: str) -> list[str]:
    block = _about_block(text)
    lines = [l.strip() for l in block.splitlines() if l.strip() and l.strip() != "Play Video"]
    return [l for l in lines[0::2] if l != "See All Features"]  # alternating title / description


def _yearly_kwh(eg: str) -> float | None:
    """Yearly kWh from the Energy Guide; None unless unambiguous (cross-checked vs yearly cost at 14c/kWh)."""
    labelled = num(r"(\d[\d,]*)\s*kWh[^\n]*\n\s*Estimated Yearly", eg, re.I)
    if labelled:
        return labelled
    cands = {int(k) for k in re.findall(r"^(\d{3,4})$", eg, re.M)}  # bare number lines
    costs = [int(c) for c in re.findall(r"^\$?(\d{2,3})$", eg, re.M)]
    if costs:
        cands = {k for k in cands if any(abs(c - k * 0.14) <= 1.5 for c in costs)}
    return float(cands.pop()) if len(cands) == 1 else None


def _feature_flag(pat: str, scope: str) -> bool | None:
    """True/False when the product feature/spec text states it, None when silent."""
    if re.search(rf"\b(?:no|without)\s+(?:an?\s+)?(?:{pat})", scope, re.I):
        return False
    return True if re.search(pat, scope, re.I) else None


def _quickspec_rows(qs: str) -> list[RawSpec]:
    rows = []
    mk = lambda sec, k, v: RawSpec(brand=BRAND, model_number=MODEL, source="QuickSpecs.pdf", section=sec, key=k, value=v)
    for m in re.finditer(r"^((?!.*\(in\.\)).{3,80}?) – (.+)$", qs, re.M):
        rows.append(mk("Features", m.group(1).strip(), m.group(2).strip()))
    labels = re.findall(r"^(.+?\(in\.\))(?: [A-I])?$", qs, re.M)
    vals = re.findall(r"^(\d+(?:-\d+/\d+|/\d+)?)$", qs, re.M)
    if len(labels) == len(vals) == 12:  # positional: 9 overall dims + 3 air clearances
        rows += [mk("Dimensions (in.)", l.strip(), v) for l, v in zip(labels, vals)]
    return rows


def scrape() -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    title, text, anchors = _fetch_page()
    docs, pdf_txt = [], {}
    for url, dt in _doc_urls(anchors).items():
        d = download_pdf(BRAND, MODEL, dt, url)
        if d:
            docs.append(d)
            pdf_txt[dt] = pdf_text(ROOT / d.local_path)
    qs, eg, man = (pdf_txt.get(k, "") for k in ("QuickSpecs", "EnergyGuide", "Manual"))

    dim = re.search(r"Dimensions:\s*([\d.]+) H x ([\d.]+) W x ([\d.]+) D", text)
    features = _features(text)
    wifi = re.search(r"[^.]*WiFi Connect[^.]*\.", man)
    wifi_ev = " ".join(wifi.group(0).replace("*", "").split()).replace("You refrigerator", "Your refrigerator") if wifi else None
    wifi_optional = bool(wifi_ev and re.search(r"optional|module|accessory", wifi_ev, re.I))
    if wifi_optional:
        wifi_ev = "GE WiFi Connect: optional accessory (ConnectPlus module), not built in"
    spec_scope = _about_block(text) + "\n" + "\n".join(r.key + " " + r.value for r in _quickspec_rows(qs))
    ice = _feature_flag(r"ice ?maker", spec_scope)
    water = _feature_flag(r"water dispenser|ice and water dispenser", spec_scope)
    amps = re.search(r"(\d+) (?:or \d+|o \d+) amp", man)
    color = re.search(r"Color:\s*(.+)", text)
    price = re.search(r"SALE\s*\$([\d,]+\.?\d*)", text)

    product = ProductRecord(
        brand=BRAND, model_number=MODEL, product_name=title.strip(), product_url=URL,
        door_style="French Door" if "French-Door" in title else None,
        finish_color=color.group(1).strip() if color else None,
        price_usd=float(price.group(1).replace(",", "")) if price else None,
        capacity_total_cuft=num(r"([\d.]+) cu\. ?ft\. capacity", text, re.I) or num(r"([\d.]+) Cu\. Ft\.", title, re.I),
        height_in=float(dim.group(1)) if dim else None,
        width_in=float(dim.group(2)) if dim else None,
        depth_in=float(dim.group(3)) if dim else None,
        weight_lb=num(r"(?:net|shipping|product) weight[^\d]{0,20}([\d.]+) ?lb", qs + man, re.I),
        voltage_v="115" if re.search(r"115 (?:volt|voltios)", man, re.I) else None,
        amps=float(amps.group(1)) if amps else None,
        frequency_hz=60.0 if re.search(r"\b60 Hz", man) else None,
        energy_kwh_year=_yearly_kwh(eg),
        energy_star=True if re.search(r"ENERGY STAR", text) else None,
        ice_maker=ice, water_dispenser=water,
        wifi_supported=True if wifi and not wifi_optional else None,
        wifi_evidence=wifi_ev,
        pod_features=features,
    )

    raw = [RawSpec(brand=BRAND, model_number=MODEL, source="web", section="Specs & Details",
                   key="Dimensions", value=dim.group(0).split(":", 1)[1].strip())] if dim else []
    if color:
        raw.append(RawSpec(brand=BRAND, model_number=MODEL, source="web", section="Specs & Details",
                           key="Color", value=color.group(1).strip()))
    raw += [RawSpec(brand=BRAND, model_number=MODEL, source="web", section="Features", key=f, value="")
            for f in features]
    raw += _quickspec_rows(qs)
    return product, docs, raw
