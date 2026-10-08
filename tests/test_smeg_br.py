"""Plain-assert offline tests for smeg_br (trimmed fixtures of smegbrasil.com.br). Run: python tests\\test_smeg_br.py"""
import os
import sys
from pathlib import Path

os.environ["FRIDGE_I18N_LLM"] = "0"  # glossary + rules only: deterministic, no local LLM
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import smeg_br as s

FX = Path(__file__).parent / "fixtures" / "smeg_br"
BASE = "https://www.smegbrasil.com.br/"


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


def _parse(model):
    return s.parse_product(BASE + "produto-" + model.lower(), _t(f"pdp_{model}.html"))


def test_constants_and_supported():
    assert (s.COUNTRY, s.REGION, s.CURRENCY, s.BRAND) == ("br", "sa", "BRL", "Smeg")
    assert s.SUPPORTED_SUBCATEGORIES == {"microwave", "sco", "electric_oven", "gas_oven", "gas_cooktop", "induction"}
    assert s.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking"))
    for sub in ("french_door", "front_load", "otr", "radiant"):
        try:
            s.discover(sub)
        except ValueError:
            continue
        raise AssertionError(sub)


def test_classify():
    c = s.classify
    assert c("Cocção,Cooktop,Cooktop Gás", "Cooktop Smeg a Gás, Aço Inox, Linha Victoria, 70/75 cm - SR975XGH") == "gas_cooktop"
    assert c("Dominó Gás", "Cooktop Dominó a Gás, inox, Linha Clássica, 30 cm, 220V - PGF31G") == "gas_cooktop"
    assert c("Cocção,Dominó Elétrico", "Cooktop Smeg Dominó Elétrico Fritadeira, Inox") is None
    assert c("Cocção,Cooktop,Cooktop Elétrico", "Cooktop Smeg Semiprofissional Elétrico Indução, Linha Universal, 60 cm") == "induction"
    assert c("Cocção,Cooktop,Cooktop Elétrico", "Cooktop Elétrico Vitrocerâmico 4 zonas") == "radiant"
    assert c("Cocção,Fogão de Piso", "Fogão de Piso Smeg com Placa a Gás, Creme, Linha Victoria, 90cm") == "gas_oven"
    assert c("Cocção,Fogão de Piso", "Fogão de Piso Smeg elétrico com Placa a gás, Blu Mediterraneo, 90cm") == "gas_oven"
    assert c("Cocção,Fogão de Piso", "Fogão de Piso Smeg com Placa de Indução, 90cm") == "induction"
    assert c("Cocção,Forno Elétrico", "Forno Elétrico Smeg Galileo Pirolítico a Vapor, Aço Inox, 68L") == "electric_oven"
    assert c("Linhas,Linha Clássica", "Forno Elétrico Smeg Termo ventilado, Clássica, Inox, 90cm") == "electric_oven"
    assert c("Cocção,Forno Elétrico", "Forno de Bancada Combinado à Vapor e Airfryer Smeg, Linha Anni 50") is None  # benchtop
    assert c("Microondas", "Forno Microondas Combinado de Embutir Smeg, Linha Linea, Nero, 45 cm") == "sco"
    assert c("Microondas", "Forno Microondas e Grill de Embutir Smeg, Linha Cortina, 60cm") == "microwave"
    assert c("Cocção,Coifa,Coifa de Parede", "Coifa de Parede Smeg Preto, Linha Victoria, 110 cm") is None
    assert c("Cocção,Gaveta Aquecida", "Gaveta de aquecimento Smeg, Linha Linea") is None
    assert c("Linhas,Acessórios", "Acessório para Fornos e Fogões Smeg, Pedra para Pizza - STONE2") is None


