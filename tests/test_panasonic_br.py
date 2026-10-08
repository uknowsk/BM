"""Plain-assert offline tests for panasonic_br (trimmed fixtures of loja.panasonic.com.br). Run: python tests\\test_panasonic_br.py"""
import os
import sys
from pathlib import Path

os.environ["FRIDGE_I18N_LLM"] = "0"  # glossary + rules only: deterministic, no local LLM
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import panasonic_br as s

FX = Path(__file__).parent / "fixtures" / "panasonic_br"
BASE = "https://loja.panasonic.com.br"
GT68 = BASE + "/micro-ondas-panasonic-gt68-black-glass-nn-gt68lbru/p"


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


def _parse(model):
    return s.parse_product(GT68, _t(f"pdp_{model}.html"))


def test_constants_and_supported():
    assert (s.COUNTRY, s.REGION, s.CURRENCY, s.BRAND) == ("br", "sa", "BRL", "Panasonic")
    assert s.SELLS == {"microwave", "sco"} and s.SELLS <= set(catalog.sub_keys("cooking"))
    assert s.SUPPORTED_SUBCATEGORIES == set() and not catalog.supported("Panasonic", "br")  # switched off by user decision
    for sub in ("french_door", "otr", "induction", "gas_cooktop"):
        try:
            s.discover(sub)
        except ValueError:
            continue
        raise AssertionError(sub)
    assert catalog.module_name("Panasonic", "br") == "panasonic_br"


def test_classify():
    assert s.classify("Micro-ondas Intuitive+ Panasonic GT68 com SmartSense e Grill 30L Black Glass - NN-GT68LBRU") == "microwave"
    assert s.classify("Micro-ondas Intuitive+ Panasonic CD89 4 em 1 Airfryer 30L Black Glass - NN-CD89NBRU") == "sco"
    assert s.classify("Forno Micro-ondas Panasonic com Convecção 27L") == "sco"
    assert s.classify("Fritadeira Airfryer Panasonic 4L") is None and s.classify("Prato giratório para forno") is None


def test_listing_candidates():
    items = s.parse_listing(_t("listing_microondas.html"))
    assert len(items) == 9
    micro = [c for i in items if (c := s.item_to_candidate(i, "microwave"))]
    sco = [c for i in items if (c := s.item_to_candidate(i, "sco"))]
    assert [c.model_number for c in sco] == ["NN-CD89NBRU"] and len(micro) == 8
    c = micro[0]
    assert (c.brand, c.category, c.subcategory, c.country, c.region, c.currency) == ("Panasonic", "cooking", "microwave", "br", "sa", "BRL")
    assert c.model_number == "NN-GT68LBRU" and c.price_usd is None and c.price_local == 1099.0 and c.url == GT68
    assert c.attrs == {"capacity_total_cuft": 1.06, "finish": "black"} and c.attrs_src["finish"] == "name"  # 30 L
    assert sco[0].price_local == 2099.0
    assert {c.model_number: c.attrs["finish"] for c in micro}["NN-ST27LWRU"] == "white"  # 'Branco espelhado'
    assert all("rating" not in c.attrs and "is_new" not in c.attrs for c in micro)  # the listing shows neither


def test_candidate_filters():
    ok = {"name": "Micro-ondas Panasonic 21L Branco - NN-ST25LWRU", "url": BASE + "/micro-ondas-st25/p", "image": None, "price": 599}
    assert s.item_to_candidate(ok, "microwave").price_local == 599.0
    assert s.item_to_candidate({**ok, "price": 0}, "microwave").price_local is None
    for bad in ({**ok, "url": "https://evil.com/micro-ondas-st25/p"}, {**ok, "url": "http://loja.panasonic.com.br/micro-ondas-st25/p"},
                {**ok, "url": BASE + "/micro-ondas-st25/p?x=1"}, {**ok, "url": BASE + "/micro-ondas-st25"},
                {**ok, "name": "Micro-ondas Panasonic 21L Branco"}, {**ok, "name": "Fritadeira Panasonic - NN-ST25LWRU"}):
        assert s.item_to_candidate(bad, "microwave") is None, bad
    assert s.item_to_candidate(ok, "sco") is None


