"""Plain-assert offline tests (saved fixtures of samsung.com/br). Run: python tests/test_samsung_br.py  (or pytest)

The i18n 'pt' translator is switched off (samsung_br._pt -> None) so the tests need neither the glossary file nor the local
LLM; PDP/finder/card responses come from tests/fixtures/samsung_br, the network is replaced by a fake Net."""
import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import samsung_br as sb
from schema import DocumentRecord

FX = Path(__file__).parent / "fixtures" / "samsung_br"
PDP_URL = "https://www.samsung.com/br/cooking-appliances/ranges/nsg90h-30-nsg90h60srax-nsg90h60sraz/"
OVEN_URL = ("https://www.samsung.com/br/cooking-appliances/ovens/"
            "nv7000b-4series-dual-cook-simple-steam-4series-dual-cook-simple-steam-nv7b4420xak-bz/")


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
    """Stand-in for sb.Net: canned text by URL; records every request."""

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
                return body
        raise sb.SamsungBrPageError(f"no route for {url}")


def _offline():
    return patched(sb, _pt=lambda: None)


# ------------------------------------------------------------------ contract
def test_module_contract():
    assert (sb.COUNTRY, sb.REGION, sb.CURRENCY) == ("br", "sa", "BRL")
    assert sb.SUPPORTED_SUBCATEGORIES == {"gas_oven", "gas_cooktop", "electric_oven"}
    assert sb.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking"))
    assert sb.SUPPORTED_SUBCATEGORIES.isdisjoint(sb.UNSUPPORTED_SUBCATEGORIES)
    assert catalog.module_name("Samsung", "br") == "samsung_br"
    assert catalog.supported("Samsung", "br") == sb.SUPPORTED_SUBCATEGORIES
    assert catalog.COUNTRIES["br"]["region"] == sb.REGION and catalog.COUNTRIES["br"]["currency"] == sb.CURRENCY


def test_discover_rejects_unsupported_sub_keys():
    for sub in ("microwave", "sco", "otr", "induction", "radiant", "french_door", "nonsense"):
        try:
            sb.discover(sub)
        except ValueError:
            continue
        raise AssertionError(f"discover({sub!r}) must raise ValueError")


# ------------------------------------------------------------------ classify
def test_classify_table():
    c = sb.classify
    assert c("Fogão de Piso a Gás Série 90 | 5 bocas | Design Inox", "NSG90H60SRAZ") == "gas_oven"
    assert c("Fogão Série 7 5 Bocas Inox Câmera e Air Fry", "NSG6DG8700SRAZ") == "gas_oven"
    assert c("Fogão a gás 4 bocas com forno") == "gas_oven"
    assert c("Cooktop à Gás 5 Queimadores Inox Wi-Fi e Dupla Chama", "NA30N6555TS/AZ") == "gas_cooktop"
    assert c("Fogão de embutir a gás 5 bocas") == "gas_cooktop"
    assert c("Cooktop por indução 4 zonas") == "induction"
    assert c("Cooktop elétrico vitrocerâmico") == "radiant"
    assert c("Forno Elétrico Função Air Fry e Wi-Fi 76L", "NV7B4420XAK/BZ") == "electric_oven"
    assert c("Forno a gás de embutir 70L") == "gas_oven"
    assert c("Forno Elétrico de bancada 40L") is None  # toaster-type oven
    assert c("Micro-ondas 30L") == "microwave"
    assert c("Micro-ondas de embutir 38L") == "microwave"
    assert c("Forno elétrico com micro-ondas combinado") == "sco"
    assert c("Micro-ondas sobre o fogão com coifa") == "otr"
    assert c("Coifa Inox com Wi-Fi e Power Ventilation de 90cm", "F-NK36C-NK-AR7") is None
    assert c("Depurador de ar 60cm") is None
    # bundles / kits are never products
    assert c("Combo Cooktop Black Inox + Coifa + Forno", "F-NA3NK36NV7B4") is None
    assert c("Fogão Série 80 com coifa", "NSG80NK36NOX", "kit-stove-series-80-semi-professional-with-range-hood") is None
    # the English pvi subtype disambiguates names that carry no keyword
    assert c("NA30N6555", pvi="Cooktop") == "gas_cooktop" and c("Série 3", pvi="Gas Range") == "gas_oven"