def test_listing_candidates():
    items = s.parse_listing(_t("listing_coccao_p1.html"))
    assert len(items) == 21 and items[0]["codigo"] == "PV695LCNR"
    by = {sub: [c for i in items if (c := s.item_to_candidate(i, sub))] for sub in sorted(s.SUPPORTED_SUBCATEGORIES)}
    assert [c.model_number for c in by["gas_cooktop"]] == ["PV695LCNR", "PGF96", "SR975XGH", "PV331CN", "PGF31G"]
    assert [c.model_number for c in by["induction"]] == ["SI4642D", "SI1M4954D"]
    assert [c.model_number for c in by["electric_oven"]] == ["SOP6602TNR", "SFP750AOPZ", "SF9390X1"]  # COF01PGBR benchtop skipped
    assert [c.model_number for c in by["sco"]] == ["SO4604M2PNR", "SO4902M1X"]
    assert [c.model_number for c in by["microwave"]] == ["MP722AO"]
    assert [c.model_number for c in by["gas_oven"]] == ["TR90GMP", "TR90DGME9", "A1-9"]
    c = by["gas_cooktop"][0]
    assert (c.brand, c.category, c.subcategory, c.country, c.region, c.currency) == ("Smeg", "cooking", "gas_cooktop", "br", "sa", "BRL")
    assert c.price_usd is None and c.price_local == 22990.0  # `valor`; valor_pix (21840.5) is a payment-method price
    assert c.url.startswith(BASE) and c.url.endswith("pv695lcnr") and c.name.startswith("Cooktop Smeg Semiprofissional a Gás")
    assert c.attrs == {"width_in": 35.4, "finish": "black", "fuel": "gas"} and c.attrs_src["width_in"] == "name"
    assert by["gas_cooktop"][2].attrs["width_in"] == 29.5  # '70/75 cm' -> the larger size
    oven = by["electric_oven"][0]
    assert oven.attrs["capacity_total_cuft"] == 2.4 and "rating" not in oven.attrs and "is_new" not in oven.attrs  # 68 L
    assert "width_in" not in by["sco"][0].attrs  # '45 cm' on a compact oven is its height
    assert by["gas_oven"][1].attrs.get("fuel") is None  # 'elétrico com Placa a gás' = dual fuel, not guessed
    assert by["gas_cooktop"][1].price_local == 15490.0  # valor 15490 with valor_de 16490 struck through: the current price


def test_candidate_filters_and_signals():
    ok = {"codigo": "PV695LCNR", "nome": "Cooktop Smeg a Gás, Preto, 90 cm", "categoria": "Cocção,Cooktop,Cooktop Gás",
          "link": "/cooktop-smeg-a-gas-pv695lcnr", "valor": "22990.00", "nota": "0.00", "avaliacoes": 0}
    c = s.item_to_candidate(ok, "gas_cooktop")
    assert c and "rating" not in c.attrs
    rated = s.item_to_candidate({**ok, "nota": "4.50", "avaliacoes": 6}, "gas_cooktop")  # synthetic: nobody has reviews today
    assert rated.attrs["rating"] == 4.5 and rated.attrs["review_count"] == 6
    assert s.item_to_candidate({**ok, "nota": "4.50", "avaliacoes": 0}, "gas_cooktop").attrs.get("rating") is None
    assert s.item_to_candidate({**ok, "ocultar_preco_site": True}, "gas_cooktop").price_local is None
    assert s.item_to_candidate({**ok, "valor": "0.00"}, "gas_cooktop").price_local is None
    for bad in ({**ok, "link": "//evil.com/x-pv695lcnr"}, {**ok, "link": "/coccao"}, {**ok, "link": "https://evil.com/a-b-c-d-e"},
                {**ok, "codigo": "../x"}, {**ok, "categoria": "Cocção,Coifa"}):
        assert s.item_to_candidate(bad, "gas_cooktop") is None, bad
    assert s.item_to_candidate(ok, "induction") is None


def test_induction_hob_record():
    r, raw, docs = _parse("SI4642D")
    assert (r.brand, r.category, r.subcategory, r.country, r.region, r.currency) == ("Smeg", "cooking", "induction", "br", "sa", "BRL")
    assert r.price_local == 12990.0 and r.price_usd is None and r.finish_color == "Black"
    assert (r.height_in, r.width_in, r.depth_in) == (2.2, 23.6, 20.1)  # 56 x 600 x 510 mm
    assert r.weight_lb == 21.2 and r.voltage_v == "220-240" and r.amps == 33.0 and r.frequency_hz == 50.0
    assert r.image_url.startswith("https://smegbrasil.cdn.magazord.com.br/") and r.release_date is None
    x = r.extra_specs
    assert x["Type > Induction"] == "Yes" and x["Type > Power supply"] == "Electric" and x["Programs / Functions > Power levels"] == "9"
    assert x["General > Colour"] == "Black" and x["Electrical connection > Power cord length"] == "120 cm"
    assert not any("flex" in v or "px" in v for v in x.values())  # <style> noise in the description is dropped
    assert any(w.key == "Peso líquido (kg)" and w.value.startswith("9,600") for w in raw)  # Portuguese original kept
    assert docs == [("Manual", "https://smegbrasil.cdn.magazord.com.br/img/2025/03/produto/2025/si4642d-manual-de-instrucoes-pt.pdf")]


def test_oven_record_units_and_launch_date():
    r, raw, docs = _parse("SOP6602TNR")
    assert r.subcategory == "electric_oven" and r.price_local == 24990.0 and r.capacity_total_cuft == 2.4  # 68 l
    assert (r.height_in, r.width_in, r.depth_in) == (23.3, 23.5, 21.6) and r.weight_lb == 86.9  # 592x597x548 mm, 39.4 kg
    assert r.amps == 13.0 and r.voltage_v == "220-240" and r.frequency_hz == 60.0
    assert (r.release_date, r.release_src) == ("2025-01-07", "distribution")  # page JSON data_lancamento
    assert (r.rating, r.review_count, r.is_new) == (None, None, None)  # 0 reviews: unknown, not 0
    assert r.extra_specs["Performance / Energy label > Energy efficiency class"] == "EU class A+" if \
        "Performance / Energy label > Energy efficiency class" in r.extra_specs else True
    assert r.pod_features[0].startswith("Sistema de fechamento amortecido")  # intro bullets (Portuguese offline)
    assert len(raw) > 80 and all(w.brand == "Smeg" and w.source == "web" for w in raw)


