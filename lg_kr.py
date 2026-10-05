"""LG Korea adapter: refrigerators, washers/dryers and cooking (https://www.lge.co.kr).

Platform facts (found by network inspection, 2026-10; lge.co.kr is a Next.js app router site, NOT lg.com/us):
  discover(): the category pages are filled by a plain JSON API on apiv2.lge.co.kr (no token, no browser needed):
      GET  /plpsvc/ajax/v1/plp/category?pageUrl=/category/<slug>        -> categoryId (CT...)
      POST /plpsvc/ajax/v1/plp/category/<CT>/model  {"page": n, "b2cYn": "Y", ...}   -> 30 models per page + totalModelCnt
      GET  /plpsvc/ajax/v1/plp/category/<CT>/filter?b2cYn=Y             -> facet ids (door count, install type)
    A listing row has the Korean sub-category (양문형, 드럼세탁기, 인덕션 ...), the name, price info, and keywds like
    "832L^1등급". Door count / install type exist only as facets, so for refrigerators discover() also runs one facet
    query per door count + built-in and caches them (10 min) -> classify() sees the same facts as the PDP spec table.
  scrape(): the PDP (/product/<cat>/<model>) is server-rendered; everything is in the inline Next flight data
      (self.__next_f.push): productInfo (ids, names, prices), initialSpecData (the spec table: sections of
      name/value rows), og:image, and the marketing <h2> headlines. Manuals come from
      GET /pdpsvc/ajax/v1/models/<MD id>/support?category=<CT super category> (PDF files on gscs-b2c.lge.com).
Prices: LG KR shows list price (정가), discounted sale price (판매가/할인가), member/coupon/card prices and a
  "max benefit price". Only the sale price is stored in price_local; the list price is kept as the extra spec
  'List price (KRW)'. Card/member/coupon/max-benefit prices are never read.
Fetching: plain HTTP first; FRIDGE_BROWSER_MODE = auto (HTTP, then headless, then visible Playwright when blocked)
  | headless | visible (browser only). Geo: no geo-redirect observed from the dev network; /<cat>/<model> redirects
  (301/302) to /product/<cat>/<model>?modelId=..., which is followed hop by hop with a host check.
Korean -> English comes from ko_en (imported lazily). Not offered by LG KR online (checked against the whole
  sitemap + llms.txt, and the category slugs gas-ranges / gas-oven-ranges / ranges 404): otr, gas_oven, electric_oven
  (no over-the-range hoods, gas ranges or plain wall ovens; the only ovens are 광파오븐 = sco).
"""
import html as _html
import json
import os
import re
import sys
import time
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

import catalog
import common
import units
from catalog import Candidate
from common import UA, download_pdf, launch_browser, looks_blocked
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "LG"
COUNTRY = "kr"
REGION = "kr"
CURRENCY = "KRW"
BASE = "https://www.lge.co.kr"
API = "https://apiv2.lge.co.kr"
MAX_DOCS = 4
DELAY_S = 1.0              # minimum gap between any two requests
MAX_LIST_PAGES = 20        # safety bound per category (30 models per page)
MAX_REDIRECTS = 5
CACHE_TTL_S = 600
COMPACT_MAX_L = 130        # ASSUMPTION: a 일반형 refrigerator up to 130 L (or any single-door unit) is "compact"
MAX_HEADLINES = 8

PAGE_HOSTS = ("lge.co.kr", "lg.com")                  # pages / API hosts we may talk to (exact suffix match)
PDF_HOSTS = ("lge.com", "lg.com", "lge.co.kr")        # gscs-b2c.lge.com serves the manuals
IMAGE_HOSTS = PDF_HOSTS

_HEADERS = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9", "Origin": BASE, "Referer": BASE + "/"}


class LGKRPageError(RuntimeError):
    """LG KR page/API did not have the expected structure (site changed) or was refused."""


class _Blocked(LGKRPageError):
    """Bot protection / rate limit answered (403, 429, challenge text)."""


# ---------------------------------------------------------------- hosts, names, politeness
def _host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    h = (u.hostname or "").lower()
    return u.scheme == "https" and not (u.username or u.password) and any(h == s or h.endswith("." + s) for s in suffixes)


def _page_host_ok(url: str) -> bool:
    return _host_in(url, PAGE_HOSTS)


def _check_url(url: str) -> None:
    """https + lge.co.kr/lg.com (exact suffix) + public IP; anything else is refused before any request."""
    if not _page_host_ok(url):
        raise LGKRPageError(f"refusing url outside lge.co.kr/lg.com over https: {url[:120]}")
    problem = common._url_problem(url, PAGE_HOSTS)
    if problem:
        raise LGKRPageError(f"refusing url ({problem}): {url[:120]}")


