"""Samsung Korea adapter (https://www.samsung.com/sec/): refrigerators, laundry and cooking (contract: catalog.py).

Data sources (all plain HTTP, no rendered-DOM scraping; samsung.com/sec is its own platform, not samsung.com/us):
  discover: GET  /sec/cxhr/pf/goodsList?dispClsfNo=<code>&rows=200&...  (the listing page's own XHR, JSON; carries the
            general sale price 'salePrice', so unlike the visible listing it does expose KRW prices).
            36010000 refrigerators, 100043932 laundry (washers, dryers, combos/washtowers), 36030000 cooking
            (microwaves, Qooker/built-in ovens, hoods, induction), 36070000 induction cooktops.
            One code spans several sub keys, so every item is classified by classify() (section / slug / name / model).
  scrape:   GET  <pdp url>                          og:title (name, model), og:image, goodsId, feature headlines (h2)
            POST /sec/xhr/goods/getGoodsSpecList    spec table, EVERY row (HTML fragment: section <dt>, rows
                                                     .spec-title / .spec-desc)
            POST /sec/xhr/goods/goodsRevampDetail   purchase panel: sale price, list price, sale status, goods type
            POST /sec/xhr/goods/goodsManual         user manual / quick guide PDFs (downloadcenter.samsung.com)
Translation (Korean -> English labels/values/headlines) is delegated to ko_en (imported lazily); RawSpec keeps the
original Korean. Prices: Samsung's general sale price only (no card/membership/coupon benefit price).
Geo: no consent banner was seen on /sec; an IP-based redirect away from /sec/ is treated as an error (never mix
another country's data). Politeness: >= MIN_DELAY_S between any two requests.
"""
import html as _html
import json
import os
import re
import sys
import time
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests

import catalog
import units
from catalog import Candidate
from common import ROOT, UA, download_pdf, launch_browser, looks_blocked
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Samsung"
COUNTRY = "kr"
REGION = "kr"
CURRENCY = "KRW"
BASE = "https://www.samsung.com"
SEC = "/sec/"
GOODS_LIST_URL = BASE + SEC + "cxhr/pf/goodsList"
XHR_GOODS = BASE + SEC + "xhr/goods/"
ROWS = 200
MIN_DELAY_S = 1.0  # between any two requests (plain or browser)
PAGE_CACHE_TTL_S = 120.0
MAX_PDFS = 4
MAX_REDIRECTS = 5
MAX_FEATURES = 12
HEADERS = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
ON_SALE = "12"  # saleStatCd: 12 on sale; 15/17 sold out / temporarily out of stock (price 0 or stale)


class SamsungKrPageError(RuntimeError):
    """Raised when a Samsung KR page/API does not have the expected structure (or leaves /sec/)."""


# ---------------------------------------------------------------- ko_en (lazy; written separately)
def _ko():
    import ko_en  # imported on first use only so this module (and its tests) load without it
    return ko_en


# ---------------------------------------------------------------- url / name safety
def _model(code: str) -> str:
    return code.replace("/", "").strip()


def _safe_model(model: str) -> str:
    """Model number safe to use in a filename."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", model).strip(".")


def _is_samsung_url(url: str) -> bool:
    """https URL whose host is samsung.com or a subdomain (exact, dot-anchored suffix match)."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return parts.scheme == "https" and not parts.username and (host == "samsung.com" or host.endswith(".samsung.com"))


def _is_sec_url(url: str) -> bool:
    """A Samsung https URL inside the Korean site (/sec/...)."""
    return _is_samsung_url(url) and (urlsplit(url).path or "").startswith(SEC)


def _require_sec(url: str) -> None:
    if not _is_sec_url(url):
        raise SamsungKrPageError(f"left the Korean Samsung site (geo redirect or foreign url): {url!r}")


def _clean(text) -> str:
    """Whitespace/nbsp-collapsed, HTML-unescaped text."""
    return " ".join(_html.unescape(str(text or "")).replace("\xa0", " ").split())


def _https(url: str | None) -> str | None:
    """Protocol-relative / absolute url -> https url on a Samsung host, else None."""
    if not isinstance(url, str) or not url.strip():
        return None
    full = urljoin(BASE + SEC, url.strip())
    full = re.sub(r"^http://", "https://", full)
    return full if _is_samsung_url(full) else None


# ---------------------------------------------------------------- sub-category map
_FRIDGE, _LAUNDRY, _COOK, _INDUCTION = "36010000", "100043932", "36030000", "36070000"

