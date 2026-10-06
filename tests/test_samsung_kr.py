"""Plain-assert offline tests (saved fixtures of samsung.com/sec). Run: python tests/test_samsung_kr.py  (or pytest)

ko_en is replaced by a deterministic fake (labels/values become 'EN(<korean>)') so the tests need neither the glossary
file nor the local LLM; the real ko_en is exercised by tests/test_ko_en.py and the live run."""
import contextlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import samsung_kr as sk
from schema import DocumentRecord

FX = Path(__file__).parent / "fixtures" / "samsung_kr"
_HANGUL = re.compile(r"[가-힣]")


def _txt(name):
    return (FX / name).read_text(encoding="utf-8")


def _list(name):
    return json.loads(_txt(name))["products"]


class FakeKo:
    """Stand-in for ko_en: Korean text -> 'EN(<text>)'; numbers/units untouched."""
    ANNUALIZED_LABEL = "estimated = monthly x 12"
    calls: list = []

    @classmethod
    def translate_many(cls, texts, kind="value"):
        cls.calls.append((kind, list(texts)))
        return [f"EN({t})" if _HANGUL.search(t) else t for t in texts]

    @staticmethod
    def annualize(kwh_month):
        return round(kwh_month * 12, 1)

    @staticmethod
    def kr_energy_grade(text):
        m = re.search(r"([1-5])\s*등급", text)
        return f"KR grade {m.group(1)}" if m else None


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


def _fake_ko():
    return patched(sk, _ko=lambda: FakeKo)


ALL_ITEMS = (_list("goodslist_refrigerators.json") + _list("goodslist_laundry.json") + _list("goodslist_cooking.json")
             + _list("goodslist_induction.json"))


# ------------------------------------------------------------------ constants / classify
def test_constants_and_supported_subcategories():
    assert (sk.COUNTRY, sk.REGION, sk.CURRENCY, sk.BRAND) == ("kr", "kr", "KRW", "Samsung")
    assert sk.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys())
    assert not sk.SUPPORTED_SUBCATEGORIES & {"scr", "otr", "gas_oven", "gas_cooktop", "radiant"}  # none sold on samsung.com/sec
    assert {"sco", "electric_oven", "microwave", "induction"} <= sk.SUPPORTED_SUBCATEGORIES
    for sub, (major, codes) in sk.SUB_SOURCES.items():
        assert catalog.major_of(sub) == major and codes
    assert catalog.supported("Samsung", "kr") == sk.SUPPORTED_SUBCATEGORIES  # auto-discovered by the registry
    assert catalog.module_name("Samsung", "kr") == "samsung_kr"


def test_classify_known_products():
    c = sk.classify
    assert c("refrigerators", "french-door-rm90h64p2w-d2c", "Bespoke AI 패밀리허브 4도어 키친핏 Max 602L", "RM90H64P2W") == "french_door"
    assert c("refrigerators", "side-by-side-rs84db5002cw-d2c", "Bespoke 양문형 냉장고 852L", "RS84DB5002CW") == "side_by_side"
    assert c("refrigerators", "top-mount-freezer-rt20farl3s9-d2c", "냉장고 203L", "RT20FARL3S9") == "top_freezer"
    assert c("refrigerators", "bottom-mount-freezer-rb30f4051ww-d2c", "냉장고 306L", "RB30F4051WW") == "bottom_freezer"
    assert c("refrigerators", "bottom-mount-freezer-brb70f26d3f0-d2c", "빌트인 냉장고 259L", "BRB70F26D3F0") == "built_in"
    assert c("refrigerators", "one-door-rr40c7905ap-d2c", "Bespoke AI 냉장고 1도어 키친핏 409L (좌열림, 냉장전용)", "RR40C7905AP") == "built_in"
    assert c("refrigerators", "one-door-rr09bg014ww-d2c", "냉장고 89L (냉장전용)", "RR09BG014WW") == "compact"
    assert c("refrigerators", "one-door-rt40h15v3sw-d2c", "냉장고 150L", "RT40H15V3SW") == "compact"
    assert c("refrigerators", "brf425220ap-d2c", "셰프컬렉션 T-Type 냉장고 621L", "BRF425220AP") == "built_in"
    # excluded groups
    assert c("refrigerators", "crf-0620", "업소용 냉장고 505L (냉장전용)", "CRF-0620") is None
    assert c("refrigerators", "one-door-rw99h33wn0-d2c", "Infinite AI 와인냉장고 1도어 키친핏 101병", "RW99H33WN0") is None
    assert c("refrigerators", "top-mount-freezer-rz22cg4000ww-d2c", "냉동고 227L (냉동전용)", "RZ22CG4000WW") is None
    assert c("refrigerators", "one-door-big-d2c", "냉장고 450L", "X1") is None  # big one-door that is neither
    assert c("washing-machines", "top-loader-wa23a8375kv-d2c", "통버블 세탁기 23kg", "WA23A8375KV") == "top_load"
    assert c("washing-machines", "wf80h21bds-d2c", "Bespoke AI 세탁기 21kg", "WF80H21BDS") == "front_load"
    assert c("washing-machines", "wd40f09n0y-d2c", "세탁기(건조 겸용) 9kg", "WD40F09N0Y") == "laundry_center"
    assert c("washing-machines", "onebody-wh90f2522bbhs-d2c", "Bespoke AI 원바디 25/22kg", "WH90F2522BBHS") == "laundry_center"
    assert c("laundry-combo", "combo-wd90h25ahs-d2c", "Bespoke AI 콤보 25/20kg", "WD90H25AHS") == "laundry_center"
    assert c("dryers", "dryer-dv80h22cds-d2c", "Bespoke AI 건조기 22kg", "DV80H22CDS") == "dryer"
    assert c("electric-range", "cooktop-nz63db657cf-d2c", "Bespoke AI 인덕션 (플렉스존)", "NZ63DB657CFH") == "induction"
    assert c("micro-wave-ovens", "ms23t5018ac-d2c", "Bespoke 전자레인지 23L", "MS23T5018AC") == "microwave"
    assert c("qooker-multi-ovens", "qooker-mo22a7797cv2-d2c", "Bespoke 큐커 멀티 22L", "MO22A7797CV2") == "sco"  # Qooker = oven + microwave
    assert c("qooker-multi-ovens", "microwave-oven-convection-nq50t9539be-d2c", "Infinite Line 콤팩트 오븐 50L", "NQ50T9539BD") == "sco"
    assert c("qooker-multi-ovens", "x", "Bespoke 큐커 오븐 35L (직화오븐)", "MC35A8599LE") == "sco"
    assert c("qooker-multi-ovens", "x", "전자레인지 20L", "MS1") == "microwave"
    assert c("qooker-multi-ovens", "microwave-oven-convection-nv75t9879ce-d2c", "Infinite Line 빌트인 전기오븐 75L", "NV75T9879CD") == "electric_oven"
    assert c("hood", "hood-nk90b8770ap-d2c", "Bespoke 후드 Air", "NK90B8770AG") is None
    assert c("kimchi-refrigerators", "x", "김치플러스 3도어", "RQ1") is None
    assert c("televisions", "x", "TV", "QN1") is None


