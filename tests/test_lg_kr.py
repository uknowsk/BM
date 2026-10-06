"""Plain-assert offline tests for lg_kr (saved fixtures, no network, no LLM). Run: python tests/test_lg_kr.py

Fixtures in tests/fixtures/lg_kr were cut from live lge.co.kr responses (listing API rows, facet filter definitions,
PDP flight data with the full spec table, support API)."""
import json
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import ko_en
import lg_kr
from schema import DocumentRecord

FIX = Path(__file__).parent / "fixtures" / "lg_kr"
read = lambda n: (FIX / n).read_text(encoding="utf-8")
jload = lambda n: json.loads(read(n))
rows_of = lambda n: jload(n)["data"]["modelList"]
_HANGUL = re.compile(r"[가-힣]")


def _stub_ko():
    """ko_en with deterministic offline translation: Hangul strings become 'EN[<text>]'; numbers pass through."""
    ns = types.SimpleNamespace(**{k: getattr(ko_en, k) for k in dir(ko_en) if not k.startswith("__")})
    ns.translate_many = lambda texts, kind="value": [f"EN[{t}]" if _HANGUL.search(str(t)) else str(t) for t in texts]
    ns.translate_key = lambda t: ns.translate_many([t], "label")[0]
    return ns


class OfflineKo:
    def __enter__(self):
        self.saved, lg_kr._ko_mod = lg_kr._ko_mod, _stub_ko()
        return self

    def __exit__(self, *a):
        lg_kr._ko_mod = self.saved


def _facets():
    out = {}
    for key, codes in jload("facets_refrigerators.json").items():
        for c in codes:
            d = out.setdefault(c, {})
            if key == "built_in":
                d["install"] = "빌트인 타입"
            else:
                d["doors"] = int(key[-1])
    return out


def _pdp(name, url="https://www.lge.co.kr/x"):
    with OfflineKo():
        return lg_kr.parse_pdp(read(name), url)


# ------------------------------------------------------------------ module contract
def test_constants_and_registry():
    assert (lg_kr.BRAND, lg_kr.COUNTRY, lg_kr.REGION, lg_kr.CURRENCY) == ("LG", "kr", "kr", "KRW")
    assert lg_kr.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys())
    for sub in ("scr", "otr", "gas_oven", "gas_cooktop", "electric_oven", "bottom_freezer"):  # none sold on lge.co.kr
        assert sub not in lg_kr.SUPPORTED_SUBCATEGORIES, sub
    assert {"french_door", "side_by_side", "top_freezer", "built_in", "compact", "top_load", "front_load", "dryer",
            "laundry_center", "microwave", "sco", "induction", "radiant"} == lg_kr.SUPPORTED_SUBCATEGORIES
    for sub, slugs in lg_kr.SUB_SOURCES.items():
        assert slugs and all(lg_kr._LISTING_MAJOR[s] == catalog.major_of(sub) for s in slugs), sub
    assert catalog.module_name("LG", "kr") == "lg_kr"   # auto-discovered, nothing registered by hand
    assert catalog.supported("LG", "kr") == lg_kr.SUPPORTED_SUBCATEGORIES
    assert catalog.region_support("kr")["LG"] == lg_kr.SUPPORTED_SUBCATEGORIES


