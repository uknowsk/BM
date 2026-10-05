"""UI-free logic for the brand search app: search, price bands, collection, Excel export."""
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import yaml

import catalog
import common
import features
from catalog import Candidate
from store import Store

logger = logging.getLogger(__name__)
ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"
UNKNOWN_BAND = "Price unknown"
PRESET_BANDS = ("Budget", "Mid", "Premium")
MAX_SELECTED = 10  # default per-band pre-selection cap (UI helper)
# Overall safety cap on ONE collect job (several models per brand are allowed). Raise it here; the API reads it.
MAX_COLLECT = 12
POLITE_DELAY_S = 1.5


def load_config(path: str | Path = ROOT / "brands.yaml") -> dict:
    """Read brands.yaml and register its adapter modules in catalog.ADAPTERS (existing entries win)."""
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    for b in cfg["brands"]:
        catalog.ADAPTERS.setdefault(b["name"], b["module"])
    return cfg


def _tag(cand: Candidate, sub: str, country: str = catalog.DEFAULT_COUNTRY) -> Candidate:
    """Stamp major/sub keys and region/country on a candidate (the queried sub key wins unless the adapter set a
    valid one; the queried country always wins). Non-US candidates default to the country's currency."""
    major = catalog.major_of(sub)
    cand.category = major
    if cand.subcategory is None or catalog.major_of(cand.subcategory) != major:
        cand.subcategory = sub
    cand.country = country
    cand.region = catalog.region_of(country) or cand.region
    if country != catalog.DEFAULT_COUNTRY and cand.currency == "USD":
        cand.currency = catalog.currency_of(country)
    return cand


def _adapter(brand: str, country: str):
    """US goes through catalog.adapter(brand) (single-arg overrides keep working); others by (brand, country)."""
    return catalog.adapter(brand) if country == catalog.DEFAULT_COUNTRY else catalog.adapter(brand, country)


def search(brands: list[str], subcategories: str | list[str] = "refrigerator", limit: int = 30,
           store: Optional[Store] = None, use_cache: bool = True, countries: Optional[list[str]] = None):
    """Discover candidates per brand x sub group; one brand failing does not hide the others.

    `subcategories` is a list of sub keys, or a single key. A single *major* key (legacy category='refrigerator')
    means every sub key of that major the brand supports. Explicitly requested but unsupported combos are skipped
    with a ('skipped', '<label> 미지원') log row. Per brand one ok/partial/failed row summarises the discovery.
    `countries` (default ['us']) adds a country dimension: brand x country x sub. A country with no adapter for the
    brand is silently ignored; an explicit sub that no requested country supports gives one 'skipped' row.
    Returns (candidates, log) with log rows (brand, status, message); candidates carry category, subcategory,
    region and country."""
    selectors = [subcategories] if isinstance(subcategories, str) else list(subcategories)
    countries = list(dict.fromkeys(countries or [catalog.DEFAULT_COUNTRY]))
    found, log = [], []
    for brand in brands:
        try:
            adapters: dict[str, tuple] = {}
            for cc in countries:
                try:
                    ad = _adapter(brand, cc)
                except Exception:  # noqa: BLE001
                    if cc == catalog.DEFAULT_COUNTRY:
                        raise  # a broken US adapter keeps failing the brand, as before
                    logger.info("no usable adapter for %s/%s", brand, cc)
                    continue
                adapters[cc] = (ad, catalog.declared_subcategories(ad))
            got: list[Candidate] = []
            n_ok = n_fail = n_cached = 0
            for sel in selectors:
                for sub in catalog.expand(sel):
                    major = catalog.major_of(sub)
                    runnable = [cc for cc, (_, sup) in adapters.items() if sub in sup]
                    if not runnable:
                        if not catalog.is_major(sel):  # a major selector only means "what the brand has"
                            log.append((brand, "skipped", f"{catalog.label_ko(sub)} 미지원"))
                        continue
                    for cc in runnable:
                        adapter = adapters[cc][0]
                        try:
                            cached = (store.get_candidates(brand, major, limit=limit, subcategory=sub, country=cc)
                                      if (store and use_cache) else None)
                            cands = cached if cached is not None else adapter.discover(sub, limit)
                            if cached is None and store:
                                store.put_candidates(brand, major, cands, limit=limit, subcategory=sub, country=cc)
                        except Exception as exc:  # noqa: BLE001 - isolate per-(brand, country, sub) failure
                            logger.exception("search failed for %s/%s/%s", brand, cc, sub)
                            n_fail += 1
                            last_err = type(exc).__name__
                            continue
                        n_ok += 1
                        n_cached += cached is not None
                        got.extend(_tag(c, sub, cc) for c in cands[:limit])
            got = list({c.url: c for c in reversed(got)}.values())[::-1]  # same URL in two sub lists: keep first
            found.extend(got)
            if n_fail and not n_ok:
                log.append((brand, "failed", last_err))  # details stay server-side; callers get the type name
            elif n_ok or not n_fail:
                msg = f"{len(got)} candidates" + (" (cache)" if n_ok and n_cached == n_ok else "")
                if n_fail:
                    msg += f"; {n_fail} listing(s) failed"
                log.append((brand, "partial" if n_fail else "ok", msg))
        except Exception as exc:  # noqa: BLE001 - isolate per-brand failure
            logger.exception("search failed for %s", brand)
            log.append((brand, "failed", type(exc).__name__))
    return found, log