def test_classify_is_exclusive_and_covers_every_supported_sub():
    subs = {}
    for item in ALL_ITEMS:
        section, slug, _ = sk._path_parts(item["goodsDetailUrl"])
        sub = sk.classify(section, slug, sk._clean(item["goodsNm"]), item["mdlCode"])
        assert sub is None or sub in sk.SUPPORTED_SUBCATEGORIES, (item["mdlCode"], sub)
        subs.setdefault(sub, set()).add(item["mdlCode"])
    for sub in sk.SUPPORTED_SUBCATEGORIES:
        assert subs.get(sub), f"no fixture product classifies as {sub}"
    # a model number is in exactly one bucket (the same model repeats across the induction/cooking lists only)
    seen = {}
    for sub, models in subs.items():
        for m in models:
            assert seen.setdefault(m, sub) == sub, (m, sub, seen[m])


def test_bundle_rule():
    assert sk._is_bundle("package-wf80h2118gdhs-d2c", "20") is True
    assert sk._is_bundle("package-dv20cb8600bwl-d2c", "10") is False  # a real single dryer with a package- slug
    assert sk._is_bundle("onebody-wh90f2120gbhy-d2c", "20") is False  # a real washtower
    assert sk._is_bundle("combo-wd90f25ahy-d2c", None) is False


# ------------------------------------------------------------------ discover
def test_parse_goods_list_candidate_fields():
    stats = {}
    cands = sk.parse_goods_list(_list("goodslist_refrigerators.json"), "french_door", stats)
    assert cands and all(c.subcategory == "french_door" and c.category == "refrigerator" for c in cands)
    c = next(c for c in cands if c.model_number == "RM90H64P2W")
    assert (c.brand, c.region, c.country, c.currency) == ("Samsung", "kr", "kr", "KRW")
    assert c.price_usd is None and c.price_local == 3890000 and isinstance(c.price_local, (int, float))
    assert c.url == "https://www.samsung.com/sec/refrigerators/french-door-rm90h64p2w-d2c/RM90H64P2W/"
    assert c.attrs["capacity_total_cuft"] == 21.3 and c.attrs_src["capacity_total_cuft"] == "name"  # 602 L
    assert c.attrs["wifi"] is True and c.attrs["screen"] is True  # 패밀리허브
    assert c.attrs["finish"] == "white" and c.attrs_src["finish"] == "listing"  # 클린 화이트
    assert c.attrs["sale_status"] == "on_sale"
    assert stats.get("other", 0) > 0  # other sub keys were skipped, not mixed in


