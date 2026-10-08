"""Plain-assert offline tests (saved fixtures). Run: python tests/test_electrolux_br.py
electrolux_br: loja.electrolux.com.br (VTEX IO) catalog API; the review summary comes from the rendered page (browser)."""
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
import catalog
import electrolux_br as e

FX = Path(__file__).parent / "fixtures" / "electrolux_br"
CATS = {"eletrodomesticos/fogao": "category_fogao.json", "eletrodomesticos/cooktops": "category_cooktops.json",
        "eletrodomesticos/fornos": "category_fornos.json", "eletrodomesticos/micro-ondas": "category_micro.json"}
FE4GG = "https://loja.electrolux.com.br/fogao-4-bocas-electrolux-cinza-efficient-mesa-vidro--tripla-chama-e-perfectcook--fe4gg-/p"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


def _product(file, ref):
    return next(p for p in _j(file) if p["productReference"] == ref)


class FakeFetcher:
    def __init__(self, pages=None, html="", rendered=""):
        self.pages = {k: _j(v) for k, v in (pages or {}).items()}
        self.page_html, self.rendered_html, self.calls, self.rendered_calls = html, rendered, [], []

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
        raise AssertionError("electrolux_br reads the rating from the rendered page, not from plain HTML")

    def rendered(self, url, needle, timeout_ms=0):
        self.rendered_calls.append((url, needle))
        return self.rendered_html


@contextlib.contextmanager
def fake_fetcher(fetcher):
    orig = wb.open_fetcher
    wb.open_fetcher = lambda site: contextlib.nullcontext(fetcher)
    try:
        yield fetcher
    finally:
        wb.open_fetcher = orig


