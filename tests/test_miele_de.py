"""Plain-assert offline tests for miele_de (saved fixtures, no network, no LLM, no browser). Run: python tests/test_miele_de.py

Fixtures in tests/fixtures/miele_de were cut from live miele.de data (2026-10): listing.json = category-page product rows
(decoded __NUXT_DATA__), pdp_*.json = decoded `product-data-*` objects with the spec table / documents trimmed to what the
adapter reads. The Nuxt/devalue layer is tested with a small encoder written here."""
import copy
import json
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import miele_de
from schema import DocumentRecord

FIX = Path(__file__).parent / "fixtures" / "miele_de"
jload = lambda n: json.loads((FIX / n).read_text(encoding="utf-8"))
_GERMAN = re.compile(r"[A-Za-zÄÖÜäöüß]")


class StubDe:
    """Deterministic offline stand-in for i18n.get('de'): every non-numeric string becomes 'EN[<text>]'."""

    def translate_many(self, texts, kind="value"):
        return [t if not re.search(r"[A-Za-zÄÖÜäöüß]{2}", str(t)) else f"EN[{t}]" for t in texts]


class Offline:
    def __enter__(self):
        self.saved, miele_de._TR = miele_de._TR, StubDe()
        return self

    def __exit__(self, *a):
        miele_de._TR = self.saved


def encode_nuxt(data: dict) -> str:
    """Minimal devalue encoder: {'data': {...}} -> an HTML page with the __NUXT_DATA__ script (root wrapped like Nuxt)."""
    arr: list = [None]

    def add(v):
        if isinstance(v, dict):
            i = len(arr)
            arr.append(None)
            arr[i] = {k: add(x) for k, x in v.items()}
            return i
        if isinstance(v, list):
            i = len(arr)
            arr.append(None)
            arr[i] = [add(x) for x in v]
            return i
        arr.append(v)
        return len(arr) - 1

    root = add({"data": data, "state": {"x": 1}})
    arr[0] = ["ShallowReactive", root]
    return ('<html><body><script type="application/json" data-nuxt-data="nuxt-app" data-ssr="true" '
            f'id="__NUXT_DATA__">{json.dumps(arr)}</script></body></html>')


def listing_html(page_rows, page=0, pages=1, total=None):
    return encode_nuxt({"category-data-1": None, "category-data-1-abc": {
        "products": {"products": page_rows, "currentPage": page, "totalPageCount": pages,
                     "totalProductCount": total or len(page_rows)}}})


# ------------------------------------------------------------------ module contract
def test_constants_and_registry():
    assert (miele_de.BRAND, miele_de.COUNTRY, miele_de.REGION, miele_de.CURRENCY) == ("Miele", "de", "eu", "EUR")
    assert miele_de.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys())
    assert miele_de.SUPPORTED_SUBCATEGORIES == {"microwave", "sco", "electric_oven", "induction", "radiant", "gas_cooktop"}
    assert all(catalog.major_of(s) == "cooking" for s in miele_de.SUPPORTED_SUBCATEGORIES)   # cooking only (scope)
    for sub in ("otr", "gas_oven"):   # Miele sells neither
        assert sub not in miele_de.SUPPORTED_SUBCATEGORIES
    assert set(miele_de.SUB_SOURCES) == miele_de.SUPPORTED_SUBCATEGORIES
    assert catalog.module_name("Miele", "de") == "miele_de"   # auto-discovered, nothing registered by hand
    assert catalog.supported("Miele", "de") == miele_de.SUPPORTED_SUBCATEGORIES