def test_parse_goods_list_sold_out_has_no_price_and_grade_from_name():
    item = dict(next(i for i in _list("goodslist_refrigerators.json") if i["mdlCode"] == "RM70H91RMA"))
    item.update(saleStatCd="17", salePrice=0)
    c = sk.parse_goods_list([item], "french_door")[0]
    assert c.price_local is None and c.attrs["sale_status"] == "sold_out"
    assert c.attrs["kr_grade"] == 1  # '에너지 1등급 최저기준 대비 -35%'


def test_parse_goods_list_washer_and_cooking_attrs():
    laundry = _list("goodslist_laundry.json")
    wt = sk.parse_goods_list(laundry, "laundry_center")
    one = next(c for c in wt if c.model_number == "WH90F2520GBHY")
    assert one.attrs["capacity_kg"] == 25.0 and one.price_local == 3109000
    dry = sk.parse_goods_list(laundry, "dryer")
    assert next(c for c in dry if c.model_number == "DV80H22CDS").attrs["capacity_kg"] == 22.0
    ovens = sk.parse_goods_list(_list("goodslist_cooking.json"), "electric_oven")
    nv = next(c for c in ovens if c.model_number == "NV75T9879CD")
    assert nv.attrs["oven_capacity_cuft"] == 2.65 and nv.price_local is None  # sold out -> no price
    assert {c.model_number for c in ovens} == {"NV75T9879CD"}  # compact/Qooker combi ovens are sco, not electric_oven
    sco = {c.model_number for c in sk.parse_goods_list(_list("goodslist_cooking.json"), "sco")}
    assert {"NQ50T8539BK", "NQ50T9539BD", "MC32B7388CC", "MO22A7797CV2", "MC35A8599LE"} <= sco
    assert not sco & {c.model_number for c in ovens}


def test_parse_goods_list_skips_bundles_offdomain_and_invalid():
    base = next(i for i in _list("goodslist_laundry.json") if i["mdlCode"] == "WF80H21BDS")
    evil = dict(base, mdlCode="EVIL1", goodsDetailUrl="https://evil.example.com/sec/washing-machines/wf80h21bds-d2c/EVIL1/")
    other_country = dict(base, mdlCode="EVIL2", goodsDetailUrl="//www.samsung.com/us/washers/x/EVIL2/")
    http = dict(base, mdlCode="EVIL3", goodsDetailUrl="http://www.samsung.com.evil.com/sec/washing-machines/x/EVIL3/")
    bundle = dict(base, mdlCode="BUN1", goodsTpCd="20", goodsDetailUrl="washing-machines/package-bun1-d2c/BUN1/")
    nocode = dict(base, mdlCode=None)
    stats = {}
    got = sk.parse_goods_list([evil, other_country, http, bundle, nocode, base], "front_load", stats)
    assert [c.model_number for c in got] == ["WF80H21BDS"]
    assert stats["off_domain"] == 3 and stats["bundle"] == 1 and stats["invalid"] == 1


class FakeNet:
    """Serves goodsList fixtures by dispClsfNo; records requests."""
    requests: list = []

    def __init__(self, page_path, use_requests=True):
        self.page_path = page_path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, url, ok=None, referer=None):
        FakeNet.requests.append(url)
        code = re.search(r"dispClsfNo=(\d+)", url).group(1)
        name = {"36010000": "goodslist_refrigerators.json", "100043932": "goodslist_laundry.json",
                "36030000": "goodslist_cooking.json", "36070000": "goodslist_induction.json"}[code]
        text = _txt(name)
        assert ok(text)
        return text


def test_discover_per_sub_with_fake_net():
    sk._clear_cache()
    FakeNet.requests = []
    with patched(sk, _Net=FakeNet):
        fd = sk.discover("french_door", limit=5)
        assert len(fd) == 5 and all(c.subcategory == "french_door" for c in fd)
        assert [c.price_local is None for c in fd] == sorted(c.price_local is None for c in fd)  # on-sale first
        sbs = sk.discover("side_by_side", limit=30)
        assert sbs and all(c.subcategory == "side_by_side" for c in sbs)
        assert len([u for u in FakeNet.requests if "36010000" in u]) == 1  # second sub key reused the cached listing
        ind = sk.discover("induction", limit=50)
        models = [c.model_number for c in ind]
        assert len(models) == len(set(models)) and "NZ63DB657CFH" in models  # two codes, de-duplicated
        for sub in sorted(sk.SUPPORTED_SUBCATEGORIES):
            got = sk.discover(sub, limit=30)
            assert got, sub
            assert all(c.url.startswith("https://www.samsung.com/sec/") and c.region == "kr" for c in got)
        for bad in ("scr", "otr", "gas_oven", "gas_cooktop", "radiant", "nope"):
            try:
                sk.discover(bad)
            except ValueError:
                continue
            raise AssertionError(f"{bad} should be unsupported")
    sk._clear_cache()