# ------------------------------------------------------------------ classification
def test_classify_table():
    c = lg_kr.classify
    # fixed sub-categories
    assert c("드럼세탁기") == "front_load" and c("통돌이") == "top_load" and c("건조기") == "dryer"
    assert c("워시타워") == "laundry_center" and c("워시콤보") == "laundry_center"
    assert c("전자레인지") == "microwave" and c("광파오븐") == "sco"
    assert c("인덕션") == "induction" and c("하이브리드") == "radiant"
    # bundles / mini washers / unknown families
    for sub in ("미니세탁기", "세탁기+건조기", "건조기 세트", "전기레인지 세트", "냉장고 세트", "페어설치키트", "김치냉장고", "냉동전용고", "", None):
        assert c(sub) is None, sub
    # refrigerators: type from the LG sub-category, door count/install from facets or the PDP spec table
    assert c("양문형", "LG 디오스 냉장고 (양문형)", doors=2) == "side_by_side"
    assert c("상냉장/하냉동", "x", doors=4) == "french_door" and c("상냉장_하냉동", "x", doors=3) == "french_door"
    assert c("상냉장/하냉동", "x") == "french_door"                     # no facet: 3/4-door layout is the only one LG sells
    assert c("상냉장/하냉동", "x", doors=2) == "bottom_freezer"         # classify can say it; LG KR just has none today
    assert c("STEM", "STEM 베이직 냉장고", doors=4) == "french_door"
    assert c("STEM", "STEM 얼음정수 냉장고 (양문형)", doors=2) == "side_by_side"
    assert c("STEM", "STEM 얼음정수 냉장고 (양문형)") == "side_by_side"   # name carries 양문형 when facets are missing
    assert c("STEM", "STEM 베이직 냉장고 (노크온)") == "french_door"      # ASSUMPTION documented in lg_kr
    assert c("일반형", "LG 일반냉장고", doors=2, liters=317) == "top_freezer"
    assert c("일반형", "LG 일반냉장고", doors=1, liters=195) == "compact"
    assert c("일반형", "LG 일반냉장고", doors=2, liters=121) == "compact" and c("일반형", "x", doors=2, liters=130) == "compact"
    assert c("일반형", "x", doors=2, liters=131) == "top_freezer"
    assert c("일반형", "LG 일반냉장고", liters=90) == "compact"
    # built-in beats every layout; Fit & Max stays freestanding
    assert c("일반형", "모던엣지", doors=2, install="빌트인 타입") == "built_in"
    assert c("상냉장/하냉동", "LG 디오스 오브제컬렉션 빌트인 타입 냉장고") == "built_in"
    assert c("상냉장/하냉동", "x", doors=4, install="Fit & Max") == "french_door"
    # PDP spec 타입 wins over the listing sub-category text
    assert c("STEM", "x", doors=4, type_ko="상냉장/하냉동") == "french_door"
    assert c("일반형", "x", doors=2, type_ko="일반 냉장고", liters=500) == "top_freezer"
    # not refrigerators even though the sub-category is a fridge one
    for name in ("LG 냉동고", "[컨버터블 2대 페어용] 설치키트", "냉장고 + 김치톡톡", "LG 와인셀러"):
        assert c("일반형", name, doors=1) is None, name


def test_classification_is_exclusive_over_all_listing_rows():
    facets = _facets()
    seen: dict[str, list[str]] = {}
    for n in ("plp_refrigerators.json", "plp_washer.json", "plp_cooking.json"):
        for item in rows_of(n):
            sub = lg_kr.classify(**lg_kr._listing_facts(item, facets))
            if sub:
                seen.setdefault(item["salesModelCode"], [])
                if sub not in seen[item["salesModelCode"]]:
                    seen[item["salesModelCode"]].append(sub)
    assert seen and all(len(v) == 1 for v in seen.values()), {k: v for k, v in seen.items() if len(v) != 1}
    got = {m: v[0] for m, v in seen.items()}
    assert got["S834MEE111"] == "side_by_side" and got["M876GBB231"] == "french_door" and got["D502MEE33"] == "top_freezer"
    assert got["B103S14"] == "compact" and got["Q342GBB153"] == "built_in" and got["M622GCB352S"] == "built_in"
    assert got["J816MEE06-B"] == "side_by_side" and got["G646GBB031"] == "french_door"
    for excluded in ("OC-KIT8", "R984GBB012", "DF165ME", "6EW2E-QKBOE"):
        assert excluded not in got, excluded


# ------------------------------------------------------------------ listing
def test_listing_prices_never_use_member_or_card_prices():
    item = next(r for r in rows_of("plp_refrigerators.json") if r["salesModelCode"] == "S834MEE111")
    pi = item["priceInfo"]
    assert (pi["obsOriginalPrice"], pi["obsSellingPrice"]) == (2350000, 1890000)
    assert pi["obsMemberPrice"] == 460000 and pi["lastBenefitPrc"] == 1664700   # present in the data ...
    assert lg_kr.listing_prices(item) == (1890000, 2350000)                       # ... but never used
    assert lg_kr.listing_prices({}) == (None, None)
    assert lg_kr.listing_prices({"priceInfo": {"obsSellingPrice": 0, "obsOriginalPrice": 0}}) == (None, None)