def _safe_name(m: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", m).strip(".") or "unknown"


_last_request = 0.0


def _polite() -> None:
    global _last_request
    wait = DELAY_S - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _clean(s: Any) -> str:
    """Plain one-line text: <br> -> space, <img alt> -> alt, other tags dropped, entities decoded."""
    t = "" if s is None else str(s)
    t = _html.unescape(t)
    t = re.sub(r"<br\s*/?>", " ", t, flags=re.I)
    t = re.sub(r"""<img\b[^>]*\balt=["']([^"']*)["'][^>]*>""", r" \1 ", t, flags=re.I)
    t = re.sub(r"<[^>]*>", " ", t)
    return " ".join(t.split())


_ko_mod = None


def _ko():
    """ko_en, imported on first use (it is written/maintained separately)."""
    global _ko_mod
    if _ko_mod is None:
        import ko_en
        _ko_mod = ko_en
    return _ko_mod


# ---------------------------------------------------------------- classification (single source of truth)
# sub key -> LG KR listing categories (url slugs) that can contain it
SUB_SOURCES: dict[str, tuple[str, ...]] = {
    "french_door": ("refrigerators",), "side_by_side": ("refrigerators",), "top_freezer": ("refrigerators",),
    "built_in": ("refrigerators",), "compact": ("refrigerators",),
    "top_load": ("washing-machines",), "front_load": ("washing-machines",), "dryer": ("dryers",),
    "laundry_center": ("wash-tower", "wash-combo"),
    "microwave": ("microwaves-and-ovens",), "sco": ("microwaves-and-ovens",),
    "induction": ("electric-ranges",), "radiant": ("electric-ranges",),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)
# Not offered by LG KR: otr (over-the-range), gas_oven (no gas range), electric_oven (no plain wall oven; the
# 광파오븐 microwave+light-wave ovens are sco) and bottom_freezer (every
# 상냉장/하냉동 model is 3- or 4-door = French-door layout; classify() still returns bottom_freezer for a 2-door one).

_LISTING_MAJOR = {"refrigerators": "refrigerator", "washing-machines": "washer", "dryers": "washer",
                  "wash-tower": "washer", "wash-combo": "washer", "electric-ranges": "cooking",
                  "microwaves-and-ovens": "cooking"}

# LG KR sub-category (소분류) that maps to exactly one sub key
_FIXED_SUB = {
    "드럼세탁기": "front_load", "통돌이": "top_load", "건조기": "dryer",   # NOT 미니세탁기 / 세탁기+건조기 / 건조기 세트
    "워시타워": "laundry_center", "워시콤보": "laundry_center",          # ASSUMPTION: both stacked/all-in-one = laundry_center
    "전자레인지": "microwave",
    "광파오븐": "sco",              # light-wave oven (microwave + light-wave/convection) = Speed Cook Oven
    "인덕션": "induction",          # includes 포터블 인덕션 (portable induction)
    "하이브리드": "radiant",        # ASSUMPTION: hybrid = induction zones + 라디언트(radiant) zone; excluded from induction
                                    # (a 하이라이트/라디언트 electric range would also be radiant; LG KR lists none)
}                                   # (전기레인지 세트 = dishwasher + induction bundles are not classified)
_FRIDGE_SUBS = {"양문형", "상냉장/하냉동", "일반형", "STEM"}   # STEM is a lineup, not a door layout
_NOT_A_FRIDGE = re.compile(r"설치\s*키트|세트|김치|냉동고|와인|\+")     # kits, bundles, freezer-only, kimchi, wine
_TYPE_ORDER = (("양문형", "양문"), ("상냉장/하냉동", "상냉장"), ("상냉장/하냉동", "하냉동"), ("일반형", "일반"))


def _norm_sub(s: Any) -> str:
    return " ".join(str(s or "").replace("_", "/").split())


def _fridge_type(*texts: Any) -> Optional[str]:
    """양문형 | 상냉장/하냉동 | 일반형 from a listing sub-category, PDP spec '타입' value or name; None if unknown."""
    for t in texts:
        t = _norm_sub(t)
        for canon, needle in _TYPE_ORDER:
            if needle in t:
                return canon
    return None


def classify(lg_sub: Any, name: Any = "", *, doors: Optional[int] = None, install: Any = None,
             liters: Optional[float] = None, type_ko: Any = None) -> Optional[str]:
    """Catalog sub key for one LG KR product, or None when it is not a supported single appliance (kits, bundles,
    freezers, kimchi, mini washers ...). The ONLY classification rule: discover() feeds it listing facts and
    scrape() feeds it the PDP's, so a model never lands under two subs. Exclusive by construction (first match)."""
    sub = _norm_sub(lg_sub)
    if sub in _FIXED_SUB:
        return _FIXED_SUB[sub]
    if sub not in _FRIDGE_SUBS:
        return None
    name = _clean(name)
    if _NOT_A_FRIDGE.search(name):
        return None
    inst = _norm_sub(install)
    if ("빌트인" in inst and "Fit" not in inst) or "빌트인" in name:   # Fit & Max is flush-fit, still freestanding
        return "built_in"
    t = _fridge_type(type_ko) or _fridge_type(sub if sub != "STEM" else "", name if "양문형" in name else "")
    if t == "양문형":
        return "side_by_side"
    if t == "상냉장/하냉동" or (t is None and doors in (3, 4)):
        return "bottom_freezer" if doors is not None and doors <= 2 else "french_door"   # LG: 4도어 = 상냉장/하냉동
    if t == "일반형" or (t is None and doors in (1, 2)):
        return "compact" if doors == 1 or (liters is not None and liters <= COMPACT_MAX_L) else "top_freezer"
    if t is None and doors is None and sub == "STEM":
        return "french_door"   # ASSUMPTION: a STEM refrigerator whose facets are missing and whose name is not 양문형 is 4-door
    return None


# ---------------------------------------------------------------- fetching (HTTP first, Playwright fallback)
def _browser_modes() -> list[bool]:
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return [True]
    if mode == "visible":
        return [False]
    return [True, False]


def _browser_only() -> bool:
    return os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower() in ("headless", "visible")


_REJECT = ("모두 거부", "모두 거절", "거부", "필수만 허용", "필수 쿠키만 허용", "동의 안 함", "Reject all", "Decline")
_ACCEPT = ("모두 허용", "모두 수락", "모두 동의", "동의", "Accept all", "Accept")


def _consent(pg, accept: bool = False) -> bool:
    """Best-effort cookie banner click: decline non-essential by default, accept only when the page is blocked."""
    for label in (_ACCEPT if accept else _REJECT):
        try:
            pg.get_by_role("button", name=label, exact=True).first.click(timeout=1200)
            return True
        except PlaywrightTimeoutError:
            continue
        except Exception:  # noqa: BLE001 - a banner click must never break scraping
            continue
    return False


_FETCH_JS = """async ({url, method, body}) => {
  const r = await fetch(url, {method, credentials: 'omit',
    headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
  return {status: r.status, text: await r.text()};
}"""


class _Fetcher:
    """HTTP for everything; when blocked (or FRIDGE_BROWSER_MODE forces it) the same requests run inside a real
    Chromium (headless first, then visible). One instance per discover()/scrape() call; close() in a finally."""

    def __init__(self):
        self._pw = self._browser = self._page = None
        self._use_browser = _browser_only()

    # -- plain HTTP
    def _http(self, method: str, url: str, body=None) -> tuple[str, str]:
        cur = url
        for _ in range(MAX_REDIRECTS + 1):
            _check_url(cur)
            _polite()
            r = requests.request(method, cur, json=body, headers=_HEADERS, timeout=60, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                cur, method, body = urljoin(cur, r.headers["Location"]), "GET", None
                continue
            if r.status_code in (403, 429):
                raise _Blocked(f"HTTP {r.status_code} from {cur[:100]}")
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            return cur, r.text
        raise LGKRPageError(f"more than {MAX_REDIRECTS} redirects from {url[:100]}")

    # -- browser
    def _open(self, headless: bool) -> None:
        self.close()
        self._pw = sync_playwright().start()
        self._browser = launch_browser(self._pw, headless=headless)
        self._page = self._browser.new_context(user_agent=UA, locale="ko-KR").new_page()
        self._page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)   # origin for in-page fetch + cookies
        _check_url(self._page.url)
        _consent(self._page)

    def close(self) -> None:
        for obj, fn in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, fn)()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._browser = self._page = None

    def _browser_run(self, work):
        last: Exception | None = None
        for headless in _browser_modes():
            try:
                if self._page is None:
                    self._open(headless)
                return work(self._page)
            except Exception as e:  # noqa: BLE001 - blocked / navigation failure -> next mode
                last = e
                print(f"lg_kr: browser (headless={headless}) failed: {e}", file=sys.stderr)
                self.close()
        raise LGKRPageError(f"browser fetch failed: {last}")

    def _browser_html(self, url: str) -> tuple[str, str]:
        def work(pg):
            _check_url(url)
            _polite()
            resp = pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            _check_url(pg.url)
            _consent(pg)
            try:
                pg.wait_for_function("document.documentElement.innerHTML.includes('initialSpecData')", timeout=20000)
            except PlaywrightTimeoutError:
                _consent(pg, accept=True)   # a banner may be blocking content; accept only now
                pg.wait_for_timeout(3000)
            _check_url(pg.url)
            html = pg.content()
            if looks_blocked(resp.status if resp else None, pg.inner_text("body")):
                raise _Blocked(f"blocked in browser at {url[:100]}")
            return pg.url, html
        return self._browser_run(work)

    def _browser_json(self, method: str, url: str, body) -> str:
        def work(pg):
            _check_url(url)
            _polite()
            res = pg.evaluate(_FETCH_JS, {"url": url, "method": method, "body": body})
            if res["status"] in (403, 429):
                raise _Blocked(f"HTTP {res['status']} in browser from {url[:100]}")
            if res["status"] >= 400:
                raise LGKRPageError(f"HTTP {res['status']} from {url[:100]}")
            return res["text"]
        return self._browser_run(work)

    # -- public
    def html(self, url: str) -> tuple[str, str]:
        """(final_url, html). Final url is host-checked on every hop."""
        if not self._use_browser:
            try:
                final, text = self._http("GET", url)
                if looks_blocked(200, text):
                    raise _Blocked("page looks like a bot challenge")
                return final, text
            except _Blocked as e:
                print(f"lg_kr: {e}; falling back to browser", file=sys.stderr)
                self._use_browser = True
        return self._browser_html(url)

    def json(self, method: str, url: str, body=None) -> dict:
        """Unwrapped `data` of an apiv2 response ({'status': 200, 'data': ...})."""
        text = None
        if not self._use_browser:
            try:
                text = self._http(method, url, body)[1]
            except _Blocked as e:
                print(f"lg_kr: {e}; falling back to browser", file=sys.stderr)
                self._use_browser = True
        if text is None:
            text = self._browser_json(method, url, body)
        try:
            j = json.loads(text)
        except ValueError as e:
            raise LGKRPageError(f"non-JSON answer from {url[:100]}") from e
        if not isinstance(j, dict) or j.get("status") != 200 or "data" not in j:
            raise LGKRPageError(f"unexpected API envelope from {url[:100]}: {str(j)[:100]}")
        return j["data"]