def test_dims_in():
    assert sb.dims_in("595 x 596 x 570 mm") == (23.4, 23.5, 22.4)
    assert sb.dims_in("761 x (914.4 ~ 933.5) x 721.1 mm") == (30.0, 36.0, 28.4)  # a range keeps its first number
    assert sb.dims_in("(29 15/16) x (36 ~ 36 3/4) x (27 7/8) polegadas") == (29.9, 36.0, 27.9)
    assert sb.dims_in("90 x 10 x 50 cm")[0] == 35.4
    assert sb.dims_in("W 723.9 x D 498.5 mm") == (None, None, None)  # two numbers: not W x H x D
    assert sb.dims_in("") == (None, None, None)


def test_price_parsing_helpers():
    assert sb.pt_number("R$ 1.299,00") == 1299.0 and sb.pt_number("R$10.999,00") == 10999.0
    assert sb.pt_number("12,7 kW") == 12.7 and sb.pt_number("3.300 W") == 3300.0 and sb.pt_number("37,5 kg") == 37.5
    assert sb.pt_number("sem número") is None and sb.pt_number(None) is None
    # stored price = the a-vista cash price (user decision 2026-10-09); regular = the 18x installment price
    m = {"price": "10999", "afterTaxPrice": "11577.89", "rrpPriceDisplay": "R$11.577,89"}
    p = sb._price_from_model(m)
    assert p == {"regular": 11577.89, "cash": 10999.0, "original": 11577.89}
    m2 = {"price": "12999", "afterTaxPrice": "13683.16", "rrpPriceDisplay": "R$14.999,00"}
    assert sb._price_from_model(m2)["regular"] == 13683.16 and sb._price_from_model(m2)["original"] == 14999.0
    assert sb._price_from_model({"price": "4749"})["regular"] == 4749.0  # no installment figure: the one price
    assert sb._price_from_model({})["regular"] is None
    assert sb.stored_price(p) == 10999.0 and sb.stored_price(sb._price_from_model({"price": "4749"})) == 4749.0
    assert sb.stored_price(sb._price_from_model({})) is None


# ------------------------------------------------------------------ discover
def test_parse_finder_by_sub():
    payload = _json("finder_cooking.json")
    ranges = sb.parse_finder(payload, "gas_oven")
    assert [c.model_number for c in ranges] == ["NSG90H60SRAZ", "NSG80H60SRAZ", "NSG6DG8700SRAZ", "NSG6DG8300SRAZ"]
    cooktops = sb.parse_finder(payload, "gas_cooktop")
    assert [c.model_number for c in cooktops] == ["NA30N6555TSAZ", "NA30N6555TGAZ"]
    ovens = sb.parse_finder(payload, "electric_oven")
    assert [c.model_number for c in ovens] == ["NV7B4420XAKBZ", "NV7B4545SAKBZ"]
    everything = sb.parse_finder(payload)
    assert len(everything) == 8  # the finder also lists 4 hoods and 1 cooktop+hood+oven kit: all dropped
    names = " ".join(c.name for c in everything).lower()
    assert "coifa" not in names and "combo" not in names  # hoods and the cooktop+hood+oven kit are dropped
    assert sb.parse_finder(payload, "gas_oven", limit=2) == ranges[:2]
    for c in everything:
        assert (c.brand, c.country, c.region, c.currency, c.category) == ("Samsung", "br", "sa", "BRL", "cooking")
        assert c.price_usd is None and c.url.startswith("https://www.samsung.com/br/cooking-appliances/")
        assert c.subcategory in sb.SUPPORTED_SUBCATEGORIES and "/" not in c.model_number


def test_candidate_prices_attrs_and_signals():
    by_model = {c.model_number: c for c in sb.parse_finder(_json("finder_cooking.json"))}
    top = by_model["NSG90H60SRAZ"]
    assert top.price_local == 10999.0  # a-vista cash price, not the 11.577,89 18x price
    assert top.attrs["fuel"] == "gas" and top.attrs["burners"] == 5 and top.attrs["wifi"] is True
    assert top.attrs["air_fry"] is True and "rating" not in top.attrs and "review_count" not in top.attrs  # none published
    assert "is_new" not in top.attrs and "release_date" not in top.attrs
    oven = by_model["NV7B4420XAKBZ"]
    assert oven.price_local == 4749.0 and oven.attrs["rating"] == 3.95 and oven.attrs["review_count"] == 85
    assert oven.attrs["capacity_total_cuft"] == 2.68 == oven.attrs["oven_capacity_cuft"]  # 76 L
    assert oven.attrs["width_in"] == 23.4 and oven.attrs["fuel"] == "electric"
    cooktop = by_model["NA30N6555TSAZ"]
    assert cooktop.attrs["width_in"] == 30.0 and cooktop.attrs["burners"] == 5 and "oven_capacity_cuft" not in cooktop.attrs
    assert by_model["NV7B4545SAKBZ"].price_local is None  # no price on the site -> unknown
    assert all(0 < c.attrs["rating"] <= 5 for c in by_model.values() if "rating" in c.attrs)
    assert all(k in c.attrs_src for c in by_model.values() for k in c.attrs)