def test_parse_listing_french_door_and_dedupe():
    rows = rows_of("plp_refrigerators.json")
    got = lg_kr.parse_listing(rows, "french_door", _facets())
    models = [c.model_number for c in got]
    assert len(models) == len(set(models))
    assert "EVIL1" not in models                      # off-domain url dropped
    assert models[:2] == ["M876GBB231", "G646GBB031"] and "F756SI014" in models and "S834MEE111" not in models
    c = got[0]
    assert (c.brand, c.category, c.subcategory) == ("LG", "refrigerator", "french_door")
    assert (c.region, c.country, c.currency) == ("kr", "kr", "KRW")
    assert c.price_usd is None and c.price_local == 4550000.0
    assert c.url == "https://www.lge.co.kr/refrigerators/m876gbb231"
    assert c.name == "LG 디오스 AI 오브제컬렉션 냉장고 (더블매직스페이스)"
    a = c.attrs
    assert a["capacity_total_cuft"] == 30.8 and a["kr_grade"] == 1 and a["door_count"] == 4   # 871 L
    assert a["energy_kwh_year"] == 516.0                                                      # 43.0 kWh/month x 12
    assert a["list_price_krw"] == 5051000
    assert set(c.attrs_src) == set(a) and set(c.attrs_src.values()) == {"listing"}
    assert len(lg_kr.parse_listing(rows, "french_door", _facets(), limit=1)) == 1


def test_parse_listing_dedupes_duplicate_rows_and_missing_price():
    rows = rows_of("plp_refrigerators.json")
    sbs = lg_kr.parse_listing(rows, "side_by_side", _facets())
    models = [c.model_number for c in sbs]
    assert models.count("S834MEE111") == 1 and "J816MEE06-B" in models
    top = {c.model_number: c for c in lg_kr.parse_listing(rows, "top_freezer", _facets())}
    assert top["NOPRICE1"].price_local is None and "list_price_krw" not in top["NOPRICE1"].attrs
    assert {c.model_number for c in lg_kr.parse_listing(rows, "compact", _facets())} == {"B103S14", "B124S14", "DL195ME"}
    assert [c.model_number for c in lg_kr.parse_listing(rows, "built_in", _facets())] == ["Q342GBB153", "M622GCB352S"]


def test_parse_listing_washer_and_cooking_attrs():
    w = jload("plp_washer.json")["data"]["modelList"]
    fl = {c.model_number: c for c in lg_kr.parse_listing(w, "front_load")}
    assert set(fl) == {"FX24KNTR"} and fl["FX24KNTR"].attrs["capacity_kg"] == 24.0 and fl["FX24KNTR"].attrs["finish"] == "black"
    assert "capacity_total_cuft" not in fl["FX24KNTR"].attrs      # kg is not convertible to cu ft
    tower = lg_kr.parse_listing(w, "laundry_center")
    assert {c.model_number for c in tower} == {"WA2525SSP6G", "FC2521KX6CX"} and tower[0].attrs["capacity_kg"] == 25.0
    assert [c.model_number for c in lg_kr.parse_listing(w, "dryer")] == ["RD21ESE"]
    assert [c.model_number for c in lg_kr.parse_listing(w, "top_load")] == ["TR15WV5"]
    cook = jload("plp_cooking.json")["data"]["modelList"]
    assert [c.model_number for c in lg_kr.parse_listing(cook, "induction")] == ["BEI3AMBLOE"]
    assert lg_kr.parse_listing(cook, "induction")[0].attrs["burners"] == 3
    rad = lg_kr.parse_listing(cook, "radiant")
    assert [c.model_number for c in rad] == ["BEY3SRBLE"] and rad[0].subcategory == "radiant"
    assert [c.model_number for c in lg_kr.parse_listing(cook, "microwave")] == ["MW20GDN"]
    ov = lg_kr.parse_listing(cook, "sco")
    assert [c.model_number for c in ov] == ["MLJ39KR"] and ov[0].attrs["oven_capacity_cuft"] == 1.38   # 39 L
    allc = [c for s in ("induction", "radiant", "microwave", "sco") for c in lg_kr.parse_listing(cook, s)]
    models = [c.model_number for c in allc]
    assert len(models) == len(set(models))   # no model under two sub keys
    assert "6EW2E-QKBOE" not in {c.model_number for c in allc}   # dishwasher + induction bundle