# ---------------------------------------------------------------- discover
_CACHE: dict[str, tuple[float, Any]] = {}


def _cached(key: str, make):
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    val = make()
    _CACHE[key] = (time.monotonic(), val)
    return val


def _category_id(f: _Fetcher, slug: str) -> str:
    def make():
        data = f.json("GET", f"{API}/plpsvc/ajax/v1/plp/category?pageUrl=%2Fcategory%2F{slug}")
        cid = data.get("categoryId") if isinstance(data, dict) else None
        if not isinstance(cid, str) or not re.fullmatch(r"CT\d+", cid):
            raise LGKRPageError(f"no categoryId for /category/{slug}")
        return cid
    return _cached("cat:" + slug, make)


def _model_body(page: int, filters: Optional[list] = None) -> dict:
    return {"sortType": "sort_pick", "mltpModelFilterFlag": "", "tomorrowDeliveryFilterFlag": "",
            "spaceModelFilterFlag": "", "upApplianceFilterFlag": "", "highEfficiencyFilterFlag": "",
            "empFilterType": "", "lineupId": "", "subCategoryId": "", "carePromotionBadgeFilterList": [],
            "subCategoryFilterList": [], "colorFilterList": [], "tagFilterList": [], "brandFilterList": [],
            "productFilterList": filters or [], "priceFilter": {}, "compSetFilter": {}, "page": page, "b2cYn": "Y"}


