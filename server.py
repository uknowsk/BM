"""Gauge web server: a thin FastAPI layer over service.py (search / classify_bands / collect / export_excel).

Run:   python server.py            (http://127.0.0.1:8765, bound to loopback only)
Mock:  FRIDGE_MOCK=1 python server.py   (built-in fake brands/products/jobs; no network, no LLM)
"""
import hashlib
import importlib.util
import logging
import os
import random
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import catalog  # noqa: E402
import compare_model  # noqa: E402
import filters  # noqa: E402
import service  # noqa: E402
from catalog import Candidate  # noqa: E402
from schema import DocumentRecord, ModeRecord, ProductRecord  # noqa: E402

HOST, DEFAULT_PORT = "127.0.0.1", 8765


def _port(env=os.environ) -> int:
    """FRIDGE_PORT (1024-65535) overrides the default port, e.g. to run a second instance for browser checks."""
    try:
        port = int(env.get("FRIDGE_PORT", DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 1024 <= port <= 65535 else DEFAULT_PORT


PORT = _port()
WEB_DIR = ROOT / "web"
DOWNLOADS_DIR = (ROOT / "downloads").resolve()
IMAGES_DIR = DOWNLOADS_DIR / "images"
IMG_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
IMG_TYPES = {".jpg": "image/jpeg", ".png": "image/png"}
IMG_CACHE = "public, max-age=604800"  # product pictures change rarely; the file name is brand/model
MAX_JOBS = 50
STALE_JOB_S = 30 * 60  # a queued/running job with no progress for this long is declared dead (frees the 409 lock)
logger = logging.getLogger("gauge")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# brand -> allowed registrable domain (https URLs must be on it or a subdomain of it)
BRAND_DOMAINS = {"Samsung": "samsung.com", "LG": "lg.com", "KitchenAid": "kitchenaid.com",
                 "GE": "geappliances.com", "Whirlpool": "whirlpool.com", "Bosch": "bosch-home.com"}
# Extra registrable domains per brand for regional sites (added together with the region's adapter).
EXTRA_DOMAINS = {"LG": {"lge.co.kr"}}
MAX_REGIONS = 4
# Shown in the UI but not runnable yet
COMING_SOON_BRANDS = ["Miele", "AEG"]
MAX_SEARCH_COMBOS = 48  # brand x country x sub-group listings per search job
BROWSER_MODES = {"auto": "1", "headless": "1", "visible": "0"}


def is_mock() -> bool:
    return os.environ.get("FRIDGE_MOCK") == "1"


def output_dir() -> Path:
    return Path(os.environ.get("FRIDGE_OUTPUT_DIR") or service.OUTPUT_DIR)


# ------------------------------------------------------------------ mock data
_MOCK_CATALOG = {
    "Samsung": [
        ("RF18A5101SR", "18 cu. ft. Top Freezer Refrigerator", 749), ("RT21M6215SR", "21 cu. ft. Top Freezer with FlexZone", 899),
        ("RF23A967135", "23 cu. ft. Counter Depth 4-Door Flex", 3299), ("RF28T5001SR", "28 cu. ft. 3-Door French Door", 1799),
        ("RF27T5201SR", "27 cu. ft. Large Capacity 3-Door French Door", 1999), ("RF29A9771SR", "29 cu. ft. Smart 4-Door Flex with AutoFill", 3599),
        ("RS27T5200SR", "27 cu. ft. Side by Side with Touch Screen", 1699), ("RF31CG7400SR", "31 cu. ft. Mega Capacity Bespoke 3-Door", 2899),
        ("RF24BB620012", "24 cu. ft. Bespoke Counter Depth 4-Door", 3499), ("RF22A4121SR", "22 cu. ft. French Door", None),
    ],
    "LG": [
        ("LTCS20020S", "20 cu. ft. Top Freezer Refrigerator", 799), ("LRMVS3006S", "30 cu. ft. Smart InstaView Door-in-Door", 2899),
        ("LRFXC2416S", "24 cu. ft. Counter-Depth MAX French Door", 2599), ("LRYKC2606S", "26 cu. ft. Smart Standard-Depth MAX", 2299),
        ("LRSXS2706S", "27 cu. ft. Side-by-Side with Ice and Water", 1599), ("LMXS28626S", "28 cu. ft. Standard-Depth Smart French Door", 1999),
        ("LRFVS3006S", "30 cu. ft. Smart Wi-Fi Enabled French Door", 2399), ("LF29H8330S", "29 cu. ft. Smart Wi-Fi 4-Door French Door", 3299),
        ("LRFOS3016S", "30 cu. ft. Smart InstaView Craft Ice", 3699), ("LRMDC2306S", "23 cu. ft. Door-in-Door Smart Refrigerator", 2099),
    ],
    "KitchenAid": [
        ("KRFF302EBS", "30 cu. ft. 36\" Multi-Door Freestanding Refrigerator", 3299), ("KRSF705HPS", "24.8 cu. ft. 36\" Side-by-Side", 2899),
        ("KRFC302ESS", "22 cu. ft. 36\" Counter-Depth French Door", 3399), ("KRMF706ESS", "25.8 cu. ft. 36\" Multi-Door with Platinum Interior", 3999),
        ("KBFN506ESS", "25.5 cu. ft. 48\" Built-In Side-by-Side", 7499), ("KRFF507HPS", "25 cu. ft. 36\" Standard-Depth French Door", 3199),
        ("KRBL102ESS", "20 cu. ft. Bottom-Mount Refrigerator", 1899), ("KRFC704FPS", "24.2 cu. ft. 36\" Counter-Depth French Door", 3599),
    ],
}
_MOCK_FAIL = {"LRFOS3016S"}  # one model whose scrape fails, so the failure path is always testable
_POD_POOL = [
    "Wi-Fi connected with SmartThings app control", "Twin Cooling Plus independent evaporators", "FlexZone convertible drawer",
    "Water and ice dispenser with filtered water", "Humidity-controlled crisper drawers keep produce fresh",
    "ENERGY STAR certified", "LED interior lighting", "Door-in-Door access", "Sabbath mode", "Turbo cool rapid chill",
    "Smooth-touch external dispenser", "Craft ice maker", "Precise temperature management", "Air filter freshness",
]
_MOCK_MODES = [
    ("Turbo Cool", "Cooling", "Runs the compressor and fan at maximum for rapid chilling after loading groceries.", None, "Press Power Cool on the display"),
    ("Vacation Mode", "Away", "Turns off the fresh food section light and raises setpoint to save energy while away.", "41-45 F", "Hold Fridge for 3 seconds"),
    ("Sabbath Mode", "Religious", "Disables lights, display and alarms for observance.", None, "Hold Lock and Alarm for 3 seconds"),
    ("Fast Freeze", "Freezing", "Lowers freezer temperature for 24 hours to freeze fresh food quickly.", "-6 to 8 F", "Press Fast Freeze"),
    ("Door Alarm", "Alert", "Sounds if a door stays open longer than 60 seconds.", None, "Enabled by default"),
]


_WASHER_SUBS = set(catalog.sub_keys("washer"))
_FRIDGE5 = set(catalog.sub_keys("refrigerator")) - {"compact"}
_MOCK_SUPPORT = {  # sub keys each mock brand "supports" (gaps exercise the 'unsupported' paths)
    "Samsung": _FRIDGE5 | _WASHER_SUBS | {"microwave", "sco", "otr", "gas_oven", "electric_oven", "induction"},
    "LG": _FRIDGE5 | _WASHER_SUBS | {"microwave", "sco", "otr", "gas_oven", "electric_oven", "induction"},
    "KitchenAid": _FRIDGE5 | {"sco", "otr", "gas_oven", "electric_oven", "induction", "radiant"},
    "GE": _FRIDGE5 | _WASHER_SUBS | set(catalog.sub_keys("cooking")),
    "Whirlpool": _FRIDGE5 | {"compact"} | _WASHER_SUBS | {"microwave", "otr", "gas_oven", "electric_oven", "radiant", "sco"},
    "Bosch": _FRIDGE5 | {"compact", "front_load", "dryer", "gas_oven", "electric_oven", "induction", "radiant", "microwave"},
}
_MOCK_EXTRA = {
    "washer": lambda r: {"Capacity (cu ft)": f"{r.uniform(3.8, 5.8):.1f}", "Spin speed (rpm)": str(r.choice([1100, 1200, 1300, 1400])),
                         "Wash cycles": str(r.randrange(8, 16)), "Steam": r.choice(["Yes", "No"])},
    "microwave": lambda r: {"Capacity (cu ft)": f"{r.uniform(0.9, 2.2):.1f}", "Wattage": str(r.choice([900, 1000, 1100, 1200])),
                            "Power levels": "10"},
    "otr": lambda r: {"Capacity (cu ft)": f"{r.uniform(1.5, 2.1):.1f}", "Wattage": str(r.choice([1000, 1100, 1200])),
                      "Vent CFM": str(r.choice([300, 400, 400]))},
    "oven": lambda r: {"Oven capacity (cu ft)": f"{r.uniform(4.8, 6.9):.1f}", "Burners": str(r.choice([4, 5, 5, 6])),
                       "Fuel": r.choice(["Gas", "Electric"])},
    "sco": lambda r: {"Oven capacity (cu ft)": f"{r.uniform(1.2, 1.8):.1f}", "Microwave power (W)": str(r.choice([900, 1000, 1100])),
                      "Cooking modes": "Microwave, Convection, Speed cook, Air fry"},
    "cooktop": lambda r: {"Cooktop elements": str(r.choice([4, 5])), "Oven capacity (cu ft)": f"{r.uniform(5.0, 6.3):.1f}",
                          "Max element wattage": str(r.choice([2600, 3200, 3700]))},
}
_MOCK_POD = {
    "washer": ["Steam sanitize cycle kills 99.9% of bacteria", "Auto-dispense detergent", "Stainless steel drum", "Wi-Fi app control",
               "Quick Wash 30 min cycle", "Vibration reduction technology", "Stackable with matching dryer", "ENERGY STAR certified",
               "Allergen cycle", "Sensor dry with wrinkle care"],
    "cooking": ["Air Fry mode", "True convection with fan", "Self-clean with steam clean option", "Wi-Fi remote preheat",
                "Center oval griddle burner", "21,000 BTU power boil burner", "Meat probe", "Sabbath mode",
                "Continuous cast-iron grates", "Induction with pan detection", "Speed cook combines microwave and convection",
                "Microwave power 1000W"],
}


def _infer_fridge_sub(name: str) -> str:
    n = name.lower()
    for needle, sub in (("top freezer", "top_freezer"), ("side", "side_by_side"), ("built-in", "built_in"), ("bottom", "bottom_freezer")):
        if needle in n:
            return sub
    return "french_door"


def _mock_rows() -> dict:
    """brand -> [(model, name, price, major, sub, url)]; legacy fridge entries first, then generated ones."""
    out = {}
    for brand in _MOCK_SUPPORT:
        dom = BRAND_DOMAINS[brand]
        rows = [(m, n, p, "refrigerator", _infer_fridge_sub(n), f"https://www.{dom}/us/refrigerators/{m.lower()}/")
                for m, n, p in _MOCK_CATALOG.get(brand, [])]
        for sub in catalog.sub_keys():
            major = catalog.major_of(sub)
            if sub not in _MOCK_SUPPORT[brand] or (major == "refrigerator" and brand in _MOCK_CATALOG):
                continue
            rnd = random.Random(f"{brand}{sub}")
            for i in range(3 if major != "refrigerator" else 2):
                model = f"{brand[:2].upper()}{sub[:3].upper()}{rnd.randrange(100, 999)}"
                price = None if (i == 2 and rnd.random() > 0.5) else float(rnd.randrange(3, 40) * 100 - 1)
                rows.append((model, f"{catalog.label_ko(sub)} {brand} {sub.replace('_', ' ')} {i + 1}", price, major, sub,
                             f"https://www.{dom}/us/{sub.replace('_', '-')}/{model.lower()}/"))
        out[brand] = rows
    return out


_MOCK_ROWS = _mock_rows()


_BRAND_RGB = {"Samsung": (30, 64, 175), "LG": (165, 0, 52), "KitchenAid": (120, 30, 30), "GE": (20, 90, 110),
              "Whirlpool": (60, 60, 60), "Bosch": (190, 40, 40)}


def _mock_image(brand: str, model: str, major: str) -> Optional[str]:
    """Generate (once) a placeholder product picture downloads/images/mock/<model>.png; None without Pillow."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    rel = Path("downloads") / "images" / "mock" / f"{re.sub(r'[^A-Za-z0-9._-]', '_', model)}.png"
    f = ROOT / rel
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        col = _BRAND_RGB.get(brand, (90, 90, 90))
        im = Image.new("RGB", (600, 600), (246, 244, 240))
        d = ImageDraw.Draw(im)
        light = tuple(min(255, c + 150) for c in col)
        if major == "refrigerator":
            d.rounded_rectangle((190, 50, 410, 550), 14, fill=light, outline=col, width=5)
            d.line((190, 210, 410, 210), fill=col, width=5)
            d.line((222, 90, 222, 170), fill=col, width=7)
            d.line((222, 245, 222, 330), fill=col, width=7)
        elif major == "washer":
            d.rounded_rectangle((150, 70, 450, 530), 14, fill=light, outline=col, width=5)
            d.ellipse((205, 210, 395, 400), fill=(235, 238, 240), outline=col, width=8)
            d.ellipse((240, 245, 360, 365), fill=light, outline=col, width=4)
            d.rectangle((175, 98, 425, 135), fill=col)
        else:
            d.rounded_rectangle((100, 120, 500, 500), 14, fill=light, outline=col, width=5)
            d.rectangle((130, 190, 470, 470), fill=(60, 64, 70), outline=col, width=4)
            d.line((150, 165, 450, 165), fill=col, width=8)
            for x in (170, 250, 330, 410):
                d.ellipse((x, 135, x + 22, 157), fill=col)
        d.text((24, 24), f"{brand}  {model}", fill=col)
        d.text((24, 572), "MOCK IMAGE", fill=(140, 140, 140))
        im.save(f, "PNG")
    return rel.as_posix()


_OVEN_MODES = ["Bake", "Convection Bake", "Convection Roast", "Broil", "Convection Broil", "Keep Warm", "Proof"]
_OVEN_RACKS = ["Standard Rack", "Flat Rack", "Extension Rack", "Full-Extension Glide Rack", "Half Rack", "Roasting Rack"]


def _mock_oven_specs(brand: str, r: random.Random) -> dict[str, str]:
    """A 'Section > Label' style spec table (what the real adapters now emit), incl. long multi-value rows."""
    modes = r.sample(_OVEN_MODES, r.randrange(3, 6)) + (["Self Clean"] if r.random() > 0.3 else [])
    if brand == "GE" or r.random() > 0.55:
        modes.append("No Preheat Air Fry")
    x = {"Capacity > Oven capacity (cu ft)": f"{r.uniform(4.8, 6.9):.1f}", "Cooking > Oven Cooking Modes": " | ".join(modes),
         "Racks > Number of oven racks": str(r.choice([2, 3, 3, 4])), "Racks > Rack positions": str(r.choice([5, 6, 7])),
         "Racks > Rack types": " | ".join(r.sample(_OVEN_RACKS, r.randrange(2, 6))),
         "Lighting > Oven light": r.choice(["2 Halogen", "1 Halogen", "LED"]),
         "Cleaning > Oven cleaning": r.choice(["Self Clean", "Steam Clean", "Self Clean | Steam Clean"]),
         "Dimensions > Cutout depth (in)": f"{r.uniform(22, 25):.1f}", "Warranty": "1 year parts and labor",
         "Fuel": r.choice(["Gas", "Electric"]), "Burners": str(r.choice([4, 5, 5, 6]))}
    if r.random() > 0.5:
        x["Features > Sabbath mode"] = "Yes"
    return x


class _MockAdapter:
    def __init__(self, brand: str):
        self.brand = brand
        self.domain = BRAND_DOMAINS.get(brand, "example.com")
        self.SUPPORTED_SUBCATEGORIES = set(_MOCK_SUPPORT.get(brand, set()))

    def discover(self, subcategory: str, limit: int = 30):
        if subcategory not in self.SUPPORTED_SUBCATEGORIES:
            raise ValueError(f"unsupported subcategory {subcategory!r}")
        time.sleep(0.45)
        return [Candidate(brand=self.brand, model_number=m, name=n, url=u, price_usd=p, category=major, subcategory=sub)
                for m, n, p, major, sub, u in _MOCK_ROWS.get(self.brand, []) if sub == subcategory][:limit]

    def scrape(self, url: str):
        time.sleep(0.5)
        rows = {r[5]: r for r in _MOCK_ROWS.get(self.brand, [])}
        if url not in rows:
            raise ValueError("unknown mock product")
        model, name, price, major, sub, _ = rows[url]
        if model in _MOCK_FAIL:
            raise RuntimeError("page layout changed (mock failure)")
        rnd = random.Random(model)
        common = dict(brand=self.brand, model_number=model, product_name=name, product_url=url, price_usd=price,
                      category=major, subcategory=sub,
                      finish_color=rnd.choice(["Stainless Steel", "Black Stainless", "White", "Fingerprint Resistant Silver"]),
                      voltage_v="115 V" if major == "refrigerator" or sub == "microwave" else rnd.choice(["120 V", "240 V"]),
                      amps=15.0, frequency_hz=60.0, energy_star=rnd.random() > 0.35, wifi_supported=rnd.random() > 0.35,
                      wifi_evidence="Works with the SmartThings / ThinQ app" if rnd.random() > 0.5 else None)
        if major == "refrigerator":
            total = round(rnd.uniform(17, 31), 1)
            freezer = round(total * rnd.uniform(0.28, 0.4), 1)
            prod = ProductRecord(
                **common, door_style=rnd.choice(["French Door", "Side by Side", "Top Freezer", "4-Door Flex"]),
                capacity_total_cuft=total, capacity_fridge_cuft=round(total - freezer, 1), capacity_freezer_cuft=freezer,
                width_in=round(rnd.uniform(29.5, 36), 1), height_in=round(rnd.uniform(66, 70.5), 1), depth_in=round(rnd.uniform(28, 36), 1),
                weight_lb=round(rnd.uniform(210, 340)), energy_kwh_year=float(rnd.randrange(340, 720, 5)),
                ice_maker=rnd.random() > 0.2, water_dispenser=rnd.random() > 0.4,
                fridge_temp_range_f="34-44 F", freezer_temp_range_f="-6 to 8 F", pod_features=rnd.sample(_POD_POOL, 5))
        else:
            kind = "washer" if major == "washer" else sub if sub in ("microwave", "otr", "sco") else (
                "cooktop" if sub in ("induction", "radiant") else "oven")
            prod = ProductRecord(
                **common, width_in=round(rnd.uniform(24, 36), 1), height_in=round(rnd.uniform(17, 47), 1),
                depth_in=round(rnd.uniform(24, 30), 1), weight_lb=round(rnd.uniform(60, 280)),
                extra_specs=_mock_oven_specs(self.brand, rnd) if kind == "oven" else _MOCK_EXTRA[kind](rnd),
                pod_features=rnd.sample(_MOCK_POD[major], 5))
        prod.image_path = _mock_image(self.brand, model, major)
        docs = []
        out = DOWNLOADS_DIR / "mock"
        out.mkdir(parents=True, exist_ok=True)
        for dt in ("Manual", "EnergyGuide"):
            f = out / f"{model}_{dt}.pdf"
            if not f.exists():
                f.write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
                              b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
            data = f.read_bytes()
            docs.append(DocumentRecord(brand=self.brand, model_number=model, doc_type=dt, source_url=url + dt.lower() + ".pdf",
                                       local_path=str(f.relative_to(ROOT)), sha256=hashlib.sha256(data).hexdigest(),
                                       size_bytes=len(data), pages=rnd.randrange(8, 96)))
        return prod, docs, []


def _mock_extract_modes(product, documents):
    if catalog.normalize_major(product.category) not in (None, "refrigerator"):
        return []  # the fridge mode list makes no sense for other product groups
    time.sleep(0.4)
    rnd = random.Random("modes" + product.model_number)
    pick = rnd.sample(_MOCK_MODES, rnd.randrange(3, 6))
    doc = next((Path(d.local_path).name for d in documents if d.doc_type == "Manual"), "manual.pdf")
    return [ModeRecord(brand=product.brand, model_number=product.model_number, mode_name=n, category=c, description=d,
                       setting_range=r, how_to_activate=a, source_doc=doc, source_page=rnd.randrange(8, 60))
            for n, c, d, r, a in pick]


def _mock_adapter(brand: str, country: str = "us"):
    if country != "us":
        raise KeyError(f"no mock adapter for {brand}/{country}")  # mock data exists for the US market only
    return _MockAdapter(brand)


def install_mock() -> None:
    catalog.adapter = _mock_adapter
    try:
        import modes as modes_mod
        modes_mod.extract_modes = _mock_extract_modes
    except Exception as exc:  # noqa: BLE001 - modes module is optional for mock UI work
        print(f"[mock] modes module unavailable: {exc}", file=sys.stderr)


if is_mock():
    install_mock()


# ------------------------------------------------------------------ jobs
TERMINAL = ("done", "error", "cancelled")


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.status = "queued"
        self.done, self.total, self.current = 0, 0, ""
        self.items: list[dict] = []
        self.log: list[str] = []
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.cancel = threading.Event()
        self.xlsx_path: Optional[Path] = None
        self.lock = threading.Lock()
        self.stale = False  # set (under lock) when the job was declared dead: its worker may no longer write status/result
        self.thread: Optional[threading.Thread] = None
        self.created = self.last_progress = time.time()

    def mark_running(self, **fields) -> bool:
        """queued -> running (plus optional field updates); refused once stale or terminal."""
        with self.lock:
            if self.stale or self.status in TERMINAL:
                return False
            self.status = "running"
            for k, v in fields.items():
                setattr(self, k, v)
            return True

    def finish(self, status: str, *, result: Optional[dict] = None, error: Optional[str] = None,
               xlsx_path: Optional[Path] = None, settle_items: Optional[str] = None) -> bool:
        """The ONLY way a worker writes its final status/result. Atomic under self.lock and refused when the job
        went stale (expired) or already reached a terminal state, so a late worker can never flip error -> done
        or publish a result after expiry. `settle_items` rewrites still pending/running items to that status."""
        with self.lock:
            if self.stale or self.status in TERMINAL:
                return False
            if settle_items is not None:
                for it in self.items:
                    if it["status"] in ("pending", "running"):
                        it["status"] = settle_items
            if result is not None:
                self.result = result
            if xlsx_path is not None:
                self.xlsx_path = xlsx_path
            self.status, self.error = status, error
            return True

    def add_log(self, msg: str) -> None:
        with self.lock:
            self.log.append(f"{time.strftime('%H:%M:%S')}  {_scrub(msg)}")
            self.last_progress = time.time()

    def snapshot(self) -> dict:
        with self.lock:
            terminal = self.status in TERMINAL
            return {
                "id": self.id, "kind": self.kind, "status": self.status,
                "progress": {"done": self.done, "total": self.total, "current": self.current, "items": list(self.items)},
                "log": list(self.log), "result": self.result if terminal else None, "error": self.error,
                "has_excel": self.xlsx_path is not None and self.xlsx_path.exists(),
            }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s'\"<>|]*|(?<![\w:./])/(?:[\w.~-]+/)+[\w.~-]*")


def _scrub(msg: str) -> str:
    """Strip filesystem paths from text shown to the browser."""
    return _PATH_RE.sub("<path>", msg)


def _public_error(exc: BaseException) -> str:
    """What the UI may see of an exception: its type name only (details go to the server log)."""
    return type(exc).__name__


def _expire_stale(now: float) -> None:
    """Declare long-silent jobs dead. `stale` is set in the same critical section as the error status, and every
    worker write goes through Job.finish/mark_running, so the dead job's thread can never change status/result."""
    for j in JOBS.values():
        with j.lock:
            if j.status in ("queued", "running") and not j.stale and now - j.last_progress > STALE_JOB_S:
                j.stale, j.status = True, "error"
                j.error = "작업이 오랫동안 응답하지 않아 중단되었습니다."
                j.cancel.set()
                logger.error("job %s (%s) marked stale", j.id, j.kind)


def _busy(j: Job) -> bool:
    """A job occupies the single slot while active, and also while it is stale but its thread still lives: the
    stale worker may still be driving the shared browser / env vars, so a new job must not start beside it
    (simplest safe choice; a truly hung thread therefore blocks new jobs until the server is restarted)."""
    return j.status in ("queued", "running") or (j.stale and j.thread is not None and j.thread.is_alive())


def _register(job: Job) -> None:
    with JOBS_LOCK:
        _expire_stale(time.time())
        if any(_busy(j) for j in JOBS.values()):
            raise HTTPException(409, "다른 작업이 실행 중입니다. 끝나거나 취소된 뒤 다시 시도하세요.")
        JOBS[job.id] = job
        for old in sorted(JOBS.values(), key=lambda j: j.created)[:-MAX_JOBS]:
            JOBS.pop(old.id, None)


def _set_browser_env(mode: str) -> None:
    changed = os.environ.get("FRIDGE_BROWSER_MODE") != mode
    os.environ["FRIDGE_HEADLESS"] = BROWSER_MODES[mode]
    os.environ["FRIDGE_BROWSER_MODE"] = mode
    if changed:  # adapters may cache which headless flag worked; that is stale under a new mode
        for name in set(catalog.ADAPTERS.values()):
            reset = getattr(sys.modules.get(name), "reset_browser_mode", None)
            if callable(reset):
                reset()


def _start(job: Job, target, *args) -> None:
    """Run target in a daemon thread; if the thread cannot start, fail the job instead of leaving it queued."""
    try:
        thread = threading.Thread(target=target, args=(job, *args), daemon=True)
        job.thread = thread
        thread.start()
    except Exception as exc:  # noqa: BLE001 - e.g. RuntimeError: can't start new thread
        logger.exception("could not start %s job %s", job.kind, job.id)
        job.thread = None
        job.finish("error", error=_public_error(exc))
        raise HTTPException(500, "작업을 시작하지 못했습니다.")


def _doc_dict(d: DocumentRecord) -> dict:
    out = d.model_dump()
    out["local_path"], out["href"] = None, None
    try:
        p = (ROOT / d.local_path).resolve()
        if p.is_file() and p.is_relative_to(DOWNLOADS_DIR):
            rel = p.relative_to(ROOT).as_posix()
            out["local_path"], out["href"] = rel, "/api/doc?path=" + rel
    except (OSError, ValueError):
        pass
    return out


def _image_src(image_path: Optional[str]) -> Optional[str]:
    """'/api/img/<brand>/<file>' for a stored picture (exists, directly inside downloads/images/<brand>), else None."""
    if not image_path:
        return None
    try:
        f = (ROOT / image_path).resolve()
        rel = f.relative_to(IMAGES_DIR)
    except (OSError, ValueError):
        return None
    if len(rel.parts) != 2 or not f.is_file() or f.suffix.lower() not in IMG_TYPES:
        return None
    return "/api/img/" + "/".join(rel.parts)


def _product_dict(p: ProductRecord) -> dict:
    out = p.model_dump()
    out["image_src"] = _image_src(p.image_path)
    if not host_allowed(p.brand, p.product_url or ""):  # scraped value is rendered as a link: only trusted hosts
        out["product_url"] = None
    return out


def _band_payload(cands: list[Candidate], band_mode: str, thresholds, currency: str = "USD"):
    """Bands for one group; `thresholds` (computed once by _group_thresholds) are what classify_bands applies."""
    bands = service.classify_bands(cands, "custom" if band_mode == "custom" else "preset", thresholds, currency)
    named = [n for n in bands if n != service.UNKNOWN_BAND]
    out = {"budget": [], "mid": [], "premium": [], "unknown": []}
    labels = {"budget": "Budget", "mid": "Mid", "premium": "Premium", "unknown": service.UNKNOWN_BAND}
    for key, name in zip(("budget", "mid", "premium"), named):
        out[key] = [c.model_dump() for c in bands[name]]
        labels[key] = name
    out["unknown"] = [c.model_dump() for c in bands.get(service.UNKNOWN_BAND, [])]
    return out, labels


def _group_thresholds(cands: list[Candidate], req: "SearchReq"):
    if req.band_mode == "custom":
        return sorted(req.thresholds)
    return service.preset_thresholds(cands)  # terciles per major category: computed once, reused for bands + display


def _countries(regions: list[str]) -> list[str]:
    return list(dict.fromkeys(cc for r in regions for cc in catalog.countries_of(r)))


def _search_plan(req: "SearchReq") -> list[tuple[str, str, str]]:
    """(brand, country, sub key) listings to run (supported ones only): brands in request order, then the
    selected regions' countries, then subs in tree order."""
    countries = _countries(req.regions or [catalog.DEFAULT_REGION])
    return [(b, cc, s) for b in req.brands for cc in countries for s in req.subcategories
            if s in catalog.supported(b, cc)]


def _sub_label(sub: str, country: str) -> str:
    return catalog.label_ko(sub) + ("" if country == catalog.DEFAULT_COUNTRY else f" ({country.upper()})")


def _run_search(job: Job, req: "SearchReq") -> None:
    try:
        _set_browser_env(req.browser_mode)
        regions = req.regions or [catalog.DEFAULT_REGION]
        plan = _search_plan(req)
        job.mark_running(total=len(plan))
        cands: list[Candidate] = []
        seen_urls: set[str] = set()
        store = None if is_mock() else _store()
        planned = {(b, s) for b, _, s in plan}
        for brand in req.brands:  # unsupported combos: a log line only (no progress item)
            for sub in req.subcategories:
                if (brand, sub) not in planned:
                    job.add_log(f"{brand}: {catalog.label_ko(sub)} 미지원")
        for i, (brand, cc, sub) in enumerate(plan):
            if job.cancel.is_set():
                break
            label = f"{brand} {_sub_label(sub, cc)}"
            with job.lock:
                job.current = f"{label} 검색 중"
                job.items.append({"label": label, "brand": brand, "subcategory": sub, "country": cc, "status": "running"})
            found, log = service.search([brand], [sub], req.limit, store=store, countries=[cc])
            allowed = [c for c in found if host_allowed(brand, c.url)]
            kept = [c for c in allowed if c.url not in seen_urls]
            seen_urls.update(c.url for c in kept)
            if len(allowed) != len(found):
                log.append((brand, "ok", f"{len(found) - len(allowed)}개 후보는 허용되지 않는 URL이라 제외됨"))
            cands.extend(kept)
            for b, status, msg in log:
                job.add_log(f"{b}: {msg}")
            with job.lock:
                job.done = i + 1
                job.items[-1]["status"] = "failed" if log and log[0][1] == "failed" else "done"
                job.items[-1]["message"] = log[0][2] if log else ""
        if job.cancel.is_set():
            job.finish("cancelled")
            return
        cands = [filters.enrich(c) for c in cands]  # name-derived facts into attrs (attrs_src='name')
        selection = req.filters or {}
        shown = filters.filter_candidates(cands, selection)
        searched_subs = list(dict.fromkeys(s for _, _, s in plan))
        facet_keys = list(dict.fromkeys(g["key"] for s in searched_subs for g in filters.groups_for(s, regions)))
        if len(regions) > 1:
            facet_keys.append("region")
        facets = filters.facet_counts(cands, selection, facet_keys)
        by_major = service.group_by_major(shown)
        ran = {(catalog.major_of(s), catalog.region_of(cc)) for _, cc, s in plan}
        groups = []
        for major in catalog.major_keys():
            for region in regions:
                if (major, region) not in ran:
                    continue
                gc = [c for c in by_major.get(major, []) if c.region == region]
                th = _group_thresholds(gc, req)
                currency = catalog.REGIONS[region]["currency"]
                bands, labels = _band_payload(gc, req.band_mode, th, currency)
                groups.append({"category": major, "label_ko": catalog.label_ko(major), "region": region,
                               "currency": currency, "bands": bands, "labels": labels, "thresholds": th,
                               "total": len(gc)})
        failures = sum(1 for it in job.items if it["status"] == "failed")
        failed_brands = list(dict.fromkeys(it["brand"] for it in job.items if it["status"] == "failed"))
        single = groups[0] if len(groups) == 1 else None  # legacy single-category shape stays populated
        result = {"groups": groups, "bands": single["bands"] if single else None,
                  "labels": single["labels"] if single else None,
                  "thresholds": single["thresholds"] if single else None, "total": len(shown),
                  "failed_brands": failed_brands, "regions": regions, "filters": selection, "facets": facets,
                  "filtered_total": len(shown), "unfiltered_total": len(cands),
                  "region_totals": {r: sum(1 for c in shown if c.region == r) for r in regions}}
        if not cands and failures == len(job.items):
            job.finish("error", result=result, error="모든 브랜드에서 검색에 실패했습니다. 로그를 확인하세요.")
        else:
            job.finish("done", result=result)
    except Exception as exc:  # noqa: BLE001 - surface to UI
        logger.exception("search job %s failed", job.id)
        job.finish("error", error=_public_error(exc))


def _pod_items(products: list[ProductRecord], modes: list[ModeRecord]) -> list:
    import pod
    llm_fn = (lambda _prompt: None) if is_mock() else None  # mock: keyword rules only, never call the local LLM
    return [pod.normalize_pod(p, llm_fn=llm_fn, modes=modes) for p in products]


def _collect_groups(products, docs, modes, job: "Job", raw_specs=()) -> list[dict]:
    """Result grouped by major category (tree order); each group has its own products/documents/modes/POD matrix."""
    import pod
    groups = []
    for major in catalog.major_keys():
        ps = [p for p in products if pod.major_of_product(p) == major]
        if not ps:
            continue
        ids = {(p.brand, p.model_number) for p in ps}
        gm = [m for m in modes if (m.brand, m.model_number) in ids]
        rows, items = [], None
        try:
            items = _pod_items(ps, gm)
            rows = pod.compare_rows(ps, items)
        except Exception as exc:  # noqa: BLE001 - POD table is optional; keep the rest of the results
            logger.exception("pod rows failed for job %s (%s)", job.id, major)
            job.add_log(f"POD 비교 표 생성 실패 ({major}): {_public_error(exc)}")
        compare, canon_stats = [], None
        try:  # the same transposed rows the Excel 'Compare' sheet is built from
            built = compare_model.build_compare(ps, docs, list(raw_specs), gm, items,
                                                image_ref=lambda p: _image_src(p.image_path))
            compare = built.get(major, [])
            canon_stats = getattr(built, "canon_stats", {}).get(major)
        except Exception as exc:  # noqa: BLE001 - UI falls back to the legacy tables
            logger.exception("compare rows failed for job %s (%s)", job.id, major)
            job.add_log(f"비교 표 생성 실패 ({major}): {_public_error(exc)}")
        groups.append({"category": major, "label_ko": catalog.label_ko(major),
                       "products": [_product_dict(p) for p in ps],
                       "documents": [_doc_dict(d) for d in docs if (d.brand, d.model_number) in ids],
                       "modes": [m.model_dump() for m in gm], "pod": {"rows": rows}, "compare": compare,
                       "canon_stats": canon_stats})
    return groups


def _export(job: Job, products, docs, raw_specs, modes, run_log) -> Optional[Path]:
    path = output_dir() / f"search_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    if is_mock():
        import pod
        from excel_writer import write_excel
        items = _pod_items(products, modes)
        return write_excel(products, docs, raw_specs, path, run_log=run_log, modes=modes, pod_items=items,
                           extra_sheets=lambda wb: pod.add_pod_sheets(wb, products, items))
    return service.export_excel(products, docs, raw_specs, modes, run_log, path)


def _run_collect(job: Job, cands: list[Candidate], req: "CollectReq") -> None:
    def progress(done: int, total: int, msg: str) -> None:
        with job.lock:
            job.last_progress = time.time()
            job.done, job.current = done, _scrub(msg)
            idx = done if msg.endswith(": start") else done - 1
            if 0 <= idx < len(job.items):
                job.items[idx]["status"] = ("running" if msg.endswith(": start")
                                            else "failed" if "FAILED" in msg else "done")
        job.add_log(msg)

    try:
        _set_browser_env(req.browser_mode)
        job.mark_running()
        kwargs = {"delay": 0.3} if is_mock() else {}
        products, docs, raw_specs, modes, run_log = service.collect(
            cands, progress_cb=progress, cancel_event=job.cancel, with_modes=req.with_modes,
            store=None if is_mock() else _store(), **kwargs)
        if job.stale:  # expired while scraping: drop everything, write nothing
            return
        cancelled = job.cancel.is_set()
        groups = _collect_groups(products, docs, modes, job, raw_specs) if products else []
        xlsx = None
        if products:
            job.add_log("엑셀 파일 작성 중")
            try:
                xlsx = _export(job, products, docs, raw_specs, modes, run_log)
            except Exception as exc:  # noqa: BLE001 - export failure must not discard the scraped data
                logger.exception("excel export failed for job %s", job.id)
                job.add_log(f"엑셀 파일 작성 실패: {_public_error(exc)}")
        result = {
            "products": [_product_dict(p) for p in products], "documents": [_doc_dict(d) for d in docs],
            "raw_specs": [r.model_dump() for r in raw_specs], "modes": [m.model_dump() for m in modes],
            "pod": {"rows": groups[0]["pod"]["rows"] if len(groups) == 1 else []},  # legacy; see groups
            "groups": groups, "run_log": [list(r) for r in run_log],
        }
        job.finish("cancelled" if cancelled else "done", result=result, xlsx_path=xlsx,
                   settle_items="cancelled" if cancelled else "failed")
    except Exception as exc:  # noqa: BLE001 - surface to UI
        logger.exception("collect job %s failed", job.id)
        job.add_log(f"오류: {_public_error(exc)}")
        job.finish("error", error=_public_error(exc))


_STORE = None


def _store():
    global _STORE
    if _STORE is None:
        from store import DEFAULT_DB, Store
        _STORE = Store(os.environ.get("FRIDGE_DB", DEFAULT_DB))
    return _STORE


# ------------------------------------------------------------------ API models + validation
class SearchReq(BaseModel):
    brands: list[str] = Field(min_length=1, max_length=8)
    subcategories: Optional[list[str]] = Field(None, max_length=32)
    category: Optional[str] = None  # legacy: a major key (all sub keys) or a sub key
    limit: int = Field(30, ge=5, le=100)
    band_mode: str = "auto"
    thresholds: Optional[list[float]] = None
    browser_mode: str = "auto"
    regions: Optional[list[str]] = Field(None, max_length=16)  # default ['na']
    filters: Optional[dict[str, Any]] = None  # filters.py selections; applied to the candidates, never to the listings


class CollectItem(BaseModel):
    brand: str
    url: str
    model_number: str = ""
    name: str = ""
    price_usd: Optional[float] = None
    category: Optional[str] = None  # major key
    subcategory: Optional[str] = None  # sub key
    region: Optional[str] = None  # optional; must agree with `country`
    country: Optional[str] = None  # default 'us'
    price_local: Optional[float] = None
    currency: Optional[str] = Field(None, max_length=3)


class CollectReq(BaseModel):
    urls: list[CollectItem] = Field(min_length=1, max_length=service.MAX_COLLECT)
    with_modes: bool = False
    browser_mode: str = "auto"


def known_brands() -> list[str]:
    return list(BRAND_DOMAINS)


def _config() -> dict:
    return service.load_config()


def enabled_brand_names() -> list[str]:
    return [b["name"] for b in _config()["brands"] if b["name"] in BRAND_DOMAINS or b["name"] in catalog.ADAPTERS]


def _brand_support(region: str = catalog.DEFAULT_REGION) -> dict[str, set[str]]:
    """Ready brands (known + adapter importable, or mock) -> sub keys they currently support in one region."""
    out = {}
    for b in _config()["brands"]:
        name = b["name"]
        if name not in BRAND_DOMAINS:
            continue
        subs: set[str] = set()
        for country in catalog.countries_of(region):
            if country == catalog.DEFAULT_COUNTRY:
                module = catalog.ADAPTERS.get(name, b["module"])
                if not (is_mock() or importlib.util.find_spec(module) is not None):
                    continue
            subs |= catalog.supported(name, country)  # imports the module: a broken adapter counts as not ready
        if subs:
            out[name] = subs
    return out


def _regions_support(regions: list[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in regions:
        for name, subs in _brand_support(r).items():
            out.setdefault(name, set()).update(subs)
    return out


def _parse_regions(value, *, require_enabled: bool, default: Optional[list[str]] = None) -> list[str]:
    """Validated, de-duplicated region keys (1..MAX_REGIONS). `value` is a list, a comma-separated string or None."""
    if value is None:
        return list(default if default is not None else [catalog.DEFAULT_REGION])
    items = [x.strip() for x in value.split(",")] if isinstance(value, str) else list(value)
    regions = list(dict.fromkeys(items))
    if not regions or len(regions) > MAX_REGIONS or any(not catalog.is_region(r) for r in regions):
        raise _bad("지원하지 않는 지역입니다.")
    if require_enabled and any(not _brand_support(r) for r in regions):
        raise _bad("지원하지 않는 지역입니다. (준비 중)")
    return regions


def _bad(msg: str) -> HTTPException:
    return HTTPException(422, msg)


def _check_browser_mode(mode: str) -> None:
    if mode not in BROWSER_MODES:
        raise _bad("browser_mode must be auto, headless or visible")


def host_allowed(brand: str, url: str) -> bool:
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
    except ValueError:
        return False
    doms = ({BRAND_DOMAINS[brand]} if brand in BRAND_DOMAINS else set()) | EXTRA_DOMAINS.get(brand, set())
    return bool(u.scheme == "https" and not u.username and any(host == d or host.endswith("." + d) for d in doms))


# ------------------------------------------------------------------ app
app = FastAPI(title="Gauge", docs_url=None, redoc_url=None, openapi_url=None)
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
SEC_FETCH_OK = ("same-origin", "none")
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'")
DOC_CSP = "sandbox; default-src 'none'"  # served PDFs get no script/plugin/same-origin privileges


def _allowed_hosts() -> tuple:
    return _LOOPBACK_HOSTS + (("testserver",) if os.environ.get("FRIDGE_TESTING") == "1" else ())


def _allowed_origins() -> set:
    return {f"http://{h}:{PORT}" for h in _LOOPBACK_HOSTS}


@app.middleware("http")
async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host not in _allowed_hosts():  # blocks DNS-rebinding style access
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if request.method == "POST":  # CSRF: only our own page (same loopback origin) may mutate state
        if request.headers.get("origin") not in _allowed_origins():
            return JSONResponse({"detail": "forbidden origin"}, status_code=403)
        site = request.headers.get("sec-fetch-site")
        if site is not None and site not in SEC_FETCH_OK:
            return JSONResponse({"detail": "forbidden origin"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        DOC_CSP if request.url.path == "/api/doc" or request.url.path.startswith("/api/img/") else CSP)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def _ordered(subs: set[str]) -> list[str]:
    return [k for k in catalog.sub_keys() if k in subs]


@app.get("/api/brands")
def api_brands(region: Optional[str] = Query(None, max_length=64)):
    regions = _parse_regions(region, require_enabled=False)
    support = _regions_support(regions)
    where = {r: _brand_support(r) for r in catalog.REGIONS}
    out, seen = [], set()
    for b in _config()["brands"]:
        name = b["name"]
        seen.add(name)
        subs = support.get(name, set())
        note = "" if subs else ("어댑터 미설치" if name in BRAND_DOMAINS else "준비 중")
        out.append({"name": name, "enabled": bool(subs), "note": note, "categories": _ordered(subs),
                    "majors": [m for m in catalog.major_keys() if subs & set(catalog.sub_keys(m))],
                    "regions": [r for r in catalog.REGIONS if name in where[r]]})
    out += [{"name": n, "enabled": False, "note": "준비 중", "categories": [], "majors": [], "regions": []}
            for n in COMING_SOON_BRANDS if n not in seen]
    return out


@app.get("/api/categories")
def api_categories(region: Optional[str] = Query(None, max_length=64)):
    support = _regions_support(_parse_regions(region, require_enabled=False))
    out = []
    for major, node in catalog.CATEGORY_TREE.items():
        children = []
        for sub, label in node["children"].items():
            brands = [b for b, subs in support.items() if sub in subs]
            children.append({"key": sub, "label_ko": label, "enabled": bool(brands), "brands": brands})
        out.append({"key": major, "label_ko": node["label_ko"], "enabled": any(c["enabled"] for c in children),
                    "children": children})
    return out


@app.get("/api/regions")
def api_regions():
    order = [b["name"] for b in _config()["brands"]]
    out = []
    for key, node in catalog.REGIONS.items():
        support = _brand_support(key)
        live = [cc for cc in node["countries"]
                if any(catalog.supported(b, cc) for b in support)] if support else []
        out.append({"key": key, "label_ko": node["label_ko"], "enabled": bool(support),
                    "default": key == catalog.DEFAULT_REGION, "countries": list(node["countries"]),
                    "enabled_countries": live, "currency": node["currency"],
                    "brands": [b for b in order if b in support], "note": "" if support else "준비 중"})
    return out


@app.get("/api/filters")
def api_filters(subcategory: str = Query(max_length=64), region: Optional[str] = Query(None, max_length=64)):
    regions = _parse_regions(region, require_enabled=False)
    if not (catalog.is_sub(subcategory) or catalog.is_major(subcategory)):
        raise _bad("지원하지 않는 제품군입니다.")
    return {"subcategory": subcategory, "region": ",".join(regions),
            "groups": filters.groups_for(subcategory, regions)}


@app.get("/api/meta")
def api_meta():
    return {"mock": is_mock(), "max_selected": service.MAX_COLLECT, "max_collect": service.MAX_COLLECT,
            "delay_s": service.POLITE_DELAY_S}


def _resolve_subcategories(req: SearchReq) -> list[str]:
    """Validated sub keys in tree order (legacy `category` expands a major key to all its sub keys)."""
    selectors = list(req.subcategories or [])
    if req.category:
        selectors.append(req.category)
    if not selectors:
        selectors = ["refrigerator"]
    wanted: set[str] = set()
    for sel in selectors:
        if not isinstance(sel, str) or not (catalog.is_sub(sel) or (catalog.is_major(sel) and sel == req.category)):
            raise _bad("지원하지 않는 제품군입니다.")
        wanted.update(catalog.expand(sel))
    return [k for k in catalog.sub_keys() if k in wanted]


@app.post("/api/search")
def api_search(req: SearchReq):
    req.regions = _parse_regions(req.regions, require_enabled=True)
    ready = set(_regions_support(req.regions))
    if any(b not in ready for b in req.brands):
        raise _bad("지원하지 않거나 준비되지 않은 브랜드입니다.")
    req.subcategories = _resolve_subcategories(req)
    try:
        filters.validate_selections(req.filters, req.subcategories, req.regions)
    except ValueError as exc:
        raise _bad(f"필터가 올바르지 않습니다: {exc}")
    if req.band_mode not in ("auto", "custom"):
        raise _bad("band_mode must be auto or custom")
    if req.band_mode == "custom":
        t = req.thresholds
        if not t or len(t) != 2 or not (0 <= t[0] < t[1] <= 1_000_000):
            raise _bad("경계값은 0 이상, 낮은 값 < 높은 값 형태의 숫자 2개여야 합니다.")
    _check_browser_mode(req.browser_mode)
    req.brands = list(dict.fromkeys(req.brands))
    plan = _search_plan(req)
    if not plan:
        raise _bad("선택한 브랜드가 지원하는 소분류가 없습니다.")
    if len(plan) > MAX_SEARCH_COMBOS:
        raise _bad(f"한 번에 검색할 수 있는 브랜드×국가×소분류 조합은 최대 {MAX_SEARCH_COMBOS}개입니다.")
    job = Job("search")
    _register(job)
    _start(job, _run_search, req)
    return {"job_id": job.id}


def _item_category(it: CollectItem) -> tuple[str, str, str]:
    """(major, sub, country) of a collect item. `subcategory` is REQUIRED and the only source of truth: it must be
    known and supported by the brand in the item's country, and the major is derived from it. A client-supplied
    `category` is accepted only if it agrees with the derived major. `country` defaults to 'us' (or the first country
    of a given `region` that supports the brand/sub); a given `region` must agree with the country."""
    if not it.subcategory:
        raise _bad("subcategory가 필요합니다.")
    if not catalog.is_sub(it.subcategory):
        raise _bad(f"알 수 없는 소분류: {it.subcategory[:40]}")
    major = catalog.major_of(it.subcategory)
    if it.category and it.category != major:
        raise _bad("category와 subcategory가 일치하지 않습니다.")
    if it.region is not None and not catalog.is_region(it.region):
        raise _bad("지원하지 않는 지역입니다.")
    country = it.country
    if country is None:
        country = next((cc for cc in catalog.countries_of(it.region)
                        if it.subcategory in catalog.supported(it.brand, cc)), catalog.countries_of(it.region)[0]) \
            if it.region else catalog.DEFAULT_COUNTRY
    if country not in catalog.COUNTRIES:
        raise _bad("지원하지 않는 국가입니다.")
    if it.region is not None and catalog.region_of(country) != it.region:
        raise _bad("region과 country가 일치하지 않습니다.")
    if it.subcategory not in catalog.supported(it.brand, country):
        raise _bad(f"{it.brand}: {catalog.label_ko(it.subcategory)} 미지원 ({country.upper()})")
    return major, it.subcategory, country


@app.post("/api/collect")
def api_collect(req: CollectReq):
    _check_browser_mode(req.browser_mode)
    cands, seen = [], set()
    for it in req.urls:
        if it.brand not in enabled_brand_names():
            raise _bad(f"알 수 없는 브랜드: {it.brand}")
        if not host_allowed(it.brand, it.url):
            raise _bad(f"허용되지 않는 URL입니다 (https + {BRAND_DOMAINS.get(it.brand)} 도메인만): {it.url[:80]}")
        if it.url in seen:
            continue
        seen.add(it.url)
        major, sub, country = _item_category(it)
        currency = it.currency.upper() if it.currency and re.fullmatch(r"[A-Za-z]{3}", it.currency) else catalog.currency_of(country)
        tail = it.url.rstrip("/").rsplit("/", 1)[-1]
        cands.append(Candidate(brand=it.brand, model_number=(it.model_number or tail)[:60], name=(it.name or tail)[:200],
                               url=it.url, price_usd=it.price_usd, category=major, subcategory=sub,
                               region=catalog.region_of(country), country=country, currency=currency,
                               price_local=it.price_local))
    job = Job("collect")
    job.total = len(cands)
    job.items = [{"url": c.url, "label": f"{c.brand} {c.model_number}", "status": "pending"} for c in cands]
    _register(job)
    _start(job, _run_collect, cands, req)
    return {"job_id": job.id}


def _job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    return _job(job_id).snapshot()


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel(job_id: str):
    job = _job(job_id)
    job.cancel.set()
    job.add_log("취소 요청됨 — 진행 중인 제품이 끝나면 멈춥니다")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/excel")
def api_excel(job_id: str):
    job = _job(job_id)
    if job.kind != "collect" or job.xlsx_path is None or not job.xlsx_path.exists():
        raise HTTPException(404, "엑셀 파일이 아직 없습니다.")
    return FileResponse(job.xlsx_path, media_type=XLSX_MIME, filename=job.xlsx_path.name)


@app.get("/api/doc")
def api_doc(path: str):
    try:
        p = (ROOT / path).resolve()
    except (OSError, ValueError):
        raise HTTPException(404, "not found")
    if not p.is_relative_to(DOWNLOADS_DIR) or not p.is_file() or p.suffix.lower() != ".pdf":
        raise HTTPException(404, "not found")
    return FileResponse(p, filename=p.name, content_disposition_type="inline")


@app.get("/api/img/{brand}/{name}")
def api_img(brand: str, name: str):
    """Serve a downloaded product picture: only <downloads/images>/<brand>/<name>.(jpg|png), nothing else."""
    suffix = Path(name).suffix.lower()
    if not (IMG_NAME_RE.fullmatch(brand) and IMG_NAME_RE.fullmatch(name)) or ".." in brand or suffix not in IMG_TYPES:
        raise HTTPException(404, "not found")
    try:
        p = (IMAGES_DIR / brand / name).resolve()
    except (OSError, ValueError):
        raise HTTPException(404, "not found")
    if p.parent.parent != IMAGES_DIR or not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type=IMG_TYPES[suffix], headers={"Cache-Control": IMG_CACHE})


# ------------------------------------------------------------------ classification form (template.py): /api/template/*
TEMPLATE_BODY_LIMIT = 8 * 1024 * 1024  # raw upload <= 5 MB; JSON + base64 adds a third
_TEMPLATE_BOUND: dict = {}             # (template id, job id) -> BoundTemplate (small LRU; rebuilt on demand)
_TEMPLATE_BOUND_MAX = 8
_TEMPLATE_LOCK = threading.Lock()      # one (possibly LLM-assisted) bind at a time
_TEMPLATE_CACHE_LOCK = threading.Lock()
_JOB_ID_RE = re.compile(r"[0-9a-f]{12}")
SAMPLE_GROUPS = ("cooking", "refrigerator")


class TemplateApplyReq(BaseModel):
    job_id: str = Field(min_length=1, max_length=32)


def _require_origin(request: Request) -> None:
    """The guard middleware only checks POST; DELETE (state changing) gets the same same-origin rule here."""
    site = request.headers.get("sec-fetch-site")
    if request.headers.get("origin") not in _allowed_origins() or (site is not None and site not in SEC_FETCH_OK):
        raise HTTPException(403, "forbidden origin")


async def _read_limited(request: Request, limit: int) -> bytes:
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > limit:
        raise _bad("파일이 너무 큽니다. (최대 5 MB)")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _bad("파일이 너무 큽니다. (최대 5 MB)")
        chunks.append(chunk)
    return b"".join(chunks)


def _template_or_404(tid: str):
    import template as tpl_mod
    try:
        return tpl_mod.load(tid)
    except tpl_mod.TemplateError:
        raise HTTPException(404, "양식을 찾을 수 없습니다.")


@app.post("/api/template")
async def api_template_upload(request: Request):
    """Upload a classification form: raw .xlsx bytes (header X-Filename) or JSON {filename, content_base64}."""
    import base64
    import binascii
    import json
    from urllib.parse import unquote

    from starlette.concurrency import run_in_threadpool

    import template as tpl_mod
    body = await _read_limited(request, TEMPLATE_BODY_LIMIT)
    if request.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json":
        try:
            obj = json.loads(body)
            filename = str(obj.get("filename") or "")[:200]
            data = base64.b64decode(obj["content_base64"], validate=True)
        except (ValueError, KeyError, TypeError, AttributeError, binascii.Error):
            raise _bad("JSON 형식이 올바르지 않습니다. (filename, content_base64)")
    else:
        filename, data = unquote(request.headers.get("x-filename", ""))[:200], body
    try:
        tid, tpl = await run_in_threadpool(tpl_mod.save_upload, data, filename)
    except tpl_mod.TemplateError as exc:
        raise _bad(str(exc))
    except Exception:  # noqa: BLE001 - details go to the log, never to the browser
        logger.exception("template upload failed")
        raise HTTPException(500, "양식을 처리하지 못했습니다.")
    return {"template_id": tid, "summary": tpl.summary()}


@app.get("/api/template/sample")
def api_template_sample(group: str = Query("cooking", max_length=24)):
    """The sample form of a product group, generated in memory (never a path taken from the request)."""
    from fastapi import Response

    import make_template_sample
    if group not in SAMPLE_GROUPS:
        raise _bad("지원하지 않는 제품군입니다. (cooking, refrigerator)")
    return Response(make_template_sample.build(group), media_type=XLSX_MIME,
                    headers={"Content-Disposition": f'attachment; filename="gauge_form_sample_{group}.xlsx"'})


@app.get("/api/template/{template_id}")
def api_template_get(template_id: str):
    return {"template_id": template_id, "summary": _template_or_404(template_id).summary()}


@app.delete("/api/template/{template_id}")
def api_template_delete(template_id: str, request: Request):
    import template as tpl_mod
    _require_origin(request)
    if not tpl_mod.valid_id(template_id) or not tpl_mod.delete(template_id):
        raise HTTPException(404, "양식을 찾을 수 없습니다.")
    for key in [k for k in _TEMPLATE_BOUND if k[0] == template_id]:
        _TEMPLATE_BOUND.pop(key, None)
    return {"ok": True}


def _job_records(job_id: str):
    """(products, raw_specs, modes, compare) of a finished collect job, rebuilt from its stored result."""
    from schema import RawSpec
    if not _JOB_ID_RE.fullmatch(job_id or ""):
        raise HTTPException(404, "job not found")
    job = _job(job_id)
    snap = job.snapshot()
    res = snap["result"]
    if job.kind != "collect" or snap["status"] not in ("done", "cancelled") or not res or not res.get("products"):
        raise HTTPException(409, "제품이 수집된 완료 작업만 선택할 수 있습니다.")
    try:
        pf, rf, mf = (set(m.model_fields) for m in (ProductRecord, RawSpec, ModeRecord))
        products = [ProductRecord(**{k: v for k, v in p.items() if k in pf}) for p in res["products"]]
        raw = [RawSpec(**{k: v for k, v in r.items() if k in rf}) for r in res.get("raw_specs") or []]
        modes = [ModeRecord(**{k: v for k, v in m.items() if k in mf}) for m in res.get("modes") or []]
    except Exception:  # noqa: BLE001
        logger.exception("could not rebuild records of job %s", job_id)
        raise HTTPException(500, "수집 결과를 읽지 못했습니다.")
    compare = {g["category"]: g["compare"] for g in res.get("groups") or [] if g.get("compare")}
    return products, raw, modes, compare


def _bound_for(template_id: str, job_id: str):
    """(Template, BoundTemplate) for a stored form and a finished collect job (cached; one bind at a time)."""
    import template as tpl_mod
    tpl = _template_or_404(template_id)
    key = (template_id, job_id)
    with _TEMPLATE_CACHE_LOCK:
        hit = _TEMPLATE_BOUND.get(key)
        if hit is not None:
            return tpl, hit
    products, raw, modes, compare = _job_records(job_id)
    if not _TEMPLATE_LOCK.acquire(blocking=False):
        raise HTTPException(409, "다른 양식 적용이 진행 중입니다. 잠시 뒤 다시 시도하세요.")
    try:
        import pod
        majors = {pod.major_of_product(p) for p in products}
        items = None if majors <= set(compare) else _pod_items(products, modes)
        bound = tpl_mod.bind(tpl, products, raw, modes, items, compare=compare if items is None else None)
    except tpl_mod.TemplateError as exc:
        raise _bad(str(exc))
    except Exception:  # noqa: BLE001
        logger.exception("template bind failed (%s, %s)", template_id, job_id)
        raise HTTPException(500, "양식을 적용하지 못했습니다.")
    finally:
        _TEMPLATE_LOCK.release()
    with _TEMPLATE_CACHE_LOCK:
        _TEMPLATE_BOUND[key] = bound
        while len(_TEMPLATE_BOUND) > _TEMPLATE_BOUND_MAX:
            _TEMPLATE_BOUND.pop(next(iter(_TEMPLATE_BOUND)))
    return tpl, bound


@app.post("/api/template/{template_id}/apply")
def api_template_apply(template_id: str, req: TemplateApplyReq):
    """Organise the products of a finished collect job by the form's rows -> status counts, preview rows, download url."""
    _, bound = _bound_for(template_id, req.job_id)
    out = bound.to_dict()
    out.update(template_id=template_id, job_id=req.job_id,
               download_url=f"/api/template/{template_id}/download?job_id={req.job_id}")
    return out


@app.get("/api/template/{template_id}/download")
def api_template_download(template_id: str, job_id: str = Query(max_length=32)):
    import template_writer
    tpl, bound = _bound_for(template_id, job_id)
    try:
        path = template_writer.write_filled(tpl, bound, output_dir() / f"template_{template_id}_{job_id}.xlsx")
    except Exception:  # noqa: BLE001
        logger.exception("template export failed (%s, %s)", template_id, job_id)
        raise HTTPException(500, "엑셀 파일을 만들지 못했습니다.")
    return FileResponse(path, media_type=XLSX_MIME, filename=f"gauge_form_{datetime.now():%Y%m%d_%H%M%S}.xlsx")


MOCK_TAG = '<script src="js/mock.js"></script>\n'


def _index_html() -> str:
    """index.html; in FRIDGE_MOCK=1 mode the dev-only mock.js tag is injected ahead of app.js (never otherwise)."""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace('<script src="js/app.js"', MOCK_TAG + '<script src="js/app.js"', 1) if is_mock() else html


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def web_index():
    if not (WEB_DIR / "index.html").is_file():
        raise HTTPException(404, "not found")
    return HTMLResponse(_index_html())


@app.get("/js/mock.js", include_in_schema=False)
def web_mock_js():
    if not is_mock() or not (WEB_DIR / "js" / "mock.js").is_file():
        raise HTTPException(404, "not found")
    return FileResponse(WEB_DIR / "js" / "mock.js", media_type="text/javascript")


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web") if WEB_DIR.is_dir() else None


if __name__ == "__main__":
    print(f"Gauge{' (MOCK data)' if is_mock() else ''}: http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