def test_listing_keyword_facts():
    k = lg_kr._kw_facts
    assert k({"keywds": "832L^1등급"}) == {"liters": 832.0, "grade": 1}
    assert k({"keywds": "25/25kg^1등급"}) == {"kg": 25.0, "grade": 1}
    assert k({"keywds": "25/21/4kg^1등급"}) == {"kg": 25.0, "grade": 1}
    assert k({"keywds": "39L"}) == {"liters": 39.0} and k({"keywds": None}) == {} and k({}) == {}


class FakeFetcher:
    """Serves saved apiv2 answers; records every call."""
    calls: list = []

    def json(self, method, url, body=None):
        FakeFetcher.calls.append((method, url, body))
        if "/plp/category?pageUrl" in url:
            assert "%2Fcategory%2Frefrigerators" in url
            return {"categoryId": "CT50000065"}
        if url.endswith("/filter?b2cYn=Y"):
            return jload("filter_refrigerators.json")["data"]
        if url.endswith("/CT50000065/model"):
            rows = rows_of("plp_refrigerators.json")
            flt = body["productFilterList"]
            if flt:
                kw = flt[0]["keywdList"][0]["keywdId"]
                key = {"KY0000002242": "door4", "KY0000002243": "door3", "KY0000002244": "door2", "KY0000002245": "door1",
                       "KY0000003546": "built_in"}[kw]
                keep = set(jload("facets_refrigerators.json")[key])
                rows = [r for r in rows if r["salesModelCode"] in keep]
            return {"totalModelCnt": len(rows), "modelList": rows if body["page"] == 1 else []}
        raise AssertionError(url)

    def close(self):
        FakeFetcher.closed = True


def test_discover_with_fake_fetcher():
    saved = lg_kr._Fetcher
    lg_kr._Fetcher, lg_kr._CACHE, FakeFetcher.calls, FakeFetcher.closed = FakeFetcher, {}, [], False
    try:
        got = lg_kr.discover("compact", limit=30)
        assert {c.model_number for c in got} == {"B103S14", "B124S14", "DL195ME"}
        assert FakeFetcher.closed                                 # fetcher closed in finally
        facet_ids = {c[2]["productFilterList"][0]["keywdList"][0]["keywdId"] for c in FakeFetcher.calls
                     if c[2] and c[2]["productFilterList"]}
        assert len(facet_ids) == 5                                # door 4/3/2/1 + built-in, resolved by Korean facet names
        n = len(FakeFetcher.calls)
        again = lg_kr.discover("top_freezer", limit=2)            # second call: category id, rows and facets are cached
        assert len(again) == 2 and len(FakeFetcher.calls) == n
        try:
            lg_kr.discover("scr")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for an unsupported sub category")
        try:
            lg_kr.discover("bottom_freezer")
        except ValueError:
            return
        raise AssertionError("bottom_freezer is not offered")
    finally:
        lg_kr._Fetcher, lg_kr._CACHE = saved, {}


# ------------------------------------------------------------------ PDP parsing
def test_rsc_text_and_decoy_references():
    html = read("pdp_fridge_m876gbb231.html")
    text = lg_kr.rsc_text(html)
    assert '"initialSpecData"' in text and text.count('"productInfo"') == 2     # one reference ("$43"), one object
    pi = lg_kr._json_after(text, "productInfo", must="modelSku")
    assert pi["modelSku"] == "M876GBB231.AKOR" and pi["modelId"] == "MD10604870"
    assert lg_kr._json_after(text, "nothing") is None
    assert lg_kr.rsc_text("<html></html>") == ""