def _list_models(f: _Fetcher, cid: str, filters: Optional[list] = None) -> list[dict]:
    """Every listing row of a category (optionally facet-filtered), paged 30 at a time, deduped by model code."""
    out: dict[str, dict] = {}
    for page in range(1, MAX_LIST_PAGES + 1):
        data = f.json("POST", f"{API}/plpsvc/ajax/v1/plp/category/{cid}/model", _model_body(page, filters))
        rows = data.get("modelList") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise LGKRPageError("listing answer has no modelList (site changed?)")
        for r in rows:
            if isinstance(r, dict) and r.get("salesModelCode"):
                out.setdefault(r["salesModelCode"], r)
        if not rows or len(out) >= int(data.get("totalModelCnt") or 0):
            break
    return list(out.values())


def _facet_filters(f: _Fetcher, cid: str) -> dict[str, list]:
    """{'door4': [filter], ..., 'built_in': [filter]} resolved by Korean facet names (ids may change)."""
    data = f.json("GET", f"{API}/plpsvc/ajax/v1/plp/category/{cid}/filter?b2cYn=Y")
    wanted = {"도어 개수": {"4도어": "door4", "3도어": "door3", "2도어": "door2", "1도어": "door1"},
              "설치타입": {"빌트인 타입": "built_in"}}
    out: dict[str, list] = {}
    for fl in (data.get("productsFilterList") or []) if isinstance(data, dict) else []:
        for kw in fl.get("attrKeywdList") or []:
            key = wanted.get(fl.get("attrNm"), {}).get(kw.get("keywdNm"))
            if key:
                out[key] = [{"attrId": kw["attrId"], "keywdList": [{"keywdId": kw["keywdId"],
                                                                      "inputModeCode": kw.get("inputModeCode") or "01"}]}]
    return out


def _fridge_facets(f: _Fetcher, cid: str) -> dict[str, dict]:
    """model code -> {'doors': n, 'install': '빌트인 타입'} from facet queries (cached)."""
    def make():
        filters = _facet_filters(f, cid)
        if "door4" not in filters:
            print("lg_kr: door-count facets not found; refrigerator classification falls back to names", file=sys.stderr)
        facts: dict[str, dict] = {}
        for key, flt in filters.items():
            for row in _list_models(f, cid, flt):
                d = facts.setdefault(row["salesModelCode"], {})
                if key == "built_in":
                    d["install"] = "빌트인 타입"
                else:
                    d["doors"] = int(key[-1])
        return facts
    return _cached("facets:" + cid, make)


_KW_L = re.compile(r"^(\d+(?:\.\d+)?)\s*L$", re.I)
_KW_KG = re.compile(r"^(\d+(?:\.\d+)?)(?:/\d+(?:\.\d+)?)*\s*kg$", re.I)
_KW_GRADE = re.compile(r"([1-5])\s*등급")
_FINISH_KO = (("블랙 스테인", "black_stainless"), ("스테인", "stainless"), ("화이트", "white"), ("블랙", "black"))


def _kw_facts(item: dict) -> dict:
    """Listing 'keywds' ('832L^1등급', '25/25kg^1등급', '39L') -> liters / kg / KR grade."""
    out: dict = {}
    for tok in str(item.get("keywds") or "").split("^"):
        tok = tok.strip()
        if (m := _KW_L.match(tok)):
            out["liters"] = float(m.group(1))
        elif (m := _KW_KG.match(tok)):
            out["kg"] = float(m.group(1))
        elif (m := _KW_GRADE.search(tok)):
            out["grade"] = int(m.group(1))
    return out