# ------------------------------------------------------------------ classification
def test_classify_table():
    c = miele_de.classify
    assert c("Einbau-Mikrowellengerät", "APPLIANCE/MDA/Microwave ovens/Microwave ovens (built-in)") == "microwave"
    assert c("Stand-Mikrowellengerät") == "microwave"
    assert c("Kompakt-Backofen mit Mikrowelle") == "sco" and c("Griffloser Kompakt-Backofen mit Mikrowelle") == "sco"
    assert c("Dampfgarer mit Mikrowelle") == "sco" and c("Dampfgarer mit Mikrowelle und Frischwasseranschluss") == "sco"
    for d in ("Backofen", "Griffloser Backofen", "Kompakt-Backofen", "90 cm breiter Backofen", "Dampfbackofen",
              "Kompakt-Dampfbackofen mit Frisch- und Abwasseranschluss"):
        assert c(d, "APPLIANCE/MDA/Ovens/Ovens <lt/>= 60 cm") == "electric_oven", d
    assert c("Gaskochfeld", "APPLIANCE/MDA/Hobs/Gas hobs") == "gas_cooktop"
    assert c("Herdunabhängiges Induktionskochfeld", "APPLIANCE/MDA/Hobs/Induction hobs independent") == "induction"
    assert c("Herdgesteuertes Induktionskochfeld", "APPLIANCE/MDA/Hobs/Induction hobs combined") == "induction"
    assert c("Induktionskochfeld mit integriertem Dunstabzug", "APPLIANCE/MDA/Hobs/Induction hobs with vapour extraction") == "induction"
    assert c("Herdunabhängiges Elektrokochfeld", "APPLIANCE/MDA/Hobs/Electric hobs independent") == "radiant"
    assert c("Herdgesteuertes Elektrokochfeld") == "radiant"
    # left out on purpose: pure steamers, cookers, combi-set modules, everything that is not cooking
    for d, cat in (("Einbau-Dampfgarer", "APPLIANCE/MDA/Steam ovens/Steam ovens 45 cm"), ("Herd", "APPLIANCE/MDA/Ovens/Cookers"),
                   ("ProLine-Element", "APPLIANCE/MDA/CombiSets/CombiSet Induction"),
                   ("SmartLine-Element", "APPLIANCE/MDA/CombiSets/CombiSet Electric"),
                   ("Stand-Kühlschrank", "APPLIANCE/MDA/Refrigeration/Refrigerators (freestanding)"),
                   ("W1 Waschmaschine Frontlader:", "APPLIANCE/MDA/Washing machines/x"), ("Dunstabzugshaube", ""), ("", ""), (None, None)):
        assert c(d, cat) is None, d


def test_classification_is_exclusive_over_all_listing_rows():
    rows = jload("listing.json")["products"]
    got = {}
    for sub in miele_de.SUPPORTED_SUBCATEGORIES:
        for cand in miele_de.parse_listing(rows, sub):
            assert cand.model_number not in got, (cand.model_number, got.get(cand.model_number), sub)
            got[cand.model_number] = sub
    assert got["H 2465 B ACTIVE"] == "electric_oven" and got["DGC 7460 HC Pro"] == "electric_oven"
    assert got["H 7240 BM"] == "sco" and got["DGM 7440"] == "sco"
    assert got["M 7240 TC"] == "microwave" and got["M 6012 SC"] == "microwave"
    assert got["KM 7361 FL"] == "induction" and got["KMDA 7876-1 FL MattFinish"] == "induction"
    assert got["KM 6521 FR"] == "radiant" and got["KM 6003 LPT"] == "radiant"
    assert got["KM 3054-1"] == "gas_cooktop"
    for excluded in ("DG 7240", "H 2455 EP ACTIVE", "CS 1212-1 I"):   # steamer, cooker, combi-set module
        assert excluded not in got, excluded


# ------------------------------------------------------------------ nuxt payload
def test_nuxt_decoder_roundtrip_and_errors():
    html = encode_nuxt({"product-data-1": {"name": "X", "list": [1, "a", None, {"k": True}], "n": 2.5},
                        "other": {"ignored": 1}})
    got = miele_de.nuxt_entries(html, "product-data-")
    assert got == {"product-data-1": {"name": "X", "list": [1, "a", None, {"k": True}], "n": 2.5}}
    for bad in ("<html></html>", '<script type="application/json" id="__NUXT_DATA__">[1,2</script>',
                '<script type="application/json" id="__NUXT_DATA__">[[], 5]</script>'):
        try:
            miele_de.nuxt_entries(bad, "x")
        except miele_de.MielePageError:
            continue
        raise AssertionError("bad payload accepted")
    for fn in (miele_de.listing_page, miele_de.pdp_product):
        try:
            fn(encode_nuxt({"nothing": {"here": 1}}))
        except miele_de.MielePageError:
            continue
        raise AssertionError("page without data accepted")