def test_signals_need_a_positive_review_count():
    assert sb._signals({"ratings": "4.5", "reviewCount": "10"}) == {"rating": 4.5, "review_count": 10}
    assert sb._signals({"ratings": "4.5", "reviewCount": "0"}) == {}
    assert sb._signals({"ratings": None, "reviewCount": None}) == {}
    assert sb._signals({"ratings": "7", "reviewCount": "3"}) == {}  # out of the 0-5 scale: not trusted


def test_discover_uses_the_finder_api_and_limit():
    net = FakeNet({"product/finder/global": json.dumps(_json("finder_cooking.json"))})
    with patched(sb, _net=lambda: net):
        got = sb.discover("gas_oven", limit=3)
    assert [c.model_number for c in got] == ["NSG90H60SRAZ", "NSG80H60SRAZ", "NSG6DG8700SRAZ"]
    url = net.calls[0][0]
    assert url.startswith("https://searchapi.samsung.com/v6/front/b2c/product/finder/global?") and "siteCode=br" in url
    assert "type=08080000" in url and "start=1" in url
    assert len(net.calls) == 1  # the whole catalog fits one page


def test_discover_follows_pagination():
    payload = _json("finder_cooking.json")
    fams = payload["response"]["resultData"]["productList"]
    page1 = json.loads(json.dumps(payload))
    page1["response"]["resultData"]["productList"] = fams[:2]
    page1["response"]["resultData"]["common"].update(totalRecord="10", fromRecord="1", toRecord="2")
    page2 = json.loads(json.dumps(payload))
    page2["response"]["resultData"]["productList"] = fams[2:]
    page2["response"]["resultData"]["common"].update(totalRecord="10", fromRecord="3", toRecord="10")
    pages = [json.dumps(page1), json.dumps(page2)]

    class Paged(FakeNet):
        def text(self, url, **kw):
            self.calls.append((url, kw))
            return pages[len(self.calls) - 1]
    net = Paged({})
    with patched(sb, _net=lambda: net):
        got = sb.discover("gas_oven", limit=30)
    assert len(got) == 4 and "start=1" in net.calls[0][0] and "start=3" in net.calls[1][0]


def test_parse_finder_rejects_a_changed_payload():
    for bad in ({}, {"response": {}}, {"response": {"resultData": {}}}):
        try:
            sb.parse_finder(bad)
        except sb.SamsungBrPageError:
            continue
        raise AssertionError("a payload without productList must raise")


# ------------------------------------------------------------------ PDP parsing
def test_parse_spec_rows_keep_every_row_in_portuguese():
    rows = sb.parse_spec_rows(_txt("pdp_range_nsg90h.html"))
    assert len(rows) == 69
    assert ("Capacidade", "Capacidade do forno", "170 L") in rows
    assert ("Cooktop", "Queimador 1", "Direita / Frente - 4.1 kW") in rows
    assert rows[0] == ("Tipo", "Tipo de instalação", "Deslize para dentro")
    assert all(s and l and v for s, l, v in rows)
    assert sb.parse_spec_rows("<html>no table</html>") == []


def test_parse_product_ld_and_manuals():
    html = _txt("pdp_range_nsg90h.html")
    ld = sb.parse_product_ld(html)
    assert ld["sku"] == "NSG90H60SRAZ" and ld["image"].startswith("https://images.samsung.com/")
    assert sb.parse_product_ld("<html></html>") == {}
    manuals = sb.parse_manuals(html)
    assert [m["lang"][:7] for m in manuals] == ["portugu", "ingles"]  # Portuguese first
    assert all(m["url"].startswith("https://org.downloadcenter.samsung.com/") for m in manuals)
    evil = '<a href="https://evil.com/ContentsFile.aspx?x=1" data-category="manual" data-accept-lang="X">'
    assert sb.parse_manuals(evil) == []  # off-site links are dropped


# ------------------------------------------------------------------ record building (offline translation)
def _build(url, pdp, card=None):
    html = _txt(pdp)
    ld, rows = sb.parse_product_ld(html), sb.parse_spec_rows(html)
    model = sb._card_model(card, ld["sku"]) if card else {}
    return sb.build_record(url, ld, rows, model, sb.parse_manuals(html))