def test_pdp_french_door_fields():
    p, raw, info = _pdp("pdp_fridge_m876gbb231.html", "https://www.lge.co.kr/refrigerators/m876gbb231")
    assert (p.brand, p.model_number, p.category, p.subcategory) == ("LG", "M876GBB231", "refrigerator", "french_door")
    assert p.product_name == "LG 디오스 AI 오브제컬렉션 냉장고 (더블매직스페이스)"
    assert p.product_url == "https://www.lge.co.kr/refrigerators/m876gbb231"
    assert (p.region, p.country, p.currency) == ("kr", "kr", "KRW")
    assert p.price_local == 4550000.0 and p.price_usd is None          # sale price; not the 501,000 member discount
    assert p.door_style == "French Door (4-door)"
    assert p.capacity_total_cuft == 30.76 and p.capacity_fridge_cuft == 17.8 and p.capacity_freezer_cuft == 12.96   # 871/504/367 L
    assert (p.width_in, p.height_in, p.depth_in) == (35.98, 73.23, 36.14)                                          # 914x1860x918 mm
    assert p.weight_lb == 377.0                                                                                   # 171 kg
    assert p.voltage_v == "220" and p.frequency_hz == 60.0 and p.amps is None
    assert p.energy_kwh_year == 516.0 and p.energy_star is None          # 43.0 kWh/month x 12; never ENERGY STAR
    assert p.ice_maker is True and p.water_dispenser is False
    assert p.wifi_supported is True and p.wifi_evidence == "LG KR spec table: ThinQ (Wi-Fi) = O"
    assert p.image_url == "https://www.lge.co.kr/kr/images/refrigerators/md10604870/gallery/medium01.jpg"
    assert info["model_id"] == "MD10604870" and info["major"] == "refrigerator"
    ex = p.extra_specs
    # curated keys keep the original metric values
    assert ex["Total capacity (L)"] == "871" and ex["Width (mm)"] == "914" and ex["Height (mm)"] == "1860"
    assert ex["Weight (kg)"] == "171" and ex["Door count"] == "4"
    assert ex["Energy efficiency grade (KR)"] == "KR grade 1" and "ENERGY STAR" not in " ".join(ex)
    assert ex["Monthly energy consumption (kWh/month)"] == "43"
    assert ex[ko_en.ANNUALIZED_LABEL] == "516" and "estimated" in ko_en.ANNUALIZED_LABEL
    assert ex["Sale price (KRW)"] == "4550000" and ex["List price (KRW)"] == "5051000"
    assert "501000" not in ex.values()                                   # member discount is not stored anywhere
    # EVERY spec row is present as 'Section > Label' (translated) and as the untouched Korean RawSpec
    assert len(raw) == 54
    assert all(r.brand == "LG" and r.model_number == "M876GBB231" and r.source == "web" for r in raw)
    assert any(r.section == "용량" and r.key == "전체 용량 (L)" and r.value == "871" for r in raw)
    pref = [k for k in ex if " > " in k]
    assert len(pref) == 54, len(pref)
    assert ex["EN[용량] > EN[전체 용량 (L)]"] == "871"
    assert ex["EN[고지정보] > EN[에너지 소비효율등급]"] == "KR grade 1"       # grade row overridden, not 'Grade 1'
    assert ex["EN[디자인] > EN[색상]"] == "EN[오브제컬렉션 베이지 / 베이지]"
    assert not any(k for k in pref if k.endswith("KC인증조회") or "<" in k)
    # key feature headlines: English in pod_features, Korean originals kept
    assert len(p.pod_features) == 6 and all(f.startswith("EN[") for f in p.pod_features)
    assert ex["Feature (KO) 1"].startswith("딥러닝 기반") and ex["Feature (KO) 6"] == "다양한 공간에 자연스러운 디자인"
    assert not any("선택의 이유" in v or "스마트한 일상" in v or "케어서비스" in v for v in info["headlines_ko"])


def test_pdp_classification_from_spec_table():
    sbs = _pdp("pdp_sbs_s834mee111.html")[0]
    assert sbs.subcategory == "side_by_side" and sbs.door_style == "Side-by-Side"
    assert sbs.extra_specs["Door count"] == "2" and sbs.price_local == 1890000.0
    compact = _pdp("pdp_compact_b053s14.html")[0]      # no '기본 사양' section at all: sub-category + 43 L decide
    assert compact.subcategory == "compact" and compact.capacity_total_cuft == 1.52
    built = _pdp("pdp_builtin_m622gcb352s.html")[0]    # no spec '설치타입' row: the name says 빌트인 타입
    assert built.subcategory == "built_in" and built.door_style == "Built-in"
    assert compact.water_dispenser is None             # no dispenser row -> unknown, not False