# sub key -> (major, [goodsList dispClsfNo codes]). Edit the mapping here only.
# NOT offered by Samsung KR (verified live 2026-10: the cooking list 36030000 holds only induction cooktops, Qooker /
# compact / built-in ovens, microwaves and one hood; no gas range / gas cooktop (re-checked 2026-10-06: no 가스 product in the list, no gas category in the consumer sitemap), no
# radiant/highlight range): otr, gas_oven, gas_cooktop, radiant.
# ASSUMPTIONS: built_in = built-in fridges (빌트인/셰프컬렉션/model BR*) + Bespoke
# 1-door 키친핏 fridge/freezer columns (fridge-only; freezer-only units are skipped); compact = one-door fridges
# up to 200 L; laundry_center = washtower (원바디) and all-in-one washer-dryer combo (콤보, 건조 겸용);
# sco = Qooker (큐커 오븐/멀티: oven + microwave) and 콤팩트 오븐 (compact oven + microwave combi);
# electric_oven = built-in electric ovens without microwave (빌트인 전기오븐); microwave = microwave ovens.
SUB_SOURCES: dict[str, tuple[str, list[str]]] = {
    "french_door": ("refrigerator", [_FRIDGE]),
    "side_by_side": ("refrigerator", [_FRIDGE]),
    "top_freezer": ("refrigerator", [_FRIDGE]),
    "bottom_freezer": ("refrigerator", [_FRIDGE]),
    "built_in": ("refrigerator", [_FRIDGE]),
    "compact": ("refrigerator", [_FRIDGE]),
    "top_load": ("washer", [_LAUNDRY]),
    "front_load": ("washer", [_LAUNDRY]),
    "dryer": ("washer", [_LAUNDRY]),
    "laundry_center": ("washer", [_LAUNDRY]),
    "microwave": ("cooking", [_COOK]),
    "sco": ("cooking", [_COOK]),
    "electric_oven": ("cooking", [_COOK]),
    "induction": ("cooking", [_INDUCTION, _COOK]),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)
UNSUPPORTED_SUBCATEGORIES = {"otr", "gas_oven", "gas_cooktop", "radiant"}

_LISTING_PAGE = {"refrigerator": "refrigerators/all-refrigerators/", "washer": "washers-and-dryers/all-washers-and-dryers/",
                 "cooking": "cooking-appliances/all-cooking-appliances/"}
_SECTION_MAJOR = {"refrigerators": "refrigerator", "washing-machines": "washer", "dryers": "washer",
                  "laundry-combo": "washer", "electric-range": "cooking", "micro-wave-ovens": "cooking",
                  "qooker-multi-ovens": "cooking"}
_FRIDGE_SLUGS = (("french-door", "french_door"), ("side-by-side", "side_by_side"), ("top-mount-freezer", "top_freezer"),
                 ("top-freezer", "top_freezer"), ("bottom-mount-freezer", "bottom_freezer"))
DOOR_STYLES = {"french_door": "French Door", "side_by_side": "Side-by-Side", "top_freezer": "Top Freezer",
               "bottom_freezer": "Bottom Freezer"}
_LITER = re.compile(r"(?<![\d/.])(\d{2,4}(?:\.\d)?)\s*l(?![a-z])")  # run on NFKC-lowercased text ('ℓ' -> 'l')
_KG = re.compile(r"(?<![\d/.])(\d{1,2}(?:\.\d)?)(?:/\d{1,2}(?:\.\d)?)?\s*kg")


def _norm_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name or "").lower()


def classify(section: str, slug: str, name: str = "", model: str = "") -> str | None:
    """THE single classifier (discover and scrape both call it): sub key, or None when the product is outside the
    supported groups (bundles, commercial/wine/kimchi/freezer-only units, hoods, ...). Rules are exclusive and
    ordered; no model can land under two subs. section/slug = first/second path segments after /sec/."""
    n, s, m = _norm_name(name), (slug or "").lower(), (model or "").upper()
    if section == "refrigerators":
        if re.search(r"업소용|와인|김치|냉동고|냉동전용", n):
            return None  # commercial, wine, kimchi, freezer-only
        if "빌트인" in n or "셰프컬렉션" in n or m.startswith("BR"):
            return "built_in"
        for prefix, sub in _FRIDGE_SLUGS:
            if s.startswith(prefix):
                return sub
        if s.startswith("one-door"):
            if "키친핏" in n:
                return "built_in"
            liters = _LITER.search(n)
            return "compact" if liters and float(liters.group(1)) <= 200 else None
        return None
    if section == "dryers":
        return "dryer"
    if section in ("washing-machines", "laundry-combo"):
        if (section == "laundry-combo" or s.startswith(("onebody-", "combo-"))
                or re.search(r"콤보|원바디|워시타워|건조 ?겸용|세탁건조", n)):
            return "laundry_center"
        if s.startswith("top-loader") or m.startswith("WA") or re.search(r"통버블|통돌이", n):
            return "top_load"
        return "front_load" if m.startswith(("WF", "WW")) or "세탁기" in n else None
    if section == "electric-range":
        return "induction" if "인덕션" in n else None
    if section == "micro-wave-ovens":
        return "microwave"
    if section == "qooker-multi-ovens":
        if re.search(r"큐커|콤팩트 ?오븐", n):
            return "sco"  # Qooker / compact combi oven: oven + microwave
        if re.search(r"전기오븐|빌트인 ?오븐", n):
            return "electric_oven"
        return "microwave" if "전자레인지" in n else None
    return None


def _is_bundle(slug: str, goods_tp: str | None) -> bool:
    """Two-product packages (washer + dryer + kit, fridge + freezer ...) are goodsTpCd 20 with a 'package-' slug.
    (Some single products carry a 'package-' slug with goodsTpCd 10 and some real washtowers are tp 20: not bundles.)"""
    return str(goods_tp) == "20" and (slug or "").lower().startswith("package-")


def _path_parts(path: str) -> tuple[str, str, str]:
    """('section', 'slug', 'MODEL') from '/sec/<section>/<slug>/<MODEL>/' or 'section/slug/MODEL/'."""
    segs = [x for x in urlsplit(path).path.split("/") if x]
    if segs and segs[0] == SEC.strip("/"):
        segs = segs[1:]
    segs += [""] * (3 - len(segs))
    return segs[0], segs[1], segs[2]