# ------------------------------------------------------------------ listing / discover
def test_parse_listing_dedupes_prices_and_hosts():
    rows = jload("listing.json")["products"]
    ovens = miele_de.parse_listing(rows, "electric_oven")
    names = [c.model_number for c in ovens]
    assert names.count("H 2465 B ACTIVE") == 1 and "H 0000 EVIL" not in names   # colour variants deduped, off-domain dropped
    first = ovens[0]
    assert (first.brand, first.country, first.region, first.currency) == ("Miele", "de", "eu", "EUR")
    assert first.price_usd is None and first.price_local == 739.0
    assert first.category == "cooking" and first.subcategory == "electric_oven" and first.url.startswith("https://www.miele.de/")
    assert first.attrs == {"fuel": "electric"} and first.attrs_src == {"fuel": "listing"}
    steam = next(c for c in ovens if c.model_number == "DGC 7460 HC Pro")
    assert steam.attrs.get("steam") is True
    gas = miele_de.parse_listing(rows, "gas_cooktop")
    assert next(c for c in gas if c.model_number == "KM 0000 NOPRICE").price_local is None   # no price -> None, not 0
    assert all(c.attrs["fuel"] == "gas" for c in gas)
    assert {c.attrs["fuel"] for c in miele_de.parse_listing(rows, "radiant")} == {"electric"}
    assert {c.attrs["fuel"] for c in miele_de.parse_listing(rows, "induction")} == {"induction"}
    assert len(miele_de.parse_listing(rows, "induction", limit=1)) == 1


class FakeFetcher:
    """Stands in for the browser fetcher: serves canned pages and records the urls asked for."""
    pages: dict = {}
    log: list = []
    closed = 0

    def html(self, url):
        FakeFetcher.log.append(url)
        return url, FakeFetcher.pages[url]

    def close(self):
        FakeFetcher.closed += 1


class PatchFetcher:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        self.saved, miele_de._Fetcher = miele_de._Fetcher, FakeFetcher
        FakeFetcher.pages, FakeFetcher.log, FakeFetcher.closed = self.pages, [], 0
        self.saved_polite, miele_de._polite = miele_de._polite, lambda: None
        return self

    def __exit__(self, *a):
        miele_de._Fetcher, miele_de._polite = self.saved, self.saved_polite


def test_discover_pages_limit_and_unsupported():
    rows = jload("listing.json")["products"]
    base = "https://www.miele.de/category/1013778/kochfelder"
    hobs = [r for r in rows if "Hobs" in r["dataLayer"]["category"]]
    p1, p2 = hobs[:4], hobs[4:]
    pages = {base: listing_html(p1, 0, 2, len(hobs)), base + "?page=2": listing_html(p2, 1, 2, len(hobs))}
    with PatchFetcher(pages):
        got = miele_de.discover("induction", 30)
        assert FakeFetcher.log == [base, base + "?page=2"] and FakeFetcher.closed == 1   # 2 pages, browser closed
        assert {c.subcategory for c in got} == {"induction"} and len(got) >= 2
        FakeFetcher.log.clear()
        one = miele_de.discover("induction", 1)
        assert len(one) == 1 and FakeFetcher.log == [base]                  # limit reached on page 1: no second request
    for sub in ("otr", "gas_oven", "french_door", "front_load", "nonsense"):
        try:
            miele_de.discover(sub)
        except ValueError:
            continue
        raise AssertionError(sub)


# ------------------------------------------------------------------ product page
def _parse(tag):
    p = jload(f"pdp_{tag}.json")
    with Offline():
        return miele_de.parse_product(p, p["pdpUrl"])