def test_grill_microwave_record():
    r, raw, docs = _parse("NN-GT68LBRU")
    assert (r.brand, r.category, r.subcategory, r.country, r.region, r.currency) == ("Panasonic", "cooking", "microwave", "br", "sa", "BRL")
    assert r.model_number == "NN-GT68LBRU" and r.price_local == 1099.0 and r.price_usd is None  # sellingPrice; listPrice is 1299
    assert r.finish_color == "Black" and r.voltage_v == "127/220"
    assert r.capacity_total_cuft == 1.06  # 'Volume Total' 30 L
    assert (r.width_in, r.height_in, r.depth_in) == (20.5, 12.8, 16.8)  # 520 x 325.5 x 427.3 mm (L x A x P)
    assert r.weight_lb == 34.0  # 15,4kg
    assert r.is_new is True and (r.release_date, r.release_src) == ("2021-02-05", "distribution")
    assert (r.rating, r.review_count) == (None, None)  # the store shows no reviews
    assert r.image_url == "https://panasonic.vtexassets.com/arquivos/ids/166023/GT68_frontal_V3.jpg?v=639071969369430000"
    assert r.pod_features[0] == "Antiaderente 4x mais fácil de limpar"
    x = r.extra_specs
    assert x["Specifications > Total volume"] == "30 L" and x["Specifications > Grill power"] == "1000W"
    assert x["Specifications > Energy efficiency class"] == "BR class A" and x["Specifications > Warranty"] == "12 months"
    assert len(raw) == 78 and any(w.key == "Dimensões do Produto (L x A x P )" and w.value == "520,0 x 325,5 x 427,3" for w in raw)
    assert docs == [("Manual", "https://panasonic-br.zendesk.com/hc/pt-br/article_attachments/360094013931/NN-GT68LBRU.pdf")]


def test_airfryer_combo_record():
    r, _, docs = _parse("NN-CD89NBRU")
    assert r.subcategory == "sco" and r.price_local == 2099.0 and r.is_new is None and r.release_date == "2023-02-27"
    assert r.weight_lb == 40.3 and docs == []


def test_basic_record():
    r, _, _ = _parse("NN-ST25LWRU")
    assert r.subcategory == "microwave" and r.finish_color == "White" and r.capacity_total_cuft == 0.74 and r.is_new is None


def test_url_check():
    assert s.url_check(GT68) == "micro-ondas-panasonic-gt68-black-glass-nn-gt68lbru"
    for bad in ("http://loja.panasonic.com.br/micro-ondas-gt68/p", "https://evil.com/micro-ondas-gt68/p",
                "https://loja.panasonic.com.br.evil.com/micro-ondas-gt68/p", BASE + "/cozinha/microondas", BASE + "/micro-ondas-gt68/p?a=b",
                "https://user@loja.panasonic.com.br/micro-ondas-gt68/p", "https://www.panasonic.com/br/consumidor/x/p"):
        try:
            s.url_check(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_state_errors():
    for html in ("<html></html>", '<template data-varname="__STATE__"><script>{}</script></template>',
                 '<template data-varname="__STATE__"><script>not json</script></template>'):
        try:
            s.product_state(html)
        except s.PanasonicBrError:
            continue
        raise AssertionError(html)


def test_discover_with_fake_fetch():
    calls = []

    def fake(url, probe):
        calls.append(url)
        return _t("listing_microondas.html") if "page=" not in url else "<html></html>"

    real, s.fetch_html = s.fetch_html, fake
    s._LISTING_CACHE.clear()
    try:
        assert [c.model_number for c in s.discover("sco")] == ["NN-CD89NBRU"]
        assert calls == [BASE + "/cozinha/microondas", BASE + "/cozinha/microondas?page=2"]  # page 2 is empty -> stop
        assert len(s.discover("microwave", limit=3)) == 3 and len(calls) == 2  # page 1 cached, stopped at the limit
    finally:
        s.fetch_html = real
        s._LISTING_CACHE.clear()


def test_scrape_with_fake_fetch_and_download():
    got = []
    real_f, real_d = s.fetch_html, s.common.download_pdf
    s.fetch_html = lambda url, probe: _t("pdp_NN-GT68LBRU.html")
    s.common.download_pdf = lambda brand, model, dtype, url: got.append((brand, model, dtype, url)) or None
    try:
        r, docs, raw = s.scrape(GT68)
    finally:
        s.fetch_html, s.common.download_pdf = real_f, real_d
    assert r.model_number == "NN-GT68LBRU" and docs == [] and len(raw) == 78
    assert got and got[0][:3] == ("Panasonic", "NN-GT68LBRU", "Manual")


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