# ------------------------------------------------------------------ parsing
def test_parse_spec_html_every_row():
    for name in ("fr", "wf", "dry", "ind", "mw", "ov", "tl", "wt", "tf"):
        html = _txt(f"spec_{name}.html")
        rows = sk.parse_spec_html(html)
        nonempty = [d for d in re.findall(r'<p class="spec-desc">(.*?)</p>', html, re.S) if re.sub(r"<[^>]+>|&nbsp;|\s", "", d)]
        assert len(rows) == len(nonempty), (name, len(rows), len(nonempty))
        assert all(s and l and v for s, l, v in rows) or all(l and v for s, l, v in rows)
        assert not any("aria-haspopup" in l or "*" in l for _, l, _ in rows), name  # help-text attribute junk
        assert not any("&nbsp;" in v or "\xa0" in v for _, _, v in rows)
    rows = sk.parse_spec_html(_txt("spec_fr.html"))
    assert ("규격", "크기(가로 × 높이 × 깊이)", "912 × 1,853 × 683 mm") in rows
    assert ("성능", "컴프레서", "AI 인버터 컴프레서") in rows  # button-style label
    tl = sk.parse_spec_html(_txt("spec_tl.html"))
    assert any(l == "무세제통세척" for _, l, _ in tl)
    try:
        sk.parse_spec_html("<div>nothing</div>")
    except sk.SamsungKrPageError:
        pass
    else:
        raise AssertionError("empty spec must raise")


def test_parse_pdp_panel_manuals():
    url = "https://www.samsung.com/sec/refrigerators/french-door-rm90h64p2w-d2c/RM90H64P2W/"
    page = sk.parse_pdp(_txt("pdp_fr.html"), url)
    assert page["model"] == "RM90H64P2W" and page["goods_id"] == "G002961598"
    assert page["name"].startswith("Bespoke AI 패밀리허브 4도어")
    assert page["image_url"].startswith("https://images.samsung.com/kdp/goods/")
    assert page["features"][:3] == ["새로운 기술로 완성된 키친핏 Max", "완전 새로운 푸드 경험 AI 푸드매니저",
                                    "스마트한 키친 라이프의 시작 패밀리허브"]
    assert "Highlights" not in page["features"] and len(set(page["features"])) == len(page["features"])
    assert not any("레이어 팝업" in f for f in page["features"])
    assert sk.parse_purchase_panel(_txt("rd_fr.html")) == {"sale_price": 3890000, "list_price": 4640000,
                                                          "status": "12", "goods_tp": "10"}
    assert sk.parse_purchase_panel("<p>x</p>")["sale_price"] is None
    m = sk.parse_manuals(_txt("manual_fr.html"))
    assert m[0]["name"] == "사용자 매뉴얼" and m[0]["url"].endswith(".pdf") and m[0]["lang"] == "한국어"
    assert all(d["url"].startswith("https://downloadcenter.samsung.com/") for d in m)  # the (HTML) manual is not a PDF
    evil = ('<li class="item"><strong class="name">사용자 매뉴얼</strong><span class="desc-item">v</span>'
            '<span class="desc-item">한국어</span><a href="https://evil.example.com/x.pdf" class="btn-download">x</a></li>'
            '<li class="item"><strong class="name">매뉴얼</strong><a href="http://downloadcenter.samsung.com/a.pdf" class="btn-download">x</a></li>'
            '<li class="item"><strong class="name">매뉴얼</strong><a href="https://samsung.com.evil.com/a.pdf" class="btn-download">x</a></li>')
    got = sk.parse_manuals(evil)
    assert [d["url"] for d in got] == ["https://downloadcenter.samsung.com/a.pdf"]  # off-domain dropped, http upgraded
    try:
        sk.parse_pdp("<html></html>", url)
    except sk.SamsungKrPageError:
        pass
    else:
        raise AssertionError("PDP without og:title must raise")


def test_helpers_dimension_axis_order_and_numbers():
    rows = [("크기(가로 × 세로 × 높이)", "외부 치수", "600 x 520 x 48 mm"), ("크기(가로 × 세로 × 높이)", "타공 사이즈", "560 x 480 mm")]
    assert sk._dimensions(rows) == (600.0, 48.0, 520.0)  # w, h, d: 세로 is depth on cooktops
    rows = [("제품 사양", "조리실 크기(가로 × 높이 × 깊이)", "330 x 211 x 324 mm"), ("제품 사양", "외관 크기(가로 × 높이 × 깊이)", "489 x 275 x 363 mm")]
    assert sk._dimensions(rows) == (489.0, 275.0, 363.0)  # exterior, never the cavity
    assert sk._dimensions([("규격", "무게", "142 kg")]) == (None, None, None)
    assert sk._num("12.2 / 13.5 kg", "kg") == 12.2 and sk._num("602 ℓ", "l") == 602.0 and sk._num("5 개", "kg") is None
    assert sk._yn("없음") is False and sk._yn("미지원") is False and sk._yn("지원") is True and sk._yn("빅 아이스메이커") is True
    assert sk._yn("") is None
    assert sk._electrical([("a", "정격 전압", "220V / 60Hz")]) == ("220", 60.0)
    assert sk._electrical([("a", "전원", "220 V/60 Hz")]) == ("220", 60.0)


