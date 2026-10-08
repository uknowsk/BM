"""Plain-assert offline tests for miele_br (trimmed fixtures of shop.mielebrasil.com.br). Run: python tests\\test_miele_br.py"""
import os
import sys
from pathlib import Path

os.environ["FRIDGE_I18N_LLM"] = "0"  # glossary + rules only: deterministic, no local LLM
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import miele_br as s

FX = Path(__file__).parent / "fixtures" / "miele_br"
BASE = "https://shop.mielebrasil.com.br"


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


def _parse(slug):
    return s.parse_product(f"{BASE}/products/{slug}", _t(f"pdp_{slug}.html"))


def test_constants_and_supported():
    assert (s.COUNTRY, s.REGION, s.CURRENCY, s.BRAND) == ("br", "sa", "BRL", "Miele")
    assert s.SUPPORTED_SUBCATEGORIES == {"gas_cooktop", "induction", "gas_oven", "electric_oven", "sco"}
    assert s.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking")) and s.DELAY_S >= 2.0  # robots.txt Crawl-delay: 2
    for sub in ("french_door", "microwave", "radiant", "otr"):
        try:
            s.discover(sub)
        except ValueError:
            continue
        raise AssertionError(sub)
    assert catalog.module_name("Miele", "br") == "miele_br"


def test_classify():
    c = s.classify
    assert c("Cooktop a gás 4 bocas KM 3465 LP") == "gas_cooktop" and c("Rangetop 48 com Chapa KMR 1356-3 LP") == "gas_cooktop"
    assert c("Placa a gás com um queimador 288 mm - CS 1011-1 LP") == "gas_cooktop"
    assert c("Cooktop de Indução KM 7464") == "induction" and c("Placa elétrica de indução 380 mm - Superfície de cer...") == "induction"
    assert c("Range Cooker Dual Fuel 36 HR 1934 2 LP") == "gas_oven"
    assert c("Forno Elétrico H 7660 CLST") == "electric_oven" and c("Forno Combinado a Vapor DGC 7870 CLST - 75CM") == "electric_oven"
    assert c("Fornos Combinado Com Micro Ondas H 7870 BM CLST") == "sco" and c("Forno Micro-ondas e Convecção H 7240 BM BR EDST/CLST") == "sco"
    assert c("Churrasqueira elétrica com pedra vulcânica 380 mm - CS 1322 BG") is None
    assert c("Placa de grelhar TepanYaki 380 mm - CS 1327 TP") is None and c("Gaveta aquecida ESW 7020") is None
    assert c("Coifa de parede DA 6808 W") is None and c("Lava-louças G 7100") is None


def test_model_of():
    m = s.model_of
    assert m("Cooktop a gás 4 bocas KM 3465 LP") == "KM 3465 LP" and m("Range Cooker 36 com Chapa HR 1136 1 LP (AG)") == "HR 1136 1 LP (AG)"
    assert m("Forno Combinado a Vapor DGC 7840  EDST - 60 CM") == "DGC 7840 EDST" and m("Forno Micro-ondas e Convecção H 7240 BM BR EDST/CLST") == "H 7240 BM BR EDST/CLST"
    assert m("Placa elétrica de indução 380 mm - Superfície de cer...", "/products/placa-eletrica-380-mm-cs-1222-i") == "CS 1222 I"  # name cut by the listing
    assert m("Produto sem código", "/products/produto-sem-codigo") is None