def test_constants_and_supported_subcategories():
    assert (e.BRAND, e.COUNTRY, e.REGION, e.CURRENCY) == ("Electrolux", "br", "sa", "BRL")
    assert e.SUPPORTED_SUBCATEGORIES == {"microwave", "sco", "gas_oven", "gas_cooktop", "electric_oven", "induction"}
    assert e.SUPPORTED_SUBCATEGORIES <= set(catalog.CATEGORY_TREE["cooking"]["children"])
    assert catalog.module_name("Electrolux", "br") == "electrolux_br"
    assert e.BASE == "https://loja.electrolux.com.br" and e.SITE.render_rating
    for bad in ("radiant", "otr", "front_load"):
        try:
            e.discover(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_classify_shared_rule():
    assert e.classify(_product("category_fogao.json", "FE4GG")) == "gas_oven"
    assert e.classify(_product("category_fogao.json", "FE5EB")) == "gas_oven"          # 'Fogão de Embutir' (with oven)
    assert e.classify(_product("category_cooktops.json", "KE5GC")) == "gas_cooktop"
    assert e.classify(_product("category_cooktops.json", "IE4TW")) == "induction"
    assert e.classify(_product("category_cooktops.json", "IE8CH")) == "induction"      # hob with an integrated hood is still a hob
    assert e.classify(_product("category_cooktops.json", "IE62H")) is None             # gas + induction hybrid
    assert e.classify(_product("category_cooktops.json", "IE3TP")) is None             # portable single induction plate
    assert e.classify(_product("category_cooktops.json", "7909569484654+7909569449349")) is None   # cooktop + oven kit
    assert e.classify(_product("category_fornos.json", "OE8EF")) == "electric_oven"
    assert e.classify(_product("category_fornos.json", "OE8GF")) == "gas_oven"
    assert e.classify(_product("category_fornos.json", "ME4EP")) == "sco"              # oven '2 em 1 com Micro-ondas'
    assert e.classify(_product("category_fornos.json", "OE8GF + ME3BC")) is None       # 'Torre de cocção' bundle
    assert e.classify(_product("category_micro.json", "ME3BC")) == "microwave"
    assert e.classify(_product("category_micro.json", "OE8EF + ME3BP")) is None


def test_discover_every_sub():
    with fake_fetcher(FakeFetcher(CATS)):
        by = {s: {x.model_number: x for x in e.discover(s, 30)} for s in sorted(e.SUPPORTED_SUBCATEGORIES)}
    assert list(by["gas_oven"]) == ["FE4GG", "FE4DB", "FE5EB", "56EXT", "OE8GF"]
    assert list(by["gas_cooktop"]) == ["KE5GC"] and list(by["induction"]) == ["IE4TW", "IE8CH"]
    assert list(by["electric_oven"]) == ["OE8EF"] and list(by["sco"]) == ["ME4EP"]
    assert list(by["microwave"]) == ["ME3BC", "ME23S"]
    x = by["gas_oven"]["FE4GG"]
    assert (x.brand, x.category, x.subcategory, x.region, x.country, x.currency) == ("Electrolux", "cooking", "gas_oven", "sa", "br", "BRL")
    assert x.price_usd is None and x.price_local == 1549.0          # sale price, not ListPrice 2519
    assert x.url == FE4GG and x.attrs["width_in"] == 20.5 and x.attrs["burners"] == 4 and x.attrs["wifi"] is False
    assert x.attrs["release_date"] == "2025-03-24" and "rating" not in x.attrs and "is_new" not in x.attrs
    assert by["gas_oven"]["FE4DB"].attrs["fuel"] == "dual_fuel"                       # gas hob + electric upper oven
    assert by["gas_oven"]["FE4DB"].attrs["capacity_total_cuft"] == 3.7                # 72.6 L + 32.1 L
    assert by["sco"]["ME4EP"].attrs["fuel"] == "electric" and by["microwave"]["ME3BC"].attrs["capacity_total_cuft"] == 1.2


def test_spec_values_repeated_per_sku_are_not_summed():
    # live FE4GB: every field came back twice ('59 L | 59 L', '52 cm | 52 cm'): 118 L and a missing width were the result
    p = dict(_product("category_fogao.json", "FE4GG"))
    for name in ("Volume", "Volume do forno", "Capacidade do forno inferior ou simples (L)"):
        if name in p:
            p[name] = ["59 L", "59 L"]
    p["Largura do produto"] = ["52 cm", "52 cm"]
    specs = wb.spec_map(p)
    assert specs["largura do produto"] == "52 cm"
    litres, src = wb.capacity_litres(p, specs, "gas_oven")
    assert litres == 59.0 and src == "listing"
    c = wb.candidate(e.SITE, p, "gas_oven")
    assert c.attrs["width_in"] == 20.5 and c.attrs["capacity_total_cuft"] == 2.08


def test_rendered_rating_parser():
    assert wb.parse_rating(_t("pdp_FE4GG_rendered.html")) == {"rating": 4.5, "review_count": 415}
    assert wb.parse_rating("<html></html>") == {}


def test_scrape_uses_rendered_page_for_rating():
    seen = []
    orig = ec.fetch_docs
    ec.fetch_docs = lambda brand, model, wanted: (seen.append(wanted), [])[1]
    try:
        with fake_fetcher(FakeFetcher(CATS, rendered=_t("pdp_FE4GG_rendered.html"))) as f:
            rec, docs, raw = e.scrape(FE4GG)
    finally:
        ec.fetch_docs = orig
    assert f.rendered_calls == [(FE4GG, "aggregateRating")]
    assert (rec.model_number, rec.subcategory, rec.price_local, rec.currency) == ("FE4GG", "gas_oven", 1549.0, "BRL")
    assert (rec.rating, rec.review_count) == (4.5, 415)
    assert rec.wifi_supported is False and rec.wifi_evidence == "wifi: Não" and rec.frequency_hz == 60.0
    assert (rec.height_in, rec.width_in, rec.depth_in) == (38.2, 20.5, 25.0) and rec.capacity_total_cuft == 2.08
    assert rec.release_date == "2025-03-24" and rec.is_new is None
    assert rec.image_url.startswith("https://electrolux.vteximg.com.br/arquivos/ids/")
    assert rec.extra_specs and len(raw) >= 20 and any(r.key == "Quantidade de bocas" for r in raw)
    assert [t for t, _ in seen[0]] == ["Manual", "QuickSpecs"] and not docs


def test_rating_failure_never_fails_the_product():
    class Broken(FakeFetcher):
        def rendered(self, url, needle, timeout_ms=0):
            raise RuntimeError("browser unavailable")

    orig = ec.fetch_docs
    ec.fetch_docs = lambda *a: []
    try:
        with fake_fetcher(Broken(CATS)):
            rec, _, _ = e.scrape(FE4GG)
    finally:
        ec.fetch_docs = orig
    assert rec.model_number == "FE4GG" and rec.rating is None and rec.review_count is None


def test_url_validation():
    for bad in ("https://www.brastemp.com.br/x-fe4gg/p", "http://loja.electrolux.com.br/x-fe4gg/p",
                "https://loja.electrolux.com.br/", "https://loja.electrolux.com.br.evil.example/x-fe4gg/p"):
        try:
            e.scrape(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert wb.link_from_url(e.SITE, "https://www.electrolux.com.br/abc-def-fe4gg/p") == "abc-def-fe4gg"


if __name__ == "__main__":
    tests = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    for n, fn in tests:
        fn()
        print("ok", n)
    print(f"{len(tests)} passed")