# ------------------------------------------------------------------ build_record
def _build(sub_fixture, url, name, model, features=(), panel=None, goods_id="G1"):
    page = {"name": name, "model": model, "goods_id": goods_id, "image_url": None, "features": list(features)}
    panel = panel or {"sale_price": None, "list_price": None, "status": None, "goods_tp": "10"}
    rows = sk.parse_spec_html(_txt(f"spec_{sub_fixture}.html"))
    with _fake_ko():
        return sk.build_record(url, page, panel, rows, [])[0], rows


def test_build_record_refrigerator_full():
    url = "https://www.samsung.com/sec/refrigerators/french-door-rm90h64p2w-d2c/RM90H64P2W/"
    page = sk.parse_pdp(_txt("pdp_fr.html"), url)
    panel = sk.parse_purchase_panel(_txt("rd_fr.html"))
    rows = sk.parse_spec_html(_txt("spec_fr.html"))
    manuals = sk.parse_manuals(_txt("manual_fr.html"))
    with _fake_ko():
        p, docs = sk.build_record(url, page, panel, rows, manuals)
    assert (p.brand, p.model_number, p.category, p.subcategory, p.door_style) == (
        "Samsung", "RM90H64P2W", "refrigerator", "french_door", "French Door")
    assert (p.region, p.country, p.currency, p.price_usd, p.price_local) == ("kr", "kr", "KRW", None, 3890000.0)
    assert p.capacity_total_cuft == 21.26 and p.capacity_fridge_cuft == 12.96 and p.capacity_freezer_cuft == 8.3
    assert (p.width_in, p.height_in, p.depth_in) == (35.9, 73.0, 26.9)
    assert p.weight_lb == 313.1 and p.voltage_v == "220" and p.frequency_hz == 60.0
    assert p.energy_kwh_year == 459.6  # 38.3 kWh/month x 12 (estimate, labelled below)
    assert p.energy_star is None  # KR grades are never ENERGY STAR
    x = p.extra_specs
    assert x["Total capacity (L)"] == "602" and x["Fresh food capacity (L)"] == "367" and x["Freezer capacity (L)"] == "235"
    assert (x["Width (mm)"], x["Height (mm)"], x["Depth (mm)"], x["Weight (kg)"]) == ("912", "1853", "683", "142")
    assert x["KR energy grade"] == "KR grade 2" and "ENERGY STAR" not in " ".join(x.values())
    assert x["Monthly energy consumption (kWh/month)"] == "38.3" and x["Annual energy consumption (kWh/year)"] == "459.6"
    assert x["Annual energy basis"] == FakeKo.ANNUALIZED_LABEL
    assert x["List price (KRW)"] == "4640000" and x["Sale price (KRW)"] == "3890000"
    assert p.wifi_supported is True and "스마트 > WiFi = 지원" in p.wifi_evidence  # evidence is the Korean source row
    assert p.ice_maker is True and p.water_dispenser is None
    assert p.finish_color == "EN(클린화이트)"
    assert p.image_url.startswith("https://images.samsung.com/")
    # features: translated in pod_features, Korean originals in extra_specs
    assert p.pod_features[0] == "EN(새로운 기술로 완성된 키친핏 Max)" and len(p.pod_features) == len(page["features"])
    assert x["Feature (KO) 1"] == "새로운 기술로 완성된 키친핏 Max" and f"Feature (KO) {len(page['features'])}" in x
    # EVERY spec row is present under a translated 'Section > Label' key, values translated, numbers kept
    assert x["EN(규격) > EN(크기(가로 × 높이 × 깊이))"] == "912 × 1,853 × 683 mm"
    assert x["EN(성능) > EN(정격 전압)"] == "220V / 60Hz"
    assert x["EN(스마트) > WiFi"] == "EN(지원)"
    table_keys = [k for k in x if " > " in k]
    assert len(table_keys) == len({(s, l) for s, l, _ in rows}) == 58
    assert docs[0][0] == "Manual" and docs[0][1].startswith("https://downloadcenter.samsung.com/")
    assert [d[0] for d in docs] == ["Manual", "Other", "Other 2"] and len(docs) <= sk.MAX_PDFS


def test_build_record_sold_out_has_no_price():
    url = "https://www.samsung.com/sec/refrigerators/french-door-rm90h64p2w-d2c/RM90H64P2W/"
    p, _ = _build("fr", url, "Bespoke AI 4도어 602L", "RM90H64P2W", panel={"sale_price": 3890000, "list_price": 4640000,
                                                                     "status": "17", "goods_tp": "10"})
    assert p.price_local is None


def test_build_record_top_freezer_and_missing_fields_stay_none():
    p, rows = _build("tf", "https://www.samsung.com/sec/refrigerators/top-mount-freezer-rt20farl3s9-d2c/RT20FARL3S9/",
                     "냉장고 203L", "RT20FARL3S9")
    assert p.subcategory == "top_freezer" and p.door_style == "Top Freezer"
    assert p.capacity_total_cuft == 7.17 and p.energy_kwh_year == 369.6 and p.extra_specs["KR energy grade"] == "KR grade 3"
    assert p.wifi_supported is None and p.wifi_evidence is None  # silent -> unknown, not False
    assert p.ice_maker is None and p.voltage_v == "220" and p.frequency_hz == 60.0
    assert p.price_local is None and p.pod_features == []
    assert (p.width_in, p.height_in, p.depth_in) == (21.9, 56.9, 25.1)