def test_pdp_washer_keeps_kg_and_infers_wifi():
    p, raw, info = _pdp("pdp_washer_fx24kntr.html")
    assert (p.category, p.subcategory, p.model_number) == ("washer", "front_load", "FX24KNTR")
    assert p.capacity_total_cuft is None                 # 24 kg drum capacity is not a volume
    assert p.extra_specs["Washer capacity (kg)"] == "24"
    assert (p.width_in, p.height_in, p.depth_in) == (27.56, 38.98, 32.68) and p.weight_lb == 209.4   # 700x990x830 mm, 95 kg
    assert p.door_style is None and p.ice_maker is None and p.water_dispenser is None and p.energy_kwh_year is None
    assert p.wifi_supported is True and p.wifi_evidence == "LG KR spec table (ThinQ): 스마트 원격제어 = O"
    assert p.voltage_v == "220" and p.energy_star is None
    assert p.extra_specs["Energy efficiency grade (KR)"] == "KR grade 1"
    assert len(raw) == 64 and len([k for k in p.extra_specs if " > " in k]) == 64


def test_pdp_cooking_hybrid_is_radiant():
    p, raw, info = _pdp("pdp_cooking_bey3srble.html")
    assert (p.category, p.subcategory) == ("cooking", "radiant") and info["major"] == "cooking"
    assert p.capacity_total_cuft is None and p.price_local == 710000.0
    assert (p.width_in, p.height_in, p.depth_in) == (22.64, 2.13, 20.28)   # 575x54x515 mm product size, NOT the 560x65x480 cut-out
    assert p.extra_specs["Width (mm)"] == "575"
    assert p.wifi_supported is True and "ThinQ = O" in p.wifi_evidence


def test_pdp_unrecognised_or_unsupported():
    for html in ("<html></html>", "<script>self.__next_f.push([1,\"nothing here\"])</script>"):
        try:
            lg_kr.parse_pdp(html, "u")
        except lg_kr.LGKRPageError:
            continue
        raise AssertionError("expected LGKRPageError")
    text = lg_kr.rsc_text(read("pdp_fridge_m876gbb231.html"))
    text = re.sub(r'"modelCategory":\s*"냉장고"', '"modelCategory": "TV"', text)
    text = re.sub(r'"modelSubCategory":\s*"상냉장_하냉동"', '"modelSubCategory": "올레드"', text)
    assert '"modelCategory": "TV"' in text and '"modelSubCategory": "올레드"' in text
    tv = "<script>self.__next_f.push([1," + json.dumps(text) + "])</script>"
    with OfflineKo():
        try:
            lg_kr.parse_pdp(tv, "u")
        except ValueError as e:
            assert "not a supported" in str(e)
        else:
            raise AssertionError("expected ValueError for a non-appliance family")


def test_wifi_rules():
    rows = lambda *r: lg_kr._Rows(list(r))
    assert lg_kr._wifi(rows(("스마트 기능", "ThinQ (Wi-Fi)", "X")), []) == (False, "LG KR spec table: ThinQ (Wi-Fi) = X")
    assert lg_kr._wifi(rows(("ThinQ", "스마트 진단", "O")), []) == (None, None)          # smart diagnosis is audio, not Wi-Fi
    assert lg_kr._wifi(rows(("ThinQ", "스마트 원격제어", "O")), [])[0] is True
    assert lg_kr._wifi(rows(), ["LG ThinQ 앱으로 제품을 손쉽게 관리하세요"])[0] is True


def test_headlines_dedupe_and_stop():
    t = ('<h2 class="x">선택의 이유를 한눈에</h2><h2>냉기 제어</h2><h2>냉기 제어</h2><h2></h2><h2>UP 가전 을 만나보세요</h2>'
         '<h2>쉬운 세탁<br>한눈에</h2><h2>설치가이드 내용 시작</h2><h2>뒤에 오는 것</h2>')
    assert lg_kr.headlines(t) == ["냉기 제어", "쉬운 세탁 한눈에"]
    many = "".join(f"<h2>기능 {i}</h2>" for i in range(30))
    assert len(lg_kr.headlines(many)) == lg_kr.MAX_HEADLINES


def test_clean_helpers():
    assert lg_kr._clean('<table ><tr><td><img src="/a.jpg" alt="KC 인증"></td></tr></table>') == "KC 인증"
    assert lg_kr._clean("스마트 페어링&lt;br&gt;(연동)") == "스마트 페어링 (연동)"
    assert lg_kr._clean('<a href="https://x">조회</a> 안내') == "조회 안내"
    assert lg_kr._clean(None) == "" and lg_kr._clean("a\xa0b") == "a b"
    assert lg_kr._yn("X") is False and lg_kr._yn("O (2)") is True and lg_kr._yn("") is None and lg_kr._yn("없음") is False
    assert lg_kr._n("20(11)") == 20 and lg_kr._n("전체 164 / 세탁 93") == 164 and lg_kr._n("1,860") == 1860 and lg_kr._n("-") is None
    assert lg_kr._unique("a", {"a": 1}) == "a (2)" and lg_kr._unique("a", {"a": 1, "a (2)": 1}) == "a (3)"