def test_build_record_gas_range():
    with _offline():
        rec, docs = _build(PDP_URL, "pdp_range_nsg90h.html", _json("card_nsg90h.json"))
    assert (rec.brand, rec.model_number, rec.category, rec.subcategory) == ("Samsung", "NSG90H60SRAZ", "cooking", "gas_oven")
    assert (rec.region, rec.country, rec.currency, rec.price_usd) == ("sa", "br", "BRL", None)
    assert rec.price_local == 10999.0
    assert rec.capacity_total_cuft == 6.0  # 170 L
    assert (rec.width_in, rec.height_in, rec.depth_in) == (30.0, 36.0, 28.4)
    assert rec.weight_lb == 197.1 and rec.voltage_v == "127" and rec.frequency_hz == 60.0
    assert rec.wifi_supported is True and rec.wifi_evidence == "Recursos > Conexão Wi-Fi = Sim"
    assert rec.finish_color == "Stainless steel" and rec.image_url.startswith("https://images.samsung.com/")
    assert rec.rating is None and rec.is_new is None and rec.release_date is None
    ex = rec.extra_specs
    assert ex["Capacity > Oven capacity"] == "170 L" and ex["Features > Air fry"] == "Yes"
    assert ex["Features > Gas type"] == "LPG" and ex["Cooktop > Burner 1"] == "Right / Front - 4.1 kW"
    assert ex["Oven capacity (L)"] == "170" and ex["Oven capacity (cu ft)"] == "6"
    assert ex["Burners/elements"] == "5" and ex["Fuel"] == "gas" and ex["Weight (kg)"] == "89.4"
    assert ex["Width (mm)"] == "761" and ex["Depth (mm)"] == "721"
    assert ex["Cash price (BRL)"] == "10999" and ex["Installment price excluded (BRL)"] == "11577.89"
    assert "cash price" in ex["Price basis"] and ex["Accessories > Air fry basket"] == "Yes (1)"
    assert "Air fry" in rec.pod_features and "Built-in Wi-Fi" in rec.pod_features
    assert [t for t, _ in docs] == ["Manual", "Manual EN"] and "BPT" in docs[0][1]


def test_build_record_oven_and_cooktop():
    with _offline():
        oven, _ = _build(OVEN_URL, "pdp_oven_nv7b4420.html")
        cook, _ = _build("https://www.samsung.com/br/cooking-appliances/ovens/na9300k-gas-cooktop-with-19k-btu-dual-burner-na30n6555ts-az/",
                         "pdp_cooktop_na30.html")
        series3, _ = _build("https://www.samsung.com/br/cooking-appliances/ovens/nx9100d--range-nsg6dg8300sraz/",
                            "pdp_range_nsg6dg8300.html")
    assert oven.subcategory == "electric_oven" and oven.model_number == "NV7B4420XAKBZ"
    assert oven.capacity_total_cuft == 2.68 and oven.price_local is None  # no card data given
    assert (oven.width_in, oven.depth_in) == (23.4, 22.4) and oven.voltage_v == "220"
    assert oven.extra_specs["Features > Wi-Fi connection"] == "Yes" and oven.extra_specs["Fuel"] == "electric"
    assert cook.subcategory == "gas_cooktop" and cook.capacity_total_cuft is None
    assert cook.extra_specs["Burners/elements"] == "5" and cook.extra_specs["Fuel"] == "gas"
    assert cook.extra_specs["Cooktop > Fuel type"] == "Natural gas" and cook.width_in == 30.0
    assert "Oven capacity (L)" not in cook.extra_specs
    # series 3 states the size in mm (decimal commas) AND in inches; the mm row wins
    assert series3.subcategory == "gas_oven" and (series3.width_in, series3.height_in, series3.depth_in) == (30.0, 36.0, 27.9)
    assert sb._dimensions([("Pesos/dimensões", "Líquido (L x A x P, polegada)", "(29 15/16) x (36 ~ 36 3/4) x (27 7/8) polegadas")])         == (29.9, 36.0, 27.9)  # inches-only pages are read too


def test_build_record_rejects_bundles_and_hoods():
    html = _txt("pdp_range_nsg90h.html")
    ld, rows = sb.parse_product_ld(html), sb.parse_spec_rows(html)
    for name, sku in (("Coifa Inox com Wi-Fi 90cm", "NK36CB600W"), ("Combo Cooktop + Coifa + Forno", "F-NA3NK36NV7B4")):
        try:
            with _offline():
                sb.build_record(PDP_URL, {**ld, "name": name, "sku": sku}, rows, {}, [])
        except sb.SamsungBrPageError:
            continue
        raise AssertionError(f"{name} must not become a product")