# ---------------------------------------------------------------- HTTP (requests first, browser fallback)
_last_request = [0.0]


def _throttle() -> None:
    wait = MIN_DELAY_S - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.monotonic()


def _http(method: str, url: str, data: dict | None = None, referer: str | None = None) -> tuple[int, str]:
    """One request; redirects are followed by hand and every hop must stay inside https samsung.com/sec/."""
    _require_sec(url)
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    if method == "POST":
        headers.update({"X-Requested-With": "XMLHttpRequest", "Origin": BASE})
    cur, meth, body = url, method, data
    for _hop in range(MAX_REDIRECTS + 1):
        _throttle()
        r = requests.request(meth, cur, data=body, headers=headers, timeout=30, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            cur = urljoin(cur, r.headers["Location"])
            _require_sec(cur)
            meth, body = "GET", None
            continue
        r.encoding = "utf-8"
        return r.status_code, r.text
    raise SamsungKrPageError(f"more than {MAX_REDIRECTS} redirects for {url}")


def _consent(page, accept: bool = False) -> bool:
    """Click a cookie/consent banner button: decline/necessary-only first; accept only when asked (it blocks)."""
    words = ("동의", "수락", "Accept", "Allow") if accept else ("거부", "필수", "Reject", "Decline", "Necessary")
    for w in words:
        try:
            loc = page.locator(f"button:visible:has-text('{w}')").first
            if loc.count():
                loc.click(timeout=2000)
                return True
        except Exception:  # noqa: BLE001 - playwright error types vary; banner handling is best effort
            continue
    return False


_BROWSER_FETCH_JS = """async ({u, m, d}) => {
  const o = {method: m, redirect: 'manual', credentials: 'include', headers: {'X-Requested-With': 'XMLHttpRequest'}};
  if (m === 'POST') { o.body = new URLSearchParams(d); o.headers['Content-Type'] = 'application/x-www-form-urlencoded'; }
  const r = await fetch(u, o);
  if (r.type === 'opaqueredirect') return {s: 'redirect', t: ''};
  return {s: r.status, t: await r.text()};
}"""


class _Net:
    """HTTP for one discover()/scrape() run. Plain requests first (except FRIDGE_BROWSER_MODE=visible); when a
    response is not usable (status/shape check fails: Akamai 403 etc.) the same request is replayed from inside a real
    browser page on the Samsung origin (same cookies/TLS as a visitor). Mode (read per call): auto = requests,
    headless, visible; headless = requests, headless; visible = visible only. The browser is closed in __exit__."""

    def __init__(self, page_path: str, use_requests: bool = True):
        self.page_url = urljoin(BASE + SEC, page_path)
        self.use_requests = use_requests
        self._pw = self._browser = self._page = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        for obj, method in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, method)()
            except Exception:  # noqa: BLE001 - shutting down; nothing useful to do with a teardown failure
                pass
        self._pw = self._browser = self._page = None

    def _open(self, headless: bool):
        from playwright.sync_api import sync_playwright
        self.close()
        self._pw = sync_playwright().start()
        self._browser = launch_browser(self._pw, headless=headless)
        page = self._browser.new_context(user_agent=UA, locale="ko-KR").new_page()
        _throttle()
        page.goto(self.page_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        _require_sec(page.url)
        if not _consent(page) and looks_blocked(None, page.inner_text("body")):
            _consent(page, accept=True)  # decline first; accept only when the banner actually blocks the page
        self._page = page

    def _browser_request(self, method, url, data):
        _throttle()
        res = self._page.evaluate(_BROWSER_FETCH_JS, {"u": url, "m": method, "d": data or {}})
        if res.get("s") == "redirect":
            raise SamsungKrPageError(f"unexpected redirect for {url} (geo?)")
        return res["s"], res["t"]

    def request(self, method: str, url: str, data: dict | None = None, ok=None, referer: str | None = None) -> str:
        """Response text. ok(text) -> bool is the shape check; None accepts any 2xx."""
        _require_sec(url)
        mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
        good = lambda st, t: isinstance(st, int) and 200 <= st < 300 and (ok is None or ok(t))
        if self.use_requests and mode != "visible":
            try:
                st, text = _http(method, url, data, referer or self.page_url)
            except requests.RequestException:
                st, text = None, ""
            if good(st, text):
                return text
        for headless in {"headless": [True], "visible": [False]}.get(mode, [True, False]):
            try:
                if self._page is None:
                    self._open(headless)
                st, text = self._browser_request(method, url, data)
            except SamsungKrPageError:
                raise
            except Exception as e:  # noqa: BLE001 - playwright raises its own error types
                print(f"samsung_kr: playwright headless={headless} failed: {e}", file=sys.stderr)
                self.close()
                continue
            if good(st, text):
                return text
            print(f"samsung_kr: browser headless={headless} got status {st}", file=sys.stderr)
            self.close()
        raise SamsungKrPageError(f"could not load {url} (blocked or unexpected response; mode={mode})")

    def get(self, url, ok=None, referer=None) -> str:
        return self.request("GET", url, None, ok, referer)

    def post(self, url, data, ok=None, referer=None) -> str:
        return self.request("POST", url, data, ok, referer)


# ---------------------------------------------------------------- discover
def _goods_list_url(code: str, page: int) -> str:
    return (f"{GOODS_LIST_URL}?searchFilter=&dispClsfNo={code}&sortType=20&page={page}&rows={ROWS}&ehcacheYn=Y"
            "&soldOutExceptYn=N&pfFasterUseYn=Y&secApp=false&secIos=false&aiscCtgYn=N&tcPlantCode=&onlyAiscGoods=N")


_LIST_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _clear_cache() -> None:
    _LIST_CACHE.clear()


def _goods_list(net: _Net, code: str) -> list[dict]:
    """All products of one category code (sold-out ones included: 'sale_status' tells them apart); cached
    PAGE_CACHE_TTL_S so sub keys sharing a code do not re-request it."""
    hit = _LIST_CACHE.get(code)
    if hit and time.monotonic() - hit[0] < PAGE_CACHE_TTL_S:
        return hit[1]
    out: list[dict] = []
    page = 1
    while True:
        text = net.get(_goods_list_url(code, page), ok=lambda t: t.lstrip().startswith("{"))
        try:
            payload = json.loads(text)
            products, count = payload["products"], int(payload.get("count") or 0)
        except (ValueError, KeyError, TypeError) as e:
            raise SamsungKrPageError(f"goodsList {code}: unexpected payload ({e!r})") from e
        out += products
        if not products or len(out) >= count or len(products) < ROWS:
            break
        page += 1
    _LIST_CACHE[code] = (time.monotonic(), out)
    return out


_FINISH = (("black_stainless", r"블랙\s*(?:스테인리스|스틸|이녹스)"), ("stainless", r"스테인리스|스틸|이녹스|메탈|실버|inox"),
           ("white", r"화이트"), ("black", r"블랙|캐비어"), ("slate", r"차콜|슬레이트"))


def _finish_ko(opt: str | None) -> str | None:
    """Finish key from goodsOptStr ('1|1001|..|색상|클린 화이트|...' -> the colour name after '색상')."""
    parts = (opt or "").split("|")
    color = parts[parts.index("색상") + 1] if "색상" in parts and parts.index("색상") + 1 < len(parts) else ""
    return next((k for k, pat in _FINISH if re.search(pat, color)), None)


def _listing_attrs(item: dict, sub: str, name: str) -> tuple[dict, dict]:
    """Listing facts under the filter keys (cu ft / kg / KR grade ...); only what the listing actually states."""
    n = _norm_name(name)
    attrs: dict = {}
    src: dict = {}

    def put(key, value, how):
        attrs[key], src[key] = value, how

    liters = _LITER.search(n)
    if liters and catalog.major_of(sub) == "refrigerator":
        put("capacity_total_cuft", round(units.l_to_cuft(float(liters.group(1))), 1), "name")
    if liters and sub in ("sco", "electric_oven"):
        put("oven_capacity_cuft", round(units.l_to_cuft(float(liters.group(1))), 2), "name")
    kg = _KG.search(n)
    if kg and catalog.major_of(sub) == "washer":
        put("capacity_kg", float(kg.group(1)), "name")  # combos/washtowers 'A/Bkg': A = washing capacity
    grade = re.search(r"에너지\s*(\d)\s*등급", n)
    if grade and 1 <= int(grade.group(1)) <= 5:
        put("kr_grade", int(grade.group(1)), "name")
    if "패밀리허브" in n:
        put("wifi", True, "name")
        put("screen", True, "name")
    finish = _finish_ko(item.get("goodsOptStr"))
    if finish:
        put("finish", finish, "listing")
    put("sale_status", "on_sale" if str(item.get("saleStatCd")) == ON_SALE else "sold_out", "listing")
    for key, value in _signals(item.get("reviewGrade"), item.get("reviewCount"), item.get("flagStr")).items():
        put(key, value, "listing")  # the site's own review summary / NEW flag (5-point scale already)
    return attrs, src


_NEW_FLAG = re.compile(r"^\s*(?:new|신제품|신상품)\s*$", re.I)


def _signals(rating, count, flag) -> dict:
    """Consumer-response / newness signals the site itself publishes; absent or empty values are left out."""
    out: dict = {}
    try:
        n, r = int(float(count)), float(rating)
    except (TypeError, ValueError):
        n = r = None
    if n and n > 0 and r is not None and 0 < r <= 5:
        out["rating"], out["review_count"] = round(r, 2), n
    if isinstance(flag, str) and _NEW_FLAG.match(flag):
        out["is_new"] = True
    return out


def parse_goods_list(products: list[dict], sub: str | None = None, stats: dict | None = None) -> list[Candidate]:
    """goodsList products -> Candidates (off-domain / non-/sec urls dropped, bundles skipped). With `sub`, only items
    classified as that sub key. `stats` accumulates skipped counts: invalid, off_domain, bundle, unclassified, other."""
    st = stats if stats is not None else {}
    bump = lambda k: st.__setitem__(k, st.get(k, 0) + 1)
    out = []
    for item in products:
        code, path = item.get("mdlCode"), item.get("goodsDetailUrl")
        if not code or not path:
            bump("invalid")
            continue
        url = urljoin(BASE + SEC, str(path).rstrip("/") + "/")
        if not _is_sec_url(url):
            bump("off_domain")
            continue
        section, slug, _ = _path_parts(url)
        if _is_bundle(slug, item.get("goodsTpCd")):
            bump("bundle")
            continue
        name = _clean(item.get("goodsNm")) or code
        found = classify(section, slug, name, code)
        if sub is not None and found != sub:
            bump("unclassified" if found is None else "other")
            continue
        if found is None:
            bump("unclassified")
            continue
        price = item.get("salePrice")
        on_sale = str(item.get("saleStatCd")) == ON_SALE and isinstance(price, (int, float)) and price > 0
        attrs, src = _listing_attrs(item, found, name)
        out.append(Candidate(brand=BRAND, model_number=_model(code), name=name, url=url, price_usd=None,
                             category=catalog.major_of(found), subcategory=found, region=REGION, country=COUNTRY,
                             currency=CURRENCY, price_local=int(price) if on_sale else None,
                             attrs=attrs, attrs_src=src))
    return out


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUB_SOURCES:
        raise ValueError(f"unsupported subcategory {subcategory!r}")
    major, codes = SUB_SOURCES[subcategory]
    seen: dict[str, Candidate] = {}
    stats: dict = {}
    with _Net(_LISTING_PAGE[major]) as net:
        for code in codes:
            for c in parse_goods_list(_goods_list(net, code), subcategory, stats):
                seen.setdefault(c.model_number, c)
    site_order = list(seen.values())  # sortType=20 = registration date, newest first (verified 2026-10-09)
    if len(codes) == 1:  # several category codes have no single order, so no rank there
        catalog.stamp_newest_order(site_order)  # before the on-sale reorder below
    ranked = sorted(site_order, key=lambda c: c.price_local is None)  # on-sale first, stable otherwise
    print(f"samsung_kr: discover({subcategory}): {len(ranked)} found, returning {min(len(ranked), limit)}; skipped "
          f"unclassified {stats.get('unclassified', 0)}, other sub keys {stats.get('other', 0)}, bundles "
          f"{stats.get('bundle', 0)}, off-domain {stats.get('off_domain', 0)}, invalid {stats.get('invalid', 0)}",
          file=sys.stderr)
    return ranked[:limit]


# ---------------------------------------------------------------- parsing: spec table / PDP / panel / manuals
class _SpecParser(HTMLParser):
    """Spec fragment: <dl><dt>Section</dt><dd><ol><li> <strong|button class=spec-title>Label</..> <p class=spec-desc>
    Value</p></li>... Quoted attribute values (button help text may contain '>') are handled by html.parser."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, str, str]] = []
        self.section = ""
        self._mode: str | None = None  # 'dt' | 'title' | 'desc'
        self._buf: list[str] = []
        self._label = ""
        self._script = False

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class") or ""
        if tag in ("script", "style"):
            self._script = True
        elif tag == "dt":
            self._mode, self._buf = "dt", []
        elif tag in ("strong", "button") and "spec-title" in cls.split():
            self._mode, self._buf = "title", []
        elif tag == "p" and "spec-desc" in cls.split():
            self._mode, self._buf = "desc", []
        elif tag == "br" and self._mode:
            self._buf.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._script = False
        elif tag == "dt" and self._mode == "dt":
            self.section = _clean("".join(self._buf))
            self._mode = None
        elif tag in ("strong", "button") and self._mode == "title":
            self._label = _clean("".join(self._buf))
            self._mode = None
        elif tag == "p" and self._mode == "desc":
            value = _clean("".join(self._buf))
            if self._label and value:
                self.rows.append((self.section, self._label, value))
            self._label, self._mode = "", None

    def handle_data(self, data):
        if self._mode and not self._script:
            self._buf.append(data)


def parse_spec_html(fragment: str) -> list[tuple[str, str, str]]:
    """EVERY spec row as (section, label, value), Korean original, in page order."""
    p = _SpecParser()
    p.feed(fragment)
    p.close()
    if not p.rows:
        raise SamsungKrPageError("spec table: no rows found")
    return p.rows


def parse_pdp(page_html: str, url: str) -> dict:
    """PDP page -> {name, model, goods_id, image_url, features (Korean headlines)}."""
    title = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', page_html)
    if not title:
        raise SamsungKrPageError("PDP: no og:title (page structure changed?)")
    parts = [x.strip() for x in _clean(title.group(1)).split(" | ")]
    name = parts[0]
    model = parts[1] if len(parts) >= 3 else _path_parts(url)[2]
    gid = re.search(r'goodsId\s*[:=]\s*["\'](G\d+)["\']', page_html) or re.search(r'data-goods-id="(G\d+)"', page_html)
    if not gid:
        raise SamsungKrPageError("PDP: no goodsId")
    img = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]*)"', page_html)
    out = {"name": name, "model": model, "goods_id": gid.group(1), "image_url": _https(img.group(1)) if img else None,
           "features": parse_features(page_html)}
    agg = re.search(r'"aggregateRating"\s*:\s*\{[^{}]*"ratingValue"\s*:\s*"?([\d.]+)"?[^{}]*"ratingCount"\s*:\s*"?(\d+)"?',
                    page_html)  # JSON-LD of the PDP
    if agg:
        out.update(_signals(agg.group(1), agg.group(2), None))
    return out


_BOILERPLATE_H2 = re.compile(r"레이어 팝업|설치\s*(?:비\s*)?가이드|Highlights", re.I)


def parse_features(page_html: str) -> list[str]:
    """Key feature headlines: the <h2> titles of the marketing area (between 'component02' and the spec section),
    de-duplicated (every one is rendered twice for mobile/desktop), boilerplate dropped. HTML comments are stripped."""
    start, end = page_html.find("component-content component02"), page_html.find('id="compGoodsSpec"')
    if start < 0 or end <= start:
        return []
    area = re.sub(r"<!--.*?-->", " ", page_html[start:end], flags=re.S)
    out: list[str] = []
    for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", area, re.S):
        text = _clean(re.sub(r"<[^>]+>", " ", m.group(1)))
        if text and not _BOILERPLATE_H2.search(text) and text not in out:
            out.append(text)
    return out[:MAX_FEATURES]


def parse_purchase_panel(panel_html: str) -> dict:
    """goodsRevampDetail fragment -> {sale_price, list_price, status, goods_tp} (ints/str or None)."""
    def val(pattern):
        m = re.search(pattern, panel_html)
        return m.group(1) if m else None

    def price(raw):
        return int(raw) if raw and raw.isdigit() and int(raw) > 0 else None
    return {"sale_price": price(val(r'id="salePrc"[^>]*value="(\d+)"')),
            "list_price": price(val(r'id="originalPrice"[^>]*value="(\d+)"')),
            "status": val(r'name="saleStatCd"[^>]*value="(\d+)"'),
            "goods_tp": val(r'name="goodsTpCd"[^>]*value="(\d+)"')}


def parse_manuals(manual_html: str) -> list[dict]:
    """goodsManual fragment -> [{name, lang, url}] for https PDF links on samsung.com hosts, Korean first."""
    docs = []
    for block in re.split(r'<li class="item"', manual_html)[1:]:
        name_m = re.search(r'<strong class="name">(.*?)</strong>', block, re.S)
        name = _clean(re.sub(r"<[^>]+>", " ", name_m.group(1))) if name_m else ""
        items = [_clean(re.sub(r"<[^>]+>", " ", x)) for x in re.findall(r'<span class="desc-item">(.*?)</span>', block, re.S)]
        link = re.search(r'<a href="([^"]+)"[^>]*class="btn-download"', block)
        url = _https(link.group(1)) if link else None
        if url and urlsplit(url).path.lower().endswith(".pdf"):
            docs.append({"name": name, "lang": items[1] if len(items) > 1 else "", "url": url})
    docs.sort(key=lambda d: d["lang"] != "한국어")  # stable: Korean documents first
    return docs


def _doc_type(name: str) -> str:
    n = _norm_name(name)
    if re.search(r"설치|install", n):
        return "Installation"
    if re.search(r"사용자 ?매뉴얼|사용설명서|user manual|manual", n):
        return "Manual"
    return "Other"


# ---------------------------------------------------------------- field mapping
def _n(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def _pick(rows, label_pat: str, section_pat: str | None = None) -> str | None:
    """Value of the first row whose whitespace-free label fully matches label_pat (and section matches, if given)."""
    for sec, label, value in rows:
        if re.fullmatch(label_pat, _n(label)) and (section_pat is None or re.search(section_pat, _n(sec))):
            return value
    return None


def _num(text: str | None, unit_pat: str) -> float | None:
    """First number in text, provided the unit appears in it ('12.2 / 13.5 kg' -> 12.2 = the net figure)."""
    t = unicodedata.normalize("NFKC", text or "")
    if not re.search(unit_pat, t, re.I):
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", t)
    return float(m.group(0).replace(",", "")) if m else None


def _axis_order(context: str) -> tuple[str, str, str]:
    """Axis order of a 'W x H x D' value from its label/section text ('가로 × 세로 × 높이' = W, D, H on cooktops)."""
    toks = [{"가로": "w", "폭": "w", "세로": "d", "깊이": "d", "높이": "h"}[t]
            for t in re.findall(r"가로|폭|세로|깊이|높이", _n(context))]
    if len(toks) == 3 and len(set(toks)) == 3:
        return tuple(toks)  # type: ignore[return-value]
    return ("w", "h", "d")


def _dimensions(rows) -> tuple[float | None, float | None, float | None]:
    """Exterior (w, h, d) in mm: the size/dimension row, never cavity (조리실), cut-out (타공) or package (포장)."""
    best = None
    for sec, label, value in rows:
        ctx = f"{sec} {label}"
        if not re.search(r"크기|치수|외관|외부", _n(ctx)) or re.search(r"조리실|타공|포장|컷|화구|플렉스", _n(label)):
            continue
        nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", unicodedata.normalize("NFKC", value))]
        if len(nums) < 3:
            continue
        if re.search(r"\bcm\b", value, re.I) and "mm" not in value.lower():
            nums = [x * 10 for x in nums]
        order = _axis_order(ctx)
        dims = dict(zip(order, nums[:3]))
        exterior = bool(re.search(r"외관|외부|제품크기|^크기", _n(label)) or re.search(r"크기|치수", _n(sec)))
        if best is None or (exterior and not best[0]):
            best = (exterior, dims)
    if not best:
        return None, None, None
    d = best[1]
    return d.get("w"), d.get("h"), d.get("d")


def _yn(value: str | None) -> bool | None:
    """Korean availability value -> bool; any other descriptive value (e.g. '빅 아이스메이커') means present."""
    v = _n(value or "")
    if not v:
        return None
    if re.match(r"(없음|미지원|해당없음|불가|아니오|X$|No$)", v, re.I):
        return False
    return True


def _wifi(rows) -> tuple[bool | None, str | None]:
    """(supported, evidence = 'Section > Label = Value' in the original Korean)."""
    for sec, label, value in rows:
        if re.fullmatch(r"wi-?fi(내장)?", _n(label), re.I):
            return _yn(value), f"{sec} > {label} = {value}"
    for sec, label, value in rows:
        if re.fullmatch(r"smartthings모바일앱지원|smartthings지원", _n(label), re.I) and _yn(value):
            return True, f"{sec} > {label} = {value}"
    return None, None


def _grade(rows) -> str | None:
    """'KR grade N' (never ENERGY STAR); washtower '세탁 1등급/건조 3 등급' -> 'KR grade 1 (washing) / KR grade 3 (drying)'."""
    value = _pick(rows, r"에너지소비효율(등급)?")
    if not value:
        return None
    parts = re.findall(r"(세탁|건조)\s*(\d)\s*등급", value)
    if parts:
        names = {"세탁": "washing", "건조": "drying"}
        return " / ".join(f"KR grade {g} ({names[k]})" for k, g in parts)
    try:
        return _ko().kr_energy_grade(value)
    except AttributeError:  # ko_en without kr_energy_grade: plain parse
        m = re.search(r"([1-5])\s*등급", value)
        return f"KR grade {m.group(1)}" if m else None


def _monthly_kwh(rows) -> float | None:
    for _, _, value in rows:
        if re.search(r"kWh\s*/\s*월", unicodedata.normalize("NFKC", value)):
            return _num(value, r"kWh")
    return None


def _electrical(rows) -> tuple[str | None, float | None]:
    value = _pick(rows, r"정격전압|전원")
    if not value:
        return None, None
    v = re.search(r"(\d{2,3})\s*V", value)
    hz = re.search(r"(\d{2})\s*Hz", value, re.I)
    return (v.group(1) if v else None), (float(hz.group(1)) if hz else None)


def _in(mm: float | None) -> float | None:
    return None if mm is None else round(units.mm_to_in(mm), 1)


def _label_map(rows) -> dict[str, str]:
    """Spec section/label -> English, ONE ko_en.translate_many(kind='label') batch (glossary, cache, then at most
    one local-LLM call per 40 unknown strings)."""
    texts = list(dict.fromkeys(t for sec, label, _ in rows for t in (sec, label) if t))
    return dict(zip(texts, _ko().translate_many(texts, "label")))


def _table(rows, english: dict[str, str], values: list[str]) -> dict[str, str]:
    """{'Section > Label': translated value} for EVERY row (a repeated key keeps all values, ' | '-joined)."""
    table: dict[str, str] = {}
    for (sec, label, _), val in zip(rows, values):
        key = f"{english[sec]} > {english[label]}" if sec else english[label]
        table[key] = f"{table[key]} | {val}" if key in table else val
    return table


def build_record(url: str, page: dict, panel: dict, rows, manuals: list[dict],
                 section: str | None = None, slug: str | None = None) -> tuple[ProductRecord, list[tuple[str, str]]]:
    """Pure function: parsed PDP + spec rows -> ProductRecord (+ [(doc_type, https url)] to download)."""
    ko = _ko()
    sec_, slug_, mdl = _path_parts(url)
    section, slug = section or sec_, slug or slug_
    model = _model(page["model"] or mdl)
    sub = classify(section, slug, page["name"], model)
    major = catalog.major_of(sub) if sub else _SECTION_MAJOR.get(section)
    if not major:
        raise SamsungKrPageError(f"unsupported Samsung KR product group (section {section!r})")
    fridge, washer = major == "refrigerator", major == "washer"

    std: dict[str, str] = {}

    def put(label, value):
        if value is not None:
            std[label] = value if isinstance(value, str) else (str(int(value)) if float(value) == int(value) else str(value))

    liters = _num(_pick(rows, r"전체용량", r"용량") if fridge else _pick(rows, r"용량", r"용량"), r"[Lℓ리]") \
        if major != "washer" else None
    fresh = _num(_pick(rows, r"냉장실용량"), r"l") if fridge else None
    freezer = _num(_pick(rows, r"냉동실용량"), r"l") if fridge else None
    wash_kg = _num(_pick(rows, r"세탁용량"), r"kg") if washer else None
    dry_kg = _num(_pick(rows, r"건조용량"), r"kg") if washer else None
    w, h, d = _dimensions(rows)
    weight_kg = _num(_pick(rows, r"(제품(/전체)?)?(무게|중량)"), r"kg")
    volt, hz = _electrical(rows)
    monthly = _monthly_kwh(rows)
    annual = ko.annualize(monthly) if monthly is not None else None
    grade = _grade(rows)
    wifi_ok, wifi_ev = _wifi(rows)

    put("Total capacity (L)", liters)
    put("Fresh food capacity (L)", fresh)
    put("Freezer capacity (L)", freezer)
    put("Washer capacity (kg)", wash_kg)
    put("Dryer capacity (kg)", dry_kg)
    put("Width (mm)", w)
    put("Height (mm)", h)
    put("Depth (mm)", d)
    put("Weight (kg)", weight_kg)
    if monthly is not None:
        put("Monthly energy consumption (kWh/month)", monthly)
        put("Annual energy consumption (kWh/year)", annual)
        std["Annual energy basis"] = getattr(ko, "ANNUALIZED_LABEL", "estimated: monthly figure x 12")
    if grade:
        std["KR energy grade"] = grade
    for i, feature in enumerate(page["features"], 1):
        std[f"Feature (KO) {i}"] = feature
    if panel.get("sale_price"):
        put("Sale price (KRW)", panel["sale_price"])
    if panel.get("list_price"):
        put("List price (KRW)", panel["list_price"])
    std["Price basis"] = "samsung.com/sec general sale price (excludes card/membership/coupon benefit prices)"
    std["Source language"] = "ko (labels/values machine-translated; originals in RawSpec)"

    finish_ko = _pick(rows, r"(도어)?색상", r"기본사양|디자인|도어|상판|^구분|외관")
    # ONE value batch for row values + feature headlines + finish (numbers/units are kept by ko_en)
    n_rows, n_feat = len(rows), len(page["features"])
    translated = ko.translate_many([v.replace("ℓ", "L") for _, _, v in rows] + list(page["features"])
                                   + ([finish_ko] if finish_ko else []))
    table = _table(rows, _label_map(rows), translated[:n_rows])
    pod_features = translated[n_rows:n_rows + n_feat]
    finish = translated[n_rows + n_feat] if finish_ko else None
    price = panel.get("sale_price") if panel.get("status") == ON_SALE else None
    docs, seen_types = [], {}
    for m in manuals[:MAX_PDFS]:
        base = _doc_type(m["name"])
        seen_types[base] = seen_types.get(base, 0) + 1
        docs.append((base if seen_types[base] == 1 else f"{base} {seen_types[base]}", m["url"]))

    cap_cuft = round(units.l_to_cuft(liters), 2) if liters is not None else None
    water = next((_yn(v) for _, lbl, v in rows if re.match(r"(정수기|워터디스펜서|디스펜서|정수)", _n(lbl))), None)
    return ProductRecord(
        brand=BRAND, model_number=model, product_name=page["name"], product_url=url, category=major, subcategory=sub,
        door_style=DOOR_STYLES.get(sub) if fridge else None, finish_color=finish,
        region=REGION, country=COUNTRY, currency=CURRENCY, price_usd=None,
        price_local=float(price) if price else None,
        capacity_total_cuft=cap_cuft,
        capacity_fridge_cuft=round(units.l_to_cuft(fresh), 2) if fresh is not None else None,
        capacity_freezer_cuft=round(units.l_to_cuft(freezer), 2) if freezer is not None else None,
        width_in=_in(w), height_in=_in(h), depth_in=_in(d),
        weight_lb=None if weight_kg is None else round(units.kg_to_lb(weight_kg), 1),
        voltage_v=volt, frequency_hz=hz, energy_kwh_year=annual, energy_star=None,
        ice_maker=next((_yn(v) for _, lbl, v in rows if _n(lbl) == "제빙기"), None) if fridge else None,
        water_dispenser=water if fridge else None,
        wifi_supported=wifi_ok, wifi_evidence=wifi_ev,
        pod_features=pod_features,
        rating=page.get("rating"), review_count=page.get("review_count"),
        extra_specs={**table, **std}, image_url=page["image_url"],
    ), docs


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    _require_sec(url)
    section, slug, model_hint = _path_parts(url)
    with _Net(urlsplit(url).path) as net:
        page = parse_pdp(net.get(url, ok=lambda t: "og:title" in t), url)
        post = lambda name, data, ok=None: net.post(XHR_GOODS + name, data, ok=ok, referer=url)
        panel_html = post("goodsRevampDetail", {"goodsId": page["goods_id"], "goodsTpCd": "10", "adminYn": "",
                                                 "adminPriceYn": "", "samsungstore": "", "advPdYn": ""})
        panel = parse_purchase_panel(panel_html)
        if _is_bundle(slug, panel.get("goods_tp")):
            raise SamsungKrPageError(f"{url} is a two-product bundle (goodsTpCd 20), not a single model")
        spec_html = post("getGoodsSpecList", {"goodsId": page["goods_id"], "goodsTpCd": "10", "goodsNm": page["name"],
                                              "adminYn": "", "taskId": "", "taskDtlNo": ""},
                         ok=lambda t: "spec-table" in t)
        rows = parse_spec_html(spec_html)
        manual_html = post("goodsManual", {"mdlCode": page["model"], "goodsId": page["goods_id"], "goodsTpCd": "10",
                                           "manualLang": "KO", "mdlNm": page["model"]})
        manuals = parse_manuals(manual_html)
    product, doc_urls = build_record(url, page, panel, rows, manuals, section, slug)
    fname_model = _safe_model(product.model_number)
    docs = []
    for doc_type, doc_url in doc_urls:
        _throttle()
        d = download_pdf(BRAND, fname_model, doc_type, doc_url)
        if d:
            docs.append(d)
    raw = [RawSpec(brand=BRAND, model_number=product.model_number, source="web", section=s, key=k, value=v)
           for s, k, v in rows]
    return product, docs, raw