def test_listing_candidates():
    gas = s.parse_listing(_t("listing_fogoes-a-gas.html"))
    assert [(i["slug"], i["price"]) for i in gas] == [("/products/fogao-cooktop-gas-4-bocas-dual-wok-km-3465-lp", "R$32.899,00"),
                                                      ("/products/cooktop-gas-5-bocas-km-3475-lp", "R$39.999,00")]  # '?taxon_id=' dropped
    c = [s.item_to_candidate(i, "gas_cooktop") for i in gas][0]
    assert (c.brand, c.category, c.subcategory, c.country, c.region, c.currency) == ("Miele", "cooking", "gas_cooktop", "br", "sa", "BRL")
    assert c.model_number == "KM 3465 LP" and c.price_usd is None and c.price_local == 32899.0
    assert c.url == BASE + "/products/fogao-cooktop-gas-4-bocas-dual-wok-km-3465-lp" and c.attrs == {"burners": 4, "fuel": "gas"}
    pro = s.parse_listing(_t("listing_proline-combisets.html"))
    assert [i["name"] for i in pro][-1] == "Placa de grelhar TepanYaki 380 mm - CS 1327 TP"
    got = {sub: [x.model_number for i in pro if (x := s.item_to_candidate(i, sub))] for sub in ("gas_cooktop", "induction", "sco")}
    assert got == {"gas_cooktop": ["CS 1011-1 LP"], "induction": ["CS 1222 I"], "sco": []}  # grill / barbecue modules left out
    assert [x for i in pro if (x := s.item_to_candidate(i, "gas_cooktop"))][0].attrs == {"width_in": 11.3, "fuel": "gas"}  # 288 mm
    rc = s.parse_listing(_t("listing_range-cookers.html"))
    cands = [s.item_to_candidate(i, "gas_oven") for i in rc]
    assert [x.model_number for x in cands] == ["HR 1956 3 LP", "HR 1134 1 LP (AG)", "HR 1136 1 LP (AG)", "HR 1934 2 LP", "HR 1936 3 LP"]
    assert cands[0].attrs == {"width_in": 48.0, "fuel": "dual_fuel"} and cands[1].attrs == {"width_in": 36.0, "fuel": "gas"}
    assert cands[0].price_local == 239999.0
    ov = [s.item_to_candidate(i, "electric_oven") for i in s.parse_listing(_t("listing_fornos-eletricos.html"))]
    assert [x.model_number for x in ov] == ["H 7660 CLST", "H 7880 BP CLST", "H 7880 BP GRGR", "H 7880 BP OBSW"] and ov[1].price_local == 100599.0
    assert s.item_to_candidate(gas[0], "induction") is None


def test_parse_price_and_filters():
    assert s.parse_price("R$17.159,40") == 17159.4 and s.parse_price("R$ 1.299,00") == 1299.0 and s.parse_price(None) is None
    assert s.parse_price("R$0,00") is None and s.parse_price("sob consulta") is None
    ok = {"slug": "/products/cooktop-gas-5-bocas-km-3475-lp", "name": "Cooktop a gás 5 bocas KM 3475 LP", "price": "R$39.999,00"}
    assert s.item_to_candidate(ok, "gas_cooktop").price_local == 39999.0
    assert s.item_to_candidate({**ok, "price": None}, "gas_cooktop").price_local is None
    for bad in ({**ok, "slug": "//evil.com/products/x-y-z-w"}, {**ok, "slug": "/products/ab"}, {**ok, "slug": "/products/a-b-c-d-e?x=1"},
                {**ok, "slug": "/products/cooktop-gas-sem-codigo", "name": "Cooktop a gás sem código"}):
        assert s.item_to_candidate(bad, "gas_cooktop") is None, bad


def test_gas_cooktop_record():
    r, raw = _parse("cooktop-gas-5-bocas-km-3475-lp")
    assert (r.brand, r.category, r.subcategory, r.country, r.region, r.currency) == ("Miele", "cooking", "gas_cooktop", "br", "sa", "BRL")
    assert r.model_number == "KM 3475 LP" and r.price_local == 39999.0 and r.price_usd is None  # microdata says CLP: ignored
    assert r.width_in == 35.8 and r.image_url.startswith("https://d21v6iwzex1yc.cloudfront.net/spree/images/")  # '91cm'
    assert (r.rating, r.review_count, r.is_new, r.release_date) == (None, None, None, None)  # the shop publishes none
    x = r.extra_specs
    assert x["General > Miele material number (TNR)"] == "11293040" and x["General > Cooking zones / burners"] == "5"
    assert x["Features > Controles simples"] == "Operação com uma só mão via controle de botão giratório"  # 'Label: value' bullet
    assert "Notes > Outlet item" not in x and len(raw) == 6 and raw[0].key == "Resumo" and raw[0].value.startswith("Fogão a gás 91cm")


def test_outlet_induction_record():
    r, _ = _parse("fogao-cooktop-inducao-km-7464")
    assert r.subcategory == "induction" and r.price_local == 25599.0 and r.width_in == 24.4  # '62cm'
    assert r.extra_specs["Notes > Outlet item"].startswith("Yes") and r.extra_specs["General > Cooking zones / burners"] == "4"
    assert not any("Aviso importante" in k for k in r.extra_specs) and not any("outlet" in f.lower() for f in r.pod_features)


def test_oven_record_with_size_line():
    r, raw = _parse("forno-eletrico-conveccao-h-7660-clst")
    assert r.subcategory == "electric_oven" and r.model_number == "H 7660 CLST" and r.price_local == 57999.0
    assert (r.width_in, r.height_in, r.depth_in) == (23.4, 23.5, 22.4)  # 595 x 596 x 569 mm (L x A x P)
    assert r.extra_specs["Size > Product dimensions (W x H x D) (mm)"] == "595 x 596 x 569"
    assert r.pod_features == ["Moister Plus", "Oven Compartment Camera", "MasterChef", "TasteControl"]
    assert any(w.key == "Dimensões do produto (L x A x P) em mm" and w.value == "595 x 596 x 569" for w in raw)