def _attr(item: dict, name: str) -> str:
    for a in item.get("productAttrKeywds") or []:
        if isinstance(a, dict) and a.get("attrNm") == name and a.get("keywds"):
            return str(a["keywds"])
    return ""


def _price_int(v: Any) -> Optional[int]:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def listing_prices(item: dict) -> tuple[Optional[int], Optional[int]]:
    """(sale price, list price) in KRW. Member / coupon / card / max-benefit prices are deliberately never read."""
    normal = ((item.get("priceArea") or {}).get("normal") or {})
    info = item.get("priceInfo") or {}
    return (_price_int(normal.get("price")) or _price_int(info.get("obsSellingPrice")),
            _price_int(normal.get("originPrice")) or _price_int(info.get("obsOriginalPrice")))


def listing_attrs(item: dict, sub: str, doors: Optional[int] = None) -> dict:
    """Candidate.attrs in standard units (cu ft, kWh/yr, kg); only what the listing really states."""
    kw = _kw_facts(item)
    a: dict = {}
    if "liters" in kw and catalog.major_of(sub) == "refrigerator":
        a["capacity_total_cuft"] = round(units.l_to_cuft(kw["liters"]), 1)
    if "liters" in kw and sub == "sco":
        a["oven_capacity_cuft"] = round(units.l_to_cuft(kw["liters"]), 2)
    if "kg" in kw:
        a["capacity_kg"] = kw["kg"]
    if "grade" in kw:
        a["kr_grade"] = kw["grade"]
    if doors:
        a["door_count"] = doors
    m = re.search(r"(\d+(?:\.\d+)?)\s*kWh\s*/\s*월", _attr(item, "소비전력"), re.I)
    if m:
        try:
            a["energy_kwh_year"] = _ko().annualize(float(m.group(1)))
        except ImportError:
            pass
    color = _attr(item, "색상")
    fin = next((k for needle, k in _FINISH_KO if needle in color), None)
    if fin:
        a["finish"] = fin
    n = sum(int(x) for x in re.findall(r"(\d)\s*구", _attr(item, "화구")))
    if n:
        a["burners"] = n
    sale, lst = listing_prices(item)
    if lst:
        a["list_price_krw"] = lst
    return a


def _listing_facts(item: dict, facets: dict) -> dict:
    fx = facets.get(item.get("salesModelCode"), {})
    return {"lg_sub": item.get("subCategoryName"), "name": item.get("modelDisplayName"), "doors": fx.get("doors"),
            "install": fx.get("install"), "liters": _kw_facts(item).get("liters")}


def parse_listing(rows: list[dict], sub: str, facets: Optional[dict] = None, limit: int = 30) -> list[Candidate]:
    """Listing rows -> Candidates that classify() files under `sub` (deduped by model code, off-domain urls dropped)."""
    out, seen = [], set()
    for item in rows:
        code = item.get("salesModelCode")
        path = item.get("modelUrlPath") or ""
        if not code or not path or code in seen:
            continue
        facts = _listing_facts(item, facets or {})
        if classify(**facts) != sub:
            continue
        url = BASE + path if path.startswith("/") else path
        if not _page_host_ok(url):
            print(f"lg_kr: dropped off-domain candidate {code}: {url[:100]}", file=sys.stderr)
            continue
        seen.add(code)
        sale, _ = listing_prices(item)
        attrs = listing_attrs(item, sub, facts["doors"])
        out.append(Candidate(brand=BRAND, model_number=code, name=_clean(item.get("modelDisplayName")) or code, url=url,
                             category=catalog.major_of(sub), subcategory=sub, region=REGION, country=COUNTRY,
                             currency=CURRENCY, price_usd=None, price_local=float(sale) if sale else None,
                             attrs=attrs, attrs_src={k: "listing" for k in attrs}))
        if len(out) >= limit:
            break
    return out


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"LG KR adapter does not support sub category {subcategory!r}")
    f = _Fetcher()
    found: dict[str, Candidate] = {}
    try:
        for slug in SUB_SOURCES[subcategory]:
            cid = _category_id(f, slug)
            rows = _cached("rows:" + cid, lambda: _list_models(f, cid))
            facets = _fridge_facets(f, cid) if slug == "refrigerators" else {}
            for c in parse_listing(rows, subcategory, facets, limit):
                found.setdefault(c.model_number, c)
            if len(found) >= limit:
                break
    finally:
        f.close()
    print(f"lg_kr: discover({subcategory}): kept {min(len(found), limit)}", file=sys.stderr)
    return list(found.values())[:limit]


# ---------------------------------------------------------------- PDP parsing
_PUSH = re.compile(r"self\.__next_f\.push\(\[1,")
_DEC = json.JSONDecoder()


def rsc_text(html: str) -> str:
    """Concatenated string payloads of the inline Next flight data (self.__next_f.push([1, "..."]))."""
    parts = []
    for m in _PUSH.finditer(html):
        try:
            s, _ = _DEC.raw_decode(html, m.end())
        except ValueError:
            continue
        if isinstance(s, str):
            parts.append(s)
    return "".join(parts)