def group_by_major(candidates: list[Candidate]) -> dict[str, list[Candidate]]:
    """Candidates per major key in tree order (only majors that have candidates)."""
    out = {m: [c for c in candidates if c.category == m] for m in catalog.major_keys()}
    return {m: lst for m, lst in out.items() if lst}


def _price(c: Candidate) -> Optional[float]:
    """The comparable price: USD for North America, else the local-currency list price."""
    return c.price_usd if c.price_usd is not None else c.price_local


def preset_thresholds(candidates: list[Candidate]) -> Optional[list[float]]:
    """Tercile cut prices [t1, t2] of the priced candidates (the single source of truth for 'preset' bands).
    Fewer than 3 priced items cannot fill three bands: 2 items -> [high, high] (Budget + Premium, Mid empty);
    1 item -> None (that item is Mid); 0 -> None."""
    priced = sorted(p for p in map(_price, candidates) if p is not None)
    n = len(priced)
    if n >= 3:
        return [priced[n // 3], priced[2 * n // 3]]
    return [priced[1], priced[1]] if n == 2 else None


def classify_bands(candidates: list[Candidate], mode: str = "preset",
                   thresholds: Optional[list[float]] = None, currency: str = "USD") -> dict[str, list[Candidate]]:
    """Group candidates into price bands.
    'preset': Budget/Mid/Premium by terciles of discovered prices (see preset_thresholds; `thresholds`, when
    given as two cuts, are used instead of recomputing; with <3 priced items some bands stay empty).
    'custom': ascending thresholds t1<t2<..: '< t1', 't1 - t2', ..., '>= tn'.
    No-price candidates go to 'Price unknown' (last; omitted when empty). Prices are USD (price_usd) or, when that
    is absent, the local price; `currency` only changes the custom-band labels ('$' for USD, else 'KRW ' ...)."""
    sym = "$" if currency == "USD" else f"{currency} "
    if mode == "custom":
        cuts = sorted(thresholds or [])
        if cuts:
            names = ([f"< {sym}{cuts[0]:,.0f}"] + [f"{sym}{a:,.0f} - {sym}{b:,.0f}" for a, b in zip(cuts, cuts[1:])]
                     + [f">= {sym}{cuts[-1]:,.0f}"])
        else:
            names = ["All prices"]
    else:
        cuts = list(thresholds) if thresholds and len(thresholds) == 2 else preset_thresholds(candidates)
        names = list(PRESET_BANDS)
    bands: dict[str, list[Candidate]] = {name: [] for name in names}
    unknown = []
    for c in candidates:
        price = _price(c)
        if price is None:
            unknown.append(c)
            continue
        idx = sum(1 for t in cuts if price >= t) if cuts else 1  # no cuts (a lone priced item): Mid
        bands[names[min(idx, len(names) - 1)]].append(c)
    for lst in bands.values():
        lst.sort(key=_price)
    if unknown:
        bands[UNKNOWN_BAND] = unknown
    return bands


def default_selection(bands: dict[str, list[Candidate]], per_band: int, max_total: int = MAX_SELECTED) -> list[str]:
    """URLs of the first `per_band` candidates of each band, capped at max_total overall."""
    return [c.url for lst in bands.values() for c in lst[:per_band]][:max_total]


def _apply_selection(product, cand: Candidate) -> None:
    """The sub group the user picked (cand.subcategory) is authoritative; a differing adapter/PDP value only warns.
    category = major of that sub (the adapter's own label is kept only when it already names the same major)."""
    sub = cand.subcategory or product.subcategory
    if cand.subcategory and product.subcategory and product.subcategory != cand.subcategory:
        logger.warning("%s %s: product page says subcategory %r but the selected one is %r; keeping the selected one",
                       cand.brand, cand.model_number, product.subcategory, cand.subcategory)
    product.subcategory = sub
    product.region, product.country = cand.region, cand.country
    if cand.country != catalog.DEFAULT_COUNTRY and product.currency == "USD":
        product.currency = cand.currency
    if product.price_local is None:
        product.price_local = cand.price_local
    major = (catalog.major_of(sub) if sub else None) or cand.category
    if catalog.normalize_major(product.category) != major:
        product.category = major  # e.g. adapter left the 'Refrigerator' default on a washer


def _ensure_image(product) -> bool:
    """Download product.image_url into downloads/images unless a file is already there. True if image_path changed.
    A failure only logs: a missing picture must never fail the product."""
    if not product.image_url:
        return False
    if product.image_path and (common.ROOT / product.image_path).is_file():
        return False
    try:
        path = common.download_image(product.brand, product.model_number, product.image_url)
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("image download failed for %s %s", product.brand, product.model_number)
        path = None
    if path is None:
        logger.info("no image stored for %s %s", product.brand, product.model_number)
    changed = path != product.image_path
    product.image_path = path
    return changed


def collect(candidates: list[Candidate], progress_cb: Optional[Callable[[int, int, str], None]] = None,
            cancel_event: Optional[threading.Event] = None, with_modes: bool = False,
            store: Optional[Store] = None, delay: float = POLITE_DELAY_S,
            sleep: Callable[[float], None] = time.sleep):
    """Scrape each candidate; per-product failures are logged, not raised.
    Returns (products, documents, raw_specs, modes, run_log); run_log rows are (brand, status, message)."""
    products, documents, raw_specs, modes, run_log = [], [], [], [], []
    total = len(candidates)
    progress = progress_cb or (lambda *_: None)
    scraped_any = False
    for i, cand in enumerate(candidates):
        if cancel_event is not None and cancel_event.is_set():
            run_log.append((cand.brand, "cancelled", f"stopped before {cand.model_number}"))
            break
        label = f"{cand.brand} {cand.model_number}"
        progress(i, total, f"{label}: start")
        try:
            hit = store.get_product(cand.url) if store else None
            if hit is not None:
                product, docs, specs, pmodes, source = hit.product, hit.documents, hit.raw_specs, hit.modes, "cache"
            else:
                if scraped_any:
                    sleep(delay)
                scraped_any = True  # a failed request still counts as having hit the site
                product, docs, specs = _adapter(cand.brand, cand.country).scrape(cand.url)
                pmodes, source = None, "web"
                if product.price_usd is None:
                    product.price_usd = cand.price_usd
            _apply_selection(product, cand)
            features.apply_flags(product, specs)  # features named anywhere in the spec table, not just curated keys
            image_changed = _ensure_image(product)
            modes_new = False
            if with_modes and pmodes is None:
                import modes as modes_mod  # lazy: pulls in PyMuPDF / LLM client
                try:
                    pmodes = modes_mod.extract_modes(product, docs)
                    modes_new = True
                except Exception as exc:  # noqa: BLE001 - modes are best-effort
                    logger.exception("modes extraction failed for %s", label)
                    run_log.append((cand.brand, "partial", f"{label}: modes failed - {type(exc).__name__}"))
            if store and source == "web":
                store.put_product(cand.url, product, docs, specs, pmodes)
            elif store and (modes_new or image_changed):
                if image_changed:
                    store.update_product(cand.url, product)  # keep the scrape's fetched_at (TTL)
                if modes_new:
                    store.update_modes(cand.url, pmodes)
            products.append(product)
            documents.extend(docs)
            raw_specs.extend(specs)
            modes.extend(pmodes or [])
            run_log.append((cand.brand, "ok", f"{label}: {len(docs)} docs, {len(specs)} raw specs ({source})"))
            progress(i + 1, total, f"{label}: done ({source})")
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the others
            logger.exception("collect failed for %s", label)
            run_log.append((cand.brand, "failed", f"{label}: {type(exc).__name__}"))
            progress(i + 1, total, f"{label}: FAILED - {type(exc).__name__}")
    return products, documents, raw_specs, modes, run_log


def export_excel(products, documents, raw_specs, modes, run_log, path: Optional[str | Path] = None) -> Path:
    """Write the workbook (incl. POD sheets) and return its path."""
    import pod
    from excel_writer import write_excel
    path = Path(path) if path else OUTPUT_DIR / f"search_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    items = [pod.normalize_pod(p, modes=modes) for p in products]  # once: shared by Compare and POD sheets
    return write_excel(products, documents, raw_specs, path, run_log=run_log, modes=modes, pod_items=items,
                       extra_sheets=lambda wb: pod.add_pod_sheets(wb, products, items))