def test_parse_oven_sco_microwave():
    rec, raw, info = _parse("oven")
    assert (rec.brand, rec.model_number, rec.subcategory, rec.category) == ("Miele", "H 2465 B ACTIVE", "electric_oven", "cooking")
    assert (rec.region, rec.country, rec.currency, rec.price_usd, rec.price_local) == ("eu", "de", "EUR", None, 739.0)
    assert rec.capacity_total_cuft == 2.68 and rec.weight_lb == 92.6        # 76 L, 42 kg
    assert rec.voltage_v == "220-240" and rec.frequency_hz == 50.0 and rec.wifi_supported is True and rec.wifi_evidence
    assert rec.extra_specs["Fuel type"] == "Electric" and rec.extra_specs["Oven capacity (L)"] == "76"
    assert rec.extra_specs["Energy efficiency class (EU)"] == "EU class A+"      # never an ENERGY STAR flag
    assert rec.energy_star is None and rec.energy_kwh_year is None
    assert rec.extra_specs["Total connected load (kW)"] == "3.5" and rec.extra_specs["Price incl. VAT (EUR)"] == "739"
    assert rec.finish_color == "EN[Obsidianschwarz]" and rec.image_url.startswith("https://media.miele.com/")
    # 'Section > Label' English keys, translated values; flags become Yes; original German kept as RawSpec
    assert "EN[Technische Daten] > EN[Garraumvolumen (l)]" in rec.extra_specs
    assert rec.extra_specs["EN[Technische Daten] > EN[Garraumvolumen (l)]"] == "76"
    assert "Yes" in rec.extra_specs.values() and "TRUE" not in rec.extra_specs.values()
    assert any(r.key == "Garraumvolumen in l" and r.value == "76" and r.section == "Technische Daten" for r in raw)
    assert all(r.brand == "Miele" and r.model_number == rec.model_number and r.source == "web" for r in raw)
    assert [t for t, _ in info["documents"]] == ["Manual", "SpecSheet", "EnergyGuide"]
    sco, _, _ = _parse("sco")
    assert sco.subcategory == "sco" and sco.extra_specs["Microwave cooking power (W)"] == "1000"
    assert sco.capacity_total_cuft == 1.52 and sco.price_local == 2099.0
    mw, _, _ = _parse("micro")
    assert mw.subcategory == "microwave" and mw.extra_specs["Microwave cooking power (W)"] == "900" and mw.wifi_supported is False
    steam, _, _ = _parse("steam")
    assert steam.subcategory == "electric_oven" and steam.extra_specs["Oven capacity (L)"] == "67"


def test_parse_cooktops():
    ind, raw, _ = _parse("induction")
    assert ind.subcategory == "induction" and ind.extra_specs["Fuel type"] == "Induction"
    assert (ind.width_in, ind.height_in, ind.depth_in) == (24.41, 2.09, 20.47)            # 620 x 53 x 520 mm
    assert ind.extra_specs["Burners/elements"] == "4" and ind.extra_specs["Width (mm)"] == "620"
    assert ind.voltage_v == "230" and ind.weight_lb == 22.0
    assert any(r.key.startswith("Max. Leistung in W") and r.section.startswith("1. Kochzone") for r in raw)
    # the four identical 'Max. Leistung in W' rows of the zones keep distinct keys thanks to the zone section
    assert len({k for k in ind.extra_specs if "Max. Leistung" in k or "EN[Max. Leistung (W)]" in k}) >= 4
    gas, _, _ = _parse("gas")
    assert gas.subcategory == "gas_cooktop" and gas.extra_specs["Fuel type"] == "Gas" and gas.extra_specs["Burners/elements"] == "5"
    assert gas.width_in == 37.09 and gas.price_local == 2149.0
    rad, _, _ = _parse("radiant")
    assert rad.subcategory == "radiant" and rad.extra_specs["Fuel type"] == "Electric" and rad.width_in == 24.17


def test_parse_rejects_other_families_and_empty_pages():
    p = jload("pdp_oven.json")
    for design, cat in (("Stand-Kühlschrank", "APPLIANCE/MDA/Refrigeration/Refrigerators (freestanding)"),
                        ("Einbau-Dampfgarer", "APPLIANCE/MDA/Steam ovens/Steam ovens 45 cm"), ("Herd", "x")):
        q = copy.deepcopy(p)
        q["designTypeName"], q["dataLayer"]["category"] = design, cat
        try:
            with Offline():
                miele_de.parse_product(q, q["pdpUrl"])
        except ValueError:
            continue
        raise AssertionError(design)
    for broken in ({**p, "techspecs": []}, {**p, "name": ""}):
        try:
            with Offline():
                miele_de.parse_product(broken, p["pdpUrl"])
        except miele_de.MielePageError:
            continue
        raise AssertionError("broken product accepted")


def test_price_unknown_stays_none_and_zero_is_ignored():
    p = jload("pdp_oven.json")
    for price in (None, {"value": 0}, {"value": True}, {}):
        q = {**p, "salesPrice": price, "price": price}
        with Offline():
            rec, _, _ = miele_de.parse_product(q, p["pdpUrl"])
        assert rec.price_local is None and "Price incl. VAT (EUR)" not in rec.extra_specs