def test_build_record_washer_dryer_washtower():
    p, _ = _build("wf", "https://www.samsung.com/sec/washing-machines/wf80h21bds-d2c/WF80H21BDS/", "Bespoke AI 세탁기 21kg", "WF80H21BDS")
    assert (p.category, p.subcategory) == ("washer", "front_load")
    assert p.capacity_total_cuft is None and p.extra_specs["Washer capacity (kg)"] == "21"  # kg is not cu ft
    assert (p.width_in, p.height_in, p.depth_in) == (27.0, 38.7, 31.3) and p.weight_lb == 209.4
    assert p.extra_specs["KR energy grade"] == "KR grade 1" and p.energy_kwh_year is None and p.energy_star is None
    assert p.wifi_supported is True and "WiFi 내장 = 지원" in p.wifi_evidence and p.ice_maker is None and p.door_style is None
    p, _ = _build("dry", "https://www.samsung.com/sec/dryers/dryer-dv80h22cds-d2c/DV80H22CDS/", "Bespoke AI 건조기 22kg", "DV80H22CDS")
    assert p.subcategory == "dryer" and p.extra_specs["Dryer capacity (kg)"] == "22" and "Washer capacity (kg)" not in p.extra_specs
    p, _ = _build("tl", "https://www.samsung.com/sec/washing-machines/top-loader-wa23a8375kv-d2c/WA23A8375KV/", "통버블 세탁기 23kg", "WA23A8375KV")
    assert p.subcategory == "top_load" and p.extra_specs["Washer capacity (kg)"] == "23"
    p, _ = _build("wt", "https://www.samsung.com/sec/washing-machines/onebody-wh90f2522bbhs-d2c/WH90F2522BBHS/", "Bespoke AI 원바디 25/22kg", "WH90F2522BBHS")
    assert p.subcategory == "laundry_center"
    assert p.extra_specs["KR energy grade"] == "KR grade 1 (washing) / KR grade 3 (drying)"
    assert p.extra_specs["Washer capacity (kg)"] == "25" and p.extra_specs["Dryer capacity (kg)"] == "22"
    assert p.extra_specs["EN(세탁 주요기능) > EN(버블)"] == "EN(있음)"  # duplicate labels live in separate sections


def test_build_record_cooking():
    p, _ = _build("ind", "https://www.samsung.com/sec/electric-range/cooktop-nz63db657cf-d2c/NZ63DB657CFH/", "Bespoke AI 인덕션 (플렉스존)", "NZ63DB657CFH")
    assert (p.category, p.subcategory) == ("cooking", "induction")
    assert (p.width_in, p.height_in, p.depth_in) == (23.6, 1.9, 20.5)  # 600 x 520 x 48 (W x D x H)
    assert p.weight_lb == 26.9 and p.voltage_v == "220" and p.wifi_supported is True
    assert p.extra_specs["KR energy grade"] == "KR grade 1" and p.capacity_total_cuft is None
    assert p.extra_specs["EN(고지정보) > EN(소비전력)"] == "3,400 W"
    p, _ = _build("mw", "https://www.samsung.com/sec/micro-wave-ovens/microwave-oven-solo-ms23t5018ac-d2c/MS23T5018AC/", "Bespoke 전자레인지 23L", "MS23T5018AC")
    assert p.subcategory == "microwave" and p.capacity_total_cuft == 0.81 and p.extra_specs["Total capacity (L)"] == "23"
    assert (p.width_in, p.height_in, p.depth_in) == (19.3, 10.8, 14.3) and p.weight_lb == 26.5
    assert p.wifi_supported is False and "WiFi 내장 = 없음" in p.wifi_evidence
    p, _ = _build("ov", "https://www.samsung.com/sec/qooker-multi-ovens/microwave-oven-convection-nq50t9539be-d2c/NQ50T9539BD/", "Infinite Line 콤팩트 오븐 50L", "NQ50T9539BD")
    assert p.subcategory == "sco" and p.capacity_total_cuft == 1.77
    assert (p.width_in, p.height_in, p.depth_in) == (23.4, 18.0, 22.4) and p.wifi_supported is True