def test_range_cooker_combi_and_proline_records():
    r, _ = _parse("fogao-range-cooker-hr-1134-1-lp-ag")
    assert r.subcategory == "gas_oven" and r.model_number == "HR 1134 1 LP (AG)" and r.price_local == 135999.0 and r.width_in == 35.8
    c, _ = _parse("fornos-combinado-com-micro-ondas-h-7870-bm-clst")
    assert c.subcategory == "sco" and c.model_number == "H 7870 BM CLST" and c.price_local == 64799.0 and c.width_in is None
    p, _ = _parse("placa-eletrica-de-inducao-380-mm-1-zona-superficie-de-ceramica-de-vidro-cs-1222-i")
    assert p.subcategory == "induction" and p.model_number == "CS 1222 I" and p.width_in == 15.0  # full name on the page, '380 mm de largura'


def test_url_check():
    assert s.url_check(BASE + "/products/cooktop-gas-5-bocas-km-3475-lp") == "/products/cooktop-gas-5-bocas-km-3475-lp"
    for bad in ("http://shop.mielebrasil.com.br/products/cooktop-gas-5-bocas-km-3475-lp", "https://evil.com/products/cooktop-gas-5-bocas",
                "https://shop.mielebrasil.com.br.evil.com/products/cooktop-gas-5-bocas", BASE + "/products/cooktop-gas-5-bocas?taxon_id=18",
                BASE + "/t/cozinha/gastronomia/fogoes", BASE + "/products/a/b", "https://user@shop.mielebrasil.com.br/products/cooktop-gas-5-bocas"):
        try:
            s.url_check(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_incomplete_page_is_an_error():
    try:
        s.parse_product(BASE + "/products/cooktop-gas-5-bocas-km-3475-lp", "<html><body>The requested URL was rejected.</body></html>")
    except s.MieleBrError:
        return
    raise AssertionError("expected MieleBrError")


def test_discover_with_fake_fetch():
    calls = []

    def fake(url, probe):
        calls.append(url)
        return _t("listing_fogoes-a-gas.html") if url.endswith("fogoes-a-gas") else "<html>product-card</html>"

    real, s.fetch_html = s.fetch_html, fake
    s._TAXON_CACHE.clear()
    try:
        got = s.discover("gas_cooktop")
        assert [c.model_number for c in got] == ["KM 3465 LP", "KM 3475 LP"]
        assert calls == [f"{BASE}/t/cozinha/gastronomia/fogoes/{t}" for t in ("fogoes-a-gas", "range-tops", "proline-combisets")]
        assert len(s.discover("gas_cooktop", limit=1)) == 1 and len(calls) == 3  # cached
    finally:
        s.fetch_html = real
        s._TAXON_CACHE.clear()


def test_scrape_with_fake_fetch():
    real, s.fetch_html = s.fetch_html, lambda url, probe: _t("pdp_forno-eletrico-conveccao-h-7660-clst.html")
    try:
        r, docs, raw = s.scrape(BASE + "/products/forno-eletrico-conveccao-h-7660-clst")
    finally:
        s.fetch_html = real
    assert r.model_number == "H 7660 CLST" and docs == [] and len(raw) == 6


def test_ssl_error_switches_to_browser_without_disabling_verification():
    import requests
    calls = []
    real_r, real_b, flag = s._via_requests, s._via_browser, s._requests_broken[0]

    def bad(url, probe):
        calls.append("requests")
        raise requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")

    s._via_requests, s._via_browser, s._requests_broken[0] = bad, lambda url, probe, headless: calls.append("browser") or "<html>ok</html>", False
    try:
        assert s.fetch_html(BASE + "/products/cooktop-gas-5-bocas-km-3475-lp", "ok") == "<html>ok</html>"
        assert s.fetch_html(BASE + "/products/cooktop-gas-5-bocas-km-3475-lp", "ok") == "<html>ok</html>"
        assert calls == ["requests", "browser", "browser"]  # requests tried once, then skipped
    finally:
        s._via_requests, s._via_browser, s._requests_broken[0] = real_r, real_b, flag


def test_browser_mode_env():
    old = os.environ.pop("FRIDGE_BROWSER_MODE", None)
    try:
        assert s._strategies() == ["requests", "headless", "visible"]
        os.environ["FRIDGE_BROWSER_MODE"] = "headless"
        assert s._strategies() == ["headless"]
        os.environ["FRIDGE_BROWSER_MODE"] = "visible"
        assert s._strategies() == ["visible"]
    finally:
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        if old is not None:
            os.environ["FRIDGE_BROWSER_MODE"] = old


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