def test_translation_is_deterministic_and_never_invents():
    with _offline():
        assert sb.translate_many(["Sim", "Não", "Sim (1)", "Inox"], "value") == ["Yes", "No", "Yes (1)", "Stainless steel"]
        assert sb.translate_many(["Direita / Frente - 4,1 kW", "Esquerda / Traseira - 2,1 kW"], "value") == \
            ["Right / Front - 4,1 kW", "Left / Rear - 2,1 kW"]
        assert sb.translate_many(["Queimador 3", "Capacidade do forno", "Voltagem"], "label") == \
            ["Burner 3", "Oven capacity", "Voltage"]
        # unknown text comes back unchanged (cleaned), not a guess
        assert sb.translate_many(["Palavra desconhecida xyz"], "value") == ["Palavra desconhecida xyz"]
        assert sb.translate_many(["12,7 kW", ""], "value") == ["12,7 kW", ""]
    calls = []

    class Fake:
        def translate_many(self, texts, kind):
            calls.append((kind, list(texts)))
            return [f"EN({t})" for t in texts]
    with patched(sb, _pt=lambda: Fake()):
        out = sb.translate_many(["Sim", "Palavra desconhecida xyz"], "value")
    assert out == ["Yes", "EN(Palavra desconhecida xyz)"] and calls == [("value", ["Palavra desconhecida xyz"])]
    with patched(sb, _pt=lambda: SimpleNamespace(translate_many=lambda *a: (_ for _ in ()).throw(RuntimeError("llm down")))):
        assert sb.translate_many(["xyz desconhecido"], "value") == ["xyz desconhecido"]  # translator failure -> original


# ------------------------------------------------------------------ scrape end to end (fake network)
def _scrape_net(pdp="pdp_range_nsg90h.html", card="card_nsg90h.json"):
    return FakeNet({"/br/cooking-appliances/": _txt(pdp), "product/card/detail/global": json.dumps(_json(card))})


def test_scrape_returns_product_documents_and_raw_specs():
    net = _scrape_net()
    pdfs = []

    def fake_download(brand, model, doc_type, url):
        pdfs.append((brand, model, doc_type, url))
        return DocumentRecord(brand=brand, model_number=model, doc_type=doc_type, source_url=url,
                              local_path=f"downloads/samsung/{model}_{doc_type}.pdf", sha256="0" * 64, size_bytes=10)
    with _offline(), patched(sb, _net=lambda: net, download_pdf=fake_download, MIN_DELAY_S=0.0):
        product, docs, raw = sb.scrape(PDP_URL)
    assert product.model_number == "NSG90H60SRAZ" and product.price_local == 10999.0
    assert [d.doc_type for d in docs] == ["Manual", "Manual EN"] and pdfs[0][:2] == ("Samsung", "NSG90H60SRAZ")
    assert len(raw) == 69 and all(r.source == "web" and r.model_number == "NSG90H60SRAZ" for r in raw)
    originals = {(r.section, r.key): r.value for r in raw}
    assert originals[("Cooktop", "Número de queimadores")] == "5" and originals[("Recursos", "Conexão Wi-Fi")] == "Sim"
    assert "NSG90H60SRAZ" in net.calls[1][0] and net.calls[0][1].get("as_page") is True
    assert all("Sim" not in v for v in product.extra_specs.values() if v in ("Sim", "Não"))


def test_scrape_survives_a_missing_price_block_but_not_a_missing_page():
    class NoCard(FakeNet):
        def text(self, url, **kw):
            if "card/detail" in url:
                raise sb.SamsungBrPageError("blocked")
            return super().text(url, **kw)
    net = NoCard({"/br/cooking-appliances/": _txt("pdp_range_nsg90h.html")})
    with _offline(), patched(sb, _net=lambda: net, download_pdf=lambda *a: None, MIN_DELAY_S=0.0):
        product, docs, raw = sb.scrape(PDP_URL)
    assert product.price_local is None and docs == [] and raw  # the product still comes back, price unknown
    empty = FakeNet({"/br/cooking-appliances/": "<html><body>nothing here</body></html>"})
    with _offline(), patched(sb, _net=lambda: empty):
        try:
            sb.scrape(PDP_URL)
        except sb.SamsungBrPageError:
            pass
        else:
            raise AssertionError("a page without sku / spec table must raise")