def test_build_record_rejects_unsupported_group_and_translates_in_batches():
    rows = sk.parse_spec_html(_txt("spec_fr.html"))
    page = {"name": "x", "model": "M1", "goods_id": "G1", "image_url": None, "features": []}
    with _fake_ko():
        try:
            sk.build_record("https://www.samsung.com/sec/hood/hood-x-d2c/M1/", page, {}, rows, [])
        except sk.SamsungKrPageError:
            pass
        else:
            raise AssertionError("hood is not a supported group")
        FakeKo.calls.clear()
        sk.build_record("https://www.samsung.com/sec/refrigerators/french-door-x-d2c/M1/", dict(page, name="4도어 602L"),
                        {}, rows, [])
    labels = [c for c in FakeKo.calls if c[0] == "label"]
    values = [c for c in FakeKo.calls if c[0] == "value"]
    assert len(labels) == 1 and len(labels[0][1]) == len(set(labels[0][1]))  # ONE batched label translation, deduped
    assert len(values) == 1  # rows + features + finish share ONE value batch


# ------------------------------------------------------------------ scrape (offline)
class FakeScrapeNet:
    seen: list = []

    def __init__(self, page_path, use_requests=True):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, url, ok=None, referer=None):
        FakeScrapeNet.seen.append(("GET", url))
        text = _txt("pdp_fr.html")
        assert ok is None or ok(text)
        return text

    def post(self, url, data, ok=None, referer=None):
        FakeScrapeNet.seen.append(("POST", url, data))
        name = url.rsplit("/", 1)[1]
        text = {"goodsRevampDetail": _txt("rd_fr.html"), "getGoodsSpecList": _txt("spec_fr.html"),
                "goodsManual": _txt("manual_fr.html")}[name]
        assert ok is None or ok(text)
        return text


def test_scrape_end_to_end_offline():
    url = "https://www.samsung.com/sec/refrigerators/french-door-rm90h64p2w-d2c/RM90H64P2W/"
    downloads = []

    def fake_download(brand, model, doc_type, u):
        downloads.append((brand, model, doc_type, u))
        return DocumentRecord(brand=brand, model_number=model, doc_type=doc_type, source_url=u,
                              local_path=f"downloads/samsung/{model}_{doc_type}.pdf", sha256="0" * 64, size_bytes=1)
    FakeScrapeNet.seen = []
    with _fake_ko(), patched(sk, _Net=FakeScrapeNet, download_pdf=fake_download, _throttle=lambda: None):
        product, docs, raw = sk.scrape(url)
    assert product.model_number == "RM90H64P2W" and product.price_local == 3890000.0
    assert [d.doc_type for d in docs] == ["Manual", "Other", "Other 2"]
    assert all(d[3].startswith("https://downloadcenter.samsung.com/") and d[1] == "RM90H64P2W" for d in downloads)
    # RawSpec keeps the ORIGINAL Korean section/label/value, one per spec row
    assert len(raw) == 58 and all(r.source == "web" and r.model_number == "RM90H64P2W" for r in raw)
    assert any((r.section, r.key, r.value) == ("규격", "크기(가로 × 높이 × 깊이)", "912 × 1,853 × 683 mm") for r in raw)
    assert any(_HANGUL.search(r.key) for r in raw) and not any(r.key.startswith("EN(") for r in raw)
    methods = [s[0] for s in FakeScrapeNet.seen]
    assert methods == ["GET", "POST", "POST", "POST"]
    assert FakeScrapeNet.seen[2][2]["goodsId"] == "G002961598"


def test_scrape_rejects_bad_urls_and_bundles():
    for bad in ("https://evil.example.com/sec/refrigerators/x/M1/", "http://www.samsung.com/sec/refrigerators/x/M1/",
                "https://www.samsung.com.evil.com/sec/refrigerators/x/M1/", "https://www.samsung.com/us/refrigerators/x/M1/",
                "https://user@www.samsung.com/sec/refrigerators/x/M1/", "ftp://www.samsung.com/sec/x/", "not a url"):
        try:
            sk.scrape(bad)
        except sk.SamsungKrPageError:
            continue
        raise AssertionError(f"{bad} must be rejected")
    bundle_url = "https://www.samsung.com/sec/washing-machines/package-wf80h2118gdhs-d2c/WF80H2118GDHS/"

    class BundleNet(FakeScrapeNet):
        def post(self, url, data, ok=None, referer=None):
            return '<input name="goodsTpCd" value="20" /><input name="saleStatCd" value="12"/>' if url.endswith("goodsRevampDetail") else ""
    with _fake_ko(), patched(sk, _Net=BundleNet, _throttle=lambda: None):
        try:
            sk.scrape(bundle_url)
        except sk.SamsungKrPageError as e:
            assert "bundle" in str(e)
        else:
            raise AssertionError("bundle must be rejected")


# ------------------------------------------------------------------ http / geo / politeness / browser fallback
class _Resp:
    def __init__(self, status, text="", location=None):
        self.status_code, self.text, self.encoding = status, text, None
        self.headers = {"Location": location} if location else {}


