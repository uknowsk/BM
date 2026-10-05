"""SQLite cache for scraped products and candidate listings (stdlib only)."""
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

import catalog
from catalog import Candidate
from schema import DocumentRecord, ModeRecord, ProductRecord, RawSpec

DEFAULT_DB = Path(__file__).parent / "data" / "cache.db"
PRODUCT_TTL = timedelta(days=7)
CANDIDATE_TTL = timedelta(days=1)
# Bump whenever adapter/parser output changes: rows written under another version are ignored (and
# overwritten on the next scrape). '2' invalidates every row cached before the parser fixes.
PARSER_VERSION = "4"  # 4: cooking sub key scr -> sco and reclassified cooking adapters; 3: full spec tables + image_url

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
    url TEXT PRIMARY KEY, brand TEXT, model_number TEXT, spec_json TEXT, docs_json TEXT,
    raw_specs_json TEXT, modes_json TEXT, fetched_at TEXT, content_hash TEXT);
CREATE TABLE IF NOT EXISTS candidates(
    key TEXT PRIMARY KEY, candidates_json TEXT, fetched_at TEXT);
"""


@dataclass
class CachedProduct:
    product: ProductRecord
    documents: list[DocumentRecord]
    raw_specs: list[RawSpec]
    modes: Optional[list[ModeRecord]]  # None = modes never extracted for this entry
    fetched_at: str


def _dump(models) -> str:
    return json.dumps([m.model_dump() for m in models], ensure_ascii=False)


def _load(model, text: str) -> list:
    return [model.model_validate(d) for d in json.loads(text)]


_CORRUPT = (ValueError, ValidationError, json.JSONDecodeError, TypeError)  # ValidationError/JSONDecodeError are ValueErrors


def _stale_sub(sub: Optional[str]) -> bool:
    """True for a stored sub key the catalog no longer knows (e.g. the retired 'scr')."""
    return bool(sub) and not catalog.is_sub(sub)


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c, c:
            c.executescript(_SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(products)")}
            if "parser_version" not in cols:  # migrate pre-versioned caches; their rows (NULL) never match
                c.execute("ALTER TABLE products ADD COLUMN parser_version TEXT")

    def _conn(self) -> sqlite3.Connection:
        # short-lived connection per call: safe across the Streamlit worker thread
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def _fresh(fetched_at: str, ttl: timedelta, now: datetime) -> bool:
        return now - datetime.fromisoformat(fetched_at) <= ttl

    # products
    def put_product(self, url: str, product: ProductRecord, documents, raw_specs,
                    modes: Optional[list[ModeRecord]] = None, now: Optional[datetime] = None) -> None:
        """Store a freshly scraped product (resets fetched_at). Use update_modes to add modes only."""
        now = now or datetime.now()
        spec = product.model_dump_json()
        with closing(self._conn()) as c, c:
            c.execute(
                "INSERT OR REPLACE INTO products(url, brand, model_number, spec_json, docs_json, raw_specs_json,"
                " modes_json, fetched_at, content_hash, parser_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (url, product.brand, product.model_number, spec, _dump(documents), _dump(raw_specs),
                 None if modes is None else _dump(modes), now.isoformat(timespec="seconds"),
                 hashlib.sha256(spec.encode()).hexdigest(), PARSER_VERSION))

    def update_modes(self, url: str, modes: list[ModeRecord]) -> None:
        """Attach extracted modes to an existing row without touching fetched_at of the scraped data."""
        with closing(self._conn()) as c, c:
            c.execute("UPDATE products SET modes_json=? WHERE url=? AND parser_version=?",
                      (_dump(modes), url, PARSER_VERSION))

    def update_product(self, url: str, product: ProductRecord) -> None:
        """Replace the stored ProductRecord (e.g. a re-downloaded image_path) without touching fetched_at."""
        spec = product.model_dump_json()
        with closing(self._conn()) as c, c:
            c.execute("UPDATE products SET spec_json=?, content_hash=? WHERE url=? AND parser_version=?",
                      (spec, hashlib.sha256(spec.encode()).hexdigest(), url, PARSER_VERSION))

    def get_product(self, url: str, ttl: timedelta = PRODUCT_TTL,
                    now: Optional[datetime] = None) -> Optional[CachedProduct]:
        """Fresh cached product, else None. Unreadable/legacy/other-parser-version rows count as a miss."""
        now = now or datetime.now()
        with closing(self._conn()) as c:
            row = c.execute("SELECT spec_json, docs_json, raw_specs_json, modes_json, fetched_at, parser_version "
                            "FROM products WHERE url=?", (url,)).fetchone()
        if row is None or row[5] != PARSER_VERSION:
            return None
        try:
            if not self._fresh(row[4], ttl, now):
                return None
            product = ProductRecord.model_validate_json(row[0])
            if _stale_sub(product.subcategory):
                return None  # retired sub key (e.g. 'scr'): re-scrape
            return CachedProduct(product, _load(DocumentRecord, row[1]),
                                 _load(RawSpec, row[2]), None if row[3] is None else _load(ModeRecord, row[3]), row[4])
        except _CORRUPT:
            return None  # self-healing: the caller re-scrapes and overwrites the bad row

    # candidate listings
    @staticmethod
    def _key(brand: str, category: str, limit: Optional[int] = None, subcategory: Optional[str] = None,
             country: str = "us") -> str:
        suffix = "" if country == "us" else f"|{country}"  # US keys stay as before (existing cache rows remain valid)
        return f"v{PARSER_VERSION}|{brand}|{category}|{subcategory or '*'}|{limit}{suffix}"

    def put_candidates(self, brand: str, category: str, candidates: list[Candidate],
                       now: Optional[datetime] = None, limit: Optional[int] = None,
                       subcategory: Optional[str] = None, country: str = "us") -> None:
        if not candidates:  # an empty listing is usually a transient failure; never pin it for the TTL
            return
        now = now or datetime.now()
        with closing(self._conn()) as c, c:
            c.execute("INSERT OR REPLACE INTO candidates VALUES(?,?,?)",
                      (self._key(brand, category, limit, subcategory, country), _dump(candidates), now.isoformat(timespec="seconds")))

    def get_candidates(self, brand: str, category: str, ttl: timedelta = CANDIDATE_TTL,
                       now: Optional[datetime] = None, limit: Optional[int] = None,
                       subcategory: Optional[str] = None, country: str = "us") -> Optional[list[Candidate]]:
        now = now or datetime.now()
        with closing(self._conn()) as c:
            row = c.execute("SELECT candidates_json, fetched_at FROM candidates WHERE key=?",
                            (self._key(brand, category, limit, subcategory, country),)).fetchone()
        if row is None:
            return None
        try:
            if not self._fresh(row[1], ttl, now):
                return None
            cands = _load(Candidate, row[0])
            return None if any(_stale_sub(c.subcategory) for c in cands) else (cands or None)
        except _CORRUPT:
            return None
