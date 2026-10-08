"""Plain-assert offline tests (saved fixtures of lg.com/br). Run: python tests/test_lg_br.py  (or pytest)

The i18n 'pt' translator is switched off (samsung_br._pt -> None; lg_br shares its vocabulary and Net) so the tests need
neither the glossary file nor the local LLM; PDP/Coveo responses come from tests/fixtures/lg_br, the network is a fake Net."""
import contextlib
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import lg_br as lg
import samsung_br as sb

FX = Path(__file__).parent / "fixtures" / "lg_br"
URL = "https://www.lg.com/br/micro-ondas/micro-ondas-solo/ms3043br/"


def _txt(name):
    return (FX / name).read_text(encoding="utf-8")


def _json(name):
    return json.loads(_txt(name))


@contextlib.contextmanager
def patched(obj, **attrs):
    old = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


class FakeNet:
    """Stand-in for samsung_br.Net: canned text by URL; records every request (url + kwargs)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def text(self, url, **kw):
        self.calls.append((url, kw))
        for needle, body in self.routes.items():
            if needle in url:
                return body(kw) if callable(body) else body
        raise lg.LgBrPageError(f"no route for {url}")


def _offline():
    return patched(sb, _pt=lambda: None)


TOKEN = '{"token": "aaa.bbb.ccc"}'


# ------------------------------------------------------------------ contract
def test_module_contract():
    assert (lg.COUNTRY, lg.REGION, lg.CURRENCY) == ("br", "sa", "BRL")
    assert lg.SUPPORTED_SUBCATEGORIES == {"microwave"}
    assert lg.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking"))
    assert lg.SUPPORTED_SUBCATEGORIES.isdisjoint(lg.UNSUPPORTED_SUBCATEGORIES)
    assert catalog.module_name("LG", "br") == "lg_br" and catalog.supported("LG", "br") == {"microwave"}


def test_discover_rejects_unsupported_sub_keys():
    for sub in ("sco", "otr", "gas_oven", "gas_cooktop", "electric_oven", "induction", "radiant", "french_door", "x"):
        try:
            lg.discover(sub)
        except ValueError:
            continue
        raise AssertionError(f"discover({sub!r}) must raise ValueError")


# ------------------------------------------------------------------ classify
def test_classify_table():
    c = lg.classify
    assert c("Micro-ondas LG 30 litros Prata Limpa Fácil (MS3043BR)", "/br/micro-ondas/micro-ondas-solo/ms3043br/") == "microwave"
    assert c("Forno de Micro-ondas Grill 30L com Grill de Quartzo", "/br/micro-ondas/micro-ondas-grill/mh7093br/") == "microwave"
    assert c("Micro-ondas de embutir 38L") == "microwave"
    assert c("Forno Elétrico NeoChef Convection 39L com Smart Inverter", "/br/micro-ondas/micro-ondas-solo/mj3967apra/") == "sco"
    # a built-in oven filed under /micro-ondas/ (LSWS306ST) is an electric oven
    assert c("Forno Elétrico de Embutir LG Studio 133 L", "/br/micro-ondas/micro-ondas-grill/lsws306st/") == "electric_oven"
    assert c("Forno a gás de embutir 70L") == "gas_oven"
    assert c("Cooktop Inox a gás LG Studio 5 bocas 92 cm", "/br/cooktops/lscg367st/") == "gas_cooktop"
    assert c("Fogão a gás 5 bocas com forno") == "gas_oven"
    assert c("Cooktop por indução 4 zonas") == "induction"
    assert c("Micro-ondas sobre o fogão com coifa") == "otr"
    assert c("Coifa de parede 90cm") is None and c("Depurador 60 cm") is None
    assert c("Geladeira Frost Free") is None
    assert c("Algo", category="Micro-Ondas Solo") == "microwave"


# ------------------------------------------------------------------ discover
def test_parse_search_keeps_only_active_microwaves():
    data = _json("coveo_cooking.json")
    got = lg.parse_search(data, "microwave")
    assert [c.model_number for c in got] == ["MS3043BR", "MS3091BC", "MS3043BRA", "MS3033DS", "MS3094NR", "MS3091BCA",
                                              "MS3033DSA", "MS3094NRA"]
    # the wall oven, gas cooktop and convection microwave in the fixture are DISCONTINUED -> never listed
    assert lg.parse_search(data) == got
    for c in got:
        assert (c.brand, c.country, c.region, c.currency, c.category, c.subcategory) == ("LG", "br", "sa", "BRL", "cooking", "microwave")
        assert c.price_usd is None and c.price_local and c.url == "https://www.lg.com" + f"/br/micro-ondas/micro-ondas-solo/{c.model_number.lower()}/"
    assert lg.parse_search(data, "microwave", limit=3) == got[:3]
    assert lg.parse_search(data, "gas_oven") == [] and lg.parse_search(data, "electric_oven") == []


def test_parse_search_files_other_active_types_under_their_own_sub():
    data = copy.deepcopy(_json("coveo_cooking.json"))
    for r in data["results"]:
        r["raw"]["ec_model_status_code"] = "ACTIVE"  # pretend the discontinued ones are on sale again
    subs = {c.model_number: c.subcategory for c in lg.parse_search(data)}
    assert subs["LSWS306ST"] == "electric_oven" and subs["LSCG367ST"] == "gas_cooktop"
    assert subs["MJ3967APRA"] == "sco" and subs["MH7093BR"] == "microwave"
    assert lg.parse_search(data, "microwave") != lg.parse_search(_json("coveo_cooking.json"), "microwave")


def test_listing_price_is_the_selling_price_not_the_pix_price():
    c = lg.parse_search(_json("coveo_cooking.json"), "microwave")[0]
    assert c.model_number == "MS3043BR" and c.price_local == 699.0  # ec_price; ec_cheaper_price 664.05 (PIX) and ec_msrp 1099 ignored
    raw = {"ec_price": 0.0}
    assert lg._selling_price(raw) is None and lg._selling_price({}) is None


def test_listing_attrs_signals_and_units():
    by = {c.model_number: c for c in lg.parse_search(_json("coveo_cooking.json"), "microwave")}
    a = by["MS3043BR"].attrs
    assert a["capacity_total_cuft"] == 1.06 == a["oven_capacity_cuft"]  # 30 L
    assert a["microwave_watts"] == 800 and a["width_in"] == 20.0 and a["fuel"] == "electric" and a["wifi"] is False
    assert a["rating"] == 4.0 and a["review_count"] == 13  # ec_review_score / ec_review_rating (the COUNT)
    assert a["release_date"] == "2024" and a["release_src"] == "site" and "is_new" not in a
    assert by["MS3091BC"].attrs["rating"] == 4.56 and by["MS3091BC"].attrs["review_count"] == 32
    assert by["MS3091BC"].attrs["release_date"] == "2023"
    assert all(k in c.attrs_src for c in by.values() for k in c.attrs)


def test_signals_only_with_a_positive_review_count():
    assert lg._signals({"ec_review_score": 4.5, "ec_review_rating": 10}) == {"rating": 4.5, "review_count": 10}
    assert lg._signals({"ec_review_score": 5.0, "ec_review_rating": 0}) == {}
    assert lg._signals({"ec_review_score": 9, "ec_review_rating": 3}) == {}  # not on the 0-5 scale
    assert lg._signals({"ec_model_year": "1999"}) == {"release_date": "1999", "release_src": "site"}
    assert lg._signals({"ec_model_year": "20xx"}) == {} and lg._signals({}) == {}


def test_parse_ec_specs():
    pairs = lg.parse_ec_specs("Marca: LG,;Capacidade do forno (L): 30,;Dimensão da cavidade (L x A x P) (mm): 359,0 x 241,0 x 378,2,;Código de Barras: 789,")
    assert pairs[0] == ("Marca", "LG") and pairs[1] == ("Capacidade do forno (L)", "30")
    assert pairs[2][1] == "359,0 x 241,0 x 378,2"  # decimal commas survive the ',;' separator
    assert lg.parse_ec_specs("") == [] and lg.parse_ec_specs(None) == []


def test_discover_queries_active_cooking_models_through_coveo():
    resp = json.dumps(_json("coveo_cooking.json"))
    net = FakeNet({"/ncms/latam/api/v1/coveo/token": TOKEN, "platform-eu.cloud.coveo.com": resp})
    with patched(lg, _net=lambda: net):
        got = lg.discover("microwave", limit=5)
    assert len(got) == 5 and got[0].model_number == "MS3043BR"
    token_url, _ = net.calls[0]
    assert token_url == "https://www.lg.com/ncms/latam/api/v1/coveo/token"
    url, kw = net.calls[1]
    assert url.startswith("https://platform-eu.cloud.coveo.com/rest/search/v2?organizationId=") and kw["method"] == "POST"
    assert kw["headers"] == {"Authorization": "Bearer aaa.bbb.ccc"}
    body = kw["body"]
    assert body["searchHub"] == "BR-B2C-Search" and body["locale"] == "pt-BR"
    assert "ACTIVE" in body["cq"] and "Cooking Appliance" in body["cq"] and "ec_review_score" in body["fieldsToInclude"]


def test_discover_pages_until_exhausted():
    data = _json("coveo_cooking.json")
    first = {"totalCount": 12, "results": data["results"][:6]}
    second = {"totalCount": 12, "results": data["results"][6:]}
    pages = [json.dumps(first), json.dumps(second)]
    net = FakeNet({"/ncms/latam/api/v1/coveo/token": TOKEN, "platform-eu.cloud.coveo.com": lambda kw: pages.pop(0)})
    with patched(lg, _net=lambda: net):
        got = lg.discover("microwave", limit=30)
    assert len(got) == 8
    assert [c[1]["body"]["firstResult"] for c in net.calls[1:]] == [0, 6]


def test_search_rejects_a_changed_response_and_a_missing_token():
    net = FakeNet({"platform-eu.cloud.coveo.com": '{"foo": 1}', "/token": '{"token": "nodots"}'})
    try:
        lg._search(net, "t", "cq")
    except lg.LgBrPageError:
        pass
    else:
        raise AssertionError("a response without results must raise")
    try:
        lg._token(net)
    except lg.LgBrPageError:
        pass
    else:
        raise AssertionError("a malformed token must raise")


# ------------------------------------------------------------------ PDP parsing and record
def test_parse_spec_rows_and_ld():
    html = _txt("pdp_ms3043br.html")
    rows = lg.parse_spec_rows(html)
    assert len(rows) == 73 and rows[0] == ("ESPECIFICAÇÕES BÁSICAS", "Marca", "LG")
    assert ("DIMENSÕES / PESO", "Dimensões do produto (L x A x P) (mm)", "508 x 290 x 400") in rows
    assert all(s and l and v for s, l, v in rows)
    ld = lg.parse_product_ld(html)
    assert ld["mpn"] == "MS3043BR" and ld["image"].startswith("/content/dam/channel/wcms/br/images/")
    assert lg.parse_spec_rows("<html></html>") == [] and lg.parse_product_ld("<html></html>") == {}
    # the JSON-LD price is the 5% PIX price; the record must not use it
    assert float(ld["offers"]["price"]) == 664.05


def test_build_record_uses_the_selling_price_and_english_specs():
    html = _txt("pdp_ms3043br.html")
    raw = _json("coveo_ms3043br.json")["results"][0]["raw"]
    with _offline():
        rec = lg.build_record(URL, lg.parse_product_ld(html), lg.parse_spec_rows(html), raw)
    assert (rec.brand, rec.model_number, rec.category, rec.subcategory) == ("LG", "MS3043BR", "cooking", "microwave")
    assert (rec.region, rec.country, rec.currency, rec.price_usd) == ("sa", "br", "BRL", None)
    assert rec.price_local == 699.0 and rec.product_name.startswith("Micro-ondas LG 30 litros")
    assert rec.capacity_total_cuft == 1.06 and (rec.width_in, rec.height_in, rec.depth_in) == (20.0, 11.4, 15.7)
    assert rec.weight_lb == 26.5 and rec.voltage_v == "127" and rec.frequency_hz == 60.0
    assert rec.wifi_supported is False and rec.wifi_evidence == "TECNOLOGIA INTELIGENTE > ThinQ (Wi-Fi) = Não"
    assert rec.finish_color == "Noble silver"
    assert rec.image_url == "https://www.lg.com/content/dam/channel/wcms/br/images/fornos-microondas/ms3043br_fslflgz_essp_br_c/gallery/450.jpg"
    assert (rec.rating, rec.review_count, rec.release_date, rec.release_src, rec.is_new) == (4.0, 13, "2024", "site", None)
    ex = rec.extra_specs
    assert ex["Basic specifications > Installation type"] == "Countertop" and ex["Cooking modes > Defrost"] == "Yes"
    assert ex["Smart technology > ThinQ (Wi-Fi)"] == "No" and ex["Power / ratings > Power output (W)"] == "800"
    assert ex["Oven capacity (L)"] == "30" and ex["Oven capacity (cu ft)"] == "1.06" and ex["Microwave output power (W)"] == "800"
    assert (ex["Width (mm)"], ex["Height (mm)"], ex["Depth (mm)"]) == ("508", "290", "400")  # exact mm, no inch round trip
    assert ex["List price (BRL)"] == "699" and ex["Struck-through MSRP (BRL)"] == "1099"
    assert ex["PIX/card price excluded (BRL)"] == "664.05" and "excludes" in ex["Price basis"]
    assert "Defrost" in rec.pod_features and "Child lock" in rec.pod_features and "Bake" not in rec.pod_features


def test_build_record_second_model_and_unsupported_types():
    html = _txt("pdp_ms3094nr.html")
    raw = next(r["raw"] for r in _json("coveo_cooking.json")["results"] if r["raw"]["ec_model_name"] == "MS3094NR")
    with _offline():
        rec = lg.build_record("https://www.lg.com/br/micro-ondas/micro-ondas-solo/ms3094nr/", lg.parse_product_ld(html),
                              lg.parse_spec_rows(html), raw)
    assert rec.model_number == "MS3094NR" and rec.price_local == 899.0 and rec.rating == 3.94 and rec.review_count == 16
    for name in ("Coifa de parede 90cm", "Geladeira Frost Free 400L"):
        try:
            with _offline():
                lg.build_record("https://www.lg.com/br/geladeiras/x1/", {}, lg.parse_spec_rows(html),
                                {"ec_user_friendly_name": name, "ec_model_name": "X1"})
        except lg.LgBrPageError:
            continue
        raise AssertionError(f"{name} must not become a product")


# ------------------------------------------------------------------ scrape end to end (fake network)
def test_scrape_returns_product_no_documents_and_raw_specs():
    rec_json = json.dumps(_json("coveo_ms3043br.json"))
    net = FakeNet({"/br/micro-ondas/": _txt("pdp_ms3043br.html"), "/ncms/latam/api/v1/coveo/token": TOKEN,
                   "platform-eu.cloud.coveo.com": rec_json})
    with _offline(), patched(lg, _net=lambda: net):
        product, docs, raw = lg.scrape(URL)
    assert product.model_number == "MS3043BR" and product.price_local == 699.0 and docs == []
    assert len(raw) == 73 and all(r.source == "web" and r.brand == "LG" and r.model_number == "MS3043BR" for r in raw)
    originals = {(r.section, r.key): r.value for r in raw}
    assert originals[("TECNOLOGIA INTELIGENTE", "ThinQ (Wi-Fi)")] == "Não"  # Portuguese kept verbatim
    assert originals[("ESPECIFICAÇÕES BÁSICAS", "Tipo de instalação")] == "Em bancada"
    page_call = net.calls[0]
    assert page_call[0] == URL and page_call[1]["as_page"] is True
    cq = net.calls[2][1]["body"]["cq"]
    assert cq == '@ec_model_url_path=="/br/micro-ondas/micro-ondas-solo/ms3043br/"'


def test_scrape_survives_a_missing_catalog_record_but_not_a_missing_table():
    empty = json.dumps({"totalCount": 0, "results": []})
    net = FakeNet({"/br/micro-ondas/": _txt("pdp_ms3043br.html"), "/token": TOKEN, "platform-eu.cloud.coveo.com": empty})
    with _offline(), patched(lg, _net=lambda: net):
        product, docs, raw = lg.scrape(URL)
    assert product.model_number == "MS3043BR" and product.price_local is None and product.rating is None and raw
    nopage = FakeNet({"/br/micro-ondas/": "<html><body>x</body></html>"})
    with _offline(), patched(lg, _net=lambda: nopage):
        try:
            lg.scrape(URL)
        except lg.LgBrPageError:
            pass
        else:
            raise AssertionError("a page without a spec table must raise")


def test_lookup_falls_back_to_the_model_name_and_rejects_odd_paths():
    raw = _json("coveo_ms3043br.json")
    none = {"totalCount": 0, "results": []}
    answers = [json.dumps(none), json.dumps(raw)]
    net = FakeNet({"platform-eu.cloud.coveo.com": lambda kw: answers.pop(0)})
    got = lg.lookup(net, "t", URL)
    assert got["ec_model_name"] == "MS3043BR"
    assert net.calls[1][1]["body"]["cq"] == '@ec_model_name=="MS3043BR"'
    for bad in ("https://www.lg.com/br/a/b/x%22%20OR%20true/", "https://www.lg.com/br/", "https://www.lg.com/br/micro-ondas/x"):
        try:
            lg.lookup(net, "t", bad)
        except lg.LgBrPageError:
            continue
        raise AssertionError(f"{bad} must not reach the query")


# ------------------------------------------------------------------ security
def test_url_guards():
    assert lg.is_br_page(URL) and lg.is_br_page("https://lg.com/br/")
    for bad in ("http://www.lg.com/br/x/", "https://www.lg.com/us/x/", "https://www.lg.com/mx/x/", "https://evil.com/br/x/",
                "https://lg.com.evil.com/br/x/", "https://u@www.lg.com/br/x/", "https://evil.com/?https://www.lg.com/br/"):
        assert not lg.is_br_page(bad), bad
        try:
            lg._require_br(bad)
        except lg.LgBrPageError:
            pass
        else:
            raise AssertionError(f"scrape must refuse {bad}")
    try:
        lg.scrape("https://www.lg.com/us/microwaves/x/")
    except lg.LgBrPageError:
        pass
    else:
        raise AssertionError("scrape must refuse other countries")
    assert lg._allowed("https://www.lg.com/ncms/latam/api/v1/coveo/token")
    assert lg._allowed("https://platform-eu.cloud.coveo.com/rest/search/v2?organizationId=x")
    assert not lg._allowed("https://platform-eu.cloud.coveo.com/other") and not lg._allowed("https://evil.com/rest/search/v2")
    assert not lg._allowed("http://platform-eu.cloud.coveo.com/rest/search/v2") and not lg._allowed("https://www.lg.com/us/x/")
    assert lg._safe_model("../../etc/pass wd") == "_.._etc_pass_wd" and "/" not in lg._safe_model("a/b\\c")


def test_image_url_stays_on_lg_hosts():
    assert lg._image_url({"image": "/content/dam/x.jpg"}, {}) == "https://www.lg.com/content/dam/x.jpg"
    assert lg._image_url({"image": "https://evil.com/x.jpg"}, {}) is None
    assert lg._image_url({}, {"ec_large_image_addr": "/br/images/a/450.jpg"}) == "https://www.lg.com/content/dam/channel/wcms/br/images/a/450.jpg"
    assert lg._image_url({}, {}) is None


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