def test_http_follows_sec_redirects_and_rejects_geo_redirect():
    pages = {"https://www.samsung.com/sec/a/": _Resp(301, location="/sec/b/"), "https://www.samsung.com/sec/b/": _Resp(200, "한글"),
             "https://www.samsung.com/sec/c/": _Resp(302, location="https://www.samsung.com/us/"),
             "https://www.samsung.com/sec/d/": _Resp(302, location="https://evil.example.com/sec/x/"),
             "https://www.samsung.com/sec/e/": _Resp(302, location="http://www.samsung.com/sec/x/")}
    calls = []
    fake = lambda method, url, **kw: (calls.append(url), pages[url])[1]
    with patched(sk.requests, request=fake), patched(sk, _throttle=lambda: None):
        assert sk._http("GET", "https://www.samsung.com/sec/a/") == (200, "한글")
        for u in ("c", "d", "e"):
            try:
                sk._http("GET", f"https://www.samsung.com/sec/{u}/")
            except sk.SamsungKrPageError:
                continue
            raise AssertionError(f"redirect {u} must be rejected")
    assert "https://evil.example.com/sec/x/" not in calls and "https://www.samsung.com/us/" not in calls


def test_throttle_enforces_min_delay():
    sleeps = []
    clock = [100.0]
    with patched(sk.time, sleep=sleeps.append, monotonic=lambda: clock[0]):
        sk._last_request[0] = 99.7
        sk._throttle()
        assert len(sleeps) == 1 and abs(sleeps[0] - 0.7) < 1e-6 and sk.MIN_DELAY_S >= 1.0
        clock[0] = 105.0
        sk._throttle()
        assert len(sleeps) == 1  # enough time passed: no sleep


def test_net_falls_back_to_browser_and_closes_it():
    events = []

    class Net(sk._Net):
        def _open(self, headless):
            events.append(("open", headless))
            self._page = object()
            self._browser = SimpleNamespace(close=lambda: events.append("browser.close"))
            self._pw = SimpleNamespace(stop=lambda: events.append("pw.stop"))

        def _browser_request(self, method, url, data):
            events.append(("fetch", method))
            return 200, '{"products": []}'

    env = {"FRIDGE_BROWSER_MODE": "headless"}
    with patched(sk, _http=lambda *a, **k: (403, "Access Denied"), _throttle=lambda: None), \
            patched(sk.os, environ={**sk.os.environ, **env}):
        with Net("refrigerators/all-refrigerators/") as net:
            out = net.get("https://www.samsung.com/sec/cxhr/pf/goodsList?x=1", ok=lambda t: t.lstrip().startswith("{"))
    assert out == '{"products": []}'
    assert events[0] == ("open", True) and ("fetch", "GET") in events
    assert events[-2:] == ["browser.close", "pw.stop"]  # closed on exit (finally semantics)


def test_net_never_uses_visible_browser_in_headless_mode_and_raises_when_blocked():
    opened = []

    class Net(sk._Net):
        def _open(self, headless):
            opened.append(headless)
            self._page = object()

        def _browser_request(self, method, url, data):
            return 403, "denied"
    with patched(sk, _http=lambda *a, **k: (403, ""), _throttle=lambda: None), \
            patched(sk.os, environ={**sk.os.environ, "FRIDGE_BROWSER_MODE": "headless"}):
        try:
            Net("x/").get("https://www.samsung.com/sec/y/")
        except sk.SamsungKrPageError:
            pass
        else:
            raise AssertionError("expected SamsungKrPageError")
    assert opened == [True]


def test_net_requires_sec_urls():
    for bad in ("https://www.samsung.com/us/x/", "https://example.com/sec/x/", "http://www.samsung.com/sec/x/"):
        try:
            sk._Net("x/").get(bad)
        except sk.SamsungKrPageError:
            continue
        raise AssertionError(bad)


def test_consent_declines_first_and_accepts_only_when_asked():
    clicked = []

    class Loc:
        def __init__(self, sel):
            self.sel = sel
            self.first = self

        def count(self):
            return 1 if any(w in self.sel for w in ("거부", "동의")) else 0

        def click(self, timeout=None):
            clicked.append(self.sel)
    page = SimpleNamespace(locator=lambda sel: Loc(sel))
    assert sk._consent(page) is True and "거부" in clicked[0] and not any("동의" in c for c in clicked)
    clicked.clear()
    assert sk._consent(page, accept=True) is True and "동의" in clicked[0]


def test_safe_model_and_host_helpers():
    assert sk._safe_model("../../etc/pass wd") == "_.._etc_pass_wd" and not sk._safe_model("..x").startswith(".")
    assert "/" not in sk._safe_model("a/b\\c") and sk._safe_model("RM90H64P2W") == "RM90H64P2W"
    assert sk._is_samsung_url("https://images.samsung.com/a.png") and sk._is_samsung_url("https://samsung.com/sec/")
    assert not sk._is_samsung_url("https://notsamsung.com/") and not sk._is_samsung_url("https://samsung.com.evil.com/")
    assert not sk._is_samsung_url("http://www.samsung.com/") and not sk._is_samsung_url("https://evil.com/?https://www.samsung.com")
    assert sk._https("//images.samsung.com/kdp/a.png") == "https://images.samsung.com/kdp/a.png"
    assert sk._https("//evil.com/a.png") is None and sk._https("") is None and sk._https(None) is None


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
