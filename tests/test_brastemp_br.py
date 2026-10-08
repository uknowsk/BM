"""Plain-assert offline tests (saved fixtures). Run: python tests/test_brastemp_br.py
brastemp_br: VTEX catalog API (listing + product), Portuguese specs, BRL sale prices, rating from the product page."""
import contextlib
import json
import os
import re
import sys
from pathlib import Path

os.environ["FRIDGE_I18N_LLM"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _electrolux_common as ec
import _whirlpool_br_common as wb
import brastemp_br as b
import catalog

FX = Path(__file__).parent / "fixtures" / "brastemp_br"
CATS = {"eletrodomesticos/fogao": "category_fogao.json", "eletrodomesticos/cooktop": "category_cooktop.json",
        "eletrodomesticos/forno": "category_forno.json", "eletrodomesticos/micro-ondas": "category_micro.json"}
PDP = "https://www.brastemp.com.br/fogao-brastemp-5-bocas-cor-inox-com-turbo-chama-bfs5gdr/p"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


def _product(file, ref):
    return next(p for p in _j(file) if p["productReference"] == ref)


class FakeTranslator:
    """Deterministic stand-in for i18n.get('pt'): a tiny dictionary, everything else unchanged."""
    LABELS = {"Quantidade de Bocas": "Number of burners", "Capacidade forno (L)": "Oven capacity (L)"}
    VALUES = {"Sim": "Yes", "Não": "No", "Inox": "Stainless steel", "5 bocas": "5 burners"}

    def translate_many(self, texts, kind="value"):
        d = self.LABELS if kind == "label" else self.VALUES
        return [d.get(t, t) for t in texts]

    def translate_value(self, text):
        return self.VALUES.get(text, text)


class FakeFetcher:
    """json(url) answers category windows (_from/_to) and single-product lookups from fixtures; html/rendered return pages."""

    def __init__(self, pages=None, html="", rendered=""):
        self.pages = {k: _j(v) for k, v in (pages or {}).items()}
        self.page_html, self.rendered_html, self.calls = html, rendered, []

    def json(self, url):
        self.calls.append(url)
        m = re.search(r"/search/(eletrodomesticos/[a-z-]+)\?_from=(\d+)&_to=(\d+)", url)
        if m:
            items = self.pages.get(m.group(1))
            if items is None:
                raise RuntimeError(f"HTTP 404 {url}")
            return items[int(m.group(2)):int(m.group(3)) + 1]
        m = re.search(r"/search/([A-Za-z0-9._-]+)/p$", url)
        if m:
            return [p for items in self.pages.values() for p in items if p.get("linkText") == m.group(1)][:1]
        raise AssertionError(f"unexpected fetch {url}")

    def html(self, url):
        self.calls.append(url)
        return self.page_html

    def rendered(self, url, needle, timeout_ms=0):
        self.calls.append(url)
        return self.rendered_html


@contextlib.contextmanager
def fake_fetcher(fetcher):
    orig_open, orig_page = wb.open_fetcher, wb.PAGE_SIZE
    wb.open_fetcher = lambda site: contextlib.nullcontext(fetcher)
    try:
        yield fetcher
    finally:
        wb.open_fetcher, wb.PAGE_SIZE = orig_open, orig_page


def test_constants_and_supported_subcategories():
    assert (b.BRAND, b.COUNTRY, b.REGION, b.CURRENCY) == ("Brastemp", "br", "sa", "BRL")
    assert b.SUPPORTED_SUBCATEGORIES == {"microwave", "sco", "gas_oven", "gas_cooktop", "electric_oven", "induction"}
    assert b.SUPPORTED_SUBCATEGORIES <= set(catalog.CATEGORY_TREE["cooking"]["children"])
    assert catalog.brand_slug(b.BRAND) + "_" + b.COUNTRY == "brastemp_br"
    for bad in ("radiant", "otr", "french_door"):
        try:
            b.discover(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_classify_shared_rule():
    f = _product("category_fogao.json", "BFS5GDR")
    assert b.classify(f) == "gas_oven"                                             # fogao = range with oven
    assert b.classify(_product("category_fogao.json", "BYS5PCR")) == "gas_oven"      # fogao de embutir (with oven)
    assert b.classify(_product("category_cooktop.json", "BDS62AE")) == "gas_cooktop"  # Tipo 'A gas'
    assert b.classify(_product("category_cooktop.json", "BDR90AR")) == "gas_cooktop"  # rangetop
    assert b.classify(_product("category_cooktop.json", "BDJ77BE")) == "induction"
    assert b.classify(_product("category_forno.json", "BOX84AE")) == "electric_oven"
    assert b.classify(_product("category_forno.json", "BOA84AE")) == "gas_oven"      # gas wall oven
    assert b.classify(_product("category_micro.json", "BMS23AE")) == "microwave"
    assert b.classify(_product("category_micro.json", "BM146AE")) == "microwave"     # built-in microwave
    assert b.classify(_product("category_micro.json", "BMR31AS")) == "sco"           # 'Forno Multifuncional com Microondas'
    # a plain oven whose 'Tipo do Produto' spec wrongly says 'Forno com Micro-ondas' stays an electric oven
    assert b.classify(_product("category_forno.json", "BOX47AR")) == "electric_oven"
    # bundles and warming drawers are not sold appliances of a sub group
    assert b.classify(_product("category_fogao.json", "BD85_BO8A_CJ")) is None
    assert b.classify(_product("category_cooktop.json", "BD61_BO8A_CJ")) is None
    assert b.classify(_product("category_forno.json", "BOE15AR")) is None
    # gas + induction hybrid fits no sub group; outlet stock and benchtop ovens are skipped
    hybrid = dict(_product("category_cooktop.json", "BDJ77BE"), productName="Cooktop 4 Bocas de Indução e a Gás Híbrido")
    assert b.classify(hybrid) is None
    outlet = dict(f, categories=["/Outlet/Fogão/"])
    assert b.classify(outlet) is None
    bench = dict(_product("category_forno.json", "BOX84AE"), productName="Forno Elétrico de Mesa 44 Litros")
    assert b.classify(bench) is None


def test_discover_candidates_and_price_rules():
    with fake_fetcher(FakeFetcher(CATS)) as f:
        got = {c.model_number: c for c in b.discover("gas_oven", 30)}
    assert list(got) == ["BFS5GDR", "BYS5PCR", "BFD5LAE", "BFO4NBR", "BOA84AE"]   # combo skipped, wall gas oven included
    c = got["BFS5GDR"]
    assert (c.brand, c.category, c.subcategory) == ("Brastemp", "cooking", "gas_oven")
    assert (c.region, c.country, c.currency, c.price_usd) == ("sa", "br", "BRL", None)
    # current sale price 'Price' (2336.0): neither ListPrice (2919.0) nor the PIX price (2219.2) of the instalment table
    assert c.price_local == 2336.0
    assert c.url == PDP
    assert c.attrs["width_in"] == 30.6 and c.attrs["capacity_total_cuft"] == 3.39 and c.attrs["burners"] == 5
    assert c.attrs["fuel"] == "gas" and c.attrs["finish"] == "stainless"
    assert c.attrs["release_date"] == "2024-08-13" and c.attrs["release_src"] == "site"
    assert "rating" not in c.attrs and "is_new" not in c.attrs and "wifi" not in c.attrs   # unknown = key absent
    assert set(c.attrs_src.values()) == {"listing"}
    assert got["BFD5LAE"].attrs["fuel"] == "dual_fuel" and got["BFD5LAE"].attrs["capacity_total_cuft"] == 4.77   # 96 L + 39 L
    assert got["BOA84AE"].attrs.get("burners") is None and got["BOA84AE"].attrs["fuel"] == "gas"
    assert f.calls[0].startswith("https://www.brastemp.com.br/api/catalog_system/pub/products/search/eletrodomesticos/fogao?")


def test_discover_every_supported_sub():
    with fake_fetcher(FakeFetcher(CATS)):
        by = {s: [c.model_number for c in b.discover(s, 30)] for s in sorted(b.SUPPORTED_SUBCATEGORIES)}
    assert by["gas_cooktop"] == ["BDS62AE", "BDR90AR"] and by["induction"] == ["BDJ77BE"]
    assert by["electric_oven"] == ["BOX84AE", "BOX47AR"] and by["sco"] == ["BMR31AS"]
    assert by["microwave"] == ["BMS23AE", "BM146AE", "BMC29AR"]
    with fake_fetcher(FakeFetcher(CATS)):
        mw = {c.model_number: c for c in b.discover("microwave", 30)}
    assert mw["BMS23AE"].attrs["capacity_total_cuft"] == 0.81            # '23 Litros' in the name
    assert mw["BMS23AE"].attrs_src["capacity_total_cuft"] == "name"       # name-derived, flagged as such


def test_limit_and_pagination():
    with fake_fetcher(FakeFetcher(CATS)) as f:
        wb.PAGE_SIZE = 2          # 5 fogao fixtures -> windows of 2
        got = b.discover("gas_oven", 3)
    assert [c.model_number for c in got] == ["BFS5GDR", "BYS5PCR", "BFD5LAE"]
    assert [u for u in f.calls if "fogao?" in u][:2] == [
        "https://www.brastemp.com.br/api/catalog_system/pub/products/search/eletrodomesticos/fogao?_from=0&_to=1",
        "https://www.brastemp.com.br/api/catalog_system/pub/products/search/eletrodomesticos/fogao?_from=2&_to=3"]


def test_rating_parsers():
    assert wb.parse_rating(_t("pdp_BFS5GDR.html")) == {"rating": 4.3, "review_count": 375}
    assert wb.parse_rating("<html>no reviews</html>") == {}
    assert wb.parse_rating('{"productRatings":{"averageRating":0,"reviewCount":0}}') == {}
    ld = '<script type="application/ld+json">{"aggregateRating":{"@type":"AggregateRating","ratingValue":"4.5","reviewCount":"415"}}</script>'
    assert wb.parse_rating(ld) == {"rating": 4.5, "review_count": 415}


def test_scrape_record_and_raw_specs():
    seen = []
    orig = ec.fetch_docs
    ec.fetch_docs = lambda brand, model, wanted: (seen.append(wanted), [])[1]
    try:
        with fake_fetcher(FakeFetcher(CATS, html=_t("pdp_BFS5GDR.html"))) as f:
            rec, docs, raw = wb.scrape(b.SITE, PDP, translator=FakeTranslator())
    finally:
        ec.fetch_docs = orig
    assert rec.model_number == "BFS5GDR" and rec.subcategory == "gas_oven" and rec.category == "cooking"
    assert (rec.region, rec.country, rec.currency, rec.price_local, rec.price_usd) == ("sa", "br", "BRL", 2336.0, None)
    assert (rec.rating, rec.review_count) == (4.3, 375)
    assert rec.release_date == "2024-08-13" and rec.release_src == "site" and rec.is_new is None
    assert (rec.height_in, rec.width_in, rec.depth_in) == (38.4, 30.6, 26.8) and rec.weight_lb == 90.4
    assert rec.capacity_total_cuft == 3.39 and rec.voltage_v == "110V/220V"
    assert rec.finish_color == "Stainless steel"
    assert rec.image_url.startswith("https://brastemp.vteximg.com.br/arquivos/ids/268690/")
    assert rec.extra_specs["Technical specifications > Number of burners"] == "5 burners"
    assert rec.extra_specs["Technical specifications > Oven capacity (L)"] == "96"
    assert rec.extra_specs["Energy > Inmetro PBE class"] == "A"
    assert not any("EAN" in k or "NCM" in k or "Manual" in k for k in rec.extra_specs)
    assert "Botões removíveis" in rec.pod_features and "Função Grill" in rec.pod_features   # untranslated stays as is
    src = next(r for r in raw if r.key == "Quantidade de Bocas")                           # Portuguese source kept
    assert src.value == "5 bocas" and src.section == "Caracteristícas Técnicas" and src.source == "web"
    assert [t for t, _ in seen[0]] == ["Manual", "QuickSpecs"]
    assert all(u.startswith("https://whirlpool.vteximg.com.br/arquivos/") for _, u in seen[0])
    assert not docs and f.calls[-1] == PDP and f.calls[0].endswith("/search/fogao-brastemp-5-bocas-cor-inox-com-turbo-chama-bfs5gdr/p")


def test_scrape_with_real_pt_translator_keeps_structure():
    with fake_fetcher(FakeFetcher(CATS, html="<html></html>")):
        orig = ec.fetch_docs
        ec.fetch_docs = lambda *a: []
        try:
            rec, _, raw = b.scrape(PDP)
        finally:
            ec.fetch_docs = orig
    assert rec.rating is None and rec.review_count is None          # page without reviews -> unknown
    assert rec.extra_specs and all(" > " in k for k in rec.extra_specs)
    assert len(raw) >= 20 and all(r.value for r in raw)


def test_url_and_host_validation():
    for bad in ("https://www.consul.com.br/fogao-x-bfs5gdr/p", "http://www.brastemp.com.br/fogao-x-bfs5gdr/p",
                "https://www.brastemp.com.br.evil.example/fogao-x-bfs5gdr/p", "https://www.brastemp.com.br/eletrodomesticos/fogao",
                "https://www.brastemp.com.br/a/b/p", "https://www.brastemp.com.br/..%2Fx/p"):
        try:
            b.scrape(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert wb.link_from_url(b.SITE, PDP + "?idsku=1") == "fogao-brastemp-5-bocas-cor-inox-com-turbo-chama-bfs5gdr"
    try:
        wb._search_url(b.SITE, "eletrodomesticos/../x", 0)
        raise AssertionError("path")
    except ValueError:
        pass


def test_fetcher_plain_http_then_browser_fallback():
    class Resp:
        def __init__(self, status, text, url, headers=None):
            self.status_code, self.text, self.url, self.headers = status, text, url, headers or {}

    class Http:
        def __init__(self, status, location=None):
            self.status, self.location, self.calls = status, location, []

        def get(self, url, timeout=0, allow_redirects=True):
            assert allow_redirects is False
            self.calls.append(url)
            if self.location and len(self.calls) == 1:
                return Resp(302, "", url, {"Location": self.location})
            return Resp(self.status, "plain", url)

        def close(self):
            pass

    class Page:
        def evaluate(self, js, url):
            return [200, "from-browser"]

    orig = wb._throttle
    wb._throttle = lambda: None
    try:
        f = wb.Fetcher(b.SITE)
        f._http = Http(200)
        assert f.get("https://www.brastemp.com.br/x") == (200, "plain")
        f._http = Http(200, location="/y")         # a same-host redirect is followed by hand
        assert f.get("https://www.brastemp.com.br/x") == (200, "plain") and f._http.calls[-1] == "https://www.brastemp.com.br/y"
        f._http = Http(200, location="https://evil.example.com/y")   # a redirect off the allowed hosts is refused
        try:
            f.get("https://www.brastemp.com.br/x")
            raise AssertionError("redirect")
        except ValueError:
            pass
        f._http = Http(403)                       # blocked -> a real browser page takes over (headless/visible helpers)
        f._open_browser = lambda: Page()
        assert f.get("https://www.brastemp.com.br/x") == (200, "from-browser")
        try:
            f.get("https://evil.example.com/x")
            raise AssertionError("host")
        except ValueError:
            pass
        try:
            f.get("http://www.brastemp.com.br/x")
            raise AssertionError("scheme")
        except ValueError:
            pass
    finally:
        wb._throttle = orig


if __name__ == "__main__":
    tests = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    for n, fn in tests:
        fn()
        print("ok", n)
    print(f"{len(tests)} passed")