def test_combi_microwave_and_grill_microwave():
    r, _, _ = _parse("SO4604M2PNR")
    assert r.subcategory == "sco" and r.capacity_total_cuft == 1.41 and r.price_local == 39990.0  # 40 litros
    assert (r.height_in, r.width_in, r.depth_in) == (17.9, 23.5, 21.6)
    m, _, _ = _parse("MP722AO")
    assert m.subcategory == "microwave" and m.finish_color == "Anthracite" and m.release_date == "2025-06-12"


def test_gas_range_record():
    r, raw, _ = _parse("TR90GMP")
    assert r.subcategory == "gas_oven" and r.finish_color == "Cream" and r.price_local == 45990.0
    assert (r.height_in, r.width_in, r.depth_in) == (35.4, 35.4, 23.6) and r.weight_lb == 175.9  # '79.800 kg' is 79.8, not 79800
    assert r.amps == 14.0
    assert r.extra_specs["General > Burners"] == "5"
    g, _, _ = _parse("PV695LCNR")
    assert g.subcategory == "gas_cooktop" and g.weight_lb == 42.5  # '19,300 kg'


def test_spec_rows_parser():
    html = ("<p><strong>Tipo</strong></p><p>Cor:&nbsp;Nero<br />Acabamento: Brilhante<br /> <br /><strong>Controles</strong></p>"
            "<p>Exibição: Sim<br />Claro! Vamos refazer: tudo</p><style>.a{width: 10px;}</style><p>Texto livre sem rótulo.</p>")
    assert s.spec_rows(html) == [("Tipo", "Cor", "Nero"), ("Tipo", "Acabamento", "Brilhante"), ("Controles", "Exibição", "Sim")]
    assert s._kg("19,300 kg") == 19.3 and s._kg("91.000 kg") == 91.0 and s._kg("39,4 kg") == 39.4 and s._kg("sem peso") is None


def test_url_check():
    assert s.url_check(BASE + "forno-smeg-pirolitico-galileo-tradicional-dolce-stil-220v-sop6602tnr").startswith("forno-smeg")
    for bad in ("http://www.smegbrasil.com.br/forno-smeg-abc", "https://evil.com/forno-smeg-abc", "https://www.smegbrasil.com.br.evil.com/forno-smeg-abc",
                BASE + "coccao", BASE + "forno-smeg-abc?caracteristica=x", BASE + "a/b", "https://user@www.smegbrasil.com.br/forno-smeg-abc"):
        try:
            s.url_check(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_discover_walks_pages_with_fake_fetch():
    calls = []

    def fake(url, probe):
        calls.append(url)
        return _t("listing_coccao_p1.html") if url.endswith("p=1") else "<html>itens: []</html>"

    real, s.fetch_html = s.fetch_html, fake
    s._LISTING_CACHE.clear()
    try:
        got = s.discover("gas_cooktop", limit=3)
        assert [c.model_number for c in got] == ["PV695LCNR", "PGF96", "SR975XGH"] and calls == [BASE + "coccao?p=1"]
        assert len(s.discover("induction", limit=30)) == 2 and calls[-1].endswith("p=2")  # page 1 cached; page 2 empty -> stop
        assert calls.count(BASE + "coccao?p=1") == 1
    finally:
        s.fetch_html = real
        s._LISTING_CACHE.clear()


def test_scrape_with_fake_fetch_and_download():
    got = []
    real_f, real_d = s.fetch_html, s.common.download_pdf
    s.fetch_html = lambda url, probe: _t("pdp_PV695LCNR.html")
    s.common.download_pdf = lambda brand, model, dtype, url: got.append((brand, model, dtype, url)) or None
    try:
        r, docs, raw = s.scrape(BASE + "cooktop-smeg-semiprofissional-5-queimadores-gas-linha-dolce-stil-novo-preto-90-cm-220v-pv695lcnr")
    finally:
        s.fetch_html, s.common.download_pdf = real_f, real_d
    assert r.model_number == "PV695LCNR" and docs == [] and len(raw) > 60
    assert got == [("Smeg", "PV695LCNR", "SpecSheet", "https://smegbrasil.cdn.magazord.com.br/img/2025/03/produto/2012/pv695lcnr.pdf")]


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


def test_catalog_discovers_module():
    assert catalog.module_name("Smeg", "br") == "smeg_br"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