# ------------------------------------------------------------------ manuals, security, scrape
def test_parse_manuals_owner_pdfs_only():
    m = lg_kr.parse_manuals(jload("support_manuals.json")["data"])
    assert m == [("Manual", "https://gscs-b2c.lge.com/open/downloadFile?fileId=MANUAL1"),
                 ("Manual 2", "https://gscs-b2c.lge.com/open/downloadFile?fileId=MANUAL2"),
                 ("Manual 3", "https://evil.example.com/open/downloadFile?fileId=EVIL")]   # host is vetted at download time
    assert lg_kr.parse_manuals({}) == [] and lg_kr.parse_manuals({"products": [{"manuals": None}]}) == []
    many = {"products": [{"manuals": [{"type": "OWNER_MANUAL", "files": [
        {"kind": "DOWNLOAD", "type": "PDF", "url": f"https://gscs-b2c.lge.com/x?fileId={i}"}]} for i in range(9)]}]}
    assert len(lg_kr.parse_manuals(many)) == lg_kr.MAX_DOCS


def test_host_checks():
    ok = ["https://www.lge.co.kr/refrigerators/x", "https://lge.co.kr/", "https://apiv2.lge.co.kr/a", "https://www.lg.com/us/x",
          "https://gscs-b2c.lge.com/downloadFile?fileId=1"]
    assert all(lg_kr._page_host_ok(u) for u in ok[:4]) and not lg_kr._page_host_ok(ok[4])   # lge.com is a PDF host only
    assert lg_kr._host_in(ok[4], lg_kr.PDF_HOSTS)
    for bad in ("http://www.lge.co.kr/x", "https://evil-lge.co.kr/x", "https://lge.co.kr.evil.com/x", "https://co.kr/",
                "https://user:pw@www.lge.co.kr/x", "ftp://www.lge.co.kr/x", "https://www.lge.co.kr@evil.com/x", "//www.lge.co.kr/x", ""):
        assert not lg_kr._page_host_ok(bad), bad
        try:
            lg_kr._check_url(bad)
        except lg_kr.LGKRPageError:
            continue
        raise AssertionError(f"_check_url accepted {bad!r}")
    assert not lg_kr._host_in("https://evil.example.com/a.pdf", lg_kr.PDF_HOSTS)
    assert lg_kr._safe_name("../../ev il/M8*76.pdf") == "_.._ev_il_M8_76.pdf"
    assert "/" not in lg_kr._safe_name("../../x") and "\\" not in lg_kr._safe_name("..\\x") and lg_kr._safe_name("") == "unknown"


def test_scrape_with_fakes():
    class F:
        closed = False

        def html(self, url):
            assert url == "https://www.lge.co.kr/microwaves-and-ovens/mlj39kr"
            return "https://www.lge.co.kr/product/microwaves-and-ovens/mlj39kr?modelId=MD10736835", read("pdp_cooking_bey3srble.html")

        def json(self, method, url, body=None):
            assert method == "GET" and "/models/MD10630858/support?category=CT50000064" in url, url
            return jload("support_manuals.json")["data"]

        def close(self):
            F.closed = True

    downloaded = []

    def fake_download(brand, model, doc_type, url):
        downloaded.append((brand, model, doc_type, url))
        sha = "same" if url.endswith("MANUAL2") else url[-8:]
        return DocumentRecord(brand=brand, model_number=model, doc_type=doc_type, source_url=url,
                              local_path=f"downloads/lg/{model}_{doc_type}.pdf", sha256=sha, size_bytes=1, pages=1)

    saved = (lg_kr._Fetcher, lg_kr.download_pdf, lg_kr._polite, lg_kr._check_url)
    lg_kr._Fetcher, lg_kr.download_pdf, lg_kr._polite, lg_kr._check_url = F, fake_download, lambda: None, lambda u: None
    try:
        with OfflineKo():
            p, docs, raw = lg_kr.scrape("https://www.lge.co.kr/microwaves-and-ovens/mlj39kr")
    finally:
        lg_kr._Fetcher, lg_kr.download_pdf, lg_kr._polite, lg_kr._check_url = saved
    assert F.closed and p.model_number == "BEY3SRBLE" and p.product_url == "https://www.lge.co.kr/microwaves-and-ovens/mlj39kr"
    assert [d[2] for d in downloaded] == ["Manual", "Manual 2"]            # evil.example.com never reaches download_pdf
    assert all(d[0] == "LG" and d[1] == "BEY3SRBLE" for d in downloaded)
    assert [d.doc_type for d in docs] == ["Manual", "Manual 2"] and len(raw) == 34