def _json_after(text: str, key: str, must: Optional[str] = None) -> Optional[dict]:
    """First object that follows '"key":' (skipping '$43'-style references); `must` = a key it has to contain."""
    marker = f'"{key}":'
    i = text.find(marker)
    while i >= 0:
        j = i + len(marker)
        if text[j:j + 1] == "{":
            try:
                obj, _ = _DEC.raw_decode(text, j)
                if isinstance(obj, dict) and (must is None or must in obj):
                    return obj
            except ValueError:
                pass
        i = text.find(marker, j)
    return None


_MAJOR_OF_CATEGORY = {"냉장고": "refrigerator", "세탁기": "washer", "의류건조기": "washer", "워시타워": "washer",
                      "워시콤보": "washer", "전기레인지": "cooking", "광파오븐/전자레인지": "cooking"}
_SUPER_CATEGORY = {"refrigerator": "CT50000064", "cooking": "CT50000064", "washer": "CT50000100"}
_GENERIC_HEADLINE = re.compile(r"선택의 이유|보다 더 스마트|함께 사용하면|한눈에 비교|UP 가전|ThinQ 앱|전모델|설치\s*가이드|케어\s*서비스|"
                               r"구독|구매 시 유의|고객지원|혜택\s*안내|자주 묻는|무상\s*A/S|무상보증")


def headlines(text: str) -> list[str]:
    """Marketing key-feature headlines (<h2> of the overview), deduped, generic section titles dropped."""
    out: list[str] = []
    for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", text, re.S):
        h = _clean(m.group(1))
        if not h:
            continue
        if re.search(r"설치\s*가이드|케어\s*서비스|구매 시 유의|고객지원", h):
            break   # everything after is purchase/support boilerplate
        if _GENERIC_HEADLINE.search(h) or h in out:
            continue
        out.append(h)
        if len(out) >= MAX_HEADLINES:
            break
    return out


def _n(s: Any) -> Optional[float]:
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(s or ""))
    return float(m.group(0).replace(",", "")) if m else None


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(round(x, 2))


def _yn(v: Any) -> Optional[bool]:
    v = _clean(v)
    if not v:
        return None
    if v.upper() in ("X", "N", "없음", "미지원", "해당없음", "해당 없음", "-"):
        return False
    return True


_NO_SPACE = re.compile(r"\s+")


class _Rows:
    """Spec rows [(section, label, value)] in Korean with label lookups that ignore whitespace."""

    def __init__(self, rows: list[tuple[str, str, str]]):
        self.rows = rows

    def get(self, *labels: str) -> Optional[str]:
        want = [_NO_SPACE.sub("", w) for w in labels]
        for w in want:
            for _, k, v in self.rows:
                if _NO_SPACE.sub("", k) == w:
                    return v
        return None

    def find(self, pred) -> Optional[tuple[str, str, str]]:
        return next((r for r in self.rows if pred(_NO_SPACE.sub("", r[1]))), None)


def _spec_rows(unit: dict) -> list[tuple[str, str, str]]:
    rows = []
    for sec in unit.get("sections") or []:
        title = _clean(sec.get("title"))
        for it in sec.get("items") or []:
            label, value = _clean(it.get("name")), _clean(it.get("value"))
            if label and value:
                rows.append((title, label, value))
    return rows


def _wifi(rows: _Rows, heads: list[str]) -> tuple[Optional[bool], Optional[str]]:
    """(supported, evidence) from the Korean spec table; Wi-Fi/ThinQ/remote-control rows only."""
    r = rows.find(lambda k: k in ("ThinQ", "ThinQ(Wi-Fi)", "Wi-Fi", "WiFi", "와이파이"))
    if r:
        yn = _yn(r[2])
        return yn, f"LG KR spec table: {r[1]} = {r[2]}"
    for sec, k, v in rows.rows:
        if "thinq" in sec.lower() and re.search(r"원격제어|다이렉트 페어링|에너지 모니터링|상태 모니터링|다운로드", k) and _yn(v):
            return True, f"LG KR spec table ({sec}): {k} = {v}"
    for h in heads:
        if re.search(r"ThinQ|와이파이|Wi-?Fi", h, re.I):
            return True, f"LG KR feature headline: {h}"
    return None, None


_DOOR_STYLE = {"french_door": "French Door", "side_by_side": "Side-by-Side", "top_freezer": "Top Freezer",
               "bottom_freezer": "Bottom Freezer", "built_in": "Built-in", "compact": "Compact"}


def _unique(key: str, taken: dict) -> str:
    if key not in taken:
        return key
    n = 2
    while f"{key} ({n})" in taken:
        n += 1
    return f"{key} ({n})"


