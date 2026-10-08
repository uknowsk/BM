"""Plain-assert offline tests (saved fixtures). Run: python tests/test_consul_br.py
consul_br: same VTEX catalog API as brastemp_br (shared _whirlpool_br_common.py); gas ranges, cooktops, ovens, microwaves."""
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
import consul_br as c

FX = Path(__file__).parent / "fixtures" / "consul_br"
CATS = {"eletrodomesticos/fogao": "category_fogao.json", "eletrodomesticos/cooktop": "category_cooktop.json",
        "eletrodomesticos/forno": "category_forno.json", "eletrodomesticos/micro-ondas": "category_micro.json"}
PDP = "https://www.consul.com.br/fogao-de-piso-consul-4-bocas-com-manipulos-removiveis-cfo4nar/p"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _product(file, ref):
    return next(p for p in _j(file) if p["productReference"] == ref)


class FakeFetcher:
    def __init__(self, pages=None, html=""):
        self.pages = {k: _j(v) for k, v in (pages or {}).items()}
        self.page_html, self.calls = html, []

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


@contextlib.contextmanager
def fake_fetcher(fetcher):
    orig = wb.open_fetcher
    wb.open_fetcher = lambda site: contextlib.nullcontext(fetcher)
    try:
        yield fetcher
    finally:
        wb.open_fetcher = orig


def test_constants_and_supported_subcategories():
    assert (c.BRAND, c.COUNTRY, c.REGION, c.CURRENCY) == ("Consul", "br", "sa", "BRL")
    # the shop sells no combination ovens, induction or electric (radiant) cooktops: nothing to claim
    assert c.SUPPORTED_SUBCATEGORIES == {"microwave", "gas_oven", "gas_cooktop", "electric_oven"}
    assert c.SUPPORTED_SUBCATEGORIES <= set(catalog.CATEGORY_TREE["cooking"]["children"])
    assert catalog.brand_slug(c.BRAND) + "_" + c.COUNTRY == "consul_br"
    for bad in ("sco", "induction", "radiant", "otr", "top_load"):
        try:
            c.discover(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_classify_and_bundles():
    assert c.classify(_product("category_fogao.json", "CFO4NAR")) == "gas_oven"
    assert c.classify(_product("category_cooktop.json", "CD075BE")) == "gas_cooktop"
    assert c.classify(_product("category_forno.json", "COB84BE")) == "electric_oven"
    assert c.classify(_product("category_forno.json", "COA84CE")) == "gas_oven"
    assert c.classify(_product("category_micro.json", "CM146AE")) == "microwave"
    for ref, f in (("COB47_CD075_CJ", "category_cooktop.json"), ("COB47_0000_CJ", "category_forno.json")):
        assert c.classify(_product(f, ref)) is None          # 'Combo ...' bundles


def test_discover_every_sub_and_prices():
    with fake_fetcher(FakeFetcher(CATS)):
        by = {s: {x.model_number: x for x in c.discover(s, 30)} for s in sorted(c.SUPPORTED_SUBCATEGORIES)}
    assert list(by["gas_oven"]) == ["CFO4NAR", "CFO4ZAB", "CFS5VAR", "COA84CE"]
    assert list(by["gas_cooktop"]) == ["CD075BE"] and list(by["electric_oven"]) == ["COB84BE"]
    assert list(by["microwave"]) == ["CMS23AR", "CM146AE"]
    x = by["gas_oven"]["CFO4NAR"]
    assert (x.brand, x.region, x.country, x.currency, x.price_usd, x.price_local) == ("Consul", "sa", "br", "BRL", None, 1429.0)
    assert x.url == PDP and x.attrs["width_in"] == 20.3 and x.attrs["burners"] == 4 and x.attrs["fuel"] == "gas"
    assert by["gas_oven"]["CFO4ZAB"].attrs["capacity_total_cuft"] == 2.05      # 'A gás: 58' -> 58 L
    assert by["electric_oven"]["COB84BE"].attrs["capacity_total_cuft"] == 2.97 and by["electric_oven"]["COB84BE"].attrs["fuel"] == "electric"
    assert by["gas_cooktop"]["CD075BE"].attrs["width_in"] == 28.7 and "capacity_total_cuft" not in by["gas_cooktop"]["CD075BE"].attrs


def test_scrape_record_and_rating():
    html = '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"data":{"product":{"productRatings":{"averageRating":4.4,"reviewCount":516}}}}}}</script>'
    seen = []
    orig = ec.fetch_docs
    ec.fetch_docs = lambda brand, model, wanted: (seen.append((brand, model, wanted)), [])[1]
    try:
        with fake_fetcher(FakeFetcher(CATS, html=html)):
            rec, docs, raw = c.scrape(PDP)
    finally:
        ec.fetch_docs = orig
    assert (rec.brand, rec.model_number, rec.subcategory, rec.category) == ("Consul", "CFO4NAR", "gas_oven", "cooking")
    assert (rec.country, rec.currency, rec.price_local, rec.rating, rec.review_count) == ("br", "BRL", 1429.0, 4.4, 516)
    assert (rec.width_in, rec.height_in, rec.depth_in, rec.weight_lb) == (20.3, 37.8, 23.7, 51.8)
    assert rec.release_date == "2015-09-21" and rec.release_src == "site" and rec.voltage_v == "Bivolt"
    assert rec.image_url.startswith("https://consul.vteximg.com.br/arquivos/ids/")
    assert rec.extra_specs and all(" > " in k for k in rec.extra_specs) and len(raw) >= 20
    assert any(r.key == "Quantidade de Bocas" and r.value == "4 bocas" for r in raw)    # Portuguese source kept
    assert seen[0][:2] == ("Consul", "CFO4NAR") and seen[0][2][0][0] == "Manual"
    assert not docs


def test_url_validation():
    for bad in ("https://www.brastemp.com.br/fogao-x-cfo4nar/p", "http://www.consul.com.br/fogao-x-cfo4nar/p",
                "https://www.consul.com.br/eletrodomesticos/fogao", "https://consul.com.br.evil.example/x-cfo4nar/p"):
        try:
            c.scrape(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    with fake_fetcher(FakeFetcher(CATS)):
        try:
            c.scrape("https://www.consul.com.br/produto-que-nao-existe-xyz/p")
            raise AssertionError("missing product")
        except ValueError:
            pass


if __name__ == "__main__":
    tests = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    for n, fn in tests:
        fn()
        print("ok", n)
    print(f"{len(tests)} passed")