def test_scrape_refuses_foreign_urls_before_any_fetch():
    class Boom:
        def __init__(self):
            raise AssertionError("fetcher must not be created for a refused url")

    saved = lg_kr._Fetcher
    lg_kr._Fetcher = Boom
    try:
        for bad in ("http://www.lge.co.kr/refrigerators/x", "https://evil.example.com/refrigerators/x", "file:///etc/passwd"):
            try:
                lg_kr.scrape(bad)
            except lg_kr.LGKRPageError:
                continue
            raise AssertionError(bad)
    finally:
        lg_kr._Fetcher = saved


# ------------------------------------------------------------------ fetching helpers
def test_browser_modes_and_consent():
    import os
    old = os.environ.get("FRIDGE_BROWSER_MODE")
    try:
        for mode, only, order in (("headless", True, [True]), ("visible", True, [False]), ("auto", False, [True, False]),
                                  ("", False, [True, False])):
            os.environ["FRIDGE_BROWSER_MODE"] = mode
            assert lg_kr._browser_only() is only and lg_kr._browser_modes() == order, mode
        os.environ.pop("FRIDGE_BROWSER_MODE")
        assert lg_kr._browser_only() is False
    finally:
        if old is not None:
            os.environ["FRIDGE_BROWSER_MODE"] = old

    from playwright.sync_api import TimeoutError as PWTimeout

    class Page:
        def __init__(self, present):
            self.present, self.clicked = present, []

        def get_by_role(self, role, name, exact):
            outer = self

            class L:
                @property
                def first(self_):
                    class C:
                        def click(_, timeout):
                            if name not in outer.present:
                                raise PWTimeout("no such button")
                            outer.clicked.append(name)
                    return C()
            return L()

    pg = Page({"모두 거부", "모두 허용"})
    assert lg_kr._consent(pg) is True and pg.clicked == ["모두 거부"]          # decline first
    pg = Page({"모두 허용"})
    assert lg_kr._consent(pg) is False and pg.clicked == []                    # never accepts unless asked
    assert lg_kr._consent(pg, accept=True) is True and pg.clicked == ["모두 허용"]


def test_fetcher_http_blocked_falls_back_to_browser():
    f = lg_kr._Fetcher.__new__(lg_kr._Fetcher)
    f._pw = f._browser = f._page = None
    f._use_browser = False
    log = []

    def blocked(method, url, body=None):
        raise lg_kr._Blocked("HTTP 403")

    f._http = blocked
    f._browser_json = lambda m, u, b: log.append(("browser", u)) or '{"status": 200, "data": {"ok": 1}}'
    f._browser_html = lambda u: log.append(("browser-html", u)) or (u, "<html>x</html>")
    assert f.json("GET", "https://apiv2.lge.co.kr/a") == {"ok": 1} and f._use_browser is True
    assert f.html("https://www.lge.co.kr/b") == ("https://www.lge.co.kr/b", "<html>x</html>")
    assert [x[0] for x in log] == ["browser", "browser-html"]
    f._browser_json = lambda m, u, b: '{"status": 500, "data": null}'
    try:
        f.json("GET", "https://apiv2.lge.co.kr/a")
    except lg_kr.LGKRPageError:
        pass
    else:
        raise AssertionError("bad envelope accepted")
    f._browser_json = lambda m, u, b: "<html>not json</html>"
    try:
        f.json("GET", "https://apiv2.lge.co.kr/a")
    except lg_kr.LGKRPageError:
        return
    raise AssertionError("non-JSON accepted")


def test_polite_delay():
    import time
    lg_kr._polite()
    t0 = time.monotonic()
    lg_kr._polite()
    assert time.monotonic() - t0 >= lg_kr.DELAY_S - 0.05


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"{len(fns)} passed")