# ------------------------------------------------------------------ security
def test_url_guards():
    ok = "https://www.samsung.com/br/cooking-appliances/ranges/x/"
    assert sb.is_br_page(ok) and sb.is_br_page("https://samsung.com/br/")
    for bad in ("http://www.samsung.com/br/x/", "https://www.samsung.com/us/x/", "https://www.samsung.com/sec/x/",
                "https://evil.com/br/x/", "https://samsung.com.evil.com/br/x/", "https://user@www.samsung.com/br/x/",
                "https://evil.com/?https://www.samsung.com/br/", "ftp://www.samsung.com/br/"):
        assert not sb.is_br_page(bad), bad
        try:
            sb._require_br(bad)
        except sb.SamsungBrPageError:
            pass
        else:
            raise AssertionError(f"scrape must refuse {bad}")
    try:
        sb.scrape("https://www.samsung.com/us/cooking/ranges/x/")
    except sb.SamsungBrPageError:
        pass
    else:
        raise AssertionError("scrape must refuse other countries")
    assert sb._is_search_api("https://searchapi.samsung.com/v6/front/b2c/product/finder/global?siteCode=br")
    assert not sb._is_search_api("https://searchapi.samsung.com/other") and not sb._is_search_api("http://searchapi.samsung.com/v6/front/b2c/product/x")
    assert sb._abs("//images.samsung.com/is/image/a") == "https://images.samsung.com/is/image/a"
    assert sb._abs("//evil.com/a.png") is None and sb._abs("") is None and sb._abs(None) is None
    assert sb._safe_model("../../etc/pass wd") == "_.._etc_pass_wd" and "/" not in sb._safe_model("a/b\\c")
    assert sb._model("NV7B4420XAK/BZ") == "NV7B4420XAKBZ"


def test_net_stays_on_site_and_declines_cookies_first():
    class Resp:
        def __init__(self, status, text="", loc=None):
            self.status_code, self.text, self.headers, self.encoding = status, text, ({"Location": loc} if loc else {}), None

    seq = [Resp(302, loc="https://www.example.org/br/"), Resp(200, "ok")]
    net = sb.Net(sb.BASE + sb.SITE, sb._allowed)
    with patched(sb.requests, request=lambda *a, **k: seq.pop(0)), patched(sb, MIN_DELAY_S=0.0):
        try:
            net.text("https://www.samsung.com/br/cooking-appliances/")
        except sb.SamsungBrPageError as e:
            assert "outside" in str(e)
        else:
            raise AssertionError("a redirect off the site must raise")
    seq = [Resp(200, "<html>fine</html>")]
    with patched(sb.requests, request=lambda *a, **k: seq.pop(0)), patched(sb, MIN_DELAY_S=0.0):
        assert net.text("https://www.samsung.com/br/cooking-appliances/") == "<html>fine</html>"
    try:
        net.text("http://www.samsung.com/br/x/")
    except sb.SamsungBrPageError:
        pass
    else:
        raise AssertionError("plain http must be refused")
    clicked = []

    class Loc:
        def __init__(self, sel):
            self.sel = sel
            self.first = self

        def count(self):
            return 1 if any(w in self.sel for w in ("Rejeitar", "Aceitar")) else 0

        def click(self, timeout=None):
            clicked.append(self.sel)
    net._page = SimpleNamespace(locator=lambda sel: Loc(sel))
    assert net._consent(False) is True and "Rejeitar" in clicked[0] and not any("Aceitar" in c for c in clicked)
    clicked.clear()
    assert net._consent(True) is True and "Aceitar" in clicked[0]


def test_net_visible_mode_skips_plain_requests_and_closes_the_browser():
    calls = []
    net = sb.Net(sb.BASE + sb.SITE, sb._allowed)
    closed = []
    net._open = lambda headless: (calls.append(("open", headless)), setattr(net, "_page", object()))
    net._in_browser = lambda *a: (200, "<html>from browser</html>")
    net.close = lambda: closed.append(True)
    with patched(sb.requests, request=lambda *a, **k: calls.append("requests")), patched(
            sb, MIN_DELAY_S=0.0), patched(sb.os, environ={"FRIDGE_BROWSER_MODE": "visible"}):
        assert net.text("https://www.samsung.com/br/x/") == "<html>from browser</html>"
    assert calls == [("open", False)]  # visible window only, requests never used
    with net:
        pass
    assert closed  # __exit__ closes the browser


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