def parse_pdp(html: str, url: str) -> tuple[ProductRecord, list[RawSpec], dict]:
    """-> (ProductRecord, [RawSpec with the ORIGINAL Korean], info{'model_id','major','headlines_ko'}).
    Raises LGKRPageError if the page is not a recognisable LG KR PDP, ValueError for a product family this
    adapter does not cover."""
    text = rsc_text(html)
    pi = _json_after(text, "productInfo", must="modelSku")
    sd = _json_after(text, "initialSpecData", must="units")
    if not pi or not sd or not isinstance(sd.get("units"), list) or not sd["units"]:
        raise LGKRPageError("PDP lacks productInfo/initialSpecData (site changed or blocked?)")
    unit = next((u for u in sd["units"] if u.get("key") == pi.get("modelId")), sd["units"][0])
    model = str(pi["modelSku"]).split(".")[0].strip()
    if not model:
        raise LGKRPageError("PDP has no model code")
    rows_ko = _spec_rows(unit)
    if not rows_ko:
        raise LGKRPageError("PDP spec table is empty")
    rows = _Rows(rows_ko)
    cat_ko = _norm_sub(pi.get("modelCategory"))
    sub_ko = _norm_sub(pi.get("modelSubCategory"))
    name = _clean(pi.get("modelName")) or model

    doors = int(_n(rows.get("도어 개수")) or 0) or None
    liters_total = _n(rows.get("전체 용량 (L)"))
    sub = classify(sub_ko, name, doors=doors, install=rows.get("설치타입"), liters=liters_total, type_ko=rows.get("타입"))
    major = catalog.major_of(sub) if sub else _MAJOR_OF_CATEGORY.get(cat_ko)
    if not major:
        raise ValueError(f"not a supported LG KR product (category {cat_ko!r}/{sub_ko!r})")
    fridge = major == "refrigerator"

    heads_ko = headlines(text)
    ko = _ko()
    labels_en = ko.translate_many([lab for _, lab, _ in rows_ko] + sorted({s for s, _, _ in rows_ko}), "label")
    lab_en, sec_en = labels_en[:len(rows_ko)], dict(zip(sorted({s for s, _, _ in rows_ko}), labels_en[len(rows_ko):]))
    vals_en = ko.translate_many([v for _, _, v in rows_ko] + heads_ko, "value")
    val_en, heads_en = vals_en[:len(rows_ko)], vals_en[len(rows_ko):]

    extra: dict[str, str] = {}
    for (sec, lab, val), le, ve in zip(rows_ko, lab_en, val_en):
        if re.fullmatch(r"[1-5]\s*등급", val) and (grade := ko.kr_energy_grade(val)):
            ve = grade   # 'KR grade N', never an ENERGY STAR equivalent
        extra[_unique(f"{sec_en[sec]} > {le}", extra)] = ve
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=s, key=k, value=v) for s, k, v in rows_ko]

    # --- typed fields -----------------------------------------------------------------------------------
    cur: dict[str, str] = {}   # curated, unprefixed English keys (original metric values kept)

    def liters(*labels):
        v = _n(rows.get(*labels))
        return v

    l_total = liters("전체 용량 (L)", "용량 (가용용량)(L)", "용량(L)")
    l_fridge, l_freezer = liters("냉장 용량 (L)"), liters("냉동 용량 (L)")
    if l_total is not None:
        cur["Total capacity (L)"] = _fmt(l_total)
    if l_fridge is not None:
        cur["Fresh food capacity (L)"] = _fmt(l_fridge)
    if l_freezer is not None:
        cur["Freezer capacity (L)"] = _fmt(l_freezer)
    for key, lab in (("Washer capacity (kg)", "세탁 용량 (kg)"), ("Dryer capacity (kg)", "건조 용량 (kg)")):
        if (v := _n(rows.get(lab))) is not None:
            cur[key] = _fmt(v)

    size = rows.find(lambda k: "크기" in k and "mm" in k and not re.search(r"열림|타공|내부", k))
    dims = ko.parse_dimensions(size[2]) if size else None
    w_in = h_in = d_in = None
    if dims:
        w_in, h_in, d_in = (round(units.mm_to_in(x), 2) for x in dims)
        cur.update({"Width (mm)": _fmt(dims[0]), "Height (mm)": _fmt(dims[1]), "Depth (mm)": _fmt(dims[2])})
    wt = rows.find(lambda k: k.startswith("무게"))
    weight_kg = _n(wt[2]) if wt else None
    if weight_kg is not None:
        cur["Weight (kg)"] = _fmt(weight_kg)
    if doors:
        cur["Door count"] = str(doors)

    volt = rows.find(lambda k: k.startswith("정격전압"))
    v_m = re.search(r"(\d{3})\s*V", volt[2]) if volt else None
    hz_m = re.search(r"(\d{2})\s*Hz", volt[2], re.I) if volt else None

    kwh_row = rows.find(lambda k: "kWh/월" in k)
    kwh_year = None
    if kwh_row and (m := _n(kwh_row[2])) is not None:
        kwh_year = ko.annualize(m)
        cur["Monthly energy consumption (kWh/month)"] = _fmt(m)
        cur[ko.ANNUALIZED_LABEL] = _fmt(kwh_year)
    grade_row = rows.get("에너지 소비효율등급")
    if grade_row:
        grades = re.findall(r"[1-5]\s*등급", grade_row)
        cur["Energy efficiency grade (KR)"] = (ko.kr_energy_grade(grade_row) or grade_row) if len(grades) <= 1 else \
            ko.translate_many([grade_row])[0]

    ice_rows = [v for s, k, v in rows_ko if "아이스메이커" in k or "제빙" in k]
    ice = None if not ice_rows else any(_yn(v) for v in ice_rows)
    disp = rows.get("디스펜서")
    wifi, wifi_ev = _wifi(rows, heads_ko)

    sale, lst = (_price_int(pi.get("obsSellingPrice")) or _price_int(_n(pi.get("discountedPrice"))),
                 _price_int(pi.get("obsOriginalPrice")))
    if sale:
        cur["Sale price (KRW)"] = str(sale)
    if lst:
        cur["List price (KRW)"] = str(lst)
    for i, h in enumerate(heads_ko, 1):
        cur[f"Feature (KO) {i}"] = h

    color = rows.get("색상") or next((v for s, k, v in rows_ko if k.startswith("색상")), None)         or rows.get("외관 색상", "글라스컬러", "도어 색상")
    color_en = val_en[next(i for i, r in enumerate(rows_ko) if r[2] == color)] if color else None

    og = re.search(r'"property":"og:image","content":"(https://[^"]+)"', text)
    ld = re.search(r'"@type":"Product"[^\n]{0,6000}?"image":"(https://[^"]+)"', text)
    image = next((u for u in (og and og.group(1), ld and ld.group(1)) if u and _host_in(u, IMAGE_HOSTS)), None)

    product = ProductRecord(
        brand=BRAND, model_number=model, product_name=name, product_url=url, category=major, subcategory=sub,
        door_style=(f"French Door ({doors}-door)" if sub == "french_door" and doors else _DOOR_STYLE.get(sub)) if fridge else None,
        finish_color=color_en,
        price_usd=None, region=REGION, country=COUNTRY, currency=CURRENCY, price_local=float(sale) if sale else None,
        capacity_total_cuft=round(units.l_to_cuft(l_total), 2) if l_total is not None and major != "washer" else None,
        capacity_fridge_cuft=round(units.l_to_cuft(l_fridge), 2) if l_fridge is not None and fridge else None,
        capacity_freezer_cuft=round(units.l_to_cuft(l_freezer), 2) if l_freezer is not None and fridge else None,
        width_in=w_in, height_in=h_in, depth_in=d_in,
        weight_lb=round(units.kg_to_lb(weight_kg), 1) if weight_kg is not None else None,
        voltage_v=v_m.group(1) if v_m else None, frequency_hz=float(hz_m.group(1)) if hz_m else None,
        energy_kwh_year=kwh_year, energy_star=None,
        ice_maker=ice if fridge else None,
        water_dispenser=(None if disp is None else _yn(disp)) if fridge else None,
        wifi_supported=wifi, wifi_evidence=wifi_ev,
        pod_features=heads_en, extra_specs={**extra, **cur}, image_url=image,
    )
    info = {"model_id": str(pi.get("modelId") or ""), "major": major, "headlines_ko": heads_ko}
    return product, raw, info


