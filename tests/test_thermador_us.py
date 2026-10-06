"""Plain-assert offline tests (saved, trimmed fixtures). Run: python tests/test_thermador_us.py
Also covers the shared BSH core in thermador_us.py (flight payload, listing, spec rows, docs, consent labels)."""
import os
import sys
from pathlib import Path

os.environ["FRIDGE_I18N_LLM"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import thermador_us as t

FX = Path(__file__).parent / "fixtures" / "thermador_us"
PDP = "https://www.thermador.com/us/en/mkt-product/"
URLS = {"MC30WS": PDP + "ovens/speed-ovens/MC30WS", "PRD366WGU": PDP + "ranges/36-ranges/PRD366WGU"}


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


def _parse(m):
    model, root = t.model_from_url(URLS[m])
    return t.parse_product(model, URLS[m], root, t.flight_text(_t(f"pdp_{m}.html")))


def test_supported_subcategories_are_cooking_only():
    assert t.SUPPORTED_SUBCATEGORIES == set(t.SUB_SOURCES)
    assert t.SUPPORTED_SUBCATEGORIES <= {"microwave", "sco", "otr", "gas_oven", "gas_cooktop", "electric_oven",
                                         "induction", "radiant"}
    assert {"sco", "gas_oven", "gas_cooktop", "induction", "electric_oven", "microwave", "otr"} == t.SUPPORTED_SUBCATEGORIES
    assert (t.COUNTRY, t.REGION, t.CURRENCY) == ("us", "na", "USD")


def test_discover_rejects_unsupported_sub():
    for sub in ("french_door", "top_load", "radiant"):
        try:
            t.discover(sub)
        except ValueError:
            continue
        raise AssertionError(sub)


def test_classify_rules():
    c = t.classify
    assert c("/mkt-product/ovens/speed-ovens/MC30WS", "Masterpiece Speed Oven 30''") == "sco"
    assert c("/mkt-product/ovens/double-ovens/MEM301WS", "Masterpiece Combination Wall Oven 30''") == "sco"
    assert c("/mkt-product/ovens/double-ovens/MEDMC301WS", "Double Combination built-in Oven with Speed Oven") == "sco"
    assert c("/mkt-product/ovens/wall-ovens/MED301LWS", "Masterpiece Single Wall Oven 30''") == "electric_oven"
    assert c("/mkt-product/ovens/double-ovens/MEDS302BS", "Double Steam Wall Oven") == "electric_oven"
    assert c("/mkt-product/ovens/ovensmicrowaves/MU30WSU", "Over-The-Range Microwave 30''") == "otr"
    assert c("/mkt-product/ovens/ovensmicrowaves/MB30WS", "Masterpiece Built-In Microwave 30''") == "microwave"
    assert c("/mkt-product/ovens/ovensmicrowaves/MD30BS", "Professional MicroDrawer Microwave 30''") == "microwave"
    assert c("/mkt-product/ovens/warming-drawers/WD30W", "Warming Drawer 30''") is None
    assert c("/mkt-product/ovens/accessories/PA24CVRR", "Cover") is None
    assert c("/mkt-product/ranges/36-ranges/PRD366WGU", "Dual Fuel Professional Range 36''") == "gas_oven"
    assert c("/mkt-product/ranges/30-ranges/PRG304WH", "Gas Professional Range 30''") == "gas_oven"
    assert c("/mkt-product/ranges/30-ranges/PRI30LBHU", "Liberty Induction Professional Range 30''") == "induction"
    assert c("/mkt-product/cooktops-rangetops/rangetops/PCG366W", "Gas Rangetop 36''") == "gas_cooktop"
    assert c("/mkt-product/cooktops-rangetops/gas-cooktops/SGSP365TS", "Masterpiece Gas Cooktop 36''") == "gas_cooktop"
    assert c("/mkt-product/cooktops-rangetops/induction-cooktops/CIT304BB", "Heritage Induction Cooktop 30''") == "induction"
    assert c("/mkt-product/refrigeration/bottom-freezer-refrigerators/T36FT820NS", "Refrigerator") is None  # out of scope


def test_listing_parse_and_candidates():
    items, total = t.parse_listing(_t("listing_ranges.html"))
    assert total == 65 and len(items) == 5
    gas = [t.item_to_candidate(i, "cooking", "gas_oven", "ranges") for i in items]
    assert [c.model_number for c in gas if c] == ["PRG486WDH", "PRD486WIGU"]
    c = gas[0]
    assert (c.brand, c.category, c.subcategory, c.country, c.currency) == ("Thermador", "cooking", "gas_oven", "us", "USD")
    assert c.price_usd == 12949.0 and c.price_local == 12949.0 and c.attrs["width_in"] == 48.0
    assert c.url == PDP + "ranges/48-ranges/PRG486WDH" and c.name.startswith("Gas Professional Range 48''")
    ind = [t.item_to_candidate(i, "cooking", "induction", "ranges") for i in items]
    assert [c.model_number for c in ind if c] == ["PRI30LBHU", "PRI36LBHU"] and ind[1].attrs["finish"] == "stainless"


def test_candidate_filters():
    ok = {"productCode": "X123", "urlPath": "/mkt-product/ranges/30-ranges/X123", "productName": ["Gas Range 30''"]}
    assert t.item_to_candidate(ok, "cooking", "gas_oven", "ranges").price_usd is None
    for bad in ({**ok, "urlPath": "/mkt-product/ranges/accessories/knobs/X123"},
                {**ok, "urlPath": "/mkt-product/ovens/wall-ovens/X123"},  # other root
                {**ok, "urlPath": "//evil.com/mkt-product/ranges/x"}, {**ok, "urlPath": "/product/ranges/x/X123"},
                {**ok, "productCode": "../../etc"}, {**ok, "productCode": None}):
        assert t.item_to_candidate(bad, "cooking", "gas_oven", "ranges") is None, bad
    assert t.item_price({"price": {"kind": "PROMOTION", "amount": 5}}) is None
    assert t.item_price({"price": {"kind": "SHOP_PRICE", "amount": 0}}) is None
    assert t.item_price({"price": {"kind": "RECOMMENDED_RETAIL_PRICE", "amount": 99.5}}) == 99.5


def test_speed_oven_record():
    r, raw = _parse("MC30WS")
    assert (r.brand, r.category, r.subcategory, r.country, r.currency) == ("Thermador", "cooking", "sco", "us", "USD")
    assert r.price_usd == 3249.0 and r.finish_color == "Stainless Steel" and r.product_name.startswith("Masterpiece Speed Oven")
    assert (r.height_in, r.width_in, r.depth_in) == (19.625, 29.75, 22.375) and r.weight_lb == 83.0
    assert r.voltage_v == "240" and r.amps == 16.0 and r.wifi_supported is False
    assert r.image_url.startswith("https://media3.bsh-group.com/Product_Shots/") and r.pod_features
    assert r.extra_specs["General > Max. Microwave Power"] == "1000 W" and r.extra_specs["Max. Microwave Power"] == "1000 W"
    assert "Weight basis" not in r.extra_specs and "Volts" not in r.extra_specs  # typed fields are not repeated flat
    assert all(f"{x.section} > {x.key}" in r.extra_specs for x in raw) and len(raw) == 24
    assert any(x.key == "Net Weight" and x.value == "83.000 lbs" for x in raw)


def test_range_record_and_fractions():
    r, _ = _parse("PRD366WGU")
    assert (r.subcategory, r.price_usd, r.wifi_supported) == ("gas_oven", 12249.0, True)
    assert (r.height_in, r.width_in, r.depth_in) == (35.875, 35.9375, 27.875)
    assert r.extra_specs["General > Number of Gas Burners"] == "6" and r.voltage_v == "240/208"
    assert t.dims_hwd("3/8 + 3 7/8 x 31 x 21 1/4") == (4.25, 31.0, 21.25) and t.dims_hwd("1 x 2") is None
    assert t.frac("35 5/8") == 35.625


def test_pick_docs_skips_interactive_manuals_and_foreign_hosts():
    docs = [{"titleKey": "user-interactive-manuals", "url": "https://www.thermador.com/us/manual/1?productCode=X"},
            {"titleKey": "user-manuals", "url": "http://media3.bsh-group.com/Documents/a.pdf"},
            {"titleKey": "user-manuals", "url": "https://evil.example.com/b.pdf"},
            {"titleKey": "user-manuals", "url": "https://media3.bsh-group.com/Documents/c.pdf"},
            {"titleKey": "product-specification", "url": "https://media3.bsh-group.com/Documents/d.pdf"},
            {"titleKey": "energy-label", "url": "https://media3.bsh-group.com/Documents/e.txt"}]
    assert t.pick_docs(t.SITE, docs) == [("SpecSheet", "https://media3.bsh-group.com/Documents/d.pdf"),
                                         ("Manual", "https://media3.bsh-group.com/Documents/c.pdf")]


def test_url_validation():
    for bad in ("http://www.thermador.com/us/en/mkt-product/ovens/speed-ovens/MC30WS",
                "https://evil.example.com/us/en/mkt-product/ovens/speed-ovens/MC30WS",
                "https://www.thermador.com/us/en/product/ovens/accessories/MC30WS",
                "https://www.thermador.com/us/en/mkt-product/dishwashers/x/MC30WS",
                "https://www.thermador.com/us/en/mkt-product/ovens/../../MC30WS?x"):
        try:
            t.model_from_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert t.model_from_url(URLS["MC30WS"]) == ("MC30WS", "ovens")


def test_missing_markers_raise():
    for fn in (lambda: t.flight_text("<html>nothing</html>"),
               lambda: t.parse_listing('<script>self.__next_f.push([1,"{}"])</script>'),
               lambda: t.parse_product("M1", URLS["MC30WS"], "ovens", '"specifications":[{"name":"G","specifications":[]}]')):
        try:
            fn()
        except ValueError:
            continue
        raise AssertionError("expected ValueError")


def test_scrape_uses_session_and_docs_without_network():
    flight_html = _t("pdp_MC30WS.html")

    class FakeSession:
        site = t.SITE

        def html(self, url):
            return flight_html

    from contextlib import contextmanager

    @contextmanager
    def fake_session(site, url):
        yield FakeSession()

    t_session, t_docs = t.session, t.download_docs
    t.session, t.download_docs = fake_session, lambda *a, **k: []
    try:
        rec, docs, raw = t.scrape(URLS["MC30WS"])
    finally:
        t.session, t.download_docs = t_session, t_docs
    assert rec.model_number == "MC30WS" and docs == [] and len(raw) == 24


def test_browser_modes():
    old = os.environ.pop("FRIDGE_BROWSER_MODE", None)
    try:
        assert t.modes() == [True, False]
        os.environ["FRIDGE_BROWSER_MODE"] = "headless"
        assert t.modes() == [True]
        os.environ["FRIDGE_BROWSER_MODE"] = "visible"
        assert t.modes() == [False]
    finally:
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        if old is not None:
            os.environ["FRIDGE_BROWSER_MODE"] = old


def test_site_signals_listing_and_page():
    # real listing excerpts: Thermador has reviews switched off (0/0), Bosch/Siemens items carry rating {average, count}
    assert t.item_signals({"rating": {"average": 0, "count": 0}, "buyAreaOptionBadges": {"primaryBadge": None, "secondaryBadge": None}}) == {}
    assert t.item_signals({"buyAreaOptionBadges": {"primaryBadge": {"text": "Explore Virtual Showroom"}, "secondaryBadge": None}}) == {}
    assert t.item_signals({"rating": {"average": 4.3, "count": 531}}) == {"rating": 4.3, "review_count": 531}
    assert t.item_signals({"rating": {"average": 5, "count": 2}, "buyAreaOptionBadges": {"primaryBadge": {"text": "NEW"}}}) == \
        {"rating": 5.0, "review_count": 2, "is_new": True}
    assert t.item_signals({"rating": {"average": 9, "count": 3}}) == {}  # out of the 5-point scale: not trusted
    # a listing item that states signals lands in attrs with src 'listing'; absent signals add no keys
    item = {"productCode": "PRG304WH", "urlPath": "/mkt-product/ranges/30-ranges/PRG304WH", "productName": ["Gas Range 30''"],
            "rating": {"average": 4.5, "count": 12}}
    c = t.item_to_candidate(item, "cooking", "gas_oven", "ranges")
    assert c.attrs["rating"] == 4.5 and c.attrs["review_count"] == 12 and c.attrs_src["rating"] == "listing"
    assert "is_new" not in c.attrs and "release_date" not in c.attrs
    # page: first isNewProduct/releaseDate = the page's product; null/false stay unknown (key omitted)
    base = '"product":{"productCode":"X","releaseDate":%s,"isNewProduct":%s,"productFamily":"Cookers"}'
    assert t.page_signals(base % ("null", "false")) == {}
    assert t.page_signals(base % ('"2025-03-01T00:00:00Z"', "true")) == \
        {"is_new": True, "release_date": "2025-03-01", "release_src": "site"}
    assert t.page_signals(base % ('"2024-11"', "false")) == {"release_date": "2024-11", "release_src": "site"}
    jl = '"aggregateRating":{"@type":"AggregateRating","bestRating":10,"ratingValue":8.6,"reviewCount":40}'
    assert t.page_signals(jl) == {"rating": 4.3, "review_count": 40}
    # fixtures: no review/NEW data on these pages -> the record leaves all five fields unset
    r, _ = _parse("MC30WS")
    assert (r.rating, r.review_count, r.is_new, r.release_date, r.release_src) == (None,) * 5


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