def test_parse_documents_pdf_and_host_rules():
    p = jload("pdp_oven.json")
    docs = miele_de.parse_documents(p)
    assert [t for t, _ in docs] == ["Manual", "SpecSheet", "EnergyGuide"]
    assert all(u.startswith("https://media.miele.com/") and u.endswith(".pdf") for _, u in docs)
    q = copy.deepcopy(p)
    for g in q["documentGroups"]:
        for d in g["documents"]:
            d["media"]["url"] = d["media"]["url"].replace("https://media.miele.com", "https://evil.example.com")
    assert miele_de.parse_documents(q) == []
    q = copy.deepcopy(p)
    for g in q["documentGroups"]:
        for d in g["documents"]:
            d["media"]["url"] = d["media"]["url"].replace("https:", "http:")
    assert miele_de.parse_documents(q) == []
    assert miele_de.parse_documents({}) == [] and miele_de.parse_documents({"documentGroups": None}) == []


def test_image_host_rule():
    p = jload("pdp_oven.json")
    assert miele_de._image(p).startswith("https://media.miele.com/")
    q = {**p, "primaryProductImage": {"url": "https://evil.example.com/a.png"}, "images": [{"url": "http://media.miele.com/a.png"}]}
    assert miele_de._image(q) is None


# ------------------------------------------------------------------ scrape + security
def test_scrape_uses_fetcher_downloads_documents_and_closes():
    p = jload("pdp_oven.json")
    html = encode_nuxt({f"product-data-{p['formattedCode']}": p})
    asked = []

    def fake_download(brand, model, doc_type, url):
        asked.append((brand, model, doc_type, url))
        return DocumentRecord(brand=brand, model_number=model, doc_type=doc_type, source_url=url,
                              local_path=f"downloads/miele/{model}_{doc_type}.pdf", sha256=doc_type, size_bytes=1, pages=1)

    saved = miele_de.download_pdf
    miele_de.download_pdf = fake_download
    try:
        with PatchFetcher({p["pdpUrl"]: html}), Offline():
            rec, docs, raw = miele_de.scrape(p["pdpUrl"])
            assert FakeFetcher.closed == 1 and FakeFetcher.log == [p["pdpUrl"]]
    finally:
        miele_de.download_pdf = saved
    assert rec.model_number == "H 2465 B ACTIVE" and len(raw) > 50
    assert [d.doc_type for d in docs] == ["Manual", "SpecSheet", "EnergyGuide"]
    assert all(a[1] == "H_2465_B_ACTIVE" for a in asked)                     # file name sanitised (no spaces)
    with PatchFetcher({}):
        for bad in ("http://www.miele.de/product/1/x", "https://evil.example.com/product/1/x",
                    "https://www.miele.de.evil.example.com/p", "https://user:pw@www.miele.de/p", "file:///etc/passwd"):
            try:
                miele_de.scrape(bad)
            except miele_de.MielePageError:
                assert FakeFetcher.log == []                                  # refused before any request
                continue
            raise AssertionError(bad)


def test_check_url_and_names():
    miele_de._check_url("https://www.miele.de/product/12428770/backofen-h-2465-b-active-obsidianschwarz")
    for bad in ("http://www.miele.de/", "https://miele.de.evil.com/", "https://notmiele.de/", "https://localhost/"):
        try:
            miele_de._check_url(bad)
        except miele_de.MielePageError:
            continue
        raise AssertionError(bad)
    assert miele_de._safe_name("H 2465/B..x") == "H_2465_B..x" and miele_de._safe_name("...") == "unknown"


def test_browser_modes_and_politeness():
    import os
    import time
    saved = os.environ.get("FRIDGE_BROWSER_MODE")
    try:
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        assert miele_de._browser_modes() == [(True, True), (True, False), (False, False)]   # new headless first, visible last
        os.environ["FRIDGE_BROWSER_MODE"] = "headless"
        assert miele_de._browser_modes() == [(True, True), (True, False)]
        os.environ["FRIDGE_BROWSER_MODE"] = "visible"
        assert miele_de._browser_modes() == [(False, False)]
    finally:
        if saved is None:
            os.environ.pop("FRIDGE_BROWSER_MODE", None)
        else:
            os.environ["FRIDGE_BROWSER_MODE"] = saved
    assert miele_de.DELAY_S >= 1.0
    miele_de._polite()
    t0 = time.monotonic()
    miele_de._polite()
    assert time.monotonic() - t0 >= miele_de.DELAY_S - 0.05


def test_robots_disallowed_api_is_never_referenced():
    src = Path(miele_de.__file__).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    body = code.split('"""', 2)[2]                                            # drop the module docstring
    for forbidden in ("price-and-stock", "/cart", "checkout", "wishlist", "/pmedia/", "compare"):
        assert forbidden not in body, forbidden


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"{len(fns)} passed")