# ---------------------------------------------------------------- manuals
def parse_manuals(data: dict) -> list[tuple[str, str]]:
    """Support API `data` -> [(doc_type, https PDF url)]: OWNER_MANUAL PDFs only ('UNKNOWN' catalogues and the
    ZIP/HTML 'live' manuals are skipped), unique urls, at most MAX_DOCS. doc_type 'Manual', 'Manual 2', ..."""
    urls: list[str] = []
    for prod in (data.get("products") or []) if isinstance(data, dict) else []:
        for m in prod.get("manuals") or []:
            if m.get("type") != "OWNER_MANUAL":
                continue
            for fl in m.get("files") or []:
                url = fl.get("url")
                if fl.get("type") == "PDF" and fl.get("kind") == "DOWNLOAD" and isinstance(url, str) and url not in urls:
                    urls.append(url)
    return [("Manual" if i == 0 else f"Manual {i + 1}", u) for i, u in enumerate(urls[:MAX_DOCS])]


# ---------------------------------------------------------------- scrape
def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    _check_url(url)
    f = _Fetcher()
    try:
        final_url, html = f.html(url)
        _check_url(final_url)
        product, raw, info = parse_pdp(html, url)
        links: list[tuple[str, str]] = []
        mid = info["model_id"]
        if re.fullmatch(r"MD\d+", mid):
            try:
                links = parse_manuals(f.json("GET", f"{API}/pdpsvc/ajax/v1/models/{mid}/support?category="
                                                    f"{_SUPER_CATEGORY[info['major']]}"))
            except Exception as e:  # noqa: BLE001 - manuals are optional
                print(f"lg_kr: skipped manuals for {product.model_number}: {e}", file=sys.stderr)
    finally:
        f.close()
    docs: list[DocumentRecord] = []
    for dt, link in links:
        if not _host_in(link, PDF_HOSTS):
            print(f"lg_kr: skipped {dt}: PDF url not https on an allowed host ({link[:100]})", file=sys.stderr)
            continue
        _polite()
        d = download_pdf(BRAND, _safe_name(product.model_number), dt, link)
        if not d:
            continue
        if any(x.sha256 == d.sha256 for x in docs):   # LG lists the same manual under several file ids
            (common.ROOT / d.local_path).unlink(missing_ok=True)
            continue
        docs.append(d)
    return product, docs, raw
